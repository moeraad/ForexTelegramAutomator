"""Pipeline that backs the Profile Generator wizard.

Steps:
  1. fetch — pull message history from the watched chat via Telethon
  2. dedup — collapse exact + near-duplicate messages
  3. classify — run ai_discovery.classify on each unique message

Runs on its own QThread so the wizard UI stays responsive. Emits Qt
signals at every stage transition + on every per-message classify so
the progress bar can tick.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src import ai_discovery, db_settings, prefilter, secret_box
from src.gui.services.stack_registry import Stack


# Normaliser lives in src/text_normalize so the live trigger matcher uses
# the same transformation. Two consumers, one source of truth, no
# silent drift between offline dedup and online trigger matching.
from src.text_normalize import normalize as _normalize


@dataclass
class _Message:
    msg_id: int
    text: str
    date: str  # ISO-8601 UTC
    norm: str  # cache the normalized form


@dataclass
class ClassifiedMessage:
    """A reviewed/labeled message destined for the profile.

    `action_types` is a tuple to support COMPOUND messages — one channel
    message may map to multiple buckets ("half close use BE" is both
    CLOSE_PARTIAL and MOVE_SL_BE). Most messages have a single entry.

    `pending` is meaningful only when "OPEN" is in `action_types`:
      True  → OPEN with broker-side pending order
      False → OPEN with market entry
      None  → not an OPEN or pending intent not derivable
    """
    action_types: tuple[str, ...]
    phrase: str
    reasoning: str
    confidence: float
    sample_text: str
    msg_count: int  # how many duplicates collapsed into this entry
    pending: bool | None = None

    @property
    def action_type(self) -> str:
        """Backwards-compat shim — first bucket, or UNKNOWN if empty."""
        return self.action_types[0] if self.action_types else "UNKNOWN"


@dataclass
class WizardParameters:
    max_messages: int = 500
    lookback_days: int = 30
    concurrency: int = 4
    batch_size: int = 10


@dataclass
class WizardResults:
    classifications: list[ClassifiedMessage] = field(default_factory=list)
    raw_fetched: int = 0
    unique_after_dedup: int = 0
    prefiltered_symbol: int = 0
    prefiltered_ad: int = 0
    failed_count: int = 0


# --- Telethon fetch -------------------------------------------------------


async def _fetch_history(
    db_path: Path,
    chat_id: int,
    max_messages: int,
    lookback_days: int,
    on_progress,
) -> list[_Message]:
    from datetime import datetime, timedelta, timezone

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = db_settings.get_int(db_path, "tg_api_id", 0)
    api_hash = db_settings.get_str(db_path, "tg_api_hash", "")
    session_blob = db_settings.get_str(db_path, "tg_session_blob", "")
    if not api_id or not api_hash or not session_blob:
        raise RuntimeError(
            "Telegram credentials missing in DB — run the Setup wizard first."
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    client = TelegramClient(StringSession(session_blob), api_id, api_hash)
    out: list[_Message] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telegram session is not authorized — re-run the Setup wizard."
            )
        async for m in client.iter_messages(chat_id, limit=max_messages):
            if m.message is None or not m.message.strip():
                continue
            if m.date and m.date < cutoff:
                break
            out.append(_Message(
                msg_id=m.id,
                text=m.message,
                date=m.date.isoformat() if m.date else "",
                norm=_normalize(m.message),
            ))
            if len(out) % 25 == 0:
                on_progress(len(out), max_messages)
        on_progress(len(out), max_messages)
    finally:
        await client.disconnect()
    return out


# --- Dedup ----------------------------------------------------------------


def _dedup(messages: list[_Message]) -> list[_Message]:
    seen: dict[str, _Message] = {}
    counts: dict[str, int] = {}
    for m in messages:
        key = m.norm
        if not key:
            continue
        if key not in seen:
            seen[key] = m
            counts[key] = 1
        else:
            counts[key] += 1
    # Attach count to each kept message via a sentinel attribute; simpler
    # than threading a second dict through callers.
    for k, m in seen.items():
        setattr(m, "_dup_count", counts[k])
    return list(seen.values())


# --- Worker thread --------------------------------------------------------


class ProfileWizardWorker(QThread):
    stage_changed = Signal(str)                 # "fetch" | "dedup" | "classify" | "done"
    progress = Signal(int, int)                 # current, total
    completed = Signal(object)                  # WizardResults
    failed = Signal(str)

    def __init__(
        self,
        stack: Stack,
        params: WizardParameters,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._stack = stack
        self._params = params
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        try:
            results = self._do_pipeline()
            self.completed.emit(results)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")

    def _do_pipeline(self) -> WizardResults:
        chat_id = db_settings.get_int(self._stack.db_path, "tg_watched_chat_id", 0)
        if not chat_id:
            raise RuntimeError(
                "No watched channel configured (tg_watched_chat_id is empty). "
                "Open the Setup wizard, pick a channel, then retry."
            )
        self.stage_changed.emit("fetch")
        msgs = asyncio.run(
            _fetch_history(
                self._stack.db_path,
                chat_id,
                self._params.max_messages,
                self._params.lookback_days,
                lambda c, t: self.progress.emit(c, t),
            )
        )
        results = WizardResults(raw_fetched=len(msgs))
        if self._cancel:
            return results

        self.stage_changed.emit("dedup")
        unique = _dedup(msgs)
        results.unique_after_dedup = len(unique)
        self.progress.emit(len(unique), len(unique))
        if self._cancel:
            return results

        # Stage 0: universal, deterministic, no-LLM pre-filter. Drops
        # messages mentioning ONLY non-target instruments (per
        # profile.other_instruments) AND messages with ad-shaped layouts
        # (URL density + length + currency patterns). On a fresh-install
        # profile both lists are empty → no-op. The same prefilter runs
        # on the live path so the wizard's IGNORE decisions match what
        # the listener will filter at trade time.
        self.stage_changed.emit("prefilter")
        survivors = self._prefilter_messages(unique, results)
        if self._cancel:
            return results

        # NOTE: A bootstrap-triage LLM gate used to sit here, but in
        # practice it kept everything (its job overlapped 1:1 with the
        # classifier's IGNORE bucket, which has explicit rules for the
        # same noise categories — greetings, ads, TP-hit announcements,
        # channel brags, banter). Keeping it was paying triage cost to
        # produce the same outcome the classifier already produces a
        # call later, so it's gone. Prefilter survivors flow straight
        # into the classifier; messages the classifier returns as
        # IGNORE still feed `commentary_filter` at render time.
        self.stage_changed.emit("classify")
        self._classify_in_parallel(survivors, results)

        self.stage_changed.emit("done")
        return results

    def _load_profile_for_prefilter(self) -> dict:
        """Load the bootstrap profile for prefilter config (symbol_aliases /
        other_instruments). Empty dict on first-run channels with no profile
        — both lists default to empty, prefilter becomes a no-op."""
        from pathlib import Path
        path = Path(self._stack.db_path).parent / "profile.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _prefilter_messages(
        self,
        unique: list["_Message"],
        results: "WizardResults",
    ) -> list["_Message"]:
        """Apply the universal Stage 0 pre-filter. Dropped messages are
        recorded as IGNORE Classifications so they're still visible in the
        review tree (operator can review what got auto-dropped)."""
        profile = self._load_profile_for_prefilter()
        survivors: list[_Message] = []
        total = len(unique)
        for i, m in enumerate(unique):
            drop_sym, _ = prefilter.should_drop_by_symbol(m.text, profile)
            if drop_sym:
                results.prefiltered_symbol += 1
                results.classifications.append(ClassifiedMessage(
                    action_types=("IGNORE",),
                    phrase=m.text.strip().splitlines()[0][:80] if m.text.strip() else "",
                    reasoning="prefilter:symbol-mismatch",
                    confidence=1.0,
                    sample_text=m.text,
                    msg_count=getattr(m, "_dup_count", 1),
                ))
                continue
            if prefilter.looks_like_ad(m.text):
                results.prefiltered_ad += 1
                results.classifications.append(ClassifiedMessage(
                    action_types=("IGNORE",),
                    phrase=m.text.strip().splitlines()[0][:80] if m.text.strip() else "",
                    reasoning="prefilter:ad-shape",
                    confidence=1.0,
                    sample_text=m.text,
                    msg_count=getattr(m, "_dup_count", 1),
                ))
                continue
            survivors.append(m)
            if (i + 1) % 25 == 0 or i + 1 == total:
                self.progress.emit(i + 1, total)
        self.progress.emit(total, total)
        return survivors

    def _discovery_context(self) -> dict:
        """Pull the discovery-prompt slots from the existing profile
        (symbol, language_note). Returns an empty-ish dict on first-run
        channels — defaults inside `classify` keep the prompt valid.
        Price magnitude is no longer carried here; the discovery prompt
        derives it from message content alone.
        """
        profile = self._load_profile_for_prefilter()
        return {
            "symbol": profile.get("symbol") or db_settings.get_str(
                self._stack.db_path, "target_symbol", ""
            ) or "XAUUSD",
            "language_note": profile.get("language_note", ""),
        }

    def _classify_in_parallel(
        self,
        unique: list["_Message"],
        results: "WizardResults",
    ) -> None:
        """Classify one message at a time in parallel via thread pool.

        With the new single-message discovery prompt (much richer per-call
        context, multilingual rules, 26 worked examples baked in), batching
        many messages into one prompt would either lose accuracy or
        balloon per-call tokens. Per-message via threads gives the same
        wall-clock with predictable per-call cost.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        concurrency = max(1, min(16, self._params.concurrency))
        provider = ai_discovery.build_discovery_provider()
        context = self._discovery_context()
        total = len(unique)
        done_count = 0

        def run_one(m: "_Message") -> tuple["_Message", "Classification"]:
            try:
                c = ai_discovery.classify(m.text, provider, context)
                return m, c
            except Exception as e:  # noqa: BLE001
                from src.ai_discovery import Classification
                return m, Classification(
                    action_types=("UNKNOWN",),
                    phrase=m.text[:60],
                    reasoning=f"classify error: {e}",
                    confidence=0.0,
                )

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(run_one, m): m for m in unique}
            for fut in as_completed(futures):
                if self._cancel:
                    for f in futures:
                        f.cancel()
                    break
                m, c = fut.result()
                if c.action_type == "UNKNOWN" and c.reasoning.startswith("classify error"):
                    results.failed_count += 1
                results.classifications.append(ClassifiedMessage(
                    action_types=c.action_types,
                    phrase=c.phrase or m.text[:60],
                    reasoning=c.reasoning,
                    confidence=c.confidence,
                    sample_text=m.text,
                    msg_count=getattr(m, "_dup_count", 1),
                    pending=c.pending,
                ))
                done_count += 1
                self.progress.emit(done_count, total)
