import streamlit as st
import fitz  # PyMuPDF
import pandas as pd
from io import BytesIO

st.set_page_config(page_title="Būvprojekta PDF pārbaude", layout="wide")

st.title("Būvprojekta PDF teksta pārbaudes prototips")

st.write(
    "Augšupielādē PDF failu. Šī pirmā versija izvelk tekstu no PDF un parāda to tabulā pa lapām."
)

uploaded_file = st.file_uploader("Augšupielādē PDF", type=["pdf"])

if uploaded_file is not None:
    file_bytes = uploaded_file.read()

    doc = fitz.open(stream=file_bytes, filetype="pdf")

    rows = []

    for page_index, page in enumerate(doc):
        blocks = page.get_text("blocks")

        for block in blocks:
            x0, y0, x1, y1, text, block_no, block_type = block

            clean_text = text.strip()

            if clean_text:
                rows.append(
                    {
                        "page": page_index + 1,
                        "x0": round(x0, 2),
                        "y0": round(y0, 2),
                        "x1": round(x1, 2),
                        "y1": round(y1, 2),
                        "text": clean_text,
                    }
                )

    if rows:
        df = pd.DataFrame(rows)

        st.success(f"Izvilkti {len(df)} teksta bloki no {len(doc)} lapām.")

        st.dataframe(df, use_container_width=True)

        excel_buffer = BytesIO()
        df.to_excel(excel_buffer, index=False, engine="openpyxl")
        excel_buffer.seek(0)

        st.download_button(
            label="Lejupielādēt tekstu Excel formātā",
            data=excel_buffer,
            file_name="pdf_teksts.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        st.warning("No PDF neizdevās izvilkt tekstu. Iespējams, tas ir skenēts PDF attēla formātā.")
