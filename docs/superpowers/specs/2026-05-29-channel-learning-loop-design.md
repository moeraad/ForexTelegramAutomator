# Channel Learning Loop (CLL) — Design

**Date:** 2026-05-29
**Status:** Approved (design); implementation plan pending
**Author:** brainstormed with operator (moe.raad@gmail.com)

## Problem

CopyTrades classifies every Telegram message through a pipeline:
`prefilter → trigger_matcher → triage (gpt-5-nano) → interpret (gpt-5) → actions`.

Diagnostics for the "Forex Engineer" channel (2026-05-28) showed **~428 OpenAI
requests/day**, of which the expensive `interpret` (gpt-5 + extended thinking)
stage ran 128 times. Outcome breakdown of those 128 calls:

| Outcome | Count | Trade-relevant? |
|---|---|---|
| `context` | 55 | no |
| `ignore` | 50 | no (triage false-positives) |
| `signal` | 16 | yes |
| `partial_signal` | 7 | yes |

**82% of expensive interpreter calls produced no actionable signal.** Root cause:
the cheap deterministic layers barely fire (prefilter 1/223, matcher 5/223,
embedding Layer 2 never even built), and triage is intentionally lenient
("when in doubt → keep"). The channel profile is generated **once** at setup by
`ai_discovery` and then frozen — it never improves from live traffic.

The investigation confirmed this is **not a bug** (zero duplicate re-processing,
no retry storm); it is structural inefficiency. The hard runtime guardrail
already exists (`cost_guard_loop` flips the kill switch on daily-budget breach),
but nothing reduces the *baseline* cost over time.

## Goal

A system that **systematically, for every new channel, learns over time how to
classify messages** so that fewer messages reach the expensive interpreter —
**without losing accuracy** (a suppressed real signal loses money; an extra
interpret call costs cents).

## Core concept — self-distillation

Every `interpret` call already produces a ground-truth label
`(message, verdict)` where `verdict ∈ {ignore, context, signal, <action_type>,
ALERT}`. Today that label is logged to `ai_calls.jsonl` and discarded.

The Channel Learning Loop **captures that label as training data and
periodically distills it into the cheap layers** — automating the manual
curation an operator would otherwise do in the Triggers tab. The expensive
interpreter is the *teacher*; the existing deterministic/triage **profile
config** is the *student*. No new model is trained.

### Decisions (locked during brainstorming)

1. **Propose-only.** The loop only ever writes *suggestions*. Nothing
   auto-activates. The operator approves every rule. (Chosen over auto-promotion
   for safety.)
2. **All four learning targets in scope:** suppression rules, deterministic
   action triggers, triage keep-trigger tuning, continuous profile refinement.
3. **Generalization = embedding cluster + LLM synthesis.** Cluster
   semantically-similar messages by verdict, then summarize each dense cluster
   into a human-readable rule. (Chosen over exact-text counting — too weak
   against varied chatter.)
4. **Rolling cadence, in the bot process.** Re-cluster a channel after it
   accumulates `LEARNING_BATCH_N` (default 50) newly-interpreted messages. No
   5th service, no external scheduler.
5. **Budget enforcement is out of scope — it already exists** (`cost_guard`).
   The CLL complements it: lower the baseline so the brake is rarely hit.

### Design stances

- **Purely additive to the live path.** Acceptance writes into the *same*
  `profile.json` fields the existing layers already read
  (`noise_patterns`, `triggers[]`, `triage_keep_triggers`, `symbol_aliases`,
  `vocabulary_table`, `worked_examples`). The live pipeline's decision logic
  needs **zero changes** — only a capture hook is added.
- **Per-channel isolation.** Every artifact is keyed by `source_channel_id`.
  A new channel starts with an empty corpus and accumulates its own. "For every
  new channel" is automatic, not a per-channel setup step.
- **Accuracy wins ties.** Cost-saving suggestions are gated on *provable* zero
  signal-loss; accuracy-improving suggestions (catching triage's missed signals)
  are surfaced first.

## Architecture

```
        ┌──────────────── live pipeline (decision logic unchanged) ────────────────┐
 msg → prefilter → trigger_matcher → triage(gpt-5-nano) → interpret(gpt-5) → actions
                                                                │
                                  (capture verdict + msg) ──────┘
                                                                ▼
                                                  learning_store (labeled corpus,
                                                  per source_channel_id)
                                                                │
              rolling trigger: every LEARNING_BATCH_N new interpreted msgs / channel
                                                                ▼
                          ┌──────────── channel_learner (bot process) ────────────┐
                          │ 1. embed uncached rows (text-embedding-3-small, batch) │
                          │ 2. cluster by verdict + cosine similarity              │
                          │ 3. LLM-synthesize each dense cluster → candidate rule  │
                          │ 4. REPLAY candidate over corpus → evidence + safety    │
                          │ 5. write ranked suggestions (status=proposed)          │
                          └────────────────────────────────────────────────────────┘
                                                                │
                                                                ▼
                              GUI Suggestions tab — operator reviews evidence,
                                       taps Accept / Dismiss
                                                                │
                          Accept → writes the correct existing profile.json field
                                                                │
                          live matcher + triage pick it up via mtime cache ────────┘
```

## Components

Five new units, each with one responsibility.

### 1. `src/learning_store.py` — labeled corpus (data layer)

New table `learning_samples`:

```
id, source_channel_id, route_id, source_msg_id,
text, norm_text,
verdict,          -- 'ignore' | 'context' | 'signal' | 'ALERT' | <action_type>
triage_decision,  -- 'keep' | 'ignore' — lets us measure triage error directly
seen_count,       -- incremented on verbatim resend (dedup on norm_text)
embedding,        -- BLOB, nullable; filled lazily by the learner
created_at, last_seen_at
```

- Capture is a **single INSERT** in `orchestrator.process_message`, immediately
  after the interpret verdict is known (adjacent to the existing
  `_record_pipeline_decision`). Zero added API cost.
- `INSERT OR IGNORE` on `(source_channel_id, norm_text)`; on conflict, bump
  `seen_count` + `last_seen_at` so frequency is preserved as evidence.
- Bounded retention: keep newest `LEARNING_CORPUS_MAX` rows/channel
  (default 2000); prune oldest beyond cap (mirrors `unmatched_store._prune`).
- **Supersedes `unmatched_store`.** The corpus is a strict superset (it captures
  `ignore`/`context` too, where the cost savings live). Migration: copy pending
  `unmatched_messages` rows into `learning_samples`, then retire the module and
  point its GUI consumers at the new Suggestions surface.
- All capture wrapped in try/except — must never break the live trade path.

### 2. `src/channel_learner.py` — batch learner (pure logic layer)

Input: a channel's recent corpus + an embedding/LLM provider. Output:
`list[Suggestion]`. No DB writes of its own → unit-testable with a mock provider
and an in-memory corpus.

- **Embed** uncached rows via `text-embedding-3-small`, batched; reuses
  `trigger_matcher._resolve_openai_key` / `_embed_batch`.
- **Cluster** within each verdict group by cosine similarity — greedy
  threshold clustering (no new deps; reuses `trigger_matcher._cosine`).
- **Synthesize**: for each cluster ≥ `MIN_CLUSTER_SIZE`, one cheap LLM call
  (gpt-5-nano / Haiku) → `{rule_kind, representative_samples, suggested_phrase,
  suggested_action_type | noise_tag, target_layer, rationale}`.
- **Replay/score**: run the candidate rule back over the full corpus to compute
  the evidence object (see Evidence & Safety).

### 3. `src/suggestion_store.py` — proposals (data layer)

New table `learning_suggestions`:

```
id, source_channel_id, rule_kind,
target_layer,      -- 'prefilter' | 'triage' | 'matcher' | 'profile'
payload_json,      -- the concrete rule to write on Accept
evidence_json,     -- replay stats (see below)
status,            -- 'proposed' | 'accepted' | 'dismissed' | 'expired'
created_at, decided_at
```

Dedups against already-active profile entries and against still-`proposed`
suggestions so the tab never repeats itself. Proposed-but-undecided rows
`expire` after `SUGGESTION_TTL_DAYS` so the tab reflects current traffic.

### 4. `src/bot_loops/learning_loop.py` — rolling scheduler

Mirrors the existing sweepers registered in `bot.py`'s `post_init`. Maintains a
per-channel "interpreted since last run" counter; when it crosses
`LEARNING_BATCH_N`, runs `channel_learner` for that channel, writes suggestions
via `suggestion_store`, and optionally DMs the operator
("N new suggestions for <channel>"). The whole loop body is wrapped so a learner
crash, embedding outage, or LLM error can never affect promotion/trade flow.

### 5. GUI `Suggestions` tab — review surface

Extends the existing Triggers view. Ranked list; each row shows the rule, its
**evidence**, and Accept / Dismiss controls.

- **Accept is the ONLY writer to `profile.json`.** It routes the suggestion
  payload into the correct existing field by `target_layer`:
  - `prefilter` → `other_instruments` / ad heuristics
  - `triage` → `noise_patterns` / `promo_indicators` / `triage_keep_triggers`
  - `matcher` → `triggers[]` (with `samples`, `action_types`)
  - `profile` → `vocabulary_table` / `worked_examples` / `symbol_aliases`
- Live layers pick up the edit via the mtime-invalidated cache already in
  `trigger_matcher` and the triage prompt renderer. No restart required.

**4th target (continuous profile refinement)** is not a separate mechanism — it
is the same pipeline with `target_layer=profile`, proposing new
vocabulary/worked-example entries from clusters of *correctly-interpreted
signals*. Reuses `ai_discovery`'s bucket definitions rather than reimplementing.

## Evidence & safety (makes propose-only trustworthy)

Every suggestion carries **replay evidence computed against the channel's own
corpus** — the operator approves with numbers, not faith.

### Suppression rule (highest risk — could hide a real signal)
- `would_suppress`: N messages the rule matches
- `verdict_breakdown`: of those N, counts by verdict
- `false_suppression_count`: matches whose verdict was `signal` / action / `ALERT`
  — **the money-losing number**
- `est_calls_saved_per_week`, `est_cost_saved`
- **Hard gate:** `false_suppression_count > 0` → suggestion is shown but flagged
  red and **cannot be one-tap accepted**; requires explicit "accept anyway" with
  the conflicting messages listed. Only `false_suppression_count == 0` AND
  `support ≥ MIN_SUPPORT` earns a green one-tap Accept.

### Deterministic action trigger (low risk)
- `support`: times the interpreter mapped this cluster → this action
- `purity`: fraction of cluster agreeing on the action (must be ≥ `MIN_PURITY`,
  e.g. 0.9)
- `precondition_consistency`: did the matcher's state preconditions hold
- `conflicts`: cluster members mapped to a *different* action → blocks one-tap

### Triage keep / context-drop tuning
- **keep-triggers (accuracy gain, top priority):** clusters of `signal`
  messages that triage had marked `ignore` (triage false-negatives).
- **context-drops (cost saving, lower risk than ignore-suppression):** clusters
  consistently `context` that triage kept.

### Background safety mechanisms
1. **Suggestion expiry (v1).** Undecided suggestions expire so the tab reflects
   current traffic, not stale clusters.
2. **Shadow re-evaluation (v2 / fast-follow — NOT in v1).** After acceptance,
   the learner periodically samples live messages the rule now suppresses/handles
   and asks the interpreter to confirm the verdict still holds. Drift (channel
   changed its language) raises an `ALERT` and proposes deactivating the rule.
   This is the long-term accuracy net and the most complex piece; the core
   capture → cluster → suggest → accept loop delivers most of the value without
   it, so it ships as a fast-follow once v1 is stable.

## Error handling

- **Capture** (live path): wrapped in try/except; any failure is logged and
  swallowed. Corpus is augmentation, never required for trading.
- **Learner**: embedding/LLM failures degrade gracefully — a failed embed batch
  skips clustering this run (retries next batch); a failed synthesis call skips
  that cluster. No partial-write corruption (suggestions written in one tx).
- **Budget interaction**: the learner's own embedding+synthesis cost is logged
  to `ai_calls.jsonl` like every other call, so `cost_guard` already accounts
  for it. The learner checks remaining daily budget before a run and defers if
  the channel is already near cap.
- **Accept writes**: validated against the profile schema before writing;
  malformed payloads are rejected with an operator-visible error rather than
  corrupting `profile.json`. Write is atomic (temp file + rename).

## Testing

- **`learning_store`**: hermetic — insert/dedup/seen_count/prune, per-channel
  isolation, capture-never-raises.
- **`channel_learner`**: pure-logic unit tests with a **mock provider** and
  in-memory corpus — clustering correctness, synthesis payload shape, and the
  full **replay/evidence math** (especially `false_suppression_count` and
  `purity`). This is the accuracy-critical surface; no live API.
- **`suggestion_store`**: dedup vs active profile + pending, expiry, status
  transitions.
- **Accept path**: round-trip test — accept a suggestion → assert the correct
  `profile.json` field is written → assert `trigger_matcher` / triage prompt
  reflect it after mtime bump.
- **Integration**: seed a corpus that mirrors the 2026-05-28 distribution,
  run a learner pass, assert it proposes (a) the noise-suppression rules that
  would have killed the 50 wasted calls with `false_suppression_count == 0`, and
  (b) the recurring management triggers.
- **Live AI replay** (opt-in, costs money): a small fixture set validating
  synthesis quality, gated behind a provider key like the existing
  `test_management_replay.py`.

## Config keys (new, profile/settings-driven, safe defaults)

| Key | Default | Meaning |
|---|---|---|
| `learning_enabled` | `1` | master on/off per stack |
| `LEARNING_BATCH_N` | `50` | new interpreted msgs before a channel re-runs |
| `LEARNING_CORPUS_MAX` | `2000` | retained rows per channel |
| `MIN_CLUSTER_SIZE` | `4` | min cluster size to synthesize a rule |
| `LEARNING_EMBED_THRESHOLD` | `0.82` | cluster cohesion (≥ matcher's 0.78) |
| `MIN_SUPPORT` | `5` | min occurrences for a one-tap suppression Accept |
| `MIN_PURITY` | `0.9` | min cluster agreement for an action trigger |
| `SUGGESTION_TTL_DAYS` | `14` | undecided-suggestion expiry |

## Out of scope / non-goals

- No auto-activation of rules (propose-only by decision).
- No new runtime budget enforcement (`cost_guard` already covers it).
- No change to the live pipeline's decision logic — capture hook only.
- No new long-running service — runs inside the existing bot process.

## Scope split

- **v1 (this plan):** capture hook → `learning_store` (+ `unmatched_store`
  migration) → `channel_learner` (embed/cluster/synthesize/replay) →
  `suggestion_store` (with expiry) → rolling `learning_loop` → GUI Suggestions
  tab with evidence-gated Accept. Operable learning targets: **noise
  suppression**, **action triggers**, **context-drop** (the three that reduce
  interpreter cost — the stated goal).
- **v2 (fast-follow, separate plan):**
  - **Triage shadow-sampling → keep-trigger learning.** The `keep_trigger`
    code path ships in v1 and is correct, but its data precondition can't be
    met in healthy operation: capture only runs on the interpret path, and
    triage-`ignore` messages return before the interpreter, so a
    `triage_decision="ignore"` signal is only ever recorded when triage itself
    errors. Detecting genuine triage false-negatives requires occasionally
    running the interpreter on triage-dropped messages (shadow-sampling) and
    feeding those verdicts into the corpus. Until that lands, keep-trigger
    suggestions effectively don't fire from real traffic.
  - **Continuous profile-vocabulary refinement** (`target_layer='profile'`).
  - **Shadow re-evaluation** drift detection on accepted rules.

## Open questions for implementation plan

- Exact greedy-clustering parameters and whether a second pass merges
  near-duplicate clusters.
