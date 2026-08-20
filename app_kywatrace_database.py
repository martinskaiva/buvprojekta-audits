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


def safe_link(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    return text if text.startswith("http") else ""


def first_stage_url(files: pd.DataFrame, stage: str) -> str | None:
    if files.empty or "file_stage" not in files.columns:
        return None
    rows = files[files["file_stage"] == stage]
    if rows.empty:
        return None
    for _, row in rows.iterrows():
        url = safe_link(row.get("drive_folder_url")) or safe_link(row.get("drive_file_url"))
        if url:
            return url
    return None


def render_all_projects(projects: pd.DataFrame):
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
        "code": "Projekts",
        "name": "Nosaukums",
        "documents_count": "Dokumenti",
        "findings_count": "Piezīmes",
        "gold_examples_count": "GOLD",
        "pending_count": "Pending",
        "no_discrepancy_count": "No discrepancies",
        "deliverables_count": "Nodevumi",
        "deliverables_status": "Nodevumu statuss",
    })
    st.dataframe(overview, use_container_width=True, hide_index=True)

    st.markdown("#### GOLD sadalījums pa projektiem")
    chart = projects[["code", "gold_examples_count"]].copy().set_index("code")
    st.bar_chart(chart)


def render_gold_search():
    st.subheader("GOLD zināšanu bāze")
    st.caption("Meklē apstiprinātās audita piezīmes pāri visiem Kywatrace projektiem.")

    gold = fetch_all(
        "gold_knowledge_base",
        "project_code,project_name,finding_id,source_audit_id,discipline,document_number,document_filename,"
        "page,location,category,comment,reference_document_number,reference_document_filename,"
        "reference_page,reference_location,reference_evidence_text,document_id",
    )

    if gold.empty:
        st.info("GOLD zināšanu bāzē vēl nav ierakstu.")
        return

    f1, f2, f3 = st.columns(3)
    with f1:
        project_filter = st.multiselect(
            "Projekts",
            sorted([x for x in gold["project_code"].dropna().unique().tolist() if x]),
        )
    with f2:
        discipline_filter = st.multiselect(
            "Disciplīna",
            sorted([x for x in gold["discipline"].dropna().unique().tolist() if x]),
        )
    with f3:
        category_filter = st.multiselect(
            "Kategorija",
            sorted([x for x in gold["category"].dropna().unique().tolist() if x]),
        )

    query = st.text_input(
        "Meklēt GOLD piezīmēs",
        placeholder="Piemēram: durvis, apjomu pārbaude, ventilācija, Design Brief...",
    )

    filtered = gold.copy()
    if project_filter:
        filtered = filtered[filtered["project_code"].isin(project_filter)]
    if discipline_filter:
        filtered = filtered[filtered["discipline"].isin(discipline_filter)]
    if category_filter:
        filtered = filtered[filtered["category"].isin(category_filter)]

    if query:
        mask = pd.Series(False, index=filtered.index)
        for col in [
            "comment", "category", "document_number", "document_filename", "location",
            "reference_document_number", "reference_document_filename", "reference_evidence_text"
        ]:
            mask = mask | filtered[col].fillna("").str.contains(query, case=False, regex=False)
        filtered = filtered[mask]

    st.caption(f"Atrasti {len(filtered)} no {len(gold)} GOLD piemēriem")

    display = filtered[[
        "project_code", "source_audit_id", "discipline", "document_number", "page",
        "location", "category", "comment", "reference_document_number", "reference_page"
    ]].rename(columns={
        "project_code": "Projekts",
        "source_audit_id": "Audit ID",
        "discipline": "Disciplīna",
        "document_number": "Dokumenta Nr.",
        "page": "Lapa",
        "location": "Vieta",
        "category": "Kategorija",
        "comment": "Piezīme",
        "reference_document_number": "Atsauces dokuments",
        "reference_page": "Atsauces lapa",
    })

    st.dataframe(
        display,
        use_container_width=True,
        hide_index=True,
        height=650,
        column_config={
            "Piezīme": st.column_config.TextColumn("Piezīme", width="large"),
            "Vieta": st.column_config.TextColumn("Vieta", width="medium"),
        },
    )


def render_project(project: pd.Series):
    selected = project["code"]
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
        "drive_created_at,drive_modified_at,kywatrace_registered_at,process_date,notes,document_id,"
        "parent_file_id,drive_file_id,relative_path,discipline",
        {"project_code": selected},
    )
    timeline = fetch_all(
        "project_timeline_overview",
        "code,name,input_created_at,audit_started_at,results_completed_at,gold_updated_at,"
        "deliverables_created_at,deliverables_sent_at,deliverables_status,drive_folder_url",
        {"code": selected},
    )

    b1, b2, b3, b4 = st.columns(4)
    for col, label, stage in [
        (b1, "📥 Input", "input"),
        (b2, "📝 Results", "result"),
        (b3, "🏆 GOLD", "gold"),
        (b4, "📦 Nodevums", "deliverable"),
    ]:
        url = first_stage_url(files, stage)
        with col:
            if url:
                st.link_button(label, url, use_container_width=True)
            else:
                st.button(label, disabled=True, use_container_width=True, key=f"disabled_{selected}_{stage}")

    tabs = st.tabs([
        "Kopsavilkums",
        "Faili",
        "Dokumentu ķēde",
        "Audita piezīmes",
        "Nodevumi",
    ])

    with tabs[0]:
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

        discipline_stats = fetch_all(
            "project_discipline_stats",
            "project_code,discipline,findings_count,gold_count,pending_count,no_discrepancy_count",
            {"project_code": selected},
        )
        category_stats = fetch_all(
            "project_category_stats",
            "project_code,category,findings_count,gold_count",
            {"project_code": selected},
        )

        a1, a2 = st.columns(2)
        with a1:
            st.markdown("#### Piezīmes pa disciplīnām")
            if not discipline_stats.empty:
                chart = discipline_stats.sort_values("findings_count", ascending=False).head(20)
                st.bar_chart(chart.set_index("discipline")[["findings_count"]])
            else:
                st.info("Nav disciplīnu statistikas.")

        with a2:
            st.markdown("#### TOP kļūdu kategorijas")
            if not category_stats.empty:
                chart = category_stats.sort_values("findings_count", ascending=False).head(15)
                st.bar_chart(chart.set_index("category")[["findings_count"]])
            else:
                st.info("Nav kategoriju statistikas.")

        st.write(
            "**Nodevumu statuss:**",
            STATUS_LABELS.get(project.get("deliverables_status"), project.get("deliverables_status") or "—"),
        )

    with tabs[1]:
        st.markdown("#### Projekta failu ķēde")
        if files.empty:
            st.info("Projektam vēl nav reģistrētu failu/mapju.")
        else:
            stage_options = [
                x for x in ["input", "result", "gold", "deliverable", "reference", "other"]
                if x in files["file_stage"].dropna().unique()
            ]
            selected_stages = st.multiselect(
                "Posms",
                stage_options,
                default=stage_options,
                format_func=lambda x: STAGE_LABELS.get(x, x),
                key=f"file_stage_{selected}",
            )
            discipline_options = sorted([x for x in files["discipline"].dropna().unique().tolist() if x])
            selected_disciplines = st.multiselect(
                "Disciplīna",
                discipline_options,
                key=f"file_disc_{selected}",
            )
            file_query = st.text_input(
                "Meklēt failos",
                placeholder="Faila nosaukums vai ceļš",
                key=f"file_search_{selected}",
            )

            display = files[files["file_stage"].isin(selected_stages)].copy()
            if selected_disciplines:
                display = display[display["discipline"].isin(selected_disciplines)]
            if file_query:
                mask = (
                    display["file_name"].fillna("").str.contains(file_query, case=False, regex=False)
                    | display["relative_path"].fillna("").str.contains(file_query, case=False, regex=False)
                )
                display = display[mask]

            display["Posms"] = display["file_stage"].map(STAGE_LABELS).fillna(display["file_stage"])
            display["Drive izveidots"] = display["drive_created_at"].apply(fmt_dt)
            display["Drive mainīts"] = display["drive_modified_at"].apply(fmt_dt)
            display["Saite"] = display.apply(
                lambda r: safe_link(r.get("drive_file_url")) or safe_link(r.get("drive_folder_url")), axis=1
            )
            display = display[[
                "Posms", "discipline", "relative_path", "file_name",
                "Drive izveidots", "Drive mainīts", "Saite"
            ]].rename(columns={
                "discipline": "Disciplīna",
                "relative_path": "Ceļš",
                "file_name": "Fails / mape",
            })

            st.caption(f"Parādīti {len(display)} reģistrēti faili/mapes")
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={"Saite": st.column_config.LinkColumn("Atvērt Drive")},
            )

    with tabs[2]:
        st.markdown("#### Input → Result → Findings")
        trace = fetch_all(
            "document_trace",
            "project_code,document_id,document_number,document_name,discipline,input_url,input_drive_id,"
            "input_modified_at,result_url,result_drive_id,result_modified_at,findings_count,gold_count",
            {"project_code": selected},
        )
        if trace.empty:
            st.info("Projektam vēl nav dokumentu ķēdes datu.")
        else:
            trace_query = st.text_input(
                "Meklēt dokumentu ķēdē",
                placeholder="Dokumenta numurs vai faila nosaukums",
                key=f"trace_search_{selected}",
            )
            trace_disc = st.multiselect(
                "Disciplīna",
                sorted([x for x in trace["discipline"].dropna().unique().tolist() if x]),
                key=f"trace_disc_{selected}",
            )
            filtered = trace.copy()
            if trace_disc:
                filtered = filtered[filtered["discipline"].isin(trace_disc)]
            if trace_query:
                mask = (
                    filtered["document_number"].fillna("").str.contains(trace_query, case=False, regex=False)
                    | filtered["document_name"].fillna("").str.contains(trace_query, case=False, regex=False)
                )
                filtered = filtered[mask]

            display = filtered[[
                "discipline", "document_number", "document_name", "findings_count", "gold_count",
                "input_url", "result_url"
            ]].rename(columns={
                "discipline": "Disciplīna",
                "document_number": "Dokumenta Nr.",
                "document_name": "Dokuments",
                "findings_count": "Piezīmes",
                "gold_count": "GOLD",
                "input_url": "Input",
                "result_url": "Result",
            })
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Input": st.column_config.LinkColumn("Input"),
                    "Result": st.column_config.LinkColumn("Result"),
                },
            )

    with tabs[3]:
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
                dsel = st.multiselect(
                    "Disciplīna",
                    sorted([x for x in findings["discipline"].dropna().unique() if x]),
                    key=f"finding_disc_{selected}",
                )
            with f2:
                ssel = st.multiselect(
                    "Statuss",
                    sorted([x for x in findings["knowledge_status"].dropna().unique() if x]),
                    key=f"finding_status_{selected}",
                )
            with f3:
                csel = st.multiselect(
                    "Kategorija",
                    sorted([x for x in findings["category"].dropna().unique() if x]),
                    key=f"finding_category_{selected}",
                )
            q = st.text_input(
                "Meklēt piezīmēs",
                key=f"finding_search_{selected}",
                placeholder="Teksts, dokumenta numurs, vieta...",
            )

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
            display = filtered[[
                "source_audit_id", "discipline", "document_number", "page", "location",
                "category", "comment", "knowledge_status", "drive_file_url"
            ]].rename(columns={
                "source_audit_id": "Audit ID",
                "discipline": "Disciplīna",
                "document_number": "Dokumenta Nr.",
                "page": "Lapa",
                "location": "Vieta",
                "category": "Kategorija",
                "comment": "Piezīme",
                "knowledge_status": "Statuss",
                "drive_file_url": "PDF Drive",
            })
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                height=620,
                column_config={
                    "Piezīme": st.column_config.TextColumn("Piezīme", width="large"),
                    "Vieta": st.column_config.TextColumn("Vieta", width="medium"),
                    "PDF Drive": st.column_config.LinkColumn("PDF Drive"),
                },
            )

    with tabs[4]:
        deliverables = fetch_all(
            "deliverable_overview",
            "project_code,deliverable_name,deliverable_type,version,issue_date,status,drive_file_url,"
            "client_delivery_folder_url,sent_date,notes",
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
                "deliverable_name": "Nodevums",
                "deliverable_type": "Tips",
                "version": "Versija",
                "issue_date": "Datums",
                "status": "Statuss",
                "drive_file_url": "Fails Drive",
                "client_delivery_folder_url": "Nodevumu mape",
                "sent_date": "Nosūtīts",
                "notes": "Piezīmes",
            })[[
                "Nodevums", "Tips", "Versija", "Datums", "Statuss",
                "Fails Drive", "Nodevumu mape", "Nosūtīts", "Piezīmes"
            ]]
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Fails Drive": st.column_config.LinkColumn("Fails Drive"),
                    "Nodevumu mape": st.column_config.LinkColumn("Nodevumu mape"),
                },
            )


def main():
    st.title("Kywatrace datubāze")
    st.caption("Projektu, auditēto dokumentu, piezīmju, failu un nodevumu pārskats")

    if get_db() is None:
        st.error(
            "Nav konfigurēts Supabase savienojums. Streamlit Secrets jāpievieno "
            "SUPABASE_URL un SUPABASE_SERVICE_ROLE_KEY."
        )
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
        options = ["Visi projekti", "GOLD zināšanu bāze"] + projects["code"].tolist()
        selected = st.selectbox("Skats / projekts", options)
        if st.button("Atjaunot datus", use_container_width=True):
            st.rerun()

    if selected == "Visi projekti":
        render_all_projects(projects)
        return

    if selected == "GOLD zināšanu bāze":
        render_gold_search()
        return

    project = projects.loc[projects["code"] == selected].iloc[0]
    render_project(project)


if __name__ == "__main__":
    main()
