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
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src import ai_discovery, db_settings, secret_box
from src.gui.services.stack_registry import Stack


_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"  # emoji blocks
    "☀-➿"          # symbols
    "　-〿"          # CJK punctuation
    "]"
)
_PUNCT_RE = re.compile(r"[^\w\s؀-ۿ]")  # keep word chars + Arabic
_WHITESPACE_RE = re.compile(r"\s+")

# Aggressive dedup helpers ------------------------------------------------
# Match runs of either ASCII digits or Arabic-Indic digits.
_DIGIT_RUN_RE = re.compile(r"[0-9٠-٩۰-۹]+")
# Tashkeel (Arabic diacritics): fatha, kasra, damma, shadda, sukun, tanween, etc.
_TASHKEEL_RE = re.compile(r"[ً-ْٰـ]")
# Letter folding map: alef variants, ya-with-dots, ta-marbuta, hamza on waw/ya.
_ARABIC_FOLD = str.maketrans({
    "أ": "ا",  # أ -> ا
    "إ": "ا",  # إ -> ا
    "آ": "ا",  # آ -> ا
    "ٱ": "ا",  # ٱ -> ا
    "ى": "ي",  # ى -> ي
    "ئ": "ي",  # ئ -> ي
    "ؤ": "و",  # ؤ -> و
    "ة": "ه",  # ة -> ه
})


def _normalize(text: str) -> str:
    """Build a dedup key that collapses cosmetic + numeric variations.

    Steps: emoji strip, Arabic diacritics strip, letter folding, digit
    runs -> <N>, lowercase, punctuation strip, whitespace collapse.
    """
    s = _EMOJI_RE.sub(" ", text)
    s = _TASHKEEL_RE.sub("", s)
    s = s.translate(_ARABIC_FOLD)
    s = _DIGIT_RUN_RE.sub("<N>", s)
    s = s.casefold()
    s = _PUNCT_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return s


@dataclass
class _Message:
    msg_id: int
    text: str
    date: str  # ISO-8601 UTC
    norm: str  # cache the normalized form


@dataclass
class ClassifiedMessage:
    action_type: str
    phrase: str
    reasoning: str
    confidence: float
    sample_text: str
    msg_count: int  # how many duplicates collapsed into this entry


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

        self.stage_changed.emit("classify")
        self._classify_in_parallel(unique, results)

        self.stage_changed.emit("done")
        return results

    def _classify_in_parallel(
        self,
        unique: list["_Message"],
        results: "WizardResults",
    ) -> None:
        """Chunk into batches, run batches in parallel via thread pool."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        batch_size = max(1, min(50, self._params.batch_size))
        concurrency = max(1, min(16, self._params.concurrency))
        provider = ai_discovery.build_discovery_provider()

        chunks: list[list[_Message]] = [
            unique[i : i + batch_size] for i in range(0, len(unique), batch_size)
        ]
        total = len(unique)
        done_count = 0

        def run_batch(chunk: list[_Message]) -> tuple[list[_Message], list]:
            texts = [m.text for m in chunk]
            try:
                clss = ai_discovery.classify_batch(texts, provider)
                return chunk, clss
            except Exception as e:  # noqa: BLE001
                # Fully failed batch -> mark each as UNKNOWN with the error.
                from src.ai_discovery import Classification
                fallback = [
                    Classification("UNKNOWN", m.text[:60], f"batch error: {e}", 0.0)
                    for m in chunk
                ]
                return chunk, fallback

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(run_batch, c): c for c in chunks}
            for fut in as_completed(futures):
                if self._cancel:
                    for f in futures:
                        f.cancel()
                    break
                chunk, clss = fut.result()
                for m, c in zip(chunk, clss, strict=False):
                    if c.action_type == "UNKNOWN" and c.reasoning.startswith("batch error"):
                        results.failed_count += 1
                    results.classifications.append(ClassifiedMessage(
                        action_type=c.action_type,
                        phrase=c.phrase or m.text[:60],
                        reasoning=c.reasoning,
                        confidence=c.confidence,
                        sample_text=m.text,
                        msg_count=getattr(m, "_dup_count", 1),
                    ))
                done_count += len(chunk)
                self.progress.emit(done_count, total)
