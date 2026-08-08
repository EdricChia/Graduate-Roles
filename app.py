"""Streamlit dashboard.

    uv run streamlit run app.py

Sorted newest-first by default, because the point of reading company career sites directly is
to see a role the day it goes up rather than the week it reaches a job board.

Every date carries its basis and every graduate classification carries its route, so a row
can always answer "why is this here, and do you actually know when it was posted".
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import streamlit as st

from gradtrack.config import load_config
from gradtrack.schema import PRIORITY_GROUPS

NEW_WINDOW_DAYS = 3

st.set_page_config(page_title="SG Graduate Roles", page_icon="🎓", layout="wide")


@st.cache_data(ttl=300)
def load() -> tuple[pl.DataFrame, pl.DataFrame, date | None]:
    config = load_config()
    postings_path = config.curated_dir / "postings.parquet"
    candidates_path = config.curated_dir / "discovery_candidates.parquet"
    if not postings_path.exists():
        return pl.DataFrame(), pl.DataFrame(), None
    frame = pl.read_parquet(postings_path)
    candidates = pl.read_parquet(candidates_path) if candidates_path.exists() else pl.DataFrame()
    snapshot = frame["snapshot_date"].max() if frame.height else None
    return frame, candidates, snapshot


frame, candidates, snapshot = load()

st.title("🎓 Singapore Graduate Roles")
if frame.is_empty():
    st.warning(
        "No curated data yet. Run `uv run python -m gradtrack.ingest.ats` and "
        "`uv run python -m gradtrack.transform.build`."
    )
    st.stop()

st.caption(
    f"Snapshot {snapshot} · {frame.height} postings tracked · "
    "links go to each firm's own careers page"
)

# --- filters ---------------------------------------------------------------
with st.sidebar:
    st.header("Filters")
    show_internships = st.checkbox("Include internships", value=False)
    grad_only = st.checkbox("Graduate-level only", value=True)
    sg_only = st.checkbox("Singapore only", value=True)

    statuses = sorted(frame["status"].unique().to_list())
    chosen_status = st.multiselect("Status", statuses, default=[s for s in statuses if s == "open"])

    groups = sorted(g for g in frame["family_group"].unique().to_list() if g)
    priority_default = [g for g in groups if g in PRIORITY_GROUPS]
    chosen_groups = st.multiselect("Family group", groups, default=priority_default)

    families = sorted(f for f in frame["job_family"].unique().to_list() if f)
    chosen_families = st.multiselect("Job family (overrides group)", families, default=[])

    sectors = sorted(s for s in frame["sector"].unique().to_list() if s)
    chosen_sectors = st.multiselect("Sector", sectors, default=[])

    firms = sorted(f for f in frame["firm_name"].unique().to_list() if f)
    chosen_firms = st.multiselect("Firm", firms, default=[])

    max_age = st.slider("Posted within (days)", 1, 365, 90)

view = frame
if grad_only:
    view = view.filter(pl.col("is_grad"))
if not show_internships:
    view = view.filter(~pl.col("is_internship"))
if sg_only:
    view = view.filter(pl.col("is_singapore"))
if chosen_status:
    view = view.filter(pl.col("status").is_in(chosen_status))
if chosen_families:
    view = view.filter(pl.col("job_family").is_in(chosen_families))
elif chosen_groups:
    view = view.filter(pl.col("family_group").is_in(chosen_groups))
if chosen_sectors:
    view = view.filter(pl.col("sector").is_in(chosen_sectors))
if chosen_firms:
    view = view.filter(pl.col("firm_name").is_in(chosen_firms))

cutoff = date.today() - timedelta(days=max_age)
# A posting with no date is kept rather than filtered out. Dropping it would silently hide
# roles from platforms that expose no publish timestamp, which is a source gap, not an age.
view = view.filter(pl.col("posted_date").is_null() | (pl.col("posted_date") >= cutoff))

new_cutoff = date.today() - timedelta(days=NEW_WINDOW_DAYS)
view = view.with_columns(
    pl.when(pl.col("first_seen").is_not_null() & (pl.col("first_seen") >= new_cutoff))
    .then(pl.lit("🆕"))
    .otherwise(pl.lit(""))
    .alias("new"),
).sort(["posted_date", "first_seen"], descending=True, nulls_last=True)

# --- headline --------------------------------------------------------------
left, mid, right, far = st.columns(4)
left.metric("Matching", view.height)
mid.metric(f"New (≤{NEW_WINDOW_DAYS}d)", int((view["new"] != "").sum()) if view.height else 0)
right.metric("Firms", view["firm_name"].n_unique() if view.height else 0)
far.metric("Families", view["job_family"].n_unique() if view.height else 0)

tab_roles, tab_families, tab_discovery, tab_health = st.tabs(
    ["Roles", "By family", "Discovery candidates", "Sources"]
)

with tab_roles:
    if view.is_empty():
        st.info("Nothing matches these filters.")
    else:
        st.dataframe(
            view.select(
                "new",
                "posted_date",
                "firm_name",
                "title",
                "job_family",
                "status",
                "apply_url",
                "posted_date_basis",
                "grad_basis",
                "source_platform",
                "location_raw",
            ).to_pandas(),
            use_container_width=True,
            hide_index=True,
            column_config={
                "new": st.column_config.TextColumn("", width="small"),
                "posted_date": st.column_config.DateColumn("Posted"),
                "firm_name": st.column_config.TextColumn("Firm"),
                "title": st.column_config.TextColumn("Role", width="large"),
                "job_family": st.column_config.TextColumn("Family"),
                "status": st.column_config.TextColumn("Status", width="small"),
                "apply_url": st.column_config.LinkColumn("Apply", display_text="open ↗"),
                # Surfaced rather than hidden: "observed" means we are showing the day we
                # first saw it, not the day the firm published it.
                "posted_date_basis": st.column_config.TextColumn("Date basis"),
                "grad_basis": st.column_config.TextColumn("Why graduate"),
                "source_platform": st.column_config.TextColumn("Source"),
                "location_raw": st.column_config.TextColumn("Location"),
            },
        )

with tab_families:
    if not view.is_empty():
        by_family = (
            view.group_by(["family_group", "job_family"])
            .len()
            .sort("len", descending=True)
            .rename({"len": "postings"})
        )
        st.dataframe(by_family.to_pandas(), use_container_width=True, hide_index=True)
        # Aggregate the count column explicitly. A bare .sum() over the group also tries to
        # sum job_family, which is a string, and raises InvalidOperationError.
        by_group = (
            by_family.group_by("family_group")
            .agg(pl.col("postings").sum())
            .sort("postings", descending=True)
        )
        st.bar_chart(by_group.to_pandas(), x="family_group", y="postings")

with tab_discovery:
    st.markdown(
        "Employers posting graduate roles on MyCareersFuture that the registry does not "
        "cover. A name here repeatedly is a suggestion to add a row to "
        "`data/firms/registry.csv`."
    )
    if candidates.is_empty():
        st.info("No discovery candidates in the latest snapshot.")
    else:
        st.dataframe(candidates.to_pandas(), use_container_width=True, hide_index=True)

with tab_health:
    st.markdown("Where the tracked postings came from, and how each date was established.")
    st.dataframe(
        frame.group_by("source_platform").len().sort("len", descending=True).to_pandas(),
        use_container_width=True,
        hide_index=True,
    )
    st.dataframe(
        frame.group_by("posted_date_basis").len().sort("len", descending=True).to_pandas(),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "`published` is the platform's own publish timestamp. `updated` is a modification "
        "time standing in for one. `observed` is the day we first saw the posting, which "
        "for a recently added firm says more about us than about the role."
    )
