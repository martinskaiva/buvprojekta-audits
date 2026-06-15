import json
from io import BytesIO

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from openai import OpenAI


st.set_page_config(page_title="Būvprojekta PDF pārbaude", layout="wide")

st.title("Būvprojekta PDF teksta pārbaudes prototips")

st.write(
    "Augšupielādē PDF failu. Sistēma izvelk tekstu no PDF un var palaist AI pārbaudi "
    "gramatikas, tulkojumu un tekstuālu neatbilstību meklēšanai."
)


def extract_pdf_text(file_bytes):
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

    return pd.DataFrame(rows), len(doc)


def build_text_for_ai(df, max_blocks=250):
    selected = df.head(max_blocks)

    lines = []
    for index, row in selected.iterrows():
        lines.append(
            f"[ID {index}] [Lapa {row['page']}] {row['text']}"
        )

    return "\n".join(lines)


def check_text_with_ai(df):
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        st.error("Nav atrasta OPENAI_API_KEY vērtība Streamlit Secrets sadaļā.")
        return pd.DataFrame()

    client = OpenAI(api_key=api_key)

    text_for_ai = build_text_for_ai(df)

    prompt = f"""
Tu esi būvprojekta dokumentācijas kvalitātes pārbaudītājs Latvijā.

Pārbaudi zemāk doto PDF izvilkto tekstu no būvprojekta sadaļas.
Meklē:
1. gramatikas kļūdas latviešu valodā;
2. acīmredzamas angļu valodas pareizrakstības/tulkojuma kļūdas;
3. nekorektus vai aizdomīgus formulējumus;
4. acīmredzamas tekstuālas pretrunas;
5. dīvainus datumus, vietturus vai nepabeigtas frāzes.

Svarīgi:
- Neizdomā kļūdas.
- Ja neesi pārliecināts, neliec piezīmi.
- Atgriez tikai reālas un pārbaudāmas piezīmes.
- Pagaidām nepārbaudi būvnormatīvus.
- Pagaidām nepārbaudi rasējuma grafiskos simbolus.
- Atbildi tikai JSON formātā.
- JSON jābūt masīvam ar objektiem.
- Katram objektam jābūt šādiem laukiem:
  - block_id
  - page
  - category
  - source_text
  - comment
  - suggestion
  - confidence

Kategorijas izmanto no šī saraksta:
- grammar
- spelling
- translation
- terminology
- contradiction
- placeholder
- other

Teksts pārbaudei:
{text_for_ai}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        temperature=0,
    )

    raw_output = response.output_text.strip()

    try:
        issues = json.loads(raw_output)
    except json.JSONDecodeError:
        st.error("AI neatgrieza derīgu JSON. Zemāk ir neapstrādāta AI atbilde:")
        st.code(raw_output)
        return pd.DataFrame()

    if not issues:
        return pd.DataFrame()

    issues_df = pd.DataFrame(issues)

    if "block_id" in issues_df.columns:
        issues_df["block_id"] = pd.to_numeric(issues_df["block_id"], errors="coerce")
        issues_df = issues_df.merge(
            df.reset_index().rename(columns={"index": "block_id"}),
            on="block_id",
            how="left",
            suffixes=("", "_pdf"),
        )

    return issues_df


uploaded_file = st.file_uploader("Augšupielādē PDF", type=["pdf"])

if uploaded_file is not None:
    file_bytes = uploaded_file.read()

    df, page_count = extract_pdf_text(file_bytes)

    if not df.empty:
        st.success(f"Izvilkti {len(df)} teksta bloki no {page_count} lapām.")

        st.subheader("Izvilktais PDF teksts")
        st.dataframe(df, use_container_width=True)

        excel_buffer = BytesIO()
        df.to_excel(excel_buffer, index=False, engine="openpyxl")
        excel_buffer.seek(0)

        st.download_button(
            label="Lejupielādēt izvilkto tekstu Excel formātā",
            data=excel_buffer,
            file_name="pdf_teksts.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.divider()

        st.subheader("AI pārbaude")

        st.warning(
            "Pirmajā AI versijā tiek pārbaudīti pirmie 250 teksta bloki. "
            "Tas ir drošības un izmaksu kontroles dēļ."
        )

        if st.button("Pārbaudīt tekstu ar AI"):
            with st.spinner("AI pārbauda tekstu..."):
                issues_df = check_text_with_ai(df)

            if issues_df.empty:
                st.info("AI neatrada drošas piezīmes vai atgrieza tukšu rezultātu.")
            else:
                st.success(f"AI atrada {len(issues_df)} iespējamas piezīmes.")

                st.dataframe(issues_df, use_container_width=True)

                issues_excel_buffer = BytesIO()
                issues_df.to_excel(issues_excel_buffer, index=False, engine="openpyxl")
                issues_excel_buffer.seek(0)

                st.download_button(
                    label="Lejupielādēt AI piezīmes Excel formātā",
                    data=issues_excel_buffer,
                    file_name="ai_piezimes.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

    else:
        st.warning(
            "No PDF neizdevās izvilkt tekstu. Iespējams, tas ir skenēts PDF attēla formātā."
        )
