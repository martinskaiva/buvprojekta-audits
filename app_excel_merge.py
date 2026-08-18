from __future__ import annotations

from io import BytesIO
from typing import Iterable

import pandas as pd
import streamlit as st
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


EXPECTED_COLUMNS = [
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

KEY_COLUMNS = ["note_id", "discipline", "target_file", "comment_text"]
MIN_HEADER_MATCH = 6

st.set_page_config(page_title="Audit Excel apvienotājs", page_icon="📊", layout="wide")


@st.cache_data(show_spinner=False)
def read_workbook(file_bytes: bytes, filename: str) -> tuple[pd.DataFrame, dict]:
    """Atrod visatbilstošāko datu šķirkli un normalizē to uz 16 kolonnu shēmu."""
    xls = pd.ExcelFile(BytesIO(file_bytes))

    best_sheet = None
    best_df = None
    best_score = -1
    sheet_scores = []

    for sheet_name in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sheet_name, dtype=object)
        except Exception:
            continue

        df.columns = [str(c).strip() for c in df.columns]
        score = len(set(df.columns) & set(EXPECTED_COLUMNS))
        sheet_scores.append((sheet_name, score))

        if score > best_score:
            best_score = score
            best_sheet = sheet_name
            best_df = df

    if best_df is None or best_score < MIN_HEADER_MATCH:
        raise ValueError(
            f"Neizdevās atrast audita datu šķirkli. Labākais kolonnu sakritību skaits: {max(best_score, 0)}/16."
        )

    original_columns = list(best_df.columns)
    missing_columns = [c for c in EXPECTED_COLUMNS if c not in best_df.columns]
    extra_columns = [c for c in best_df.columns if c not in EXPECTED_COLUMNS]

    # Trūkstošās kolonnas izveido tukšas; liekās kolonnas gala failā neiekļauj.
    for col in missing_columns:
        best_df[col] = None

    normalized = best_df[EXPECTED_COLUMNS].copy()

    # Noņem pilnībā tukšās rindas un rindas, kurās nav neviena praktiski nozīmīga lauka.
    normalized = normalized.dropna(how="all")
    existing_keys = [c for c in KEY_COLUMNS if c in normalized.columns]
    if existing_keys:
        meaningful_mask = normalized[existing_keys].apply(
            lambda row: any(pd.notna(v) and str(v).strip() != "" for v in row), axis=1
        )
        normalized = normalized.loc[meaningful_mask]

    normalized = normalized.reset_index(drop=True)

    info = {
        "filename": filename,
        "sheet": best_sheet,
        "header_match": best_score,
        "rows": len(normalized),
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "sheet_scores": sheet_scores,
        "original_columns": original_columns,
    }
    return normalized, info


def combine_frames(frames: Iterable[pd.DataFrame], renumber: bool) -> pd.DataFrame:
    frames = list(frames)
    if not frames:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined[EXPECTED_COLUMNS]

    if renumber:
        combined["Nr"] = range(1, len(combined) + 1)

    return combined


def build_excel(df: pd.DataFrame) -> bytes:
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Combined_Audit", index=False)

    output.seek(0)
    wb = load_workbook(output)
    ws = wb["Combined_Audit"]

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    ws.row_dimensions[1].height = 32

    widths = {
        "note_id": 18,
        "Nr": 8,
        "discipline": 14,
        "target_file": 48,
        "target_page": 12,
        "target_area": 34,
        "target_text": 42,
        "comment_text": 60,
        "issue_type": 30,
        "severity": 14,
        "comparison_files": 55,
        "comparison_pages": 35,
        "comparison_evidence": 65,
        "markup_type": 16,
        "placement_confidence": 22,
        "status": 22,
    }

    for idx, col_name in enumerate(EXPECTED_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(col_name, 20)

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    final = BytesIO()
    wb.save(final)
    final.seek(0)
    return final.getvalue()


def format_list(values: list[str]) -> str:
    return ", ".join(values) if values else "—"


st.title("📊 Audit Excel apvienotājs")
st.caption("Apvieno vairākus audita Excel failus vienā failā ar vienotu 16 kolonnu struktūru.")

with st.expander("Gala 16 kolonnu struktūra", expanded=False):
    st.code(" | ".join(EXPECTED_COLUMNS), language=None)

uploaded_files = st.file_uploader(
    "Augšupielādē vienu vai vairākus Excel failus",
    type=["xlsx", "xlsm"],
    accept_multiple_files=True,
)

renumber = st.checkbox(
    "Pārrēķināt Nr. kolonnu secīgi visam apvienotajam failam",
    value=True,
    help="note_id netiek mainīts; tikai Nr. tiek izveidots 1, 2, 3... pēc failu apvienošanas.",
)

if uploaded_files:
    frames = []
    infos = []
    errors = []

    for uploaded in uploaded_files:
        try:
            frame, info = read_workbook(uploaded.getvalue(), uploaded.name)
            frames.append(frame)
            infos.append(info)
        except Exception as exc:
            errors.append({"Fails": uploaded.name, "Kļūda": str(exc)})

    if infos:
        status_rows = []
        for info in infos:
            status_rows.append(
                {
                    "Fails": info["filename"],
                    "Izmantotais šķirklis": info["sheet"],
                    "Kolonnu sakritība": f'{info["header_match"]}/16',
                    "Importētās rindas": info["rows"],
                    "Trūkstošās kolonnas": format_list(info["missing_columns"]),
                    "Ignorētās liekās kolonnas": format_list(info["extra_columns"]),
                }
            )

        st.subheader("Failu pārbaude")
        st.dataframe(pd.DataFrame(status_rows), use_container_width=True, hide_index=True)

        combined = combine_frames(frames, renumber=renumber)

        duplicate_note_ids = 0
        if "note_id" in combined.columns:
            note_ids = combined["note_id"].astype("string").str.strip()
            duplicate_note_ids = int(note_ids[note_ids.notna() & (note_ids != "")].duplicated().sum())

        c1, c2, c3 = st.columns(3)
        c1.metric("Apvienotie faili", len(infos))
        c2.metric("Gala rindas", len(combined))
        c3.metric("Dublēti note_id", duplicate_note_ids)

        if duplicate_note_ids:
            st.warning(
                "Apvienotajā failā ir dublēti note_id. Rīks tos automātiski nepārraksta, jo note_id var būt sasaistīts ar citiem procesiem."
            )

        st.subheader("Priekšskatījums")
        st.dataframe(combined.head(100), use_container_width=True, hide_index=True)

        excel_bytes = build_excel(combined)
        st.download_button(
            "⬇️ Lejupielādēt apvienoto Excel",
            data=excel_bytes,
            file_name="combined_audit_16_columns.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

    if errors:
        st.error("Daļu failu neizdevās importēt.")
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
else:
    st.info("Ieliec Excel failus, un rīks automātiski atradīs audita datu šķirkli katrā failā.")
