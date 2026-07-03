import ast
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

st.set_page_config(page_title="BP universālais audita rīks v3", layout="wide")

st.title("BP universālais audita rīks v3")
st.write(
    "Šī versija papildina universālo auditu ar strukturētu C moduļa salīdzināšanu, "
    "diagnostikas režīmu, pagaidu faktu indeksu, raw AI kandidātu tabulu un filtra pārskatu. "
    "PDF anotēšana šajā versijā vēl netiek ģenerēta."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"


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

    for item in list_folder_items(service, folder_id):
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
            rows.extend(list_items_recursive(service, item.get("id"), item_path))

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


def export_google_file_bytes(service, file_id: str, mime_type: str) -> bytes:
    request = service.files().export_media(fileId=file_id, mimeType=mime_type)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


# =========================================================
# Klasifikācija
# =========================================================

def get_discipline_code_from_folder_name(folder_name: str) -> str:
    name = str(folder_name).strip()
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return name


def classify_document_type(file_name: str, path: str = "") -> str:
    text = f"{file_name} {path}".lower()

    if any(k in text for k in ["explanatory", "description", "skaidrojo", "apraksts", "_td", "td_", "note"]):
        return "explanatory_note"

    if any(k in text for k in ["specification", "specifik", "apjomi", "boq", "bill of quantities", "_ms", "ms_"]):
        return "specification"

    if any(k in text for k in ["general data", "general_data", "vispār", "vispar", "drawing list", "rasējumu saraksts"]):
        return "general_data"

    if any(k in text for k in ["calculation", "aprēķ", "aprek", "calcs"]):
        return "calculation"

    if any(k in text for k in [
        "scheme", "layout", "section", "plan", "profile", "floor", "site plan",
        "drawing", "rasēj", "rasej", "plāns", "plans", "griezums", "shēma",
        "shema", "_ra", "ra_"
    ]):
        return "drawing"

    return "other_pdf"


def get_discipline_folders(service, input_folder_id: str) -> pd.DataFrame:
    rows = []

    for item in list_folder_items(service, input_folder_id):
        if item.get("mimeType") != FOLDER_MIME_TYPE:
            continue

        folder_name = item.get("name", "")

        rows.append({
            "folder_name": folder_name,
            "discipline_code": get_discipline_code_from_folder_name(folder_name),
            "folder_id": item.get("id"),
            "modifiedTime": item.get("modifiedTime", ""),
        })

    return pd.DataFrame(rows).sort_values("folder_name") if rows else pd.DataFrame()


def get_pdf_documents_in_discipline(service, discipline_folder_id: str, discipline_folder_name: str) -> pd.DataFrame:
    rows = list_items_recursive(service, discipline_folder_id, discipline_folder_name)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    pdf_df = df[(df["is_folder"] == False) & (df["mimeType"] == PDF_MIME_TYPE)].copy()

    if pdf_df.empty:
        return pdf_df

    pdf_df["document_type"] = pdf_df.apply(
        lambda r: classify_document_type(r.get("name", ""), r.get("path", "")),
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

                rows.append({
                    "page": page_index + 1,
                    "block_id": block_index,
                    "x0": round(float(x0), 2),
                    "y0": round(float(y0), 2),
                    "x1": round(float(x1), 2),
                    "y1": round(float(y1), 2),
                    "text": clean_text,
                })

    return pd.DataFrame(rows), total_pages


def build_text_from_blocks(blocks_df: pd.DataFrame, max_blocks: int = 220) -> str:
    if blocks_df.empty:
        return ""

    selected = blocks_df.head(max_blocks)
    lines = []

    for _, row in selected.iterrows():
        lines.append(
            f"[source_file={row.get('source_file')} "
            f"document_type={row.get('document_type')} "
            f"page={row.get('page')} "
            f"block_id={row.get('block_id')}] {row.get('text')}"
        )

    return "\n".join(lines)


def expand_blocks_with_context(
    all_blocks_df: pd.DataFrame,
    anchor_blocks_df: pd.DataFrame,
    context_window: int = 2,
) -> pd.DataFrame:
    if all_blocks_df.empty or anchor_blocks_df.empty:
        return anchor_blocks_df.copy()

    pieces = []

    for _, anchor in anchor_blocks_df.iterrows():
        try:
            source_file = str(anchor.get("source_file"))
            page = int(anchor.get("page"))
            block_id = int(anchor.get("block_id"))
        except Exception:
            continue

        match = all_blocks_df[
            (all_blocks_df["source_file"].astype(str) == source_file)
            & (all_blocks_df["page"].astype(int) == page)
            & (all_blocks_df["block_id"].astype(int).between(block_id - context_window, block_id + context_window))
        ].copy()

        if not match.empty:
            pieces.append(match)

    if not pieces:
        return anchor_blocks_df.copy()

    expanded = pd.concat(pieces, ignore_index=True)
    expanded = expanded.drop_duplicates(subset=["source_file", "page", "block_id"]).copy()
    expanded = expanded.sort_values(["source_file", "page", "block_id"])

    return expanded


def build_context_text(
    all_blocks_df: pd.DataFrame,
    anchor_blocks_df: pd.DataFrame,
    max_blocks: int = 260,
    context_window: int = 2,
) -> str:
    expanded = expand_blocks_with_context(
        all_blocks_df,
        anchor_blocks_df,
        context_window=context_window,
    )
    return build_text_from_blocks(expanded, max_blocks=max_blocks)


def get_blocks_by_type(blocks_df: pd.DataFrame, document_types: List[str]) -> pd.DataFrame:
    if blocks_df.empty or "document_type" not in blocks_df.columns:
        return pd.DataFrame()

    return blocks_df[blocks_df["document_type"].isin(document_types)].copy()


def get_orientation_blocks(blocks_df: pd.DataFrame) -> pd.DataFrame:
    note_blocks = get_blocks_by_type(blocks_df, ["explanatory_note"])

    if not note_blocks.empty:
        return note_blocks

    general_blocks = get_blocks_by_type(blocks_df, ["general_data"])

    if not general_blocks.empty:
        return general_blocks

    return blocks_df.head(260).copy()


def make_block_batches(blocks_df: pd.DataFrame, max_blocks_per_batch: int) -> List[pd.DataFrame]:
    if blocks_df.empty:
        return []

    batches = []

    for start in range(0, len(blocks_df), max_blocks_per_batch):
        batch = blocks_df.iloc[start:start + max_blocks_per_batch].copy()

        if not batch.empty:
            batches.append(batch)

    return batches


# =========================================================
# Memory un Prompt lasīšana
# =========================================================

def parse_list_value(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]

    text = str(value).strip()

    if not text or text.lower() == "nan":
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass

    return [p.strip() for p in re.split(r"[,;]", text) if p.strip()]


def load_memory_json_files(service, memory_folder_id: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    items = list_folder_items(service, memory_folder_id)
    catalog_rows = []
    requirements_records = []
    facts_records = []

    for item in items:
        name = str(item.get("name", ""))

        if not name.lower().endswith(".json"):
            continue

        try:
            payload = json.loads(
                download_drive_file_bytes(service, item.get("id")).decode("utf-8", errors="replace")
            )

            kind = "unknown_json"
            records = []
            detected_discipline = ""
            schema = ""

            if isinstance(payload, dict):
                schema = str(payload.get("memory_schema", ""))

                if isinstance(payload.get("requirements"), list):
                    kind = "design_brief_requirements"
                    records = payload.get("requirements", [])

                elif isinstance(payload.get("facts"), list):
                    kind = "discipline_facts"
                    records = payload.get("facts", [])

                elif isinstance(payload.get("records"), list):
                    records = payload.get("records", [])

            elif isinstance(payload, list):
                records = payload

            if kind == "unknown_json" and records:
                sample = records[0]

                if "requirement" in sample or ("memory_type" in sample and "requirement" in str(sample.get("memory_type"))):
                    kind = "design_brief_requirements"

                elif "fact_type" in sample or "fact_id" in sample:
                    kind = "discipline_facts"

            if records:
                for rec in records:
                    if not isinstance(rec, dict):
                        continue

                    rec = dict(rec)
                    rec["memory_source_file"] = name

                    if kind == "design_brief_requirements":
                        requirements_records.append(rec)

                    elif kind == "discipline_facts":
                        facts_records.append(rec)
                        detected_discipline = detected_discipline or str(
                            rec.get("discipline") or rec.get("memory_discipline") or ""
                        )

            catalog_rows.append({
                "name": name,
                "kind": kind,
                "memory_schema": schema,
                "records_count": len(records),
                "detected_discipline": detected_discipline,
                "mimeType": item.get("mimeType"),
                "size": item.get("size", ""),
                "modifiedTime": item.get("modifiedTime", ""),
            })

        except Exception as e:
            catalog_rows.append({
                "name": name,
                "kind": "error",
                "error": str(e),
            })

    req_df = pd.DataFrame(requirements_records)
    facts_df = pd.DataFrame(facts_records)

    if not req_df.empty:
        if "discipline_list" not in req_df.columns and "discipline" in req_df.columns:
            req_df["discipline_list"] = req_df["discipline"].apply(parse_list_value)

        if "applies_to_sections_list" not in req_df.columns and "applies_to_sections" in req_df.columns:
            req_df["applies_to_sections_list"] = req_df["applies_to_sections"].apply(parse_list_value)

    return pd.DataFrame(catalog_rows), req_df, facts_df


def load_prompt_assets(service, prompt_folder_id: str) -> Tuple[pd.DataFrame, str, str]:
    items = list_folder_items(service, prompt_folder_id)
    catalog = []
    universal_prompt = ""
    error_examples_text = ""

    for item in items:
        name = str(item.get("name", ""))
        mime = str(item.get("mimeType", ""))
        row = {
            "name": name,
            "mimeType": mime,
            "size": item.get("size", ""),
            "modifiedTime": item.get("modifiedTime", ""),
        }

        try:
            lower = name.lower()

            if lower == "universal_bp_audit_prompt.txt" or lower.endswith(".txt"):
                text = download_drive_file_bytes(service, item.get("id")).decode("utf-8", errors="replace")

                if "universal" in lower or not universal_prompt:
                    universal_prompt = text

                row["loaded"] = True

            elif lower.endswith(".xlsx") or mime == GOOGLE_SHEET_MIME_TYPE:
                if mime == GOOGLE_SHEET_MIME_TYPE:
                    data = export_google_file_bytes(
                        service,
                        item.get("id"),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                else:
                    data = download_drive_file_bytes(service, item.get("id"))

                df = pd.read_excel(io.BytesIO(data))
                preview = df.head(120).fillna("").astype(str)
                error_examples_text += f"\n\n### {name}\n" + preview.to_csv(index=False)[:20000]
                row["rows_loaded"] = len(df)

            elif lower.endswith(".pdf"):
                row["loaded"] = "pdf_registered"

        except Exception as e:
            row["error"] = str(e)

        catalog.append(row)

    return pd.DataFrame(catalog), universal_prompt, error_examples_text


# =========================================================
# OpenAI helpers
# =========================================================

def get_openai_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        raise ValueError("Secrets nav atrasts OPENAI_API_KEY.")

    return OpenAI(api_key=api_key)


def strip_code_fences(text: str) -> str:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    return text


def parse_json_array(raw_text: str) -> List[Dict[str, Any]]:
    text = strip_code_fences(raw_text)
    start = text.find("[")
    end = text.rfind("]")

    if start == -1 or end == -1 or end <= start:
        return []

    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []

    except Exception:
        return []


def call_ai_json_array(
    client: OpenAI,
    model: str,
    system: str,
    prompt: str,
    temperature: float = 0.0,
) -> Tuple[List[Dict[str, Any]], str]:
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )

    raw = response.choices[0].message.content or ""

    return parse_json_array(raw), raw


# =========================================================
# Prompts
# =========================================================

def mode_rules(audit_depth: str, confidence_threshold: float) -> str:
    if audit_depth == "Conservative":
        return f"""
Režīms: Conservative.
Atgriez tikai ļoti drošas, anotējamas piezīmes.
Minimālā mērķa pārliecība: {confidence_threshold}.
Ja šaubies, neatgriez piezīmi.
"""

    if audit_depth == "Balanced":
        return f"""
Režīms: Balanced.
Atgriez drošas un diezgan ticamas kandidātpiezīmes, bet tikai ar konkrētu source_file/page/block_id.
Minimālā mērķa pārliecība: {confidence_threshold}.
Neiekļauj vispārīgus "pārbaudīt" komentārus.
"""

    return f"""
Režīms: Diagnostic.
Atgriez plašāku kandidātu sarakstu diagnostikai, arī ja confidence ir zemāks.
Tomēr katrai rindai joprojām jābūt piesaistītai konkrētam source_file/page/block_id.
Šajā režīmā include_in_pdf drīkst būt false, bet kandidātu rādi tabulā.
"""


def issue_schema_instruction() -> str:
    return """
Atbildi tikai JSON masīvā. Katram objektam jābūt laukiem:
- issue_id
- audit_mode
- issue_type
- priority: 1-10
- confidence: 0-1
- include_in_pdf: true/false
- source_file: auditējamā BP faila nosaukums
- page: auditējamā BP faila lapa
- block_id: auditējamā BP teksta bloka ID
- source_text: tieši tas BP teksta fragments, ko varētu apvilkt PDF
- comment: īsa piezīme latviski
- suggestion: īss ieteikums latviski
- related_memory_id: ja attiecas uz Design Brief prasību
- related_requirement: ja attiecas uz Design Brief prasību
- related_fact_id: ja attiecas uz faktu atmiņu
- related_fact: ja attiecas uz faktu atmiņu
- related_files: saraksts vai īss teksts ar saistītiem failiem

Nekad neatgriez piezīmi bez source_file, page un block_id.
Ja piezīmi nevar piesaistīt konkrētam BP teksta blokam, neatgriez to kā PDF piezīmi.
"""


def build_fact_prompt(
    project_code: str,
    discipline_code: str,
    text_blocks: str,
    source_hint: str,
    error_examples_text: str,
) -> str:
    return f"""
Tu veido pagaidu faktu indeksu būvprojekta audita vajadzībām.
Projekts: {project_code}
Disciplīna: {discipline_code}
Avots: {source_hint}

Uzdevums: no teksta blokiem izvelc tehniskus faktus, kas vēlāk palīdz atrast nesakritības starp dokumentiem.
Neveido kļūdu piezīmes. Nevērtē pareizību. Tikai strukturēti fakti.

Īpaši izvelc:
- diametrus, materiālus, markas, tipus, klases, daudzumus, jaudas, sistēmu kodus;
- specifikācijas pozīcijas un apjomus;
- rasējumu numurus, dokumentu identifikāciju;
- iekārtas, telpas, pieslēgumus, robežas starp sadaļām.

C2-2 kļūdu piemēru loģika, kas rāda, kādi fakti vēlāk var būt svarīgi:
{error_examples_text[:6000]}

Atbildi tikai JSON masīvā. Katram objektam:
- fact_id
- fact_type: system_reference / pipe_diameter / material / quantity / equipment / connection / interface / room_or_space / drawing_reference / specification_item / power_or_flow / document_identity / other_fact
- element
- parameter_name
- parameter_value
- unit
- source_file
- page
- block_id
- source_text
- confidence

Teksta bloki:
{text_blocks}
"""


def build_design_brief_prompt(
    requirements_text: str,
    text_blocks: str,
    audit_depth: str,
    confidence_threshold: float,
) -> str:
    return f"""
Tu pārbaudi BP dokumenta tekstu pret Design Brief prasību atmiņu.
Mērķis NAV izveidot prasību statusa sarakstu.
Mērķis ir atrast tikai tos auditējamā BP dokumenta teksta blokus, kuros redzama skaidra vai ticama pretruna pret Design Brief prasību.

NEZIŅO par prasībām, kas vienkārši nav atrastas.
NEZIŅO "vajag pārbaudīt" bez konkrēta teksta bloka.
Atgriez tikai tādus kandidātus, ko teorētiski varētu apvilkt PDF.

Atļautie issue_type:
- design_brief_direct_conflict
- design_brief_wrong_parameter
- design_brief_partial_solution_visible
- design_brief_scope_gap_with_anchor

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

Design Brief prasību atmiņas fragments:
{requirements_text}

Auditējamie BP teksta bloki:
{text_blocks}
"""


def build_single_doc_prompt(
    document_name: str,
    document_type: str,
    text_blocks: str,
    facts_text: str,
    error_examples_text: str,
    audit_depth: str,
    confidence_threshold: float,
) -> str:
    return f"""
Tu veic viena BP dokumenta iekšējo konsekvences pārbaudi.
Dokuments: {document_name}
Dokumenta tips: {document_type}

Meklē tikai kļūdas un nesakritības šī paša dokumenta ietvaros.
Izmanto C2-2 kļūdu piemēru loģiku kā kalibrāciju, nevis meklē identisku tekstu.

Meklē:
- diametru, materiālu, marku, sistēmu kodu, daudzumu pretrunas vienā dokumentā;
- LV/ENG vai paralēlu tekstu nesakritības;
- tabulu/rindu/pozīciju savstarpējas pretrunas;
- dokumenta nosaukuma, rasējuma numura, revīzijas, sadaļas identifikācijas nesakritības;
- specifikācijas apjomu vai materiālu neatbilstības, kas redzamas šajā dokumentā.

Nedod vispārīgas piezīmes. Katram kandidātam jābūt piesaistītam konkrētam teksta blokam.

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

C2-2 kļūdu piemēri:
{error_examples_text[:10000]}

Pagaidu fakti no šī dokumenta:
{facts_text[:14000]}

Dokumenta teksta bloki:
{text_blocks}
"""


def build_structured_discipline_pair_prompt(
    discipline_code: str,
    comparison_step: str,
    reference_label: str,
    reference_text: str,
    target_label: str,
    target_text: str,
    facts_text: str,
    error_examples_text: str,
    audit_depth: str,
    confidence_threshold: float,
) -> str:
    return f"""
Tu veic universālu būvprojekta disciplīnas iekšējās konsekvences pārbaudi.
Disciplīna: {discipline_code}
Salīdzināšanas solis: {comparison_step}

Šis nav jauns specifisks tests konkrētai disciplīnai. Šis ir universāls princips:
1. Skaidrojošais apraksts / vispārīgie dati ir orientieris tam, ko sadaļa apgalvo.
2. Rasējumi parāda, kā apgalvojumi realizēti grafiski un tekstuālos parametros.
3. Specifikācija rāda materiālus, markas, daudzumus un pozīcijas.
4. Piezīmi drīkst dot tikai par konkrētu tekstuālu bloku, ko varētu apvilkt PDF.

Tavs uzdevums šajā solī:
- salīdzini reference materiālu pret target materiālu;
- meklē skaidras vai diezgan ticamas pretrunas starp tiem;
- neatgriez vispārīgas piezīmes, ka “jāpārbauda”;
- neatgriez “nav atrasts” piezīmes bez konkrēta teksta enkura;
- ja target tekstā ir bloks, kas ir pretrunā reference materiālam, kā source_file/page/block_id izvēlies target bloku;
- ja reference tekstā ir bloks, kas ir tieši pretrunā target materiālam, source_file/page/block_id drīkst būt reference bloks;
- ja bloks satur tikai vienu īsu parametru, izmanto apkārtējo kontekstu, kas dots tekstā.

Īpaši meklē šādus universālus konfliktus:
- diametra konflikts: DN/D/OD/Ø vērtība nesakrīt;
- materiāla konflikts: PP/PVC/PE/tērauds/marka/tips nesakrīt;
- skaita vai daudzuma konflikts: gab., m, m2, m3 vai pozīciju skaits nesakrīt;
- sistēmas koda konflikts: K1/K2/K3/U1 vai līdzīgs kods lietots pretrunīgi;
- specifikācijas pozīcija neatbilst rasējuma tekstam vai aprakstam;
- iekārtas tips/marka/parametrs atšķiras;
- dokumenta identitāte, rasējuma numurs, nosaukums vai revīzija ir pretrunīga, ja tas var radīt audita piezīmi.

C2-2 kļūdu piemēri ir kalibrācija kļūdu loģikai, nevis teksts, kas jāmeklē burtiski:
{error_examples_text[:9000]}

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

Pagaidu faktu indekss no auditējamās disciplīnas:
{facts_text[:18000]}

REFERENCE — {reference_label}:
{reference_text[:24000]}

TARGET — {target_label}:
{target_text[:24000]}
"""


def build_interdisciplinary_prompt(
    discipline_code: str,
    prior_facts_text: str,
    current_facts_text: str,
    text_blocks: str,
    audit_depth: str,
    confidence_threshold: float,
) -> str:
    return f"""
Tu veic starpdisciplīnu konsekvences pārbaudi.
Auditējamā disciplīna: {discipline_code}

Salīdzini auditējamās disciplīnas tekstu/faktus pret 03_Memory jau saglabātajiem citu disciplīnu faktiem.
Piezīmi drīkst piesaistīt tikai auditējamās disciplīnas teksta blokam, nevis iepriekšējās disciplīnas memory faktam.

Meklē tikai skaidras pretrunas:
- diametrs/jauda/skaits/materiāls nesakrīt;
- sistēmas kods vai pieslēguma robeža nesakrīt;
- telpa/iekārta/pieslēgums vienā disciplīnā aprakstīts pretēji citai;
- scope/boundary conflict starp disciplīnām.

Nedod "jāpārbauda ar citu sadaļu" vispārīgu piezīmi. Vajag konkrētu auditējamās disciplīnas source_file/page/block_id.

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

Citu līdz šim auditēto disciplīnu fakti no 03_Memory:
{prior_facts_text[:18000]}

Auditējamās disciplīnas pagaidu fakti:
{current_facts_text[:12000]}

Auditējamās disciplīnas teksta bloki:
{text_blocks[:22000]}
"""


# =========================================================
# Datu sagatavošana AI promptiem
# =========================================================

def df_to_compact_text(df: pd.DataFrame, max_rows: int, cols: Optional[List[str]] = None) -> str:
    if df is None or df.empty:
        return ""

    use_df = df.copy()

    if cols:
        use_cols = [c for c in cols if c in use_df.columns]
        if use_cols:
            use_df = use_df[use_cols]

    return use_df.head(max_rows).fillna("").astype(str).to_csv(index=False)


def select_relevant_requirements(req_df: pd.DataFrame, discipline_code: str, max_rows: int) -> pd.DataFrame:
    if req_df.empty:
        return req_df

    disc = discipline_code.upper()

    def relevant(row):
        values = []

        for col in ["discipline_list", "applies_to_sections_list", "discipline", "applies_to_sections"]:
            if col in row:
                values.extend(parse_list_value(row.get(col)))

        values_upper = [v.upper() for v in values]

        return disc in values_upper or not values_upper

    filtered = req_df[req_df.apply(relevant, axis=1)].copy()

    if filtered.empty:
        filtered = req_df.copy()

    if "priority" in filtered.columns:
        filtered["priority_num"] = pd.to_numeric(filtered["priority"], errors="coerce").fillna(0)
        filtered = filtered.sort_values("priority_num", ascending=False)

    return filtered.head(max_rows)


def select_prior_facts(facts_df: pd.DataFrame, current_discipline: str, max_rows: int) -> pd.DataFrame:
    if facts_df.empty:
        return facts_df

    disc = current_discipline.upper()
    work = facts_df.copy()

    if "discipline" in work.columns:
        work = work[work["discipline"].astype(str).str.upper() != disc]

    elif "memory_discipline" in work.columns:
        work = work[work["memory_discipline"].astype(str).str.upper() != disc]

    return work.head(max_rows)


# =========================================================
# Issues post-processing
# =========================================================

def normalize_issue(issue: Dict[str, Any], default_mode: str, source_lookup: pd.DataFrame) -> Dict[str, Any]:
    item = dict(issue)

    item["audit_mode"] = item.get("audit_mode") or default_mode
    item["issue_type"] = item.get("issue_type") or "unknown"
    item["source_file"] = str(item.get("source_file") or "").strip()
    item["source_text"] = str(item.get("source_text") or "").strip()
    item["comment"] = str(item.get("comment") or "").strip()
    item["suggestion"] = str(item.get("suggestion") or "").strip()

    try:
        item["page"] = int(float(item.get("page")))
    except Exception:
        item["page"] = None

    try:
        item["block_id"] = int(float(item.get("block_id")))
    except Exception:
        item["block_id"] = None

    try:
        item["confidence"] = float(item.get("confidence"))
    except Exception:
        item["confidence"] = 0.0

    try:
        item["priority"] = int(float(item.get("priority")))
    except Exception:
        item["priority"] = 0

    if "include_in_pdf" not in item:
        item["include_in_pdf"] = False

    if isinstance(item["include_in_pdf"], str):
        item["include_in_pdf"] = item["include_in_pdf"].strip().lower() in ["true", "1", "yes", "jā", "ja"]

    item["has_anchor"] = False

    if item["source_file"] and item["page"] is not None and item["block_id"] is not None and not source_lookup.empty:
        match = source_lookup[
            (source_lookup["source_file"].astype(str) == item["source_file"])
            & (source_lookup["page"].astype(int) == int(item["page"]))
            & (source_lookup["block_id"].astype(int) == int(item["block_id"]))
        ]

        if not match.empty:
            item["has_anchor"] = True
            item["x0"] = match.iloc[0].get("x0")
            item["y0"] = match.iloc[0].get("y0")
            item["x1"] = match.iloc[0].get("x1")
            item["y1"] = match.iloc[0].get("y1")

            if not item["source_text"]:
                item["source_text"] = match.iloc[0].get("text", "")

    return item


def filter_issues(
    raw_df: pd.DataFrame,
    confidence_threshold: float,
    audit_depth: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if raw_df.empty:
        return raw_df, raw_df

    work = raw_df.copy()

    disallowed_types = [
        "not_found",
        "general_uncertainty",
        "please_check",
        "missing_without_anchor",
        "unanchored_possible_omission",
    ]

    if audit_depth == "Diagnostic":
        kept = work[work["has_anchor"] == True].copy()
        removed = work[work["has_anchor"] != True].copy()

        kept["passes_pdf_filter"] = (
            (kept["confidence"] >= confidence_threshold)
            & (kept["include_in_pdf"] == True)
            & (~kept["issue_type"].isin(disallowed_types))
        )

        return kept, removed

    mask = (
        (work["has_anchor"] == True)
        & (work["confidence"] >= confidence_threshold)
        & (work["include_in_pdf"] == True)
        & (~work["issue_type"].isin(disallowed_types))
    )

    kept = work[mask].copy()
    removed = work[~mask].copy()

    return kept, removed


def make_excel_bytes(
    df: pd.DataFrame,
    raw_df: pd.DataFrame,
    removed_df: pd.DataFrame,
    facts_df: pd.DataFrame,
) -> bytes:
    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="issues_filtered", index=False)
        raw_df.to_excel(writer, sheet_name="issues_raw", index=False)
        removed_df.to_excel(writer, sheet_name="issues_removed", index=False)
        facts_df.to_excel(writer, sheet_name="temp_facts", index=False)

    output.seek(0)
    return output.getvalue()


def make_json_bytes(
    df: pd.DataFrame,
    raw_df: pd.DataFrame,
    removed_df: pd.DataFrame,
    facts_df: pd.DataFrame,
    project_code: str,
    discipline_code: str,
) -> bytes:
    payload = {
        "schema": "bp_audit_universal_v3",
        "project_code": project_code,
        "discipline_code": discipline_code,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "filtered_count": len(df),
        "raw_count": len(raw_df),
        "removed_count": len(removed_df),
        "temp_facts_count": len(facts_df),
        "issues_filtered": df.where(pd.notna(df), None).to_dict(orient="records"),
        "issues_raw": raw_df.where(pd.notna(raw_df), None).to_dict(orient="records"),
        "issues_removed": removed_df.where(pd.notna(removed_df), None).to_dict(orient="records"),
        "temp_facts": facts_df.where(pd.notna(facts_df), None).to_dict(orient="records"),
    }

    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# =========================================================
# Session state init
# =========================================================

for key, default in {
    "disciplines_df": pd.DataFrame(),
    "memory_catalog_df": pd.DataFrame(),
    "requirements_df": pd.DataFrame(),
    "memory_facts_df": pd.DataFrame(),
    "prompt_catalog_df": pd.DataFrame(),
    "universal_prompt": "",
    "error_examples_text": "",
    "discipline_pdfs_df": pd.DataFrame(),
    "selected_docs_df": pd.DataFrame(),
    "blocks_df": pd.DataFrame(),
    "file_summary_df": pd.DataFrame(),
    "temp_facts_df": pd.DataFrame(),
    "issues_raw_df": pd.DataFrame(),
    "issues_filtered_df": pd.DataFrame(),
    "issues_removed_df": pd.DataFrame(),
    "raw_ai_log_df": pd.DataFrame(),
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# =========================================================
# UI: konfigurācija
# =========================================================

input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")
prompt_folder_id = st.secrets.get("GOOGLE_DRIVE_PROMPT_FOLDER_ID")

st.markdown("## 1. Konfigurācija")
st.write("Input folder ID:", input_folder_id)
st.write("Memory folder ID:", memory_folder_id)
st.write("Prompt folder ID:", prompt_folder_id)

project_code = st.text_input("Projekta kods", value="C2-3")
model = st.selectbox("AI modelis", options=["gpt-4.1-mini", "gpt-4.1"], index=0)

col_cfg1, col_cfg2 = st.columns(2)

with col_cfg1:
    max_pages_per_pdf = st.number_input(
        "Maksimālais lapu skaits no viena PDF",
        min_value=1,
        max_value=300,
        value=100,
        step=5,
    )

    max_blocks_per_ai = st.number_input(
        "Teksta bloku skaits vienā AI pieprasījumā",
        min_value=50,
        max_value=800,
        value=220,
        step=10,
    )

    max_design_requirements = st.number_input(
        "Maksimālais Design Brief prasību skaits promptā",
        min_value=20,
        max_value=500,
        value=180,
        step=10,
    )

with col_cfg2:
    max_memory_facts = st.number_input(
        "Maksimālais citu disciplīnu faktu skaits promptā",
        min_value=20,
        max_value=500,
        value=180,
        step=10,
    )

    confidence_threshold = st.slider(
        "Minimālā pārliecība anotējamām piezīmēm",
        min_value=0.0,
        max_value=1.0,
        value=0.60,
        step=0.05,
    )

    delay_between_ai_calls = st.number_input(
        "Pauze starp AI pieprasījumiem sekundēs",
        min_value=0.0,
        max_value=5.0,
        value=0.5,
        step=0.5,
    )

audit_depth = st.radio(
    "Audita dziļums",
    options=["Conservative", "Balanced", "Diagnostic"],
    index=2,
    horizontal=True,
)

st.markdown("### Audita moduļi")

run_module_a = st.checkbox("A. Pret Design Brief prasību atmiņu", value=True)
run_module_b = st.checkbox("B. Katra dokumenta iekšējā konsekvence", value=True)
run_module_c = st.checkbox("C. Disciplīnas savstarpējā konsekvence", value=True)
run_module_d = st.checkbox("D. Starpdisciplīnu konsekvence pret līdzšinējo 03_Memory", value=False)

st.info(
    "v3 režīmā C modulis salīdzina: apraksts ↔ rasējumi, apraksts ↔ specifikācija, "
    "rasējumi ↔ specifikācija; rīks rāda arī raw AI kandidātus un izfiltrētās rindas. "
    "PDF anotēšana vēl nav ieslēgta."
)

if st.button("1) Nolasīt 01_Input, 03_Memory un 04_Prompt"):
    try:
        drive_service = get_drive_service()

        st.session_state.disciplines_df = get_discipline_folders(drive_service, input_folder_id)

        mem_catalog, req_df, facts_df = load_memory_json_files(drive_service, memory_folder_id)
        prompt_catalog, universal_prompt, error_examples_text = load_prompt_assets(drive_service, prompt_folder_id)

        st.session_state.memory_catalog_df = mem_catalog
        st.session_state.requirements_df = req_df
        st.session_state.memory_facts_df = facts_df
        st.session_state.prompt_catalog_df = prompt_catalog
        st.session_state.universal_prompt = universal_prompt
        st.session_state.error_examples_text = error_examples_text

        st.success(
            f"Nolasīts: disciplīnas {len(st.session_state.disciplines_df)}, "
            f"Design Brief prasības {len(req_df)}, disciplīnu fakti {len(facts_df)}, "
            f"kļūdu piemēru teksta garums {len(error_examples_text)}."
        )

    except Exception as e:
        st.error("Neizdevās nolasīt sākuma datus.")
        st.exception(e)

if not st.session_state.memory_catalog_df.empty:
    with st.expander("03_Memory katalogs"):
        st.dataframe(st.session_state.memory_catalog_df, use_container_width=True)

if not st.session_state.prompt_catalog_df.empty:
    with st.expander("04_Prompt katalogs"):
        st.dataframe(st.session_state.prompt_catalog_df, use_container_width=True)


# =========================================================
# UI: disciplīnas izvēle
# =========================================================

disciplines_df = st.session_state.disciplines_df

if not disciplines_df.empty:
    st.markdown("## 2. Izvēlies auditējamo disciplīnu")
    st.dataframe(disciplines_df, use_container_width=True)

    folder_options = disciplines_df["folder_name"].tolist()
    default_index = folder_options.index("09_UKT") if "09_UKT" in folder_options else 0

    selected_folder_name = st.selectbox(
        "Disciplīnas mape",
        options=folder_options,
        index=default_index,
    )

    selected_row = disciplines_df[disciplines_df["folder_name"] == selected_folder_name].iloc[0]
    selected_discipline_code = selected_row["discipline_code"]
    selected_folder_id = selected_row["folder_id"]

    st.write("Izvēlētā disciplīna:", selected_discipline_code)

    if st.button("2) Atrast disciplīnas PDF failus"):
        try:
            drive_service = get_drive_service()
            pdfs_df = get_pdf_documents_in_discipline(
                drive_service,
                selected_folder_id,
                selected_folder_name,
            )

            st.session_state.discipline_pdfs_df = pdfs_df

            if pdfs_df.empty:
                st.warning("Disciplīnā nav atrasti PDF faili.")
            else:
                st.success(f"Atrasti {len(pdfs_df)} PDF faili.")

        except Exception as e:
            st.error("Neizdevās atrast PDF failus.")
            st.exception(e)


# =========================================================
# UI: PDF izvēle un teksta bloki
# =========================================================

pdfs_df = st.session_state.discipline_pdfs_df

if not pdfs_df.empty:
    st.markdown("## 3. Disciplīnas PDF faili")
    st.dataframe(
        pdfs_df[["name", "path", "document_type", "size", "modifiedTime"]],
        use_container_width=True,
    )

    file_options = pdfs_df["path"].tolist()

    preferred_types = ["explanatory_note", "specification", "general_data", "drawing"]
    default_paths = []

    for dtype in preferred_types:
        matches = pdfs_df[pdfs_df["document_type"] == dtype]["path"].tolist()
        default_paths.extend(matches[:2 if dtype == "drawing" else 1])

    default_paths = list(dict.fromkeys(default_paths))[:6]

    selected_paths = st.multiselect(
        "Izvēlies PDF failus auditam",
        options=file_options,
        default=default_paths,
    )

    selected_docs_df = pdfs_df[pdfs_df["path"].isin(selected_paths)].copy()
    st.session_state.selected_docs_df = selected_docs_df

    st.markdown("### Auditam izvēlētie faili")
    st.dataframe(
        selected_docs_df[["name", "path", "document_type", "size"]],
        use_container_width=True,
    )

    if st.button("3) Izvilkt PDF teksta blokus"):
        try:
            drive_service = get_drive_service()

            all_blocks = []
            file_summaries = []

            for _, doc_row in selected_docs_df.iterrows():
                file_name = doc_row["name"]
                file_id = doc_row["id"]

                pdf_bytes = download_drive_file_bytes(drive_service, file_id)
                blocks_df, total_pages = extract_pdf_page_blocks(pdf_bytes, int(max_pages_per_pdf))

                if not blocks_df.empty:
                    blocks_df["source_file"] = file_name
                    blocks_df["drive_file_id"] = file_id
                    blocks_df["drive_path"] = doc_row["path"]
                    blocks_df["document_type"] = doc_row["document_type"]
                    blocks_df["discipline"] = get_discipline_code_from_folder_name(selected_folder_name)
                    all_blocks.append(blocks_df)

                file_summaries.append({
                    "source_file": file_name,
                    "drive_file_id": file_id,
                    "drive_path": doc_row["path"],
                    "document_type": doc_row["document_type"],
                    "total_pages": total_pages,
                    "processed_pages": min(total_pages, int(max_pages_per_pdf)),
                    "text_blocks": len(blocks_df),
                })

            combined_blocks = pd.concat(all_blocks, ignore_index=True) if all_blocks else pd.DataFrame()

            st.session_state.blocks_df = combined_blocks
            st.session_state.file_summary_df = pd.DataFrame(file_summaries)

            st.success(f"Izvilkti {len(combined_blocks)} teksta bloki no {len(file_summaries)} PDF failiem.")

        except Exception as e:
            st.error("Neizdevās izvilkt PDF teksta blokus.")
            st.exception(e)


blocks_df = st.session_state.blocks_df

if not blocks_df.empty:
    st.markdown("## 4. PDF teksta bloku indekss")

    st.markdown("### Failu kopsavilkums")
    st.dataframe(st.session_state.file_summary_df, use_container_width=True)

    st.markdown("### Teksta bloku priekšskatījums")
    st.dataframe(
        blocks_df[["source_file", "document_type", "page", "block_id", "text", "x0", "y0", "x1", "y1"]].head(100),
        use_container_width=True,
    )

    if st.button("4) Palaist universālo auditu v3"):
        try:
            client = get_openai_client()
            discipline_code = blocks_df["discipline"].iloc[0]
            error_examples_text = st.session_state.error_examples_text or st.session_state.universal_prompt or ""
            source_lookup = blocks_df[["source_file", "page", "block_id", "text", "x0", "y0", "x1", "y1"]].copy()

            all_raw_issues: List[Dict[str, Any]] = []
            all_temp_facts: List[Dict[str, Any]] = []
            raw_ai_log: List[Dict[str, Any]] = []

            st.markdown("## 5. Audita izpilde")

            # -------------------------------------------------
            # 0. Pagaidu faktu indekss
            # -------------------------------------------------

            st.markdown("### 0. Pagaidu faktu indekss")

            fact_batches = make_block_batches(blocks_df, int(max_blocks_per_ai))
            fact_progress = st.progress(0)

            for i, batch_df in enumerate(fact_batches, start=1):
                st.write(f"Faktu indekss: batch {i}/{len(fact_batches)}")

                text_blocks = build_text_from_blocks(batch_df, int(max_blocks_per_ai))
                prompt = build_fact_prompt(
                    project_code,
                    discipline_code,
                    text_blocks,
                    f"batch {i}",
                    error_examples_text,
                )

                facts, raw = call_ai_json_array(
                    client,
                    model,
                    "Tu atbildi tikai derīgā JSON masīvā. Bez paskaidrojumiem.",
                    prompt,
                    temperature=0.0,
                )

                for j, fact in enumerate(facts, start=1):
                    fact = dict(fact)
                    fact["temp_fact_id"] = fact.get("fact_id") or f"TEMP-{i:03d}-{j:03d}"
                    fact["project_code"] = project_code
                    fact["discipline"] = discipline_code
                    fact["batch_index"] = i
                    all_temp_facts.append(fact)

                raw_ai_log.append({
                    "module": "facts",
                    "batch": i,
                    "raw_length": len(raw),
                    "parsed_count": len(facts),
                })

                fact_progress.progress(i / len(fact_batches))

                if delay_between_ai_calls:
                    time.sleep(float(delay_between_ai_calls))

            temp_facts_df = pd.DataFrame(all_temp_facts)
            st.session_state.temp_facts_df = temp_facts_df

            st.success(f"Pagaidu faktu indeksā iegūti {len(temp_facts_df)} fakti.")

            temp_facts_text = df_to_compact_text(
                temp_facts_df,
                max_rows=350,
                cols=[
                    "temp_fact_id",
                    "fact_type",
                    "element",
                    "parameter_name",
                    "parameter_value",
                    "unit",
                    "source_file",
                    "page",
                    "block_id",
                    "source_text",
                    "confidence",
                ],
            )

            # -------------------------------------------------
            # A. Design Brief conflict
            # -------------------------------------------------

            if run_module_a:
                st.markdown("### A. Design Brief conflict")

                relevant_req = select_relevant_requirements(
                    st.session_state.requirements_df,
                    discipline_code,
                    int(max_design_requirements),
                )

                req_text = df_to_compact_text(
                    relevant_req,
                    max_rows=int(max_design_requirements),
                    cols=[
                        "memory_id",
                        "engineering_system",
                        "discipline",
                        "applies_to_sections",
                        "requirement",
                        "condition",
                        "source_file",
                        "page",
                        "priority",
                    ],
                )

                batches = make_block_batches(blocks_df, int(max_blocks_per_ai))
                progress = st.progress(0)

                for i, batch_df in enumerate(batches, start=1):
                    st.write(f"Design Brief audits: batch {i}/{len(batches)}")

                    text_blocks = build_text_from_blocks(batch_df, int(max_blocks_per_ai))
                    prompt = build_design_brief_prompt(
                        req_text,
                        text_blocks,
                        audit_depth,
                        confidence_threshold,
                    )

                    issues, raw = call_ai_json_array(
                        client,
                        model,
                        "Tu atbildi tikai JSON masīvā.",
                        prompt,
                        0.0,
                    )

                    for issue in issues:
                        all_raw_issues.append(
                            normalize_issue(issue, "design_brief_conflict", source_lookup)
                        )

                    raw_ai_log.append({
                        "module": "A",
                        "batch": i,
                        "raw_length": len(raw),
                        "parsed_count": len(issues),
                    })

                    progress.progress(i / len(batches))

                    if delay_between_ai_calls:
                        time.sleep(float(delay_between_ai_calls))

            # -------------------------------------------------
            # B. Single document consistency
            # -------------------------------------------------

            if run_module_b:
                st.markdown("### B. Single document consistency")

                for doc_index, (source_file, doc_blocks) in enumerate(blocks_df.groupby("source_file"), start=1):
                    document_type = str(doc_blocks["document_type"].iloc[0])

                    st.write(
                        f"Viena dokumenta audits: {source_file} "
                        f"({doc_index}/{blocks_df['source_file'].nunique()})"
                    )

                    if not temp_facts_df.empty and "source_file" in temp_facts_df.columns:
                        doc_facts = temp_facts_df[
                            temp_facts_df["source_file"].astype(str) == str(source_file)
                        ].copy()
                    else:
                        doc_facts = pd.DataFrame()

                    facts_text = df_to_compact_text(
                        doc_facts,
                        220,
                        [
                            "temp_fact_id",
                            "fact_type",
                            "element",
                            "parameter_name",
                            "parameter_value",
                            "unit",
                            "page",
                            "block_id",
                            "source_text",
                            "confidence",
                        ],
                    )

                    batches = make_block_batches(doc_blocks, int(max_blocks_per_ai))

                    for i, batch_df in enumerate(batches, start=1):
                        text_blocks = build_text_from_blocks(batch_df, int(max_blocks_per_ai))

                        prompt = build_single_doc_prompt(
                            source_file,
                            document_type,
                            text_blocks,
                            facts_text,
                            error_examples_text,
                            audit_depth,
                            confidence_threshold,
                        )

                        issues, raw = call_ai_json_array(
                            client,
                            model,
                            "Tu atbildi tikai JSON masīvā.",
                            prompt,
                            0.0,
                        )

                        for issue in issues:
                            all_raw_issues.append(
                                normalize_issue(issue, "single_document_consistency", source_lookup)
                            )

                        raw_ai_log.append({
                            "module": "B",
                            "source_file": source_file,
                            "batch": i,
                            "raw_length": len(raw),
                            "parsed_count": len(issues),
                        })

                        if delay_between_ai_calls:
                            time.sleep(float(delay_between_ai_calls))

            # -------------------------------------------------
            # C. Discipline consistency — strukturēta salīdzināšana
            # -------------------------------------------------

            if run_module_c:
                st.markdown("### C. Discipline consistency — strukturēti soļi")

                orientation_blocks = get_orientation_blocks(blocks_df)
                drawing_blocks = get_blocks_by_type(blocks_df, ["drawing"])
                specification_blocks = get_blocks_by_type(blocks_df, ["specification"])
                general_blocks = get_blocks_by_type(blocks_df, ["general_data"])

                c_steps = []

                if not orientation_blocks.empty and not drawing_blocks.empty:
                    c_steps.append({
                        "step_id": "C1",
                        "label": "explanatory_or_general_vs_drawings",
                        "reference_label": "skaidrojošais apraksts / vispārīgie dati",
                        "reference_blocks": orientation_blocks,
                        "target_label": "rasējumi",
                        "target_blocks": drawing_blocks,
                    })

                if not orientation_blocks.empty and not specification_blocks.empty:
                    c_steps.append({
                        "step_id": "C2",
                        "label": "explanatory_or_general_vs_specification",
                        "reference_label": "skaidrojošais apraksts / vispārīgie dati",
                        "reference_blocks": orientation_blocks,
                        "target_label": "specifikācija",
                        "target_blocks": specification_blocks,
                    })

                if not drawing_blocks.empty and not specification_blocks.empty:
                    c_steps.append({
                        "step_id": "C3a",
                        "label": "drawings_vs_specification",
                        "reference_label": "rasējumi",
                        "reference_blocks": drawing_blocks,
                        "target_label": "specifikācija",
                        "target_blocks": specification_blocks,
                    })

                    c_steps.append({
                        "step_id": "C3b",
                        "label": "specification_vs_drawings",
                        "reference_label": "specifikācija",
                        "reference_blocks": specification_blocks,
                        "target_label": "rasējumi",
                        "target_blocks": drawing_blocks,
                    })

                if not general_blocks.empty and not general_blocks.equals(orientation_blocks):
                    other_blocks = blocks_df[blocks_df["document_type"] != "general_data"].copy()

                    if not other_blocks.empty:
                        c_steps.append({
                            "step_id": "C4",
                            "label": "general_data_vs_other_documents",
                            "reference_label": "vispārīgie dati",
                            "reference_blocks": general_blocks,
                            "target_label": "pārējie disciplīnas dokumenti",
                            "target_blocks": other_blocks,
                        })

                if not c_steps:
                    st.info("C modulim nav pietiekamu dokumentu tipu strukturētai salīdzināšanai.")

                else:
                    total_c_batches = sum(
                        len(make_block_batches(step["target_blocks"], int(max_blocks_per_ai)))
                        for step in c_steps
                    )

                    done_c_batches = 0
                    progress = st.progress(0)

                    for step in c_steps:
                        step_id = step["step_id"]
                        label = step["label"]

                        st.write(f"{step_id}: {label}")

                        reference_text = build_context_text(
                            blocks_df,
                            step["reference_blocks"].head(int(max_blocks_per_ai)),
                            max_blocks=int(max_blocks_per_ai),
                            context_window=2,
                        )

                        target_batches = make_block_batches(
                            step["target_blocks"],
                            int(max_blocks_per_ai),
                        )

                        for i, target_batch_df in enumerate(target_batches, start=1):
                            target_text = build_context_text(
                                blocks_df,
                                target_batch_df,
                                max_blocks=int(max_blocks_per_ai),
                                context_window=2,
                            )

                            prompt = build_structured_discipline_pair_prompt(
                                discipline_code=discipline_code,
                                comparison_step=f"{step_id} {label} batch {i}/{len(target_batches)}",
                                reference_label=step["reference_label"],
                                reference_text=reference_text,
                                target_label=step["target_label"],
                                target_text=target_text,
                                facts_text=temp_facts_text,
                                error_examples_text=error_examples_text,
                                audit_depth=audit_depth,
                                confidence_threshold=confidence_threshold,
                            )

                            issues, raw = call_ai_json_array(
                                client,
                                model,
                                "Tu atbildi tikai JSON masīvā.",
                                prompt,
                                0.0,
                            )

                            for issue in issues:
                                normalized = normalize_issue(
                                    issue,
                                    "discipline_consistency",
                                    source_lookup,
                                )
                                normalized["comparison_step"] = step_id
                                normalized["comparison_label"] = label
                                all_raw_issues.append(normalized)

                            raw_ai_log.append({
                                "module": "C",
                                "step": step_id,
                                "label": label,
                                "batch": i,
                                "raw_length": len(raw),
                                "parsed_count": len(issues),
                            })

                            done_c_batches += 1
                            progress.progress(done_c_batches / max(total_c_batches, 1))

                            if delay_between_ai_calls:
                                time.sleep(float(delay_between_ai_calls))

            # -------------------------------------------------
            # D. Interdisciplinary consistency
            # -------------------------------------------------

            if run_module_d:
                st.markdown("### D. Starpdisciplīnu konsekvence")

                prior_facts = select_prior_facts(
                    st.session_state.memory_facts_df,
                    discipline_code,
                    int(max_memory_facts),
                )

                if prior_facts.empty:
                    st.info("03_Memory nav citu disciplīnu faktu, pret ko salīdzināt.")

                else:
                    prior_text = df_to_compact_text(
                        prior_facts,
                        int(max_memory_facts),
                        [
                            "memory_id",
                            "fact_id",
                            "discipline",
                            "fact_type",
                            "element",
                            "parameter_name",
                            "parameter_value",
                            "unit",
                            "source_file",
                            "page",
                            "block_id",
                            "source_text",
                        ],
                    )

                    batches = make_block_batches(blocks_df, int(max_blocks_per_ai))
                    progress = st.progress(0)

                    for i, batch_df in enumerate(batches, start=1):
                        st.write(f"Starpdisciplīnu audits: batch {i}/{len(batches)}")

                        text_blocks = build_text_from_blocks(batch_df, int(max_blocks_per_ai))

                        prompt = build_interdisciplinary_prompt(
                            discipline_code,
                            prior_text,
                            temp_facts_text,
                            text_blocks,
                            audit_depth,
                            confidence_threshold,
                        )

                        issues, raw = call_ai_json_array(
                            client,
                            model,
                            "Tu atbildi tikai JSON masīvā.",
                            prompt,
                            0.0,
                        )

                        for issue in issues:
                            all_raw_issues.append(
                                normalize_issue(issue, "interdisciplinary_consistency", source_lookup)
                            )

                        raw_ai_log.append({
                            "module": "D",
                            "batch": i,
                            "raw_length": len(raw),
                            "parsed_count": len(issues),
                        })

                        progress.progress(i / len(batches))

                        if delay_between_ai_calls:
                            time.sleep(float(delay_between_ai_calls))

            raw_df = pd.DataFrame(all_raw_issues)

            if not raw_df.empty:
                if "issue_id" not in raw_df.columns:
                    raw_df["issue_id"] = ""

                raw_df["issue_id"] = [
                    v if str(v).strip() else f"{project_code}-{discipline_code}-ISSUE-{i + 1:04d}"
                    for i, v in enumerate(raw_df["issue_id"].tolist())
                ]

            filtered_df, removed_df = filter_issues(
                raw_df,
                float(confidence_threshold),
                audit_depth,
            )

            st.session_state.issues_raw_df = raw_df
            st.session_state.issues_filtered_df = filtered_df
            st.session_state.issues_removed_df = removed_df
            st.session_state.raw_ai_log_df = pd.DataFrame(raw_ai_log)

            st.success(
                f"AI raw kandidāti: {len(raw_df)}; "
                f"pēc filtra redzamie: {len(filtered_df)}; "
                f"izfiltrēti: {len(removed_df)}."
            )

        except Exception as e:
            st.error("Kļūda universālā audita izpildē.")
            st.exception(e)


# =========================================================
# Rezultāti
# =========================================================

raw_df = st.session_state.issues_raw_df
filtered_df = st.session_state.issues_filtered_df
removed_df = st.session_state.issues_removed_df
temp_facts_df = st.session_state.temp_facts_df

if not temp_facts_df.empty or not raw_df.empty:
    st.markdown("## 6. Diagnostika un rezultāti")

    if "raw_ai_log_df" in st.session_state and not st.session_state.raw_ai_log_df.empty:
        st.markdown("### AI pieprasījumu diagnostika")
        st.dataframe(st.session_state.raw_ai_log_df, use_container_width=True)

    if not temp_facts_df.empty:
        st.markdown("### Pagaidu faktu indekss")

        if "fact_type" in temp_facts_df.columns:
            fact_summary = (
                temp_facts_df
                .groupby("fact_type")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            st.dataframe(fact_summary, use_container_width=True)

        st.dataframe(temp_facts_df.head(300), use_container_width=True)

    st.markdown("### Issues kopsavilkums")

    col1, col2, col3 = st.columns(3)

    col1.metric("Raw kandidāti", len(raw_df))
    col2.metric("Redzamie pēc filtra", len(filtered_df))
    col3.metric("Izfiltrēti", len(removed_df))

    if not filtered_df.empty:
        st.markdown("### Redzamās kandidātpiezīmes")

        if "audit_mode" in filtered_df.columns:
            st.dataframe(
                filtered_df.groupby("audit_mode").size().reset_index(name="count"),
                use_container_width=True,
            )

        if "issue_type" in filtered_df.columns:
            st.dataframe(
                filtered_df.groupby("issue_type").size().reset_index(name="count"),
                use_container_width=True,
            )

        st.dataframe(filtered_df, use_container_width=True)

    else:
        st.info("Pēc izvēlētā filtra nav redzamu kandidātpiezīmju.")

    if not raw_df.empty:
        with st.expander("Raw AI kandidāti pirms gala filtra"):
            st.dataframe(raw_df, use_container_width=True)

    if not removed_df.empty:
        with st.expander("Izfiltrētie kandidāti un iemeslu pārbaude"):
            st.dataframe(removed_df, use_container_width=True)

    excel_bytes = make_excel_bytes(filtered_df, raw_df, removed_df, temp_facts_df)

    json_bytes = make_json_bytes(
        filtered_df,
        raw_df,
        removed_df,
        temp_facts_df,
        project_code,
        blocks_df["discipline"].iloc[0] if not blocks_df.empty else "",
    )

    c1, c2 = st.columns(2)

    with c1:
        st.download_button(
            "Lejupielādēt universal audit Excel",
            data=excel_bytes,
            file_name=f"{project_code.lower().replace('-', '_')}_universal_audit_v3.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with c2:
        st.download_button(
            "Lejupielādēt universal audit JSON",
            data=json_bytes,
            file_name=f"{project_code.lower().replace('-', '_')}_universal_audit_v3.json",
            mime="application/json",
        )
