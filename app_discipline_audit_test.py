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


st.set_page_config(page_title="BP disciplīnas audita tests", layout="wide")

st.title("BP disciplīnas audita tests pret Design Brief un disciplīnas atmiņu")

st.write(
    "Šī aplikācija pārbauda vienu izvēlētu BP disciplīnu. Tā nolasa Design Brief prasību atmiņu, "
    "disciplīnas faktu atmiņu un izvēlētās disciplīnas PDF teksta blokus. Rezultātā tiek rādītas "
    "tikai tādas kandidātpiezīmes, kuras var piesaistīt konkrētam PDF teksta blokam. "
    "PDF anotēšana šajā versijā vēl netiek veikta."
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


def get_discipline_code_from_folder_name(folder_name: str) -> str:
    name = str(folder_name).strip()
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return name.strip()


def get_discipline_folders(service, input_folder_id: str) -> pd.DataFrame:
    items = list_folder_items(service, input_folder_id)
    rows = []

    for item in items:
        if item.get("mimeType") != FOLDER_MIME_TYPE:
            continue

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
        return pd.DataFrame()

    return pd.DataFrame(rows).sort_values("folder_name")


def classify_document_type(file_name: str, path: str = "") -> str:
    text = f"{file_name} {path}".lower()

    if any(k in text for k in ["explanatory", "description", "skaidrojo", "apraksts", "_sa", "sa_"]):
        return "explanatory_note"

    if any(k in text for k in ["specification", "specifik", "apjomi", "boq", "bill of quantities", "_ms", "ms_"]):
        return "specification"

    if any(k in text for k in ["general data", "vispār", "vispar", "drawing list", "rasējumu saraksts"]):
        return "general_data"

    if any(k in text for k in ["calculation", "aprēķ", "aprek", "calcs"]):
        return "calculation"

    if any(k in text for k in ["plan", "profile", "section", "layout", "scheme", "drawing", "rasēj", "rasej", "plāns", "plans", "griezums", "shēma", "shema", "_ra", "ra_"]):
        return "drawing"

    return "other_pdf"


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
        lambda row: classify_document_type(row.get("name", ""), row.get("path", "")),
        axis=1,
    )

    return pdf_df


# =========================================================
# Memory JSON
# =========================================================

def load_json_from_drive(service, file_id: str) -> Any:
    raw = download_drive_file_bytes(service, file_id)
    text = raw.decode("utf-8", errors="replace")
    return json.loads(text)


def detect_memory_kind(file_name: str, payload: Any) -> str:
    name = str(file_name).lower()

    if "requirements" in name or "mep_requirements" in name:
        return "design_brief_requirements"

    if "facts" in name:
        return "discipline_facts"

    if isinstance(payload, dict):
        schema = str(payload.get("memory_schema", "")).lower()
        if "requirement" in schema:
            return "design_brief_requirements"
        if "fact" in schema or "discipline" in schema:
            return "discipline_facts"

    return "unknown"


def payload_records(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ["requirements", "facts", "records", "items"]:
            if isinstance(payload.get(key), list):
                return payload.get(key)

    if isinstance(payload, list):
        return payload

    return []


def load_project_memory(service, memory_folder_id: str) -> Dict[str, pd.DataFrame]:
    items = list_folder_items(service, memory_folder_id)
    json_items = [item for item in items if str(item.get("name", "")).lower().endswith(".json")]

    requirement_rows = []
    fact_rows = []
    catalog_rows = []

    for item in json_items:
        file_name = item.get("name", "")
        payload = load_json_from_drive(service, item.get("id"))
        kind = detect_memory_kind(file_name, payload)
        records = payload_records(payload)

        catalog_rows.append(
            {
                "name": file_name,
                "kind": kind,
                "records_count": len(records),
                "size": item.get("size", ""),
                "modifiedTime": item.get("modifiedTime", ""),
                "id": item.get("id"),
            }
        )

        for record in records:
            record = dict(record)
            record["memory_source_file"] = file_name
            if kind == "design_brief_requirements":
                requirement_rows.append(record)
            elif kind == "discipline_facts":
                fact_rows.append(record)

    requirements_df = pd.DataFrame(requirement_rows)
    facts_df = pd.DataFrame(fact_rows)
    catalog_df = pd.DataFrame(catalog_rows)

    return {
        "requirements": requirements_df,
        "facts": facts_df,
        "catalog": catalog_df,
    }


def parse_list_value(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]

    if pd.isna(value):
        return []

    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            import ast
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass

    return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]


def filter_requirements_for_discipline(requirements_df: pd.DataFrame, discipline_code: str, max_rows: int) -> pd.DataFrame:
    if requirements_df.empty:
        return requirements_df

    disc = discipline_code.upper().strip()
    result = requirements_df.copy()

    def applies(row) -> bool:
        values = []
        for col in ["discipline", "discipline_list", "applies_to_sections", "applies_to_sections_list"]:
            if col in row.index:
                values.extend(parse_list_value(row.get(col)))
        values_upper = [v.upper().strip() for v in values]
        return disc in values_upper

    filtered = result[result.apply(applies, axis=1)].copy()

    if filtered.empty:
        filtered = result.copy()

    if "priority" in filtered.columns:
        filtered["priority"] = pd.to_numeric(filtered["priority"], errors="coerce").fillna(0)
        filtered = filtered.sort_values("priority", ascending=False)

    return filtered.head(max_rows)


def filter_facts_for_discipline(facts_df: pd.DataFrame, discipline_code: str, max_rows: int) -> pd.DataFrame:
    if facts_df.empty:
        return facts_df

    disc = discipline_code.upper().strip()
    result = facts_df.copy()

    possible_cols = ["discipline", "memory_discipline", "discipline_code"]
    mask = pd.Series([False] * len(result))

    for col in possible_cols:
        if col in result.columns:
            mask = mask | (result[col].astype(str).str.upper().str.strip() == disc)

    filtered = result[mask].copy()

    if filtered.empty:
        filtered = result.copy()

    return filtered.head(max_rows)


def requirements_to_prompt_lines(df: pd.DataFrame) -> str:
    lines = []

    for _, row in df.iterrows():
        memory_id = str(row.get("memory_id") or row.get("requirement_id") or "").strip()
        system = str(row.get("engineering_system") or "").strip()
        discipline = str(row.get("discipline") or "").strip()
        requirement = str(row.get("requirement") or "").strip()
        source_file = str(row.get("source_file") or "").strip()

        if not requirement:
            continue

        lines.append(
            f"[memory_id={memory_id}] [system={system}] [discipline={discipline}] "
            f"[source={source_file}] {requirement}"
        )

    return "\n".join(lines)


def facts_to_prompt_lines(df: pd.DataFrame) -> str:
    lines = []

    for _, row in df.iterrows():
        memory_id = str(row.get("memory_id") or row.get("fact_id") or "").strip()
        fact_type = str(row.get("fact_type") or "").strip()
        system_code = str(row.get("system_code") or row.get("system") or "").strip()
        element = str(row.get("element") or "").strip()
        parameter_name = str(row.get("parameter_name") or "").strip()
        parameter_value = str(row.get("parameter_value") or "").strip()
        unit = str(row.get("unit") or "").strip()
        source_file = str(row.get("source_file") or "").strip()
        source_text = str(row.get("source_text") or "").strip()

        line = (
            f"[memory_id={memory_id}] [fact_type={fact_type}] [system={system_code}] "
            f"[element={element}] [parameter={parameter_name}:{parameter_value} {unit}] "
            f"[source={source_file}] {source_text}"
        )
        lines.append(line)

    return "\n".join(lines)


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


def build_block_index(blocks_df: pd.DataFrame) -> Dict[Tuple[str, int, int], Dict[str, Any]]:
    index: Dict[Tuple[str, int, int], Dict[str, Any]] = {}

    if blocks_df.empty:
        return index

    for _, row in blocks_df.iterrows():
        key = (
            str(row.get("source_file", "")),
            int(row.get("page", 0)),
            int(row.get("block_id", -1)),
        )
        index[key] = row.to_dict()

    return index


def make_batches_from_blocks(blocks_df: pd.DataFrame, blocks_per_batch: int) -> List[pd.DataFrame]:
    if blocks_df.empty:
        return []

    sorted_df = blocks_df.sort_values(["source_file", "page", "block_id"]).reset_index(drop=True)
    batches = []

    for start in range(0, len(sorted_df), blocks_per_batch):
        batches.append(sorted_df.iloc[start:start + blocks_per_batch].copy())

    return batches


def blocks_to_prompt_text(batch_df: pd.DataFrame) -> str:
    lines = []

    for _, row in batch_df.iterrows():
        source_file = str(row.get("source_file", ""))
        document_type = str(row.get("document_type", ""))
        page = int(row.get("page", 0))
        block_id = int(row.get("block_id", -1))
        text = str(row.get("text", "")).strip()

        lines.append(
            f"[source_file={source_file}] [document_type={document_type}] "
            f"[page={page}] [block_id={block_id}] {text}"
        )

    return "\n".join(lines)


def download_and_extract_selected_blocks(
    service,
    selected_docs_df: pd.DataFrame,
    max_pages_per_pdf: int,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    all_blocks = []
    pdf_summary = []

    for _, doc_row in selected_docs_df.iterrows():
        file_name = doc_row["name"]
        file_id = doc_row["id"]
        path = doc_row["path"]
        document_type = doc_row.get("document_type", "other_pdf")

        pdf_bytes = download_drive_file_bytes(service, file_id)
        blocks_df, total_pages = extract_pdf_page_blocks(
            pdf_bytes=pdf_bytes,
            max_pages=int(max_pages_per_pdf),
        )

        if not blocks_df.empty:
            blocks_df["source_file"] = file_name
            blocks_df["drive_file_id"] = file_id
            blocks_df["drive_path"] = path
            blocks_df["document_type"] = document_type
            all_blocks.append(blocks_df)

        pdf_summary.append(
            {
                "source_file": file_name,
                "document_type": document_type,
                "total_pages": total_pages,
                "processed_pages": min(total_pages, int(max_pages_per_pdf)),
                "blocks_count": len(blocks_df),
                "drive_path": path,
            }
        )

    if all_blocks:
        combined_blocks = pd.concat(all_blocks, ignore_index=True)
    else:
        combined_blocks = pd.DataFrame()

    return combined_blocks, pd.DataFrame(pdf_summary)


# =========================================================
# OpenAI
# =========================================================

def get_openai_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Secrets nav atrasts OPENAI_API_KEY.")
    return OpenAI(api_key=api_key)


def parse_json_array(raw_text: str) -> List[Dict[str, Any]]:
    text = str(raw_text or "").strip()

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


def call_openai_json_array(client: OpenAI, model: str, prompt: str) -> List[Dict[str, Any]]:
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[
            {
                "role": "system",
                "content": (
                    "Tu atbildi tikai derīgā JSON masīvā. Nekādu Markdown, nekādu paskaidrojumu. "
                    "Atgriez tikai skaidras, konkrētam BP teksta blokam piesaistāmas audita piezīmes."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )

    raw = response.choices[0].message.content or ""
    return parse_json_array(raw)


def build_design_brief_conflict_prompt(
    discipline_code: str,
    requirements_text: str,
    bp_blocks_text: str,
    min_confidence: float,
) -> str:
    return f"""
Tu esi būvprojekta audita palīgs Latvijā.

Uzdevums: pārbaudīt, vai dotajos BP disciplīnas {discipline_code} teksta blokos ir redzamas acīmredzamas pretrunas pret Design Brief / MEP prasību atmiņu.

SVARĪGI:
- Šis NAV prasību statusa saraksts.
- Neziņo par prasībām, kas vienkārši nav atrastas.
- Neziņo "jāpārbauda", "nav skaidrs", "iespējams trūkst" bez konkrēta BP teksta bloka.
- Atgriez tikai tādus BP teksta blokus, kuros pašā tekstā ir redzams konflikts, nepareizs parametrs, nepilnīgs risinājums vai skaidrs enkurs neatbilstībai pret Design Brief prasību.
- Ja nav konkrēta source_file + page + block_id no BP dokumenta, neatgriez piezīmi.
- Ja piezīmi nevarētu atzīmēt PDF ar sarkanu rāmi ap konkrēto teksta bloku, neatgriez piezīmi.
- Labāk neatgriezt piezīmi nekā atgriezt apšaubāmu piezīmi.

Atļautie issue_type:
- design_brief_direct_conflict
- design_brief_wrong_parameter
- design_brief_partial_solution_visible
- design_brief_scope_gap_with_anchor

Aizliegtie issue_type:
- not_found
- missing_without_anchor
- general_uncertainty
- please_check

Minimālā pārliecība PDF kandidātam: {min_confidence}

DESIGN BRIEF / MEP PRASĪBU ATMIŅA:
{requirements_text}

BP DOKUMENTA TEKSTA BLOKI:
{bp_blocks_text}

ATBILDES FORMĀTS:
Atbildi tikai JSON masīvā. Ja nav skaidru anotējamu neatbilstību, atgriez [].

Katram objektam jābūt:
- audit_mode: "design_brief_conflict"
- issue_type
- source_file
- page
- block_id
- source_text
- related_memory_id
- related_requirement
- comment: īsa piezīme latviski
- suggestion: īss ieteikums latviski
- confidence: skaitlis 0..1
- priority: skaitlis 1..10
- include_in_pdf: true vai false
"""


def build_internal_consistency_prompt(
    discipline_code: str,
    facts_text: str,
    bp_blocks_text: str,
    min_confidence: float,
) -> str:
    return f"""
Tu esi būvprojekta disciplīnas {discipline_code} iekšējās koordinācijas auditors Latvijā.

Uzdevums: dotajos BP teksta blokos atrodi tikai skaidras un konkrētam PDF teksta blokam piesaistāmas savstarpējās neatbilstības.

Pārbaudes konteksts:
- Viena dokumenta ietvaros: pretrunas vienā PDF.
- Vienas disciplīnas ietvaros: pretrunas starp šīs pašas disciplīnas aprakstu, rasējumiem, specifikāciju, apjomiem un vispārīgajiem datiem.
- Papildus vari izmantot jau izvilkto disciplīnas faktu atmiņu kā atsauci.

Meklē tikai skaidras lietas, piemēram:
- vienam un tam pašam elementam atšķiras diametrs, daudzums, materiāls, sistēmas kods vai parametrs;
- aprakstā minēts viens skaits, specifikācijā vai rasējumā cits;
- specifikācijā ir pozīcija, kas konfliktē ar rasējuma/apraksta parametru;
- LV un EN teksts skaidri saka atšķirīgas tehniskas lietas;
- dokumenta identifikācija vai nosaukums skaidri neatbilst saturam, ja tas var radīt kļūdu.

SVARĪGI:
- Neziņo par lietām, kas vienkārši nav atrastas.
- Neziņo vispārīgi "pārbaudīt".
- Neveido piezīmi, ja nav konkrēta source_file + page + block_id.
- Ja piezīmi nevarētu atzīmēt PDF ar sarkanu rāmi ap konkrēto teksta bloku, neatgriez piezīmi.
- Labāk neatgriezt piezīmi nekā atgriezt apšaubāmu piezīmi.
- Neuzskati atkārtotus grafiskos U1/K1/K2/K3 marķējumus rasējumā par vienu tekstu vai kļūdu.

Atļautie issue_type:
- internal_document_conflict
- discipline_consistency_conflict
- wrong_parameter
- quantity_conflict
- diameter_conflict
- material_conflict
- system_code_conflict
- lv_en_technical_conflict
- specification_drawing_conflict

Aizliegtie issue_type:
- not_found
- missing_without_anchor
- general_uncertainty
- please_check

Minimālā pārliecība PDF kandidātam: {min_confidence}

DISCIPLĪNAS FAKTU ATMIŅA:
{facts_text}

BP DOKUMENTA TEKSTA BLOKI:
{bp_blocks_text}

ATBILDES FORMĀTS:
Atbildi tikai JSON masīvā. Ja nav skaidru anotējamu neatbilstību, atgriez [].

Katram objektam jābūt:
- audit_mode: "discipline_internal_consistency"
- issue_type
- source_file
- page
- block_id
- source_text
- related_memory_id
- related_fact
- related_files: saraksts ar saistītajiem failiem, ja zināmi
- comment: īsa piezīme latviski
- suggestion: īss ieteikums latviski
- confidence: skaitlis 0..1
- priority: skaitlis 1..10
- include_in_pdf: true vai false
"""


# =========================================================
# Issues apstrāde
# =========================================================

def enrich_issues_with_coordinates(
    issues: List[Dict[str, Any]],
    block_index: Dict[Tuple[str, int, int], Dict[str, Any]],
    audit_run: str,
) -> pd.DataFrame:
    enriched = []

    for idx, issue in enumerate(issues, start=1):
        item = dict(issue)

        source_file = str(item.get("source_file", "")).strip()
        try:
            page = int(item.get("page"))
        except Exception:
            page = 0
        try:
            block_id = int(item.get("block_id"))
        except Exception:
            block_id = -1

        key = (source_file, page, block_id)
        block = block_index.get(key)

        item["issue_id"] = f"{audit_run}-ISSUE-{idx:04d}"
        item["has_block_match"] = block is not None

        if block:
            item["x0"] = block.get("x0")
            item["y0"] = block.get("y0")
            item["x1"] = block.get("x1")
            item["y1"] = block.get("y1")
            item["drive_file_id"] = block.get("drive_file_id")
            item["drive_path"] = block.get("drive_path")
            item["document_type"] = block.get("document_type")
            if not item.get("source_text"):
                item["source_text"] = block.get("text")

        item["confidence"] = safe_float(item.get("confidence"), 0.0)
        item["priority"] = safe_int(item.get("priority"), 0)
        item["include_in_pdf"] = bool(item.get("include_in_pdf")) and block is not None

        enriched.append(item)

    if not enriched:
        return pd.DataFrame()

    return pd.DataFrame(enriched)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def filter_final_issues(df: pd.DataFrame, min_confidence: float) -> pd.DataFrame:
    if df.empty:
        return df

    result = df.copy()

    result["confidence"] = pd.to_numeric(result.get("confidence", 0), errors="coerce").fillna(0.0)
    result["priority"] = pd.to_numeric(result.get("priority", 0), errors="coerce").fillna(0).astype(int)

    allowed_issue_types = {
        "design_brief_direct_conflict",
        "design_brief_wrong_parameter",
        "design_brief_partial_solution_visible",
        "design_brief_scope_gap_with_anchor",
        "internal_document_conflict",
        "discipline_consistency_conflict",
        "wrong_parameter",
        "quantity_conflict",
        "diameter_conflict",
        "material_conflict",
        "system_code_conflict",
        "lv_en_technical_conflict",
        "specification_drawing_conflict",
    }

    result = result[result["issue_type"].astype(str).isin(allowed_issue_types)].copy()
    result = result[result["confidence"] >= float(min_confidence)].copy()
    result = result[result["has_block_match"] == True].copy()
    result = result[result["include_in_pdf"] == True].copy()

    return result.reset_index(drop=True)


def clean_excel_illegal_chars(value):
    if isinstance(value, str):
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)
    return value


def normalize_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for col in result.columns:
        result[col] = result[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)
        result[col] = result[col].map(clean_excel_illegal_chars)
    return result


def make_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    excel_df = normalize_for_excel(df)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        excel_df.to_excel(writer, sheet_name="issues", index=False)

        if not excel_df.empty and "audit_mode" in excel_df.columns:
            summary_mode = excel_df.groupby("audit_mode").size().reset_index(name="count")
            summary_mode.to_excel(writer, sheet_name="summary_by_mode", index=False)

        if not excel_df.empty and "issue_type" in excel_df.columns:
            summary_type = excel_df.groupby("issue_type").size().reset_index(name="count")
            summary_type.to_excel(writer, sheet_name="summary_by_type", index=False)

    output.seek(0)
    return output.getvalue()


def make_json_bytes(df: pd.DataFrame) -> bytes:
    export_df = df.copy()

    for col in export_df.columns:
        export_df[col] = export_df[col].apply(lambda v: ", ".join(v) if isinstance(v, list) else v)

    records = export_df.where(pd.notnull(export_df), None).to_dict(orient="records")

    payload = {
        "schema": "bp_audit_issues_v1",
        "count": len(records),
        "issues": records,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# =========================================================
# Streamlit UI
# =========================================================

input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")

st.markdown("## 1. Konfigurācija")
st.write("Input folder ID:", input_folder_id)
st.write("Memory folder ID:", memory_folder_id)

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

blocks_per_ai_batch = st.number_input(
    "Teksta bloku skaits vienā AI pieprasījumā",
    min_value=50,
    max_value=1000,
    value=300,
    step=50,
)

max_requirements_in_prompt = st.number_input(
    "Maksimālais Design Brief prasību skaits promptā",
    min_value=20,
    max_value=300,
    value=120,
    step=10,
)

max_facts_in_prompt = st.number_input(
    "Maksimālais disciplīnas faktu skaits promptā",
    min_value=20,
    max_value=300,
    value=150,
    step=10,
)

min_confidence = st.slider(
    "Minimālā pārliecība anotējamām piezīmēm",
    min_value=0.50,
    max_value=0.95,
    value=0.80,
    step=0.05,
)

delay_between_ai_calls = st.number_input(
    "Pauze starp AI pieprasījumiem sekundēs",
    min_value=0.0,
    max_value=5.0,
    value=0.5,
    step=0.5,
)

run_design_brief_audit = st.checkbox("Pārbaudīt pret Design Brief memory", value=True)
run_internal_audit = st.checkbox("Pārbaudīt disciplīnas iekšējās nesakritības", value=True)

st.warning(
    "Šis ir tabulas tests. Tas vēl neģenerē anotētus PDF. Rezultātā tiek rādītas tikai tās piezīmes, "
    "kurām ir konkrēts source_file + page + block_id un kuras teorētiski varētu apvilkt PDF."
)

if "discipline_folders_df" not in st.session_state:
    st.session_state.discipline_folders_df = pd.DataFrame()

if "memory_bundle" not in st.session_state:
    st.session_state.memory_bundle = {}

if "discipline_pdfs_df" not in st.session_state:
    st.session_state.discipline_pdfs_df = pd.DataFrame()

if "audit_blocks_df" not in st.session_state:
    st.session_state.audit_blocks_df = pd.DataFrame()

if "audit_issues_df" not in st.session_state:
    st.session_state.audit_issues_df = pd.DataFrame()

if st.button("1) Nolasīt 01_Input disciplīnas un 03_Memory"):
    try:
        if not input_folder_id:
            st.error("Secrets nav atrasts GOOGLE_DRIVE_INPUT_FOLDER_ID.")
            st.stop()
        if not memory_folder_id:
            st.error("Secrets nav atrasts GOOGLE_DRIVE_MEMORY_FOLDER_ID.")
            st.stop()

        drive_service = get_drive_service()
        folders_df = get_discipline_folders(drive_service, input_folder_id)
        memory_bundle = load_project_memory(drive_service, memory_folder_id)

        st.session_state.discipline_folders_df = folders_df
        st.session_state.memory_bundle = memory_bundle

        req_count = len(memory_bundle.get("requirements", pd.DataFrame()))
        fact_count = len(memory_bundle.get("facts", pd.DataFrame()))

        st.success(
            f"Nolasītas disciplīnas: {len(folders_df)}. "
            f"Memory prasības: {req_count}. Memory fakti: {fact_count}."
        )

    except Exception as e:
        st.error("Neizdevās nolasīt Google Drive vai memory.")
        st.exception(e)

folders_df = st.session_state.discipline_folders_df
memory_bundle = st.session_state.memory_bundle

if not folders_df.empty:
    st.markdown("## 2. Izvēlies auditējamo disciplīnu")

    st.dataframe(folders_df, use_container_width=True)

    folder_options = folders_df["folder_name"].tolist()
    default_index = 0
    for i, folder_name in enumerate(folder_options):
        if "09_UKT" in folder_name:
            default_index = i
            break

    selected_folder_name = st.selectbox(
        "Disciplīnas mape",
        options=folder_options,
        index=default_index,
    )

    selected_folder_row = folders_df[folders_df["folder_name"] == selected_folder_name].iloc[0]
    selected_discipline_code = selected_folder_row["discipline_code"]
    selected_folder_id = selected_folder_row["folder_id"]

    st.write("Izvēlētā disciplīna:", selected_discipline_code)

    if st.button("2) Atrast disciplīnas PDF failus"):
        try:
            drive_service = get_drive_service()
            pdfs_df = get_pdf_documents_in_discipline(
                drive_service,
                discipline_folder_id=selected_folder_id,
                discipline_folder_name=selected_folder_name,
            )

            st.session_state.discipline_pdfs_df = pdfs_df

            if pdfs_df.empty:
                st.warning("Šajā disciplīnā nav atrasti PDF faili.")
            else:
                st.success(f"Atrasti {len(pdfs_df)} PDF faili.")

        except Exception as e:
            st.error("Neizdevās atrast disciplīnas PDF failus.")
            st.exception(e)

pdfs_df = st.session_state.discipline_pdfs_df

if not pdfs_df.empty:
    st.markdown("## 3. Disciplīnas PDF faili")
    st.dataframe(
        pdfs_df[["name", "path", "document_type", "size", "modifiedTime"]],
        use_container_width=True,
    )

    file_options = pdfs_df["path"].tolist()

    default_paths = file_options[:]

    selected_paths = st.multiselect(
        "Izvēlies PDF failus auditam",
        options=file_options,
        default=default_paths,
    )

    selected_docs_df = pdfs_df[pdfs_df["path"].isin(selected_paths)].copy()

    st.markdown("### Auditam izvēlētie faili")
    st.dataframe(selected_docs_df[["name", "path", "document_type", "size"]], use_container_width=True)

    if st.button("3) Izvilkt PDF teksta blokus"):
        try:
            drive_service = get_drive_service()
            blocks_df, pdf_summary_df = download_and_extract_selected_blocks(
                service=drive_service,
                selected_docs_df=selected_docs_df,
                max_pages_per_pdf=int(max_pages_per_pdf),
            )

            st.session_state.audit_blocks_df = blocks_df

            st.success(f"Izvilkti {len(blocks_df)} teksta bloki no {len(selected_docs_df)} PDF failiem.")
            st.markdown("### PDF apstrādes kopsavilkums")
            st.dataframe(pdf_summary_df, use_container_width=True)

        except Exception as e:
            st.error("Neizdevās izvilkt PDF teksta blokus.")
            st.exception(e)

blocks_df = st.session_state.audit_blocks_df

if not blocks_df.empty:
    st.markdown("## 4. Teksta bloku priekšskatījums")
    st.dataframe(
        blocks_df[["source_file", "document_type", "page", "block_id", "text", "x0", "y0", "x1", "y1"]].head(100),
        use_container_width=True,
    )

    if st.button("4) Palaist disciplīnas auditu pret Design Brief un iekšējām nesakritībām"):
        try:
            if not run_design_brief_audit and not run_internal_audit:
                st.warning("Nav izvēlēts neviens audita režīms.")
                st.stop()

            client = get_openai_client()
            block_index = build_block_index(blocks_df)

            requirements_df = memory_bundle.get("requirements", pd.DataFrame())
            facts_df = memory_bundle.get("facts", pd.DataFrame())

            # Disciplīnas kodu ņemam no mapes, kas atbilst izvēlētajiem failiem.
            # Ja lietotājs maina izvēli pēc pirmās nolasīšanas, kods tiek atrasts no path pirmās mapes.
            first_path = str(blocks_df["drive_path"].iloc[0]) if "drive_path" in blocks_df.columns else ""
            first_folder = first_path.split("/", 1)[0] if "/" in first_path else selected_folder_name
            discipline_code_for_audit = get_discipline_code_from_folder_name(first_folder)

            relevant_requirements_df = filter_requirements_for_discipline(
                requirements_df,
                discipline_code=discipline_code_for_audit,
                max_rows=int(max_requirements_in_prompt),
            )

            relevant_facts_df = filter_facts_for_discipline(
                facts_df,
                discipline_code=discipline_code_for_audit,
                max_rows=int(max_facts_in_prompt),
            )

            requirements_text = requirements_to_prompt_lines(relevant_requirements_df)
            facts_text = facts_to_prompt_lines(relevant_facts_df)

            st.markdown("### Auditam izmantotā memory atlase")
            st.write(f"Design Brief prasības promptā: {len(relevant_requirements_df)}")
            st.write(f"Disciplīnas fakti promptā: {len(relevant_facts_df)}")

            batches = make_batches_from_blocks(blocks_df, blocks_per_batch=int(blocks_per_ai_batch))
            st.write(f"AI pieprasījumu batch skaits: {len(batches)}")

            all_issues: List[Dict[str, Any]] = []
            progress = st.progress(0)
            status = st.empty()

            total_calls = len(batches) * int(run_design_brief_audit + run_internal_audit)
            completed_calls = 0
            audit_run = f"{project_code}-{discipline_code_for_audit}"

            for batch_index, batch_df in enumerate(batches, start=1):
                bp_blocks_text = blocks_to_prompt_text(batch_df)

                if run_design_brief_audit:
                    status.write(f"Design Brief audits: batch {batch_index}/{len(batches)}")
                    prompt = build_design_brief_conflict_prompt(
                        discipline_code=discipline_code_for_audit,
                        requirements_text=requirements_text,
                        bp_blocks_text=bp_blocks_text,
                        min_confidence=float(min_confidence),
                    )

                    try:
                        issues = call_openai_json_array(client, model=model, prompt=prompt)
                        for issue in issues:
                            issue["batch_index"] = batch_index
                        all_issues.extend(issues)
                    except Exception as e:
                        st.error(f"Kļūda Design Brief auditā, batch {batch_index}")
                        st.exception(e)

                    completed_calls += 1
                    if total_calls:
                        progress.progress(completed_calls / total_calls)
                    if float(delay_between_ai_calls) > 0:
                        time.sleep(float(delay_between_ai_calls))

                if run_internal_audit:
                    status.write(f"Iekšējās nesakritības audits: batch {batch_index}/{len(batches)}")
                    prompt = build_internal_consistency_prompt(
                        discipline_code=discipline_code_for_audit,
                        facts_text=facts_text,
                        bp_blocks_text=bp_blocks_text,
                        min_confidence=float(min_confidence),
                    )

                    try:
                        issues = call_openai_json_array(client, model=model, prompt=prompt)
                        for issue in issues:
                            issue["batch_index"] = batch_index
                        all_issues.extend(issues)
                    except Exception as e:
                        st.error(f"Kļūda iekšējās nesakritības auditā, batch {batch_index}")
                        st.exception(e)

                    completed_calls += 1
                    if total_calls:
                        progress.progress(completed_calls / total_calls)
                    if float(delay_between_ai_calls) > 0:
                        time.sleep(float(delay_between_ai_calls))

            raw_issues_df = enrich_issues_with_coordinates(
                issues=all_issues,
                block_index=block_index,
                audit_run=audit_run,
            )

            final_issues_df = filter_final_issues(raw_issues_df, min_confidence=float(min_confidence))
            st.session_state.audit_issues_df = final_issues_df

            st.success(
                f"AI atgrieza {len(raw_issues_df)} kandidātpiezīmes; "
                f"pēc stingrā filtra palika {len(final_issues_df)} anotējamas piezīmes."
            )

        except Exception as e:
            st.error("Neizdevās palaist auditu.")
            st.exception(e)

issues_df = st.session_state.audit_issues_df

if not issues_df.empty:
    st.markdown("## 5. Anotējamo piezīmju kandidāti")

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
        "related_fact",
        "related_files",
        "document_type",
        "x0",
        "y0",
        "x1",
        "y1",
        "include_in_pdf",
        "has_block_match",
        "drive_path",
        "batch_index",
    ]

    existing_cols = [col for col in preferred_cols if col in issues_df.columns]
    other_cols = [col for col in issues_df.columns if col not in existing_cols]

    st.markdown("### Kopsavilkums pēc audita režīma")
    if "audit_mode" in issues_df.columns:
        st.dataframe(issues_df.groupby("audit_mode").size().reset_index(name="count"), use_container_width=True)

    st.markdown("### Kopsavilkums pēc issue_type")
    if "issue_type" in issues_df.columns:
        st.dataframe(issues_df.groupby("issue_type").size().reset_index(name="count"), use_container_width=True)

    st.markdown("### Piezīmju tabula")
    edited_df = st.data_editor(
        issues_df[existing_cols + other_cols],
        use_container_width=True,
        num_rows="dynamic",
        key="audit_issues_editor",
    )

    st.session_state.audit_issues_df = edited_df

    excel_bytes = make_excel_bytes(edited_df)
    json_bytes = make_json_bytes(edited_df)

    base_name = f"{project_code.lower().replace('-', '_')}_discipline_audit_issues"

    col1, col2 = st.columns(2)

    with col1:
        st.download_button(
            "Lejupielādēt issues Excel",
            data=excel_bytes,
            file_name=f"{base_name}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with col2:
        st.download_button(
            "Lejupielādēt issues JSON",
            data=json_bytes,
            file_name=f"{base_name}.json",
            mime="application/json",
        )
