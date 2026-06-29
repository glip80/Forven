"""ChromaDB integration — local vector store for quantitative data.

Stores structured quant data that agents query:
- Backtest results (strategy_id, metrics, params)
- Trade post-mortems
- Research hypotheses
- Execution slippage samples

ChromaDB also hosts agent narratives (lessons, conversations) and
powers fast local strategy/backtest results lookup.
"""

import json
import logging
import subprocess
import sys
import threading
import math
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from forven.config import FORVEN_HOME

log = logging.getLogger("forven.vectordb")

CHROMA_DIR = FORVEN_HOME / "chromadb"

# Lazy import — chromadb is imported on first use to avoid ONNX segfault at
# module load time.  The _chroma_available flag gates all operations.
chromadb = None  # type: ignore[assignment]
_chroma_imported = False
_chroma_available: bool | None = None  # None = not yet tested
_client = None
_client_lock = threading.Lock()
_upsert_lock = threading.Lock()
_upsert_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="forven-chroma")
_HEALTH_COLLECTION_NAME = "forven_health_check"
_HEALTH_DOCUMENT_ID = "hc"
_HEALTH_METADATA = {"source": "forven_health_check"}
_DISABLE_IN_PROCESS_ENV = "FORVEN_DISABLE_CHROMA_IN_PROCESS"
_disable_log_emitted = False


def _in_process_chroma_disabled() -> bool:
    global _disable_log_emitted
    raw = str(__import__("os").environ.get(_DISABLE_IN_PROCESS_ENV, "")).strip().lower()
    disabled = raw in {"1", "true", "yes", "on", "y"}
    if disabled and not _disable_log_emitted:
        log.warning(
            "In-process ChromaDB disabled via %s; vector recall will be skipped in this process.",
            _DISABLE_IN_PROCESS_ENV,
        )
        _disable_log_emitted = True
    return disabled


def _check_chroma_available() -> bool:
    """Test whether ChromaDB can be initialised without segfaulting.

    Runs a tiny subprocess that imports chromadb and creates a PersistentClient.
    If the subprocess exits cleanly the runtime is safe; if it segfaults
    (exit code 3221225477 on Windows / 139 on Linux) we know ONNX is broken
    and disable all in-process ChromaDB usage for the lifetime of this process.
    """
    global _chroma_available
    if _chroma_available is not None:
        return _chroma_available
    if _in_process_chroma_disabled():
        _chroma_available = False
        return False

    # Test a full round-trip: create client, get collection, and do a small
    # upsert + query.  The ONNX segfault triggers on embedding operations,
    # not on client creation alone.
    script = (
        "import chromadb, pathlib; "
        f"p = pathlib.Path(r'{CHROMA_DIR}'); p.mkdir(parents=True, exist_ok=True); "
        f"c = chromadb.PersistentClient(path=str(p)); "
        f"col = c.get_or_create_collection('{_HEALTH_COLLECTION_NAME}', metadata={{'hnsw:space': 'cosine'}}); "
        f"col.upsert(ids=['{_HEALTH_DOCUMENT_ID}'], documents=['health check'], metadatas=[{_HEALTH_METADATA!r}]); "
        "col.query(query_texts=['test'], n_results=1); "
        "print('OK')"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and "OK" in (proc.stdout or ""):
            _chroma_available = True
            log.info("ChromaDB availability check: OK (in-process operations enabled)")
        else:
            _chroma_available = False
            log.warning(
                "ChromaDB availability check FAILED (exit %d). stderr=%s. "
                "All ChromaDB operations will use subprocess isolation or be skipped.",
                proc.returncode,
                (proc.stderr or "").strip()[:300],
            )
    except Exception as exc:
        _chroma_available = False
        log.warning("ChromaDB availability check error: %s. Operations will be skipped.", exc)

    return _chroma_available


def _ensure_chromadb_imported():
    """Lazily import chromadb on first use."""
    global chromadb, _chroma_imported
    if _chroma_imported:
        return
    try:
        import chromadb as _chromadb_mod
        chromadb = _chromadb_mod
        _chroma_imported = True
    except ImportError:
        log.warning("chromadb package not installed; vector store disabled.")
        _chroma_imported = True  # don't retry


def _coerce_optional_float(value) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _normalize_pct_value(value: float) -> float:
    """Values are already in percent points (e.g. 12.0 means 12%). Pass through."""
    return value


def _timeframe_to_minutes(value: str | None) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    match = re.fullmatch(r"(\d+)\s*([mhdw])", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2)
    scale = {"m": 1.0, "h": 60.0, "d": 1440.0, "w": 10080.0}.get(unit)
    if not scale:
        return None
    return amount * scale


def _estimate_backtest_months(metrics: dict, params: dict) -> float | None:
    explicit = _coerce_optional_float(metrics.get("lookback_months"))
    if explicit is None:
        explicit = _coerce_optional_float(metrics.get("backtest_months"))
    if explicit is not None and explicit > 0:
        return explicit

    start_raw = metrics.get("start_date") or metrics.get("start")
    end_raw = metrics.get("end_date") or metrics.get("end")
    if start_raw and end_raw:
        try:
            start_dt = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
            delta_seconds = (end_dt - start_dt).total_seconds()
            if delta_seconds > 0:
                return delta_seconds / (60.0 * 60.0 * 24.0 * 30.4375)
        except Exception:
            pass

    bars = _coerce_optional_float(
        metrics.get("total_bars")
        or params.get("bars")
        or params.get("total_bars")
        or params.get("lookback_bars")
    )
    if bars is None or bars <= 0:
        return None

    timeframe = str(metrics.get("timeframe") or params.get("timeframe") or "1h")
    tf_minutes = _timeframe_to_minutes(timeframe) or 60.0
    months = (bars * tf_minutes) / (60.0 * 24.0 * 30.4375)
    return months if months > 0 else None


def _derive_return_metrics(metrics: dict, params: dict) -> tuple[float | None, float | None, float | None]:
    total_return_raw = _coerce_optional_float(metrics.get("total_return_pct"))
    if total_return_raw is None:
        total_return_raw = _coerce_optional_float(metrics.get("total_return"))
    if total_return_raw is None:
        return None, None, None

    monthly_return_pct = _coerce_optional_float(metrics.get("monthly_return_pct"))
    annualized_return_pct = _coerce_optional_float(metrics.get("annualized_return_pct"))
    backtest_months = _coerce_optional_float(metrics.get("lookback_months"))
    if backtest_months is None:
        backtest_months = _coerce_optional_float(metrics.get("backtest_months"))
    if backtest_months is not None and backtest_months <= 0:
        backtest_months = None
    if backtest_months is None:
        backtest_months = _estimate_backtest_months(metrics, params)

    if backtest_months is not None and backtest_months > 0:
        growth = 1.0 + (float(total_return_raw) / 100.0)
        if monthly_return_pct is None:
            if growth > 0:
                monthly_return_pct = (pow(growth, 1.0 / backtest_months) - 1.0) * 100.0
            else:
                monthly_return_pct = float(total_return_raw) / backtest_months
        if annualized_return_pct is None:
            if growth > 0:
                annualized_return_pct = (pow(growth, 12.0 / backtest_months) - 1.0) * 100.0
            else:
                annualized_return_pct = float(total_return_raw) * (12.0 / backtest_months)

    return monthly_return_pct, annualized_return_pct, backtest_months


def _safe_json_dumps(value, fallback: str = "{}") -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return fallback


def _extract_lifecycle_strategy_id(*candidates: str) -> str | None:
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        if re.fullmatch(r"S\d{4,6}", text, re.IGNORECASE):
            return text.upper()
        match = re.search(r"\bS\d{4,6}\b", text, re.IGNORECASE)
        if match:
            return match.group(0).upper()
    return None


def get_client():
    """Get or create the persistent ChromaDB client.

    Returns None if ChromaDB is unavailable (ONNX segfault on this platform).
    """
    global _client
    _ensure_chromadb_imported()
    if chromadb is None:
        return None
    if _in_process_chroma_disabled():
        return None
    if not _check_chroma_available():
        return None
    if _client is None:
        with _client_lock:
            if _client is None:
                CHROMA_DIR.mkdir(parents=True, exist_ok=True)
                _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


def get_collection(name: str):
    """Get or create a collection.

    Returns None if ChromaDB is unavailable.
    """
    client = get_client()
    if client is None:
        return None
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def _upsert_subprocess(collection_name: str, ids: list[str], documents: list[str], metadatas: list[dict]):
    """Run the ChromaDB upsert in a subprocess to isolate segfaults.

    ChromaDB's ONNX runtime can segfault on Windows.  Running the upsert in a
    child process prevents the crash from taking down the uvicorn server.
    """
    import textwrap

    payload = json.dumps({
        "chroma_path": str(CHROMA_DIR),
        "collection": collection_name,
        "ids": ids,
        "documents": documents,
        "metadatas": metadatas,
    })
    script = textwrap.dedent("""\
        import pathlib
        import sys, json

        import chromadb

        data = json.loads(sys.stdin.read())
        chroma_path = pathlib.Path(data["chroma_path"])
        chroma_path.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(chroma_path))
        col = client.get_or_create_collection(
            data["collection"],
            metadata={"hnsw:space": "cosine"},
        )
        col.upsert(ids=data["ids"], documents=data["documents"], metadatas=data["metadatas"])
        log.info("Vector upsert OK")
    """)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            log.warning(
                "ChromaDB upsert subprocess failed (exit %d) for %s: %s",
                proc.returncode, collection_name, (proc.stderr or "")[:200],
            )
        else:
            log.debug("ChromaDB upsert subprocess OK for %s", collection_name)
    except Exception as exc:
        log.warning("ChromaDB upsert subprocess error for %s: %s", collection_name, exc)


def _upsert_sync(collection_name: str, ids: list[str], documents: list[str], metadatas: list[dict]):
    """Perform a Chroma upsert synchronously (CPU-heavy embedding path)."""
    col = get_collection(collection_name)
    with _upsert_lock:
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)


def _upsert(collection_name: str, ids: list[str], documents: list[str], metadatas: list[dict]):
    """Upsert without blocking an active asyncio event loop.

    Prefers a subprocess-based approach to isolate ChromaDB segfaults on
    Windows.  Falls back to in-process only if the subprocess path is
    explicitly disabled.
    """
    # Check availability in the MAIN process first to avoid spawning
    # subprocesses that will only fail.  Each subprocess spawns its own
    # health-check subprocess, creating a chain that can deadlock the
    # server under load (file locks on the ChromaDB directory + limited
    # thread pool workers).
    if not _check_chroma_available():
        log.debug(
            "Skipping ChromaDB upsert for %s — embeddings unavailable on this platform.",
            collection_name,
        )
        return
    # Use subprocess isolation to prevent segfaults from crashing the server
    _upsert_subprocess(collection_name, ids, documents, metadatas)
    return


# ── Backtest Results Collection ──────────────────────────────────────────────

def store_backtest_result(
    strategy_id: str, asset: str, strategy_type: str,
    params: dict, metrics: dict, fitness: float,
    *,
    result_id: str | None = None,
    job_id: str | None = None,
    strategy_name: str | None = None,
    lifecycle_strategy_id: str | None = None,
    config: dict | None = None,
    definition_json: dict | None = None,
    result_type: str = "backtest",
):
    """Store a backtest result for future strategy research."""
    recorded_at = datetime.now(timezone.utc).isoformat()
    doc_id = str(result_id or "").strip()
    if not doc_id:
        # Keep every run as a unique result row instead of overwriting by params hash.
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        param_hash = hash(_safe_json_dumps(params, fallback="{}")) & 0xFFFF
        doc_id = f"{strategy_id}-{ts_ms}-{param_hash:04x}"

    canonical_strategy_name = str(strategy_name or strategy_id or "").strip() or str(strategy_id)
    canonical_lifecycle_id = _extract_lifecycle_strategy_id(
        lifecycle_strategy_id or "",
        strategy_id,
        canonical_strategy_name,
        doc_id,
    )
    canonical_job_id = str(job_id or result_id or doc_id).strip() or doc_id

    document = (
        f"Strategy {canonical_strategy_name} ({strategy_type}) on {asset}: "
        f"Sharpe={metrics.get('sharpe', 0):.2f}, "
        f"WinRate={metrics.get('win_rate', 0):.1%}, "
        f"PF={metrics.get('profit_factor', 0):.2f}, "
        f"MaxDD={metrics.get('max_drawdown_pct', 0):.2%}, "
        f"Fitness={fitness:.1f}. "
        f"Params: {_safe_json_dumps(params)}"
    )

    monthly_return_pct, annualized_return_pct, backtest_months = _derive_return_metrics(metrics, params)

    timeframe = str(metrics.get("timeframe") or params.get("timeframe") or "").strip()
    start_date = str(metrics.get("start_date") or metrics.get("start") or "").strip()
    end_date = str(metrics.get("end_date") or metrics.get("end") or "").strip()
    evaluation_start_date = str(
        metrics.get("evaluation_start_date")
        or metrics.get("evaluation_start")
        or ""
    ).strip()
    evaluation_end_date = str(
        metrics.get("evaluation_end_date")
        or metrics.get("evaluation_end")
        or ""
    ).strip()
    evaluation_backtest_months = _coerce_optional_float(
        metrics.get("evaluation_backtest_months")
        or metrics.get("evaluation_months")
    )

    config_payload = dict(config) if isinstance(config, dict) else {}
    if "strategy_id" not in config_payload:
        config_payload["strategy_id"] = strategy_id
    if "strategy_name" not in config_payload:
        config_payload["strategy_name"] = canonical_strategy_name
    if "strategy_type" not in config_payload:
        config_payload["strategy_type"] = strategy_type
    if "symbol" not in config_payload and asset:
        config_payload["symbol"] = asset
    if "asset" not in config_payload and asset:
        config_payload["asset"] = asset
    if "timeframe" not in config_payload and timeframe:
        config_payload["timeframe"] = timeframe
    if "params" not in config_payload and isinstance(params, dict):
        config_payload["params"] = params
    if "start" not in config_payload and start_date:
        config_payload["start"] = start_date
    if "end" not in config_payload and end_date:
        config_payload["end"] = end_date
    if isinstance(definition_json, dict) and definition_json:
        config_payload["definition_json"] = definition_json

    metadata = {
        "strategy_id": strategy_id,
        "strategy_name": canonical_strategy_name,
        "lifecycle_strategy_id": canonical_lifecycle_id or strategy_id,
        "result_type": str(result_type or "backtest").strip().lower() or "backtest",
        "asset": asset,
        "strategy_type": strategy_type,
        "fitness": fitness,
        "job_id": canonical_job_id,
        "sharpe": float(metrics.get("sharpe", 0)),
        "total_return_pct": float(metrics.get("total_return_pct", 0)),
        "monthly_return_pct": float(metrics["monthly_return_pct"]) if metrics.get("monthly_return_pct") not in (None, "", 0) else -999.0,
        "annualized_return_pct": float(metrics["annualized_return_pct"]) if metrics.get("annualized_return_pct") not in (None, "", 0) else -999.0,
        "backtest_months": float(metrics["backtest_months"]) if metrics.get("backtest_months") not in (None, "", 0) else -999.0,
        "win_rate": float(metrics.get("win_rate", 0)),
        "profit_factor": float(metrics.get("profit_factor", 0)),
        "max_drawdown": float(metrics.get("max_drawdown_pct", 0)),
        "total_trades": int(metrics.get("total_trades", 0)),
        "recorded_at": recorded_at,
        "config_json": _safe_json_dumps(config_payload),
        "params_json": _safe_json_dumps(params),
    }
    if isinstance(definition_json, dict) and definition_json:
        metadata["definition_json"] = _safe_json_dumps(definition_json)
    if monthly_return_pct is not None:
        metadata["monthly_return_pct"] = float(monthly_return_pct)
    if annualized_return_pct is not None:
        metadata["annualized_return_pct"] = float(annualized_return_pct)
    if backtest_months is not None and backtest_months > 0:
        metadata["backtest_months"] = float(backtest_months)

    if timeframe:
        metadata["timeframe"] = timeframe
    if start_date:
        metadata["start_date"] = start_date
    if end_date:
        metadata["end_date"] = end_date
    if evaluation_start_date:
        metadata["evaluation_start_date"] = evaluation_start_date
    if evaluation_end_date:
        metadata["evaluation_end_date"] = evaluation_end_date
    if evaluation_backtest_months is not None and evaluation_backtest_months > 0:
        metadata["evaluation_backtest_months"] = float(evaluation_backtest_months)

    _upsert("backtest_results", [doc_id], [document], [metadata])
    log.debug("Stored backtest result: %s (fitness=%.1f)", strategy_id, fitness)

    # Quant Learning Loop: extract insights from this backtest
    try:
        from forven.quant_skills_extractor import maybe_extract
        maybe_extract({
            "strategy_id": strategy_id,
            "strategy_name": canonical_strategy_name,
            "strategy_type": strategy_type,
            "asset": asset,
            "params": params,
            "metrics": metrics,
            "regime": str(config_payload.get("regime", "")),
        })
    except Exception as exc:
        log.debug("Quant skill extraction skipped: %s", exc)


def search_backtest_results(
    query: str, n_results: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """Search backtest results by semantic similarity."""
    col = get_collection("backtest_results")
    if col is None or col.count() == 0:
        return []
    n_results = min(n_results, col.count())
    kwargs = {"query_texts": [query], "n_results": n_results}
    if where:
        kwargs["where"] = where
    results = col.query(**kwargs)
    return _flatten_results(results)


# ── Trade Post-Mortems Collection ────────────────────────────────────────────

def store_post_mortem(
    trade_id: str, strategy: str, asset: str,
    pnl_pct: float, analysis: str,
    *,
    failure_category: str | None = None,
    observed_regime: str | None = None,
    backtest_vs_live_delta: dict | None = None,
    catching_test: str | None = None,
):
    """Store a trade post-mortem for learning.

    P3-6: Structured post-mortem with required taxonomy fields.
    """
    metadata = {
        "strategy": strategy,
        "asset": asset,
        "pnl_pct": pnl_pct,
        "outcome": "win" if pnl_pct > 0 else "loss",
    }
    # P3-6: Structured fields
    if failure_category:
        metadata["failure_category"] = failure_category
    if observed_regime:
        metadata["observed_regime"] = observed_regime
    if catching_test:
        metadata["catching_test"] = catching_test

    _upsert(
        "trade_post_mortems",
        [trade_id],
        [analysis],
        [metadata],
    )


# P3-6: Structured post-mortem template schema
POST_MORTEM_TEMPLATE = {
    "required_fields": [
        "failure_category",  # regime_shift, parameter_fragility, execution_gap, cost_erosion, insufficient_edge
        "observed_regime",  # TREND_UP, TREND_DOWN, RANGE_BOUND, HIGH_VOL
        "backtest_vs_live_deltas",  # {sharpe_delta, dd_delta, pf_delta}
        "catching_test",  # which robustness test would have caught this (if any)
    ],
    "failure_categories": [
        "regime_shift",
        "parameter_fragility",
        "execution_gap",
        "cost_erosion",
        "insufficient_edge",
    ],
}


def search_post_mortems(query: str, n_results: int = 5) -> list[dict]:
    """Search trade post-mortems."""
    col = get_collection("trade_post_mortems")
    if col is None or col.count() == 0:
        return []
    n_results = min(n_results, col.count())
    results = col.query(query_texts=[query], n_results=n_results)
    return _flatten_results(results)


# ── Research Hypotheses Collection ───────────────────────────────────────────

def store_hypothesis(
    hypothesis_id: str, description: str,
    metadata: dict | None = None,
):
    """Store a research hypothesis."""
    _upsert("research_hypotheses", [hypothesis_id], [description], [metadata or {}])


def search_hypotheses(query: str, n_results: int = 5) -> list[dict]:
    """Search research hypotheses."""
    col = get_collection("research_hypotheses")
    if col is None or col.count() == 0:
        return []
    n_results = min(n_results, col.count())
    results = col.query(query_texts=[query], n_results=n_results)
    return _flatten_results(results)


# ── Execution Slippage Collection ────────────────────────────────────────────

def store_slippage_sample(
    trade_id: str,
    strategy: str,
    asset: str,
    direction: str,
    leg: str,
    signal_price: float,
    fill_price: float,
    slippage_bps: float,
    abs_slippage_bps: float,
):
    """Store an execution slippage sample for future cost modeling."""
    doc_id = f"{trade_id}-{leg}"
    now = datetime.now(timezone.utc).isoformat()

    document = (
        f"Execution slippage ({leg}) for {strategy} on {asset} [{direction}]: "
        f"signal={signal_price:.6f}, fill={fill_price:.6f}, "
        f"slippage={slippage_bps:+.2f} bps (abs={abs_slippage_bps:.2f} bps)."
    )

    metadata = {
        "trade_id": trade_id,
        "strategy": strategy,
        "asset": asset,
        "direction": direction,
        "leg": leg,
        "signal_price": float(signal_price),
        "fill_price": float(fill_price),
        "slippage_bps": float(slippage_bps),
        "abs_slippage_bps": float(abs_slippage_bps),
        "adverse_bps": float(max(slippage_bps, 0.0)),
        "recorded_at": now,
    }

    _upsert("execution_slippage", [doc_id], [document], [metadata])


def search_slippage_samples(
    query: str,
    n_results: int = 10,
    where: dict | None = None,
) -> list[dict]:
    """Search execution slippage samples by semantic similarity."""
    col = get_collection("execution_slippage")
    if col is None or col.count() == 0:
        return []
    n_results = min(n_results, col.count())
    kwargs = {"query_texts": [query], "n_results": n_results}
    if where:
        kwargs["where"] = where
    results = col.query(**kwargs)
    return _flatten_results(results)


# ── Quant Skills Collection ───────────────────────────────────────────────────

def upsert_quant_skill(skill) -> None:
    """Index a QuantSkill in ChromaDB for semantic search.

    Accepts a ``forven.quant_skills.QuantSkill`` instance.
    """
    doc = (
        f"{skill.description}. "
        f"Type: {skill.skill_type}. "
        f"Confidence: {skill.confidence:.0%}. "
        f"Samples: {skill.sample_size}."
    )
    metadata = {
        "name": skill.name,
        "skill_type": skill.skill_type,
        "confidence": float(skill.confidence),
        "sample_size": int(skill.sample_size),
        "last_validated": skill.last_validated or "",
    }
    regime = skill.metadata.get("regime", "")
    if regime:
        metadata["regime"] = regime

    _upsert("quant_skills", [skill.name], [doc], [metadata])
    log.debug("Indexed quant skill in ChromaDB: %s", skill.name)


def search_quant_skills(
    query: str,
    n_results: int = 5,
    regime: str | None = None,
    min_confidence: float = 0.0,
) -> list[dict]:
    """Search quant skills by semantic similarity."""
    col = get_collection("quant_skills")
    if col is None or col.count() == 0:
        return []
    n_results = min(n_results, col.count())
    kwargs: dict = {"query_texts": [query], "n_results": n_results}
    where_clauses: list[dict] = []
    if regime:
        where_clauses.append({"regime": {"$eq": regime.upper()}})
    if min_confidence > 0:
        where_clauses.append({"confidence": {"$gte": min_confidence}})
    if len(where_clauses) == 1:
        kwargs["where"] = where_clauses[0]
    elif len(where_clauses) > 1:
        kwargs["where"] = {"$and": where_clauses}
    results = col.query(**kwargs)
    return _flatten_results(results)


def remove_quant_skill(name: str) -> None:
    """Remove a quant skill from the ChromaDB index."""
    col = get_collection("quant_skills")
    if col is None:
        return
    try:
        col.delete(ids=[name])
        log.debug("Removed quant skill from ChromaDB: %s", name)
    except Exception as exc:
        log.warning("Failed to remove quant skill %s from ChromaDB: %s", name, exc)


# ── Agent Narratives Collection ──────────────────────────────────────────────

def store_narrative(content: str, metadata: dict | None = None) -> None:
    """Store an agent narrative/observation in ChromaDB."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    doc_id = f"narrative-{int(now.timestamp() * 1000)}"
    meta = {
        "recorded_at": now.isoformat(),
        **(metadata or {}),
    }
    _upsert("agent_narratives", [doc_id], [content], [meta])


def search_narratives(query: str, n_results: int = 5) -> list[dict]:
    """Search agent narratives by semantic similarity."""
    col = get_collection("agent_narratives")
    if col is None or col.count() == 0:
        return []
    n_results = min(n_results, col.count())
    results = col.query(query_texts=[query], n_results=n_results)
    return _flatten_results(results)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _flatten_results(results: dict) -> list[dict]:
    """Convert ChromaDB query results to a flat list of dicts."""
    flat = []
    if not results or not results.get("ids"):
        return flat
    for i, doc_id in enumerate(results["ids"][0]):
        entry = {"id": doc_id}
        if results.get("documents"):
            entry["document"] = results["documents"][0][i]
        if results.get("metadatas"):
            entry["metadata"] = results["metadatas"][0][i]
        if results.get("distances"):
            entry["distance"] = results["distances"][0][i]
        flat.append(entry)
    return flat


def wipe_collections(collection_names: list[str]):
    """Surgically delete specific collections."""
    client = get_client()
    if client is None:
        log.warning("ChromaDB unavailable; cannot wipe collections.")
        return
    existing = {c.name for c in client.list_collections()}
    for name in collection_names:
        if name in existing:
            log.info("Wiping Chroma collection: %s", name)
            client.delete_collection(name)
