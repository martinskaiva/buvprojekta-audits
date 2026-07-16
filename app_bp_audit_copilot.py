import io
import os
import re
import hashlib
import hmac
import secrets
import json
import time
import zipfile
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as UserCredentials
    from google_auth_oauthlib.flow import Flow
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
    from googleapiclient.errors import HttpError
except Exception:
    service_account = None
    UserCredentials = None
    Flow = None
    GoogleAuthRequest = None
    build = None
    MediaIoBaseDownload = None
    MediaIoBaseUpload = None
    HttpError = Exception


APP_VERSION = "v0.7.9"
APP_TITLE = f"BP AI Audit Copilot {APP_VERSION}"

REQUIRED_EXPORT_COLUMNS = [
    "note_id",
    "Nr",
    "discipline",
    "target_file",
    "target_page",
    "target_area",
    "target_text",
    "target_text_candidates",
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

INDEX_FOLDER_NAME = "02_Audit_examples_index"
FEEDBACK_FOLDER_NAME = "03_Audit_feedback"
FEEDBACK_INDEX_FOLDER_NAME = "04_Audit_feedback_index"
PENDING_FOLDER_NAME = "05_Audit_examples_pending"
PROJECT_MEMORY_FOLDER_NAME = "06_Project_memory"
PROJECT_MANIFEST_FILE = "project_manifest.json"
DOCUMENT_REGISTRY_FILE = "document_registry.xlsx"
PROJECT_FINDINGS_FILE = "project_findings.xlsx"

DEFAULT_FAMILIES = [
    "A_text_language",
    "B_lv_en",
    "C_dates_versions",
    "D_document_identity",
    "E_drawing_list_references",
    "F_normative_references",
    "G_material_type_model",
    "H_quantity_position",
    "I_specification_coverage",
    "J_cross_document_traceability",
    "K_solution_or_graphic_clarity",
    "L_fire_safety_or_regulatory_logic",
    "M_scope_or_discipline_boundary",
    "N_completeness_or_missing_content",
]

FAMILY_INSTRUCTIONS = {
    "A_text_language": {
        "name": "Teksta, gramatikas, terminoloģijas un pieraksta kļūdas",
        "look_for": "drukas kļūdas, nepareizi vārdi, locījumi, tehniskie termini, mērvienības, simboli, nepabeigti teikumi, neskaidri formulējumi",
        "report_if": "kļūda pasliktina dokumenta saprotamību, profesionālo kvalitāti vai tehnisko precizitāti",
        "do_not_report": "neziņo tikai stila gaumes jautājumus vai nebūtiskas kļūdas bez tehniskas ietekmes",
    },
    "B_lv_en": {
        "name": "LV/EN tehniskā vai satura neatbilstība",
        "look_for": "latviešu un angļu nosaukumu neatbilstības, maldinošus tulkojumus, atšķirīgus parametrus vai atšķirīgu tehnisko saturu",
        "report_if": "angļu teksts nozīmē ko citu nekā latviešu teksts vai tehniskais termins ir maldinošs",
        "do_not_report": "neziņo stilistiski atšķirīgu, bet tehniski pareizu tulkojumu",
    },
    "C_dates_versions": {
        "name": "Datumu, versiju un revīziju neatbilstības",
        "look_for": "datumu, revīziju, versiju un izlaidumu konfliktus vienā dokumentā vai ar faila identitāti",
        "report_if": "vienā dokumentā dažādās vietās redzami atšķirīgi datumi vai revīzijas tabula neatbilst titullaukam",
        "do_not_report": "neziņo vēsturisku atsauces datumu, ja nav pierādījuma, ka tam jāsakrīt ar izlaiduma datumu",
    },
    "D_document_identity": {
        "name": "Dokumenta identitāte, faila nosaukums, kods un titullauks",
        "look_for": "faila nosaukuma, dokumenta numura, projekta koda, sadaļas koda, rasējuma nosaukuma un titullauka neatbilstības",
        "report_if": "failā redzamais dokumenta numurs vai nosaukums neatbilst faila nosaukumam/titullaukam",
        "do_not_report": "neziņo 2/2 kā lapu skaita kļūdu, ja tas apzīmē būvprojekta kārtu; neziņo tikai failu sistēmas zīmju atšķirības",
    },
    "E_drawing_list_references": {
        "name": "Rasējumu saraksti un savstarpējās atsauces",
        "look_for": "rasējumu saraksta kļūdas, atsauces uz neesošiem/nepareiziem dokumentiem, nepareizus rasējuma numurus",
        "report_if": "sarakstā vai atsaucē minētais dokuments neatbilst faktiskajai dokumentu kopai vai dokumenta saturam",
        "do_not_report": "neziņo, ja nav pieejams salīdzināmais saraksts vai atsauce var būt uz ārēju dokumentu",
    },
    "F_normative_references": {
        "name": "Normatīvu atsauces",
        "look_for": "novecojušas vai nepareizas normatīvu atsauces, numura un nosaukuma pretrunas, normatīvu atšķirīgu lietojumu dokumentos",
        "report_if": "normatīva numurs un nosaukums acīmredzami neatbilst vai vienā dokumentā normatīvs norādīts pretrunīgi",
        "do_not_report": "neziņo, ja vajadzīga aktuāla ārēja normatīvu pārbaude un dokumentā nav tiešas pretrunas",
    },
    "G_material_type_model": {
        "name": "Materiāli, tipi, modeļi un tehniskie parametri",
        "look_for": "materiālu, tipu, modeļu, diametru, klašu, izmēru, marku un tehnisko parametru konfliktus",
        "report_if": "rasējumā/specifikācijā/aprakstā viens un tas pats elements norādīts ar atšķirīgu materiālu, tipu, modeli vai parametru",
        "do_not_report": "neziņo, ja atšķirība var būt vispārīgs apraksts pret detalizētu specifikāciju un nav droša salīdzināmā avota",
    },
    "H_quantity_position": {
        "name": "Daudzumi, pozīcijas un numerācija",
        "look_for": "daudzumu neatbilstības, pozīciju numuru konfliktus, trūkstošas/atkārtotas pozīcijas, nepareizu elementu skaitu",
        "report_if": "specifikācijas daudzums neatbilst rasējumā redzamajam vai pozīcijas numurs atkārtojas ar citu nozīmi",
        "do_not_report": "neziņo, ja daudzums nav droši pārbaudāms no teksta un vajadzīga grafiska mērīšana",
    },
    "I_specification_coverage": {
        "name": "Trūkumi specifikācijā",
        "look_for": "rasējumā vai aprakstā esošus elementus, kuri nav specifikācijā, trūkstošas iekārtas, materiālus vai komponentes",
        "report_if": "ir skaidri minēts elements, bet specifikācijā vai materiālu tabulā nav atbilstošas pozīcijas",
        "do_not_report": "neziņo, ja specifikācijas dokuments nav pieejams vai elements var būt iekļauts apvienotā pozīcijā",
    },
    "J_cross_document_traceability": {
        "name": "Izsekojamība starp dokumentiem",
        "look_for": "sistēmu kodu, risinājumu, plāna/profila/specifikācijas un apraksta savstarpējas pretrunas",
        "report_if": "vienā dokumentā minēts risinājums, sistēma vai kods nav izsekojams citā saistītā dokumentā vai tiek lietots atšķirīgi",
        "do_not_report": "neziņo, ja auditēts tikai viens dokuments un nav salīdzināmo failu",
    },
    "K_solution_or_graphic_clarity": {
        "name": "Risinājuma, apzīmējumu vai grafiskās skaidrības problēmas",
        "look_for": "neskaidras atsauces, nepabeigtus apzīmējumus, placeholder zīmes, neskaidrus mezglus vai formulējumus",
        "report_if": "rasējumā palicis ?, XX, TODO vai apzīmējums nav saprotams bez papildinformācijas",
        "do_not_report": "neziņo, ja neskaidrība rodas tikai no sliktas PDF kvalitātes un nav pierādāmas kļūdas",
    },
    "L_fire_safety_or_regulatory_logic": {
        "name": "Ugunsdrošības vai regulatīvās loģikas neatbilstības",
        "look_for": "ugunsdrošības, evakuācijas, ugunsnodalījumu vai regulatīvo risinājumu pretrunas dokumentā",
        "report_if": "ugunsdrošības teksts ir pretrunā rasējumam vai prasības savstarpēji konfliktē",
        "do_not_report": "neziņo, ja vajadzīga plaša normatīvu interpretācija bez konkrētas dokumenta pretrunas",
    },
    "M_scope_or_discipline_boundary": {
        "name": "Disciplīnas robežas un atbildības apjoms",
        "look_for": "citas sadaļas risinājumus nepareizā dokumentā, disciplīnu robežu sajaukumu vai neatbilstošu sadaļas saturu",
        "report_if": "sadaļas saturs neatbilst dokumenta disciplīnai vai rada nepareizu atbildības robežu",
        "do_not_report": "neziņo vispārīgas koordinācijas piezīmes, kur citas disciplīnas pieminēšana ir nepieciešama kontekstam",
    },
    "N_completeness_or_missing_content": {
        "name": "Nepabeigts vai trūkstošs saturs",
        "look_for": "tukšus laukus, placeholder tekstu, nepabeigtus teikumus, trūkstošas sadaļas, nepilnīgi aizpildītas tabulas",
        "report_if": "dokumentā redzams tukšs obligāts lauks, nepabeigts teksts/apzīmējums vai satura rādītāja neatbilstība faktiskajam saturam",
        "do_not_report": "neziņo, ja nav skaidrs, ka laukam jābūt aizpildītam vai saturs var būt citā pielikumā",
    },
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    text = str(value)
    text = text.replace("\r", " ").replace("\t", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_secret(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        try:
            if name in st.secrets:
                val = st.secrets[name]
                if isinstance(val, str):
                    return val
                return json.dumps(dict(val))
        except Exception:
            pass
        try:
            val = os.environ.get(name)
            if val:
                return val
        except Exception:
            pass
    return default


OAUTH_SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_google_oauth_config() -> Optional[Dict[str, str]]:
    """Read long-lived OAuth credentials from Streamlit Secrets.

    This version uses a refresh token and does not rely on a browser callback.
    """
    try:
        section = st.secrets.get("google_oauth")
        if section:
            config = {
                "client_id": clean_text(section.get("client_id")),
                "client_secret": clean_text(section.get("client_secret")),
                "refresh_token": clean_text(section.get("refresh_token")),
            }
            if all(config.values()):
                return config
    except Exception:
        pass

    config = {
        "client_id": clean_text(get_secret("GOOGLE_OAUTH_CLIENT_ID", default="")),
        "client_secret": clean_text(get_secret("GOOGLE_OAUTH_CLIENT_SECRET", default="")),
        "refresh_token": clean_text(get_secret("GOOGLE_OAUTH_REFRESH_TOKEN", default="")),
    }
    return config if all(config.values()) else None


def get_oauth_drive_service(config: Optional[Dict[str, str]] = None):
    """Build a Drive service from a stored OAuth refresh token."""
    if UserCredentials is None or GoogleAuthRequest is None or build is None:
        raise RuntimeError("Nav pieejamas Google OAuth bibliotēkas.")

    config = config or get_google_oauth_config()
    if not config:
        return None

    credentials = UserCredentials(
        token=None,
        refresh_token=config["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=OAUTH_SCOPES,
    )
    credentials.refresh(GoogleAuthRequest())
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def get_oauth_user(service) -> Dict[str, str]:
    about = service.about().get(fields="user(displayName,emailAddress)").execute()
    user = about.get("user") or {}
    return {
        "email": clean_text(user.get("emailAddress")),
        "name": clean_text(user.get("displayName")),
    }


def get_service_account_info() -> Optional[Dict[str, Any]]:
    raw = get_secret("GOOGLE_SERVICE_ACCOUNT_JSON", "google_service_account_json")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            st.error("GOOGLE_SERVICE_ACCOUNT_JSON nav derīgs JSON.")
            return None
    try:
        if "google_service_account" in st.secrets:
            return dict(st.secrets["google_service_account"])
    except Exception:
        pass
    try:
        if "gcp_service_account" in st.secrets:
            return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass
    return None


@st.cache_resource(show_spinner=False)
def get_drive_service_cached(sa_json: str):
    if service_account is None or build is None:
        raise RuntimeError("Nav pieejamas google-api-python-client bibliotēkas.")
    info = json.loads(sa_json)
    # Nepieciešams gan lasīšanai, gan audita rezultātu rakstīšanai Drive.
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_drive_service():
    info = get_service_account_info()
    if not info:
        return None
    return get_drive_service_cached(json.dumps(info, sort_keys=True))


def drive_list_children(service, folder_id: str, mime_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    files = []
    page_token = None
    q_parts = [f"'{folder_id}' in parents", "trashed=false"]
    if mime_filter:
        q_parts.append(f"mimeType='{mime_filter}'")
    q = " and ".join(q_parts)
    while True:
        resp = service.files().list(
            q=q,
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
            pageToken=page_token,
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def drive_get_file_metadata(service, file_id: str) -> Dict[str, Any]:
    """Nolasa Drive faila/mapes pamata metadatus, ieskaitot vecākmapes."""
    return service.files().get(
        fileId=file_id,
        fields="id,name,mimeType,parents,modifiedTime",
        supportsAllDrives=True,
    ).execute()


def resolve_input_root(service, configured_folder_id: str, wanted_name: str = "01_Input") -> Dict[str, Any]:
    """Atrod īsto 01_Input mapi arī tad, ja secrets norāda uz tās apakšmapi.

    Meklē no konfigurētās mapes uz augšu pa pirmo vecākmapju ķēdi.
    Ja 01_Input netiek atrasta, izmanto konfigurēto mapi un atgriež brīdinājumu.
    """
    current_id = clean_text(configured_folder_id)
    visited = set()
    chain: List[Dict[str, Any]] = []

    while current_id and current_id not in visited and len(chain) < 20:
        visited.add(current_id)
        meta = drive_get_file_metadata(service, current_id)
        chain.append(meta)

        if clean_text(meta.get("name")).lower() == wanted_name.lower():
            return {
                "id": clean_text(meta.get("id")),
                "name": clean_text(meta.get("name")),
                "resolved": True,
                "configured_id": clean_text(configured_folder_id),
                "chain": chain,
                "warning": "",
            }

        parents = meta.get("parents") or []
        if not parents:
            break
        current_id = clean_text(parents[0])

    fallback = chain[0] if chain else {
        "id": clean_text(configured_folder_id),
        "name": "",
    }
    return {
        "id": clean_text(fallback.get("id")) or clean_text(configured_folder_id),
        "name": clean_text(fallback.get("name")) or "Konfigurētā mape",
        "resolved": False,
        "configured_id": clean_text(configured_folder_id),
        "chain": chain,
        "warning": (
            f"Neizdevās atrast vecākmapi ar nosaukumu {wanted_name}. "
            "Tiek izmantota konfigurētā mape."
        ),
    }


def resolve_results_folder(
    service,
    input_folder_id: str,
    explicit_results_folder_id: str = "",
) -> Dict[str, Any]:
    """Atrod 02_Results mapi.

    Prioritāte:
    1) explicit_results_folder_id no secrets/UI;
    2) 02_Results kā 01_Input māsas mape zem BP_Audits_tests.
    """
    explicit_id = clean_text(explicit_results_folder_id)
    if explicit_id:
        meta = drive_get_file_metadata(service, explicit_id)
        if clean_text(meta.get("mimeType")) != "application/vnd.google-apps.folder":
            raise RuntimeError("Norādītais 02_Results ID nav Google Drive mape.")
        return {
            "id": clean_text(meta.get("id")),
            "name": clean_text(meta.get("name")),
            "source": "explicit",
            "parent_id": clean_text((meta.get("parents") or [""])[0]),
        }

    input_root = resolve_input_root(service, input_folder_id, wanted_name="01_Input")
    input_root_id = clean_text(input_root.get("id"))
    if not input_root_id:
        raise RuntimeError("Neizdevās noteikt 01_Input mapi.")

    input_meta = drive_get_file_metadata(service, input_root_id)
    parents = input_meta.get("parents") or []
    if not parents:
        raise RuntimeError("01_Input mapei nav atrodama vecākmape BP_Audits_tests.")

    bp_root_id = clean_text(parents[0])
    results_folder = drive_find_child_folder(service, bp_root_id, "02_Results")
    if not results_folder:
        raise RuntimeError(
            "Zem BP_Audits_tests netika atrasta mape 02_Results. "
            "Norādi tās ID sānjoslā."
        )

    return {
        "id": clean_text(results_folder.get("id")),
        "name": clean_text(results_folder.get("name")),
        "source": "sibling_of_01_Input",
        "parent_id": bp_root_id,
    }


def drive_upload_bytes(
    service,
    folder_id: str,
    file_name: str,
    data: bytes,
    mime_type: str,
) -> Dict[str, Any]:
    """Augšupielādē baita saturu konkrētā Google Drive mapē."""
    if MediaIoBaseUpload is None:
        raise RuntimeError("Nav pieejama MediaIoBaseUpload bibliotēka.")
    if not clean_text(folder_id):
        raise ValueError("Nav norādīts mērķa Drive mapes ID.")
    if not clean_text(file_name):
        raise ValueError("Nav norādīts augšupielādējamā faila nosaukums.")

    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype=mime_type,
        resumable=False,
    )
    metadata = {
        "name": clean_text(file_name),
        "parents": [clean_text(folder_id)],
    }
    return service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,mimeType,size,createdTime,webViewLink,parents",
        supportsAllDrives=True,
    ).execute()



EXCEL_ILLEGAL_CHARACTERS_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F]"
)


def excel_safe_value(value: Any) -> Any:
    """Sagatavo vērtību drošai ierakstīšanai XLSX.

    PDF teksta slānī mēdz būt vadības rakstzīmes, kuras openpyxl nepieņem.
    Skaitļus un tukšās vērtības saglabā to sākotnējā tipā; tekstam izņem
    neatļautos XML vadības simbolus un ievēro Excel šūnas garuma limitu.
    """
    if value is None:
        return ""

    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass

    if isinstance(value, (int, float, bool)):
        return value

    text = str(value)
    text = EXCEL_ILLEGAL_CHARACTERS_RE.sub("", text)
    text = text.replace("\x7f", "")
    return text[:32767]


def make_excel_safe_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    safe_df = df.copy()
    for column in safe_df.columns:
        safe_df[column] = safe_df[column].map(excel_safe_value)
    safe_df.columns = [
        excel_safe_value(column)[:255]
        for column in safe_df.columns
    ]
    return safe_df


def dataframe_to_xlsx_bytes(df: pd.DataFrame, sheet_name: str) -> bytes:
    """Ieraksta DataFrame XLSX, pirms tam iztīrot PDF vadības rakstzīmes."""
    safe_df = make_excel_safe_dataframe(df)
    safe_sheet_name = re.sub(
        r"[\[\]:*?/\\\\]",
        "_",
        clean_text(sheet_name),
    )[:31] or "Sheet1"

    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        safe_df.to_excel(
            writer,
            sheet_name=safe_sheet_name,
            index=False,
        )
    return bio.getvalue()


def ensure_memory_project_folder(
    service,
    memory_folder_id: str,
    section_folder_name: str,
    project_folder_name: str,
) -> Dict[str, Any]:
    """Atrod vai izveido 03_Memory sadaļas projekta mapi."""
    section = drive_find_child_folder(service, memory_folder_id, section_folder_name)
    if not section:
        raise RuntimeError(
            f"Mape 03_Memory/{section_folder_name} nav atrasta. "
            "Izveido to Google Drive struktūrā."
        )
    project_code = normalize_project_code(project_folder_name)
    project_folder = find_project_folder(service, clean_text(section.get("id")), project_code)
    if project_folder:
        return project_folder
    return drive_create_folder(service, clean_text(section.get("id")), project_folder_name)



def _canonical_drive_rel_path(value: Any) -> str:
    value = clean_text(value).replace("\\", "/")
    value = re.sub(r"/+", "/", value).strip("/")
    return value.casefold()


def _select_rows_for_pdf(
    accepted_df: pd.DataFrame,
    pdf_rel_path: str,
    pdf_name: str,
    all_pdf_items: List[Dict[str, Any]],
) -> pd.DataFrame:
    if accepted_df is None or accepted_df.empty or "target_file" not in accepted_df.columns:
        return pd.DataFrame()
    targets = accepted_df["target_file"].astype(str).map(_canonical_drive_rel_path)
    rel_key = _canonical_drive_rel_path(pdf_rel_path)
    rows = accepted_df[targets.eq(rel_key)].copy()
    if not rows.empty:
        return rows
    name_key = _canonical_drive_rel_path(pdf_name)
    same_name_count = sum(
        1 for item in all_pdf_items or []
        if _canonical_drive_rel_path(clean_text(item.get("name"))) == name_key
    )
    if same_name_count != 1:
        return pd.DataFrame()
    basenames = accepted_df["target_file"].astype(str).map(
        lambda value: _canonical_drive_rel_path(
            clean_text(value).replace("\\", "/").rsplit("/", 1)[-1]
        )
    )
    return accepted_df[basenames.eq(name_key)].copy()


def build_annotated_pdf_exports(
    accepted_df: pd.DataFrame,
    pdf_items: List[Dict[str, Any]],
    timestamp: str,
) -> List[Dict[str, Any]]:
    exports: List[Dict[str, Any]] = []
    for item in pdf_items or []:
        pdf_name = clean_text(item.get("name"))
        pdf_rel_path = clean_text(item.get("rel_path")) or pdf_name
        pdf_bytes = item.get("bytes")
        if not pdf_name or not pdf_bytes:
            continue
        rows = _select_rows_for_pdf(accepted_df, pdf_rel_path, pdf_name, pdf_items)
        if rows.empty:
            result_pdf, report = add_no_findings_banner(pdf_bytes)
            audit_status = "no_findings"
        else:
            result_pdf, report = annotate_pdf_bytes(pdf_bytes, rows)
            audit_status = "findings"
        if not result_pdf:
            continue
        source_stem = os.path.splitext(pdf_name)[0]
        safe_stem = re.sub(r"[^A-Za-z0-9_\-]+", "_", source_stem)[:120]
        exports.append({
            "name": f"annotated_{safe_stem}_{timestamp}.pdf",
            "data": result_pdf,
            "mime_type": "application/pdf",
            "source": pdf_rel_path,
            "report": report,
            "audit_status": audit_status,
            "accepted_count": int(len(rows)),
            "banner_verified": any(
                clean_text(r.get("status")) == "no_findings_note_verified"
                for r in report or [] if isinstance(r, dict)
            ),
        })
    return exports

def upload_audit_files_to_drive(
    service,
    results_target: Dict[str, Any],
    memory_folder_id: str,
    project_folder_name: str,
    accepted_df: pd.DataFrame,
    rejected_df: pd.DataFrame,
    pdf_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Saglabā PDF rezultātos, bet mācību Excel atbilstošajās 03_Memory mapēs."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    uploaded_results: List[Dict[str, Any]] = []
    uploaded_memory: List[Dict[str, Any]] = []

    pdf_exports = build_annotated_pdf_exports(accepted_df, pdf_items, timestamp)
    for export in pdf_exports:
        uploaded = drive_upload_bytes(
            service,
            folder_id=clean_text(results_target.get("id")),
            file_name=export["name"],
            data=export["data"],
            mime_type=export["mime_type"],
        )
        uploaded["destination_path"] = clean_text(results_target.get("path"))
        uploaded["source"] = clean_text(export.get("source"))
        uploaded["audit_status"] = clean_text(export.get("audit_status"))
        uploaded["accepted_count"] = int(export.get("accepted_count") or 0)
        uploaded["banner_verified"] = bool(export.get("banner_verified"))
        uploaded["annotation_report"] = export.get("report") or []
        uploaded_results.append(uploaded)

    project_name = clean_text(project_folder_name) or clean_text(results_target.get("name")) or "Nezinams_projekts"

    if accepted_df is not None and not accepted_df.empty:
        pending_project = ensure_memory_project_folder(
            service, memory_folder_id, PENDING_FOLDER_NAME, project_name
        )
        pending_name = f"accepted_candidates_{normalize_project_code(project_name) or 'project'}_{timestamp}.xlsx"
        uploaded = drive_upload_bytes(
            service,
            folder_id=clean_text(pending_project.get("id")),
            file_name=pending_name,
            data=dataframe_to_xlsx_bytes(accepted_df, "accepted_candidates"),
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        uploaded["destination_path"] = f"03_Memory/{PENDING_FOLDER_NAME}/{clean_text(pending_project.get('name'))}"
        uploaded_memory.append(uploaded)

    if rejected_df is not None and not rejected_df.empty:
        feedback_project = ensure_memory_project_folder(
            service, memory_folder_id, FEEDBACK_FOLDER_NAME, project_name
        )
        feedback_name = f"rejected_patterns_{normalize_project_code(project_name) or 'project'}_{timestamp}.xlsx"
        uploaded = drive_upload_bytes(
            service,
            folder_id=clean_text(feedback_project.get("id")),
            file_name=feedback_name,
            data=dataframe_to_xlsx_bytes(rejected_df, "rejected_patterns"),
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        uploaded["destination_path"] = f"03_Memory/{FEEDBACK_FOLDER_NAME}/{clean_text(feedback_project.get('name'))}"
        uploaded_memory.append(uploaded)

    return {
        "results_files": uploaded_results,
        "memory_files": uploaded_memory,
        "target": results_target,
        "timestamp": timestamp,
    }


def drive_find_child_file(service, parent_id: str, file_name: str) -> Optional[Dict[str, Any]]:
    wanted = clean_text(file_name)
    for item in drive_list_children(service, parent_id):
        if (
            clean_text(item.get("name")) == wanted
            and clean_text(item.get("mimeType")) != "application/vnd.google-apps.folder"
        ):
            return item
    return None


def drive_upsert_bytes(
    service,
    folder_id: str,
    file_name: str,
    data: bytes,
    mime_type: str,
) -> Dict[str, Any]:
    if MediaIoBaseUpload is None:
        raise RuntimeError("Nav pieejama MediaIoBaseUpload bibliotēka.")
    existing = drive_find_child_file(service, folder_id, file_name)
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    if existing:
        return service.files().update(
            fileId=clean_text(existing.get("id")),
            media_body=media,
            fields="id,name,mimeType,size,createdTime,modifiedTime,webViewLink,parents",
            supportsAllDrives=True,
        ).execute()
    return drive_upload_bytes(service, folder_id, file_name, data, mime_type)


def ensure_project_memory_root(service, memory_folder_id: str) -> Dict[str, Any]:
    root = drive_find_child_folder(service, memory_folder_id, PROJECT_MEMORY_FOLDER_NAME)
    return root or drive_create_folder(service, memory_folder_id, PROJECT_MEMORY_FOLDER_NAME)


def list_project_memories(service, memory_folder_id: str) -> List[Dict[str, Any]]:
    if not clean_text(memory_folder_id):
        return []
    root = drive_find_child_folder(service, memory_folder_id, PROJECT_MEMORY_FOLDER_NAME)
    if not root:
        return []
    folders = drive_list_children(
        service,
        clean_text(root.get("id")),
        "application/vnd.google-apps.folder",
    )
    out = []
    for folder in folders:
        item = dict(folder)
        item["project_code"] = normalize_project_code(clean_text(folder.get("name")))
        item["path"] = (
            f"03_Memory/{PROJECT_MEMORY_FOLDER_NAME}/"
            f"{clean_text(folder.get('name'))}"
        )
        out.append(item)
    return sorted(out, key=lambda x: clean_text(x.get("name")).casefold())


def read_json_drive_file(service, folder_id: str, file_name: str) -> Dict[str, Any]:
    info = drive_find_child_file(service, folder_id, file_name)
    if not info:
        return {}
    try:
        raw = drive_download_bytes(service, clean_text(info.get("id")))
        data = json.loads(raw.decode("utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_project_memory_excel(service, folder_id: str, file_name: str) -> pd.DataFrame:
    info = drive_find_child_file(service, folder_id, file_name)
    if not info:
        return pd.DataFrame()
    try:
        raw = drive_download_bytes(service, clean_text(info.get("id")))
        frame = pd.read_excel(io.BytesIO(raw), dtype=object)
        frame.columns = [clean_text(col) for col in frame.columns]
        return frame
    except Exception:
        return pd.DataFrame()


def write_project_memory_excel(
    service,
    folder_id: str,
    file_name: str,
    dataframe: pd.DataFrame,
    sheet_name: str,
) -> Dict[str, Any]:
    return drive_upsert_bytes(
        service,
        folder_id,
        file_name,
        dataframe_to_xlsx_bytes(dataframe, sheet_name),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def infer_document_code(file_name: str) -> str:
    stem = os.path.splitext(clean_text(file_name))[0]
    stem = re.sub(r"\s+-\s+.*$", "", stem).strip()
    match = re.search(r"\b[A-Z0-9]+(?:[-_][A-Z0-9]+){4,}\b", stem, flags=re.I)
    return clean_text(match.group(0)) if match else stem[:180]


def infer_revision_from_text(file_name: str, pdf_text: str) -> str:
    combined = f"{file_name}\n{pdf_text[:5000]}"
    for pattern in [
        r"\b(?:REVISION|REVĪZIJA|REV\.?)\s*[:\-]?\s*([A-Z0-9]{1,8})\b",
        r"\b(?:VERSION|VERSIJA)\s*[:\-]?\s*([A-Z0-9.]{1,12})\b",
        r"\bR\s*([0-9]{1,3}[A-Z]?)\b",
    ]:
        match = re.search(pattern, combined, flags=re.I)
        if match:
            return clean_text(match.group(1))
    return ""


def infer_document_date(pdf_text: str) -> str:
    sample = clean_text(pdf_text[:10000])
    for pattern in [
        r"\b(20\d{2}[-./]\d{1,2}[-./]\d{1,2})\b",
        r"\b(\d{1,2}[./]\d{1,2}[./]20\d{2})\b",
    ]:
        match = re.search(pattern, sample)
        if match:
            return clean_text(match.group(1))
    return ""


def make_document_summary(file_name: str, pdf_text: str, max_length: int = 1800) -> str:
    content = re.sub(r"--- PAGE \d+ ---", " ", clean_text(pdf_text), flags=re.I)
    content = clean_text(content)
    return content[:max_length] if content else f"Dokuments: {clean_text(file_name)}. Teksts nav nolasīts."


def create_or_activate_project_memory(
    service,
    memory_folder_id: str,
    project_code: str,
    project_name: str = "",
) -> Dict[str, Any]:
    code = normalize_project_code(project_code)
    if not code:
        raise ValueError("Projekta kods ir tukšs.")
    root = ensure_project_memory_root(service, memory_folder_id)
    folder = find_project_folder(service, clean_text(root.get("id")), code)
    if not folder:
        folder = drive_create_folder(service, clean_text(root.get("id")), code)
    folder_id = clean_text(folder.get("id"))
    now = datetime.now().isoformat(timespec="seconds")
    manifest = read_json_drive_file(service, folder_id, PROJECT_MANIFEST_FILE)
    if not manifest:
        manifest = {
            "schema_version": "1.0",
            "project_code": code,
            "project_name": clean_text(project_name) or code,
            "created_at": now,
            "last_activated_at": now,
            "last_audited_at": "",
            "document_count": 0,
            "audit_count": 0,
            "disciplines": [],
        }
    else:
        manifest["project_code"] = code
        manifest["project_name"] = clean_text(manifest.get("project_name")) or clean_text(project_name) or code
        manifest["last_activated_at"] = now

    drive_upsert_bytes(
        service,
        folder_id,
        PROJECT_MANIFEST_FILE,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        "application/json",
    )

    if not drive_find_child_file(service, folder_id, DOCUMENT_REGISTRY_FILE):
        registry_columns = [
            "document_id", "drive_file_id", "file_name", "relative_path",
            "project_code", "discipline", "document_type", "document_code",
            "revision", "document_date", "page_count", "content_hash",
            "first_audited_at", "last_audited_at", "audit_count",
            "accepted_findings", "rejected_findings", "summary",
        ]
        write_project_memory_excel(
            service, folder_id, DOCUMENT_REGISTRY_FILE,
            pd.DataFrame(columns=registry_columns), "document_registry",
        )

    if not drive_find_child_file(service, folder_id, PROJECT_FINDINGS_FILE):
        finding_columns = [
            "memory_record_id", "audit_run_id", "created_at", "decision",
            "project_code", "source_file", "source_file_rel_path",
            "discipline", "family", "issue_type", "target_page",
            "target_area", "target_text", "comment_text", "problem",
            "reject_reason", "do_not_show_similar",
        ]
        write_project_memory_excel(
            service, folder_id, PROJECT_FINDINGS_FILE,
            pd.DataFrame(columns=finding_columns), "project_findings",
        )

    return {
        "id": folder_id,
        "name": clean_text(folder.get("name")) or code,
        "project_code": code,
        "project_name": clean_text(manifest.get("project_name")) or code,
        "manifest": manifest,
    }


def update_project_memory_after_audit(
    service,
    project_folder_id: str,
    project_code: str,
    audit_run_id: str,
    pdf_items: List[Dict[str, Any]],
    accepted_df: pd.DataFrame,
    rejected_df: pd.DataFrame,
) -> Dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    code = normalize_project_code(project_code)
    registry = read_project_memory_excel(service, project_folder_id, DOCUMENT_REGISTRY_FILE)

    columns = [
        "document_id", "drive_file_id", "file_name", "relative_path",
        "project_code", "discipline", "document_type", "document_code",
        "revision", "document_date", "page_count", "content_hash",
        "first_audited_at", "last_audited_at", "audit_count",
        "accepted_findings", "rejected_findings", "summary",
    ]
    for col in columns:
        if col not in registry.columns:
            registry[col] = ""

    accepted_targets = (
        accepted_df["target_file"].astype(str).map(_canonical_drive_rel_path)
        if accepted_df is not None and not accepted_df.empty and "target_file" in accepted_df.columns
        else pd.Series(dtype=str)
    )
    rejected_targets = (
        rejected_df["source_file"].astype(str).map(_canonical_drive_rel_path)
        if rejected_df is not None and not rejected_df.empty and "source_file" in rejected_df.columns
        else pd.Series(dtype=str)
    )

    for item in pdf_items or []:
        file_name = clean_text(item.get("name"))
        rel_path = clean_text(item.get("rel_path")) or file_name
        pdf_bytes = item.get("bytes") or b""
        pdf_text = clean_text(item.get("text"))
        content_hash = hashlib.sha256(pdf_bytes).hexdigest()
        path_key = _canonical_drive_rel_path(rel_path)
        name_key = _canonical_drive_rel_path(file_name)
        accepted_count = int(accepted_targets.isin([path_key, name_key]).sum()) if not accepted_targets.empty else 0
        rejected_count = int(rejected_targets.isin([path_key, name_key]).sum()) if not rejected_targets.empty else 0
        document_id = hashlib.sha1(f"{code}|{rel_path}|{content_hash}".encode("utf-8")).hexdigest()[:24]

        match_mask = pd.Series(False, index=registry.index, dtype=bool)
        if not registry.empty:
            match_mask = (
                registry["relative_path"].astype(str).map(_canonical_drive_rel_path).eq(path_key)
                & registry["content_hash"].astype(str).eq(content_hash)
            )

        row = {
            "document_id": document_id,
            "drive_file_id": clean_text(item.get("id")),
            "file_name": file_name,
            "relative_path": rel_path,
            "project_code": code,
            "discipline": infer_discipline_from_filename(file_name),
            "document_type": infer_document_role(file_name),
            "document_code": infer_document_code(file_name),
            "revision": infer_revision_from_text(file_name, pdf_text),
            "document_date": infer_document_date(pdf_text),
            "page_count": len(item.get("pages") or []),
            "content_hash": content_hash,
            "first_audited_at": now,
            "last_audited_at": now,
            "audit_count": 1,
            "accepted_findings": accepted_count,
            "rejected_findings": rejected_count,
            "summary": make_document_summary(file_name, pdf_text),
        }

        if match_mask.any():
            idx = registry[match_mask].index[0]
            previous_count = pd.to_numeric(
                pd.Series([registry.at[idx, "audit_count"]]), errors="coerce"
            ).fillna(0).iloc[0]
            row["first_audited_at"] = clean_text(registry.at[idx, "first_audited_at"]) or now
            row["audit_count"] = int(previous_count) + 1
            for key, value in row.items():
                registry.at[idx, key] = value
        else:
            registry = pd.concat([registry, pd.DataFrame([row])], ignore_index=True, sort=False)

    registry = registry[columns].copy()
    registry_file = write_project_memory_excel(
        service, project_folder_id, DOCUMENT_REGISTRY_FILE,
        registry, "document_registry",
    )

    findings = read_project_memory_excel(service, project_folder_id, PROJECT_FINDINGS_FILE)
    finding_rows = []
    if accepted_df is not None and not accepted_df.empty:
        for _, row in accepted_df.iterrows():
            source = clean_text(row.get("target_file"))
            finding_rows.append({
                "memory_record_id": hashlib.sha1(
                    f"{audit_run_id}|accepted|{source}|{row.get('note_id')}".encode("utf-8")
                ).hexdigest()[:24],
                "audit_run_id": audit_run_id,
                "created_at": now,
                "decision": "accepted",
                "project_code": code,
                "source_file": source.rsplit("/", 1)[-1],
                "source_file_rel_path": source,
                "discipline": clean_text(row.get("discipline")),
                "family": clean_text(row.get("issue_type")),
                "issue_type": clean_text(row.get("issue_type")),
                "target_page": clean_text(row.get("target_page")),
                "target_area": clean_text(row.get("target_area")),
                "target_text": clean_text(row.get("target_text")),
                "comment_text": clean_text(row.get("comment_text")),
                "problem": clean_text(row.get("comparison_evidence")),
                "reject_reason": "",
                "do_not_show_similar": False,
            })
    if rejected_df is not None and not rejected_df.empty:
        for _, row in rejected_df.iterrows():
            source = clean_text(row.get("source_file"))
            finding_rows.append({
                "memory_record_id": hashlib.sha1(
                    f"{audit_run_id}|rejected|{source}|{row.get('candidate_index')}".encode("utf-8")
                ).hexdigest()[:24],
                "audit_run_id": audit_run_id,
                "created_at": now,
                "decision": "rejected",
                "project_code": code,
                "source_file": source.rsplit("/", 1)[-1],
                "source_file_rel_path": source,
                "discipline": infer_discipline_from_filename(source),
                "family": clean_text(row.get("family")),
                "issue_type": clean_text(row.get("issue_type")),
                "target_page": clean_text(row.get("target_page")),
                "target_area": clean_text(row.get("target_area")),
                "target_text": clean_text(row.get("target_text")),
                "comment_text": clean_text(row.get("comment_text")),
                "problem": clean_text(row.get("problem")),
                "reject_reason": clean_text(row.get("reject_reason")),
                "do_not_show_similar": clean_text(row.get("do_not_show_similar")),
            })

    if finding_rows:
        findings = pd.concat([findings, pd.DataFrame(finding_rows)], ignore_index=True, sort=False)
        if "memory_record_id" in findings.columns:
            findings = findings.drop_duplicates(subset=["memory_record_id"], keep="last")

    findings_file = write_project_memory_excel(
        service, project_folder_id, PROJECT_FINDINGS_FILE,
        findings, "project_findings",
    )

    manifest = read_json_drive_file(service, project_folder_id, PROJECT_MANIFEST_FILE)
    disciplines = sorted({
        clean_text(v) for v in registry.get("discipline", pd.Series(dtype=str)).tolist()
        if clean_text(v)
    })
    manifest.update({
        "project_code": code,
        "last_audited_at": now,
        "document_count": int(len(registry)),
        "audit_count": int(pd.to_numeric(
            registry.get("audit_count", pd.Series(dtype=float)), errors="coerce"
        ).fillna(0).sum()),
        "disciplines": disciplines,
    })
    manifest_file = drive_upsert_bytes(
        service, project_folder_id, PROJECT_MANIFEST_FILE,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        "application/json",
    )
    return {
        "manifest": manifest,
        "manifest_file": manifest_file,
        "registry_file": registry_file,
        "findings_file": findings_file,
        "documents": len(registry),
        "disciplines": disciplines,
    }

def run_drive_write_test(
    service,
    input_folder_id: str,
    results_folder_id: str = "",
) -> Dict[str, Any]:
    """Izveido nelielu testa TXT failu 02_Results mapē."""
    target = resolve_results_folder(
        service,
        input_folder_id=input_folder_id,
        explicit_results_folder_id=results_folder_id,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = hashlib.sha1(
        f"{timestamp}|{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:6]
    file_name = f"drive_write_test_{timestamp}_{suffix}.txt"
    content = (
        "BP AI Audit Copilot Google Drive rakstīšanas tests\n"
        f"App version: {APP_VERSION}\n"
        f"Created at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"Target folder: {target.get('name')}\n"
        "Ja šis fails ir redzams 02_Results mapē, rakstīšanas tiesības darbojas.\n"
    ).encode("utf-8")
    uploaded = drive_upload_bytes(
        service,
        folder_id=target["id"],
        file_name=file_name,
        data=content,
        mime_type="text/plain",
    )
    return {
        "ok": True,
        "target": target,
        "file": uploaded,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def drive_find_child_folder(service, parent_id: str, folder_name: str) -> Optional[Dict[str, Any]]:
    children = drive_list_children(service, parent_id, "application/vnd.google-apps.folder")
    for item in children:
        if item.get("name") == folder_name:
            return item
    return None


def drive_create_folder(service, parent_id: str, folder_name: str) -> Dict[str, Any]:
    """Izveido jaunu Google Drive mapi norādītajā vecākmapē."""
    name = clean_text(folder_name)
    if not clean_text(parent_id):
        raise ValueError("Nav norādīts vecākmapes ID.")
    if not name:
        raise ValueError("Jaunās mapes nosaukums ir tukšs.")
    if "/" in name or "\\" in name:
        raise ValueError("Mapes nosaukumā nedrīkst būt / vai \\.")

    existing = drive_find_child_folder(service, parent_id, name)
    if existing:
        return {**existing, "already_existed": True}

    created = service.files().create(
        body={
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [clean_text(parent_id)],
        },
        fields="id,name,mimeType,parents,webViewLink,createdTime",
        supportsAllDrives=True,
    ).execute()
    created["already_existed"] = False
    return created


def run_drive_write_test_to_folder(
    service,
    target_folder_id: str,
    target_folder_name: str = "",
) -> Dict[str, Any]:
    """Izveido testa TXT failu lietotāja izvēlētā Drive mapē."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = hashlib.sha1(
        f"{timestamp}|{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:6]
    file_name = f"drive_write_test_{timestamp}_{suffix}.txt"
    content = (
        "BP AI Audit Copilot Google Drive rakstīšanas tests\n"
        f"App version: {APP_VERSION}\n"
        f"Created at: {datetime.now().isoformat(timespec='seconds')}\n"
        f"Target folder: {clean_text(target_folder_name) or target_folder_id}\n"
        "Ja šis fails ir redzams izvēlētajā mapē, rakstīšanas tiesības darbojas.\n"
    ).encode("utf-8")
    uploaded = drive_upload_bytes(
        service,
        folder_id=target_folder_id,
        file_name=file_name,
        data=content,
        mime_type="text/plain",
    )
    return {
        "ok": True,
        "target": {"id": target_folder_id, "name": target_folder_name},
        "file": uploaded,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def drive_list_recursive(service, folder_id: str, extensions: Tuple[str, ...], prefix: str = "", max_files: int = 5000) -> List[Dict[str, Any]]:
    """Recursively list Drive files by extension.

    This function is intentionally defensive: a single inaccessible subfolder or
    strange Drive item must not crash the whole Streamlit app.
    """
    out: List[Dict[str, Any]] = []
    stack: List[Tuple[str, str]] = [(str(folder_id).strip(), prefix)]
    visited = set()
    warnings: List[str] = []

    while stack and len(out) < max_files:
        current_id, current_prefix = stack.pop()
        if not current_id or current_id in visited:
            continue
        visited.add(current_id)

        try:
            children = drive_list_children(service, current_id)
        except Exception as e:
            where = current_prefix or current_id
            warnings.append(f"Neizdevās nolasīt mapi: {where} — {e}")
            continue

        for item in children:
            try:
                name = clean_text(item.get("name", ""))
                mime_type = clean_text(item.get("mimeType", ""))
                item_id = clean_text(item.get("id", ""))
                if not name or not item_id:
                    continue
                rel_path = f"{current_prefix}/{name}" if current_prefix else name

                if mime_type == "application/vnd.google-apps.folder":
                    stack.append((item_id, rel_path))
                    continue

                if name.lower().endswith(tuple(x.lower() for x in extensions)):
                    item2 = dict(item)
                    item2["rel_path"] = rel_path
                    out.append(item2)
                    if len(out) >= max_files:
                        break
            except Exception as e:
                warnings.append(f"Izlaists Drive ieraksts mapē {current_prefix or folder_id}: {e}")
                continue

    if warnings:
        st.session_state["drive_list_warnings"] = warnings[:50]
    return sorted(out, key=lambda x: clean_text(x.get("rel_path", x.get("name", ""))))


def drive_download_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return fh.getvalue()


def normalize_project_code(folder_name: str) -> str:
    """Normalizē numurētu projekta mapes nosaukumu vienotam sasaistes kodam."""
    value = clean_text(folder_name)
    value = re.sub(r"^\d+[\s_-]*", "", value)
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value


def find_project_folder(service, parent_folder_id: str, project_code: str) -> Optional[Dict[str, Any]]:
    """Atrod projekta mapi pēc normalizēta koda, nevis precīza nosaukuma."""
    wanted = normalize_project_code(project_code).lower()
    if not wanted:
        return None
    folders = drive_list_children(service, parent_folder_id, "application/vnd.google-apps.folder")
    matches = [
        folder for folder in folders
        if normalize_project_code(clean_text(folder.get("name"))).lower() == wanted
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: clean_text(item.get("name")).lower())[0]


def find_latest_index_file(service, memory_folder_id: str) -> Optional[Dict[str, Any]]:
    index_folder = drive_find_child_folder(service, memory_folder_id, INDEX_FOLDER_NAME)
    if not index_folder:
        return None
    files = drive_list_recursive(service, index_folder["id"], (".xlsx", ".xlsm"), prefix=INDEX_FOLDER_NAME, max_files=200)
    files = [f for f in files if f.get("name", "").lower().endswith((".xlsx", ".xlsm")) and not f.get("name", "").startswith("~$")]
    if not files:
        return None
    return sorted(files, key=lambda x: x.get("modifiedTime", ""), reverse=True)[0]


def read_excel_sheet_from_bytes(data: bytes, preferred_sheets: List[str]) -> pd.DataFrame:
    xls = pd.ExcelFile(io.BytesIO(data))
    sheet_name = None
    for wanted in preferred_sheets:
        for s in xls.sheet_names:
            if s.strip().lower() == wanted.strip().lower():
                sheet_name = s
                break
        if sheet_name:
            break
    if sheet_name is None:
        sheet_name = xls.sheet_names[0]
    df = pd.read_excel(io.BytesIO(data), sheet_name=sheet_name, dtype=object)
    df.columns = [clean_text(c) for c in df.columns]
    return df


def normalize_index_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in REQUIRED_EXPORT_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    for col in [
        "normalized_family", "normalized_scenario", "scenario_label",
        "document_role", "source_path", "source_file",
        "project_folder_name", "project_code",
    ]:
        if col not in df.columns:
            df[col] = ""
    for col in df.columns:
        df[col] = df[col].map(clean_text)
    df = df[df["comment_text"].astype(str).str.strip().ne("") | df["target_text"].astype(str).str.strip().ne("")]
    return df.reset_index(drop=True)


def load_audit_examples_index(service, memory_folder_id: str) -> Tuple[pd.DataFrame, Optional[Dict[str, Any]], List[str]]:
    """Droši atrod, lejupielādē un nolasa jaunāko audita piemēru indeksu."""
    messages: List[str] = []
    index_file: Optional[Dict[str, Any]] = None
    try:
        index_file = find_latest_index_file(service, memory_folder_id)
        if not index_file:
            return pd.DataFrame(), None, [f"Nav atrasts .xlsx fails mapē 03_Memory/{INDEX_FOLDER_NAME}."]

        data = drive_download_bytes(service, index_file["id"])
        if not data:
            return pd.DataFrame(), index_file, [f"Indeksa fails {index_file.get('name')} ir tukšs vai netika lejupielādēts."]

        df = read_excel_sheet_from_bytes(data, ["1_examples_index", "examples_index"])
        df = normalize_index_df(df)
        if df.empty:
            messages.append(f"Indekss {index_file.get('name')} tika nolasīts, bet tajā nav izmantojamu piemēru.")
        return df, index_file, messages
    except Exception as e:
        name = clean_text(index_file.get("name")) if index_file else "audit_examples_index"
        return pd.DataFrame(), index_file, [f"Neizdevās nolasīt indeksu {name}: {e}"]


def load_feedback(
    service,
    memory_folder_id: str,
    project_code: str,
) -> Tuple[pd.DataFrame, List[str]]:
    """Nolasa visas konkrētā projekta noraidītās piezīmes."""
    messages: List[str] = []
    project_code = normalize_project_code(project_code)
    if not project_code:
        return pd.DataFrame(), ["Feedback netika nolasīts, jo nav izvēlēts auditējamais projekts."]
    try:
        feedback_root = drive_find_child_folder(service, memory_folder_id, FEEDBACK_FOLDER_NAME)
        if not feedback_root:
            return pd.DataFrame(), [f"Mape 03_Memory/{FEEDBACK_FOLDER_NAME} nav atrasta. Turpinu bez negatīvās atmiņas."]
        project_folder = find_project_folder(service, feedback_root["id"], project_code)
        if not project_folder:
            return pd.DataFrame(), [f"Projektam {project_code} nav feedback mapes zem 03_Memory/{FEEDBACK_FOLDER_NAME}. Turpinu bez negatīvās atmiņas."]
        files = drive_list_recursive(
            service,
            project_folder["id"],
            (".xlsx", ".xlsm"),
            prefix=f"{FEEDBACK_FOLDER_NAME}/{clean_text(project_folder.get('name'))}",
            max_files=1000,
        )
        files = [item for item in files if not clean_text(item.get("name")).startswith("~$")]
        if not files:
            return pd.DataFrame(), []
        frames: List[pd.DataFrame] = []
        for item in sorted(files, key=lambda x: x.get("modifiedTime", "")):
            try:
                data = drive_download_bytes(service, item["id"])
                if not data:
                    messages.append(f"Izlaists tukšs feedback fails: {item.get('name')}")
                    continue
                frame = pd.read_excel(io.BytesIO(data), dtype=object)
                frame.columns = [clean_text(col) for col in frame.columns]
                for col in frame.columns:
                    frame[col] = frame[col].map(clean_text)
                frame["feedback_source_file"] = clean_text(item.get("name"))
                frame["feedback_project_code"] = project_code
                frames.append(frame)
            except Exception as exc:
                messages.append(f"Neizdevās nolasīt feedback failu {item.get('name')}: {exc}")
        if not frames:
            return pd.DataFrame(), messages
        combined = pd.concat(frames, ignore_index=True, sort=False)
        dedupe_cols = [col for col in ["note_id", "source_file", "target_page", "comment_text", "reject_reason"] if col in combined.columns]
        if dedupe_cols:
            combined = combined.drop_duplicates(subset=dedupe_cols, keep="last")
        return combined.reset_index(drop=True), messages
    except Exception as exc:
        return pd.DataFrame(), [f"Feedback nolasīšana projektam {project_code} neizdevās: {exc}. Turpinu bez negatīvās atmiņas."]



def extract_pdf_text(pdf_bytes: bytes, max_chars: int) -> Tuple[str, List[Dict[str, Any]], str]:
    if fitz is None:
        return "", [], "PyMuPDF nav pieejams."
    pages = []
    chunks = []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for i, page in enumerate(doc, start=1):
            text = page.get_text("text") or ""
            text = re.sub(r"\n{3,}", "\n\n", text)
            pages.append({"page": i, "text": text, "chars": len(text)})
            chunks.append(f"--- PAGE {i} ---\n{text}")
        full = "\n\n".join(chunks)
        if len(full) > max_chars:
            full = full[:max_chars] + "\n\n[PDF konteksts saīsināts garuma dēļ.]"
        return full, pages, ""
    except Exception as e:
        return "", [], str(e)


def infer_discipline_from_filename(name: str) -> str:
    m = re.search(r"_([A-ZĀČĒĢĪĶĻŅŠŪŽ]{2,}(?:-[A-ZĀČĒĢĪĶĻŅŠŪŽ]{2,})?)_", name)
    if m:
        return m.group(1)
    parts = name.split("_")
    for p in parts:
        if re.fullmatch(r"[A-Z]{2,}(?:-[A-Z]{2,})?", p):
            return p
    return ""


def infer_document_role(name: str) -> str:
    n = name.lower()
    if any(x in n for x in ["spec", "specification", "ms_"]):
        return "specification"
    if any(x in n for x in ["general", "vispar", "vispār", "gd_"]):
        return "general_data"
    if any(x in n for x in ["profile", "profils"]):
        return "profile"
    if any(x in n for x in ["site", "plan", "plāns", "layout"]):
        return "plan"
    if any(x in n for x in ["isometry", "isomet"]):
        return "isometry"
    if any(x in n for x in ["description", "aprakst", "td_"]):
        return "description"
    return "unknown"


def score_example(
    row: pd.Series,
    pdf_name: str,
    pdf_text_sample: str,
    family: str,
    doc_role: str,
    discipline: str,
    project_code: str,
) -> int:
    """Novērtē globālā indeksa piemēra atbilstību konkrētajam auditam.

    Project_code nekad nav filtrs. Citu projektu zelta piemēri vienmēr ir
    pieejami. Tā paša projekta piemēram ir tikai neliels izšķirošais bonuss.
    """
    score = 0
    if clean_text(row.get("normalized_family")) == family:
        score += 100

    if doc_role and clean_text(row.get("document_role")) == doc_role:
        score += 25

    row_discipline = clean_text(
        row.get("discipline_final") or row.get("discipline")
    )
    if discipline and row_discipline.lower() == discipline.lower():
        score += 20

    tf = clean_text(row.get("target_file")).lower()
    if discipline and discipline.lower() in tf:
        score += 5

    txt = clean_text(row.get("target_text"))
    if txt and len(txt) > 3 and txt.lower() in pdf_text_sample.lower():
        score += 20

    # Izcelsmes projekts ir tikai vājš prioritātes bonuss, nevis robeža.
    row_project = normalize_project_code(clean_text(row.get("project_code")))
    wanted_project = normalize_project_code(project_code)
    if wanted_project and row_project == wanted_project:
        score += 5

    return score


def select_examples(
    index_df: pd.DataFrame,
    family: str,
    pdf_name: str,
    pdf_text: str,
    max_examples: int,
    project_code: str = "",
) -> List[Dict[str, str]]:
    """Atlasa piemērus no visa globālā indeksa.

    Atlase nekad netiek ierobežota ar auditējamo projektu. Ja indeksā ir
    piemēri no vairākiem projektiem, priekšroka tiek dota kvalitatīvi
    atbilstošiem un vienlaikus dažādu projektu piemēriem.
    """
    if index_df.empty or max_examples <= 0:
        return []

    doc_role = infer_document_role(pdf_name)
    discipline = infer_discipline_from_filename(pdf_name)
    fam_df = index_df[index_df["normalized_family"].eq(family)].copy()
    if fam_df.empty:
        return []

    sample = pdf_text[:10000]
    fam_df["_score"] = fam_df.apply(
        lambda r: score_example(
            r,
            pdf_name,
            sample,
            family,
            doc_role,
            discipline,
            project_code,
        ),
        axis=1,
    )
    fam_df["_project_key"] = fam_df["project_code"].map(
        lambda value: normalize_project_code(clean_text(value)) or "GLOBAL"
    )
    fam_df = fam_df.sort_values(
        ["_score", "_project_key", "note_id"],
        ascending=[False, True, True],
    )

    # Vispirms paņemam labāko piemēru no katra pieejamā projekta.
    selected_indices: List[Any] = []
    for _, group in fam_df.groupby("_project_key", sort=False):
        if len(selected_indices) >= max_examples:
            break
        selected_indices.append(group.index[0])

    # Atlikušās vietas aizpildām ar kopumā labākajiem piemēriem.
    if len(selected_indices) < max_examples:
        for row_index in fam_df.index:
            if row_index in selected_indices:
                continue
            selected_indices.append(row_index)
            if len(selected_indices) >= max_examples:
                break

    selected_df = fam_df.loc[selected_indices].sort_values(
        "_score", ascending=False
    )

    examples: List[Dict[str, str]] = []
    for _, row in selected_df.iterrows():
        examples.append({
            "note_id": clean_text(row.get("note_id")),
            "family": clean_text(row.get("normalized_family")),
            "scenario": clean_text(row.get("normalized_scenario")),
            "target_area": clean_text(row.get("target_area")),
            "target_text": clean_text(row.get("target_text")),
            "comment_text": clean_text(row.get("comment_text")),
            "issue_type": clean_text(row.get("issue_type")),
            "comparison_evidence": clean_text(
                row.get("comparison_evidence")
            ),
            "project_code": clean_text(row.get("project_code")),
            "source_path": clean_text(row.get("source_path")),
        })
    return examples


def make_negative_rules(feedback_df: pd.DataFrame, max_rules: int = 20) -> List[str]:
    if feedback_df.empty:
        return []
    rules = []
    for _, r in feedback_df.tail(max_rules).iterrows():
        reason = clean_text(r.get("reject_reason") or r.get("reason") or r.get("noraidīšanas iemesls"))
        text = clean_text(r.get("target_text") or r.get("title") or r.get("comment_text"))
        do_not = clean_text(
            r.get("do_not_show_similar")
            or r.get("turpmāk līdzīgas piezīmes nerādīt")
        ).lower()
        is_reusable = do_not in {"true", "1", "yes", "jā", "ja"}
        if is_reusable and (reason or text):
            rules.append(f"Nerādīt līdzīgas piezīmes: {text}. Iemesls: {reason}.")
    return rules


def get_openai_client():
    if OpenAI is None:
        st.error("OpenAI Python bibliotēka nav pieejama.")
        return None
    api_key = get_secret("OPENAI_API_KEY", "openai_api_key")
    if not api_key:
        st.error("Nav atrasts OPENAI_API_KEY Streamlit secrets.")
        return None
    return OpenAI(api_key=api_key)


def strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        return text[first:last + 1]
    return text



def extract_specific_values(text: str) -> List[str]:
    """Atrod salīdzinājumam izmantojamas konkrētas vērtības/kodus.

    Heiristika nav domāta tehniskai validācijai. Tā tikai palīdz atmest AI
    formulējumus, kuros teikts "neatbilst", bet nav nosauktas abas puses.
    """
    text = clean_text(text)
    values: List[str] = []

    for match in re.findall(r'["“”\']([^"“”\']{2,120})["“”\']', text):
        value = clean_text(match)
        if value:
            values.append(value.lower())

    patterns = [
        r"\b(?:DN|D|Ø)\s*\d{2,4}\b",
        r"\b\d+(?:[.,]\d+)?\s*(?:mm|cm|m|m²|m2|m³|m3|MPa|kPa|bar|kW|W|V|A|l/s|m3/h)\b",
        r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b",
        r"\b(?:REV|R|V)\s*[-_.]?\s*\d+[A-Z]?\b",
        r"\b[A-Z]{2,}(?:[-_][A-Z0-9]{2,})+\b",
        r"\b[A-Z]{2,}\s*(?:SN\d+|SDR\d+|PN\d+)\b",
        r"\b(?:PVC|PP|PE|HDPE|LDPE|BETONS|TĒRAUDS|ČUGUNS)\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            value = clean_text(match)
            if value:
                values.append(value.lower())

    # Saglabā secību, izmetot dublikātus.
    return list(dict.fromkeys(values))


def _quoted_values(text: str) -> List[str]:
    return [
        clean_text(x)
        for x in re.findall(r'["“”\']([^"“”\']{1,160})["“”\']', clean_text(text))
        if clean_text(x)
    ]


def _value_kind(value: str) -> str:
    value = clean_text(value)
    compact = re.sub(r"\s+", "", value)
    if re.fullmatch(r"\d{4}\s+\d{3}\s+\d{4}", value):
        return "cadastral_number"
    if re.fullmatch(r"RWC\d+(?:[-_][A-Z0-9]+)+", value, flags=re.I):
        return "document_code"
    if re.fullmatch(r"C\s*\d+(?:[-–]\d+)+", value, flags=re.I):
        return "object_code"
    if re.fullmatch(r"(?:DN|D|Ø)\s*\d{2,4}", value, flags=re.I):
        return "diameter"
    if re.fullmatch(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", value):
        return "date"
    if re.fullmatch(r"\d+(?:[.,]\d+)?\s*(?:mm|cm|m|m²|m2|m³|m3|MPa|Mpa|kPa|Kpa|bar|kW|Kw|W|V|A|l/s|m3/h)", value, flags=re.I):
        return "technical_value"
    if re.fullmatch(r"[A-Z]{2,}(?:[-_][A-Z0-9]{2,})+", compact, flags=re.I):
        return "code"
    return "text"


def _candidate_says_no_issue(text: str) -> bool:
    low = clean_text(text).lower()
    phrases = [
        "nav neatbilstības",
        "neatbilstība nav konstatēta",
        "atšķirība nav konstatēta",
        "abi teksti sakrīt",
        "vērtības sakrīt",
        "vērtības ir vienādas",
        "kļūda nav konstatēta",
        "pretruna nav konstatēta",
    ]
    return any(p in low for p in phrases)


def _comparison_types_conflict(text: str) -> bool:
    values = _quoted_values(text)
    if len(values) < 2:
        return False
    kinds = [_value_kind(v) for v in values[:4]]
    known = [k for k in kinds if k != "text"]
    if len(known) < 2:
        return False
    # Acīmredzami nesalīdzināmi datu lauki nedrīkst kļūt par neatbilstību.
    incompatible = {
        frozenset({"cadastral_number", "object_code"}),
        frozenset({"cadastral_number", "document_code"}),
        frozenset({"date", "document_code"}),
        frozenset({"diameter", "date"}),
    }
    return any(frozenset({a, b}) in incompatible for a in known for b in known if a != b)


ANCHOR_CODE_PATTERNS = [
    r"\b[A-Za-z]{1,5}-[A-Za-z0-9]{1,8}(?:\.[A-Za-z0-9]{1,8})?\b",
    r"\b(?:EI|REI|EW)\s*-?\s*\d{2,3}\b",
    r"\b\d{4}\b",
]

GENERIC_NOTE_PHRASES = [
    "nav saskaņots starp",
    "nav saskaņota starp",
    "nav saskaņoti starp",
    "ir vienāds, bet",
    "atšķiras starp dokumentiem",
    "nav saskaņots ar",
]


def extract_anchor_tokens(text: str) -> List[str]:
    text = clean_text(text)
    out: List[str] = []
    seen = set()

    def add(value: str) -> None:
        value = clean_text(value)
        if not value:
            return
        value = re.sub(r"\s+", " ", value).strip(" ,;")
        if len(value) < 2 or len(value) > 160:
            return
        key = value.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(value)

    for x in re.findall(r'["“”\']([^"“”\']{2,160})["“”\']', text):
        add(x)
    for pattern in ANCHOR_CODE_PATTERNS:
        for x in re.findall(pattern, text, flags=re.I):
            add(x)
    return out


def infer_target_text_from_candidate(c: Dict[str, Any]) -> str:
    existing = clean_text(c.get("target_text"))
    if existing and existing != "MANUAL_PLACEMENT_REQUIRED":
        return existing
    pool = " | ".join([
        clean_text(c.get("target_area")),
        clean_text(c.get("where")),
        clean_text(c.get("designer_note")),
        clean_text(c.get("comment_text")),
        clean_text(c.get("problem")),
        clean_text(c.get("evidence")),
    ])
    anchors = extract_anchor_tokens(pool)
    return anchors[0] if anchors else ""


def build_target_text_candidates(c: Dict[str, Any]) -> List[str]:
    seen = set()
    candidates: List[str] = []

    def add(value: str) -> None:
        value = clean_text(value)
        if not value or value == "MANUAL_PLACEMENT_REQUIRED":
            return
        value = re.sub(r"\s+", " ", value).strip(" ,;")
        if len(value) < 2 or len(value) > 160:
            return
        key = value.casefold()
        if key in seen:
            return
        seen.add(key)
        candidates.append(value)

    add(c.get("target_text"))
    for field in ["target_area", "where", "designer_note", "comment_text", "problem", "evidence"]:
        txt = clean_text(c.get(field))
        if not txt:
            continue
        for x in re.findall(r'["“”\']([^"“”\']{2,160})["“”\']', txt):
            add(x)
        for x in extract_anchor_tokens(txt):
            add(x)
    return candidates


def note_is_too_generic(text: str) -> bool:
    text = clean_text(text)
    if not text:
        return True
    low = text.lower()
    specifics = extract_specific_values(text)
    quoted = _quoted_values(text)
    if len(specifics) >= 2 or len(quoted) >= 2:
        return False
    return any(p in low for p in GENERIC_NOTE_PHRASES)


def choose_best_comment_text(c: Dict[str, Any]) -> str:
    note = clean_text(c.get("designer_note") or c.get("comment_text"))
    problem = clean_text(c.get("problem") or c.get("evidence"))
    if not note:
        return problem
    if note_is_too_generic(note) and problem:
        return problem
    note_specifics = len(extract_specific_values(note)) + len(_quoted_values(note))
    prob_specifics = len(extract_specific_values(problem)) + len(_quoted_values(problem))
    if problem and prob_specifics > note_specifics:
        return problem
    return note


def row_anchor_candidates(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    seen = set()

    def add(value: str) -> None:
        value = clean_text(value)
        if not value or value == "MANUAL_PLACEMENT_REQUIRED":
            return
        value = re.sub(r"\s+", " ", value).strip(" ,;")
        if len(value) < 2 or len(value) > 160:
            return
        key = value.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(value)

    raw_candidates = clean_text(row.get("target_text_candidates"))
    if raw_candidates:
        for part in raw_candidates.split("||"):
            add(part)
    add(row.get("target_text"))
    add(row.get("target_area"))
    for field in ["comment_text", "comparison_evidence"]:
        for x in extract_anchor_tokens(clean_text(row.get(field))):
            add(x)
    return out


def find_best_anchor_on_page(page: Any, row: Dict[str, Any]) -> Tuple[str, List[Any]]:
    for candidate in row_anchor_candidates(row):
        try:
            matches = page.search_for(candidate, quads=True)
        except Exception:
            matches = []
        if matches:
            if len(candidate) <= 3:
                matches = matches[:1]
            return candidate, matches
    return "", []


def normalize_candidate(c: Dict[str, Any], family: str) -> Dict[str, Any]:
    """Normalizē kandidātu un sagatavo konkrētus PDF enkurtekstus."""
    out = dict(c)
    out["family"] = family
    problem = clean_text(out.get("problem") or out.get("evidence"))
    note = clean_text(out.get("designer_note") or out.get("comment_text"))
    if not problem:
        problem = note
    out["problem"] = problem
    out["designer_note"] = clean_text(note or problem)
    inferred_target = infer_target_text_from_candidate(out)
    if inferred_target:
        out["target_text"] = inferred_target
    elif not clean_text(out.get("target_text")):
        out["target_text"] = "MANUAL_PLACEMENT_REQUIRED"
    out["target_text_candidates"] = build_target_text_candidates(out)
    return out


def candidate_is_too_vague(c: Dict[str, Any]) -> bool:
    problem = clean_text(c.get("problem"))
    note = clean_text(c.get("designer_note") or c.get("comment_text"))
    evidence = clean_text(c.get("evidence"))
    target_area = clean_text(c.get("target_area") or c.get("where"))
    text = " ".join([clean_text(c.get("title")), problem, note, evidence]).lower()

    if not note and not problem:
        return True
    if _candidate_says_no_issue(text):
        return True

    speculative_phrases = [
        "nav skaidrs, vai",
        "nav zināms, vai",
        "iespējams, ka",
        "varētu būt",
        "var neatbilst",
        "var nebūt",
        "iespējama neatbilstība",
        "nepieciešams pārbaudīt",
        "jāpārbauda ar citiem",
    ]
    if any(p in text for p in speculative_phrases):
        return True

    vague_phrases = [
        "dažādos dokumentos",
        "citiem projekta dokumentiem",
        "jāsaskaņo ar citiem",
        "pārbaudīt un saskaņot",
        "pilnībā saskaņots",
        "nav pilnībā saskaņots",
    ]
    comparison_phrases = [
        " neatbilst ", " nesakrīt ", " atšķiras ", " pretrunā ",
        " savukārt ", " salīdzinot ar ", " norādīts citādi ",
    ]
    has_vague = any(p in text for p in vague_phrases)
    has_comparison = any(p in f" {text} " for p in comparison_phrases)
    specifics = extract_specific_values(" ".join([problem, note, evidence]))
    has_source_location = bool(target_area) or any(
        source in text
        for source in [
            "titullauk", "faila nosauk", "galvenajā tekst", "tabulā", "profilā",
            "plānā", "site plan", "specifikācijā", "aprakstā", "rasējumā", "lapā",
        ]
    )

    if has_comparison and (len(specifics) < 2 or not has_source_location):
        return True
    if has_vague and len(specifics) < 2:
        return True
    if _comparison_types_conflict(" ".join([problem, note, evidence])):
        return True

    # B_lv_en salīdzinājumā identiski citāti nav kļūda.
    if clean_text(c.get("family")) == "B_lv_en":
        values = _quoted_values(problem)
        if len(values) >= 2:
            a = re.sub(r"\s+", " ", values[0]).strip().casefold()
            b = re.sub(r"\s+", " ", values[1]).strip().casefold()
            if a == b:
                return True

    return False


def make_candidate_id(c: Dict[str, Any], audit_run_id: str, ordinal: int) -> str:
    raw = "|".join([
        audit_run_id,
        clean_text(c.get("source_pdf_rel_path") or c.get("source_pdf")),
        clean_text(c.get("family")),
        clean_text(c.get("target_page")),
        clean_text(c.get("target_text")),
        clean_text(c.get("problem")),
        str(ordinal),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def clear_review_widget_state() -> None:
    prefixes = ("designer_note_", "decision_", "reject_reason_", "do_not_show_")
    for key in list(st.session_state.keys()):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            del st.session_state[key]


def detect_unit_case_candidates(pdf_item: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deterministiski atrod biežākās SI mērvienību reģistra kļūdas."""
    replacements = {
        "Mpa": "MPa",
        "Kpa": "kPa",
        "Kw": "kW",
    }
    out: List[Dict[str, Any]] = []
    for page_data in pdf_item.get("pages", []) or []:
        page_no = int(page_data.get("page") or 1)
        page_text = str(page_data.get("text") or "")
        for wrong, correct in replacements.items():
            if re.search(rf"(?<![A-Za-z]){re.escape(wrong)}(?![A-Za-z])", page_text):
                out.append({
                    "title": f"Mērvienības simbola pieraksts: {wrong}",
                    "where": f"{page_no}. lapa",
                    "target_page": page_no,
                    "target_area": "teksts",
                    "target_text": wrong,
                    "status": "unit_symbol_case_error",
                    "problem": f"{page_no}. lapā mērvienības simbols norādīts kā “{wrong}”; pareizais SI pieraksts ir “{correct}”.",
                    "designer_note": f"{page_no}. lapā mērvienības simbols norādīts kā “{wrong}”; pareizais SI pieraksts ir “{correct}”.",
                    "issue_type": "unit_symbol_case_error",
                    "severity": "low",
                    "markup_type": "highlight",
                    "placement_confidence": "exact",
                    "evidence": wrong,
                    "family": "A_text_language",
                })
    return out


def call_ai_for_family(
    client,
    model: str,
    pdf_name: str,
    pdf_text: str,
    family: str,
    examples: List[Dict[str, str]],
    negative_rules: List[str],
    max_candidates: int,
) -> Tuple[List[Dict[str, Any]], str]:
    instr = FAMILY_INSTRUCTIONS.get(family, {"name": family, "look_for": "", "report_if": "", "do_not_report": ""})
    system = (
        "Tu esi būvprojekta audita asistents. Ģenerē tikai pierādāmas piezīmes. "
        "Neizdomā faktus. Līdzīgie audit_examples piemēri ir globāli zelta paraugi no dažādiem projektiem un ir izmantojami jebkura projekta auditā tikai FORMULĒJUMAM un kļūdu tipa izpratnei — "
        "nekad nepārnes no tiem konkrētus faktus, diametrus, materiālus, failu nosaukumus, projektu kodus vai dokumentu atsauces uz jaunu piezīmi. "
        "Piezīmi drīkst ģenerēt tikai tad, ja auditējamā PDF tekstā ir konkrēts pierādījums. "
        "Ja piezīme salīdzina divas vērtības, skaidri nosauc abas vērtības un to avotus. "
        "Nedrīkst rakstīt vispārīgi: 'var neatbilst', 'dažādos dokumentos', 'jāsaskaņo ar citiem dokumentiem', "
        "ja nav precīzi nosaukts, kas tieši kam neatbilst. "
        "Ja nav pietiekama pierādījuma, atgriez tukšu candidates sarakstu. "
        "Raksti īsi. Neraksti sekas, riskus vai risinājuma instrukcijas. "
        "PDF komentāram vajag tikai konkrētu konstatējumu: kas dokumentā redzams un ar ko tas nesakrīt. "
        "designer_note jābūt tik garai, cik nepieciešams konkrētās kļūdas vai nesakritības nepārprotamam aprakstam, bet bez lieka konteksta. "
        "Nelieto virsrakstus Kāpēc tas ir svarīgi, Ieteikums vai Risinājums. "
        "Virsrakstam, problem, designer_note, target_text un evidence obligāti jāapraksta viena un tā pati problēma. "
        "Ja secini, ka vērtības sakrīt vai neatbilstības nav, kandidātu neradi. "
        "Nesalīdzini atšķirīgus datu laukus, piemēram, kadastra numuru ar objekta apzīmējumu. "
        "Atbildi tikai derīgā JSON formātā."
    )
    user = {
        "task": "Analizē vienu PDF dokumentu un atrodi piezīmes konkrētajā kļūdu ģimenē.",
        "pdf_file": pdf_name,
        "family": family,
        "family_instruction": instr,
        "max_candidates": max_candidates,
        "precision_rules": [
            "Problēmas aprakstā jābūt konkrētai kļūdai, nevis vispārīgam riskam.",
            "Ja raksti, ka A neatbilst B, obligāti nosauc A vērtību/tekstu un B vērtību/tekstu.",
            "Ja salīdzināmais avots nav iekļauts auditējamā PDF tekstā, neatsaucies uz šo avotu kā uz pierādījumu.",
            "Nedrīkst pārņemt faktus no similar_positive_examples; tie ir tikai stila un tipoloģijas piemēri.",
            "Frāzes 'var neatbilst', 'var nebūt saskaņots', 'jāpārbauda ar citiem dokumentiem' ir atļautas tikai tad, ja blakus ir konkrēts nepareizais teksts un konkrēts salīdzināmais teksts.",
            "designer_note jābūt lietojamai bez failu atvēršanas: tajā jāmin abas konkrētās vērtības/teksti un vietas/avoti.",
            "designer_note nav cieta zīmju vai teikumu limita; izmanto tikai tik daudz teksta, cik vajadzīgs konkrētās kļūdas vai nesakritības pilnam aprakstam.",
            "designer_note nedrīkst saturēt: Kāpēc tas ir svarīgi, Ieteikums, Risinājums, Lūdzu pārbaudīt, Lūdzu saskaņot, risku vai seku aprakstu.",
            "title, problem, designer_note, target_text un evidence apraksta tikai vienu un to pašu kļūdu.",
            "target_text obligāti ir precīzs, īss, burtiski auditējamā PDF lapā atrodams teksta fragments; izvēlies elementa kodu, vērtību, datumu vai īsu tabulas tekstu, nevis pārfrāzētu teikumu.",
            "Ja konkrētu burtiski atrodamu target_text nevar nosaukt, kandidātu neatgriez, izņemot gadījumus, kuros kļūda attiecas uz visu lapu vai trūkstošu saturu.",
            "Starpdokumentu nesakritībā designer_note obligāti nosauc target faila konkrēto vērtību un salīdzināmā faila konkrēto konfliktējošo vērtību, kā arī abus failus vai vietas.",
            "Neraksti tikai 'nav saskaņots starp dokumentiem', 'ir vienāds, bet apraksts atšķiras' vai līdzīgu vispārīgu apgalvojumu bez abām precīzajām vērtībām.",
            "Ja abi salīdzinātie teksti vai skaitļi sakrīt, kandidātu neatgriez.",
            "Neizmanto spekulatīvas frāzes: nav skaidrs vai, iespējams, varētu būt.",
            "Salīdzini tikai viena tipa laukus: kodu ar kodu, datumu ar datumu, diametru ar diametru, kadastra numuru ar kadastra numuru.",
        ],
        "similar_positive_examples": examples,
        "negative_rules_do_not_repeat": negative_rules,
        "pdf_text": pdf_text,
        "required_json_schema": {
            "candidates": [
                {
                    "title": "īss piezīmes virsraksts",
                    "where": "lapa un zona/tabula/teksts",
                    "target_page": 1,
                    "target_area": "zona, tabula vai vieta dokumentā",
                    "target_text": "obligāts īss, precīzs un burtiski target lapā atrodams teksts, ko iezīmēt PDF; MANUAL_PLACEMENT_REQUIRED tikai visas lapas vai trūkstoša satura kļūdai",
                    "status": "kļūdas tips vai risks",
                    "problem": "precīzi apraksti kļūdu: norādi nepareizo tekstu/vērtību pēdiņās, salīdzināmo pareizo vai konfliktējošo tekstu/vērtību pēdiņās un avotu; bez vispārīgām frāzēm",
                    "why_important": "atstāj tukšu; neraksti sekas vai riska aprakstu",
                    "designer_note": "konkrēts PDF komentārs bez cieta garuma limita: apraksti tikai konstatēto kļūdu vai nesakritību, norādot vajadzīgās vērtības/tekstus un to vietas; bez pamatojuma, ieteikuma, riska, sekām vai risinājuma",
                    "comparison_files": "salīdzināmo failu nosaukumi; tukšs, ja salīdzinājums ir vienā failā",
                    "comparison_pages": "salīdzināmo lapu numuri; tukšs, ja nav zināmi",
                    "issue_type": "normalizēts issue_type",
                    "severity": "low|medium|high",
                    "markup_type": "highlight|rectangle|sticky_note|page_note",
                    "placement_confidence": "exact|approximate|manual_needed",
                    "evidence": "precīzs pierādījums: citāts vai vērtības no auditējamā PDF; ja ir salīdzinājums, norādi abas puses",
                }
            ]
        },
    }
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(strip_json_fences(content))
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            return [], "AI JSON laukam candidates nav saraksta tips."
        cleaned_candidates = []
        for c in candidates:
            if isinstance(c, dict):
                normalized = normalize_candidate(c, family)
                if not candidate_is_too_vague(normalized):
                    cleaned_candidates.append(normalized)
        return cleaned_candidates, ""
    except Exception as e:
        return [], str(e)


def call_ai_for_cross_document_family(
    client,
    model: str,
    pdf_items: List[Dict[str, Any]],
    examples: List[Dict[str, str]],
    negative_rules: List[str],
    max_candidates: int,
) -> Tuple[List[Dict[str, Any]], str]:
    """J ģimeni palaiž vienā pieprasījumā ar vairāku PDF skaidri marķētu kontekstu."""
    family = "J_cross_document_traceability"
    docs = []
    total_chars = 0
    for item in pdf_items:
        rel_path = clean_text(item.get("rel_path") or item.get("name"))
        content = str(item.get("text") or "")[:18000]
        block = f"===== FILE: {rel_path} =====\n{content}"
        if total_chars + len(block) > 100000:
            break
        docs.append(block)
        total_chars += len(block)
    if len(docs) < 2:
        return [], "J_cross_document_traceability vajag vismaz divus nolasītus PDF."

    system = (
        "Tu pārbaudi izsekojamību starp vairākiem būvprojekta PDF. "
        "Ziņo tikai par konkrētu viena un tā paša elementa pretrunu starp vismaz diviem nosauktiem failiem. "
        "Obligāti nosauc abus failus, lapas, abas vērtības un precīzos citātus. "
        "Dažādi mezglu kodi paši par sevi nav kļūda. Neraksti 'nav skaidrs, vai', 'iespējams' vai 'varētu'. "
        "Ja nav pierādāmas pretrunas, atgriez tukšu candidates sarakstu. "
        "problem un designer_note apraksta vienu un to pašu konstatējumu. Atbildi tikai JSON."
    )
    payload = {
        "family": family,
        "max_candidates": max_candidates,
        "rules": [
            "Salīdzini tikai vienu un to pašu elementu vai dokumenta lauku.",
            "Katrai piezīmei norādi target_file, comparison_files, target_page un comparison_pages.",
            "target_text obligāti ir īss, precīzs un burtiski target faila lapā atrodams teksta fragments, kuru var iezīmēt.",
            "comparison_target_text obligāti ir precīzs citāts vai vērtība salīdzināmajā failā.",
            "problem un designer_note obligāti nosauc abas konfliktējošās vērtības, abus failus un vietas; vispārīgas frāzes bez vērtībām nav pieļaujamas.",
            "Ja target_text nav burtiski atrodams target failā vai abas konfliktējošās vērtības nevar nosaukt, kandidātu neradi.",
            "Ja vērtības sakrīt, piezīmi neradi.",
            "Nesalīdzini kadastra numuru ar objekta kodu vai citus atšķirīgus datu tipus.",
        ],
        "similar_positive_examples": examples,
        "negative_rules_do_not_repeat": negative_rules,
        "documents": "\n\n".join(docs),
        "required_json_schema": {
            "candidates": [{
                "title": "īss virsraksts",
                "target_file": "fails, kur tiks ievietota piezīme",
                "target_page": 1,
                "target_area": "vieta target failā",
                "target_text": "precīzs target faila teksts",
                "comparison_files": "otrs fails vai faili",
                "comparison_pages": "otra faila lapas",
                "comparison_target_text": "precīzs teksts otrā failā",
                "problem": "konkrēta pretruna ar abām vērtībām un avotiem",
                "designer_note": "tas pats konkrētais konstatējums bez riska un ieteikuma",
                "issue_type": "cross_document_mismatch",
                "severity": "low|medium|high",
                "markup_type": "highlight|page_note",
                "placement_confidence": "exact|approximate|manual_needed",
                "evidence": "abi citāti",
            }]
        },
    }
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        data = json.loads(strip_json_fences(resp.choices[0].message.content or "{}"))
        candidates = data.get("candidates", [])
        if not isinstance(candidates, list):
            return [], "AI JSON laukam candidates nav saraksta tips."
        cleaned = []
        valid_paths = {clean_text(x.get("rel_path") or x.get("name")) for x in pdf_items}
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            c = normalize_candidate(raw, family)
            target_file = clean_text(c.get("target_file"))
            if target_file not in valid_paths:
                continue
            if not clean_text(c.get("comparison_files")):
                continue
            if not candidate_is_too_vague(c):
                cleaned.append(c)
        return cleaned, ""
    except Exception as e:
        return [], str(e)


def candidate_to_export_row(c: Dict[str, Any], idx: int, pdf_name: str, discipline: str) -> Dict[str, Any]:
    target_text = clean_text(c.get("target_text")) or "MANUAL_PLACEMENT_REQUIRED"
    placement = clean_text(c.get("placement_confidence")) or "manual_needed"
    markup = clean_text(c.get("markup_type"))
    if not markup:
        markup = "highlight" if placement == "exact" and target_text != "MANUAL_PLACEMENT_REQUIRED" else "page_note"
    problem = clean_text(c.get("problem"))
    evidence = clean_text(c.get("evidence"))
    comparison_evidence = problem or evidence
    page = clean_text(c.get("target_page")) or "1"
    best_comment = choose_best_comment_text(c)
    target_candidates = c.get("target_text_candidates") or build_target_text_candidates(c)
    return {
        "note_id": clean_text(c.get("note_id")) or f"AI-{datetime.now().strftime('%Y%m%d%H%M%S')}-{idx:03d}",
        "Nr": idx,
        "discipline": discipline,
        "target_file": pdf_name,
        "target_page": page,
        "target_area": clean_text(c.get("target_area") or c.get("where")),
        "target_text": target_text,
        "target_text_candidates": " || ".join(target_candidates),
        "comment_text": shorten_pdf_comment(best_comment or comparison_evidence),
        "issue_type": clean_text(c.get("issue_type") or c.get("family")),
        "severity": clean_text(c.get("severity")) or "medium",
        "comparison_files": clean_text(c.get("comparison_files")),
        "comparison_pages": clean_text(c.get("comparison_pages")),
        "comparison_evidence": comparison_evidence,
        "markup_type": markup,
        "placement_confidence": placement,
        "status": "accepted_candidate",
    }


def candidate_to_rejected_row(c: Dict[str, Any], idx: int, pdf_name: str, reason: str, do_not_show: bool) -> Dict[str, Any]:
    comparison_evidence = clean_text(c.get("problem") or c.get("evidence"))
    return {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": pdf_name,
        "family": clean_text(c.get("family")),
        "title": clean_text(c.get("title")),
        "target_page": clean_text(c.get("target_page")),
        "target_area": clean_text(c.get("target_area") or c.get("where")),
        "target_text": clean_text(c.get("target_text")),
        "comment_text": clean_text(c.get("designer_note") or c.get("comment_text") or comparison_evidence),
        "issue_type": clean_text(c.get("issue_type")),
        "reject_reason": reason,
        "do_not_show_similar": bool(do_not_show),
        "status": "rejected_by_user",
        "candidate_index": idx,
    }




def shorten_pdf_comment(text: str) -> str:
    """Notīra PDF komentāru, nepārraujot konstatējuma tekstu.

    Netiek lietots simbolu limits un teksts netiek griezts teikuma vidū.
    Tiek atmestas tikai skaidri nodalītas papildu sadaļas, kas sākas ar
    pamatojuma, seku vai risinājuma virsrakstu.
    """
    original = clean_text(text)
    if not original:
        return "Piezīme auditā."

    cleaned = re.sub(r"(?i)^komentārs\s*:\s*", "", original).strip()
    section_pattern = re.compile(
        r"(?i)(?:^|[\n\r]+|(?<=[.!?])\s+)(?:kāpēc tas ir svarīgi|ieteikums|risinājums|sekas|riski?|kā novērst)\s*:\s*"
    )
    match = section_pattern.search(cleaned)
    if match:
        candidate = cleaned[:match.start()].strip()
        # Saglabājam tikai gramatiski pilnu rezultātu; pretējā gadījumā atstājam oriģinālu.
        if candidate and candidate[-1:] in ".!?)]}”\"'":
            cleaned = candidate

    return cleaned or original or "Piezīme auditā."

def make_pdf_comment(row: Dict[str, Any]) -> str:
    comment = clean_text(row.get("comment_text")) or clean_text(row.get("comparison_evidence"))
    comment = shorten_pdf_comment(comment)
    area = clean_text(row.get("target_area"))
    page = clean_text(row.get("target_page"))
    prefix = area or (f"{page}. lapa" if page else "")
    if prefix and prefix.lower() not in comment.lower():
        comment = f"{prefix}: {comment}"
    return f"Komentārs:\n{comment}"


def safe_int_page(value: Any, page_count: int) -> int:
    txt = clean_text(value)
    m = re.search(r"\d+", txt)
    if not m:
        return 0
    page = int(m.group(0)) - 1
    if page < 0:
        page = 0
    if page >= page_count:
        page = page_count - 1
    return page


def add_page_note(page: Any, row: Dict[str, Any], comment: str) -> None:
    try:
        rect = page.rect
        point = fitz.Point(rect.x1 - 36, rect.y0 + 36)
        annot = page.add_text_annot(point, comment)
        annot.set_info(title="AI būvprojekta audits", content=comment)
        annot.update()
    except Exception:
        pass



def add_no_findings_banner(
    pdf_bytes: bytes,
) -> Tuple[Optional[bytes], List[Dict[str, Any]]]:
    """Pievieno parastu dzeltenu PDF piezīmes ikonu failam bez neatbilstībām.

    Statuss netiek zīmēts kā liels krāsains bloks. Tā vietā redzamās pirmās
    lapas augšējā labajā stūrī tiek ievietota standarta sticky-note anotācija.
    Uz tās uzspiežot, atveras pilns audita rezultāta teksts.
    """
    message = "Audita rezultātā piezīmes nav konstatētas."

    if fitz is None:
        return None, [{
            "status": "error",
            "message": "PyMuPDF/fitz nav pieejams Streamlit vidē.",
        }]

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(doc) == 0:
            doc.close()
            return None, [{
                "status": "error",
                "message": "PDF nav lapu.",
            }]

        page = doc[0]
        page_rotation = int(page.rotation)
        visual_rect = page.rect

        # Izvēlamies punktu redzamās lapas augšējā labajā stūrī.
        margin = max(
            18.0,
            min(42.0, min(visual_rect.width, visual_rect.height) * 0.02),
        )
        visual_point = fitz.Point(
            visual_rect.x1 - margin,
            visual_rect.y0 + margin,
        )

        # PyMuPDF anotāciju koordinātas ir nesašķiebtās lapas koordinātās.
        point = visual_point * page.derotation_matrix

        annot = page.add_text_annot(
            point,
            message,
            icon="Note",
        )
        annot.set_info(
            title="AI būvprojekta audits",
            subject="Audita rezultāts",
            content=message,
        )
        try:
            annot.set_colors(
                stroke=(1.0, 0.82, 0.0),
            )
        except Exception:
            pass
        annot.update()

        out = io.BytesIO()
        doc.save(out, garbage=4, deflate=True)
        doc.close()
        result = out.getvalue()

        verified = False
        verification_error = ""
        try:
            check_doc = fitz.open(stream=result, filetype="pdf")
            check_page = check_doc[0]
            for check_annot in check_page.annots() or []:
                info = check_annot.info or {}
                content = clean_text(info.get("content"))
                title = clean_text(info.get("title"))
                if (
                    content == message
                    and title == "AI būvprojekta audits"
                ):
                    verified = True
                    break
            check_doc.close()
        except Exception as exc:
            verification_error = str(exc)

        return result, [{
            "status": (
                "no_findings_note_verified"
                if verified
                else "no_findings_note_unverified"
            ),
            "target_page": 1,
            "message": message,
            "page_rotation": page_rotation,
            "verification_error": verification_error,
        }]

    except Exception as exc:
        return None, [{
            "status": "error",
            "message": (
                "Neizdevās pievienot audita rezultāta piezīmi: "
                f"{exc}"
            ),
        }]

def annotate_pdf_bytes(pdf_bytes: bytes, accepted_df: pd.DataFrame) -> Tuple[Optional[bytes], List[Dict[str, Any]]]:
    """Anotē PDF, primāri piesaistot piezīmi konkrētam, atrodamam teksta fragmentam."""
    report: List[Dict[str, Any]] = []
    if fitz is None:
        return None, [{"status": "error", "message": "PyMuPDF/fitz nav pieejams Streamlit vidē."}]
    if accepted_df is None or accepted_df.empty:
        return pdf_bytes, []
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as e:
        return None, [{"status": "error", "message": f"PDF nevar atvērt anotēšanai: {e}"}]

    for _, row_s in accepted_df.iterrows():
        row = {str(k): v for k, v in row_s.to_dict().items()}
        note_id = clean_text(row.get("note_id"))
        placement = clean_text(row.get("placement_confidence")).lower()
        page_index = safe_int_page(row.get("target_page"), len(doc))
        page = doc[page_index]
        comment = make_pdf_comment(row)
        status = "page_note"
        matches_count = 0
        used_anchor = ""
        error_text = ""
        try:
            used_anchor, matches = find_best_anchor_on_page(page, row)
            matches_count = len(matches)
            if matches:
                annot = page.add_highlight_annot(matches)
                annot.set_info(title="AI būvprojekta audits", content=comment)
                annot.update()
                status = "highlight_exact" if placement == "exact" else "highlight_found"
            else:
                add_page_note(page, row, comment)
                status = "text_not_found_page_note"
        except Exception as e:
            error_text = str(e)
            try:
                add_page_note(page, row, comment)
                status = "annotation_error_page_note"
            except Exception:
                status = "annotation_error"
        report.append({
            "note_id": note_id,
            "target_page": page_index + 1,
            "target_text": clean_text(row.get("target_text")),
            "used_anchor": used_anchor,
            "status": status,
            "matches": matches_count,
            "error": error_text,
        })

    out = io.BytesIO()
    try:
        doc.save(out, garbage=4, deflate=True)
    except Exception:
        out = io.BytesIO()
        doc.save(out)
    finally:
        doc.close()
    return out.getvalue(), report


def make_zip(
    accepted_df: pd.DataFrame,
    rejected_df: pd.DataFrame,
    review_df: pd.DataFrame,
    base_name: str,
    pdf_items: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    """Create export ZIP.

    v0.6 supports multiple selected PDFs. Accepted rows are split by target_file
    and each matching PDF is annotated separately.
    """
    bio = io.BytesIO()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_items = pdf_items or []

    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        acc_b = io.BytesIO()
        with pd.ExcelWriter(acc_b, engine="openpyxl") as writer:
            accepted_df.to_excel(writer, sheet_name="accepted_candidates", index=False)
        zf.writestr(f"accepted_candidates_{base_name}_{ts}.xlsx", acc_b.getvalue())

        # Rejected feedback files are only useful when the user actually rejects at least one note.
        # Do not create empty rejected_patterns files. They add noise and can confuse the workflow.
        if rejected_df is not None and not rejected_df.empty:
            rej_b = io.BytesIO()
            with pd.ExcelWriter(rej_b, engine="openpyxl") as writer:
                rejected_df.to_excel(writer, sheet_name="rejected_patterns", index=False)
            zf.writestr(f"rejected_patterns_{base_name}_{ts}.xlsx", rej_b.getvalue())
            zf.writestr(f"rejected_patterns_{base_name}_{ts}.json", rejected_df.to_json(orient="records", force_ascii=False, indent=2))

        rev_b = io.BytesIO()
        with pd.ExcelWriter(rev_b, engine="openpyxl") as writer:
            review_df.to_excel(writer, sheet_name="all_ai_notes_review", index=False)
        zf.writestr(f"all_ai_notes_review_{base_name}_{ts}.xlsx", rev_b.getvalue())

        # Annotate each selected PDF separately.
        all_reports: List[Dict[str, Any]] = []
        if pdf_items:
            for item in pdf_items:
                pdf_name = clean_text(item.get("name"))
                pdf_bytes = item.get("bytes")
                if not pdf_name or not pdf_bytes:
                    continue
                pdf_rel_path = clean_text(item.get("rel_path")) or pdf_name
                pdf_rows = pd.DataFrame()
                if accepted_df is not None and not accepted_df.empty and "target_file" in accepted_df.columns:
                    target_series = accepted_df["target_file"].astype(str).map(clean_text)
                    pdf_rows = accepted_df[target_series.eq(pdf_rel_path)].copy()
                    if pdf_rows.empty:
                        # Atpakaļsaderība vecākiem ierakstiem, kuros target_file bija tikai faila nosaukums.
                        pdf_rows = accepted_df[target_series.eq(pdf_name)].copy()
                if pdf_rows.empty:
                    annotated_pdf, pdf_report = add_no_findings_banner(pdf_bytes)
                else:
                    annotated_pdf, pdf_report = annotate_pdf_bytes(pdf_bytes, pdf_rows)
                safe_pdf_source = os.path.splitext(pdf_rel_path)[0]
                safe_pdf_base = re.sub(r"[^A-Za-z0-9_\-]+", "_", safe_pdf_source)[:100]
                if annotated_pdf:
                    zf.writestr(f"annotated_pdf_{safe_pdf_base}_{ts}.pdf", annotated_pdf)
                for r in pdf_report:
                    r["pdf_file"] = pdf_name
                    r["pdf_rel_path"] = clean_text(item.get("rel_path"))
                    all_reports.append(r)

        if all_reports:
            rep_b = io.BytesIO()
            with pd.ExcelWriter(rep_b, engine="openpyxl") as writer:
                pd.DataFrame(all_reports).to_excel(writer, sheet_name="pdf_markup_report", index=False)
            zf.writestr(f"pdf_markup_report_{base_name}_{ts}.xlsx", rep_b.getvalue())
    return bio.getvalue()




MANUAL_IMPORT_REQUIRED_COLUMNS = [
    "note_number",
    "source_file",
    "family",
    "issue_type",
    "target_page",
    "target_area",
    "target_text",
    "problem",
    "comment_text",
]

MANUAL_IMPORT_OPTIONAL_COLUMNS = [
    "comparison_file",
    "comparison_page",
    "comparison_text",
    "evidence",
    "severity",
    "markup_type",
    "placement_confidence",
    "active",
    "scope",
    "notes_for_ai",
]

MANUAL_IMPORT_COLUMNS = (
    MANUAL_IMPORT_REQUIRED_COLUMNS
    + MANUAL_IMPORT_OPTIONAL_COLUMNS
)


def parse_bool_value(value: Any, default: bool = True) -> bool:
    text = clean_text(value).casefold()
    if not text:
        return default
    if text in {"true", "1", "yes", "y", "jā", "ja", "x", "active"}:
        return True
    if text in {"false", "0", "no", "n", "nē", "ne", "inactive"}:
        return False
    return default


def normalize_manual_import_dataframe(
    dataframe: pd.DataFrame,
) -> Tuple[pd.DataFrame, List[str]]:
    """Validē un normalizē ChatGPT/manuāli sagatavotu piezīmju Excel."""
    errors: List[str] = []
    if dataframe is None or dataframe.empty:
        return pd.DataFrame(columns=MANUAL_IMPORT_COLUMNS), [
            "Excel failā nav piezīmju rindu."
        ]

    df = dataframe.copy()
    df.columns = [
        clean_text(column).casefold().replace(" ", "_")
        for column in df.columns
    ]

    aliases = {
        "nr": "note_number",
        "number": "note_number",
        "id": "note_number",
        "pdf": "source_file",
        "target_file": "source_file",
        "source_pdf": "source_file",
        "comparison_files": "comparison_file",
        "comparison_pages": "comparison_page",
        "page": "target_page",
        "where": "target_area",
        "designer_note": "comment_text",
        "comment": "comment_text",
        "title": "problem",
        "description": "problem",
        "comparison_evidence": "evidence",
    }
    df = df.rename(
        columns={
            column: aliases.get(column, column)
            for column in df.columns
        }
    )

    for column in MANUAL_IMPORT_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    missing = [
        column
        for column in MANUAL_IMPORT_REQUIRED_COLUMNS
        if column not in dataframe.rename(
            columns={
                original: aliases.get(
                    clean_text(original).casefold().replace(" ", "_"),
                    clean_text(original).casefold().replace(" ", "_"),
                )
                for original in dataframe.columns
            }
        ).columns
    ]
    if missing:
        errors.append(
            "Trūkst obligātās kolonnas: " + ", ".join(missing)
        )

    for column in df.columns:
        if column != "active":
            df[column] = df[column].map(clean_text)

    df["active"] = df["active"].map(
        lambda value: parse_bool_value(value, default=True)
    )
    df = df[df["active"]].copy()

    for row_index, row in df.iterrows():
        row_label = clean_text(row.get("note_number")) or str(row_index + 2)
        if not clean_text(row.get("source_file")):
            errors.append(
                f"Rindai {row_label} nav norādīts source_file."
            )
        if not clean_text(row.get("family")):
            errors.append(
                f"Rindai {row_label} nav norādīta family."
            )
        if not clean_text(row.get("comment_text")):
            errors.append(
                f"Rindai {row_label} nav norādīts comment_text."
            )
        if (
            not clean_text(row.get("problem"))
            and not clean_text(row.get("evidence"))
        ):
            errors.append(
                f"Rindai {row_label} nav problem vai evidence."
            )

    return df[MANUAL_IMPORT_COLUMNS].reset_index(drop=True), errors


def read_manual_import_excel(
    uploaded_file: Any,
) -> Tuple[pd.DataFrame, List[str]]:
    if uploaded_file is None:
        return pd.DataFrame(columns=MANUAL_IMPORT_COLUMNS), []

    try:
        raw = uploaded_file.getvalue()
        xls = pd.ExcelFile(io.BytesIO(raw))
        preferred = [
            "audit_notes_import",
            "manual_notes",
            "notes",
            "piezimes",
        ]
        sheet_name = next(
            (
                sheet
                for wanted in preferred
                for sheet in xls.sheet_names
                if clean_text(sheet).casefold() == wanted
            ),
            xls.sheet_names[0],
        )
        dataframe = pd.read_excel(
            io.BytesIO(raw),
            sheet_name=sheet_name,
            dtype=object,
        )
        return normalize_manual_import_dataframe(dataframe)
    except Exception as exc:
        return pd.DataFrame(columns=MANUAL_IMPORT_COLUMNS), [
            f"Excel failu neizdevās nolasīt: {exc}"
        ]


def _manual_pdf_name_key(value: Any) -> str:
    """Normalizē PDF nosaukumu manuālā Excel sasaistīšanai.

    Google Drive, pārlūks un ChatGPT lejupielādes vienam failam var pievienot
    tehniskus sufiksus, piemēram, ``(1)`` vai ``(2)``. Tie nav dokumenta
    identitātes daļa un sasaistē tiek ignorēti.
    """
    value = clean_text(value).replace("\\", "/")
    name = value.rsplit("/", 1)[-1]
    name = re.sub(r"\.pdf$", "", name, flags=re.I)
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)
    name = re.sub(r"\s*-\s*copy\s*$", "", name, flags=re.I)
    name = name.casefold()
    name = re.sub(r"[\s\-–—]+", "_", name)
    name = re.sub(r"[^0-9a-zāčēģīķļņšūž._]+", "", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name


def _manual_pdf_document_code_key(value: Any) -> str:
    """Izvelk stabilo dokumenta kodu no PDF faila nosaukuma.

    Piemēram, abi nosaukumi:
    - RWC2-02_SN_SA_ZZ_ZZ_TD_00001_ExplanatoryNoteEN_Rev.1.pdf
    - RWC2-02_SN_SA_ZZ_ZZ_TD_00001_ExplanatoryNoteLV_Rev.1.pdf

    tiek sasaistīti ar vienu kodu:
    RWC2-02_SN_SA_ZZ_ZZ_TD_00001
    """
    raw = clean_text(value).replace("\\", "/").rsplit("/", 1)[-1]
    raw = re.sub(r"\.pdf$", "", raw, flags=re.I)
    raw = re.sub(r"\s*\(\d+\)\s*$", "", raw)

    patterns = [
        # Pēc ciparu koda drīkst sekot "_" un aprakstošais faila nosaukums.
        r"\b(RWC\d+-\d+(?:_[A-Z0-9]+){5}_\d{4,6})(?=_|$)",
        r"\b(RWC\d+-\d+(?:_[A-Z0-9]+){5}_(?:RA|SP|MS|TD)_?\d{4,6})(?=_|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I)
        if match:
            return re.sub(
                r"[\s\-–—]+",
                "_",
                match.group(1).casefold(),
            )

    key = _manual_pdf_name_key(value)
    match = re.search(
        r"\b(rwc\d+_\d+(?:_[a-z0-9]+){5}_\d{4,6})(?=_|$)",
        key,
        flags=re.I,
    )
    return match.group(1).casefold() if match else ""

def match_manual_source_pdf(
    source_file: str,
    selected_pdf_items: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Atrod Excel source_file atbilstošo auditā izvēlēto PDF.

    Prioritāte:
    1) pilns relatīvais ceļš;
    2) precīzs faila nosaukums;
    3) normalizēts nosaukums bez lejupielādes sufiksa ``(1)/(2)``;
    4) stabilais dokumenta kods.
    """
    wanted = _canonical_drive_rel_path(source_file)
    if not wanted:
        return None

    exact = [
        item for item in selected_pdf_items
        if _canonical_drive_rel_path(
            clean_text(item.get("rel_path") or item.get("name"))
        ) == wanted
    ]
    if len(exact) == 1:
        return exact[0]

    wanted_name = wanted.rsplit("/", 1)[-1]
    by_name = [
        item for item in selected_pdf_items
        if _canonical_drive_rel_path(
            clean_text(item.get("name"))
        ) == wanted_name
    ]
    if len(by_name) == 1:
        return by_name[0]

    wanted_key = _manual_pdf_name_key(source_file)
    by_normalized_name = [
        item for item in selected_pdf_items
        if _manual_pdf_name_key(
            clean_text(item.get("name"))
        ) == wanted_key
        or _manual_pdf_name_key(
            clean_text(item.get("rel_path"))
        ) == wanted_key
    ]
    if len(by_normalized_name) == 1:
        return by_normalized_name[0]

    wanted_code = _manual_pdf_document_code_key(source_file)
    if wanted_code:
        by_document_code = [
            item for item in selected_pdf_items
            if _manual_pdf_document_code_key(
                clean_text(item.get("name"))
            ) == wanted_code
            or _manual_pdf_document_code_key(
                clean_text(item.get("rel_path"))
            ) == wanted_code
        ]
        if len(by_document_code) == 1:
            return by_document_code[0]

    partial = [
        item for item in selected_pdf_items
        if (
            wanted_key
            and (
                wanted_key in _manual_pdf_name_key(
                    clean_text(item.get("rel_path") or item.get("name"))
                )
                or _manual_pdf_name_key(
                    clean_text(item.get("rel_path") or item.get("name"))
                ) in wanted_key
            )
        )
    ]
    return partial[0] if len(partial) == 1 else None


def _manual_candidate_page_text(
    pdf_item: Dict[str, Any],
    target_page: Any,
) -> str:
    try:
        page_number = max(1, int(float(clean_text(target_page) or "1")))
    except Exception:
        page_number = 1

    pages = pdf_item.get("pages") or []
    for page in pages:
        try:
            if int(page.get("page") or 0) == page_number:
                return clean_text(page.get("text"))
        except Exception:
            continue

    return clean_text(pdf_item.get("text"))[:12000]


def _manual_exact_anchor_from_page(
    page_text: str,
    values: List[str],
) -> str:
    """Atrod īsu, burtiski lapā sastopamu teksta enkuru."""
    page_text = clean_text(page_text)
    if not page_text:
        return ""

    candidates: List[str] = []
    for value in values:
        value = clean_text(value)
        if not value:
            continue
        candidates.append(value)
        candidates.extend(extract_anchor_tokens(value))
        candidates.extend(_quoted_values(value))

    seen = set()
    ordered = []
    for value in candidates:
        value = clean_text(value).strip(" ,.;:")
        key = value.casefold()
        if (
            not value
            or key in seen
            or len(value) < 3
            or len(value) > 180
        ):
            continue
        seen.add(key)
        ordered.append(value)

    # Garāki un specifiskāki teksti vispirms.
    ordered.sort(key=lambda value: len(value), reverse=True)
    page_low = page_text.casefold()

    for value in ordered:
        if value.casefold() in page_low:
            return value

    return ""


def _looks_mainly_english(text: str) -> bool:
    low = f" {clean_text(text).casefold()} "
    if not low:
        return False

    english_markers = [
        " please ",
        " the ",
        " and ",
        " is ",
        " are ",
        " should ",
        " section ",
        " building ",
        " floor ",
        " drawing ",
        " document ",
        " different ",
        " described ",
        " maximum ",
    ]
    latvian_markers = [
        " lūdzu ",
        " norādīts ",
        " dokumentā ",
        " rasējumā ",
        " sadaļā ",
        " nepieciešams ",
        " precizēt ",
        " neatbilst ",
        " salīdzinot ",
        " augstums ",
    ]

    en_score = sum(marker in low for marker in english_markers)
    lv_score = sum(marker in low for marker in latvian_markers)
    return en_score >= 2 and en_score > lv_score


def refine_manual_import_candidate(
    client: Any,
    model: str,
    candidate: Dict[str, Any],
    selected_pdf_items: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], str]:
    """Pārvērš importa piezīmi latviski un piesaista precīzam PDF tekstam.

    Vispirms izmanto deterministisku burtiska teksta meklēšanu. Ja enkurs nav
    atrodams vai komentārs ir angliski, AI saņem tikai konkrētās lapas tekstu
    un atgriež īsu komentāru latviešu valodā un burtisku target_text.
    """
    refined = dict(candidate)
    source_rel = clean_text(
        refined.get("source_pdf_rel_path")
        or refined.get("source_pdf")
    )
    pdf_item = next(
        (
            item
            for item in selected_pdf_items
            if _canonical_drive_rel_path(
                clean_text(item.get("rel_path") or item.get("name"))
            ) == _canonical_drive_rel_path(source_rel)
        ),
        None,
    )
    if pdf_item is None:
        return refined, "Importa kandidātam neizdevās atrast sasaistīto PDF."

    page_text = _manual_candidate_page_text(
        pdf_item,
        refined.get("target_page"),
    )
    exact_anchor = _manual_exact_anchor_from_page(
        page_text,
        [
            clean_text(refined.get("target_text")),
            clean_text(refined.get("problem")),
            clean_text(refined.get("evidence")),
            clean_text(refined.get("designer_note")),
            clean_text(refined.get("comparison_text")),
        ],
    )

    if exact_anchor:
        refined["target_text"] = exact_anchor
        refined["markup_type"] = "highlight"
        refined["placement_confidence"] = "exact"

    comment = clean_text(
        refined.get("designer_note")
        or refined.get("comment_text")
    )
    needs_ai = (
        not exact_anchor
        or _looks_mainly_english(comment)
        or not comment
    )
    if not needs_ai:
        return refined, ""

    system_prompt = (
        "Tu esi būvprojekta audita redaktors. "
        "Atbildi tikai ar derīgu JSON objektu. "
        "Komentāram obligāti jābūt latviešu valodā. "
        "target_text obligāti jābūt īsam, burtiski un precīzi atrodamam "
        "dotās PDF lapas tekstā. Neizdomā tekstu. "
        "Ja precīzu enkuru nevar atrast, target_text atstāj tukšu."
    )
    user_payload = {
        "source_file": clean_text(refined.get("source_pdf")),
        "target_page": clean_text(refined.get("target_page")),
        "target_area": clean_text(refined.get("target_area")),
        "problem": clean_text(refined.get("problem")),
        "current_comment": comment,
        "current_target_text": clean_text(refined.get("target_text")),
        "comparison_files": clean_text(refined.get("comparison_files")),
        "comparison_text": clean_text(refined.get("comparison_text")),
        "evidence": clean_text(refined.get("evidence")),
        "pdf_page_text": page_text[:10000],
        "required_output": {
            "comment_text_lv": (
                "īss, pašpietiekams komentārs projektētājam latviešu valodā"
            ),
            "target_text": (
                "īss burtisks citāts no pdf_page_text vai tukša virkne"
            ),
        },
    }

    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        user_payload,
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        raw = clean_text(response.choices[0].message.content)
        data = json.loads(strip_json_fences(raw))

        comment_lv = clean_text(data.get("comment_text_lv"))
        ai_anchor = clean_text(data.get("target_text"))

        if comment_lv:
            refined["designer_note"] = comment_lv
            refined["comment_text"] = comment_lv

        # AI enkuru pieņem tikai tad, ja tas patiešām ir lapā.
        if ai_anchor and ai_anchor.casefold() in page_text.casefold():
            refined["target_text"] = ai_anchor
            refined["markup_type"] = "highlight"
            refined["placement_confidence"] = "exact"
        elif not exact_anchor:
            refined["target_text"] = "MANUAL_PLACEMENT_REQUIRED"
            refined["markup_type"] = "page_note"
            refined["placement_confidence"] = "manual_needed"

        return refined, ""
    except Exception as exc:
        if not exact_anchor:
            refined["target_text"] = "MANUAL_PLACEMENT_REQUIRED"
            refined["markup_type"] = "page_note"
            refined["placement_confidence"] = "manual_needed"
        return refined, f"Importa piezīmes precizēšana neizdevās: {exc}"


def refine_manual_import_candidates(
    client: Any,
    model: str,
    candidates: List[Dict[str, Any]],
    selected_pdf_items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    refined_candidates: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for candidate in candidates:
        refined, warning = refine_manual_import_candidate(
            client,
            model,
            candidate,
            selected_pdf_items,
        )
        refined_candidates.append(refined)
        if warning:
            number = clean_text(candidate.get("manual_note_number"))
            warnings.append(
                f"{number + ': ' if number else ''}{warning}"
            )

    return refined_candidates, warnings


def manual_import_rows_to_candidates(
    dataframe: pd.DataFrame,
    selected_pdf_items: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Pārvērš importētās Excel rindas par parastiem pārskatāmiem kandidātiem."""
    candidates: List[Dict[str, Any]] = []
    warnings: List[str] = []

    if dataframe is None or dataframe.empty:
        return candidates, warnings

    for row_index, row in dataframe.iterrows():
        number = clean_text(row.get("note_number")) or f"IMP-{row_index + 1}"
        requested_source_file = clean_text(row.get("source_file"))
        matched_pdf = match_manual_source_pdf(
            requested_source_file,
            selected_pdf_items,
        )
        if matched_pdf is None:
            selected_names = [
                clean_text(item.get("name"))
                for item in selected_pdf_items
                if clean_text(item.get("name"))
            ]
            available = "; ".join(selected_names[:6])
            if len(selected_names) > 6:
                available += f"; … vēl {len(selected_names) - 6}"
            requested_code = _manual_pdf_document_code_key(
                clean_text(row.get("source_file"))
            )
            selected_codes = [
                _manual_pdf_document_code_key(item.get("name"))
                for item in selected_pdf_items
                if _manual_pdf_document_code_key(item.get("name"))
            ]
            code_info = ""
            if requested_code or selected_codes:
                code_info = (
                    f" Excel dokumenta kods: {requested_code or 'nav'}; "
                    f"izvēlēto PDF kodi: {', '.join(selected_codes) or 'nav'}."
                )
            warnings.append(
                f"{number}: source_file “{clean_text(row.get('source_file'))}” "
                "neatbilst nevienam no šajā auditā izvēlētajiem PDF. "
                f"Izvēlētie faili: {available or 'nav'}.{code_info}"
            )
            continue

        source_name = clean_text(matched_pdf.get("name"))
        source_rel_path = clean_text(
            matched_pdf.get("rel_path") or source_name
        )
        target_text = clean_text(row.get("target_text"))
        target_page = clean_text(row.get("target_page")) or "1"
        placement = clean_text(row.get("placement_confidence"))
        markup_type = clean_text(row.get("markup_type"))

        # Importa rindai atļaujam arī page note, ja precīza teksta enkura nav.
        if not placement:
            placement = "exact" if target_text else "manual_needed"
        if not markup_type:
            markup_type = "highlight" if target_text else "page_note"

        evidence_parts = [
            clean_text(row.get("evidence")),
            clean_text(row.get("comparison_text")),
        ]
        evidence = " | ".join(
            value for value in evidence_parts if value
        )

        candidate = {
            "candidate_source": "manual_excel_import",
            "manual_note_number": number,
            "title": clean_text(row.get("problem"))[:180]
            or f"Importēta piezīme {number}",
            "family": clean_text(row.get("family")),
            "issue_type": clean_text(row.get("issue_type")),
            "status": "manual_import",
            "where": clean_text(row.get("target_area")),
            "target_area": clean_text(row.get("target_area")),
            "target_page": target_page,
            "target_text": target_text or "MANUAL_PLACEMENT_REQUIRED",
            "problem": clean_text(row.get("problem")),
            "evidence": evidence,
            "designer_note": clean_text(row.get("comment_text")),
            "comment_text": clean_text(row.get("comment_text")),
            "severity": clean_text(row.get("severity")) or "medium",
            "markup_type": markup_type,
            "placement_confidence": placement,
            "comparison_files": clean_text(row.get("comparison_file")),
            "comparison_pages": clean_text(row.get("comparison_page")),
            "comparison_text": clean_text(row.get("comparison_text")),
            "scope": clean_text(row.get("scope")),
            "notes_for_ai": clean_text(row.get("notes_for_ai")),
            "source_pdf": source_name,
            "source_pdf_rel_path": source_rel_path,
            "manual_requested_source_file": requested_source_file,
            "include_default": True,
            "reject_default": False,
        }
        candidates.append(candidate)

    return candidates, warnings


def render_pdf_progress_dashboard(
    placeholder: Any,
    states: List[Dict[str, Any]],
    family_count: int,
) -> None:
    denominator = max(1, int(family_count or 0))
    with placeholder.container():
        st.markdown("#### Auditējamo failu progress")
        for index, state in enumerate(states, start=1):
            status_code = clean_text(state.get("status")) or "waiting"
            labels = {"waiting": "Gaida", "running": "Analizē", "done": "Pabeigts", "error": "Kļūda"}
            icons = {"waiting": "⏳", "running": "🔎", "done": "✅", "error": "⚠️"}
            completed = int(state.get("completed") or 0)
            with st.container(border=True):
                c1, c2, c3 = st.columns([7, 2, 2])
                c1.markdown(f"**{index}. {clean_text(state.get('name'))}**")
                c2.markdown(f"{icons.get(status_code, '•')} **{labels.get(status_code, status_code)}**")
                c3.markdown(f"**Piezīmes: {int(state.get('candidates') or 0)}**")
                st.progress(min(1.0, completed / denominator))
                if status_code == "running":
                    st.caption(
                        f"Pārbaudes: {completed}/{family_count} · "
                        f"Pašlaik: {clean_text(state.get('current_family')) or 'analīze'}"
                    )
                elif status_code == "done":
                    st.caption(f"Pārbaudes pabeigtas: {completed}/{family_count}")
                elif status_code == "error":
                    st.caption(clean_text(state.get("error")) or "Faila analīzi neizdevās pabeigt.")
                else:
                    st.caption(f"Pārbaudes: {completed}/{family_count}")

def init_state():
    defaults = {
        "pdf_files": [],
        "index_df": pd.DataFrame(),
        "index_file": None,
        "feedback_df": pd.DataFrame(),
        "feedback_project_code": "",
        "feedback_messages": [],
        "selected_pdf_bytes": None,
        "selected_pdf_name": "",
        "selected_pdf_rel_path": "",
        "selected_pdf_items": [],
        "pdf_text": "",
        "pdf_pages": [],
        "candidates": [],
        "ai_errors": [],
        "manual_import_df": pd.DataFrame(columns=MANUAL_IMPORT_COLUMNS),
        "manual_import_errors": [],
        "manual_import_warnings": [],
        "manual_import_file_name": "",
        "selected_project_filter": "",
        "selected_folder_filter": "Viss projekts",
        "pdf_search_value": "",
        "applied_project_filter": "",
        "applied_folder_filter": "Viss projekts",
        "applied_pdf_search": "",
        "selected_pdf_ids_ui": [],
        "selected_subfolder_paths": [],
        "drive_target_folder_id": "",
        "drive_target_folder_name": "",
        "drive_target_folder_path": "",
        "drive_save_result": None,
        "drive_save_error": "",
        "input_root_info": None,
        "project_folders": [],
        "audit_run_id": "",
        "drive_write_test_result": None,
        "drive_write_test_error": "",
        "oauth_user_email": "",
        "oauth_user_name": "",
        "oauth_error": "",
        "active_project_memory_id": "",
        "active_project_memory_code": "",
        "active_project_memory_name": "",
        "active_project_memory_manifest": {},
        "project_memory_list": [],
        "project_memory_error": "",
        "project_memory_update_result": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    init_state()

    st.title(APP_TITLE)
    st.caption(
        "AI ģenerē pierādāmas piezīmes, cilvēks tās pārskata, un "
        "izskatītie dokumenti papildina aktīvā būvprojekta atmiņu."
    )

    oauth_config = get_google_oauth_config()

    service = get_drive_service()
    if service is None:
        st.error("Nav atrasti Google service account dati Streamlit secrets. Vajadzīgs GOOGLE_SERVICE_ACCOUNT_JSON vai [google_service_account].")
        st.stop()

    input_folder_id = get_secret("GOOGLE_DRIVE_INPUT_FOLDER_ID", "DRIVE_INPUT_FOLDER_ID", default="") or ""
    memory_folder_id = get_secret("GOOGLE_DRIVE_MEMORY_FOLDER_ID", "DRIVE_MEMORY_FOLDER_ID", default="") or ""
    results_folder_id = get_secret(
        "GOOGLE_DRIVE_RESULTS_FOLDER_ID",
        "DRIVE_RESULTS_FOLDER_ID",
        default="",
    ) or ""

    with st.sidebar:
        st.header("Iestatījumi")

        st.subheader("Google Drive OAuth")
        if not oauth_config:
            st.error(
                "Nav atrasti [google_oauth] secrets: client_id, "
                "client_secret un refresh_token."
            )
            st.caption("Šī versija neizmanto redirect URI vai pārlūka callback.")
        else:
            try:
                sidebar_oauth_service = get_oauth_drive_service(oauth_config)
                oauth_user = get_oauth_user(sidebar_oauth_service)
                st.session_state.oauth_user_email = oauth_user.get("email", "")
                st.session_state.oauth_user_name = oauth_user.get("name", "")
                oauth_label = oauth_user.get("email") or oauth_user.get("name") or "Google lietotājs"
                st.success(f"Drive OAuth aktīvs: {oauth_label}")
                st.caption("Piekļuve tiek atjaunota automātiski ar refresh token.")
            except Exception as exc:
                st.session_state.oauth_error = str(exc)
                st.error("Drive OAuth refresh token nedarbojas.")
                st.code(str(exc))

        input_folder_id = st.text_input("01_Input folder ID", value=input_folder_id)
        memory_folder_id = st.text_input("03_Memory folder ID", value=memory_folder_id)
        results_folder_id = st.text_input(
            "02_Results folder ID (nav obligāts)",
            value=results_folder_id,
            help=(
                "Ja lauks ir tukšs, rīks mēģina atrast 02_Results kā "
                "01_Input māsas mapi zem BP_Audits_tests."
            ),
        )
        model = st.text_input("OpenAI modelis", value=get_secret("OPENAI_MODEL", default="gpt-4.1-mini") or "gpt-4.1-mini")
        max_context_chars = st.slider("PDF konteksta garums", 5000, 60000, 25000, 5000)
        max_examples_per_family = st.slider("Piemēri vienai ģimenei", 1, 12, 12, 1)
        max_candidates_per_family = st.slider("Max piezīmes vienai ģimenei", 0, 8, 4, 1)
        st.caption("0 nozīmē: ģimeni šoreiz nepalaist.")

        index_df = st.session_state.get("index_df", pd.DataFrame())
        if not index_df.empty:
            families_available = [f for f in DEFAULT_FAMILIES if f in set(index_df["normalized_family"].astype(str))]
            extra = sorted(set(index_df["normalized_family"].astype(str)) - set(families_available) - {""})
            family_options = families_available + extra
        else:
            family_options = DEFAULT_FAMILIES
        selected_families = st.multiselect("Iekšēji palaistās ģimenes", options=family_options, default=family_options)
        st.caption("Lietotājam ikdienā šo var paslēpt. Testā atstājam kontrolei.")


    st.header("0. Projekta atmiņa")
    st.caption(
        "Atmiņa tiek papildināta tikai pēc pārskatīta audita saglabāšanas. "
        "Aktivizējot citu projektu, iepriekšējā atmiņa paliek saglabāta un "
        "jebkurā brīdī ir atkārtoti izmantojama."
    )

    project_memory_service = None
    project_memory_service_error = ""
    try:
        project_memory_service = get_oauth_drive_service(oauth_config)
    except Exception as exc:
        project_memory_service_error = str(exc)

    if not memory_folder_id.strip():
        st.warning("Norādi 03_Memory folder ID, lai izmantotu projekta atmiņu.")
    elif project_memory_service is None:
        st.warning("Projekta atmiņai vajadzīgs strādājošs Google Drive OAuth.")
        if project_memory_service_error:
            st.code(project_memory_service_error)
    else:
        try:
            memories = list_project_memories(project_memory_service, memory_folder_id.strip())
            st.session_state.project_memory_list = memories
            memory_by_id = {clean_text(item.get("id")): item for item in memories}
            active_id = clean_text(st.session_state.get("active_project_memory_id"))
            if active_id and active_id not in memory_by_id:
                st.session_state.active_project_memory_id = ""
                st.session_state.active_project_memory_code = ""
                st.session_state.active_project_memory_name = ""
                st.session_state.active_project_memory_manifest = {}
                active_id = ""

            if memories:
                options = [clean_text(item.get("id")) for item in memories]
                default_index = options.index(active_id) if active_id in options else 0
                selected_memory_id = st.selectbox(
                    "Pieejamās projekta atmiņas",
                    options=options,
                    index=default_index,
                    format_func=lambda folder_id: (
                        f"{normalize_project_code(memory_by_id[folder_id].get('name'))} — "
                        f"{memory_by_id[folder_id].get('path')}"
                    ),
                    key="project_memory_selector",
                )
                activate_col, _ = st.columns([1, 4])
                with activate_col:
                    activate_clicked = st.button(
                        "Aktivizēt izvēlēto atmiņu",
                        type="primary",
                        use_container_width=True,
                        key="activate_project_memory",
                    )
                if activate_clicked:
                    activated = create_or_activate_project_memory(
                        project_memory_service,
                        memory_folder_id.strip(),
                        normalize_project_code(memory_by_id[selected_memory_id].get("name")),
                        normalize_project_code(memory_by_id[selected_memory_id].get("name")),
                    )
                    st.session_state.active_project_memory_id = clean_text(activated.get("id"))
                    st.session_state.active_project_memory_code = normalize_project_code(activated.get("project_code"))
                    st.session_state.active_project_memory_name = clean_text(activated.get("project_name"))
                    st.session_state.active_project_memory_manifest = activated.get("manifest") or {}
                    st.rerun()
            else:
                st.info("Vēl nav izveidota neviena projekta atmiņa.")

            with st.expander("+ Izveidot jaunu projekta atmiņu", expanded=not bool(memories)):
                suggested_code = normalize_project_code(
                    st.session_state.get("applied_project_filter")
                    or st.session_state.get("selected_project_filter")
                )
                new_code = st.text_input(
                    "Projekta kods",
                    value=suggested_code,
                    placeholder="piem., C2-3",
                    key="new_project_memory_code",
                )
                new_name = st.text_input(
                    "Projekta nosaukums",
                    value=suggested_code,
                    placeholder="piem., Dzīvojamā ēka C2-3",
                    key="new_project_memory_name",
                )
                create_col, _ = st.columns([1, 4])
                with create_col:
                    create_clicked = st.button(
                        "Izveidot un aktivizēt",
                        type="primary",
                        use_container_width=True,
                        key="create_project_memory_button",
                    )
                if create_clicked:
                    created = create_or_activate_project_memory(
                        project_memory_service,
                        memory_folder_id.strip(),
                        new_code,
                        new_name,
                    )
                    st.session_state.active_project_memory_id = clean_text(created.get("id"))
                    st.session_state.active_project_memory_code = normalize_project_code(created.get("project_code"))
                    st.session_state.active_project_memory_name = clean_text(created.get("project_name"))
                    st.session_state.active_project_memory_manifest = created.get("manifest") or {}
                    st.rerun()

            active_code = clean_text(st.session_state.get("active_project_memory_code"))
            active_name = clean_text(st.session_state.get("active_project_memory_name"))
            active_manifest = st.session_state.get("active_project_memory_manifest") or {}
            if active_code:
                st.success(
                    f"Aktīvā projekta atmiņa: {active_code}"
                    + (f" — {active_name}" if active_name and active_name != active_code else "")
                )
                m1, m2, m3 = st.columns(3)
                m1.metric("Dokumenti atmiņā", int(active_manifest.get("document_count") or 0))
                m2.metric("Audita reizes", int(active_manifest.get("audit_count") or 0))
                m3.metric("Disciplīnas", len(active_manifest.get("disciplines") or []))
            else:
                st.warning(
                    "Nav aktīvas projekta atmiņas. Auditu var palaist, "
                    "bet projekta konteksts netiks papildināts."
                )
        except Exception as exc:
            st.session_state.project_memory_error = str(exc)
            st.error("Projekta atmiņas sadaļu neizdevās sagatavot.")
            st.code(str(exc))

    st.header("1. Zināšanu bāzes nolasīšana")
    st.caption("Nolasi globālo zelta piemēru indeksu. Visi indeksa piemēri ir izmantojami jebkura projekta auditā; projekta kods nosaka tikai izcelsmi un nelielu atlases prioritāti. Projekta feedback tiks nolasīts pēc projekta izvēles.")

    kb_btn_col, _ = st.columns([1, 4])
    with kb_btn_col:
        read_kb_clicked = st.button(
            "Nolasīt zināšanu bāzi",
            type="primary",
            use_container_width=True,
        )

    if read_kb_clicked:
        if not memory_folder_id.strip():
            st.error("Nav norādīts 03_Memory folder ID.")
        else:
            try:
                with st.spinner("Nolasu audit_examples_index..."):
                    df, index_file, index_messages = load_audit_examples_index(service, memory_folder_id.strip())

                st.session_state.index_df = df
                st.session_state.index_file = index_file

                st.session_state.feedback_df = pd.DataFrame()
                st.session_state.feedback_project_code = ""
                st.session_state.feedback_messages = []

                for msg in index_messages:
                    st.warning(msg)

                if not df.empty:
                    index_name = clean_text(index_file.get("name")) if index_file else ""
                    st.success(
                        f"Globālais zināšanu indekss nolasīts: {index_name} — {len(df)} piemēri."
                    )
                else:
                    st.error("Indekss nav nolasīts. PDF failu solis paliek bloķēts.")
            except Exception as e:
                st.session_state.index_df = pd.DataFrame()
                st.session_state.index_file = None
                st.session_state.feedback_df = pd.DataFrame()
                st.error("Zināšanu bāzes nolasīšana neizdevās, bet lietotne turpina darboties.")
                st.code(str(e))
                with st.expander("Pilns tehniskais traceback"):
                    st.code(traceback.format_exc())

    if st.session_state.index_file:
        idx = st.session_state.index_file
        st.info(
            f"Aktīvais audit_examples_index: {idx.get('name')} | "
            f"Modified: {idx.get('modifiedTime', '')} | "
            f"Piemēri: {len(st.session_state.index_df)} | "
            f"Feedback projekts: {st.session_state.get('feedback_project_code') or 'vēl nav izvēlēts'} | "
            f"Feedback rindas: {len(st.session_state.feedback_df)}"
        )

    st.header("2. PDF failu saraksta nolasīšana")
    index_ready = not st.session_state.index_df.empty
    if not index_ready:
        st.caption("Vispirms pabeidz 1. soli — nolasi audit_examples_index.")

    pdf_list_btn_col, _ = st.columns([1, 4])
    with pdf_list_btn_col:
        read_pdf_list_clicked = st.button(
            "Nolasīt PDF sarakstu",
            disabled=not index_ready,
            use_container_width=True,
        )

    if read_pdf_list_clicked:
        if not input_folder_id.strip():
            st.error("Nav norādīts 01_Input folder ID.")
        else:
            try:
                with st.spinner("Atrodu 01_Input saknes mapi..."):
                    input_root_info = resolve_input_root(
                        service,
                        input_folder_id.strip(),
                        wanted_name="01_Input",
                    )
                    input_root_id = clean_text(input_root_info.get("id"))

                with st.spinner("Nolasu projektu mapes un PDF failu sarakstu..."):
                    project_folders = drive_list_children(
                        service,
                        input_root_id,
                        "application/vnd.google-apps.folder",
                    )
                    listed_files = drive_list_recursive(
                        service,
                        input_root_id,
                        (".pdf",),
                        prefix="",
                        max_files=5000,
                    )

                st.session_state.input_root_info = input_root_info
                st.session_state.project_folders = [
                    dict(item) for item in project_folders if isinstance(item, dict)
                ]
                # Saglabā tikai vienkāršas kopijas. UI nedrīkst mainīt Drive rezultātu objektus uz vietas.
                st.session_state.pdf_files = [
                    dict(item) for item in listed_files if isinstance(item, dict)
                ]

                root_name = clean_text(input_root_info.get("name"))
                resolved_note = (
                    f"01_Input atrasta automātiski: {root_name}"
                    if input_root_info.get("resolved")
                    else f"Izmantota konfigurētā mape: {root_name}"
                )
                st.success(
                    f"{resolved_note}. Projektu mapes: "
                    f"{len(st.session_state.project_folders)}; "
                    f"PDF faili: {len(st.session_state.pdf_files)}"
                )
                if input_root_info.get("warning"):
                    st.warning(clean_text(input_root_info.get("warning")))
                if len(st.session_state.pdf_files) == 0:
                    st.warning("01_Input mapē vai tās apakšmapēs netika atrasts neviens PDF. Pārbaudi folder ID un service account piekļuvi.")
                if st.session_state.get("drive_list_warnings"):
                    with st.expander("Drive nolasīšanas brīdinājumi"):
                        for w in st.session_state.get("drive_list_warnings", []):
                            st.warning(w)
            except Exception as e:
                st.session_state.pdf_files = []
                st.error("Neizdevās nolasīt PDF failus no 01_Input. Lietotne turpina darboties.")
                st.code(str(e))
                with st.expander("Pilns tehniskais traceback"):
                    st.code(traceback.format_exc())

    input_root_info = st.session_state.get("input_root_info")
    if input_root_info:
        root_chain = input_root_info.get("chain") or []
        configured_name = clean_text(root_chain[0].get("name")) if root_chain else ""
        root_name = clean_text(input_root_info.get("name"))
        if input_root_info.get("resolved"):
            st.info(
                f"PDF nolasīšanas sakne: {root_name}. "
                f"Konfigurētā mape: {configured_name or 'nav zināma'}."
            )
        else:
            st.warning(
                f"PDF nolasīšanas sakne nav apstiprināta kā 01_Input; "
                f"tiek izmantota: {root_name}."
            )

    pdf_files = st.session_state.pdf_files
    if pdf_files or st.session_state.get("project_folders"):
        st.subheader("3. Izvēlies un nolasi auditējamos PDF")
        st.caption(
            "Izvēlies projektu, tad atzīmē vienu vai vairākas apakšmapes vertikālajā sarakstā. "
            "Zemāk automātiski parādīsies visi PDF no atzīmētajām mapēm."
        )

        try:
            def _path_parts(rel_path: str) -> List[str]:
                return [part for part in clean_text(rel_path).split("/") if part]

            def _project_folder(rel_path: str) -> str:
                parts = _path_parts(rel_path)
                return parts[0] if len(parts) > 1 else "01_Input sakne"

            def _pdf_folder(rel_path: str) -> str:
                parts = _path_parts(rel_path)
                if len(parts) <= 1:
                    return "01_Input sakne"
                if len(parts) == 2:
                    return parts[0]
                return "/".join(parts[:-1])

            normalized_pdf_files: List[Dict[str, Any]] = []
            for raw in pdf_files:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                rel_path = clean_text(item.get("rel_path") or item.get("name"))
                file_id = clean_text(item.get("id"))
                if not rel_path or not file_id:
                    continue
                item["rel_path"] = rel_path
                item["project_path"] = _project_folder(rel_path)
                item["folder_path"] = _pdf_folder(rel_path)
                item["display_name"] = rel_path.rsplit("/", 1)[-1]
                normalized_pdf_files.append(item)

            actual_project_folders = [
                dict(item)
                for item in st.session_state.get("project_folders", [])
                if isinstance(item, dict) and clean_text(item.get("name"))
            ]
            project_options = sorted({
                clean_text(item.get("name"))
                for item in actual_project_folders
                if clean_text(item.get("name"))
            })
            if any(clean_text(f.get("project_path")) == "01_Input sakne" for f in normalized_pdf_files):
                project_options = ["01_Input sakne"] + project_options

            if not project_options:
                st.warning("01_Input mapē nav atrasta neviena projekta apakšmape.")
            else:
                project_counts = {
                    project: sum(
                        1 for f in normalized_pdf_files
                        if clean_text(f.get("project_path")) == project
                    )
                    for project in project_options
                }
                current_project = clean_text(st.session_state.get("selected_project_filter"))
                if current_project not in project_options:
                    current_project = project_options[0]
                    st.session_state.selected_project_filter = current_project

                project_value = st.selectbox(
                    "Auditējamā projekta mape 01_Input mapē",
                    options=project_options,
                    format_func=lambda x: f"{x} ({project_counts.get(x, 0)} PDF)",
                    key="selected_project_filter",
                )

                previous_project = clean_text(st.session_state.get("applied_project_filter"))
                if project_value != previous_project:
                    st.session_state.applied_project_filter = project_value
                    st.session_state.selected_subfolder_paths = []
                    st.session_state.selected_pdf_ids_ui = []
                    st.session_state.pdf_search_value = ""

                active_project_code = normalize_project_code(
                    st.session_state.get("active_project_memory_code") or project_value
                )
                if active_project_code and active_project_code != st.session_state.get("feedback_project_code"):
                    with st.spinner(f"Automātiski nolasu projekta {active_project_code} problēmu Excel..."):
                        feedback_df, feedback_messages = load_feedback(
                            service, memory_folder_id.strip(), active_project_code
                        )
                    st.session_state.feedback_df = feedback_df
                    st.session_state.feedback_project_code = active_project_code
                    st.session_state.feedback_messages = feedback_messages

                st.info(
                    f"Problēmu Excel ielasīti automātiski: {len(st.session_state.feedback_df)} rindas "
                    f"projektam {active_project_code or '-'}"
                )
                for feedback_message in st.session_state.get("feedback_messages", []):
                    st.warning(feedback_message)

                selected_project_files = [
                    f for f in normalized_pdf_files
                    if clean_text(f.get("project_path")) == project_value
                ]
                folder_paths = sorted({
                    clean_text(f.get("folder_path"))
                    for f in selected_project_files
                    if clean_text(f.get("folder_path"))
                })
                folder_counts = {
                    folder: sum(
                        1 for f in selected_project_files
                        if clean_text(f.get("folder_path")) == folder
                    )
                    for folder in folder_paths
                }

                st.markdown("**Apakšmapes projektā**")
                st.caption("Atzīmē vienu vai vairākas mapes. Failu saraksts zemāk atjaunosies automātiski.")
                previous_selected_folders = set(st.session_state.get("selected_subfolder_paths", []))
                selected_folders: List[str] = []
                folder_box = st.container(border=True)
                with folder_box:
                    if not folder_paths:
                        st.caption("Projektā nav atrastas apakšmapes ar PDF failiem.")
                    for folder in folder_paths:
                        key_hash = hashlib.sha1(f"{project_value}|{folder}".encode("utf-8")).hexdigest()[:16]
                        label = folder
                        prefix = f"{project_value}/"
                        if label.startswith(prefix):
                            label = label[len(prefix):]
                        checked = st.checkbox(
                            f"{label} ({folder_counts.get(folder, 0)} PDF)",
                            value=folder in previous_selected_folders,
                            key=f"subfolder_check_{key_hash}",
                        )
                        if checked:
                            selected_folders.append(folder)
                st.session_state.selected_subfolder_paths = selected_folders

                search_value = st.text_input(
                    "Meklēt PDF izvēlētajās mapēs",
                    placeholder="piem., UKT, explanatory note, RA_11100",
                    key="pdf_search_value",
                )
                search_norm = clean_text(search_value).lower()

                visible_pdf_files = [
                    f for f in selected_project_files
                    if clean_text(f.get("folder_path")) in set(selected_folders)
                ]
                if search_norm:
                    visible_pdf_files = [
                        f for f in visible_pdf_files
                        if search_norm in clean_text(f.get("rel_path")).lower()
                    ]

                st.caption(
                    f"Atzīmētas mapes: {len(selected_folders)} | "
                    f"Redzami PDF: {len(visible_pdf_files)}"
                )

                if not selected_folders:
                    st.warning("Atzīmē vismaz vienu apakšmapi.")
                elif not visible_pdf_files:
                    st.warning("Izvēlētajās mapēs vai pēc meklēšanas PDF faili nav atrasti.")
                else:
                    max_selectable = 300
                    shown_pdf_files = visible_pdf_files[:max_selectable]
                    if len(visible_pdf_files) > max_selectable:
                        st.warning(
                            f"Parādīti pirmie {max_selectable} no {len(visible_pdf_files)} PDF. "
                            "Izmanto meklēšanu, lai sašaurinātu sarakstu."
                        )

                    by_id = {clean_text(f.get("id")): f for f in shown_pdf_files}
                    active_ids = set(by_id)
                    stored_ids = [
                        x for x in st.session_state.get("selected_pdf_ids_ui", [])
                        if x in active_ids
                    ]

                    select_all_col, clear_all_col, _ = st.columns([1, 1, 3])
                    select_all = select_all_col.button("Atzīmēt visus redzamos", key="select_all_visible_pdfs")
                    clear_all = clear_all_col.button("Noņemt visus", key="clear_all_visible_pdfs")
                    if select_all:
                        stored_ids = list(by_id.keys())
                        st.session_state.selected_pdf_ids_ui = stored_ids
                    if clear_all:
                        stored_ids = []
                        st.session_state.selected_pdf_ids_ui = []

                    selection_signature = hashlib.sha1(
                        f"{project_value}|{'|'.join(selected_folders)}|{search_norm}".encode("utf-8")
                    ).hexdigest()[:12]
                    checked_ids: List[str] = []
                    st.markdown("**Atzīmē auditējamos PDF:**")
                    for folder in selected_folders:
                        folder_items = [
                            f for f in shown_pdf_files
                            if clean_text(f.get("folder_path")) == folder
                        ]
                        if not folder_items:
                            continue
                        short_folder = folder
                        prefix = f"{project_value}/"
                        if short_folder.startswith(prefix):
                            short_folder = short_folder[len(prefix):]
                        with st.expander(f"{short_folder} ({len(folder_items)} PDF)", expanded=True):
                            for item in folder_items:
                                file_id = clean_text(item.get("id"))
                                rel_path = clean_text(item.get("rel_path"))
                                file_name = clean_text(item.get("display_name")) or rel_path
                                key_hash = hashlib.sha1(
                                    f"{selection_signature}|{file_id}".encode("utf-8")
                                ).hexdigest()[:16]
                                checked = st.checkbox(
                                    file_name,
                                    value=file_id in stored_ids,
                                    key=f"pdf_check_multi_{key_hash}",
                                    help=rel_path,
                                )
                                if checked:
                                    checked_ids.append(file_id)

                    apply_col, _ = st.columns([1, 4])
                    with apply_col:
                        apply_pdf_selection = st.button(
                            "Apstiprināt PDF izvēli",
                            type="primary",
                            use_container_width=True,
                            key=f"apply_pdf_selection_{selection_signature}",
                        )
                    if apply_pdf_selection:
                        st.session_state.selected_pdf_ids_ui = checked_ids

                    selected_pdf_ids = [
                        x for x in st.session_state.get("selected_pdf_ids_ui", [])
                        if x in by_id
                    ]
                    selected_pdf_files = [by_id[x] for x in selected_pdf_ids]
                    st.caption(f"Apstiprināti PDF: {len(selected_pdf_files)}")

                    if selected_pdf_files:
                        with st.expander("Izvēlētie PDF ceļi", expanded=False):
                            for f in selected_pdf_files:
                                st.write(clean_text(f.get("rel_path")))

                        read_content_col, _ = st.columns([1, 4])
                        with read_content_col:
                            read_selected_clicked = st.button(
                                "Nolasīt izvēlēto PDF saturu",
                                type="primary",
                                use_container_width=True,
                                key="read_selected_pdf_content",
                            )

                        if read_selected_clicked:
                            loaded_items: List[Dict[str, Any]] = []
                            errors: List[str] = []
                            progress = st.progress(0)
                            status = st.empty()
                            for i, selected_pdf in enumerate(selected_pdf_files, start=1):
                                try:
                                    status.write(
                                        f"Nolasu {i}/{len(selected_pdf_files)}: {selected_pdf.get('name')}"
                                    )
                                    pdf_bytes = drive_download_bytes(service, selected_pdf["id"])
                                    pdf_text_value, pages, err = extract_pdf_text(pdf_bytes, max_context_chars)
                                    if err:
                                        errors.append(
                                            f"{selected_pdf.get('rel_path', selected_pdf.get('name'))}: {err}"
                                        )
                                    else:
                                        loaded_items.append({
                                            "id": selected_pdf.get("id"),
                                            "name": selected_pdf.get("name", "audit.pdf"),
                                            "rel_path": selected_pdf.get("rel_path", selected_pdf.get("name", "audit.pdf")),
                                            "bytes": pdf_bytes,
                                            "text": pdf_text_value,
                                            "pages": pages,
                                        })
                                except Exception as exc:
                                    errors.append(
                                        f"{selected_pdf.get('rel_path', selected_pdf.get('name'))}: {exc}"
                                    )
                                progress.progress(i / max(1, len(selected_pdf_files)))
                            status.empty()
                            progress.empty()

                            if loaded_items:
                                st.session_state.selected_pdf_items = loaded_items
                                first = loaded_items[0]
                                st.session_state.selected_pdf_bytes = first.get("bytes")
                                st.session_state.selected_pdf_name = clean_text(first.get("name"))
                                st.session_state.selected_pdf_rel_path = clean_text(first.get("rel_path"))
                                st.session_state.pdf_text = "\n\n".join(
                                    f"===== PDF: {clean_text(item.get('rel_path'))} =====\n{clean_text(item.get('text'))}"
                                    for item in loaded_items
                                )[:max_context_chars]
                                st.session_state.pdf_pages = [
                                    page
                                    for item in loaded_items
                                    for page in item.get("pages", [])
                                ]
                                st.session_state.candidates = []
                                st.session_state.ai_errors = []
                                st.session_state.audit_run_id = ""
                                st.success(f"Nolasīti PDF: {len(loaded_items)}")
                            if errors:
                                with st.expander(f"PDF nolasīšanas kļūdas ({len(errors)})"):
                                    for message in errors:
                                        st.warning(message)
        except Exception as exc:
            st.error(f"PDF izvēles sadaļas kļūda: {exc}")
            with st.expander("PDF izvēles traceback"):
                st.code(traceback.format_exc())


    st.header("3B. Papildu piezīmju Excel imports")
    st.caption(
        "Šeit vari augšupielādēt ChatGPT vai cilvēka sagatavotu Excel. "
        "Importētās rindas tiks pievienotas AI kandidātiem un būs jāpārskata "
        "5. sadaļā tāpat kā rīka ģenerētās piezīmes. Nekas netiek automātiski "
        "saglabāts Drive, kamēr piezīmes nav pārskatītas un audits nav saglabāts."
    )

    manual_upload = st.file_uploader(
        "Augšupielādēt piezīmju Excel (.xlsx)",
        type=["xlsx", "xlsm"],
        key="manual_audit_notes_upload",
        help=(
            "Ieteicamais lapas nosaukums: audit_notes_import. "
            "Failā jābūt source_file, family, issue_type, target_page, "
            "target_area, target_text, problem un comment_text kolonnām."
        ),
    )

    if manual_upload is not None:
        upload_name = clean_text(getattr(manual_upload, "name", ""))
        if upload_name != st.session_state.get("manual_import_file_name"):
            manual_df, manual_errors = read_manual_import_excel(manual_upload)
            st.session_state.manual_import_df = manual_df
            st.session_state.manual_import_errors = manual_errors
            st.session_state.manual_import_file_name = upload_name
            st.session_state.manual_import_warnings = []

    manual_df = st.session_state.get(
        "manual_import_df",
        pd.DataFrame(columns=MANUAL_IMPORT_COLUMNS),
    )
    manual_errors = st.session_state.get("manual_import_errors", [])

    for message in manual_errors:
        st.warning(message)

    if manual_upload is None and st.session_state.get("manual_import_file_name"):
        st.info(
            "Iepriekš nolasītais importa fails šajā sesijā: "
            f"{st.session_state.get('manual_import_file_name')}"
        )

    if manual_df is not None and not manual_df.empty:
        st.success(
            f"Importam sagatavotas piezīmes: {len(manual_df)}"
        )
        preview_columns = [
            "note_number",
            "source_file",
            "family",
            "target_page",
            "target_text",
            "comment_text",
        ]
        st.dataframe(
            manual_df[preview_columns],
            use_container_width=True,
            hide_index=True,
        )

        selected_for_match = st.session_state.get(
            "selected_pdf_items",
            [],
        )
        if selected_for_match:
            matched_count = 0
            unmatched_sources = []
            for source_value in manual_df["source_file"].tolist():
                if match_manual_source_pdf(
                    clean_text(source_value),
                    selected_for_match,
                ) is not None:
                    matched_count += 1
                else:
                    unmatched_sources.append(clean_text(source_value))

            if matched_count == len(manual_df):
                st.success(
                    f"PDF sasaistes pārbaude: visas {matched_count} Excel rindas "
                    "atbilst auditā izvēlētajiem PDF."
                )
            else:
                st.warning(
                    f"PDF sasaistes pārbaude: atbilst {matched_count} no "
                    f"{len(manual_df)} Excel rindām."
                )
                unique_unmatched = list(dict.fromkeys(unmatched_sources))
                if unique_unmatched:
                    st.caption(
                        "Neatpazītie source_file: "
                        + "; ".join(unique_unmatched[:8])
                    )
        else:
            st.info(
                "PDF sasaisti varēs pārbaudīt pēc auditējamo PDF izvēles "
                "un to satura nolasīšanas 3. sadaļā."
            )
        clear_import_col, _ = st.columns([1, 4])
        with clear_import_col:
            if st.button(
                "Noņemt importēto Excel",
                use_container_width=True,
                key="clear_manual_import",
            ):
                st.session_state.manual_import_df = pd.DataFrame(
                    columns=MANUAL_IMPORT_COLUMNS
                )
                st.session_state.manual_import_errors = []
                st.session_state.manual_import_warnings = []
                st.session_state.manual_import_file_name = ""
                st.rerun()

        st.markdown("### Nākamie soļi")
        kb_ready_for_import = not st.session_state.get(
            "index_df",
            pd.DataFrame(),
        ).empty
        pdfs_ready_for_import = bool(
            st.session_state.get("selected_pdf_items", [])
        )

        step1_icon = "✅" if kb_ready_for_import else "⬜"
        step2_icon = "✅" if pdfs_ready_for_import else "⬜"
        step3_icon = "✅"

        st.markdown(
            f"{step1_icon} **1. Zināšanu bāze nolasīta**  \\n"
            f"{step2_icon} **2. Auditējamie PDF izvēlēti un to saturs nolasīts**  \\n"
            f"{step3_icon} **3. Papildu piezīmju Excel nolasīts**"
        )

        if not kb_ready_for_import:
            st.warning(
                "Atgriezies 1. sadaļā un nospied “Nolasīt zināšanu bāzi”."
            )
        if not pdfs_ready_for_import:
            st.warning(
                "Atgriezies 3. sadaļā, izvēlies PDF, apstiprini izvēli un "
                "nospied “Nolasīt izvēlēto PDF saturu”."
            )

        if kb_ready_for_import and pdfs_ready_for_import:
            st.success(
                "Viss ir sagatavots. Ritini uz 4. sadaļu un nospied "
                "“Analizēt izvēlētos PDF”. Importētās piezīmes tiks "
                "pievienotas AI kandidātiem un parādīsies 5. sadaļā pārskatīšanai."
            )
        else:
            st.info(
                "Importētais Excel paliks šajā Streamlit sesijā. "
                "Pēc trūkstošo soļu izpildes atgriezies 4. sadaļā."
            )
    else:
        st.caption("Papildu piezīmju Excel nav ielādēts.")


    st.header("4. AI piezīmju ģenerēšana")
    selected_pdf_items = st.session_state.get("selected_pdf_items", [])
    ready = bool(selected_pdf_items) and not st.session_state.index_df.empty
    if not ready:
        missing_steps = []
        if st.session_state.index_df.empty:
            missing_steps.append(
                "1. sadaļā jānospiež “Nolasīt zināšanu bāzi”"
            )
        if not selected_pdf_items:
            missing_steps.append(
                "3. sadaļā jāizvēlas PDF un jānospiež "
                "“Nolasīt izvēlēto PDF saturu”"
            )

        st.warning(
            "AI analīzi vēl nevar palaist. Trūkst: "
            + "; ".join(missing_steps)
            + "."
        )

        manual_count = len(
            st.session_state.get(
                "manual_import_df",
                pd.DataFrame(),
            )
        )
        if manual_count:
            st.info(
                f"Papildu Excel ir nolasīts ({manual_count} piezīmes), "
                "bet vispirms jāizpilda iepriekš norādītie soļi."
            )
    else:
        st.caption(f"Analīzei sagatavoti PDF: {len(selected_pdf_items)}")
        analyze_col, _ = st.columns([1, 4])
        with analyze_col:
            analyze_clicked = st.button("Analizēt izvēlētos PDF", type="primary", use_container_width=True)
        if analyze_clicked:
            client = get_openai_client()
            if client is None:
                st.stop()

            clear_review_widget_state()
            audit_run_id = f"RUN-{datetime.now().strftime('%Y%m%d%H%M%S')}-{str(time.time_ns())[-6:]}"
            st.session_state.audit_run_id = audit_run_id
            st.session_state.candidates = []
            st.session_state.ai_errors = []

            all_candidates: List[Dict[str, Any]] = []
            errors: List[Dict[str, Any]] = []

            manual_candidates, manual_warnings = manual_import_rows_to_candidates(
                st.session_state.get(
                    "manual_import_df",
                    pd.DataFrame(columns=MANUAL_IMPORT_COLUMNS),
                ),
                selected_pdf_items,
            )

            if manual_candidates:
                status = st.empty()
                status.write(
                    "Precizē manuālā Excel piezīmes: tulko latviski un "
                    "meklē precīzus teksta enkurus PDF lapās."
                )
                refined_manual, refine_warnings = refine_manual_import_candidates(
                    client,
                    model,
                    manual_candidates,
                    selected_pdf_items,
                )
                manual_candidates = refined_manual
                manual_warnings.extend(refine_warnings)

            all_candidates.extend(manual_candidates)
            st.session_state.manual_import_warnings = manual_warnings

            progress = st.progress(0)
            if "status" not in locals():
                status = st.empty()
            pdf_progress_placeholder = st.empty()

            selected_family_set = set(selected_families)
            per_pdf_families = [
                family for family in selected_families
                if family != "J_cross_document_traceability"
                and max_candidates_per_family > 0
            ]
            run_cross_document = (
                "J_cross_document_traceability" in selected_family_set
                and max_candidates_per_family > 0
                and len(selected_pdf_items) >= 2
            )
            negative_rules = make_negative_rules(st.session_state.feedback_df)
            total_steps = max(
                1,
                len(selected_pdf_items) * len(per_pdf_families)
                + (1 if run_cross_document else 0),
            )
            step = 0

            pdf_states: List[Dict[str, Any]] = []
            state_by_path: Dict[str, Dict[str, Any]] = {}
            for item in selected_pdf_items:
                rel_path = clean_text(item.get("rel_path") or item.get("name"))
                state = {
                    "name": rel_path,
                    "status": "waiting",
                    "completed": 0,
                    "current_family": "",
                    "candidates": 0,
                    "error": "",
                }
                pdf_states.append(state)
                state_by_path[_canonical_drive_rel_path(rel_path)] = state

            render_pdf_progress_dashboard(
                pdf_progress_placeholder, pdf_states, len(per_pdf_families)
            )

            for pdf_i, pdf_item in enumerate(selected_pdf_items, start=1):
                pdf_name = clean_text(pdf_item.get("name")) or "audit.pdf"
                pdf_rel_path = clean_text(pdf_item.get("rel_path")) or pdf_name
                pdf_text = clean_text(pdf_item.get("text"))
                pdf_state = state_by_path.get(_canonical_drive_rel_path(pdf_rel_path))
                if pdf_state is not None:
                    pdf_state["status"] = "running"
                    pdf_state["current_family"] = "Sagatavo analīzi"
                    render_pdf_progress_dashboard(
                        pdf_progress_placeholder, pdf_states, len(per_pdf_families)
                    )

                if not pdf_text:
                    error_message = "PDF teksts ir tukšs; AI analīze izlaista."
                    errors.append({"pdf": pdf_rel_path, "family": "", "error": error_message})
                    if pdf_state is not None:
                        pdf_state["status"] = "error"
                        pdf_state["error"] = error_message
                        render_pdf_progress_dashboard(
                            pdf_progress_placeholder, pdf_states, len(per_pdf_families)
                        )
                    continue

                if "A_text_language" in selected_family_set:
                    deterministic = detect_unit_case_candidates(pdf_item)
                    for candidate in deterministic:
                        candidate["source_pdf"] = pdf_name
                        candidate["source_pdf_rel_path"] = pdf_rel_path
                        all_candidates.append(candidate)
                    if pdf_state is not None:
                        pdf_state["candidates"] += len(deterministic)

                for family in per_pdf_families:
                    step += 1
                    if pdf_state is not None:
                        pdf_state["status"] = "running"
                        pdf_state["current_family"] = family
                    status.write(
                        f"PDF {pdf_i}/{len(selected_pdf_items)} | "
                        f"pārbaude {step}/{total_steps}: {family} | {pdf_rel_path}"
                    )
                    render_pdf_progress_dashboard(
                        pdf_progress_placeholder, pdf_states, len(per_pdf_families)
                    )
                    examples = select_examples(
                        st.session_state.index_df,
                        family,
                        pdf_name,
                        pdf_text,
                        max_examples_per_family,
                        project_code=normalize_project_code(
                            st.session_state.get("active_project_memory_code")
                            or st.session_state.get("applied_project_filter", "")
                        ),
                    )
                    candidates, err = call_ai_for_family(
                        client=client,
                        model=model,
                        pdf_name=pdf_rel_path,
                        pdf_text=pdf_text,
                        family=family,
                        examples=examples,
                        negative_rules=negative_rules,
                        max_candidates=max_candidates_per_family,
                    )
                    if err:
                        errors.append({"pdf": pdf_rel_path, "family": family, "error": err})
                    for candidate in candidates:
                        candidate["source_pdf"] = pdf_name
                        candidate["source_pdf_rel_path"] = pdf_rel_path
                        all_candidates.append(candidate)
                    if pdf_state is not None:
                        pdf_state["completed"] += 1
                        pdf_state["candidates"] += len(candidates)
                    progress.progress(step / total_steps)
                    render_pdf_progress_dashboard(
                        pdf_progress_placeholder, pdf_states, len(per_pdf_families)
                    )

                if pdf_state is not None:
                    pdf_state["status"] = "done"
                    pdf_state["current_family"] = ""
                    render_pdf_progress_dashboard(
                        pdf_progress_placeholder, pdf_states, len(per_pdf_families)
                    )

            if run_cross_document:
                step += 1
                status.write(
                    f"Starpdokumentu pārbaude {step}/{total_steps}: "
                    "J_cross_document_traceability"
                )
                combined_text = "\n".join(
                    str(item.get("text") or "")[:5000]
                    for item in selected_pdf_items
                )
                examples = select_examples(
                    st.session_state.index_df,
                    "J_cross_document_traceability",
                    "MULTI_PDF",
                    combined_text,
                    max_examples_per_family,
                    project_code=normalize_project_code(
                        st.session_state.get("active_project_memory_code")
                        or st.session_state.get("applied_project_filter", "")
                    ),
                )
                cross_candidates, err = call_ai_for_cross_document_family(
                    client=client,
                    model=model,
                    pdf_items=selected_pdf_items,
                    examples=examples,
                    negative_rules=negative_rules,
                    max_candidates=max_candidates_per_family,
                )
                if err:
                    errors.append({
                        "pdf": "MULTI_PDF",
                        "family": "J_cross_document_traceability",
                        "error": err,
                    })
                for candidate in cross_candidates:
                    rel_path = clean_text(candidate.get("target_file"))
                    matched = next(
                        (
                            item for item in selected_pdf_items
                            if _canonical_drive_rel_path(
                                clean_text(item.get("rel_path") or item.get("name"))
                            ) == _canonical_drive_rel_path(rel_path)
                        ),
                        None,
                    )
                    candidate["source_pdf"] = (
                        clean_text(matched.get("name"))
                        if matched else rel_path.rsplit("/", 1)[-1]
                    )
                    candidate["source_pdf_rel_path"] = rel_path
                    all_candidates.append(candidate)
                    target_state = state_by_path.get(_canonical_drive_rel_path(rel_path))
                    if target_state is not None:
                        target_state["candidates"] += 1
                progress.progress(step / total_steps)

            final_candidates: List[Dict[str, Any]] = []
            seen = set()
            for ordinal, raw in enumerate(all_candidates, start=1):
                family = clean_text(raw.get("family"))
                candidate = normalize_candidate(raw, family)
                if candidate_is_too_vague(candidate):
                    continue
                dedupe_key = (
                    _canonical_drive_rel_path(candidate.get("source_pdf_rel_path")),
                    family,
                    clean_text(candidate.get("target_page")),
                    clean_text(candidate.get("target_text")).casefold(),
                    clean_text(candidate.get("problem")).casefold(),
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                candidate["candidate_id"] = make_candidate_id(
                    candidate, audit_run_id, ordinal
                )
                candidate["include_default"] = True
                candidate["reject_default"] = False
                final_candidates.append(candidate)

            for state in pdf_states:
                state["candidates"] = 0
            for candidate in final_candidates:
                candidate_state = state_by_path.get(
                    _canonical_drive_rel_path(candidate.get("source_pdf_rel_path"))
                )
                if candidate_state is not None:
                    candidate_state["candidates"] += 1
            render_pdf_progress_dashboard(
                pdf_progress_placeholder, pdf_states, len(per_pdf_families)
            )

            st.session_state.candidates = final_candidates
            st.session_state.ai_errors = errors
            status.write("AI analīze pabeigta.")
            st.success(f"Ģenerētas pārskatāmas piezīmes: {len(final_candidates)}")
            if manual_candidates:
                st.info(
                    f"No manuālā Excel pievienoti kandidāti: {len(manual_candidates)}"
                )
            for warning in manual_warnings:
                st.warning(warning)

    if st.session_state.ai_errors:
        with st.expander("AI batch kļūdas"):
            st.dataframe(pd.DataFrame(st.session_state.ai_errors), use_container_width=True)

    candidates = st.session_state.candidates
    if candidates:
        st.header("5. Piezīmju pārskatīšana")
        st.caption("Noklusēti piezīme ir iekļauta Excel/markup. Ja noraidi, ieraksti iemeslu; vari atzīmēt arī 'turpmāk līdzīgas nerādīt'.")
        accepted_rows = []
        rejected_rows = []
        review_rows = []
        audit_run_id = clean_text(st.session_state.get("audit_run_id")) or "RUN-UNKNOWN"
        for idx, c in enumerate(candidates, start=1):
            candidate_id = clean_text(c.get("candidate_id")) or make_candidate_id(c, audit_run_id, idx)
            title = clean_text(c.get("title")) or f"Piezīme {idx}"
            family = clean_text(c.get("family"))
            with st.container(border=True):
                st.markdown(f"### {idx}. {title}")
                source_pdf = clean_text(c.get("source_pdf")) or st.session_state.selected_pdf_name
                source_pdf_rel = clean_text(c.get("source_pdf_rel_path")) or source_pdf
                st.markdown(f"**PDF:** {source_pdf_rel}")
                if clean_text(c.get("candidate_source")) == "manual_excel_import":
                    requested_source = clean_text(
                        c.get("manual_requested_source_file")
                    )
                    mapped_note = ""
                    if (
                        requested_source
                        and _manual_pdf_name_key(requested_source)
                        != _manual_pdf_name_key(source_pdf_rel)
                    ):
                        mapped_note = (
                            f" · Excel fails: {requested_source}"
                            f" → auditā izmantots: {source_pdf_rel}"
                        )
                    st.info(
                        "Avots: manuāli augšupielādēts piezīmju Excel"
                        + (
                            f" · Nr. {clean_text(c.get('manual_note_number'))}"
                            if clean_text(c.get("manual_note_number"))
                            else ""
                        )
                        + mapped_note
                    )
                st.markdown(f"**Ģimene:** `{family}`")
                st.markdown(f"**Kur:** {clean_text(c.get('where') or c.get('target_area'))}")
                st.markdown(f"**Statuss:** {clean_text(c.get('status'))}")
                st.markdown("**Problēma:**")
                st.write(clean_text(c.get("problem")))
                st.markdown("**PDF komentārs:**")
                edited_note = st.text_area(
                    "Labot īso komentāru",
                    value=clean_text(c.get("designer_note") or c.get("problem") or c.get("comment_text")),
                    key=f"designer_note_{audit_run_id}_{candidate_id}",
                    height=140,
                )
                c["designer_note"] = edited_note
                decision = st.radio(
                    "Lēmums par piezīmi",
                    options=["Iekļaut Excel / markup", "Noraidīt"],
                    index=0,
                    horizontal=True,
                    key=f"decision_{audit_run_id}_{candidate_id}",
                )
                include = decision == "Iekļaut Excel / markup"
                reject = decision == "Noraidīt"
                reject_reason = ""
                do_not_show = False
                if reject:
                    reject_reason = st.text_input("Noraidīšanas iemesls", key=f"reject_reason_{audit_run_id}_{candidate_id}")
                    do_not_show = st.checkbox("Turpmāk līdzīgas piezīmes nerādīt", key=f"do_not_show_{audit_run_id}_{candidate_id}")
                row_review = dict(c)
                row_review["candidate_id"] = candidate_id
                row_review["audit_run_id"] = audit_run_id
                row_review["ui_include"] = include
                row_review["ui_reject"] = reject
                row_review["reject_reason"] = reject_reason
                row_review["do_not_show_similar"] = do_not_show
                review_rows.append(row_review)
                source_pdf_name = clean_text(c.get("source_pdf")) or st.session_state.selected_pdf_name
                source_pdf_for_row = clean_text(c.get("source_pdf_rel_path")) or source_pdf_name
                discipline = infer_discipline_from_filename(source_pdf_name)
                if include and not reject:
                    export_candidate = dict(c)
                    export_candidate["designer_note"] = edited_note
                    if clean_text(edited_note) and not _candidate_says_no_issue(edited_note):
                        accepted_rows.append(candidate_to_export_row(export_candidate, len(accepted_rows) + 1, source_pdf_for_row, discipline))
                    else:
                        st.warning("Piezīme netiks eksportēta, jo PDF komentārs ir tukšs vai pasaka, ka neatbilstības nav.")
                if reject:
                    rejected_rows.append(candidate_to_rejected_row(c, idx, source_pdf_for_row, reject_reason, do_not_show))

        st.header("6. Eksports")
        accepted_df = pd.DataFrame(accepted_rows, columns=REQUIRED_EXPORT_COLUMNS)
        rejected_df = pd.DataFrame(rejected_rows)
        review_df = pd.DataFrame(review_rows)
        c1, c2, c3 = st.columns(3)
        c1.metric("Akceptētas", len(accepted_df))
        c2.metric("Noraidītas", len(rejected_df))
        c3.metric("Kopā piezīmes", len(review_df))
        st.caption("ZIP satur accepted/review Excel. Rejected faili tiek pievienoti tikai tad, ja ir noraidītas piezīmes. ZIP satur rezultāta PDF katram auditētajam dokumentam; failiem bez konstatētām neatbilstībām pirmajā lapā ir audita rezultāta paziņojums. Zemāk vari pārbaudīt rakstīšanu uz Google Drive 02_Results.")
        selected_pdf_items = st.session_state.get("selected_pdf_items", [])
        if len(selected_pdf_items) == 1:
            base_source = selected_pdf_items[0].get("name", st.session_state.selected_pdf_name)
            base = re.sub(r"[^A-Za-z0-9_\-]+", "_", os.path.splitext(base_source)[0])[:80]
        else:
            base = f"multi_pdf_{len(selected_pdf_items)}_files"
        zip_bytes = make_zip(accepted_df, rejected_df, review_df, base, selected_pdf_items)
        st.download_button(
            "Lejupielādēt ZIP ar PDF + Excel",
            data=zip_bytes,
            file_name=f"bp_ai_audit_copilot_{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            type="primary",
        )


        st.subheader("Saglabāšana Google Drive")
        st.caption(
            "Koriģētie PDF tiek saglabāti atsevišķi izvēlētajā 02_Results projekta mapē. "
            "Akceptēto piemēru Excel tiek saglabāts 03_Memory/05_Audit_examples_pending, "
            "bet noraidījumu Excel — 03_Memory/03_Audit_feedback. ZIP paliek tikai kā papildu lejupielāde."
        )

        oauth_service = None
        oauth_service_error = ""
        try:
            oauth_service = get_oauth_drive_service(oauth_config)
        except Exception as exc:
            oauth_service_error = str(exc)

        if oauth_service_error:
            st.error(f"OAuth Drive servisu neizdevās izveidot: {oauth_service_error}")

        if oauth_service is None:
            st.warning(
                "Google Drive OAuth nav konfigurēts. Streamlit Secrets sadaļā "
                "pievieno client_id, client_secret un refresh_token."
            )
        else:
            oauth_user = (
                clean_text(st.session_state.get("oauth_user_email"))
                or clean_text(st.session_state.get("oauth_user_name"))
                or "Google lietotājs"
            )
            st.success(f"Drive rakstīšana autorizēta kā: {oauth_user}")

            try:
                results_root = resolve_results_folder(
                    oauth_service,
                    input_folder_id=input_folder_id.strip(),
                    explicit_results_folder_id=results_folder_id.strip(),
                )
                results_root_id = clean_text(results_root.get("id"))
                results_root_name = clean_text(results_root.get("name")) or "02_Results"

                # Galamērķu saraksts vienmēr sākas 02_Results saknē.
                # Tas ļauj brīvi izvēlēties citu projektu neatkarīgi no auditējamā 01_Input projekta.
                project_folders = drive_list_children(
                    oauth_service, results_root_id, "application/vnd.google-apps.folder"
                )
                folder_options: List[Dict[str, str]] = [{
                    "id": results_root_id,
                    "name": results_root_name,
                    "path": results_root_name,
                }]
                for folder in sorted(project_folders, key=lambda x: clean_text(x.get("name")).lower()):
                    folder_options.append({
                        "id": clean_text(folder.get("id")),
                        "name": clean_text(folder.get("name")),
                        "path": f"{results_root_name}/{clean_text(folder.get('name'))}",
                    })

                option_by_id = {item["id"]: item for item in folder_options}
                pending_target_id = clean_text(st.session_state.pop("pending_drive_target_folder_id", ""))
                if pending_target_id:
                    pending_name = clean_text(st.session_state.pop("pending_drive_target_folder_name", ""))
                    pending_path = clean_text(st.session_state.pop("pending_drive_target_folder_path", ""))
                    if pending_target_id not in option_by_id:
                        item = {
                            "id": pending_target_id,
                            "name": pending_name or "Jaunā projekta mape",
                            "path": pending_path or f"{results_root_name}/{pending_name}",
                        }
                        folder_options.append(item)
                        option_by_id[pending_target_id] = item
                    st.session_state["drive_target_folder_id"] = pending_target_id

                active_project_name = (
                    clean_text(st.session_state.get("applied_project_filter"))
                    or clean_text(st.session_state.get("selected_project_filter"))
                )
                active_project_code = normalize_project_code(active_project_name)
                preferred_folder = None
                if active_project_code:
                    preferred_folder = find_project_folder(oauth_service, results_root_id, active_project_code)

                current_target_id = clean_text(st.session_state.get("drive_target_folder_id"))
                if current_target_id not in option_by_id:
                    preferred_id = clean_text((preferred_folder or {}).get("id"))
                    st.session_state["drive_target_folder_id"] = (
                        preferred_id if preferred_id in option_by_id else results_root_id
                    )

                selected_target_id = st.selectbox(
                    "Kurā 02_Results projekta mapē saglabāt koriģētos PDF?",
                    options=[item["id"] for item in folder_options],
                    format_func=lambda folder_id: option_by_id[folder_id]["path"],
                    key="drive_target_folder_id",
                )
                selected_target = option_by_id[selected_target_id]
                st.info(f"Izvēlētais PDF galamērķis: {selected_target['path']}")

                with st.expander("+ Izveidot jaunu projekta mapi 02_Results", expanded=False):
                    suggested_name = active_project_name if active_project_name and active_project_name != "01_Input sakne" else "Jauns_projekts"
                    new_folder_name = st.text_input(
                        "Jaunās projekta mapes nosaukums",
                        value=suggested_name,
                        key="new_drive_folder_name",
                    )
                    st.caption(f"Jaunā mape tiks izveidota tieši zem: {results_root_name}")
                    create_col, _ = st.columns([1, 4])
                    with create_col:
                        create_folder_clicked = st.button(
                            "Izveidot projekta mapi",
                            type="primary",
                            use_container_width=True,
                            key="create_new_drive_target_folder",
                        )
                    if create_folder_clicked:
                        created = drive_create_folder(oauth_service, results_root_id, new_folder_name)
                        created_id = clean_text(created.get("id"))
                        created_name = clean_text(created.get("name"))
                        st.session_state["pending_drive_target_folder_id"] = created_id
                        st.session_state["pending_drive_target_folder_name"] = created_name
                        st.session_state["pending_drive_target_folder_path"] = f"{results_root_name}/{created_name}"
                        st.rerun()

                st.caption(
                    f"Memory Excel projekta mape tiks sasaistīta ar izvēlēto rezultātu projektu: "
                    f"{selected_target['name']}"
                )

                action_col1, action_col2, _ = st.columns([1, 1.35, 3])
                with action_col1:
                    test_drive_write_clicked = st.button(
                        "Testēt rakstīšanu",
                        use_container_width=True,
                        key=f"test_oauth_drive_write_button_{audit_run_id}",
                    )
                with action_col2:
                    save_audit_clicked = st.button(
                        "Saglabāt audita failus Google Drive",
                        type="primary",
                        use_container_width=True,
                        key=f"save_audit_files_drive_{audit_run_id}",
                    )

                if test_drive_write_clicked:
                    st.session_state.drive_write_test_result = None
                    st.session_state.drive_write_test_error = ""
                    try:
                        with st.spinner("Izveidoju testa failu izvēlētajā mapē..."):
                            result = run_drive_write_test_to_folder(
                                oauth_service, selected_target_id, selected_target["path"]
                            )
                        st.session_state.drive_write_test_result = result
                    except Exception as exc:
                        st.session_state.drive_write_test_error = str(exc)

                if save_audit_clicked:
                    st.session_state.drive_save_result = None
                    st.session_state.drive_save_error = ""
                    try:
                        if not selected_pdf_items:
                            raise RuntimeError("Nav izvēlētu auditējamo PDF failu, ko saglabāt.")
                        with st.spinner("Saglabāju PDF un Memory Excel Google Drive..."):
                            result = upload_audit_files_to_drive(
                                oauth_service,
                                results_target=selected_target,
                                memory_folder_id=memory_folder_id.strip(),
                                project_folder_name=(
                                    clean_text(st.session_state.get("active_project_memory_code"))
                                    or selected_target["name"]
                                ),
                                accepted_df=accepted_df,
                                rejected_df=rejected_df,
                                pdf_items=selected_pdf_items,
                            )
                        active_memory_id = clean_text(
                            st.session_state.get("active_project_memory_id")
                        )
                        active_memory_code = normalize_project_code(
                            st.session_state.get("active_project_memory_code")
                        )
                        if active_memory_id and active_memory_code:
                            memory_update = update_project_memory_after_audit(
                                oauth_service,
                                project_folder_id=active_memory_id,
                                project_code=active_memory_code,
                                audit_run_id=audit_run_id,
                                pdf_items=selected_pdf_items,
                                accepted_df=accepted_df,
                                rejected_df=rejected_df,
                            )
                            result["project_memory"] = memory_update
                            st.session_state.project_memory_update_result = memory_update
                            st.session_state.active_project_memory_manifest = (
                                memory_update.get("manifest") or {}
                            )
                        st.session_state.drive_save_result = result
                    except Exception as exc:
                        st.session_state.drive_save_error = str(exc)

            except Exception as exc:
                st.error(f"Neizdevās sagatavot Drive mapju izvēli: {exc}")
                with st.expander("Drive mapju izvēles traceback"):
                    st.code(traceback.format_exc())

        write_test_error = clean_text(st.session_state.get("drive_write_test_error"))
        if write_test_error:
            st.error("OAuth Drive rakstīšanas tests neizdevās.")
            st.code(write_test_error)

        write_test_result = st.session_state.get("drive_write_test_result")
        if isinstance(write_test_result, dict) and write_test_result:
            test_file = write_test_result.get("file") or {}
            st.success(f"Drive rakstīšanas tests izdevās: {clean_text(test_file.get('name'))}")
            if clean_text(test_file.get("webViewLink")):
                st.markdown(f"[Atvērt testa failu Google Drive]({clean_text(test_file.get('webViewLink'))})")

        save_error = clean_text(st.session_state.get("drive_save_error"))
        if save_error:
            st.error("Audita failu saglabāšana Google Drive neizdevās.")
            if "cannot be used in worksheets" in save_error.lower():
                st.warning(
                    "PDF teksta slānī bija Excel neatļautas vadības rakstzīmes. "
                    "v0.7.1 tās pirms XLSX saglabāšanas automātiski iztīra."
                )
            st.code(save_error)

        save_result = st.session_state.get("drive_save_result")
        if isinstance(save_result, dict) and save_result:
            result_files = save_result.get("results_files") or []
            memory_files = save_result.get("memory_files") or []
            st.success(
                f"Saglabāšana pabeigta: {len(result_files)} PDF rezultātu faili un "
                f"{len(memory_files)} Memory Excel faili."
            )

            st.markdown("#### PDF eksporta pārbaude")
            for result_file in result_files:
                source = clean_text(result_file.get("source")) or clean_text(result_file.get("name"))
                audit_status = clean_text(result_file.get("audit_status"))
                accepted_count = int(result_file.get("accepted_count") or 0)
                banner_verified = bool(result_file.get("banner_verified"))
                if audit_status == "no_findings":
                    if banner_verified:
                        st.success(
                            f"{source} — 0 akceptētas piezīmes — "
                            "dzeltenā PDF piezīmes ikona ir pievienota un pārbaudīta."
                        )
                    else:
                        st.warning(
                            f"{source} — 0 akceptētas piezīmes — "
                            "PDF piezīmes ikona izveidota, bet automātiskā anotācijas pārbaude to neapstiprināja."
                        )
                else:
                    st.info(f"{source} — akceptētas piezīmes: {accepted_count}.")

            project_memory_result = save_result.get("project_memory") or {}
            if project_memory_result:
                st.success(
                    "Projekta atmiņa papildināta: "
                    f"{project_memory_result.get('documents', 0)} reģistrēti dokumenti."
                )
                disciplines = project_memory_result.get("disciplines") or []
                if disciplines:
                    st.caption("Atmiņā reģistrētās disciplīnas: " + ", ".join(disciplines))

            for file_info in result_files + memory_files:
                name = clean_text(file_info.get("name"))
                path = clean_text(file_info.get("destination_path"))
                link = clean_text(file_info.get("webViewLink"))
                if link:
                    st.markdown(f"- [{name}]({link}) — `{path}`")
                else:
                    st.markdown(f"- {name} — `{path}`")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        st.error(f"Script execution error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())
