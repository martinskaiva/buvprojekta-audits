import io
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


st.set_page_config(page_title="BP projekta atmiņas lasītājs", layout="wide")

st.title("BP projekta atmiņas lasītājs")

st.write(
    "Šī aplikācija nolasa visus JSON failus no `03_Memory` mapes, atpazīst Design Brief "
    "prasību atmiņu un disciplīnu faktu atmiņas, un parāda, kas šobrīd ir projekta atmiņā."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


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


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


def load_json_from_drive(service, file_id: str) -> Any:
    raw_bytes = download_drive_file_bytes(service, file_id)
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    return json.loads(raw_text)


# =========================================================
# JSON memory atpazīšana
# =========================================================

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
            import ast
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass

    parts = re.split(r"[,;]", text)
    return [part.strip() for part in parts if part.strip()]


def detect_memory_kind(file_name: str, payload: Any) -> str:
    name = str(file_name).lower()

    if isinstance(payload, dict):
        schema = str(payload.get("memory_schema", "")).lower()

        if "requirement" in schema or "requirements" in schema:
            return "design_brief_requirements"

        if "fact" in schema or "facts" in schema:
            return "discipline_facts"

        if isinstance(payload.get("requirements"), list):
            return "design_brief_requirements"

        if isinstance(payload.get("facts"), list):
            return "discipline_facts"

        if isinstance(payload.get("issues"), list):
            return "issues_memory"

    if isinstance(payload, list):
        if not payload:
            return "empty_list"

        first = payload[0]
        if isinstance(first, dict):
            keys = set(first.keys())
            if "requirement" in keys or "memory_id" in keys and "mep" in name:
                return "design_brief_requirements"
            if "fact_type" in keys or "fact" in keys or "fact_id" in keys:
                return "discipline_facts"
            if "issue_type" in keys or "comment" in keys:
                return "issues_memory"

    if "requirements" in name or "mep" in name:
        return "design_brief_requirements"

    if "facts" in name or "fact" in name:
        return "discipline_facts"

    if "issues" in name:
        return "issues_memory"

    return "unknown_json"


def extract_records(payload: Any, kind: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        return []

    if kind == "design_brief_requirements":
        records = payload.get("requirements", [])
        return records if isinstance(records, list) else []

    if kind == "discipline_facts":
        records = payload.get("facts", [])
        if isinstance(records, list):
            return records

        # Dažās testa versijās fakti var būt zem cita lauka nosaukuma.
        records = payload.get("records", [])
        if isinstance(records, list):
            return records

        records = payload.get("items", [])
        if isinstance(records, list):
            return records

        return []

    if kind == "issues_memory":
        records = payload.get("issues", [])
        return records if isinstance(records, list) else []

    # Fallback: mēģinām atrast pirmo saraksta lauku ar dict ierakstiem.
    for _, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value

    return []


def dataframe_from_records(records: List[Dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)

    if "discipline_list" not in df.columns and "discipline" in df.columns:
        df["discipline_list"] = df["discipline"].apply(parse_list_value)

    if "applies_to_sections_list" not in df.columns and "applies_to_sections" in df.columns:
        df["applies_to_sections_list"] = df["applies_to_sections"].apply(parse_list_value)

    if "priority" in df.columns:
        df["priority"] = pd.to_numeric(df["priority"], errors="coerce").fillna(0).astype(int)

    if "confidence" in df.columns:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce").fillna(0.0)

    return df


def infer_discipline_from_filename(file_name: str) -> str:
    name = str(file_name).lower()

    # c2_3_ukt_facts.json -> UKT
    match = re.search(r"c2[_-]3[_-]([a-z0-9-]+)[_-]facts", name)
    if match:
        return match.group(1).upper()

    if "ukt" in name:
        return "UKT"
    if re.search(r"[_-]uk[_-]", name):
        return "UK"
    if "avk" in name:
        return "AVK"
    if re.search(r"[_-]sm[_-]", name):
        return "SM"
    if re.search(r"[_-]el[_-]", name):
        return "EL"
    if "ess-vas" in name:
        return "ESS-VAS"
    if "ess" in name:
        return "ESS"
    if "uats" in name:
        return "UATS"
    if "uas" in name:
        return "UAS"
    if re.search(r"[_-]gp[_-]", name):
        return "GP"

    return ""


def explode_list_summary(df: pd.DataFrame, list_col: str, label_col: str) -> pd.DataFrame:
    if df.empty or list_col not in df.columns:
        return pd.DataFrame(columns=[label_col, "count"])

    rows = []

    for _, row in df.iterrows():
        values = parse_list_value(row.get(list_col))
        if not values:
            values = ["(empty)"]

        for value in values:
            rows.append({label_col: value})

    if not rows:
        return pd.DataFrame(columns=[label_col, "count"])

    return (
        pd.DataFrame(rows)
        .groupby(label_col)
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )


def build_memory_catalog(json_files: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []

    for item in json_files:
        payload = item.get("payload")
        kind = item.get("kind")
        records = item.get("records", [])

        schema = ""
        payload_count = None

        if isinstance(payload, dict):
            schema = str(payload.get("memory_schema", ""))
            payload_count = payload.get("count")

        rows.append(
            {
                "name": item.get("name"),
                "kind": kind,
                "detected_discipline": item.get("detected_discipline", ""),
                "records_count": len(records),
                "payload_count": payload_count,
                "memory_schema": schema,
                "mimeType": item.get("mimeType"),
                "size": item.get("size", ""),
                "modifiedTime": item.get("modifiedTime", ""),
                "id": item.get("id"),
            }
        )

    return pd.DataFrame(rows)


# =========================================================
# Streamlit UI
# =========================================================

memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")

st.markdown("## 1. Konfigurācija")
st.write("Memory folder ID:", memory_folder_id)

if "project_memory_json_files" not in st.session_state:
    st.session_state.project_memory_json_files = []

if "requirements_df" not in st.session_state:
    st.session_state.requirements_df = pd.DataFrame()

if "facts_df" not in st.session_state:
    st.session_state.facts_df = pd.DataFrame()

if "issues_df" not in st.session_state:
    st.session_state.issues_df = pd.DataFrame()

if st.button("1) Nolasīt visu 03_Memory projektu atmiņu"):
    try:
        if not memory_folder_id:
            st.error("Secrets nav atrasts GOOGLE_DRIVE_MEMORY_FOLDER_ID.")
            st.stop()

        drive_service = get_drive_service()
        memory_items = list_folder_items(drive_service, memory_folder_id)

        if not memory_items:
            st.warning("03_Memory mape ir tukša vai rīkam nav piekļuves.")
            st.stop()

        json_items = [
            item for item in memory_items
            if str(item.get("name", "")).lower().endswith(".json")
        ]

        if not json_items:
            st.error("03_Memory mapē nav atrasts neviens JSON fails.")
            st.stop()

        loaded_json_files = []
        all_requirements = []
        all_facts = []
        all_issues = []

        for item in json_items:
            file_name = item.get("name", "")

            try:
                payload = load_json_from_drive(drive_service, item.get("id"))
                kind = detect_memory_kind(file_name, payload)
                records = extract_records(payload, kind)
                detected_discipline = infer_discipline_from_filename(file_name)

                enriched_records = []
                for record in records:
                    record = dict(record)
                    record["memory_source_file"] = file_name
                    record["memory_file_id"] = item.get("id")
                    if detected_discipline and not record.get("memory_discipline"):
                        record["memory_discipline"] = detected_discipline
                    enriched_records.append(record)

                if kind == "design_brief_requirements":
                    all_requirements.extend(enriched_records)
                elif kind == "discipline_facts":
                    all_facts.extend(enriched_records)
                elif kind == "issues_memory":
                    all_issues.extend(enriched_records)

                loaded_json_files.append(
                    {
                        **item,
                        "payload": payload,
                        "kind": kind,
                        "records": enriched_records,
                        "detected_discipline": detected_discipline,
                    }
                )

            except Exception as file_error:
                loaded_json_files.append(
                    {
                        **item,
                        "payload": {},
                        "kind": "load_error",
                        "records": [],
                        "detected_discipline": infer_discipline_from_filename(file_name),
                        "error": str(file_error),
                    }
                )

        st.session_state.project_memory_json_files = loaded_json_files
        st.session_state.requirements_df = dataframe_from_records(all_requirements)
        st.session_state.facts_df = dataframe_from_records(all_facts)
        st.session_state.issues_df = dataframe_from_records(all_issues)

        st.success(
            f"Nolasīti {len(loaded_json_files)} JSON faili no 03_Memory. "
            f"Prasības: {len(all_requirements)}, fakti: {len(all_facts)}, issues: {len(all_issues)}."
        )

    except Exception as e:
        st.error("Neizdevās nolasīt projekta atmiņu.")
        st.exception(e)

json_files = st.session_state.project_memory_json_files
requirements_df = st.session_state.requirements_df
facts_df = st.session_state.facts_df
issues_df = st.session_state.issues_df

if json_files:
    st.markdown("## 2. 03_Memory JSON katalogs")

    catalog_df = build_memory_catalog(json_files)
    st.dataframe(catalog_df, use_container_width=True)

    load_errors = [item for item in json_files if item.get("kind") == "load_error"]
    if load_errors:
        st.warning("Dažus JSON failus neizdevās nolasīt.")
        for item in load_errors:
            st.write(item.get("name"), item.get("error"))

    st.markdown("## 3. Projekta atmiņas kopsavilkums")

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("JSON faili", len(json_files))

    with col2:
        st.metric("Design Brief prasības", len(requirements_df))

    with col3:
        st.metric("Disciplīnu fakti", len(facts_df))

    with col4:
        st.metric("Issue memory", len(issues_df))

    if not requirements_df.empty:
        st.markdown("## 4. Design Brief / MEP prasību atmiņa")

        col1, col2, col3 = st.columns(3)
        with col1:
            if "engineering_system" in requirements_df.columns:
                st.metric("Sistēmas", requirements_df["engineering_system"].nunique())
        with col2:
            if "source_file" in requirements_df.columns:
                st.metric("Avota faili", requirements_df["source_file"].nunique())
        with col3:
            if "priority" in requirements_df.columns:
                st.metric("Vidējā prioritāte", round(requirements_df["priority"].mean(), 2))

        st.markdown("### Prasības pēc engineering_system")
        if "engineering_system" in requirements_df.columns:
            summary_req_system = (
                requirements_df.groupby("engineering_system")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            st.dataframe(summary_req_system, use_container_width=True)

        st.markdown("### Prasības pēc discipline")
        summary_req_discipline = explode_list_summary(
            requirements_df,
            list_col="discipline_list",
            label_col="discipline",
        )
        st.dataframe(summary_req_discipline, use_container_width=True)

        st.markdown("### Prasību tabula")
        preferred_requirement_cols = [
            "memory_id",
            "project_code",
            "engineering_system",
            "discipline",
            "source_file",
            "page",
            "block_id",
            "requirement",
            "condition",
            "applies_to_sections",
            "priority",
            "verification_hint",
            "source_text",
            "confidence",
            "review_status",
            "memory_source_file",
        ]
        existing_cols = [col for col in preferred_requirement_cols if col in requirements_df.columns]
        other_cols = [col for col in requirements_df.columns if col not in existing_cols]
        st.dataframe(requirements_df[existing_cols + other_cols], use_container_width=True)

    if not facts_df.empty:
        st.markdown("## 5. Disciplīnu faktu atmiņa")

        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("Faktu skaits", len(facts_df))
        with col2:
            if "memory_discipline" in facts_df.columns:
                st.metric("Disciplīnas", facts_df["memory_discipline"].nunique())
        with col3:
            if "fact_type" in facts_df.columns:
                st.metric("Faktu tipi", facts_df["fact_type"].nunique())
        with col4:
            if "source_file" in facts_df.columns:
                st.metric("Avota faili", facts_df["source_file"].nunique())

        st.markdown("### Fakti pēc disciplīnas")
        if "memory_discipline" in facts_df.columns:
            summary_fact_discipline = (
                facts_df.groupby("memory_discipline")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            st.dataframe(summary_fact_discipline, use_container_width=True)

        st.markdown("### Fakti pēc fact_type")
        if "fact_type" in facts_df.columns:
            summary_fact_type = (
                facts_df.groupby("fact_type")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            st.dataframe(summary_fact_type, use_container_width=True)

        st.markdown("### Fakti pēc source_file")
        if "source_file" in facts_df.columns:
            summary_fact_file = (
                facts_df.groupby("source_file")
                .size()
                .reset_index(name="count")
                .sort_values("count", ascending=False)
            )
            st.dataframe(summary_fact_file, use_container_width=True)

        st.markdown("### Faktu tabula")
        preferred_fact_cols = [
            "memory_fact_id",
            "fact_id",
            "project_code",
            "discipline",
            "memory_discipline",
            "document_type",
            "fact_type",
            "system",
            "element",
            "parameter_name",
            "parameter_value",
            "unit",
            "quantity",
            "material",
            "location",
            "source_file",
            "page",
            "block_id",
            "source_text",
            "confidence",
            "memory_source_file",
        ]
        existing_cols = [col for col in preferred_fact_cols if col in facts_df.columns]
        other_cols = [col for col in facts_df.columns if col not in existing_cols]
        st.dataframe(facts_df[existing_cols + other_cols], use_container_width=True)

    if not issues_df.empty:
        st.markdown("## 6. Iepriekš saglabātās piezīmes / issues")
        st.dataframe(issues_df, use_container_width=True)

    st.markdown("## 7. Eksports pārbaudei")

    export_tabs = st.tabs(["Requirements CSV", "Facts CSV", "Catalog CSV"])

    with export_tabs[0]:
        if not requirements_df.empty:
            st.download_button(
                label="Lejupielādēt requirements CSV",
                data=requirements_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="project_memory_requirements.csv",
                mime="text/csv",
            )
        else:
            st.info("Nav requirements datu eksportam.")

    with export_tabs[1]:
        if not facts_df.empty:
            st.download_button(
                label="Lejupielādēt facts CSV",
                data=facts_df.to_csv(index=False).encode("utf-8-sig"),
                file_name="project_memory_facts.csv",
                mime="text/csv",
            )
        else:
            st.info("Nav facts datu eksportam.")

    with export_tabs[2]:
        st.download_button(
            label="Lejupielādēt memory catalog CSV",
            data=catalog_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="project_memory_catalog.csv",
            mime="text/csv",
        )
