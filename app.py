import json
from io import BytesIO

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from openai import OpenAI


st.set_page_config(page_title="Būvprojekta PDF pārbaude", layout="wide")

st.title("Būvprojekta PDF teksta pārbaudes prototips")

st.write(
    "Augšupielādē PDF failu. Sistēma izvelk tekstu no PDF, palaiž AI pārbaudi "
    "un var ģenerēt PDF ar komentāriem konkrētās vietās."
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
        lines.append(f"[ID {index}] [Lapa {row['page']}] {row['text']}")

    return "\n".join(lines)


def clean_ai_json_output(raw_output):
    raw_output = raw_output.strip()

    if raw_output.startswith("```json"):
        raw_output = raw_output.replace("```json", "", 1).strip()

    if raw_output.startswith("```"):
        raw_output = raw_output.replace("```", "", 1).strip()

    if raw_output.endswith("```"):
        raw_output = raw_output[:-3].strip()

    return raw_output


def check_text_with_ai(df):
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        st.error("Nav atrasta OPENAI_API_KEY vērtība Streamlit Secrets sadaļā.")
        return pd.DataFrame()

    client = OpenAI(api_key=api_key)

    text_for_ai = build_text_for_ai(df)

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas kvalitātes pārbaudītājs Latvijā.

GALVENAIS PRINCIPS:
Atzīmē tikai drošas, acīmredzamas un praktiski labojamas kļūdas.
Ja ir kaut nelielas šaubas, piezīmi neliec.
Labāk neatgriezt piezīmi nekā atgriezt viltus pozitīvu piezīmi.

Pārbaudi zemāk doto PDF izvilkto tekstu no būvprojekta sadaļas.

DRĪKST atzīmēt tikai šādus gadījumus:
1. Acīmredzamas latviešu valodas pareizrakstības kļūdas.
2. Acīmredzamas latviešu valodas gramatikas kļūdas.
3. Acīmredzamas angļu valodas pareizrakstības kļūdas.
4. Acīmredzami nepareizus latviešu/angļu tulkojumu pārus, ja tulkojums maina nozīmi vai ir skaidri kļūdains.
5. Neaizpildītus vietturus, piemēram, dd.mm.gggg, Nr.X, XXX, TODO, [ievietot], ____, ???.
6. Acīmredzami bojātus datumus vai tehniskus pierakstus, piemēram, 00.00.0000, XX.XX.XXXX, dd.mm.gggg.
7. Vienā dokumentā skaidri pretrunīgus skaitļus, nosaukumus vai marķējumus, ja pretruna redzama dotajā tekstā.

TULKOJUMU PĀRBAUDE:
Pārbaudi latviešu/angļu tekstus tikai tad, ja blakus vai vienā blokā redzams skaidrs LV/EN pāris.
Atzīmē tikai nepārprotamas tulkojuma kļūdas:
- ja angļu teksts nozīmē kaut ko citu nekā latviešu teksts;
- ja angļu tekstā ir acīmredzama drukas kļūda;
- ja LV/EN titullauka pāris ir acīmredzami sajaukts.

NEATZĪMĒ pieņemamus būvprojekta tulkojumu variantus, piemēram:
- VISPĀRĪGIE RĀDĪTĀJI / GENERAL DATA;
- RASĒJUMA NR. / SHEET ID;
- MĒROGS / SCALE;
- DATUMS / DATE;
- IZMAIŅA / REVISION;
- STĀVS / FLOOR;
- NOSAUKUMS / TITLE vai NAME, ja kontekstā tas ir saprotams.

DATUMU NOTEIKUMS:
Neatzīmē datumus formātā dd.mm.yyyy vai dd.mm.yyyy. kā kļūdu.
Piemēri, kurus nedrīkst atzīmēt kā kļūdu:
- 23.03.2026
- 23.03.2026.
- 01.12.2025
Nākotnes datums pats par sevi nav kļūda būvprojekta dokumentācijā.
Datumu atzīmē tikai tad, ja tas ir acīmredzams vietturis vai bojāts pieraksts, piemēram:
- dd.mm.gggg
- XX.XX.XXXX
- 00.00.0000

NEDRĪKST atzīmēt:
- stilistiskus uzlabojumus;
- gaumes jautājumus;
- pieņemamus sinonīmus;
- virsrakstus;
- attēlu parakstus;
- tabulu šūnas;
- sarakstu punktus;
- atsauces uz pielikumiem;
- frāzes, kas izskatās nepilnīgas tikai tāpēc, ka PDF teksts ir sadalīts blokos;
- tehniskus terminus, ja tie var būt pieņemami projektēšanas dokumentācijā;
- vietvārdus, īpašvārdus, uzņēmumu nosaukumus vai projekta specifiskus nosaukumus;
- vārdu locījumus, ja tie var būt gramatiski pareizi konkrētajā teikumā;
- vārdus, kur piedāvātais labojums būtiski neatšķiras no esošā teksta;
- pareizus savienojumus, piemēram, “zaļo toņu gammā”;
- nākotnes datumus, ja tie ir normālā Latvijas datuma formātā;
- rasējumu titullauku vērtības, ja tās nav acīmredzami bojātas.

NEPĀRBAUDI:
- būvnormatīvu atbilstību;
- rasējuma grafiskos simbolus;
- attēlu saturu;
- tehniskā risinājuma pareizību;
- vai datums atbilst projekta grafikam;
- vai revīzijas numurs ir pareizs.

Svarīgi:
- Neizdomā kļūdas.
- Ja neesi pārliecināts, neliec piezīmi.
- Ja kļūda ir tikai stila jautājums, neliec piezīmi.
- Ja kļūda balstās tikai uz to, ka viens PDF teksta bloks izskatās nepabeigts, neliec piezīmi.
- Atgriez tikai piezīmes, kuras cilvēkam tiešām būtu vērts pārbaudīt.
- Labāk atgriezt 0 piezīmes nekā 1 nepamatotu piezīmi.
- Atbildi tikai JSON formātā.
- JSON jābūt masīvam ar objektiem.
- Ja nav drošu piezīmju, atgriez tukšu masīvu [].
- Neizmanto Markdown.
- Neievieto atbildi ```json blokā.

Katram objektam jābūt šādiem laukiem:
- block_id
- page
- category
- severity
- source_text
- comment
- suggestion
- confidence

Kategorijas izmanto no šī saraksta:
- grammar
- spelling
- translation
- contradiction
- placeholder
- other

Severity izmanto:
- low
- medium
- high

Confidence norādi kā skaitli no 0 līdz 1.
Atgriez tikai piezīmes ar confidence 0.93 vai augstāku.

Teksts pārbaudei:
{text_for_ai}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        temperature=0,
    )

    raw_output = response.output_text.strip()
    cleaned_output = clean_ai_json_output(raw_output)

    try:
        issues = json.loads(cleaned_output)
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

    if not issues_df.empty:
        issues_df.insert(0, "include_in_pdf", True)

    return issues_df


def create_annotated_pdf(file_bytes, issues_df):
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    for _, issue in issues_df.iterrows():
        try:
            page_number = int(issue.get("page_pdf", issue.get("page", 0)))
            x0 = float(issue.get("x0", 50))
            y0 = float(issue.get("y0", 50))
            x1 = float(issue.get("x1", x0 + 100))
            y1 = float(issue.get("y1", y0 + 20))
        except (TypeError, ValueError):
            continue

        if page_number < 1 or page_number > len(doc):
            continue

        page = doc[page_number - 1]

        category = str(issue.get("category", "other"))
        severity = str(issue.get("severity", ""))
        source_text = str(issue.get("source_text", ""))
        comment = str(issue.get("comment", ""))
        suggestion = str(issue.get("suggestion", ""))
        confidence = issue.get("confidence", "")

        annotation_text = (
            f"AI piezīme\n"
            f"Kategorija: {category}\n"
            f"Nopietnība: {severity}\n"
            f"Ticamība: {confidence}\n\n"
            f"Atrastais teksts:\n{source_text}\n\n"
            f"Komentārs:\n{comment}\n\n"
            f"Ieteikums:\n{suggestion}"
        )

        rect = fitz.Rect(x0, y0, x1, y1)

        square_annot = page.add_rect_annot(rect)
        square_annot.set_info(
            title="AI būvprojekta pārbaude",
            content=annotation_text,
        )
        square_annot.set_colors(stroke=(1, 0, 0))
        square_annot.set_border(width=1)
        square_annot.update()

        note_x = max(x1 + 5, x0 + 5)
        note_y = y0
        note_point = fitz.Point(note_x, note_y)

        text_annot = page.add_text_annot(note_point, annotation_text)
        text_annot.set_info(
            title="AI būvprojekta pārbaude",
            content=annotation_text,
        )
        text_annot.update()

    output = BytesIO()
    doc.save(output)
    output.seek(0)
    doc.close()

    return output


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

            st.session_state["issues_df"] = issues_df
            st.session_state["file_bytes"] = file_bytes

        issues_df = st.session_state.get("issues_df")

        if issues_df is not None:
            if issues_df.empty:
                st.info("AI neatrada drošas piezīmes vai atgrieza tukšu rezultātu.")
            else:
                st.success(f"AI atrada {len(issues_df)} iespējamas piezīmes.")

                st.write(
                    "Pārbaudi AI piezīmes. Ja kāda piezīme nav pamatota, noņem ķeksi kolonnā "
                    "**include_in_pdf**. PDF tiks ģenerēts tikai ar atzīmētajām piezīmēm."
                )

                edited_issues_df = st.data_editor(
                    issues_df,
                    use_container_width=True,
                    num_rows="fixed",
                    key="issues_editor",
                )

                issues_excel_buffer = BytesIO()
                edited_issues_df.to_excel(
                    issues_excel_buffer, index=False, engine="openpyxl"
                )
                issues_excel_buffer.seek(0)

                st.download_button(
                    label="Lejupielādēt AI piezīmes Excel formātā",
                    data=issues_excel_buffer,
                    file_name="ai_piezimes.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

                approved_issues_df = edited_issues_df[
                    edited_issues_df["include_in_pdf"] == True
                ].copy()

                st.info(
                    f"PDF anotācijām atlasītas {len(approved_issues_df)} no "
                    f"{len(edited_issues_df)} piezīmēm."
                )

                if not approved_issues_df.empty:
                    annotated_pdf = create_annotated_pdf(file_bytes, approved_issues_df)

                    st.download_button(
                        label="Lejupielādēt PDF ar atlasītajām AI piezīmēm",
                        data=annotated_pdf,
                        file_name="pdf_ar_ai_piezimem.pdf",
                        mime="application/pdf",
                    )
                else:
                    st.warning("Nav atlasīta neviena piezīme PDF anotācijām.")

    else:
        st.warning(
            "No PDF neizdevās izvilkt tekstu. Iespējams, tas ir skenēts PDF attēla formātā."
        )
