"""Surface and repair agents pinned to a credential-less AI provider.

A fresh install seeds every agent with ``model="openai"``. If the operator
connects a *different* provider (e.g. MiniMax) those agents would otherwise fail
every task with "openai has no API credentials". The runner already falls back at
call time (see ``runner._first_configured_provider``); these helpers make the
*persisted* config correct (wizard finish) and surface a dashboard hint.
"""
from __future__ import annotations

import logging

from forven.agents.runner import _first_configured_provider, _provider_has_credentials
from forven.db import get_db

log = logging.getLogger("forven.agents.provider_health")


def _agent_provider(row) -> str:
    value = row["model"] if row["model"] is not None else ""
    return str(value).strip() or "openai"


def list_agent_provider_warnings() -> list[dict]:
    """Enabled agents whose configured provider has no usable credentials."""
    alt = _first_configured_provider()
    fallback = alt[0] if alt else None
    warnings: list[dict] = []
    with get_db() as conn:
        rows = conn.execute("SELECT id, model FROM agents WHERE enabled = 1").fetchall()
    for row in rows:
        provider = _agent_provider(row)
        if not _provider_has_credentials(provider):
            warnings.append(
                {"agent_id": row["id"], "provider": provider, "fallback": fallback}
            )
    return warnings


def reconcile_agent_providers() -> dict:
    """Repoint enabled agents whose provider lacks credentials to a configured one.

    No-op (``updated=0``) when no provider is configured. Used at setup-wizard
    finish so connecting a provider makes it the agents' default, and exposed via
    ``POST /api/agents/reconcile-providers`` for manual repair.
    """
    alt = _first_configured_provider()
    if alt is None:
        return {"updated": 0, "provider": None, "model_id": None, "agents": []}
    provider, model_id = alt
    updated: list[str] = []
    with get_db() as conn:
        rows = conn.execute("SELECT id, model FROM agents WHERE enabled = 1").fetchall()
        for row in rows:
            if not _provider_has_credentials(_agent_provider(row)):
                conn.execute(
                    "UPDATE agents SET model = ?, model_id = ? WHERE id = ?",
                    (provider, model_id, row["id"]),
                )
                updated.append(row["id"])
    if updated:
        log.info(
            "Reconciled %d agent(s) to configured provider %s/%s: %s",
            len(updated), provider, model_id, ", ".join(updated),
        )
    return {
        "updated": len(updated),
        "provider": provider,
        "model_id": model_id,
        "agents": updated,
    }
