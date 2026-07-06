import io
import json
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

st.set_page_config(page_title="BP audita PDF markup rīks v1.3", layout="wide")

st.title("BP audita PDF markup rīks v1.3")
st.write(
    "Rīks nolasa ChatGPT/Excel audita piezīmes, sasaista tās ar izvēlētiem PDF failiem "
    "un ģenerē anotētus PDF failus. PDF komentāros tiek rādīts tikai īss komentārs un ieteikums. "
    "v1.3: komentāra sadaļā tiek rādīts kļūdas skaidrojums, ieteikumā pilns ieteikums; failiem bez piezīmēm tiek pievienota “izskatīts, piezīmju nav” atzīme; īsiem target_text tiek izvairīts no pārāk plašas apvilkšanas."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

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

CORE_COLUMNS = [
    "note_id",
    "target_file",
    "target_page",
    "target_area",
    "target_text",
    "comment_text",
    "markup_type",
    "placement_confidence",
    "status",
]


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
    all_files: List[Dict[str, Any]] = []
    page_token = None

    while True:
        result = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        all_files.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return all_files


def list_items_recursive(service, folder_id: str, parent_path: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for item in list_folder_items(service, folder_id):
        item_name = item.get("name", "")
        item_path = f"{parent_path}/{item_name}" if parent_path else item_name
        is_folder = item.get("mimeType") == FOLDER_MIME_TYPE

        row = {
            "name": item_name,
            "path": item_path,
            "id": item.get("id"),
            "mimeType": item.get("mimeType"),
            "size": item.get("size", ""),
            "modifiedTime": item.get("modifiedTime", ""),
            "is_folder": is_folder,
        }
        rows.append(row)

        if is_folder:
            rows.extend(list_items_recursive(service, item.get("id"), item_path))

    return rows


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


def export_google_sheet_as_xlsx(service, file_id: str) -> bytes:
    request = service.files().export_media(
        fileId=file_id,
        mimeType=XLSX_MIME_TYPE,
    )
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


def get_discipline_code_from_folder_name(folder_name: str) -> str:
    name = str(folder_name).strip()
    if "_" in name:
        return name.split("_", 1)[1].strip()
    return name


def get_discipline_folders(service, input_folder_id: str) -> pd.DataFrame:
    rows = []
    for item in list_folder_items(service, input_folder_id):
        if item.get("mimeType") != FOLDER_MIME_TYPE:
            continue
        folder_name = item.get("name", "")
        rows.append(
            {
                "folder_name": folder_name,
                "discipline_code": get_discipline_code_from_folder_name(folder_name),
                "folder_id": item.get("id"),
                "modifiedTime": item.get("modifiedTime", ""),
            }
        )
    return pd.DataFrame(rows).sort_values("folder_name") if rows else pd.DataFrame()


def get_pdf_documents_in_discipline(service, discipline_folder_id: str, discipline_folder_name: str) -> pd.DataFrame:
    rows = list_items_recursive(service, discipline_folder_id, discipline_folder_name)
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    pdf_df = df[(df["is_folder"] == False) & (df["mimeType"] == PDF_MIME_TYPE)].copy()
    return pdf_df.sort_values("name") if not pdf_df.empty else pdf_df


def get_audit_example_excels(service, memory_folder_id: str) -> pd.DataFrame:
    rows = list_items_recursive(service, memory_folder_id, "03_Memory")
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    file_df = df[df["is_folder"] == False].copy()

    def is_audit_example(row: pd.Series) -> bool:
        name = str(row.get("name", "")).lower()
        path = str(row.get("path", "")).lower()
        mime = str(row.get("mimeType", ""))
        is_excel = name.endswith(".xlsx") or mime == GOOGLE_SHEET_MIME_TYPE
        return is_excel and "audit_examples" in path and "accepted_audit_examples" in name

    ex_df = file_df[file_df.apply(is_audit_example, axis=1)].copy()
    return ex_df.sort_values("path") if not ex_df.empty else ex_df


def normalize_filename(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def safe_int(value: Any) -> Optional[int]:
    try:
        if pd.isna(value):
            return None
        return int(float(str(value).strip()))
    except Exception:
        return None


def read_notes_from_excel_bytes(data: bytes, source_excel_name: str) -> pd.DataFrame:
    xls = pd.ExcelFile(io.BytesIO(data))
    sheet_name = xls.sheet_names[0]
    df = pd.read_excel(io.BytesIO(data), sheet_name=sheet_name)
    df = df.dropna(how="all").copy()
    df.columns = [str(c).strip() for c in df.columns]

    if df.empty:
        return df

    for col in REQUIRED_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df["source_excel"] = source_excel_name
    df["target_page"] = df["target_page"].apply(safe_int)
    df["target_file_norm"] = df["target_file"].apply(normalize_filename)
    df["status"] = df["status"].fillna("").astype(str)
    df["markup_type"] = df["markup_type"].fillna("").astype(str).str.lower().str.strip()
    df["placement_confidence"] = df["placement_confidence"].fillna("").astype(str).str.lower().str.strip()
    df["target_text"] = df["target_text"].fillna("").astype(str)
    df["comment_text"] = df["comment_text"].fillna("").astype(str)

    return df


def load_selected_notes(service, excel_rows: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for _, row in excel_rows.iterrows():
        file_id = row["id"]
        name = row["name"]
        mime = row.get("mimeType", "")

        if mime == GOOGLE_SHEET_MIME_TYPE:
            data = export_google_sheet_as_xlsx(service, file_id)
        else:
            data = download_drive_file_bytes(service, file_id)

        df = read_notes_from_excel_bytes(data, name)
        if not df.empty:
            frames.append(df)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined["row_index"] = range(1, len(combined) + 1)
    return combined


def validate_notes(notes_df: pd.DataFrame, selected_pdf_df: pd.DataFrame) -> pd.DataFrame:
    if notes_df.empty:
        return notes_df

    pdf_names = set(selected_pdf_df["name"].apply(normalize_filename).tolist())
    work = notes_df.copy()

    work["file_selected"] = work["target_file_norm"].isin(pdf_names)
    work["has_page"] = work["target_page"].notna()
    work["has_comment"] = work["comment_text"].astype(str).str.strip().ne("")
    work["has_target_text"] = work["target_text"].astype(str).str.strip().ne("")

    work["validation_status"] = "ok"
    work.loc[~work["file_selected"], "validation_status"] = "target_file_not_selected"
    work.loc[~work["has_page"], "validation_status"] = "missing_target_page"
    work.loc[~work["has_comment"], "validation_status"] = "missing_comment_text"
    work.loc[~work["has_target_text"], "validation_status"] = "missing_target_text"

    return work


def clean_evidence_text(text: str) -> str:
    """Return a readable issue explanation for the PDF comment body."""
    value = re.sub(r"\s+", " ", str(text or "").strip())

    # Many generated rows start with source references, e.g.
    # "MS L52-L56 — Pozīcijā...". For the visible PDF note, keep the explanation.
    for separator in [" — ", " - "]:
        if separator in value:
            left, right = value.split(separator, 1)
            if len(right.strip()) >= 20:
                value = right.strip()
                break

    return value


def make_short_comment(row: pd.Series) -> str:
    """
    Build the visible PDF note.

    Principle for v1.2:
    - Komentārs = issue explanation, preferably comparison_evidence.
    - Ieteikums = full recommendation, preferably comment_text.

    This avoids putting the full recommendation under both sections.
    """
    comment = str(row.get("comment_short") or "").strip()
    suggestion = str(row.get("suggestion_short") or "").strip()

    if not comment:
        comment = clean_evidence_text(row.get("comparison_evidence") or "")

    if not comment:
        issue_type = str(row.get("issue_type") or "").strip()
        target_area = str(row.get("target_area") or "").strip()
        comment = f"Konstatēta neatbilstība: {issue_type}. {target_area}".strip()

    if not suggestion:
        suggestion = str(row.get("comment_text") or "").strip()

    if not suggestion:
        suggestion = "Lūdzu pārbaudīt un precizēt atbilstoši piezīmei."

    return f"Komentārs:\n{comment}\n\nIeteikums:\n{suggestion}"


def is_2_of_2_stage_note(row: pd.Series) -> bool:
    """
    Exclude notes that interpret “2 / 2” as page count.
    User clarified this is the project construction stage, not missing page count.
    """
    haystack = " ".join([
        str(row.get("target_text") or ""),
        str(row.get("target_area") or ""),
        str(row.get("comment_text") or ""),
        str(row.get("comparison_evidence") or ""),
        str(row.get("issue_type") or ""),
    ]).lower()

    has_2_2 = bool(re.search(r"\b2\s*/\s*2\b", haystack))
    talks_about_pages = any(word in haystack for word in [
        "lapu numer",
        "lapu skaits",
        "pdf satur vienu lapu",
        "netrūkst 1. lapas",
        "page count",
        "page_note",
    ])
    return has_2_2 and talks_about_pages


def is_very_short_search_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if len(value) <= 2:
        return True
    return bool(re.fullmatch(r"\d+(?:[.,]\d+)?", value))


def choose_rects_for_short_target(rects: List[fitz.Rect]) -> List[fitz.Rect]:
    """For very short targets like row number '7', do not union all matches on page."""
    if not rects:
        return []
    # Choose the upper-left match. This is not perfect, but prevents one tiny target
    # from creating a huge rectangle around many unrelated matches.
    ordered = sorted(rects, key=lambda r: (round(r.y0, 1), round(r.x0, 1), r.get_area()))
    return [ordered[0]]


def build_search_variants(target_text: str) -> List[str]:
    text = str(target_text or "").strip()
    if not text or text.upper() == "MANUAL_PLACEMENT_REQUIRED":
        return []

    variants = []

    def add(v: str):
        v = re.sub(r"\s+", " ", str(v or "").strip())
        if v and v not in variants:
            variants.append(v)

    add(text)

    # If text contains several clauses, try shorter searchable anchors.
    for sep in [";", "|", "\n"]:
        if sep in text:
            for part in text.split(sep):
                if len(part.strip()) >= 6:
                    add(part)

    # Very long comments are often not exact PDF text. Try first meaningful 80-120 chars.
    if len(text) > 120:
        add(text[:120])
        add(text[:80])

    # Common PDF extraction variants.
    add(text.replace("–", "-").replace("—", "-"))
    add(text.replace("“", '"').replace("”", '"'))

    return variants


def add_page_note(page: fitz.Page, content: str, index_on_page: int) -> None:
    rect = page.rect
    x = max(36, rect.width - 120)
    y = 36 + 22 * index_on_page
    point = fitz.Point(x, y)
    annot = page.add_text_annot(point, content)
    annot.set_info(title="AI būvprojekta audits", subject="AI piezīme")
    annot.update()


def add_reviewed_without_comments_mark(doc: fitz.Document) -> None:
    """Add a visible mark and a PDF comment for a reviewed document without issues."""
    if len(doc) == 0:
        return

    page = doc[0]
    rect = page.rect

    box_width = min(230, max(150, rect.width * 0.28))
    box_height = 54
    margin = 28
    x0 = max(margin, rect.width - box_width - margin)
    y0 = margin
    box = fitz.Rect(x0, y0, x0 + box_width, y0 + box_height)

    text = "AI būvprojekta audits\nDokuments izskatīts.\nPiezīmes nav konstatētas."

    # Draw a light, non-aggressive stamp directly on the page.
    try:
        page.draw_rect(box, color=(0, 0.45, 0), fill=(0.92, 1, 0.92), width=0.8)
        page.insert_textbox(
            box + (6, 5, -6, -5),
            text,
            fontsize=8.5,
            fontname="helv",
            color=(0, 0.25, 0),
            align=0,
        )
    except Exception:
        # If direct drawing fails for a particular PDF, still add the annotation.
        pass

    content = "Komentārs:\nDokuments izskatīts. Piezīmes nav konstatētas.\n\nIeteikums:\nNav."
    note = page.add_text_annot(fitz.Point(x0, y0 + box_height + 8), content)
    note.set_info(title="AI būvprojekta audits", subject="Dokuments izskatīts bez piezīmēm")
    note.update()


def annotate_pdf_without_comments(pdf_bytes: bytes, output_name: str, target_file: str) -> Tuple[bytes, pd.DataFrame]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    add_reviewed_without_comments_mark(doc)

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    output.seek(0)

    report_df = pd.DataFrame([
        {
            "note_id": "REVIEWED_NO_COMMENTS",
            "target_file": target_file,
            "output_file": output_name,
            "target_page": 1,
            "target_text": "",
            "markup_type": "reviewed_no_comments",
            "placement_confidence": "not_applicable",
            "result": "reviewed_no_comments",
            "reason": "selected_pdf_has_no_notes",
            "matches": 0,
            "rectangles_drawn": 0,
            "visual_strategy": "reviewed_stamp",
        }
    ])

    return output.read(), report_df


def padded_rect(rect: fitz.Rect, padding: float = 1.5) -> fitz.Rect:
    return fitz.Rect(rect.x0 - padding, rect.y0 - padding, rect.x1 + padding, rect.y1 + padding)


def union_rects(rects: List[fitz.Rect], padding: float = 1.5) -> fitz.Rect:
    if not rects:
        raise ValueError("union_rects called with empty rect list")

    r = fitz.Rect(rects[0])
    for rect in rects[1:]:
        r.include_rect(rect)

    return padded_rect(r, padding=padding)


def add_red_rect(page: fitz.Page, rect: fitz.Rect, content: str = "") -> None:
    rect_annot = page.add_rect_annot(rect)
    rect_annot.set_colors(stroke=(1, 0, 0))
    rect_annot.set_border(width=1.0)
    rect_annot.set_info(title="AI būvprojekta audits", subject="AI piezīme", content=content)
    rect_annot.update()


def add_sticky_note_near_rect(page: fitz.Page, rect: fitz.Rect, content: str) -> None:
    note_x = min(page.rect.width - 36, rect.x1 + 6)
    note_y = max(24, rect.y0)
    note = page.add_text_annot(fitz.Point(note_x, note_y), content)
    note.set_info(title="AI būvprojekta audits", subject="AI piezīme")
    note.update()


def add_highlight_markup(
    page: fitz.Page,
    rects: List[fitz.Rect],
    content: str,
    visual_strategy: str,
) -> Tuple[str, int]:
    """
    Adds visual markup for one note.

    visual_strategy:
    - union: one combined red rectangle around all matched text fragments + one sticky note.
    - multiple_rects_one_note: red rectangles around all matched fragments + one sticky note.

    Returns: (strategy_result, rectangles_drawn)
    """
    if not rects:
        return "no_rects", 0

    limited_rects = rects[:12]

    if visual_strategy == "multiple_rects_one_note":
        padded = [padded_rect(r) for r in limited_rects]
        for rect in padded:
            add_red_rect(page, rect, content="")

        note_anchor = union_rects(padded, padding=0)
        add_sticky_note_near_rect(page, note_anchor, content)
        return "multiple_rects_one_note", len(padded)

    # Default: one combined visual zone and one note.
    combined = union_rects(limited_rects)
    add_red_rect(page, combined, content=content)
    add_sticky_note_near_rect(page, combined, content)
    return "union_rect_one_note", 1


def annotate_pdf(pdf_bytes: bytes, notes_df: pd.DataFrame, output_name: str, visual_strategy: str) -> Tuple[bytes, pd.DataFrame]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    report_rows: List[Dict[str, Any]] = []
    page_note_counts: Dict[int, int] = {}

    for _, note in notes_df.iterrows():
        note_id = str(note.get("note_id") or note.get("Nr") or note.get("row_index") or "").strip()
        page_no = safe_int(note.get("target_page"))
        target_text = str(note.get("target_text") or "").strip()
        markup_type = str(note.get("markup_type") or "").lower().strip()
        content = make_short_comment(note)

        base_report = {
            "note_id": note_id,
            "target_file": note.get("target_file"),
            "output_file": output_name,
            "target_page": page_no,
            "target_text": target_text,
            "markup_type": markup_type,
            "placement_confidence": note.get("placement_confidence"),
        }

        if note.get("skip_reason"):
            report_rows.append({**base_report, "result": "skipped", "reason": note.get("skip_reason")})
            continue

        if page_no is None:
            report_rows.append({**base_report, "result": "skipped", "reason": "missing_target_page"})
            continue

        if page_no < 1 or page_no > len(doc):
            report_rows.append({**base_report, "result": "skipped", "reason": f"page_not_found_pdf_has_{len(doc)}_pages"})
            continue

        page = doc[page_no - 1]
        page_note_counts.setdefault(page_no, 0)

        should_page_note = (
            markup_type == "page_note"
            or target_text.upper() == "MANUAL_PLACEMENT_REQUIRED"
            or not target_text
        )

        if should_page_note:
            add_page_note(page, content, page_note_counts[page_no])
            page_note_counts[page_no] += 1
            report_rows.append({**base_report, "result": "page_note_added", "reason": "manual_or_page_note"})
            continue

        variants = build_search_variants(target_text)
        found_rects: List[fitz.Rect] = []
        found_variant = ""

        for variant in variants:
            rects = page.search_for(variant)
            if rects:
                found_rects = rects
                found_variant = variant
                break

        if found_rects:
            if is_very_short_search_text(found_variant):
                found_rects = choose_rects_for_short_target(found_rects)

            strategy_result, rectangles_drawn = add_highlight_markup(
                page=page,
                rects=found_rects,
                content=content,
                visual_strategy=visual_strategy,
            )
            report_rows.append(
                {
                    **base_report,
                    "result": "highlight_added",
                    "reason": f"found_text_variant: {found_variant}",
                    "matches": len(found_rects),
                    "rectangles_drawn": rectangles_drawn,
                    "visual_strategy": strategy_result,
                }
            )
        else:
            add_page_note(page, content, page_note_counts[page_no])
            page_note_counts[page_no] += 1
            report_rows.append(
                {
                    **base_report,
                    "result": "text_not_found_page_note_added",
                    "reason": "target_text_not_found_on_page",
                    "matches": 0,
                }
            )

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    output.seek(0)

    return output.read(), pd.DataFrame(report_rows)


def make_safe_output_name(name: str) -> str:
    base = str(name).rsplit(".", 1)[0]
    return f"{base}_annotated.pdf"


def make_excel_report_bytes(report_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        report_df.to_excel(writer, sheet_name="markup_report", index=False)
    output.seek(0)
    return output.getvalue()


def make_zip_bytes(pdf_outputs: Dict[str, bytes], report_df: pd.DataFrame) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for filename, data in pdf_outputs.items():
            zf.writestr(filename, data)
        zf.writestr("markup_report.xlsx", make_excel_report_bytes(report_df))
    output.seek(0)
    return output.getvalue()


# Session state
for key, default in {
    "disciplines_df": pd.DataFrame(),
    "pdfs_df": pd.DataFrame(),
    "excel_df": pd.DataFrame(),
    "selected_notes_df": pd.DataFrame(),
    "validated_notes_df": pd.DataFrame(),
    "markup_report_df": pd.DataFrame(),
    "zip_bytes": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# Configuration
input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
memory_folder_id = st.secrets.get("GOOGLE_DRIVE_MEMORY_FOLDER_ID")

st.markdown("## 1. Konfigurācija")
st.write("Input folder ID:", input_folder_id)
st.write("Memory folder ID:", memory_folder_id)
st.info(
    "v1.3: rīks ģenerē anotētos PDF un ZIP lejupielādei. Rezultāti paredzēti 02_Results mapei. Drive augšupielāde šajā versijā nav ieslēgta. "
    "PDF komentārā tiek rādīts tikai: Komentārs + Ieteikums. Komentārs = kļūdas skaidrojums, Ieteikums = pilns ieteikuma teksts."
)

visual_strategy_label = st.radio(
    "Highlight noformējums",
    options=[
        "Viena kopēja sarkanā zona + viena piezīme",
        "Vairāki sarkanie rāmji + viena piezīme",
    ],
    index=0,
    horizontal=True,
)

visual_strategy = (
    "multiple_rects_one_note"
    if visual_strategy_label.startswith("Vairāki")
    else "union"
)

mark_reviewed_without_comments = st.checkbox(
    "Atzīmēt izvēlētos PDF failus bez piezīmēm kā “izskatīts, piezīmju nav”",
    value=True,
)

if st.button("1) Nolasīt PDF un audit examples Excel failus"):
    try:
        service = get_drive_service()
        st.session_state.disciplines_df = get_discipline_folders(service, input_folder_id)
        st.session_state.excel_df = get_audit_example_excels(service, memory_folder_id)
        st.success(
            f"Nolasītas disciplīnas: {len(st.session_state.disciplines_df)}; "
            f"audit examples Excel faili: {len(st.session_state.excel_df)}."
        )
    except Exception as e:
        st.error("Neizdevās nolasīt Drive datus.")
        st.exception(e)

if not st.session_state.excel_df.empty:
    with st.expander("Atrasti audit_examples Excel faili"):
        st.dataframe(st.session_state.excel_df[["name", "path", "mimeType", "modifiedTime"]], use_container_width=True)


# Discipline and PDFs
if not st.session_state.disciplines_df.empty:
    st.markdown("## 2. Izvēlies disciplīnu")
    st.dataframe(st.session_state.disciplines_df, use_container_width=True)

    folder_options = st.session_state.disciplines_df["folder_name"].tolist()
    default_index = folder_options.index("09_UKT") if "09_UKT" in folder_options else 0
    selected_folder_name = st.selectbox("Disciplīnas mape", folder_options, index=default_index)
    selected_row = st.session_state.disciplines_df[
        st.session_state.disciplines_df["folder_name"] == selected_folder_name
    ].iloc[0]

    selected_discipline_code = selected_row["discipline_code"]
    selected_folder_id = selected_row["folder_id"]
    st.write("Izvēlētā disciplīna:", selected_discipline_code)

    if st.button("2) Atrast disciplīnas PDF failus"):
        try:
            service = get_drive_service()
            pdfs_df = get_pdf_documents_in_discipline(service, selected_folder_id, selected_folder_name)
            st.session_state.pdfs_df = pdfs_df
            if pdfs_df.empty:
                st.warning("Disciplīnā nav atrasti PDF faili.")
            else:
                st.success(f"Atrasti {len(pdfs_df)} PDF faili.")
        except Exception as e:
            st.error("Neizdevās atrast PDF failus.")
            st.exception(e)


if not st.session_state.pdfs_df.empty:
    st.markdown("## 3. Izvēlies PDF failus")
    st.dataframe(st.session_state.pdfs_df[["name", "path", "size", "modifiedTime"]], use_container_width=True)

    pdf_options = st.session_state.pdfs_df["path"].tolist()
    default_pdf_paths = pdf_options[:]
    selected_pdf_paths = st.multiselect("PDF faili anotēšanai", pdf_options, default=default_pdf_paths)
    selected_pdf_df = st.session_state.pdfs_df[st.session_state.pdfs_df["path"].isin(selected_pdf_paths)].copy()

    st.markdown("### Izvēlētie PDF")
    st.dataframe(selected_pdf_df[["name", "path", "id"]], use_container_width=True)

    st.markdown("## 4. Izvēlies piezīmju Excel failus")

    excel_df = st.session_state.excel_df.copy()
    if not excel_df.empty:
        # Preselect likely files for the selected discipline.
        disc_lower = selected_discipline_code.lower()
        likely = excel_df[excel_df["name"].str.lower().str.contains(f"_{disc_lower}_", regex=False)].copy()
        default_excel_paths = likely["path"].tolist() if not likely.empty else []

        excel_options = excel_df["path"].tolist()
        selected_excel_paths = st.multiselect(
            "Audit examples Excel faili",
            excel_options,
            default=default_excel_paths,
        )
        selected_excel_df = excel_df[excel_df["path"].isin(selected_excel_paths)].copy()
        st.dataframe(selected_excel_df[["name", "path", "id"]], use_container_width=True)

        if st.button("3) Ielasīt un pārbaudīt piezīmes"):
            try:
                service = get_drive_service()
                notes_df = load_selected_notes(service, selected_excel_df)
                validated = validate_notes(notes_df, selected_pdf_df)
                st.session_state.selected_notes_df = notes_df
                st.session_state.validated_notes_df = validated

                st.success(f"Ielasītas {len(validated)} piezīmes no {len(selected_excel_df)} Excel failiem.")
            except Exception as e:
                st.error("Neizdevās ielasīt piezīmju Excel failus.")
                st.exception(e)
    else:
        st.warning("Nav atrasti audit_examples Excel faili.")


# Validation preview
validated_notes_df = st.session_state.validated_notes_df
if not validated_notes_df.empty:
    st.markdown("## 5. Piezīmju pārbaude pirms anotēšanas")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Piezīmes kopā", len(validated_notes_df))
    c2.metric("Highlight", int((validated_notes_df["markup_type"] == "highlight").sum()))
    c3.metric("Page note", int((validated_notes_df["markup_type"] == "page_note").sum()))
    c4.metric("OK validācija", int((validated_notes_df["validation_status"] == "ok").sum()))

    if "placement_confidence" in validated_notes_df.columns:
        st.markdown("### Placement confidence")
        st.dataframe(
            validated_notes_df.groupby("placement_confidence").size().reset_index(name="count"),
            use_container_width=True,
        )

    st.markdown("### Piezīmju priekšskatījums")
    preview_cols = [c for c in CORE_COLUMNS + ["source_excel", "validation_status"] if c in validated_notes_df.columns]
    st.dataframe(validated_notes_df[preview_cols], use_container_width=True)

    invalid = validated_notes_df[validated_notes_df["validation_status"] != "ok"].copy()
    if not invalid.empty:
        st.warning("Daļai piezīmju ir validācijas problēmas. Tās anotēšanas laikā var tikt izlaistas.")
        with st.expander("Validācijas problēmas"):
            st.dataframe(invalid[preview_cols], use_container_width=True)

    if st.button("4) Ģenerēt anotētos PDF"):
        try:
            service = get_drive_service()
            pdf_outputs: Dict[str, bytes] = {}
            report_frames = []

            ok_notes = validated_notes_df[validated_notes_df["validation_status"] == "ok"].copy()
            if not ok_notes.empty:
                ok_notes["skip_reason"] = ""

            selected_pdf_map = {
                normalize_filename(row["name"]): row
                for _, row in selected_pdf_df.iterrows()
            }

            grouped_notes: Dict[str, pd.DataFrame] = {}
            if not ok_notes.empty:
                grouped_notes = {target_norm: group.copy() for target_norm, group in ok_notes.groupby("target_file_norm")}

            if selected_pdf_df.empty:
                st.warning("Nav izvēlēti PDF faili.")
            elif ok_notes.empty and not mark_reviewed_without_comments:
                st.warning("Nav derīgu piezīmju anotēšanai, un failu bez piezīmēm atzīmēšana ir izslēgta.")
            else:
                progress = st.progress(0)
                total_pdfs = len(selected_pdf_df)

                # Report any valid note that points to a file outside the selected PDF set.
                for target_norm, group in grouped_notes.items():
                    if target_norm not in selected_pdf_map:
                        missing_report = group.copy()
                        missing_report["result"] = "skipped"
                        missing_report["reason"] = "target_file_not_selected"
                        report_frames.append(missing_report)

                for idx, (_, pdf_row) in enumerate(selected_pdf_df.iterrows(), start=1):
                    target_norm = normalize_filename(pdf_row["name"])
                    group = grouped_notes.get(target_norm, pd.DataFrame())

                    if group.empty and not mark_reviewed_without_comments:
                        progress.progress(idx / max(total_pdfs, 1))
                        continue

                    pdf_bytes = download_drive_file_bytes(service, pdf_row["id"])
                    output_name = make_safe_output_name(pdf_row["name"])

                    if group.empty:
                        annotated_bytes, report_df = annotate_pdf_without_comments(pdf_bytes, output_name, pdf_row["name"])
                    else:
                        annotated_bytes, report_df = annotate_pdf(pdf_bytes, group, output_name, visual_strategy)

                    pdf_outputs[output_name] = annotated_bytes
                    report_frames.append(report_df)
                    progress.progress(idx / max(total_pdfs, 1))

                final_report = pd.concat(report_frames, ignore_index=True) if report_frames else pd.DataFrame()
                st.session_state.markup_report_df = final_report
                st.session_state.zip_bytes = make_zip_bytes(pdf_outputs, final_report)

                st.success(f"Ģenerēti {len(pdf_outputs)} anotēti PDF faili ZIP lejupielādei. Rezultātus pēc pārbaudes ievieto 02_Results mapē.")
        except Exception as e:
            st.error("Neizdevās ģenerēt anotētos PDF.")
            st.exception(e)


# Results
if st.session_state.zip_bytes is not None:
    st.markdown("## 6. Rezultāti")

    report_df = st.session_state.markup_report_df
    if not report_df.empty:
        st.markdown("### Markup report")
        st.dataframe(report_df, use_container_width=True)

        if "result" in report_df.columns:
            st.markdown("### Rezultātu kopsavilkums")
            st.dataframe(report_df.groupby("result").size().reset_index(name="count"), use_container_width=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    st.download_button(
        "Lejupielādēt anotētos PDF ZIP",
        data=st.session_state.zip_bytes,
        file_name=f"bp_audit_markup_{timestamp}.zip",
        mime="application/zip",
    )
