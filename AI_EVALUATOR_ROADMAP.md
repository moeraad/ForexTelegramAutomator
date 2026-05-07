# AI Signal-Quality Evaluator — Roadmap

Post-v1 improvement plan for the signal-quality evaluator
(`src/ai_evaluator.py`, the `/market/snapshot` and
`/actions/latest_open_evaluation` API endpoints, EA `PostMarketSnapshot()`
and the `DrawSignalQuality` dashboard widget).

Ordered by **signal-to-cost ratio** — what gives you the most for the
least work, with explicit gates so you don't build features the data
doesn't justify yet.

---

## Phase 1 — Validate v1 before building anything new

**Don't add any of the features below until v1 has produced ≥2 weeks of
real scores with real outcomes.** Premature optimization is the easiest
trap with this kind of system.

### 1.1 Score-vs-outcome calibration

Pull every `actions WHERE action_type='OPEN'` joined to its resulting
`positions` row, bucket by score band, compute realized P&L per bucket.

```sql
SELECT
    CASE
        WHEN json_extract(a.payload_json, '$.evaluation.score') >= 80 THEN 'strong (80-100)'
        WHEN json_extract(a.payload_json, '$.evaluation.score') >= 60 THEN 'moderate (60-79)'
        WHEN json_extract(a.payload_json, '$.evaluation.score') >= 40 THEN 'weak (40-59)'
        ELSE 'avoid (0-39)'
    END                                      AS bucket,
    COUNT(*)                                 AS trades,
    SUM(CASE WHEN p.partial_close_count>0 THEN 1 ELSE 0 END) AS wins,
    SUM(CASE WHEN p.close_reason='mt5_close'
              AND p.partial_close_count=0 THEN 1 ELSE 0 END) AS losses
FROM actions a
JOIN positions p ON p.action_id = a.id
WHERE a.action_type = 'OPEN'
  AND a.status IN ('executed', 'failed')
GROUP BY bucket;
```

**Decision gate:**
- High-score signals materially outperform low-score → calibration
  works, proceed to Phase 2.
- Buckets are flat (similar win rates regardless of score) → AI isn't
  actually distinguishing signal quality. Adding more inputs won't fix
  this. Either rewrite the prompt or turn the feature off.

### 1.2 Identify systematic blind spots

For every losing trade, read the evaluation's `summary` and `key_factor`
fields. Look for patterns:
- Did the AI flag a concern but the trade was taken anyway?
- Did the AI miss a recurring failure mode (always over-scoring
  counter-trend setups, always missing news-event risk, etc.)?

Document the failure modes — that's the input list for Phase 3.

---

## Phase 2 — Make the score actionable

**Gate:** only after Phase 1.1 confirms calibration.

The v1 score is purely informational. Once it's known to be calibrated,
the natural next step is letting it affect behaviour.

### 2.1 Operator approval gate for borderline scores  *(recommended start)*

If `40 ≤ score < 60`, the action holds in `pending` for N minutes and
the bot DMs the operator: *"Action #N — score 47 (weak). Approve to
proceed?"*. Operator confirms via inline keyboard.

- **Pros:** hybrid auto/manual. No fully-automatic gate yet, so
  calibration errors are recoverable.
- **Implementation:** ~30 lines in the bot's notification dispatcher;
  reuse the existing inline-keyboard pattern.

### 2.2 Score-modulated position sizing

Multiply `LotsFromRisk` by a factor derived from the score:

| Score band | Multiplier |
|---|---|
| 80–100 (strong) | 1.0× |
| 60–79 (moderate) | 0.7× |
| 40–59 (weak) | 0.3× |
| 0–39 (avoid) | 0.0× (skip) |

- **Pros:** continuous gradient on quality; doesn't binary-reject.
- **Cons:** makes calibration accuracy genuinely matter — under-scored
  good trades cost real money.
- **Implementation:** ~10 lines in EA `LotsFromRisk` reading the
  evaluation cache populated by `FetchLatestEvaluation`.

### 2.3 Hard threshold rejection

`if score < threshold → reject the action with reason='low_eval_score'`.

Skip this unless 2.1 + 2.2 prove insufficient. Hard rejection loses
information vs modulation and is easier to revert via threshold tuning.

---

## Phase 3 — Improve the inputs the AI sees

**Gate:** only justified by specific blind spots from Phase 1.2.

Each addition should be tied to a concrete failure mode the AI is
making, not "more data = better."

### 3.1 Round-number proximity *(free win, no external dependency)*

Gold respects 4700, 4750, 4800. Compute distance from current price
and signal entry to nearest round level. Pure Python math, no API
needed.

- **Implementation:** ~15 lines in `build_evaluator_input`.

### 3.2 DXY direction (and US 10Y yields)

Gold is dollar-inverse and yield-inverse. A BUY signal during a sharp
DXY rally has structurally lower edge than the same signal during DXY
weakness.

- **Implementation:** Python listener fetches DXY from a free feed
  (Yahoo, Alpha Vantage, TwelveData free tier) every minute, stores in
  `settings`, evaluator reads it.
- **Cost:** 1 free API account, ~30 lines.

### 3.3 News calendar awareness

ForexFactory CSV → "no high-impact news in next 60 min" / "FOMC in 8
min". Gold spikes through normal SL on FOMC/NFP/CPI announcements.

- **Implementation:** ~80 lines, weekly scrape with daily refresh.
  Brittle (ForexFactory format changes). Manual fallback: maintain a
  small JSON of upcoming high-impact events.
- **Decision:** worth it if Phase 1.2 shows news-driven losses.

### 3.4 Recent swing levels

Algorithmic detection of last 3 swing highs/lows on H1 (3-bar fractal
or simple lookback). Tells the AI:
- Is signal SL below the most recent swing low (good support) or in
  the middle of chop (poor SL placement)?
- Are TPs at meaningful resistance levels or arbitrary numbers?

- **Implementation:** ~50 lines of Python on the M15/H1 OHLC the
  snapshot already provides.

### 3.5 Channel-type-conditional history

Today's "channel win rate" is global across all signal types. Improve
by conditioning on signal pattern:
- Compound message vs bare command
- With explicit entry zone vs implicit
- With/without "اشتري الذهب" preamble

If "structured BUY block + structured TP1/TP2/TP3" wins 70% but bare
"اشتري الذهب" wins 40%, the AI should know that.

- **Implementation:** ~40 lines extending
  `_compute_channel_recent_outcomes`.

---

## Phase 4 — Architecture improvements

How the evaluator works, rather than what it sees.

### 4.1 Combine triage + interpreter + evaluator into one LLM call

Currently three round-trips per signal (~1.2s triage + ~10s interpreter
+ ~5s evaluator = ~16s end-to-end). One unified call could cut this to
~10s.

- **Trade-off:** prompt complexity grows substantially; harder to test
  and to swap models per task.
- **Worth doing:** only if latency is biting actual fills (fast
  breakouts past entry zone before EA can react).

### 4.2 Specialized model per axis

Use a cheap model (Haiku 4.5 / gpt-5-nano) for deterministic axes
(R:R math, premise check) and the smarter model only for the
reasoning-heavy axes (trend, macro alignment).

- **Win:** ~50% cost reduction on routine signals.
- **Cost:** ~80 lines split-call orchestration.

### 4.3 Cached snapshot reuse

Currently every evaluator call rebuilds context from DB. For signals
arriving in clusters (3 signals in 2 minutes), cache the base context
for 30 seconds.

- **Win:** modest. Maybe 1–2 seconds per clustered signal.
- **Skip until** clustering becomes a measured problem.

---

## Phase 5 — Learn from history

### 5.1 Self-calibration loop

Once you have 100+ scored signals with outcomes, run a periodic
retrospective: compare predicted score vs realized R-multiple per
signal. If the AI is systematically over-scoring counter-trend setups
by 15 points, add that bias correction to the prompt:

> "You've historically over-rated counter-trend; subtract 10 from
> scores for those."

Closes the calibration loop without retraining or fine-tuning.

### 5.2 Per-signal-type score baseline

Different signal types have different baseline outcomes (see Phase 3.5).
Compute a separate baseline-adjusted score per type. Mostly a
prompt-engineering project — feed the AI the type-specific baseline
explicitly.

### 5.3 Session-aware scoring

Asian session ranges differ from London open differs from NY afternoon.
Add session as an explicit input and let the prompt reason about
volatility regime expectations.

---

## What to skip / defer indefinitely

These are tempting but typically don't justify the cost on this system:

- **Multi-asset correlation matrices** (gold × DXY × oil × yields ×
  etc). Sounds smart, adds noise. Skip unless Phase 1.2 directly
  demands it.
- **Sentiment / positioning data** (COT reports, retail trader
  positioning). Lagged, low signal-to-noise on intraday gold.
- **Volume profile / market microstructure.** Gold spot CFD volume is
  unreliable. Hard pass.
- **Reinforcement learning / fine-tuning to optimize the prompt.**
  Too few samples, too noisy a signal. Manual prompt iteration is
  faster and more interpretable.

---

## Concrete next action

After v1 has 1–2 weeks of live data:

1. Write `scripts/score_calibration.py` — runs the SQL above, prints
   the bucket-vs-outcome table, optionally writes a markdown summary.
2. Run it. Look at the table.
3. **If buckets are flat:** stop. Reread the AI's `summary` text on
   recent losers and figure out why before building anything else.
4. **If buckets correlate:** start Phase 2.1 (approval gate for
   borderline). It's the lowest-commitment way to make the score
   useful.

The temptation will be to keep adding inputs because "more data must be
better." Resist it. The biggest improvement comes from **knowing
whether v1 actually works**, not from making it more elaborate.
