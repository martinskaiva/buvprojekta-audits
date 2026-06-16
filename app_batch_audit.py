import json
import zipfile
from io import BytesIO

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from openai import OpenAI


st.set_page_config(page_title="Būvprojekta komplekta audits", layout="wide")

st.title("Būvprojekta komplekta audits")

st.write(
    "Augšupielādē vairākus PDF failus. Rīks izvelk teksta laukus, veic AI auditu visa "
    "failu komplekta kontekstā, ļauj atķeksēt piezīmes un lejupielādēt anotētus PDF."
)


def get_openai_client():
    api_key = st.secrets.get("OPENAI_API_KEY")

    if not api_key:
        st.error("Nav atrasta OPENAI_API_KEY vērtība Streamlit Secrets sadaļā.")
        return None

    return OpenAI(api_key=api_key)


def detect_document_type(file_name):
    name = file_name.lower()

    explanatory_keywords = [
        "explanatory note",
        "explanatory",
        "description",
        "apraksts",
        "skaidrojo",
        "td_",
        "_td_",
    ]

    specification_keywords = [
        "specification",
        "specifik",
        "apjomi",
        "works",
        "boq",
        "bill of quantities",
        "tame",
        "tāme",
        "ms_",
        "_ms_",
    ]

    drawing_keywords = [
        "scheme",
        "layout",
        "section",
        "plan",
        "floor",
        "general data",
        "site plan",
        "drawing",
        "rasēj",
        "rasej",
        "stāva",
        "stava",
        "griezums",
        "shēma",
        "shema",
        "plāns",
        "plans",
        "ra_",
        "_ra_",
    ]

    if any(keyword in name for keyword in explanatory_keywords):
        return "explanatory_note"

    if any(keyword in name for keyword in specification_keywords):
        return "specification"

    if any(keyword in name for keyword in drawing_keywords):
        return "drawing"

    return "unknown"


def document_type_label(document_type):
    labels = {
        "explanatory_note": "Skaidrojošais apraksts",
        "drawing": "Rasējums / shēma / plāns / griezums",
        "specification": "Specifikācija / apjomu tabula",
        "unknown": "Neatpazīts dokumenta tips",
    }
    return labels.get(document_type, "Neatpazīts dokumenta tips")


def extract_pdf_text(file_bytes, file_name, document_type):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    rows = []

    local_block_id = 0

    for page_index, page in enumerate(doc):
        blocks = page.get_text("blocks")

        for block in blocks:
            x0, y0, x1, y1, text, block_no, block_type = block
            clean_text = text.strip()

            if clean_text:
                rows.append(
                    {
                        "source_file": file_name,
                        "document_type": document_type,
                        "block_id": local_block_id,
                        "page": page_index + 1,
                        "x0": round(x0, 2),
                        "y0": round(y0, 2),
                        "x1": round(x1, 2),
                        "y1": round(y1, 2),
                        "text": clean_text,
                    }
                )
                local_block_id += 1

    doc.close()
    return pd.DataFrame(rows)


def clean_ai_json_output(raw_output):
    raw_output = raw_output.strip()

    if raw_output.startswith("```json"):
        raw_output = raw_output.replace("```json", "", 1).strip()

    if raw_output.startswith("```"):
        raw_output = raw_output.replace("```", "", 1).strip()

    if raw_output.endswith("```"):
        raw_output = raw_output[:-3].strip()

    return raw_output


def build_audit_text(all_blocks_df, max_blocks_per_file):
    parts = []

    for source_file, file_df in all_blocks_df.groupby("source_file"):
        document_type = file_df["document_type"].iloc[0]
        selected = file_df.head(max_blocks_per_file)

        parts.append(
            f"\n=== DOKUMENTS: {source_file} | TIPS: {document_type} | BLOKI ANALĪZEI: {len(selected)} ==="
        )

        for _, row in selected.iterrows():
            parts.append(
                f"[source_file={row['source_file']}] "
                f"[document_type={row['document_type']}] "
                f"[page={row['page']}] "
                f"[block_id={row['block_id']}] "
                f"{row['text']}"
            )

    return "\n".join(parts)


def audit_documents_with_ai(client, all_blocks_df, priority_threshold, max_blocks_per_file):
    audit_text = build_audit_text(all_blocks_df, max_blocks_per_file)

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas auditors Latvijā.

Tavs uzdevums:
Auditēt vairākus viena būvprojekta PDF dokumentus kopā. Dokumenti var būt:
- skaidrojošie apraksti;
- rasējumi, shēmas, stāvu plāni, ģenerālplāni, griezumi;
- specifikācijas un apjomu tabulas.

Lietotāja izvēlētais kļūdu svarīguma slieksnis:
{priority_threshold}

Atgriez tikai tās piezīmes, kuru priority ir >= {priority_threshold}.

GALVENAIS PRINCIPS:
- Neizdomā kļūdas.
- Ja ir šaubas, piezīmi neliec.
- Labāk neatgriezt piezīmi nekā atgriezt viltus pozitīvu.
- Piezīmei jābūt piesaistāmai konkrētam failam, lapai un teksta blokam.
- Ja piezīmi nevar droši piesaistīt blokam, to drīkst dot tikai tad, ja tā ir būtiska, bet block_id liec null.

Dokumentu tipi:
1. Skaidrojošais apraksts:
   Parasti nosaukumā ir explanatory note, description, apraksts, skaidrojoš.
   Meklē sistēmu aprakstus, prasības, diametrus, materiālus, apjomus, aprēķinus,
   normatīvus, saistītos projektus un atsauces uz rasējumiem/specifikāciju.

2. Rasējums / shēma / plāns / griezums:
   Parasti nosaukumā ir scheme, layout, section, plan, floor, general data, site plan, drawing, rasējums, plāns, griezums, RA.
   Meklē titullaukus, rasējuma numurus, nosaukumus, revīzijas, datumus, mērogus,
   leģendas, tīklu apzīmējumus, marķējumus, diametrus, materiālus, spiediena/stingrības klases.

3. Specifikācija / apjomu tabula:
   Parasti nosaukumā ir specification, specifikācija, apjomi, works, BOQ, MS.
   Meklē pozīcijas, pozīciju numurus, markas, daudzumus, mērvienības, LV/EN aprakstus,
   diametrus, materiālus, spiediena/stingrības klases un tukšas/izlaistas pozīcijas.

Iebūvētā audita loģika pēc lietotāja piemēra:

PRIORITĀTE 10 — obligāti ziņot, ja ir drošs pamats:
- adreses drukas kļūdas, piemēram ANREJOSTAS pret ANDREJOSTAS;
- būtiskas gramatikas kļūdas, piemēram “centralizētājā” pret “centralizētajā”;
- acīmredzami kļūdaini angļu tulkojumi vispārīgajos rādītājos, leģendās vai specifikācijās;
- drukas kļūdas specifikācijās, piemēram “Skataka”, “pārsedzī”, “grūžu ķērājs”, “adatflitriem”;
- LV/EN virsrakstu sajaukšana, piemēram Vispārīgie rādītāji / Drawing list vai Rasējumu saraksts / General Data;
- aprēķinu summu nesakritības;
- nepabeigti aprēķini vai aprēķini bez mērvienību/lielumu skaidrojuma;
- tukša vai izlaista specifikācijas pozīcija;
- specifikācijas rindā trūkstoša marka/sistēmas apzīmējums;
- provizoriskas pozīcijas noformētas kā konkrēts apjoms;
- starpdokumentu diametru pretrunas, piemēram OD90 pret D110, D75 pret D50, OD75 pret OD110;
- SA/plānā paredzēts U1 pievads, bet specifikācijā nav skaidri redzamas U1 materiālu/montāžas pozīcijas;
- normatīvu sarakstu neatbilstības starp dokumentiem;
- saistīto projektu saraksta neatbilstības starp dokumentiem;
- revīzijas/apjomu aktualizācijas jautājumi pēc izmaiņām;
- A15 slodzes klases risks akām transporta/slodzes zonā;
- diametru pamatojuma trūkums, ja tekstā redzama acīmredzama neatbilstība vai nepilnība;
- 3 gab. D110 vienvirziena vārsti bez skaidras piesaistes akām/ievadiem;
- formulējumi specifikācijā, kas neatbilst faktiskajam darbu apjomam;
- ārējās ugunsdzēsības vai citu būtisku risinājumu atkarība no saistītā projekta, ja nav skaidrs risinājums.

PRIORITĀTE 6 — ziņot, ja slieksnis ir 6 vai zemāks:
- vienā dokumentu komplektā lietoti dažādi objekta apzīmējumi, piemēram C2-02, C2-2, C 2-2;
- būtiskas iekšējas nekonsekvences dokumentā;
- būtiskas tulkojuma neprecizitātes, kas var mainīt tehnisko nozīmi;
- specifikācijas un rasējuma apzīmējumu nesakritības, ja tās ir pietiekami drošas.

PRIORITĀTE 3–4 — ziņot tikai zema sliekšņa gadījumā:
- aprēķina apzīmējumu noformējuma problēmas;
- nebūtiskas gramatikas kļūdas;
- neskaidri formulējumi, kas nerada būtisku tehnisku risku.

PRIORITĀTE 1–2 — ziņot tikai ļoti jutīgā režīmā:
- liekas pēdiņas;
- nelieli noformējuma jautājumi;
- neskaidri saīsinājumi bez būtiska riska.

PRIORITĀTE 0 — neziņot:
- datumu atšķirības, ja nav skaidra pamata tās uzskatīt par kļūdu;
- revīzijas/titullauka sīkumi, ja nav pierādīta ietekme;
- stila jautājumi.

Īpaši pārbaudi:
1. Gramatiku un drukas kļūdas — tikai drošas.
2. Tulkojumus — īpaši LV/EN pārus rasējumos, leģendās, specifikācijās un vispārīgajos rādītājos.
3. Specifikācijas struktūru — tukšas pozīcijas, izlaistas pozīcijas, trūkstošas markas, neskaidri apjomi.
4. Starpdokumentu pretrunas — SA pret rasējumiem un specifikāciju.
5. U1/K1/K2/K3 sistēmas — U1 pievads, K1 tauku atsūknēšana, K2/K3 teknes, K3 tīkli, vienvirziena vārsti.
6. Normatīvus un saistītos projektus.
7. Aprēķinus un summas.

Svarīgi, lai nebūtu viltus pozitīvu:
- PN10 pret SN8 NAV pretruna. PN ir spiediena klase, SN ir stingrības klase.
- K2 pret K2-T1 NAV automātiska pretruna. K2-T1 var būt K2 sistēmas apakšmezgls.
- K2 pret K3 ziņo tikai tad, ja tie attiecas uz vienu un to pašu pozīciju/elementu.
- PE pret PP ziņo tikai tad, ja skaidrs, ka abi attiecas uz vienu un to pašu elementu.
- D110 pret D160 ziņo tikai tad, ja skaidrs, ka abi attiecas uz vienu un to pašu elementu.
- Ja viens dokuments kaut ko nemin, tā nav automātiska pretruna.
- Datuma formāts dd.mm.yyyy vai dd.mm.yyyy. ir pieņemams Latvijā.
- Nākotnes datums pats par sevi nav kļūda.
- Neatzīmē pieņemamus nozares terminus, sinonīmus vai locījumus.
- Neatzīmē grafiskus elementus, kurus nevar droši nolasīt no teksta.

Atbildi tikai JSON formātā.
JSON jābūt masīvam ar objektiem.
Ja nav drošu piezīmju, atgriez tukšu masīvu [].
Neizmanto Markdown.

Katram objektam jābūt:
- include_in_pdf
- priority
- issue_type
- category
- source_file
- page
- block_id
- source_text
- comment
- suggestion
- related_files
- confidence

issue_type vērtības:
- grammar
- spelling
- translation
- internal_consistency
- cross_document
- specification_structure
- calculation
- quantity
- diameter
- material
- pressure_class
- stiffness_class
- marking
- normative
- related_project
- other

Atgriez tikai piezīmes ar priority >= {priority_threshold}.
Confidence norādi kā skaitli no 0 līdz 1.

PDF teksta bloki:
{audit_text}
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

    if not isinstance(issues, list) or not issues:
        return pd.DataFrame()

    issues_df = pd.DataFrame(issues)

    if "include_in_pdf" not in issues_df.columns:
        issues_df.insert(0, "include_in_pdf", True)

    if "priority" in issues_df.columns:
        issues_df["priority"] = pd.to_numeric(issues_df["priority"], errors="coerce").fillna(0)
        issues_df = issues_df[issues_df["priority"] >= priority_threshold].copy()

    if "block_id" in issues_df.columns:
        issues_df["block_id"] = pd.to_numeric(issues_df["block_id"], errors="coerce")

    if "page" in issues_df.columns:
        issues_df["page"] = pd.to_numeric(issues_df["page"], errors="coerce")

    return issues_df


def merge_issue_coordinates(issues_df, all_blocks_df):
    if issues_df.empty:
        return issues_df

    merged = issues_df.merge(
        all_blocks_df[
            [
                "source_file",
                "block_id",
                "page",
                "x0",
                "y0",
                "x1",
                "y1",
                "text",
                "document_type",
            ]
        ],
        on=["source_file", "block_id", "page"],
        how="left",
        suffixes=("", "_pdf"),
    )

    return merged


def make_excel_bytes(issues_df, all_blocks_df):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        issues_df.to_excel(writer, sheet_name="audit_issues", index=False)
        all_blocks_df.to_excel(writer, sheet_name="text_blocks", index=False)

    output.seek(0)
    return output


def add_annotation(page, x0, y0, x1, y1, annotation_text):
    rect = fitz.Rect(float(x0), float(y0), float(x1), float(y1))

    square_annot = page.add_rect_annot(rect)
    square_annot.set_info(
        title="AI būvprojekta audits",
        content=annotation_text,
    )
    square_annot.set_colors(stroke=(1, 0, 0))
    square_annot.set_border(width=1)
    square_annot.update()

    note_point = fitz.Point(float(x1) + 5, float(y0))
    text_annot = page.add_text_annot(note_point, annotation_text)
    text_annot.set_info(
        title="AI būvprojekta audits",
        content=annotation_text,
    )
    text_annot.update()


def create_annotated_pdf(file_bytes, file_issues_df):
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    for _, issue in file_issues_df.iterrows():
        try:
            page_number = int(issue.get("page"))
            x0 = float(issue.get("x0"))
            y0 = float(issue.get("y0"))
            x1 = float(issue.get("x1"))
            y1 = float(issue.get("y1"))
        except (TypeError, ValueError):
            continue

        if page_number < 1 or page_number > len(doc):
            continue

        page = doc[page_number - 1]

        annotation_text = (
            f"AI piezīme\n"
            f"Prioritāte: {issue.get('priority', '')}\n"
            f"Tips: {issue.get('issue_type', '')}\n"
            f"Kategorija: {issue.get('category', '')}\n"
            f"Ticamība: {issue.get('confidence', '')}\n\n"
            f"Atrastais teksts:\n{issue.get('source_text', '')}\n\n"
            f"Komentārs:\n{issue.get('comment', '')}\n\n"
            f"Ieteikums:\n{issue.get('suggestion', '')}\n\n"
            f"Saistītie faili:\n{issue.get('related_files', '')}"
        )

        add_annotation(page, x0, y0, x1, y1, annotation_text)

    output = BytesIO()
    doc.save(output)
    output.seek(0)
    doc.close()

    return output


def create_zip_with_results(uploaded_file_bytes, approved_issues_df, all_blocks_df):
    zip_buffer = BytesIO()

    issues_with_coords = merge_issue_coordinates(approved_issues_df, all_blocks_df)

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        excel_bytes = make_excel_bytes(issues_with_coords, all_blocks_df)
        zf.writestr("audit_results.xlsx", excel_bytes.getvalue())

        for source_file, file_issues_df in issues_with_coords.groupby("source_file"):
            if file_issues_df.empty:
                continue

            file_issues_df = file_issues_df.dropna(subset=["x0", "y0", "x1", "y1"])

            if file_issues_df.empty:
                continue

            if source_file not in uploaded_file_bytes:
                continue

            annotated_pdf = create_annotated_pdf(
                uploaded_file_bytes[source_file],
                file_issues_df,
            )

            safe_name = source_file.replace("/", "_").replace("\\", "_")
            zf.writestr(f"annotated_{safe_name}", annotated_pdf.getvalue())

    zip_buffer.seek(0)
    return zip_buffer


uploaded_files = st.file_uploader(
    "Augšupielādē auditējamos PDF failus",
    type=["pdf"],
    accept_multiple_files=True,
)

priority_threshold = st.slider(
    "Kļūdu svarīguma slieksnis",
    min_value=0,
    max_value=10,
    value=6,
    step=1,
    help="0 = rādīt arī sīkumus; 6 = būtiskās piezīmes; 10 = tikai ļoti būtiskas piezīmes.",
)

max_blocks_per_file = st.number_input(
    "Cik teksta blokus analizēt no katra PDF?",
    min_value=50,
    max_value=1500,
    value=700,
    step=50,
)

if uploaded_files:
    st.subheader("Augšupielādētie dokumenti")

    file_bytes_map = {}
    all_block_frames = []
    file_summary_rows = []

    for uploaded_file in uploaded_files:
        file_name = uploaded_file.name
        file_bytes = uploaded_file.read()
        file_bytes_map[file_name] = file_bytes

        document_type = detect_document_type(file_name)

        text_df = extract_pdf_text(
            file_bytes=file_bytes,
            file_name=file_name,
            document_type=document_type,
        )

        all_block_frames.append(text_df)

        file_summary_rows.append(
            {
                "file_name": file_name,
                "document_type": document_type,
                "document_type_label": document_type_label(document_type),
                "text_blocks": len(text_df),
            }
        )

    summary_df = pd.DataFrame(file_summary_rows)
    st.dataframe(summary_df, use_container_width=True)

    all_blocks_df = (
        pd.concat(all_block_frames, ignore_index=True)
        if all_block_frames
        else pd.DataFrame()
    )

    st.success(f"Kopā izvilkti {len(all_blocks_df)} teksta bloki no {len(uploaded_files)} PDF failiem.")

    with st.expander("Apskatīt izvilktos teksta blokus"):
        st.dataframe(all_blocks_df, use_container_width=True)

    if st.button("Palaist AI auditu"):
        client = get_openai_client()

        if client is not None:
            with st.spinner("AI auditē visu dokumentu komplektu..."):
                issues_df = audit_documents_with_ai(
                    client=client,
                    all_blocks_df=all_blocks_df,
                    priority_threshold=priority_threshold,
                    max_blocks_per_file=max_blocks_per_file,
                )

            issues_df = merge_issue_coordinates(issues_df, all_blocks_df)

            st.session_state["batch_audit_issues_df"] = issues_df
            st.session_state["batch_audit_blocks_df"] = all_blocks_df
            st.session_state["batch_audit_file_bytes_map"] = file_bytes_map

    issues_df = st.session_state.get("batch_audit_issues_df")
    stored_blocks_df = st.session_state.get("batch_audit_blocks_df")
    stored_file_bytes_map = st.session_state.get("batch_audit_file_bytes_map")

    if issues_df is not None:
        st.divider()
        st.subheader("AI atrastās piezīmes")

        if issues_df.empty:
            st.info("AI neatrada drošas piezīmes pie izvēlētā svarīguma sliekšņa.")
        else:
            st.success(f"AI atrada {len(issues_df)} piezīmes.")

            edited_issues_df = st.data_editor(
                issues_df,
                use_container_width=True,
                num_rows="fixed",
                key="batch_audit_editor",
            )

            approved_issues_df = (
                edited_issues_df[edited_issues_df["include_in_pdf"] == True].copy()
                if "include_in_pdf" in edited_issues_df.columns
                else edited_issues_df.copy()
            )

            st.info(
                f"PDF anotācijām atlasītas {len(approved_issues_df)} no {len(edited_issues_df)} piezīmēm."
            )

            excel_bytes = make_excel_bytes(edited_issues_df, stored_blocks_df)

            st.download_button(
                label="Lejupielādēt Excel audita atskaiti",
                data=excel_bytes,
                file_name="audit_results.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            if not approved_issues_df.empty:
                zip_bytes = create_zip_with_results(
                    uploaded_file_bytes=stored_file_bytes_map,
                    approved_issues_df=approved_issues_df,
                    all_blocks_df=stored_blocks_df,
                )

                st.download_button(
                    label="Lejupielādēt ZIP ar anotētiem PDF",
                    data=zip_bytes,
                    file_name="annotetie_pdf_un_audita_atskaite.zip",
                    mime="application/zip",
                )
            else:
                st.warning("Nav atlasīta neviena piezīme PDF anotācijām.")
else:
    st.info("Augšupielādē vairākus PDF failus, lai sāktu komplekta auditu.")
