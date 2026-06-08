"""Deterministic + embedding trigger matcher.

Runs in the live listener between triage and Sonnet. Operator-curated
triggers stored in the channel profile become enforced rules rather
than prompt suggestions the LLM is free to ignore. The structure is
two cooperating layers; the third "layer" is the listener falling
through to Sonnet when neither hits.

Layer 1 — DETERMINISTIC
  Normalised substring match (same `_normalize` the wizard uses for
  dedup). Optional `context_tokens` AND-requirement to disambiguate
  short phrases. State preconditions enforced per action_type so a
  CLOSE_FULL trigger doesn't fire without an open position.

Layer 2 — EMBEDDING
  When Layer 1 misses, embed the incoming message with OpenAI
  `text-embedding-3-small` and take cosine similarity vs the cached
  trigger embeddings. Best match above `EMBEDDING_THRESHOLD` (0.78
  default) wins, subject to the same state preconditions.

Cache lifecycle
  Trigger embeddings are computed lazily on first use and held in
  process memory. Profile-file mtime is checked on every match attempt;
  any change invalidates the cache so editing the Triggers tab while
  the listener is running takes effect within one message.

What gets emitted
  Layer 1/2 returns a list of validators.Action subclasses that the
  orchestrator's existing persistence loop consumes. Compound triggers
  (multi-bucket) emit one Action per bucket.

What's deliberately NOT matched here
  OPEN, OPEN_INSTANT, ATTACH_SIGNAL, MODIFY_TPS, MOVE_SL, ALERT —
  these need price parsing, side inference, or operator-text fields
  that a deterministic matcher can't produce. They stay on Sonnet.
  The matcher covers the management vocabulary the operator can
  curate by phrase alone.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from src.text_normalize import normalize
from src.validators import (
    Action,
    CancelPendingAction,
    ClosePartialAction,
    CloseFullAction,
    MoveSlBeAction,
    ReinforceAction,
    ReopenLastAction,
    TightenSlAction,
)

log = logging.getLogger(__name__)


# Historical: a minimum-length safety used to gate matchable triggers
# (to keep "ok" / "thanks" from substring-matching everything). The
# matcher now matches against `samples[0]` only, so this constant is
# no longer enforced — operators decide what counts as a sufficient
# sample for their channel. Kept defined (= 0) for any out-of-tree
# code that may still import it; safe to remove later.
MIN_NORMALISED_PHRASE_LEN = 0

# Cosine threshold for Layer 2 embedding match. Tuned empirically:
# 0.78 catches paraphrases without firing on superficially-similar but
# semantically different messages. Tunable per deployment via
# COPYTRADES_EMBED_THRESHOLD env var if a channel needs looser or
# stricter behaviour.
EMBEDDING_THRESHOLD = float(
    os.environ.get("COPYTRADES_EMBED_THRESHOLD", "0.78")
)

# OpenAI's small embedding model — 1536-dim, $0.02/1M tokens, multilingual.
# Hardcoded rather than configurable because the cached vectors are
# model-specific; switching models silently would invalidate every cache
# entry without warning.
EMBEDDING_MODEL = "text-embedding-3-small"

# After an embedding-build failure (no key, API error, rate limit), Layer 2
# is suppressed for this long before the next message retries it. Long enough
# not to hammer a broken endpoint per-message; short enough that a transient
# blip self-heals without a listener restart.
EMBED_RETRY_COOLDOWN_SEC = float(
    os.environ.get("COPYTRADES_EMBED_RETRY_COOLDOWN_SEC", "300")
)


# Action types the matcher is allowed to emit. Anything else stays with
# Sonnet because deterministic-emit needs more than a phrase (price,
# side inference, structured payload).
_ALLOWED_ACTION_TYPES: frozenset[str] = frozenset({
    "CLOSE_FULL",
    "MOVE_SL_BE",
    "CLOSE_PARTIAL",
    "REOPEN_LAST",
    "REINFORCE",
    "TIGHTEN_SL",
    "CANCEL_PENDING",
    # Reply-intent meta-types. Not real action types — the matcher
    # uses them as dispatch signals when the current message is a
    # Telegram reply. TRIGGER_PARENT fires → matcher re-runs against
    # the parent's text. IGNORE_PARENT fires → matcher returns [] so
    # the orchestrator skips the AI fallback too. Both are stripped
    # from `_build_actions`'s output; they never become DB rows.
    "TRIGGER_PARENT",
    "IGNORE_PARENT",
})

# Reply-intent meta-types: dispatch signals, never persisted as actions.
_REPLY_INTENT_TYPES: frozenset[str] = frozenset({
    "TRIGGER_PARENT",
    "IGNORE_PARENT",
})


@dataclass(frozen=True)
class TriggerRule:
    """One operator-confirmed trigger entry loaded from the profile.

    `phrase` is the operator-curated distinctive substring (post-edit
    in the Triggers tab). `context_tokens` is the optional AND-list
    that all must additionally appear in the message for the rule to
    fire — used to disambiguate short phrases that would otherwise
    over-trigger (a common greeting paired with a TP-hit marker, for
    instance — the greeting alone is too generic, the combination is
    specific to a close-intent message).

    `match_text` / `norm_match_text` is the actual string the matcher
    compares against the incoming message. Defaults to `phrase` but
    falls back to `samples[0]` (the full original message) when the
    phrase is too short to be reliable on its own — that's what
    happens for discovery-wizard output where the classifier
    sometimes returns a short stub like "TP1 ✅" but the operator's
    intent is "messages SHAPED like this full sample." Using the
    full sample for matching makes the trigger as specific as the
    operator's reference example.

    `source` records which field won the resolution so the Test pane
    can show "matched on phrase 'X'" vs "matched on sample (phrase
    'X' was too short)" without re-deriving the choice.
    """
    action_type: str
    phrase: str
    norm_phrase: str
    norm_context_tokens: tuple[str, ...]
    match_text: str = ""
    norm_match_text: str = ""
    source: str = "phrase"  # "phrase" or "sample"
    pending: bool | None = None  # carried for OPEN compatibility; unused here

    @property
    def is_emittable(self) -> bool:
        if self.action_type not in _ALLOWED_ACTION_TYPES:
            return False
        # Emittability gates on `norm_match_text` (the normalised
        # samples[0]). A trigger with no sample has no match text and
        # is silently skipped. Length is intentionally NOT bounded —
        # operators get to decide what counts as a sufficient sample.
        return bool(self.norm_match_text)


@dataclass(frozen=True)
class MatchContext:
    """State the matcher needs to enforce action preconditions.

    Built once per incoming message via `build_context(conn)`. Cheap
    SQL — no LLM call. Mirrors the checks the live interpreter prompt
    encodes as natural-language rules, but promoted to deterministic
    code so the matcher can refuse a CLOSE_FULL when there's nothing
    to close (instead of emitting an action that the EA would then
    reject).
    """
    has_open_position: bool
    open_position_side: str | None  # "BUY" / "SELL" / None
    has_closed_within_24h: bool
    has_pending_open: bool


@dataclass
class _ProfileCache:
    """Per-profile in-memory cache: parsed rules + their embeddings.

    Invalidated when the underlying profile.json's mtime changes — so
    edits in the Triggers tab take effect on the next incoming
    message without a listener restart.

    `symbol` is the channel's trading symbol pulled from profile.json
    (`{"symbol": "..."}`) — used when building CANCEL_PENDING actions
    so the matcher stays channel-agnostic instead of assuming
    project-wide constants.

    Reply-intent meta-rules (action_type=TRIGGER_PARENT / IGNORE_PARENT)
    live INSIDE ``rules`` like any other trigger; the matcher just
    treats hits on those action_types as dispatch signals instead of
    emit candidates.
    """
    path: Path
    mtime: float
    rules: list[TriggerRule]
    symbol: str
    # Parallel list of 1536-dim vectors aligned with `rules`. Populated
    # lazily on the first Layer 2 attempt to avoid embedding cost when
    # Layer 1 covers everything. `None` means "not built yet, or a prior
    # build failed" — Layer 2 retries (subject to the cooldown below) rather
    # than treating a one-off failure as a permanent disable.
    embeddings: list[list[float]] | None = None
    # Monotonic-clock deadline; after a populate failure Layer 2 stays off
    # until now() passes this, so a broken/rate-limited embedding endpoint
    # isn't hit once per message but still recovers on its own.
    embeddings_retry_after: float = 0.0


_cache: _ProfileCache | None = None
_cache_lock = threading.Lock()


# ---- Public entry point --------------------------------------------------


def match(
    text: str,
    conn: sqlite3.Connection,
    *,
    parent_text: str | None = None,
    meta_out: dict | None = None,
) -> list[Action] | None:
    """Try Layer 1 (deterministic) then Layer 2 (embedding) against the
    current profile's triggers. Return matched Actions or None to fall
    through to Sonnet.

    A None return means "no match" — the caller (orchestrator) should
    proceed with the LLM call. An empty list is intentionally NOT used
    because it would conflate "matched but produced no actions" with
    "no match", and the persistence loop would silently drop the
    message.

    Reply handling (added 2026-05-26): when ``parent_text`` is supplied
    (the current message is a Telegram reply), the matcher first
    checks the operator-curated reply-intent phrase lists:
      - Activate phrase matched → re-run the matcher against the
        PARENT's text and return that result. The parent's trigger is
        what gets fired.
      - Ignore phrase matched → return [] (matched, no action).
        Suppresses both the parent's would-be-trigger AND the AI
        fallback. Use for "skip this", "cancel that order".
      - Neither matched → fall through to the existing current-text
        matching, then to AI if still no hit.

    Deterministic IGNORE (added 2026-05-29): if the whole normalised
    message EQUALS an operator-curated IGNORE sample, return ``[]`` with
    ``meta_out["layer"]="deterministic_ignore"``. The orchestrator buckets
    it as ``trigger_ignore`` and skips triage + the interpreter. Equality
    (not substring) guarantees a real signal that merely quotes a noise
    phrase is never suppressed.
    """
    if not text or not text.strip():
        return None
    cache = _load_cache()
    if cache is None or not cache.rules:
        return None
    ctx = build_context(conn)
    norm_msg = normalize(text)
    if not norm_msg:
        return None

    # Reply-context dispatch: if the current message is a reply AND
    # any rule with action_type TRIGGER_PARENT / IGNORE_PARENT matches
    # the current text, treat the hit as a dispatch signal instead of
    # an emit candidate. TRIGGER_PARENT wins ties so operator typos
    # never silently drop a real signal.
    if parent_text and parent_text.strip():
        intent = _classify_reply_intent(norm_msg, cache.rules)
        if intent == "TRIGGER_PARENT":
            log.info(
                "trigger_match reply_intent=TRIGGER_PARENT — "
                "recursing on parent"
            )
            return match(parent_text, conn, parent_text=None, meta_out=meta_out)
        if intent == "IGNORE_PARENT":
            log.info(
                "trigger_match reply_intent=IGNORE_PARENT — "
                "suppressing parent + AI"
            )
            if meta_out is not None:
                meta_out["layer"] = "reply_intent_ignore"
            return []

    # Layer 1 — deterministic.
    hits = _layer1_deterministic(norm_msg, cache.rules, ctx)
    if hits:
        actions = _build_actions(hits, ctx, cache.symbol)
        if actions:
            log.info(
                "trigger_match layer=det count=%d types=%s",
                len(hits),
                ",".join(h.action_type for h in hits),
            )
            if meta_out is not None:
                meta_out["layer"] = "deterministic"
            return actions

    # Deterministic IGNORE — the operator curated this exact message as
    # noise. Runs AFTER the emittable Layer 1 (an actionable trigger always
    # wins over a noise tag) and BEFORE the fuzzy embedding layer (explicit
    # operator curation beats a similarity guess, and we skip the embedding
    # API call on known noise). Whole-message EQUALITY, never substring:
    # a genuine signal can quote a noise phrase as a fragment, so substring
    # matching here would silently suppress trades.
    ignore_hit = _layer1_ignore_match(norm_msg, cache.rules)
    if ignore_hit is not None:
        log.info(
            "trigger_match layer=det_ignore phrase=%r — suppressed as "
            "curated noise", ignore_hit.phrase,
        )
        if meta_out is not None:
            meta_out["layer"] = "deterministic_ignore"
            meta_out["rule_phrase"] = ignore_hit.phrase
        return []

    # Layer 2 — embedding similarity.
    hit = _layer2_embedding(text, cache, ctx)
    if hit is not None:
        rule, score = hit
        actions = _build_actions([rule], ctx, cache.symbol)
        if actions:
            log.info(
                "trigger_match layer=embed score=%.3f type=%s",
                score,
                rule.action_type,
            )
            if meta_out is not None:
                meta_out["layer"] = "embedding"
                meta_out["embedding_score"] = score
                meta_out["rule_action_type"] = rule.action_type
            return actions

    return None


def _classify_reply_intent(
    norm_msg: str, rules: list[TriggerRule],
) -> str | None:
    """Return 'TRIGGER_PARENT' / 'IGNORE_PARENT' / None for a normalised
    current message by substring-matching against meta-rule phrases.

    A reply-intent rule fires when its normalised match_text (or
    phrase) is a substring of ``norm_msg`` AND all its context tokens
    are present. State preconditions are NOT consulted — these meta
    types don't gate on open/closed state; they delegate to the
    parent which has its own.

    Both meta-types may have multiple rules (operator can curate
    distinct phrases per intent); first hit wins per type. When both
    types have hits on the same message, TRIGGER_PARENT wins so a
    real signal isn't silently dropped.
    """
    found: dict[str, bool] = {}
    for rule in rules:
        if rule.action_type not in _REPLY_INTENT_TYPES:
            continue
        active = rule.norm_match_text or rule.norm_phrase
        if not active or active not in norm_msg:
            continue
        if rule.norm_context_tokens and not all(
            tok in norm_msg for tok in rule.norm_context_tokens
        ):
            continue
        found[rule.action_type] = True
    if found.get("TRIGGER_PARENT"):
        return "TRIGGER_PARENT"
    if found.get("IGNORE_PARENT"):
        return "IGNORE_PARENT"
    return None


def build_context(conn: sqlite3.Connection) -> MatchContext:
    """Cheap structured snapshot of the bits of state the matcher needs.

    Reads only what each action precondition consults — open count,
    open side, recent-close existence, pending-open existence. Three
    indexed queries; no rendered text.
    """
    open_row = conn.execute(
        "SELECT json_extract(payload_json,'$.side') AS side "
        "FROM positions p "
        "JOIN actions a ON a.id = p.action_id "
        "WHERE p.status='open' LIMIT 1"
    ).fetchone()
    has_open = open_row is not None
    open_side = open_row["side"] if has_open and "side" in open_row.keys() else None

    closed_row = conn.execute(
        "SELECT 1 FROM positions "
        "WHERE status='closed' AND closed_at > datetime('now', '-24 hours') "
        "LIMIT 1"
    ).fetchone()
    has_closed = closed_row is not None

    pending_row = conn.execute(
        "SELECT 1 FROM actions "
        "WHERE action_type='OPEN' AND status='watching' LIMIT 1"
    ).fetchone()
    has_pending = pending_row is not None

    return MatchContext(
        has_open_position=has_open,
        open_position_side=open_side,
        has_closed_within_24h=has_closed,
        has_pending_open=has_pending,
    )


# ---- Layer 1: deterministic ---------------------------------------------


def _layer1_deterministic(
    norm_msg: str,
    rules: list[TriggerRule],
    ctx: MatchContext,
) -> list[TriggerRule]:
    """Return every rule whose normalised phrase + context tokens are
    substrings of the normalised message AND whose state preconditions
    are satisfied.

    Conflict policy: when multiple rules match, longest normalised
    phrase wins per action_type (rough proxy for "most specific"). If
    two rules with different action_types match, both fire — this is
    intentional for compound channel messages ("secure entry and book
    half" hitting both MOVE_SL_BE and CLOSE_PARTIAL triggers).
    """
    candidates: list[TriggerRule] = []
    for rule in rules:
        if not rule.is_emittable:
            continue
        # Match against the resolved `match_text` (phrase or sample-
        # fallback), not the raw phrase. For discovery-wizard output
        # this is the full original message instead of the classifier's
        # short stub.
        active = rule.norm_match_text or rule.norm_phrase
        if active not in norm_msg:
            continue
        if rule.norm_context_tokens and not all(
            tok in norm_msg for tok in rule.norm_context_tokens
        ):
            continue
        if not _precondition_ok(rule.action_type, ctx):
            # Bumped from DEBUG to INFO: when an operator's CLOSE_FULL
            # trigger phrase matches a TP1 message but the position is
            # already closed (broker-side SL/TP hit before the channel
            # confirmed), the operator needs to SEE the skip in the
            # default INFO-level log to understand why no action fired.
            # The corresponding `messages.received` line on its own
            # left the trail looking like a system failure.
            log.info(
                "trigger_match skip (precondition) type=%s phrase=%r reason=%s",
                rule.action_type, rule.phrase,
                _precondition_label(rule.action_type),
            )
            continue
        candidates.append(rule)
    if not candidates:
        return []
    # Per-action_type: longest active-match-text wins (rough proxy for
    # "most specific" — a fuller sample beats a shorter substring).
    best: dict[str, TriggerRule] = {}
    for r in candidates:
        cur = best.get(r.action_type)
        r_len = len(r.norm_match_text or r.norm_phrase)
        cur_len = len(cur.norm_match_text or cur.norm_phrase) if cur else -1
        if cur is None or r_len > cur_len:
            best[r.action_type] = r
    return list(best.values())


# ---- Deterministic IGNORE -----------------------------------------------


def _ignore_sample_eligible(norm_sample: str) -> bool:
    """True when an IGNORE sample carries real content to suppress on.

    Language-agnostic — no character-count threshold and no script
    assumptions (a 4-char floor would wave through one-word English noise
    yet reject a full CJK sentence). The ONLY hard rejection is a sample
    that is nothing but digit placeholders: `normalize` collapses every
    digit run to a standalone "n" token, so a bare-number message ("4696")
    normalises to "n". Equality on that would suppress every other
    bare-number message — and a lone number can be a price / SL shorthand,
    not noise. Any sample carrying at least one non-number token is
    eligible; whole-message equality keeps even short, single-word samples
    safe in every language.
    """
    return any(tok != "n" for tok in norm_sample.split())


def _layer1_ignore_match(
    norm_msg: str, rules: list[TriggerRule],
) -> TriggerRule | None:
    """Return an IGNORE-typed rule whose normalised sample EQUALS the whole
    normalised message, else None.

    Equality (not substring) is the safety contract: an operator-curated
    noise sample appearing as a *fragment* of a longer message must never
    suppress it, because a genuine signal can quote a noise phrase. Only a
    verbatim repeat (modulo emoji / digit / diacritic normalisation) of a
    sample the operator explicitly tagged IGNORE is dropped here. Degenerate
    ultra-short samples are screened by `_ignore_sample_eligible`.
    """
    if not norm_msg:
        return None
    for rule in rules:
        if rule.action_type != "IGNORE":
            continue
        nmt = rule.norm_match_text
        if not nmt or not _ignore_sample_eligible(nmt):
            continue
        if nmt != norm_msg:
            continue
        # Honour optional context tokens with the same AND-check as the
        # emittable path, so an operator who narrows an IGNORE rule isn't
        # silently overridden. (With whole-message equality these are
        # usually redundant, but parity beats a surprising divergence.)
        if rule.norm_context_tokens and not all(
            tok in norm_msg for tok in rule.norm_context_tokens
        ):
            continue
        return rule
    return None


# ---- Layer 2: embedding similarity --------------------------------------


def _layer2_embedding(
    text: str,
    cache: _ProfileCache,
    ctx: MatchContext,
) -> tuple[TriggerRule, float] | None:
    """Embed the message, score against every cached trigger embedding,
    return the highest-scoring rule above EMBEDDING_THRESHOLD whose
    state precondition is satisfied. None if the embedding API is
    unavailable or no rule clears the threshold.
    """
    # (Re)build the per-rule embedding cache. `None` = not built yet OR a
    # prior build failed; retry, but no sooner than the cooldown so a broken
    # endpoint isn't hit once per message. Fixes the old behaviour where a
    # single populate failure disabled Layer 2 for the whole process life.
    # Cache fields below are mutated lock-free. Safe because match() is only
    # called from the single-threaded listener asyncio loop; the GUI Test
    # pane uses a separate inline cache (_parse_raw_triggers), never _cache.
    if cache.embeddings is None:
        if time.monotonic() < cache.embeddings_retry_after:
            return None
        if not _populate_embeddings(cache):
            cache.embeddings_retry_after = (
                time.monotonic() + EMBED_RETRY_COOLDOWN_SEC
            )
            log.warning(
                "trigger_match Layer 2 suppressed for %.0fs after embedding "
                "build failure (auto-retries after)", EMBED_RETRY_COOLDOWN_SEC,
            )
            return None
    # Defensive: rules and their embeddings must stay 1:1. A mismatch means a
    # partial build slipped through; skip + force a rebuild rather than raise
    # a ValueError on every message (which the orchestrator would swallow,
    # silently killing Layer 2).
    if len(cache.embeddings) != len(cache.rules):
        log.warning(
            "trigger_match Layer 2 skipped: embeddings/rules length mismatch "
            "(%d vs %d) — forcing rebuild",
            len(cache.embeddings), len(cache.rules),
        )
        cache.embeddings = None
        return None
    msg_vec = _embed_one(text)
    if msg_vec is None:
        return None
    best_score = 0.0
    best_rule: TriggerRule | None = None
    scored = 0
    for rule, vec in zip(cache.rules, cache.embeddings):
        if not rule.is_emittable:
            continue
        if not _precondition_ok(rule.action_type, ctx):
            continue
        scored += 1
        score = _cosine(msg_vec, vec)
        if score > best_score:
            best_score = score
            best_rule = rule
    if best_rule is None or best_score < EMBEDDING_THRESHOLD:
        # Per-message tuning detail at DEBUG (this fires on most messages that
        # reach Layer 2 — INFO would swamp the log). "Did Layer 2 run at all?"
        # is answered by the rare INFO "embeddings built" line and the failure
        # WARNING; this line is for threshold calibration once you know it
        # ran. scored==0 → no emittable rule to score; scored>0 → ran, nothing
        # cleared the bar.
        log.debug(
            "trigger_match Layer 2 no hit: scored=%d best=%.3f threshold=%.2f%s",
            scored, best_score, EMBEDDING_THRESHOLD,
            (" top=" + best_rule.action_type) if best_rule else "",
        )
        return None
    return best_rule, best_score


def _populate_embeddings(cache: _ProfileCache) -> bool:
    """Compute and cache an embedding per rule. Embed the SAME string
    Layer 1 substring-matches against (`match_text` resolution above),
    so semantic similarity is measured against the full intent the
    operator captured, not against a short stub phrase that's
    ambiguous on its own.

    Returns False on failure and leaves `cache.embeddings = None` so the
    caller can schedule a cooldown-bounded retry (a one-off API blip must
    not disable Layer 2 for the whole process lifetime).
    """
    phrases = [r.match_text or r.phrase for r in cache.rules]
    if not phrases:
        cache.embeddings = []
        return True
    vecs = _embed_batch(phrases)
    if vecs is None:
        cache.embeddings = None  # not "empty" — "needs retry"; caller gates it
        return False
    if len(vecs) != len(phrases):
        log.warning(
            "trigger_match embedding count mismatch: %d phrases, %d vectors",
            len(phrases), len(vecs),
        )
        cache.embeddings = None
        return False
    cache.embeddings = vecs
    log.info(
        "trigger_match Layer 2 embeddings built: rules=%d dim=%d",
        len(vecs), len(vecs[0]) if vecs[0] else 0,
    )
    return True


def _embed_one(text: str) -> list[float] | None:
    vecs = _embed_batch([text])
    if not vecs:
        return None
    return vecs[0]


def _probe_openai_key_dpapi(db_path: "Path | None") -> str:
    """Return a human-readable diagnostic if `openai_api_key` exists
    in the given DB but can't be DPAPI-decrypted by this session.
    Returns "" when the key is simply absent (caller falls back to the
    generic "no key" message). Used by the Test pane to distinguish
    "never saved" from "saved but unreadable" — those need different
    remediations."""
    if db_path is None:
        return ""
    try:
        from src import db_settings
        raw = db_settings._read_raw(db_path, "openai_api_key")
    except Exception:  # noqa: BLE001
        return ""
    if not raw:
        return ""
    try:
        from src.secret_box import decrypt
        if decrypt(raw):
            # Decryptable — caller's _resolve_openai_key would have
            # returned a value already; reaching here means something
            # else is off, not a DPAPI problem.
            return ""
    except OSError:
        return (
            f"openai_api_key exists in {db_path.name} but this Windows "
            f"session can't decrypt it (DPAPI rejected the ciphertext). "
            f"Most likely the key was saved under a different security "
            f"context (different user, password reset, machine restore). "
            f"Open Settings → Tuning and re-enter the OpenAI API key to "
            f"re-encrypt it under this session."
        )
    return ""


def _resolve_openai_key(db_path: "Path | None" = None) -> str:
    """Resolve the OpenAI API key for embedding calls.

    Source precedence (matches what an operator naturally expects):
      1. `OPENAI_API_KEY` env var — convenient for ad-hoc CLI runs
         and for harnesses that explicitly export it.
      2. The supplied `db_path` (if any) — directly reads + DPAPI-
         decrypts `openai_api_key` from that DB. Used by the GUI's
         Test pane so the matcher checks the ACTIVE stack's DB, not
         the GUI process's default `config.DB_PATH`.
      3. `config.OPENAI_API_KEY` — the live listener path's source of
         truth; reads from `config.DB_PATH` which NSSM has already
         pinned to the right stack via `--db-path`.
    Returns "" when none of them have it.
    """
    env_key = os.environ.get("OPENAI_API_KEY") or ""
    if env_key:
        return env_key
    if db_path is not None:
        try:
            from src import db_settings
            v = db_settings.get_str(db_path, "openai_api_key", "")
            if v:
                return v
        except OSError as e:
            # DPAPI decrypt failure — most common cause is a key
            # saved under a different security context (e.g., user
            # password change broke the credential chain, or stack
            # DB was restored from backup on another machine). The
            # ciphertext is intact but unreadable; only a re-save
            # via Settings restores access. Log at WARNING so the
            # operator sees it once on first match attempt.
            log.warning(
                "trigger_match: openai_api_key in %s is unreadable by this "
                "session (DPAPI: %s). Re-enter the key in Settings → Tuning "
                "to re-encrypt under the current Windows context.",
                db_path, e,
            )
        except Exception as e:  # noqa: BLE001 — other failures must not raise
            log.warning("trigger_match: openai_api_key read failed: %s", e)
    try:
        from src import config
        return str(config.OPENAI_API_KEY or "")
    except Exception:  # noqa: BLE001 — config probe must never raise here
        return ""


def _embed_batch(
    inputs: list[str], db_path: "Path | None" = None,
) -> list[list[float]] | None:
    """One OpenAI embeddings call; returns None on any failure.

    `db_path` is forwarded to `_resolve_openai_key` so the GUI's Test
    pane can target the active stack's DB instead of falling back to
    `config.DB_PATH` (which the GUI process inherits as the `default`
    stack, not the operator's actual stack).

    Failures are logged at WARNING (not exception) — Layer 2 is
    optional augmentation; a missing API key or network blip must
    never break Layer 1 or the Sonnet fallback.
    """
    if not inputs:
        return []
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("trigger_match embedding: openai package not installed")
        return None
    api_key = _resolve_openai_key(db_path)
    if not api_key:
        log.warning(
            "trigger_match embedding: no OpenAI key (env OPENAI_API_KEY unset "
            "AND config.OPENAI_API_KEY empty — set one via the env or Settings)"
        )
        return None
    try:
        client = OpenAI(api_key=api_key)
        t0 = time.monotonic()
        resp = client.embeddings.create(model=EMBEDDING_MODEL, input=inputs)
        ms = int((time.monotonic() - t0) * 1000)
        log.debug("trigger_match embeddings call: n=%d latency_ms=%d", len(inputs), ms)
        return [d.embedding for d in resp.data]
    except Exception as e:  # noqa: BLE001 — bound for telemetry
        log.warning("trigger_match embeddings call failed: %s", e)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


# ---- State preconditions ------------------------------------------------


def _precondition_ok(action_type: str, ctx: MatchContext) -> bool:
    """Refuse a candidate match if state forbids the action.

    These mirror the live interpreter's idempotency rules — promoted
    to code so the matcher can't fire a CLOSE_FULL with no open
    position (which would just be rejected by the EA, wasting a row).

    Reply-intent meta-types (TRIGGER_PARENT / IGNORE_PARENT) shouldn't
    reach Layer 1's normal candidate path — they're handled separately
    in `_classify_reply_intent`. Returning False here ensures that
    if they do leak through (e.g. an embedding-similarity hit), they
    can't accidentally be passed to `_build_actions` and emit a real
    action row.
    """
    if action_type in _REPLY_INTENT_TYPES:
        return False
    if action_type in (
        "CLOSE_FULL", "MOVE_SL_BE", "CLOSE_PARTIAL",
        "MOVE_SL", "TIGHTEN_SL", "MODIFY_TPS",
    ):
        return ctx.has_open_position
    if action_type == "REINFORCE":
        return ctx.has_open_position
    if action_type == "REOPEN_LAST":
        return not ctx.has_open_position and ctx.has_closed_within_24h
    if action_type == "CANCEL_PENDING":
        return ctx.has_pending_open
    # OPEN / OPEN_INSTANT / etc. aren't matched here at all; allowed-set
    # gate above already rejected them.
    return True


# ---- Action construction ------------------------------------------------


def _build_actions(
    rules: list[TriggerRule],
    ctx: MatchContext,
    symbol: str,
) -> list[Action]:
    """Turn matched trigger rules into validators.Action instances the
    orchestrator's persistence loop already knows how to handle.

    Each action_type has a fixed default payload (the channels we
    target don't carry overrides for these — operator-curated phrase
    means "the canonical CLOSE_PARTIAL", etc.). If a deployment ever
    needs per-trigger overrides (e.g. CLOSE_PARTIAL fraction=0.7 for
    one phrase), this is the place to wire it in.

    `symbol` is the channel-specific trading symbol from profile.json
    — passed in so per-channel matchers don't share a single hardcoded
    project constant.
    """
    out: list[Action] = []
    for r in rules:
        # Defensive: reply-intent meta-rules are dispatch signals, not
        # real actions. `_precondition_ok` already rejects them, but
        # belt-and-braces in case a future code path bypasses that.
        if r.action_type in _REPLY_INTENT_TYPES:
            continue
        action = _build_one(r, ctx, symbol)
        if action is not None:
            out.append(action)
    return out


def _build_one(
    rule: TriggerRule,
    ctx: MatchContext,
    symbol: str,
) -> Action | None:
    at = rule.action_type
    if at == "CLOSE_FULL":
        return CloseFullAction()
    if at == "MOVE_SL_BE":
        return MoveSlBeAction()
    if at == "CLOSE_PARTIAL":
        return ClosePartialAction()  # default fraction=0.5
    if at == "REOPEN_LAST":
        return ReopenLastAction()    # default within_hours=24
    if at == "TIGHTEN_SL":
        return TightenSlAction()     # default by_fraction=0.5
    if at == "CANCEL_PENDING":
        # CANCEL_PENDING needs the channel's trading symbol — sourced
        # from profile.json (cached upstream) so the matcher stays
        # channel-agnostic instead of assuming a project-wide constant.
        if not symbol:
            log.warning(
                "trigger_match CANCEL_PENDING skipped: profile.symbol empty",
            )
            return None
        return CancelPendingAction(symbol=symbol)
    if at == "REINFORCE":
        # REINFORCE requires a side. Take it from the currently-open
        # position — REINFORCE is meaningless without one, and the
        # precondition gate has already confirmed it exists.
        side = ctx.open_position_side
        if side not in ("BUY", "SELL"):
            log.warning(
                "trigger_match REINFORCE skipped: open side unknown (%r)", side,
            )
            return None
        return ReinforceAction(side=side)
    return None


# ---- Profile cache loader ------------------------------------------------


def _resolve_profile_path() -> Path | None:
    """Resolve the active stack's profile.json path.

    Mirrors `src.ai._resolve_profile_path`'s logic conceptually but
    inline so the matcher doesn't pull AI imports. Falls back to None
    when the listener can't find a profile (fresh install) — Layer 1
    and 2 then both no-op and Sonnet handles every message as today.
    """
    from src.config import DB_PATH
    if not DB_PATH:
        return None
    return Path(DB_PATH).parent / "profile.json"


def _load_cache() -> _ProfileCache | None:
    """Load and parse the active profile's triggers, refreshing the
    in-memory cache when the underlying file's mtime changes.

    Thread-safe via a single module-level lock — the listener calls
    this from one thread but the trades_log scheduler is theoretically
    concurrent. Avoids the embedding recompute cost on every message.
    """
    global _cache
    path = _resolve_profile_path()
    if path is None or not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _cache_lock:
        if _cache is not None and _cache.path == path and _cache.mtime == mtime:
            return _cache
        rules, symbol = _read_profile(path)
        _cache = _ProfileCache(
            path=path, mtime=mtime, rules=rules, symbol=symbol, embeddings=None,
        )
        log.info(
            "trigger_match cache loaded: path=%s symbol=%s rules=%d",
            path, symbol or "(empty)", len(rules),
        )
        return _cache


def _read_profile(path: Path) -> tuple[list[TriggerRule], str]:
    """Parse the profile's `triggers` array + `symbol` field.

    Returns (rules, symbol). Tolerant: malformed entries are skipped
    with a warning. Unknown keys are ignored.

    Reply-intent rules live INSIDE the triggers array with the special
    action_types TRIGGER_PARENT and IGNORE_PARENT. The matcher
    recognises them when handling a Telegram reply.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("trigger_match: failed to read profile %s: %s", path, e)
        return [], ""
    symbol = str(data.get("symbol") or "").strip()
    raw_triggers = data.get("triggers") or []
    out: list[TriggerRule] = []
    for i, t in enumerate(raw_triggers):
        if not isinstance(t, dict):
            continue
        if not t.get("enabled", True):
            continue
        phrase = (t.get("phrase") or "").strip()
        if not phrase:
            continue
        # action_types (new compound shape) takes precedence; fall back to
        # legacy action_type (single string). Each bucket becomes its own
        # rule so compound triggers fan out cleanly into the matcher.
        ats_raw = t.get("action_types")
        if isinstance(ats_raw, list) and ats_raw:
            action_types = [str(a).upper().strip() for a in ats_raw if a]
        else:
            single = t.get("action_type")
            action_types = [str(single).upper().strip()] if single else []
        if not action_types:
            continue
        context_tokens_raw = t.get("context_tokens") or []
        if not isinstance(context_tokens_raw, list):
            continue
        norm_context = tuple(
            normalize(str(c)) for c in context_tokens_raw if str(c).strip()
        )
        norm_phrase = normalize(phrase)
        if not norm_phrase:
            continue
        pending = t.get("pending")
        if pending is not None and not isinstance(pending, bool):
            pending = None
        match_text, norm_match_text, source = _resolve_match_text(t, phrase, norm_phrase)
        for at in action_types:
            out.append(TriggerRule(
                action_type=at,
                phrase=phrase,
                norm_phrase=norm_phrase,
                norm_context_tokens=norm_context,
                match_text=match_text,
                norm_match_text=norm_match_text,
                source=source,
                pending=pending,
            ))
    return out, symbol


def _resolve_match_text(
    trigger: dict, phrase: str, norm_phrase: str,
) -> tuple[str, str, str]:
    """Pick the string the matcher compares against the incoming message.

    Policy: **always use `samples[0]` (the full original message) when
    present, regardless of length. Never match against `phrase`.**

    Rationale: the classifier's `phrase` is a short distinctive
    substring meant for labelling, not matching. Substring-matching
    against the short phrase under-matches (a 4-char "TP1 ✅" can't
    distinguish a celebration message from a close-intent message)
    and over-matches at the same time (every TP1 announcement
    contains those exact characters). `samples[0]` is the full
    message the operator confirmed as the canonical example —
    matching against it is what "messages like THIS one" actually
    means in operator mental model.

    Returns (match_text, norm_match_text, source). When the trigger
    has no samples[0], returns ("", "", "phrase") to signal there's
    nothing to match — `is_emittable` then rejects the rule. Phrase
    is kept on the rule purely as a label for diagnostics; it never
    drives a match.
    """
    samples = trigger.get("samples") or []
    if isinstance(samples, list) and samples:
        first = str(samples[0]).strip()
        if first:
            return first, normalize(first), "sample"
    return "", "", "phrase"


def invalidate_cache() -> None:
    """Force the next match() call to re-read the profile from disk.

    Optional helper for tests and for callers that want immediate
    pickup after a profile edit instead of waiting for the next mtime
    tick. Not required for normal operation — mtime polling handles it.
    """
    global _cache
    with _cache_lock:
        _cache = None


# ---- Diagnostic API for the Triggers editor's Test pane ------------------
#
# Operator-facing dry-run: take in-memory triggers + a sample message +
# fake state, return per-rule "matched / why-not" diagnostics for both
# layers PLUS the actions that match() would emit if this were a live
# message. Lets the operator calibrate phrases + context tokens +
# state preconditions before saving.


@dataclass(frozen=True)
class RuleDiagnostic:
    """One Layer-1 rule's outcome against the test message."""
    rule: TriggerRule
    matched: bool
    reason: str  # short human-readable explanation


@dataclass(frozen=True)
class EmbeddingDiagnostic:
    """One Layer-2 rule's cosine score against the test message."""
    rule: TriggerRule
    score: float
    would_fire: bool          # score >= threshold AND preconditions ok
    skipped_reason: str = ""  # why the rule wouldn't fire even with score


@dataclass(frozen=True)
class TestMatchResult:
    """Full diagnostic result of a Test pane invocation."""
    layer1: list[RuleDiagnostic]
    layer2_available: bool
    layer2: list[EmbeddingDiagnostic]  # sorted desc by score
    layer2_note: str                   # e.g. "OPENAI_API_KEY not set"
    actions: list[Action]              # what match() would emit for this msg
    # The IGNORE rule that would suppress this message (whole-message
    # equality), or None. Set only when no emittable action fires — mirrors
    # production precedence (emittable Layer 1 → IGNORE → Layer 2).
    suppressed_by: TriggerRule | None = None


def test_match(
    text: str,
    triggers_raw: list[dict],
    ctx: MatchContext,
    symbol: str,
    db_path: "Path | None" = None,
) -> TestMatchResult:
    """Run both matcher layers against in-memory triggers and return
    per-rule diagnostics — does NOT touch the live cache or DB.

    `db_path` should be the active stack's DB path; it's used only to
    resolve the OpenAI API key for the embedding call (Layer 2). The
    GUI process's `config.DB_PATH` defaults to the `default` stack so
    without this the Test pane silently reports "embedding disabled"
    even when the operator has the key saved on a different stack.

    Mirrors the production match() decision logic exactly so what the
    Test pane shows == what the listener would do for this message:
      - Layer 1: per-rule "matched" or "why not"
      - Layer 2: per-rule cosine score, would-fire flag
      - actions: the actual emit list (longest-match-wins applied,
        compound buckets fanned out, state-aware payloads built)
    """
    rules = _parse_raw_triggers(triggers_raw)
    norm_msg = normalize(text)

    # Layer 1 — diagnostic per rule (even non-matches get a reason).
    layer1_diag: list[RuleDiagnostic] = [
        _diagnose_rule(r, norm_msg, ctx) for r in rules
    ]

    # Layer 2 — embedding cosine, conditional on API availability.
    # Resolve the key through the same env-then-config fallback the
    # live matcher uses, so the Test pane never says "disabled" when
    # the live path is actually working (operator stored the key via
    # Settings instead of env). That was a confusing diagnostic.
    layer2_diag: list[EmbeddingDiagnostic] = []
    layer2_note = ""
    layer2_available = False
    if not _resolve_openai_key(db_path):
        # Distinguish "no key anywhere" from "key exists but DPAPI
        # refused it" — the second case requires the operator to
        # re-enter the key, not save a new one for the first time.
        dpapi_diagnostic = _probe_openai_key_dpapi(db_path)
        if dpapi_diagnostic:
            layer2_note = dpapi_diagnostic
        else:
            layer2_note = (
                "No OpenAI key — set OPENAI_API_KEY in the env or save one "
                "via Settings to enable the embedding layer."
            )
    else:
        try:
            from openai import OpenAI  # noqa: F401  — availability probe only
            layer2_available = True
        except ImportError:
            layer2_note = "openai package not installed — embedding layer disabled."

    if layer2_available and rules:
        # Embed match_text (full sample for short-phrase triggers,
        # phrase otherwise) — matches what production Layer 2 does
        # and what Layer 1 substring-matches against.
        phrases = [r.match_text or r.phrase for r in rules]
        # One batched call: all trigger phrases + the message. Slicing
        # back out keeps a single round-trip regardless of trigger count.
        # NOTE: this _embed_batch is inside test_match; the production
        # _layer2_embedding path uses cached embeddings and only fetches
        # the message vector each time.
        vecs = _embed_batch(phrases + [text], db_path=db_path)
        if vecs is None or len(vecs) != len(phrases) + 1:
            layer2_note = (
                "OpenAI embedding API call failed (see listener log)."
            )
        else:
            trigger_vecs = vecs[:-1]
            msg_vec = vecs[-1]
            for r, vec in zip(rules, trigger_vecs, strict=True):
                score = _cosine(msg_vec, vec)
                precondition_ok = _precondition_ok(r.action_type, ctx)
                emittable = r.is_emittable
                would_fire = (
                    score >= EMBEDDING_THRESHOLD
                    and precondition_ok
                    and emittable
                )
                skipped = ""
                if not emittable:
                    skipped = "rule not emittable (phrase too short or unsupported action type)"
                elif not precondition_ok:
                    skipped = _precondition_label(r.action_type)
                layer2_diag.append(EmbeddingDiagnostic(
                    rule=r, score=score, would_fire=would_fire,
                    skipped_reason=skipped,
                ))
            layer2_diag.sort(key=lambda d: -d.score)

    # Determine what the production match() would do, in the same order:
    # emittable Layer 1 → deterministic IGNORE → Layer 2 embedding. IGNORE
    # rules never emit, so they're excluded from the action candidates and
    # surfaced separately as `suppressed_by`.
    actions: list[Action] = []
    suppressed_by: TriggerRule | None = None
    layer1_matched_rules = [
        d.rule for d in layer1_diag
        if d.matched and d.rule.action_type != "IGNORE"
    ]
    if layer1_matched_rules:
        best: dict[str, TriggerRule] = {}
        for r in layer1_matched_rules:
            cur = best.get(r.action_type)
            if cur is None or len(r.norm_phrase) > len(cur.norm_phrase):
                best[r.action_type] = r
        actions = _build_actions(list(best.values()), ctx, symbol)
    else:
        suppressed_by = _layer1_ignore_match(norm_msg, rules)
        if suppressed_by is None and layer2_diag:
            firing = [d for d in layer2_diag if d.would_fire]
            if firing:
                # layer2_diag is sorted by score desc; firing[0] wins.
                actions = _build_actions([firing[0].rule], ctx, symbol)

    return TestMatchResult(
        layer1=layer1_diag,
        layer2_available=layer2_available,
        layer2=layer2_diag,
        layer2_note=layer2_note,
        actions=actions,
        suppressed_by=suppressed_by,
    )


def _parse_raw_triggers(raw_triggers: list[dict]) -> list[TriggerRule]:
    """Build TriggerRule instances from in-memory trigger dicts (the
    Triggers editor's edit state). Same parsing logic as _read_profile
    but takes the list directly instead of reading from disk — so the
    Test pane tests UNSAVED edits, not the on-disk version."""
    out: list[TriggerRule] = []
    for t in raw_triggers:
        if not isinstance(t, dict):
            continue
        if not t.get("enabled", True):
            continue
        phrase = (t.get("phrase") or "").strip()
        if not phrase:
            continue
        ats_raw = t.get("action_types")
        if isinstance(ats_raw, list) and ats_raw:
            action_types = [str(a).upper().strip() for a in ats_raw if a]
        else:
            single = t.get("action_type")
            action_types = [str(single).upper().strip()] if single else []
        if not action_types:
            continue
        context_tokens_raw = t.get("context_tokens") or []
        if not isinstance(context_tokens_raw, list):
            continue
        norm_context = tuple(
            normalize(str(c)) for c in context_tokens_raw if str(c).strip()
        )
        norm_phrase = normalize(phrase)
        if not norm_phrase:
            continue
        pending = t.get("pending")
        if pending is not None and not isinstance(pending, bool):
            pending = None
        match_text, norm_match_text, source = _resolve_match_text(t, phrase, norm_phrase)
        for at in action_types:
            out.append(TriggerRule(
                action_type=at,
                phrase=phrase,
                norm_phrase=norm_phrase,
                norm_context_tokens=norm_context,
                match_text=match_text,
                norm_match_text=norm_match_text,
                source=source,
                pending=pending,
            ))
    return out


def _diagnose_rule(
    rule: TriggerRule, norm_msg: str, ctx: MatchContext,
) -> RuleDiagnostic:
    """Explain why a rule did or didn't match. Order of checks mirrors
    _layer1_deterministic so the diagnostic accurately reflects the
    production decision path."""
    if rule.action_type == "IGNORE":
        # IGNORE rules don't emit — they suppress on whole-message equality.
        if not rule.norm_match_text:
            return RuleDiagnostic(
                rule, False,
                "no sample stored — add the original message as a sample to "
                "enable this IGNORE rule",
            )
        if not _ignore_sample_eligible(rule.norm_match_text):
            return RuleDiagnostic(
                rule, False,
                "sample is only digits — too generic to suppress on",
            )
        if rule.norm_match_text != norm_msg:
            return RuleDiagnostic(
                rule, False,
                "whole-message equality required — message differs from "
                "the IGNORE sample",
            )
        if rule.norm_context_tokens:
            missing = [
                t for t in rule.norm_context_tokens if t not in norm_msg
            ]
            if missing:
                return RuleDiagnostic(
                    rule, False,
                    "missing context token(s): "
                    + ", ".join(repr(t) for t in missing),
                )
        return RuleDiagnostic(
            rule, True, "would suppress as curated noise (whole-message match)",
        )
    if rule.action_type not in _ALLOWED_ACTION_TYPES:
        return RuleDiagnostic(
            rule, False,
            f"action_type '{rule.action_type}' isn't deterministic-emittable "
            "(OPEN family stays on Sonnet)",
        )
    if not rule.norm_match_text:
        return RuleDiagnostic(
            rule, False,
            "no sample stored — matcher uses samples[0] as the comparison "
            "source; add the original message as a sample to enable this "
            "trigger",
        )
    if rule.norm_match_text not in norm_msg:
        return RuleDiagnostic(
            rule, False, "sample not in normalised message",
        )
    if rule.norm_context_tokens:
        missing = [t for t in rule.norm_context_tokens if t not in norm_msg]
        if missing:
            return RuleDiagnostic(
                rule, False,
                "missing context token(s): " + ", ".join(repr(t) for t in missing),
            )
    if not _precondition_ok(rule.action_type, ctx):
        return RuleDiagnostic(
            rule, False, _precondition_label(rule.action_type),
        )
    return RuleDiagnostic(rule, True, "matched")


def _precondition_label(action_type: str) -> str:
    """Human-readable explanation of which state precondition gates an
    action. Used by the Test pane so the operator sees WHY a rule was
    skipped, not just THAT it was."""
    return {
        "CLOSE_FULL":     "precondition: no open position",
        "MOVE_SL_BE":     "precondition: no open position",
        "CLOSE_PARTIAL":  "precondition: no open position",
        "MOVE_SL":        "precondition: no open position",
        "TIGHTEN_SL":     "precondition: no open position",
        "MODIFY_TPS":     "precondition: no open position",
        "REINFORCE":      "precondition: no open position",
        "REOPEN_LAST":    "precondition: need NO open position AND a closed one within 24h",
        "CANCEL_PENDING": "precondition: no pending OPEN to cancel",
    }.get(action_type, "precondition failed")
