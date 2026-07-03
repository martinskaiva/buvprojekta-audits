import io
import json
import re
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


st.set_page_config(page_title="BP Memory Reader", layout="wide")

st.title("BP prasību atmiņas nolasīšanas tests")

st.write(
    "Šī aplikācija pārbauda, vai rīks prot nolasīt `03_Memory` mapē saglabāto "
    "MEP prasību JSON failu un parādīt prasību bāzi."
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


def find_memory_json_file(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    preferred_names = [
        "c2_3_mep_requirements_accepted.json",
        "c2-3_mep_requirements_accepted.json",
    ]

    for preferred_name in preferred_names:
        for item in items:
            if item.get("name") == preferred_name:
                return item

    json_items = [
        item for item in items
        if str(item.get("name", "")).lower().endswith(".json")
    ]

    if len(json_items) == 1:
        return json_items[0]

    for item in json_items:
        name = str(item.get("name", "")).lower()
        if "mep" in name and "requirements" in name and "accepted" in name:
            return item

    return None


# =========================================================
# Memory JSON apstrāde
# =========================================================

def load_memory_payload(service, file_id: str) -> Dict[str, Any]:
    raw_bytes = download_drive_file_bytes(service, file_id)
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    return json.loads(raw_text)


def extract_requirements_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("requirements"), list):
        return payload["requirements"]

    if isinstance(payload, list):
        return payload

    raise ValueError("Memory JSON struktūrā nav atrasts `requirements` saraksts.")


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


def requirements_to_dataframe(requirements: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(requirements)

    if df.empty:
        return df

    if "discipline_list" not in df.columns:
        if "discipline" in df.columns:
            df["discipline_list"] = df["discipline"].apply(parse_list_value)
        else:
            df["discipline_list"] = [[] for _ in range(len(df))]

    if "applies_to_sections_list" not in df.columns:
        if "applies_to_sections" in df.columns:
            df["applies_to_sections_list"] = df["applies_to_sections"].apply(parse_list_value)
        else:
            df["applies_to_sections_list"] = [[] for _ in range(len(df))]

    if "priority" in df.columns:
        df["priority"] = pd.to_numeric(df["priority"], errors="coerce").fillna(0).astype(int)

    if "confidence" in df.columns:
        df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce").fillna(0.0)

    return df


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

    summary = (
        pd.DataFrame(rows)
        .groupby(label_col)
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )

    return summary


# =========================================================
# Streamlit UI
# =========================================================

memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")

st.markdown("## 1. Konfigurācija")

st.write("Memory folder ID:", memory_folder_id)

if "memory_df" not in st.session_state:
    st.session_state.memory_df = pd.DataFrame()

if "memory_payload" not in st.session_state:
    st.session_state.memory_payload = {}

if st.button("1) Nolasīt 03_Memory JSON"):
    try:
        if not memory_folder_id:
            st.error("Secrets nav atrasts GOOGLE_DRIVE_MEMORY_FOLDER_ID.")
            st.stop()

        drive_service = get_drive_service()

        memory_items = list_folder_items(drive_service, memory_folder_id)

        if not memory_items:
            st.warning("03_Memory mape ir tukša vai rīkam nav piekļuves.")
            st.stop()

        st.markdown("## 2. 03_Memory mapes saturs")
        st.dataframe(pd.DataFrame(memory_items), use_container_width=True)

        memory_json_file = find_memory_json_file(memory_items)

        if not memory_json_file:
            st.error("03_Memory mapē nav atrasts MEP prasību JSON fails.")
            st.stop()

        st.success(f"Atrasts memory JSON: {memory_json_file.get('name')}")

        payload = load_memory_payload(
            service=drive_service,
            file_id=memory_json_file.get("id"),
        )

        requirements = extract_requirements_from_payload(payload)
        memory_df = requirements_to_dataframe(requirements)

        st.session_state.memory_payload = payload
        st.session_state.memory_df = memory_df

        st.success(f"Nolasītas {len(memory_df)} prasības no memory JSON.")

    except Exception as e:
        st.error("Neizdevās nolasīt memory JSON.")
        st.exception(e)

memory_df = st.session_state.memory_df
payload = st.session_state.memory_payload

if not memory_df.empty:
    st.markdown("## 3. Memory metadati")

    if isinstance(payload, dict):
        meta = {
            "memory_schema": payload.get("memory_schema"),
            "created_at_utc": payload.get("created_at_utc"),
            "count": payload.get("count"),
        }
        st.json(meta)

    st.markdown("## 4. Kopsavilkums")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("Prasību skaits", len(memory_df))

    with col2:
        if "engineering_system" in memory_df.columns:
            st.metric("Sistēmu skaits", memory_df["engineering_system"].nunique())
        else:
            st.metric("Sistēmu skaits", 0)

    with col3:
        if "source_file" in memory_df.columns:
            st.metric("Avota failu skaits", memory_df["source_file"].nunique())
        else:
            st.metric("Avota failu skaits", 0)

    st.markdown("### Kopsavilkums pēc engineering_system")

    if "engineering_system" in memory_df.columns:
        summary_system = (
            memory_df.groupby("engineering_system")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        st.dataframe(summary_system, use_container_width=True)
    else:
        st.info("Nav kolonnas engineering_system.")

    st.markdown("### Kopsavilkums pēc discipline")

    summary_discipline = explode_list_summary(
        memory_df,
        list_col="discipline_list",
        label_col="discipline",
    )
    st.dataframe(summary_discipline, use_container_width=True)

    st.markdown("### Kopsavilkums pēc applies_to_sections")

    summary_sections = explode_list_summary(
        memory_df,
        list_col="applies_to_sections_list",
        label_col="section",
    )
    st.dataframe(summary_sections, use_container_width=True)

    st.markdown("### Kopsavilkums pēc priority")

    if "priority" in memory_df.columns:
        summary_priority = (
            memory_df.groupby("priority")
            .size()
            .reset_index(name="count")
            .sort_values("priority", ascending=False)
        )
        st.dataframe(summary_priority, use_container_width=True)
    else:
        st.info("Nav kolonnas priority.")

    st.markdown("## 5. Prasību tabula")

    preferred_cols = [
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
        "drive_path",
    ]

    existing_cols = [col for col in preferred_cols if col in memory_df.columns]
    other_cols = [col for col in memory_df.columns if col not in existing_cols]

    st.dataframe(
        memory_df[existing_cols + other_cols],
        use_container_width=True,
    )

    csv_bytes = memory_df.to_csv(index=False).encode("utf-8-sig")

    st.download_button(
        label="Lejupielādēt memory CSV pārbaudei",
        data=csv_bytes,
        file_name="memory_reader_export.csv",
        mime="text/csv",
    )
