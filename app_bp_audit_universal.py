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

st.set_page_config(page_title="BP universālais audita rīks v3.3.2", layout="wide")

st.title("BP universālais audita rīks v3.3.2")
st.write(
    "Universāls būvprojekta audita tests: Design Brief, viena dokumenta konsekvence, "
    "disciplīnas iekšējā konsekvence un starpdisciplīnu konsekvence. "
    "v3.3.2: audit_mode tiek noteikts pēc moduļa; Balanced filtrs atpazīst arī tehniski līdzīgus issue_type un neprasa include_in_pdf=true. "
    "PDF anotēšana vēl nav ieslēgta."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"

# =========================================================
# Google Drive
# =========================================================


def get_drive_service():
    service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not service_account_json:
        raise ValueError("Secrets nav atrasts GOOGLE_SERVICE_ACCOUNT_JSON.")

    info = json.loads(service_account_json)
    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=credentials)


def list_folder_items(service, folder_id: str) -> List[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and trashed = false"
    out = []
    page_token = None
    while True:
        result = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        out.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return out


def list_items_recursive(service, folder_id: str, parent_path: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in list_folder_items(service, folder_id):
        name = item.get("name", "")
        path = f"{parent_path}/{name}" if parent_path else name
        is_folder = item.get("mimeType") == FOLDER_MIME_TYPE
        rows.append({
            "name": name,
            "path": path,
            "id": item.get("id"),
            "mimeType": item.get("mimeType"),
            "size": item.get("size", ""),
            "modifiedTime": item.get("modifiedTime", ""),
            "is_folder": is_folder,
        })
        if is_folder:
            rows.extend(list_items_recursive(service, item.get("id"), path))
    return rows


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()


def export_google_file_bytes(service, file_id: str, mime_type: str) -> bytes:
    request = service.files().export_media(fileId=file_id, mimeType=mime_type)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buffer.seek(0)
    return buffer.read()

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


def get_pdf_documents_in_discipline(service, folder_id: str, folder_name: str) -> pd.DataFrame:
    rows = list_items_recursive(service, folder_id, folder_name)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    pdf_df = df[(df["is_folder"] == False) & (df["mimeType"] == PDF_MIME_TYPE)].copy()
    if pdf_df.empty:
        return pdf_df
    pdf_df["document_type"] = pdf_df.apply(lambda r: classify_document_type(r.get("name", ""), r.get("path", "")), axis=1)
    return pdf_df

# =========================================================
# PDF teksta bloki
# =========================================================


def extract_pdf_page_blocks(pdf_bytes: bytes, max_pages: int) -> Tuple[pd.DataFrame, int]:
    rows = []
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
                clean = re.sub(r"\s+", " ", str(text)).strip()
                if not clean:
                    continue
                rows.append({
                    "page": page_index + 1,
                    "block_id": block_index,
                    "x0": round(float(x0), 2),
                    "y0": round(float(y0), 2),
                    "x1": round(float(x1), 2),
                    "y1": round(float(y1), 2),
                    "text": clean,
                })
    return pd.DataFrame(rows), total_pages


def make_block_batches(blocks_df: pd.DataFrame, max_blocks: int) -> List[pd.DataFrame]:
    if blocks_df.empty:
        return []
    return [blocks_df.iloc[i:i + max_blocks].copy() for i in range(0, len(blocks_df), max_blocks)]


def build_text_from_blocks(blocks_df: pd.DataFrame, max_blocks: int = 220) -> str:
    lines = []
    for _, r in blocks_df.head(max_blocks).iterrows():
        lines.append(
            f"[source_file={r.get('source_file')} document_type={r.get('document_type')} "
            f"page={r.get('page')} block_id={r.get('block_id')}] {r.get('text')}"
        )
    return "\n".join(lines)


def expand_blocks_with_context(all_blocks_df: pd.DataFrame, anchor_blocks_df: pd.DataFrame, context_window: int = 2) -> pd.DataFrame:
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
    expanded = expanded.drop_duplicates(subset=["source_file", "page", "block_id"])
    return expanded.sort_values(["source_file", "page", "block_id"])


def build_context_text(all_blocks_df: pd.DataFrame, anchor_blocks_df: pd.DataFrame, max_blocks: int = 260, context_window: int = 2) -> str:
    expanded = expand_blocks_with_context(all_blocks_df, anchor_blocks_df, context_window=context_window)
    return build_text_from_blocks(expanded, max_blocks=max_blocks)


def get_blocks_by_type(blocks_df: pd.DataFrame, document_types: List[str]) -> pd.DataFrame:
    if blocks_df.empty or "document_type" not in blocks_df.columns:
        return pd.DataFrame()
    return blocks_df[blocks_df["document_type"].isin(document_types)].copy()


def get_orientation_blocks(blocks_df: pd.DataFrame) -> pd.DataFrame:
    note = get_blocks_by_type(blocks_df, ["explanatory_note"])
    if not note.empty:
        return note
    general = get_blocks_by_type(blocks_df, ["general_data"])
    if not general.empty:
        return general
    return blocks_df.head(260).copy()

# =========================================================
# Memory un prompti
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
    catalog = []
    requirements = []
    facts = []
    for item in list_folder_items(service, memory_folder_id):
        name = str(item.get("name", ""))
        if not name.lower().endswith(".json"):
            continue
        try:
            payload = json.loads(download_drive_file_bytes(service, item.get("id")).decode("utf-8", errors="replace"))
            records = []
            kind = "unknown_json"
            schema = ""
            detected_discipline = ""
            if isinstance(payload, dict):
                schema = str(payload.get("memory_schema", payload.get("schema", "")))
                if isinstance(payload.get("requirements"), list):
                    records = payload["requirements"]
                    kind = "design_brief_requirements"
                elif isinstance(payload.get("facts"), list):
                    records = payload["facts"]
                    kind = "discipline_facts"
                elif isinstance(payload.get("records"), list):
                    records = payload["records"]
            elif isinstance(payload, list):
                records = payload
            if kind == "unknown_json" and records and isinstance(records[0], dict):
                sample = records[0]
                if "requirement" in sample or "memory_type" in sample and "requirement" in str(sample.get("memory_type")):
                    kind = "design_brief_requirements"
                elif "fact_type" in sample or "fact_id" in sample:
                    kind = "discipline_facts"
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                rec = dict(rec)
                rec["memory_source_file"] = name
                if kind == "design_brief_requirements":
                    requirements.append(rec)
                elif kind == "discipline_facts":
                    facts.append(rec)
                    detected_discipline = detected_discipline or str(rec.get("discipline") or rec.get("memory_discipline") or "")
            catalog.append({
                "name": name,
                "kind": kind,
                "memory_schema": schema,
                "records_count": len(records),
                "detected_discipline": detected_discipline,
                "size": item.get("size", ""),
                "modifiedTime": item.get("modifiedTime", ""),
            })
        except Exception as e:
            catalog.append({"name": name, "kind": "error", "error": str(e)})
    req_df = pd.DataFrame(requirements)
    facts_df = pd.DataFrame(facts)
    if not req_df.empty:
        if "discipline_list" not in req_df.columns and "discipline" in req_df.columns:
            req_df["discipline_list"] = req_df["discipline"].apply(parse_list_value)
        if "applies_to_sections_list" not in req_df.columns and "applies_to_sections" in req_df.columns:
            req_df["applies_to_sections_list"] = req_df["applies_to_sections"].apply(parse_list_value)
    return pd.DataFrame(catalog), req_df, facts_df



def load_audit_examples_json_files(service, memory_folder_id: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Load accepted audit example JSON files from 03_Memory and subfolders, especially audit_examples/."""
    catalog: List[Dict[str, Any]] = []
    examples: List[Dict[str, Any]] = []

    for item in list_items_recursive(service, memory_folder_id, ""):
        name = str(item.get("name", ""))
        path = str(item.get("path", name))
        mime = str(item.get("mimeType", ""))

        if item.get("is_folder"):
            continue
        if not name.lower().endswith(".json"):
            continue

        lower_path = path.lower()
        lower_name = name.lower()
        looks_like_examples = (
            "audit_examples" in lower_path
            or "accepted_audit_examples" in lower_name
            or "audit_example" in lower_name
        )
        if not looks_like_examples:
            continue

        try:
            payload = json.loads(download_drive_file_bytes(service, item.get("id")).decode("utf-8", errors="replace"))
            schema = ""
            project_code_value = ""
            source_discipline = ""
            records: List[Dict[str, Any]] = []

            if isinstance(payload, dict):
                schema = str(payload.get("memory_schema", payload.get("schema", "")))
                project_code_value = str(payload.get("project_code", ""))
                source_discipline = str(payload.get("source_discipline", payload.get("discipline", "")))
                for key in ["examples", "audit_examples", "records"]:
                    if isinstance(payload.get(key), list):
                        records = payload.get(key)
                        break
            elif isinstance(payload, list):
                records = payload

            for rec in records:
                if not isinstance(rec, dict):
                    continue
                row = dict(rec)
                row["audit_examples_source_file"] = name
                row["audit_examples_source_path"] = path
                if not row.get("project_code"):
                    row["project_code"] = project_code_value
                if not row.get("source_discipline"):
                    row["source_discipline"] = source_discipline
                examples.append(row)

            catalog.append({
                "name": name,
                "path": path,
                "kind": "accepted_audit_examples",
                "memory_schema": schema,
                "project_code": project_code_value,
                "source_discipline": source_discipline,
                "records_count": len(records),
                "size": item.get("size", ""),
                "modifiedTime": item.get("modifiedTime", ""),
                "mimeType": mime,
            })
        except Exception as e:
            catalog.append({"name": name, "path": path, "kind": "accepted_audit_examples_error", "error": str(e)})

    examples_df = pd.DataFrame(examples)
    if not examples_df.empty and "use_as_training_example" in examples_df.columns:
        examples_df = examples_df[
            examples_df["use_as_training_example"].astype(str).str.lower().isin(["true", "1", "yes", "jā", "ja"])
        ].copy()

    return pd.DataFrame(catalog), examples_df

def load_prompt_assets(service, prompt_folder_id: str) -> Tuple[pd.DataFrame, str, str]:
    catalog = []
    universal_prompt = ""
    error_examples_text = ""
    for item in list_folder_items(service, prompt_folder_id):
        name = str(item.get("name", ""))
        mime = str(item.get("mimeType", ""))
        row = {"name": name, "mimeType": mime, "size": item.get("size", ""), "modifiedTime": item.get("modifiedTime", "")}
        try:
            lower = name.lower()
            if lower.endswith(".txt"):
                text = download_drive_file_bytes(service, item.get("id")).decode("utf-8", errors="replace")
                if "universal" in lower or not universal_prompt:
                    universal_prompt = text
                row["loaded"] = True
            elif lower.endswith(".xlsx") or mime == GOOGLE_SHEET_MIME_TYPE:
                if mime == GOOGLE_SHEET_MIME_TYPE:
                    data = export_google_file_bytes(service, item.get("id"), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                else:
                    data = download_drive_file_bytes(service, item.get("id"))
                df = pd.read_excel(io.BytesIO(data))
                error_examples_text += f"\n\n### {name}\n" + df.head(150).fillna("").astype(str).to_csv(index=False)[:25000]
                row["rows_loaded"] = len(df)
            elif lower.endswith(".pdf"):
                row["loaded"] = "pdf_registered"
        except Exception as e:
            row["error"] = str(e)
        catalog.append(row)
    return pd.DataFrame(catalog), universal_prompt, error_examples_text

# =========================================================
# OpenAI
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


def call_ai_json_array(client: OpenAI, model: str, system: str, prompt: str, temperature: float = 0.0) -> Tuple[List[Dict[str, Any]], str]:
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
# Prompt builders
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
Atgriez tikai tehniski vērtīgas kandidātpiezīmes, kas varētu kļūt par PDF anotācijām.
Minimālā mērķa pārliecība: {confidence_threshold}.
Nerādi zemas vērtības dokumenta identitātes/datuma/projekta koda piezīmes, ja tās nav tieši tehniski nozīmīgas.
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
- related_memory_id
- related_requirement
- related_fact_id
- related_fact
- related_files
- audit_scenario: scenārijs no zelta parauga loģikas, ja piemērojams
- comparison_basis: pret ko tieši salīdzināts
- conflicting_evidence: konkrētā pretējā puse / trūkstošā izsekojamība
- why_it_matters: kāpēc tas ir būtiski

Svarīgi:
- audit_mode vari aizpildīt, bet sistēma to pārrakstīs pēc moduļa.
- Nekad neatgriez piezīmi bez source_file, page un block_id.
- Ja piezīmi nevar piesaistīt konkrētam BP teksta blokam, neatgriez to kā PDF piezīmi.
"""


def build_fact_prompt(project_code: str, discipline_code: str, text_blocks: str, source_hint: str, error_examples_text: str) -> str:
    return f"""
Tu veido pagaidu faktu indeksu būvprojekta audita vajadzībām.
Projekts: {project_code}
Disciplīna: {discipline_code}
Avots: {source_hint}

Uzdevums: no teksta blokiem izvelc tehniskus faktus, kas vēlāk palīdz atrast nesakritības starp dokumentiem.
Neveido kļūdu piezīmes. Nevērtē pareizību. Tikai strukturēti fakti.

Izvelc: diametrus, materiālus, markas, tipus, klases, daudzumus, jaudas, sistēmu kodus, specifikācijas pozīcijas, iekārtas, telpas, pieslēgumus, robežas starp sadaļām.

C2-2 kļūdu piemēru loģika:
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


def build_design_brief_prompt(requirements_text: str, text_blocks: str, audit_depth: str, confidence_threshold: float) -> str:
    return f"""
Tu pārbaudi BP dokumenta tekstu pret Design Brief prasību atmiņu.
Mērķis NAV izveidot prasību statusa sarakstu. Meklē tikai BP dokumenta teksta blokus, kuros redzama skaidra vai ticama pretruna pret Design Brief prasību.

NEZIŅO par prasībām, kas vienkārši nav atrastas.
NEZIŅO "vajag pārbaudīt" bez konkrēta teksta bloka.

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


def build_single_doc_prompt(document_name: str, document_type: str, text_blocks: str, facts_text: str, error_examples_text: str, audit_depth: str, confidence_threshold: float) -> str:
    return f"""
Tu veic viena BP dokumenta iekšējo konsekvences pārbaudi.
Dokuments: {document_name}
Dokumenta tips: {document_type}

Meklē tikai kļūdas un nesakritības šī paša dokumenta ietvaros.
Meklē tehniskas pretrunas: diametri, materiāli, markas, sistēmu kodi, daudzumi, specifikācijas pozīcijas, iekārtu parametri, LV/ENG tehniskās nozīmes nesakritības.
Nedod zemas vērtības piezīmes par datumu, projekta kodu vai dokumenta identitāti, ja tās nav tieši būtiskas tehniskai neatbilstībai.

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

C2-2 kļūdu piemēri:
{error_examples_text[:10000]}

Pagaidu fakti no šī dokumenta:
{facts_text[:14000]}

Dokumenta teksta bloki:
{text_blocks}
"""


def build_structured_discipline_pair_prompt(discipline_code: str, comparison_step: str, reference_label: str, reference_text: str, target_label: str, target_text: str, facts_text: str, error_examples_text: str, audit_depth: str, confidence_threshold: float) -> str:
    return f"""
Tu veic universālu būvprojekta disciplīnas iekšējās konsekvences pārbaudi.
Disciplīna: {discipline_code}
Salīdzināšanas solis: {comparison_step}

Universāls princips:
1. Skaidrojošais apraksts / vispārīgie dati ir orientieris.
2. Rasējumi parāda realizāciju un tekstuālos parametrus.
3. Specifikācija rāda materiālus, markas, daudzumus un pozīcijas.
4. Piezīmi drīkst dot tikai par konkrētu tekstuālu bloku, ko varētu apvilkt PDF.

Meklē tehniskus konfliktus: diametrs, materiāls, skaits, sistēmas kods, specifikācijas pozīcija pret rasējumu/aprakstu, iekārtas tips/marka/parametrs, pieslēguma robeža.
Nedod zemas vērtības piezīmes par datumu, projekta kodu vai dokumenta identitāti, ja tās nav tieši būtiskas tehniskai neatbilstībai.

Zelta parauga piemēru princips:
- Ja zelta piemēros redzi scenāriju “risinājums ir aprakstā/rasējumā, bet nav specifikācijā”, meklē šādu izsekojamības trūkumu arī jaunajos dokumentos.
- Ja redzi scenāriju “rasējuma nosaukums/saraksts min sistēmas, kas pašā rasējumā nav izsekojamas”, meklē līdzīgu problēmu.
- Ja nav konkrētas comparison_basis vai conflicting_evidence, piezīmi neatgriez Balanced/Conservative režīmā.
- Laba piezīme nav “pārbaudīt”; laba piezīme pasaka, kas konkrēti nav izsekojams vai kas ar ko nesakrīt.

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

C2-2 kļūdu piemēri:
{error_examples_text[:9000]}

Pagaidu faktu indekss:
{facts_text[:18000]}

REFERENCE — {reference_label}:
{reference_text[:24000]}

TARGET — {target_label}:
{target_text[:24000]}
"""


def build_interdisciplinary_prompt(discipline_code: str, prior_facts_text: str, current_facts_text: str, text_blocks: str, audit_depth: str, confidence_threshold: float) -> str:
    return f"""
Tu veic starpdisciplīnu konsekvences pārbaudi.
Auditējamā disciplīna: {discipline_code}

Salīdzini auditējamās disciplīnas tekstu/faktus pret 03_Memory saglabātajiem citu disciplīnu faktiem.
Piezīmi drīkst piesaistīt tikai auditējamās disciplīnas teksta blokam.

Meklē tikai skaidras pretrunas: diametrs, jauda, skaits, materiāls, sistēmas kods, pieslēguma robeža, telpa, iekārta, scope/boundary conflict.

{mode_rules(audit_depth, confidence_threshold)}
{issue_schema_instruction()}

Citu disciplīnu fakti:
{prior_facts_text[:18000]}

Auditējamās disciplīnas pagaidu fakti:
{current_facts_text[:12000]}

Auditējamās disciplīnas teksta bloki:
{text_blocks[:22000]}
"""

# =========================================================
# Datu sagatavošana un filtrēšana
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




def build_audit_examples_library_text(audit_examples_df: pd.DataFrame, current_discipline: str, max_rows: int = 80) -> str:
    """Create compact text for accepted audit examples. Prefer examples from other disciplines first."""
    if audit_examples_df is None or audit_examples_df.empty:
        return ""

    df = audit_examples_df.copy()
    current = str(current_discipline).upper().strip()

    if "use_as_training_example" in df.columns:
        df = df[df["use_as_training_example"].astype(str).str.lower().isin(["true", "1", "yes", "jā", "ja"])].copy()
    if df.empty:
        return ""

    if "source_discipline" in df.columns:
        df["_is_current_discipline"] = df["source_discipline"].astype(str).str.upper().str.strip().eq(current)
        df = df.sort_values(["_is_current_discipline"], ascending=True)

    useful_cols = [
        "example_id", "source_discipline", "source_document_role", "audit_category", "audit_scenario",
        "issue_type", "problem", "why_it_matters", "good_comment_style", "comparison_basis",
        "comparison_references", "notes_for_tool",
    ]
    cols = [c for c in useful_cols if c in df.columns]
    if not cols:
        return ""

    csv_text = df[cols].head(max_rows).fillna("").astype(str).to_csv(index=False)
    return f"""
ZELTA PARAUGA AUDITA PIEMĒRI
Šie nav projekta fakti un nav Design Brief prasības. Tie ir cilvēka akceptēti audita piezīmju piemēri.
No tiem jāmācās audita scenāriju loģika, komentāra stils un tas, kas ir derīga piezīme.
Nedrīkst akli kopēt konkrētās atbildes uz citu disciplīnu. Jāmeklē līdzīga tipa kļūdas jaunajos dokumentos.

{csv_text}
"""


def enrich_calibration_text(error_examples_text: str, audit_examples_df: pd.DataFrame, current_discipline: str) -> str:
    audit_examples_text = build_audit_examples_library_text(audit_examples_df, current_discipline, max_rows=80)
    if audit_examples_text:
        return error_examples_text + "\n\n" + audit_examples_text
    return error_examples_text

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


def normalize_issue(issue: Dict[str, Any], default_mode: str, source_lookup: pd.DataFrame) -> Dict[str, Any]:
    item = dict(issue)
    item["audit_mode"] = default_mode  # v3.1: nepārņemam AI doto audit_mode
    item["issue_type"] = str(item.get("issue_type") or "unknown").strip()
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


def filter_issues(raw_df: pd.DataFrame, confidence_threshold: float, audit_depth: str) -> Tuple[pd.DataFrame, pd.DataFrame]:
    if raw_df.empty:
        return raw_df, raw_df

    work = raw_df.copy()
    work["issue_type_norm"] = work["issue_type"].fillna("").astype(str).str.lower().str.strip()

    disallowed = [
        "not_found",
        "general_uncertainty",
        "please_check",
        "missing_without_anchor",
        "unanchored_possible_omission",
    ]

    low_value_for_pdf = [
        "project_code_inconsistency",
        "document_identity_conflict",
        "document_identity_mismatch",
        "document_identity_inconsistency",
        "date_conflict",
        "date_inconsistency",
        "parallel_text_inconsistency",
    ]

    technical_issue_types = [
        "diameter_conflict",
        "diameter_inconsistency",
        "pipe_diameter_inconsistency",
        "pipe_diameter_conflict",
        "pipe_material_inconsistency",
        "pipe_material_conflict",
        "material_conflict",
        "material_inconsistency",
        "quantity_conflict",
        "quantity_inconsistency",
        "quantity_mismatch",
        "specification_position_conflict",
        "system_code_conflict",
        "drawing_reference_conflict",
        "design_brief_direct_conflict",
        "design_brief_wrong_parameter",
        "design_brief_partial_solution_visible",
        "design_brief_scope_gap_with_anchor",
        "equipment_conflict",
        "equipment_parameter_conflict",
        "connection_conflict",
        "interface_conflict",
        "power_or_flow_conflict",
    ]

    technical_keywords = [
        "diameter",
        "material",
        "quantity",
        "specification",
        "system_code",
        "drawing_reference",
        "design_brief",
        "equipment",
        "connection",
        "interface",
        "power",
        "flow",
        "pipe",
        "mark",
        "type",
        "class",
        "parameter",
    ]

    conflict_words = ["conflict", "mismatch", "inconsistency", "discrepancy", "contradiction"]

    base_mask = (
        (work["has_anchor"] == True)
        & (~work["issue_type_norm"].isin(disallowed))
    )

    explicit_technical = work["issue_type_norm"].isin(technical_issue_types)
    keyword_technical = work["issue_type_norm"].apply(
        lambda value: any(keyword in value for keyword in technical_keywords)
        and any(word in value for word in conflict_words)
    )
    technical_like = explicit_technical | keyword_technical

    if audit_depth == "Diagnostic":
        kept = work[base_mask].copy()
        removed = work[~base_mask].copy()
        return kept, removed

    if audit_depth == "Balanced":
        # v3.3.2: Balanced is meant for human review, not final PDF.
        # It requires anchor + confidence + technical-like issue type,
        # but it does not require include_in_pdf=True because AI often sets it inconsistently.
        mask = (
            base_mask
            & (work["confidence"] >= confidence_threshold)
            & technical_like
            & (~work["issue_type_norm"].isin(low_value_for_pdf))
        )
        return work[mask].copy(), work[~mask].copy()

    # Conservative is closer to PDF annotation quality.
    # It is stricter and still avoids low-value identity/date comments.
    mask = (
        base_mask
        & (work["confidence"] >= max(confidence_threshold, 0.75))
        & technical_like
        & (~work["issue_type_norm"].isin(low_value_for_pdf))
        & (work["priority"] >= 7)
    )
    return work[mask].copy(), work[~mask].copy()


def make_excel_bytes(df: pd.DataFrame, raw_df: pd.DataFrame, removed_df: pd.DataFrame, facts_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="issues_filtered", index=False)
        raw_df.to_excel(writer, sheet_name="issues_raw", index=False)
        removed_df.to_excel(writer, sheet_name="issues_removed", index=False)
        facts_df.to_excel(writer, sheet_name="temp_facts", index=False)
    output.seek(0)
    return output.getvalue()


def make_json_bytes(df: pd.DataFrame, raw_df: pd.DataFrame, removed_df: pd.DataFrame, facts_df: pd.DataFrame, project_code: str, discipline_code: str) -> bytes:
    payload = {
        "schema": "bp_audit_universal_v3_3",
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
# Session state
# =========================================================

for key, default in {
    "disciplines_df": pd.DataFrame(), "memory_catalog_df": pd.DataFrame(), "requirements_df": pd.DataFrame(),
    "memory_facts_df": pd.DataFrame(), "prompt_catalog_df": pd.DataFrame(), "universal_prompt": "", "error_examples_text": "",
    "discipline_pdfs_df": pd.DataFrame(), "selected_docs_df": pd.DataFrame(), "blocks_df": pd.DataFrame(), "file_summary_df": pd.DataFrame(),
    "temp_facts_df": pd.DataFrame(), "issues_raw_df": pd.DataFrame(), "issues_filtered_df": pd.DataFrame(), "issues_removed_df": pd.DataFrame(),
    "raw_ai_log_df": pd.DataFrame(),
    "audit_examples_catalog_df": pd.DataFrame(),
    "audit_examples_df": pd.DataFrame(),
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# =========================================================
# UI
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

col1, col2 = st.columns(2)
with col1:
    max_pages_per_pdf = st.number_input("Maksimālais lapu skaits no viena PDF", min_value=1, max_value=300, value=100, step=5)
    max_blocks_per_ai = st.number_input("Teksta bloku skaits vienā AI pieprasījumā", min_value=50, max_value=800, value=220, step=10)
    max_design_requirements = st.number_input("Maksimālais Design Brief prasību skaits promptā", min_value=20, max_value=500, value=180, step=10)
with col2:
    max_memory_facts = st.number_input("Maksimālais citu disciplīnu faktu skaits promptā", min_value=20, max_value=500, value=180, step=10)
    confidence_threshold = st.slider("Minimālā pārliecība anotējamām piezīmēm", min_value=0.0, max_value=1.0, value=0.70, step=0.05)
    delay_between_ai_calls = st.number_input("Pauze starp AI pieprasījumiem sekundēs", min_value=0.0, max_value=5.0, value=0.5, step=0.5)

audit_depth = st.radio("Audita dziļums", options=["Conservative", "Balanced", "Diagnostic"], index=1, horizontal=True)

st.markdown("### Audita moduļi")
run_module_a = st.checkbox("A. Pret Design Brief prasību atmiņu", value=True)
run_module_b = st.checkbox("B. Katra dokumenta iekšējā konsekvence", value=True)
run_module_c = st.checkbox("C. Disciplīnas savstarpējā konsekvence", value=True)
run_module_d = st.checkbox("D. Starpdisciplīnu konsekvence pret līdzšinējo 03_Memory", value=False)

st.info("v3.3.2: audit_mode tiek noteikts pēc moduļa; Balanced filtrs atpazīst arī tehniski līdzīgus issue_type un neprasa include_in_pdf=true. v3.3 lasa audit_examples zelta paraugus no 03_Memory/audit_examples un izmanto tos kā audita scenāriju bibliotēku. PDF anotēšana vēl nav ieslēgta.")

if st.button("1) Nolasīt 01_Input, 03_Memory un 04_Prompt"):
    try:
        service = get_drive_service()
        st.session_state.disciplines_df = get_discipline_folders(service, input_folder_id)
        mem_catalog, req_df, facts_df = load_memory_json_files(service, memory_folder_id)
        audit_examples_catalog, audit_examples_df = load_audit_examples_json_files(service, memory_folder_id)
        prompt_catalog, universal_prompt, error_examples_text = load_prompt_assets(service, prompt_folder_id)
        st.session_state.memory_catalog_df = mem_catalog
        st.session_state.requirements_df = req_df
        st.session_state.memory_facts_df = facts_df
        st.session_state.audit_examples_catalog_df = audit_examples_catalog
        st.session_state.audit_examples_df = audit_examples_df
        st.session_state.prompt_catalog_df = prompt_catalog
        st.session_state.universal_prompt = universal_prompt
        st.session_state.error_examples_text = error_examples_text
        st.success(f"Nolasīts: disciplīnas {len(st.session_state.disciplines_df)}, Design Brief prasības {len(req_df)}, disciplīnu fakti {len(facts_df)}, accepted audit examples {len(audit_examples_df)}, kļūdu piemēru teksta garums {len(error_examples_text)}.")
    except Exception as e:
        st.error("Neizdevās nolasīt sākuma datus.")
        st.exception(e)

if not st.session_state.memory_catalog_df.empty:
    with st.expander("03_Memory katalogs"):
        st.dataframe(st.session_state.memory_catalog_df, use_container_width=True)
if not st.session_state.audit_examples_catalog_df.empty:
    with st.expander("03_Memory / audit_examples katalogs"):
        st.dataframe(st.session_state.audit_examples_catalog_df, use_container_width=True)

if not st.session_state.audit_examples_df.empty:
    with st.expander("Accepted audit examples priekšskatījums"):
        preview_cols = [c for c in ["example_id", "source_discipline", "source_document_role", "audit_scenario", "issue_type", "problem", "good_comment_style", "comparison_basis"] if c in st.session_state.audit_examples_df.columns]
        st.dataframe(st.session_state.audit_examples_df[preview_cols].head(100), use_container_width=True)

if not st.session_state.prompt_catalog_df.empty:
    with st.expander("04_Prompt katalogs"):
        st.dataframe(st.session_state.prompt_catalog_df, use_container_width=True)

# Disciplīnas izvēle
if not st.session_state.disciplines_df.empty:
    st.markdown("## 2. Izvēlies auditējamo disciplīnu")
    st.dataframe(st.session_state.disciplines_df, use_container_width=True)
    folder_options = st.session_state.disciplines_df["folder_name"].tolist()
    default_index = folder_options.index("09_UKT") if "09_UKT" in folder_options else 0
    selected_folder_name = st.selectbox("Disciplīnas mape", options=folder_options, index=default_index)
    selected_row = st.session_state.disciplines_df[st.session_state.disciplines_df["folder_name"] == selected_folder_name].iloc[0]
    selected_discipline_code = selected_row["discipline_code"]
    selected_folder_id = selected_row["folder_id"]
    st.write("Izvēlētā disciplīna:", selected_discipline_code)

    if st.button("2) Atrast disciplīnas PDF failus"):
        try:
            service = get_drive_service()
            pdfs_df = get_pdf_documents_in_discipline(service, selected_folder_id, selected_folder_name)
            st.session_state.discipline_pdfs_df = pdfs_df
            if pdfs_df.empty:
                st.warning("Disciplīnā nav atrasti PDF faili.")
            else:
                st.success(f"Atrasti {len(pdfs_df)} PDF faili.")
        except Exception as e:
            st.error("Neizdevās atrast PDF failus.")
            st.exception(e)

# PDF izvēle
pdfs_df = st.session_state.discipline_pdfs_df
if not pdfs_df.empty:
    st.markdown("## 3. Disciplīnas PDF faili")
    st.dataframe(pdfs_df[["name", "path", "document_type", "size", "modifiedTime"]], use_container_width=True)
    preferred_types = ["explanatory_note", "specification", "general_data", "drawing"]
    default_paths = []
    for dtype in preferred_types:
        matches = pdfs_df[pdfs_df["document_type"] == dtype]["path"].tolist()
        default_paths.extend(matches[:2 if dtype == "drawing" else 1])
    default_paths = list(dict.fromkeys(default_paths))[:6]
    selected_paths = st.multiselect("Izvēlies PDF failus auditam", options=pdfs_df["path"].tolist(), default=default_paths)
    selected_docs_df = pdfs_df[pdfs_df["path"].isin(selected_paths)].copy()
    st.session_state.selected_docs_df = selected_docs_df
    st.markdown("### Auditam izvēlētie faili")
    st.dataframe(selected_docs_df[["name", "path", "document_type", "size"]], use_container_width=True)

    if st.button("3) Izvilkt PDF teksta blokus"):
        try:
            service = get_drive_service()
            all_blocks = []
            summaries = []
            for _, doc_row in selected_docs_df.iterrows():
                file_name = doc_row["name"]
                file_id = doc_row["id"]
                pdf_bytes = download_drive_file_bytes(service, file_id)
                bdf, total_pages = extract_pdf_page_blocks(pdf_bytes, int(max_pages_per_pdf))
                if not bdf.empty:
                    bdf["source_file"] = file_name
                    bdf["drive_file_id"] = file_id
                    bdf["drive_path"] = doc_row["path"]
                    bdf["document_type"] = doc_row["document_type"]
                    bdf["discipline"] = get_discipline_code_from_folder_name(selected_folder_name)
                    all_blocks.append(bdf)
                summaries.append({
                    "source_file": file_name,
                    "drive_file_id": file_id,
                    "drive_path": doc_row["path"],
                    "document_type": doc_row["document_type"],
                    "total_pages": total_pages,
                    "processed_pages": min(total_pages, int(max_pages_per_pdf)),
                    "text_blocks": len(bdf),
                })
            combined = pd.concat(all_blocks, ignore_index=True) if all_blocks else pd.DataFrame()
            st.session_state.blocks_df = combined
            st.session_state.file_summary_df = pd.DataFrame(summaries)
            st.success(f"Izvilkti {len(combined)} teksta bloki no {len(summaries)} PDF failiem.")
        except Exception as e:
            st.error("Neizdevās izvilkt PDF teksta blokus.")
            st.exception(e)

# Audits
blocks_df = st.session_state.blocks_df
if not blocks_df.empty:
    st.markdown("## 4. PDF teksta bloku indekss")
    st.markdown("### Failu kopsavilkums")
    st.dataframe(st.session_state.file_summary_df, use_container_width=True)
    st.markdown("### Teksta bloku priekšskatījums")
    st.dataframe(blocks_df[["source_file", "document_type", "page", "block_id", "text", "x0", "y0", "x1", "y1"]].head(100), use_container_width=True)

    if st.button("4) Palaist universālo auditu v3.3"):
        try:
            client = get_openai_client()
            discipline_code = blocks_df["discipline"].iloc[0]
            base_examples_text = st.session_state.error_examples_text or st.session_state.universal_prompt or ""
            error_examples_text = enrich_calibration_text(
                base_examples_text,
                st.session_state.audit_examples_df,
                discipline_code,
            )
            source_lookup = blocks_df[["source_file", "page", "block_id", "text", "x0", "y0", "x1", "y1"]].copy()
            all_raw_issues: List[Dict[str, Any]] = []
            all_temp_facts: List[Dict[str, Any]] = []
            raw_ai_log: List[Dict[str, Any]] = []

            st.markdown("## 5. Audita izpilde")
            if not st.session_state.audit_examples_df.empty:
                st.caption(f"v3.3.2: audita scenāriju bibliotēkā ielādēti {len(st.session_state.audit_examples_df)} accepted audit examples.")
            st.markdown("### 0. Pagaidu faktu indekss")
            fact_batches = make_block_batches(blocks_df, int(max_blocks_per_ai))
            fact_progress = st.progress(0)
            for i, batch_df in enumerate(fact_batches, start=1):
                st.write(f"Faktu indekss: batch {i}/{len(fact_batches)}")
                text_blocks = build_text_from_blocks(batch_df, int(max_blocks_per_ai))
                prompt = build_fact_prompt(project_code, discipline_code, text_blocks, f"batch {i}", error_examples_text)
                facts, raw = call_ai_json_array(client, model, "Tu atbildi tikai derīgā JSON masīvā. Bez paskaidrojumiem.", prompt, 0.0)
                for j, fact in enumerate(facts, start=1):
                    fact = dict(fact)
                    fact["temp_fact_id"] = fact.get("fact_id") or f"TEMP-{i:03d}-{j:03d}"
                    fact["project_code"] = project_code
                    fact["discipline"] = discipline_code
                    fact["batch_index"] = i
                    all_temp_facts.append(fact)
                raw_ai_log.append({"module": "facts", "batch": i, "raw_length": len(raw), "parsed_count": len(facts)})
                fact_progress.progress(i / len(fact_batches))
                if delay_between_ai_calls:
                    time.sleep(float(delay_between_ai_calls))

            temp_facts_df = pd.DataFrame(all_temp_facts)
            st.session_state.temp_facts_df = temp_facts_df
            st.success(f"Pagaidu faktu indeksā iegūti {len(temp_facts_df)} fakti.")
            temp_facts_text = df_to_compact_text(temp_facts_df, 350, ["temp_fact_id", "fact_type", "element", "parameter_name", "parameter_value", "unit", "source_file", "page", "block_id", "source_text", "confidence"])

            if run_module_a:
                st.markdown("### A. Design Brief conflict")
                relevant_req = select_relevant_requirements(st.session_state.requirements_df, discipline_code, int(max_design_requirements))
                req_text = df_to_compact_text(relevant_req, int(max_design_requirements), ["memory_id", "engineering_system", "discipline", "applies_to_sections", "requirement", "condition", "source_file", "page", "priority"])
                batches = make_block_batches(blocks_df, int(max_blocks_per_ai))
                prog = st.progress(0)
                for i, batch_df in enumerate(batches, start=1):
                    st.write(f"Design Brief audits: batch {i}/{len(batches)}")
                    prompt = build_design_brief_prompt(req_text, build_text_from_blocks(batch_df, int(max_blocks_per_ai)), audit_depth, confidence_threshold)
                    issues, raw = call_ai_json_array(client, model, "Tu atbildi tikai JSON masīvā.", prompt, 0.0)
                    all_raw_issues.extend([normalize_issue(x, "design_brief_conflict", source_lookup) for x in issues])
                    raw_ai_log.append({"module": "A", "batch": i, "raw_length": len(raw), "parsed_count": len(issues)})
                    prog.progress(i / len(batches))
                    if delay_between_ai_calls:
                        time.sleep(float(delay_between_ai_calls))

            if run_module_b:
                st.markdown("### B. Single document consistency")
                for doc_index, (source_file, doc_blocks) in enumerate(blocks_df.groupby("source_file"), start=1):
                    document_type = str(doc_blocks["document_type"].iloc[0])
                    st.write(f"Viena dokumenta audits: {source_file} ({doc_index}/{blocks_df['source_file'].nunique()})")
                    doc_facts = temp_facts_df[temp_facts_df["source_file"].astype(str) == str(source_file)].copy() if not temp_facts_df.empty and "source_file" in temp_facts_df.columns else pd.DataFrame()
                    facts_text = df_to_compact_text(doc_facts, 220, ["temp_fact_id", "fact_type", "element", "parameter_name", "parameter_value", "unit", "page", "block_id", "source_text", "confidence"])
                    for i, batch_df in enumerate(make_block_batches(doc_blocks, int(max_blocks_per_ai)), start=1):
                        prompt = build_single_doc_prompt(source_file, document_type, build_text_from_blocks(batch_df, int(max_blocks_per_ai)), facts_text, error_examples_text, audit_depth, confidence_threshold)
                        issues, raw = call_ai_json_array(client, model, "Tu atbildi tikai JSON masīvā.", prompt, 0.0)
                        all_raw_issues.extend([normalize_issue(x, "single_document_consistency", source_lookup) for x in issues])
                        raw_ai_log.append({"module": "B", "source_file": source_file, "batch": i, "raw_length": len(raw), "parsed_count": len(issues)})
                        if delay_between_ai_calls:
                            time.sleep(float(delay_between_ai_calls))

            if run_module_c:
                st.markdown("### C. Discipline consistency — strukturēti soļi")
                orientation = get_orientation_blocks(blocks_df)
                drawings = get_blocks_by_type(blocks_df, ["drawing"])
                specs = get_blocks_by_type(blocks_df, ["specification"])
                general = get_blocks_by_type(blocks_df, ["general_data"])
                c_steps = []
                if not orientation.empty and not drawings.empty:
                    c_steps.append({"step_id": "C1", "label": "explanatory_or_general_vs_drawings", "reference_label": "skaidrojošais apraksts / vispārīgie dati", "reference_blocks": orientation, "target_label": "rasējumi", "target_blocks": drawings})
                if not orientation.empty and not specs.empty:
                    c_steps.append({"step_id": "C2", "label": "explanatory_or_general_vs_specification", "reference_label": "skaidrojošais apraksts / vispārīgie dati", "reference_blocks": orientation, "target_label": "specifikācija", "target_blocks": specs})
                if not drawings.empty and not specs.empty:
                    c_steps.append({"step_id": "C3a", "label": "drawings_vs_specification", "reference_label": "rasējumi", "reference_blocks": drawings, "target_label": "specifikācija", "target_blocks": specs})
                    c_steps.append({"step_id": "C3b", "label": "specification_vs_drawings", "reference_label": "specifikācija", "reference_blocks": specs, "target_label": "rasējumi", "target_blocks": drawings})
                if not general.empty and not general.equals(orientation):
                    other = blocks_df[blocks_df["document_type"] != "general_data"].copy()
                    if not other.empty:
                        c_steps.append({"step_id": "C4", "label": "general_data_vs_other_documents", "reference_label": "vispārīgie dati", "reference_blocks": general, "target_label": "pārējie disciplīnas dokumenti", "target_blocks": other})
                total_c = sum(len(make_block_batches(s["target_blocks"], int(max_blocks_per_ai))) for s in c_steps)
                done = 0
                prog = st.progress(0)
                for step in c_steps:
                    st.write(f"{step['step_id']}: {step['label']}")
                    reference_text = build_context_text(blocks_df, step["reference_blocks"].head(int(max_blocks_per_ai)), int(max_blocks_per_ai), 2)
                    for i, target_batch in enumerate(make_block_batches(step["target_blocks"], int(max_blocks_per_ai)), start=1):
                        target_text = build_context_text(blocks_df, target_batch, int(max_blocks_per_ai), 2)
                        prompt = build_structured_discipline_pair_prompt(discipline_code, f"{step['step_id']} {step['label']} batch {i}", step["reference_label"], reference_text, step["target_label"], target_text, temp_facts_text, error_examples_text, audit_depth, confidence_threshold)
                        issues, raw = call_ai_json_array(client, model, "Tu atbildi tikai JSON masīvā.", prompt, 0.0)
                        for x in issues:
                            norm = normalize_issue(x, "discipline_consistency", source_lookup)
                            norm["comparison_step"] = step["step_id"]
                            norm["comparison_label"] = step["label"]
                            all_raw_issues.append(norm)
                        raw_ai_log.append({"module": "C", "step": step["step_id"], "label": step["label"], "batch": i, "raw_length": len(raw), "parsed_count": len(issues)})
                        done += 1
                        prog.progress(done / max(total_c, 1))
                        if delay_between_ai_calls:
                            time.sleep(float(delay_between_ai_calls))

            if run_module_d:
                st.markdown("### D. Starpdisciplīnu konsekvence")
                prior = select_prior_facts(st.session_state.memory_facts_df, discipline_code, int(max_memory_facts))
                if prior.empty:
                    st.info("03_Memory nav citu disciplīnu faktu, pret ko salīdzināt.")
                else:
                    prior_text = df_to_compact_text(prior, int(max_memory_facts), ["memory_id", "fact_id", "discipline", "fact_type", "element", "parameter_name", "parameter_value", "unit", "source_file", "page", "block_id", "source_text"])
                    batches = make_block_batches(blocks_df, int(max_blocks_per_ai))
                    prog = st.progress(0)
                    for i, batch_df in enumerate(batches, start=1):
                        st.write(f"Starpdisciplīnu audits: batch {i}/{len(batches)}")
                        prompt = build_interdisciplinary_prompt(discipline_code, prior_text, temp_facts_text, build_text_from_blocks(batch_df, int(max_blocks_per_ai)), audit_depth, confidence_threshold)
                        issues, raw = call_ai_json_array(client, model, "Tu atbildi tikai JSON masīvā.", prompt, 0.0)
                        all_raw_issues.extend([normalize_issue(x, "interdisciplinary_consistency", source_lookup) for x in issues])
                        raw_ai_log.append({"module": "D", "batch": i, "raw_length": len(raw), "parsed_count": len(issues)})
                        prog.progress(i / len(batches))
                        if delay_between_ai_calls:
                            time.sleep(float(delay_between_ai_calls))

            raw_df = pd.DataFrame(all_raw_issues)
            if not raw_df.empty:
                if "issue_id" not in raw_df.columns:
                    raw_df["issue_id"] = ""
                raw_df["issue_id"] = [v if str(v).strip() else f"{project_code}-{discipline_code}-ISSUE-{i+1:04d}" for i, v in enumerate(raw_df["issue_id"].tolist())]
            filtered_df, removed_df = filter_issues(raw_df, float(confidence_threshold), audit_depth)
            st.session_state.issues_raw_df = raw_df
            st.session_state.issues_filtered_df = filtered_df
            st.session_state.issues_removed_df = removed_df
            st.session_state.raw_ai_log_df = pd.DataFrame(raw_ai_log)
            st.success(f"AI raw kandidāti: {len(raw_df)}; pēc filtra redzamie: {len(filtered_df)}; izfiltrēti: {len(removed_df)}.")
        except Exception as e:
            st.error("Kļūda universālā audita izpildē.")
            st.exception(e)

# Rezultāti
raw_df = st.session_state.issues_raw_df
filtered_df = st.session_state.issues_filtered_df
removed_df = st.session_state.issues_removed_df
temp_facts_df = st.session_state.temp_facts_df

if not temp_facts_df.empty or not raw_df.empty:
    st.markdown("## 6. Diagnostika un rezultāti")
    if not st.session_state.raw_ai_log_df.empty:
        st.markdown("### AI pieprasījumu diagnostika")
        st.dataframe(st.session_state.raw_ai_log_df, use_container_width=True)
    if not temp_facts_df.empty:
        st.markdown("### Pagaidu faktu indekss")
        if "fact_type" in temp_facts_df.columns:
            st.dataframe(temp_facts_df.groupby("fact_type").size().reset_index(name="count").sort_values("count", ascending=False), use_container_width=True)
        st.dataframe(temp_facts_df.head(300), use_container_width=True)
    st.markdown("### Issues kopsavilkums")
    c1, c2, c3 = st.columns(3)
    c1.metric("Raw kandidāti", len(raw_df))
    c2.metric("Redzamie pēc filtra", len(filtered_df))
    c3.metric("Izfiltrēti", len(removed_df))
    if not filtered_df.empty:
        st.markdown("### Redzamās kandidātpiezīmes")
        if "audit_mode" in filtered_df.columns:
            st.dataframe(filtered_df.groupby("audit_mode").size().reset_index(name="count"), use_container_width=True)
        if "issue_type" in filtered_df.columns:
            st.dataframe(filtered_df.groupby("issue_type").size().reset_index(name="count"), use_container_width=True)
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
    json_bytes = make_json_bytes(filtered_df, raw_df, removed_df, temp_facts_df, project_code, blocks_df["discipline"].iloc[0] if not blocks_df.empty else "")
    d1, d2 = st.columns(2)
    with d1:
        st.download_button("Lejupielādēt universal audit Excel", data=excel_bytes, file_name=f"{project_code.lower().replace('-', '_')}_universal_audit_v3_3.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    with d2:
        st.download_button("Lejupielādēt universal audit JSON", data=json_bytes, file_name=f"{project_code.lower().replace('-', '_')}_universal_audit_v3_3.json", mime="application/json")
