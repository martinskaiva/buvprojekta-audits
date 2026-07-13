# app_bp_audit_copilot.py
# ------------------------------------------------------------
# BP AI Audit Copilot v0.1
#
# Mērķis:
# - nolasa PDF failus no 01_Input Google Drive mapes;
# - nolasa zelta piemērus no 03_Memory/audit_examples/**/*.xlsx;
# - nolasa negatīvos piemērus no 03_Memory/audit_feedback, ja tādi ir;
# - iekšēji pārbauda visas kļūdu ģimenes pa batchiem;
# - ģenerē kandidātpiezīmes ChatGPT-stila kartītēs;
# - lietotājs akceptē/noraida katru kandidātu;
# - akceptētās piezīmes eksportē 16 kolonnu Excel markup formātā;
# - noraidītās piezīmes eksportē kā rejected_patterns.xlsx/json.
#
# SVARĪGI:
# - v0.1 ir kandidātu ģenerators, nevis automātisks gala audits.
# - Drive piekļuve šajā versijā ir read-only. Rezultātus lejupielādē ZIP failā
#   un, ja vajag, manuāli ielādē 02_Results / 03_Memory/audit_feedback.
# - app_bp_audit_markup.py paliek atsevišķs rīks PDF komentāru uzlikšanai.
#
# Streamlit secrets atbalsts:
#   GOOGLE_SERVICE_ACCOUNT_JSON = '{...}'
#   OPENAI_API_KEY = '...'
#   GOOGLE_DRIVE_INPUT_FOLDER_ID = '...'
#   GOOGLE_DRIVE_MEMORY_FOLDER_ID = '...'
#
# Alternatīvi service account var būt TOML tabulā:
#   [google_service_account]
#   type = "service_account"
#   ...
# ------------------------------------------------------------

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI


APP_TITLE = "BP AI Audit Copilot v0.1"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PDF = "application/pdf"

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

FAMILY_ORDER = [
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
        "label": "Teksta, gramatikas, terminoloģijas un mērvienību kļūdas",
        "look_for": "drukas kļūdas, nepareizi vārdi, locījumi, tehniskie termini, mērvienības, simboli, nepabeigti teikumi, neskaidri formulējumi",
        "report_when": "kļūda ir oficiālā dokumenta tekstā, virsrakstā, tabulā, piezīmē vai specifikācijā un var radīt neprofesionālu vai tehniski neskaidru uztveri",
        "do_not_report": "neziņo tikai stila gaumes jautājumus vai nebūtiskas kļūdas, kas nemaina saprotamību",
    },
    "B_lv_en": {
        "label": "Latviešu un angļu teksta neatbilstības",
        "look_for": "LV/EN nosaukumu, tehnisko tulkojumu, skaitļu, parametru un nozīmes neatbilstības",
        "report_when": "angļu teksts nozīmē ko citu nekā latviešu teksts vai tehniskais tulkojums ir maldinošs",
        "do_not_report": "neziņo stilistiski atšķirīgu, bet tehniski pareizu tulkojumu",
    },
    "C_dates_versions": {
        "label": "Datumu, versiju un revīziju neatbilstības",
        "look_for": "atšķirīgus datumus, revīzijas, versijas, vecas atsauces uz iepriekšējiem izlaidumiem",
        "report_when": "vienā dokumentā vai saistītā dokumentu kopā datumi/revīzijas savstarpēji konfliktē",
        "do_not_report": "neziņo vēsturisku atsauces datumu, ja nav pierādījuma, ka tam jābūt vienādam ar izlaiduma datumu",
    },
    "D_document_identity": {
        "label": "Dokumenta identitātes neatbilstības",
        "look_for": "faila nosaukuma, titullauka, dokumenta koda, sadaļas koda, projekta koda un rasējuma nosaukuma neatbilstības",
        "report_when": "faila nosaukums un dokumentā redzamais numurs/nosaukums/kods konfliktē",
        "do_not_report": "neziņo '2/2' kā lapu skaita kļūdu, ja tas apzīmē būvprojekta kārtu; neziņo tikai punktu/pasvītru atšķirības faila nosaukumā",
    },
    "E_drawing_list_references": {
        "label": "Rasējumu saraksti un savstarpējās atsauces",
        "look_for": "rasējumu saraksta neatbilstības, atsauces uz neesošiem vai nepareiziem dokumentiem",
        "report_when": "sarakstā vai atsaucē minēts dokuments/kods, kas neatbilst faktiskajam dokumentam vai pieejamajai dokumentu kopai",
        "do_not_report": "neziņo, ja salīdzināmais saraksts vai atsauces dokuments nav pieejams",
    },
    "F_normative_references": {
        "label": "Normatīvu atsauces",
        "look_for": "nepareizus normatīvu numurus, nosaukumus, savstarpējas pretrunas normatīvu atsaucēs",
        "report_when": "normatīva numurs un nosaukums acīmredzami neatbilst vai saistītajos dokumentos minēts citādi",
        "do_not_report": "neziņo spēkā esamības jautājumus, ja vajadzīga ārēja normatīvu pārbaude",
    },
    "G_material_type_model": {
        "label": "Materiālu, tipu, modeļu un tehnisko parametru neatbilstības",
        "look_for": "materiālu, tipu, modeļu, diametru, klašu, marku, izmēru un parametru konfliktus",
        "report_when": "vienā vietā norādīts viens materiāls/tips/parametrs, citā vietā cits, un ir salīdzināms avots",
        "do_not_report": "neziņo, ja atšķirība var būt vispārīgs apraksts pret detalizētu specifikāciju un nav skaidra konflikta",
    },
    "H_quantity_position": {
        "label": "Daudzumu, pozīciju un numerācijas neatbilstības",
        "look_for": "daudzumu neatbilstības, atkārtotus/trūkstošus pozīciju numurus, nepareizu elementu skaitu",
        "report_when": "no dokumenta teksta/tabulām droši redzams skaita vai pozīcijas konflikts",
        "do_not_report": "neziņo, ja vajadzīga grafiska mērīšana vai manuāla elementu skaitīšana, ko PDF teksts nedod",
    },
    "I_specification_coverage": {
        "label": "Specifikācijas pārklājuma trūkumi",
        "look_for": "rasējumā vai piezīmēs esošus elementus, kas nav specifikācijā; trūkstošas iekārtas, materiālus, komponentes",
        "report_when": "ir pieejama specifikācija vai tabula un skaidri redzams, ka elements/risinājums nav iekļauts",
        "do_not_report": "neziņo, ja specifikācija nav pieejama vai elements var būt apvienotā pozīcijā",
    },
    "J_cross_document_traceability": {
        "label": "Izsekojamība starp dokumentiem",
        "look_for": "sistēmu kodu, plānu, profilu, SA, specifikāciju un citu dokumentu savstarpējas neatbilstības",
        "report_when": "vienā dokumentā minēts risinājums/sistēma, bet citā saistītā dokumentā to nevar izsekot vai tas konfliktē",
        "do_not_report": "neziņo, ja auditēts tikai viens dokuments un nav salīdzināmo failu",
    },
    "K_solution_or_graphic_clarity": {
        "label": "Risinājuma vai grafiskās skaidrības problēmas",
        "look_for": "neskaidras atsauces, placeholder zīmes, nepabeigtus apzīmējumus, neskaidrus mezglus/risinājumus",
        "report_when": "dokumentā palicis '?', 'XX', 'TODO' vai apzīmējums nav saprotams bez papildinformācijas",
        "do_not_report": "neziņo, ja neskaidrība var būt tikai PDF kvalitātes vai teksta ekstrakcijas problēma",
    },
    "L_fire_safety_or_regulatory_logic": {
        "label": "Ugunsdrošības vai regulatīvās loģikas neatbilstības",
        "look_for": "ugunsdrošības, evakuācijas, ugunsnodalījumu, regulatīvu risinājumu pretrunas",
        "report_when": "dokumentā redzama konkrēta pretruna starp prasību, aprakstu un risinājumu",
        "do_not_report": "neziņo normatīvu interpretāciju bez pietiekama dokumenta konteksta",
    },
    "M_scope_or_discipline_boundary": {
        "label": "Sadaļas tvēruma un disciplīnu robežas",
        "look_for": "nepareizu informāciju nepareizā sadaļā, disciplīnu robežu sajaukumu, citas sadaļas risinājumus",
        "report_when": "sadaļas saturs vai atbildība skaidri neatbilst dokumenta disciplīnai",
        "do_not_report": "neziņo vispārīgas koordinācijas piezīmes, kur citas disciplīnas pieminēšana ir nepieciešama",
    },
    "N_completeness_or_missing_content": {
        "label": "Nepabeigts vai trūkstošs saturs",
        "look_for": "tukšus laukus, placeholder tekstu, nepabeigtus teikumus, trūkstošas sadaļas, nepilnīgas tabulas",
        "report_when": "redzams tukšs obligāts lauks, nepabeigts apzīmējums vai satura rādītājā minēta sadaļa bez satura",
        "do_not_report": "neziņo, ja nav skaidrs, vai lauks attiecīgajā dokumentā ir obligāts",
    },
}


@dataclass
class DriveItem:
    id: str
    name: str
    mimeType: str
    path: str
    modifiedTime: Optional[str] = None
    size: Optional[str] = None


# -----------------------------
# Google Drive helpers
# -----------------------------

@st.cache_resource(show_spinner=False)
def get_drive_service():
    sa_info = None

    service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", None)
    if service_account_json:
        try:
            sa_info = json.loads(service_account_json)
        except Exception as exc:
            st.error(f"GOOGLE_SERVICE_ACCOUNT_JSON nav derīgs JSON: {exc}")
            st.stop()

    if sa_info is None:
        for key in ("google_service_account", "gcp_service_account", "service_account"):
            if key in st.secrets:
                sa_info = dict(st.secrets[key])
                break

    if sa_info is None:
        st.error("Nav atrasts Google service account. Pievieno GOOGLE_SERVICE_ACCOUNT_JSON vai [google_service_account] Streamlit secrets.")
        st.stop()

    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_secret_value(*names: str, default: str = "") -> str:
    for name in names:
        if name in st.secrets:
            return str(st.secrets.get(name, ""))
    if "app" in st.secrets:
        app_section = st.secrets["app"]
        for name in names:
            short = name.lower().replace("google_drive_", "").replace("_folder_id", "_folder_id")
            if name in app_section:
                return str(app_section[name])
            if short in app_section:
                return str(app_section[short])
    return default


def list_children(service, folder_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"
    while True:
        resp = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


@st.cache_data(show_spinner=False, ttl=600)
def list_drive_recursive(folder_id: str, only_mimes: Optional[Tuple[str, ...]] = None) -> List[Dict[str, Any]]:
    service = get_drive_service()
    out: List[DriveItem] = []

    def walk(current_id: str, current_path: str):
        for raw in list_children(service, current_id):
            item_path = f"{current_path}/{raw['name']}" if current_path else raw["name"]
            if raw["mimeType"] == MIME_FOLDER:
                walk(raw["id"], item_path)
                continue
            if only_mimes is None or raw["mimeType"] in only_mimes:
                out.append(
                    DriveItem(
                        id=raw["id"],
                        name=raw["name"],
                        mimeType=raw["mimeType"],
                        path=item_path,
                        modifiedTime=raw.get("modifiedTime"),
                        size=raw.get("size"),
                    )
                )

    walk(folder_id, "")
    return [x.__dict__ for x in out]


def download_drive_file(file_id: str) -> bytes:
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return fh.getvalue()


# -----------------------------
# Data loading and normalization
# -----------------------------

def safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def normalize_text(value: Any) -> str:
    text = safe_str(value).lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def infer_discipline_from_path(path: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    parts = path.replace("\\", "/").split("/")
    for part in parts:
        if re.match(r"^\d{2}(_\d)?_[A-ZĀČĒĢĪĶĻŅŠŪŽA-Z\-]+", part, re.IGNORECASE):
            return part
    return "UNKNOWN"


def infer_document_role(filename: str, text_hint: str = "") -> str:
    s = normalize_text(filename + " " + text_hint)
    rules = [
        ("specification", ["specifik", "specification", "ms_", "material", "iekārtu", "materiāl"]),
        ("drawing_list", ["rasējumu sarak", "drawing list", "saraksts", "td_", "list"]),
        ("site_plan", ["ģenpl", "general plan", "site plan", "plan", "plāns"]),
        ("profile", ["profile", "profils"]),
        ("section_or_detail", ["detail", "mezgl", "section", "šķērsgriez"]),
        ("explanatory_description", ["skaidrojo", "explanatory", "description", "apraksts", "sa_"]),
        ("general_data", ["general data", "vispār", "gd_"]),
        ("isometry", ["isometry", "izometrij"]),
    ]
    for role, needles in rules:
        if any(n in s for n in needles):
            return role
    return "other"


def normalize_family(issue_type: str, comment_text: str, target_text: str, comparison: str) -> str:
    s = normalize_text(" ".join([issue_type, comment_text, target_text, comparison]))

    issue_map = {
        "text_error": "A_text_language",
        "architecture_description_text_error": "A_text_language",
        "technical_notation_error": "A_text_language",
        "technical_term_error": "A_text_language",
        "technical_text_error": "A_text_language",
        "technical_note_language_error": "A_text_language",
        "official_note_language_error": "A_text_language",
        "orientation_text_error": "A_text_language",
        "title_text_error": "A_text_language",
        "language/document_title": "A_text_language",
        "drawing_text_error": "A_text_language",
        "system_code_mismatch": "J_cross_document_traceability",
        "signal_traceability": "J_cross_document_traceability",
        "traceability_issue": "J_cross_document_traceability",
        "door_hardware_code_ambiguity": "K_solution_or_graphic_clarity",
        "designation_inconsistency": "K_solution_or_graphic_clarity",
        "formatting_incomplete": "N_completeness_or_missing_content",
        "incomplete_text": "N_completeness_or_missing_content",
        "missing_document_fields": "N_completeness_or_missing_content",
        "placeholder_text": "N_completeness_or_missing_content",
        "placeholder_left_in_drawing": "N_completeness_or_missing_content",
        "nepabeigts telpas/laukuma apzīmējums": "N_completeness_or_missing_content",
        "discipline_scope_mismatch": "M_scope_or_discipline_boundary",
        "document_completeness_mismatch": "N_completeness_or_missing_content",
        "missing_from_specification": "I_specification_coverage",
        "technical_conflict": "G_material_type_model",
        "technical_parameter_check": "G_material_type_model",
    }
    issue_key = normalize_text(issue_type)
    if issue_key in issue_map:
        return issue_map[issue_key]

    if any(x in s for x in ["nav specifik", "trūkst specifik", "missing from specification", "missing_from_specification", "nav iekļauts specifik"]):
        return "I_specification_coverage"
    if any(x in s for x in ["lv/en", "latviešu", "angļu", "english", "translation", "tulkoj"]):
        return "B_lv_en"
    if any(x in s for x in ["datums", "date", "revīzij", "revision", "versij", "izlaid"]):
        return "C_dates_versions"
    if any(x in s for x in ["faila nosauk", "titullauk", "document number", "drawing number", "projekta kod", "vecais kod", "document identity"]):
        return "D_document_identity"
    if any(x in s for x in ["rasējumu sarak", "drawing list", "atsauce", "reference", "neeksist", "sarakstā"]):
        return "E_drawing_list_references"
    if any(x in s for x in ["lbn", "eurocode", "normat", "standart", "regula"]):
        return "F_normative_references"
    if any(x in s for x in ["materiāl", "tips", "type", "model", "diametr", "dn", "klase", "marka", "parametr", "technical_conflict"]):
        return "G_material_type_model"
    if any(x in s for x in ["daudzum", "quantity", "pozīc", "numbering", "skaits", "atkārtojas"]):
        return "H_quantity_position"
    if any(x in s for x in ["izsekojam", "traceability", "sistēmas kod", "system code", "plānā", "profilā"]):
        return "J_cross_document_traceability"
    if any(x in s for x in ["?", "xx", "todo", "placeholder", "neskaidr", "apzīmēj", "designation"]):
        return "K_solution_or_graphic_clarity"
    if any(x in s for x in ["uguns", "evakuāc", "fire", "fire safety"]):
        return "L_fire_safety_or_regulatory_logic"
    if any(x in s for x in ["disciplīn", "scope", "robež", "sadaļas tvērums"]):
        return "M_scope_or_discipline_boundary"
    if any(x in s for x in ["nepabeigt", "trūkst", "tukš", "incomplete", "missing_document"]):
        return "N_completeness_or_missing_content"
    if any(x in s for x in ["drukas", "gramat", "termin", "mērvien", "simbol", "wording", "text"]):
        return "A_text_language"
    return "A_text_language"


def family_to_scenario(family: str) -> str:
    scenario_map = {
        "A_text_language": "SC-A01_text_language_or_unit_error",
        "B_lv_en": "SC-B01_lv_en_content_or_translation_mismatch",
        "C_dates_versions": "SC-C01_date_revision_version_mismatch",
        "D_document_identity": "SC-D01_file_title_code_identity_mismatch",
        "E_drawing_list_references": "SC-E01_drawing_list_or_reference_mismatch",
        "F_normative_references": "SC-F01_normative_reference_mismatch",
        "G_material_type_model": "SC-G01_material_type_model_parameter_mismatch",
        "H_quantity_position": "SC-H01_quantity_position_numbering_mismatch",
        "I_specification_coverage": "SC-I01_missing_from_specification",
        "J_cross_document_traceability": "SC-J01_cross_document_traceability_issue",
        "K_solution_or_graphic_clarity": "SC-K01_unclear_solution_or_graphic_reference",
        "L_fire_safety_or_regulatory_logic": "SC-L01_fire_safety_or_regulatory_logic_conflict",
        "M_scope_or_discipline_boundary": "SC-M01_wrong_discipline_scope",
        "N_completeness_or_missing_content": "SC-N01_incomplete_or_missing_content",
    }
    return scenario_map.get(family, "SC-A01_text_language_or_unit_error")


@st.cache_data(show_spinner=False, ttl=600)
def load_audit_examples(memory_folder_id: str) -> pd.DataFrame:
    items = list_drive_recursive(memory_folder_id, only_mimes=(MIME_XLSX,))
    example_items = [x for x in items if "/audit_examples/" in f"/{x['path']}".replace("\\", "/")]
    rows: List[pd.DataFrame] = []

    progress = st.progress(0, text="Nolasu audit_examples Excel failus...")
    for i, item in enumerate(example_items):
        try:
            data = download_drive_file(item["id"])
            df = pd.read_excel(io.BytesIO(data), dtype=str)
            df.columns = [safe_str(c) for c in df.columns]
            if not set(REQUIRED_COLUMNS).issubset(set(df.columns)):
                continue
            df = df[REQUIRED_COLUMNS].copy()
            df = df.dropna(how="all")
            if df.empty:
                continue
            df["source_excel_file"] = item["name"]
            df["source_path"] = item["path"]
            df["document_role"] = df.apply(
                lambda r: infer_document_role(safe_str(r.get("target_file")), safe_str(r.get("target_area"))), axis=1
            )
            df["normalized_family"] = df.apply(
                lambda r: normalize_family(
                    r.get("issue_type"),
                    r.get("comment_text"),
                    r.get("target_text"),
                    r.get("comparison_evidence"),
                ),
                axis=1,
            )
            df["normalized_scenario"] = df["normalized_family"].map(family_to_scenario)
            rows.append(df)
        except Exception as exc:
            st.warning(f"Neizdevās nolasīt {item['path']}: {exc}")
        progress.progress((i + 1) / max(len(example_items), 1), text=f"Nolasu audit_examples: {i+1}/{len(example_items)}")
    progress.empty()

    if not rows:
        return pd.DataFrame(columns=REQUIRED_COLUMNS + ["source_excel_file", "source_path", "document_role", "normalized_family", "normalized_scenario"])
    return pd.concat(rows, ignore_index=True)


@st.cache_data(show_spinner=False, ttl=600)
def load_rejected_patterns(memory_folder_id: str) -> pd.DataFrame:
    items = list_drive_recursive(memory_folder_id, only_mimes=(MIME_XLSX,))
    feedback_items = [x for x in items if "/audit_feedback/" in f"/{x['path']}".replace("\\", "/") and "rejected" in x["name"].lower()]
    rows: List[pd.DataFrame] = []
    for item in feedback_items:
        try:
            data = download_drive_file(item["id"])
            df = pd.read_excel(io.BytesIO(data), dtype=str)
            df["source_feedback_file"] = item["name"]
            df["source_path"] = item["path"]
            rows.append(df)
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


# -----------------------------
# PDF extraction
# -----------------------------

def extract_pdf_blocks(pdf_bytes: bytes, filename: str, max_pages: int = 80) -> Tuple[pd.DataFrame, str]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    rows: List[Dict[str, Any]] = []
    page_texts: List[str] = []
    for page_index in range(min(len(doc), max_pages)):
        page = doc[page_index]
        blocks = page.get_text("blocks")
        page_lines = []
        for block_idx, block in enumerate(blocks):
            x0, y0, x1, y1, text, *_ = block
            text = re.sub(r"\s+", " ", safe_str(text)).strip()
            if not text:
                continue
            rows.append(
                {
                    "source_file": filename,
                    "page": page_index + 1,
                    "block_id": block_idx,
                    "x0": round(float(x0), 2),
                    "y0": round(float(y0), 2),
                    "x1": round(float(x1), 2),
                    "y1": round(float(y1), 2),
                    "text": text,
                }
            )
            page_lines.append(text)
        page_texts.append(f"\n--- PAGE {page_index + 1} ---\n" + "\n".join(page_lines))
    doc.close()
    full_text = "\n".join(page_texts)
    return pd.DataFrame(rows), full_text


def truncate_text(text: str, max_chars: int) -> str:
    text = safe_str(text)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[TRUNCATED]"


def build_pdf_context(blocks_df: pd.DataFrame, full_text: str, max_chars: int) -> str:
    if blocks_df.empty:
        return truncate_text(full_text, max_chars)
    lines = []
    for _, row in blocks_df.head(500).iterrows():
        lines.append(f"Lapa {row['page']}, bloks {row['block_id']}: {row['text']}")
    context = "\n".join(lines)
    if len(context) < max_chars // 2:
        context += "\n\nPILNS TEKSTS:\n" + full_text
    return truncate_text(context, max_chars)


# -----------------------------
# Example retrieval
# -----------------------------

def retrieve_examples(examples_df: pd.DataFrame, family: str, document_role: str, limit: int = 8) -> pd.DataFrame:
    if examples_df.empty:
        return examples_df
    df = examples_df.copy()
    df["score"] = 0
    df.loc[df["normalized_family"] == family, "score"] += 10
    df.loc[df["document_role"] == document_role, "score"] += 4
    df.loc[df["markup_type"].isin(["highlight", "rectangle"]), "score"] += 1
    df.loc[df["placement_confidence"].isin(["exact", "approximate"]), "score"] += 1
    df = df[df["score"] > 0].sort_values(["score", "source_path"], ascending=[False, True])
    return df.head(limit)


def examples_to_prompt(examples: pd.DataFrame) -> str:
    if examples.empty:
        return "Nav atrasti līdzīgi zelta piemēri šai ģimenei."
    chunks = []
    for i, (_, r) in enumerate(examples.iterrows(), start=1):
        chunks.append(
            f"PIEMĒRS {i}\n"
            f"family: {safe_str(r.get('normalized_family'))}\n"
            f"scenario: {safe_str(r.get('normalized_scenario'))}\n"
            f"document_role: {safe_str(r.get('document_role'))}\n"
            f"target_text: {safe_str(r.get('target_text'))}\n"
            f"comment_text: {safe_str(r.get('comment_text'))}\n"
            f"issue_type: {safe_str(r.get('issue_type'))}\n"
            f"comparison_evidence: {safe_str(r.get('comparison_evidence'))}\n"
            f"markup_type: {safe_str(r.get('markup_type'))}\n"
            f"placement_confidence: {safe_str(r.get('placement_confidence'))}"
        )
    return "\n\n".join(chunks)


def rejected_to_prompt(rejected_df: pd.DataFrame, family: str, limit: int = 12) -> str:
    if rejected_df.empty:
        return "Nav noraidīto patternu."
    df = rejected_df.copy()
    cols = [c for c in df.columns if c in ["normalized_family", "family", "issue_type", "title", "target_text", "comment_text", "reason", "do_not_show_similar"]]
    if "normalized_family" in df.columns:
        df = df[df["normalized_family"].fillna("").eq(family) | df["normalized_family"].fillna("").eq("")]
    if df.empty:
        return "Nav noraidīto patternu šai ģimenei."
    lines = []
    for i, (_, r) in enumerate(df.head(limit).iterrows(), start=1):
        lines.append("; ".join([f"{c}: {safe_str(r.get(c))}" for c in cols]))
    return "\n".join(lines)


# -----------------------------
# OpenAI call
# -----------------------------

def get_openai_client() -> Optional[OpenAI]:
    api_key = st.secrets.get("OPENAI_API_KEY", None) or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    return OpenAI(api_key=api_key)


def parse_json_candidates(raw: str) -> List[Dict[str, Any]]:
    raw = safe_str(raw)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[\s*\{.*\}\s*\]", raw, flags=re.DOTALL)
        if not match:
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("AI atbilde nesatur derīgu JSON.")
        data = json.loads(match.group(0))
    if isinstance(data, dict) and "candidates" in data:
        data = data["candidates"]
    if not isinstance(data, list):
        raise ValueError("AI JSON nav saraksts.")
    return [x for x in data if isinstance(x, dict)]


def call_ai_for_family(
    client: OpenAI,
    model: str,
    family: str,
    filename: str,
    document_role: str,
    pdf_context: str,
    examples_prompt: str,
    rejected_prompt: str,
    max_candidates: int,
) -> List[Dict[str, Any]]:
    instr = FAMILY_INSTRUCTIONS[family]
    system = (
        "Tu esi būvprojekta kvalitātes audita asistents Latvijā. "
        "Tu ģenerē tikai kandidātpiezīmes cilvēka pārbaudei. "
        "Nedrīkst izdomāt faktus. Ziņo tikai tad, ja PDF tekstā ir pierādāms target_text, lapa vai skaidra zona. "
        "Atbildei jābūt tikai derīgam JSON sarakstam. Nekāds Markdown ārpus JSON."
    )
    user = f"""
Auditējamais fails: {filename}
Atpazītais dokumenta tips: {document_role}

Kļūdu ģimene: {family} — {instr['label']}
Meklē: {instr['look_for']}
Ziņot, ja: {instr['report_when']}
Neziņot, ja: {instr['do_not_report']}

Līdzīgi akceptētie audit_examples:
{examples_prompt}

Noraidītie / nerādāmie patterni:
{rejected_prompt}

PDF teksts un bloki:
{pdf_context}

Uzdevums:
Atrodi ne vairāk kā {max_candidates} kvalitatīvas kandidātpiezīmes tikai šai kļūdu ģimenei.
Ja nav pietiekama pierādījuma, atgriez tukšu sarakstu [].

Katram kandidātam obligāti norādi šādus laukus:
- title: īss virsraksts latviski
- kur: lapa un zona cilvēkam saprotami
- status: kļūdas tips / risks
- problem: kas tieši nav pareizi
- why_important: kāpēc tas ir būtiski
- designer_note: īsa gatava piezīme projektētājam
- target_file: precīzs faila nosaukums
- target_page: lapas numurs kā vesels skaitlis vai tukšs, ja nav droši
- target_area: zona, tabula, titullauks, piezīme vai bloks
- target_text: precīzs teksts no PDF, ko var mēģināt izcelt; ja nav, MANUAL_PLACEMENT_REQUIRED
- issue_type: īss machine-readable tips, piemēram text_error, lv_en_translation_error
- severity: low, medium vai high
- comparison_files: salīdzināmie faili, ja ir; citādi tukšs
- comparison_pages: salīdzināmās lapas, ja ir; citādi tukšs
- comparison_evidence: īss pierādījums / problēmas skaidrojums PDF komentāra sadaļai "Komentārs"
- markup_type: highlight, rectangle, sticky_note vai page_note
- placement_confidence: exact, approximate vai manual_needed
- normalized_family: {family}
- normalized_scenario: {family_to_scenario(family)}

Atbildes JSON piemērs:
[
  {{
    "title": "...",
    "kur": "1. lapa, titullauks",
    "status": "...",
    "problem": "...",
    "why_important": "...",
    "designer_note": "...",
    "target_file": "{filename}",
    "target_page": 1,
    "target_area": "titullauks",
    "target_text": "...",
    "issue_type": "...",
    "severity": "medium",
    "comparison_files": "",
    "comparison_pages": "",
    "comparison_evidence": "...",
    "markup_type": "highlight",
    "placement_confidence": "exact",
    "normalized_family": "{family}",
    "normalized_scenario": "{family_to_scenario(family)}"
  }}
]
"""
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content or ""
    parsed = parse_json_candidates(raw)
    # Dažreiz json_object režīmā modelis atgriež {"candidates": [...]}.
    cleaned = []
    for item in parsed:
        item["normalized_family"] = family
        item["normalized_scenario"] = family_to_scenario(family)
        item["target_file"] = item.get("target_file") or filename
        cleaned.append(item)
    return cleaned


# -----------------------------
# Candidate and export helpers
# -----------------------------

def make_candidate_id(file_name: str, idx: int) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", file_name).strip("_")[:40]
    return f"AI-{base}-{idx:03d}"


def normalize_candidate(candidate: Dict[str, Any], filename: str, idx: int) -> Dict[str, Any]:
    family = safe_str(candidate.get("normalized_family")) or normalize_family(
        candidate.get("issue_type"), candidate.get("designer_note"), candidate.get("target_text"), candidate.get("comparison_evidence")
    )
    target_page = candidate.get("target_page", "")
    try:
        if safe_str(target_page):
            target_page = int(float(str(target_page).replace(",", ".")))
    except Exception:
        target_page = ""
    out = {
        "candidate_id": make_candidate_id(filename, idx),
        "title": safe_str(candidate.get("title")),
        "kur": safe_str(candidate.get("kur")),
        "status": safe_str(candidate.get("status")),
        "problem": safe_str(candidate.get("problem")),
        "why_important": safe_str(candidate.get("why_important")),
        "designer_note": safe_str(candidate.get("designer_note") or candidate.get("comment_text")),
        "target_file": safe_str(candidate.get("target_file")) or filename,
        "target_page": target_page,
        "target_area": safe_str(candidate.get("target_area")),
        "target_text": safe_str(candidate.get("target_text")) or "MANUAL_PLACEMENT_REQUIRED",
        "issue_type": safe_str(candidate.get("issue_type")) or family,
        "severity": safe_str(candidate.get("severity")) or "medium",
        "comparison_files": safe_str(candidate.get("comparison_files")),
        "comparison_pages": safe_str(candidate.get("comparison_pages")),
        "comparison_evidence": safe_str(candidate.get("comparison_evidence") or candidate.get("problem")),
        "markup_type": safe_str(candidate.get("markup_type")) or "highlight",
        "placement_confidence": safe_str(candidate.get("placement_confidence")) or "approximate",
        "normalized_family": family,
        "normalized_scenario": safe_str(candidate.get("normalized_scenario")) or family_to_scenario(family),
        "include": True,
        "reject": False,
        "reject_reason": "",
        "do_not_show_similar": False,
    }
    if out["target_text"].upper() == "MANUAL_PLACEMENT_REQUIRED":
        out["placement_confidence"] = "manual_needed"
        if out["markup_type"] == "highlight":
            out["markup_type"] = "page_note"
    return out


def candidates_to_accepted_df(candidates: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    nr = 1
    for c in candidates:
        if not c.get("include") or c.get("reject"):
            continue
        comment_text = safe_str(c.get("designer_note"))
        if not comment_text:
            comment_text = safe_str(c.get("problem"))
        rows.append(
            {
                "note_id": c.get("candidate_id") or f"AI-NOTE-{nr:03d}",
                "Nr": nr,
                "discipline": infer_discipline_from_path(safe_str(c.get("target_file"))),
                "target_file": safe_str(c.get("target_file")),
                "target_page": c.get("target_page"),
                "target_area": safe_str(c.get("target_area") or c.get("kur")),
                "target_text": safe_str(c.get("target_text")),
                "comment_text": comment_text,
                "issue_type": safe_str(c.get("issue_type")),
                "severity": safe_str(c.get("severity")) or "medium",
                "comparison_files": safe_str(c.get("comparison_files")),
                "comparison_pages": safe_str(c.get("comparison_pages")),
                "comparison_evidence": safe_str(c.get("comparison_evidence") or c.get("problem")),
                "markup_type": safe_str(c.get("markup_type")) or "highlight",
                "placement_confidence": safe_str(c.get("placement_confidence")) or "approximate",
                "status": "accepted_candidate",
            }
        )
        nr += 1
    return pd.DataFrame(rows, columns=REQUIRED_COLUMNS)


def candidates_to_rejected_df(candidates: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for c in candidates:
        if not c.get("reject"):
            continue
        rows.append(
            {
                "candidate_id": c.get("candidate_id"),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": c.get("title"),
                "normalized_family": c.get("normalized_family"),
                "normalized_scenario": c.get("normalized_scenario"),
                "issue_type": c.get("issue_type"),
                "target_file": c.get("target_file"),
                "target_page": c.get("target_page"),
                "target_area": c.get("target_area"),
                "target_text": c.get("target_text"),
                "comment_text": c.get("designer_note"),
                "reason": c.get("reject_reason"),
                "do_not_show_similar": c.get("do_not_show_similar"),
                "status": "rejected_by_user",
            }
        )
    return pd.DataFrame(rows)


def to_excel_bytes(sheets: Dict[str, pd.DataFrame]) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            safe_name = sheet_name[:31]
            df.to_excel(writer, sheet_name=safe_name, index=False)
            ws = writer.book[safe_name]
            ws.freeze_panes = "A2"
            for col_cells in ws.columns:
                max_len = 10
                col_letter = col_cells[0].column_letter
                for cell in col_cells[:200]:
                    max_len = max(max_len, min(len(str(cell.value or "")), 80))
                ws.column_dimensions[col_letter].width = min(max_len + 2, 60)
    return output.getvalue()


def build_result_zip(accepted_df: pd.DataFrame, rejected_df: pd.DataFrame, all_candidates_df: pd.DataFrame) -> bytes:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        accepted_xlsx = to_excel_bytes({"accepted_candidates": accepted_df})
        zf.writestr(f"accepted_candidates_{ts}.xlsx", accepted_xlsx)
        rejected_xlsx = to_excel_bytes({"rejected_patterns": rejected_df})
        zf.writestr(f"rejected_patterns_{ts}.xlsx", rejected_xlsx)
        zf.writestr(f"rejected_patterns_{ts}.json", rejected_df.to_json(orient="records", force_ascii=False, indent=2))
        review_xlsx = to_excel_bytes({"all_ai_candidates": all_candidates_df})
        zf.writestr(f"all_ai_candidates_review_{ts}.xlsx", review_xlsx)
    zip_buffer.seek(0)
    return zip_buffer.getvalue()


# -----------------------------
# UI
# -----------------------------

def render_candidate_card(c: Dict[str, Any], idx: int) -> Dict[str, Any]:
    with st.container(border=True):
        st.markdown(f"### {idx}. {safe_str(c.get('title')) or 'Kandidātpiezīme'}")
        st.markdown(f"**Kur:** {safe_str(c.get('kur')) or safe_str(c.get('target_area'))}")
        st.markdown(f"**Statuss:** {safe_str(c.get('status')) or safe_str(c.get('issue_type'))}")
        st.markdown("**Problēma:**")
        st.write(safe_str(c.get("problem")))
        st.markdown("**Kāpēc tas ir svarīgi:**")
        st.write(safe_str(c.get("why_important")))
        st.markdown("**Piezīme projektētājam:**")
        c["designer_note"] = st.text_area(
            "Rediģēt piezīmi projektētājam",
            value=safe_str(c.get("designer_note")),
            key=f"designer_note_{c['candidate_id']}",
            label_visibility="collapsed",
        )

        col1, col2 = st.columns(2)
        with col1:
            include = st.checkbox(
                "Iekļaut Excel / markup",
                value=bool(c.get("include", True)) and not bool(c.get("reject", False)),
                key=f"include_{c['candidate_id']}",
            )
        with col2:
            reject = st.checkbox(
                "Noraidīt",
                value=bool(c.get("reject", False)),
                key=f"reject_{c['candidate_id']}",
            )

        c["reject"] = reject
        c["include"] = include and not reject
        if reject:
            c["include"] = False
            c["reject_reason"] = st.text_input(
                "Noraidīšanas iemesls",
                value=safe_str(c.get("reject_reason")),
                placeholder="Piemēram: nebūtiska gramatiska kļūda; 2/2 ir kārtu skaits, nevis lapu skaits",
                key=f"reject_reason_{c['candidate_id']}",
            )
            c["do_not_show_similar"] = st.checkbox(
                "Turpmāk līdzīgas piezīmes nerādīt",
                value=bool(c.get("do_not_show_similar", False)),
                key=f"do_not_show_{c['candidate_id']}",
            )

        with st.expander("Tehniskie 16 kolonnu dati"):
            c["target_page"] = st.text_input("target_page", value=safe_str(c.get("target_page")), key=f"page_{c['candidate_id']}")
            c["target_area"] = st.text_input("target_area", value=safe_str(c.get("target_area")), key=f"area_{c['candidate_id']}")
            c["target_text"] = st.text_area("target_text", value=safe_str(c.get("target_text")), key=f"target_text_{c['candidate_id']}")
            c["issue_type"] = st.text_input("issue_type", value=safe_str(c.get("issue_type")), key=f"issue_{c['candidate_id']}")
            c["severity"] = st.selectbox("severity", ["low", "medium", "high"], index=["low", "medium", "high"].index(safe_str(c.get("severity")) if safe_str(c.get("severity")) in ["low", "medium", "high"] else "medium"), key=f"severity_{c['candidate_id']}")
            c["markup_type"] = st.selectbox("markup_type", ["highlight", "rectangle", "sticky_note", "page_note"], index=["highlight", "rectangle", "sticky_note", "page_note"].index(safe_str(c.get("markup_type")) if safe_str(c.get("markup_type")) in ["highlight", "rectangle", "sticky_note", "page_note"] else "highlight"), key=f"markup_{c['candidate_id']}")
            c["placement_confidence"] = st.selectbox("placement_confidence", ["exact", "approximate", "manual_needed"], index=["exact", "approximate", "manual_needed"].index(safe_str(c.get("placement_confidence")) if safe_str(c.get("placement_confidence")) in ["exact", "approximate", "manual_needed"] else "approximate"), key=f"placement_{c['candidate_id']}")
    return c


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    st.title(APP_TITLE)
    st.caption("AI ģenerē kandidātpiezīmes. Cilvēks akceptē vai noraida. Tikai akceptētās piezīmes iet uz markup Excel.")

    with st.sidebar:
        st.header("Iestatījumi")
        input_default = get_secret_value("GOOGLE_DRIVE_INPUT_FOLDER_ID", "input_folder_id")
        memory_default = get_secret_value("GOOGLE_DRIVE_MEMORY_FOLDER_ID", "memory_folder_id")
        input_folder_id = st.text_input("01_Input folder ID", value=input_default)
        memory_folder_id = st.text_input("03_Memory folder ID", value=memory_default)
        model = st.text_input("OpenAI modelis", value="gpt-4.1-mini")
        max_chars = st.slider("PDF konteksta garums vienam batcham", 8000, 60000, 25000, step=1000)
        max_candidates_per_family = st.slider("Max kandidāti vienā kļūdu ģimenē", 1, 8, 3)
        families_to_run = st.multiselect(
            "Iekšējās kļūdu ģimenes",
            options=FAMILY_ORDER,
            default=FAMILY_ORDER,
            help="Lietotājam parasti nav jāmaina. Atstāj visas ģimenes. Ja tests ir lēns, īslaicīgi vari samazināt.",
        )
        dry_run = st.checkbox("Testa režīms bez OpenAI API", value=False)

    if not input_folder_id or not memory_folder_id:
        st.warning("Norādi 01_Input un 03_Memory folder ID.")
        return

    st.subheader("1. Datu nolasīšana")
    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("Nolasīt PDF failus no 01_Input", type="primary"):
            with st.spinner("Nolasu PDF failus..."):
                st.session_state["pdf_items"] = list_drive_recursive(input_folder_id, only_mimes=(MIME_PDF,))
    with col_b:
        if st.button("Nolasīt audit_examples un feedback"):
            with st.spinner("Nolasu audit_examples..."):
                st.session_state["examples_df"] = load_audit_examples(memory_folder_id)
                st.session_state["rejected_df"] = load_rejected_patterns(memory_folder_id)

    pdf_items = st.session_state.get("pdf_items", [])
    examples_df = st.session_state.get("examples_df", pd.DataFrame())
    rejected_df = st.session_state.get("rejected_df", pd.DataFrame())

    if pdf_items:
        st.success(f"Atrasti PDF faili: {len(pdf_items)}")
    if isinstance(examples_df, pd.DataFrame) and not examples_df.empty:
        st.success(f"Nolasīti audit_examples: {len(examples_df)} rindas")
        fam_counts = examples_df["normalized_family"].value_counts().reset_index()
        fam_counts.columns = ["family", "count"]
        st.dataframe(fam_counts, use_container_width=True, hide_index=True)
    if isinstance(rejected_df, pd.DataFrame) and not rejected_df.empty:
        st.info(f"Nolasīti noraidītie patterni: {len(rejected_df)}")

    st.subheader("2. Izvēlies auditējamo PDF")
    if not pdf_items:
        st.info("Vispirms nolasi PDF failus no 01_Input.")
        return

    pdf_options = {f"{x['path']}": x for x in pdf_items}
    selected_paths = st.multiselect("PDF faili", options=list(pdf_options.keys()), default=list(pdf_options.keys())[:1])
    if not selected_paths:
        return

    st.subheader("3. Ģenerēt kandidātpiezīmes")
    st.write("Rīks iekšēji skata visas kļūdu ģimenes pa batchiem. Lietotājam nav jāizvēlas kļūdu tips.")

    if st.button("Analizēt izvēlētos PDF", type="primary"):
        client = None if dry_run else get_openai_client()
        if client is None and not dry_run:
            st.error("Nav atrasts OPENAI_API_KEY. Pievieno Streamlit secrets vai ieslēdz testa režīmu bez OpenAI API.")
            return
        if examples_df.empty:
            st.error("Nav nolasīti audit_examples. Vispirms spied 'Nolasīt audit_examples un feedback'.")
            return

        all_candidates: List[Dict[str, Any]] = []
        progress = st.progress(0, text="Sāku analīzi...")
        total_steps = max(len(selected_paths) * len(families_to_run), 1)
        step = 0

        for selected_path in selected_paths:
            item = pdf_options[selected_path]
            pdf_bytes = download_drive_file(item["id"])
            blocks_df, full_text = extract_pdf_blocks(pdf_bytes, item["name"])
            doc_role = infer_document_role(item["name"], full_text[:3000])
            pdf_context = build_pdf_context(blocks_df, full_text, max_chars=max_chars)

            for family in families_to_run:
                step += 1
                progress.progress(step / total_steps, text=f"{item['name']} — {family} ({step}/{total_steps})")
                examples = retrieve_examples(examples_df, family, doc_role, limit=8)
                examples_prompt = examples_to_prompt(examples)
                rejected_prompt = rejected_to_prompt(rejected_df, family)

                if dry_run:
                    # Testa režīmā neģenerējam viltus kļūdas. Parādām tikai tukšu rezultātu.
                    family_candidates: List[Dict[str, Any]] = []
                else:
                    try:
                        family_candidates = call_ai_for_family(
                            client=client,
                            model=model,
                            family=family,
                            filename=item["name"],
                            document_role=doc_role,
                            pdf_context=pdf_context,
                            examples_prompt=examples_prompt,
                            rejected_prompt=rejected_prompt,
                            max_candidates=max_candidates_per_family,
                        )
                    except Exception as exc:
                        st.warning(f"AI batch kļūda: {item['name']} / {family}: {exc}")
                        family_candidates = []

                for raw in family_candidates:
                    normalized = normalize_candidate(raw, item["name"], len(all_candidates) + 1)
                    normalized["source_path"] = selected_path
                    all_candidates.append(normalized)
                time.sleep(0.1)

        progress.empty()
        st.session_state["ai_candidates"] = all_candidates
        st.success(f"Analīze pabeigta. Kandidātpiezīmes: {len(all_candidates)}")

    st.subheader("4. Kandidātpiezīmju pārbaude")
    candidates = st.session_state.get("ai_candidates", [])
    if not candidates:
        st.info("Pagaidām nav kandidātpiezīmju. Palaid analīzi.")
        return

    updated_candidates: List[Dict[str, Any]] = []
    for i, candidate in enumerate(candidates, start=1):
        updated_candidates.append(render_candidate_card(candidate, i))
    st.session_state["ai_candidates"] = updated_candidates

    st.subheader("5. Eksports")
    accepted_df = candidates_to_accepted_df(updated_candidates)
    rejected_df_out = candidates_to_rejected_df(updated_candidates)
    all_candidates_df = pd.DataFrame(updated_candidates)

    c1, c2, c3 = st.columns(3)
    c1.metric("Akceptētas", len(accepted_df))
    c2.metric("Noraidītas", len(rejected_df_out))
    c3.metric("Kopā kandidāti", len(all_candidates_df))

    zip_bytes = build_result_zip(accepted_df, rejected_df_out, all_candidates_df)
    st.download_button(
        "Lejupielādēt rezultātu ZIP",
        data=zip_bytes,
        file_name=f"bp_audit_copilot_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
        type="primary",
    )

    with st.expander("Akceptēto piezīmju Excel priekšskatījums"):
        st.dataframe(accepted_df, use_container_width=True, hide_index=True)
    with st.expander("Noraidīto patternu priekšskatījums"):
        st.dataframe(rejected_df_out, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
