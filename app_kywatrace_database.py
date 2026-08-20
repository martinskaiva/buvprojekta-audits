import streamlit as st
import pandas as pd
from supabase import create_client

st.set_page_config(page_title="Kywatrace | Datubāze", page_icon="🔎", layout="wide")

STATUS_LABELS = {
    "available": "Ir nodevumi",
    "not_created": "Nav izveidoti",
    "partial_unknown": "Nav precizēts",
}

STAGE_LABELS = {
    "input": "Iesniegtie materiāli",
    "result": "Auditētie / marked faili",
    "gold": "GOLD",
    "deliverable": "Nodevuma materiāli",
    "reference": "References",
    "other": "Citi",
}


@st.cache_resource
def get_db():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


def fetch_all(table: str, columns: str = "*", filters: dict | None = None) -> pd.DataFrame:
    db = get_db()
    if db is None:
        return pd.DataFrame()
    page_size = 1000
    start = 0
    rows: list[dict] = []
    while True:
        q = db.table(table).select(columns)
        for field, value in (filters or {}).items():
            q = q.eq(field, value)
        response = q.range(start, start + page_size - 1).execute()
        batch = response.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return pd.DataFrame(rows)


def metric_value(row: pd.Series, field: str) -> int:
    try:
        return int(row.get(field, 0) or 0)
    except Exception:
        return 0


def fmt_dt(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return "Nav precizēts"
    try:
        ts = pd.to_datetime(value, utc=True)
        return ts.tz_convert("Europe/Riga").strftime("%d.%m.%Y %H:%M")
    except Exception:
        return str(value)


def first_stage_url(files: pd.DataFrame, stage: str) -> str | None:
    if files.empty or "file_stage" not in files.columns:
        return None
    rows = files[files["file_stage"] == stage]
    if rows.empty:
        return None
    row = rows.iloc[0]
    return row.get("drive_folder_url") or row.get("drive_file_url")


def main():
    st.title("Kywatrace datubāze")
    st.caption("Projektu, auditēto dokumentu, piezīmju, failu un nodevumu pārskats")

    if get_db() is None:
        st.error("Nav konfigurēts Supabase savienojums. Streamlit Secrets jāpievieno SUPABASE_URL un SUPABASE_SERVICE_ROLE_KEY.")
        st.stop()

    projects = fetch_all(
        "project_overview",
        "id,code,name,status,drive_folder_url,deliverables_status,documents_count,audit_runs_count,"
        "findings_count,gold_examples_count,pending_count,no_discrepancy_count,deliverables_count",
    )
    if projects.empty:
        st.info("Datubāzē nav projektu.")
        st.stop()
    projects = projects.sort_values("code")

    with st.sidebar:
        st.header("Navigācija")
        selected = st.selectbox("Projekts", ["Visi projekti"] + projects["code"].tolist())
        if st.button("Atjaunot datus", use_container_width=True):
            st.rerun()

    if selected == "Visi projekti":
        st.subheader("Projektu pārskats")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Projekti", len(projects))
        c2.metric("Auditētie dokumenti", int(projects["documents_count"].fillna(0).sum()))
        c3.metric("Audita piezīmes", int(projects["findings_count"].fillna(0).sum()))
        c4.metric("GOLD piemēri", int(projects["gold_examples_count"].fillna(0).sum()))

        overview = projects[[
            "code", "name", "documents_count", "findings_count", "gold_examples_count",
            "pending_count", "no_discrepancy_count", "deliverables_count", "deliverables_status"
        ]].copy()
        overview["deliverables_status"] = overview["deliverables_status"].map(STATUS_LABELS).fillna(overview["deliverables_status"])
        overview = overview.rename(columns={
            "code": "Projekts", "name": "Nosaukums", "documents_count": "Dokumenti",
            "findings_count": "Piezīmes", "gold_examples_count": "GOLD", "pending_count": "Pending",
            "no_discrepancy_count": "No discrepancies", "deliverables_count": "Nodevumi",
            "deliverables_status": "Nodevumu statuss",
        })
        st.dataframe(overview, use_container_width=True, hide_index=True)
        return

    project = projects.loc[projects["code"] == selected].iloc[0]
    title = project.get("name") or selected
    st.subheader(f"{selected} — {title}" if title != selected else selected)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Dokumenti", metric_value(project, "documents_count"))
    c2.metric("Piezīmes", metric_value(project, "findings_count"))
    c3.metric("GOLD", metric_value(project, "gold_examples_count"))
    c4.metric("Pending", metric_value(project, "pending_count"))
    c5.metric("Nodevumi", metric_value(project, "deliverables_count"))

    files = fetch_all(
        "project_file_overview",
        "project_code,id,file_stage,file_role,file_name,drive_file_url,drive_folder_url,mime_type,"
        "drive_created_at,drive_modified_at,kywatrace_registered_at,process_date,notes",
        {"project_code": selected},
    )
    timeline = fetch_all(
        "project_timeline_overview",
        "code,name,input_created_at,audit_started_at,results_completed_at,gold_updated_at,"
        "deliverables_created_at,deliverables_sent_at,deliverables_status,drive_folder_url",
        {"code": selected},
    )

    b1, b2, b3, b4 = st.columns(4)
    stage_buttons = [
        (b1, "📥 Input", "input"),
        (b2, "📝 Results", "result"),
        (b3, "🏆 GOLD", "gold"),
        (b4, "📦 Nodevums", "deliverable"),
    ]
    for col, label, stage in stage_buttons:
        url = first_stage_url(files, stage)
        with col:
            if url:
                st.link_button(label, url, use_container_width=True)
            else:
                st.button(label, disabled=True, use_container_width=True, key=f"disabled_{stage}")

    tab_summary, tab_files, tab_docs, tab_findings, tab_deliverables = st.tabs(
        ["Kopsavilkums", "Faili", "Dokumenti", "Audita piezīmes", "Nodevumi"]
    )

    with tab_summary:
        st.markdown("#### Audita laika līnija")
        if not timeline.empty:
            t = timeline.iloc[0]
            timeline_df = pd.DataFrame([
                {"Posms": "Input mape izveidota", "Datums": fmt_dt(t.get("input_created_at"))},
                {"Posms": "Audits sākts", "Datums": fmt_dt(t.get("audit_started_at"))},
                {"Posms": "Results pabeigts", "Datums": fmt_dt(t.get("results_completed_at"))},
                {"Posms": "GOLD atjaunots", "Datums": fmt_dt(t.get("gold_updated_at"))},
                {"Posms": "Nodevums sagatavots", "Datums": fmt_dt(t.get("deliverables_created_at"))},
                {"Posms": "Nodevums nosūtīts", "Datums": fmt_dt(t.get("deliverables_sent_at"))},
            ])
            st.dataframe(timeline_df, use_container_width=True, hide_index=True)
            st.caption("Procesa datumi tiek glabāti atsevišķi no Drive failu tehniskajiem created/modified datumiem.")

        st.markdown("#### Audita statuss")
        summary = pd.DataFrame([
            {"Rādītājs": "Auditētie dokumenti", "Skaits": metric_value(project, "documents_count")},
            {"Rādītājs": "Visas piezīmes", "Skaits": metric_value(project, "findings_count")},
            {"Rādītājs": "GOLD piemēri", "Skaits": metric_value(project, "gold_examples_count")},
            {"Rādītājs": "Pending", "Skaits": metric_value(project, "pending_count")},
            {"Rādītājs": "No discrepancies", "Skaits": metric_value(project, "no_discrepancy_count")},
            {"Rādītājs": "Nodevumi", "Skaits": metric_value(project, "deliverables_count")},
        ])
        st.dataframe(summary, use_container_width=True, hide_index=True)
        st.write("**Nodevumu statuss:**", STATUS_LABELS.get(project.get("deliverables_status"), project.get("deliverables_status") or "—"))

    with tab_files:
        st.markdown("#### Projekta failu ķēde")
        if files.empty:
            st.info("Projektam vēl nav reģistrētu failu/mapju.")
        else:
            stage_options = [x for x in ["input", "result", "gold", "deliverable", "reference", "other"] if x in files["file_stage"].unique()]
            selected_stages = st.multiselect(
                "Posms",
                stage_options,
                default=stage_options,
                format_func=lambda x: STAGE_LABELS.get(x, x),
            )
            display = files[files["file_stage"].isin(selected_stages)].copy()
            display["Posms"] = display["file_stage"].map(STAGE_LABELS).fillna(display["file_stage"])
            display["Drive izveidots"] = display["drive_created_at"].apply(fmt_dt)
            display["Drive mainīts"] = display["drive_modified_at"].apply(fmt_dt)
            display["Kywatrace reģistrēts"] = display["kywatrace_registered_at"].apply(fmt_dt)
            display["Saite"] = display["drive_file_url"].fillna(display["drive_folder_url"])
            display = display[["Posms", "file_name", "file_role", "Drive izveidots", "Drive mainīts", "Kywatrace reģistrēts", "Saite"]]
            display = display.rename(columns={"file_name": "Fails / mape", "file_role": "Loma"})
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={"Saite": st.column_config.LinkColumn("Atvērt Drive")},
            )
            st.caption("Input un Results pašlaik ir reģistrēti kā projekta mapes; GOLD un nodevuma materiāli — arī kā konkrēti faili.")

    with tab_docs:
        docs = fetch_all(
            "document_overview",
            "project_code,document_id,document_name,document_number,discipline,drive_file_url,"
            "audit_status,findings_count,gold_examples_count,pending_count,no_discrepancy_count",
            {"project_code": selected},
        )
        if docs.empty:
            st.info("Projektam nav dokumentu.")
        else:
            disciplines = sorted([x for x in docs["discipline"].dropna().unique().tolist() if x])
            selected_disc = st.multiselect("Disciplīna", disciplines, key="docs_disc")
            search = st.text_input("Meklēt dokumentā", key="docs_search", placeholder="Faila nosaukums vai dokumenta numurs")
            filtered = docs.copy()
            if selected_disc:
                filtered = filtered[filtered["discipline"].isin(selected_disc)]
            if search:
                mask = filtered["document_name"].fillna("").str.contains(search, case=False, regex=False) | filtered["document_number"].fillna("").str.contains(search, case=False, regex=False)
                filtered = filtered[mask]
            st.caption(f"Parādīti {len(filtered)} no {len(docs)} dokumentiem")
            display = filtered[["discipline", "document_number", "document_name", "findings_count", "gold_examples_count", "pending_count", "no_discrepancy_count", "drive_file_url"]].rename(columns={
                "discipline": "Disciplīna", "document_number": "Dokumenta Nr.", "document_name": "Fails",
                "findings_count": "Piezīmes", "gold_examples_count": "GOLD", "pending_count": "Pending",
                "no_discrepancy_count": "No discrepancies", "drive_file_url": "PDF Drive",
            })
            st.dataframe(display, use_container_width=True, hide_index=True, column_config={"PDF Drive": st.column_config.LinkColumn("PDF Drive")})

    with tab_findings:
        findings = fetch_all(
            "finding_browser",
            "project_code,audit_run_id,source_audit_id,discipline,document_filename,document_number,page,location,"
            "category,comment,annotation_status,knowledge_status,drive_file_url,reference_document_filename,"
            "reference_document_number,reference_page,reference_location,reference_evidence_text",
            {"project_code": selected},
        )
        if findings.empty:
            st.info("Projektam nav audita piezīmju.")
        else:
            f1, f2, f3 = st.columns(3)
            with f1:
                dsel = st.multiselect("Disciplīna", sorted([x for x in findings["discipline"].dropna().unique() if x]), key="finding_disc")
            with f2:
                ssel = st.multiselect("Statuss", sorted([x for x in findings["knowledge_status"].dropna().unique() if x]), key="finding_status")
            with f3:
                csel = st.multiselect("Kategorija", sorted([x for x in findings["category"].dropna().unique() if x]), key="finding_category")
            q = st.text_input("Meklēt piezīmēs", key="finding_search", placeholder="Teksts, dokumenta numurs, vieta...")
            filtered = findings.copy()
            if dsel:
                filtered = filtered[filtered["discipline"].isin(dsel)]
            if ssel:
                filtered = filtered[filtered["knowledge_status"].isin(ssel)]
            if csel:
                filtered = filtered[filtered["category"].isin(csel)]
            if q:
                mask = pd.Series(False, index=filtered.index)
                for col in ["comment", "document_number", "document_filename", "location", "category"]:
                    mask = mask | filtered[col].fillna("").str.contains(q, case=False, regex=False)
                filtered = filtered[mask]
            st.caption(f"Parādītas {len(filtered)} no {len(findings)} piezīmēm")
            display = filtered[["source_audit_id", "discipline", "document_number", "page", "location", "category", "comment", "knowledge_status", "drive_file_url"]].rename(columns={
                "source_audit_id": "Audit ID", "discipline": "Disciplīna", "document_number": "Dokumenta Nr.",
                "page": "Lapa", "location": "Vieta", "category": "Kategorija", "comment": "Piezīme",
                "knowledge_status": "Statuss", "drive_file_url": "PDF Drive",
            })
            st.dataframe(display, use_container_width=True, hide_index=True, height=620, column_config={
                "Piezīme": st.column_config.TextColumn("Piezīme", width="large"),
                "Vieta": st.column_config.TextColumn("Vieta", width="medium"),
                "PDF Drive": st.column_config.LinkColumn("PDF Drive"),
            })

    with tab_deliverables:
        deliverables = fetch_all(
            "deliverable_overview",
            "project_code,deliverable_name,deliverable_type,version,issue_date,status,drive_file_url,client_delivery_folder_url,sent_date,notes",
            {"project_code": selected},
        )
        if deliverables.empty:
            status = project.get("deliverables_status") or "—"
            if status == "not_created":
                st.info("Šim projektam klienta nodevuma dokumentācija netika izveidota.")
            else:
                st.info(f"Nodevumi datubāzē nav identificēti. Statuss: {STATUS_LABELS.get(status, status)}")
        else:
            display = deliverables.rename(columns={
                "deliverable_name": "Nodevums", "deliverable_type": "Tips", "version": "Versija",
                "issue_date": "Datums", "status": "Statuss", "drive_file_url": "Fails Drive",
                "client_delivery_folder_url": "Nodevumu mape", "sent_date": "Nosūtīts", "notes": "Piezīmes",
            })[["Nodevums", "Tips", "Versija", "Datums", "Statuss", "Fails Drive", "Nodevumu mape", "Nosūtīts", "Piezīmes"]]
            st.dataframe(display, use_container_width=True, hide_index=True, column_config={
                "Fails Drive": st.column_config.LinkColumn("Fails Drive"),
                "Nodevumu mape": st.column_config.LinkColumn("Nodevumu mape"),
            })


if __name__ == "__main__":
    main()
