import io
import json
import re
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
import pandas as pd
import streamlit as st
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI


st.set_page_config(page_title="BP izejas prasību tests", layout="wide")

st.title("BP izejas dokumentu prasību izvilkšanas tests")

st.write(
    "Šī testa aplikācija nolasa `01_VD` sadaļu no Google Drive, atrod izejas dokumentus "
    "un ar AI palīdzību mēģina izvilkt pārbaudāmas projekta prasības."
)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
PDF_MIME_TYPE = "application/pdf"


# -----------------------------
# Google Drive pieslēgums
# -----------------------------

def get_drive_service():
    service_account_json = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON")

    if not service_account_json:
        raise ValueError("Secrets nav atrasts GOOGLE_SERVICE_ACCOUNT_JSON.")

    service_account_info = json.loads(service_account_json)

    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=["https://www.googleapis.com/auth/drive"],
    )

    return build("drive", "v3", credentials=credentials)


def list_folder_items(service, folder_id: str) -> List[Dict[str, Any]]:
    query = f"'{folder_id}' in parents and trashed = false"

    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name, mimeType, size, modifiedTime)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )

    return results.get("files", [])


def list_items_recursive(service, folder_id: str, parent_path: str = "") -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    items = list_folder_items(service, folder_id)

    for item in items:
        item_name = item.get("name", "")
        item_path = f"{parent_path}/{item_name}" if parent_path else item_name
        is_folder = item.get("mimeType") == FOLDER_MIME_TYPE

        row = {
            "name": item_name,
            "path": item_path,
            "id": item.get("id"),
            "mimeType": item.get("mimeType"),
            "size": item.get("size", ""),
            "modifiedTime": item.get("modifiedTime", ""),
            "is_folder": is_folder,
        }

        rows.append(row)

        if is_folder:
            rows.extend(
                list_items_recursive(
                    service=service,
                    folder_id=item.get("id"),
                    parent_path=item_path,
                )
            )

    return rows


def find_folder_by_name(items: List[Dict[str, Any]], folder_name: str) -> Optional[Dict[str, Any]]:
    target = folder_name.strip().lower()

    for item in items:
        if (
            item.get("mimeType") == FOLDER_MIME_TYPE
            and item.get("name", "").strip().lower() == target
        ):
            return item

    return None


def download_drive_file_bytes(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    file_buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(file_buffer, request)

    done = False
    while not done:
        _, done = downloader.next_chunk()

    file_buffer.seek(0)
    return file_buffer.read()


def download_text_file(service, file_id: str) -> str:
    return download_drive_file_bytes(service, file_id).decode("utf-8", errors="replace")


# -----------------------------
# Prompt mapes apstrāde
# -----------------------------

def find_prompt_file(prompt_items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for item in prompt_items:
        if item.get("name") == "universal_bp_audit_prompt.txt":
            return item
    return None


def load_universal_prompt(service, prompt_folder_id: str) -> str:
    prompt_items = list_folder_items(service, prompt_folder_id)
    prompt_file = find_prompt_file(prompt_items)

    if not prompt_file:
        return ""

    return download_text_file(service, prompt_file.get("id"))


# -----------------------------
# 01_VD dokumentu klasifikācija
# -----------------------------

def classify_source_document(row: Dict[str, Any]) -> str:
    name = str(row.get("name", "")).lower()
    path = str(row.get("path", "")).lower()
    text = f"{path} {name}"

    if "design" in text or "brief" in text or "uzdev" in text:
        return "design_brief"

    if "geotechnical" in text or "gi_" in text or "ģeotehn" in text or "geotehn" in text:
        return "geotechnical"

    if "hydrogeology" in text or "hgi" in text or "hidro" in text:
        return "hydrogeology"

    if "tree" in text or "assessment" in text or "koku" in text or "dendro" in text:
        return "tree_assessment"

    if "topo" in text or "topogr" in text:
        return "topography"

    if "photofixation" in text or "photo" in text or "foto" in text:
        return "photofixation"

    return "other_source_data"


def get_source_documents(service, input_folder_id: str) -> pd.DataFrame:
    input_items = list_folder_items(service, input_folder_id)
    vd_folder = find_folder_by_name(input_items, "01_VD")

    if not vd_folder:
        raise ValueError("Nav atrasta `01_VD` mape zem C2-3_TD.")

    vd_rows = list_items_recursive(
        service=service,
        folder_id=vd_folder.get("id"),
        parent_path="01_VD",
    )

    if not vd_rows:
        return pd.DataFrame()

    vd_df = pd.DataFrame(vd_rows)

    docs_df = vd_df[
        (vd_df["is_folder"] == False)
        & (
            vd_df["mimeType"].str.contains("pdf", case=False, na=False)
            | vd_df["mimeType"].str.contains("document", case=False, na=False)
            | vd_df["mimeType"].str.contains("spreadsheet", case=False, na=False)
            | vd_df["mimeType"].str.contains("text", case=False, na=False)
        )
    ].copy()

    if docs_df.empty:
        return docs_df

    docs_df["source_document_type"] = docs_df.apply(
        lambda row: classify_source_document(row.to_dict()),
        axis=1,
    )

    return docs_df


# -----------------------------
# PDF teksta izvilkšana
# -----------------------------

def extract_pdf_text_blocks(pdf_bytes: bytes, max_pages: int = 20) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        page_count = min(len(doc), max_pages)

        for page_index in range(page_count):
            page = doc[page_index]
            blocks = page.get_text("blocks")

            for block_index, block in enumerate(blocks):
                if len(block) < 5:
                    continue

                x0, y0, x1, y1, text = block[:5]
                clean_text = re.sub(r"\s+", " ", str(text)).strip()

                if not clean_text:
                    continue

                rows.append(
                    {
                        "page": page_index + 1,
                        "block_id": block_index,
                        "x0": round(float(x0), 2),
                        "y0": round(float(y0), 2),
                        "x1": round(float(x1), 2),
                        "y1": round(float(y1), 2),
                        "text": clean_text,
                    }
                )

    return pd.DataFrame(rows)


def prepare_text_for_ai(blocks_df: pd.DataFrame, max_blocks: int) -> str:
    if blocks_df.empty:
        return ""

    selected = blocks_df.head(max_blocks)

    chunks = []
    for _, row in selected.iterrows():
        chunks.append(
            f"[page={row['page']} block_id={row['block_id']}] {row['text']}"
        )

    return "\n".join(chunks)


# -----------------------------
# AI prasību izvilkšana
# -----------------------------

def get_openai_client() -> OpenAI:
    api_key = st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("Secrets nav atrasts OPENAI_API_KEY.")
    return OpenAI(api_key=api_key)


def build_requirement_extraction_prompt(
    universal_prompt: str,
    source_document_type: str,
    source_file: str,
    source_text: str,
) -> str:
    return f"""
Tu esi būvprojekta audita palīgs Latvijā.

Tavs uzdevums ir no izejas dokumenta teksta izvilkt TIKAI pārbaudāmas prasības, nosacījumus un faktus,
pret kuriem vēlāk var pārbaudīt būvprojekta sadaļas.

Dokumenta tips: {source_document_type}
Dokumenta fails: {source_file}

Izmanto šo audita principu kontekstu, bet neatkārto to atbildē:
{universal_prompt[:6000]}

KO IZVILKT:
- Pasūtītāja prasības no Design Brief.
- Nosacījuma prasības: "ja X, tad jāparedz Y".
- Ģeotehniskās prasības: gruntsūdens, pamati, būvbedre, atsūknēšana, drenāža, agresivitāte, slāņi.
- Hidroģeoloģijas prasības: ūdens līmeņi, pazemināšana, atsūknēšana, monitorings.
- Koku/apstādījumu prasības: saglabājamie koki, aizsargzonas, cērtamie koki, dendrologa nosacījumi.
- Topogrāfijas/esošo tīklu prasības: esošie pieslēgumi, aizsargjoslas, esošie tīkli, augstuma atzīmes.
- Konkrētas prasības, kas vēlāk jāpārbauda AR, BK, GP, DOP, UKT, UK, EL, AVK, SM, UAS, UATS, ESS vai citās sadaļās.

KO NEIZVILKT:
- Vispārīgus ievadtekstus bez pārbaudāmas prasības.
- Reklāmas vai aprakstošu tekstu.
- Sīkas noformējuma lietas.
- Prasības, kuru tekstā nav pietiekama pamata.
- Vienkārši dokumenta nosaukumus bez prasības.
- Pārāk plašus secinājumus, ja nav konkrēta pārbaudāma fakta.

ATBILDES FORMĀTS:
Atbildi tikai kā JSON masīvu.
Ja nav prasību, atgriez [].

Katram objektam jābūt šādiem laukiem:
- requirement_id: īss ID, piemēram "REQ-001"
- source_document_type
- source_file
- page
- block_id
- requirement_type: viens no ["design_brief", "geotechnical", "hydrogeology", "tree_assessment", "topography", "technical_condition", "other"]
- requirement: īsa pārbaudāma prasība latviski
- condition: ja prasība ir nosacījuma veida, ieraksti nosacījumu; citādi tukša virkne
- applies_to_sections: saraksts ar sadaļu saīsinājumiem, piemēram ["AR", "BK", "UK", "EL"]
- priority: skaitlis 1-10, kur 10 ir būtiska prasība
- verification_hint: īss teksts, kā vēlāk pārbaudīt BP atbilstību
- source_text: īss citāts vai teksta fragments no dokumenta
- confidence: skaitlis no 0 līdz 1

DOKUMENTA TEKSTS:
{source_text}
"""


def parse_json_array(raw_text: str) -> List[Dict[str, Any]]:
    text = raw_text.strip()

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()

    start = text.find("[")
    end = text.rfind("]")

    if start == -1 or end == -1 or end <= start:
        raise ValueError("AI neatgrieza JSON masīvu.")

    json_text = text[start : end + 1]
    data = json.loads(json_text)

    if not isinstance(data, list):
        raise ValueError("AI atbilde nav JSON masīvs.")

    return data


def extract_requirements_with_ai(
    client: OpenAI,
    universal_prompt: str,
    source_document_type: str,
    source_file: str,
    source_text: str,
    model: str,
) -> List[Dict[str, Any]]:
    prompt = build_requirement_extraction_prompt(
        universal_prompt=universal_prompt,
        source_document_type=source_document_type,
        source_file=source_file,
        source_text=source_text,
    )

    response = client.chat.completions.create(
        model=model,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": "Tu atbildi tikai derīgā JSON masīvā. Nekādu paskaidrojumu ārpus JSON.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
    )

    raw = response.choices[0].message.content or ""
    return parse_json_array(raw)


# -----------------------------
# Streamlit UI
# -----------------------------

input_folder_id = st.secrets.get("GOOGLE_DRIVE_INPUT_FOLDER_ID")
prompt_folder_id = st.secrets.get("GOOGLE_DRIVE_PROMPT_FOLDER_ID")

st.markdown("## 1. Konfigurācija")

st.write("Input folder ID:", input_folder_id)
st.write("Prompt folder ID:", prompt_folder_id)

model = st.selectbox(
    "AI modelis",
    options=["gpt-4.1-mini", "gpt-4.1"],
    index=0,
)

max_pages_per_pdf = st.number_input(
    "Maksimālais lapu skaits no viena PDF",
    min_value=1,
    max_value=100,
    value=20,
    step=1,
)

max_blocks_per_pdf = st.number_input(
    "Maksimālais teksta bloku skaits no viena PDF AI analīzei",
    min_value=20,
    max_value=1000,
    value=250,
    step=10,
)

st.info(
    "Sākumā ieteicams testēt tikai 1–3 failus, piemēram Design Brief galveno PDF "
    "un vienu ģeotehnikas vai koku novērtējuma dokumentu."
)

if "source_docs_df" not in st.session_state:
    st.session_state.source_docs_df = pd.DataFrame()

if "requirements_df" not in st.session_state:
    st.session_state.requirements_df = pd.DataFrame()

if st.button("1) Atrast 01_VD izejas dokumentus"):
    try:
        drive_service = get_drive_service()
        source_docs_df = get_source_documents(drive_service, input_folder_id)

        if source_docs_df.empty:
            st.warning("Nav atrasti izejas dokumenti 01_VD sadaļā.")
        else:
            st.session_state.source_docs_df = source_docs_df
            st.success(f"Atrasti {len(source_docs_df)} izejas dokumenti.")

    except Exception as e:
        st.error("Neizdevās atrast 01_VD izejas dokumentus.")
        st.exception(e)

source_docs_df = st.session_state.source_docs_df

if not source_docs_df.empty:
    st.markdown("## 2. Atrastie izejas dokumenti")

    st.dataframe(
        source_docs_df[
            ["name", "path", "mimeType", "source_document_type", "size", "modifiedTime"]
        ],
        use_container_width=True,
    )

    source_types = sorted(source_docs_df["source_document_type"].dropna().unique().tolist())

    selected_types = st.multiselect(
        "Izvēlies analizējamos izejas dokumentu tipus",
        options=source_types,
        default=["design_brief"] if "design_brief" in source_types else source_types[:1],
    )

    filtered_df = source_docs_df[
        source_docs_df["source_document_type"].isin(selected_types)
    ].copy()

    file_options = filtered_df["path"].tolist()

    selected_paths = st.multiselect(
        "Izvēlies konkrētus failus analīzei",
        options=file_options,
        default=file_options[:1],
    )

    selected_docs_df = filtered_df[filtered_df["path"].isin(selected_paths)].copy()

    st.markdown("### Analīzei izvēlētie faili")
    st.dataframe(
        selected_docs_df[["name", "path", "source_document_type", "size"]],
        use_container_width=True,
    )

    if st.button("2) Izvilkt prasības ar AI"):
        try:
            if selected_docs_df.empty:
                st.warning("Nav izvēlēts neviens fails.")
                st.stop()

            drive_service = get_drive_service()
            universal_prompt = load_universal_prompt(drive_service, prompt_folder_id)

            if not universal_prompt:
                st.warning("Nav izdevies nolasīt universal_bp_audit_prompt.txt. Turpinu bez tā.")

            client = get_openai_client()

            all_requirements: List[Dict[str, Any]] = []

            progress = st.progress(0)
            status = st.empty()

            for idx, (_, doc_row) in enumerate(selected_docs_df.iterrows(), start=1):
                file_name = doc_row["name"]
                file_id = doc_row["id"]
                file_type = doc_row["source_document_type"]
                mime_type = doc_row["mimeType"]

                status.write(f"Apstrādāju: {file_name}")

                if mime_type != PDF_MIME_TYPE:
                    st.warning(f"Izlaižu failu, jo pagaidām apstrādājam tikai PDF: {file_name}")
                    continue

                pdf_bytes = download_drive_file_bytes(drive_service, file_id)

                blocks_df = extract_pdf_text_blocks(
                    pdf_bytes=pdf_bytes,
                    max_pages=int(max_pages_per_pdf),
                )

                if blocks_df.empty:
                    st.warning(f"No PDF neizdevās izvilkt tekstu: {file_name}")
                    continue

                source_text = prepare_text_for_ai(
                    blocks_df=blocks_df,
                    max_blocks=int(max_blocks_per_pdf),
                )

                requirements = extract_requirements_with_ai(
                    client=client,
                    universal_prompt=universal_prompt,
                    source_document_type=file_type,
                    source_file=file_name,
                    source_text=source_text,
                    model=model,
                )

                for req in requirements:
                    req["source_file"] = req.get("source_file") or file_name
                    req["source_document_type"] = req.get("source_document_type") or file_type
                    req["drive_file_id"] = file_id
                    req["drive_path"] = doc_row["path"]

                all_requirements.extend(requirements)

                progress.progress(idx / len(selected_docs_df))

            if not all_requirements:
                st.info("AI neatrada pārbaudāmas prasības izvēlētajos failos.")
                st.session_state.requirements_df = pd.DataFrame()
            else:
                req_df = pd.DataFrame(all_requirements)
                st.session_state.requirements_df = req_df
                st.success(f"Izvilktas {len(req_df)} prasības.")

        except Exception as e:
            st.error("Kļūda, izvelkot prasības ar AI.")
            st.exception(e)

requirements_df = st.session_state.requirements_df

if not requirements_df.empty:
    st.markdown("## 3. AI izvilktās prasības")

    preferred_cols = [
        "requirement_id",
        "source_document_type",
        "source_file",
        "page",
        "block_id",
        "requirement_type",
        "requirement",
        "condition",
        "applies_to_sections",
        "priority",
        "verification_hint",
        "source_text",
        "confidence",
        "drive_path",
    ]

    existing_cols = [col for col in preferred_cols if col in requirements_df.columns]
    other_cols = [col for col in requirements_df.columns if col not in existing_cols]

    st.dataframe(
        requirements_df[existing_cols + other_cols],
        use_container_width=True,
    )

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        requirements_df.to_excel(writer, sheet_name="source_requirements", index=False)

    st.download_button(
        label="Lejupielādēt prasību tabulu Excel formātā",
        data=excel_buffer.getvalue(),
        file_name="c2_3_source_requirements_test.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
