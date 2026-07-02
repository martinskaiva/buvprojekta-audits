import json

import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build


st.set_page_config(page_title="Google Drive tests", layout="wide")

st.title("Google Drive savienojuma tests")

st.write(
    "Šī testa aplikācija pārbauda, vai Streamlit var pieslēgties Google Drive "
    "un nolasīt projekta mapes."
)


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


def show_folder(service, title, folder_id):
    st.subheader(title)

    if not folder_id:
        st.error("Nav norādīts mapes ID.")
        return

    items = list_folder_items(service, folder_id)

    if not items:
        st.info("Mape ir tukša vai rīkam nav piekļuves.")
        return

    st.dataframe(items, use_container_width=True)


input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
results_folder_id = st.secrets.get("GOOGLE_DRIVE_RESULTS_FOLDER_ID")
memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")
prompt_folder_id = st.secrets.get("GOOGLE_DRIVE_PROMPT_FOLDER_ID")

st.markdown("### Konfigurācija")

st.write("Input folder ID:", input_folder_id)
st.write("Results folder ID:", results_folder_id)
st.write("Memory folder ID:", memory_folder_id)
st.write("Prompt folder ID:", prompt_folder_id)

if st.button("Pārbaudīt Google Drive savienojumu"):
    try:
        drive_service = get_drive_service()

        st.success("Google Drive savienojums izveidots.")

        show_folder(
            drive_service,
            "01_Input / C2-3_TD saturs",
            input_folder_id,
        )

        show_folder(
            drive_service,
            "02_Results saturs",
            results_folder_id,
        )

        show_folder(
            drive_service,
            "03_Memory saturs",
            memory_folder_id,
        )

        show_folder(
            drive_service,
            "04_Prompt saturs",
            prompt_folder_id,
        )

    except Exception as e:
        st.error("Neizdevās pieslēgties Google Drive.")
        st.exception(e)
