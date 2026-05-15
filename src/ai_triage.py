"""Cheap first-stage classifier: decide whether a message is worth the full
AI interpreter.

Returns either "ignore" (promotional / greeting / emoji-only / off-topic noise)
or "keep" (anything that might carry trade info, management intent, or market
commentary). On any ambiguity the model is instructed to return "keep" so the
full interpreter pass never misses a real signal.

Token footprint is tiny: short system prompt + one-line input + ~32-token
output, no extended thinking. Roughly 3x cheaper per call than the
interpreter, and we only pay for the full call on KEEP messages.

The CHANNEL-SPECIFIC high-signal phrase list comes from the active
channel profile (channels/<CHANNEL_PROFILE>.json -> triage_keep_triggers).
The universal IGNORE/KEEP definitions stay hard-coded.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Literal

from src import config
from src.llm_provider import (
    AnthropicProvider,
    LLMProvider,
    build_triage_provider,
)


TriageDecision = Literal["ignore", "keep"]


_LEGACY_PROFILE_DIR = Path(__file__).resolve().parent.parent / "channels"


def _load_profile(name: str | None = None) -> dict:
    target = name or config.CHANNEL_PROFILE
    if not target:
        raise RuntimeError(
            "No channel profile configured. Set channel_profile in the stack's "
            "DB settings (the setup wizard does this automatically)."
        )
    appdata_path = Path(config.DB_PATH).parent / "profile.json"
    legacy_path = _LEGACY_PROFILE_DIR / f"{target}.json"
    p = appdata_path if appdata_path.exists() else legacy_path
    if not p.exists():
        raise FileNotFoundError(
            f"Channel profile not found: {appdata_path}. Run the setup "
            f"wizard or create the file manually."
        )
    return json.loads(p.read_text(encoding="utf-8"))


_TEMPLATE = Template("""You are a fast pre-filter for a gold (XAUUSD) Telegram signals channel.

Classify each incoming message as either "ignore" or "keep".

IGNORE: promotional posts, ads, greetings, thank-yous, emoji-only reactions,
motivational quotes, channel announcements unrelated to gold trading, pure
chit-chat with no price/direction/position reference.

KEEP: anything that COULD be a trade signal, a management instruction (move
SL, close, partial, reopen, reinforce, tighten, cancel a pending), a
market-commentary / bias / zone note, a TP-hit confirmation, or anything
referencing prices, numbers that look like gold levels, buy/sell/long/short
words in any language, or an existing position. Also KEEP short ambiguous
messages when there are open positions (e.g. "close", "exit", "out", "delete
limit") because they may be management commands.

${triage_keep_triggers}

WHEN IN DOUBT, RETURN "keep". False negatives (losing a real signal) are
much worse than false positives (letting noise through to the next stage,
which filters it anyway).

OUTPUT FORMAT: a single JSON object, nothing else:
{"decision": "ignore" | "keep"}
""")


def _render_triage_prompt(profile_name: str | None = None) -> str:
    p = _load_profile(profile_name)
    return _TEMPLATE.substitute(
        triage_keep_triggers=p.get("triage_keep_triggers", ""),
    )


try:
    TRIAGE_SYSTEM_PROMPT: str | None = _render_triage_prompt()
except (RuntimeError, FileNotFoundError):
    TRIAGE_SYSTEM_PROMPT = None


@dataclass
class TriageResult:
    decision: TriageDecision
    raw_text: str
    usage: dict[str, int]
    latency_ms: int


_DECISION_RE = re.compile(r'"decision"\s*:\s*"(ignore|keep)"', re.IGNORECASE)


def _parse_decision(raw: str) -> TriageDecision:
    """Tolerant parse: prefer JSON, fall back to regex, default to 'keep'."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        obj = json.loads(raw)
        d = str(obj.get("decision", "")).lower()
        if d in ("ignore", "keep"):
            return d  # type: ignore[return-value]
    except Exception:
        pass
    m = _DECISION_RE.search(raw)
    if m:
        return m.group(1).lower()  # type: ignore[return-value]
    # Conservative fallback: when we can't parse, don't drop the message.
    return "keep"


class TriageClient:
    def __init__(
        self,
        client: Any = None,
        model: str = "claude-haiku-4-5-20251001",
        max_retries: int = 2,
        retry_sleep: float = 1.0,
        provider: LLMProvider | None = None,
    ):
        if provider is not None:
            self._provider: LLMProvider = provider
        elif client is not None:
            self._provider = AnthropicProvider(client=client, model=model)
        else:
            self._provider = build_triage_provider(model=model)
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep

    def classify(self, text: str, open_count: int) -> TriageResult:
        user_content = (
            f"OPEN_POSITIONS: {open_count}\n"
            f"MESSAGE:\n{text}"
        )
        last_err: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                result = self._provider.triage(
                    system_prompt=TRIAGE_SYSTEM_PROMPT,
                    user_content=user_content,
                    max_output_tokens=32,
                )
                decision = _parse_decision(result.raw_text)
                return TriageResult(
                    decision=decision,
                    raw_text=result.raw_text,
                    usage=result.usage,
                    latency_ms=result.latency_ms,
                )
            except Exception as e:
                last_err = e
                if attempt < self._max_retries - 1:
                    time.sleep(self._retry_sleep * (2 ** attempt))
        raise RuntimeError(f"triage call failed after {self._max_retries} attempts: {last_err}")
