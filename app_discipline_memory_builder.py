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
    """
    Piemēri:
    09_UKT -> UKT
    18_UK -> UK
    21_EL -> EL
    23_ESS-VAS -> ESS-VAS
    """
    name = str(folder_name).strip()

    if "_" in name:
        return name.split("_", 1)[1].strip()

    return name.strip()


def classify_document_type(file_name: str, path: str = "") -> str:
    text = f"{file_name} {path}".lower()

    if any(keyword in text for keyword in [
        "explanatory", "description", "skaidrojo", "apraksts", "sa_", "_sa", "td_"
    ]):
        return "explanatory_note"

    if any(keyword in text for keyword in [
        "specification", "specifik", "apjomi", "boq", "bill of quantities", "ms_", "_ms"
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
        "rasēj", "rasej", "plāns", "plans", "griezums", "shēma", "shema", "ra_", "_ra"
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

Tavā gadījumā prioritāte ir vēlākai salīdzināšanai starp BP sadaļām.
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
  "room_or
