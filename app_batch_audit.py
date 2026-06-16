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


def get_openai_client():
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        st.error("Nav atrasta OPENAI_API_KEY vērtība Streamlit Secrets sadaļā.")
        return None
    return OpenAI(api_key=api_key)


def detect_document_type(file_name):
    name = file_name.lower()
    explanatory_keywords = ["explanatory note", "explanatory", "description", "apraksts", "skaidrojo", "td_", "_td_"]
    specification_keywords = ["specification", "specifik", "apjomi", "works", "boq", "bill of quantities", "tame", "tāme", "ms_", "_ms_"]
    drawing_keywords = ["scheme", "layout", "section", "plan", "floor", "general data", "site plan", "drawing", "rasēj", "rasej", "stāva", "stava", "griezums", "shēma", "shema", "plāns", "plans", "ra_", "_ra_"]
    if any(k in name for k in explanatory_keywords):
        return "explanatory_note"
    if any(k in name for k in specification_keywords):
        return "specification"
    if any(k in name for k in drawing_keywords):
        return "drawing"
    return "unknown"


def document_type_label(document_type):
    return {
        "explanatory_note": "Skaidrojošais apraksts",
        "drawing": "Rasējums / shēma / plāns / griezums",
        "specification": "Specifikācija / apjomu tabula",
        "unknown": "Neatpazīts dokumenta tips",
    }.get(document_type, "Neatpazīts dokumenta tips")


def extract_pdf_text(file_bytes, file_name, document_type):
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    rows = []
    local_block_id = 0
    for page_index, page in enumerate(doc):
        blocks = page.get_text("blocks")
        for block in blocks:
            x0, y0, x1, y1, text, block_no, block_type = block
            clean_text = str(text).strip()
            if clean_text:
                rows.append({
                    "source_file": file_name,
                    "document_type": document_type,
                    "block_id": local_block_id,
                    "page": page_index + 1,
                    "x0": round(x0, 2),
                    "y0": round(y0, 2),
                    "x1": round(x1, 2),
                    "y1": round(y1, 2),
                    "text": clean_text,
                })
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
        "include_in_pdf", "priority", "issue_type", "category", "source_file", "page", "block_id",
        "source_text", "comment", "suggestion", "related_files", "confidence"
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


def local_document_audit(client, file_df, priority_threshold, chunk_size):
    source_file = file_df["source_file"].iloc[0]
    document_type = file_df["document_type"].iloc[0]
    all_issues = []
    chunks = []
    for start in range(0, len(file_df), chunk_size):
        end = min(start + chunk_size, len(file_df))
        chunks.append(file_df.iloc[start:end])
    for chunk_index, chunk_df in enumerate(chunks, start=1):
        blocks_text = build_blocks_text(chunk_df)
        prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta dokumentācijas auditors Latvijā.

Šis ir LOKĀLAIS audits vienam dokumentam.
Meklē kļūdas tikai šajā dokumentā:
{source_file}

Dokumenta tips:
{document_type}

Audita daļa:
{chunk_index} no {len(chunks)}

{audit_rules_text(priority_threshold)}

Šajā lokālajā auditā īpaši meklē:
1. Gramatikas un drukas kļūdas.
2. Tulkojuma kļūdas LV/EN tekstos.
3. Tukšas vai izlaistas specifikācijas pozīcijas.
4. Neskaidras specifikācijas rindas.
5. Nepabeigtus aprēķinus.
6. Acīmredzamas summu kļūdas.
7. Vietturus vai nepabeigtus teksta laukus.
8. Iekšējas pretrunas vienā dokumentā.
9. Normatīvu vai saistīto projektu sarakstu problēmas, ja tās redzamas šajā dokumentā.

Ja dokumenta tips ir specification:
- īpaši meklē pozīciju numerācijas caurumus;
- tukšas pozīcijas;
- trūkstošas markas;
- LV/EN rindas neatbilstības;
- daudzumu vai mērvienību neskaidrības;
- pozīcijas ar “nepieciešamības gadījumā”, kur dots konkrēts apjoms.

Ja dokumenta tips ir drawing:
- īpaši meklē nepareizus LV/EN titullauku tulkojumus;
- leģendu tulkojuma kļūdas;
- rasējuma nosaukuma neatbilstības;
- acīmredzamus vietturus vai drukas kļūdas.

Teksta bloki auditam:
{blocks_text}
"""
        issues = call_ai_json(client, prompt, f"AI neatgrieza derīgu JSON lokālajam auditam failā {source_file}.")
        all_issues.extend(issues)
    return normalize_issues(all_issues, default_source_file=source_file, default_issue_scope="local_document_audit")


def specification_structure_audit(client, all_blocks_df, priority_threshold, max_blocks_per_spec):
    spec_df = all_blocks_df[all_blocks_df["document_type"] == "specification"].copy()
    if spec_df.empty:
        return pd.DataFrame()
    all_issues = []
    for source_file, file_df in spec_df.groupby("source_file"):
        selected = file_df.head(max_blocks_per_spec)
        blocks_text = build_blocks_text(selected)
        prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta specifikāciju auditors Latvijā.

Šis audits ir paredzēts tikai specifikācijas/apjomu tabulas struktūrai.

Fails:
{source_file}

{audit_rules_text(priority_threshold)}

Īpaši meklē:
1. Tukšas pozīcijas.
2. Izlaistus pozīciju numurus.
3. Pozīcijas bez markas, ja marka ir nepieciešama.
4. Pozīcijas, kur LV un EN apraksti atsaucas uz dažādām sistēmām vai marķējumiem.
5. Pozīcijas, kur norādīts konkrēts apjoms, bet tekstā rakstīts “nepieciešamības gadījumā”.
6. Vienvirziena vārstus, lūkas, teknes, akas, caurules bez skaidras piesaistes.
7. Diametrus/materiālus/spiediena klases, kas specifikācijā izskatās pretrunīgi.
8. Redzamas drukas kļūdas specifikācijas rindās.

Atceries:
- PN un SN nav viens un tas pats.
- K2 un K2-T1 nav automātiska pretruna.
- Ziņo tikai par praktiski pārbaudāmām problēmām.

Specifikācijas teksta bloki:
{blocks_text}
"""
        issues = call_ai_json(client, prompt, f"AI neatgrieza derīgu JSON specifikācijas auditam failā {source_file}.")
        all_issues.extend(issues)
    return normalize_issues(all_issues, default_issue_scope="specification_structure_audit")


def build_cross_document_summary(all_blocks_df, max_blocks_per_file):
    parts = []
    keywords = [
        "u1", "k1", "k2", "k3", "od", "d110", "d160", "d75", "d50", "dn", "ø",
        "pn10", "pn16", "sn8", "sn4", "pe", "pe100", "pvc", "pp", "gab", "90 m", "3 gab",
        "vienvirziena", "vārst", "varst", "tekne", "aka", "lūka", "luka", "normat", "lbn", "mk ",
        "saistīt", "saistit", "ugunsdzēs", "ugunsdzes", "tauku", "atsūkn", "atsukn",
        "andrejostas", "anrejostas", "specifik", "apjomi", "izmaiņa", "izmaina", "revision"
    ]
    for source_file, file_df in all_blocks_df.groupby("source_file"):
        document_type = file_df["document_type"].iloc[0]
        selected = file_df.head(max_blocks_per_file)
        parts.append(f"\n=== DOKUMENTS: {source_file} | TIPS: {document_type} | BLOKI: {len(selected)} ===")
        for _, row in selected.iterrows():
            lower = str(row["text"]).lower()
            if any(k in lower for k in keywords):
                parts.append(
                    f"[source_file={row['source_file']}] "
                    f"[document_type={row['document_type']}] "
                    f"[page={row['page']}] "
                    f"[block_id={row['block_id']}] "
                    f"{row['text']}"
                )
    return "\n".join(parts)


def cross_document_audit(client, all_blocks_df, priority_threshold, max_blocks_per_file):
    summary_text = build_cross_document_summary(all_blocks_df, max_blocks_per_file)
    if not summary_text.strip():
        return pd.DataFrame()
    prompt = f"""
Tu esi ļoti piesardzīgs būvprojekta starpdokumentu auditors Latvijā.

Šis ir STARPDOUMENTU audits visam augšupielādēto PDF komplektam.
Salīdzini skaidrojošos aprakstus, rasējumus un specifikācijas savā starpā.

{audit_rules_text(priority_threshold)}

Šajā starpdokumentu auditā īpaši meklē:
1. SA pret rasējumiem:
   - vai diametri sakrīt;
   - vai materiāli sakrīt;
   - vai sistēmu apzīmējumi sakrīt;
   - vai aprakstītie elementi ir redzami rasējumu/specifikāciju tekstā.

2. Rasējumi pret specifikāciju:
   - D110 pret D160;
   - D75 pret D50;
   - OD75 pret OD110;
   - PE pret PP/PVC;
   - PN10 pret PN16;
   - K2 pret K3, ja attiecas uz vienu pozīciju;
   - teknes, akas, vārsti, lūkas, caurules.

3. U1/K1/K2/K3:
   - U1 ūdensvada pievads;
   - K1 tauku atsūknēšanas caurule;
   - K2/K3 teknes;
   - K3 tīkli;
   - vienvirziena vārsti.

4. Normatīvi un saistītie projekti:
   - normatīvu sarakstu atšķirības;
   - saistīto projektu sarakstu atšķirības;
   - ārējās ugunsdzēsības atkarība no saistītā projekta.

5. Apjomi:
   - 3 gab. elementi bez piesaistes;
   - 90 m pozīcijas bez skaidras trases;
   - “nepieciešamības gadījumā” ar konkrētu apjomu.

Atceries:
- Ja viens dokuments kaut ko nemin, tā nav automātiska pretruna.
- Tomēr, ja skaidrojošajā aprakstā un rasējumā ir būtisks elements, bet specifikācijā nav skaidri redzamas atbilstošas pozīcijas, drīkst ziņot kā audita jautājumu.
- Piezīmi piesaisti tam failam/blokam, kur problēma vislabāk redzama.

Starpdokumentu salīdzināšanai atlasītie teksta bloki:
{summary_text}
"""
    issues = call_ai_json(client, prompt, "AI neatgrieza derīgu JSON starpdokumentu auditam.")
    return normalize_issues(issues, default_issue_scope="cross_document_audit")


def combine_and_filter_issues(issue_frames, priority_threshold):
    frames = [df for df in issue_frames if df is not None and not df.empty]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined["priority"] = pd.to_numeric(combined["priority"], errors="coerce").fillna(0).astype(int)
    combined = combined[combined["priority"] >= priority_threshold].copy()
    combined["source_file"] = combined["source_file"].astype(str)
    combined["source_text"] = combined["source_text"].astype(str)
    combined["comment"] = combined["comment"].astype(str)
    combined = combined.drop_duplicates(
        subset=["source_file", "page", "block_id", "issue_type", "category", "source_text", "comment"],
        keep="first",
    )
    combined = combined.sort_values(by=["priority", "source_file", "page"], ascending=[False, True, True]).reset_index(drop=True)
    return combined


def merge_issue_coordinates(issues_df, all_blocks_df):
    if issues_df.empty:
        return issues_df
    issues_df = issues_df.copy()
    issues_df["block_id"] = pd.to_numeric(issues_df["block_id"], errors="coerce")
    issues_df["page"] = pd.to_numeric(issues_df["page"], errors="coerce")
    merged = issues_df.merge(
        all_blocks_df[["source_file", "block_id", "page", "x0", "y0", "x1", "y1", "text", "document_type"]],
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
    square_annot.set_info(title="AI būvprojekta audits", content=annotation_text)
    square_annot.set_colors(stroke=(1, 0, 0))
    square_annot.set_border(width=1)
    square_annot.update()
    note_point = fitz.Point(float(x1) + 5, float(y0))
    text_annot = page.add_text_annot(note_point, annotation_text)
    text_annot.set_info(title="AI būvprojekta audits", content=annotation_text)
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
            f"Audita posms: {issue.get('audit_scope', '')}\n"
            f"Tips: {issue.get('issue_type', '')}\n"
            f"Kategorija: {issue.get('category', '')}\n"
            f"Ticamība: {issue.get('confidence', '')}\n\n"
            f"Atrasts teksts:\n{issue.get('source_text', '')}\n\n"
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
            annotated_pdf = create_annotated_pdf(uploaded_file_bytes[source_file], file_issues_df)
            safe_name = source_file.replace("/", "_").replace("\\", "_")
            zf.writestr(f"annotated_{safe_name}", annotated_pdf.getvalue())
    zip_buffer.seek(0)
    return zip_buffer


# ----------------------------
# UI sākas šeit. Ja šī daļa nav failā, upload lauks neparādīsies.
# ----------------------------

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

chunk_size = st.number_input(
    "Lokālā audita porcijas izmērs",
    min_value=50,
    max_value=400,
    value=200,
    step=50,
    help="Mazāka porcija nozīmē rūpīgāku, bet lēnāku auditu.",
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
        text_df = extract_pdf_text(file_bytes=file_bytes, file_name=file_name, document_type=document_type)
        all_block_frames.append(text_df)
        file_summary_rows.append({
            "file_name": file_name,
            "document_type": document_type,
            "document_type_label": document_type_label(document_type),
            "text_blocks": len(text_df),
        })

    summary_df = pd.DataFrame(file_summary_rows)
    st.dataframe(summary_df, use_container_width=True)

    all_blocks_df = pd.concat(all_block_frames, ignore_index=True) if all_block_frames else pd.DataFrame()
    st.success(f"Kopā izvilkti {len(all_blocks_df)} teksta bloki no {len(uploaded_files)} PDF failiem.")

    with st.expander("Apskatīt izvilktos teksta blokus"):
        st.dataframe(all_blocks_df, use_container_width=True)

    if st.button("Palaist AI auditu"):
        client = get_openai_client()
        if client is not None:
            progress = st.progress(0)
            status = st.empty()
            issue_frames = []
            grouped_files = list(all_blocks_df.groupby("source_file"))
            total_steps = len(grouped_files) + 2
            current_step = 0

            for source_file, file_df in grouped_files:
                current_step += 1
                status.write(f"Lokālais audits: {source_file}")
                selected_file_df = file_df.head(max_blocks_per_file)
                with st.spinner(f"AI lokāli auditē {source_file}..."):
                    local_issues_df = local_document_audit(
                        client=client,
                        file_df=selected_file_df,
                        priority_threshold=priority_threshold,
                        chunk_size=chunk_size,
                    )
                if not local_issues_df.empty:
                    issue_frames.append(local_issues_df)
                progress.progress(current_step / total_steps)

            current_step += 1
            status.write("Specifikāciju strukturālais audits...")
            with st.spinner("AI pārbauda specifikāciju struktūru..."):
                spec_issues_df = specification_structure_audit(
                    client=client,
                    all_blocks_df=all_blocks_df,
                    priority_threshold=priority_threshold,
                    max_blocks_per_spec=max_blocks_per_file,
                )
            if not spec_issues_df.empty:
                issue_frames.append(spec_issues_df)
            progress.progress(current_step / total_steps)

            current_step += 1
            status.write("Starpdokumentu audits...")
            with st.spinner("AI salīdzina dokumentus savā starpā..."):
                cross_issues_df = cross_document_audit(
                    client=client,
                    all_blocks_df=all_blocks_df,
                    priority_threshold=priority_threshold,
                    max_blocks_per_file=max_blocks_per_file,
                )
            if not cross_issues_df.empty:
                issue_frames.append(cross_issues_df)
            progress.progress(current_step / total_steps)
            status.write("Audits pabeigts.")

            combined_issues_df = combine_and_filter_issues(issue_frames=issue_frames, priority_threshold=priority_threshold)
            combined_issues_df = merge_issue_coordinates(combined_issues_df, all_blocks_df)
            st.session_state["batch_audit_issues_df"] = combined_issues_df
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
            st.info(f"PDF anotācijām atlasītas {len(approved_issues_df)} no {len(edited_issues_df)} piezīmēm.")
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
