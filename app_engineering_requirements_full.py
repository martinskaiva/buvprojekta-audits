import io
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI


st.set_page_config(page_title="Pilna MEP prasību analīze", layout="wide")

st.title("Pilna Design Brief inženiertīklu prasību analīze")

st.write(
    "Šī aplikācija analizē Design Brief dokumentus pa lapu grupām un izvelk visas "
    "inženiertīklu / MEP prasību kandidātes. Mērķis nav kopsavilkums, bet pilns "
    "prasību reģistra kandidātu saraksts."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"


# =========================================================
# Google Drive
# =========================================================

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


# =========================================================
# Prompt mape
# =========================================================

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


# =========================================================
# 01_VD / Design Brief dokumenti
# =========================================================

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


# =========================================================
# PDF teksta izvilkšana
# =========================================================

def extract_pdf_page_blocks(pdf_bytes: bytes, max_pages: int) -> Tuple[pd.DataFrame, int]:
    rows: List[Dict[str, Any]] = []

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        total_pages = len(doc)
        page_count = min(total_pages, max_pages)

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

    return pd.DataFrame(rows), total_pages


def make_page_batches(
    blocks_df: pd.DataFrame,
    pages_per_batch: int,
    max_blocks_per_batch: int,
) -> List[Dict[str, Any]]:
    if blocks_df.empty:
        return []

    pages = sorted(blocks_df["page"].dropna().unique().tolist())
    batches: List[Dict[str, Any]] = []

    for start_index in range(0, len(pages), pages_per_batch):
        batch_pages = pages[start_index:start_index + pages_per_batch]
        batch_df = blocks_df[blocks_df["page"].isin(batch_pages)].copy()

        if max_blocks_per_batch > 0:
            batch_df = batch_df.head(max_blocks_per_batch)

        text_lines = []
        for _, row in batch_df.iterrows():
            text_lines.append(
                f"[page={row['page']} block_id={row['block_id']}] {row['text']}"
            )

        batch_text = "\n".join(text_lines).strip()

        if not batch_text:
            continue

        batches.append(
            {
                "start_page": min(batch_pages),
                "end_page": max(batch_pages),
                "pages": batch_pages,
                "blocks_count": len(batch_df),
                "text": batch_text,
            }
        )

    return batches


# =========================================================
# OpenAI
# =========================================================

def get_openai_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Secrets nav atrasts OPENAI_API_KEY.")
    return OpenAI(api_key=api_key)


def build_full_engineering_requirements_prompt(
    universal_prompt: str,
    source_file: str,
    batch_start_page: int,
    batch_end_page: int,
    source_text: str,
    strictness_level: int,
) -> str:
    return f"""
Tu esi būvprojekta Design Brief prasību reģistra sagatavošanas asistents Latvijā.

ŠIS NAV KOPSAVILKUMA UZDEVUMS.
ŠIS NAV "atrodi būtiskāko" UZDEVUMS.
ŠIS IR PILNA PRASĪBU REĢISTRA KANDIDĀTU IZVILKŠANAS UZDEVUMS.

Tev jāizvelk VISAS inženiertīklu / MEP / tehnisko sistēmu / tehnisko telpu prasību kandidātes no dotā dokumenta fragmenta.

Dokuments:
{source_file}

Analizējamās lapas:
{batch_start_page}–{batch_end_page}

Audita konteksts, ko drīksti izmantot kā vispārīgu orientieri:
{universal_prompt[:5000]}

PAMATPRINCIPS:
- Neapkopo.
- Nesamazini.
- Neizvēlies tikai svarīgākās prasības.
- Neapvieno dažādas prasības vienā rindā, ja tās var pārbaudīt atsevišķi.
- Ja vienā rindkopā ir 5 tehniskas prasības, izveido 5 atsevišķus JSON objektus.
- Ja prasība šķiet sīka, bet ir pārbaudāma, iekļauj to ar zemāku priority.
- Dublikātus pagaidām neatmet. Dublikātus vēlāk apstrādās atsevišķs solis.
- Prasību kandidātēm jābūt pēc iespējas pilnīgām.
- Labāk iekļaut vairāk kandidātu ar zemāku priority nekā palaist garām potenciālu prasību.

Meklē ne tikai vārdus "shall", "must", "required", "provide", bet arī:
- should
- allow for
- to be designed
- to be coordinated
- to be provided
- to be installed
- to be connected
- to be routed
- to be located
- to be accessible
- jāparedz
- jānodrošina
- jāprojektē
- jāuzstāda
- jāizvieto
- jāpieslēdz
- jāņem vērā
- nepieciešams
- prasība
- paredzēt
- nodrošināt
- saskaņot
- izstrādāt
- izbūvēt
- pieslēgt
- uzrādīt
- nodrošināma piekļuve

IZVELKAMĀS PRASĪBU GRUPAS:

1. Ūdensapgāde:
- ūdens pieslēgumi
- iekšējā ūdensapgāde
- ārējā ūdensapgāde
- dzeramais ūdens
- karstais ūdens
- recirkulācija
- ūdens spiediens
- ūdens patēriņš
- ūdens uzskaite
- skaitītāji
- stāvvadi
- šahtas
- revīzijas piekļuve
- tehniskās telpas ūdens sistēmām
- ūdens kvalitāte
- ūdensapgāde komerctelpām
- ūdensapgāde velonovietnēm, tehniskām telpām, pagalmiem, terasēm

2. Kanalizācija:
- sadzīves kanalizācija
- tehnoloģiskā kanalizācija
- komerctelpu kanalizācija
- restorānu / virtuves kanalizācija
- tauku atdalītāji
- pagraba kanalizācija
- pretvārsti
- sūkņi
- plūdu sūkņi
- drenāžas sūkņi
- avārijas ūdens savākšana
- kanalizācijas stāvvadi
- revīzijas lūkas
- apkopes piekļuve
- pieslēgumi ārējiem tīkliem

3. Lietusūdens un drenāža:
- jumta lietusūdens novadīšana
- sifoniskā lietusūdens sistēma
- zaļo jumtu drenāža
- terašu drenāža
- pagalma drenāža
- pazemes autostāvvietas drenāža
- teritorijas lietus kanalizācija
- lietusūdens aizturēšana
- lietusūdens uzkrāšana
- infiltrācija
- gruntsūdens riski
- noteces apsilde
- noteku aizsalšanas novēršana

4. Apkure:
- apkures sistēmas
- siltummezgls
- siltumapgādes pieslēgumi
- radiatoru apkure
- grīdas apkure
- dvieļu žāvētāji
- komerctelpu apkure
- koplietošanas telpu apkure
- tehnisko telpu apkure
- temperatūras uzturēšana
- regulācija
- uzskaite

5. Dzesēšana:
- dzesēšanas sistēmas
- dzīvokļu dzesēšana
- komerctelpu dzesēšana
- tehnisko telpu dzesēšana
- dzesēšanas jauda
- ārējo bloku izvietojums
- kondensāta novadīšana
- troksnis
- piekļuve apkopei

6. Ventilācija:
- dzīvokļu ventilācija
- komerctelpu ventilācija
- pagraba ventilācija
- pazemes autostāvvietas ventilācija
- CO/NOx kontrole
- virtuves nosūces
- restorānu nosūces
- smaku novadīšana
- tehnisko telpu ventilācija
- atkritumu telpu ventilācija
- rekuperācija
- gaisa pieplūde
- dūmu novadīšana, ja tekstā minēta
- trokšņa un vibrāciju ierobežošana

7. Elektroapgāde:
- pieslēguma jauda
- transformatoru apakšstacija
- galvenā sadalne
- apakšsadales
- dzīvokļu elektroapgāde
- komerctelpu elektroapgāde
- koplietošanas telpu elektroapgāde
- tehnisko iekārtu elektroapgāde
- rezerves barošana
- UPS
- ģenerators
- elektroenerģijas uzskaite
- kabeļu trases
- kabeļu šahtas
- iekārtu piekļuve
- zemējums
- zibensaizsardzība

8. Apgaismojums:
- iekšējais apgaismojums
- koplietošanas telpu apgaismojums
- fasādes apgaismojums
- teritorijas apgaismojums
- avārijas apgaismojums
- evakuācijas apgaismojums
- autostāvvietu apgaismojums
- vadība
- sensori
- dienasgaismas / kustības sensori
- apgaismojuma līmeņi, ja minēti

9. EV charging un velonovietņu uzlāde:
- elektroauto uzlāde
- uzlādes vietu skaits
- kabeļu kanāli nākotnes uzlādei
- jaudas rezerve
- uzlādes vadība
- skaitītāji
- velosipēdu uzlāde
- e-bike uzlāde
- elektrisko skrejriteņu uzlāde
- ugunsdrošības vai ventilācijas prasības uzlādei, ja minētas

10. Vājstrāvas, sakari, drošība:
- internets
- TV
- datu tīkli
- optika
- telekomunikācijas
- domofoni
- piekļuves kontrole
- videonovērošana
- apsardzes signalizācija
- durvju vadība
- stāvvietu vadība
- Wi-Fi
- vājstrāvu skapji
- serveru/tīklu telpas
- kabeļu ceļi
- dzīvokļu sakaru pieslēgumi

11. BMS / automatizācija / skaitītāji:
- BMS
- vadības automatizācija
- attālināta skaitītāju nolasīšana
- enerģijas monitorings
- patēriņa uzskaite
- apkures/dzesēšanas/ventilācijas vadība
- signalizācija par avārijām
- tehnisko sistēmu integrācija
- centralizēta kontrole

12. Ugunsdrošības inženiersistēmas:
- ugunsgrēka signalizācija
- ugunsgrēka atklāšana
- trauksmes izziņošana
- evakuācijas vadība
- dūmu novadīšana
- dūmu kontrole
- sprinkleru sistēma, ja minēta
- iekšējā ugunsdzēsības ūdensapgāde
- hidranti, ja minēti
- ugunsdzēsības sūkņi
- ugunsdrošie kabeļi
- ugunsdrošie šķērsojumi
- automātikas algoritmi

13. Tehniskās telpas, šahtas un piekļuve:
- siltummezgla telpa
- ūdens ievada telpa
- elektro telpa
- transformatoru telpa
- vājstrāvu telpa
- ventilācijas iekārtu telpas
- atkritumu telpas
- sūkņu telpas
- tehnisko telpu izmēri
- piekļuve apkopei
- iekārtu nomaiņas ceļi
- šahtas
- revīzijas lūkas
- griestu zonas
- stāvvadi
- koordinācija ar AR un BK
- iekārtu slodzes uz konstrukcijām
- atvērumi konstrukcijās
- troksnis un vibrācijas

14. Ārējie pieslēgumi un koordinācija ar GP:
- ārējais ūdens pieslēgums
- ārējā sadzīves kanalizācija
- ārējā lietus kanalizācija
- ārējie elektro tīkli
- sakaru pieslēgumi
- siltumtīklu pieslēgumi
- pieslēgumu vietas
- esošo tīklu aizsardzība
- pārbūves
- izbūves secība
- tehniskie noteikumi

15. Nosacījuma prasības:
Īpaši izvelc "ja/tad" prasības, piemēram:
- ja ir transformatoru apakšstacija, tad jāparedz plūdu sūknis;
- ja ir restorāns/komercvirtuve, tad jāparedz tauku atdalītājs vai atsevišķa ventilācija;
- ja ir pazemes autostāvvieta, tad jāparedz ventilācija, drenāža, CO kontrole vai ugunsdrošības risinājumi;
- ja ir EV uzlāde, tad jāparedz jauda, kabeļceļi, vadība vai rezerves;
- ja ir zaļais jumts, tad jāparedz drenāža un ūdens novadīšana;
- ja ir atkritumu telpa, tad jāparedz ventilācija, ūdens vai kanalizācija, ja tekstā tas izriet.

KO NEIZVILKT:
- arhitektūras, interjera vai platību prasības, ja no tām neizriet MEP prasība;
- fasādes materiālus, krāsas, logu tipus, ja nav MEP saistības;
- vispārīgu frāzi "ievērot normatīvus", ja nav konkrētas tehniskas prasības;
- mārketinga tekstus;
- tikai dokumentu nosaukumus;
- vispārīgu aprakstu bez pārbaudāmas prasības;
- prasības, kurām nav redzama avota tekstā.

PRASĪBAS DETALIZĀCIJA:
- Prasībai jābūt īsai, bet ne kopsavilkuma līmeņa.
- Saglabā konkrētos skaitļus, vietas, telpas, sistēmas, daudzumus, jaudas, nosacījumus.
- Viena prasība = viena pārbaudāma doma.
- Ja teksts saka "jāparedz X un Y", izveido divas prasības, ja X un Y vēlāk pārbaudāmi atsevišķi.
- Ja teksts saka "sistēmai jānodrošina A, B, C", izveido atsevišķas prasības A, B un C.
- Ja teksts tikai apraksta telpu, bet no tā skaidri izriet MEP vajadzība, iekļauj kā prasību kandidāti ar zemāku confidence.

PRASĪBU SLIEKSNIS:
Lietotāja izvēlētais stingrības līmenis: {strictness_level}

Interpretācija:
- 1 = ļoti plaši, iekļaut arī sīkas un netiešas MEP prasību kandidātes.
- 3 = plaši, iekļaut vairumu pārbaudāmu MEP prasību kandidāšu.
- 6 = vidēji stingri, iekļaut skaidras MEP prasības.
- 8 = stingri, tikai ļoti skaidras un būtiskas MEP prasības.
- 10 = tikai kritiskas prasības.

Šajā testā prioritāte ir pilnīgums. Ja šaubies starp iekļaut/neiekļaut, iekļauj ar zemāku priority un confidence.

PRIORITĀTES:
10 = būtiska prasība, kas ietekmē risinājumu, drošību, pieslēgumu, jaudu, ekspluatāciju, apjomu vai ekspertīzi.
8 = svarīga tehniska prasība, kas jāsaskaņo vairākās sadaļās.
6 = skaidri pārbaudāma MEP prasība ar vidēju risku.
3 = MEP konteksta fakts vai mazāka prasība, kas vēlāk var noderēt.
1 = fona informācija; parasti vēlāk noraidāma, bet saglabājama kā kandidāts, ja ir MEP saistība.

ATBILDES FORMĀTS:
Atbildi tikai kā JSON masīvu.
Neizmanto Markdown.
Ja nav nevienas MEP prasības, atgriez [].

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
- requirement
- condition
- applies_to_sections
- priority
- verification_hint
- source_text
- confidence
- review_status

review_status vienmēr ir "pending".

DOKUMENTA FRAGMENTS:
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

    json_text = text[start:end + 1]
    data = json.loads(json_text)

    if not isinstance(data, list):
        raise ValueError("AI atbilde nav JSON masīvs.")

    return data


def extract_full_engineering_requirements_with_ai(
    client: OpenAI,
    universal_prompt: str,
    source_file: str,
    batch_start_page: int,
    batch_end_page: int,
    source_text: str,
    model: str,
    strictness_level: int,
) -> List[Dict[str, Any]]:
    prompt = build_full_engineering_requirements_prompt(
        universal_prompt=universal_prompt,
        source_file=source_file,
        batch_start_page=batch_start_page,
        batch_end_page=batch_end_page,
        source_text=source_text,
        strictness_level=strictness_level,
    )

    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu atbildi tikai derīgā JSON masīvā. "
                    "Nekādu paskaidrojumu ārpus JSON. "
                    "Nekādus kopsavilkumus. Izvelc visas prasību kandidātes."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    raw = response.choices[0].message.content or ""
    return parse_json_array(raw)


# =========================================================
# Excel drošība
# =========================================================

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


def normalize_list_columns_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    normalized = df.copy()

    for col in normalized.columns:
        normalized[col] = normalized[col].apply(
            lambda value: ", ".join(value) if isinstance(value, list) else value
        )

    return normalized


def make_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()

    safe_df = normalize_list_columns_for_excel(df)
    safe_df = clean_dataframe_for_excel(safe_df)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        safe_df.to_excel(writer, sheet_name="engineering_requirements", index=False)

    output.seek(0)
    return output.getvalue()


# =========================================================
# Rezultātu sakārtošana
# =========================================================

def add_batch_metadata(
    requirements: List[Dict[str, Any]],
    source_file: str,
    drive_file_id: str,
    drive_path: str,
    batch_start_page: int,
    batch_end_page: int,
    batch_index: int,
) -> List[Dict[str, Any]]:
    enriched = []

    for item_index, req in enumerate(requirements, start=1):
        req = dict(req)

        req["source_file"] = req.get("source_file") or source_file
        req["drive_file_id"] = drive_file_id
        req["drive_path"] = drive_path
        req["batch_index"] = batch_index
        req["batch_start_page"] = batch_start_page
        req["batch_end_page"] = batch_end_page

        if not req.get("requirement_id"):
            req["requirement_id"] = f"MEP-REQ-B{batch_index:03d}-{item_index:03d}"

        if not req.get("review_status"):
            req["review_status"] = "pending"

        enriched.append(req)

    return enriched


def postprocess_requirements_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    result = df.copy()

    if "priority" in result.columns:
        result["priority"] = pd.to_numeric(result["priority"], errors="coerce").fillna(0).astype(int)

    if "confidence" in result.columns:
        result["confidence"] = pd.to_numeric(result["confidence"], errors="coerce").fillna(0)

    if "requirement" in result.columns:
        result["requirement"] = result["requirement"].astype(str).str.strip()

    if "source_text" in result.columns:
        result["source_text"] = result["source_text"].astype(str).str.strip()

    return result


# =========================================================
# Streamlit UI
# =========================================================

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
    max_value=300,
    value=100,
    step=5,
)

pages_per_batch = st.number_input(
    "Lapas vienā AI pieprasījumā",
    min_value=1,
    max_value=10,
    value=2,
    step=1,
)

max_blocks_per_batch = st.number_input(
    "Maksimālais teksta bloku skaits vienā AI pieprasījumā",
    min_value=20,
    max_value=1500,
    value=350,
    step=25,
)

strictness_level = st.slider(
    "Prasību izvilkšanas stingrība",
    min_value=1,
    max_value=10,
    value=2,
    step=1,
    help=(
        "1 = ļoti plaši, daudz kandidātu; "
        "6 = tikai skaidras prasības; "
        "10 = tikai kritiskās prasības. "
        "Pilnam prasību reģistram ieteicams 1–3."
    ),
)

delay_between_ai_calls = st.number_input(
    "Pauze starp AI pieprasījumiem sekundēs",
    min_value=0.0,
    max_value=5.0,
    value=0.5,
    step=0.5,
)

st.info(
    "Pilnajai analīzei rīks analizē PDF pa lapu grupām. "
    "Tas būs lēnāk un dārgāk, bet izvilks daudz vairāk prasību kandidāšu. "
    "Šajā režīmā kopsavilkuma prasības nav mērķis."
)

if "full_mep_source_docs_df" not in st.session_state:
    st.session_state.full_mep_source_docs_df = pd.DataFrame()

if "full_mep_requirements_df" not in st.session_state:
    st.session_state.full_mep_requirements_df = pd.DataFrame()

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

            st.session_state.full_mep_source_docs_df = design_df

            if design_df.empty:
                st.warning("Nav atrasti Design Brief dokumenti.")
            else:
                st.success(f"Atrasti {len(design_df)} Design Brief dokumenti.")

    except Exception as e:
        st.error("Neizdevās atrast Design Brief dokumentus.")
        st.exception(e)

source_docs_df = st.session_state.full_mep_source_docs_df

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
        "Izvēlies konkrētus Design Brief failus pilnai MEP prasību analīzei",
        options=file_options,
        default=suggested_defaults[:1],
    )

    selected_docs_df = source_docs_df[source_docs_df["path"].isin(selected_paths)].copy()

    st.markdown("### Analīzei izvēlētie faili")
    st.dataframe(
        selected_docs_df[["name", "path", "size"]],
        use_container_width=True,
    )

    st.warning(
        "Pilna analīze var palaist daudz AI pieprasījumu. "
        "Pirmajam pilnajam testam izvēlies tikai vienu PDF."
    )

    if st.button("2) Pilnā režīmā izvilkt VISAS inženiertīklu prasību kandidātes"):
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

            total_steps = 0
            planned_batches_by_doc = []

            status = st.empty()
            progress = st.progress(0)

            st.markdown("## 3. PDF teksta sagatavošana")

            for _, doc_row in selected_docs_df.iterrows():
                file_name = doc_row["name"]
                file_id = doc_row["id"]

                status.write(f"Lejupielādēju un sadalu lapās: {file_name}")

                pdf_bytes = download_drive_file_bytes(drive_service, file_id)
                blocks_df, total_pages = extract_pdf_page_blocks(
                    pdf_bytes=pdf_bytes,
                    max_pages=int(max_pages_per_pdf),
                )

                batches = make_page_batches(
                    blocks_df=blocks_df,
                    pages_per_batch=int(pages_per_batch),
                    max_blocks_per_batch=int(max_blocks_per_batch),
                )

                planned_batches_by_doc.append(
                    {
                        "doc_row": doc_row,
                        "total_pages": total_pages,
                        "processed_pages": min(total_pages, int(max_pages_per_pdf)),
                        "blocks_df": blocks_df,
                        "batches": batches,
                    }
                )

                total_steps += len(batches)

                st.write(
                    f"{file_name}: PDF lapas kopā {total_pages}, analizējamās lapas "
                    f"{min(total_pages, int(max_pages_per_pdf))}, teksta bloki {len(blocks_df)}, "
                    f"AI pieprasījumi {len(batches)}."
                )

            if total_steps == 0:
                st.warning("Nav sagatavots neviens AI analīzes fragments.")
                st.stop()

            st.markdown("## 4. AI pilnā analīze pa lapu grupām")

            completed_steps = 0
            batch_global_index = 0

            for plan in planned_batches_by_doc:
                doc_row = plan["doc_row"]
                batches = plan["batches"]

                file_name = doc_row["name"]
                file_id = doc_row["id"]
                drive_path = doc_row["path"]

                for batch_index, batch in enumerate(batches, start=1):
                    batch_global_index += 1

                    status.write(
                        f"AI analizē: {file_name}, lapas "
                        f"{batch['start_page']}–{batch['end_page']} "
                        f"({completed_steps + 1}/{total_steps})"
                    )

                    try:
                        requirements = extract_full_engineering_requirements_with_ai(
                            client=client,
                            universal_prompt=universal_prompt,
                            source_file=file_name,
                            batch_start_page=batch["start_page"],
                            batch_end_page=batch["end_page"],
                            source_text=batch["text"],
                            model=model,
                            strictness_level=int(strictness_level),
                        )

                        enriched = add_batch_metadata(
                            requirements=requirements,
                            source_file=file_name,
                            drive_file_id=file_id,
                            drive_path=drive_path,
                            batch_start_page=batch["start_page"],
                            batch_end_page=batch["end_page"],
                            batch_index=batch_global_index,
                        )

                        all_requirements.extend(enriched)

                        st.write(
                            f"✅ {file_name}, lapas {batch['start_page']}–{batch['end_page']}: "
                            f"{len(enriched)} prasību kandidātes."
                        )

                    except Exception as batch_error:
                        st.error(
                            f"Kļūda AI analīzē: {file_name}, lapas "
                            f"{batch['start_page']}–{batch['end_page']}"
                        )
                        st.exception(batch_error)

                    completed_steps += 1
                    progress.progress(completed_steps / total_steps)

                    if float(delay_between_ai_calls) > 0:
                        time.sleep(float(delay_between_ai_calls))

            if not all_requirements:
                st.info("AI neatrada inženiertīklu prasību kandidātes izvēlētajos failos.")
                st.session_state.full_mep_requirements_df = pd.DataFrame()
            else:
                req_df = pd.DataFrame(all_requirements)
                req_df = postprocess_requirements_df(req_df)

                st.session_state.full_mep_requirements_df = req_df

                st.success(
                    f"Pabeigts. Izvilktas {len(req_df)} inženiertīklu prasību kandidātes."
                )

        except Exception as e:
            st.error("Kļūda pilnajā MEP prasību analīzē.")
            st.exception(e)

requirements_df = st.session_state.full_mep_requirements_df

if not requirements_df.empty:
    st.markdown("## 5. AI izvilktās inženiertīklu prasību kandidātes")

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
        "batch_index",
        "batch_start_page",
        "batch_end_page",
        "drive_path",
    ]

    existing_cols = [col for col in preferred_cols if col in requirements_df.columns]
    other_cols = [col for col in requirements_df.columns if col not in existing_cols]

    display_df = requirements_df[existing_cols + other_cols].copy()

    st.markdown("### Kopsavilkums pēc sistēmas")
    if "engineering_system" in display_df.columns:
        summary_system = (
            display_df.groupby("engineering_system")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        st.dataframe(summary_system, use_container_width=True)

    st.markdown("### Kopsavilkums pēc prioritātes")
    if "priority" in display_df.columns:
        summary_priority = (
            display_df.groupby("priority")
            .size()
            .reset_index(name="count")
            .sort_values("priority", ascending=False)
        )
        st.dataframe(summary_priority, use_container_width=True)

    st.markdown("### Rediģējama prasību tabula")

    edited_df = st.data_editor(
        display_df,
        use_container_width=True,
        num_rows="dynamic",
        key="full_mep_requirements_editor",
    )

    st.session_state.full_mep_requirements_df = edited_df

    excel_bytes = make_excel_bytes(edited_df)

    st.download_button(
        label="Lejupielādēt pilno MEP prasību kandidātu tabulu Excel formātā",
        data=excel_bytes,
        file_name="c2_3_full_engineering_requirements_candidates.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
