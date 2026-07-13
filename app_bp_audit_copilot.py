import io
import os
import re
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
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except Exception:
    service_account = None
    build = None
    MediaIoBaseDownload = None


APP_VERSION = "v0.3"
APP_TITLE = f"BP AI Audit Copilot {APP_VERSION}"

REQUIRED_EXPORT_COLUMNS = [
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

INDEX_FOLDER_NAME = "audit_examples_index"
FEEDBACK_FOLDER_NAME = "audit_feedback"

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
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
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


def drive_find_child_folder(service, parent_id: str, folder_name: str) -> Optional[Dict[str, Any]]:
    children = drive_list_children(service, parent_id, "application/vnd.google-apps.folder")
    for item in children:
        if item.get("name") == folder_name:
            return item
    return None


def drive_list_recursive(service, folder_id: str, extensions: Tuple[str, ...], prefix: str = "", max_files: int = 5000) -> List[Dict[str, Any]]:
    out = []
    stack = [(folder_id, prefix)]
    while stack and len(out) < max_files:
        current_id, current_prefix = stack.pop()
        for item in drive_list_children(service, current_id):
            name = item.get("name", "")
            mime_type = item.get("mimeType", "")
            rel_path = f"{current_prefix}/{name}" if current_prefix else name
            if mime_type == "application/vnd.google-apps.folder":
                stack.append((item["id"], rel_path))
            else:
                if name.lower().endswith(extensions):
                    item2 = dict(item)
                    item2["rel_path"] = rel_path
                    out.append(item2)
    return sorted(out, key=lambda x: x.get("rel_path", ""))


def drive_download_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    return fh.getvalue()


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
    for col in ["normalized_family", "normalized_scenario", "scenario_label", "document_role", "source_path", "source_file"]:
        if col not in df.columns:
            df[col] = ""
    for col in df.columns:
        df[col] = df[col].map(clean_text)
    df = df[df["comment_text"].astype(str).str.strip().ne("") | df["target_text"].astype(str).str.strip().ne("")]
    return df.reset_index(drop=True)


def load_audit_examples_index(service, memory_folder_id: str) -> Tuple[pd.DataFrame, Optional[Dict[str, Any]], List[str]]:
    messages = []
    index_file = find_latest_index_file(service, memory_folder_id)
    if not index_file:
        return pd.DataFrame(), None, [f"Nav atrasts .xlsx fails mapē 03_Memory/{INDEX_FOLDER_NAME}."]
    data = drive_download_bytes(service, index_file["id"])
    try:
        df = read_excel_sheet_from_bytes(data, ["1_examples_index", "examples_index"])
        df = normalize_index_df(df)
        return df, index_file, messages
    except Exception as e:
        return pd.DataFrame(), index_file, [f"Neizdevās nolasīt indeksu {index_file.get('name')}: {e}"]


def load_feedback(service, memory_folder_id: str) -> pd.DataFrame:
    feedback_folder = drive_find_child_folder(service, memory_folder_id, FEEDBACK_FOLDER_NAME)
    if not feedback_folder:
        return pd.DataFrame()
    files = drive_list_recursive(service, feedback_folder["id"], (".xlsx", ".xlsm"), prefix=FEEDBACK_FOLDER_NAME, max_files=200)
    files = [f for f in files if "rejected" in f.get("name", "").lower() and not f.get("name", "").startswith("~$")]
    if not files:
        return pd.DataFrame()
    latest = sorted(files, key=lambda x: x.get("modifiedTime", ""), reverse=True)[0]
    try:
        data = drive_download_bytes(service, latest["id"])
        df = pd.read_excel(io.BytesIO(data), dtype=object)
        df.columns = [clean_text(c) for c in df.columns]
        for col in df.columns:
            df[col] = df[col].map(clean_text)
        return df
    except Exception:
        return pd.DataFrame()


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


def score_example(row: pd.Series, pdf_name: str, pdf_text_sample: str, family: str, doc_role: str, discipline: str) -> int:
    score = 0
    if clean_text(row.get("normalized_family")) == family:
        score += 100
    if doc_role and clean_text(row.get("document_role")) == doc_role:
        score += 15
    if discipline and clean_text(row.get("discipline")) == discipline:
        score += 15
    tf = clean_text(row.get("target_file")).lower()
    if discipline and discipline.lower() in tf:
        score += 5
    txt = clean_text(row.get("target_text"))
    if txt and len(txt) > 3 and txt.lower() in pdf_text_sample.lower():
        score += 20
    return score


def select_examples(index_df: pd.DataFrame, family: str, pdf_name: str, pdf_text: str, max_examples: int) -> List[Dict[str, str]]:
    if index_df.empty:
        return []
    doc_role = infer_document_role(pdf_name)
    discipline = infer_discipline_from_filename(pdf_name)
    fam_df = index_df[index_df["normalized_family"].eq(family)].copy()
    if fam_df.empty:
        return []
    sample = pdf_text[:10000]
    fam_df["_score"] = fam_df.apply(lambda r: score_example(r, pdf_name, sample, family, doc_role, discipline), axis=1)
    fam_df = fam_df.sort_values("_score", ascending=False).head(max_examples)
    examples = []
    for _, r in fam_df.iterrows():
        examples.append({
            "note_id": clean_text(r.get("note_id")),
            "family": clean_text(r.get("normalized_family")),
            "scenario": clean_text(r.get("normalized_scenario")),
            "target_area": clean_text(r.get("target_area")),
            "target_text": clean_text(r.get("target_text")),
            "comment_text": clean_text(r.get("comment_text")),
            "issue_type": clean_text(r.get("issue_type")),
            "comparison_evidence": clean_text(r.get("comparison_evidence")),
        })
    return examples


def make_negative_rules(feedback_df: pd.DataFrame, max_rules: int = 20) -> List[str]:
    if feedback_df.empty:
        return []
    rules = []
    for _, r in feedback_df.tail(max_rules).iterrows():
        reason = clean_text(r.get("reject_reason") or r.get("reason") or r.get("noraidīšanas iemesls"))
        text = clean_text(r.get("target_text") or r.get("title") or r.get("comment_text"))
        do_not = clean_text(r.get("do_not_show_similar") or r.get("turpmāk līdzīgas piezīmes nerādīt"))
        if reason or text:
            rules.append(f"Nerādīt līdzīgas piezīmes: {text}. Iemesls: {reason}. Do-not-show: {do_not}")
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
        "Tu esi būvprojekta audita asistents. Ģenerē tikai pierādāmas kandidātpiezīmes. "
        "Neizdomā faktus. Ja nav pietiekama pierādījuma, atgriez tukšu candidates sarakstu. "
        "Atbildi tikai derīgā JSON formātā."
    )
    user = {
        "task": "Analizē vienu PDF dokumentu un atrodi kandidātpiezīmes konkrētajā kļūdu ģimenē.",
        "pdf_file": pdf_name,
        "family": family,
        "family_instruction": instr,
        "max_candidates": max_candidates,
        "similar_positive_examples": examples,
        "negative_rules_do_not_repeat": negative_rules,
        "pdf_text": pdf_text,
        "required_json_schema": {
            "candidates": [
                {
                    "title": "īss kandidātpiezīmes virsraksts",
                    "where": "lapa un zona/tabula/teksts",
                    "target_page": 1,
                    "target_area": "zona, tabula vai vieta dokumentā",
                    "target_text": "precīzs teksts, ko var mēģināt iezīmēt PDF; ja nav, MANUAL_PLACEMENT_REQUIRED",
                    "status": "kļūdas tips vai risks",
                    "problem": "kas tieši nav pareizi",
                    "why_important": "kāpēc tas ir būtiski",
                    "designer_note": "īsa gatava piezīme projektētājam PDF komentāram",
                    "issue_type": "normalizēts issue_type",
                    "severity": "low|medium|high",
                    "markup_type": "highlight|rectangle|sticky_note|page_note",
                    "placement_confidence": "exact|approximate|manual_needed",
                    "evidence": "īss pierādījums no dokumenta vai salīdzinājuma",
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
        for c in candidates:
            if isinstance(c, dict):
                c["family"] = family
        return [c for c in candidates if isinstance(c, dict)], ""
    except Exception as e:
        return [], str(e)


def candidate_to_export_row(c: Dict[str, Any], idx: int, pdf_name: str, discipline: str) -> Dict[str, Any]:
    target_text = clean_text(c.get("target_text")) or "MANUAL_PLACEMENT_REQUIRED"
    placement = clean_text(c.get("placement_confidence")) or "manual_needed"
    markup = clean_text(c.get("markup_type"))
    if not markup:
        markup = "highlight" if placement == "exact" and target_text != "MANUAL_PLACEMENT_REQUIRED" else "page_note"
    problem = clean_text(c.get("problem"))
    why = clean_text(c.get("why_important"))
    evidence = clean_text(c.get("evidence"))
    comparison_evidence = "\n".join([x for x in [problem, why, evidence] if x])
    page = clean_text(c.get("target_page"))
    if not page:
        page = "1"
    return {
        "note_id": clean_text(c.get("note_id")) or f"AI-{datetime.now().strftime('%Y%m%d%H%M%S')}-{idx:03d}",
        "Nr": idx,
        "discipline": discipline,
        "target_file": pdf_name,
        "target_page": page,
        "target_area": clean_text(c.get("target_area") or c.get("where")),
        "target_text": target_text,
        "comment_text": clean_text(c.get("designer_note") or c.get("comment_text")),
        "issue_type": clean_text(c.get("issue_type") or c.get("family")),
        "severity": clean_text(c.get("severity")) or "medium",
        "comparison_files": "",
        "comparison_pages": "",
        "comparison_evidence": comparison_evidence,
        "markup_type": markup,
        "placement_confidence": placement,
        "status": "accepted_candidate",
    }


def candidate_to_rejected_row(c: Dict[str, Any], idx: int, pdf_name: str, reason: str, do_not_show: bool) -> Dict[str, Any]:
    return {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": pdf_name,
        "family": clean_text(c.get("family")),
        "title": clean_text(c.get("title")),
        "target_page": clean_text(c.get("target_page")),
        "target_area": clean_text(c.get("target_area") or c.get("where")),
        "target_text": clean_text(c.get("target_text")),
        "comment_text": clean_text(c.get("designer_note") or c.get("comment_text")),
        "issue_type": clean_text(c.get("issue_type")),
        "reject_reason": reason,
        "do_not_show_similar": bool(do_not_show),
        "status": "rejected_by_user",
        "candidate_index": idx,
    }


def make_zip(accepted_df: pd.DataFrame, rejected_df: pd.DataFrame, review_df: pd.DataFrame, base_name: str) -> bytes:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        acc_b = io.BytesIO()
        with pd.ExcelWriter(acc_b, engine="openpyxl") as writer:
            accepted_df.to_excel(writer, sheet_name="accepted_candidates", index=False)
        zf.writestr(f"accepted_candidates_{base_name}_{ts}.xlsx", acc_b.getvalue())

        rej_b = io.BytesIO()
        with pd.ExcelWriter(rej_b, engine="openpyxl") as writer:
            rejected_df.to_excel(writer, sheet_name="rejected_patterns", index=False)
        zf.writestr(f"rejected_patterns_{base_name}_{ts}.xlsx", rej_b.getvalue())
        zf.writestr(f"rejected_patterns_{base_name}_{ts}.json", rejected_df.to_json(orient="records", force_ascii=False, indent=2))

        rev_b = io.BytesIO()
        with pd.ExcelWriter(rev_b, engine="openpyxl") as writer:
            review_df.to_excel(writer, sheet_name="all_ai_candidates_review", index=False)
        zf.writestr(f"all_ai_candidates_review_{base_name}_{ts}.xlsx", rev_b.getvalue())
    return bio.getvalue()


def init_state():
    defaults = {
        "pdf_files": [],
        "index_df": pd.DataFrame(),
        "index_file": None,
        "feedback_df": pd.DataFrame(),
        "selected_pdf_bytes": None,
        "selected_pdf_name": "",
        "pdf_text": "",
        "pdf_pages": [],
        "candidates": [],
        "ai_errors": [],
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    init_state()

    st.title(APP_TITLE)
    st.caption("AI ģenerē kandidātpiezīmes no viena indeksēta audit_examples Excel. Cilvēks akceptē vai noraida. Tikai akceptētās piezīmes iet uz markup Excel.")

    service = get_drive_service()
    if service is None:
        st.error("Nav atrasti Google service account dati Streamlit secrets. Vajadzīgs GOOGLE_SERVICE_ACCOUNT_JSON vai [google_service_account].")
        st.stop()

    input_folder_id = get_secret("GOOGLE_DRIVE_INPUT_FOLDER_ID", "DRIVE_INPUT_FOLDER_ID", default="") or ""
    memory_folder_id = get_secret("GOOGLE_DRIVE_MEMORY_FOLDER_ID", "DRIVE_MEMORY_FOLDER_ID", default="") or ""

    with st.sidebar:
        st.header("Iestatījumi")
        input_folder_id = st.text_input("01_Input folder ID", value=input_folder_id)
        memory_folder_id = st.text_input("03_Memory folder ID", value=memory_folder_id)
        model = st.text_input("OpenAI modelis", value=get_secret("OPENAI_MODEL", default="gpt-4.1-mini") or "gpt-4.1-mini")
        max_context_chars = st.slider("PDF konteksta garums", 5000, 60000, 25000, 5000)
        max_examples_per_family = st.slider("Piemēri vienai ģimenei", 1, 12, 5, 1)
        max_candidates_per_family = st.slider("Max kandidāti vienai ģimenei", 0, 5, 1, 1)
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

    st.header("1. Datu nolasīšana")
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Nolasīt PDF failus no 01_Input", type="primary"):
            if not input_folder_id.strip():
                st.error("Nav norādīts 01_Input folder ID.")
            else:
                with st.spinner("Nolasu PDF failus..."):
                    st.session_state.pdf_files = drive_list_recursive(service, input_folder_id.strip(), (".pdf",), prefix="", max_files=1000)
                st.success(f"Atrasti PDF faili: {len(st.session_state.pdf_files)}")

    with c2:
        if st.button("Nolasīt audit_examples_index un feedback"):
            if not memory_folder_id.strip():
                st.error("Nav norādīts 03_Memory folder ID.")
            else:
                with st.spinner("Nolasu audit_examples_index/audit_feedback..."):
                    df, index_file, messages = load_audit_examples_index(service, memory_folder_id.strip())
                    st.session_state.index_df = df
                    st.session_state.index_file = index_file
                    st.session_state.feedback_df = load_feedback(service, memory_folder_id.strip())
                if messages:
                    for msg in messages:
                        st.warning(msg)
                if not df.empty:
                    st.success(f"Nolasīts indekss: {index_file.get('name')} — {len(df)} piemēri")
                else:
                    st.error("Indekss nav nolasīts.")

    if st.session_state.index_file:
        idx = st.session_state.index_file
        st.info(f"Aktīvais audit_examples_index: {idx.get('name')} | Modified: {idx.get('modifiedTime', '')}")

    pdf_files = st.session_state.pdf_files
    if pdf_files:
        st.subheader("2. Izvēlies auditējamo PDF")
        label_map = {f"{f.get('rel_path', f.get('name'))}": f for f in pdf_files}
        selected_label = st.selectbox("PDF", options=list(label_map.keys()))
        selected_pdf = label_map[selected_label]
        if st.button("Lejupielādēt un nolasīt PDF tekstu"):
            with st.spinner("Lejupielādēju un nolasu PDF..."):
                pdf_bytes = drive_download_bytes(service, selected_pdf["id"])
                text, pages, err = extract_pdf_text(pdf_bytes, max_context_chars)
                if err:
                    st.error(err)
                else:
                    st.session_state.selected_pdf_bytes = pdf_bytes
                    st.session_state.selected_pdf_name = selected_pdf.get("name", "audit.pdf")
                    st.session_state.pdf_text = text
                    st.session_state.pdf_pages = pages
                    st.success(f"Nolasīts: {selected_pdf.get('name')} | lapas: {len(pages)} | teksts: {len(text)} zīmes")

    if st.session_state.pdf_text:
        with st.expander("PDF teksta priekšskatījums"):
            st.text_area("PDF teksts", value=st.session_state.pdf_text[:10000], height=300)

    index_df = st.session_state.index_df
    if not index_df.empty:
        st.subheader("Audit_examples indeksa kopsavilkums")
        c1, c2, c3 = st.columns(3)
        c1.metric("Piemēri indeksā", len(index_df))
        c2.metric("Kļūdu ģimenes", index_df["normalized_family"].nunique())
        c3.metric("Feedback rindas", len(st.session_state.feedback_df))
        with st.expander("Ģimeņu sadalījums"):
            fam_summary = index_df["normalized_family"].value_counts().reset_index()
            fam_summary.columns = ["normalized_family", "count"]
            st.dataframe(fam_summary, use_container_width=True)

    st.header("3. AI kandidātpiezīmju ģenerēšana")
    ready = bool(st.session_state.pdf_text) and not st.session_state.index_df.empty
    if not ready:
        st.caption("Vispirms jānolasa PDF un audit_examples_index.")
    else:
        if st.button("Analizēt PDF", type="primary"):
            client = get_openai_client()
            if client is None:
                st.stop()
            all_candidates = []
            errors = []
            progress = st.progress(0)
            status = st.empty()
            families_to_run = [f for f in selected_families if max_candidates_per_family > 0]
            negative_rules = make_negative_rules(st.session_state.feedback_df)
            for i, family in enumerate(families_to_run, start=1):
                status.write(f"Pārbaude {i}/{len(families_to_run)}: {family}")
                examples = select_examples(
                    st.session_state.index_df,
                    family,
                    st.session_state.selected_pdf_name,
                    st.session_state.pdf_text,
                    max_examples_per_family,
                )
                candidates, err = call_ai_for_family(
                    client=client,
                    model=model,
                    pdf_name=st.session_state.selected_pdf_name,
                    pdf_text=st.session_state.pdf_text,
                    family=family,
                    examples=examples,
                    negative_rules=negative_rules,
                    max_candidates=max_candidates_per_family,
                )
                if err:
                    errors.append({"family": family, "error": err})
                for c in candidates:
                    c["source_pdf"] = st.session_state.selected_pdf_name
                    c["include_default"] = True
                    c["reject_default"] = False
                    all_candidates.append(c)
                progress.progress(i / max(1, len(families_to_run)))
            st.session_state.candidates = all_candidates
            st.session_state.ai_errors = errors
            status.write("AI analīze pabeigta.")
            st.success(f"Ģenerētas kandidātpiezīmes: {len(all_candidates)}")

    if st.session_state.ai_errors:
        with st.expander("AI batch kļūdas"):
            st.dataframe(pd.DataFrame(st.session_state.ai_errors), use_container_width=True)

    candidates = st.session_state.candidates
    if candidates:
        st.header("4. Kandidātu pārskatīšana")
        st.caption("Noklusēti kandidāts ir iekļauts Excel/markup. Ja noraidi, ieraksti iemeslu; vari atzīmēt arī 'turpmāk līdzīgas nerādīt'.")
        accepted_rows = []
        rejected_rows = []
        review_rows = []
        discipline = infer_discipline_from_filename(st.session_state.selected_pdf_name)
        for idx, c in enumerate(candidates, start=1):
            title = clean_text(c.get("title")) or f"Kandidāts {idx}"
            family = clean_text(c.get("family"))
            with st.container(border=True):
                st.markdown(f"### {idx}. {title}")
                st.markdown(f"**Ģimene:** `{family}`")
                st.markdown(f"**Kur:** {clean_text(c.get('where') or c.get('target_area'))}")
                st.markdown(f"**Statuss:** {clean_text(c.get('status'))}")
                st.markdown("**Problēma:**")
                st.write(clean_text(c.get("problem")))
                st.markdown("**Kāpēc tas ir svarīgi:**")
                st.write(clean_text(c.get("why_important")))
                st.markdown("**Piezīme projektētājam:**")
                edited_note = st.text_area(
                    "Labot piezīmi projektētājam",
                    value=clean_text(c.get("designer_note") or c.get("comment_text")),
                    key=f"designer_note_{idx}",
                    height=90,
                )
                c["designer_note"] = edited_note
                col_a, col_b = st.columns(2)
                include = col_a.checkbox("Iekļaut Excel / markup", value=True, key=f"include_{idx}")
                reject = col_b.checkbox("Noraidīt", value=False, key=f"reject_{idx}")
                reject_reason = ""
                do_not_show = False
                if reject:
                    include = False
                    reject_reason = st.text_input("Noraidīšanas iemesls", key=f"reject_reason_{idx}")
                    do_not_show = st.checkbox("Turpmāk līdzīgas piezīmes nerādīt", key=f"do_not_show_{idx}")
                row_review = dict(c)
                row_review["ui_include"] = include
                row_review["ui_reject"] = reject
                row_review["reject_reason"] = reject_reason
                row_review["do_not_show_similar"] = do_not_show
                review_rows.append(row_review)
                if include and not reject:
                    accepted_rows.append(candidate_to_export_row(c, len(accepted_rows) + 1, st.session_state.selected_pdf_name, discipline))
                if reject:
                    rejected_rows.append(candidate_to_rejected_row(c, idx, st.session_state.selected_pdf_name, reject_reason, do_not_show))

        st.header("5. Eksports")
        accepted_df = pd.DataFrame(accepted_rows, columns=REQUIRED_EXPORT_COLUMNS)
        rejected_df = pd.DataFrame(rejected_rows)
        review_df = pd.DataFrame(review_rows)
        c1, c2, c3 = st.columns(3)
        c1.metric("Akceptētas", len(accepted_df))
        c2.metric("Noraidītas", len(rejected_df))
        c3.metric("Kopā kandidāti", len(review_df))
        base = re.sub(r"[^A-Za-z0-9_\-]+", "_", os.path.splitext(st.session_state.selected_pdf_name)[0])[:80]
        zip_bytes = make_zip(accepted_df, rejected_df, review_df, base)
        st.download_button(
            "Lejupielādēt ZIP ar accepted/rejected/review Excel",
            data=zip_bytes,
            file_name=f"bp_ai_audit_copilot_{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            mime="application/zip",
            type="primary",
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        st.error(f"Script execution error: {exc}")
        with st.expander("Traceback"):
            st.code(traceback.format_exc())
