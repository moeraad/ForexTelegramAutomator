# CopyTrades — Senior Code Review

_Generated 2026-05-16. Read-only audit. No code modified._

Scope: full repo (`src/`, `ea/`, `tests/`, `src/gui/`). Reviewer's job is to flag what
will hurt money/data/trust on the next run, what will hurt them over the next month,
and what the UI is asking the operator to do that the UI doesn't actually make easy.

Architecture is taken as **deliberate**: four long-running processes (api, bot,
listener, EA) share a single per-stack SQLite DB as the sole coordination medium.
No queue, no IPC, no RPC. Findings that propose violating that contract are flagged
as such.

---

## 1. Executive Summary

The system is internally consistent and the lifecycle (`pending → sent → claimed →
{executed | failed | rejected | watching}`) is enforced end-to-end. The AI prompt and
EA state machine are clearly the load-bearing IP and they are documented well.

The most acute problems are at the **edges** of that lifecycle:

- **ALERT actions are silently swallowed.** They are written with `status='pending'`
  and **no** `execute_after`, so the promoter never advances them and the bot never
  DMs them. The operator believes the AI-error path notifies them; it does not.
- **`POST /actions/{id}/result` is not idempotent.** Combined with the 300 s
  stale-claim release, a slow EA POST can race a re-claim and cause **two broker
  orders for one AI signal**.
- **A successful EA result on a re-claimed action will mutate `positions` twice**
  via `INSERT OR IGNORE` semantics on the second pass (first insert wins, which is
  correct), but `partial_close_count` / `sl_moved_at` book-keeping fires on every
  `/update` call and there is no guard against a duplicate POST from a retrying EA.
- **The "Oldest claim" indicator in Live view queries the wrong column** (looking at
  `execute_after`, not `claimed_at`), so the operator's primary signal that a claim
  is stuck is misleading.
- **`TG_WATCHED_CHAT_ID` is captured at decorator-attach time** in the listener; the
  Settings UI implies it's a live setting, but it requires a listener restart.

UI side is in good shape after the qfluentwidgets refactor. The biggest remaining
issues are accessibility (no keyboard story for the nav rail, badges are purely
visual), the Wizard's many-state initializePage flow, and the **single-window /
single-active-stack assumption** baked into MainWindow that doesn't match the
"stack switcher" the header offers.

---

## 2. Code Review — Findings

Severity legend:
- **P0** — can cost money, data, or trust on the next run.
- **P1** — will cost it within a month or significantly hurts operator effectiveness.
- **P2** — correctness/UX bug that doesn't immediately bite.
- **P3** — code health, future-proofing, minor cleanup.

### P0 — `POST /actions/{id}/result` is not idempotent against stale-claim recycling

**Where:** `src/api.py:367-393` (post_result), `src/promoter.py:30-50`
(release_stale_claims).

**What:** `release_stale_claims` flips `claimed → sent` after 300 s and **NULLs
`claimed_at`**. If the EA was actually executing during that window and POSTs
`result` after release fired, two things happen sequentially:

1. The promoter has already returned the action to `sent`. Next EA poll → re-claim
   → second `OrderSend` against the broker.
2. The late POST from the first execution lands as
   `UPDATE actions SET status='executed' WHERE id=?` with no status guard, so the
   row reads `executed` even though there are two broker tickets.

**Why it matters:** Two orders, one signal. Real money. This isn't theoretical —
the bot's claim-sweeper loop runs every 15 s and 300 s is reachable on a flaky
network or slow broker confirmation.

**Suggested fix (diff-shaped, NOT applied):**

```sql
-- api.py post_result, replace the bare UPDATE with a status-guarded one:
UPDATE actions
   SET status = ?, result_json = ?, executed_at = ?
 WHERE id = ?
   AND status = 'claimed'
   AND claimed_at IS NOT NULL;
```

If `rowcount == 0`, the row was already released. Return 409 + a structured
`{detail: "claim_expired"}` so the EA can decide whether to attempt a corrective
close. Add a `tests/test_api.py::test_post_result_after_release` covering this.

**Open question:** Is this expected to be the EA's responsibility (it owns the
real broker state)? If so, the EA needs a guard that on re-claim it first checks
whether `magic + symbol` already has an open ticket whose request matches the
action — and only then opens. Today `DoOpen` does not consult MT5 state for an
existing matching ticket before sending.

---

### P1 — ALERT actions are written `status='pending'` with no `execute_after`, then never DMed

**Where:**
- `src/orchestrator.py:280-290` (ALERT insert path).
- `src/promoter.py:25` (`WHERE status='pending' AND execute_after IS NOT NULL AND execute_after <= ?`).
- `src/bot.py:171-254` (notification_dispatcher filters DMs to `executed|failed|rejected`).
- `src/api.py:809-846` (`/alerts` POST also writes `status='pending'`).
- `src/listener.py` first-launch backfill ALERT.

**What:** ALERTs go into the table and are then invisible. The promoter skips them
(no `execute_after`), the dispatcher skips them (terminal-status only), and the
GUI Live view treats them as non-terminal so they accumulate forever as "Pending."

**Why it matters:** ALERTs are the AI-error escape hatch and the listener's
"started up while archived messages exist" escape hatch. The operator believes
they are being notified about edge cases; they are not. Silent failure of the
notification path.

**Suggested fix (diff-shaped):**

```python
# orchestrator.py, ALERT insert — make it terminal-at-rest and DM-eligible.
conn.execute(
    "INSERT INTO actions (action_type, payload_json, status, source_msg_id, "
    "execute_after, notified_at) VALUES (?,?,?,?,?,?)",
    (
        "ALERT",
        json.dumps(action.model_dump()),
        "executed",          # terminal — dispatcher picks it up
        msg_id,
        now_iso,
        None,                # dispatcher will stamp + DM
    ),
)
```

Or alternatively: extend `notification_dispatcher` to DM ALERTs in `pending` and
mark them dispatched via `notified_at`. The first option is simpler and matches
the lifecycle constraint (ALERTs have nothing to execute against MT5).

---

### P1 — `MIN(execute_after)` instead of `MIN(claimed_at)` for "Oldest claim"

**Where:** `src/gui/views/live_view.py:97`.

```sql
SELECT MIN(execute_after) FROM actions WHERE status='claimed'
```

**What:** `claimed` actions have no `execute_after` for "Oldest claim" purposes —
that column reflects the *original* promotion delay, not the claim age. The
operator's only at-a-glance signal that a claim is stuck reads the wrong field.

**Suggested fix:**

```sql
SELECT MIN(claimed_at) FROM actions WHERE status='claimed'
```

Cross-check with `release_stale_claims` (300 s) so the indicator turns red
**before** the sweeper releases.

---

### P1 — `TG_WATCHED_CHAT_ID` captured at decorator-attach time

**Where:** `src/listener.py:266`,
`@client.on(events.NewMessage(chats=config.TG_WATCHED_CHAT_ID))`.

**What:** Changing the channel from the Settings UI or the Wizard's dialog picker
writes to `.env` / DB, but the running listener still listens on the prior value
because Telethon snapshotted it. The UI does not surface this; it looks like a
live setting.

**Why it matters:** Operator changes channels, sees no signals, can't tell why.
This has burned us at least once during stack-switching.

**Suggested fix:** Either:
- Filter inside the handler against a value read from `config` each tick, or
- Make Settings warn explicitly "Listener restart required" and disable the
  "save" action when services are running unless the user accepts a restart.

---

### P1 — `last_seen_tg_msg_id` written AFTER `process_message`

**Where:** `src/listener.py:286`.

**What:** If the orchestrator (or the AI provider) hangs/crashes between
`process_message()` and `set_setting("last_seen_tg_msg_id", ...)`, the next launch
re-processes the same Telegram message. OPENs are deduplicated by the price-band
fingerprint, so they are safe. **Management actions are not fingerprinted** —
`MOVE_SL_BE`, `CLOSE_PARTIAL`, `REINFORCE`, etc. would re-fire on relaunch.

**Why it matters:** A crash at the wrong moment after a `CLOSE_PARTIAL` can fire
a second `CLOSE_PARTIAL` on next launch. With `fraction=0.5` of a now-halved
position you'd close to 0.25× original. Worse with REINFORCE which closes-and-reopens.

**Suggested fix:** Write `last_seen_tg_msg_id` **before** invoking
`process_message` (at-least-once becomes at-most-once for the AI path; first
processing of new messages is best-effort, but a crashed run loses the *new* one
rather than re-running an old one), OR add an `idempotency_key` to management
actions and dedupe against it.

**Open question:** Is the channel's source-of-truth recovery story "operator
re-types it"? If yes, at-most-once is correct. If no, we need the idempotency key.

---

### P2 — `original_volume` snapshot is set inside `OR IGNORE` semantics, so a re-POST does not heal a missed snapshot

**Where:** `src/api.py` post_result (positions insert).

**What:** If the EA POSTs a successful open but the positions row was already
inserted from a prior partial fill (unlikely but possible due to retry quirks),
`original_volume` will reflect the **earlier** smaller volume. Subsequent
`partial_close_count` arithmetic will look correct but the AI's "0.04 of 0.02
orig" rendering will be visibly wrong.

**Why it matters:** Low blast radius today (the timing required is unlikely) but
the prompt's idempotency rules depend on `original_volume`.

**Suggested fix:** Backfill `original_volume = MAX(original_volume, NEW.volume)`
on first `/update` if the prior value was lower than the just-reported volume.

---

### P2 — `_evaluator_worker` daemon thread dies with the listener

**Where:** `src/orchestrator.py:359-429`.

**What:** OPEN-evaluator is a `threading.Thread(daemon=True)`. If the listener
crashes mid-evaluation, the AI call is abandoned. No bookkeeping in
`actions` — the AI was called and billed but no row was written. That cost shows
up in `ai_calls.jsonl` as a paid call with no executed work.

**Suggested fix:** Wrap the worker in `try/except` that writes an ALERT or a
`rejected` row tagged `reason=evaluator_crash`. Today the only trace is the log
file.

---

### P2 — `/alerts` POST does not include the operator-facing `reason`

**Where:** `src/api.py:809-846`.

**What:** `/alerts` accepts a free-form text but inserts as a vanilla ALERT.
There's no `level` discrimination passed to the dispatcher (which today doesn't
DM ALERTs anyway — see P1).

**Recommendation:** Fold this into the P1 ALERT fix.

---

### P2 — Magic number `919191` hardcoded in EA

**Where:** `ea/CopyTrades.mq5:228, 729, 822, 2209, 2227`.

**What:** Two CopyTrades stacks running on the same MT5 account will reconcile
each other's tickets. The stack registry already differentiates by `db_path`;
the EA needs a per-stack magic.

**Suggested fix:** Add `input ulong Magic = 919191;` and surface it in the
wizard's stack-creation step. Use a deterministic hash of stack name for the
default.

---

### P2 — Auth defaults to unauthenticated

**Where:** `src/api.py:224-241`.

**What:** Empty `config.EA_SHARED_TOKEN` → middleware passes all requests. The
default `.env.example` ships with an empty token. On a fresh install bound to
`127.0.0.1` this is fine; the moment someone exposes the port (Tailscale,
WireGuard, multi-machine setup) the API is open to any process on the network.

**Suggested fix:** Refuse to start the API if token is blank AND bind address is
not `127.0.0.1`. Wizard should generate a random token by default.

---

### P3 — Promoter sweeper polls every 1 s, claim sweeper every 15 s, watch sweeper every 5 s

**Where:** `src/bot.py:312-389`.

**What:** Fine on a single laptop. Three asyncio tasks sharing the bot's event
loop with the polling Telegram client. Worth instrumenting wall-clock for each
loop iteration; an SQLite WAL checkpoint stall would block all of them.

---

### P3 — `MainWindow` reaches into `src.cost_guard._todays_cost_usd`

**Where:** `src/gui/windows/main_window.py:109`.

**What:** Importing a private (`_` prefix) function across module boundaries.
Either promote `todays_cost_usd` to public, or move the badge math into a public
helper.

---

### P3 — `src/gui/windows/main_window.py:300` references undefined `new_stack`

**Where:**
```python
def _on_crash_restart(self, service: str) -> None:
    ...
    save_state(replace(load_state(), last_stack=new_stack.name))
```

`new_stack` is unbound here — this is in the crash-restart handler, not the
new-stack wizard. Dead code path that will raise `NameError` the moment a user
clicks "Restart" on a crashed service.

**Suggested fix:** Remove that final line (it's a leftover paste from
`_open_new_stack_wizard`).

---

### P3 — `release_stale_claims` does not write any audit trail

**Where:** `src/promoter.py:30-50`.

**What:** Released claims are released silently. A re-claim that fires a second
broker order (see P0) leaves no breadcrumb tying the new claim to the released
one. An `events` row would help triage.

---

### P3 — `tests/test_api.py` has no coverage for duplicate `/result` POST

**Where:** `tests/test_api.py:97-107` covers the **claim** race but not the
**result** race. The P0 above is exactly the gap.

---

## 3. UI Review

### Theming

- Tokens are centralized in `styles.qss` and filled by `theme.py`. Light + dark
  both work after the recent fixes.
- Risk: `QSS` is global. Anything that uses `setStyleSheet(...)` locally bypasses
  the token system — `settings_view.py:60` and `telegram_wizard.py:336` both
  hardcode `color: #787b86`. These dim labels will look wrong in light theme. Two
  options: (a) ship a `[role="muted"]` selector in the QSS template and switch
  these calls to `setProperty("role", "muted")`, (b) audit and replace all
  hardcoded hex with palette lookups.
- The `${chevron_url}` token is well-handled; verify the cache busts on theme
  swap.

### Component fit (qfluentwidgets)

- `NavigationInterface` is the right primitive. The rail correctly stays at 64 px
  collapsed.
- `InfoBadge` works but the call site in `nav_rail.py:97` rewrites the **text**
  rather than attaching an InfoBadge widget. Reads as `"Live  ●3"` and is not
  screen-reader friendly. Consider `addItem(..., infoBadge=...)` if the version
  supports it.
- The qfluentwidgets `SettingCard`s in Settings → Tuning are a good fit. The
  `ExpandSettingCard` for the prompt text field is the right call.
- `SwitchButton` for booleans: good. But there's a mix of `QCheckBox` (legacy)
  and `SwitchButton` (new). Audit and unify.
- `PrimaryPushButton` should be used for the wizard's "Next" and Settings's
  "Save" — at least one of those is still a plain `QPushButton`.

### Accessibility

- **No keyboard story for the nav rail.** `NavigationInterface` items aren't
  focusable by Tab in the default config; the operator can't drive the app
  without a mouse.
- Color is the only signal for state in StatCards (`set_value(value, accent)`).
  Add a small icon or label affix for color-blind operators (✓ / ! / ×).
- Headings: every view's title is a `QLabel` with inline rich text. No semantic
  heading role. Screen readers will treat them as generic text.
- The dim-label hardcoded `#787b86` (see Theming) is below WCAG AA contrast on
  the light palette background. Verify with a contrast checker.

### Empty / Loading / Error states

- Live view: empty state is "no rows" — acceptable, but no "polling…" indicator
  during the first 2 s after launch.
- Journal: shows "No closed positions" — good.
- Cost: blank chart on a fresh install. Should show "No AI calls yet — chart
  appears after first signal."
- Prompts: live-mode fallback to demo is good. Should label the fallback
  explicitly: "Live data unavailable, showing demo render."
- The crash banner is excellent — it pushes a specific service + tail lines.
  Make sure the tail isn't ANSI-colored (some logs use color codes that render
  as garbage in a `QLabel`).

### RTL readiness

- Not a runtime requirement (operator is English-speaking) but the AI source
  data is Arabic. The Journal/Live views render the original message text in
  cells and **do not set `Qt.AlignmentFlag.AlignRight`** for the message column.
  Set `setLayoutDirection(Qt.LayoutDirection.RightToLeft)` on the message
  column's `QTableWidgetItem`s — readability for the operator who does want to
  double-check the original.

---

## 4. Per-Page Layout Review

ASCII wireframes show the **current** layout; commentary calls out the
operator-job fit and naming the qfluentwidgets primitive that would tighten each.

### 4.1 Live view

```
┌─────────────────────────────────────────────────────────────────┐
│ LIVE                                            ⓘ Oldest claim │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐         │
│  │Pending │ │ Sent   │ │Claimed │ │Watching│ │Today's │         │
│  │   3    │ │   1    │ │   1    │ │   0    │ │  trades│         │
│  └────────┘ └────────┘ └────────┘ └────────┘ └────────┘         │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Action queue table (status / type / age / source / btn)  │   │
│  │  …                                                       │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

- Stat cards are the right idea but they all use one accent. **"Oldest claim"
  isn't shown when there is no claim** — it should be a tile, not a label, that
  shows `—` until needed (currently the indicator label hides → operator's eye
  has to scan).
- Use `qfluentwidgets.CardWidget` instead of the bespoke StatCard for the
  enclosing primitive. Keep `set_value` as-is.
- Add a row of one-click actions next to each row (`Cancel`, `Promote now`,
  `Inspect`) — today the operator uses `/cancel <id>` via Telegram.

### 4.2 Journal

```
┌────────────────────────────────────────────────────────────────┐
│ JOURNAL                          [today ▼] [winners ☐] [⟳]    │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐                   │
│  │ Trades │ │ Wins   │ │  WR    │ │  PnL   │                   │
│  └────────┘ └────────┘ └────────┘ └────────┘                   │
│  Equity curve ▓▓▓▓▓▓▓▓▓▓░░░░░░░                                │
│  Closed positions table                                        │
│   ticket • side • lots • open • close • pnl • reason • src    │
└────────────────────────────────────────────────────────────────┘
```

- Equity curve currently lives in the same scroll surface as the table — when
  there are 200+ rows, the chart scrolls out of view. Pin it to the top via
  `QSplitter` or a fixed-height container.
- "reason" column is `mt5_not_found` for reconcile-driven closes; rename to
  "close source" with a tooltip explaining.

### 4.3 Rejected

```
┌────────────────────────────────────────────────────────────────┐
│ REJECTED                                       [today ▼] [⟳]  │
│  ┌────────┐ ┌────────┐ ┌────────┐                              │
│  │ Total  │ │  AI    │ │  EA    │                              │
│  │  rej   │ │ skips  │ │ rejects│                              │
│  └────────┘ └────────┘ └────────┘                              │
│  Cards: reason • original message • timestamp • [Replay]       │
└────────────────────────────────────────────────────────────────┘
```

- Use `qfluentwidgets.SimpleCardWidget` per reject. Today they're plain frames.
- "Replay" button is implied but missing — wire to the existing Replay view
  with the message pre-loaded.

### 4.4 Cost

```
┌────────────────────────────────────────────────────────────────┐
│ COST                                  Today $0.42 / Budget $5  │
│  Sparkline (7d) ▁▂▃▅▂▁▁                                       │
│  ┌─────────────┐ ┌─────────────┐                               │
│  │ Triage $    │ │ Interpret $ │                               │
│  └─────────────┘ └─────────────┘                               │
│  Recent call log (provider / model / tokens / cost / latency) │
└────────────────────────────────────────────────────────────────┘
```

- Cap auto-halt setting is buried — surface a `qfluentwidgets.SwitchButton`
  inline: "Auto-halt at 120% of budget" with the multiplier next to it.
- The chart background fix is in; verify it renders in dark mode after a theme
  swap mid-session.

### 4.5 Risk

```
┌────────────────────────────────────────────────────────────────┐
│ RISK                                                           │
│  Daily loss limit       [────────▓░░] -$ 28 / -$50            │
│  Daily trades           [─────▓░░░░░] 4 / 10                  │
│  Open positions         1                                      │
│  [Settings → edit limits]                                      │
└────────────────────────────────────────────────────────────────┘
```

- Progress bars are the right primitive. Reverse the color logic for loss limit
  — green when near zero, red when near the limit. Current implementation looks
  identical at 4/10 and 9/10.

### 4.6 Replay

```
┌────────────────────────────────────────────────────────────────┐
│ REPLAY                                                         │
│  ┌── Message ──────────────────┐  ┌── State snapshot ────────┐│
│  │ <Arabic text>               │  │ Open pos / Last closed   ││
│  └──────────────────────────────┘  │ Market / Memory          ││
│  [Run triage] [Run interpret]      └──────────────────────────┘│
│  ┌── Output ───────────────────────────────────────────────┐  │
│  │ Triage decision: keep • Interpreted action(s): [...]    │  │
│  └─────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
```

- Split layout uses `QSplitter` — good. The state editor should accept
  freeform JSON so the operator can simulate "what if position were at BE."
- "Save as fixture" button → writes a row into `fixtures/management_messages.jsonl`
  ready for `test_management_replay.py`. This shortens the prompt-iteration loop.

### 4.7 Profile

- Currently a single-form layout. Use `SettingCardGroup`s to chunk:
  `Account · API keys · Telegram session · Services`. Better visual hierarchy
  than one tall form.

### 4.8 Prompts

- The tabs along the top for SYSTEM / TRIAGE / INTERPRET / EVALUATOR are right.
- Use a `qfluentwidgets.SegmentedWidget` instead of `QTabWidget` — the prompt
  inspector is read-only, segmented matches the affordance.
- "Demo" vs "Live" toggle on the right is good. Label the fallback explicitly
  when Live degrades to Demo.

### 4.9 Settings

- Tab-based, dense. The Wizard is now reachable via the title row buttons.
- The Tuning tab benefits from the SettingCard refactor — good.
- Channels tab uses a raw `QTableWidget`. Acceptable, but consider
  `qfluentwidgets.TableWidget` for theme consistency with the rest.

### 4.10 Wizard (9 pages)

- Welcome → Identity → AI → Bot → Credentials → Phone → Code → Dialogs →
  Services → Done.
- 9 pages is **too many** for the new-stack flow. Identity + AI fits in one
  page; Phone + Code is two pages because Telegram requires the round-trip, but
  Credentials is a 1-field page that could fold into Identity.
- "Skip to channel picker" auto-advance in `_on_connected` is a UX foot-gun —
  operator who tabbed back to verify their phone is bounced forward and may
  miss the channel choice. Make it a "Continue" button on the page with the
  authorized indicator shown.

---

## 5. Top-10 ranked actions for this week

1. **P0 — Make `/actions/{id}/result` status-guarded.** Add the `AND status='claimed'`
   filter, return 409 on miss, write a test.
2. **P1 — Fix ALERT silent-swallow.** Insert as `executed` (or extend dispatcher
   to DM `pending` ALERTs). Add a smoke test that a forced AI error produces a
   Telegram DM.
3. **P1 — Fix "Oldest claim" indicator in Live view** (`MIN(claimed_at)`).
4. **P1 — Listener restart warning when channel ID changes** — display in
   Settings when services are running.
5. **P1 — Reorder `last_seen_tg_msg_id` write before `process_message`** OR add
   an `idempotency_key` to management actions.
6. **P2 — Per-stack EA magic number.** Surface in wizard.
7. **P2 — Auth refuses to start unauthenticated on non-loopback bind.**
8. **P2 — Remove the `new_stack.name` `NameError` in `_on_crash_restart`.**
9. **P3 — Add `events` row when `release_stale_claims` fires** so the duplicate-
   order risk leaves a breadcrumb.
10. **P3 — Audit hardcoded `#787b86` dim-label color** → palette token.

Everything below this is over the next month, not this week.

---

## 6. Open questions for the author

1. **Is the EA expected to own deduplication on re-claim?** If yes, `DoOpen`
   needs to check MT5 for an existing matching ticket before sending. If no, the
   P0 fix is the right place.
2. **What is the intended recovery semantics on listener crash for management
   actions?** At-most-once (lose one new message) vs at-least-once (re-fire one
   old action)? OPENs are safe; management is not.
3. **Multi-stack on one MT5 — is this a real scenario or a hypothetical?** The
   magic-number fix is cheap either way but the wizard UX depends on the
   answer.
4. **Should the Live view expose `/cancel <id>` as a row button?** Today the
   operator uses Telegram. If yes, what does "promote now" mean for a `watching`
   action (force-trigger the EA, or just flip to `sent`)?
5. **`REINFORCE` closes regardless of PnL by design** (per CLAUDE.md). Confirm
   that a Telegram-side reminder of an old REINFORCE arriving on listener
   relaunch should NOT re-close-and-reopen. (Today it would — see P1 #5.)
6. **Is ALERT supposed to be a notification, an action, or both?** Today it's
   neither. Pick one and document.
7. **Cost cap auto-halt — should it auto-resume at midnight UTC?** Today it
   stays halted until the operator hits `/resume`. Both choices are defensible;
   document the intended one in CLAUDE.md.

---

_End of review._
