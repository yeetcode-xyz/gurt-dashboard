"""Seed one row of every alert class, for checking the dashboard classifies.

    DATABASE_URL=postgres://postgres:gurt@localhost:55432/gurt_test \
      .venv/bin/python seed_demo.py

Writes to whatever `DATABASE_URL` points at, so **only ever point it at a
throwaway database**. It refuses anything that looks like Supabase.

The point is not to make the dashboard look busy. It is to put one row in each
category — including the categories that must *not* alert — so the split
between "gurt was wrong" and "gurt correctly refused" can be checked rather
than assumed.
"""

from __future__ import annotations

import os
import sys

import psycopg

URL = os.environ.get("DATABASE_URL", "")
if not URL:
    sys.exit("set DATABASE_URL to a throwaway database")
if "supabase" in URL or "pooler" in URL:
    sys.exit("refusing to seed what looks like a production database")

INSTALLATION = 4242


def main() -> None:
    with psycopg.connect(URL, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM migration_attempt WHERE installation_id = %s", (INSTALLATION,)
        )
        conn.execute("DELETE FROM api_check_result WHERE installation_id = %s", (INSTALLATION,))
        conn.execute("DELETE FROM learned_spec WHERE host LIKE 'seed.%'")
        conn.execute("DELETE FROM route_mapping WHERE host LIKE 'seed.%'")
        conn.execute("DELETE FROM installation_state WHERE installation_id = %s", (INSTALLATION,))

        change = conn.execute(
            """INSERT INTO breaking_change
                 (ecosystem, package, from_version, to_version, symbol_from,
                  symbol_to, change_type)
               VALUES ('npm','discord.js','13.0.0','14.0.0','MessageEmbed',
                       'EmbedBuilder','renamed')
               ON CONFLICT DO NOTHING
               RETURNING id"""
        ).fetchone()
        if change is None:
            change = conn.execute(
                "SELECT id FROM breaking_change WHERE package = 'discord.js' LIMIT 1"
            ).fetchone()
        change_id = change[0]

        # A demoted codemod: HARM.
        demoted = conn.execute(
            """INSERT INTO codemod (breaking_change_id, origin, status, ops,
                                    clean_merges, reverts, distinct_repos)
               VALUES (%s,'llm','needs_review','[]'::jsonb, 2, 1, 2)
               ON CONFLICT (breaking_change_id, origin)
                 DO UPDATE SET status='needs_review', reverts=1
               RETURNING id""",
            (change_id,),
        ).fetchone()[0]

        # A codemod with attempts that never promoted: DECAY.
        stalled = conn.execute(
            """INSERT INTO codemod (breaking_change_id, origin, status, ops, clean_merges)
               VALUES (%s,'mechanical','candidate','[]'::jsonb, 1)
               ON CONFLICT (breaking_change_id, origin)
                 DO UPDATE SET status='candidate'
               RETURNING id""",
            (change_id,),
        ).fetchone()[0]

        rows = [
            # --- HARM ---------------------------------------------------
            ("reverted-repo", "left-pad", dict(
                pull_request=41, merge_outcome="reverted", outcome="green",
                codemod_id=demoted, human_edited=False, post_merge_ci=None)),
            ("ci-failed-repo", "react", dict(
                pull_request=42, merge_outcome="merged", outcome="green",
                codemod_id=None, human_edited=False, post_merge_ci="failed")),
            ("repaired-repo", "uuid", dict(
                pull_request=43, merge_outcome="merged", outcome="green",
                codemod_id=stalled, human_edited=True, post_merge_ci="passed")),
            ("crashed-repo", "chalk", dict(
                pull_request=None, merge_outcome=None, outcome="error",
                codemod_id=None, human_edited=None, post_merge_ci=None)),
            # --- NOT HARM: gurt refused, or the maintainer declined -----
            ("refused-repo", "semver", dict(
                pull_request=None, merge_outcome=None, outcome="red",
                codemod_id=None, human_edited=None, post_merge_ci=None)),
            ("blocked-repo", "react-dom", dict(
                pull_request=None, merge_outcome=None, outcome="blocked",
                codemod_id=None, human_edited=None, post_merge_ci=None)),
            ("declined-repo", "lodash", dict(
                pull_request=44, merge_outcome="closed", outcome="green",
                codemod_id=None, human_edited=False, post_merge_ci=None)),
            # A clean merge, so the happy path is represented too.
            ("happy-repo", "ms", dict(
                pull_request=45, merge_outcome="merged", outcome="green",
                codemod_id=stalled, human_edited=False, post_merge_ci="passed")),
        ]

        for repo, package, extra in rows:
            conn.execute(
                """INSERT INTO migration_attempt
                     (installation_id, owner, repo, ecosystem, package,
                      from_version, to_version, pull_request, merge_outcome,
                      outcome, codemod_id, human_edited, post_merge_ci,
                      confirmed, head_sha, resolved_at)
                   VALUES (%s,'acme',%s,'npm',%s,'1.0.0','2.0.0',%s,%s,%s,%s,%s,%s,
                           TRUE,'abc123', NOW())""",
                (
                    INSTALLATION, repo, package, extra["pull_request"],
                    extra["merge_outcome"], extra["outcome"], extra["codemod_id"],
                    extra["human_edited"], extra["post_merge_ci"],
                ),
            )

        # An unattributed batch failure: NOT harm (confirmed = false).
        conn.execute(
            """INSERT INTO migration_attempt
                 (installation_id, owner, repo, ecosystem, package, from_version,
                  to_version, outcome, confirmed, failure_step, head_sha)
               VALUES (%s,'acme','unattributed-repo','npm','strip-ansi','1.0.0',
                       '2.0.0','red', FALSE, 'test','abc123')""",
            (INSTALLATION,),
        )

        # --- DECAY -------------------------------------------------------
        conn.execute(
            """INSERT INTO learned_spec (host, url, origin, matched, failures)
               VALUES ('seed.rotted.test','https://seed.rotted.test/openapi.json',
                       'discovered', 4, 3)
               ON CONFLICT (host) DO UPDATE SET failures = 3"""
        )
        conn.execute(
            """INSERT INTO route_mapping
                 (host, spec_fingerprint, from_path, to_path, origin, status)
               VALUES ('seed.mapper.test','fp1','/v1/gone', NULL, 'llm','none')
               ON CONFLICT (host, spec_fingerprint, from_path) DO NOTHING"""
        )
        for i in range(2):
            conn.execute(
                """INSERT INTO api_check_result
                     (installation_id, owner, repo, head_sha, host, conforms,
                      broken_paths, fix_derivable, pull_request)
                   VALUES (%s,'acme','drifting-repo',%s,'api.seed.test', FALSE,
                           '["/v1/designs"]'::jsonb, FALSE, NULL)""",
                (INSTALLATION, f"sha{i}"),
            )
        conn.execute(
            """INSERT INTO installation_state (installation_id, state)
               VALUES (%s,'suspended')
               ON CONFLICT (installation_id) DO UPDATE SET state='suspended'""",
            (INSTALLATION,),
        )

    print("seeded installation", INSTALLATION)


main()
