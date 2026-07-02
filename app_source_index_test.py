import io
import json

import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


st.set_page_config(page_title="BP izejas datu tests", layout="wide")

st.title("BP izejas datu / 01_VD indeksa tests")

st.write(
    "Šī testa aplikācija pārbauda, vai rīks prot Google Drive struktūrā atrast "
    "`01_VD` sadaļu, nolasīt tās dokumentus un izmantot `04_Prompt` mapi."
)


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def get_drive_service():
    service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not service_account_json:
        raise ValueError("Secrets nav atrasts GOOGLE_SERVICE_ACCOUNT_JSON.")

    service_account_info = json.loads(service_account_json)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    service = build("drive", "v3", credentials=credentials)
    return service


def list_folder_items(service, folder_id):
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


def list_items_recursive(service, folder_id, parent_path=""):
    rows = []
    items = list_folder_items(service, folder_id)

    for item in items:
        item_name = item.get("name", "")
        item_path = f"{parent_path}/{item_name}" if parent_path else item_name

        row = {
            "name": item_name,
            "path": item_path,
            "id": item.get("id"),
            "mimeType": item.get("mimeType"),
            "size": item.get("size", ""),
            "modifiedTime": item.get("modifiedTime", ""),
            "is_folder": item.get("mimeType") == FOLDER_MIME_TYPE,
        }
        rows.append(row)

        if item.get("mimeType") == FOLDER_MIME_TYPE:
            child_rows = list_items_recursive(
                service=service,
                folder_id=item.get("id"),
                parent_path=item_path,
            )
            rows.extend(child_rows)

    return rows


def find_folder_by_name(items, folder_name):
    for item in items:
        if (
            item.get("mimeType") == FOLDER_MIME_TYPE
            and item.get("name", "").strip().lower() == folder_name.strip().lower()
        ):
            return item
    return None


def download_text_file(service, file_id):
    request = service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read().decode("utf-8", errors="replace")


def find_prompt_file(prompt_items):
    for item in prompt_items:
        if item.get("name") == "universal_bp_audit_prompt.txt":
            return item
    return None


def classify_source_document(row):
    name = row.get("name", "").lower()
    path = row.get("path", "").lower()
    text = f"{path} {name}"

    if "design" in text or "brief" in text or "uzdev" in text:
        return "design_brief"

    if "geotechnical" in text or "gi_" in text or "ģeotehn" in text or "geotehn" in text:
        return "geotechnical"

    if "hydrogeology" in text or "hgi" in text or "hidro" in text:
        return "hydrogeology"

    if "tree" in text or "assessment" in text or "koku" in text or "dendro" in text:
        return "tree_assessment"

    if "topo" in text or "topogr" in text:
        return "topography"

    if "photofixation" in text or "photo" in text or "foto" in text:
        return "photofixation"

    return "other_source_data"


input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
prompt_folder_id = st.secrets.get("GOOGLE_DRIVE_PROMPT_FOLDER_ID")

st.markdown("## 1. Konfigurācija")

st.write("Input folder ID:", input_folder_id)
st.write("Prompt folder ID:", prompt_folder_id)

if st.button("Pārbaudīt 01_VD un prompt mapes"):
    try:
        drive_service = get_drive_service()

        st.success("Google Drive savienojums izveidots.")

        st.markdown("## 2. C2-3_TD sadaļu mapes")

        input_items = list_folder_items(drive_service, input_folder_id)
        input_df = pd.DataFrame(input_items)

        if input_df.empty:
            st.error("C2-3_TD mapē nav atrasti faili vai mapes.")
            st.stop()

        st.dataframe(input_df, use_container_width=True)

        vd_folder = find_folder_by_name(input_items, "01_VD")

        if not vd_folder:
            st.error("Nav atrasta sadaļas mape `01_VD`.")
            st.stop()

        st.success(f"Atrasta 01_VD mape: {vd_folder.get('name')}")

        st.markdown("## 3. 01_VD saturs, ieskaitot apakšmapes")

        vd_rows = list_items_recursive(
            service=drive_service,
            folder_id=vd_folder.get("id"),
            parent_path="01_VD",
        )

        vd_df = pd.DataFrame(vd_rows)

        if vd_df.empty:
            st.warning("01_VD mapē nav atrasts saturs.")
        else:
            st.dataframe(vd_df, use_container_width=True)

        st.markdown("## 4. Izejas dokumentu kandidāti")

        if not vd_df.empty:
            source_docs_df = vd_df[
                (vd_df["is_folder"] == False)
                & (
                    vd_df["mimeType"].str.contains("pdf", case=False, na=False)
                    | vd_df["mimeType"].str.contains("document", case=False, na=False)
                    | vd_df["mimeType"].str.contains("spreadsheet", case=False, na=False)
                    | vd_df["mimeType"].str.contains("text", case=False, na=False)
                )
            ].copy()

            if source_docs_df.empty:
                st.info("Nav atrasti izejas dokumentu kandidāti.")
            else:
                source_docs_df["source_document_type"] = source_docs_df.apply(
                    classify_source_document,
                    axis=1,
                )

                st.dataframe(source_docs_df, use_container_width=True)

                st.markdown("### Dokumentu tipu kopsavilkums")
                summary_df = (
                    source_docs_df.groupby("source_document_type")
                    .size()
                    .reset_index(name="count")
                    .sort_values("source_document_type")
                )
                st.dataframe(summary_df, use_container_width=True)

        st.markdown("## 5. 04_Prompt saturs")

        prompt_items = list_folder_items(drive_service, prompt_folder_id)
        prompt_df = pd.DataFrame(prompt_items)

        if prompt_df.empty:
            st.warning("04_Prompt mape ir tukša vai nav piekļuves.")
        else:
            st.dataframe(prompt_df, use_container_width=True)

        prompt_file = find_prompt_file(prompt_items)

        if prompt_file:
            st.success("Atrasts universal_bp_audit_prompt.txt")

            prompt_text = download_text_file(
                service=drive_service,
                file_id=prompt_file.get("id"),
            )

            st.markdown("### Prompt priekšskatījums")
            st.text_area(
                "Pirmie 4000 simboli no universal_bp_audit_prompt.txt",
                value=prompt_text[:4000],
                height=300,
            )
        else:
            st.warning("04_Prompt mapē nav atrasts universal_bp_audit_prompt.txt.")

    except Exception as e:
        st.error("Kļūda, pārbaudot 01_VD / Prompt struktūru.")
        st.exception(e)
