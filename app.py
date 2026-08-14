"""gurt admin dashboard.

    streamlit run app.py

Reads the Postgres store, read-only. See README.md for what it cannot tell you
— particularly that there is no "gurt ran and all was well" record, so repo
liveness is a prompt to go and look rather than an answer.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import alerts
import auth
import db
import queries

st.set_page_config(page_title="gurt admin", page_icon="🩺", layout="wide")


def setup_page(error: Exception) -> None:
    """Shown when there is no database to talk to. Not a traceback."""
    st.title("🩺 gurt admin")
    st.error(str(error))
    st.markdown(
        """
        Point the dashboard at a database:

        ```bash
        export DATABASE_URL='postgresql://...'
        streamlit run app.py
        ```

        or copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml`.
        """
    )


def link_column(frame: pd.DataFrame) -> pd.DataFrame:
    """Add a clickable PR link where there is a pull request."""
    if frame.empty or "pull_request" not in frame.columns:
        return frame
    out = frame.copy()
    out.insert(0, "pr", out.apply(queries.pr_url, axis=1))
    return out


def show(frame: pd.DataFrame, **kwargs) -> None:
    """A dataframe, or an honest note that there is nothing to show."""
    if frame is None or frame.empty:
        st.caption("Nothing here.")
        return
    config = kwargs.pop("column_config", {})
    if "pr" in frame.columns:
        config["pr"] = st.column_config.LinkColumn("PR", display_text=r"#(\d+)$")
    st.dataframe(frame, width='stretch', hide_index=True, column_config=config, **kwargs)


# --- pages ----------------------------------------------------------------


def page_overview(days: int) -> None:
    st.subheader("Fleet")
    summary = queries.fleet_summary()
    columns = st.columns(4)
    for column, (label, key) in zip(
        columns,
        [
            ("Installations", "installations"),
            ("Repositories", "repositories"),
            ("Attempts", "attempts"),
            ("PRs opened", "prs_opened"),
        ],
    ):
        column.metric(label, f"{summary[key]:,}")

    columns = st.columns(4)
    for column, (label, key) in zip(
        columns,
        [
            ("Merged", "merged"),
            ("Reverted", "reverted"),
            ("Codemods", "codemods"),
            ("Validated", "validated_codemods"),
        ],
    ):
        column.metric(label, f"{summary[key]:,}")

    st.divider()
    left, right = st.columns([2, 1])

    with left:
        st.subheader(f"Attempts, last {days} days")
        over_time = queries.attempts_over_time(days)
        if over_time.empty:
            st.caption("No attempts in this window.")
        else:
            pivot = over_time.pivot_table(
                index="day", columns="outcome", values="attempts", fill_value=0
            )
            st.bar_chart(pivot)

    with right:
        st.subheader("Outcomes, all time")
        show(queries.outcome_breakdown())

    st.divider()
    st.subheader("Rows per table")
    present = sorted(db.existing_tables() & set(db.ALL_TABLES))
    show(queries.table_counts(present))


def page_mistakes(days: int) -> None:
    harm = alerts.harm(days)
    decay = alerts.decay(days)

    st.subheader("🔴 Mistakes — gurt shipped something wrong")
    if not harm:
        st.success(
            f"Nothing in the last {days} days. No reverts, no post-merge CI "
            "failures, no branches a human had to repair, no demoted codemods."
        )
    for alert in harm:
        with st.expander(f"**{alert.title}** — {alert.count}", expanded=True):
            st.caption(alert.explanation)
            show(link_column(alert.rows))

    st.divider()
    st.subheader("🟠 Decay — gurt doing less than it looks like it is")
    if not decay:
        st.success("Nothing decaying.")
    for alert in decay:
        with st.expander(f"**{alert.title}** — {alert.count}", expanded=False):
            st.caption(alert.explanation)
            show(alert.rows)

    st.divider()
    st.subheader("🟡 Working as intended — deliberately not alerts")
    st.caption(
        "gurt refusing an upgrade is the gate doing its job, and a maintainer "
        "closing a PR says nothing about whether the transform was correct — "
        "which is why neither moves a promotion counter, and neither appears "
        "above. They are here so an empty mistakes panel reads as *gurt is "
        "running and refusing things*, rather than *gurt is not running*."
    )
    intended = alerts.working_as_intended(days)
    left, right = st.columns(2)
    with left:
        st.markdown("**Refused before opening a PR** (`red` / `blocked`)")
        show(intended["refusals"])
        st.markdown("**Batch failures gurt could not attribute**")
        st.caption(
            "`confirmed = false`, so these suppress nothing and will be retried."
        )
        show(intended["unattributed"])
    with right:
        st.markdown("**PRs the maintainer closed**")
        show(link_column(intended["declined"]))

    st.divider()
    st.subheader("Last recorded activity")
    st.warning(
        "**This is not 'last scanned'.** Every table is an event log of changes "
        "and problems — a run where nothing changed writes no row at all. A "
        "stale timestamp means *either* gurt stopped running here *or* nothing "
        "has changed, and this cannot tell you which. Telling them apart needs "
        "a heartbeat row per run, which gurt does not write today.",
        icon="⚠️",
    )
    show(queries.last_recorded_activity())


def page_codemods() -> None:
    st.subheader("Codemod health")
    split = queries.codemod_status_split()
    if split.empty:
        st.info(
            "No codemods yet. The builder has never run — `gurt-app build --queue` "
            "is what fills this table, and until then every repository derives "
            "what it needs from scratch."
        )
    else:
        left, right = st.columns([1, 2])
        with left:
            show(split)
        with right:
            st.bar_chart(
                split.pivot_table(
                    index="status", columns="origin", values="codemods", fill_value=0
                )
            )

    st.divider()
    st.subheader("Every codemod")
    st.caption(
        "`validated` codemods are the only ones ever reused. A `candidate` has "
        "not earned unattended use; a `needs_review` row was demoted by a "
        "rollback and does not climb back on its own."
    )
    show(
        queries.codemod_health(),
        column_config={"ops": st.column_config.JsonColumn("ops", width="medium")},
    )

    st.divider()
    st.subheader("The builder's queue")
    st.caption(
        "Version pairs the fleet hit with no validated codemod behind them, "
        "needing at least two distinct repositories — the same query "
        "`gurt-app build --queue` uses to decide what to build."
    )
    show(queries.unbuilt_pairs())


def page_repositories() -> None:
    repos = queries.repositories()
    if repos.empty:
        st.info("No repositories have been processed yet.")
        return

    st.subheader("Repositories")
    show(repos)

    st.divider()
    labels = [
        f"{row.owner}/{row.repo}  (installation {row.installation_id})"
        for row in repos.itertuples()
    ]
    chosen = st.selectbox("Drill into", labels, index=0)
    row = repos.iloc[labels.index(chosen)]
    key = (int(row["installation_id"]), row["owner"], row["repo"])

    attempts_tab, api_tab, snapshots_tab = st.tabs(
        ["Attempts", "API checks", "Snapshots"]
    )
    with attempts_tab:
        show(link_column(queries.repository_attempts(*key)))
    with api_tab:
        show(
            queries.repository_api_checks(*key),
            column_config={
                "broken_paths": st.column_config.JsonColumn("broken_paths")
            },
        )
    with snapshots_tab:
        st.caption(
            "Written only when the inventory changed — a run that found nothing "
            "new adds no row here."
        )
        show(
            queries.repository_snapshots(*key),
            column_config={
                "dependencies": st.column_config.JsonColumn("dependencies"),
                "api_hosts": st.column_config.JsonColumn("api_hosts"),
            },
        )


def page_api() -> None:
    st.subheader("Drift by host")
    show(queries.api_drift_by_host())

    st.divider()
    st.subheader("Learned spec locations")
    st.caption(
        "Fleet-wide: where a provider publishes its contract is a fact about the "
        "provider, so one repository finding it teaches every installation. A "
        "NULL url means *searched, found nothing* — recorded as deliberately as "
        "a hit, so discovery is not repeated. `failures >= 3` is retired."
    )
    show(queries.learned_specs())

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("Route mappings")
        st.caption("`none` is the negative cache: asked, and nothing was found.")
        show(queries.route_mappings())
    with right:
        st.subheader("Contract versions seen")
        st.caption("Two or more versions is what makes a rename derivable.")
        show(queries.spec_versions())


def page_tables() -> None:
    st.subheader("Raw tables")
    present = sorted(db.existing_tables() & set(db.ALL_TABLES))
    if not present:
        st.warning("None of gurt's tables exist here. Has `gurt-app migrate` run?")
        return
    table = st.selectbox("Table", present)
    limit = st.slider("Rows", 10, 1000, 200, step=10)
    # `table` comes from the intersection with ALL_TABLES above, never from
    # free text — the query interpolates it, so that check is load-bearing.
    show(queries.table_preview(table, limit))


# --- shell ----------------------------------------------------------------


def main() -> None:
    # First, before the sidebar renders and before anything opens a connection.
    # `require_password` calls st.stop() when it is not satisfied, so nothing
    # below this line runs and no query is ever issued for a visitor who has
    # not signed in.
    auth.require_password()

    st.sidebar.title("🩺 gurt admin")

    try:
        db.get_connection()
    except db.NotConfigured as err:
        setup_page(err)
        return
    except Exception as err:  # noqa: BLE001 — any connection failure is the same page
        st.title("🩺 gurt admin")
        st.error(f"Could not connect: {err}")
        st.caption(
            "If this is `self-signed certificate in certificate chain`, "
            "PGSSLROOTCERT is not resolving to the vendored Supabase CA."
        )
        return

    missing = db.missing_tables()
    if len(missing) == len(db.ALL_TABLES):
        st.title("🩺 gurt admin")
        st.warning(
            "Connected, but none of gurt's tables exist. Run `gurt-app migrate` "
            "against this database first."
        )
        return
    if missing:
        st.sidebar.warning(
            "Missing tables: " + ", ".join(missing) + ". Older migration?"
        )

    days = st.sidebar.slider("Window (days)", 1, 180, queries.DEFAULT_DAYS)
    if st.sidebar.button("Refresh", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    # The banner sits above every page: an admin who opens this on the
    # repositories tab should still find out that something was reverted.
    severity, message = alerts.headline(alerts.harm(days), alerts.decay(days))
    {"error": st.error, "warning": st.warning, "success": st.success}[severity](message)

    page = st.sidebar.radio(
        "Page",
        ["Overview", "Mistakes", "Codemods", "Repositories", "API", "Tables"],
        label_visibility="collapsed",
    )
    st.sidebar.caption(
        "Read-only. Reads the Postgres store only — the probe cache and job "
        "journal are local files on whichever machine ran the job."
    )

    st.title(f"gurt — {page}")
    {
        "Overview": lambda: page_overview(days),
        "Mistakes": lambda: page_mistakes(days),
        "Codemods": page_codemods,
        "Repositories": page_repositories,
        "API": page_api,
        "Tables": page_tables,
    }[page]()


main()
