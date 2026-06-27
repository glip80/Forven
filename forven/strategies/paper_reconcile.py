"""Pure reconciliation between the execution kernel's view and recorded paper trades.

The live/paper scanner, each closed bar, runs the shared ``execution_kernel`` over a
strategy's history (via ``backtest.run_strategy_execution``) and gets a
:class:`~forven.strategies.execution_kernel.KernelResult` — the trades the backtest
WOULD have taken and the position it WOULD currently hold. This module turns that view
plus the strategy's already-recorded paper trades into a list of concrete actions
(open / close / backfill / refresh) for the scanner to apply.

Keeping this logic pure (no DB, no exchange) is what lets us prove, bar-by-bar, that
the resulting paper trades equal the backtest's trades — parity by construction — in a
fast unit test. The scanner layer only has to apply the actions via its existing
persistence/execution calls.

Matching is by ``(direction, entry_time)``: the kernel's ``entry_time`` is the bar's
open timestamp string (``str(df.index[idx])``), identical across runs over a growing
history prefix, so a recorded paper trade and its kernel counterpart line up exactly.
This is robust to missed scan cycles: any kernel-closed trade with no recorded
counterpart is backfilled, so a gap in scanner uptime never loses trades.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from forven.strategies.execution_kernel import KernelResult


ActionKind = Literal["open", "close", "backfill", "refresh", "orphan_close"]


def _ts_lt(a: str, b: str) -> bool:
    """a < b for ISO-ish timestamp strings, tolerant of format drift (space vs 'T',
    with/without tz). Falls back to plain string compare."""
    from datetime import datetime, timezone

    def _parse(s: str):
        s = str(s or "").strip().replace(" ", "T")
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    pa, pb = _parse(a), _parse(b)
    if pa is not None and pb is not None:
        return pa < pb
    return str(a) < str(b)


@dataclass
class ReconcileAction:
    kind: ActionKind
    direction: str
    entry_time: str
    # For open/refresh: the kernel's current open-position state (entry_price,
    # size_fraction, stop_price, target_price, trail_pct, entry_bar, regime, …).
    position: dict | None = None
    # For close/backfill: the kernel's finalized trade dict (exit_price, exit_time,
    # pnl_pct (net), exit_reason, …).
    trade: dict | None = None
    # For close/refresh: the recorded paper trade being acted on.
    recorded: dict | None = None


def _canonical_ts(entry_time: str) -> str:
    """Canonicalize a timestamp string for keying — tolerant of the same format drift
    (space vs 'T', tz present/absent) that ``_ts_lt`` handles, so a recorded OPEN and
    its kernel-finalized close still match when the candle index dtype/tz representation
    shifts between scans. Falls back to the raw string when parsing fails."""
    from datetime import datetime, timezone

    s = str(entry_time or "").strip().replace(" ", "T")
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(entry_time)


def _key(direction: str, entry_time: str) -> tuple[str, str]:
    return (str(direction or "long").strip().lower(), _canonical_ts(entry_time))


def reconcile(res: KernelResult, recorded: list[dict], *, recent_cutoff: str | None = None, window_start: str | None = None) -> list[ReconcileAction]:
    """Diff the kernel's view against recorded paper trades → ordered actions.

    ``recorded`` is the strategy's paper trades (open and closed), each a dict with at
    least ``direction``, ``entry_time`` and ``status`` ("open"/"closed"). The kernel
    ``res`` never force-closes, so ``res.closed_trades`` are real exits and
    ``res.open_positions`` is what should be live now.

    ``recent_cutoff`` (an ISO timestamp string, same format the kernel uses for
    ``entry_time``) bounds what gets RECORDED to "from go-live forward": kernel trades
    that ENTERED before it are treated as pre-tracking — their backfill is suppressed
    and such open positions are NOT adopted (they belong on the chart as triggers, not
    as actual trades). This is what stops a fresh/reset paper book from replaying the
    strategy's ENTIRE would-be history as trades. Closes/refreshes of ALREADY-recorded
    trades always proceed regardless of the cutoff. ``None`` (default) = full replay,
    which is the backtest-parity semantics the tests assert.

    Actions, in apply order:
      * ``close``    — a recorded OPEN trade the kernel has now finalized.
      * ``backfill`` — a kernel-finalized trade (entry ≥ cutoff) with no recorded
                       counterpart (opened & closed between scans) → record it closed.
      * ``open``     — a kernel OPEN position (entry ≥ cutoff) with no recorded
                       counterpart → open it.
      * ``refresh``  — a kernel OPEN position matching a recorded OPEN trade → update
                       its SL/TP/trailing for display.
    """
    def _recent(entry_time: str) -> bool:
        return recent_cutoff is None or str(entry_time) >= recent_cutoff

    recorded_by_key: dict[tuple[str, str], dict] = {}
    recorded_open_by_dir: dict[str, dict] = {}
    for r in recorded:
        recorded_by_key[_key(r.get("direction", "long"), r.get("entry_time"))] = r
        if str(r.get("status") or "open").strip().lower() != "closed":
            recorded_open_by_dir.setdefault(str(r.get("direction") or "long").strip().lower(), r)

    matched: set[int] = set()  # id() of recorded dicts consumed by a close/refresh/adopt

    closes: list[ReconcileAction] = []
    backfills: list[ReconcileAction] = []
    # Closes & backfills, in the kernel's chronological (exit) order.
    for kc in res.closed_trades:
        if kc.get("open_at_end"):
            continue  # defensive; simulate() never emits these
        direction = str(kc.get("direction", "long")).strip().lower()
        entry_time = str(kc.get("entry_time"))
        k = _key(direction, entry_time)
        r = recorded_by_key.get(k)
        if r is None:
            if _recent(entry_time):  # only catch up RECENT missed trades, never the whole history
                backfills.append(ReconcileAction("backfill", direction, entry_time, trade=kc))
        elif str(r.get("status") or "open").strip().lower() != "closed":
            closes.append(ReconcileAction("close", direction, entry_time, trade=kc, recorded=r))
            matched.add(id(r))
        else:
            matched.add(id(r))  # already recorded closed → nothing to do, but it IS matched.

    opens: list[ReconcileAction] = []
    refreshes: list[ReconcileAction] = []
    kernel_open_dirs: set[str] = set()
    for direction, pos in res.open_positions.items():
        direction = str(direction or "long").strip().lower()
        kernel_open_dirs.add(direction)
        entry_time = str(pos.get("entry_time"))
        r = recorded_by_key.get(_key(direction, entry_time))
        if r is not None and str(r.get("status") or "open").strip().lower() != "closed" and id(r) not in matched:
            refreshes.append(ReconcileAction("refresh", direction, entry_time, position=pos, recorded=r))
            matched.add(id(r))
            continue
        # No exact (direction, entry_time) match. Before opening a NEW trade, ADOPT a
        # same-direction recorded OPEN whose entry has DRIFTED — e.g. it was opened on a
        # different data source (the HL→Binance switch) or a non-kernel path, so its
        # kernel_entry_time no longer matches. There is ≤1 open per direction (unique
        # index), so this is unambiguous and avoids a duplicate-open + a stranded orphan.
        r_dir = recorded_open_by_dir.get(direction)
        if r_dir is not None and id(r_dir) not in matched:
            refreshes.append(ReconcileAction("refresh", direction, entry_time, position=pos, recorded=r_dir))
            matched.add(id(r_dir))
        elif _recent(entry_time):  # don't adopt a position that opened before tracking began
            opens.append(ReconcileAction("open", direction, entry_time, position=pos))

    # ORPHAN CLOSE: a recorded OPEN trade the kernel can no longer see (no exact match,
    # not adopted) for a direction the kernel now holds NO position on → the kernel has
    # exited it. Converge by closing it, so paper never holds a trade the strategy/kernel
    # already covered. Guarded to entries WITHIN the evaluated window (the kernel can't
    # speak to an entry older than its history). ``window_start`` None disables the guard
    # (the parity tests pass full history, so it never fires there anyway).
    orphan_closes: list[ReconcileAction] = []
    for direction, r in recorded_open_by_dir.items():
        if id(r) in matched or direction in kernel_open_dirs:
            continue
        entry_time = str(r.get("entry_time") or "")
        if window_start is not None and entry_time and _ts_lt(entry_time, str(window_start)):
            continue  # entry predates the kernel's evaluated window → leave it alone
        orphan_closes.append(ReconcileAction("orphan_close", direction, entry_time, recorded=r))

    # Apply closes/backfills before opens so a same-direction re-entry after an exit is
    # never mistaken for a still-open position; orphan-closes last (pure cleanup).
    return closes + backfills + opens + refreshes + orphan_closes
