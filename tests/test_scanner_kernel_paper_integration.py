"""Integration: the live scanner's kernel-driven paper path persists trades that
match the backtest.

Drives ``scanner.manage_positions_via_kernel`` bar-by-bar against a REAL (temp) DB —
exactly how the scan loop calls it — and asserts the recorded paper trades equal the
backtest's, including the net PnL stored on each closed trade. This verifies the whole
wiring on top of the already-proven parity logic: config resolution, candle fetch,
reconcile, persistence (open/close/fill), and the net-PnL override.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from forven.strategies import backtest as bt
from forven.strategies import execution_kernel as ek
from forven.strategies.builtin.rsi_momentum import RSIMomentumStrategy
from forven.trade_state import parse_trade_signal_data


def _frame(n: int = 420, seed: int = 4) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0005, 0.02, size=n).cumsum()
    close = 100.0 * np.exp(steps)
    spread = np.abs(rng.normal(0.0, 0.012, size=n)) + 0.004
    high = close * (1.0 + spread)
    low = close * (1.0 - spread)
    openp = np.empty(n)
    openp[0] = close[0]
    openp[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.004, size=n - 1))
    high = np.maximum.reduce([high, openp, close])
    low = np.minimum.reduce([low, openp, close])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {"open": openp, "high": high, "low": low, "close": close, "volume": 1000.0},
        index=idx,
    )


def test_kernel_paper_path_persists_trades_matching_backtest(forven_db, monkeypatch):
    import forven.scanner as scanner

    strat_id = "PAPER-RSI-1"
    asset = "BTC"
    params = {
        "rsi_period": 14, "rsi_entry": 45, "rsi_exit": 55,
        "ema_fast": 10, "ema_slow": 30, "adx_period": 14, "adx_min": 0,
        "leverage": 2.0,
        "execution_profile": {
            "sizing_mode": "fraction", "risk_per_trade": 0.01,
            "stop_loss_pct": 3.0, "take_profit_pct": 5.0,
        },
    }
    strat = {
        "id": strat_id, "asset": asset, "type": "rsi_momentum",
        "runtime_type": "rsi_momentum", "timeframe": "1h",
        "stage": "paper", "params": params,
    }
    strategy = RSIMomentumStrategy(strat_id, dict(params, _asset=asset))

    df = _frame()

    # Drive the scanner's candle fetch from a growing prefix (one new closed bar per
    # cycle), and keep enrich/trim as identity so the kernel sees exactly our frame.
    state = {"i": len(df)}
    monkeypatch.setattr(scanner, "fetch_candles", lambda coin, bars=300, interval="1h": df.iloc[: state["i"]].copy())
    monkeypatch.setattr(scanner, "_enrich_scan_frame", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "_trim_unclosed_latest_candle", lambda d, *a, **k: d)
    monkeypatch.setattr(scanner, "register", lambda *a, **k: None)
    monkeypatch.setattr("forven.strategies.registry.get_active", lambda: {strat_id: strategy})

    # Backtest reference, using the SAME config the scanner resolves.
    _, fee_bps, slip_bps = scanner._resolve_trade_assumptions(params)
    leverage = float(params["leverage"])
    ec = bt.execution_controls_from_params(params)
    ref = bt.run_strategy_execution(
        df, strategy, params=params, warmup=200, leverage=leverage,
        fee_bps=fee_bps, slippage_bps=slip_bps, regime_gate=False,
        trade_mode="long_only", execution_controls=ec, initial_capital=10000.0,
        strategy_type="rsi_momentum",
    )
    drag = ek.round_trip_drag(fee_bps, slip_bps, leverage)
    bt_trades = ek.force_close(ref, df, leverage=leverage, round_trip_drag=drag, trade_mode="long_only")
    bt_closed = sorted([t for t in bt_trades if not t.get("open_at_end")], key=lambda t: t["entry_bar"])
    bt_open = [t for t in bt_trades if t.get("open_at_end")]

    # Bar-by-bar scanner cycles.
    for i in range(202, len(df) + 1):
        state["i"] = i
        scanner.manage_positions_via_kernel(strat_id, strat, account_equity=10000.0)

    # Read what the scanner persisted.
    from forven.db import get_db
    with get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE COALESCE(strategy_id, strategy) = ?", (strat_id,)
        ).fetchall()]

    def _kbar(r):
        return int(parse_trade_signal_data(r.get("signal_data")).get("kernel_entry_bar") or 0)

    closed = sorted([r for r in rows if str(r["status"]).upper() == "CLOSED"], key=_kbar)
    open_rows = [r for r in rows if str(r["status"]).upper() == "OPEN"]

    assert len(closed) == len(bt_closed), f"closed count paper={len(closed)} backtest={len(bt_closed)}"
    for b, r in zip(bt_closed, closed):
        sd = parse_trade_signal_data(r.get("signal_data"))
        assert r["direction"] == b["direction"]
        assert sd.get("kernel_entry_time") == b["entry_time"]
        assert float(r["entry_price"]) == pytest.approx(b["entry_price"])
        assert float(r["exit_price"]) == pytest.approx(b["exit_price"])
        # The stored pnl_pct must be the kernel's NET equity-fraction pnl.
        assert float(r["pnl_pct"]) == pytest.approx(b["pnl_pct"], abs=1e-6)
        assert sd.get("close_reason") == b["exit_reason"]
        assert round(float(sd.get("kernel_size_fraction")), 4) == b["size_fraction"]

    # The scanner leaves the final position open; the backtest force-closed it.
    assert len(open_rows) == len(bt_open)
    for b, r in zip(bt_open, open_rows):
        sd = parse_trade_signal_data(r.get("signal_data"))
        assert r["direction"] == b["direction"]
        assert sd.get("kernel_entry_time") == b["entry_time"]
        assert float(r["entry_price"]) == pytest.approx(b["entry_price"])
