"""Every query the dashboard runs, in one place.

Kept together so the SQL is reviewable as a set rather than scattered through
layout code, and so a column rename in `schema.ts` has one file to check.

Nothing here decides what is *worth alerting on* — that lives in `alerts.py`,
because it is a product judgment rather than a data-access one.
"""

from __future__ import annotations

import pandas as pd

from db import fetch, scalar

# The window most panels default to. Long enough to survive a quiet week.
DEFAULT_DAYS = 30


def pr_url(row: pd.Series) -> str | None:
    """A link to the pull request an attempt opened, when it opened one."""
    if not row.get("pull_request"):
        return None
    return f"https://github.com/{row['owner']}/{row['repo']}/pull/{int(row['pull_request'])}"


# --- headline counts ------------------------------------------------------


def fleet_summary() -> dict[str, int]:
    return {
        "installations": scalar(
            "SELECT COUNT(DISTINCT installation_id) FROM migration_attempt"
        ),
        "repositories": scalar(
            "SELECT COUNT(*) FROM (SELECT DISTINCT installation_id, owner, repo"
            " FROM migration_attempt) t"
        ),
        "attempts": scalar("SELECT COUNT(*) FROM migration_attempt"),
        "prs_opened": scalar(
            "SELECT COUNT(DISTINCT (installation_id, owner, repo, pull_request))"
            " FROM migration_attempt WHERE pull_request IS NOT NULL"
        ),
        "merged": scalar(
            "SELECT COUNT(*) FROM migration_attempt WHERE merge_outcome = 'merged'"
        ),
        "reverted": scalar(
            "SELECT COUNT(*) FROM migration_attempt WHERE merge_outcome = 'reverted'"
        ),
        "codemods": scalar("SELECT COUNT(*) FROM codemod"),
        "validated_codemods": scalar(
            "SELECT COUNT(*) FROM codemod WHERE status = 'validated'"
        ),
    }


def attempts_over_time(days: int = DEFAULT_DAYS) -> pd.DataFrame:
    return fetch(
        """
        SELECT DATE_TRUNC('day', opened_at)::date AS day,
               outcome,
               COUNT(*)::int AS attempts
          FROM migration_attempt
         WHERE opened_at > NOW() - MAKE_INTERVAL(days => %s)
         GROUP BY 1, 2
         ORDER BY 1
        """,
        (days,),
    )


def outcome_breakdown() -> pd.DataFrame:
    return fetch(
        """
        SELECT COALESCE(outcome, 'unrecorded') AS outcome, COUNT(*)::int AS attempts
          FROM migration_attempt
         GROUP BY 1
         ORDER BY attempts DESC
        """
    )


# --- harm: gurt shipped something and it was wrong ------------------------


def reverted_attempts(days: int) -> pd.DataFrame:
    """A human undid a change gurt merged. The strongest signal there is."""
    return fetch(
        """
        SELECT owner, repo, ecosystem, package, from_version, to_version,
               pull_request, codemod_id, resolved_at
          FROM migration_attempt
         WHERE merge_outcome = 'reverted'
           AND COALESCE(resolved_at, opened_at) > NOW() - MAKE_INTERVAL(days => %s)
         ORDER BY resolved_at DESC NULLS LAST, id DESC
        """,
        (days,),
    )


def post_merge_ci_failures(days: int) -> pd.DataFrame:
    """Merged, then the customer's own CI went red.

    The only signal that observes their environment rather than gurt's sandbox,
    which is what makes it worth more than a passing validation.
    """
    return fetch(
        """
        SELECT owner, repo, ecosystem, package, from_version, to_version,
               pull_request, merge_commit_sha, resolved_at
          FROM migration_attempt
         WHERE post_merge_ci = 'failed'
           AND COALESCE(resolved_at, opened_at) > NOW() - MAKE_INTERVAL(days => %s)
         ORDER BY resolved_at DESC NULLS LAST, id DESC
        """,
        (days,),
    )


def repaired_before_merge(days: int) -> pd.DataFrame:
    """Merged, but a human had to edit the branch first.

    Softer than a revert — the upgrade shipped — but the transform was not
    right as written, and that is the thing being measured.
    """
    return fetch(
        """
        SELECT owner, repo, ecosystem, package, from_version, to_version,
               pull_request, codemod_id, resolved_at
          FROM migration_attempt
         WHERE human_edited IS TRUE
           AND merge_outcome = 'merged'
           AND COALESCE(resolved_at, opened_at) > NOW() - MAKE_INTERVAL(days => %s)
         ORDER BY resolved_at DESC NULLS LAST, id DESC
        """,
        (days,),
    )


def demoted_codemods() -> pd.DataFrame:
    """Codemods a rollback pulled back for review.

    No date filter: a demotion is permanent until someone intervenes, so it
    stays on the board until it is dealt with rather than ageing out.
    """
    return fetch(
        """
        SELECT c.id, c.origin, c.status, c.clean_merges, c.edited_merges,
               c.reverts, c.distinct_repos, c.updated_at,
               b.ecosystem, b.package, b.from_version, b.to_version,
               b.symbol_from, b.symbol_to, b.change_type
          FROM codemod c
          JOIN breaking_change b ON b.id = c.breaking_change_id
         WHERE c.status = 'needs_review'
         ORDER BY c.reverts DESC, c.updated_at DESC
        """
    )


def crashed_runs(days: int) -> pd.DataFrame:
    """`outcome = 'error'` — gurt itself failed, not the upgrade."""
    return fetch(
        """
        SELECT owner, repo, ecosystem, package, from_version, to_version,
               failure_step, failure_summary, opened_at
          FROM migration_attempt
         WHERE outcome = 'error'
           AND opened_at > NOW() - MAKE_INTERVAL(days => %s)
         ORDER BY opened_at DESC
        """,
        (days,),
    )


# --- working as intended: refusals, not mistakes --------------------------


def refusals(days: int) -> pd.DataFrame:
    """`red` and `blocked` — gurt declined and opened nothing.

    Counted, not alerted. These are the pipeline doing its job, and mixing them
    into the harm list is how an alert panel becomes noise nobody reads.
    """
    return fetch(
        """
        SELECT outcome, failure_step, COUNT(*)::int AS attempts
          FROM migration_attempt
         WHERE outcome IN ('red', 'blocked')
           AND opened_at > NOW() - MAKE_INTERVAL(days => %s)
         GROUP BY 1, 2
         ORDER BY attempts DESC
        """,
        (days,),
    )


def declined_prs(days: int) -> pd.DataFrame:
    """Closed without merging.

    Explicitly not a mistake: closing says the maintainer did not want the
    upgrade, not that the transform was wrong. `applyOutcome` moves no counter
    for a close, and neither does this dashboard.
    """
    return fetch(
        """
        SELECT owner, repo, ecosystem, package, to_version, pull_request, resolved_at
          FROM migration_attempt
         WHERE merge_outcome = 'closed'
           AND COALESCE(resolved_at, opened_at) > NOW() - MAKE_INTERVAL(days => %s)
         ORDER BY resolved_at DESC NULLS LAST, id DESC
        """,
        (days,),
    )


def unattributed_failures(days: int) -> pd.DataFrame:
    """A batch failed and gurt could not say which package caused it.

    Honest rather than wrong — `confirmed = false` means it suppresses nothing
    — but a rising count means the attribution parser is earning its keep less
    often than it should.
    """
    return fetch(
        """
        SELECT owner, repo, ecosystem, package, to_version, failure_step, opened_at
          FROM migration_attempt
         WHERE outcome = 'red' AND confirmed IS FALSE
           AND opened_at > NOW() - MAKE_INTERVAL(days => %s)
         ORDER BY opened_at DESC
        """,
        (days,),
    )


# --- decay: gurt failing quietly ------------------------------------------


def retired_spec_locations() -> pd.DataFrame:
    """Spec URLs that failed enough times to be retired (>= 3).

    `learnedSpecLocation` stops serving these, so the host silently falls back
    to having no contract at all.
    """
    return fetch(
        """
        SELECT host, url, origin, matched, failures, updated_at
          FROM learned_spec
         WHERE failures >= 3
         ORDER BY failures DESC, updated_at DESC
        """
    )


def unfixed_api_drift() -> pd.DataFrame:
    """Drift found repeatedly on a host, with no pull request ever opened."""
    return fetch(
        """
        SELECT owner, repo, host,
               COUNT(*)::int AS runs_with_drift,
               MAX(created_at) AS last_seen
          FROM api_check_result
         WHERE conforms IS FALSE AND pull_request IS NULL
         GROUP BY 1, 2, 3
        HAVING COUNT(*) > 1
         ORDER BY runs_with_drift DESC, last_seen DESC
        """
    )


def mapper_misses() -> pd.DataFrame:
    """Paths the route mapper was asked about and could not resolve."""
    return fetch(
        """
        SELECT host, COUNT(*)::int AS unresolved_paths, MAX(updated_at) AS last_seen
          FROM route_mapping
         WHERE status = 'none'
         GROUP BY 1
         ORDER BY unresolved_paths DESC
        """
    )


def stalled_codemods() -> pd.DataFrame:
    """Codemods that repositories have used but that never reached `validated`."""
    return fetch(
        """
        SELECT c.id, c.origin, c.status, c.clean_merges, c.edited_merges,
               c.distinct_repos, COUNT(a.id)::int AS attempts,
               b.package, b.from_version, b.to_version
          FROM codemod c
          JOIN breaking_change b ON b.id = c.breaking_change_id
          LEFT JOIN migration_attempt a ON a.codemod_id = c.id
         WHERE c.status = 'candidate'
         GROUP BY c.id, b.package, b.from_version, b.to_version
        HAVING COUNT(a.id) > 0
         ORDER BY attempts DESC
        """
    )


def suspended_installations() -> pd.DataFrame:
    return fetch(
        """
        SELECT installation_id, state, updated_at
          FROM installation_state
         WHERE state IN ('suspended', 'deleted')
         ORDER BY updated_at DESC
        """
    )


def last_recorded_activity() -> pd.DataFrame:
    """When gurt last *wrote something* about each repository.

    Read this carefully. It is **not** "last scanned": every table is an event
    log of changes and problems, and a run where nothing changed writes no row
    at all (`recordSnapshot` returns early on an unchanged inventory,
    `recordApiCheck` only fires for hosts with drift, `up-to-date` drops are
    skipped). So a stale timestamp means *either* gurt has stopped running here
    *or* nothing has changed — and this query cannot tell you which.

    Making that distinction needs a heartbeat row per run, which gurt does not
    write today. Until it does, treat this as a prompt to go and look.
    """
    return fetch(
        """
        SELECT owner, repo, installation_id, MAX(at) AS last_activity
          FROM (
            SELECT installation_id, owner, repo, opened_at AS at FROM migration_attempt
            UNION ALL
            SELECT installation_id, owner, repo, created_at FROM repo_snapshot
            UNION ALL
            SELECT installation_id, owner, repo, created_at FROM api_check_result
          ) events
         GROUP BY 1, 2, 3
         ORDER BY last_activity ASC
        """
    )


def builder_has_run() -> bool:
    return scalar("SELECT COUNT(*) FROM codemod") > 0


# --- browsing -------------------------------------------------------------


def repositories() -> pd.DataFrame:
    return fetch(
        """
        SELECT installation_id, owner, repo,
               COUNT(*)::int AS attempts,
               COUNT(*) FILTER (WHERE merge_outcome = 'merged')::int AS merged,
               COUNT(*) FILTER (WHERE merge_outcome = 'reverted')::int AS reverted,
               COUNT(*) FILTER (WHERE outcome = 'red')::int AS red,
               MAX(opened_at) AS last_attempt
          FROM migration_attempt
         GROUP BY 1, 2, 3
         ORDER BY last_attempt DESC
        """
    )


def repository_attempts(installation_id: int, owner: str, repo: str) -> pd.DataFrame:
    return fetch(
        """
        SELECT package, ecosystem, from_version, to_version, outcome, confirmed,
               failure_step, pull_request, merge_outcome, human_edited,
               post_merge_ci, codemod_id, opened_at, resolved_at
          FROM migration_attempt
         WHERE installation_id = %s AND owner = %s AND repo = %s
         ORDER BY opened_at DESC
        """,
        (installation_id, owner, repo),
    )


def repository_api_checks(installation_id: int, owner: str, repo: str) -> pd.DataFrame:
    return fetch(
        """
        SELECT host, conforms, broken_paths, fix_derivable, pull_request,
               head_sha, created_at
          FROM api_check_result
         WHERE installation_id = %s AND owner = %s AND repo = %s
         ORDER BY created_at DESC
        """,
        (installation_id, owner, repo),
    )


def repository_snapshots(installation_id: int, owner: str, repo: str) -> pd.DataFrame:
    return fetch(
        """
        SELECT head_sha, verdict, dependencies, api_hosts, created_at
          FROM repo_snapshot
         WHERE installation_id = %s AND owner = %s AND repo = %s
         ORDER BY created_at DESC
        """,
        (installation_id, owner, repo),
    )


def codemod_health() -> pd.DataFrame:
    return fetch(
        """
        SELECT c.id, c.origin, c.status, c.clean_merges, c.edited_merges,
               c.reverts, c.distinct_repos, c.ops, c.created_at, c.updated_at,
               b.ecosystem, b.package, b.from_version, b.to_version,
               b.symbol_from, b.symbol_to, b.change_type
          FROM codemod c
          JOIN breaking_change b ON b.id = c.breaking_change_id
         ORDER BY c.updated_at DESC
        """
    )


def codemod_status_split() -> pd.DataFrame:
    return fetch(
        """
        SELECT status, origin, COUNT(*)::int AS codemods
          FROM codemod
         GROUP BY 1, 2
         ORDER BY 1, 2
        """
    )


def api_drift_by_host() -> pd.DataFrame:
    return fetch(
        """
        SELECT host,
               COUNT(*)::int AS checks,
               COUNT(*) FILTER (WHERE conforms IS FALSE)::int AS with_drift,
               COUNT(*) FILTER (WHERE fix_derivable)::int AS fixable,
               COUNT(*) FILTER (WHERE pull_request IS NOT NULL)::int AS fixed,
               MAX(created_at) AS last_seen
          FROM api_check_result
         GROUP BY 1
         ORDER BY with_drift DESC, checks DESC
        """
    )


def learned_specs() -> pd.DataFrame:
    return fetch(
        """
        SELECT host, url, origin, matched, failures, created_at, updated_at
          FROM learned_spec
         ORDER BY failures DESC, updated_at DESC
        """
    )


def route_mappings() -> pd.DataFrame:
    return fetch(
        """
        SELECT host, from_path, to_path, origin, status, spec_fingerprint,
               created_at, updated_at
          FROM route_mapping
         ORDER BY updated_at DESC
        """
    )


def spec_versions() -> pd.DataFrame:
    return fetch(
        """
        SELECT host,
               COUNT(*)::int AS versions_seen,
               MAX(created_at) AS last_seen
          FROM spec_snapshot
         GROUP BY 1
         ORDER BY versions_seen DESC
        """
    )


def unbuilt_pairs(min_repos: int = 2, limit: int = 50) -> pd.DataFrame:
    """The builder's queue, as `unbuiltPairs` computes it.

    Mirrors `PostgresStore.unbuiltPairs` so the dashboard shows what
    `gurt-app build --queue` would actually pick up.
    """
    return fetch(
        """
        SELECT a.ecosystem, a.package, a.from_version, a.to_version,
               COUNT(DISTINCT a.installation_id || '/' || a.owner || '/' || a.repo)::int AS repos
          FROM migration_attempt a
         WHERE a.codemod_id IS NULL
           AND NOT EXISTS (
             SELECT 1 FROM breaking_change b
               JOIN codemod c ON c.breaking_change_id = b.id AND c.status = 'validated'
              WHERE b.ecosystem = a.ecosystem AND b.package = a.package
                AND b.from_version = a.from_version AND b.to_version = a.to_version
           )
         GROUP BY 1, 2, 3, 4
        HAVING COUNT(DISTINCT a.installation_id || '/' || a.owner || '/' || a.repo) >= %s
         ORDER BY repos DESC, a.package
         LIMIT %s
        """,
        (min_repos, limit),
    )


def table_preview(table: str, limit: int = 200) -> pd.DataFrame:
    """Raw rows, for the questions the curated pages do not anticipate.

    `table` is checked against the known list by the caller — it is
    interpolated into the SQL, so it must never come straight from input.
    """
    return fetch(f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT %s", (limit,))


def table_counts(tables: list[str]) -> pd.DataFrame:
    """Row count per table, for the overview."""
    if not tables:
        return pd.DataFrame(columns=["table", "rows"])
    union = " UNION ALL ".join(
        f"SELECT '{t}' AS table, COUNT(*)::int AS rows FROM {t}" for t in tables
    )
    return fetch(f"{union} ORDER BY rows DESC")
