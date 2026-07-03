import ast
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


st.set_page_config(page_title="BP Memory Builder", layout="wide")

st.title("BP prasību atmiņas izveide")

st.write(
    "Šī aplikācija apvieno pārskatītos prasību Excel failus, atlasa accepted/edited rindas "
    "un saglabā projekta prasību atmiņu Google Drive 03_Memory mapē gan Excel, gan JSON formātā."
)


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


def upload_bytes_to_drive(
    service,
    folder_id: str,
    file_name: str,
    data: bytes,
    mime_type: str,
) -> Dict[str, Any]:
    file_metadata = {
        "name": file_name,
        "parents": [folder_id],
    }

    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype=mime_type,
        resumable=False,
    )

    created = (
        service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    return created


# =========================================================
# Datu tīrīšana
# =========================================================

def clean_excel_illegal_chars(value):
    if isinstance(value, str):
        return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", value)
    return value


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()

    for col in cleaned.columns:
        cleaned[col] = cleaned[col].map(clean_excel_illegal_chars)

    return cleaned


def normalize_text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def parse_list_value(value: Any) -> List[str]:
    if value is None:
        return []

    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    if pd.isna(value):
        return []

    text = str(value).strip()

    if not text:
        return []

    # Ja Excelā saglabājies kā Python saraksts: ['UK', 'UKT']
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:
            pass

    # Ja saglabājies kā "UK, UKT"
    parts = re.split(r"[,;]", text)
    return [part.strip() for part in parts if part.strip()]


def list_to_excel_text(value: Any) -> str:
    items = parse_list_value(value)
    return ", ".join(items)


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


def ensure_required_columns(df: pd.DataFrame) -> pd.DataFrame:
    required_columns = [
        "requirement_id",
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

    result = df.copy()

    for col in required_columns:
        if col not in result.columns:
            result[col] = ""

    return result


def filter_accepted_requirements(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()

    if "review_status" not in result.columns:
        result["review_status"] = "pending"

    result["review_status_normalized"] = (
        result["review_status"]
        .astype(str)
        .str.strip()
        .str.lower()
    )

    accepted_df = result[
        result["review_status_normalized"].isin(["accepted", "edited"])
    ].copy()

    accepted_df = accepted_df.drop(columns=["review_status_normalized"], errors="ignore")

    return accepted_df


def build_memory_dataframe(df: pd.DataFrame, project_code: str) -> pd.DataFrame:
    result = ensure_required_columns(df)
    result = filter_accepted_requirements(result)

    if result.empty:
        return result

    result = result.copy()

    # Sakārto pamata laukus
    result["requirement"] = result["requirement"].apply(normalize_text)
    result["source_text"] = result["source_text"].apply(normalize_text)
    result["verification_hint"] = result["verification_hint"].apply(normalize_text)
    result["condition"] = result["condition"].apply(normalize_text)
    result["source_file"] = result["source_file"].apply(normalize_text)
    result["drive_path"] = result["drive_path"].apply(normalize_text)
    result["engineering_system"] = result["engineering_system"].apply(normalize_text)
    result["review_status"] = result["review_status"].apply(lambda x: normalize_text(x).lower())

    result["priority"] = result["priority"].apply(lambda x: safe_int(x, default=10))
    result["confidence"] = result["confidence"].apply(lambda x: safe_float(x, default=0.0))

    result["discipline_list"] = result["discipline"].apply(parse_list_value)
    result["applies_to_sections_list"] = result["applies_to_sections"].apply(parse_list_value)

    result["discipline"] = result["discipline_list"].apply(lambda items: ", ".join(items))
    result["applies_to_sections"] = result["applies_to_sections_list"].apply(lambda items: ", ".join(items))

    # Tukšās requirement rindas izmetam
    result = result[result["requirement"].astype(str).str.strip() != ""].copy()

    # Memory ID
    result = result.reset_index(drop=True)
    result["memory_id"] = [
        f"{project_code}-MEP-REQ-{i + 1:04d}"
        for i in range(len(result))
    ]

    result["memory_type"] = "mep_requirement"
    result["project_code"] = project_code
    result["created_at_utc"] = datetime.now(timezone.utc).isoformat()

    preferred_cols = [
        "memory_id",
        "project_code",
        "memory_type",
        "requirement_id",
        "engineering_system",
        "discipline",
        "discipline_list",
        "source_file",
        "page",
        "block_id",
        "requirement",
        "condition",
        "applies_to_sections",
        "applies_to_sections_list",
        "priority",
        "verification_hint",
        "source_text",
        "confidence",
        "review_status",
        "drive_path",
        "batch_index",
        "batch_start_page",
        "batch_end_page",
        "drive_file_id",
        "created_at_utc",
    ]

    existing_cols = [col for col in preferred_cols if col in result.columns]
    other_cols = [col for col in result.columns if col not in existing_cols]

    return result[existing_cols + other_cols]


def dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    excel_df = df.copy()

    for col in excel_df.columns:
        if col.endswith("_list"):
            excel_df[col] = excel_df[col].apply(
                lambda value: ", ".join(value) if isinstance(value, list) else value
            )

    excel_df = clean_dataframe(excel_df)

    return excel_df


def dataframe_for_json(df: pd.DataFrame) -> List[Dict[str, Any]]:
    records = []

    for _, row in df.iterrows():
        item = {}

        for col, value in row.items():
            if isinstance(value, float) and pd.isna(value):
                item[col] = None
            elif pd.isna(value) if not isinstance(value, list) else False:
                item[col] = None
            elif isinstance(value, pd.Timestamp):
                item[col] = value.isoformat()
            else:
                item[col] = value

        # Nodrošinām, ka list kolonnas JSONā tiešām ir saraksti
        if "discipline_list" in item:
            item["discipline_list"] = parse_list_value(item["discipline_list"])

        if "applies_to_sections_list" in item:
            item["applies_to_sections_list"] = parse_list_value(item["applies_to_sections_list"])

        records.append(item)

    return records


def make_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()

    excel_df = dataframe_for_excel(df)

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        excel_df.to_excel(writer, sheet_name="accepted_mep_requirements", index=False)

        summary_system = (
            excel_df.groupby("engineering_system")
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        summary_system.to_excel(writer, sheet_name="summary_by_system", index=False)

        summary_priority = (
            excel_df.groupby("priority")
            .size()
            .reset_index(name="count")
            .sort_values("priority", ascending=False)
        )
        summary_priority.to_excel(writer, sheet_name="summary_by_priority", index=False)

    output.seek(0)
    return output.getvalue()


def make_json_bytes(df: pd.DataFrame) -> bytes:
    records = dataframe_for_json(df)

    payload = {
        "memory_schema": "bp_audit_mep_requirements_v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "requirements": records,
    }

    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


# =========================================================
# Streamlit UI
# =========================================================

memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")

st.markdown("## 1. Konfigurācija")

st.write("Memory folder ID:", memory_folder_id)

project_code = st.text_input(
    "Projekta kods",
    value="C2-3",
)

uploaded_files = st.file_uploader(
    "Augšupielādē pārskatītos prasību Excel failus",
    type=["xlsx"],
    accept_multiple_files=True,
)

st.info(
    "Augšupielādē gan galvenā Design Brief prasību Excel, gan pielikumu prasību Excel. "
    "Rīks atlasīs tikai rindas ar review_status = accepted vai edited."
)

if "memory_df" not in st.session_state:
    st.session_state.memory_df = pd.DataFrame()

if uploaded_files:
    st.markdown("## 2. Augšupielādētie faili")

    all_dfs = []

    for file in uploaded_files:
        try:
            df = pd.read_excel(file)
            df["uploaded_source_excel"] = file.name
            all_dfs.append(df)

            st.write(f"✅ {file.name}: {len(df)} rindas")

        except Exception as e:
            st.error(f"Neizdevās nolasīt failu: {file.name}")
            st.exception(e)

    if all_dfs:
        combined_df = pd.concat(all_dfs, ignore_index=True)

        st.markdown("### Apvienotie kandidāti")
        st.write(f"Kopā kandidātu rindas: {len(combined_df)}")
        st.dataframe(combined_df.head(50), use_container_width=True)

        if st.button("1) Izveidot accepted MEP prasību atmiņu"):
            memory_df = build_memory_dataframe(
                df=combined_df,
                project_code=project_code,
            )

            if memory_df.empty:
                st.warning("Nav atrasta neviena accepted/edited prasība.")
            else:
                st.session_state.memory_df = memory_df
                st.success(f"Izveidota atmiņas tabula ar {len(memory_df)} prasībām.")

memory_df = st.session_state.memory_df

if not memory_df.empty:
    st.markdown("## 3. Accepted MEP prasību atmiņa")

    st.markdown("### Kopsavilkums pēc sistēmas")
    summary_system = (
        memory_df.groupby("engineering_system")
        .size()
        .reset_index(name="count")
        .sort_values("count", ascending=False)
    )
    st.dataframe(summary_system, use_container_width=True)

    st.markdown("### Kopsavilkums pēc prioritātes")
    summary_priority = (
        memory_df.groupby("priority")
        .size()
        .reset_index(name="count")
        .sort_values("priority", ascending=False)
    )
    st.dataframe(summary_priority, use_container_width=True)

    st.markdown("### Atmiņas tabula")
    st.dataframe(memory_df, use_container_width=True)

    excel_bytes = make_excel_bytes(memory_df)
    json_bytes = make_json_bytes(memory_df)

    excel_name = f"{project_code.lower().replace('-', '_')}_mep_requirements_accepted.xlsx"
    json_name = f"{project_code.lower().replace('-', '_')}_mep_requirements_accepted.json"

    col1, col2 = st.columns(2)

    with col1:
        st.download_button(
            label="Lejupielādēt Memory Excel",
            data=excel_bytes,
            file_name=excel_name,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with col2:
        st.download_button(
            label="Lejupielādēt Memory JSON",
            data=json_bytes,
            file_name=json_name,
            mime="application/json",
        )

    st.markdown("## 4. Saglabāt Google Drive 03_Memory mapē")

    if st.button("2) Augšupielādēt Excel un JSON uz 03_Memory"):
        try:
            if not memory_folder_id:
                st.error("Secrets nav atrasts GOOGLE_DRIVE_MEMORY_FOLDER_ID.")
                st.stop()

            drive_service = get_drive_service()

            uploaded_excel = upload_bytes_to_drive(
                service=drive_service,
                folder_id=memory_folder_id,
                file_name=excel_name,
                data=excel_bytes,
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            uploaded_json = upload_bytes_to_drive(
                service=drive_service,
                folder_id=memory_folder_id,
                file_name=json_name,
                data=json_bytes,
                mime_type="application/json",
            )

            st.success("Faili augšupielādēti Google Drive 03_Memory mapē.")

            st.write("Excel:", uploaded_excel)
            st.write("JSON:", uploaded_json)

        except Exception as e:
            st.error("Neizdevās augšupielādēt failus Google Drive 03_Memory mapē.")
            st.exception(e)
