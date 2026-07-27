from __future__ import annotations

import io
import json
import re
import ssl
import socket
import time
import zipfile
from datetime import datetime
from typing import Any

import fitz
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

APP_NAME = "BP audita PDF Markup"
APP_VERSION = "2.2.5"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
INPUT_FOLDER_NAME = "01_Input"
RESULTS_FOLDER_NAME = "02_Results"
MEMORY_FOLDER_NAME = "03_Memory"
CONFIG_FOLDER_NAME = "04_Config"
PROJECT_CHAT_LINKS_FILENAME = "project_chat_links.json"
PACKAGE_ROOT_LABEL = "Dokumentu komplekta sakne"
EXCEL_SHEET_NAME = "Audit"
YELLOW = (1.0, 1.0, 0.0)

FAST_SAVE_SIZE_MB = 15
FAST_SAVE_PAGE_COUNT = 30
FAST_SAVE_TEXT_BLOCK_COUNT = 5000
TEXTUAL_DOCUMENT_HINTS = (
    "boq", "bill of quantities", "quantity", "take-off", "takeoff",
    "explanatory", "description", "specification", "report",
    "schedule", "technical note", "method statement",
)

REQUIRED_COLUMNS = [
    "Audit_ID",
    "Document_Filename",
    "Document_Number",
    "Page",
    "Location",
    "Category",
    "Element_Code",
    "Comment",
    "Anchor_Text",
    "Alternative_Anchor",
    "Reference_Document_Filename",
    "Reference_Document_Number",
    "Reference_Page",
    "Reference_Location",
    "Reference_Evidence_Text",
    "Annotation_Status",
]

st.set_page_config(page_title=f"{APP_NAME} v{APP_VERSION}", layout="wide")
st.title(f"{APP_NAME} v{APP_VERSION}")
st.caption(
    "ChatGPT sagatavots Excel → automātiska dzeltena teksta iezīmēšana → "
    "PDF komentārs → lejupielāde un saglabāšana Google Drive."
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def normalize_filename(value: Any) -> str:
    text = clean_text(value).casefold()
    text = re.sub(r"\.pdf$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*\d+\s*\)$", "", text)
    text = re.sub(r"[^a-z0-9āčēģīķļņōŗšūž]+", "", text)
    return text


def normalize_document_number(value: Any) -> str:
    text = clean_text(value).casefold()
    return re.sub(r"[^a-z0-9āčēģīķļņōŗšūž]+", "", text)


def safe_int(value: Any) -> int | None:
    try:
        if value is None or pd.isna(value):
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def safe_filename_part(value: Any) -> str:
    text = re.sub(r"[^\w\-]+", "_", clean_text(value), flags=re.UNICODE)
    return re.sub(r"_+", "_", text).strip("_") or "Audit"


def extract_folder_id(value: str) -> str:
    text = clean_text(value)
    for pattern in [r"/folders/([A-Za-z0-9_-]+)", r"[?&]id=([A-Za-z0-9_-]+)"]:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return text


def project_short_name(folder_name: str) -> str:
    return re.sub(r"^\d+[_\-\s]*", "", clean_text(folder_name)) or clean_text(folder_name)


def discipline_short_name(folder_name: str) -> str:
    name = re.sub(r"^\d+[_\-\s]*", "", clean_text(folder_name))
    first = re.split(r"[_\-\s]+", name, maxsplit=1)[0]
    aliases = {
        "Architecture": "AR", "Structure": "BK", "HVAC": "HVAC",
        "Site": "GP", "Fire": "UPP", "Power": "EL",
        "Communications": "ESS", "Water": "UK", "BoQ": "BoQ",
    }
    return aliases.get(first, first or "Audit")


def execute_with_retry(request_factory, attempts: int = 5):
    """Execute a Google API request with retries for transient transport errors."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return request_factory().execute(num_retries=2)
        except (ssl.SSLError, socket.timeout, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            time.sleep(min(8.0, 0.75 * (2 ** (attempt - 1))))
    if last_error:
        raise last_error
    raise RuntimeError("Google Drive pieprasījums neizdevās.")


DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def oauth_client_config() -> dict[str, Any]:
    client_id = clean_text(st.secrets.get("GOOGLE_OAUTH_CLIENT_ID", ""))
    client_secret = clean_text(st.secrets.get("GOOGLE_OAUTH_CLIENT_SECRET", ""))
    redirect_uri = clean_text(st.secrets.get("GOOGLE_OAUTH_REDIRECT_URI", ""))
    if not client_id or not client_secret or not redirect_uri:
        raise ValueError(
            "Streamlit Secrets jānorāda GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET un GOOGLE_OAUTH_REDIRECT_URI."
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": TOKEN_URI,
            "redirect_uris": [redirect_uri],
        }
    }


def credentials_to_dict(credentials: Credentials) -> dict[str, Any]:
    return {
        "token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri or TOKEN_URI,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": list(credentials.scopes or [DRIVE_SCOPE]),
    }


def credentials_from_session() -> Credentials | None:
    payload = st.session_state.get("oauth_credentials")
    if not payload:
        return None
    credentials = Credentials.from_authorized_user_info(payload, scopes=[DRIVE_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        st.session_state.oauth_credentials = credentials_to_dict(credentials)
    return credentials


def credentials_from_secrets() -> Credentials | None:
    refresh_token = clean_text(st.secrets.get("GOOGLE_OAUTH_REFRESH_TOKEN", ""))
    if not refresh_token:
        return None
    config = oauth_client_config()["web"]
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=[DRIVE_SCOPE],
    )
    credentials.refresh(Request())
    return credentials


def current_credentials() -> Credentials | None:
    credentials = credentials_from_session()
    if credentials:
        return credentials
    return credentials_from_secrets()


def get_drive_service():
    credentials = current_credentials()
    if not credentials:
        raise ValueError("Google Drive OAuth autorizācija nav pabeigta.")
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def begin_oauth_url() -> str:
    config = oauth_client_config()
    redirect_uri = config["web"]["redirect_uris"][0]
    flow = Flow.from_client_config(config, scopes=[DRIVE_SCOPE])
    flow.redirect_uri = redirect_uri
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    st.session_state.oauth_state = state
    return authorization_url


def process_oauth_callback() -> None:
    code = clean_text(st.query_params.get("code", ""))
    returned_state = clean_text(st.query_params.get("state", ""))
    error = clean_text(st.query_params.get("error", ""))
    if error:
        st.session_state.oauth_error = error
        st.query_params.clear()
        return
    if not code:
        return

    expected_state = clean_text(st.session_state.get("oauth_state", ""))
    if expected_state and returned_state != expected_state:
        st.session_state.oauth_error = "OAuth state neatbilst. Sāc autorizāciju vēlreiz."
        st.query_params.clear()
        return

    config = oauth_client_config()
    redirect_uri = config["web"]["redirect_uris"][0]
    flow = Flow.from_client_config(
        config,
        scopes=[DRIVE_SCOPE],
        state=returned_state or None,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    credentials = flow.credentials
    st.session_state.oauth_credentials = credentials_to_dict(credentials)
    st.session_state.new_refresh_token = credentials.refresh_token or ""
    st.session_state.oauth_error = ""
    st.query_params.clear()


def render_oauth_gate() -> bool:
    process_oauth_callback()
    st.markdown("## 0. Google Drive autorizācija")

    if st.session_state.get("oauth_error"):
        st.error(st.session_state.oauth_error)

    credentials = current_credentials()
    if credentials:
        st.success("Google Drive ir pieslēgts ar lietotāja OAuth kontu.")
        refresh_token = clean_text(st.session_state.get("new_refresh_token", ""))
        if refresh_token:
            st.warning(
                "Nokopē zemāk redzamo refresh token uz Streamlit Secrets. "
                "Pēc saglabāšanas šo lauku vari aizvērt."
            )
            st.code(
                f'GOOGLE_OAUTH_REFRESH_TOKEN = "{refresh_token}"',
                language="toml",
            )
        if st.button("Atvienot Google Drive šajā sesijā"):
            st.session_state.pop("oauth_credentials", None)
            st.session_state.pop("new_refresh_token", None)
            st.session_state.root_structure = None
            st.rerun()
        return True

    try:
        authorization_url = begin_oauth_url()
        st.info(
            "Pieslēdz Google kontu, kuram pieder 03_Markup mape. "
            "Pirmajā reizē Google atgriezīs refresh token."
        )
        st.link_button("Pieslēgt Google Drive", authorization_url, type="primary")
    except Exception as exc:
        st.error("OAuth konfigurācija nav derīga.")
        st.exception(exc)
    return False


def list_folder_items(service, folder_id: str) -> list[dict[str, Any]]:
    query = f"'{folder_id}' in parents and trashed = false"
    rows: list[dict[str, Any]] = []
    token = None
    while True:
        response = execute_with_retry(
            lambda: service.files().list(
                q=query,
                fields="nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
        )
        rows.extend(response.get("files", []))
        token = response.get("nextPageToken")
        if not token:
            break
    return sorted(rows, key=lambda x: (x.get("mimeType") != FOLDER_MIME_TYPE, x.get("name", "").casefold()))


def child_folders(service, parent_id: str) -> list[dict[str, Any]]:
    return [x for x in list_folder_items(service, parent_id) if x.get("mimeType") == FOLDER_MIME_TYPE]


def find_child_folder(service, parent_id: str, name: str) -> dict[str, Any] | None:
    return next((x for x in child_folders(service, parent_id) if x.get("name") == name), None)


def ensure_child_folder(service, parent_id: str, name: str) -> dict[str, Any]:
    existing = find_child_folder(service, parent_id, name)
    if existing:
        return existing
    return execute_with_retry(
        lambda: service.files().create(
            body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
            fields="id,name,mimeType",
            supportsAllDrives=True,
        )
    )


def find_child_file(
    service,
    parent_id: str,
    filename: str,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in list_folder_items(service, parent_id)
            if item.get("mimeType") != FOLDER_MIME_TYPE
            and item.get("name") == filename
        ),
        None,
    )


def upsert_bytes_to_drive(
    service,
    folder_id: str,
    filename: str,
    data: bytes,
    mime_type: str,
) -> dict[str, Any]:
    existing = find_child_file(service, folder_id, filename)
    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype=mime_type,
        resumable=False,
    )

    if existing:
        return execute_with_retry(
            lambda: service.files().update(
                fileId=existing["id"],
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
        )

    return upload_bytes_to_drive(
        service,
        folder_id,
        filename,
        data,
        mime_type,
    )


def load_project_chat_links(
    service,
    config_folder_id: str,
) -> dict[str, str]:
    file_item = find_child_file(
        service,
        config_folder_id,
        PROJECT_CHAT_LINKS_FILENAME,
    )
    if not file_item:
        return {}

    try:
        raw = download_drive_file_bytes(
            service,
            file_item["id"],
        )
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    return {
        clean_text(project_name): clean_text(chat_url)
        for project_name, chat_url in payload.items()
        if clean_text(project_name) and clean_text(chat_url)
    }


def save_project_chat_links(
    service,
    config_folder_id: str,
    links: dict[str, str],
) -> None:
    payload = {
        clean_text(project_name): clean_text(chat_url)
        for project_name, chat_url in sorted(
            links.items(),
            key=lambda item: clean_text(item[0]).casefold(),
        )
        if clean_text(project_name) and clean_text(chat_url)
    }
    data = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")

    upsert_bytes_to_drive(
        service,
        config_folder_id,
        PROJECT_CHAT_LINKS_FILENAME,
        data,
        "application/json",
    )


def is_valid_chatgpt_url(value: str) -> bool:
    url = clean_text(value)
    return bool(
        re.fullmatch(
            r"https://chatgpt\.com/[^\s]+",
            url,
            flags=re.IGNORECASE,
        )
    )


def render_project_chat_panel(
    service,
    config_folder_id: str,
    project_name: str,
) -> None:
    st.markdown("### Projekta ChatGPT audita čats")
    st.caption(
        "Katram projektam vari saglabāt atšķirīgu ChatGPT čata adresi. "
        "Čats atvērsies jaunā cilnē vai atsevišķā logā, bet Markup rīks paliks atvērts."
    )

    links = st.session_state.get("project_chat_links", {})
    field_key = f"project_chat_url_{safe_filename_part(project_name)}"

    if field_key not in st.session_state:
        st.session_state[field_key] = clean_text(
            links.get(project_name, "")
        )

    chat_url = st.text_input(
        "ChatGPT projekta čata adrese",
        key=field_key,
        placeholder="https://chatgpt.com/c/...",
    )

    save_col, delete_col = st.columns([1, 1])

    with save_col:
        if st.button(
            "Saglabāt čata adresi",
            key=f"save_chat_url_{safe_filename_part(project_name)}",
            type="primary",
        ):
            chat_url = clean_text(chat_url)
            if not is_valid_chatgpt_url(chat_url):
                st.error(
                    "Ievadi pilnu ChatGPT adresi, kas sākas ar "
                    "https://chatgpt.com/."
                )
            else:
                links = dict(
                    st.session_state.get(
                        "project_chat_links",
                        {},
                    )
                )
                links[project_name] = chat_url
                save_project_chat_links(
                    service,
                    config_folder_id,
                    links,
                )
                st.session_state.project_chat_links = links
                st.success(
                    f"Projekta {project_name} čata adrese saglabāta."
                )

    with delete_col:
        if st.button(
            "Dzēst saglabāto adresi",
            key=f"delete_chat_url_{safe_filename_part(project_name)}",
            disabled=not bool(
                clean_text(
                    st.session_state.get(
                        field_key,
                        "",
                    )
                )
            ),
        ):
            links = dict(
                st.session_state.get(
                    "project_chat_links",
                    {},
                )
            )
            links.pop(project_name, None)
            save_project_chat_links(
                service,
                config_folder_id,
                links,
            )
            st.session_state.project_chat_links = links
            st.session_state[field_key] = ""
            st.success(
                f"Projekta {project_name} čata adrese izdzēsta."
            )
            st.rerun()

    active_url = clean_text(
        st.session_state.get(field_key, "")
    )
    if is_valid_chatgpt_url(active_url):
        open_tab_col, popup_col = st.columns([1, 1])

        with open_tab_col:
            st.link_button(
                "Atvērt ChatGPT jaunā cilnē",
                active_url,
                use_container_width=True,
            )

        with popup_col:
            escaped_url = json.dumps(active_url)
            components.html(
                f"""
                <button
                    onclick='window.open(
                        {escaped_url},
                        "ChatGPTAudit",
                        "width=1000,height=900,resizable=yes,scrollbars=yes"
                    )'
                    style="
                        width:100%;
                        padding:0.62rem 0.75rem;
                        border:1px solid rgba(49,51,63,0.2);
                        border-radius:0.5rem;
                        background:white;
                        color:rgb(49,51,63);
                        font-size:1rem;
                        cursor:pointer;
                    "
                >
                    Atvērt ChatGPT atsevišķā logā
                </button>
                """,
                height=48,
            )
    else:
        st.info(
            "Šim projektam vēl nav saglabāta ChatGPT čata adrese."
        )


def list_pdfs_recursive(service, folder_id: str, parent_path: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in list_folder_items(service, folder_id):
        name = item.get("name", "")
        path = f"{parent_path}/{name}" if parent_path else name
        if item.get("mimeType") == FOLDER_MIME_TYPE:
            rows.extend(list_pdfs_recursive(service, item["id"], path))
        elif item.get("mimeType") == PDF_MIME_TYPE:
            rows.append({**item, "path": path})
    return sorted(rows, key=lambda x: x["path"].casefold())


def list_folders_recursive(service, folder_id: str, parent_path: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in child_folders(service, folder_id):
        path = f"{parent_path}/{item['name']}" if parent_path else item["name"]
        rows.append({**item, "path": path})
        rows.extend(list_folders_recursive(service, item["id"], path))
    return sorted(rows, key=lambda x: x["path"].casefold())


@st.cache_data(ttl=300, show_spinner=False)
def cached_child_folders(parent_id: str) -> list[dict[str, Any]]:
    return child_folders(get_drive_service(), parent_id)


@st.cache_data(ttl=300, show_spinner=False)
def cached_list_folder_items(folder_id: str) -> list[dict[str, Any]]:
    return list_folder_items(get_drive_service(), folder_id)


@st.cache_data(ttl=300, show_spinner=False)
def cached_folders_recursive(folder_id: str, parent_path: str = "") -> list[dict[str, Any]]:
    return list_folders_recursive(get_drive_service(), folder_id, parent_path)


@st.cache_data(ttl=300, show_spinner=False)
def cached_pdfs_recursive(folder_id: str, parent_path: str = "") -> list[dict[str, Any]]:
    return list_pdfs_recursive(get_drive_service(), folder_id, parent_path)


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    attempts = 0
    while not done:
        try:
            _, done = downloader.next_chunk(num_retries=2)
            attempts = 0
        except (ssl.SSLError, socket.timeout, ConnectionError, OSError):
            attempts += 1
            if attempts >= 5:
                raise
            time.sleep(min(8.0, 0.75 * (2 ** (attempts - 1))))
    return buffer.getvalue()


def upload_bytes_to_drive(service, folder_id: str, filename: str, data: bytes, mime_type: str) -> dict[str, Any]:
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    return service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id,name,webViewLink",
        supportsAllDrives=True,
    ).execute()


def read_audit_excel(data: bytes) -> pd.DataFrame:
    xls = pd.ExcelFile(io.BytesIO(data))
    sheet = EXCEL_SHEET_NAME if EXCEL_SHEET_NAME in xls.sheet_names else xls.sheet_names[0]
    df = pd.read_excel(io.BytesIO(data), sheet_name=sheet).dropna(how="all").copy()
    df.columns = [clean_text(c) for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError("Excel trūkst obligāto kolonnu: " + ", ".join(missing))
    df = df[REQUIRED_COLUMNS].copy()
    df["Page"] = df["Page"].apply(safe_int)
    df["Reference_Page"] = df["Reference_Page"].apply(safe_int)
    for column in REQUIRED_COLUMNS:
        if column not in {"Page", "Reference_Page"}:
            df[column] = df[column].apply(clean_text)
    df["Document_Filename_Norm"] = df["Document_Filename"].apply(normalize_filename)
    df["Annotation_Status"] = ""
    return df


def validate_rows(
    df: pd.DataFrame,
    selected_pdf_rows: list[dict[str, Any]],
) -> pd.DataFrame:
    work = df.copy()

    selected_index: list[dict[str, Any]] = []
    for item in selected_pdf_rows:
        actual_name = clean_text(item.get("name"))
        selected_index.append(
            {
                "id": item.get("id", ""),
                "name": actual_name,
                "path": clean_text(item.get("path")),
                "filename_norm": normalize_filename(actual_name),
                "filename_document_norm": normalize_document_number(
                    re.sub(
                        r"\.pdf$",
                        "",
                        actual_name,
                        flags=re.IGNORECASE,
                    )
                ),
            }
        )

    statuses: list[str] = []
    messages: list[str] = []
    matched_names: list[str] = []
    matched_ids: list[str] = []
    match_methods: list[str] = []

    for _, row in work.iterrows():
        status = "ok"
        message = ""
        matched_name = ""
        matched_id = ""
        match_method = ""

        excel_filename = clean_text(row["Document_Filename"])
        excel_filename_norm = normalize_filename(excel_filename)
        document_number_norm = normalize_document_number(
            row["Document_Number"]
        )

        matches = [
            item
            for item in selected_index
            if item["filename_norm"] == excel_filename_norm
        ]
        if len(matches) == 1:
            match_method = "normalized_filename"

        if not matches and document_number_norm:
            matches = [
                item
                for item in selected_index
                if document_number_norm in item["filename_document_norm"]
            ]
            if len(matches) == 1:
                match_method = "document_number_in_filename"

        if not row["Audit_ID"]:
            status, message = "invalid", "Nav Audit_ID."
        elif not excel_filename and not row["Document_Number"]:
            status, message = "invalid", "Nav ne Document_Filename, ne Document_Number."
        elif len(matches) == 0:
            status = "file_not_selected"
            message = (
                "Excel norādītajam dokumentam nav atrasts izvēlēts PDF "
                "pēc normalizēta faila nosaukuma vai Document_Number."
            )
        elif len(matches) > 1:
            status = "file_match_ambiguous"
            message = (
                "Atrasti vairāki iespējamie PDF. Nepieciešams precīzāks "
                "Document_Filename vai Document_Number."
            )
        elif row["Page"] is None:
            status, message = "invalid", "Nav derīga Page vērtība."
        elif not row["Comment"]:
            status, message = "invalid", "Nav Comment."
        elif (
            clean_text(row["Category"]).casefold() != "no discrepancies"
            and not any(
                [
                    row["Anchor_Text"],
                    row["Alternative_Anchor"],
                    row["Element_Code"],
                ]
            )
        ):
            status, message = (
                "invalid",
                "Nav Anchor_Text, Alternative_Anchor vai Element_Code.",
            )

        if len(matches) == 1:
            matched_name = matches[0]["name"]
            matched_id = matches[0]["id"]

        statuses.append(status)
        messages.append(message)
        matched_names.append(matched_name)
        matched_ids.append(matched_id)
        match_methods.append(match_method)

    work["_validation_status"] = statuses
    work["_validation_message"] = messages
    work["_matched_pdf_name"] = matched_names
    work["_matched_pdf_id"] = matched_ids
    work["_match_method"] = match_methods

    valid_mask = work["_validation_status"] == "ok"
    work.loc[valid_mask, "Document_Filename"] = work.loc[
        valid_mask,
        "_matched_pdf_name",
    ]
    work.loc[valid_mask, "Document_Filename_Norm"] = work.loc[
        valid_mask,
        "_matched_pdf_name",
    ].apply(normalize_filename)

    return work

def completed_excel_bytes(df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df[REQUIRED_COLUMNS].to_excel(writer, sheet_name=EXCEL_SHEET_NAME, index=False)
        ws = writer.book[EXCEL_SHEET_NAME]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        widths = [14, 52, 34, 10, 42, 24, 18, 95, 45, 38, 52, 34, 14, 42, 60, 28]
        for idx, width in enumerate(widths, 1):
            ws.column_dimensions[chr(64 + idx)].width = width
    return output.getvalue()


def search_variants(primary: str, alternative: str, element_code: str) -> list[tuple[str, str]]:
    variants: list[tuple[str, str]] = []
    for kind, raw in [("primary", primary), ("alternative", alternative), ("element", element_code)]:
        value = clean_text(raw)
        for candidate in [value, value.replace("–", "-").replace("—", "-"), value.replace("“", '"').replace("”", '"')]:
            candidate = clean_text(candidate)
            if candidate and all(existing[1] != candidate for existing in variants):
                variants.append((kind, candidate))
    return variants


def add_comment(page: fitz.Page, point: fitz.Point, comment: str) -> None:
    note = page.add_text_annot(point, comment)
    note.set_info(title="BP audits", subject="Audita piezīme", content=comment)
    note.update()


def is_textual_document_filename(filename: str) -> bool:
    name = clean_text(filename).casefold()
    return any(hint in name for hint in TEXTUAL_DOCUMENT_HINTS)


def estimate_text_block_count(doc: fitz.Document, max_pages_to_scan: int = 12) -> int:
    if len(doc) == 0:
        return 0
    pages_to_scan = min(len(doc), max_pages_to_scan)
    sampled = 0
    for page_index in range(pages_to_scan):
        try:
            sampled += len(doc[page_index].get_text("blocks"))
        except Exception:
            continue
    if pages_to_scan == len(doc):
        return sampled
    return int(round((sampled / max(pages_to_scan, 1)) * len(doc)))


def choose_pdf_save_mode(doc: fitz.Document, filename: str, source_size_bytes: int) -> tuple[str, dict[str, Any]]:
    size_mb = source_size_bytes / (1024 * 1024)
    page_count = len(doc)
    estimated_blocks = estimate_text_block_count(doc)
    textual_name = is_textual_document_filename(filename)
    fast = (
        size_mb >= FAST_SAVE_SIZE_MB
        or page_count >= FAST_SAVE_PAGE_COUNT
        or estimated_blocks >= FAST_SAVE_TEXT_BLOCK_COUNT
        or (textual_name and page_count >= 8)
    )
    mode = "fast_text_document" if fast else "standard_drawing"
    return mode, {
        "mode": mode,
        "size_mb": round(size_mb, 2),
        "page_count": page_count,
        "estimated_text_blocks": estimated_blocks,
        "filename_textual": textual_name,
    }


def annotate_pdf(pdf_bytes: bytes, rows: pd.DataFrame, filename: str = "") -> tuple[bytes, dict[int, str], dict[str, Any]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    statuses: dict[int, str] = {}
    page_counts: dict[int, int] = {}
    no_issue_pages: set[int] = set()

    for row_index, row in rows.iterrows():
        page_no = safe_int(row["Page"])
        comment = clean_text(row["Comment"])
        category = clean_text(row["Category"]).casefold()

        if page_no is None or page_no < 1 or page_no > len(doc):
            statuses[row_index] = "page_not_found"
            continue

        page = doc[page_no - 1]
        page_counts.setdefault(page_no, 0)

        if category == "no discrepancies":
            if page_no not in no_issue_pages:
                note_text = comment or "Audita rezultātā piezīmes nav konstatētas."
                add_comment(
                    page,
                    fitz.Point(max(36, page.rect.width - 80), 36),
                    note_text,
                )
                no_issue_pages.add(page_no)
                statuses[row_index] = "no_issues_note_added"
            else:
                statuses[row_index] = "no_issues_note_duplicate_skipped"
            continue

        found_rects: list[fitz.Rect] = []
        found_kind = ""

        for kind, search_text in search_variants(
            row["Anchor_Text"],
            row["Alternative_Anchor"],
            row["Element_Code"],
        ):
            rects = page.search_for(search_text)
            if rects:
                found_rects = rects
                found_kind = kind
                break

        if not found_rects:
            add_comment(
                page,
                fitz.Point(
                    max(36, page.rect.width - 80),
                    36 + page_counts[page_no] * 24,
                ),
                comment,
            )
            page_counts[page_no] += 1
            statuses[row_index] = "comment_only"
            continue

        rect = found_rects[0]
        highlight = page.add_highlight_annot(rect)
        highlight.set_colors(stroke=YELLOW)
        highlight.update()

        add_comment(
            page,
            fitz.Point(
                min(page.rect.width - 24, rect.x1 + 8),
                max(24, rect.y0),
            ),
            comment,
        )

        if len(found_rects) > 1:
            statuses[row_index] = "highlighted_first_match"
        elif found_kind == "alternative":
            statuses[row_index] = "highlighted_alternative_anchor"
        elif found_kind == "element":
            statuses[row_index] = "highlighted_element_code"
        else:
            statuses[row_index] = "highlighted"

    save_mode, save_diagnostics = choose_pdf_save_mode(
        doc, filename, len(pdf_bytes)
    )
    output = io.BytesIO()
    if save_mode == "fast_text_document":
        doc.save(output, deflate=True)
    else:
        doc.save(output, garbage=4, deflate=True)
    doc.close()
    return output.getvalue(), statuses, save_diagnostics

def marked_name(filename: str) -> str:
    return re.sub(r"\.pdf$", "", filename, flags=re.I) + "_marked.pdf"


def zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


for key, default in {
    "root_structure": None,
    "project_folders": [],
    "result_folders": [],
    "audit_df": pd.DataFrame(),
    "completed_df": pd.DataFrame(),
    "zip_bytes": None,
    "memory_filename": "",
    "oauth_credentials": None,
    "oauth_state": "",
    "oauth_error": "",
    "new_refresh_token": "",
    "drive_upload_message": "",
    "project_chat_links": {},
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

if not render_oauth_gate():
    st.stop()

st.markdown("## 1. Google Drive struktūra")
default_root = st.secrets.get("GOOGLE_DRIVE_MARKUP_ROOT_FOLDER_ID", "")
root_value = st.text_input("03_Markup mapes ID vai saite", value=default_root)
root_id = extract_folder_id(root_value)

if st.button("Nolasīt 03_Markup struktūru", type="primary"):
    try:
        if not root_id:
            raise ValueError("Nav norādīta 03_Markup mapes saite vai ID.")
        service = get_drive_service()
        input_folder = find_child_folder(service, root_id, INPUT_FOLDER_NAME)
        results_folder = find_child_folder(service, root_id, RESULTS_FOLDER_NAME)
        memory_folder = find_child_folder(service, root_id, MEMORY_FOLDER_NAME)
        missing = [
            name
            for name, item in [
                (INPUT_FOLDER_NAME, input_folder),
                (RESULTS_FOLDER_NAME, results_folder),
                (MEMORY_FOLDER_NAME, memory_folder),
            ]
            if item is None
        ]
        if missing:
            raise ValueError(
                "03_Markup mapē nav atrastas mapes: "
                + ", ".join(missing)
            )

        config_folder = ensure_child_folder(
            service,
            root_id,
            CONFIG_FOLDER_NAME,
        )
        st.session_state.root_structure = {
            "root_id": root_id,
            "input": input_folder,
            "results": results_folder,
            "memory": memory_folder,
            "config": config_folder,
        }
        st.session_state.project_chat_links = load_project_chat_links(
            service,
            config_folder["id"],
        )
        st.session_state.project_folders = cached_child_folders(input_folder["id"])
        st.session_state.result_folders = [{"id": results_folder["id"], "name": results_folder["name"], "path": results_folder["name"]}, *cached_folders_recursive(results_folder["id"], results_folder["name"])]
        st.success("03_Markup struktūra nolasīta.")
    except Exception as exc:
        st.error("Neizdevās nolasīt Google Drive struktūru.")
        st.exception(exc)

root = st.session_state.root_structure
if root:
    service = get_drive_service()
    st.markdown("## 2. Avota PDF izvēle")
    projects = st.session_state.project_folders
    if not projects:
        st.warning("01_Input mapē nav projektu mapju.")
        st.stop()
    project_name = st.selectbox("Projekts", [x["name"] for x in projects])
    project = next(x for x in projects if x["name"] == project_name)

    render_project_chat_panel(
        service,
        root["config"]["id"],
        project_name,
    )

    packages = cached_child_folders(project["id"])
    if packages:
        package_name = st.selectbox("Dokumentu komplekts", [x["name"] for x in packages])
        package = next(x for x in packages if x["name"] == package_name)
    else:
        package_name, package = project_name, project
    discipline_folders = cached_folders_recursive(package["id"])

    root_pdf_rows = [
        {
            **item,
            "path": f"{PACKAGE_ROOT_LABEL}/{item['name']}",
        }
        for item in cached_list_folder_items(package["id"])
        if item.get("mimeType") == PDF_MIME_TYPE
    ]

    selectable_folders: list[dict[str, Any]] = []
    if root_pdf_rows:
        selectable_folders.append(
            {
                "id": package["id"],
                "name": PACKAGE_ROOT_LABEL,
                "path": PACKAGE_ROOT_LABEL,
                "is_virtual_root": True,
            }
        )
    selectable_folders.extend(discipline_folders)

    if not selectable_folders:
        st.warning(
            "Izvēlētajā komplektā nav ne apakšmapju, ne PDF failu "
            "dokumentu komplekta saknē."
        )
        st.stop()

    st.markdown("### Mapes")
    st.caption(
        "Atzīmē vienu vai vairākas mapes. Ja PDF atrodas tieši dokumentu "
        "komplekta mapē, izvēlies “Dokumentu komplekta sakne”."
    )

    selected_folder_rows: list[dict[str, Any]] = []
    folder_key_prefix = f"source_folder_{project['id']}_{package['id']}"
    for folder in selectable_folders:
        virtual_suffix = "_root" if folder.get("is_virtual_root") else ""
        folder_key = f"{folder_key_prefix}_{folder['id']}{virtual_suffix}"
        if st.checkbox(folder["path"], key=folder_key, value=False):
            selected_folder_rows.append(folder)

    if not selected_folder_rows:
        st.info("Atzīmē vismaz vienu mapi, lai parādītu tajā esošos PDF failus.")
        selected_pdfs = []
        discipline_name = "Vairākas_mapes"
    else:
        pdf_by_id: dict[str, dict[str, Any]] = {}
        for folder in selected_folder_rows:
            if folder.get("is_virtual_root"):
                folder_pdfs = root_pdf_rows
            else:
                folder_pdfs = cached_pdfs_recursive(
                    folder["id"],
                    folder["path"],
                )
            for pdf_item in folder_pdfs:
                pdf_by_id[pdf_item["id"]] = pdf_item

        pdf_rows = sorted(pdf_by_id.values(), key=lambda x: x["path"].casefold())
        discipline_name = (
            selected_folder_rows[0]["name"]
            if len(selected_folder_rows) == 1
            else "Vairākas_mapes"
        )

        st.markdown("### PDF faili")
        if not pdf_rows:
            st.warning("Izvēlētajās mapēs nav PDF failu.")
            selected_pdfs = []
        else:
            select_all_key = f"select_all_files_{project['id']}_{package['id']}"
            select_all_files = st.checkbox(
                "Atzīmēt visus failus izvēlētajās mapēs",
                key=select_all_key,
                value=False,
            )

            selected_pdfs = []
            file_key_prefix = f"source_file_{project['id']}_{package['id']}"
            for pdf_item in pdf_rows:
                file_key = f"{file_key_prefix}_{pdf_item['id']}"
                if select_all_files:
                    st.session_state[file_key] = True
                is_selected = st.checkbox(
                    pdf_item["path"],
                    key=file_key,
                    value=bool(st.session_state.get(file_key, False)),
                )
                if is_selected:
                    selected_pdfs.append(pdf_item)

            st.caption(
                f"Izvēlētas {len(selected_folder_rows)} mapes un "
                f"{len(selected_pdfs)} no {len(pdf_rows)} PDF failiem."
            )

    st.markdown("## 3. ChatGPT sagatavotais Excel")
    st.caption(
        "Rīks ignorē dublikātu sufiksus, piemēram, (1), (2) un (3), "
        "un vajadzības gadījumā sasaista dokumentu pēc Document_Number."
    )
    upload = st.file_uploader("Augšupielādē apstiprināto piezīmju Excel", type=["xlsx"])
    if upload is not None:
        try:
            audit_df = read_audit_excel(upload.getvalue())
            validated = validate_rows(audit_df, selected_pdfs)
            st.session_state.audit_df = validated
            ok = int((validated["_validation_status"] == "ok").sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Excel piezīmes", len(validated))
            c2.metric("Tehniski derīgas", ok)
            c3.metric("Neapstrādājamas", len(validated) - ok)
            if ok != len(validated):
                with st.expander("Parādīt tehniskās validācijas problēmas"):
                    st.dataframe(
                        validated[validated["_validation_status"] != "ok"][
                            [
                                "Audit_ID",
                                "Document_Filename",
                                "Document_Number",
                                "Page",
                                "_validation_status",
                                "_validation_message",
                                "_matched_pdf_name",
                                "_match_method",
                            ]
                        ],
                        use_container_width=True,
                        hide_index=True,
                    )
            else:
                st.success("Visas Excel rindas ir tehniski derīgas.")
        except Exception as exc:
            st.session_state.audit_df = pd.DataFrame()
            st.error("Excel struktūra nav derīga.")
            st.exception(exc)

    st.markdown("## 4. Rezultātu saglabāšana")
    st.caption(
        "Lieliem tekstuāliem PDF, piemēram, BoQ, skaidrojošajiem aprakstiem "
        "un specifikācijām, rīks automātiski izmanto ātro saglabāšanas režīmu. "
        "Rasējumiem paliek pilnā PDF optimizācija."
    )
    result_folders = st.session_state.result_folders
    result_path = st.selectbox("Drive rezultātu mape", [x["path"] for x in result_folders])
    result_folder = next(x for x in result_folders if x["path"] == result_path)
    can_run = bool(selected_pdfs) and not st.session_state.audit_df.empty and (st.session_state.audit_df["_validation_status"] == "ok").any()

    if st.button("Automātiski uzlikt piezīmes", type="primary", disabled=not can_run):
        try:
            work = st.session_state.audit_df.copy()
            selected_map = {normalize_filename(x["name"]): x for x in selected_pdfs}
            valid = work[work["_validation_status"] == "ok"].copy()
            grouped = {k: g.copy() for k, g in valid.groupby("Document_Filename_Norm")}
            outputs: dict[str, bytes] = {}
            progress = st.progress(0, text="Sagatavoju PDF anotēšanu…")
            for idx, (filename_norm, group) in enumerate(grouped.items(), 1):
                pdf_item = selected_map.get(filename_norm)
                if not pdf_item:
                    for row_index in group.index:
                        work.at[row_index, "Annotation_Status"] = "file_not_found"
                    continue
                pdf_bytes = download_drive_file_bytes(service, pdf_item["id"])
                annotated, statuses, save_diagnostics = annotate_pdf(pdf_bytes, group, pdf_item["name"])
                for row_index, status in statuses.items():
                    work.at[row_index, "Annotation_Status"] = status
                outputs[marked_name(pdf_item["name"])] = annotated
                save_mode_label = (
                    "ātrais teksta PDF režīms"
                    if save_diagnostics["mode"] == "fast_text_document"
                    else "standarta rasējumu režīms"
                )
                progress.progress(idx / max(len(grouped), 1), text=f"Apstrādāts {idx}. no {len(grouped)} PDF.")
            for row_index, row in work.iterrows():
                if not row["Annotation_Status"] and row["_validation_status"] != "ok":
                    work.at[row_index, "Annotation_Status"] = row["_validation_status"]
            completed_excel = completed_excel_bytes(work)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            project_short = project_short_name(project_name)
            discipline_short = discipline_short_name(discipline_name)
            memory_filename = f"{timestamp}_{safe_filename_part(project_short)}_{safe_filename_part(discipline_short)}_Audit.xlsx"

            st.session_state.completed_df = work
            st.session_state.zip_bytes = zip_bytes(outputs)
            st.session_state.memory_filename = memory_filename

            try:
                memory_project = ensure_child_folder(
                    service,
                    root["memory"]["id"],
                    project_name,
                )
                upload_bytes_to_drive(
                    service,
                    memory_project["id"],
                    memory_filename,
                    completed_excel,
                    XLSX_MIME_TYPE,
                )

                for filename, data in outputs.items():
                    upload_bytes_to_drive(
                        service,
                        result_folder["id"],
                        filename,
                        data,
                        PDF_MIME_TYPE,
                    )

                st.session_state.drive_upload_message = (
                    f"Anotēti {len(outputs)} PDF. PDF saglabāti tieši izvēlētajā "
                    "Drive rezultātu mapē un sagatavoti lejupielādei."
                )
                st.success(st.session_state.drive_upload_message)
            except Exception as upload_exc:
                st.session_state.drive_upload_message = (
                    "PDF anotēšana ir pabeigta un ZIP ir pieejams, bet augšupielāde Drive neizdevās."
                )
                st.warning(st.session_state.drive_upload_message)
                st.exception(upload_exc)
        except Exception as exc:
            st.error("Piezīmju uzlikšana neizdevās.")
            st.exception(exc)

if st.session_state.zip_bytes is not None:
    st.markdown("## 5. Gatavie rezultāti")
    summary = st.session_state.completed_df.groupby("Annotation_Status", dropna=False).size().reset_index(name="Skaits")
    st.dataframe(summary, use_container_width=True, hide_index=True)
    if st.session_state.drive_upload_message:
        st.info(st.session_state.drive_upload_message)
    if st.session_state.memory_filename:
        st.caption("Memory Excel faila nosaukums: " + st.session_state.memory_filename)
    st.download_button("Lejupielādēt anotētos PDF ZIP", data=st.session_state.zip_bytes, file_name="BP_Audit_Marked_PDF.zip", mime="application/zip", type="primary")
    with st.expander("Parādīt pilnu anotēšanas atskaiti"):
        st.dataframe(st.session_state.completed_df[REQUIRED_COLUMNS], use_container_width=True, hide_index=True)
