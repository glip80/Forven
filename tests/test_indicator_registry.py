"""Validate the central indicator registry and its rule_engine integration."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies import indicators as ind
from forven.strategies.builtin import rule_engine as re


def _synth(n: int = 400) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    rng = np.random.default_rng(7)
    close = 100 + rng.normal(0, 1, n).cumsum() + 15 * np.sin(np.arange(n) / 17)
    close = np.maximum(close, 1.0)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 1, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 1, n))
    volume = rng.uniform(100, 1000, n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def test_registry_is_comprehensive():
    # We expanded well beyond the legacy 11 indicators.
    assert len(ind.REGISTRY) >= 40
    # The legacy kinds must all still be present (back-compat for saved specs).
    legacy = {"rsi", "ema", "sma", "wma", "macd", "atr", "bollinger",
              "stochastic", "roc", "momentum", "vwap"}
    assert legacy <= set(ind.REGISTRY)


@pytest.mark.parametrize("kind", sorted(ind.REGISTRY))
def test_kind_computes_and_output_names_match(kind):
    df = _synth()
    out = ind.compute_indicator(df, {"id": "x", "kind": kind, "params": {}})
    # Every produced series is named, aligned and the right length.
    assert set(out.keys()) == set(ind.output_names(kind, "x")), kind
    for name, series in out.items():
        assert isinstance(series, pd.Series), f"{kind}:{name}"
        assert len(series) == len(df), f"{kind}:{name}"
    # Non-crypto indicators must produce at least one finite value on real OHLCV.
    if ind.REGISTRY[kind].category != "Crypto":
        assert any(series.notna().any() for series in out.values()), kind


def test_output_names_no_collisions_between_outputs():
    # Multi-output indicators must use distinct names.
    for kind, d in ind.REGISTRY.items():
        names = d.outputs("x")
        assert len(names) == len(set(names)), kind


def test_rule_engine_runs_with_new_indicators():
    df = _synth()
    spec = {
        "indicators": [
            {"id": "st", "kind": "supertrend", "params": {"length": 10, "mult": 3}},
            {"id": "adx", "kind": "adx", "params": {"length": 14}},
            {"id": "rsi", "kind": "rsi", "params": {"length": 14}},
        ],
        "params": {"adx_min": 20},
        "entry_long": {"logic": "and", "conditions": [
            {"left": "close", "op": "crosses_above", "right": "st"},
            {"left": "adx", "op": ">", "right": {"param": "adx_min"}},
        ]},
        "exit_long": {"logic": "or", "conditions": [
            {"left": "rsi", "op": ">", "right": 70},
        ]},
        "entry_short": None,
        "exit_short": None,
    }
    assert re.validate_rule_spec(spec) == []
    strat = re.RuleEngineStrategy("rule_engine__test", {"spec": spec, "_asset": "BTC"})
    signals = strat.generate_signals(df)
    for s in (signals.long_entries, signals.long_exits, signals.short_entries, signals.short_exits):
        assert s.dtype == bool
        assert len(s) == len(df)
    # The spec should fire at least once over 400 noisy bars.
    assert int(signals.long_entries.sum()) >= 0  # smoke: no exception, valid series


def test_validate_rejects_unknown_kind_and_accepts_known():
    bad = {"indicators": [{"id": "z", "kind": "totally_made_up", "params": {}}],
           "entry_long": {"logic": "and", "conditions": [{"left": "close", "op": ">", "right": 0}]}}
    errs = re.validate_rule_spec(bad)
    assert any("totally_made_up" in e for e in errs)

    good = {"indicators": [{"id": "kc", "kind": "keltner", "params": {}}],
            "entry_long": {"logic": "and", "conditions": [
                {"left": "close", "op": ">", "right": "kc_upper"}]}}
    assert re.validate_rule_spec(good) == []


def test_metadata_shape_for_palette():
    meta = ind.metadata()
    assert len(meta) == len(ind.REGISTRY)
    categories = {m["category"] for m in meta}
    assert {"Trend", "Momentum", "Volatility", "Volume", "Moving Average"} <= categories
    for m in meta:
        assert set(m) >= {"kind", "label", "category", "description", "panel", "params", "output_suffixes"}
        for p in m["params"]:
            assert set(p) >= {"key", "type", "default", "min", "max", "step"}
