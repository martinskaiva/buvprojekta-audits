import io
import json
import re
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI


st.set_page_config(page_title="Inženiertīklu prasību tests", layout="wide")

st.title("Design Brief inženiertīklu prasību izvilkšanas tests")

st.write(
    "Šī testa aplikācija nolasa `01_VD` sadaļas Design Brief dokumentus no Google Drive "
    "un ar AI palīdzību mēģina izvilkt tikai inženiertīklu / MEP prasības."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"


# ---------------------------------------------------------
# Google Drive
# ---------------------------------------------------------

def get_drive_service():
    service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not service_account_json:
        raise ValueError("Secrets nav atrasts GOOGLE_SERVICE_ACCOUNT_JSON.")

    service_account_info = json.loads(service_account_json)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    return build("drive", "v3", credentials=credentials)


def list_folder_items(service, folder_id: str) -> List[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and trashed = false"

    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name, mimeType, size, modifiedTime)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )

    return results.get("files", [])


def list_items_recursive(service, folder_id: str, parent_path: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    items = list_folder_items(service, folder_id)

    for item in items:
        item_name = item.get("name", "")
        item_path = f"{parent_path}/{item_name}" if parent_path else item_name
        is_folder = item.get("mimeType") == FOLDER_MIME_TYPE

        row = {
            "name": item_name,
            "path": item_path,
            "id": item.get("id"),
            "mimeType": item.get("mimeType"),
            "size": item.get("size", ""),
            "modifiedTime": item.get("modifiedTime", ""),
            "is_folder": is_folder,
        }

        rows.append(row)

        if is_folder:
            rows.extend(
                list_items_recursive(
                    service=service,
                    folder_id=item.get("id"),
                    parent_path=item_path,
                )
            )

    return rows


def find_folder_by_name(items: List[Dict[str, Any]], folder_name: str) -> Optional[Dict[str, Any]]:
    target = folder_name.strip().lower()

    for item in items:
        if (
            item.get("mimeType") == FOLDER_MIME_TYPE
            and item.get("name", "").strip().lower() == target
        ):
            return item

    return None


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


def download_text_file(service, file_id: str) -> str:
    return download_drive_file_bytes(service, file_id).decode("utf-8", errors="replace")


# ---------------------------------------------------------
# Prompt
# ---------------------------------------------------------

def find_prompt_file(prompt_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in prompt_items:
        if item.get("name") == "universal_bp_audit_prompt.txt":
            return item
    return None


def load_universal_prompt(service, prompt_folder_id: str) -> str:
    prompt_items = list_folder_items(service, prompt_folder_id)
    prompt_file = find_prompt_file(prompt_items)

    if not prompt_file:
        return ""

    return download_text_file(service, prompt_file.get("id"))


# ---------------------------------------------------------
# 01_VD / Design Brief dokumenti
# ---------------------------------------------------------

def classify_source_document(row: Dict[str, Any]) -> str:
    name = str(row.get("name", "")).lower()
    path = str(row.get("path", "")).lower()
    text = f"{path} {name}"

    if "design" in text or "brief" in text or "uzdev" in text:
        return "design_brief"

    if "geotechnical" in text or "gi_" in text or "ģeotehn" in text or "geotehn" in text:
        return "geotechnical"

    if "hydrogeology" in text or "hgi" in text or "hidro" in text:
        return "hydrogeology"

    if "tree" in text or "assessment" in text or "koku" in text or "dendro" in text:
        return "tree_assessment"

    if "topo" in text or "topogr" in text:
        return "topography"

    if "photofixation" in text or "photo" in text or "foto" in text:
        return "photofixation"

    return "other_source_data"


def get_source_documents(service, input_folder_id: str) -> pd.DataFrame:
    input_items = list_folder_items(service, input_folder_id)
    vd_folder = find_folder_by_name(input_items, "01_VD")

    if not vd_folder:
        raise ValueError("Nav atrasta `01_VD` mape zem C2-3_TD.")

    vd_rows = list_items_recursive(
        service=service,
        folder_id=vd_folder.get("id"),
        parent_path="01_VD",
    )

    if not vd_rows:
        return pd.DataFrame()

    vd_df = pd.DataFrame(vd_rows)

    docs_df = vd_df[
        (vd_df["is_folder"] == False)
        & (
            vd_df["mimeType"].str.contains("pdf", case=False, na=False)
            | vd_df["mimeType"].str.contains("document", case=False, na=False)
            | vd_df["mimeType"].str.contains("spreadsheet", case=False, na=False)
            | vd_df["mimeType"].str.contains("text", case=False, na=False)
        )
    ].copy()

    if docs_df.empty:
        return docs_df

    docs_df["source_document_type"] = docs_df.apply(
        lambda row: classify_source_document(row.to_dict()),
        axis=1,
    )

    return docs_df


# ---------------------------------------------------------
# PDF teksta izvilkšana
# ---------------------------------------------------------

def extract_pdf_text_blocks(pdf_bytes: bytes, max_pages: int = 50) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = min(len(doc), max_pages)

        for page_index in range(page_count):
            page = doc[page_index]
            blocks = page.get_text("blocks")

            for block_index, block in enumerate(blocks):
                if len(block) < 5:
                    continue

                x0, y0, x1, y1, text = block[:5]
                clean_text = re.sub(r"\s+", " ", str(text)).strip()

                if not clean_text:
                    continue

                rows.append(
                    {
                        "page": page_index + 1,
                        "block_id": block_index,
                        "x0": round(float(x0), 2),
                        "y0": round(float(y0), 2),
                        "x1": round(float(x1), 2),
                        "y1": round(float(y1), 2),
                        "text": clean_text,
                    }
                )

    return pd.DataFrame(rows)


def prepare_text_for_ai(blocks_df: pd.DataFrame, max_blocks: int) -> str:
    if blocks_df.empty:
        return ""

    selected = blocks_df.head(max_blocks)

    chunks = []
    for _, row in selected.iterrows():
        chunks.append(
            f"[page={row['page']} block_id={row['block_id']}] {row['text']}"
        )

    return "\n".join(chunks)


# ---------------------------------------------------------
# OpenAI
# ---------------------------------------------------------

def get_openai_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Secrets nav atrasts OPENAI_API_KEY.")
    return OpenAI(api_key=api_key)


def build_engineering_requirements_prompt(
    universal_prompt: str,
    source_file: str,
    source_text: str,
) -> str:
    return f"""
Tu esi būvprojekta audita palīgs Latvijā.

Tavs uzdevums ir no Design Brief / projektēšanas uzdevuma teksta izvilkt TIKAI inženiertīklu, MEP, tehnisko sistēmu un tehnisko telpu prasības.

Dokumenta fails:
{source_file}

Kontekstam izmanto šo būvprojekta audita principu aprakstu, bet neatkārto to atbildē:
{universal_prompt[:6000]}

GALVENAIS UZDEVUMS:
Izvelc pārbaudāmas prasības, ko vēlāk var salīdzināt ar būvprojekta sadaļām:
- UKT / ārējie ūdensapgādes un kanalizācijas tīkli
- UK / iekšējā ūdensapgāde un kanalizācija
- UK-IUK / iekšējā ugunsdzēsības ūdensapgāde
- AVK / apkure, ventilācija, kondicionēšana, dzesēšana
- SM / siltummezgls, siltumapgāde
- EL / elektroapgāde
- ELT / ārējie elektroapgādes tīkli
- EST / elektronisko sakaru tīkli
- UAS / ugunsdzēsības automātika, vadība
- UATS / ugunsgrēka atklāšanas un trauksmes signalizācija
- ESS / elektronisko sakaru sistēmas, vājstrāvas, drošības sistēmas
- ESS-VAS / vadības automatizācijas sistēmas, BMS
- AR / arhitektūra, ja prasība saistīta ar tehniskām telpām, šahtām, stāvvadiem, piekļuvi, telpu izmēriem
- BK / būvkonstrukcijas, ja prasība saistīta ar iekārtu slodzēm, atvērumiem, šahtām, pamatiem, tehnisko telpu konstrukcijām
- GP / ģenerālplāns, ja prasība saistīta ar pieslēgumiem, ārējiem tīkliem, teritorijas drenāžu, lietusūdeni, EV uzlādi

MEKLĒ ŠĀDAS PRASĪBU GRUPAS:
1. Ūdensapgāde:
- ūdens pieslēgumi
- ūdens patēriņš
- ūdens spiediens
- ūdens uzskaite
- dzeramais ūdens
- karstais ūdens
- recirkulācija
- ūdens kvalitāte
- stāvvadi, šahtas, piekļuve apkopei

2. Kanalizācija:
- sadzīves kanalizācija
- tehnoloģiskā kanalizācija
- tauku atdalītāji
- pretvārsti
- plūdu sūkņi
- pagraba drenāža
- avārijas ūdens novadīšana
- pieslēgumi ārējiem tīkliem

3. Lietusūdens un drenāža:
- lietus kanalizācija
- jumta noteces
- teritorijas drenāža
- zaļo jumtu / terašu / pagalma ūdens novadīšana
- gruntsūdens vai infiltrācijas riski
- ūdens aizturēšana vai uzkrāšana

4. Apkure, dzesēšana, ventilācija:
- siltumapgādes avots
- siltummezgls
- radiatoru/grīdas apkures prasības
- dzesēšanas sistēmas
- ventilācijas iekārtas
- rekuperācija
- trokšņa prasības
- smaku novadīšana
- virtuves/restorānu nosūces
- CO/NOx ventilācija autostāvvietās
- tehnisko telpu ventilācija

5. Elektroapgāde:
- pieslēguma jauda
- transformatoru apakšstacija
- galvenās sadalnes
- rezerves barošana
- UPS
- ģenerators
- elektroenerģijas uzskaite
- dzīvokļu/skaitītāju prasības
- koplietošanas elektroapgāde
- fasādes/apkārtnes apgaismojums
- avārijas/evakuācijas apgaismojums

6. EV un velonovietņu uzlāde:
- elektroauto uzlādes vietas
- cauruļvadi/kabeļkanāli nākotnes uzlādei
- velosipēdu uzlāde
- jaudas rezerve
- skaitītāji un vadība

7. Vājstrāvas, sakari, drošība:
- internets
- TV
- domofoni
- piekļuves kontrole
- videonovērošana
- apsardzes signalizācija
- ugunsgrēka signalizācija
- balss izziņošana
- BMS
- automatizācija
- skaitītāju attālināta nolasīšana

8. Ugunsdrošības inženiersistēmas:
- iekšējā ugunsdzēsība
- sprinkleri, ja minēti
- dūmu novadīšana
- ugunsdrošie vārsti
- E30/E60/E90 kabeļi
- EI/REI šķērsojumi
- vadības algoritmi
- ugunsdzēsības sūkņi
- ūdens padeves drošums

9. Tehniskās telpas un koordinācija:
- tehnisko telpu izvietojums
- tehnisko telpu piekļuve
- apkope
- iekārtu nomaiņas ceļi
- šahtas
- revīzijas lūkas
- stāvvadi
- iekārtu radītais troksnis/vibrācija
- slodzes uz konstrukcijām
- koordinācija ar AR/BK/GP

10. Nosacījuma prasības:
Īpaši meklē "ja/tad" loģiku, piemēram:
- ja ēkā ir transformatoru apakšstacija, tad pagrabā jāparedz plūdu sūknis;
- ja ir komerctelpas/restorāns, tad jāparedz tauku atdalītājs vai atsevišķa ventilācija;
- ja ir EV uzlāde, tad jāparedz jauda, kabeļu ceļi vai rezerves;
- ja ir pazemes autostāvvieta, tad jāparedz CO ventilācija, drenāža, sūkņi vai ugunsdrošības risinājumi.

KO NEIZVILKT:
- dzīvokļu platības, istabu skaitu un interjera prasības, ja tās nav saistītas ar inženiertīkliem;
- fasādes dizainu, apdares materiālus, krāsas, logu tipu, ja nav tehniskas sistēmas prasības;
- vispārīgu frāzi “ievērot normatīvus”, ja nav konkrētas pārbaudāmas inženiertīklu prasības;
- mārketinga aprakstus;
- telpu programmu, ja no tās neizriet inženiertīklu prasība;
- pārāk vispārīgus apgalvojumus, ko nevar vēlāk pārbaudīt BP dokumentos.

SVARĪGI:
- Izvelc tikai tādas prasības, kuru avots ir redzams dokumenta tekstā.
- Neizdomā prasības.
- Ja prasība ir neskaidra, bet potenciāli būtiska, iekļauj to ar zemāku confidence.
- Prasībām jābūt īsām un pārbaudāmām.
- Katru prasību formulē latviski.
- Ja tekstā ir konkrēts skaitlis, vērtība, jauda, daudzums, sistēmas tips vai nosacījums, saglabā to prasībā.
- Vienu prasību nedrīkst sadalīt pārāk sīkās bezjēdzīgās rindās.
- Vienā rindā jābūt vienai pārbaudāmai prasībai.

PRIORITĀTES:
10 = būtiska inženiertīklu prasība, kas ietekmē risinājumu, drošību, apjomu, pieslēgumus, jaudu, ekspluatāciju vai ekspertīzi.
8 = svarīga pārbaudāma tehniska prasība, kas jākoordinē vairākās sadaļās.
6 = pārbaudāma prasība ar vidēju risku.
3 = konteksta fakts, kas var palīdzēt auditā, bet pats par sevi nav būtiska prasība.
1 = fona informācija; parasti neizmantot kā audita piezīmi.

ATBILDES FORMĀTS:
Atbildi tikai kā JSON masīvu.
Ja nav prasību, atgriez [].
Neizmanto Markdown.

Katram objektam jābūt:
- requirement_id: īss ID, piemēram "MEP-REQ-001"
- engineering_system: viens no [
  "water_supply",
  "sewerage",
  "rainwater",
  "drainage",
  "heating",
  "cooling",
  "ventilation",
  "electrical",
  "lighting",
  "low_voltage",
  "BMS",
  "fire_safety",
  "EV_charging",
  "technical_rooms",
  "metering",
  "external_connections",
  "other_engineering"
]
- discipline: viena vai vairākas sadaļas kā saraksts, piemēram ["UK", "UKT", "EL"]
- source_file
- page
- block_id
- requirement: īsa pārbaudāma prasība latviski
- condition: ja ir "ja/tad" nosacījums, ieraksti nosacījumu; citādi tukša virkne
- applies_to_sections: saraksts ar BP sadaļām, kurās vēlāk jāpārbauda atbilstība
- priority
- verification_hint: īsi, kur un kā BP dokumentos pārbaudīt šo prasību
- source_text: īss avota teksta fragments
- confidence: skaitlis no 0 līdz 1
- review_status: "pending"

DOKUMENTA TEKSTS:
{source_text}
"""


def parse_json_array(raw_text: str) -> List[Dict[str, Any]]:
    text = raw_text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    start = text.find("[")
    end = text.rfind("]")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("AI neatgrieza JSON masīvu.")

    json_text = text[start : end + 1]
    data = json.loads(json_text)

    if not isinstance(data, list):
        raise ValueError("AI atbilde nav JSON masīvs.")

    return data


def extract_engineering_requirements_with_ai(
    client: OpenAI,
    universal_prompt: str,
    source_file: str,
    source_text: str,
    model: str,
) -> List[Dict[str, Any]]:
    prompt = build_engineering_requirements_prompt(
        universal_prompt=universal_prompt,
        source_file=source_file,
        source_text=source_text,
    )

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": "Tu atbildi tikai derīgā JSON masīvā. Nekādu paskaidrojumu ārpus JSON.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    raw = response.choices[0].message.content or ""
    return parse_json_array(raw)


# ---------------------------------------------------------
# Excel drošība
# ---------------------------------------------------------

def clean_excel_illegal_chars(value):
    if isinstance(value, str):
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)
    return value


def clean_dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    cleaned = df.copy()

    for col in cleaned.columns:
        cleaned[col] = cleaned[col].map(clean_excel_illegal_chars)

    return cleaned


def make_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    safe_df = clean_dataframe_for_excel(df)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        safe_df.to_excel(writer, sheet_name="engineering_requirements", index=False)

    output.seek(0)
    return output.getvalue()


# ---------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------

input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
prompt_folder_id = st.secrets.get("GOOGLE_DRIVE_PROMPT_FOLDER_ID")

st.markdown("## 1. Konfigurācija")

st.write("Input folder ID:", input_folder_id)
st.write("Prompt folder ID:", prompt_folder_id)

model = st.selectbox(
    "AI modelis",
    options=["gpt-4.1-mini", "gpt-4.1"],
    index=0,
)

max_pages_per_pdf = st.number_input(
    "Maksimālais lapu skaits no viena PDF",
    min_value=1,
    max_value=150,
    value=60,
    step=5,
)

max_blocks_per_pdf = st.number_input(
    "Maksimālais teksta bloku skaits no viena PDF AI analīzei",
    min_value=20,
    max_value=2000,
    value=700,
    step=50,
)

st.info(
    "Šis tests meklē tikai inženiertīklu / MEP prasības. "
    "Sākumā izvēlies 1–5 Design Brief failus, nevis visu komplektu."
)

if "engineering_source_docs_df" not in st.session_state:
    st.session_state.engineering_source_docs_df = pd.DataFrame()

if "engineering_requirements_df" not in st.session_state:
    st.session_state.engineering_requirements_df = pd.DataFrame()

if st.button("1) Atrast Design Brief dokumentus"):
    try:
        drive_service = get_drive_service()
        source_docs_df = get_source_documents(drive_service, input_folder_id)

        if source_docs_df.empty:
            st.warning("Nav atrasti izejas dokumenti 01_VD sadaļā.")
        else:
            design_df = source_docs_df[
                source_docs_df["source_document_type"] == "design_brief"
            ].copy()

            st.session_state.engineering_source_docs_df = design_df

            if design_df.empty:
                st.warning("Nav atrasti Design Brief dokumenti.")
            else:
                st.success(f"Atrasti {len(design_df)} Design Brief dokumenti.")

    except Exception as e:
        st.error("Neizdevās atrast Design Brief dokumentus.")
        st.exception(e)

source_docs_df = st.session_state.engineering_source_docs_df

if not source_docs_df.empty:
    st.markdown("## 2. Atrastie Design Brief dokumenti")

    st.dataframe(
        source_docs_df[
            ["name", "path", "mimeType", "source_document_type", "size", "modifiedTime"]
        ],
        use_container_width=True,
    )

    file_options = source_docs_df["path"].tolist()

    suggested_defaults = [
        path for path in file_options
        if path.lower().endswith("rw designbrief_residential.pdf")
    ]

    selected_paths = st.multiselect(
        "Izvēlies konkrētus Design Brief failus inženiertīklu prasību analīzei",
        options=file_options,
        default=suggested_defaults[:1],
    )

    selected_docs_df = source_docs_df[source_docs_df["path"].isin(selected_paths)].copy()

    st.markdown("### Analīzei izvēlētie faili")
    st.dataframe(
        selected_docs_df[["name", "path", "size"]],
        use_container_width=True,
    )

    if st.button("2) Izvilkt inženiertīklu prasības ar AI"):
        try:
            if selected_docs_df.empty:
                st.warning("Nav izvēlēts neviens fails.")
                st.stop()

            drive_service = get_drive_service()
            universal_prompt = load_universal_prompt(drive_service, prompt_folder_id)

            if not universal_prompt:
                st.warning("Nav izdevies nolasīt universal_bp_audit_prompt.txt. Turpinu bez tā.")

            client = get_openai_client()

            all_requirements: List[Dict[str, Any]] = []

            progress = st.progress(0)
            status = st.empty()

            for idx, (_, doc_row) in enumerate(selected_docs_df.iterrows(), start=1):
                file_name = doc_row["name"]
                file_id = doc_row["id"]
                mime_type = doc_row["mimeType"]

                status.write(f"Apstrādāju: {file_name}")

                if mime_type != PDF_MIME_TYPE:
                    st.warning(f"Izlaižu failu, jo pagaidām apstrādājam tikai PDF: {file_name}")
                    continue

                pdf_bytes = download_drive_file_bytes(drive_service, file_id)

                blocks_df = extract_pdf_text_blocks(
                    pdf_bytes=pdf_bytes,
                    max_pages=int(max_pages_per_pdf),
                )

                if blocks_df.empty:
                    st.warning(f"No PDF neizdevās izvilkt tekstu: {file_name}")
                    continue

                source_text = prepare_text_for_ai(
                    blocks_df=blocks_df,
                    max_blocks=int(max_blocks_per_pdf),
                )

                requirements = extract_engineering_requirements_with_ai(
                    client=client,
                    universal_prompt=universal_prompt,
                    source_file=file_name,
                    source_text=source_text,
                    model=model,
                )

                for req in requirements:
                    req["source_file"] = req.get("source_file") or file_name
                    req["drive_file_id"] = file_id
                    req["drive_path"] = doc_row["path"]

                    if "review_status" not in req or not req["review_status"]:
                        req["review_status"] = "pending"

                all_requirements.extend(requirements)

                progress.progress(idx / len(selected_docs_df))

            if not all_requirements:
                st.info("AI neatrada inženiertīklu prasības izvēlētajos failos.")
                st.session_state.engineering_requirements_df = pd.DataFrame()
            else:
                req_df = pd.DataFrame(all_requirements)

                if "priority" in req_df.columns:
                    req_df["priority"] = pd.to_numeric(
                        req_df["priority"],
                        errors="coerce",
                    ).fillna(0).astype(int)

                if "confidence" in req_df.columns:
                    req_df["confidence"] = pd.to_numeric(
                        req_df["confidence"],
                        errors="coerce",
                    ).fillna(0)

                st.session_state.engineering_requirements_df = req_df
                st.success(f"Izvilktas {len(req_df)} inženiertīklu prasības.")

        except Exception as e:
            st.error("Kļūda, izvelkot inženiertīklu prasības ar AI.")
            st.exception(e)

requirements_df = st.session_state.engineering_requirements_df

if not requirements_df.empty:
    st.markdown("## 3. AI izvilktās inženiertīklu prasības")

    preferred_cols = [
        "requirement_id",
        "engineering_system",
        "discipline",
        "source_file",
        "page",
        "block_id",
        "requirement",
        "condition",
        "applies_to_sections",
        "priority",
        "verification_hint",
        "source_text",
        "confidence",
        "review_status",
        "drive_path",
    ]

    existing_cols = [col for col in preferred_cols if col in requirements_df.columns]
    other_cols = [col for col in requirements_df.columns if col not in existing_cols]

    edited_df = st.data_editor(
        requirements_df[existing_cols + other_cols],
        use_container_width=True,
        num_rows="dynamic",
        key="engineering_requirements_editor",
    )

    st.session_state.engineering_requirements_df = edited_df

    excel_bytes = make_excel_bytes(edited_df)

    st.download_button(
        label="Lejupielādēt inženiertīklu prasību tabulu Excel formātā",
        data=excel_bytes,
        file_name="c2_3_engineering_requirements_test.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
