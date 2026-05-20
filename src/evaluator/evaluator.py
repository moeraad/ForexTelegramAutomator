"""Top-level v2 evaluator: glue the deterministic + synthesizer layers.

This is the entry point the orchestrator calls. It:

  1. Resolves the effective trading style (stack default, optionally
     overridden per signal by `style_override.resolve` when the message
     declares one).
  2. Gathers all available inputs from the feature store + EA snapshot.
  3. Runs the deterministic layers: regime tag + per-axis scores +
     composed verdict.
  4. Calls the LLM synthesizer for forward estimates (multi-horizon
     probabilities, MAE/MFE, edge thesis).
  5. Assembles the final evaluation dict, preserving the legacy schema
     for the EA dashboard and stamping forensic fields
     (`context_excerpt`, `effective_style`, `style_source`).

Failure semantics: a missing LLM client or LLM error degrades the
output to the deterministic verdict alone. A crash anywhere in the
deterministic layer raises (these are pure functions — a failure is a
bug, not transient noise). The orchestrator already wraps the whole
call in a try/except and writes an ALERT row on crash.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src import db_settings, feature_store
from src.evaluator.compose import compose, to_dict
from src.evaluator.features import gather_inputs
from src.evaluator.regime import classify
from src.evaluator.scorers import score_all
from src.evaluator.sizing import compute_sizing, to_dict as sizing_to_dict
from src.evaluator.style_override import resolve as resolve_style
from src.evaluator.synthesizer import ChatClient, Synthesis, synthesize
from src.trading_style import DEFAULT_STYLE, TradingStyle, get_spec

log = logging.getLogger(__name__)


# Persist the full input context inside the evaluation dict. Capped to
# keep payload_json from bloating — 4KB is plenty for ~30 lines of
# diagnostics and still well under SQLite's blob ceiling.
_CONTEXT_EXCERPT_MAX_LEN = 4096


def evaluate_signal_v2(
    signal: dict,
    conn: sqlite3.Connection,
    ai_client: ChatClient | None,
    *,
    db_path: Any | None = None,
    symbol: str = "XAUUSD",
) -> dict:
    """Run the full v2 evaluator pipeline and return the evaluation dict.

    `signal` is the OPEN-shape action payload (at minimum `side`; for
    REOPEN_LAST / REINFORCE the EA passes a synthetic payload with the
    filled trade params).

    `ai_client` is the same `chat(system, user) -> str` adapter the v1
    evaluator uses. None disables the synthesizer; deterministic output
    still ships.

    `db_path` is consulted via `db_settings` for the stack's trading-
    style configuration. Falls back to the package default when the
    setting isn't present.
    """
    side = (signal.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        return _stub({"reason": f"unknown side: {side!r}"})

    # --- Resolve effective style.
    stack_default_str = (
        db_settings.get_str(db_path, "trading_style", DEFAULT_STYLE.value)
        if db_path is not None else DEFAULT_STYLE.value
    )
    try:
        stack_default = TradingStyle(stack_default_str)
    except ValueError:
        log.warning(
            "evaluate_signal_v2: invalid trading_style=%r, falling back to %s",
            stack_default_str, DEFAULT_STYLE.value,
        )
        stack_default = DEFAULT_STYLE
    ai_override_enabled = (
        db_settings.get_str(db_path, "trading_style_ai_override", "1") == "1"
        if db_path is not None else True
    )
    message_text = signal.get("source_message_text") or signal.get("comment") or ""
    effective_style, style_source = resolve_style(
        stack_default, message_text, ai_override_enabled,
    )

    # --- Deterministic layers.
    inputs = gather_inputs(conn, symbol=symbol)
    axes = score_all(side, inputs)
    regime = classify(inputs)
    composed = compose(side, effective_style, axes, regime)

    # --- LLM synthesizer (optional).
    syn: Synthesis | None = None
    context_excerpt = ""
    if ai_client is not None:
        syn, context_excerpt = synthesize(composed, effective_style, inputs, ai_client)

    # --- Assemble final dict.
    out = to_dict(composed)
    out["evaluator_version"] = "v2"
    out["effective_style"] = effective_style.value
    out["style_source"] = style_source
    out["horizons_minutes"] = list(get_spec(effective_style).horizons_minutes)
    if context_excerpt:
        out["context_excerpt"] = context_excerpt[:_CONTEXT_EXCERPT_MAX_LEN]
    if syn is not None:
        out["probabilities"] = [
            {
                "minutes": h.minutes,
                "p_profit": round(h.p_profit, 3),
                "p_drawdown_first": round(h.p_drawdown_first, 3),
            }
            for h in syn.horizons
        ]
        if syn.excursion is not None:
            out["expected_excursion"] = {
                "expected_mae_atr": syn.excursion.expected_mae_atr,
                "expected_mfe_atr": syn.excursion.expected_mfe_atr,
                "implied_rr": syn.excursion.implied_rr,
            }
        if syn.edge_thesis:
            out["edge_thesis"] = syn.edge_thesis
        if syn.key_risks:
            out["key_risks"] = list(syn.key_risks)
        if syn.parse_errors:
            out["synthesizer_parse_errors"] = list(syn.parse_errors)
        out["synthesizer_status"] = "ok"
    else:
        out["synthesizer_status"] = "unavailable" if ai_client is None else "failed"

    # Score-tied sizing decision. Always written so the EA can read it
    # uniformly; whether the EA respects it is gated on its own
    # `EnableScoreTiedSizing` input. The threshold can be configured
    # per stack via `score_tied_skip_threshold` (default 0.45).
    skip_threshold = 0.45
    if db_path is not None:
        try:
            raw = db_settings.get_str(db_path, "score_tied_skip_threshold", "0.45")
            skip_threshold = float(raw)
        except (TypeError, ValueError):
            log.warning(
                "evaluate_signal_v2: invalid score_tied_skip_threshold=%r; using 0.45",
                raw,
            )
    out["sizing"] = sizing_to_dict(
        compute_sizing(out, skip_threshold=skip_threshold)
    )

    out["evaluated_at"] = datetime.now(timezone.utc).isoformat()
    return out


def _stub(reason: dict) -> dict:
    """Tiny fallback when the side is unparseable. Mirrors the legacy
    stub shape so the dashboard renders something rather than empty."""
    return {
        "score": 0,
        "verdict": "unavailable",
        "key_factor": f"evaluator_v2 stub: {reason}",
        "summary": "Cannot evaluate without valid side.",
        "factors": {},
        "missing": ["side"],
        "data_quality": "reduced",
        "evaluator_version": "v2",
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
