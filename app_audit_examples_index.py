# app_audit_examples_index.py
# ------------------------------------------------------------
# BP audit_examples indeksētājs v1.2
#
# Mērķis:
# - nolasa 03_Memory/audit_examples mapē esošos 16 kolonnu audit_examples Excel failus;
# - apvieno vienā indeksā;
# - pārbauda struktūru un datu kvalitāti;
# - automātiski piedāvā kļūdu ģimeni un scenāriju;
# - izveido review_needed lapu klasifikācijas pārskatīšanai;
# - ļauj lejupielādēt audit_examples_index.xlsx.
#
# Streamlit secrets piemērs:
# [google_service_account]
# type = "service_account"
# project_id = "..."
# private_key_id = "..."
# private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
# client_email = "..."
# client_id = "..."
# auth_uri = "https://accounts.google.com/o/oauth2/auth"
# token_uri = "https://oauth2.googleapis.com/token"
# auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
# client_x509_cert_url = "..."
#
# [app]
# memory_folder_id = "GOOGLE_DRIVE_03_MEMORY_FOLDER_ID"
# ------------------------------------------------------------

from __future__ import annotations

import io
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload


APP_TITLE = "BP audit_examples indeksētājs v1.2"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

REQUIRED_COLUMNS = [
    "note_id",
    "Nr",
    "discipline",
    "target_file",
    "target_page",
    "target_area",
    "target_text",
    "comment_text",
    "issue_type",
    "severity",
    "comparison_files",
    "comparison_pages",
    "comparison_evidence",
    "markup_type",
    "placement_confidence",
    "status",
]

CORE_REQUIRED_FOR_MARKUP = [
    "note_id",
    "discipline",
    "target_file",
    "target_page",
    "target_text",
    "comment_text",
    "markup_type",
    "placement_confidence",
    "status",
]

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass
class DriveItem:
    id: str
    name: str
    mimeType: str
    path: str
    parent_id: Optional[str] = None
    modifiedTime: Optional[str] = None
    size: Optional[str] = None


# -----------------------------
# Google Drive helpers
# -----------------------------

@st.cache_resource(show_spinner=False)
def get_drive_service():
    """
    Atbalsta abus secrets pierakstus:
    1) esošais projekta pieraksts: GOOGLE_SERVICE_ACCOUNT_JSON = '{...}'
    2) TOML tabula: [google_service_account] / [gcp_service_account] / [service_account]
    """
    sa_info = None

    service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", None)
    if service_account_json:
        try:
            sa_info = json.loads(service_account_json)
        except Exception as e:
            st.error(f"GOOGLE_SERVICE_ACCOUNT_JSON nav derīgs JSON: {e}")
            st.stop()

    if sa_info is None:
        if "google_service_account" in st.secrets:
            sa_info = dict(st.secrets["google_service_account"])
        elif "gcp_service_account" in st.secrets:
            sa_info = dict(st.secrets["gcp_service_account"])
        elif "service_account" in st.secrets:
            sa_info = dict(st.secrets["service_account"])

    if not sa_info:
        st.error(
            "Nav atrasti Google service account dati Streamlit secrets. "
            "Šajā projektā parasti jābūt GOOGLE_SERVICE_ACCOUNT_JSON. "
            "Alternatīvi var izmantot [google_service_account] TOML tabulu."
        )
        st.stop()

    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_list_children(service, folder_id: str) -> List[DriveItem]:
    items: List[DriveItem] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        resp = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                pageSize=1000,
            )
            .execute()
        )
        for f in resp.get("files", []):
            items.append(
                DriveItem(
                    id=f["id"],
                    name=f["name"],
                    mimeType=f["mimeType"],
                    path=f["name"],
                    parent_id=folder_id,
                    modifiedTime=f.get("modifiedTime"),
                    size=f.get("size"),
                )
            )
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def find_child_folder(service, parent_id: str, folder_name: str) -> Optional[DriveItem]:
    for item in drive_list_children(service, parent_id):
        if item.mimeType == MIME_FOLDER and item.name == folder_name:
            return item
    return None


def recursively_list_excel_files(service, root_id: str, root_path: str = "audit_examples") -> List[DriveItem]:
    found: List[DriveItem] = []

    def walk(folder_id: str, current_path: str):
        children = drive_list_children(service, folder_id)
        for item in children:
            item.path = f"{current_path}/{item.name}"
            if item.mimeType == MIME_FOLDER:
                walk(item.id, item.path)
            else:
                if item.name.startswith("~$"):
                    continue
                if item.name.lower().endswith(".xlsx") or item.mimeType == MIME_XLSX:
                    found.append(item)

    walk(root_id, root_path)
    return found


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


# -----------------------------
# Excel parsing
# -----------------------------

def clean_string(v: Any) -> str:
    if pd.isna(v):
        return ""
    return str(v).strip()


def canonicalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    # Noņem liekās unnamed kolonnas.
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df[[c for c in df.columns if not c.lower().startswith("unnamed")]]
    return df


def detect_examples_sheet(xls: pd.ExcelFile) -> Optional[str]:
    # Prioritāte lapām, kas satur note_id un target_file.
    for sheet in xls.sheet_names:
        try:
            preview = pd.read_excel(xls, sheet_name=sheet, nrows=5)
            preview = canonicalize_columns(preview)
            cols = set(preview.columns)
            if "target_file" in cols and ("note_id" in cols or "comment_text" in cols):
                return sheet
        except Exception:
            continue
    return xls.sheet_names[0] if xls.sheet_names else None


def read_audit_examples_from_xlsx(file_bytes: bytes, source_item: DriveItem) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "source_excel": source_item.name,
        "source_path": source_item.path,
        "source_file_id": source_item.id,
        "read_ok": False,
        "sheet_name": None,
        "error": "",
    }

    try:
        xls = pd.ExcelFile(io.BytesIO(file_bytes), engine="openpyxl")
        sheet = detect_examples_sheet(xls)
        meta["sheet_name"] = sheet
        if not sheet:
            meta["error"] = "Excel failā nav lapu"
            return pd.DataFrame(), meta

        df = pd.read_excel(xls, sheet_name=sheet)
        df = canonicalize_columns(df)

        # Ignorē pilnīgi tukšas rindas.
        if not df.empty:
            df = df.dropna(how="all")
            # Ignorē rindas, kur visi svarīgie lauki ir tukši.
            likely_cols = [c for c in ["note_id", "target_file", "target_text", "comment_text"] if c in df.columns]
            if likely_cols:
                mask_any = df[likely_cols].apply(lambda r: any(clean_string(v) for v in r), axis=1)
                df = df.loc[mask_any].copy()

        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                df[col] = ""

        # Paturam arī papildkolonnas, bet sākumā sakārtojam 16 kolonnas.
        extra_cols = [c for c in df.columns if c not in REQUIRED_COLUMNS]
        df = df[REQUIRED_COLUMNS + extra_cols]

        df["source_excel"] = source_item.name
        df["source_path"] = source_item.path
        df["source_file_id"] = source_item.id
        df["source_sheet"] = sheet
        df["source_modifiedTime"] = source_item.modifiedTime or ""
        df["source_size"] = source_item.size or ""

        meta["read_ok"] = True
        meta["rows"] = len(df)
        return df, meta

    except Exception as e:
        meta["error"] = repr(e)
        return pd.DataFrame(), meta


# -----------------------------
# Normalization / families
# -----------------------------

def text_blob(row: pd.Series) -> str:
    """
    Klasifikācijai apzināti NEizmantojam note_id un target_file.
    Iepriekšējā versijā gandrīz katrā target_file bija C2-03, tāpēc liela daļa
    piezīmju kļūdaini iekrita dokumenta identitātes / projekta koda ģimenē.
    Kļūdu ģimeni drīkst noteikt pēc piezīmes satura, nevis tikai pēc faila nosaukuma.
    """
    fields = [
        "target_area",
        "target_text",
        "comment_text",
        "issue_type",
        "comparison_evidence",
        "comparison_files",
        "comparison_pages",
    ]
    return " ".join(clean_string(row.get(f, "")) for f in fields).lower()


def identity_blob(row: pd.Series) -> str:
    """Šo izmantojam tikai dokumenta identitātes pazīmju pārbaudei."""
    fields = [
        "target_area",
        "target_text",
        "comment_text",
        "issue_type",
        "comparison_evidence",
        "comparison_pages",
    ]
    return " ".join(clean_string(row.get(f, "")) for f in fields).lower()


def infer_document_role(row: pd.Series) -> str:
    s = (clean_string(row.get("target_file", "")) + " " + clean_string(row.get("source_path", ""))).lower()
    if any(x in s for x in ["td_", "skaidrojo", "explanatory", "aprakst"]):
        return "explanatory_note"
    if any(x in s for x in ["general data", "visp", "ra_11100", "ra_10001", "general_data"]):
        return "general_data"
    if any(x in s for x in ["site plan", "ģenerālpl", "general plan", "ra_11101"]):
        return "site_plan"
    if any(x in s for x in ["profile", "garenprofil", "ra_11201"]):
        return "profile"
    if any(x in s for x in ["specification", "specifik", "ms_"]):
        return "specification"
    if any(x in s for x in ["section", "griez", "floor", "plan", "scheme", "shēma", "isometry"]):
        return "drawing"
    return "unknown"


def infer_family_and_scenario(row: pd.Series) -> Tuple[str, str, str]:
    s = text_blob(row)
    issue = clean_string(row.get("issue_type", "")).lower()

    # A. teksts / gramatika / terminoloģija
    if any(k in s for k in ["drukas", "gramatik", "valodas kļ", "pārrakst", "typo", "spelling", "termin", "mērvien", "mpa", "centralizētāj", "excretion"]):
        if any(k in s for k in ["excretion", "translation", "tulkoj", "angļu", "english"]):
            return "A_text_language", "SC-A02_wrong_technical_translation", "Teksta / valodas / tehniskā termina kļūda"
        if any(k in s for k in ["mērvien", "mpa", "l/s", "bar", "mm", "m³", "m3"]):
            return "A_text_language", "SC-A03_unit_or_symbol_error", "Mērvienības vai simbola pieraksta kļūda"
        return "A_text_language", "SC-A01_spelling_or_wording_error", "Drukas, locījuma vai formulējuma kļūda"

    # B. LV/EN
    if any(k in s for k in ["lv/en", "latvie", "angļu", "english", "tulkoj", "translation", "en text", "lv text"]):
        if any(k in s for k in ["2.2", "2,8", "skait", "vērtīb", "numeric", "daudzum", "diametr"]):
            return "B_lv_en", "SC-B01_lv_en_numeric_mismatch", "LV/EN skaitliska vai parametra neatbilstība"
        return "B_lv_en", "SC-B02_lv_en_term_or_title_mismatch", "LV/EN termina vai nosaukuma neatbilstība"

    # C. datumi / versijas
    if any(k in s for k in ["datums", "date", "revīz", "revision", "versij", "aktuāl"]):
        return "C_dates_versions", "SC-C01_date_or_revision_mismatch", "Datumu, versiju vai revīziju neatbilstība"

    # D. dokumenta identitāte
    # Piezīme: neklasificējam par D tikai tāpēc, ka target_file satur C2-03.
    id_s = identity_blob(row)
    has_identity_words = any(k in id_s for k in [
        "faila nosauk", "titullauk", "titullapa", "rasējuma num", "drawing number",
        "sheet id", "document number", "dokumenta num", "document identity",
        "projekta kod", "project code", "veca projekta", "nepareizs projekts"
    ])
    has_project_code_context = any(k in id_s for k in ["c2-02", "c2-03", "projekta kod", "project code", "veca projekta"])
    if has_identity_words:
        if has_project_code_context:
            return "D_document_identity", "SC-D02_wrong_project_code", "Nepareizs projekta kods vai veca projekta atsauce"
        return "D_document_identity", "SC-D01_file_title_block_mismatch", "Faila, titullauka vai dokumenta identitātes neatbilstība"

    # E. rasējumu saraksti / atsauces
    if any(k in s for k in ["rasējumu sarak", "drawing list", "atsauce", "reference", "neeksist", "nav atrodams", "sarakstā"]):
        return "E_drawing_list_references", "SC-E01_drawing_list_or_reference_mismatch", "Rasējumu saraksta vai savstarpējas atsauces neatbilstība"

    # F. normatīvi
    if any(k in s for k in ["lbn", "mk nr", "normat", "standart", "lvs", "en 805", "atsauci"]):
        return "F_normative_references", "SC-F01_normative_reference_mismatch", "Normatīvu vai standartu atsauču neatbilstība"

    # G. materiāli / tipi / markas / modeļi
    if any(k in s for k in ["materi", "model", "ražot", "manufacturer", "tips", "type", "marka", "sn8", "pe100", "pp", "slodzes klase", "diametr", "dn", "d110", "d160", "od110", "armatūra", "equipment"]):
        if any(k in s for k in ["diametr", "dn", "d110", "d160", "od110", "ø"]):
            return "G_material_type_model", "SC-G01_diameter_or_parameter_mismatch", "Diametra vai tehniska parametra neatbilstība"
        return "G_material_type_model", "SC-G02_material_type_model_mismatch", "Materiāla, tipa, markas vai modeļa neatbilstība"

    # H. daudzumi / pozīcijas
    if any(k in s for k in ["daudzum", "quantity", "pozīc", "position", "tukša rinda", "kpl", "set", "numerāc"]):
        return "H_quantity_position", "SC-H01_quantity_or_position_mismatch", "Daudzuma, pozīcijas vai numerācijas neatbilstība"

    # I. specifikācijas trūkumi
    if any(k in s for k in ["nav iekļauts", "nav iekļauti", "nav specifik", "trūkst specifik", "missing_from_specification", "papildināt specifik"]):
        return "I_specification_coverage", "SC-I01_missing_from_specification", "Risinājums/elements nav iekļauts specifikācijā"

    # J. izsekojamība
    if any(k in s for k in ["nav izsekoj", "not traceable", "nesasaist", "saskaņot", "starp", "pret", "profile", "site plan"]):
        return "J_traceability", "SC-J01_not_traceable_between_documents", "Izsekojamības problēma starp dokumentiem"

    # K. grafika / salasāmība
    if any(k in s for k in ["neskaidr", "pārklāj", "salasām", "grafisk", "leģend", "manual_placement_required"]):
        return "K_graphical_clarity", "SC-K01_graphical_or_placement_clarity", "Grafiska vai izvietojuma skaidrības problēma"

    # fallback pēc issue_type
    if "missing" in issue and "spec" in issue:
        return "I_specification_coverage", "SC-I01_missing_from_specification", "Risinājums/elements nav iekļauts specifikācijā"
    if "lv" in issue and "en" in issue:
        return "B_lv_en", "SC-B02_lv_en_term_or_title_mismatch", "LV/EN termina vai nosaukuma neatbilstība"
    if "version" in issue or "date" in issue:
        return "C_dates_versions", "SC-C01_date_or_revision_mismatch", "Datumu, versiju vai revīziju neatbilstība"
    if "drawing" in issue or "identity" in issue:
        return "D_document_identity", "SC-D01_file_title_block_mismatch", "Faila, titullauka vai dokumenta identitātes neatbilstība"
    if "quantity" in issue:
        return "H_quantity_position", "SC-H01_quantity_or_position_mismatch", "Daudzuma, pozīcijas vai numerācijas neatbilstība"
    if "material" in issue or "type" in issue or "model" in issue:
        return "G_material_type_model", "SC-G02_material_type_model_mismatch", "Materiāla, tipa, markas vai modeļa neatbilstība"

    return "Z_unclassified", "SC-Z99_unclassified", "Neklasificēts / jāpārskata"


def infer_discipline_from_path_or_row(row: pd.Series) -> str:
    disc = clean_string(row.get("discipline", ""))
    if disc:
        return disc
    path = clean_string(row.get("source_path", ""))
    m = re.search(r"audit_examples/([^/]+)/", path)
    if m:
        return m.group(1)
    return ""


def derive_group_subgroup_from_path(path: str) -> Tuple[str, str]:
    parts = path.split("/")
    # audit_examples/18_UK/UK/file.xlsx
    group = ""
    subgroup = ""
    try:
        idx = parts.index("audit_examples")
        if len(parts) > idx + 1:
            group = parts[idx + 1]
        if len(parts) > idx + 2 and not parts[idx + 2].lower().endswith(".xlsx"):
            subgroup = parts[idx + 2]
    except ValueError:
        pass
    return group, subgroup


def enrich_index(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["discipline_final"] = out.apply(infer_discipline_from_path_or_row, axis=1)
    out[["audit_examples_group", "audit_examples_subgroup"]] = out["source_path"].apply(
        lambda p: pd.Series(derive_group_subgroup_from_path(clean_string(p)))
    )
    out["document_role"] = out.apply(infer_document_role, axis=1)
    fams = out.apply(infer_family_and_scenario, axis=1)
    out["normalized_family"] = [x[0] for x in fams]
    out["normalized_scenario"] = [x[1] for x in fams]
    out["scenario_label"] = [x[2] for x in fams]

    # Kolonnas manuālai klasifikācijas labošanai eksportētajā indeksā.
    out["review_status"] = ""
    out["corrected_family"] = ""
    out["corrected_scenario"] = ""
    out["review_comment"] = ""

    out["has_manual_placement"] = out["target_text"].astype(str).str.contains("MANUAL_PLACEMENT_REQUIRED", case=False, na=False)
    out["has_exact_highlight"] = (
        out["markup_type"].astype(str).str.lower().eq("highlight")
        & out["placement_confidence"].astype(str).str.lower().eq("exact")
        & ~out["has_manual_placement"]
    )
    out["target_text_len"] = out["target_text"].fillna("").astype(str).str.len()
    out["comment_len"] = out["comment_text"].fillna("").astype(str).str.len()
    return out


# -----------------------------
# Quality checks and summaries
# -----------------------------

def build_file_catalog(items: List[DriveItem], metas: List[Dict[str, Any]]) -> pd.DataFrame:
    meta_by_id = {m.get("source_file_id"): m for m in metas}
    rows = []
    for item in items:
        m = meta_by_id.get(item.id, {})
        group, subgroup = derive_group_subgroup_from_path(item.path)
        rows.append(
            {
                "name": item.name,
                "path": item.path,
                "id": item.id,
                "group": group,
                "subgroup": subgroup,
                "modifiedTime": item.modifiedTime,
                "size": item.size,
                "read_ok": m.get("read_ok"),
                "rows": m.get("rows", 0),
                "sheet_name": m.get("sheet_name", ""),
                "error": m.get("error", ""),
            }
        )
    return pd.DataFrame(rows)


def build_quality_check(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if df.empty:
        return pd.DataFrame()

    for idx, row in df.iterrows():
        problems = []
        for col in REQUIRED_COLUMNS:
            if col not in df.columns:
                problems.append(f"missing_column:{col}")
        for col in CORE_REQUIRED_FOR_MARKUP:
            if not clean_string(row.get(col, "")):
                problems.append(f"empty_core:{col}")
        # target_page jābūt skaitlim vai vismaz tekstam, ko var saprast.
        tp = clean_string(row.get("target_page", ""))
        if not tp:
            problems.append("empty_target_page")
        mt = clean_string(row.get("markup_type", "")).lower()
        pc = clean_string(row.get("placement_confidence", "")).lower()
        tt = clean_string(row.get("target_text", ""))
        if mt == "highlight" and pc == "exact" and (not tt or tt.upper() == "MANUAL_PLACEMENT_REQUIRED"):
            problems.append("exact_highlight_without_target_text")
        if clean_string(row.get("note_id", "")) and df["note_id"].astype(str).eq(str(row.get("note_id"))).sum() > 1:
            problems.append("duplicate_note_id")

        if problems:
            rows.append(
                {
                    "row_index": idx,
                    "note_id": row.get("note_id", ""),
                    "source_excel": row.get("source_excel", ""),
                    "source_path": row.get("source_path", ""),
                    "target_file": row.get("target_file", ""),
                    "problems": "; ".join(problems),
                }
            )
    return pd.DataFrame(rows)


def build_review_needed(df: pd.DataFrame, quality_df: pd.DataFrame) -> pd.DataFrame:
    """
    Izveido rindu kopu, ko vajadzētu pārskatīt, pirms no piemēriem būvē scenāriju katalogu.
    Šī nav datu kļūdu lapa vien. Tā palīdz ieraudzīt, kur automātiskā klasifikācija varētu būt pārāk rupja.
    """
    if df.empty:
        return pd.DataFrame()

    review_indices = set()
    reasons: Dict[int, List[str]] = {}

    def add_reason(idx: int, reason: str):
        review_indices.add(idx)
        reasons.setdefault(idx, []).append(reason)

    for idx, row in df.iterrows():
        fam = clean_string(row.get("normalized_family", ""))
        scen = clean_string(row.get("normalized_scenario", ""))
        tt = clean_string(row.get("target_text", ""))
        mt = clean_string(row.get("markup_type", "")).lower()
        pc = clean_string(row.get("placement_confidence", "")).lower()
        blob = text_blob(row)

        if fam == "Z_unclassified":
            add_reason(idx, "unclassified")
        if fam == "D_document_identity" and not any(k in blob for k in ["titullauk", "titullapa", "faila nosauk", "rasējuma num", "project code", "projekta kod", "c2-02"]):
            add_reason(idx, "possible_overclassified_document_identity")
        if mt == "highlight" and pc == "exact" and len(tt) <= 2:
            add_reason(idx, "very_short_exact_target_text")
        if "manual_placement_required" in tt.lower():
            add_reason(idx, "manual_placement")
        if scen.endswith("unclassified"):
            add_reason(idx, "scenario_unclassified")

    if quality_df is not None and not quality_df.empty:
        for _, qrow in quality_df.iterrows():
            try:
                idx = int(qrow.get("row_index"))
                add_reason(idx, "quality_check:" + clean_string(qrow.get("problems", "")))
            except Exception:
                continue

    if not review_indices:
        return pd.DataFrame()

    cols = [
        "note_id", "audit_examples_group", "audit_examples_subgroup", "discipline_final",
        "document_role", "normalized_family", "normalized_scenario", "scenario_label",
        "issue_type", "target_file", "target_page", "target_area", "target_text",
        "comment_text", "comparison_evidence", "source_path",
        "review_status", "corrected_family", "corrected_scenario", "review_comment",
    ]
    cols = [c for c in cols if c in df.columns]
    out = df.loc[sorted(review_indices), cols].copy()
    out.insert(0, "review_reason", ["; ".join(reasons.get(i, [])) for i in sorted(review_indices)])
    return out


def summary_count(df: pd.DataFrame, group_cols: List[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=group_cols + ["count"])
    return (
        df.groupby(group_cols, dropna=False)
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )


def build_scenario_catalog(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    agg = (
        df.groupby(["normalized_family", "normalized_scenario", "scenario_label"], dropna=False)
        .agg(
            count=("note_id", "count"),
            example_note_ids=("note_id", lambda s: ", ".join([str(x) for x in s.dropna().astype(str).head(8)])),
            typical_issue_types=("issue_type", lambda s: ", ".join(sorted(set([str(x) for x in s.dropna().astype(str) if str(x).strip()]))[:10])),
            typical_document_roles=("document_role", lambda s: ", ".join(sorted(set([str(x) for x in s.dropna().astype(str) if str(x).strip()]))[:10])),
            exact_highlight_count=("has_exact_highlight", "sum"),
            manual_placement_count=("has_manual_placement", "sum"),
        )
        .reset_index()
        .sort_values(["normalized_family", "count"], ascending=[True, False])
    )

    # Tukši lauki manuālai papildināšanai.
    agg["required_evidence"] = ""
    agg["target_text_strategy"] = ""
    agg["good_comment_pattern"] = ""
    agg["do_not_report_when"] = ""
    agg["priority_for_api_v1"] = ""
    return agg


def build_family_catalog() -> pd.DataFrame:
    """Darba kļūdu ģimeņu katalogs, ko vēlāk lietos audit copilot."""
    rows = [
        {"family": "A_text_language", "meaning": "Teksta, gramatikas, formulējuma, tehniskā termina un mērvienību kļūdas", "api_v1_priority": "high"},
        {"family": "B_lv_en", "meaning": "Latviešu un angļu teksta tehniskās vai skaitliskās neatbilstības", "api_v1_priority": "high"},
        {"family": "C_dates_versions", "meaning": "Datumu, versiju, revīziju un aktualitātes neatbilstības", "api_v1_priority": "high"},
        {"family": "D_document_identity", "meaning": "Faila nosaukuma, titullauka, rasējuma numura, projekta koda vai dokumenta identitātes problēmas", "api_v1_priority": "high"},
        {"family": "E_drawing_list_references", "meaning": "Rasējumu sarakstu, savstarpējo atsauču un neesošu dokumentu problēmas", "api_v1_priority": "medium"},
        {"family": "F_normative_references", "meaning": "Normatīvu, LBN, standartu un ārējo atsauču neatbilstības", "api_v1_priority": "medium"},
        {"family": "G_material_type_model", "meaning": "Materiālu, tipu, marku, modeļu, diametru un tehnisko parametru neatbilstības", "api_v1_priority": "medium"},
        {"family": "H_quantity_position", "meaning": "Daudzumu, pozīciju, numerācijas un tukšu/neskaidru pozīciju neatbilstības", "api_v1_priority": "medium"},
        {"family": "I_specification_coverage", "meaning": "Risinājumi vai elementi nav iekļauti specifikācijā", "api_v1_priority": "later"},
        {"family": "J_traceability", "meaning": "Izsekojamības problēmas starp SA, plāniem, profiliem, specifikācijām u.c.", "api_v1_priority": "later"},
        {"family": "K_graphical_clarity", "meaning": "Grafiskās salasāmības, izvietojuma un piesaistes skaidrības problēmas", "api_v1_priority": "later"},
        {"family": "Z_unclassified", "meaning": "Automātiski neklasificēts; jāpārskata", "api_v1_priority": "review"},
    ]
    return pd.DataFrame(rows)


def to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            safe_name = sheet_name[:31]
            df.to_excel(writer, index=False, sheet_name=safe_name)
            ws = writer.book[safe_name]
            ws.freeze_panes = "A2"
            # basic widths
            for col_cells in ws.columns:
                header = str(col_cells[0].value) if col_cells[0].value else ""
                width = min(max(len(header) + 2, 12), 60)
                ws.column_dimensions[col_cells[0].column_letter].width = width
    return output.getvalue()


# -----------------------------
# Streamlit UI
# -----------------------------

def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption(
        "Indeksē 03_Memory/audit_examples mapē uzkrātos 16 kolonnu Excel piemērus, "
        "piedāvā kļūdu ģimenes un sagatavo audit_examples_index.xlsx."
    )

    service = get_drive_service()

    default_memory_id = ""
    try:
        default_memory_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID", "")
        if not default_memory_id:
            default_memory_id = st.secrets.get("app", {}).get("memory_folder_id", "")
    except Exception:
        default_memory_id = ""

    with st.expander("1. Konfigurācija", expanded=True):
        memory_folder_id = st.text_input("03_Memory folder ID", value=default_memory_id)
        st.caption("Norādi 03_Memory mapes ID. Rīks pats meklēs apakšmapi audit_examples.")

    if not memory_folder_id:
        st.warning("Norādi 03_Memory folder ID.")
        return

    if st.button("1) Nolasīt audit_examples Excel failus", type="primary"):
        st.session_state.pop("examples_df", None)
        st.session_state.pop("file_catalog_df", None)
        st.session_state.pop("quality_df", None)
        st.session_state.pop("scenario_catalog_df", None)
        st.session_state.pop("excel_bytes", None)

        try:
            audit_examples_folder = find_child_folder(service, memory_folder_id, "audit_examples")
            if audit_examples_folder is None:
                st.error("03_Memory mapē netika atrasta apakšmape audit_examples.")
                return

            with st.spinner("Meklēju Excel failus audit_examples mapē..."):
                excel_items = recursively_list_excel_files(
                    service, audit_examples_folder.id, root_path="audit_examples"
                )

            if not excel_items:
                st.warning("audit_examples mapē netika atrasti .xlsx faili.")
                return

            st.info(f"Atrasti {len(excel_items)} Excel faili. Sāku ielasi...")

            all_frames: List[pd.DataFrame] = []
            metas: List[Dict[str, Any]] = []
            progress = st.progress(0)
            status = st.empty()

            for i, item in enumerate(excel_items, start=1):
                status.write(f"Lasa {i}/{len(excel_items)}: {item.path}")
                try:
                    b = download_drive_file_bytes(service, item.id)
                    df, meta = read_audit_examples_from_xlsx(b, item)
                    metas.append(meta)
                    if not df.empty:
                        all_frames.append(df)
                except Exception as e:
                    metas.append(
                        {
                            "source_excel": item.name,
                            "source_path": item.path,
                            "source_file_id": item.id,
                            "read_ok": False,
                            "error": repr(e),
                        }
                    )
                progress.progress(i / len(excel_items))

            if all_frames:
                examples_df = pd.concat(all_frames, ignore_index=True)
            else:
                examples_df = pd.DataFrame(columns=REQUIRED_COLUMNS)

            examples_df = enrich_index(examples_df)
            file_catalog_df = build_file_catalog(excel_items, metas)
            quality_df = build_quality_check(examples_df)
            scenario_catalog_df = build_scenario_catalog(examples_df)
            review_needed_df = build_review_needed(examples_df, quality_df)
            family_catalog_df = build_family_catalog()

            sheets = {
                "1_examples_index": examples_df,
                "2_family_summary": summary_count(examples_df, ["normalized_family", "scenario_label"]),
                "3_issue_type_summary": summary_count(examples_df, ["issue_type", "normalized_family", "normalized_scenario"]),
                "4_document_role_summary": summary_count(examples_df, ["document_role", "normalized_family"]),
                "5_discipline_summary": summary_count(examples_df, ["audit_examples_group", "audit_examples_subgroup", "discipline_final"]),
                "6_scenario_catalog": scenario_catalog_df,
                "7_family_catalog": family_catalog_df,
                "8_quality_check": quality_df,
                "9_review_needed": review_needed_df,
                "10_file_catalog": file_catalog_df,
            }
            excel_bytes = to_excel_bytes(sheets)

            st.session_state.examples_df = examples_df
            st.session_state.file_catalog_df = file_catalog_df
            st.session_state.quality_df = quality_df
            st.session_state.scenario_catalog_df = scenario_catalog_df
            st.session_state.review_needed_df = review_needed_df
            st.session_state.family_catalog_df = family_catalog_df
            st.session_state.excel_bytes = excel_bytes

            st.success(
                f"Ielasīti {len(excel_items)} Excel faili, kopā {len(examples_df)} piezīmju rindas."
            )

        except HttpError as e:
            st.error(f"Google Drive kļūda: {e}")
        except Exception as e:
            st.error(f"Kļūda: {repr(e)}")

    if "examples_df" not in st.session_state:
        st.stop()

    examples_df: pd.DataFrame = st.session_state.examples_df
    file_catalog_df: pd.DataFrame = st.session_state.file_catalog_df
    quality_df: pd.DataFrame = st.session_state.quality_df
    scenario_catalog_df: pd.DataFrame = st.session_state.scenario_catalog_df
    review_needed_df: pd.DataFrame = st.session_state.get("review_needed_df", pd.DataFrame())
    family_catalog_df: pd.DataFrame = st.session_state.get("family_catalog_df", pd.DataFrame())

    st.header("2. Kopsavilkums")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Piezīmju rindas", len(examples_df))
    c2.metric("Excel faili", len(file_catalog_df))
    c3.metric("Kļūdu ģimenes", examples_df["normalized_family"].nunique() if not examples_df.empty else 0)
    c4.metric("Scenāriji", examples_df["normalized_scenario"].nunique() if not examples_df.empty else 0)
    c5.metric("Pārskatāmās rindas", len(review_needed_df))

    st.subheader("Kļūdu ģimeņu kopsavilkums")
    st.dataframe(summary_count(examples_df, ["normalized_family", "scenario_label"]), use_container_width=True)

    st.subheader("Scenāriju katalogs kandidāts")
    st.dataframe(scenario_catalog_df, use_container_width=True)

    st.subheader("Darba kļūdu ģimeņu katalogs")
    st.dataframe(family_catalog_df, use_container_width=True)

    if not review_needed_df.empty:
        with st.expander("Pārskatāmās rindas klasifikācijas precizēšanai", expanded=True):
            st.info("Šīs rindas nav obligāti kļūdainas. Tās ir rindas, kur automātiskā klasifikācija vai markup dati būtu jāpārskata pirms AI copilot būvēšanas.")
            st.dataframe(review_needed_df, use_container_width=True)

    with st.expander("Piezīmju indekss", expanded=False):
        show_cols = [
            "note_id",
            "audit_examples_group",
            "audit_examples_subgroup",
            "discipline_final",
            "document_role",
            "normalized_family",
            "normalized_scenario",
            "issue_type",
            "target_file",
            "target_page",
            "target_area",
            "target_text",
            "comment_text",
            "source_path",
        ]
        show_cols = [c for c in show_cols if c in examples_df.columns]
        st.dataframe(examples_df[show_cols], use_container_width=True)

    with st.expander("Issue type kopsavilkums", expanded=False):
        st.dataframe(
            summary_count(examples_df, ["issue_type", "normalized_family", "normalized_scenario"]),
            use_container_width=True,
        )

    with st.expander("Dokumentu lomu kopsavilkums", expanded=False):
        st.dataframe(summary_count(examples_df, ["document_role", "normalized_family"]), use_container_width=True)

    with st.expander("Failu katalogs", expanded=False):
        st.dataframe(file_catalog_df, use_container_width=True)

    if not quality_df.empty:
        with st.expander("Kvalitātes problēmas", expanded=True):
            st.warning("Daļai rindu ir struktūras vai markup datu problēmas. Tās jāizskata pirms izmantošanas AI auditam.")
            st.dataframe(quality_df, use_container_width=True)
    else:
        st.success("Kvalitātes pārbaudē būtiskas problēmas nav atrastas.")

    st.header("3. Lejupielāde")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    st.download_button(
        label="Lejupielādēt audit_examples_index.xlsx",
        data=st.session_state.excel_bytes,
        file_name=f"audit_examples_index_{ts}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    st.caption(
        "Nākamais solis: pārskatīt 9_review_needed lapu, precizēt normalized_family / normalized_scenario un aizpildīt "
        "scenario_catalog laukus: required_evidence, target_text_strategy, good_comment_pattern, do_not_report_when."
    )


if __name__ == "__main__":
    main()
