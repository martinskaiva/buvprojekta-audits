from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime
from typing import Any

import fitz
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

APP_NAME = "BP audita PDF Markup"
APP_VERSION = "2.0.2"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
INPUT_FOLDER_NAME = "01_Input"
RESULTS_FOLDER_NAME = "02_Results"
MEMORY_FOLDER_NAME = "03_Memory"
EXCEL_SHEET_NAME = "Audit"
YELLOW = (1.0, 1.0, 0.0)

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
    return clean_text(value).casefold()


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


@st.cache_resource(show_spinner=False)
def get_drive_service():
    raw = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise ValueError("Streamlit Secrets nav atrasts GOOGLE_SERVICE_ACCOUNT_JSON.")
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def list_folder_items(service, folder_id: str) -> list[dict[str, Any]]:
    query = f"'{folder_id}' in parents and trashed = false"
    rows: list[dict[str, Any]] = []
    token = None
    while True:
        response = service.files().list(
            q=query,
            fields="nextPageToken,files(id,name,mimeType,size,modifiedTime)",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
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
    return service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
        fields="id,name,mimeType",
        supportsAllDrives=True,
    ).execute()


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


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
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


def validate_rows(df: pd.DataFrame, selected_pdf_names: set[str]) -> pd.DataFrame:
    work = df.copy()
    statuses, messages = [], []
    for _, row in work.iterrows():
        status, message = "ok", ""
        if not row["Audit_ID"]:
            status, message = "invalid", "Nav Audit_ID."
        elif not row["Document_Filename"]:
            status, message = "invalid", "Nav Document_Filename."
        elif row["Document_Filename_Norm"] not in selected_pdf_names:
            status, message = "file_not_selected", "Excel norādītais PDF nav izvēlēts."
        elif row["Page"] is None:
            status, message = "invalid", "Nav derīga Page vērtība."
        elif not row["Comment"]:
            status, message = "invalid", "Nav Comment."
        elif not any([row["Anchor_Text"], row["Alternative_Anchor"], row["Element_Code"]]):
            status, message = "invalid", "Nav Anchor_Text, Alternative_Anchor vai Element_Code."
        statuses.append(status)
        messages.append(message)
    work["_validation_status"] = statuses
    work["_validation_message"] = messages
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


def annotate_pdf(pdf_bytes: bytes, rows: pd.DataFrame) -> tuple[bytes, dict[int, str]]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    statuses: dict[int, str] = {}
    page_counts: dict[int, int] = {}
    for row_index, row in rows.iterrows():
        page_no = safe_int(row["Page"])
        comment = clean_text(row["Comment"])
        if page_no is None or page_no < 1 or page_no > len(doc):
            statuses[row_index] = "page_not_found"
            continue
        page = doc[page_no - 1]
        page_counts.setdefault(page_no, 0)
        found_rects: list[fitz.Rect] = []
        found_kind = ""
        for kind, text in search_variants(row["Anchor_Text"], row["Alternative_Anchor"], row["Element_Code"]):
            rects = page.search_for(text)
            if rects:
                found_rects, found_kind = rects, kind
                break
        if not found_rects:
            add_comment(page, fitz.Point(max(36, page.rect.width - 80), 36 + page_counts[page_no] * 24), comment)
            page_counts[page_no] += 1
            statuses[row_index] = "comment_only"
            continue
        rect = found_rects[0]
        highlight = page.add_highlight_annot(rect)
        highlight.set_colors(stroke=YELLOW)
        highlight.update()
        add_comment(page, fitz.Point(min(page.rect.width - 24, rect.x1 + 8), max(24, rect.y0)), comment)
        if len(found_rects) > 1:
            statuses[row_index] = "highlighted_first_match"
        elif found_kind == "alternative":
            statuses[row_index] = "highlighted_alternative_anchor"
        elif found_kind == "element":
            statuses[row_index] = "highlighted_element_code"
        else:
            statuses[row_index] = "highlighted"
    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    return output.getvalue(), statuses


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
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

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
        missing = [n for n, x in [(INPUT_FOLDER_NAME, input_folder), (RESULTS_FOLDER_NAME, results_folder), (MEMORY_FOLDER_NAME, memory_folder)] if x is None]
        if missing:
            raise ValueError("03_Markup mapē nav atrastas mapes: " + ", ".join(missing))
        st.session_state.root_structure = {"root_id": root_id, "input": input_folder, "results": results_folder, "memory": memory_folder}
        st.session_state.project_folders = child_folders(service, input_folder["id"])
        st.session_state.result_folders = [{"id": results_folder["id"], "name": results_folder["name"], "path": results_folder["name"]}, *list_folders_recursive(service, results_folder["id"], results_folder["name"])]
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
    packages = child_folders(service, project["id"])
    if packages:
        package_name = st.selectbox("Dokumentu komplekts", [x["name"] for x in packages])
        package = next(x for x in packages if x["name"] == package_name)
    else:
        package_name, package = project_name, project
    discipline_folders = list_folders_recursive(service, package["id"])
    if not discipline_folders:
        st.warning("Izvēlētajā komplektā nav mapju ar PDF dokumentiem.")
        st.stop()

    st.markdown("### Mapes")
    st.caption("Atzīmē vienu vai vairākas mapes. Pilnais mapes ceļš redzams katrā rindā.")

    selected_folder_rows: list[dict[str, Any]] = []
    folder_key_prefix = f"source_folder_{project['id']}_{package['id']}"
    for folder in discipline_folders:
        folder_key = f"{folder_key_prefix}_{folder['id']}"
        if st.checkbox(folder["path"], key=folder_key, value=False):
            selected_folder_rows.append(folder)

    if not selected_folder_rows:
        st.info("Atzīmē vismaz vienu mapi, lai parādītu tajā esošos PDF failus.")
        selected_pdfs = []
        discipline_name = "Vairākas_mapes"
    else:
        # Savāc PDF no katras izvēlētās mapes. Ja atzīmēts arī vecāks un bērna
        # folderis, vienu un to pašu failu sarakstā iekļauj tikai vienu reizi.
        pdf_by_id: dict[str, dict[str, Any]] = {}
        for folder in selected_folder_rows:
            folder_pdfs = list_pdfs_recursive(service, folder["id"], folder["path"])
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
    upload = st.file_uploader("Augšupielādē apstiprināto piezīmju Excel", type=["xlsx"])
    if upload is not None:
        try:
            audit_df = read_audit_excel(upload.getvalue())
            selected_names = {normalize_filename(x["name"]) for x in selected_pdfs}
            validated = validate_rows(audit_df, selected_names)
            st.session_state.audit_df = validated
            ok = int((validated["_validation_status"] == "ok").sum())
            c1, c2, c3 = st.columns(3)
            c1.metric("Excel piezīmes", len(validated))
            c2.metric("Tehniski derīgas", ok)
            c3.metric("Neapstrādājamas", len(validated) - ok)
            if ok != len(validated):
                with st.expander("Parādīt tehniskās validācijas problēmas"):
                    st.dataframe(validated[validated["_validation_status"] != "ok"][["Audit_ID", "Document_Filename", "Page", "_validation_status", "_validation_message"]], use_container_width=True, hide_index=True)
            else:
                st.success("Visas Excel rindas ir tehniski derīgas.")
        except Exception as exc:
            st.session_state.audit_df = pd.DataFrame()
            st.error("Excel struktūra nav derīga.")
            st.exception(exc)

    st.markdown("## 4. Rezultātu saglabāšana")
    result_folders = st.session_state.result_folders
    result_path = st.selectbox("Drive rezultātu mape", [x["path"] for x in result_folders])
    result_folder = next(x for x in result_folders if x["path"] == result_path)
    create_session = st.checkbox("Rezultātu mapē izveidot sesijas apakšmapi", value=True)
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
                annotated, statuses = annotate_pdf(pdf_bytes, group)
                for row_index, status in statuses.items():
                    work.at[row_index, "Annotation_Status"] = status
                outputs[marked_name(pdf_item["name"])] = annotated
                progress.progress(idx / max(len(grouped), 1), text=f"Apstrādāts {idx}. no {len(grouped)} PDF.")
            for row_index, row in work.iterrows():
                if not row["Annotation_Status"] and row["_validation_status"] != "ok":
                    work.at[row_index, "Annotation_Status"] = row["_validation_status"]
            completed_excel = completed_excel_bytes(work)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            project_short = project_short_name(project_name)
            discipline_short = discipline_short_name(discipline_name)
            memory_filename = f"{timestamp}_{safe_filename_part(project_short)}_{safe_filename_part(discipline_short)}_Audit.xlsx"
            memory_project = ensure_child_folder(service, root["memory"]["id"], project_name)
            upload_bytes_to_drive(service, memory_project["id"], memory_filename, completed_excel, XLSX_MIME_TYPE)
            target_folder = result_folder
            if create_session:
                session_name = f"{timestamp}_{safe_filename_part(project_short)}_{safe_filename_part(discipline_short)}"
                target_folder = ensure_child_folder(service, result_folder["id"], session_name)
            for filename, data in outputs.items():
                upload_bytes_to_drive(service, target_folder["id"], filename, data, PDF_MIME_TYPE)
            report_name = f"{timestamp}_{safe_filename_part(project_short)}_{safe_filename_part(discipline_short)}_Markup_Report.xlsx"
            upload_bytes_to_drive(service, target_folder["id"], report_name, completed_excel, XLSX_MIME_TYPE)
            download_files = {**outputs, report_name: completed_excel}
            st.session_state.completed_df = work
            st.session_state.zip_bytes = zip_bytes(download_files)
            st.session_state.memory_filename = memory_filename
            st.success(f"Anotēti {len(outputs)} PDF. Rezultāti saglabāti Drive un sagatavoti lejupielādei.")
        except Exception as exc:
            st.error("Piezīmju uzlikšana neizdevās.")
            st.exception(exc)

if st.session_state.zip_bytes is not None:
    st.markdown("## 5. Gatavie rezultāti")
    summary = st.session_state.completed_df.groupby("Annotation_Status", dropna=False).size().reset_index(name="Skaits")
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.info("Memory mapē saglabāts: " + st.session_state.memory_filename)
    st.download_button("Lejupielādēt anotētos PDF un Excel ZIP", data=st.session_state.zip_bytes, file_name="BP_Audit_Markup_Results.zip", mime="application/zip", type="primary")
    with st.expander("Parādīt pilnu anotēšanas atskaiti"):
        st.dataframe(st.session_state.completed_df[REQUIRED_COLUMNS], use_container_width=True, hide_index=True)
