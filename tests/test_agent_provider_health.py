"""Agents seeded on a credential-less provider are surfaced and repaired.

Regression guard for the fresh-install failure where the wizard connected only
MiniMax but every agent stayed pinned to the seed default ``openai`` and failed
every task.
"""
from __future__ import annotations

from forven.agents import provider_health
from forven.db import get_db


def _insert_agent(agent_id: str, provider: str = "openai") -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agents (id, name, role, model, model_id, enabled) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (agent_id, agent_id, "strategy-developer", provider, ""),
        )


def test_warnings_and_reconcile_repoint_to_configured_provider(forven_db, monkeypatch):
    _insert_agent("sd-test-1", "openai")
    _insert_agent("risk-test-1", "openai")

    # Only MiniMax is configured.
    monkeypatch.setattr(provider_health, "_provider_has_credentials", lambda p: p == "minimax")
    monkeypatch.setattr(provider_health, "_first_configured_provider", lambda: ("minimax", "MiniMax-M2.5"))

    flagged = {w["agent_id"] for w in provider_health.list_agent_provider_warnings()}
    assert {"sd-test-1", "risk-test-1"} <= flagged

    result = provider_health.reconcile_agent_providers()
    assert result["provider"] == "minimax"
    assert result["updated"] >= 2

    with get_db() as conn:
        rows = {r["id"]: r["model"] for r in conn.execute("SELECT id, model FROM agents").fetchall()}
    assert rows["sd-test-1"] == "minimax"
    assert rows["risk-test-1"] == "minimax"

    # Idempotent: the repaired agents no longer warn.
    flagged_after = {w["agent_id"] for w in provider_health.list_agent_provider_warnings()}
    assert "sd-test-1" not in flagged_after
    assert "risk-test-1" not in flagged_after


def test_reconcile_noop_when_nothing_configured(forven_db, monkeypatch):
    _insert_agent("sd-test-2", "openai")
    monkeypatch.setattr(provider_health, "_provider_has_credentials", lambda p: False)
    monkeypatch.setattr(provider_health, "_first_configured_provider", lambda: None)

    result = provider_health.reconcile_agent_providers()
    assert result == {"updated": 0, "provider": None, "model_id": None, "agents": []}

    with get_db() as conn:
        row = conn.execute("SELECT model FROM agents WHERE id = ?", ("sd-test-2",)).fetchone()
    assert row["model"] == "openai"  # untouched
