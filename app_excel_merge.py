from __future__ import annotations

from io import BytesIO
from typing import Iterable

import pandas as pd
import streamlit as st
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


EXPECTED_COLUMNS = [
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

TEXT_COLUMNS = [
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

KEY_COLUMNS = ["Audit_ID", "Document_Filename", "Comment"]
MIN_HEADER_MATCH = 10
PREFERRED_SHEET = "Audit"

st.set_page_config(page_title="Audit Excel apvienotājs", page_icon="📊", layout="wide")


@st.cache_data(show_spinner=False)
def read_workbook(file_bytes: bytes, filename: str) -> tuple[pd.DataFrame, dict]:
    """Atrod audita šķirkli un normalizē to uz kanonisko 16 kolonnu shēmu."""
    xls = pd.ExcelFile(BytesIO(file_bytes))

    sheet_scores = []
    parsed_sheets: dict[str, pd.DataFrame] = {}

    for sheet_name in xls.sheet_names:
        try:
            df = pd.read_excel(xls, sheet_name=sheet_name, dtype=object)
        except Exception:
            continue

        df.columns = [str(c).strip() for c in df.columns]
        score = len(set(df.columns) & set(EXPECTED_COLUMNS))
        parsed_sheets[sheet_name] = df
        sheet_scores.append((sheet_name, score))

    if not parsed_sheets:
        raise ValueError("Excel failā neizdevās nolasīt nevienu šķirkli.")

    preferred_matches = [
        name for name in parsed_sheets if name.strip().lower() == PREFERRED_SHEET.lower()
    ]

    if preferred_matches:
        best_sheet = preferred_matches[0]
        best_df = parsed_sheets[best_sheet]
        best_score = len(set(best_df.columns) & set(EXPECTED_COLUMNS))
    else:
        best_sheet, best_score = max(sheet_scores, key=lambda item: item[1])
        best_df = parsed_sheets[best_sheet]

    if best_score < MIN_HEADER_MATCH:
        raise ValueError(
            f"Neizdevās atrast atbilstošu audita datu šķirkli. Labākais kolonnu sakritību skaits: {best_score}/16."
        )

    missing_columns = [c for c in EXPECTED_COLUMNS if c not in best_df.columns]
    extra_columns = [c for c in best_df.columns if c not in EXPECTED_COLUMNS]

    for col in missing_columns:
        best_df[col] = None

    normalized = best_df[EXPECTED_COLUMNS].copy()
    normalized = normalized.dropna(how="all")

    meaningful_mask = normalized[KEY_COLUMNS].apply(
        lambda row: any(pd.notna(v) and str(v).strip() != "" for v in row), axis=1
    )
    normalized = normalized.loc[meaningful_mask].reset_index(drop=True)

    # Saglabā lapu numurus un citus laukus kā tekstu, lai nezaudētu vērtības kā "13; 18" vai "2–4".
    for col in TEXT_COLUMNS:
        normalized[col] = normalized[col].apply(
            lambda value: "" if pd.isna(value) else str(value).strip()
        )

    ignored_sheets = [name for name in xls.sheet_names if name != best_sheet]

    info = {
        "filename": filename,
        "sheet": best_sheet,
        "header_match": best_score,
        "rows": len(normalized),
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "ignored_sheets": ignored_sheets,
        "sheet_scores": sheet_scores,
    }
    return normalized, info


def combine_frames(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    frames = list(frames)
    if not frames:
        return pd.DataFrame(columns=EXPECTED_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    return combined[EXPECTED_COLUMNS]


def duplicate_audit_ids(df: pd.DataFrame) -> pd.DataFrame:
    ids = df["Audit_ID"].astype("string").str.strip()
    valid = ids.notna() & (ids != "")
    duplicated_mask = valid & ids.duplicated(keep=False)
    return df.loc[duplicated_mask].copy()


def build_excel(df: pd.DataFrame) -> bytes:
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Audit", index=False)

    output.seek(0)
    wb = load_workbook(output)
    ws = wb["Audit"]

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
        "Audit_ID": 18,
        "Document_Filename": 52,
        "Document_Number": 30,
        "Page": 14,
        "Location": 40,
        "Category": 24,
        "Element_Code": 20,
        "Comment": 68,
        "Anchor_Text": 48,
        "Alternative_Anchor": 48,
        "Reference_Document_Filename": 52,
        "Reference_Document_Number": 32,
        "Reference_Page": 20,
        "Reference_Location": 40,
        "Reference_Evidence_Text": 68,
        "Annotation_Status": 22,
    }

    for idx, col_name in enumerate(EXPECTED_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(col_name, 24)

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
st.caption("Apvieno vairākus audita Excel failus vienā failā ar Kywatrace kanonisko 16 kolonnu struktūru.")

with st.expander("Kanoniskā 16 kolonnu struktūra", expanded=False):
    st.code(" | ".join(EXPECTED_COLUMNS), language=None)

uploaded_files = st.file_uploader(
    "Augšupielādē vienu vai vairākus Excel failus",
    type=["xlsx", "xlsm"],
    accept_multiple_files=True,
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
                    "Ignorētie šķirkļi": format_list(info["ignored_sheets"]),
                }
            )

        st.subheader("Failu pārbaude")
        st.dataframe(pd.DataFrame(status_rows), use_container_width=True, hide_index=True)

        combined = combine_frames(frames)
        duplicates = duplicate_audit_ids(combined)

        c1, c2, c3 = st.columns(3)
        c1.metric("Apvienotie faili", len(infos))
        c2.metric("Gala rindas", len(combined))
        c3.metric("Dublēti Audit_ID", len(duplicates))

        if not duplicates.empty:
            duplicate_id_count = duplicates["Audit_ID"].nunique()
            st.warning(
                f"Atrasti {duplicate_id_count} dublēti Audit_ID. Rīks tos automātiski nepārraksta, lai nesalauztu sasaistes ar citiem procesiem."
            )
            with st.expander("Parādīt dublētās rindas"):
                st.dataframe(duplicates, use_container_width=True, hide_index=True)
        else:
            st.success("Audit_ID dublikāti nav atrasti.")

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
    st.info("Ieliec Excel failus. Ja failā ir šķirklis 'Audit', rīks izmantos to un ignorēs pārējos šķirkļus.")
