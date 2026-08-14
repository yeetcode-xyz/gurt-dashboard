"""What counts as gurt having made a mistake.

One file, because this is a product judgment and a judgment with two
implementations has two answers. `queries.py` knows how to read the rows; this
knows which rows mean something went wrong.

The distinction that matters most is what is **excluded**:

- `outcome IN ('red', 'blocked')` — gurt tried, validation refused, and **no
  pull request was opened**. That is the gate working. Counting it as harm
  would mean the safest possible run looked like the worst one.
- `merge_outcome = 'closed'` — the maintainer did not want the upgrade. It says
  nothing about whether the transform was correct, which is why `applyOutcome`
  moves no counter for a close. Three teams saying "not this quarter" must not
  read as a broken codemod.
- `confirmed = false` failures — gurt could not attribute a batch failure and
  declined to guess. Honest, and it suppresses nothing.

Harm is narrower and specific: gurt proposed something, it was accepted, and it
turned out to be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

import queries


@dataclass
class Alert:
    """One class of problem, with the rows behind it."""

    key: str
    title: str
    severity: str  # 'harm' | 'decay'
    #: What this means and why it is (or is not) gurt's fault.
    explanation: str
    rows: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def count(self) -> int:
        return 0 if self.rows is None or self.rows.empty else len(self.rows)


def harm(days: int) -> list[Alert]:
    """gurt shipped something and it was wrong. Most severe first."""
    found = [
        Alert(
            key="reverted",
            title="Reverted after merge",
            severity="harm",
            explanation=(
                "A human undid a change gurt merged. The strongest signal available: "
                "the upgrade shipped, someone lived with it, and took it back out. "
                "Detected from lockfile state rather than commit messages, so a "
                "squash or a hand-written rollback counts the same as `git revert`."
            ),
            rows=queries.reverted_attempts(days),
        ),
        Alert(
            key="post_merge_ci",
            title="Merged, then their CI failed",
            severity="harm",
            explanation=(
                "gurt's validation passed in its own sandbox and the repository's "
                "own CI failed afterwards. The only signal that observes the "
                "customer's environment, which is what makes it worth more than a "
                "green validation."
            ),
            rows=queries.post_merge_ci_failures(days),
        ),
        Alert(
            key="repaired",
            title="A human repaired it before merging",
            severity="harm",
            explanation=(
                "The upgrade shipped, but somebody had to edit gurt's branch to "
                "make it work. Softer than a revert — the outcome was fine — but "
                "the transform was not right as written, and that is the thing "
                "being measured."
            ),
            rows=queries.repaired_before_merge(days),
        ),
        Alert(
            key="demoted",
            title="Codemods pulled back for review",
            severity="harm",
            explanation=(
                "A rollback demoted these, so they are no longer served to anyone. "
                "Permanent until a human intervenes: the counters that would "
                "re-promote them are the ones already proven wrong. Not date "
                "filtered, because a demotion does not age out."
            ),
            rows=queries.demoted_codemods(),
        ),
        Alert(
            key="crashed",
            title="Runs that crashed",
            severity="harm",
            explanation=(
                "`outcome = 'error'` — gurt itself failed rather than the upgrade "
                "failing. Not harm to a repository, but a defect in gurt, and the "
                "one class here that is always a bug."
            ),
            rows=queries.crashed_runs(days),
        ),
    ]
    return [alert for alert in found if alert.count]


def decay(days: int) -> list[Alert]:
    """gurt failing quietly — no bad PR, just less working than it looks."""
    found: list[Alert] = []

    if not queries.builder_has_run():
        found.append(
            Alert(
                key="builder_never_ran",
                title="The builder has never run",
                severity="decay",
                explanation=(
                    "`codemod` is empty, so every repository is deriving from "
                    "scratch what one build could serve to all of them. This is "
                    "the cache-first economics the whole builder/servicer split "
                    "exists for, currently switched off. Run "
                    "`gurt-app build --queue`."
                ),
                rows=queries.unbuilt_pairs(),
            )
        )

    for alert in (
        Alert(
            key="retired_specs",
            title="Spec locations retired after repeated failures",
            severity="decay",
            explanation=(
                "These URLs failed three or more times, so gurt stopped serving "
                "them. The host now has no contract at all and silently falls back "
                "to probing — the API check quietly does less than it appears to."
            ),
            rows=queries.retired_spec_locations(),
        ),
        Alert(
            key="unfixed_drift",
            title="API drift found repeatedly and never fixed",
            severity="decay",
            explanation=(
                "Drift was detected on more than one run for these host/repo pairs "
                "and no pull request was ever opened. Usually means no contract "
                "authorised a rewrite — a derived spec reports but never fixes — "
                "so the drift is known and nobody is acting on it."
            ),
            rows=queries.unfixed_api_drift(),
        ),
        Alert(
            key="mapper_misses",
            title="Route mapper misses piling up",
            severity="decay",
            explanation=(
                "Paths the mapper was asked about and could not resolve, cached as "
                "`none` so they are not re-asked. Expected in small numbers; a "
                "large count on one host means the spec and the call sites have "
                "diverged further than the mapper can bridge."
            ),
            rows=queries.mapper_misses(),
        ),
        Alert(
            key="stalled",
            title="Codemods stuck at candidate",
            severity="decay",
            explanation=(
                "Repositories have used these and they have still not reached "
                "`validated`. Either they are not merging cleanly, or the merges "
                "are not being observed — both worth knowing, because a codemod "
                "that never promotes is one nobody else can benefit from."
            ),
            rows=queries.stalled_codemods(),
        ),
        Alert(
            key="suspended",
            title="Installations switched off",
            severity="decay",
            explanation=(
                "gurt is suspended or deleted for these installations and is doing "
                "no work for them. Listed because an installation that went quiet "
                "months ago is easy to forget about."
            ),
            rows=queries.suspended_installations(),
        ),
    ):
        if alert.count:
            found.append(alert)

    return found


def working_as_intended(days: int) -> dict[str, pd.DataFrame]:
    """Deliberately *not* alerts. Shown so their absence is never mistaken.

    Every frame here is gurt behaving correctly. They are surfaced because an
    empty mistakes panel is only reassuring if you can see the pipeline is
    actually running and refusing things.
    """
    return {
        "refusals": queries.refusals(days),
        "declined": queries.declined_prs(days),
        "unattributed": queries.unattributed_failures(days),
    }


def headline(harm_alerts: list[Alert], decay_alerts: list[Alert]) -> tuple[str, str]:
    """The banner: a severity and a one-line summary."""
    harm_count = sum(alert.count for alert in harm_alerts)
    decay_count = sum(alert.count for alert in decay_alerts)

    if harm_count:
        parts = [f"{alert.count} {alert.title.lower()}" for alert in harm_alerts]
        return "error", f"{harm_count} recorded mistakes — " + "; ".join(parts)
    if decay_count:
        parts = [f"{alert.count} {alert.title.lower()}" for alert in decay_alerts]
        return "warning", "No mistakes, but: " + "; ".join(parts)
    return "success", "No recorded mistakes and nothing decaying."
