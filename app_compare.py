import json
from io import BytesIO

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from openai import OpenAI


st.set_page_config(page_title="Divu PDF salīdzināšana", layout="wide")

st.title("Divu būvprojekta PDF dokumentu salīdzināšanas prototips")

st.write(
    "Šis ir atsevišķs rīks divu PDF dokumentu salīdzināšanai. "
    "Tas nemaina esošo viena dokumenta gramatikas pārbaudes rīku."
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


def clean_ai_json_output(raw_output):
    raw_output = raw_output.strip()

    if raw_output.startswith("```json"):
        raw_output = raw_output.replace("```json", "", 1).strip()

    if raw_output.startswith("```"):
        raw_output = raw_output.replace("```", "", 1).strip()

    if raw_output.endswith("```"):
        raw_output = raw_output[:-3].strip()

    return raw_output


def build_document_text(df, max_blocks):
    selected = df.head(max_blocks)

    lines = []
    for index, row in selected.iterrows():
        lines.append(f"[ID {index}] [Lapa {row['page']}] {row['text']}")

    return "\n".join(lines)


def get_openai_client():
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        st.error("Nav atrasta OPENAI_API_KEY vērtība Streamlit Secrets sadaļā.")
        return None

    return OpenAI(api_key=api_key)


def extract_facts_with_ai(client, document_name, df, max_blocks):
    document_text = build_document_text(df, max_blocks)

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas analizētājs Latvijā.

Tavs uzdevums:
No zemāk dotā PDF izvilktā teksta izveido strukturētu faktu sarakstu, ko vēlāk var salīdzināt ar cita dokumenta faktiem.

Dokumenta nosaukums:
{document_name}

GALVENAIS PRINCIPS:
Izvelc tikai konkrētus, pārbaudāmus faktus.
Neizdomā informāciju.
Ja nav pārliecības, faktu neizvelc.
Neveido secinājumus par normatīvu atbilstību.

Meklē šādus faktu tipus:
- object_name
- address
- project_stage
- project_phase
- discipline
- drawing_number
- drawing_title
- sheet_id
- revision
- date
- scale
- system_name
- equipment_mark
- quantity
- material_or_parameter
- other

Īpaši vērtīgi fakti:
- objekta nosaukums;
- objekta adrese;
- projekta stadija;
- kārta;
- sadaļas nosaukums vai kods;
- rasējuma numurs;
- rasējuma nosaukums;
- revīzija;
- datums;
- mērogs;
- sistēmas vai tīkla nosaukums;
- apjomi, skaiti, diametri, marķējumi tikai tad, ja tie skaidri parādās tekstā.

Nedrīkst izvilkt:
- nejaušus īsus tehniskos kodus bez konteksta;
- atsevišķus simbolus, piemēram, K1, K2, U1, PN10, ja nav skaidrs, ko tie nozīmē;
- frāzes, kas izskatās saraustītas tikai PDF teksta izvilkšanas dēļ;
- attēlu saturu;
- grafiskus elementus;
- pieņēmumus.

Atbildi tikai JSON formātā.
JSON jābūt masīvam ar objektiem.
Ja nav drošu faktu, atgriez tukšu masīvu [].
Neizmanto Markdown.

Katram objektam jābūt šādiem laukiem:
- fact_id
- block_id
- page
- fact_type
- label
- value
- evidence
- confidence

Lauku skaidrojums:
- fact_id: īss unikāls ID šajā dokumentā, piemēram, F001
- block_id: PDF teksta bloka ID no ievades
- page: lapas numurs
- fact_type: viens no atļautajiem faktu tipiem
- label: cilvēkam saprotams lauka nosaukums, piemēram, "Objekta adrese"
- value: konkrētā vērtība
- evidence: īss fragments no teksta, kas pamato faktu
- confidence: skaitlis no 0 līdz 1

Atgriez tikai faktus ar confidence 0.85 vai augstāku.

PDF teksts:
{document_text}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        temperature=0,
    )

    raw_output = response.output_text.strip()
    cleaned_output = clean_ai_json_output(raw_output)

    try:
        facts = json.loads(cleaned_output)
    except json.JSONDecodeError:
        st.error(f"AI neatgrieza derīgu JSON faktu izvilkšanai dokumentam: {document_name}")
        st.code(raw_output)
        return pd.DataFrame()

    if not facts:
        return pd.DataFrame()

    facts_df = pd.DataFrame(facts)

    if "block_id" in facts_df.columns:
        facts_df["block_id"] = pd.to_numeric(facts_df["block_id"], errors="coerce")
        facts_df = facts_df.dropna(subset=["block_id"])
        facts_df["block_id"] = facts_df["block_id"].astype(int)

        facts_df = facts_df.merge(
            df.reset_index().rename(columns={"index": "block_id"}),
            on="block_id",
            how="left",
            suffixes=("", "_pdf"),
        )

    facts_df.insert(0, "document", document_name)

    return facts_df


def facts_to_compact_text(facts_df, prefix):
    lines = []

    for index, row in facts_df.iterrows():
        fact_id = row.get("fact_id", f"{prefix}_{index}")
        fact_type = row.get("fact_type", "")
        label = row.get("label", "")
        value = row.get("value", "")
        page = row.get("page", "")
        evidence = row.get("evidence", "")

        lines.append(
            f"[{prefix} FACT {fact_id}] [type={fact_type}] [page={page}] "
            f"{label}: {value} | evidence: {evidence}"
        )

    return "\n".join(lines)


def compare_facts_with_ai(client, facts_a_df, facts_b_df):
    facts_a_text = facts_to_compact_text(facts_a_df, "A")
    facts_b_text = facts_to_compact_text(facts_b_df, "B")

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas salīdzinātājs Latvijā.

Tavs uzdevums:
Salīdzini divu būvprojekta dokumentu faktus un atrodi tikai drošas, acīmredzamas un praktiski pārbaudāmas pretrunas.

GALVENAIS PRINCIPS:
Labāk neatgriezt pretrunu nekā atgriezt viltus pozitīvu piezīmi.
Ja ir kaut nelielas šaubas, pretrunu neliec.
Atgriez tikai tādas pretrunas, kuras cilvēkam tiešām būtu vērts pārbaudīt.

Drīkst atzīmēt:
1. Atšķirīgu objekta nosaukumu, ja atšķirība nav tikai locījums vai saīsinājums.
2. Atšķirīgu adresi, ja tā izskatās kā reāla pretruna vai pārrakstīšanās kļūda.
3. Atšķirīgu projekta stadiju, ja vērtības tiešām konfliktē.
4. Atšķirīgu kārtu, ja vērtības tiešām konfliktē.
5. Sadaļas vai rasējuma numura pretrunu, ja viens dokuments skaidri atsaucas uz citu kodu.
6. Rasējuma nosaukuma pretrunu.
7. Revīzijas vai datuma pretrunu tikai tad, ja pretruna ir acīmredzama.
8. Skaitļu, daudzumu, diametru vai marķējumu pretrunu tikai tad, ja abos dokumentos ir skaidri salīdzināmas vērtības.

Nedrīkst atzīmēt:
- atšķirīgus datumus, ja tie var būt normāli dažādi dokumentu datumi;
- nākotnes datumus kā kļūdu;
- atšķirīgus formulējumus, ja nozīme ir tā pati;
- pieņemamus sinonīmus;
- locījumu atšķirības;
- saīsinājumus, ja tie nepārprotami nozīmē to pašu;
- faktus, kas vienā dokumentā nav minēti;
- gadījumu, kur viens dokuments ir detalizētāks par otru;
- tehniskus kodus bez skaidra konteksta;
- normatīvu neatbilstības;
- grafiskus simbolus vai attēlus.

Īpaša uzmanība:
Ja viens dokuments kaut ko nemin, bet otrs min, tā nav pretruna.
Pretruna ir tikai tad, ja abi dokumenti par vienu un to pašu jautājumu apgalvo atšķirīgas lietas.

Atbildi tikai JSON formātā.
JSON jābūt masīvam ar objektiem.
Ja nav drošu pretrunu, atgriez tukšu masīvu [].
Neizmanto Markdown.

Katram objektam jābūt šādiem laukiem:
- include_in_report
- category
- field
- document_a_fact_id
- document_a_value
- document_a_page
- document_b_fact_id
- document_b_value
- document_b_page
- comment
- suggestion
- confidence

Kategorijas:
- object_name
- address
- stage
- phase
- discipline
- drawing_number
- drawing_title
- revision
- date
- quantity
- marking
- other

Confidence norādi kā skaitli no 0 līdz 1.
Atgriez tikai pretrunas ar confidence 0.90 vai augstāku.

Dokumenta A fakti:
{facts_a_text}

Dokumenta B fakti:
{facts_b_text}
"""

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        temperature=0,
    )

    raw_output = response.output_text.strip()
    cleaned_output = clean_ai_json_output(raw_output)

    try:
        contradictions = json.loads(cleaned_output)
    except json.JSONDecodeError:
        st.error("AI neatgrieza derīgu JSON dokumentu salīdzināšanai.")
        st.code(raw_output)
        return pd.DataFrame()

    if not contradictions:
        return pd.DataFrame()

    contradictions_df = pd.DataFrame(contradictions)

    if "include_in_report" not in contradictions_df.columns:
        contradictions_df.insert(0, "include_in_report", True)

    return contradictions_df


def make_excel_download(sheets):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            safe_sheet_name = sheet_name[:31]
            df.to_excel(writer, sheet_name=safe_sheet_name, index=False)

    output.seek(0)
    return output


st.subheader("1. Augšupielādē divus PDF dokumentus")

col1, col2 = st.columns(2)

with col1:
    file_a = st.file_uploader("Dokuments A", type=["pdf"], key="file_a")
    document_a_name = st.text_input("Dokumenta A nosaukums", value="Dokuments A")

with col2:
    file_b = st.file_uploader("Dokuments B", type=["pdf"], key="file_b")
    document_b_name = st.text_input("Dokumenta B nosaukums", value="Dokuments B")


if file_a is not None and file_b is not None:
    file_a_bytes = file_a.read()
    file_b_bytes = file_b.read()

    df_a, page_count_a = extract_pdf_text(file_a_bytes)
    df_b, page_count_b = extract_pdf_text(file_b_bytes)

    st.success(
        f"Dokuments A: {len(df_a)} teksta bloki no {page_count_a} lapām. "
        f"Dokuments B: {len(df_b)} teksta bloki no {page_count_b} lapām."
    )

    with st.expander("Apskatīt izvilkto tekstu no dokumenta A"):
        st.dataframe(df_a, use_container_width=True)

    with st.expander("Apskatīt izvilkto tekstu no dokumenta B"):
        st.dataframe(df_b, use_container_width=True)

    st.divider()

    st.subheader("2. Izvēlies, cik teksta blokus analizēt")

    col3, col4 = st.columns(2)

    with col3:
        max_blocks_a = st.number_input(
            "Dokumenta A bloku skaits analīzei",
            min_value=1,
            max_value=max(1, len(df_a)),
            value=min(len(df_a), 500),
            step=50,
        )

    with col4:
        max_blocks_b = st.number_input(
            "Dokumenta B bloku skaits analīzei",
            min_value=1,
            max_value=max(1, len(df_b)),
            value=min(len(df_b), 500),
            step=50,
        )

    st.caption(
        "Pirmajā salīdzināšanas versijā AI vispirms izvelk faktus no katra dokumenta, "
        "pēc tam salīdzina šos faktus. Lieliem rasējumiem var sākt ar 300–500 blokiem."
    )

    if st.button("Salīdzināt dokumentus ar AI"):
        client = get_openai_client()

        if client is not None:
            with st.spinner("AI izvelk faktus no dokumenta A..."):
                facts_a_df = extract_facts_with_ai(
                    client=client,
                    document_name=document_a_name,
                    df=df_a,
                    max_blocks=max_blocks_a,
                )

            with st.spinner("AI izvelk faktus no dokumenta B..."):
                facts_b_df = extract_facts_with_ai(
                    client=client,
                    document_name=document_b_name,
                    df=df_b,
                    max_blocks=max_blocks_b,
                )

            st.session_state["facts_a_df"] = facts_a_df
            st.session_state["facts_b_df"] = facts_b_df

            if facts_a_df.empty or facts_b_df.empty:
                st.warning(
                    "Vienā no dokumentiem AI neatrada pietiekami drošus faktus salīdzināšanai."
                )
                st.session_state["contradictions_df"] = pd.DataFrame()
            else:
                with st.spinner("AI salīdzina faktus starp dokumentiem..."):
                    contradictions_df = compare_facts_with_ai(
                        client=client,
                        facts_a_df=facts_a_df,
                        facts_b_df=facts_b_df,
                    )

                st.session_state["contradictions_df"] = contradictions_df

    facts_a_df = st.session_state.get("facts_a_df")
    facts_b_df = st.session_state.get("facts_b_df")
    contradictions_df = st.session_state.get("contradictions_df")

    if facts_a_df is not None and facts_b_df is not None:
        st.divider()
        st.subheader("3. AI izvilktie fakti")

        col5, col6 = st.columns(2)

        with col5:
            st.write(f"**{document_a_name} — fakti**")
            if facts_a_df.empty:
                st.info("Nav atrasti droši fakti.")
            else:
                st.dataframe(facts_a_df, use_container_width=True)

        with col6:
            st.write(f"**{document_b_name} — fakti**")
            if facts_b_df.empty:
                st.info("Nav atrasti droši fakti.")
            else:
                st.dataframe(facts_b_df, use_container_width=True)

    if contradictions_df is not None:
        st.divider()
        st.subheader("4. Iespējamās pretrunas starp dokumentiem")

        if contradictions_df.empty:
            st.info("AI neatrada drošas pretrunas starp dokumentiem.")
        else:
            st.success(f"AI atrada {len(contradictions_df)} iespējamas pretrunas.")

            edited_contradictions_df = st.data_editor(
                contradictions_df,
                use_container_width=True,
                num_rows="fixed",
                key="contradictions_editor",
            )

            approved_contradictions_df = edited_contradictions_df[
                edited_contradictions_df["include_in_report"] == True
            ].copy()

            st.info(
                f"Atskaitē atlasītas {len(approved_contradictions_df)} no "
                f"{len(edited_contradictions_df)} pretrunām."
            )

            excel_buffer = make_excel_download(
                {
                    "Dokuments A fakti": facts_a_df if facts_a_df is not None else pd.DataFrame(),
                    "Dokuments B fakti": facts_b_df if facts_b_df is not None else pd.DataFrame(),
                    "Pretrunas": edited_contradictions_df,
                    "Atlasītās pretrunas": approved_contradictions_df,
                }
            )

            st.download_button(
                label="Lejupielādēt salīdzināšanas rezultātus Excel formātā",
                data=excel_buffer,
                file_name="dokumentu_salidzinasana.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
else:
    st.info("Augšupielādē abus PDF dokumentus, lai sāktu salīdzināšanu.")
