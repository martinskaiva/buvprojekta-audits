import json
from io import BytesIO

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from openai import OpenAI


st.set_page_config(page_title="Projekta konteksta pārbaude", layout="wide")

st.title("Būvprojekta projekta konteksta prototips")

st.write(
    "Šis rīks izveido projekta kontekstu no vairākiem PDF dokumentiem un pēc tam ļauj "
    "pārbaudīt jaunu PDF pret iepriekš izveidoto projekta kontekstu. "
    "Papildus tiek pārbaudītas arī drošas valodas/tulkojuma kļūdas jaunajā PDF."
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
        "tāme",
        "estimate",
        "bill of quantities",
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
        "stāva",
        "stava",
        "griezums",
        "shēma",
        "shema",
        "plāns",
        "plans",
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


def extract_pdf_text(file_bytes, document_name, document_type):
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
                        "document_name": document_name,
                        "document_type": document_type,
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


def call_ai_json(client, prompt, error_title):
    response = client.responses.create(
        model="gpt-4.1-mini",
        input=prompt,
        temperature=0,
    )

    raw_output = response.output_text.strip()
    cleaned_output = clean_ai_json_output(raw_output)

    try:
        data = json.loads(cleaned_output)
    except json.JSONDecodeError:
        st.error(error_title)
        st.code(raw_output)
        return []

    if not isinstance(data, list):
        return []

    return data


def build_document_text(df, max_blocks):
    selected = df.head(max_blocks)

    lines = []
    for index, row in selected.iterrows():
        lines.append(
            f"[ID {index}] [Dokuments: {row['document_name']}] "
            f"[Tips: {row['document_type']}] [Lapa {row['page']}] {row['text']}"
        )

    return "\n".join(lines)


def extract_facts_from_document(client, document_name, document_type, text_df, max_blocks):
    document_text = build_document_text(text_df, max_blocks)
    doc_type_readable = document_type_label(document_type)

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas analizētājs Latvijā.

Tavs uzdevums:
No zemāk dotā PDF izvilktā teksta izveido strukturētu faktu sarakstu, ko vēlāk var izmantot
projekta konteksta veidošanai un salīdzināšanai ar citiem dokumentiem.

Dokuments:
{document_name}

Automātiski noteiktais dokumenta tips:
{document_type} — {doc_type_readable}

Dokumenta tipi:
1. Skaidrojošais apraksts:
   Faila nosaukumā parasti ir "explanatory note", "description", "apraksts", "skaidrojoš".
   Meklē sistēmu aprakstus, prasības, diametrus, materiālus, daudzumus, stadiju, kārtu,
   adresi, objektu un atsauces uz rasējumiem.

2. Rasējums, shēma, stāva plāns, ģenerālplāns vai griezums:
   Faila nosaukumā parasti ir "scheme", "layout", "section", "plan", "floor",
   "general data", "site plan", "drawing", "rasējums", "plāns", "griezums".
   Meklē rasējuma numuru, nosaukumu, sadaļas kodu, titullauka datus, revīziju,
   datumu, mērogu, lapas ID, sistēmu/tīklu marķējumus, leģendu, apzīmējumus,
   diametrus, materiālus, spiediena klases un piezīmes.

3. Specifikācija vai apjomu tabula:
   Faila nosaukumā parasti ir "specification", "specifikācija", "apjomi", "works", "boq".
   Meklē pozīcijas, pozīciju numurus, markas, sistēmas, diametrus, materiālus,
   spiediena klases, daudzumus, mērvienības un LV/EN aprakstus.

GALVENAIS PRINCIPS:
Izvelc tikai konkrētus, pārbaudāmus faktus.
Neizdomā informāciju.
Ja nav pārliecības, faktu neizvelc.
Šis nav gramatikas pārbaudes uzdevums.

Meklē šādus faktu tipus:
- object_name
- address
- project_stage
- project_phase
- discipline
- document_title
- drawing_number
- drawing_title
- sheet_id
- revision
- date
- scale
- system_name
- network_name
- legend_item
- note
- specification_item
- specification_position
- equipment_mark
- pipe_mark
- manhole_mark
- trench_or_route_section
- pipe_diameter
- pipe_material
- pressure_class
- stiffness_class
- quantity
- unit
- material_or_parameter
- technical_requirement
- lv_en_description_pair
- previous_issue_reference
- other

Ja dokumenta tips ir explanatory_note:
- Meklē objekta nosaukumu, adresi, stadiju, kārtu.
- Meklē sistēmu nosaukumus un aprakstus.
- Meklē diametrus, materiālus, spiediena klases un daudzumus, ja tie minēti tekstā.
- Meklē prasības, piemēram, kur jāuzstāda konkrēti elementi.

Ja dokumenta tips ir drawing:
- Neignorē titullaukus un īsus blokus.
- Izvelc rasējuma numuru, nosaukumu, lapas ID, datumu, revīziju, mērogu, sadaļas kodu.
- Izvelc GENERAL DATA / VISPĀRĪGIE RĀDĪTĀJI kā drawing_title vai document_title.
- Izvelc SITE PLAN, WATER AND SEWERAGE NETWORKS, STĀVA PLĀNS, SHĒMA, GRIEZUMS kā drawing_title.
- Izvelc tīklu un sistēmu kodus, piemēram U1, K1, K2, K3, ja tie parādās kopā ar diametru,
  materiālu, leģendu, līniju, tīklu vai piezīmēm.
- Izvelc parametrus, piemēram D110, D160, OD75, OD110, Ø110, DN100, PE, PE100, PN10, PN16, SN8.
- PN10/PN16 klasificē kā pressure_class.
- SN4/SN8/SN16 klasificē kā stiffness_class, nevis pressure_class.
- Izvelc leģendas un apzīmējumu ierakstus kā legend_item.
- Izvelc rasējuma piezīmes kā note vai technical_requirement.

Ja dokumenta tips ir specification:
- Izvelc specifikācijas rindas kā specification_item.
- Izvelc pozīciju numurus kā specification_position.
- Izvelc markas, mezglus, akas, teknes, caurules, vārstus, lūkas un citas pozīcijas.
- Izvelc daudzumus un mērvienības.
- Izvelc diametrus, materiālus, spiediena klases un stingrības klases.
- PN10/PN16 klasificē kā pressure_class.
- SN4/SN8/SN16 klasificē kā stiffness_class.
- Ja rindā ir latviešu un angļu apraksts, izvelc to kā lv_en_description_pair.
- Ja vienā rindā parādās atšķirīgi marķējumi LV un EN aprakstā, saglabā tos kā faktus.

Svarīgi par tehniskajiem kodiem:
- Tehniskos kodus drīkst izvilkt kā faktus, ja tiem ir konteksts.
- Neizvelc pilnīgi izolētus kodus bez skaidra konteksta.
- Ja vienā blokā redzami vairāki parametri, izvelc tos kā atsevišķus faktus.
- Piemēri derīgiem faktiem ar kontekstu: U1 OD110 PE PN10, K2-T1, K3-5, OD75, D160, PN10, PE100, SN8.

Nedrīkst izvilkt:
- pieņēmumus;
- attēlu saturu;
- grafiskus elementus;
- faktus, kuri nav redzami ievadītajā tekstā.

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

Atgriez tikai faktus ar confidence 0.70 vai augstāku.

PDF teksts:
{document_text}
"""

    facts = call_ai_json(
        client=client,
        prompt=prompt,
        error_title=f"AI neatgrieza derīgu JSON faktu izvilkšanai dokumentam: {document_name}",
    )

    if not facts:
        return pd.DataFrame()

    facts_df = pd.DataFrame(facts)

    if "block_id" in facts_df.columns:
        facts_df["block_id"] = pd.to_numeric(facts_df["block_id"], errors="coerce")
        facts_df = facts_df.dropna(subset=["block_id"])
        facts_df["block_id"] = facts_df["block_id"].astype(int)

        facts_df = facts_df.merge(
            text_df.reset_index().rename(columns={"index": "block_id"}),
            on="block_id",
            how="left",
            suffixes=("", "_pdf"),
        )

    facts_df.insert(0, "source_document", document_name)
    facts_df.insert(1, "source_document_type", document_type)

    return facts_df


def check_language_in_new_document(client, document_name, document_type, text_df, max_blocks):
    document_text = build_document_text(text_df, max_blocks)
    doc_type_readable = document_type_label(document_type)

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas valodas un tulkojumu pārbaudītājs Latvijā.

Tavs uzdevums:
Pārbaudi JAUNĀ PDF dokumenta izvilkto tekstu un atrodi tikai drošas, acīmredzamas un praktiski labojamas
valodas, pareizrakstības vai tulkojuma kļūdas.

Dokuments:
{document_name}

Dokumenta tips:
{document_type} — {doc_type_readable}

GALVENAIS PRINCIPS:
Atzīmē tikai drošas kļūdas.
Ja ir kaut nelielas šaubas, piezīmi neliec.
Labāk atgriezt 0 piezīmes nekā 1 nepamatotu piezīmi.

Drīkst atzīmēt:
1. Acīmredzamas latviešu valodas pareizrakstības kļūdas.
2. Acīmredzamas latviešu valodas gramatikas kļūdas.
3. Acīmredzamas angļu valodas pareizrakstības kļūdas.
4. Acīmredzami nepareizus LV/EN tulkojumu pārus, ja tie maina nozīmi.
5. Neaizpildītus vietturus, piemēram dd.mm.gggg, Nr.X, XXX, TODO, [ievietot].
6. Acīmredzami bojātus tehniskus pierakstus.

Tulkojumu pārbaude:
- Pārbaudi LV/EN pārus rasējumu titullaukos, tabulās un specifikācijās.
- Atzīmē tikai tad, ja tulkojums ir acīmredzami nepareizs vai maina tehnisko nozīmi.
- Neatzīmē pieņemamus variantus:
  VISPĀRĪGIE RĀDĪTĀJI / GENERAL DATA,
  RASĒJUMA NR. / SHEET ID,
  MĒROGS / SCALE,
  DATUMS / DATE,
  IZMAIŅA / REVISION,
  STĀVS / FLOOR.

Datumu noteikums:
Neatzīmē datumus formātā dd.mm.yyyy vai dd.mm.yyyy. kā kļūdu.
Nākotnes datums pats par sevi nav kļūda.
Atzīmē tikai vietturus vai bojātus datumus, piemēram dd.mm.gggg, XX.XX.XXXX, 00.00.0000.

Nedrīkst atzīmēt:
- stilistiskus uzlabojumus;
- pieņemamus sinonīmus;
- virsrakstus vai tabulu šūnas kā nepilnīgas frāzes;
- tehniskus kodus kā valodas kļūdas;
- PN10, SN8, OD110, D160, PE100 un līdzīgus tehniskus pierakstus kā valodas kļūdas;
- vietvārdus, īpašvārdus un projekta specifiskus nosaukumus, ja nav pilnīgas pārliecības;
- frāzes, kas izskatās nepilnīgas tikai PDF teksta bloku sadalījuma dēļ.

Atbildi tikai JSON formātā.
JSON jābūt masīvam ar objektiem.
Ja nav drošu piezīmju, atgriez tukšu masīvu [].
Neizmanto Markdown.

Katram objektam jābūt šādiem laukiem:
- include_in_pdf
- issue_type
- block_id
- page
- category
- severity
- source_text
- comment
- suggestion
- confidence

Kategorijas:
- grammar
- spelling
- translation
- placeholder
- other

Severity:
- low
- medium
- high

Confidence norādi kā skaitli no 0 līdz 1.
Atgriez tikai piezīmes ar confidence 0.93 vai augstāku.

PDF teksts:
{document_text}
"""

    issues = call_ai_json(
        client=client,
        prompt=prompt,
        error_title="AI neatgrieza derīgu JSON jaunā dokumenta valodas pārbaudei.",
    )

    if not issues:
        return pd.DataFrame()

    issues_df = pd.DataFrame(issues)

    if "include_in_pdf" not in issues_df.columns:
        issues_df.insert(0, "include_in_pdf", True)

    if "issue_type" not in issues_df.columns:
        issues_df.insert(1, "issue_type", "language")

    if "block_id" in issues_df.columns:
        issues_df["block_id"] = pd.to_numeric(issues_df["block_id"], errors="coerce")
        issues_df = issues_df.dropna(subset=["block_id"])
        issues_df["block_id"] = issues_df["block_id"].astype(int)

        issues_df = issues_df.merge(
            text_df.reset_index().rename(columns={"index": "block_id"}),
            on="block_id",
            how="left",
            suffixes=("", "_pdf"),
        )

    return issues_df


def make_context_excel(all_text_df, all_facts_df):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        all_facts_df.to_excel(writer, sheet_name="facts", index=False)
        all_text_df.to_excel(writer, sheet_name="text_blocks", index=False)

    output.seek(0)
    return output


def read_context_excel(uploaded_context_file):
    try:
        facts_df = pd.read_excel(uploaded_context_file, sheet_name="facts")
    except Exception as exc:
        st.error(f"Neizdevās nolasīt project_context.xlsx lapu 'facts': {exc}")
        return pd.DataFrame()

    return facts_df


def facts_to_compact_text(facts_df, prefix, max_facts=250):
    if facts_df.empty:
        return ""

    selected = facts_df.head(max_facts)
    lines = []

    for index, row in selected.iterrows():
        source_document = row.get("source_document", row.get("document_name", ""))
        source_document_type = row.get("source_document_type", row.get("document_type", ""))
        fact_id = row.get("fact_id", f"{prefix}_{index}")
        fact_type = row.get("fact_type", "")
        label = row.get("label", "")
        value = row.get("value", "")
        page = row.get("page", "")
        evidence = row.get("evidence", "")

        lines.append(
            f"[{prefix} FACT {fact_id}] [source={source_document}] "
            f"[document_type={source_document_type}] [type={fact_type}] "
            f"[page={page}] {label}: {value} | evidence: {evidence}"
        )

    return "\n".join(lines)


def compare_new_document_to_context(client, context_facts_df, new_facts_df, max_context_facts):
    context_text = facts_to_compact_text(
        context_facts_df,
        prefix="CONTEXT",
        max_facts=max_context_facts,
    )

    new_text = facts_to_compact_text(
        new_facts_df,
        prefix="NEW",
        max_facts=250,
    )

    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas salīdzinātājs Latvijā.

Tavs uzdevums:
Salīdzini JAUNĀ dokumenta faktus ar IEPRIEKŠ IZVEIDOTU projekta kontekstu un atrodi tikai drošas,
acīmredzamas un praktiski pārbaudāmas pretrunas.

GALVENAIS PRINCIPS:
Labāk neatgriezt pretrunu nekā atgriezt viltus pozitīvu piezīmi.
Ja ir kaut nelielas šaubas, pretrunu neliec.
Atgriez tikai tādas pretrunas, kuras cilvēkam tiešām būtu vērts pārbaudīt.

Drīkst atzīmēt:
1. Atšķirīgu objekta nosaukumu, adresi, stadiju vai kārtu, ja vērtības tiešām konfliktē.
2. Sadaļas, rasējuma numura vai dokumenta nosaukuma pretrunu.
3. Diametra pretrunu, piemēram OD75 pret OD110, D110 pret D160, ja abas vērtības attiecas uz vienu un to pašu elementu.
4. Materiāla pretrunu, piemēram PE100 pret PVC, ja abas vērtības attiecas uz vienu un to pašu elementu.
5. Spiediena klases pretrunu, piemēram PN10 pret PN16.
6. Stingrības klases pretrunu, piemēram SN4 pret SN8.
7. Daudzuma pretrunu, piemēram 3 gab. pret 4 gab., ja tā attiecas uz vienu un to pašu pozīciju.
8. Marķējuma pretrunu, piemēram viena un tā pati tekne vai aka vienā vietā piesaistīta K2, citur K3.
9. LV/EN apraksta pretrunu specifikācijas rindā, ja tā maina tehnisko nozīmi.

Īpaši meklē:
- OD75 pret OD110;
- D110 pret D160;
- Ø110 pret Ø160;
- PN10 pret PN16;
- SN4 pret SN8;
- PE100 pret PVC;
- K2 pret K3 vienai un tai pašai pozīcijai;
- K1-1 pret K2-2 vai K3-4 vienas specifikācijas pozīcijas LV/EN aprakstos.

Ļoti svarīgi:
- PN un SN nav viena un tā pati parametru grupa.
- PN10/PN16 ir spiediena klase.
- SN4/SN8/SN16 ir stingrības klase.
- NEDRĪKST atzīmēt PN10 pret SN8 kā pretrunu, jo tie nav salīdzināmi parametri.
- PE pret PP drīkst atzīmēt tikai tad, ja skaidrs, ka abi attiecas uz vienu un to pašu elementu.
- D110 pret D160 drīkst atzīmēt tikai tad, ja skaidrs, ka abi attiecas uz vienu un to pašu elementu.
- K2 pret K2-T1 nav automātiska pretruna, jo K2-T1 var būt K2 sistēmas apakšmezgls.
- K2 pret K3 var būt pretruna tikai tad, ja tie attiecas uz vienu un to pašu pozīciju vai elementu.

Nedrīkst atzīmēt:
- faktu, kas ir tikai vienā pusē un otrā pusē nav minēts, ja nav skaidra iemesla to uzskatīt par problēmu;
- gadījumu, kur jaunais dokuments vienkārši ir detalizētāks;
- dažādus datumus, ja tie var būt normāli dažādi dokumentu datumi;
- pieņemamus sinonīmus;
- locījumu atšķirības;
- normatīvu neatbilstības;
- grafiskus simbolus vai attēlus;
- tehniskus kodus bez konteksta.

Pretruna ir tikai tad, ja var saprast, ka abas vērtības attiecas uz vienu un to pašu elementu, sistēmu, pozīciju, dokumentu, tīklu vai prasību.

Atbildi tikai JSON formātā.
JSON jābūt masīvam ar objektiem.
Ja nav drošu pretrunu, atgriez tukšu masīvu [].
Neizmanto Markdown.

Katram objektam jābūt šādiem laukiem:
- include_in_pdf
- issue_type
- category
- field
- context_source_document
- context_value
- context_page
- new_document_fact_id
- new_document_value
- new_document_page
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
- diameter
- material
- pressure_class
- stiffness_class
- quantity
- marking
- specification
- unresolved_previous_issue
- other

Confidence norādi kā skaitli no 0 līdz 1.
Atgriez tikai pretrunas ar confidence 0.90 vai augstāku.

IEPRIEKŠĒJAIS PROJEKTA KONTEKSTS:
{context_text}

JAUNĀ DOKUMENTA FAKTI:
{new_text}
"""

    contradictions = call_ai_json(
        client=client,
        prompt=prompt,
        error_title="AI neatgrieza derīgu JSON salīdzināšanai ar projekta kontekstu.",
    )

    if not contradictions:
        return pd.DataFrame()

    contradictions_df = pd.DataFrame(contradictions)

    if "include_in_pdf" not in contradictions_df.columns:
        contradictions_df.insert(0, "include_in_pdf", True)

    if "issue_type" not in contradictions_df.columns:
        contradictions_df.insert(1, "issue_type", "context_contradiction")

    if "new_document_fact_id" in contradictions_df.columns and "fact_id" in new_facts_df.columns:
        contradictions_df = contradictions_df.merge(
            new_facts_df[
                [
                    "fact_id",
                    "block_id",
                    "page",
                    "x0",
                    "y0",
                    "x1",
                    "y1",
                    "text",
                    "source_document",
                ]
            ],
            left_on="new_document_fact_id",
            right_on="fact_id",
            how="left",
            suffixes=("", "_new_fact"),
        )

    return contradictions_df


def make_review_excel(new_facts_df, language_issues_df, contradictions_df):
    output = BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        new_facts_df.to_excel(writer, sheet_name="new_document_facts", index=False)
        language_issues_df.to_excel(writer, sheet_name="language_issues", index=False)
        contradictions_df.to_excel(writer, sheet_name="context_contradictions", index=False)

    output.seek(0)
    return output


def add_pdf_annotation(page, x0, y0, x1, y1, annotation_text, stroke_color=(1, 0, 0)):
    rect = fitz.Rect(float(x0), float(y0), float(x1), float(y1))

    square_annot = page.add_rect_annot(rect)
    square_annot.set_info(
        title="AI būvprojekta pārbaude",
        content=annotation_text,
    )
    square_annot.set_colors(stroke=stroke_color)
    square_annot.set_border(width=1)
    square_annot.update()

    note_x = max(float(x1) + 5, float(x0) + 5)
    note_y = float(y0)
    note_point = fitz.Point(note_x, note_y)

    text_annot = page.add_text_annot(note_point, annotation_text)
    text_annot.set_info(
        title="AI būvprojekta pārbaude",
        content=annotation_text,
    )
    text_annot.update()


def create_annotated_new_pdf(new_file_bytes, approved_language_df, approved_contradictions_df):
    doc = fitz.open(stream=new_file_bytes, filetype="pdf")

    for _, issue in approved_language_df.iterrows():
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

        category = str(issue.get("category", "language"))
        severity = str(issue.get("severity", ""))
        source_text = str(issue.get("source_text", ""))
        comment = str(issue.get("comment", ""))
        suggestion = str(issue.get("suggestion", ""))
        confidence = issue.get("confidence", "")

        annotation_text = (
            f"AI piezīme — valoda/tulkojums\n"
            f"Kategorija: {category}\n"
            f"Nopietnība: {severity}\n"
            f"Ticamība: {confidence}\n\n"
            f"Atrastais teksts:\n{source_text}\n\n"
            f"Komentārs:\n{comment}\n\n"
            f"Ieteikums:\n{suggestion}"
        )

        add_pdf_annotation(page, x0, y0, x1, y1, annotation_text, stroke_color=(1, 0, 0))

    for _, issue in approved_contradictions_df.iterrows():
        try:
            page_number = int(issue.get("page", issue.get("new_document_page", 0)))
            x0 = float(issue.get("x0", 50))
            y0 = float(issue.get("y0", 50))
            x1 = float(issue.get("x1", x0 + 100))
            y1 = float(issue.get("y1", y0 + 20))
        except (TypeError, ValueError):
            continue

        if page_number < 1 or page_number > len(doc):
            continue

        page = doc[page_number - 1]

        category = str(issue.get("category", "context_contradiction"))
        field = str(issue.get("field", ""))
        context_source = str(issue.get("context_source_document", ""))
        context_value = str(issue.get("context_value", ""))
        new_value = str(issue.get("new_document_value", ""))
        comment = str(issue.get("comment", ""))
        suggestion = str(issue.get("suggestion", ""))
        confidence = issue.get("confidence", "")

        annotation_text = (
            f"AI piezīme — pretruna pret projekta kontekstu\n"
            f"Kategorija: {category}\n"
            f"Lauks: {field}\n"
            f"Ticamība: {confidence}\n\n"
            f"Konteksta avots:\n{context_source}\n\n"
            f"Konteksta vērtība:\n{context_value}\n\n"
            f"Jaunā dokumenta vērtība:\n{new_value}\n\n"
            f"Komentārs:\n{comment}\n\n"
            f"Ieteikums:\n{suggestion}"
        )

        add_pdf_annotation(page, x0, y0, x1, y1, annotation_text, stroke_color=(1, 0, 0))

    output = BytesIO()
    doc.save(output)
    output.seek(0)
    doc.close()

    return output


tab1, tab2 = st.tabs(
    [
        "1. Izveidot projekta kontekstu",
        "2. Pārbaudīt jaunu PDF pret kontekstu",
    ]
)


with tab1:
    st.subheader("1. Izveidot projekta kontekstu no vairākiem PDF")

    st.write(
        "Augšupielādē vairākus viena projekta PDF dokumentus. Rīks pēc faila nosaukuma mēģinās noteikt, "
        "vai fails ir skaidrojošais apraksts, rasējums vai specifikācija, un faktus izvilks atbilstoši dokumenta tipam."
    )

    context_files = st.file_uploader(
        "Augšupielādē projekta PDF dokumentus",
        type=["pdf"],
        accept_multiple_files=True,
        key="context_files",
    )

    max_blocks_per_context_document = st.number_input(
        "Cik teksta blokus analizēt no katra konteksta dokumenta?",
        min_value=50,
        max_value=1500,
        value=500,
        step=50,
    )

    if context_files:
        st.info(f"Augšupielādēti {len(context_files)} PDF dokumenti.")

        detected_rows = []
        for uploaded_file in context_files:
            detected_type = detect_document_type(uploaded_file.name)
            detected_rows.append(
                {
                    "file_name": uploaded_file.name,
                    "detected_document_type": detected_type,
                    "document_type_label": document_type_label(detected_type),
                }
            )

        st.subheader("Automātiski noteiktie dokumentu tipi")
        st.dataframe(pd.DataFrame(detected_rows), use_container_width=True)

        if st.button("Izveidot projekta kontekstu"):
            client = get_openai_client()

            if client is not None:
                all_text_frames = []
                all_fact_frames = []

                progress_bar = st.progress(0)
                status = st.empty()

                for i, uploaded_file in enumerate(context_files, start=1):
                    document_name = uploaded_file.name
                    document_type = detect_document_type(document_name)

                    status.write(
                        f"Apstrādā dokumentu {i}/{len(context_files)}: "
                        f"{document_name} ({document_type_label(document_type)})"
                    )

                    file_bytes = uploaded_file.read()
                    text_df, page_count = extract_pdf_text(
                        file_bytes,
                        document_name,
                        document_type,
                    )

                    all_text_frames.append(text_df)

                    st.write(
                        f"**{document_name}** — {document_type_label(document_type)} — "
                        f"izvilkti {len(text_df)} teksta bloki no {page_count} lapām."
                    )

                    if not text_df.empty:
                        with st.spinner(f"AI izvelk faktus no {document_name}..."):
                            facts_df = extract_facts_from_document(
                                client=client,
                                document_name=document_name,
                                document_type=document_type,
                                text_df=text_df,
                                max_blocks=min(len(text_df), max_blocks_per_context_document),
                            )

                        if not facts_df.empty:
                            all_fact_frames.append(facts_df)
                            st.success(f"No {document_name} izvilkti {len(facts_df)} fakti.")
                        else:
                            st.warning(f"No {document_name} netika izvilkti droši fakti.")

                    progress_bar.progress(i / len(context_files))

                all_text_df = (
                    pd.concat(all_text_frames, ignore_index=True)
                    if all_text_frames
                    else pd.DataFrame()
                )

                all_facts_df = (
                    pd.concat(all_fact_frames, ignore_index=True)
                    if all_fact_frames
                    else pd.DataFrame()
                )

                st.session_state["project_context_text_df"] = all_text_df
                st.session_state["project_context_facts_df"] = all_facts_df

                status.write("Projekta konteksta izveide pabeigta.")

        all_facts_df = st.session_state.get("project_context_facts_df")
        all_text_df = st.session_state.get("project_context_text_df")

        if all_facts_df is not None:
            st.divider()
            st.subheader("Izvilktie projekta fakti")

            if all_facts_df.empty:
                st.warning("Nav izvilkti droši fakti.")
            else:
                st.success(f"Kopā izvilkti {len(all_facts_df)} projekta fakti.")
                st.dataframe(all_facts_df, use_container_width=True)

                context_excel = make_context_excel(
                    all_text_df if all_text_df is not None else pd.DataFrame(),
                    all_facts_df,
                )

                st.download_button(
                    label="Lejupielādēt project_context.xlsx",
                    data=context_excel,
                    file_name="project_context.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )


with tab2:
    st.subheader("2. Pārbaudīt jaunu PDF pret projekta kontekstu")

    st.write(
        "Augšupielādē iepriekš izveidoto `project_context.xlsx` un jaunu PDF dokumentu. "
        "Rīks pārbaudīs jauno PDF valodas/tulkojuma kļūdas un salīdzinās to ar projekta kontekstu."
    )

    uploaded_context = st.file_uploader(
        "Augšupielādē project_context.xlsx",
        type=["xlsx"],
        key="uploaded_context",
    )

    new_pdf = st.file_uploader(
        "Augšupielādē jauno PDF dokumentu",
        type=["pdf"],
        key="new_pdf",
    )

    new_document_name = st.text_input(
        "Jaunā dokumenta nosaukums",
        value="Jaunais dokuments",
        key="new_document_name",
    )

    col1, col2, col3 = st.columns(3)

    with col1:
        max_blocks_new_document = st.number_input(
            "Cik teksta blokus analizēt no jaunā PDF?",
            min_value=50,
            max_value=1500,
            value=500,
            step=50,
            key="max_blocks_new_document",
        )

    with col2:
        max_context_facts = st.number_input(
            "Cik projekta konteksta faktus izmantot salīdzināšanai?",
            min_value=50,
            max_value=1000,
            value=300,
            step=50,
            key="max_context_facts",
        )

    with col3:
        max_language_blocks = st.number_input(
            "Cik teksta blokus pārbaudīt valodai/tulkojumam?",
            min_value=50,
            max_value=1500,
            value=500,
            step=50,
            key="max_language_blocks",
        )

    if uploaded_context is not None and new_pdf is not None:
        context_facts_df = read_context_excel(uploaded_context)

        if not context_facts_df.empty:
            st.success(f"Kontekstā ielādēti {len(context_facts_df)} fakti.")

            with st.expander("Apskatīt projekta konteksta faktus"):
                st.dataframe(context_facts_df, use_container_width=True)

            detected_new_type = detect_document_type(new_pdf.name)
            st.info(
                f"Jaunā PDF automātiski noteiktais tips: "
                f"**{detected_new_type} — {document_type_label(detected_new_type)}**"
            )

            if st.button("Pārbaudīt jauno PDF pret kontekstu"):
                client = get_openai_client()

                if client is not None:
                    new_file_bytes = new_pdf.read()

                    actual_new_document_name = (
                        new_pdf.name if new_document_name == "Jaunais dokuments" else new_document_name
                    )

                    new_text_df, new_page_count = extract_pdf_text(
                        new_file_bytes,
                        actual_new_document_name,
                        detected_new_type,
                    )

                    st.session_state["new_file_bytes"] = new_file_bytes
                    st.session_state["new_text_df"] = new_text_df

                    st.write(
                        f"No jaunā dokumenta izvilkti {len(new_text_df)} teksta bloki no {new_page_count} lapām."
                    )

                    with st.spinner("AI pārbauda jaunā dokumenta valodu un tulkojumu..."):
                        language_issues_df = check_language_in_new_document(
                            client=client,
                            document_name=actual_new_document_name,
                            document_type=detected_new_type,
                            text_df=new_text_df,
                            max_blocks=min(len(new_text_df), max_language_blocks),
                        )

                    st.session_state["language_issues_df"] = language_issues_df

                    with st.spinner("AI izvelk faktus no jaunā dokumenta..."):
                        new_facts_df = extract_facts_from_document(
                            client=client,
                            document_name=actual_new_document_name,
                            document_type=detected_new_type,
                            text_df=new_text_df,
                            max_blocks=min(len(new_text_df), max_blocks_new_document),
                        )

                    st.session_state["new_facts_df"] = new_facts_df

                    if new_facts_df.empty:
                        st.warning("No jaunā dokumenta netika izvilkti droši fakti.")
                        st.session_state["context_contradictions_df"] = pd.DataFrame()
                    else:
                        with st.spinner("AI salīdzina jauno dokumentu ar projekta kontekstu..."):
                            contradictions_df = compare_new_document_to_context(
                                client=client,
                                context_facts_df=context_facts_df,
                                new_facts_df=new_facts_df,
                                max_context_facts=max_context_facts,
                            )

                        st.session_state["context_contradictions_df"] = contradictions_df

        language_issues_df = st.session_state.get("language_issues_df")
        new_facts_df = st.session_state.get("new_facts_df")
        contradictions_df = st.session_state.get("context_contradictions_df")
        new_file_bytes = st.session_state.get("new_file_bytes")

        if language_issues_df is not None:
            st.divider()
            st.subheader("Valodas un tulkojuma piezīmes jaunajā dokumentā")

            if language_issues_df.empty:
                st.info("AI neatrada drošas valodas/tulkojuma piezīmes jaunajā dokumentā.")
                edited_language_issues_df = pd.DataFrame()
            else:
                st.success(f"AI atrada {len(language_issues_df)} valodas/tulkojuma piezīmes.")

                edited_language_issues_df = st.data_editor(
                    language_issues_df,
                    use_container_width=True,
                    num_rows="fixed",
                    key="language_issues_editor",
                )

                st.session_state["edited_language_issues_df"] = edited_language_issues_df

        if new_facts_df is not None:
            st.divider()
            st.subheader("Jaunā dokumenta fakti")

            if new_facts_df.empty:
                st.info("Nav atrasti droši fakti jaunajā dokumentā.")
            else:
                st.success(f"No jaunā dokumenta izvilkti {len(new_facts_df)} fakti.")
                st.dataframe(new_facts_df, use_container_width=True)

        if contradictions_df is not None:
            st.divider()
            st.subheader("Iespējamās pretrunas pret projekta kontekstu")

            if contradictions_df.empty:
                st.info("AI neatrada drošas pretrunas pret projekta kontekstu.")
                edited_contradictions_df = pd.DataFrame()
            else:
                st.success(f"AI atrada {len(contradictions_df)} iespējamas pretrunas.")

                edited_contradictions_df = st.data_editor(
                    contradictions_df,
                    use_container_width=True,
                    num_rows="fixed",
                    key="context_contradictions_editor",
                )

                st.session_state["edited_contradictions_df"] = edited_contradictions_df

            edited_language_issues_df = st.session_state.get(
                "edited_language_issues_df",
                language_issues_df if language_issues_df is not None else pd.DataFrame(),
            )

            edited_contradictions_df = st.session_state.get(
                "edited_contradictions_df",
                contradictions_df if contradictions_df is not None else pd.DataFrame(),
            )

            if edited_language_issues_df is None:
                edited_language_issues_df = pd.DataFrame()

            if edited_contradictions_df is None:
                edited_contradictions_df = pd.DataFrame()

            approved_language_df = (
                edited_language_issues_df[
                    edited_language_issues_df["include_in_pdf"] == True
                ].copy()
                if not edited_language_issues_df.empty and "include_in_pdf" in edited_language_issues_df.columns
                else pd.DataFrame()
            )

            approved_contradictions_df = (
                edited_contradictions_df[
                    edited_contradictions_df["include_in_pdf"] == True
                ].copy()
                if not edited_contradictions_df.empty and "include_in_pdf" in edited_contradictions_df.columns
                else pd.DataFrame()
            )

            st.info(
                f"PDF anotācijām atlasītas {len(approved_language_df)} valodas/tulkojuma piezīmes "
                f"un {len(approved_contradictions_df)} pretrunas pret kontekstu."
            )

            review_excel = make_review_excel(
                new_facts_df if new_facts_df is not None else pd.DataFrame(),
                edited_language_issues_df,
                edited_contradictions_df,
            )

            st.download_button(
                label="Lejupielādēt pārbaudes rezultātus Excel formātā",
                data=review_excel,
                file_name="jauna_dokumenta_parbaude_pret_kontekstu.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

            if new_file_bytes is not None and (
                not approved_language_df.empty or not approved_contradictions_df.empty
            ):
                annotated_pdf = create_annotated_new_pdf(
                    new_file_bytes,
                    approved_language_df,
                    approved_contradictions_df,
                )

                st.download_button(
                    label="Lejupielādēt jauno PDF ar atlasītajām AI piezīmēm",
                    data=annotated_pdf,
                    file_name="jaunais_pdf_ar_ai_piezimem.pdf",
                    mime="application/pdf",
                )
            else:
                st.warning("Nav atlasītu piezīmju PDF anotācijām.")
    else:
        st.info("Augšupielādē gan project_context.xlsx, gan jauno PDF dokumentu.")
