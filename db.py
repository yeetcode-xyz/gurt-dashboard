"""Read-only access to the gurt store.

One connection, opened once and reused; every query goes through `fetch`, and
every result is cached for a minute so leaning on the refresh button does not
hammer a production database.

Two things here are deliberate rather than incidental:

**TLS is verified against a named CA.** Supabase signs its certificates with
"Supabase Root 2021 CA", which is not in the system trust store, so a verifying
connection fails without it. The tempting fix is `sslmode=require`, which keeps
the traffic encrypted while accepting *any* certificate — anything able to
intercept the connection could then read and rewrite every query. The App
refuses that (see `PostgresStore`), and so does this.

**The session is read-only.** A dashboard has no business writing, and saying
so at connect time means a stray `INSERT` fails on its own rather than relying
on someone having remembered to grant the role correctly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import psycopg
import streamlit as st
from psycopg.rows import dict_row

# Resolved from __file__ rather than the working directory, because Streamlit
# Community Cloud runs the app from the repository root while `streamlit run
# app.py` runs it from here, and the certificate has to be found either way.
ROOT = Path(__file__).resolve().parent
DEFAULT_CA = ROOT / "certs" / "supabase-prod-ca-2021.crt"


class NotConfigured(RuntimeError):
    """No DATABASE_URL anywhere. Not a crash — the app renders a setup page."""


def _secret(name: str) -> str | None:
    """`st.secrets` first, then the environment, so an existing `.env` works."""
    try:
        value = st.secrets.get(name)  # type: ignore[union-attr]
        if value:
            return str(value)
    except Exception:
        # No secrets.toml at all is the normal local case, not an error.
        pass
    return os.environ.get(name)


def database_url() -> str:
    url = _secret("DATABASE_URL")
    if not url:
        raise NotConfigured(
            "DATABASE_URL is not set. Put it in dashboard/.streamlit/secrets.toml "
            "or export it (the repo's .env already has one)."
        )
    return url


def ca_path() -> str | None:
    """The CA bundle to verify against, when one is configured or vendored."""
    explicit = _secret("PGSSLROOTCERT")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            path = ROOT / path
        return str(path) if path.exists() else None
    return str(DEFAULT_CA) if DEFAULT_CA.exists() else None


def _without_sslmode(url: str) -> str:
    """Drop `sslmode` from the URL so the explicit argument is what applies.

    Same reason the TypeScript store does it: a stray `sslmode=require` in a
    copy-pasted connection string would otherwise quietly win and turn
    verification off, which is the one setting that must not be decided by
    whichever string somebody pasted.
    """
    cleaned = re.sub(r"([?&])sslmode=[^&]*&?", r"\1", url)
    return cleaned.rstrip("?&")


def is_local(url: str) -> bool:
    """Does this connection stay on the machine?

    A throwaway Postgres in a container speaks no TLS at all, so demanding
    `verify-full` of it fails outright — which is how the first version of this
    file could not talk to its own test database. Loopback traffic has nothing
    to intercept, so plain is correct there and verification stays mandatory
    for everything that leaves the host.
    """
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", ""} or host.endswith(".localhost")


@st.cache_resource(show_spinner=False)
def get_connection() -> psycopg.Connection:
    url = database_url()
    kwargs: dict[str, object] = {"row_factory": dict_row, "autocommit": True}

    if is_local(url):
        # No TLS settings at all; whatever the URL says applies.
        conn = psycopg.connect(url, **kwargs)  # type: ignore[arg-type]
    else:
        # Remote: verify, always. `sslmode` is stripped from the URL first so a
        # stray `sslmode=require` in a pasted connection string cannot quietly
        # win — that setting encrypts while accepting *any* certificate, and it
        # is the one thing that must not be decided by copy-paste.
        ca = ca_path()
        kwargs["sslmode"] = "verify-full"
        if ca:
            kwargs["sslrootcert"] = ca
        conn = psycopg.connect(_without_sslmode(url), **kwargs)  # type: ignore[arg-type]

    # Belt and braces: the role should be read-only, but this fails a write
    # even if somebody points the dashboard at a full-privilege account.
    conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    return conn


@st.cache_data(ttl=60, show_spinner=False)
def fetch(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Run a query and return a DataFrame. Cached for a minute."""
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
    except psycopg.errors.UndefinedTable:
        # A table this query needs has not been created yet.
        #
        # Not an error worth a traceback: a database on an older migration is a
        # normal thing to open the dashboard against, and the alternative is
        # every page dying on the first table it touches. The sidebar already
        # names what is missing and says to run `gurt-app migrate`; a panel
        # that reads "nothing here" is the honest rendering of a table that
        # does not exist.
        return pd.DataFrame()
    except psycopg.Error:
        # Anything else may have left the connection unusable; reconnecting
        # next call is cheaper than reasoning about which errors do.
        get_connection.clear()
        raise
    return pd.DataFrame(rows)


def scalar(sql: str, params: tuple = ()) -> int:
    """First column of the first row, or 0. For counts."""
    frame = fetch(sql, params)
    if frame.empty:
        return 0
    return int(frame.iloc[0, 0])


@st.cache_data(ttl=60, show_spinner=False)
def existing_tables() -> set[str]:
    """Which of gurt's tables actually exist.

    A database that has not had `gurt-app migrate` run against it, or one on an
    older migration, is a normal state to open the dashboard on — every page
    checks rather than throwing a `relation does not exist` traceback at
    somebody who is trying to work out why nothing is there.
    """
    frame = fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    return set() if frame.empty else set(frame["table_name"])


def missing_tables() -> list[str]:
    return sorted(set(ALL_TABLES) - existing_tables())


ALL_TABLES = [
    "api_check_result",
    "breaking_change",
    "codemod",
    "derived_spec",
    "gurt_migration",
    "installation_state",
    "learned_spec",
    "migration_attempt",
    "package_preference",
    "probe_result",
    "registry_fact",
    "repo_snapshot",
    "route_mapping",
    "spec_document",
    "spec_snapshot",
]
