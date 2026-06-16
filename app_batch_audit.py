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
    "Augšupielādē vairākus PDF failus. Rīks izvelk teksta laukus, veic vairākposmu AI auditu "
    "visa failu komplekta kontekstā, ļauj atķeksēt piezīmes un lejupielādēt anotētus PDF."
)


# ----------------------------
# Pamata funkcijas
# ----------------------------

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


def normalize_issues(issues, default_source_file=None, default_issue_scope=None):
    if not issues:
        return pd.DataFrame()

    df = pd.DataFrame(issues)

    required_columns = [
        "include_in_pdf",
        "priority",
        "issue_type",
        "category",
        "source_file",
        "page",
        "block_id",
        "source_text",
        "comment",
        "suggestion",
        "related_files",
        "confidence",
    ]

    for col in required_columns:
        if col not in df.columns:
            if col == "include_in_pdf":
                df[col] = True
            elif col == "source_file" and default_source_file:
                df[col] = default_source_file
            else:
                df[col] = ""

    if default_issue_scope:
        df["audit_scope"] = default_issue_scope
    elif "audit_scope" not in df.columns:
        df["audit_scope"] = ""

    df["priority"] = pd.to_numeric(df["priority"], errors="coerce").fillna(0).astype(int)
    df["page"] = pd.to_numeric(df["page"], errors="coerce")
    df["block_id"] = pd.to_numeric(df["block_id"], errors="coerce")
    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce").fillna(0)

    return df[required_columns + ["audit_scope"]]


def build_blocks_text(blocks_df):
    lines = []

    for _, row in blocks_df.iterrows():
        lines.append(
            f"[source_file={row['source_file']}] "
            f"[document_type={row['document_type']}] "
            f"[page={row['page']}] "
            f"[block_id={row['block_id']}] "
            f"{row['text']}"
        )

    return "\n".join(lines)


# ----------------------------
# Prompta kopīgā audita loģika
# ----------------------------

def audit_rules_text(priority_threshold):
    return f"""
Lietotāja izvēlētais kļūdu svarīguma slieksnis:
{priority_threshold}

Atgriez tikai piezīmes, kuru priority >= {priority_threshold}.

GALVENAIS PRINCIPS:
- Neizdomā kļūdas.
- Ja ir šaubas, piezīmi neliec.
- Tomēr drīkst uzdot pamatotu audita jautājumu, ja tas balstās konkrētā tekstā un ir praktiski svarīgs.
- Piezīmei jābūt piesaistāmai konkrētam failam, lapai un teksta blokam.
- Ja piezīmi nevar droši piesaistīt blokam, block_id liec null.
- Prioritāte nav tas pats, kas confidence. Prioritāte nozīmē kļūdas nozīmīgumu auditā.
- Pie sliekšņa 6 vai augstāka nerādi sīkas redakcionālas, noformējuma vai stila piezīmes.

PRIORITĀTE 10 — obligāti ziņot, ja ir drošs pamats:
- adreses drukas kļūdas, piemēram ANREJOSTAS pret ANDREJOSTAS;
- būtiskas gramatikas kļūdas, kas ir acīmredzamas un nav stila jautājums;
- acīmredzami kļūdaini angļu tulkojumi vispārīgajos rādītājos, leģendās vai specifikācijās;
- drukas kļūdas parastos vārdos, ja pareizā forma ir nepārprotama;
- drukas kļūdas specifikācijās, piemēram “pārsedzī”, “grūžu ķērājs”, “adatflitriem”, ja pareizais vārds ir skaidrs;
- LV/EN virsrakstu sajaukšana, piemēram Vispārīgie rādītāji / Drawing list vai Rasējumu saraksts / General Data;
- aprēķinu summu nesakritības;
- nepabeigti aprēķini vai aprēķini bez mērvienību/lielumu skaidrojuma;
- tukša vai izlaista specifikācijas pozīcija;
- specifikācijas rindā trūkstoša marka/sistēmas apzīmējums, ja tas var radīt neskaidrību būvdarbos;
- provizoriskas pozīcijas noformētas kā konkrēts apjoms;
- starpdokumentu diametru pretrunas, piemēram OD90 pret D110, D75 pret D50, OD75 pret OD110, ja tās attiecas uz vienu un to pašu elementu;
- SA/plānā paredzēts U1 pievads, bet specifikācijā nav skaidri redzamas U1 materiālu/montāžas pozīcijas;
- normatīvu sarakstu neatbilstības starp dokumentiem, ja tās ir skaidri redzamas;
- saistīto projektu saraksta neatbilstības starp dokumentiem;
- revīzijas/apjomu aktualizācijas jautājumi pēc izmaiņām, ja dokumentos redzama neatbilstība;
- A15 slodzes klases risks akām transporta/slodzes zonā, ja tekstā ir pamats šādam jautājumam;
- 3 gab. D110 vienvirziena vārsti bez skaidras piesaistes akām/ievadiem;
- formulējumi specifikācijā, kas neatbilst faktiskajam darbu apjomam;
- ārējās ugunsdzēsības vai citu būtisku risinājumu atkarība no saistītā projekta, ja nav skaidrs risinājums.

PRIORITĀTE 6 — ziņot, ja slieksnis ir 6 vai zemāks:
- būtiskas iekšējas nekonsekvences dokumentā, ja tās var radīt praktisku pārpratumu;
- būtiskas tulkojuma neprecizitātes, kas var mainīt tehnisko nozīmi;
- specifikācijas un rasējuma apzīmējumu nesakritības, ja tās ir pietiekami drošas;
- sistēmas, markas, diametra, materiāla, spiediena klases vai daudzuma neatbilstības, ja tās ietekmē būvdarbu apjomu vai risinājumu;
- būtiskas specifikācijas nepilnības, piemēram pozīcijas bez skaidras piesaistes sistēmai, markai vai elementam;
- būtiski audita jautājumi, kas var ietekmēt apjomus, izbūvi, ekspluatāciju vai risinājumu savstarpējo saskaņotību.

PRIORITĀTE 3–4 — ziņot tikai zema sliekšņa gadījumā:
- C2-02 / C2-2 / C 2-2 tipa objekta apzīmējumu atšķirības, ja nav pierādīts, ka tās rada tehnisku vai juridisku pretrunu;
- aprēķina apzīmējumu noformējuma problēmas;
- nebūtiskas gramatikas kļūdas;
- neskaidri formulējumi, kas nerada būtisku tehnisku risku;
- tehnisku terminu rakstības pārbaudes jautājumi, ja AI nav pilnīgi pārliecināts par pareizo formu;
- nelielas terminoloģijas nekonsekvences, ja tās nerada praktisku risku.

PRIORITĀTE 1–2 — ziņot tikai ļoti jutīgā režīmā:
- liekas pēdiņas;
- nelieli noformējuma jautājumi;
- neskaidri saīsinājumi bez būtiska riska;
- sīkas stila vai redakcionālas piezīmes;
- tekstuālas nianses, kas nav būtiskas būvprojekta auditam.

PRIORITĀTE 0 — neziņot:
- datumu atšķirības, ja nav skaidra pamata tās uzskatīt par kļūdu;
- revīzijas/titullauka sīkumi, ja nav pierādīta ietekme;
- stila jautājumi;
- pieņemami nozares saīsinājumi;
- projekta vai sadaļas kodi, ja tie ir saprotami būvprojekta kontekstā;
- kļūdas, kas nav pietiekami pamatotas ar dokumentos redzamu tekstu.

ĪPAŠI IZŅĒMUMI, KURUS NEDRĪKST ZIŅOT PIE SLIEKŠŅA 6 VAI AUGSTĀKA:
- Neatzīmē “ŪKT BP” kā neskaidru saīsinājumu. Tas ir pieņemams apzīmējums: ŪKT sadaļas būvprojekts.
- Neatzīmē C2-02 / C2-2 / C 2-2 kā priority 6 vai 10, ja nav pierādīts, ka tas rada būtisku tehnisku vai juridisku pretrunu.
- Neatzīmē liekas pēdiņas kā priority 6 vai 10.
- Neatzīmē sīkus noformējuma jautājumus kā priority 6 vai 10.
- Neatzīmē tehniska termina pareizrakstību kā drošu kļūdu, ja AI nav pilnīgi pārliecināts par pareizo formu.
- Neatzīmē “Skataka” / “Skateka” tipa gadījumus kā drošu kļūdu, ja dokumentā nav cita pierādījuma pareizajai formai.
- Ja vārds var būt specifikācijas pozīcijas nosaukums, ražotāja apzīmējums, tehnisks termins vai projekta specifisks termins, to nedrīkst labot pašpārliecināti.
- Ja AI nav pārliecināts par pareizo rakstību, šādu piezīmi drīkst dot tikai kā priority 3 vai zemāku.
- Neatzīmē vispārzināmus sadaļu, projekta vai dokumentācijas apzīmējumus kā neskaidrus, ja tie ir saprotami būvprojekta kontekstā.
- Neatzīmē virsrakstus, tabulu šūnas vai rasējuma titullaukus kā nepabeigtas frāzes tikai tāpēc, ka tie nav pilni teikumi.

ĪPAŠI JĀZIŅO PIE SLIEKŠŅA 6 VAI AUGSTĀKA:
- skaidras adreses kļūdas, piemēram ANREJOSTAS pret ANDREJOSTAS;
- skaidras drukas kļūdas parastos vārdos;
- LV/EN tulkojuma kļūdas, kas maina tehnisko nozīmi;
- diametru, materiālu, spiediena klases, stingrības klases, daudzumu vai marku pretrunas starp SA, rasējumiem un specifikāciju;
- tukšas, izlaistas vai būtiski nepilnīgas specifikācijas pozīcijas;
- konkrēti apjomi bez skaidras piesaistes, ja tas var ietekmēt būvdarbu apjomu;
- U1/K1/K2/K3 sistēmu būtiskas nesakritības;
- būtiski jautājumi par vienvirziena vārstiem, akām, teknēm, lūkām, tauku atsūknēšanu, ūdensvada pievadu un ārējiem tīkliem;
- normatīvu vai saistīto projektu sarakstu neatbilstības, ja tās var ietekmēt dokumentācijas saskaņotību.

DOKUMENTU TIPU INTERPRETĀCIJA:
1. Skaidrojošais apraksts:
   Parasti nosaukumā ir explanatory note, description, apraksts, skaidrojoš.
   Meklē sistēmu aprakstus, prasības, diametrus, materiālus, apjomus, aprēķinus,
   normatīvus, saistītos projektus un atsauces uz rasējumiem/specifikāciju.

2. Rasējums / shēma / plāns / griezums:
   Parasti nosaukumā ir scheme, layout, section, plan, floor, general data, site plan, drawing, rasējums, plāns, griezums, RA.
   Meklē titullaukus, rasējuma numurus, nosaukumus, revīzijas, datumus, mērogus,
   leģendas, tīklu apzīmējumus, marķējumus, diametrus, materiālus, spiediena/stingrības klases.
   Rasējumos īsi teksta bloki var būt pilnvērtīgi tehniski fakti.

3. Specifikācija / apjomu tabula:
   Parasti nosaukumā ir specification, specifikācija, apjomi, works, BOQ, MS.
   Meklē pozīcijas, pozīciju numurus, markas, daudzumus, mērvienības, LV/EN aprakstus,
   diametrus, materiālus, spiediena/stingrības klases un tukšas/izlaistas pozīcijas.

GRAMATIKAS UN VALODAS NOTEIKUMI:
- Meklē tikai drošas, acīmredzamas kļūdas.
- Neatzīmē stila izvēles.
- Neatzīmē pieņemamus nozares terminus.
- Neatzīmē vietvārdus, īpašvārdus, uzņēmumu nosaukumus vai projekta specifiskus nosaukumus, ja nav pilnīgas pārliecības.
- Neatzīmē locījumu kā kļūdu, ja tas var būt gramatiski pareizs konkrētajā kontekstā.
- Neatzīmē tehniskus kodus kā valodas kļūdas.
- Tehniskā termina labojumu drīkst dot tikai tad, ja pareizā forma ir nepārprotama.

TULKOJUMU NOTEIKUMI:
- Pārbaudi LV/EN pārus rasējumu titullaukos, leģendās, vispārīgajos rādītājos un specifikācijās.
- Atzīmē tikai tad, ja tulkojums ir acīmredzami nepareizs vai maina tehnisko nozīmi.
- Neatzīmē pieņemamus variantus:
  VISPĀRĪGIE RĀDĪTĀJI / GENERAL DATA,
  RASĒJUMA NR. / SHEET ID,
  MĒROGS / SCALE,
  DATUMS / DATE,
  IZMAIŅA / REVISION,
  STĀVS / FLOOR.
- Ja tulkojums ir tikai diskutabls, bet nozīme ir saprotama, neliec piezīmi pie sliekšņa 6 vai augstāka.

TEHNISKO PARAMETRU NOTEIKUMI:
- PN10/PN16 ir spiediena klase.
- SN4/SN8/SN16 ir stingrības klase.
- PN10 pret SN8 NAV pretruna, jo tie nav viena parametru grupa.
- K2 pret K2-T1 NAV automātiska pretruna. K2-T1 var būt K2 sistēmas apakšmezgls.
- K2 pret K3 ziņo tikai tad, ja tie attiecas uz vienu un to pašu pozīciju/elementu.
- PE pret PP/PVC ziņo tikai tad, ja skaidrs, ka tie attiecas uz vienu un to pašu elementu.
- D110 pret D160 ziņo tikai tad, ja skaidrs, ka tie attiecas uz vienu un to pašu elementu.
- Ja viens dokuments kaut ko nemin, tā nav automātiska pretruna.
- Tomēr, ja skaidrojošajā aprakstā un rasējumā ir būtisks elements, bet specifikācijā nav skaidri redzamas atbilstošas pozīcijas, drīkst ziņot kā audita jautājumu.

DATUMU NOTEIKUMI:
- Datuma formāts dd.mm.yyyy vai dd.mm.yyyy. ir pieņemams Latvijā.
- Nākotnes datums pats par sevi nav kļūda.
- Neatzīmē datumu atšķirības, ja nav skaidra pierādījuma, ka tās rada pretrunu.
- Atzīmē tikai acīmredzamus vietturus vai bojātus datumus, piemēram dd.mm.gggg, XX.XX.XXXX, 00.00.0000.

NORMATĪVU UN SAISTĪTO PROJEKTU NOTEIKUMI:
- Salīdzini normatīvu sarakstus starp dokumentiem tikai tad, ja tie ir skaidri redzami tekstā.
- Ziņo, ja vienā dokumentā ir novecojis vai atšķirīgs normatīvs, bet citā dokumentā tas pats jautājums norādīts citādi.
- Ziņo, ja saistīto projektu saraksti dažādos dokumentos būtiski atšķiras.
- Neizdomā normatīvu aktualitāti, ja tā nav redzama tekstā.

SPECIFIKĀCIJU NOTEIKUMI:
- Meklē tukšas vai izlaistas pozīcijas.
- Meklē pozīcijas bez skaidras markas/sistēmas, ja tāda nepieciešama.
- Meklē LV/EN apraksta neatbilstības vienā specifikācijas rindā.
- Meklē konkrētu apjomu pozīcijas ar tekstu “nepieciešamības gadījumā”, ja nav skaidras piemērošanas loģikas.
- Meklē daudzumus bez piesaistes konkrētam elementam, piemēram 3 gab. vārsti bez akas/ievada norādes.
- Meklē apjomus, piemēram 90 m, bez skaidras piesaistes trases posmiem, ja tas var ietekmēt būvdarbu apjomu.

ATBILDES FORMĀTS:
Atbildi tikai JSON formātā.
JSON jābūt masīvam ar objektiem.
Ja nav drošu piezīmju, atgriez tukšu masīvu [].
Neizmanto Markdown.
Neievieto atbildi ```json blokā.

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

category vērtības izvēlies pēc būtības, piemēram:
- grammar
- spelling
- translation
- address
- drawing_title
- specification_position
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

Piemērs:
[
  {{
    "include_in_pdf": true,
    "priority": 10,
    "issue_type": "cross_document",
    "category": "diameter",
    "source_file": "fails.pdf",
    "page": 1,
    "block_id": 123,
    "source_text": "teksta fragments",
    "comment": "Kas ir problēma",
    "suggestion": "Ko lietotājam vajadzētu pārbaudīt vai labot",
    "related_files": "fails_A.pdf; fails_B.pdf",
    "confidence": 0.95
  }}
]

Atgriez tikai piezīmes ar priority >= {priority_threshold}.
Confidence norādi kā skaitli no 0 līdz 1.
"""
