import io
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI


st.set_page_config(page_title="Disciplīnas atmiņas veidotājs", layout="wide")

st.title("BP disciplīnas faktu atmiņas veidotājs")

st.write(
    "Šī aplikācija nolasa izvēlētas būvprojekta disciplīnas PDF failus no Google Drive, "
    "izvelk būtiskos tehniskos faktus un sagatavo JSON/Excel atmiņas failus, ko manuāli "
    "ievietot `03_Memory` mapē."
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


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


# =========================================================
# Disciplīnu un dokumentu atpazīšana
# =========================================================

def get_discipline_code_from_folder_name(folder_name: str) -> str:
    name = str(folder_name).strip()

    if "_" in name:
        return name.split("_", 1)[1].strip()

    return name.strip()


def classify_document_type(file_name: str, path: str = "") -> str:
    text = f"{file_name} {path}".lower()

    if any(keyword in text for keyword in [
        "explanatory", "description", "skaidrojo", "apraksts", " sa", "_sa", "td_"
    ]):
        return "explanatory_note"

    if any(keyword in text for keyword in [
        "specification", "specifik", "apjomi", "boq", "bill of quantities", " ms", "_ms"
    ]):
        return "specification"

    if any(keyword in text for keyword in [
        "general data", "vispār", "vispar", "drawing list", "rasējumu saraksts"
    ]):
        return "general_data"

    if any(keyword in text for keyword in [
        "calculation", "aprēķ", "aprek", "calcs"
    ]):
        return "calculation"

    if any(keyword in text for keyword in [
        "scheme", "layout", "section", "plan", "floor", "site plan", "drawing",
        "rasēj", "rasej", "plāns", "plans", "griezums", "shēma", "shema", " ra", "_ra"
    ]):
        return "drawing"

    return "other_pdf"


def get_discipline_folders(service, input_folder_id: str) -> pd.DataFrame:
    items = list_folder_items(service, input_folder_id)

    folders = [
        item for item in items
        if item.get("mimeType") == FOLDER_MIME_TYPE
    ]

    rows = []

    for item in folders:
        folder_name = item.get("name", "")
        discipline_code = get_discipline_code_from_folder_name(folder_name)

        rows.append(
            {
                "folder_name": folder_name,
                "discipline_code": discipline_code,
                "folder_id": item.get("id"),
                "modifiedTime": item.get("modifiedTime", ""),
            }
        )

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows).sort_values("folder_name")


def get_pdf_documents_in_discipline(service, discipline_folder_id: str, discipline_folder_name: str) -> pd.DataFrame:
    rows = list_items_recursive(
        service=service,
        folder_id=discipline_folder_id,
        parent_path=discipline_folder_name,
    )

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    pdf_df = df[
        (df["is_folder"] == False)
        & (df["mimeType"] == PDF_MIME_TYPE)
    ].copy()

    if pdf_df.empty:
        return pdf_df

    pdf_df["document_type"] = pdf_df.apply(
        lambda row: classify_document_type(
            file_name=row.get("name", ""),
            path=row.get("path", ""),
        ),
        axis=1,
    )

    return pdf_df


# =========================================================
# PDF teksta bloki
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


def build_fact_extraction_prompt(
    project_code: str,
    discipline_code: str,
    document_type: str,
    source_file: str,
    batch_start_page: int,
    batch_end_page: int,
    source_text: str,
    extraction_breadth: int,
) -> str:
    return f"""
Tu esi būvprojekta tehniskās dokumentācijas faktu indeksēšanas asistents Latvijā.

Tavs uzdevums ir no dotā būvprojekta dokumenta fragmenta izvilkt strukturētus tehniskos faktus,
kas vēlāk palīdzēs salīdzināt šo disciplīnu ar citām būvprojekta sadaļām.

ŠIS NAV KOPSAVILKUMS.
ŠIS NAV KĻŪDU MEKLĒŠANAS UZDEVUMS.
NEVEIDO PIEZĪMES.
NEVĒRTĒ, VAI RISINĀJUMS IR PAREIZS.
Tikai izvelc faktus, kas redzami tekstā.

Projekts: {project_code}
Disciplīna: {discipline_code}
Dokumenta tips: {document_type}
Fails: {source_file}
Analizējamās lapas: {batch_start_page}–{batch_end_page}

Faktu izvilkšanas plašums: {extraction_breadth}

Interpretācija:
1 = ļoti plaši, iekļaut arī mazākus faktus un kontekstu;
3 = plaši, labs noklusējums;
6 = tikai skaidri tehniski fakti;
8 = tikai būtiski galvenie fakti;
10 = tikai kritiski galvenie fakti.

Prioritāte ir vēlākai salīdzināšanai starp BP sadaļām.
Labāk iekļaut vairāk strukturētu faktu nekā palaist garām būtisku parametru.

IZVELC ŠĀDU TIPU FAKTUS, JA TIE REDZAMI TEKSTĀ:

1. Sistēmas un apzīmējumi:
- U1, K1, K2, K3, LK, D, EL, ESS, UATS, UAS, AVK, SM vai citi sistēmu kodi
- stāvvadi, ievadi, izvadi, pieslēgumi
- akas, mezgli, telpas, šahtas, sadalnes, iekārtas

2. Diametri, izmēri, materiāli:
- DN, D, OD, Ø, diametri
- PE, PP, PVC, tērauds, varš, materiāli
- PN spiediena klase
- SN stingrības klase
- izolācijas biezumi
- cauruļu / kabeļu / kanālu tipi

3. Daudzumi un apjomi:
- gab., m, m2, m3, komplekti
- specifikācijas pozīcijas
- montāžas apjomi
- “3 gab. D110 pretvārsti”
- “90 m tranšejas nostiprināšana”
- nulles vai tukši apjomi, ja redzami kā fakts

4. Iekārtas:
- sūkņi
- vārsti
- pretvārsti
- tauku atdalītāji
- siltummezgli
- ventilācijas iekārtas
- sadalnes
- transformatori
- UPS
- BMS iekārtas
- ugunsdrošības iekārtas

5. Pieslēgumi un robežas:
- pieslēgums ārējiem tīkliem
- pieslēgums citai sadaļai
- projektē/piegādā/montē cita sadaļa
- robeža starp UK/UKT, EL/ELT, ESS/EST, UAS/UATS, AVK/SM, AR/BK/MEP
- saistītie projekti

6. Telpas un piekļuve:
- tehniskās telpas
- transformatoru telpa
- siltummezgla telpa
- ūdens ievada telpa
- elektro telpa
- ventilācijas iekārtu telpas
- šahtas
- revīzijas lūkas
- apkopes piekļuve
- iekārtu nomaiņas ceļi

7. Ugunsdrošība un drošība:
- EI/E/REI klases
- E30/E60/E90 kabeļi
- ugunsdrošie vārsti
- dūmu novadīšana
- ugunsgrēka signalizācija
- evakuācijas / avārijas apgaismojums
- ugunsdzēsības ūdensapgāde

8. Jaudas, plūsmas, spiedieni:
- kW, W, A, V
- l/s, m3/h
- Pa, bar
- siltuma/dzesēšanas/elektriskās jaudas
- ūdens patēriņi
- ventilācijas gaisa daudzumi

9. Aprēķinu un tabulu fakti:
- aprēķinu rezultāti
- kopsummas
- izmantojamās vērtības
- normatīvās robežvērtības
- tabulu rindas, ja tās satur tehnisku parametru

10. Dokumentu identifikācijas fakti:
- rasējuma numurs
- specifikācijas numurs
- revīzija
- dokumenta nosaukums
- sadaļas nosaukums
- ja tas var palīdzēt vēlāk salīdzināt dokumentus

SVARĪGI:
- Neinterpretē grafiskus simbolus bez teksta.
- Neizdomā trūkstošu informāciju.
- Ja tekstā tikai minēts sistēmas kods bez parametra, iekļauj tikai tad, ja tas palīdz vēlākai salīdzināšanai.
- Atkārtotus U1/K1/K2/K3 grafiskus marķējumus rasējumā neuzskati par vienu teikumu.
- Ja ir teksts ar parametru blakus marķējumam, to drīkst izvilkt.
- Ja fakts nav pietiekami skaidrs, dod zemāku confidence.
- Vienā JSON objektā liec vienu pārbaudāmu faktu.
- Ja vienā teksta blokā ir vairāki fakti, izveido vairākus objektus.

ATBILDES FORMĀTS:
Atbildi tikai JSON masīvā.
Neizmanto Markdown.
Ja nav faktu, atgriez [].

Katram objektam jābūt šādiem laukiem:

- fact_id: īss ID, piemēram "FACT-001"
- project_code
- discipline
- document_type
- fact_type: viens no [
  "system_reference",
  "pipe_diameter",
  "cable_parameter",
  "material",
  "pressure_class",
  "stiffness_class",
  "quantity",
  "equipment",
  "connection",
  "interface",
  "room_or_access",
  "fire_safety",
  "power_or_flow",
  "calculation_value",
  "document_identity",
  "specification_item",
  "other_fact"
]
- system_code: piemēram "U1", "K1", "K2", "K3", "EL", "AVK"; ja nav, tukša virkne
- element: īss elementa nosaukums, piemēram "ūdensvada pievads", "pretvārsts", "aka", "siltummezgls"
- parameter_name: piemēram "diameter", "material", "quantity", "power", "pressure_class"; ja nav, tukša virkne
- parameter_value: konkrētā vērtība, piemēram "OD110", "3", "PN10", "90"; ja nav, tukša virkne
- unit: piemēram "gab.", "m", "kW", "l/s"; ja nav, tukša virkne
- applies_to: īss skaidrojums, uz ko fakts attiecas
- source_file
- page
- block_id
- source_text: īss avota teksta fragments
- confidence: skaitlis no 0 līdz 1

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


def extract_facts_with_ai(
    client: OpenAI,
    project_code: str,
    discipline_code: str,
    document_type: str,
    source_file: str,
    batch_start_page: int,
    batch_end_page: int,
    source_text: str,
    model: str,
    extraction_breadth: int,
) -> List[Dict[str, Any]]:
    prompt = build_fact_extraction_prompt(
        project_code=project_code,
        discipline_code=discipline_code,
        document_type=document_type,
        source_file=source_file,
        batch_start_page=batch_start_page,
        batch_end_page=batch_end_page,
        source_text=source_text,
        extraction_breadth=extraction_breadth,
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
                    "Tu neveido kļūdu sarakstu, tikai strukturētus faktus."
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
# Excel / JSON sagatavošana
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


def postprocess_facts_df(df: pd.DataFrame, project_code: str, discipline_code: str) -> pd.DataFrame:
    if df.empty:
        return df

    result = df.copy()

    required_cols = [
        "fact_id",
        "project_code",
        "discipline",
        "document_type",
        "fact_type",
        "system_code",
        "element",
        "parameter_name",
        "parameter_value",
        "unit",
        "applies_to",
        "source_file",
        "page",
        "block_id",
        "source_text",
        "confidence",
        "drive_path",
        "drive_file_id",
        "batch_index",
        "batch_start_page",
        "batch_end_page",
    ]

    for col in required_cols:
        if col not in result.columns:
            result[col] = ""

    result["project_code"] = result["project_code"].fillna(project_code).replace("", project_code)
    result["discipline"] = result["discipline"].fillna(discipline_code).replace("", discipline_code)

    if "page" in result.columns:
        result["page"] = pd.to_numeric(result["page"], errors="coerce").fillna(0).astype(int)

    if "block_id" in result.columns:
        result["block_id"] = pd.to_numeric(result["block_id"], errors="coerce").fillna(-1).astype(int)

    if "confidence" in result.columns:
        result["confidence"] = pd.to_numeric(result["confidence"], errors="coerce").fillna(0.0)

    text_cols = [
        "fact_id", "project_code", "discipline", "document_type", "fact_type",
        "system_code", "element", "parameter_name", "parameter_value", "unit",
        "applies_to", "source_file", "source_text", "drive_path"
    ]

    for col in text_cols:
        if col in result.columns:
            result[col] = result[col].astype(str).str.strip()

    result = result[result["source_text"].astype(str).str.strip() != ""].copy()

    result = result.reset_index(drop=True)
    result["memory_id"] = [
        f"{project_code}-{discipline_code}-FACT-{i + 1:04d}"
        for i in range(len(result))
    ]
    result["memory_type"] = "discipline_fact"
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()

    preferred_cols = [
        "memory_id",
        "memory_type",
        "project_code",
        "discipline",
        "fact_id",
        "document_type",
        "fact_type",
        "system_code",
        "element",
        "parameter_name",
        "parameter_value",
        "unit",
        "applies_to",
        "source_file",
        "page",
        "block_id",
        "source_text",
        "confidence",
        "drive_path",
        "drive_file_id",
        "batch_index",
        "batch_start_page",
        "batch_end_page",
        "created_at_utc",
    ]

    existing_cols = [col for col in preferred_cols if col in result.columns]
    other_cols = [col for col in result.columns if col not in existing_cols]

    return result[existing_cols + other_cols]


def make_excel_bytes(df: pd.DataFrame, discipline_code: str) -> bytes:
    output = io.BytesIO()
    safe_df = clean_dataframe_for_excel(df)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        safe_df.to_excel(writer, sheet_name="discipline_facts", index=False)

        if "fact_type" in safe_df.columns:
            summary_type = (
                safe_df.groupby("fact_type")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            summary_type.to_excel(writer, sheet_name="summary_by_fact_type", index=False)

        if "source_file" in safe_df.columns:
            summary_file = (
                safe_df.groupby("source_file")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            summary_file.to_excel(writer, sheet_name="summary_by_file", index=False)

        if "system_code" in safe_df.columns:
            summary_system = (
                safe_df.groupby("system_code")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            summary_system.to_excel(writer, sheet_name="summary_by_system", index=False)

    output.seek(0)
    return output.getvalue()


def make_json_bytes(df: pd.DataFrame, project_code: str, discipline_code: str) -> bytes:
    records = df.where(pd.notna(df), None).to_dict(orient="records")

    payload = {
        "memory_schema": "bp_audit_discipline_facts_v1",
        "project_code": project_code,
        "discipline": discipline_code,
        "memory_type": "discipline_fact",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "facts": records,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# =========================================================
# Streamlit UI
# =========================================================

input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")

st.markdown("## 1. Konfigurācija")
st.write("Input folder ID:", input_folder_id)

project_code = st.text_input("Projekta kods", value="C2-3")

model = st.selectbox(
    "AI modelis",
    options=["gpt-4.1-mini", "gpt-4.1"],
    index=0,
)

max_pages_per_pdf = st.number_input(
    "Maksimālais lapu skaits no viena PDF",
    min_value=1,
    max_value=300,
    value=80,
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

extraction_breadth = st.slider(
    "Faktu izvilkšanas plašums",
    min_value=1,
    max_value=10,
    value=3,
    step=1,
    help="1 = ļoti plaši; 3 = plaši; 6 = tikai skaidri fakti; 10 = tikai kritiski galvenie fakti.",
)

delay_between_ai_calls = st.number_input(
    "Pauze starp AI pieprasījumiem sekundēs",
    min_value=0.0,
    max_value=5.0,
    value=0.5,
    step=0.5,
)

st.info(
    "Pirmajam testam izvēlies vienu disciplīnu, piemēram `09_UKT`, un sākumā tikai dažus galvenos PDF. "
    "Kad redzēsim, ka faktu kvalitāte ir laba, varēs analizēt visu disciplīnu."
)

if "discipline_folders_df" not in st.session_state:
    st.session_state.discipline_folders_df = pd.DataFrame()

if "discipline_docs_df" not in st.session_state:
    st.session_state.discipline_docs_df = pd.DataFrame()

if "discipline_facts_df" not in st.session_state:
    st.session_state.discipline_facts_df = pd.DataFrame()

if st.button("1) Atrast disciplīnu mapes"):
    try:
        if not input_folder_id:
            st.error("Secrets nav atrasts GOOGLE_DRIVE_INPUT_FOLDER_ID.")
            st.stop()

        drive_service = get_drive_service()
        folders_df = get_discipline_folders(drive_service, input_folder_id)

        if folders_df.empty:
            st.warning("C2-3_TD mapē nav atrastas disciplīnu mapes.")
        else:
            st.session_state.discipline_folders_df = folders_df
            st.success(f"Atrastas {len(folders_df)} disciplīnu mapes.")

    except Exception as e:
        st.error("Neizdevās atrast disciplīnu mapes.")
        st.exception(e)

folders_df = st.session_state.discipline_folders_df

if not folders_df.empty:
    st.markdown("## 2. Disciplīnu mapes")
    st.dataframe(folders_df, use_container_width=True)

    folder_options = folders_df["folder_name"].tolist()

    default_index = 0
    for idx, name in enumerate(folder_options):
        if name.startswith("09_UKT"):
            default_index = idx
            break

    selected_folder_name = st.selectbox(
        "Izvēlies disciplīnu",
        options=folder_options,
        index=default_index,
    )

    selected_folder_row = folders_df[folders_df["folder_name"] == selected_folder_name].iloc[0]
    selected_discipline_code = selected_folder_row["discipline_code"]
    selected_folder_id = selected_folder_row["folder_id"]

    st.write("Izvēlētā disciplīna:", selected_discipline_code)

    if st.button("2) Atrast PDF failus izvēlētajā disciplīnā"):
        try:
            drive_service = get_drive_service()
            docs_df = get_pdf_documents_in_discipline(
                service=drive_service,
                discipline_folder_id=selected_folder_id,
                discipline_folder_name=selected_folder_name,
            )

            if docs_df.empty:
                st.warning("Izvēlētajā disciplīnā nav atrasti PDF faili.")
            else:
                docs_df["discipline"] = selected_discipline_code
                docs_df["discipline_folder"] = selected_folder_name
                st.session_state.discipline_docs_df = docs_df
                st.success(f"Atrasti {len(docs_df)} PDF faili.")

        except Exception as e:
            st.error("Neizdevās atrast PDF failus disciplīnā.")
            st.exception(e)

docs_df = st.session_state.discipline_docs_df

if not docs_df.empty:
    st.markdown("## 3. Atrastie PDF faili")

    st.dataframe(
        docs_df[["name", "path", "document_type", "size", "modifiedTime"]],
        use_container_width=True,
    )

    file_options = docs_df["path"].tolist()

    suggested_defaults = []
    for path in file_options:
        lower = path.lower()
        if any(key in lower for key in ["spec", "apjomi", "description", "explanatory", "skaidrojo", "general"]):
            suggested_defaults.append(path)

    if not suggested_defaults:
        suggested_defaults = file_options[: min(3, len(file_options))]

    selected_paths = st.multiselect(
        "Izvēlies PDF failus faktu izvilkšanai",
        options=file_options,
        default=suggested_defaults[: min(5, len(suggested_defaults))],
    )

    selected_docs_df = docs_df[docs_df["path"].isin(selected_paths)].copy()

    st.markdown("### Analīzei izvēlētie faili")
    st.dataframe(
        selected_docs_df[["name", "path", "document_type", "size"]],
        use_container_width=True,
    )

    st.warning(
        "Šis solis var palaist vairākus AI pieprasījumus. Pirmajā testā neizvēlies pārāk daudz lielu rasējumu."
    )

    if st.button("3) Izvilkt disciplīnas faktus ar AI"):
        try:
            if selected_docs_df.empty:
                st.warning("Nav izvēlēts neviens PDF fails.")
                st.stop()

            drive_service = get_drive_service()
            client = get_openai_client()

            all_facts: List[Dict[str, Any]] = []
            planned_batches_by_doc = []
            total_steps = 0

            status = st.empty()
            progress = st.progress(0)

            st.markdown("## 4. PDF teksta sagatavošana")

            for _, doc_row in selected_docs_df.iterrows():
                file_name = doc_row["name"]
                file_id = doc_row["id"]
                document_type = doc_row["document_type"]

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
                        "document_type": document_type,
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

            st.markdown("## 5. AI faktu izvilkšana pa lapu grupām")

            completed_steps = 0
            batch_global_index = 0

            discipline_code = str(selected_docs_df["discipline"].iloc[0])

            for plan in planned_batches_by_doc:
                doc_row = plan["doc_row"]
                batches = plan["batches"]
                document_type = plan["document_type"]

                file_name = doc_row["name"]
                file_id = doc_row["id"]
                drive_path = doc_row["path"]

                for _, batch in enumerate(batches, start=1):
                    batch_global_index += 1

                    status.write(
                        f"AI analizē: {file_name}, lapas "
                        f"{batch['start_page']}–{batch['end_page']} "
                        f"({completed_steps + 1}/{total_steps})"
                    )

                    try:
                        facts = extract_facts_with_ai(
                            client=client,
                            project_code=project_code,
                            discipline_code=discipline_code,
                            document_type=document_type,
                            source_file=file_name,
                            batch_start_page=batch["start_page"],
                            batch_end_page=batch["end_page"],
                            source_text=batch["text"],
                            model=model,
                            extraction_breadth=int(extraction_breadth),
                        )

                        enriched = []
                        for item_index, fact in enumerate(facts, start=1):
                            fact = dict(fact)
                            fact["project_code"] = fact.get("project_code") or project_code
                            fact["discipline"] = fact.get("discipline") or discipline_code
                            fact["document_type"] = fact.get("document_type") or document_type
                            fact["source_file"] = fact.get("source_file") or file_name
                            fact["drive_file_id"] = file_id
                            fact["drive_path"] = drive_path
                            fact["batch_index"] = batch_global_index
                            fact["batch_start_page"] = batch["start_page"]
                            fact["batch_end_page"] = batch["end_page"]

                            if not fact.get("fact_id"):
                                fact["fact_id"] = f"FACT-B{batch_global_index:03d}-{item_index:03d}"

                            enriched.append(fact)

                        all_facts.extend(enriched)

                        st.write(
                            f"✅ {file_name}, lapas {batch['start_page']}–{batch['end_page']}: "
                            f"{len(enriched)} fakti."
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

            if not all_facts:
                st.info("AI neatrada strukturētus faktus izvēlētajos failos.")
                st.session_state.discipline_facts_df = pd.DataFrame()
            else:
                facts_df = pd.DataFrame(all_facts)
                facts_df = postprocess_facts_df(
                    facts_df,
                    project_code=project_code,
                    discipline_code=discipline_code,
                )

                st.session_state.discipline_facts_df = facts_df

                st.success(
                    f"Pabeigts. Izvilkti {len(facts_df)} strukturēti fakti."
                )

        except Exception as e:
            st.error("Kļūda disciplīnas faktu izvilkšanā.")
            st.exception(e)

facts_df = st.session_state.discipline_facts_df

if not facts_df.empty:
    st.markdown("## 6. Izvilktie disciplīnas fakti")

    st.markdown("### Kopsavilkums pēc fact_type")
    if "fact_type" in facts_df.columns:
        summary_type = (
            facts_df.groupby("fact_type")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        st.dataframe(summary_type, use_container_width=True)

    st.markdown("### Kopsavilkums pēc source_file")
    if "source_file" in facts_df.columns:
        summary_file = (
            facts_df.groupby("source_file")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        st.dataframe(summary_file, use_container_width=True)

    st.markdown("### Kopsavilkums pēc system_code")
    if "system_code" in facts_df.columns:
        summary_system = (
            facts_df.groupby("system_code")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        st.dataframe(summary_system, use_container_width=True)

    st.markdown("### Rediģējama faktu tabula")

    preferred_cols = [
        "memory_id",
        "fact_type",
        "system_code",
        "element",
        "parameter_name",
        "parameter_value",
        "unit",
        "applies_to",
        "source_file",
        "page",
        "block_id",
        "source_text",
        "confidence",
        "document_type",
        "discipline",
        "drive_path",
    ]

    existing_cols = [col for col in preferred_cols if col in facts_df.columns]
    other_cols = [col for col in facts_df.columns if col not in existing_cols]

    edited_df = st.data_editor(
        facts_df[existing_cols + other_cols],
        use_container_width=True,
        num_rows="dynamic",
        key="discipline_facts_editor",
    )

    st.session_state.discipline_facts_df = edited_df

    discipline_code_for_file = str(edited_df["discipline"].iloc[0]) if "discipline" in edited_df.columns else "DISC"
    safe_project = project_code.lower().replace("-", "_").replace(" ", "_")
    safe_discipline = discipline_code_for_file.lower().replace("-", "_").replace(" ", "_")

    excel_name = f"{safe_project}_{safe_discipline}_facts.xlsx"
    json_name = f"{safe_project}_{safe_discipline}_facts.json"

    excel_bytes = make_excel_bytes(edited_df, discipline_code_for_file)
    json_bytes = make_json_bytes(edited_df, project_code, discipline_code_for_file)

    col1, col2 = st.columns(2)

    with col1:
        st.download_button(
            label="Lejupielādēt disciplīnas faktu Excel",
            data=excel_bytes,
            file_name=excel_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with col2:
        st.download_button(
            label="Lejupielādēt disciplīnas faktu JSON",
            data=json_bytes,
            file_name=json_name,
            mime="application/json",
        )

    st.info(
        f"Lejupielādē `{excel_name}` un `{json_name}`, pēc tam manuāli ievieto tos Google Drive `03_Memory` mapē."
    )
