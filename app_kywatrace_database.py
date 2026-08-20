import streamlit as st
import pandas as pd
from supabase import create_client

st.set_page_config(
    page_title="Kywatrace | Datubāze",
    page_icon="🔎",
    layout="wide",
)


@st.cache_resource
def get_db():
    url = st.secrets.get("SUPABASE_URL")
    key = st.secrets.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    return create_client(url, key)


def fetch_all(table: str, columns: str = "*", filters: dict | None = None) -> pd.DataFrame:
    """Read a Supabase table/view in pages so >1000 findings are not truncated."""
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
    value = row.get(field, 0)
    try:
        return int(value or 0)
    except Exception:
        return 0


def show_link_button(label: str, url: str | None):
    if url:
        st.link_button(label, url, use_container_width=True)


def main():
    st.title("Kywatrace datubāze")
    st.caption("Projektu, auditēto dokumentu, piezīmju un nodevumu pārskats")

    if get_db() is None:
        st.error(
            "Nav konfigurēts Supabase savienojums. Streamlit Secrets jāpievieno "
            "SUPABASE_URL un SUPABASE_SERVICE_ROLE_KEY."
        )
        st.stop()

    projects = fetch_all(
        "project_overview",
        "id,code,name,status,drive_folder_url,deliverables_status,documents_count,"
        "audit_runs_count,findings_count,gold_examples_count,pending_count,"
        "no_discrepancy_count,deliverables_count",
    )

    if projects.empty:
        st.info("Datubāzē nav projektu.")
        st.stop()

    projects = projects.sort_values("code")

    with st.sidebar:
        st.header("Navigācija")
        options = ["Visi projekti"] + projects["code"].tolist()
        selected = st.selectbox("Projekts", options)
        if st.button("Atjaunot datus", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    if selected == "Visi projekti":
        st.subheader("Projektu pārskats")
        total_docs = int(projects["documents_count"].fillna(0).sum())
        total_findings = int(projects["findings_count"].fillna(0).sum())
        total_gold = int(projects["gold_examples_count"].fillna(0).sum())
        total_deliverables = int(projects["deliverables_count"].fillna(0).sum())

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Projekti", len(projects))
        c2.metric("Auditētie dokumenti", total_docs)
        c3.metric("Audita piezīmes", total_findings)
        c4.metric("GOLD piemēri", total_gold)

        overview = projects[
            [
                "code",
                "name",
                "documents_count",
                "findings_count",
                "gold_examples_count",
                "pending_count",
                "no_discrepancy_count",
                "deliverables_count",
                "deliverables_status",
                "drive_folder_url",
            ]
        ].rename(
            columns={
                "code": "Projekts",
                "name": "Nosaukums",
                "documents_count": "Dokumenti",
                "findings_count": "Piezīmes",
                "gold_examples_count": "GOLD",
                "pending_count": "Pending",
                "no_discrepancy_count": "No discrepancies",
                "deliverables_count": "Nodevumi",
                "deliverables_status": "Nodevumu statuss",
                "drive_folder_url": "Drive",
            }
        )
        st.dataframe(
            overview,
            use_container_width=True,
            hide_index=True,
            column_config={"Drive": st.column_config.LinkColumn("Drive")},
        )
        st.caption(f"Kopā klientu/nodevuma faili: {total_deliverables}")
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

    drive_url = project.get("drive_folder_url")
    if drive_url:
        show_link_button("Atvērt projekta rezultātu mapi Drive", drive_url)

    tab_summary, tab_docs, tab_findings, tab_deliverables = st.tabs(
        ["Kopsavilkums", "Dokumenti", "Audita piezīmes", "Nodevumi"]
    )

    with tab_summary:
        st.markdown("#### Audita statuss")
        summary = pd.DataFrame(
            [
                {"Rādītājs": "Auditētie dokumenti", "Skaits": metric_value(project, "documents_count")},
                {"Rādītājs": "Visas piezīmes", "Skaits": metric_value(project, "findings_count")},
                {"Rādītājs": "GOLD piemēri", "Skaits": metric_value(project, "gold_examples_count")},
                {"Rādītājs": "Pending", "Skaits": metric_value(project, "pending_count")},
                {"Rādītājs": "No discrepancies", "Skaits": metric_value(project, "no_discrepancy_count")},
                {"Rādītājs": "Nodevumi", "Skaits": metric_value(project, "deliverables_count")},
            ]
        )
        st.dataframe(summary, use_container_width=True, hide_index=True)
        st.write("**Nodevumu statuss:**", project.get("deliverables_status") or "—")

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
            discipline_values = sorted([x for x in docs["discipline"].dropna().unique().tolist() if x])
            discipline_filter = st.multiselect("Disciplīna", discipline_values, key="docs_discipline")
            search = st.text_input("Meklēt dokumentā", key="docs_search", placeholder="Faila nosaukums vai dokumenta numurs")
            filtered = docs.copy()
            if discipline_filter:
                filtered = filtered[filtered["discipline"].isin(discipline_filter)]
            if search:
                mask = (
                    filtered["document_name"].fillna("").str.contains(search, case=False, regex=False)
                    | filtered["document_number"].fillna("").str.contains(search, case=False, regex=False)
                )
                filtered = filtered[mask]

            st.caption(f"Parādīti {len(filtered)} no {len(docs)} dokumentiem")
            display = filtered[
                [
                    "discipline",
                    "document_number",
                    "document_name",
                    "findings_count",
                    "gold_examples_count",
                    "pending_count",
                    "no_discrepancy_count",
                    "drive_file_url",
                ]
            ].rename(
                columns={
                    "discipline": "Disciplīna",
                    "document_number": "Dokumenta Nr.",
                    "document_name": "Fails",
                    "findings_count": "Piezīmes",
                    "gold_examples_count": "GOLD",
                    "pending_count": "Pending",
                    "no_discrepancy_count": "No discrepancies",
                    "drive_file_url": "PDF Drive",
                }
            )
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={"PDF Drive": st.column_config.LinkColumn("PDF Drive")},
            )

    with tab_findings:
        findings = fetch_all(
            "finding_browser",
            "project_code,audit_run_id,source_audit_id,discipline,document_filename,document_number,"
            "page,location,category,comment,annotation_status,knowledge_status,drive_file_url,"
            "reference_document_filename,reference_document_number,reference_page,reference_location,"
            "reference_evidence_text",
            {"project_code": selected},
        )
        if findings.empty:
            st.info("Projektam nav audita piezīmju.")
        else:
            f1, f2, f3 = st.columns(3)
            disciplines = sorted([x for x in findings["discipline"].dropna().unique().tolist() if x])
            knowledge = sorted([x for x in findings["knowledge_status"].dropna().unique().tolist() if x])
            categories = sorted([x for x in findings["category"].dropna().unique().tolist() if x])
            with f1:
                selected_disc = st.multiselect("Disciplīna", disciplines, key="finding_discipline")
            with f2:
                selected_knowledge = st.multiselect("Statuss", knowledge, key="finding_status")
            with f3:
                selected_category = st.multiselect("Kategorija", categories, key="finding_category")
            text_search = st.text_input(
                "Meklēt piezīmēs",
                key="finding_search",
                placeholder="Teksts, dokumenta numurs, vieta...",
            )

            filtered = findings.copy()
            if selected_disc:
                filtered = filtered[filtered["discipline"].isin(selected_disc)]
            if selected_knowledge:
                filtered = filtered[filtered["knowledge_status"].isin(selected_knowledge)]
            if selected_category:
                filtered = filtered[filtered["category"].isin(selected_category)]
            if text_search:
                search_cols = ["comment", "document_number", "document_filename", "location", "category"]
                mask = pd.Series(False, index=filtered.index)
                for col in search_cols:
                    mask = mask | filtered[col].fillna("").str.contains(text_search, case=False, regex=False)
                filtered = filtered[mask]

            st.caption(f"Parādītas {len(filtered)} no {len(findings)} piezīmēm")
            display = filtered[
                [
                    "source_audit_id",
                    "discipline",
                    "document_number",
                    "page",
                    "location",
                    "category",
                    "comment",
                    "knowledge_status",
                    "drive_file_url",
                ]
            ].rename(
                columns={
                    "source_audit_id": "Audit ID",
                    "discipline": "Disciplīna",
                    "document_number": "Dokumenta Nr.",
                    "page": "Lapa",
                    "location": "Vieta",
                    "category": "Kategorija",
                    "comment": "Piezīme",
                    "knowledge_status": "Statuss",
                    "drive_file_url": "PDF Drive",
                }
            )
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

    with tab_deliverables:
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
                st.info(f"Nodevumi datubāzē nav identificēti. Statuss: {status}")
        else:
            display = deliverables.rename(
                columns={
                    "deliverable_name": "Nodevums",
                    "deliverable_type": "Tips",
                    "version": "Versija",
                    "issue_date": "Datums",
                    "status": "Statuss",
                    "drive_file_url": "Fails Drive",
                    "client_delivery_folder_url": "Nodevumu mape",
                    "sent_date": "Nosūtīts",
                    "notes": "Piezīmes",
                }
            )[
                [
                    "Nodevums",
                    "Tips",
                    "Versija",
                    "Datums",
                    "Statuss",
                    "Fails Drive",
                    "Nodevumu mape",
                    "Nosūtīts",
                    "Piezīmes",
                ]
            ]
            st.dataframe(
                display,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Fails Drive": st.column_config.LinkColumn("Fails Drive"),
                    "Nodevumu mape": st.column_config.LinkColumn("Nodevumu mape"),
                },
            )


if __name__ == "__main__":
    main()
