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


st.set_page_config(page_title="BP universālais audits", layout="wide")

st.title("BP universālais audita rīks")

st.write(
    "Šī aplikācija pārbauda vienu izvēlētu būvprojekta disciplīnu pēc vieniem principiem: "
    "pret Design Brief prasību atmiņu, katra dokumenta ietvaros, disciplīnas ietvaros un "
    "pret līdz šim 03_Memory saglabātajām citu disciplīnu faktu atmiņām. "
    "Rezultātā tiek rādītas tikai tādas kandidātpiezīmes, kuras var piesaistīt konkrētam PDF teksta blokam."
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


@st.cache_data(show_spinner=False)
def _cached_list_folder_items(_dummy: str, folder_id: str, service_account_json: str) -> List[Dict[str, Any]]:
    service_account_info = json.loads(service_account_json)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    service = build("drive", "v3", credentials=credentials)
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


def list_folder_items(service, folder_id: str) -> List[Dict[str, Any]]:
    # Neizmantojam cache, ja nav secrets string pieejams; tas atvieglo debug.
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


def download_drive_file_bytes(service, file_id: str, mime_type: Optional[str] = None) -> bytes:
    if mime_type == GOOGLE_SHEET_MIME_TYPE:
        request = service.files().export_media(
            fileId=file_id,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    elif mime_type == GOOGLE_DOC_MIME_TYPE:
        request = service.files().export_media(
            fileId=file_id,
            mimeType="text/plain",
        )
    else:
        request = service.files().get_media(fileId=file_id)

    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


def download_text_file(service, file_id: str, mime_type: Optional[str] = None) -> str:
    return download_drive_file_bytes(service, file_id, mime_type=mime_type).decode("utf-8", errors="replace")


# =========================================================
# Basic helpers
# =========================================================


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def parse_list_value(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    text = str(value).strip()

    if not text or text.lower() == "nan":
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass

    parts = re.split(r"[,;]", text)
    return [part.strip() for part in parts if part.strip()]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def clean_excel_illegal_chars(value):
    if isinstance(value, str):
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)
    return value


def clean_dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()
    for col in cleaned.columns:
        cleaned[col] = cleaned[col].map(clean_excel_illegal_chars)
        cleaned[col] = cleaned[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
    return cleaned


# =========================================================
# Discipline and document discovery
# =========================================================


def get_discipline_code_from_folder_name(folder_name: str) -> str:
    name = str(folder_name).strip()
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return name.strip()


def classify_document_type(file_name: str, path: str = "") -> str:
    text = f"{file_name} {path}".lower()

    if any(keyword in text for keyword in [
        "explanatory", "description", "skaidrojo", "apraksts", "skaidroj", "_td_", "td_", "note"
    ]):
        return "explanatory_note"

    if any(keyword in text for keyword in [
        "specification", "specifik", "apjomi", "boq", "bill of quantities", "_ms_", "ms_"
    ]):
        return "specification"

    if any(keyword in text for keyword in [
        "general data", "vispār", "vispar", "drawing list", "rasējumu saraksts", "general"
    ]):
        return "general_data"

    if any(keyword in text for keyword in ["calculation", "aprēķ", "aprek", "calcs"]):
        return "calculation"

    if any(keyword in text for keyword in [
        "scheme", "layout", "section", "plan", "floor", "site plan", "drawing",
        "rasēj", "rasej", "plāns", "plans", "griezums", "shēma", "shema", "_ra_", "ra_",
        "profile"
    ]):
        return "drawing"

    return "other_pdf"


def get_discipline_folders(service, input_folder_id: str) -> pd.DataFrame:
    items = list_folder_items(service, input_folder_id)
    folders = [item for item in items if item.get("mimeType") == FOLDER_MIME_TYPE]

    rows = []
    for item in folders:
        folder_name = item.get("name", "")
        rows.append(
            {
                "folder_name": folder_name,
                "discipline_code": get_discipline_code_from_folder_name(folder_name),
                "folder_id": item.get("id"),
                "modifiedTime": item.get("modifiedTime", ""),
            }
        )

    if not rows:
        return pd.DataFrame(columns=["folder_name", "discipline_code", "folder_id", "modifiedTime"])

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
    pdf_df = df[(df["is_folder"] == False) & (df["mimeType"] == PDF_MIME_TYPE)].copy()

    if pdf_df.empty:
        return pdf_df

    pdf_df["document_type"] = pdf_df.apply(
        lambda row: classify_document_type(row.get("name", ""), row.get("path", "")),
        axis=1,
    )

    return pdf_df


# =========================================================
# 03_Memory reading
# =========================================================


def detect_memory_kind(file_name: str, payload: Any) -> str:
    name = file_name.lower()
    if isinstance(payload, dict):
        schema = str(payload.get("memory_schema", "")).lower()
        if "requirement" in schema or "requirements" in payload:
            return "design_brief_requirements"
        if "discipline" in schema or "facts" in payload:
            return "discipline_facts"
    if "requirements" in name or "mep_requirements" in name:
        return "design_brief_requirements"
    if "facts" in name:
        return "discipline_facts"
    return "unknown_json"


def extract_records_from_memory_payload(payload: Any, kind: str) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        if kind == "design_brief_requirements" and isinstance(payload.get("requirements"), list):
            return payload["requirements"]
        if kind == "discipline_facts" and isinstance(payload.get("facts"), list):
            return payload["facts"]
        for key in ["records", "items", "data"]:
            if isinstance(payload.get(key), list):
                return payload[key]
    if isinstance(payload, list):
        return payload
    return []


def load_project_memory(service, memory_folder_id: str) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    items = list_folder_items(service, memory_folder_id)
    json_items = [item for item in items if str(item.get("name", "")).lower().endswith(".json")]

    catalog_rows = []
    requirements_rows = []
    facts_rows = []

    for item in json_items:
        file_name = item.get("name", "")
        try:
            raw_text = download_text_file(service, item.get("id"), mime_type=item.get("mimeType"))
            payload = json.loads(raw_text)
            kind = detect_memory_kind(file_name, payload)
            records = extract_records_from_memory_payload(payload, kind)

            catalog_rows.append(
                {
                    "name": file_name,
                    "kind": kind,
                    "records_count": len(records),
                    "memory_schema": payload.get("memory_schema") if isinstance(payload, dict) else "",
                    "mimeType": item.get("mimeType"),
                    "size": item.get("size", ""),
                    "modifiedTime": item.get("modifiedTime", ""),
                    "id": item.get("id"),
                }
            )

            if kind == "design_brief_requirements":
                for record in records:
                    if isinstance(record, dict):
                        row = dict(record)
                        row["memory_source_file"] = file_name
                        requirements_rows.append(row)
            elif kind == "discipline_facts":
                detected_discipline = ""
                match = re.search(r"c2_3_([a-zA-Z0-9\-]+)_facts", file_name)
                if match:
                    detected_discipline = match.group(1).upper()
                for record in records:
                    if isinstance(record, dict):
                        row = dict(record)
                        row["memory_source_file"] = file_name
                        if not row.get("memory_discipline"):
                            row["memory_discipline"] = detected_discipline or row.get("discipline", "")
                        facts_rows.append(row)
        except Exception as e:
            catalog_rows.append(
                {
                    "name": file_name,
                    "kind": "error",
                    "records_count": 0,
                    "memory_schema": "",
                    "error": str(e),
                    "mimeType": item.get("mimeType"),
                    "size": item.get("size", ""),
                    "modifiedTime": item.get("modifiedTime", ""),
                    "id": item.get("id"),
                }
            )

    catalog_df = pd.DataFrame(catalog_rows)
    requirements_df = pd.DataFrame(requirements_rows)
    facts_df = pd.DataFrame(facts_rows)

    return catalog_df, requirements_df, facts_df


# =========================================================
# 04_Prompt reading
# =========================================================


def read_prompt_materials(service, prompt_folder_id: str) -> Dict[str, Any]:
    materials: Dict[str, Any] = {
        "universal_prompt": "",
        "error_examples_df": pd.DataFrame(),
        "error_examples_text": "",
        "prompt_catalog": pd.DataFrame(),
    }

    if not prompt_folder_id:
        return materials

    items = list_folder_items(service, prompt_folder_id)
    materials["prompt_catalog"] = pd.DataFrame(items)

    # universal prompt txt
    for item in items:
        name = str(item.get("name", "")).lower()
        if name == "universal_bp_audit_prompt.txt" or ("universal" in name and name.endswith(".txt")):
            try:
                materials["universal_prompt"] = download_text_file(service, item.get("id"), mime_type=item.get("mimeType"))
            except Exception:
                pass
            break

    # examples xlsx / google sheet
    for item in items:
        name = str(item.get("name", "")).lower()
        mime = item.get("mimeType", "")
        if name.endswith(".xlsx") or mime == GOOGLE_SHEET_MIME_TYPE or "error_examples" in name:
            try:
                bytes_data = download_drive_file_bytes(service, item.get("id"), mime_type=mime)
                df = pd.read_excel(io.BytesIO(bytes_data))
                materials["error_examples_df"] = df
                materials["error_examples_text"] = error_examples_to_text(df, max_rows=40)
                break
            except Exception:
                continue

    return materials


def error_examples_to_text(df: pd.DataFrame, max_rows: int = 40) -> str:
    if df is None or df.empty:
        return ""

    use_df = df.head(max_rows).copy()
    lines = []

    for idx, row in use_df.iterrows():
        parts = []
        for col in use_df.columns:
            value = normalize_text(row.get(col))
            if value:
                parts.append(f"{col}: {value}")
        if parts:
            lines.append(f"Piemērs {idx + 1}: " + " | ".join(parts))

    return "\n".join(lines)


# =========================================================
# PDF text block index
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
                        "block_id": int(block_index),
                        "x0": round(float(x0), 2),
                        "y0": round(float(y0), 2),
                        "x1": round(float(x1), 2),
                        "y1": round(float(y1), 2),
                        "text": clean_text,
                    }
                )

    return pd.DataFrame(rows), total_pages


def extract_selected_pdf_blocks(service, selected_docs_df: pd.DataFrame, max_pages: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    all_blocks = []
    file_summary = []

    for _, doc_row in selected_docs_df.iterrows():
        file_name = doc_row["name"]
        file_id = doc_row["id"]
        mime_type = doc_row["mimeType"]
        document_type = doc_row.get("document_type", "other_pdf")
        drive_path = doc_row.get("path", "")

        pdf_bytes = download_drive_file_bytes(service, file_id, mime_type=mime_type)
        blocks_df, total_pages = extract_pdf_page_blocks(pdf_bytes, max_pages=max_pages)

        if not blocks_df.empty:
            blocks_df["source_file"] = file_name
            blocks_df["drive_file_id"] = file_id
            blocks_df["drive_path"] = drive_path
            blocks_df["document_type"] = document_type
            all_blocks.append(blocks_df)

        file_summary.append(
            {
                "source_file": file_name,
                "drive_file_id": file_id,
                "drive_path": drive_path,
                "document_type": document_type,
                "total_pages": total_pages,
                "processed_pages": min(total_pages, max_pages),
                "text_blocks": len(blocks_df),
            }
        )

    if all_blocks:
        combined = pd.concat(all_blocks, ignore_index=True)
    else:
        combined = pd.DataFrame()

    return combined, pd.DataFrame(file_summary)


def make_block_batches(blocks_df: pd.DataFrame, max_blocks_per_batch: int) -> List[pd.DataFrame]:
    if blocks_df.empty:
        return []
    batches = []
    for start in range(0, len(blocks_df), max_blocks_per_batch):
        batches.append(blocks_df.iloc[start:start + max_blocks_per_batch].copy())
    return batches


def blocks_to_prompt_text(blocks_df: pd.DataFrame, max_chars: int = 26000) -> str:
    lines = []
    total = 0
    for _, row in blocks_df.iterrows():
        line = (
            f"[source_file={row.get('source_file')} document_type={row.get('document_type')} "
            f"page={row.get('page')} block_id={row.get('block_id')}] {row.get('text')}"
        )
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def make_docs_overview(blocks_df: pd.DataFrame, max_blocks_per_doc: int = 25) -> str:
    if blocks_df.empty:
        return ""
    lines = []
    for source_file, group in blocks_df.groupby("source_file"):
        doc_type = normalize_text(group["document_type"].iloc[0]) if "document_type" in group.columns else ""
        lines.append(f"--- {source_file} ({doc_type}) ---")
        sample = group.head(max_blocks_per_doc)
        for _, row in sample.iterrows():
            lines.append(f"[page={row['page']} block_id={row['block_id']}] {row['text']}")
    return "\n".join(lines[:500])


# =========================================================
# Memory subset selection
# =========================================================


def row_matches_discipline(row: pd.Series, discipline_code: str) -> bool:
    discipline_code_upper = discipline_code.upper()
    candidates = []
    for col in ["discipline", "applies_to_sections", "discipline_list", "applies_to_sections_list"]:
        if col in row.index:
            candidates.extend(parse_list_value(row.get(col)))
    return any(str(item).upper() == discipline_code_upper for item in candidates)


def select_requirements_for_discipline(requirements_df: pd.DataFrame, discipline_code: str, max_items: int) -> pd.DataFrame:
    if requirements_df.empty:
        return requirements_df

    df = requirements_df.copy()
    mask = df.apply(lambda row: row_matches_discipline(row, discipline_code), axis=1)
    selected = df[mask].copy()

    if selected.empty:
        selected = df.copy()

    if "priority" in selected.columns:
        selected["_priority"] = pd.to_numeric(selected["priority"], errors="coerce").fillna(0)
        selected = selected.sort_values("_priority", ascending=False).drop(columns=["_priority"], errors="ignore")

    return selected.head(max_items)


def requirements_to_prompt_text(requirements_df: pd.DataFrame, max_chars: int = 18000) -> str:
    if requirements_df.empty:
        return ""

    lines = []
    total = 0
    for _, row in requirements_df.iterrows():
        memory_id = normalize_text(row.get("memory_id")) or normalize_text(row.get("requirement_id"))
        system = normalize_text(row.get("engineering_system"))
        sections = normalize_text(row.get("applies_to_sections")) or ", ".join(parse_list_value(row.get("applies_to_sections_list")))
        req = normalize_text(row.get("requirement"))
        source = normalize_text(row.get("source_file"))
        line = f"[{memory_id}] system={system}; sections={sections}; source={source}; requirement={req}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def select_facts_for_interdisciplinary(facts_df: pd.DataFrame, current_discipline: str, max_items: int) -> pd.DataFrame:
    if facts_df.empty:
        return facts_df

    df = facts_df.copy()
    current = current_discipline.upper()

    def fact_discipline(row):
        for col in ["memory_discipline", "discipline"]:
            value = normalize_text(row.get(col)).upper()
            if value:
                return value
        return ""

    df["_fact_disc"] = df.apply(fact_discipline, axis=1)
    selected = df[df["_fact_disc"] != current].copy()

    if selected.empty:
        return selected.drop(columns=["_fact_disc"], errors="ignore")

    if "confidence" in selected.columns:
        selected["_confidence"] = pd.to_numeric(selected["confidence"], errors="coerce").fillna(0)
        selected = selected.sort_values("_confidence", ascending=False)

    return selected.head(max_items).drop(columns=["_fact_disc", "_confidence"], errors="ignore")


def facts_to_prompt_text(facts_df: pd.DataFrame, max_chars: int = 18000) -> str:
    if facts_df.empty:
        return ""

    lines = []
    total = 0
    for _, row in facts_df.iterrows():
        fact_id = normalize_text(row.get("memory_id")) or normalize_text(row.get("fact_id"))
        discipline = normalize_text(row.get("memory_discipline")) or normalize_text(row.get("discipline"))
        fact_type = normalize_text(row.get("fact_type"))
        element = normalize_text(row.get("element"))
        param_name = normalize_text(row.get("parameter_name"))
        param_value = normalize_text(row.get("parameter_value"))
        unit = normalize_text(row.get("unit"))
        source_file = normalize_text(row.get("source_file"))
        page = normalize_text(row.get("page"))
        block_id = normalize_text(row.get("block_id"))
        source_text = normalize_text(row.get("source_text"))
        line = (
            f"[{fact_id}] discipline={discipline}; fact_type={fact_type}; element={element}; "
            f"parameter={param_name} {param_value} {unit}; source={source_file} p.{page} block {block_id}; text={source_text}"
        )
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


# =========================================================
# OpenAI and JSON parsing
# =========================================================


def get_openai_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Secrets nav atrasts OPENAI_API_KEY.")
    return OpenAI(api_key=api_key)


def parse_json_array(raw_text: str) -> List[Dict[str, Any]]:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("AI neatgrieza JSON masīvu.")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("AI atbilde nav JSON masīvs.")
    return data


def call_ai_json_array(client: OpenAI, model: str, prompt: str) -> List[Dict[str, Any]]:
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu esi ļoti piesardzīgs būvprojekta dokumentācijas auditors. "
                    "Atbildi tikai ar derīgu JSON masīvu. Nekādu Markdown, nekādu paskaidrojumu ārpus JSON. "
                    "Ja nav drošu, anotējamu piezīmju, atgriez []."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.choices[0].message.content or ""
    return parse_json_array(raw)


def issue_schema_instruction() -> str:
    return """
ATBILDES FORMĀTS:
Atbildi tikai JSON masīvā. Ja nav drošu anotējamu piezīmju, atgriez [].

Katram objektam jābūt:
- audit_mode: viens no ["design_brief_conflict", "single_document_consistency", "discipline_consistency", "interdisciplinary_consistency"]
- issue_type: īss tips, piemēram direct_conflict, wrong_parameter, partial_solution_visible, diameter_conflict, material_conflict, quantity_conflict, system_code_conflict, translation_conflict, cross_discipline_conflict
- priority: 1-10
- confidence: 0.0-1.0
- source_file: tieši auditējamā BP faila nosaukums no [source_file=...]
- page: auditējamā BP faila lapa
- block_id: auditējamā BP faila block_id
- source_text: īss auditējamais BP teksts, ko var apvilkt PDF
- comment: īsa piezīme latviski
- suggestion: īss ieteikums latviski
- related_memory_id: Design Brief memory ID, ja attiecas, citādi ""
- related_requirement: Design Brief prasība, ja attiecas, citādi ""
- related_fact_id: disciplīnas memory fact ID, ja attiecas, citādi ""
- related_fact: īss saistītais fakts, ja attiecas, citādi ""
- related_files: saraksts ar citiem failiem vai tukšs saraksts
- include_in_pdf: true vai false

Stingrs noteikums: include_in_pdf drīkst būt true tikai tad, ja source_file, page, block_id un source_text ir konkrēti un atrodami auditējamajā BP tekstā.
"""


# =========================================================
# Audit prompts
# =========================================================


def build_design_brief_conflict_prompt(
    project_code: str,
    discipline_code: str,
    requirements_text: str,
    blocks_text: str,
    error_examples_text: str,
) -> str:
    return f"""
Tu pārbaudi būvprojekta disciplīnu pret Design Brief / prasību atmiņu.

Projekts: {project_code}
Auditējamā disciplīna: {discipline_code}

MĒRĶIS:
Atrodi tikai tādus auditējamās BP disciplīnas teksta blokus, kuros pašā tekstā redzama acīmredzama pretruna, nepareizs parametrs vai daļējs risinājums pret Design Brief prasībām.

NEDRĪKST:
- neveido prasību statusa sarakstu;
- neziņo par prasībām, kuras vienkārši neatradi;
- neraksti "nav atrasts" bez konkrēta teksta enkura;
- neraksti vispārīgu "pārbaudīt";
- neiekļauj piezīmi, ja nav konkrēta BP teksta bloka, ko var apvilkt PDF.

DRĪKST IEKĻAUT TIKAI:
- direct_conflict: BP teksts tieši konfliktē ar Design Brief prasību;
- wrong_parameter: BP tekstā ir skaits, diametrs, jauda, tips vai marka, kas neatbilst prasībai;
- partial_solution_visible: BP teksts apraksta tikai daļu no prasītā risinājuma;
- scope_gap_with_anchor: BP tekstā ir skaidrs enkurs, pie kura redzama būtiska prasības nepilnība.

KĻŪDU PIEMĒRU KALIBRĀCIJA NO C2-2:
{error_examples_text[:7000]}

DESIGN BRIEF / MEP PRASĪBU ATMIŅA:
{requirements_text}

AUDITĒJAMĀ BP TEKSTA BLOKI:
{blocks_text}

{issue_schema_instruction()}
"""


def build_single_document_consistency_prompt(
    project_code: str,
    discipline_code: str,
    source_file: str,
    document_type: str,
    blocks_text: str,
    error_examples_text: str,
    universal_prompt: str,
) -> str:
    return f"""
Tu pārbaudi vienu būvprojekta PDF dokumentu pats pret sevi.

Projekts: {project_code}
Disciplīna: {discipline_code}
Fails: {source_file}
Dokumenta tips: {document_type}

MĒRĶIS:
Atrodi tikai tādas iekšējās nesakritības šī viena dokumenta ietvaros, kuras var piesaistīt konkrētam teksta blokam.

Meklē pēc C2-2 kļūdu piemēru loģikas:
- diametru, materiālu, daudzumu, marku, sistēmu apzīmējumu pretrunas;
- LV/ENG blakus tekstu nesakritības, bet neuzskati to par kļūdu, ja tulkojums ir dots pareizi;
- drukas kļūdas, kas maina tehnisko nozīmi;
- vienā dokumentā atšķirīgi objekta/sadaļas/rasējuma nosaukumi;
- aprēķinu vai tabulu neatbilstības, ja tās ir redzamas tekstā;
- specifikācijas pozīcijas, kuras ir iekšēji neskaidras vai pretrunīgas.

NEDRĪKST:
- neinterpretē rasējuma grafiskos atkārtotos kodus U1/K1/K2/K3 kā vienu teksta teikumu;
- neraksti piezīmi, ja nav konkrēta teksta bloka;
- neraksti vispārīgas "pārbaudīt" piezīmes;
- labāk atgriezt [] nekā apšaubāmu piezīmi.

UNIVERSĀLAIS AUDITA PROMPTS / PRINCIPI:
{universal_prompt[:5000]}

C2-2 KĻŪDU PIEMĒRI:
{error_examples_text[:9000]}

DOKUMENTA TEKSTA BLOKI:
{blocks_text}

{issue_schema_instruction()}
Visām piezīmēm audit_mode = "single_document_consistency".
"""


def build_discipline_consistency_prompt(
    project_code: str,
    discipline_code: str,
    docs_overview: str,
    current_blocks_text: str,
    error_examples_text: str,
) -> str:
    return f"""
Tu pārbaudi izvēlētās būvprojekta disciplīnas iekšējo savstarpējo konsekvenci.

Projekts: {project_code}
Disciplīna: {discipline_code}

MĒRĶIS:
Atrodi tikai skaidras nesakritības starp šīs pašas disciplīnas dokumentiem: skaidrojošo aprakstu, rasējumiem, specifikāciju, vispārīgajiem datiem, aprēķiniem un citiem PDF.

UNIVERSĀLA PĀRBAUDES SECĪBA:
- skaidrojošais apraksts ir pirmais orientieris, ja tāds ir;
- rasējumi jāsalīdzina ar skaidrojošo aprakstu;
- specifikācija jāsalīdzina ar rasējumos un aprakstā norādītajiem materiāliem, markām, daudzumiem, diametriem, sistēmām;
- vispārīgie dati un aprēķini jāizmanto kā papildu konteksts.

NEDRĪKST:
- neraksti "nav atrasts" bez konkrēta teksta enkura;
- neapvelc neko, ja nav konkrēta BP teksta bloka;
- neizdomā grafiku vai simbolu nozīmi, ja tā nav tekstā;
- neraksti zemas vērtības pārbaudes piezīmes.

C2-2 KĻŪDU PIEMĒRI:
{error_examples_text[:9000]}

DISCIPLĪNAS DOKUMENTU ĪSS KONTEKSTS:
{docs_overview[:16000]}

PAŠREIZ AUDITĒJAMIE TEKSTA BLOKI:
{current_blocks_text}

{issue_schema_instruction()}
Visām piezīmēm audit_mode = "discipline_consistency".
"""


def build_interdisciplinary_consistency_prompt(
    project_code: str,
    discipline_code: str,
    other_facts_text: str,
    current_blocks_text: str,
    error_examples_text: str,
) -> str:
    return f"""
Tu pārbaudi auditējamo būvprojekta disciplīnu pret līdz šim 03_Memory saglabātajām citu disciplīnu faktu atmiņām.

Projekts: {project_code}
Auditējamā disciplīna: {discipline_code}

MĒRĶIS:
Atrodi tikai tādas skaidras starpdisciplīnu nesakritības, kur auditējamās disciplīnas teksta bloks konfliktē ar iepriekš auditētās disciplīnas faktu.

PIEMĒRI:
- auditējamā sadaļa saka K2 D110, bet iepriekšējā memory faktā K2 ir OD160;
- auditējamā sadaļa saka materiāls PVC, bet iepriekšējā memory faktā tas pats elements ir PP;
- auditējamā sadaļa norāda citu iekārtas skaitu, marku, pieslēgumu, telpu vai sistēmas robežu;
- auditējamā sadaļa apraksta daļēju risinājumu, kas konfliktē ar iepriekšējās disciplīnas faktu.

NEDRĪKST:
- neizmanto pašreizējās disciplīnas faktus kā citu disciplīnu memory;
- neraksti piezīmi bez konkrēta auditējamās sadaļas teksta bloka;
- neraksti "nav atrasts";
- neraksti vispārīgu starpdisciplīnu brīdinājumu.

C2-2 KĻŪDU PIEMĒRI:
{error_examples_text[:6000]}

CITU DISCIPLĪNU FAKTU ATMIŅA:
{other_facts_text}

AUDITĒJAMĀS DISCIPLĪNAS TEKSTA BLOKI:
{current_blocks_text}

{issue_schema_instruction()}
Visām piezīmēm audit_mode = "interdisciplinary_consistency".
"""


# =========================================================
# Issue post-processing
# =========================================================


def build_valid_block_keys(blocks_df: pd.DataFrame) -> set:
    keys = set()
    if blocks_df.empty:
        return keys
    for _, row in blocks_df.iterrows():
        keys.add((normalize_text(row.get("source_file")), safe_int(row.get("page")), safe_int(row.get("block_id"))))
    return keys


def normalize_issue(issue: Dict[str, Any], project_code: str, discipline_code: str, source_tag: str) -> Dict[str, Any]:
    row = dict(issue)
    row["project_code"] = project_code
    row["discipline"] = discipline_code
    row["source_tag"] = source_tag

    row["source_file"] = normalize_text(row.get("source_file"))
    row["page"] = safe_int(row.get("page"), default=0)
    row["block_id"] = safe_int(row.get("block_id"), default=-1)
    row["source_text"] = normalize_text(row.get("source_text"))
    row["comment"] = normalize_text(row.get("comment"))
    row["suggestion"] = normalize_text(row.get("suggestion"))
    row["audit_mode"] = normalize_text(row.get("audit_mode"))
    row["issue_type"] = normalize_text(row.get("issue_type"))
    row["priority"] = safe_int(row.get("priority"), default=0)
    row["confidence"] = safe_float(row.get("confidence"), default=0.0)
    row["include_in_pdf"] = bool(row.get("include_in_pdf", False))
    row["related_files"] = parse_list_value(row.get("related_files"))
    row["related_memory_id"] = normalize_text(row.get("related_memory_id"))
    row["related_requirement"] = normalize_text(row.get("related_requirement"))
    row["related_fact_id"] = normalize_text(row.get("related_fact_id"))
    row["related_fact"] = normalize_text(row.get("related_fact"))

    return row


def filter_issues(
    issues: List[Dict[str, Any]],
    blocks_df: pd.DataFrame,
    min_confidence: float,
    project_code: str,
    discipline_code: str,
    source_tag: str,
) -> pd.DataFrame:
    valid_keys = build_valid_block_keys(blocks_df)
    normalized = [normalize_issue(item, project_code, discipline_code, source_tag) for item in issues if isinstance(item, dict)]

    filtered = []
    banned_issue_types = {
        "not_found",
        "missing_without_anchor",
        "general_uncertainty",
        "please_check",
        "unclear_without_anchor",
    }

    for row in normalized:
        key = (row["source_file"], row["page"], row["block_id"])
        if key not in valid_keys:
            continue
        if row["confidence"] < min_confidence:
            continue
        if row["issue_type"] in banned_issue_types:
            continue
        if not row["source_text"] or not row["comment"]:
            continue
        if not row["include_in_pdf"]:
            continue
        filtered.append(row)

    if not filtered:
        return pd.DataFrame()

    df = pd.DataFrame(filtered)
    df = df.drop_duplicates(subset=["audit_mode", "issue_type", "source_file", "page", "block_id", "comment"])
    df = df.reset_index(drop=True)
    df["issue_id"] = [f"{project_code}-{discipline_code}-ISSUE-{i+1:04d}" for i in range(len(df))]
    df["created_at_utc"] = datetime.now(timezone.utc).isoformat()

    preferred = [
        "issue_id",
        "audit_mode",
        "issue_type",
        "priority",
        "confidence",
        "source_file",
        "page",
        "block_id",
        "source_text",
        "comment",
        "suggestion",
        "related_memory_id",
        "related_requirement",
        "related_fact_id",
        "related_fact",
        "related_files",
        "project_code",
        "discipline",
        "source_tag",
        "include_in_pdf",
        "created_at_utc",
    ]
    existing = [col for col in preferred if col in df.columns]
    other = [col for col in df.columns if col not in existing]
    return df[existing + other]


def make_excel_bytes(issues_df: pd.DataFrame, blocks_df: pd.DataFrame, file_summary_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        clean_dataframe_for_excel(issues_df).to_excel(writer, sheet_name="issues", index=False)
        clean_dataframe_for_excel(file_summary_df).to_excel(writer, sheet_name="file_summary", index=False)
        if not issues_df.empty and "audit_mode" in issues_df.columns:
            issues_df.groupby("audit_mode").size().reset_index(name="count").to_excel(writer, sheet_name="summary_audit_mode", index=False)
        if not issues_df.empty and "issue_type" in issues_df.columns:
            issues_df.groupby("issue_type").size().reset_index(name="count").to_excel(writer, sheet_name="summary_issue_type", index=False)
        clean_dataframe_for_excel(blocks_df.head(5000)).to_excel(writer, sheet_name="text_blocks_sample", index=False)
    output.seek(0)
    return output.getvalue()


def make_json_bytes(issues_df: pd.DataFrame) -> bytes:
    records = []
    for _, row in issues_df.iterrows():
        item = {}
        for col, value in row.items():
            if isinstance(value, list):
                item[col] = value
            elif pd.isna(value):
                item[col] = None
            else:
                item[col] = value
        records.append(item)

    payload = {
        "schema": "bp_audit_universal_issues_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "issues": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# =========================================================
# Audit runners
# =========================================================


def run_design_brief_audit(
    client: OpenAI,
    model: str,
    project_code: str,
    discipline_code: str,
    blocks_df: pd.DataFrame,
    requirements_df: pd.DataFrame,
    error_examples_text: str,
    max_requirements: int,
    max_blocks_per_batch: int,
    min_confidence: float,
    delay_seconds: float,
) -> pd.DataFrame:
    selected_requirements = select_requirements_for_discipline(requirements_df, discipline_code, max_requirements)
    requirements_text = requirements_to_prompt_text(selected_requirements)
    if not requirements_text:
        return pd.DataFrame()

    batches = make_block_batches(blocks_df, max_blocks_per_batch)
    all_issues: List[Dict[str, Any]] = []

    progress = st.progress(0)
    status = st.empty()

    for idx, batch_df in enumerate(batches, start=1):
        status.write(f"Design Brief audits: batch {idx}/{len(batches)}")
        prompt = build_design_brief_conflict_prompt(
            project_code=project_code,
            discipline_code=discipline_code,
            requirements_text=requirements_text,
            blocks_text=blocks_to_prompt_text(batch_df),
            error_examples_text=error_examples_text,
        )
        try:
            issues = call_ai_json_array(client, model, prompt)
            all_issues.extend(issues)
        except Exception as e:
            st.warning(f"Design Brief batch {idx} kļūda: {e}")
        progress.progress(idx / len(batches))
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return filter_issues(all_issues, blocks_df, min_confidence, project_code, discipline_code, "design_brief")


def run_single_document_audit(
    client: OpenAI,
    model: str,
    project_code: str,
    discipline_code: str,
    blocks_df: pd.DataFrame,
    error_examples_text: str,
    universal_prompt: str,
    max_blocks_per_request: int,
    min_confidence: float,
    delay_seconds: float,
) -> pd.DataFrame:
    all_issues: List[Dict[str, Any]] = []
    groups = list(blocks_df.groupby("source_file"))

    progress = st.progress(0)
    status = st.empty()
    step = 0
    total_steps = sum(max(1, (len(group) + max_blocks_per_request - 1) // max_blocks_per_request) for _, group in groups)

    for source_file, group_df in groups:
        document_type = normalize_text(group_df["document_type"].iloc[0]) if "document_type" in group_df.columns else "other_pdf"
        batches = make_block_batches(group_df, max_blocks_per_request)
        for batch_df in batches:
            step += 1
            status.write(f"Viena dokumenta audits: {source_file} ({step}/{total_steps})")
            prompt = build_single_document_consistency_prompt(
                project_code=project_code,
                discipline_code=discipline_code,
                source_file=source_file,
                document_type=document_type,
                blocks_text=blocks_to_prompt_text(batch_df),
                error_examples_text=error_examples_text,
                universal_prompt=universal_prompt,
            )
            try:
                issues = call_ai_json_array(client, model, prompt)
                all_issues.extend(issues)
            except Exception as e:
                st.warning(f"Viena dokumenta audits kļūda failam {source_file}: {e}")
            progress.progress(step / total_steps)
            if delay_seconds > 0:
                time.sleep(delay_seconds)

    return filter_issues(all_issues, blocks_df, min_confidence, project_code, discipline_code, "single_document")


def run_discipline_consistency_audit(
    client: OpenAI,
    model: str,
    project_code: str,
    discipline_code: str,
    blocks_df: pd.DataFrame,
    error_examples_text: str,
    max_blocks_per_batch: int,
    min_confidence: float,
    delay_seconds: float,
) -> pd.DataFrame:
    docs_overview = make_docs_overview(blocks_df, max_blocks_per_doc=30)
    batches = make_block_batches(blocks_df, max_blocks_per_batch)
    all_issues: List[Dict[str, Any]] = []

    progress = st.progress(0)
    status = st.empty()

    for idx, batch_df in enumerate(batches, start=1):
        status.write(f"Disciplīnas savstarpējais audits: batch {idx}/{len(batches)}")
        prompt = build_discipline_consistency_prompt(
            project_code=project_code,
            discipline_code=discipline_code,
            docs_overview=docs_overview,
            current_blocks_text=blocks_to_prompt_text(batch_df),
            error_examples_text=error_examples_text,
        )
        try:
            issues = call_ai_json_array(client, model, prompt)
            all_issues.extend(issues)
        except Exception as e:
            st.warning(f"Disciplīnas savstarpējais audits batch {idx} kļūda: {e}")
        progress.progress(idx / len(batches))
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return filter_issues(all_issues, blocks_df, min_confidence, project_code, discipline_code, "discipline_consistency")


def run_interdisciplinary_audit(
    client: OpenAI,
    model: str,
    project_code: str,
    discipline_code: str,
    blocks_df: pd.DataFrame,
    facts_df: pd.DataFrame,
    error_examples_text: str,
    max_facts: int,
    max_blocks_per_batch: int,
    min_confidence: float,
    delay_seconds: float,
) -> pd.DataFrame:
    selected_facts = select_facts_for_interdisciplinary(facts_df, discipline_code, max_facts)
    facts_text = facts_to_prompt_text(selected_facts)
    if not facts_text:
        return pd.DataFrame()

    batches = make_block_batches(blocks_df, max_blocks_per_batch)
    all_issues: List[Dict[str, Any]] = []

    progress = st.progress(0)
    status = st.empty()

    for idx, batch_df in enumerate(batches, start=1):
        status.write(f"Starpdisciplīnu audits: batch {idx}/{len(batches)}")
        prompt = build_interdisciplinary_consistency_prompt(
            project_code=project_code,
            discipline_code=discipline_code,
            other_facts_text=facts_text,
            current_blocks_text=blocks_to_prompt_text(batch_df),
            error_examples_text=error_examples_text,
        )
        try:
            issues = call_ai_json_array(client, model, prompt)
            all_issues.extend(issues)
        except Exception as e:
            st.warning(f"Starpdisciplīnu audits batch {idx} kļūda: {e}")
        progress.progress(idx / len(batches))
        if delay_seconds > 0:
            time.sleep(delay_seconds)

    return filter_issues(all_issues, blocks_df, min_confidence, project_code, discipline_code, "interdisciplinary")


# =========================================================
# Streamlit UI
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

col_a, col_b = st.columns(2)
with col_a:
    max_pages_per_pdf = st.number_input("Maksimālais lapu skaits no viena PDF", min_value=1, max_value=300, value=100, step=5)
    max_blocks_per_ai_request = st.number_input("Teksta bloku skaits vienā AI pieprasījumā", min_value=30, max_value=800, value=220, step=10)
    max_design_brief_requirements = st.number_input("Maksimālais Design Brief prasību skaits promptā", min_value=20, max_value=500, value=160, step=20)
with col_b:
    max_interdisciplinary_facts = st.number_input("Maksimālais citu disciplīnu faktu skaits promptā", min_value=20, max_value=600, value=180, step=20)
    min_confidence = st.slider("Minimālā pārliecība anotējamām piezīmēm", min_value=0.50, max_value=0.95, value=0.75, step=0.05)
    delay_seconds = st.number_input("Pauze starp AI pieprasījumiem sekundēs", min_value=0.0, max_value=5.0, value=0.5, step=0.5)

st.markdown("### Audita moduļi")
run_module_a = st.checkbox("A. Pret Design Brief prasību atmiņu", value=True)
run_module_b = st.checkbox("B. Katra dokumenta iekšējā konsekvence", value=True)
run_module_c = st.checkbox("C. Disciplīnas savstarpējā konsekvence", value=True)
run_module_d = st.checkbox("D. Starpdisciplīnu konsekvence pret līdzšinējo 03_Memory", value=True)

st.info(
    "Šis ir universālais tabulas tests. Tas vēl neģenerē anotētus PDF. "
    "Rezultātā jāpaliek tikai tām piezīmēm, kurām ir konkrēts source_file + page + block_id."
)

if "disciplines_df" not in st.session_state:
    st.session_state.disciplines_df = pd.DataFrame()
if "memory_catalog_df" not in st.session_state:
    st.session_state.memory_catalog_df = pd.DataFrame()
if "requirements_df" not in st.session_state:
    st.session_state.requirements_df = pd.DataFrame()
if "facts_df" not in st.session_state:
    st.session_state.facts_df = pd.DataFrame()
if "prompt_materials" not in st.session_state:
    st.session_state.prompt_materials = {}
if "discipline_pdf_df" not in st.session_state:
    st.session_state.discipline_pdf_df = pd.DataFrame()
if "blocks_df" not in st.session_state:
    st.session_state.blocks_df = pd.DataFrame()
if "file_summary_df" not in st.session_state:
    st.session_state.file_summary_df = pd.DataFrame()
if "issues_df" not in st.session_state:
    st.session_state.issues_df = pd.DataFrame()


if st.button("1) Nolasīt 01_Input, 03_Memory un 04_Prompt"):
    try:
        if not input_folder_id:
            st.error("Nav GOOGLE_DRIVE_INPUT_FOLDER_ID.")
            st.stop()
        if not memory_folder_id:
            st.error("Nav GOOGLE_DRIVE_MEMORY_FOLDER_ID.")
            st.stop()

        service = get_drive_service()
        disciplines_df = get_discipline_folders(service, input_folder_id)
        memory_catalog_df, requirements_df, facts_df = load_project_memory(service, memory_folder_id)
        prompt_materials = read_prompt_materials(service, prompt_folder_id)

        st.session_state.disciplines_df = disciplines_df
        st.session_state.memory_catalog_df = memory_catalog_df
        st.session_state.requirements_df = requirements_df
        st.session_state.facts_df = facts_df
        st.session_state.prompt_materials = prompt_materials

        st.success(
            f"Nolasīts: disciplīnas {len(disciplines_df)}, "
            f"Design Brief prasības {len(requirements_df)}, "
            f"disciplīnu fakti {len(facts_df)}, "
            f"kļūdu piemēri {len(prompt_materials.get('error_examples_df', pd.DataFrame()))}."
        )
    except Exception as e:
        st.error("Neizdevās nolasīt Drive / Memory / Prompt datus.")
        st.exception(e)


disciplines_df = st.session_state.disciplines_df
memory_catalog_df = st.session_state.memory_catalog_df
requirements_df = st.session_state.requirements_df
facts_df = st.session_state.facts_df
prompt_materials = st.session_state.prompt_materials

if not memory_catalog_df.empty:
    with st.expander("03_Memory katalogs"):
        st.dataframe(memory_catalog_df, use_container_width=True)

if not prompt_materials:
    prompt_materials = {}

if not prompt_materials.get("prompt_catalog", pd.DataFrame()).empty:
    with st.expander("04_Prompt katalogs"):
        st.dataframe(prompt_materials.get("prompt_catalog"), use_container_width=True)

if not disciplines_df.empty:
    st.markdown("## 2. Izvēlies auditējamo disciplīnu")
    st.dataframe(disciplines_df, use_container_width=True)

    folder_options = disciplines_df["folder_name"].tolist()
    default_idx = 0
    for i, name in enumerate(folder_options):
        if str(name).startswith("09_UKT"):
            default_idx = i
            break

    selected_folder_name = st.selectbox("Disciplīnas mape", options=folder_options, index=default_idx)
    selected_row = disciplines_df[disciplines_df["folder_name"] == selected_folder_name].iloc[0]
    selected_discipline_code = selected_row["discipline_code"]
    selected_folder_id = selected_row["folder_id"]

    st.write("Izvēlētā disciplīna:", selected_discipline_code)

    if st.button("2) Atrast disciplīnas PDF failus"):
        try:
            service = get_drive_service()
            pdf_df = get_pdf_documents_in_discipline(service, selected_folder_id, selected_folder_name)
            st.session_state.discipline_pdf_df = pdf_df
            if pdf_df.empty:
                st.warning("Izvēlētajā disciplīnā nav atrasti PDF faili.")
            else:
                st.success(f"Atrasti {len(pdf_df)} PDF faili.")
        except Exception as e:
            st.error("Neizdevās atrast disciplīnas PDF failus.")
            st.exception(e)


discipline_pdf_df = st.session_state.discipline_pdf_df

if not discipline_pdf_df.empty:
    st.markdown("## 3. Disciplīnas PDF faili")
    st.dataframe(
        discipline_pdf_df[["name", "path", "document_type", "size", "modifiedTime"]],
        use_container_width=True,
    )

    pdf_options = discipline_pdf_df["path"].tolist()

    # Noklusējumā izvēlamies skaidrojošo aprakstu, specifikāciju, general_data un līdz 2 rasējumiem.
    default_paths = []
    for doc_type in ["explanatory_note", "specification", "general_data"]:
        matches = discipline_pdf_df[discipline_pdf_df["document_type"] == doc_type]["path"].tolist()
        default_paths.extend(matches[:1])
    drawing_matches = discipline_pdf_df[discipline_pdf_df["document_type"] == "drawing"]["path"].tolist()
    default_paths.extend(drawing_matches[:2])
    default_paths = [path for path in default_paths if path in pdf_options]

    selected_pdf_paths = st.multiselect(
        "Izvēlies PDF failus auditam",
        options=pdf_options,
        default=default_paths,
    )

    selected_docs_df = discipline_pdf_df[discipline_pdf_df["path"].isin(selected_pdf_paths)].copy()

    st.markdown("### Auditam izvēlētie faili")
    st.dataframe(selected_docs_df[["name", "path", "document_type", "size"]], use_container_width=True)

    if st.button("3) Izvilkt PDF teksta blokus"):
        try:
            if selected_docs_df.empty:
                st.warning("Nav izvēlēts neviens PDF fails.")
                st.stop()
            service = get_drive_service()
            with st.spinner("Izvelku PDF teksta blokus..."):
                blocks_df, file_summary_df = extract_selected_pdf_blocks(
                    service=service,
                    selected_docs_df=selected_docs_df,
                    max_pages=int(max_pages_per_pdf),
                )
            st.session_state.blocks_df = blocks_df
            st.session_state.file_summary_df = file_summary_df
            st.success(f"Izvilkti {len(blocks_df)} teksta bloki no {len(file_summary_df)} failiem.")
        except Exception as e:
            st.error("Neizdevās izvilkt PDF teksta blokus.")
            st.exception(e)


blocks_df = st.session_state.blocks_df
file_summary_df = st.session_state.file_summary_df

if not blocks_df.empty:
    st.markdown("## 4. PDF teksta bloku indekss")
    st.markdown("### Failu kopsavilkums")
    st.dataframe(file_summary_df, use_container_width=True)

    st.markdown("### Teksta bloku priekšskatījums")
    st.dataframe(
        blocks_df[["source_file", "document_type", "page", "block_id", "text", "x0", "y0", "x1", "y1"]].head(100),
        use_container_width=True,
    )

    if st.button("4) Palaist universālo auditu"):
        try:
            if disciplines_df.empty:
                st.error("Vispirms nolasiet disciplīnas.")
                st.stop()

            selected_folder_name = st.session_state.get("_selected_folder_name_for_audit", None)
            # Streamlit widgets netiek tieši saglabāti ar šo nosaukumu, tāpēc paņemam no pēdējā redzamā selectbox vērtības, ja pieejams caur locals.
            # Praktiski izmantojam selected_discipline_code, ja tas eksistē šī rerun scope.
            try:
                discipline_code_for_audit = selected_discipline_code
            except Exception:
                discipline_code_for_audit = normalize_text(blocks_df.get("discipline", "")) or "UNKNOWN"

            client = get_openai_client()

            all_issue_dfs = []
            error_examples_text = prompt_materials.get("error_examples_text", "") if isinstance(prompt_materials, dict) else ""
            universal_prompt = prompt_materials.get("universal_prompt", "") if isinstance(prompt_materials, dict) else ""

            st.markdown("## 5. Audita izpilde")

            if run_module_a:
                st.markdown("### A. Design Brief conflict")
                df_a = run_design_brief_audit(
                    client=client,
                    model=model,
                    project_code=project_code,
                    discipline_code=discipline_code_for_audit,
                    blocks_df=blocks_df,
                    requirements_df=requirements_df,
                    error_examples_text=error_examples_text,
                    max_requirements=int(max_design_brief_requirements),
                    max_blocks_per_batch=int(max_blocks_per_ai_request),
                    min_confidence=float(min_confidence),
                    delay_seconds=float(delay_seconds),
                )
                st.write(f"A modulis: {len(df_a)} piezīmes.")
                if not df_a.empty:
                    all_issue_dfs.append(df_a)

            if run_module_b:
                st.markdown("### B. Single document consistency")
                df_b = run_single_document_audit(
                    client=client,
                    model=model,
                    project_code=project_code,
                    discipline_code=discipline_code_for_audit,
                    blocks_df=blocks_df,
                    error_examples_text=error_examples_text,
                    universal_prompt=universal_prompt,
                    max_blocks_per_request=int(max_blocks_per_ai_request),
                    min_confidence=float(min_confidence),
                    delay_seconds=float(delay_seconds),
                )
                st.write(f"B modulis: {len(df_b)} piezīmes.")
                if not df_b.empty:
                    all_issue_dfs.append(df_b)

            if run_module_c:
                st.markdown("### C. Discipline consistency")
                df_c = run_discipline_consistency_audit(
                    client=client,
                    model=model,
                    project_code=project_code,
                    discipline_code=discipline_code_for_audit,
                    blocks_df=blocks_df,
                    error_examples_text=error_examples_text,
                    max_blocks_per_batch=int(max_blocks_per_ai_request),
                    min_confidence=float(min_confidence),
                    delay_seconds=float(delay_seconds),
                )
                st.write(f"C modulis: {len(df_c)} piezīmes.")
                if not df_c.empty:
                    all_issue_dfs.append(df_c)

            if run_module_d:
                st.markdown("### D. Interdisciplinary consistency")
                df_d = run_interdisciplinary_audit(
                    client=client,
                    model=model,
                    project_code=project_code,
                    discipline_code=discipline_code_for_audit,
                    blocks_df=blocks_df,
                    facts_df=facts_df,
                    error_examples_text=error_examples_text,
                    max_facts=int(max_interdisciplinary_facts),
                    max_blocks_per_batch=int(max_blocks_per_ai_request),
                    min_confidence=float(min_confidence),
                    delay_seconds=float(delay_seconds),
                )
                st.write(f"D modulis: {len(df_d)} piezīmes.")
                if not df_d.empty:
                    all_issue_dfs.append(df_d)

            if all_issue_dfs:
                issues_df = pd.concat(all_issue_dfs, ignore_index=True)
                issues_df = issues_df.drop_duplicates(subset=["audit_mode", "issue_type", "source_file", "page", "block_id", "comment"])
                issues_df = issues_df.reset_index(drop=True)
                issues_df["issue_id"] = [f"{project_code}-{discipline_code_for_audit}-ISSUE-{i+1:04d}" for i in range(len(issues_df))]
                st.session_state.issues_df = issues_df
                st.success(f"Kopā atlasītas {len(issues_df)} anotējamas kandidātpiezīmes.")
            else:
                st.session_state.issues_df = pd.DataFrame()
                st.info("Netika atrastas anotējamas kandidātpiezīmes pēc izvēlētajiem filtriem.")

        except Exception as e:
            st.error("Kļūda universālā audita izpildē.")
            st.exception(e)


issues_df = st.session_state.issues_df

if not issues_df.empty:
    st.markdown("## 6. Issues tabula")

    st.markdown("### Kopsavilkums pēc audita moduļa")
    st.dataframe(
        issues_df.groupby("audit_mode").size().reset_index(name="count").sort_values("count", ascending=False),
        use_container_width=True,
    )

    st.markdown("### Kopsavilkums pēc issue_type")
    st.dataframe(
        issues_df.groupby("issue_type").size().reset_index(name="count").sort_values("count", ascending=False),
        use_container_width=True,
    )

    st.markdown("### Piezīmju tabula")
    preferred_cols = [
        "issue_id",
        "audit_mode",
        "issue_type",
        "priority",
        "confidence",
        "source_file",
        "page",
        "block_id",
        "source_text",
        "comment",
        "suggestion",
        "related_memory_id",
        "related_requirement",
        "related_fact_id",
        "related_fact",
        "related_files",
        "include_in_pdf",
    ]
    existing = [col for col in preferred_cols if col in issues_df.columns]
    other = [col for col in issues_df.columns if col not in existing]
    st.dataframe(issues_df[existing + other], use_container_width=True)

    excel_bytes = make_excel_bytes(issues_df, blocks_df, file_summary_df)
    json_bytes = make_json_bytes(issues_df)

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "Lejupielādēt issues Excel",
            data=excel_bytes,
            file_name=f"{project_code.lower().replace('-', '_')}_universal_audit_issues.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with col2:
        st.download_button(
            "Lejupielādēt issues JSON",
            data=json_bytes,
            file_name=f"{project_code.lower().replace('-', '_')}_universal_audit_issues.json",
            mime="application/json",
        )

    st.markdown("## 7. Nākamais solis")
    st.info(
        "Ja šī issues tabula ir kvalitatīva, nākamajā versijā pievienosim anotēto PDF ģenerēšanu: "
        "rīks izmantos source_file + page + block_id + koordinātas no teksta bloku indeksa un apvilks attiecīgo tekstu ar sarkanu rāmi."
    )
