//+------------------------------------------------------------------+
//|  CopyTrades.mq5 — polls FastAPI bridge for actions, executes     |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>
#include "Dashboard.mqh"
#include "LogPanel.mqh"

input string ApiBaseUrl              = "http://127.0.0.1:8765";
// Shared secret for the API auth_gate middleware. MUST match the
// EA_SHARED_TOKEN value in the project's .env. Leave blank only when the
// API is also running with EA_SHARED_TOKEN unset (dev mode); a mismatch
// results in 401 on every request and the EA effectively goes silent.
input string ApiSharedToken          = "";
input int    PollIntervalSec         = 1;
// Position sizing: balance-based. lots = (ACCOUNT_BALANCE / 100) * LotsPer100Balance.
// Default 0.01 means "0.01 lot per $100 of balance" -> $1000 balance = 0.10 lot,
// $10000 balance = 1.0 lot. Independent of SL distance, so dollar risk per trade
// scales with how wide the signal's SL is. Cap with MaxLotsPerSignal below.
input double LotsPer100Balance       = 0.01;
// MaxLotsPerSignal is a hard upper cap on the computed lot size. Default
// 100 is intentionally wide — for retail-sized accounts it never binds, so
// the formula above is the only sizing constraint. Lower it (e.g., 0.10
// during validation) if you want a hard ceiling regardless of balance.
input double MaxLotsPerSignal        = 100.0;
// Hard ceiling on per-trade dollar risk as a percentage of account balance.
// Computed from the signal's SL distance × proposed lot size × tick value.
// When the balance-based sizing produces a position whose SL hit would
// exceed this %, the position is capped down to the size that meets this
// limit. If even minLot exceeds the cap (signal's SL is so wide that even
// the smallest tradable size loses more than X% of balance), the OPEN is
// rejected outright with reason "sl_too_wide_for_max_risk_pct".
//
// Set to 0 to disable the cap entirely (LotsPer100Balance + MaxLotsPerSignal
// are then the only sizing constraints — original pre-cap behaviour).
//
// Default 2.0%: a $10,000 account risks at most $200 per trade. Two
// consecutive losers cost ~4% of balance, recoverable in a single winner.
input double MaxSlLossPercent        = 2.0;
// Score-tied sizing — when ON, the EA reads `evaluation.sizing.multiplier`
// from the action's payload (populated by the Python-side evaluator) and
// scales the baseline lot size by that multiplier. A `skip=true` from the
// evaluator (e.g. low conviction or hard veto) makes the EA post 'rejected'
// instead of opening. Default OFF — calibration of the multiplier table
// happens in Python; flip ON once the operator is satisfied with the
// evaluator's probability output.
input bool   EnableScoreTiedSizing   = false;
// Maximum wall-time the EA waits for the evaluator to write its result
// into the action's payload_json. The evaluator runs async (~3-10s);
// 8000ms covers Sonnet+thinking + provider latency. If the result isn't
// ready within this window, EA proceeds with baseline lots and logs a
// warning — degraded behavior is still better than blocking the trade.
input int    EvaluationWaitMs        = 8000;
input int    EvaluationPollMs        = 500;
input int    SlippagePoints          = 50;
input string Symbol_Override         = "XAUUSD";
// Magic number tags positions opened by this EA so reconciliation, plan
// management, and singleton-position lookups can ignore unrelated orders on
// the same account. Override per stack when running multiple CopyTrades
// stacks against one MT5 terminal (REVIEW.md P2 / Q3).
input ulong  Magic                   = 919191;
// Effective entry zone is widened by this many price units on EACH side
// of [entry_low, entry_high] before the in-zone check. Lets a signal that
// arrived a few ticks late still fill at market instead of being rejected.
// Set to 0 to require strict in-zone fills.
input double EntryPriceMargin        = 5.0;
// Chase-price: if price has already moved past the entry zone (BUY above
// entry_high, SELL below entry_low) but the last TP is still far enough
// ahead, open at current market instead of waiting for a pullback. Guards
// against missing fast breakouts while keeping R:R sane.
input bool   ChasePriceEnabled       = true;
input double ChaseMinRewardRatio     = 0.5;   // require remaining/original >= this; e.g. 0.5 = >=50% of move still ahead
// Staged-partial retry cap. CTrade.PositionClosePartial can return false
// in pre-OrderSend validation (hedging-mode quirks, transient stops-level
// violations, broker rate-limits) while reporting a stale success retcode
// from an earlier call. We verify the partial actually executed by reading
// volume after the attempt; if not, retry on the next ManagePlans tick up
// to this many times before giving up and advancing the stage anyway.
input int    PartialMaxRetries       = 10;
input bool   ShowDashboard           = true;
input int    DashboardX              = 20;    // pixels from right edge (CORNER_RIGHT_UPPER)
input int    DashboardY              = 20;    // pixels from top
// Side-panel "ACTION LOG" widget — renders the last ~20 actions with
// status glyphs. Polls GET /events/recent every LogPanelPollSec seconds.
// Hash-gated repaint: zero canvas work when the head rows haven't moved.
input bool   ShowLogPanel            = true;
input int    LogPanelX               = 0;     // 0 = auto (right of dashboard); else absolute X
input int    LogPanelPollSec         = 3;
// Market-price heartbeat: every N seconds, POST current bid/ask to the API
// so the AI prompt has a fresh quote for two-digit SL shorthand decoding
// (e.g. "ستوبك 56" -> 4856 only when gold is around 4850). Set to 0 to
// disable. Heartbeat is unconditional (runs even when kill switch is on).
input int    MarketPriceHeartbeatSec = 15;
input bool   EnableMarketSnapshot    = true;
// Break-even calculation: when the EA moves SL to "BE" (either via the
// AI's MOVE_SL_BE action OR via the automated TP1-hit stage-0 move), it
// targets a price that nets ZERO PnL after broker costs. The cost
// offset is computed from POSITION_COMMISSION, POSITION_SWAP, and the
// symbol's tick value/size. CommissionMultiplier accounts for brokers
// that charge commission per-side: 2.0 means the open-side commission
// is paid again at close. Set to 1.0 for round-turn-upfront brokers.
input double CommissionMultiplier    = 2.0;
// Gate the AI's reactive "احجز نصف أرباحك واحفظ دخولك" flow: when false,
// both DoMoveSlBe and DoClosePartial silently no-op the incoming action
// (status=executed, ea_response="noop_partial_and_be_disabled") so they
// don't pollute the rejected/failed counters or DM the operator. The
// EA-internal staged plan in
// ManagePlans (TP1/TP2 partial closes triggered by price crossings) is
// NOT affected by this — only the AI's ad-hoc reactions to channel
// messages mid-trade. Default false: most operators prefer the channel
// signal to ride to its broker-set final TP without mid-flight partials.
input bool   EnableAiPartialAndBe    = true;
// Gate the AI's REINFORCE action (close current + reopen same side with
// prior params). When false, REINFORCE actions silently no-op
// (status=executed, ea_response="noop_reinforce_disabled") so they don't
// pollute the rejected/failed counters. Useful when the channel uses
// ambiguous wording ("any reinforce opportunity I'll post") that the AI
// over-reads as an imperative. Default true preserves prior behavior.
input bool   EnableReinforce          = false;
// Final-stage exit policy for 2-TP and 3-TP plans:
//   FINAL_TRAIL  — at the stage that activates the trail (2-TP stage 0→1,
//                  3-TP stage 1→2) the broker TP is removed and the
//                  remainder rides TrailStage2Sls until SL hits. Captures
//                  extensions past the channel's final TP.
//   FINAL_KEEP_TP — the broker TP stays at the signal's final TP so the
//                  remainder closes at market when price reaches that
//                  level. TrailStage2Sls is skipped for this plan.
// SL still moves at each stage transition in both modes (anchor / TP1).
// 1-TP signals are unaffected (no plan registered; broker TP rides).
enum ENUM_FINAL_STAGE_MODE { FINAL_TRAIL, FINAL_KEEP_TP };
input ENUM_FINAL_STAGE_MODE FinalStageMode = FINAL_KEEP_TP;
// ---- Phase 4: directional-command-first flow (OPEN_INSTANT / ATTACH_SIGNAL).
// Channel posts "اشتري الذهب" / "بيع الذهب" → AI emits OPEN_INSTANT → EA
// opens market with no signal SL/TP, just an emergency SL sized so a hit
// equals InstantRiskPercent of account balance. Structured signal expected
// within InstantTimeoutMinutes; if it arrives, ATTACH_SIGNAL wires real
// SL/TPs and registers a staged plan. If the timeout fires first, the EA
// installs a fallback TP at InstantTpMultiplier × original_sl_distance and
// trails SL at InstantTrailPoints behind price (ratchet-only).
// Set EnableInstantOpen=false to disable the whole pipeline (DoOpenInstant
// rejects with "instant_open_disabled").
input bool   EnableInstantOpen        = true;
input double InstantRiskPercent       = 1.0;
input int    InstantTimeoutMinutes    = 5;
input int    InstantTrailPoints       = 300;
input double InstantTpMultiplier      = 2.0;

CTrade trade;

// ---- Dashboard state (read by g_dashboard.Update each second) ----
CDashboard g_dashboard;
// ---- LogPanel state (right-of-dashboard action stream widget) ----
CLogPanel  g_log_panel;
LogEvent   g_log_events[];        // populated by FetchRecentEvents
datetime   g_last_log_fetch = 0;  // throttle for FetchRecentEvents
datetime   g_ea_start            = 0;
datetime   g_last_api_ok         = 0;
bool       g_kill_switch_cached  = false;
int        g_stats_day           = 0;   // YYYYMMDD; resets counters on change
int        g_stats_signals       = 0;
int        g_stats_executed      = 0;
int        g_stats_rejected      = 0;
int        g_stats_chased        = 0;
double     g_equity_peak_today   = 0.0;
double     g_balance_open_today  = 0.0;
long       g_last_action_id      = 0;
string     g_last_action_type    = "";
string     g_last_action_status  = "";
datetime   g_last_action_at      = 0;
datetime   g_last_price_heartbeat = 0;  // throttle for HeartbeatMarketPrice()
datetime   g_last_snapshot       = 0;  // throttle for PostMarketSnapshot()
datetime   g_last_evaluation_fetch = 0; // throttle for FetchLatestEvaluation()

// ---- Cached signal-quality evaluation (read by BuildStats each tick) ----
// Populated by FetchLatestEvaluation from GET /actions/latest_open_evaluation.
// 404 from the API is normal (no OPEN yet, or eval not yet attached) and
// keeps g_eval_available=false so the dashboard shows the empty-state.
bool       g_eval_available    = false;
long       g_eval_action_id    = 0;
int        g_eval_score        = 0;
string     g_eval_verdict      = "";
string     g_eval_key_factor   = "";
string     g_eval_summary      = "";
string     g_eval_data_quality = "";
int        g_eval_age_sec      = 0;
datetime   g_eval_evaluated_at = 0;  // for computing eval_age_sec

// Broker compatibility check result. Populated once in OnInit by
// RunBrokerChecks (see BrokerCheck.mqh, transitively included via
// Dashboard.mqh). Read by BuildStats every tick and rendered in the
// dashboard's BROKER section so the operator sees missing requirements
// without opening the journal.
BrokerCheckResult g_broker_check;

// Staged-management plan for a multi-TP position. Strategy:
//   1 TP  → single position at full lots, TP=tp1 (no staged plan registered).
//   2 TPs → open at full lots, TP=tp2, SL=orig. On tp1 cross: close 50%,
//           move SL to entry (BE). Remainder auto-closes at tp2.
//   3 TPs → open at full lots, TP=tp3, SL=orig. On tp1 cross: close 33%,
//           move SL to entry. On tp2 cross: close another 33% of original,
//           move SL to tp1. Remainder auto-closes at tp3.
// Plans are in-memory: lost on EA restart. Positions opened pre-restart keep
// their MT5-side final TP so they still close safely, just without partials.
struct TradePlan {
   long     ticket;
   bool     isBuy;
   double   origLots;
   double   entry;
   double   slOrig;
   double   entryLow;       // signal-zone lower bound (for SignalAnchorSl)
   double   entryHigh;      // signal-zone upper bound (for SignalAnchorSl)
   double   tps[3];
   int      tpCount;
   int      stage;          // 0 initial, 1 after tp1, 2 after tp2
   int      stage_attempts; // failed PositionClosePartial attempts on the
                            // CURRENT stage. Reset on stage advance. If it
                            // hits PartialMaxRetries we give up and advance
                            // anyway. Not persisted; reset on EA restart.
};
TradePlan g_plans[];

// ---- Phase 5: pending-limit orders (broker BuyLimit/SellLimit) --------
// Each entry tracks a real broker-side pending order placed by DoOpen
// when the AI emits OPEN with `pending: true`. Lives until the limit
// fires (-> position opens, action transitions to executed) OR a
// CANCEL_PENDING server-side flips the action to rejected (then
// ManagePendingOrders calls trade.OrderDelete locally).
struct PendingOrder {
   long       action_id;        // server-side actions.id
   ulong      order_ticket;     // broker order ticket from trade.ResultOrder()
   bool       isBuy;
   double     entry;            // the limit price
   double     sl;
   double     tps[3];
   int        tpCount;
   datetime   placedAt;
   datetime   lastStatusCheck;  // throttle GET /actions/{id} polls
};

PendingOrder g_pending_orders[];

// Sequence counter for retry-queue file names. Combined with TimeCurrent()
// to disambiguate multiple EnqueueRetry calls within the same second so
// each pending retry has a unique filename in MQL5\Files.
int g_retry_counter = 0;

int OnInit() {
   trade.SetExpertMagicNumber(Magic);
   trade.SetDeviationInPoints(SlippagePoints);
   EventSetTimer(PollIntervalSec);
   LoadPersistedPlans();
   LoadPersistedNaked();
   LoadPersistedPendingOrders();
   g_ea_start = TimeCurrent();
   g_balance_open_today = AccountInfoDouble(ACCOUNT_BALANCE);
   g_equity_peak_today = AccountInfoDouble(ACCOUNT_EQUITY);
   if(ShowDashboard) g_dashboard.Create(DashboardX, DashboardY);
   if(ShowLogPanel) {
      // Default X = right edge of dashboard + 8 px gap (388 = 380 width + 8).
      // Override via LogPanelX input if the operator wants a custom layout.
      int lx = (LogPanelX > 0) ? LogPanelX : (DashboardX + 388);
      g_log_panel.Create(lx, DashboardY);
      g_log_panel.Hide();  // hidden by default; revealed via toggle button
      // Toggle button anchored just below the dashboard. Persists across
      // ticks; OnChartEvent flips g_log_panel visibility and updates label.
      CreateLogToggleButton();
   }

   // Run broker-compatibility checks once. Result is cached in
   // g_broker_check, read by BuildStats every tick, and rendered in
   // the BROKER section of the dashboard. We do NOT fail INIT on
   // FAIL severity — the operator sees the missing requirements on
   // the chart and can decide whether to detach or proceed. The
   // existing kill-switch + algo-trading guards still gate execution.
   RunBrokerChecks(Symbol_Override, MaxLotsPerSignal, g_broker_check);
   Print("CT broker: ", BrokerCheckSummary(g_broker_check));
   for(int i = 0; i < g_broker_check.count; i++) {
      Print("CT broker ",
            (g_broker_check.issues[i].severity == BC_FAIL ? "FAIL" : "WARN"),
            " ", g_broker_check.issues[i].label,
            ": ", g_broker_check.issues[i].detail);
   }
   // Single-line summary to /alerts so the bot DMs the operator on
   // every (re)attach. Compose by concatenating each issue label so
   // the message is grep-able from the bot history.
   string alertText = BrokerCheckSummary(g_broker_check);
   for(int i = 0; i < g_broker_check.count; i++) {
      alertText += " | " + g_broker_check.issues[i].label
                 + ": " + g_broker_check.issues[i].detail;
   }
   // Minimal JSON escape: only " and \ matter for our composed strings;
   // newlines/tabs aren't produced by RunBrokerChecks. Same lightweight
   // approach as the post-result error path at ~line 1555.
   StringReplace(alertText, "\\", "\\\\");
   StringReplace(alertText, "\"", "\\\"");
   string alertLevel = (g_broker_check.fails > 0) ? "warning" : "info";
   string alertJson = StringFormat(
      "{\"level\":\"%s\",\"text\":\"EA started: %s\"}",
      alertLevel, alertText);
   string aresp;
   HttpPostJson(ApiBaseUrl + "/alerts", alertJson, aresp);

   Print("CopyTrades EA started. API=", ApiBaseUrl,
         " restored_plans=", ArraySize(g_plans));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
   if(ShowDashboard) g_dashboard.Destroy();
   if(ShowLogPanel)  { g_log_panel.Destroy(); DestroyLogToggleButton(); }
}

// ---- LogPanel toggle button (anchored just below the dashboard) ----
// MT5 OBJ_BUTTON fires CHARTEVENT_OBJECT_CLICK in OnChartEvent. The
// button stays "pressed" after click, so we always reset OBJPROP_STATE
// back to false to keep it behaving like a momentary action.
#define CT_LOG_TOGGLE_NAME "CT_LogToggle"

void RefreshLogToggleLabel() {
   string label = g_log_panel.IsVisible() ? "Hide Log" : "Show Log";
   ObjectSetString(0, CT_LOG_TOGGLE_NAME, OBJPROP_TEXT, label);
   ChartRedraw(0);
}

void CreateLogToggleButton() {
   // Place it directly under the dashboard (dashboard height = 900).
   // Width 120, height 24. Cheap enough to recreate on re-attach.
   if(ObjectFind(0, CT_LOG_TOGGLE_NAME) >= 0)
      ObjectDelete(0, CT_LOG_TOGGLE_NAME);
   if(!ObjectCreate(0, CT_LOG_TOGGLE_NAME, OBJ_BUTTON, 0, 0, 0)) {
      Print("CT logtoggle: ObjectCreate failed, err=", GetLastError());
      return;
   }
   // Anchored inside the dashboard header row, to the left of the LIVE
   // pill (which sits at canvas-relative x=300..380, y=8..22).
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_ANCHOR, ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_XDISTANCE, DashboardX + 220);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_YDISTANCE, DashboardY + 6);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_XSIZE, 70);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_YSIZE, 18);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_BGCOLOR, 0x141210);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_BORDER_COLOR, 0x3D2F18);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_COLOR, 0xD4AF37);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_FONTSIZE, 8);
   ObjectSetString(0, CT_LOG_TOGGLE_NAME, OBJPROP_FONT, "Tahoma");
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_BACK, false);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_STATE, false);
   RefreshLogToggleLabel();
}

void DestroyLogToggleButton() {
   if(ObjectFind(0, CT_LOG_TOGGLE_NAME) >= 0)
      ObjectDelete(0, CT_LOG_TOGGLE_NAME);
}

void OnChartEvent(const int id, const long &lparam,
                  const double &dparam, const string &sparam) {
   if(id != CHARTEVENT_OBJECT_CLICK) return;
   if(sparam != CT_LOG_TOGGLE_NAME) return;
   // Reset visual pressed-state (button is momentary, not toggle-style).
   ObjectSetInteger(0, CT_LOG_TOGGLE_NAME, OBJPROP_STATE, false);
   if(!ShowLogPanel) return;  // input-disabled, nothing to flip
   g_log_panel.Toggle();
   RefreshLogToggleLabel();
   // If just shown, fetch+paint immediately rather than waiting for the
   // next OnTimer tick so the operator doesn't see an empty panel.
   if(g_log_panel.IsVisible()) {
      g_last_log_fetch = 0;  // force FetchRecentEvents on next call
      FetchRecentEvents();
      g_log_panel.Update(g_log_events, ArraySize(g_log_events));
   }
}

void OnTimer() {
   RolloverDayIfNeeded();
   g_kill_switch_cached = KillSwitchOn();
   if(!g_kill_switch_cached) {
      PollAndExecute();
      ManagePlans();  // also driven by OnTick; belt-and-suspenders
      TrailStage2Sls();  // post-TP2 trailing SL on 3-TP plans (Flavour A)
      ManageNakedPlans();  // Phase 4: OPEN_INSTANT timeout fallback + trail
      ManagePendingOrders();  // Phase 5: broker pending-limit fill + cancel poll
      ReconcileClosedPositions();
      DrainRetryQueue();  // resend any persisted POSTs from a prior outage
   }
   // Heartbeat market price unconditionally (even when halted) — the AI
   // still needs a fresh quote to decode shorthand SL on incoming messages.
   HeartbeatMarketPrice();
   // Push multi-timeframe OHLC snapshot for the signal-quality evaluator
   // (src/ai_evaluator.py). Throttled to ~once per minute. Best-effort —
   // failures degrade evaluator to reduced-context mode but never block.
   PostMarketSnapshot();
   if(ShowDashboard) {
      DashboardStats s;
      BuildStats(s);
      g_dashboard.Update(s);
   }
   if(ShowLogPanel && g_log_panel.IsVisible()) {
      FetchRecentEvents();   // self-throttled to LogPanelPollSec
      g_log_panel.Update(g_log_events, ArraySize(g_log_events));
   }
}

// Throttled POST of current bid/ask to /market/price so the AI prompt has
// a recent quote for two-digit SL shorthand decoding. Best-effort: failures
// are silent — a stale or missing quote degrades gracefully (the prompt
// shows "STALE" or "no recent quote" and the model treats shorthand more
// conservatively).
void HeartbeatMarketPrice() {
   if(MarketPriceHeartbeatSec <= 0) return;
   datetime now = TimeCurrent();
   if(now - g_last_price_heartbeat < MarketPriceHeartbeatSec) return;
   double bid = SymbolInfoDouble(Symbol_Override, SYMBOL_BID);
   double ask = SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
   if(bid <= 0.0 || ask <= 0.0) return;  // symbol not ready
   string body = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f}",
      Symbol_Override, bid, ask
   );
   string resp; int status;
   HttpPostJsonWithStatus(ApiBaseUrl + "/market/price", body, resp, status);
   g_last_price_heartbeat = now;
}

// Push M15/H1/H4 OHLC + ATR(14) to /market/snapshot. Consumed by the
// signal-quality evaluator (src/ai_evaluator.py) when a new OPEN action
// is inserted. Without this push the evaluator runs in reduced-context
// mode and caps scores at 70.
//
// Throttle: 60s. ATR indicator handles are created lazily on first call
// and cached in static state — IndicatorRelease isn't called in OnDeinit
// because EA reattaches re-create them.
void PostMarketSnapshot() {
   if(!EnableMarketSnapshot) return;
   datetime now = TimeCurrent();
   if(now - g_last_snapshot < 60) return;

   // ATR(14) handles for the three short timeframes (existing).
   static int hAtrM15 = INVALID_HANDLE;
   static int hAtrH1  = INVALID_HANDLE;
   static int hAtrH4  = INVALID_HANDLE;
   if(hAtrM15 == INVALID_HANDLE) hAtrM15 = iATR(Symbol_Override, PERIOD_M15, 14);
   if(hAtrH1  == INVALID_HANDLE) hAtrH1  = iATR(Symbol_Override, PERIOD_H1,  14);
   if(hAtrH4  == INVALID_HANDLE) hAtrH4  = iATR(Symbol_Override, PERIOD_H4,  14);
   // Directional-rubric extensions (added 2026-05-09): D1 ATR + 50/200 SMAs
   // + ADX on H1. The evaluator uses these for trend / volatility / range
   // exhaustion factors. Handles are static so we only create them once.
   static int hAtrD1     = INVALID_HANDLE;
   static int hSma50D1   = INVALID_HANDLE;
   static int hSma200D1  = INVALID_HANDLE;
   static int hAdxH1     = INVALID_HANDLE;
   if(hAtrD1    == INVALID_HANDLE) hAtrD1    = iATR(Symbol_Override, PERIOD_D1, 14);
   if(hSma50D1  == INVALID_HANDLE) hSma50D1  = iMA(Symbol_Override, PERIOD_D1, 50,  0, MODE_SMA, PRICE_CLOSE);
   if(hSma200D1 == INVALID_HANDLE) hSma200D1 = iMA(Symbol_Override, PERIOD_D1, 200, 0, MODE_SMA, PRICE_CLOSE);
   if(hAdxH1    == INVALID_HANDLE) hAdxH1    = iADX(Symbol_Override, PERIOD_H1, 14);
   if(hAtrM15 == INVALID_HANDLE || hAtrH1 == INVALID_HANDLE || hAtrH4 == INVALID_HANDLE) {
      Print("PostMarketSnapshot: ATR handle creation failed, last_err=",
            GetLastError(), " — will retry next cycle");
      return;
   }

   // Read most-recent ATR value. shift=0 is the current (forming) bar's
   // ATR; shift=1 would be the last closed bar. Either is fine for a
   // volatility sanity check; using the live bar gives the freshest read.
   double atrM15[]; double atrH1[]; double atrH4[];
   if(CopyBuffer(hAtrM15, 0, 0, 1, atrM15) <= 0
      || CopyBuffer(hAtrH1, 0, 0, 1, atrH1) <= 0
      || CopyBuffer(hAtrH4, 0, 0, 1, atrH4) <= 0) {
      // Indicator data not yet ready (just-after-attach race). Try again
      // next minute; the evaluator just runs reduced-context until then.
      return;
   }

   // OHLC for the live bar. iOpen/iHigh/iLow/iClose with shift=0 reflect
   // the current incomplete bar — appropriate for "current snapshot" use.
   double mO = iOpen(Symbol_Override, PERIOD_M15, 0);
   double mH = iHigh(Symbol_Override, PERIOD_M15, 0);
   double mL = iLow(Symbol_Override, PERIOD_M15, 0);
   double mC = iClose(Symbol_Override, PERIOD_M15, 0);
   double h1O = iOpen(Symbol_Override, PERIOD_H1, 0);
   double h1H = iHigh(Symbol_Override, PERIOD_H1, 0);
   double h1L = iLow(Symbol_Override, PERIOD_H1, 0);
   double h1C = iClose(Symbol_Override, PERIOD_H1, 0);
   double h4O = iOpen(Symbol_Override, PERIOD_H4, 0);
   double h4H = iHigh(Symbol_Override, PERIOD_H4, 0);
   double h4L = iLow(Symbol_Override, PERIOD_H4, 0);
   double h4C = iClose(Symbol_Override, PERIOD_H4, 0);

   // All series-data calls return 0.0 until the symbol has streamed bars.
   // If anything is zero we treat the snapshot as not-ready and skip.
   if(mO <= 0 || mC <= 0 || h1O <= 0 || h1C <= 0 || h4O <= 0 || h4C <= 0) return;

   // ---- Directional-rubric extensions ------------------------------------
   // Best-effort: any of these can fail (handle not yet ready, history not
   // loaded). The body is composed dynamically so missing pieces just get
   // omitted from the JSON — the API + evaluator handle the missing case.
   string ext = "";

   double d1O = iOpen(Symbol_Override, PERIOD_D1, 0);
   double d1H = iHigh(Symbol_Override, PERIOD_D1, 0);
   double d1L = iLow(Symbol_Override, PERIOD_D1, 0);
   double d1C = iClose(Symbol_Override, PERIOD_D1, 0);
   double atrD1Buf[]; double sma50Buf[]; double sma200Buf[];
   bool d1Ok = (d1O > 0 && d1C > 0
                && hAtrD1 != INVALID_HANDLE && hSma50D1 != INVALID_HANDLE && hSma200D1 != INVALID_HANDLE
                && CopyBuffer(hAtrD1, 0, 0, 1, atrD1Buf) > 0
                && CopyBuffer(hSma50D1, 0, 0, 1, sma50Buf) > 0
                && CopyBuffer(hSma200D1, 0, 0, 1, sma200Buf) > 0);
   if(d1Ok) {
      ext += StringFormat(
         ",\"d1\":{\"open\":%.2f,\"high\":%.2f,\"low\":%.2f,\"close\":%.2f,"
         "\"atr14\":%.4f,\"sma50\":%.2f,\"sma200\":%.2f}",
         d1O, d1H, d1L, d1C, atrD1Buf[0], sma50Buf[0], sma200Buf[0]);
   }

   // d1_prev: yesterday's closed D1 bar (shift=1).
   double pdO = iOpen(Symbol_Override, PERIOD_D1, 1);
   double pdH = iHigh(Symbol_Override, PERIOD_D1, 1);
   double pdL = iLow(Symbol_Override, PERIOD_D1, 1);
   double pdC = iClose(Symbol_Override, PERIOD_D1, 1);
   double atrD1PrevBuf[];
   bool d1PrevOk = (pdO > 0 && pdC > 0
                    && hAtrD1 != INVALID_HANDLE
                    && CopyBuffer(hAtrD1, 0, 1, 1, atrD1PrevBuf) > 0);
   if(d1PrevOk) {
      ext += StringFormat(
         ",\"d1_prev\":{\"open\":%.2f,\"high\":%.2f,\"low\":%.2f,\"close\":%.2f,\"atr14\":%.4f}",
         pdO, pdH, pdL, pdC, atrD1PrevBuf[0]);
   }

   // ADR20: rolling avg of the last 20 closed D1 ranges (shifts 1..20).
   // Skipping shift=0 because the current day's range is incomplete.
   double adr20 = 0.0; int adrCount = 0;
   for(int i = 1; i <= 20; i++) {
      double hi = iHigh(Symbol_Override, PERIOD_D1, i);
      double lo = iLow(Symbol_Override, PERIOD_D1, i);
      if(hi > 0 && lo > 0 && hi > lo) {
         adr20 += (hi - lo);
         adrCount++;
      }
   }
   if(adrCount > 0) {
      adr20 /= (double)adrCount;
      ext += StringFormat(",\"adr20\":%.2f", adr20);
   }

   // ADX(14) on H1.
   double adxBuf[];
   if(hAdxH1 != INVALID_HANDLE && CopyBuffer(hAdxH1, MAIN_LINE, 0, 1, adxBuf) > 0) {
      ext += StringFormat(",\"adx_h1\":%.2f", adxBuf[0]);
   }

   // h1_recent_closes: last 5 closed H1 bars (shifts 5..1, oldest first).
   string closesArr = "";
   int closesCount = 0;
   for(int i = 5; i >= 1; i--) {
      double c = iClose(Symbol_Override, PERIOD_H1, i);
      if(c <= 0) continue;
      if(closesCount > 0) closesArr += ",";
      closesArr += StringFormat("%.2f", c);
      closesCount++;
   }
   if(closesCount > 0) {
      ext += ",\"h1_recent_closes\":[" + closesArr + "]";
   }

   string body = StringFormat(
      "{\"symbol\":\"%s\","
      "\"m15\":{\"open\":%.2f,\"high\":%.2f,\"low\":%.2f,\"close\":%.2f,\"atr14\":%.4f},"
      "\"h1\":{\"open\":%.2f,\"high\":%.2f,\"low\":%.2f,\"close\":%.2f,\"atr14\":%.4f},"
      "\"h4\":{\"open\":%.2f,\"high\":%.2f,\"low\":%.2f,\"close\":%.2f,\"atr14\":%.4f}%s}",
      Symbol_Override,
      mO, mH, mL, mC, atrM15[0],
      h1O, h1H, h1L, h1C, atrH1[0],
      h4O, h4H, h4L, h4C, atrH4[0],
      ext
   );
   string resp; int status;
   if(HttpPostJsonWithStatus(ApiBaseUrl + "/market/snapshot", body, resp, status)) {
      g_last_snapshot = now;
   }
}

// Fetch the latest OPEN action's signal-quality evaluation from the API
// and refresh the g_eval_* cache used by BuildStats / the dashboard.
// Throttled to once per 5s — the score doesn't change between OPEN
// signals, so polling faster wastes HTTP. 404 is the no-eval-yet case
// and clears the cache; any other failure leaves the cache untouched
// so the dashboard keeps showing the last good score.
void FetchLatestEvaluation() {
   datetime now = TimeCurrent();
   if(now - g_last_evaluation_fetch < 5) {
      // Refresh age each tick from the cached evaluated_at so the "X sec
      // ago" label updates smoothly even between fetches.
      if(g_eval_available && g_eval_evaluated_at > 0)
         g_eval_age_sec = (int)(now - g_eval_evaluated_at);
      return;
   }
   g_last_evaluation_fetch = now;

   string body;
   if(!HttpGet(ApiBaseUrl + "/actions/latest_open_evaluation", body)) {
      // Network/API down — leave cache as-is, age will keep ticking.
      return;
   }
   if(StringFind(body, "\"evaluation\"") < 0) {
      // 404 / no evaluation yet. Clear the cache so the dashboard
      // shows the empty-state line.
      g_eval_available = false;
      g_eval_action_id = 0;
      g_eval_score = 0;
      g_eval_verdict = "";
      g_eval_key_factor = "";
      g_eval_summary = "";
      g_eval_data_quality = "";
      g_eval_age_sec = 0;
      g_eval_evaluated_at = 0;
      return;
   }

   long actionId = (long)StringToInteger(JsonField(body, "action_id"));
   string scoreStr = JsonField(body, "score");
   string verdict = JsonField(body, "verdict");
   string keyFactor = JsonField(body, "key_factor");
   string summary = JsonField(body, "summary");
   string dataQuality = JsonField(body, "data_quality");
   string evaluatedAt = JsonField(body, "evaluated_at");

   g_eval_available = true;
   g_eval_action_id = actionId;
   g_eval_score = (int)StringToInteger(scoreStr);
   g_eval_verdict = verdict;
   g_eval_key_factor = keyFactor;
   g_eval_summary = summary;
   g_eval_data_quality = dataQuality;

   // evaluated_at is ISO-8601 like "2026-05-07T14:30:00.123456+00:00".
   // MQL5's StringToTime parses "YYYY.MM.DD HH:MM:SS" so reformat first.
   datetime evalTs = 0;
   if(StringLen(evaluatedAt) >= 19) {
      string dt = evaluatedAt;
      StringReplace(dt, "-", ".");
      StringReplace(dt, "T", " ");
      // Drop fractional seconds + timezone suffix.
      int dotPos = StringFind(dt, ".", 11);  // skip date dots
      if(dotPos > 0) dt = StringSubstr(dt, 0, dotPos);
      int plusPos = StringFind(dt, "+");
      if(plusPos > 0) dt = StringSubstr(dt, 0, plusPos);
      int zPos = StringFind(dt, "Z");
      if(zPos > 0) dt = StringSubstr(dt, 0, zPos);
      evalTs = StringToTime(dt);
   }
   g_eval_evaluated_at = evalTs;
   g_eval_age_sec = (evalTs > 0) ? (int)(now - evalTs) : 0;
}

// Pull the recent action stream from the API and refresh g_log_events for
// the LogPanel widget. Throttled to LogPanelPollSec — the panel itself
// hash-gates repaints, so polling at 3s costs at most one HTTP round-trip
// every 3s and zero canvas work when nothing changed.
//
// Parser is a one-shot scan over the JSON body. The endpoint shape is
// {"events":[{"id":N,"type":"...","status":"...","summary":"...",
//             "ea_response":"...","created_at":"YYYY-MM-DDTHH:MM:SS..."}, ...]}.
// We rely on JsonField for per-field extraction inside each {...} block.
void FetchRecentEvents() {
   datetime now = TimeCurrent();
   if(now - g_last_log_fetch < LogPanelPollSec) return;
   g_last_log_fetch = now;

   string body;
   if(!HttpGet(ApiBaseUrl + "/events/recent?limit=20", body)) {
      // Network/API down — leave cache as-is; LogPanel keeps showing
      // the last good rows so the operator isn't blinded mid-incident.
      return;
   }

   // Reset the cache and walk each event object in the array.
   ArrayResize(g_log_events, 0);
   int pos = 0;
   while(true) {
      // Each event starts with `{"id":` — same anchor pattern used by
      // ProcessActionsJson for /actions polling.
      int objStart = StringFind(body, "{\"id\":", pos);
      if(objStart < 0) break;
      int depth = 0;
      int objEnd = -1;
      for(int i = objStart; i < StringLen(body); i++) {
         ushort c = StringGetCharacter(body, i);
         if(c == '{') depth++;
         else if(c == '}') { depth--; if(depth == 0) { objEnd = i; break; } }
      }
      if(objEnd < 0) break;
      string obj = StringSubstr(body, objStart, objEnd - objStart + 1);
      pos = objEnd + 1;

      LogEvent e;
      e.id          = StringToInteger(JsonField(obj, "id"));
      e.type        = JsonField(obj, "type");
      e.status      = JsonField(obj, "status");
      e.summary     = JsonField(obj, "summary");
      e.ea_response = JsonField(obj, "ea_response");

      // created_at is ISO-8601 like "2026-05-09T13:42:18.123+00:00".
      // Extract HH:MM (chars 11..16) for the panel's leftmost column.
      string created = JsonField(obj, "created_at");
      if(StringLen(created) >= 16)
         e.ts = StringSubstr(created, 11, 5);
      else
         e.ts = "--:--";

      int n = ArraySize(g_log_events);
      ArrayResize(g_log_events, n + 1);
      g_log_events[n] = e;

      if(n + 1 >= LP_MAX_EVENTS) break;
   }
}

// Day rollover in broker-server time: when the date changes, reset the
// per-day counters and rebase the equity peak + realized-P&L baseline.
// Broker-server date is what the user reads on the chart, not UTC.
void RolloverDayIfNeeded() {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   int today = dt.year * 10000 + dt.mon * 100 + dt.day;
   if(today == g_stats_day) return;
   g_stats_day          = today;
   g_stats_signals      = 0;
   g_stats_executed     = 0;
   g_stats_rejected     = 0;
   g_stats_chased       = 0;
   g_balance_open_today = AccountInfoDouble(ACCOUNT_BALANCE);
   g_equity_peak_today  = AccountInfoDouble(ACCOUNT_EQUITY);
}

// Walk every open position with our magic. Returns aggregated metrics
// the dashboard needs — kept in one pass to avoid repeated PositionGetX.
void AggregateOpenPositions(int &count, double &lots, double &pnl,
                            double &sl_risk_total) {
   count = 0; lots = 0.0; pnl = 0.0; sl_risk_total = 0.0;
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != Magic) continue;
      count++;
      double vol   = PositionGetDouble(POSITION_VOLUME);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      lots += vol;
      pnl  += PositionGetDouble(POSITION_PROFIT)
            + PositionGetDouble(POSITION_SWAP);
      if(sl > 0) {
         string sym = PositionGetString(POSITION_SYMBOL);
         double tickSize  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
         double tickValue = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
         if(tickSize > 0 && tickValue > 0) {
            double ticks = MathAbs(entry - sl) / tickSize;
            sl_risk_total += ticks * tickValue * vol;
         }
      }
   }
}

void BuildStats(DashboardStats &s) {
   datetime now = TimeCurrent();

   s.uptime_sec     = (int)(now - g_ea_start);
   s.api_ok         = (g_last_api_ok > 0 && (now - g_last_api_ok) < 30);
   s.api_age_sec    = g_last_api_ok > 0 ? (int)(now - g_last_api_ok) : 999;
   s.kill_switch_on = g_kill_switch_cached;
   s.algo_allowed   = (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
                   && (bool)MQLInfoInteger(MQL_TRADE_ALLOWED);

   int pos_count; double lots; double open_pnl; double sl_risk;
   AggregateOpenPositions(pos_count, lots, open_pnl, sl_risk);
   s.open_positions = pos_count;
   s.lots_deployed  = lots;
   s.lots_cap       = MaxLotsPerSignal;
   s.open_pnl       = open_pnl;

   s.last_action_id     = g_last_action_id;
   s.last_action_type   = g_last_action_type;
   s.last_action_status = g_last_action_status;
   s.last_action_age_sec = g_last_action_at > 0
                          ? (int)(now - g_last_action_at) : 0;

   s.signals_today    = g_stats_signals;
   s.executed_today   = g_stats_executed;
   s.rejected_today   = g_stats_rejected;
   s.chased_today     = g_stats_chased;

   s.balance        = AccountInfoDouble(ACCOUNT_BALANCE);
   s.equity         = AccountInfoDouble(ACCOUNT_EQUITY);
   s.free_margin    = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   s.account_ccy    = AccountInfoString(ACCOUNT_CURRENCY);

   // Static broker-check result. Populated once at OnInit; copied each
   // tick so the dashboard sees it after the first paint. Cheap struct
   // copy (BC_MAX_ISSUES=24 fixed-size array of {int, string, string}).
   s.broker = g_broker_check;

   // Latest signal-quality evaluation (cached in module statics; refreshed
   // on a 5s throttle by FetchLatestEvaluation). Populated by the Python
   // orchestrator after each OPEN action (see src/ai_evaluator.py).
   FetchLatestEvaluation();
   s.eval_available    = g_eval_available;
   s.eval_action_id    = g_eval_action_id;
   s.eval_score        = g_eval_score;
   s.eval_verdict      = g_eval_verdict;
   s.eval_key_factor   = g_eval_key_factor;
   s.eval_summary      = g_eval_summary;
   s.eval_data_quality = g_eval_data_quality;
   s.eval_age_sec      = g_eval_age_sec;

   // Today's realized = current balance - balance at start of day. Broker
   // deposits/withdrawals distort this; acceptable trade-off for MVP.
   s.realized_pnl_today = s.balance - g_balance_open_today;

   if(s.equity > g_equity_peak_today) g_equity_peak_today = s.equity;
   s.drawdown_pct = g_equity_peak_today > 0
      ? (g_equity_peak_today - s.equity) / g_equity_peak_today * 100.0 : 0.0;

   s.risk_if_all_sl_hit_pct = s.equity > 0
      ? sl_risk / s.equity * 100.0 : 0.0;

   // OPEN TRADES panel. Walk magic-owned positions; for each, prefer the
   // in-memory g_plans[] entry (has all TPs + staged progress) and fall back
   // to the MT5-side single TP for pre-restart tickets or 1-TP signals.
   // Profit math uses OrderCalcProfit() so broker-specific contract/tick
   // settings are honored (tick_value on XAUUSD varies between brokers).

   s.open_trades_count = 0;
   int slot = 0;
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != Magic) continue;
      s.open_trades_count++;
      if(slot >= DSH_MAX_TRADES) continue;
      DashboardTrade dt;
      dt.ticket     = (long)t;
      dt.isBuy      = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
      dt.currentVol = PositionGetDouble(POSITION_VOLUME);
      dt.entry      = PositionGetDouble(POSITION_PRICE_OPEN);
      dt.origLots   = dt.currentVol;
      for(int k = 0; k < 3; k++) {
         dt.tps[k] = 0.0;
         dt.profit_per_stage[k] = 0.0;
      }
      dt.profit_total = 0.0;
      dt.tpCount = 0;
      dt.stage   = 0;
      dt.hasPlan = false;
      int pi = FindPlanIdx((long)t);
      if(pi >= 0) {
         dt.hasPlan  = true;
         dt.tpCount  = g_plans[pi].tpCount;
         dt.stage    = g_plans[pi].stage;
         dt.entry    = g_plans[pi].entry;
         dt.origLots = g_plans[pi].origLots;
         for(int k = 0; k < 3; k++) dt.tps[k] = g_plans[pi].tps[k];

         // Project per-stage profit as equal shares of origLots across each
         // TP (1/tpCount per level). This is the design intent of the
         // staged plan; we show it as a planning figure even when the
         // broker's minLot/lotStep constraints would skip a real partial
         // (e.g. 0.01 lot on 3 TPs — the actual EA closes full size at
         // the final TP, but the user still wants to see what each level
         // is worth at its target).
         ENUM_ORDER_TYPE otype = dt.isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
         double fullPnl[3];
         for(int k = 0; k < 3; k++) fullPnl[k] = 0.0;
         for(int k = 0; k < dt.tpCount; k++) {
            double pnl = 0.0;
            if(!OrderCalcProfit(otype, Symbol_Override, dt.origLots,
                                dt.entry, dt.tps[k], pnl)) pnl = 0.0;
            fullPnl[k] = pnl;
         }
         double share = 1.0 / (double)dt.tpCount;
         for(int k = 0; k < dt.tpCount; k++) {
            dt.profit_per_stage[k] = fullPnl[k] * share;
            dt.profit_total       += dt.profit_per_stage[k];
         }
      } else {
         double tp = PositionGetDouble(POSITION_TP);
         if(tp > 0) { dt.tps[0] = tp; dt.tpCount = 1; }
      }
      s.open_trades[slot] = dt;
      slot++;
   }
}

void OnTick() {
   // Price-driven: reacts faster than the 1s timer to TP crossings.
   ManagePlans();
}

// File-scope cache for the kill-switch state. The previous implementation
// returned `false` on HTTP failure, which meant "off" — i.e. trades fire
// when the API is unreachable, even when the operator had halted the
// system. Now we cache the last-known state and default to `true` (halted)
// until the first successful read so the failure mode is fail-closed.
static bool g_last_kill_switch = false;
static bool g_kill_switch_known = false;

bool KillSwitchOn() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/settings/kill_switch", body)) {
      // First-failure: assume halted (safer default than firing trades
      // blindly during an outage).
      // Subsequent failures: trust the last successfully-read value.
      return g_kill_switch_known ? g_last_kill_switch : true;
   }
   g_last_kill_switch = (StringFind(body, "\"value\":\"on\"") >= 0);
   g_kill_switch_known = true;
   return g_last_kill_switch;
}

void PollAndExecute() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=sent", body)) return;
   g_last_api_ok = TimeCurrent();
   // Minimal JSON parse: find action objects
   ProcessActionsJson(body);
}

// ---- HTTP helpers ----

// Build the X-EA-Token header line for the API auth_gate middleware.
// Empty when the input is blank (dev mode / matching unconfigured server).
string AuthHeader() {
   return StringLen(ApiSharedToken) > 0
      ? "X-EA-Token: " + ApiSharedToken + "\r\n"
      : "";
}

bool HttpGet(string url, string &outBody) {
   char post[]; char result[]; string headers;
   int res = WebRequest("GET", url, AuthHeader(), "", 5000, post, 0, result, headers);
   if(res == -1) {
      Print("WebRequest GET error ", GetLastError(), " url=", url);
      return false;
   }
   // Force UTF-8 decoding — the API serializes Arabic summaries as raw
   // UTF-8 (FastAPI/Starlette uses ensure_ascii=False). Without CP_UTF8
   // here the bytes get reinterpreted in the system codepage and the
   // LogPanel shows mojibake.
   outBody = CharArrayToString(result, 0, -1, CP_UTF8);
   return true;
}

bool HttpPostJson(string url, string jsonBody, string &outBody) {
   int status;
   return HttpPostJsonWithStatus(url, jsonBody, outBody, status);
}

bool HttpPostJsonWithStatus(string url, string jsonBody, string &outBody, int &outStatus) {
   char post[]; char result[];
   string reqHeaders = "Content-Type: application/json; charset=utf-8\r\n" + AuthHeader();
   string respHeaders;
   // Encode outgoing body as UTF-8 so any Arabic in alert text / payloads
   // round-trips correctly. Default StringToCharArray uses CP_ACP and
   // would mangle multibyte chars before they ever reach the API.
   int blen = StringToCharArray(jsonBody, post, 0, -1, CP_UTF8);
   if(blen > 0) ArrayResize(post, blen - 1);  // drop trailing NUL
   int res = WebRequest("POST", url, reqHeaders, 10000, post, result, respHeaders);
   if(res == -1) {
      Print("WebRequest POST error ", GetLastError(), " url=", url);
      outStatus = -1;
      return false;
   }
   outStatus = res;
   outBody = CharArrayToString(result, 0, -1, CP_UTF8);
   if(res >= 400) {
      Print("HTTP ", res, " on ", url, " body=", outBody);
      return false;
   }
   return true;
}

// ---- Lightweight JSON helpers (copytrades only emits the fields below) ----
string JsonField(string s, string key) {
   string pat = "\"" + key + "\":";
   int p = StringFind(s, pat);
   if(p < 0) return "";
   p += StringLen(pat);
   while(p < StringLen(s) && (StringGetCharacter(s, p) == ' ')) p++;
   if(p >= StringLen(s)) return "";
   ushort c = StringGetCharacter(s, p);
   if(c == '"') {
      int end = StringFind(s, "\"", p + 1);
      return StringSubstr(s, p + 1, end - p - 1);
   }
   // Arrays/objects: return the full [...] or {...} span so callers like
   // ParseTps get every element, not just the first one before a comma.
   if(c == '[' || c == '{') {
      ushort open = c;
      ushort close = (c == '[') ? (ushort)']' : (ushort)'}';
      int depth = 0;
      int end = p;
      while(end < StringLen(s)) {
         ushort cc = StringGetCharacter(s, end);
         if(cc == open) depth++;
         else if(cc == close) { depth--; if(depth == 0) { end++; break; } }
         end++;
      }
      return StringSubstr(s, p, end - p);
   }
   int end = p;
   while(end < StringLen(s)) {
      ushort cc = StringGetCharacter(s, end);
      if(cc == ',' || cc == '}' || cc == ']') break;
      end++;
   }
   return StringSubstr(s, p, end - p);
}

// ---- Action processing ----
void ProcessActionsJson(string body) {
   // body looks like: {"actions":[ {...}, {...} ]}
   int pos = 0;
   while(true) {
      int objStart = StringFind(body, "{\"id\":", pos);
      if(objStart < 0) break;
      int depth = 0;
      int objEnd = -1;
      for(int i = objStart; i < StringLen(body); i++) {
         ushort c = StringGetCharacter(body, i);
         if(c == '{') depth++;
         else if(c == '}') { depth--; if(depth == 0) { objEnd = i; break; } }
      }
      if(objEnd < 0) break;
      string obj = StringSubstr(body, objStart, objEnd - objStart + 1);
      pos = objEnd + 1;
      ExecuteOne(obj);
   }
}

void ExecuteOne(string obj) {
   long id = StringToInteger(JsonField(obj, "id"));
   string atype = JsonField(obj, "action_type");
   string payload = ExtractPayload(obj);
   if(id <= 0 || atype == "") return;

   // Two-phase: claim atomically before placing any orders. If another tick
   // (or another EA instance) already claimed this action, skip silently.
   if(!ClaimAction(id)) return;

   // We won the claim — this is a signal we're responsible for. Count it
   // here (not in ProcessActionsJson) so duplicates from our own polling
   // don't inflate the stat.
   if(atype == "OPEN") g_stats_signals++;
   g_last_action_id = id;
   g_last_action_type = atype;
   g_last_action_status = "claimed";
   g_last_action_at = TimeCurrent();

   // Single-position invariant: at most one open trade at a time. New OPEN
   // signals while a position is already open are rejected here.
   if(atype == "OPEN" && CountOurOpenPositions() >= 1) {
      PostResult(id, "rejected", 0, "already_open");
      return;
   }

   if(atype == "OPEN")              DoOpen(id, payload);
   else if(atype == "MODIFY")       DoModify(id, payload);
   else if(atype == "CLOSE")        DoClose(id, payload);
   else if(atype == "CLOSE_ALL")    DoCloseAll(id, payload);
   else if(atype == "MOVE_SL_BE")   DoMoveSlBe(id, payload);
   else if(atype == "MOVE_SL")      DoMoveSl(id, payload);
   else if(atype == "CLOSE_PARTIAL") DoClosePartial(id, payload);
   else if(atype == "CLOSE_FULL")   DoCloseFull(id, payload);
   else if(atype == "REOPEN_LAST")  DoReopenLast(id, payload);
   else if(atype == "REINFORCE")    DoReinforce(id, payload);
   else if(atype == "TIGHTEN_SL")   DoTightenSl(id, payload);
   else if(atype == "MODIFY_TPS")   DoModifyTps(id, payload);
   else if(atype == "OPEN_INSTANT") DoOpenInstant(id, payload);
   else if(atype == "ATTACH_SIGNAL") DoAttachSignal(id, payload);
   else PostResult(id, "rejected", 0, "unknown_action_type:" + atype);
}

// Atomically transition action from 'sent' to 'claimed'. Returns true if we won.
bool ClaimAction(long id) {
   string url = ApiBaseUrl + "/actions/" + IntegerToString(id) + "/claim";
   string resp; int status;
   bool ok = HttpPostJsonWithStatus(url, "{}", resp, status);
   if(!ok && status == 409) return false;     // already claimed — skip
   if(!ok) { Print("Claim failed for action ", id, " status=", status); return false; }
   return true;
}

string ExtractPayload(string obj) {
   int p = StringFind(obj, "\"payload\":");
   if(p < 0) return "";
   p += StringLen("\"payload\":");
   int depth = 0;
   int start = -1, end = -1;
   for(int i = p; i < StringLen(obj); i++) {
      ushort c = StringGetCharacter(obj, i);
      if(c == '{') { if(depth == 0) start = i; depth++; }
      else if(c == '}') { depth--; if(depth == 0) { end = i; break; } }
   }
   if(start < 0 || end < 0) return "";
   return StringSubstr(obj, start, end - start + 1);
}

// Balance-based position sizing with optional risk-percentage cap.
//
//   lots = (ACCOUNT_BALANCE / 100) * LotsPer100Balance
//
// Independent of SL distance — so the dollar risk per trade scales with
// the signal's SL placement. The MaxSlLossPercent input adds a hard
// ceiling: if the SL hit on the proposed lot size would lose more than
// X% of balance, the lot size is capped to whatever keeps the loss at X%.
// If even minLot exceeds the cap (signal's SL is too wide for the
// account), this function returns 0.0 and DoOpen rejects the action.
//
// MaxLotsPerSignal is still applied as the absolute upper bound after
// the risk cap.
double LotsFromRisk(double slPrice, double entryPrice) {
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0) lotStep = 0.01;
   if(minLot <= 0) minLot = lotStep;

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance <= 0 || LotsPer100Balance <= 0) return minLot;

   // Step 1: balance-based proposed size.
   double lots = (balance / 100.0) * LotsPer100Balance;
   lots = MathFloor(lots / lotStep) * lotStep;

   // Step 2: per-trade risk-percentage cap (when enabled, slPrice valid,
   // and broker tick info available). Computes dollars-at-risk if SL is
   // hit at the proposed size; if it exceeds balance * MaxSlLossPercent / 100,
   // shrink the size to the largest that fits the cap.
   string riskCapMsg = "";
   if(MaxSlLossPercent > 0 && slPrice > 0 && entryPrice > 0) {
      double tickSize = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_SIZE);
      double tickValue = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_VALUE);
      if(tickSize > 0 && tickValue > 0) {
         double slDistance = MathAbs(entryPrice - slPrice);
         double dollarsPerLot = (slDistance / tickSize) * tickValue;
         double maxLossDollars = balance * MaxSlLossPercent / 100.0;
         double riskAtLots = dollarsPerLot * lots;
         if(riskAtLots > maxLossDollars && dollarsPerLot > 0) {
            double cappedLots = maxLossDollars / dollarsPerLot;
            cappedLots = MathFloor(cappedLots / lotStep) * lotStep;
            if(cappedLots < minLot) {
               // Even minLot would lose more than the configured cap.
               // Reject the trade — return 0.0 signals DoOpen to abort.
               Print("CT LotsFromRisk REJECT: even minLot=", minLot,
                     " loses $", DoubleToString(dollarsPerLot * minLot, 2),
                     " > max $", DoubleToString(maxLossDollars, 2),
                     " (", MaxSlLossPercent, "% of balance ", balance,
                     "). slDistance=", slDistance);
               return 0.0;
            }
            riskCapMsg = StringFormat(
               " [risk-capped: orig %.2f lots @ $%.2f loss > $%.2f max -> %.2f lots @ $%.2f loss]",
               lots, riskAtLots, maxLossDollars, cappedLots, dollarsPerLot * cappedLots);
            lots = cappedLots;
         }
      }
   }

   // Step 3: existing absolute caps + min-lot floor.
   if(lots > MaxLotsPerSignal) lots = MaxLotsPerSignal;
   if(lots < minLot) lots = minLot;
   double result = NormalizeDouble(lots, 2);
   Print("CT LotsFromRisk(balance): balance=", balance,
         " ratio=", LotsPer100Balance, " per $100 -> lots=", result,
         " (cap=", MaxLotsPerSignal, " step=", lotStep, " min=", minLot, ")",
         riskCapMsg);
   return result;
}

// ---- Score-tied sizing -----------------------------------------------
//
// The Python-side evaluator writes a `sizing` block into the action's
// `payload_json["evaluation"]` after the async LLM synthesizer returns.
// Shape:
//     "sizing": {
//        "multiplier": 1.4,
//        "skip": false,
//        "p_profit_used": 0.62,
//        "horizon_minutes": 240,
//        "reason": "p_profit=0.620 in band [0.60,0.65) -> 1.4x"
//     }
//
// EA reads this and either skips the trade (skip=true) or multiplies
// baseline lots by `multiplier`. Honors MaxLotsPerSignal as the hard
// cap regardless of multiplier.

struct EvalSizing {
   bool   ready;        // false = evaluator hadn't written yet
   bool   skip;
   double multiplier;
   double p_profit;
   string reason;
};

void InitEvalSizing(EvalSizing &s) {
   s.ready = false;
   s.skip = false;
   s.multiplier = 1.0;
   s.p_profit = 0.0;
   s.reason = "";
}

// Fetch sizing block from /actions/{id}; returns ready=false when the
// evaluation key is missing. Tolerant of HTTP errors — callers fall
// back to baseline lots on any failure path.
EvalSizing FetchEvalSizing(long actionId) {
   EvalSizing s; InitEvalSizing(s);
   string url = ApiBaseUrl + "/actions/" + IntegerToString(actionId);
   string body;
   if(!HttpGet(url, body) || body == "") return s;
   string payloadJson = JsonField(body, "payload");
   if(payloadJson == "") return s;
   string evalJson = JsonField(payloadJson, "evaluation");
   if(evalJson == "") return s;
   string sizingJson = JsonField(evalJson, "sizing");
   if(sizingJson == "") return s;
   s.ready = true;
   string multStr = JsonField(sizingJson, "multiplier");
   if(multStr != "" && multStr != "null") s.multiplier = StringToDouble(multStr);
   string skipStr = JsonField(sizingJson, "skip");
   s.skip = (skipStr == "true");
   string ppStr = JsonField(sizingJson, "p_profit_used");
   if(ppStr != "" && ppStr != "null") s.p_profit = StringToDouble(ppStr);
   s.reason = JsonField(sizingJson, "reason");
   return s;
}

// Poll the action endpoint up to EvaluationWaitMs total. Returns the
// last attempt's EvalSizing — `ready=false` when nothing arrived in
// time, which the caller treats as "use baseline".
EvalSizing WaitForEvalSizing(long actionId) {
   ulong start = GetTickCount();
   ulong deadline = start + (ulong)EvaluationWaitMs;
   EvalSizing s = FetchEvalSizing(actionId);
   while(!s.ready && GetTickCount() < deadline) {
      Sleep(EvaluationPollMs);
      s = FetchEvalSizing(actionId);
   }
   ulong elapsed = GetTickCount() - start;
   if(s.ready) {
      Print("CT eval_sizing: action ", actionId,
            " ready in ", elapsed, "ms — multiplier=", s.multiplier,
            " skip=", (s.skip ? "true" : "false"),
            " p_profit=", s.p_profit, " (", s.reason, ")");
   } else {
      Print("CT eval_sizing: action ", actionId,
            " NOT READY after ", elapsed, "ms — using baseline lots");
   }
   return s;
}

// Apply the score-tied multiplier to baseline lots. Returns the
// adjusted lot size, or 0.0 with `skipReason` set when the evaluator
// flagged skip. The caller checks `skipReason != ""` to know whether
// to PostResult 'rejected' instead of OrderSend.
double ApplyEvalSizing(long actionId, double baselineLots, string &skipReason) {
   skipReason = "";
   if(!EnableScoreTiedSizing) return baselineLots;
   EvalSizing s = WaitForEvalSizing(actionId);
   if(!s.ready) return baselineLots;
   if(s.skip) {
      skipReason = "score_tied_skip:" + s.reason;
      return 0.0;
   }
   double adjusted = baselineLots * s.multiplier;
   if(adjusted > MaxLotsPerSignal) adjusted = MaxLotsPerSignal;
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0) lotStep = 0.01;
   if(minLot <= 0) minLot = lotStep;
   adjusted = MathFloor(adjusted / lotStep) * lotStep;
   if(adjusted < minLot) adjusted = minLot;
   adjusted = NormalizeDouble(adjusted, 2);
   Print("CT eval_sizing: action ", actionId,
         " baseline=", baselineLots, " * multiplier=", s.multiplier,
         " -> adjusted=", adjusted, " (cap=", MaxLotsPerSignal, ")");
   return adjusted;
}


void DoOpen(long id, string payload) {
   string side = JsonField(payload, "side");
   double entryLow = StringToDouble(JsonField(payload, "entry_low"));
   double entryHigh = StringToDouble(JsonField(payload, "entry_high"));
   double sl = StringToDouble(JsonField(payload, "sl"));
   string tpsStr = JsonField(payload, "tps");
   double tps[];
   ParseTps(tpsStr, tps);
   int tpCount = ArraySize(tps);
   if(tpCount == 0) { PostResult(id, "failed", 0, "no_tps"); return; }
   if(tpCount > 3) tpCount = 3;  // strategy handles at most 3 stages

   // Phase 5: pending-limit flow. Channel-style "buy limit X SL Y Tp Z"
   // signals carry `pending: true` in the payload. EA places a real
   // broker-side BuyLimit / SellLimit at the entry-zone midpoint (or
   // single price for SMC-style single-line entries) and tracks it in
   // g_pending_orders[]. ManagePendingOrders() (in OnTimer) detects
   // fills and server-side cancellations.
   string pendingField = JsonField(payload, "pending");
   bool isPending = (pendingField == "true");
   if(isPending) {
      // pending_type chooses Limit vs Stop semantics. Default "limit"
      // when the field is missing (back-compat with profiles emitted
      // before pending_type was plumbed). "stop" support is FALLBACK
      // ONLY for now — we log and place a Limit so breakout-style
      // signals don't silently fail. Real BuyStop/SellStop plumbing
      // lands when a channel actually needs it.
      string pendingTypeField = JsonField(payload, "pending_type");
      if(pendingTypeField == "" || pendingTypeField == "null") {
         pendingTypeField = "limit";
      }
      if(pendingTypeField == "stop") {
         Print("CT OPEN id=", id, " pending_type=stop is FALLBACK-ONLY; "
               "placing as LIMIT (breakout plumbing not implemented).");
      }
      bool isBuyP = (side == "BUY");
      // Midpoint formula collapses to entry_low when low == high (the
      // single-price-entry convention used by SMC-style channels).
      double entryLimit = (entryLow + entryHigh) / 2.0;
      double lotsP = LotsFromRisk(sl, entryLimit);
      if(lotsP <= 0.0) {
         PostResult(id, "rejected", 0, "sl_too_wide_for_max_risk_pct");
         g_stats_rejected++;
         return;
      }
      string sizingSkipP = "";
      lotsP = ApplyEvalSizing(id, lotsP, sizingSkipP);
      if(sizingSkipP != "") {
         PostResult(id, "rejected", 0, sizingSkipP);
         g_stats_rejected++;
         return;
      }
      double tpFinalP = tps[tpCount - 1];
      bool okP = isBuyP
         ? trade.BuyLimit(lotsP, entryLimit, Symbol_Override, sl, tpFinalP,
                          ORDER_TIME_GTC, 0, "ct-pending")
         : trade.SellLimit(lotsP, entryLimit, Symbol_Override, sl, tpFinalP,
                           ORDER_TIME_GTC, 0, "ct-pending");
      if(!okP) {
         PostResult(id, "failed", 0,
            "pending_place_failed:" + IntegerToString(trade.ResultRetcode()));
         g_stats_rejected++;
         return;
      }
      ulong order_ticket = trade.ResultOrder();
      if(order_ticket == 0) {
         PostResult(id, "failed", 0, "pending_no_order_ticket");
         g_stats_rejected++;
         return;
      }
      PendingOrder po;
      po.action_id = id;
      po.order_ticket = order_ticket;
      po.isBuy = isBuyP;
      po.entry = entryLimit;
      po.sl = sl;
      for(int kp = 0; kp < 3; kp++) po.tps[kp] = 0.0;
      for(int kp = 0; kp < tpCount; kp++) po.tps[kp] = tps[kp];
      po.tpCount = tpCount;
      po.placedAt = TimeCurrent();
      po.lastStatusCheck = TimeCurrent();
      int nP = ArraySize(g_pending_orders);
      ArrayResize(g_pending_orders, nP + 1);
      g_pending_orders[nP] = po;
      PersistPendingOrder(po);

      // POST status='watching' — the action sits in this state until
      // ManagePendingOrders detects a fill (-> executed) or the server
      // CANCEL_PENDING handler flips us to rejected.
      string watchBody = StringFormat(
         "{\"status\":\"watching\",\"error\":\"pending_order_ticket=%I64u\"}",
         order_ticket);
      string watchResp;
      HttpPostJson(ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result",
                   watchBody, watchResp);
      Print("CT OPEN id=", id, " pending limit placed ticket=", order_ticket,
            " entry=", entryLimit, " sl=", sl, " tp=", tpFinalP);
      return;
   }

   // Normalise TP ordering by DISTANCE FROM ENTRY so tps[0] = nearest target
   // (first partial-close trigger) and tps[tpCount-1] = farthest (final TP
   // auto-close on MT5). See SortTpsByDistance.
   SortTpsByDistance(tps, tpCount, side == "BUY");
   Print("CT tps normalised id=", id, " side=", side,
         " tp1=", (tpCount>=1?DoubleToString(tps[0],2):"-"),
         " tp2=", (tpCount>=2?DoubleToString(tps[1],2):"-"),
         " tp3=", (tpCount>=3?DoubleToString(tps[2],2):"-"),
         " final=", DoubleToString(tps[tpCount-1],2));

   double entry = (entryLow + entryHigh) / 2.0;
   double price = SymbolInfoDouble(Symbol_Override, side == "BUY" ? SYMBOL_ASK : SYMBOL_BID);
   // Effective entry zone widens by EntryPriceMargin on each side. A signal
   // that arrived a few ticks late, or a tight broker spread that puts ASK
   // outside the strict zone by a small amount, still fills at market.
   double effectiveLow = entryLow - EntryPriceMargin;
   double effectiveHigh = entryHigh + EntryPriceMargin;
   bool inZone = (price >= effectiveLow && price <= effectiveHigh);
   bool useMarket = inZone;
   // When the price is in the EXTENDED zone (i.e., outside the strict
   // [entry_low, entry_high] but inside the margin band), open at the
   // current market price rather than the midpoint — same convention as
   // the chase path uses.
   if(useMarket) entry = price;

   bool isBuy = (side == "BUY");

   // Chase-price: price blew past the entry zone (BUY above entry_high,
   // SELL below entry_low) before we could claim the signal. If the last
   // TP is still far enough ahead, take the trade at current market rather
   // than waiting for a pullback that may never come. Skipped when price
   // hasn't reached the zone yet (that's a legitimate pending setup) or
   // when the last TP has already been taken out (no edge left).
   if(!useMarket && ChasePriceEnabled) {
      bool pastZone = isBuy ? (price > entryHigh) : (price < entryLow);
      double tpLast = tps[tpCount - 1];
      double origReward = MathAbs(tpLast - entry);   // entry here is midpoint
      double remaining = isBuy ? (tpLast - price) : (price - tpLast);
      bool slStillBehind = isBuy ? (price > sl) : (price < sl);
      if(pastZone && slStillBehind && origReward > 0 && remaining > 0
         && remaining / origReward >= ChaseMinRewardRatio) {
         Print("CT chase: price=", price, " past zone [", entryLow, ",", entryHigh,
               "] but remaining/original=", remaining / origReward,
               " >= ", ChaseMinRewardRatio, "; opening at market");
         useMarket = true;
         entry = price;
         g_stats_chased++;
      }
   }

   // Out of effective zone, chase didn't fire → reject. No pending-limit
   // fallback: the channel's signal has aged past the margin; firing later
   // would risk filling at a price the analyst never intended.
   if(!useMarket) {
      PostResult(id, "rejected", 0, StringFormat(
         "price_outside_margin: price=%.2f zone=[%.2f,%.2f] margin=%.2f",
         price, entryLow, entryHigh, EntryPriceMargin));
      g_stats_rejected++;
      return;
   }

   // Single full-lots position. Final TP set on MT5 so the last leg auto-closes
   // without EA involvement; intermediate TPs are handled by ManagePlans().
   double lotsTotal = LotsFromRisk(sl, entry);
   if(lotsTotal <= 0.0) {
      // Signal's SL is too wide for the configured per-trade risk cap
      // (MaxSlLossPercent). LotsFromRisk already logged the details.
      PostResult(id, "rejected", 0, "sl_too_wide_for_max_risk_pct");
      g_stats_rejected++;
      return;
   }
   string sizingSkipM = "";
   lotsTotal = ApplyEvalSizing(id, lotsTotal, sizingSkipM);
   if(sizingSkipM != "") {
      PostResult(id, "rejected", 0, sizingSkipM);
      g_stats_rejected++;
      return;
   }
   double tpFinal = tps[tpCount - 1];

   bool ok = isBuy
      ? trade.Buy(lotsTotal, Symbol_Override, 0, sl, tpFinal, "copytrades")
      : trade.Sell(lotsTotal, Symbol_Override, 0, sl, tpFinal, "copytrades");
   if(!ok) {
      PostResult(id, "failed", 0,
                 "trade.send failed: " + IntegerToString(trade.ResultRetcode()));
      return;
   }
   long ticket = (long)trade.ResultOrder() != 0
                 ? (long)trade.ResultOrder()
                 : (long)trade.ResultDeal();
   if(ticket <= 0) { PostResult(id, "failed", 0, "no_ticket_returned"); return; }

   // Suppress the reconciler's authoritative pass on this ticket for a
   // brief grace window. Without this, ReconcileClosedPositions can run
   // a few ms after the broker fill and PositionSelectByTicket returns
   // false (cache lag) → false-closes the just-opened position. See
   // RememberRecentOpen comment for the 2026-05-07 13:03 incident.
   RememberRecentOpen(ticket);

   // All fills here are market orders (out-of-margin signals rejected
   // earlier). Multi-TP signals get a staged plan for tp1/tp2 partials.
   if(tpCount >= 2) {
      RegisterPlan(ticket, isBuy, lotsTotal, entry, sl, entryLow, entryHigh,
                   tps, tpCount);
   }

   string legJson = StringFormat(
      "{\"mt5_ticket\":%I64d,\"snapshot\":{\"symbol\":\"%s\",\"side\":\"%s\","
      "\"volume\":%.2f,\"entry_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f}}",
      ticket, Symbol_Override, side, lotsTotal, entry, sl, tpFinal
   );
   string body = "{\"status\":\"executed\",\"legs\":[" + legJson + "]}";
   string resp; int status;
   string resultUrl = ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result";
   bool postOk = HttpPostJsonWithStatus(resultUrl, body, resp, status);
   if(!postOk) {
      // Order is live at the broker but the API didn't get the result.
      // Don't drop it — that strands the trade with no DB row and the
      // sweeper will eventually re-queue the action, which would cause
      // a second OPEN attempt. Persist for retry instead.
      Print("Result POST failed for action ", id, " status=", status,
            " — queued for retry");
      EnqueueRetry(resultUrl, body);
   }
   g_stats_executed++;
   g_last_action_status = "executed";
   g_last_action_at = TimeCurrent();
}

void RegisterPlan(long ticket, bool isBuy, double origLots, double entry,
                  double sl, double entryLow, double entryHigh,
                  double &tps[], int tpCount) {
   TradePlan p;
   p.ticket = ticket;
   p.isBuy = isBuy;
   p.origLots = origLots;
   p.entry = entry;
   p.slOrig = sl;
   p.entryLow = entryLow;
   p.entryHigh = entryHigh;
   p.tpCount = tpCount;
   p.stage = 0;
   p.stage_attempts = 0;
   for(int i = 0; i < 3; i++) p.tps[i] = (i < tpCount ? tps[i] : 0.0);

   // Dedupe by ticket: two plans for one ticket make ManagePlans fire each
   // stage twice in a single iteration, closing 2× the intended portion.
   // Sources of duplicates we've guarded against: a recompile-time
   // LoadPersistedPlans that matches a ticket the broker later recycled, or
   // any future call path that double-registers.
   int existing = FindPlanIdx(ticket);
   if(existing >= 0) {
      Print("CT plan REPLACED ticket=", ticket,
            " (duplicate register — prior stage=", g_plans[existing].stage,
            " prior origLots=", g_plans[existing].origLots, ")");
      g_plans[existing] = p;
   } else {
      int n = ArraySize(g_plans);
      ArrayResize(g_plans, n + 1);
      g_plans[n] = p;
   }
   PersistPlan(p);
}

void RemovePlan(int idx) {
   long ticket = g_plans[idx].ticket;
   int n = ArraySize(g_plans);
   for(int j = idx; j < n - 1; j++) g_plans[j] = g_plans[j + 1];
   ArrayResize(g_plans, n - 1);
   ErasePersistedPlan(ticket);
}

bool RemovePlanByTicket(long ticket) {
   for(int i = 0; i < ArraySize(g_plans); i++) {
      if(g_plans[i].ticket == ticket) { RemovePlan(i); return true; }
   }
   return false;
}

int FindPlanIdx(long ticket) {
   for(int i = 0; i < ArraySize(g_plans); i++)
      if(g_plans[i].ticket == ticket) return i;
   return -1;
}

// ---- EA-restart persistence (GlobalVariables are doubles; bool/int encoded) ----
string PlanKey(long ticket, string field) {
   return "ct_plan_" + IntegerToString(ticket) + "_" + field;
}

void PersistPlan(const TradePlan &p) {
   GlobalVariableSet(PlanKey(p.ticket, "stage"),    (double)p.stage);
   GlobalVariableSet(PlanKey(p.ticket, "isBuy"),    p.isBuy ? 1.0 : 0.0);
   GlobalVariableSet(PlanKey(p.ticket, "origLots"), p.origLots);
   GlobalVariableSet(PlanKey(p.ticket, "entry"),    p.entry);
   GlobalVariableSet(PlanKey(p.ticket, "slOrig"),   p.slOrig);
   GlobalVariableSet(PlanKey(p.ticket, "eL"),       p.entryLow);
   GlobalVariableSet(PlanKey(p.ticket, "eH"),       p.entryHigh);
   GlobalVariableSet(PlanKey(p.ticket, "tpCount"),  (double)p.tpCount);
   GlobalVariableSet(PlanKey(p.ticket, "tp1"),      p.tps[0]);
   GlobalVariableSet(PlanKey(p.ticket, "tp2"),      p.tps[1]);
   GlobalVariableSet(PlanKey(p.ticket, "tp3"),      p.tps[2]);
}

void ErasePersistedPlan(long ticket) {
   string fields[] = {"stage","isBuy","origLots","entry","slOrig","eL","eH",
                      "tpCount","tp1","tp2","tp3"};
   for(int i = 0; i < ArraySize(fields); i++)
      GlobalVariableDel(PlanKey(ticket, fields[i]));
}

void LoadPersistedPlans() {
   // Scan all GlobalVariables for our ct_plan_<ticket>_tpCount anchor.
   int total = GlobalVariablesTotal();
   for(int i = 0; i < total; i++) {
      string name = GlobalVariableName(i);
      if(StringFind(name, "ct_plan_") != 0) continue;
      if(StringFind(name, "_tpCount") < 0) continue;
      string mid = StringSubstr(name, 8);             // drop "ct_plan_"
      int us = StringFind(mid, "_tpCount");
      if(us < 0) continue;
      long ticket = StringToInteger(StringSubstr(mid, 0, us));
      if(ticket <= 0) continue;

      // If the position is already gone (closed while EA was down), just
      // purge the orphan keys and move on.
      if(!PositionSelectByTicket(ticket)) { ErasePersistedPlan(ticket); continue; }

      // If we already have this ticket in memory (should not happen at
      // OnInit since g_plans starts empty, but guard against any future
      // double-load), skip rather than create a second plan.
      if(FindPlanIdx(ticket) >= 0) {
         Print("CT plan restore SKIPPED ticket=", ticket,
               " — already in memory");
         continue;
      }

      TradePlan p;
      p.ticket    = ticket;
      p.stage     = (int)GlobalVariableGet(PlanKey(ticket, "stage"));
      p.isBuy     = GlobalVariableGet(PlanKey(ticket, "isBuy")) > 0.5;
      p.origLots  = GlobalVariableGet(PlanKey(ticket, "origLots"));
      p.entry     = GlobalVariableGet(PlanKey(ticket, "entry"));
      p.slOrig    = GlobalVariableGet(PlanKey(ticket, "slOrig"));
      // entryLow/entryHigh added 2026-05-09. Legacy plans persisted before
      // this change have no eL/eH keys — GlobalVariableGet returns 0.0
      // and SignalAnchorSl falls back to p.entry. See plan §6 migration.
      p.entryLow  = GlobalVariableGet(PlanKey(ticket, "eL"));
      p.entryHigh = GlobalVariableGet(PlanKey(ticket, "eH"));
      p.tpCount   = (int)GlobalVariableGet(PlanKey(ticket, "tpCount"));
      p.tps[0]    = GlobalVariableGet(PlanKey(ticket, "tp1"));
      p.tps[1]    = GlobalVariableGet(PlanKey(ticket, "tp2"));
      p.tps[2]    = GlobalVariableGet(PlanKey(ticket, "tp3"));

      int n = ArraySize(g_plans);
      ArrayResize(g_plans, n + 1);
      g_plans[n] = p;
      Print("CT plan restored ticket=", ticket, " stage=", p.stage,
            " tpCount=", p.tpCount);
   }
}

// Look up the most recent DEAL_ENTRY_OUT for `ticket` and return its
// net P&L (DEAL_PROFIT + DEAL_SWAP + DEAL_COMMISSION). Returns 0.0 if no
// closing deal is found within the 7-day window. Used by partial-close
// call sites to pass realized_pnl_delta to /positions/{ticket}/update.
double LookupLatestExitDealPnl(long ticket) {
   datetime now = TimeCurrent();
   datetime since = now - 7 * 24 * 3600;
   if(!HistorySelect(since, now)) return 0.0;
   int total = HistoryDealsTotal();
   datetime bestTime = 0;
   double bestPnl = 0.0;
   for(int i = 0; i < total; i++) {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      if((long)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID) != ticket) continue;
      if(HistoryDealGetInteger(dealTicket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      datetime dt = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);
      if(dt > bestTime) {
         bestTime = dt;
         bestPnl = HistoryDealGetDouble(dealTicket, DEAL_PROFIT)
                 + HistoryDealGetDouble(dealTicket, DEAL_SWAP)
                 + HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
      }
   }
   return bestPnl;
}

// `realized_pnl_delta`: when non-zero, sent to the API which adds it to
// the position's running realized_pnl. Pass the result of
// LookupLatestExitDealPnl(ticket) right after a partial-close so the
// score-calibration script (Step 3 of AI_EVALUATOR_ROADMAP) can compute
// realized R-multiples per evaluator score band. Pass 0.0 (default) for
// non-partial updates (SL move, TP modify) — the field is then omitted.
void PostPositionUpdate(long ticket, double newVolume, double newSl,
                        double realized_pnl_delta = 0.0) {
   string body = "{";
   bool first = true;
   if(newVolume > 0) {
      body += "\"volume\":" + DoubleToString(newVolume, 2);
      first = false;
   }
   if(newSl > 0) {
      if(!first) body += ",";
      body += "\"sl\":" + DoubleToString(newSl, 5);
      first = false;
   }
   if(realized_pnl_delta != 0.0) {
      if(!first) body += ",";
      body += "\"realized_pnl_delta\":" + DoubleToString(realized_pnl_delta, 2);
   }
   body += "}";
   string resp; int status;
   string url = ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/update";
   if(!HttpPostJsonWithStatus(url, body, resp, status)) {
      // Without retry, partial_close_count and sl_moved_at would stay
      // stale in the DB. The AI prompt's SYSTEM STATE block would then
      // show partials_taken=0 / at_BE=false when those should be true,
      // causing duplicate management actions on reminder messages.
      Print("PostPositionUpdate failed ticket=", ticket,
            " status=", status, " — queued for retry");
      EnqueueRetry(url, body);
   }
}

// After PositionClosePartial sends the deal to the broker, the local
// MT5 position cache is updated when the fill confirmation arrives.
// There's a tiny race where reading POSITION_VOLUME immediately after
// the call returns the OLD volume — observed twice on 2026-05-07
// (Trade 8526804766 09:26:57 and Trade 8532532660 14:13:05): journal
// shows the partial deal completed, but the EA's verify-then-advance
// check read stale 0.88 / 1.05 volume microseconds later. Result: false
// "partial_did_not_execute retcode=10009 vol_unchanged=X" failures and
// a DB that thinks no partial happened.
//
// Fix: poll POSITION_VOLUME up to 5 times with 100ms gaps before
// declaring failure. Sleep blocks the OnTimer thread, but the worst
// case (real failure) is 500ms — well within our 1s timer budget.
//
// Returns the post-fill volume. Returns 0.0 if the position is now
// fully gone (rounding edge cases on very small remainders).
double WaitForPartialFill(long ticket, double volBefore, double lotStep) {
   double volAfter = volBefore;
   double threshold = volBefore - lotStep / 2.0;
   for(int i = 0; i < 5; i++) {
      if(i > 0) Sleep(100);
      if(!PositionSelectByTicket(ticket)) return 0.0;
      volAfter = PositionGetDouble(POSITION_VOLUME);
      if(volAfter < threshold) {
         if(i > 0)
            Print("CT partial fill confirmed after ", i, " retry(ies) ticket=",
                  ticket, " vol ", DoubleToString(volBefore, 2),
                  " -> ", DoubleToString(volAfter, 2));
         return volAfter;
      }
   }
   return volAfter;  // last read; still equal to volBefore -> real failure
}

// Recent-open cache: maps ticket -> EA timestamp of the OPEN fill
// confirmation. Used by ReconcileClosedPositions to suppress the
// authoritative pass on tickets that were just opened, where there's
// a millisecond-scale window between the broker confirming the fill
// and MT5's local position table refreshing.
//
// Concrete failure mode observed 2026-05-07 13:03 (ticket 8531279417):
// position_opened logged at 13:03:08.800, deal completed in journal at
// 13:03:08.808, ReconcileClosedPositions ran at 13:03:08.815 and
// PositionSelectByTicket returned false → POSTed close(mt5_not_found)
// 11ms after open. The position was actually still open in MT5; the
// authoritative pass just raced the cache refresh.
//
// Grace window: 10s. Tickets older than 2× grace are pruned to keep
// the array bounded.
struct RecentOpen {
   long     ticket;
   datetime opened_at;
};
RecentOpen g_recent_opens[];
const int  RECENT_OPEN_GRACE_SECS = 10;

void PruneRecentOpens() {
   datetime now = TimeCurrent();
   for(int i = ArraySize(g_recent_opens) - 1; i >= 0; i--) {
      if((now - g_recent_opens[i].opened_at) >= RECENT_OPEN_GRACE_SECS * 2) {
         for(int j = i; j < ArraySize(g_recent_opens) - 1; j++) {
            g_recent_opens[j] = g_recent_opens[j + 1];
         }
         ArrayResize(g_recent_opens, ArraySize(g_recent_opens) - 1);
      }
   }
}

void RememberRecentOpen(long ticket) {
   PruneRecentOpens();
   int n = ArraySize(g_recent_opens);
   ArrayResize(g_recent_opens, n + 1);
   g_recent_opens[n].ticket = ticket;
   g_recent_opens[n].opened_at = TimeCurrent();
}

bool IsRecentlyOpened(long ticket) {
   datetime now = TimeCurrent();
   for(int i = 0; i < ArraySize(g_recent_opens); i++) {
      if(g_recent_opens[i].ticket == ticket
         && (now - g_recent_opens[i].opened_at) < RECENT_OPEN_GRACE_SECS) {
         return true;
      }
   }
   return false;
}

// Compute the new SL when a stage transition asks to "BE on signal anchor"
// (the edge of the signal entry zone behind the chase fill, not the actual
// fill price). For BUY this is entryLow; for SELL it is entryHigh — the
// "behind-the-chase" edge in both cases. Returns the clamped value:
//   - Falls back to p.entry when the plan was registered before entryLow/
//     entryHigh existed (legacy persisted plans on first load post-upgrade).
//   - Falls back to p.entry if the chosen anchor would be looser than the
//     original SL (would *increase* risk vs current SL).
//   - Falls back to p.entry if the anchor is on the wrong side of current
//     price (would trigger an immediate stop-out).
// Clamp a requested SL price to the broker's minimum-stop-distance rule
// (SYMBOL_TRADE_STOPS_LEVEL) and reject if the SL is on the wrong side of
// price entirely.
//
//   isBuy        = position side
//   requestedSl  = the SL the caller wants (from AI payload, anchor, etc.)
//   currentExit  = bid for BUY / ask for SELL (close-side price)
//   finalSl  (out) = the SL to pass to PositionModify
//   wasClamped (out) = true when finalSl != requestedSl because of clamping
//
// Returns true when the modify should proceed with finalSl. Returns false
// when the SL is on the wrong side of price (BUY SL >= bid, SELL SL <= ask)
// — caller should respond with "invalid_stop" and skip the modify, because
// (a) the broker would reject anyway with retcode 10016 and (b) a
// wrong-side SL is almost always an AI mis-read of side or price, not a
// near-miss that should be silently corrected.
//
// When the SL is on the correct side but inside the no-modify zone
// (distance < minDist), we nudge it to the just-outside-minDist edge plus
// a 1-point buffer (some brokers reject at minDist exactly).
bool ClampStopForBroker(bool isBuy, double requestedSl, double currentExit,
                        double &finalSl, bool &wasClamped) {
   wasClamped = false;
   finalSl = requestedSl;
   if(requestedSl <= 0 || currentExit <= 0) return false;
   long stopsLevel = SymbolInfoInteger(Symbol_Override, SYMBOL_TRADE_STOPS_LEVEL);
   double pointSize = SymbolInfoDouble(Symbol_Override, SYMBOL_POINT);
   if(pointSize <= 0) pointSize = 0.01;
   double minDist = (double)stopsLevel * pointSize;
   double buffer = pointSize;  // 1 point past minDist
   int digits = (int)SymbolInfoInteger(Symbol_Override, SYMBOL_DIGITS);
   if(isBuy) {
      if(requestedSl >= currentExit) return false;  // wrong side
      double maxAllowed = currentExit - minDist - buffer;
      if(requestedSl > maxAllowed) {
         finalSl = NormalizeDouble(maxAllowed, digits);
         wasClamped = (MathAbs(finalSl - requestedSl) > pointSize / 2.0);
      }
   } else {
      if(requestedSl <= currentExit) return false;  // wrong side
      double minAllowed = currentExit + minDist + buffer;
      if(requestedSl < minAllowed) {
         finalSl = NormalizeDouble(minAllowed, digits);
         wasClamped = (MathAbs(finalSl - requestedSl) > pointSize / 2.0);
      }
   }
   return true;
}


double SignalAnchorSl(const TradePlan &p, double currentExit) {
   double anchor = p.isBuy ? p.entryLow : p.entryHigh;
   if(anchor <= 0.0) return p.entry;  // legacy plan, no zone persisted
   if(p.isBuy) {
      // SL must stay below current price; SL must not loosen vs slOrig.
      if(anchor >= currentExit) return p.entry;
      if(anchor < p.slOrig)     return p.entry;
   } else {
      if(anchor <= currentExit) return p.entry;
      if(anchor > p.slOrig)     return p.entry;
   }
   return anchor;
}

//-----------------------------------------------------------------------
// TrueBreakEvenSl
// Compute the SL price at which the position nets exactly $0 after broker
// costs (commission round-turn + accrued swap). For BUY this is ABOVE
// entry; for SELL this is BELOW entry. Falls back to the position's
// entry price when costs are unknown, when the symbol metadata is bad,
// or when current price hasn't moved enough to put BE on the valid SL
// side (so caller can still attempt PositionModify without rejection).
//-----------------------------------------------------------------------
// PositionCommissionAccrued
// Returns the sum of commission across every deal that belongs to this
// position. Replaces the deprecated POSITION_COMMISSION property —
// commission is now a per-DEAL field because netting/hedging accounts
// can have multiple fills per position.
double PositionCommissionAccrued(ulong position_id) {
   if(!HistorySelectByPosition((long)position_id)) return 0.0;
   double total = 0.0;
   int deals = HistoryDealsTotal();
   for(int i = 0; i < deals; i++) {
      ulong deal_ticket = HistoryDealGetTicket(i);
      if(deal_ticket == 0) continue;
      total += HistoryDealGetDouble(deal_ticket, DEAL_COMMISSION);
   }
   return total;
}

double TrueBreakEvenSl(ulong ticket) {
   if(!PositionSelectByTicket(ticket)) return 0.0;
   string sym = PositionGetString(POSITION_SYMBOL);
   double entry = PositionGetDouble(POSITION_PRICE_OPEN);
   double vol   = PositionGetDouble(POSITION_VOLUME);
   ulong  pid   = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   double comm  = PositionCommissionAccrued(pid);
   double swap  = PositionGetDouble(POSITION_SWAP);
   double tv    = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   double ts    = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   if(vol <= 0.0 || tv <= 0.0 || ts <= 0.0) {
      return NormalizeDouble(entry, digits);
   }

   double total_cost = MathAbs(comm) * CommissionMultiplier + MathAbs(swap);
   double offset     = total_cost * ts / (tv * vol);

   bool isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   double be  = isBuy ? entry + offset : entry - offset;

   // Validity guard: BE must be on the correct side of the current
   // close-side price (BID for BUY, ASK for SELL). If price hasn't moved
   // enough yet, fall back to entry — at least SL = entry covers move
   // risk even if commission/swap will be eaten as a small net loss.
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   if(isBuy  && be >= bid) be = entry;
   if(!isBuy && be <= ask) be = entry;

   return NormalizeDouble(be, digits);
}

void ManagePlans() {
   if(ArraySize(g_plans) == 0) return;
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0) lotStep = 0.01;

   for(int i = ArraySize(g_plans) - 1; i >= 0; i--) {
      TradePlan p = g_plans[i];

      // If the position no longer exists (closed at final TP, SL, or manually),
      // drop the plan. Intermediate stages may have fired; nothing left to do.
      if(!PositionSelectByTicket(p.ticket)) { RemovePlan(i); continue; }

      if(p.tpCount < 2 || p.stage >= 2) continue;

      double bid = SymbolInfoDouble(Symbol_Override, SYMBOL_BID);
      double ask = SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
      // Exit-side price: BUY closes at bid, SELL at ask. That's the price
      // that actually realizes the TP1/TP2 target.
      double exitPrice = p.isBuy ? bid : ask;

      // ---- Staged SL-management policy (revised 2026-05-09) -----------
      // 1-TP signal: no plan registered (RegisterPlan gate in DoOpen). The
      //              broker-set SL + TP rides the position to closure;
      //              EA never touches it.
      // 2-TP signal: TP1 closes 70%; SL → SignalAnchorSl (entry zone edge
      //              behind the chase, not the fill price); broker TP
      //              REMOVED; remaining ~30% rides TrailStage2Sls until
      //              the trail SL hits on a reversal. TP2 is no longer a
      //              broker-side close target — only a reference for the
      //              trail-gap formula.
      // 3-TP signal: TP1 closes 50%; SL → SignalAnchorSl; broker TP stays
      //              at TP3 as a worst-case ceiling. TP2 closes 30% of
      //              ORIGINAL lots; SL → TP1 price; broker TP REMOVED;
      //              remaining ~20% rides TrailStage2Sls until reversal.
      // -----------------------------------------------------------------

      if(p.stage == 0) {
         double tp1 = p.tps[0];
         bool hit = p.isBuy ? (exitPrice >= tp1) : (exitPrice <= tp1);
         if(!hit) continue;

         // Stage-0 close fraction (revised 2026-05-09):
         //   2-TP: 70% (front-loaded; remainder trails for the rest)
         //   3-TP: 50% (still room for stage-1 to close another 30%)
         double frac = (p.tpCount == 2) ? 0.70 : 0.50;
         double closeLots = MathFloor((p.origLots * frac) / lotStep) * lotStep;
         closeLots = NormalizeDouble(closeLots, 2);
         double volBefore = PositionGetDouble(POSITION_VOLUME);
         double remaining = volBefore - closeLots;

         bool partialOk = false;
         bool partialSkipped = false;

         if(closeLots < minLot || remaining < minLot) {
            // Lot-size sanity prevents the partial. Math won't change on
            // retry, so skip it and advance the stage immediately.
            partialSkipped = true;
            Print("CT plan stage1 partial skipped (lot size) ticket=", p.ticket,
                  " want=", closeLots, " remain=", remaining);
         } else {
            // Verify-then-advance with retry: CTrade.PositionClosePartial
            // can return false on pre-OrderSend validation, AND the local
            // POSITION_VOLUME read can lag the broker fill by ms. Trust
            // the volume diff and poll briefly so the cache catches up
            // before declaring failure. See WaitForPartialFill above.
            bool ctradeOk = trade.PositionClosePartial(p.ticket, closeLots);
            double volAfter = WaitForPartialFill(p.ticket, volBefore, lotStep);
            partialOk = (volAfter < volBefore - lotStep / 2.0);
            if(partialOk) {
               Print("CT plan stage1 partial ok ticket=", p.ticket,
                     " lots=", closeLots,
                     " (vol ", DoubleToString(volBefore, 2),
                     " -> ", DoubleToString(volAfter, 2), ")");
            } else {
               p.stage_attempts++;
               Print("CT plan stage1 partial FAILED ticket=", p.ticket,
                     " lots=", closeLots,
                     " ctrade_ok=", ctradeOk,
                     " retcode=", trade.ResultRetcode(),
                     " last_err=", GetLastError(),
                     " vol_unchanged=", DoubleToString(volBefore, 2),
                     " attempt=", p.stage_attempts, "/", PartialMaxRetries);
               if(p.stage_attempts < PartialMaxRetries) {
                  // Persist the bumped attempt count, retry next tick.
                  g_plans[i] = p;
                  continue;
               }
               Print("CT plan stage1 partial GIVING UP ticket=", p.ticket,
                     " after ", p.stage_attempts,
                     " attempts; advancing stage to 1 anyway");
               // Operator visibility: position rides full size into TP2.
               // POST an ALERT so the bot DMs the owner.
               string giveupBody = StringFormat(
                  "{\"level\":\"warning\",\"text\":\"stage1 giveup ticket=%I64d "
                  "after %d attempts; partial abandoned, position rides full "
                  "size to next TP. Manual review recommended.\"}",
                  p.ticket, p.stage_attempts);
               string aresp;
               HttpPostJson(ApiBaseUrl + "/alerts", giveupBody, aresp);
            }
         }

         // Stage-0 SL move (revised 2026-05-15):
         //   Both 2-TP and 3-TP push SL to TrueBreakEvenSl — the price
         //   where the remaining lots net $0 after commission round-trip
         //   + accrued swap. Falls back to entry when costs unknown or
         //   when price hasn't moved enough yet (price-side guard).
         //   2-TP additionally drops the broker TP (was TP2) to 0 so the
         //   remaining 30% can ride the trail past TP2 if the move extends.
         //   3-TP keeps the broker TP at TP3 as a worst-case ceiling
         //   through stage 1; it is removed at the stage-1→2 transition.
         double newSl = TrueBreakEvenSl(p.ticket);
         if(newSl <= 0.0) newSl = SignalAnchorSl(p, exitPrice);  // hard fallback
         // 2-TP stage-0 broker TP removal is gated by FinalStageMode:
         //   FINAL_TRAIL  → remove TP (0.0) so trail can capture extensions
         //   FINAL_KEEP_TP → leave broker TP at signal final (rides to market hit)
         // 3-TP at stage 0 always keeps current TP (final ceiling persists
         // through stage 1; removal happens at the stage 1→2 transition).
         double newTp;
         if(p.tpCount == 2) {
            newTp = (FinalStageMode == FINAL_TRAIL)
                    ? 0.0
                    : (PositionSelectByTicket(p.ticket)
                       ? PositionGetDouble(POSITION_TP) : 0.0);
         } else {
            newTp = PositionSelectByTicket(p.ticket)
                    ? PositionGetDouble(POSITION_TP) : 0.0;
         }
         double curSl0 = PositionSelectByTicket(p.ticket)
                         ? PositionGetDouble(POSITION_SL) : 0.0;
         double pointSize0 = SymbolInfoDouble(Symbol_Override, SYMBOL_POINT);
         double tol0 = (pointSize0 > 0 ? pointSize0 : 0.01) * 5.0;
         bool slNeedsMove0 = (MathAbs(curSl0 - newSl) > tol0);
         double curTp0 = PositionSelectByTicket(p.ticket)
                         ? PositionGetDouble(POSITION_TP) : 0.0;
         bool tpNeedsChange0 = (MathAbs(curTp0 - newTp) > tol0);
         if(!slNeedsMove0 && !tpNeedsChange0) {
            Print("CT plan stage1 SL/TP already correct ticket=", p.ticket);
         } else if(!trade.PositionModify(p.ticket, newSl, newTp)) {
            Print("CT plan stage1 SL->anchor FAILED ticket=", p.ticket,
                  " want_sl=", DoubleToString(newSl, 5),
                  " want_tp=", DoubleToString(newTp, 5),
                  " retcode=", trade.ResultRetcode(),
                  " last_err=", GetLastError());
         } else {
            Print("CT plan stage1 SL->", DoubleToString(newSl, 5),
                  (p.tpCount == 2 ? " +removeTp" : ""),
                  " ok ticket=", p.ticket,
                  " (tpCount=", p.tpCount, ")");
         }

         p.stage = 1;
         p.stage_attempts = 0;  // reset retry counter for next stage
         g_plans[i] = p;
         GlobalVariableSet(PlanKey(p.ticket, "stage"), (double)p.stage);
         double postVol = PositionSelectByTicket(p.ticket)
                          ? PositionGetDouble(POSITION_VOLUME) : 0.0;
         double pnlDelta = partialOk ? LookupLatestExitDealPnl(p.ticket) : 0.0;
         PostPositionUpdate(p.ticket, postVol, newSl, pnlDelta);
      }
      else if(p.stage == 1 && p.tpCount == 3) {
         double tp2 = p.tps[1];
         bool hit = p.isBuy ? (exitPrice >= tp2) : (exitPrice <= tp2);
         if(!hit) continue;

         // Close 30% of ORIGINAL lots (revised 2026-05-09). Combined with
         // the 50% closed at stage 0, ~20% remains to ride the trail.
         double closeLots = MathFloor((p.origLots * 0.30) / lotStep) * lotStep;
         closeLots = NormalizeDouble(closeLots, 2);
         double volBefore = PositionGetDouble(POSITION_VOLUME);
         double remaining = volBefore - closeLots;

         bool partialOk = false;
         bool partialSkipped = false;

         if(closeLots < minLot || remaining < minLot) {
            partialSkipped = true;
            Print("CT plan stage2 partial skipped (lot size) ticket=", p.ticket,
                  " want=", closeLots, " remain=", remaining);
         } else {
            // Same race protection as stage 1 — see WaitForPartialFill comment.
            bool ctradeOk = trade.PositionClosePartial(p.ticket, closeLots);
            double volAfter = WaitForPartialFill(p.ticket, volBefore, lotStep);
            partialOk = (volAfter < volBefore - lotStep / 2.0);
            if(partialOk) {
               Print("CT plan stage2 partial ok ticket=", p.ticket,
                     " lots=", closeLots,
                     " (vol ", DoubleToString(volBefore, 2),
                     " -> ", DoubleToString(volAfter, 2), ")");
            } else {
               p.stage_attempts++;
               Print("CT plan stage2 partial FAILED ticket=", p.ticket,
                     " lots=", closeLots,
                     " ctrade_ok=", ctradeOk,
                     " retcode=", trade.ResultRetcode(),
                     " last_err=", GetLastError(),
                     " vol_unchanged=", DoubleToString(volBefore, 2),
                     " attempt=", p.stage_attempts, "/", PartialMaxRetries);
               if(p.stage_attempts < PartialMaxRetries) {
                  g_plans[i] = p;
                  continue;
               }
               Print("CT plan stage2 partial GIVING UP ticket=", p.ticket,
                     " after ", p.stage_attempts,
                     " attempts; advancing stage to 2 anyway");
               string giveupBody = StringFormat(
                  "{\"level\":\"warning\",\"text\":\"stage2 giveup ticket=%I64d "
                  "after %d attempts; partial abandoned, SL still moves to BE "
                  "but position rides full size to TP3. Manual review "
                  "recommended.\"}",
                  p.ticket, p.stage_attempts);
               string aresp;
               HttpPostJson(ApiBaseUrl + "/alerts", giveupBody, aresp);
            }
         }

         // Stage-1→2 transition (revised 2026-05-09):
         //   SL → TP1 price (locks at least the TP1 distance of profit on
         //   the ~20% remainder), broker TP REMOVED so the trail can
         //   capture extensions past the channel's TP3.
         double newSl = p.tps[0];
         double curSl = PositionSelectByTicket(p.ticket)
                        ? PositionGetDouble(POSITION_SL) : 0.0;
         double curTp = PositionSelectByTicket(p.ticket)
                        ? PositionGetDouble(POSITION_TP) : 0.0;
         double pointSize = SymbolInfoDouble(Symbol_Override, SYMBOL_POINT);
         double tol = (pointSize > 0 ? pointSize : 0.01) * 5.0;
         // 3-TP stage 1→2 broker TP removal gated by FinalStageMode:
         //   FINAL_TRAIL  → remove TP (0.0) so trail engages
         //   FINAL_KEEP_TP → leave broker TP at TP3 (rides to market hit)
         double targetTp = (FinalStageMode == FINAL_TRAIL) ? 0.0 : curTp;
         bool slNeedsMove = (MathAbs(curSl - newSl) > tol);
         bool tpNeedsRemoval = (MathAbs(curTp - targetTp) > tol);
         if(!slNeedsMove && !tpNeedsRemoval) {
            Print("CT plan stage2 SL/TP already correct ticket=", p.ticket);
         } else if(!trade.PositionModify(p.ticket, newSl, targetTp)) {
            Print("CT plan stage2 SL->TP1+removeTp FAILED ticket=", p.ticket,
                  " want_sl=", DoubleToString(newSl, 5),
                  " retcode=", trade.ResultRetcode(),
                  " last_err=", GetLastError());
         } else {
            Print("CT plan stage2 SL->", DoubleToString(newSl, 5),
                  " (TP1) +removeTp ok ticket=", p.ticket,
                  " (trailing SL now active)");
         }

         p.stage = 2;
         p.stage_attempts = 0;
         g_plans[i] = p;
         GlobalVariableSet(PlanKey(p.ticket, "stage"), (double)p.stage);
         double postVol2 = PositionSelectByTicket(p.ticket)
                           ? PositionGetDouble(POSITION_VOLUME) : 0.0;
         double pnlDelta2 = partialOk ? LookupLatestExitDealPnl(p.ticket) : 0.0;
         PostPositionUpdate(p.ticket, postVol2, newSl, pnlDelta2);
      }
   }
}

// Trailing SL for the post-broker-TP-removed remainder. Activates on:
//   - 2-TP plans at stage >= 1 (after TP1: 30% remainder, broker TP gone)
//   - 3-TP plans at stage >= 2 (after TP2: 20% remainder, broker TP gone)
// Both leave the position with no broker upper bound. We ratchet SL up
// (BUY) or down (SELL) so the remainder rides the trend and exits on a
// reversal instead of stopping at a fixed level.
//
// Trail gap: |finalTp - entry| divided by N, where N = 2 for 2-TP plans
// and 3 for 3-TP plans. The 2-TP gap is intentionally wider — the
// remainder rides a single trail stage with no intermediate SL bump,
// so a tighter gap stops out on normal noise. The 3-TP gap is tighter
// because SL already sits at TP1 (locked profit) when trail engages.
// Examples: 3-TP entry 4673.71 / TP3 4720 → gap $15.43/oz.
//           2-TP entry 4710 / TP2 4738 → gap $14.00/oz.
//
// Step threshold: 5 * SYMBOL_POINT. Filters tick-noise micro-updates
// without missing meaningful trail steps. On gold (point=$0.01) that's
// $0.05/oz; updating SL on each $0.05 advance keeps broker traffic
// reasonable (~10 modifies per dollar of trend, not 100).
//
// Stops level: respect SYMBOL_TRADE_STOPS_LEVEL — if the broker's
// minimum SL distance is greater than the trail-gap-derived SL would
// allow, clamp to broker minimum so the modify isn't rejected.
//
// Direction: SL only ratchets toward the favourable side (up for BUY,
// down for SELL). Never loosens.
void TrailStage2Sls() {
   if(ArraySize(g_plans) == 0) return;
   // Skip trailing entirely when operator prefers the broker TP at the
   // signal's final TP to close the remainder at market.
   if(FinalStageMode == FINAL_KEEP_TP) return;

   double pointSize = SymbolInfoDouble(Symbol_Override, SYMBOL_POINT);
   if(pointSize <= 0) pointSize = 0.01;
   double stepThreshold = pointSize * 5.0;
   long stopsLevel = SymbolInfoInteger(Symbol_Override, SYMBOL_TRADE_STOPS_LEVEL);
   double minDist = (double)stopsLevel * pointSize;
   int digits = (int)SymbolInfoInteger(Symbol_Override, SYMBOL_DIGITS);

   for(int i = 0; i < ArraySize(g_plans); i++) {
      TradePlan p = g_plans[i];
      // Activation gates (revised 2026-05-09):
      //   2-TP: trail starts at stage >= 1 (after TP1 partial + TP removal)
      //   3-TP: trail starts at stage >= 2 (after TP2 partial + TP removal)
      bool active = (p.tpCount == 2 && p.stage >= 1)
                 || (p.tpCount == 3 && p.stage >= 2);
      if(!active) continue;
      if(!PositionSelectByTicket(p.ticket)) continue;

      // finalTp: last TP in the signal — TP2 for 2-TP, TP3 for 3-TP.
      // Gap divisor: 2 for 2-TP (wider gap matches the wider remainder
      // sitting on a single-stage trail), 3 for 3-TP (tighter, since the
      // remainder is smaller and SL already sits at TP1).
      double finalTp = p.tps[p.tpCount - 1];
      double divisor = (p.tpCount == 2) ? 2.0 : 3.0;
      double trailGap = MathAbs(finalTp - p.entry) / divisor;
      if(trailGap <= 0.0) continue;

      double bid = SymbolInfoDouble(Symbol_Override, SYMBOL_BID);
      double ask = SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
      if(bid <= 0.0 || ask <= 0.0) continue;

      double curSl = PositionGetDouble(POSITION_SL);
      double newSl = 0.0;

      if(p.isBuy) {
         double exitPrice = bid;  // BUY closes at bid
         double slFloor = exitPrice - trailGap;
         // Stops-level clamp: SL must be at least minDist below current price.
         if(exitPrice - slFloor < minDist) slFloor = exitPrice - minDist;
         // Ratchet up only.
         if(slFloor <= curSl + stepThreshold) continue;
         newSl = NormalizeDouble(slFloor, digits);
      } else {
         double exitPrice = ask;  // SELL closes at ask
         double slCeil = exitPrice + trailGap;
         if(slCeil - exitPrice < minDist) slCeil = exitPrice + minDist;
         // Ratchet down only.
         if(slCeil >= curSl - stepThreshold) continue;
         newSl = NormalizeDouble(slCeil, digits);
      }

      // TP stays at 0 (removed at stage 2 transition). Pass 0.0 to keep
      // it that way. Some brokers reject identical TP=0 modifies; the
      // trade.PositionModify wrapper handles that uniformly.
      if(trade.PositionModify(p.ticket, newSl, 0.0)) {
         PostPositionUpdate(p.ticket, 0, newSl);
         Print("CT plan trail SL ok ticket=", p.ticket,
               " tpCount=", p.tpCount, " stage=", p.stage,
               " ", DoubleToString(curSl, digits),
               " -> ", DoubleToString(newSl, digits),
               " (gap=", DoubleToString(trailGap, digits), ")");
      } else {
         // Don't spam — only log when retcode changes from prior tick
         // would be ideal, but for now log every failure so issues are
         // visible in the journal.
         Print("CT plan trail SL FAILED ticket=", p.ticket,
               " tpCount=", p.tpCount, " stage=", p.stage,
               " want=", DoubleToString(newSl, digits),
               " retcode=", trade.ResultRetcode(),
               " last_err=", GetLastError());
      }
   }
}

void DoModify(long id, string payload) {
   long ticket = StringToInteger(JsonField(payload, "mt5_ticket"));
   double newSl = StringToDouble(JsonField(payload, "new_sl"));
   double newTp = StringToDouble(JsonField(payload, "new_tp"));
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double curSl = PositionGetDouble(POSITION_SL);
   double curTp = PositionGetDouble(POSITION_TP);
   if(newSl == 0) newSl = curSl;
   if(newTp == 0) newTp = curTp;
   if(trade.PositionModify(ticket, newSl, newTp)) {
      // Operator override: drop any staged plan on this ticket so the next
      // tp1/tp2 cross doesn't silently stomp the SL/TP the user just set.
      if(RemovePlanByTicket(ticket))
         Print("CT plan dropped on MODIFY ticket=", ticket,
               " — user override supersedes staged management");
      PostResult(id, "executed", ticket, "");
   } else {
      PostResult(id, "failed", ticket, "modify_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void DoClose(long id, string payload) {
   long ticket = StringToInteger(JsonField(payload, "mt5_ticket"));
   if(trade.PositionClose(ticket)) {
      // Plan would self-drop on next ManagePlans tick (position gone), but
      // explicit cleanup avoids a one-tick window of stale state.
      RemovePlanByTicket(ticket);
      PostResult(id, "executed", ticket, "");
      string body;
      HttpPostJson(ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/close",
                   BuildCloseBody("ai_close", ticket), body);
   } else {
      PostResult(id, "failed", ticket, "close_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void DoCloseAll(long id, string payload) {
   string sym = JsonField(payload, "symbol");
   int closed = 0, failed = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(trade.PositionClose(t)) {
         closed++;
         RemovePlanByTicket((long)t);
         string body;
         HttpPostJson(ApiBaseUrl + "/positions/" + IntegerToString(t) + "/close",
                      BuildCloseBody("close_all", (long)t), body);
      } else failed++;
   }
   // Status by outcome: a CLOSE_ALL where every PositionClose failed must
   // not be reported as 'executed' — the operator's DM would show a green
   // checkmark while positions stay open. Vacuous success (no matching
   // positions, closed=0 failed=0) stays 'executed'.
   string status = (failed > 0 && closed == 0) ? "failed" : "executed";
   PostResult(id, status, 0, StringFormat("closed=%d failed=%d", closed, failed));
}

int CountOurOpenPositions() {
   int n = 0;
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) == Magic) n++;
   }
   return n;
}

// ---- Phase 2: management actions for the singleton open position ------
//
// Single-position mode: the channel sends instructions like "أمن دخولك"
// without a ticket, because there's at most one open trade at a time.
// These helpers + handlers resolve "the open position" implicitly.

// Returns the ticket of our (Magic) open position on `symbol`, or 0
// if none. If multiple are open (shouldn't happen in single-position mode
// but guard anyway), returns the first encountered.
long FindSingletonOpenTicket(string symbol) {
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != Magic) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      return (long)t;
   }
   return 0;
}

// Build an OPEN-action-shaped payload string from a /positions/last_closed
// response so we can pipe it into DoOpen() and reuse its chase / market
// logic for REOPEN_LAST and REINFORCE. Returns "" on failure.
string BuildOpenPayloadFromLastClosed(string lastClosedBody) {
   string sigBlock = JsonField(lastClosedBody, "signal");
   if(sigBlock == "" || StringGetCharacter(sigBlock, 0) != '{') return "";
   string side = JsonField(sigBlock, "side");
   string entryLow = JsonField(sigBlock, "entry_low");
   string entryHigh = JsonField(sigBlock, "entry_high");
   string sl = JsonField(sigBlock, "sl");
   string tps = JsonField(sigBlock, "tps");      // "[4710,4720,4735]"
   string sym = JsonField(sigBlock, "symbol");
   if(sym == "") sym = Symbol_Override;
   if(side == "" || sl == "" || tps == "") return "";
   // entry_low/high may be missing on legacy rows — fall back to the closed
   // position's recorded entry_price so REOPEN_LAST still has a zone.
   if(entryLow == "" || entryHigh == "") {
      string ep = JsonField(lastClosedBody, "entry_price");
      if(ep == "") return "";
      entryLow = ep;
      entryHigh = ep;
   }
   return StringFormat(
      "{\"symbol\":\"%s\",\"side\":\"%s\",\"entry_low\":%s,\"entry_high\":%s,"
      "\"sl\":%s,\"tps\":%s}",
      sym, side, entryLow, entryHigh, sl, tps
   );
}

void DoMoveSlBe(long id, string payload) {
   if(!EnableAiPartialAndBe) {
      PostResult(id, "executed", 0, "noop_partial_and_be_disabled");
      return;
   }
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double entry = PositionGetDouble(POSITION_PRICE_OPEN);
   double curTp = PositionGetDouble(POSITION_TP);
   // True break-even: SL price where the open position nets $0 after
   // broker costs (commission × CommissionMultiplier + abs(swap)).
   // For BUY this is ABOVE entry; for SELL this is BELOW. Falls back
   // to entry when costs unknown or when current price hasn't moved
   // enough yet (TrueBreakEvenSl internal guard).
   double beSl = TrueBreakEvenSl(ticket);
   if(beSl <= 0.0) beSl = entry;  // hard fallback (ticket vanished mid-call)
   // Broker minDist clamp (BE often lands close to current price by design;
   // some brokers reject SL inside SYMBOL_TRADE_STOPS_LEVEL).
   bool isBuyBe = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   double curExitBe = isBuyBe ? SymbolInfoDouble(Symbol_Override, SYMBOL_BID)
                              : SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
   double clampedBeSl;
   bool wasClampedBe;
   if(!ClampStopForBroker(isBuyBe, beSl, curExitBe, clampedBeSl, wasClampedBe)) {
      // BE SL on the wrong side of price → trade is already in significant
      // profit, BE move is moot. Report and skip the modify rather than
      // letting the broker reject with retcode 10016.
      PostResult(id, "rejected", ticket,
                 StringFormat("invalid_stop_be: sl=%.2f vs price=%.2f",
                              beSl, curExitBe));
      return;
   }
   beSl = clampedBeSl;
   if(trade.PositionModify(ticket, beSl, curTp)) {
      // For 3-TP plans still in stage 0: keep the plan armed and advance
      // stage 0 -> 1 instead of dropping it. This lets ManagePlans fire
      // the natural stage-1 partial close at TP2 even after the AI's
      // half-and-BE override, producing a clean three-stage exit
      // (50% at AI signal, ~33% at TP2, ~17% at TP3 or BE). For 2-TP
      // plans, plans already past stage 0, and 1-TP signals (which
      // never had a plan registered), fall back to the original
      // operator-override behaviour and drop the plan entirely.
      int planIdx = FindPlanIdx(ticket);
      if(planIdx >= 0
         && g_plans[planIdx].tpCount == 3
         && g_plans[planIdx].stage == 0) {
         g_plans[planIdx].stage = 1;
         GlobalVariableSet(PlanKey(ticket, "stage"), 1.0);
         Print("CT plan stage advanced 0->1 on AI MOVE_SL_BE ticket=", ticket,
               " — TP2 partial close still armed");
      } else {
         RemovePlanByTicket(ticket);
      }
      PostPositionUpdate(ticket, 0, beSl);
      PostResult(id, "executed", ticket, "");
   } else {
      PostResult(id, "failed", ticket,
                 "modify_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void DoMoveSl(long id, string payload) {
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double requestedSl = StringToDouble(JsonField(payload, "price"));
   if(requestedSl <= 0) { PostResult(id, "failed", ticket, "invalid_price"); return; }
   double curTp = PositionGetDouble(POSITION_TP);
   bool isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   double curExit = isBuy ? SymbolInfoDouble(Symbol_Override, SYMBOL_BID)
                          : SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
   double finalSl;
   bool wasClamped;
   if(!ClampStopForBroker(isBuy, requestedSl, curExit, finalSl, wasClamped)) {
      PostResult(id, "rejected", ticket,
                 StringFormat("invalid_stop: sl=%.2f wrong side of price=%.2f",
                              requestedSl, curExit));
      return;
   }
   if(trade.PositionModify(ticket, finalSl, curTp)) {
      RemovePlanByTicket(ticket);
      PostPositionUpdate(ticket, 0, finalSl);
      string note = wasClamped
         ? StringFormat("clamped %.2f->%.2f (broker minDist)", requestedSl, finalSl)
         : "";
      PostResult(id, "executed", ticket, note);
   } else {
      PostResult(id, "failed", ticket,
                 "modify_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

// MODIFY_TPS — replace the broker TP and re-stage g_plans[] with a fresh
// ladder. Emitted only by the AI's "new OPEN signal arrives with position
// open" RULE C path (see ai.py SYSTEM_PROMPT). Caller responsibility:
//  - tps array is already filtered to values still ahead of current price
//    (Python prompt does this); we trust the input here.
//  - SL is updated by an accompanying MOVE_SL action that runs BEFORE this
//    one (orchestrator inserts in [MOVE_SL, MODIFY_TPS] order; EA claims
//    in id order). So we read CURRENT SL post-MOVE_SL when modifying TP.
//
// Stage handling: we reset stage to 0 and rebase origLots to the CURRENT
// volume (post any prior partials). ManagePlans treats the new ladder as
// fresh, sized against what's actually open. RegisterPlan dedupes by
// ticket so calling it on an existing entry replaces the plan in place.
void DoModifyTps(long id, string payload) {
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }

   string tpsStr = JsonField(payload, "tps");
   double tps[];
   ParseTps(tpsStr, tps);
   int tpCount = ArraySize(tps);
   if(tpCount == 0) { PostResult(id, "rejected", ticket, "empty_tps"); return; }
   if(tpCount > 3) tpCount = 3;

   bool isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   SortTpsByDistance(tps, tpCount, isBuy);

   double curSl = PositionGetDouble(POSITION_SL);
   double finalTp = tps[tpCount - 1];

   if(!trade.PositionModify(ticket, curSl, finalTp)) {
      PostResult(id, "failed", ticket,
                 "modify_failed:" + IntegerToString(trade.ResultRetcode()));
      return;
   }

   double curVol = PositionGetDouble(POSITION_VOLUME);
   double entry  = PositionGetDouble(POSITION_PRICE_OPEN);
   // RegisterPlan dedupes by ticket — replaces existing entry in place.
   // Pass curVol as origLots so future stage closes are sized against
   // what's actually open (not the pre-partial original).
   // entryLow/entryHigh = 0 -> SignalAnchorSl falls back to `entry` (the
   // chased fill), preserving the pre-Step-1 BE-on-fill behaviour for
   // re-staged plans. MODIFY_TPS does not currently carry the new signal's
   // entry zone in its payload; revisit if you want full new-policy
   // fidelity on re-stages.
   RegisterPlan(ticket, isBuy, curVol, entry, curSl, 0.0, 0.0, tps, tpCount);

   PostPositionUpdate(ticket, curVol, 0);    // newSl=0 → leave DB SL as-is (set by preceding MOVE_SL)
   PostResult(id, "executed", ticket,
              StringFormat("tps=%d final=%.2f", tpCount, finalTp));
}

void DoClosePartial(long id, string payload) {
   if(!EnableAiPartialAndBe) {
      PostResult(id, "executed", 0, "noop_partial_and_be_disabled");
      return;
   }
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double frac = StringToDouble(JsonField(payload, "fraction"));
   if(frac <= 0.0 || frac >= 1.0) frac = 0.5;  // sanity fallback
   // Use plan.origLots when available so partials chain correctly across
   // multiple manual closes; fall back to current volume otherwise.
   int planIdx = FindPlanIdx(ticket);
   double basis = (planIdx >= 0) ? g_plans[planIdx].origLots
                                 : PositionGetDouble(POSITION_VOLUME);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0) lotStep = 0.01;
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   double closeLots = MathFloor((basis * frac) / lotStep) * lotStep;
   closeLots = NormalizeDouble(closeLots, 2);
   double currentVol = PositionGetDouble(POSITION_VOLUME);
   double remaining = currentVol - closeLots;
   if(closeLots < minLot || remaining < minLot) {
      PostResult(id, "rejected", ticket, StringFormat(
         "lot_too_small: close=%.2f remain=%.2f", closeLots, remaining));
      return;
   }
   // Verify-then-advance with retry: CTrade::PositionClosePartial can return
   // false in pre-OrderSend validation while ResultRetcode is a stale
   // success code from a previous call, AND the local POSITION_VOLUME read
   // can lag the deal confirmation by milliseconds. Trust the volume diff,
   // and poll briefly so the cache catches up before declaring failure.
   // See WaitForPartialFill comment for the 2026-05-07 incident details.
   double volBefore = PositionGetDouble(POSITION_VOLUME);
   trade.PositionClosePartial(ticket, closeLots);
   double volAfter = WaitForPartialFill(ticket, volBefore, lotStep);
   bool partialOk = (volAfter < volBefore - lotStep / 2.0);
   if(partialOk) {
      double pnlDelta = LookupLatestExitDealPnl(ticket);
      PostPositionUpdate(ticket, volAfter, 0, pnlDelta);
      PostResult(id, "executed", ticket,
                 StringFormat("closed=%.2f vol=%.2f->%.2f pnl=%.2f",
                              closeLots, volBefore, volAfter, pnlDelta));
   } else {
      PostResult(id, "failed", ticket,
                 StringFormat("partial_did_not_execute retcode=%d vol_unchanged=%.2f",
                              trade.ResultRetcode(), volBefore));
   }
}

void DoCloseFull(long id, string payload) {
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(trade.PositionClose(ticket)) {
      RemovePlanByTicket(ticket);
      string body;
      HttpPostJson(ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/close",
                   BuildCloseBody("ai_close_full", ticket), body);
      PostResult(id, "executed", ticket, "");
   } else {
      PostResult(id, "failed", ticket,
                 "close_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void DoReopenLast(long id, string payload) {
   if(FindSingletonOpenTicket(Symbol_Override) > 0) {
      PostResult(id, "rejected", 0, "already_open");
      return;
   }
   string within = JsonField(payload, "within_hours");
   if(within == "") within = "24";
   string url = ApiBaseUrl + "/positions/last_closed?symbol=" + Symbol_Override
              + "&within_hours=" + within;
   string body;
   if(!HttpGet(url, body) || body == "" || StringFind(body, "\"ticket\"") < 0) {
      PostResult(id, "rejected", 0, "no_recent_close");
      return;
   }
   string fakePayload = BuildOpenPayloadFromLastClosed(body);
   if(fakePayload == "") {
      PostResult(id, "failed", 0, "last_closed_unparseable");
      return;
   }
   Print("CT REOPEN_LAST id=", id, " payload=", fakePayload);
   DoOpen(id, fakePayload);  // reuses chase/market flow + PostResult
}

void DoReinforce(long id, string payload) {
   if(!EnableReinforce) {
      PostResult(id, "executed", 0, "noop_reinforce_disabled");
      return;
   }
   long currentTicket = FindSingletonOpenTicket(Symbol_Override);
   string fakePayload = "";

   if(currentTicket > 0) {
      // Snapshot the originating signal BEFORE closing. The previous
      // implementation closed first, then queried /positions/last_closed
      // which races the fire-and-forget /positions/{t}/close POST and
      // could return an older trade's params.
      string snapUrl = ApiBaseUrl + "/positions/by_ticket/"
                     + IntegerToString(currentTicket);
      string snapBody;
      if(HttpGet(snapUrl, snapBody)
         && snapBody != "" && StringFind(snapBody, "\"ticket\"") >= 0) {
         fakePayload = BuildOpenPayloadFromLastClosed(snapBody);
      }
      // Close regardless of PnL (channel semantics).
      if(trade.PositionClose(currentTicket)) {
         RemovePlanByTicket(currentTicket);
         string closeBody;
         HttpPostJson(
            ApiBaseUrl + "/positions/" + IntegerToString(currentTicket) + "/close",
            BuildCloseBody("reinforce", currentTicket), closeBody);
      } else {
         PostResult(id, "failed", currentTicket,
                    "reinforce_close_failed:" + IntegerToString(trade.ResultRetcode()));
         return;
      }
   }

   // Fallback for the no-open-position case (e.g. closed at SL just
   // before this REINFORCE message arrived): try /positions/last_closed.
   if(fakePayload == "") {
      string url = ApiBaseUrl + "/positions/last_closed?symbol="
                 + Symbol_Override + "&within_hours=24";
      string body;
      if(!HttpGet(url, body) || body == "" || StringFind(body, "\"ticket\"") < 0) {
         PostResult(id, "rejected", 0, "no_recent_close");
         return;
      }
      fakePayload = BuildOpenPayloadFromLastClosed(body);
   }

   if(fakePayload == "") {
      PostResult(id, "failed", 0, "reinforce_payload_unparseable");
      return;
   }
   Print("CT REINFORCE id=", id, " payload=", fakePayload);
   DoOpen(id, fakePayload);
}

void DoTightenSl(long id, string payload) {
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double byFrac = StringToDouble(JsonField(payload, "by_fraction"));
   if(byFrac <= 0.0 || byFrac >= 1.0) byFrac = 0.5;
   double entry = PositionGetDouble(POSITION_PRICE_OPEN);
   double curSl = PositionGetDouble(POSITION_SL);
   double curTp = PositionGetDouble(POSITION_TP);
   if(curSl <= 0) { PostResult(id, "rejected", ticket, "no_sl_to_tighten"); return; }
   bool isBuy = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY);
   double newSl;
   if(isBuy) {
      // BUY: current SL below entry. Tighten = move SL toward entry.
      double dist = entry - curSl;
      if(dist <= 0) { PostResult(id, "rejected", ticket, "sl_already_above_entry"); return; }
      newSl = entry - dist * (1.0 - byFrac);
   } else {
      // SELL: current SL above entry. Tighten = move SL toward entry.
      double dist = curSl - entry;
      if(dist <= 0) { PostResult(id, "rejected", ticket, "sl_already_below_entry"); return; }
      newSl = entry + dist * (1.0 - byFrac);
   }
   // Broker minDist clamp. Tightening can land the SL close to price.
   double curExitT = isBuy ? SymbolInfoDouble(Symbol_Override, SYMBOL_BID)
                           : SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
   double clampedT;
   bool wasClampedT;
   double requestedT = newSl;
   if(!ClampStopForBroker(isBuy, newSl, curExitT, clampedT, wasClampedT)) {
      PostResult(id, "rejected", ticket,
                 StringFormat("invalid_stop_tighten: sl=%.2f vs price=%.2f",
                              newSl, curExitT));
      return;
   }
   newSl = clampedT;
   if(trade.PositionModify(ticket, newSl, curTp)) {
      RemovePlanByTicket(ticket);
      PostPositionUpdate(ticket, 0, newSl);
      string clampNote = wasClampedT
         ? StringFormat(" clamped %.2f->%.2f", requestedT, newSl)
         : "";
      PostResult(id, "executed", ticket,
                 StringFormat("sl_was=%.2f sl_now=%.2f%s",
                              curSl, newSl, clampNote));
   } else {
      PostResult(id, "failed", ticket,
                 "modify_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void ReconcileClosedPositions() {
   // Runs every OnTimer tick (PollIntervalSec, default 1s).
   //
   // Authoritative pass: ask the API which tickets it still thinks are
   // open, then for any ticket MT5 cannot select as a live position,
   // POST /positions/{t}/close with reason=mt5_not_found. This single
   // pass catches every close case:
   //   - closes inside any time window
   //   - closes outside the previous 48h history window
   //   - manual closes the EA missed during downtime
   //   - anything stuck from past bugs
   //
   // The previous implementation also had a 48h history-deal scan that
   // POSTed close(mt5_close) for every DEAL_ENTRY_OUT it found. That
   // path was buggy: in hedging mode partial closes ALSO carry
   // DEAL_ENTRY=DEAL_ENTRY_OUT, so the scan treated each partial as a
   // full close and prematurely flipped the DB row to status='closed'
   // while the position was still riding open in MT5.
   //
   // Concrete impact observed on 2026-05-06: tickets 8506648007 (08:48:55)
   // and 8511718622 (12:24:50) were marked closed by the API immediately
   // after their partial fills, causing the listener's state_summary to
   // render "no open position" for the rest of those positions' lifetime.
   // New OPEN signals arriving in that window were inserted by the
   // orchestrator (Python validator passed because DB lied) then rejected
   // by the EA's CountOurOpenPositions() guard — bypassing the entire
   // MOVE_SL+MODIFY_TPS decision tree. The history scan was strictly
   // redundant with the authoritative pass below (every case it caught
   // is also caught here), so it has been removed.
   //
   // --- Authoritative pass: ask the API which tickets it still thinks are
   // open. Any ticket MT5 can't select as an open position gets closed with
   // mt5_not_found. ---
   string openBody;
   if(!HttpGet(ApiBaseUrl + "/positions?status=open&limit=500", openBody)) return;
   int pos = 0;
   while(true) {
      int p = StringFind(openBody, "\"mt5_ticket\":", pos);
      if(p < 0) break;
      p += StringLen("\"mt5_ticket\":");
      int end = p;
      while(end < StringLen(openBody)) {
         ushort c = StringGetCharacter(openBody, end);
         if(c == ',' || c == '}' || c == ']') break;
         end++;
      }
      pos = end;
      long ticket = StringToInteger(StringSubstr(openBody, p, end - p));
      if(ticket <= 0) continue;
      // Skip tickets we just opened — there's a brief window after the
      // broker fill where MT5's local position cache hasn't refreshed
      // and PositionSelectByTicket returns false even though the
      // position is genuinely open. See RememberRecentOpen comment for
      // the 2026-05-07 13:03 incident this prevents.
      if(IsRecentlyOpened(ticket)) continue;
      if(!PositionSelectByTicket(ticket)) {
         // Query MT5 history for the position's most recent closing
         // deal and surface its DEAL_REASON to the API so close_reason
         // distinguishes TP / SL / manual / expert closes instead of
         // the generic 'mt5_not_found'. Phase-3 fix for the May-7
         // observation that all 5 closes that day surfaced as
         // 'mt5_not_found', losing all forensic information.
         string reason = ResolveCloseReason(ticket);
         string url = ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/close";
         string resp;
         HttpPostJson(url, BuildCloseBody(reason, ticket), resp);
         Print("CT reconcile: ticket=", ticket,
               " absent from MT5 → POSTed close (", reason, ")");
      }
   }
}

// Map MT5's DEAL_REASON enum to a stable string the API/DB can store.
// Returns 'mt5_close_unknown' when history lookup fails or no closing
// deal is found for the ticket. Mirrors enum names from MQL5 docs:
//   DEAL_REASON_CLIENT  = 0  (operator click in terminal)
//   DEAL_REASON_MOBILE  = 1
//   DEAL_REASON_WEB     = 2
//   DEAL_REASON_EXPERT  = 3  (programmatic — could be us or another EA)
//   DEAL_REASON_SL      = 4  (broker SL hit)
//   DEAL_REASON_TP      = 5  (broker TP hit)
//   DEAL_REASON_SO      = 6  (stop-out)
//   DEAL_REASON_ROLLOVER= 7
//   DEAL_REASON_VMARGIN = 8
//   DEAL_REASON_SPLIT   = 9
string ResolveCloseReason(long ticket) {
   datetime now = TimeCurrent();
   datetime since = now - 7 * 24 * 3600;  // 7 days back; closes outside
                                          // this window are extremely rare
                                          // and not worth a wider scan.
   if(!HistorySelect(since, now)) return "mt5_close_unknown";
   int total = HistoryDealsTotal();
   long bestReason = -1;
   datetime bestTime = 0;
   for(int i = 0; i < total; i++) {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      if((long)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID) != ticket) continue;
      if(HistoryDealGetInteger(dealTicket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      datetime dt = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);
      if(dt > bestTime) {
         bestTime = dt;
         bestReason = HistoryDealGetInteger(dealTicket, DEAL_REASON);
      }
   }
   if(bestReason < 0) return "mt5_not_found";  // ticket truly absent
   switch((int)bestReason) {
      case 0: return "mt5_manual";        // DEAL_REASON_CLIENT
      case 1: return "mt5_manual_mobile"; // DEAL_REASON_MOBILE
      case 2: return "mt5_manual_web";    // DEAL_REASON_WEB
      case 3: return "mt5_expert";        // DEAL_REASON_EXPERT
      case 4: return "mt5_sl";            // DEAL_REASON_SL
      case 5: return "mt5_tp";            // DEAL_REASON_TP
      case 6: return "mt5_so";            // DEAL_REASON_SO
      case 7: return "mt5_rollover";      // DEAL_REASON_ROLLOVER
      case 8: return "mt5_vmargin";       // DEAL_REASON_VMARGIN
      case 9: return "mt5_split";         // DEAL_REASON_SPLIT
      default: return StringFormat("mt5_reason_%d", (int)bestReason);
   }
}

// Walk MT5 deal history (7-day window) for the closing deal of `ticket`
// and produce the JSON body for POST /positions/{ticket}/close, including
// the new exit_price + realized_pnl fields when available. The Python
// calibration script (scripts/score_calibration.py) reads these to bucket
// realized R-multiples per evaluator score band.
//
// Calculations:
//   exit_price   = price of the most recent DEAL_ENTRY_OUT for this position
//   realized_pnl = sum of DEAL_PROFIT across ALL exit deals for this position
//                  (covers stage-1/stage-2 partials + the final close)
//
// Returns just `{"reason":"<reason>"}` when history lookup fails — the API
// handler accepts that legacy shape and leaves exit_price/realized_pnl NULL.
string BuildCloseBody(string reason, long ticket) {
   datetime now = TimeCurrent();
   datetime since = now - 7 * 24 * 3600;
   double exit_price = 0.0;
   double realized_pnl = 0.0;
   datetime bestTime = 0;
   bool foundAny = false;
   if(HistorySelect(since, now)) {
      int total = HistoryDealsTotal();
      for(int i = 0; i < total; i++) {
         ulong dealTicket = HistoryDealGetTicket(i);
         if(dealTicket == 0) continue;
         if((long)HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID) != ticket) continue;
         if(HistoryDealGetInteger(dealTicket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
         realized_pnl += HistoryDealGetDouble(dealTicket, DEAL_PROFIT);
         realized_pnl += HistoryDealGetDouble(dealTicket, DEAL_SWAP);
         realized_pnl += HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);
         datetime dt = (datetime)HistoryDealGetInteger(dealTicket, DEAL_TIME);
         if(dt >= bestTime) {
            bestTime = dt;
            exit_price = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
         }
         foundAny = true;
      }
   }
   if(!foundAny) {
      return "{\"reason\":\"" + reason + "\"}";
   }
   return StringFormat(
      "{\"reason\":\"%s\",\"exit_price\":%.2f,\"realized_pnl\":%.2f}",
      reason, exit_price, realized_pnl);
}

void ParseTps(string tpsStr, double &out[]) {
   ArrayResize(out, 0);
   string s = tpsStr;
   StringReplace(s, "[", ""); StringReplace(s, "]", "");
   string parts[]; int n = StringSplit(s, ',', parts);
   for(int i = 0; i < n; i++) {
      double v = StringToDouble(parts[i]);
      if(v > 0) { ArrayResize(out, ArraySize(out) + 1); out[ArraySize(out) - 1] = v; }
   }
}

// Sort TPs so tps[0] is nearest the trade's intended entry direction and
// tps[n-1] is farthest. Works by signal side, not raw numeric order:
//   BUY  → all TPs above entry  → ascending (nearest first).
//   SELL → all TPs below entry  → descending (nearest first).
// The EA treats tps[n-1] as the final TP sent to MT5 and tps[0..n-2] as
// partial-close triggers; callers must normalise before either use.
void SortTpsByDistance(double &tps[], int n, bool isBuy) {
   for(int a = 0; a < n - 1; a++) {
      for(int b = a + 1; b < n; b++) {
         bool swap = isBuy ? (tps[b] < tps[a]) : (tps[b] > tps[a]);
         if(swap) { double t = tps[a]; tps[a] = tps[b]; tps[b] = t; }
      }
   }
}

// ---- Persistent retry queue ----
//
// When an HTTP POST to the API fails (api.py down, network blip), we don't
// want to drop the result — that's how trades get stranded in `claimed`
// (action #5 in FIXES_TODO.md). Instead we persist the request body to
// MQL5\Files\ct_retry_<seq>.txt and drain the queue every OnTimer tick
// until the API is back. Files survive EA reload and terminal restart, so
// even an overnight outage is recoverable.
//
// File format (4 lines, ANSI):
//   line 1: url
//   line 2: body (JSON, single line)
//   line 3: first_at (Unix epoch seconds)
//   line 4: attempts (int)
//
// Drop policy: entries older than 24h are purged with a log line. By that
// point the position has likely closed, the broker view has diverged from
// the DB, and re-POSTing stale data would do more harm than good.
string _RetryFilename(long seq) {
   return "ct_retry_" + IntegerToString(seq) + ".txt";
}

long _NextRetrySeq() {
   g_retry_counter++;
   // High 48 bits: seconds since epoch. Low 16 bits: counter mod 65536.
   // Multiply chosen so a single second can hold up to 65535 retries.
   return ((long)TimeCurrent() << 16) | (long)(g_retry_counter & 0xFFFF);
}

void EnqueueRetry(string url, string body) {
   long seq = _NextRetrySeq();
   string fname = _RetryFilename(seq);
   int fh = FileOpen(fname, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE) {
      Print("CT retry: FileOpen FAILED for ", fname,
            " err=", GetLastError(), " — request will be lost");
      return;
   }
   FileWriteString(fh, url + "\n");
   FileWriteString(fh, body + "\n");
   FileWriteString(fh, IntegerToString((long)TimeCurrent()) + "\n");
   FileWriteString(fh, "0\n");
   FileClose(fh);
   Print("CT retry: queued ", fname, " url=", url);
}

void _PurgeRetry(string fname) {
   if(!FileDelete(fname))
      Print("CT retry: FileDelete failed for ", fname,
            " err=", GetLastError());
}

void DrainRetryQueue() {
   string fname;
   long handle = FileFindFirst("ct_retry_*.txt", fname);
   if(handle == INVALID_HANDLE) return;

   datetime now = TimeCurrent();
   do {
      int fh = FileOpen(fname, FILE_READ | FILE_TXT | FILE_ANSI);
      if(fh == INVALID_HANDLE) {
         Print("CT retry: read FileOpen failed ", fname,
               " err=", GetLastError());
         continue;
      }
      string url      = FileReadString(fh);
      string body     = FileReadString(fh);
      string firstStr = FileReadString(fh);
      string attStr   = FileReadString(fh);
      FileClose(fh);

      datetime first_at = (datetime)StringToInteger(firstStr);
      int attempts = (int)StringToInteger(attStr);

      // Drop-dead 24h.
      if(now - first_at > 86400) {
         Print("CT retry: EXPIRED after 24h, purging ", fname);
         _PurgeRetry(fname);
         continue;
      }

      string resp; int status;
      if(HttpPostJsonWithStatus(url, body, resp, status)) {
         Print("CT retry: SUCCESS after ", attempts + 1,
               " attempt(s), purging ", fname);
         _PurgeRetry(fname);
      } else {
         // Bump attempts and keep the entry for the next tick.
         int fhw = FileOpen(fname, FILE_WRITE | FILE_TXT | FILE_ANSI);
         if(fhw != INVALID_HANDLE) {
            FileWriteString(fhw, url + "\n");
            FileWriteString(fhw, body + "\n");
            FileWriteString(fhw, firstStr + "\n");
            FileWriteString(fhw, IntegerToString(attempts + 1) + "\n");
            FileClose(fhw);
         }
      }
   } while(FileFindNext(handle, fname));
   FileFindClose(handle);
}

void PostResult(long id, string status, long ticket, string err) {
   // Dashboard counters + last-action trail. "executed" on an OPEN counts as
   // a filled signal; "rejected"/"failed" feed the rejected stat regardless
   // of action type (CLOSE_ALL failures still matter to the operator).
   if(status == "executed" && id == g_last_action_id
      && g_last_action_type == "OPEN") g_stats_executed++;
   else if(status == "rejected" || status == "failed") g_stats_rejected++;
   if(id == g_last_action_id) {
      g_last_action_status = status;
      g_last_action_at = TimeCurrent();
   }

   string body = "{\"status\":\"" + status + "\"";
   if(ticket > 0) body += ",\"mt5_ticket\":" + IntegerToString(ticket);
   if(err != "") {
      string esc = err; StringReplace(esc, "\"", "'");
      body += ",\"error\":\"" + esc + "\"";
   }
   body += "}";
   string resp;
   HttpPostJson(ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result", body, resp);
}

// =====================================================================
// Phase 4: OPEN_INSTANT / ATTACH_SIGNAL
// =====================================================================
//
// One naked ticket at a time (single-position invariant). The g_naked[]
// array is always size 0 or 1 in normal flow; we use an array (not a
// single struct) so persistence + restart recovery is symmetric with
// g_plans[].
//
// Lifecycle:
//   OPEN_INSTANT  → opens market, computes emergency SL, appends naked entry
//   ATTACH_SIGNAL → matches ticket+side, sets real SL/TP, registers TradePlan,
//                   removes naked entry
//   Timeout       → ManageNakedPlans installs fallback TP at
//                   InstantTpMultiplier × emergency_sl_distance and trails SL
//                   InstantTrailPoints behind price (ratchet-only).
//                   naked entry stays present, fallback_armed=1, so trail
//                   continues each OnTimer tick.
//   Manual close  → ManageNakedPlans drops the entry when the ticket is gone.

struct NakedPlan {
   long     ticket;
   bool     isBuy;
   double   entry;          // actual broker fill price
   double   emergencySl;    // SL set at open (the 1%-balance-loss price)
   datetime openedAt;       // broker time of the fill
   bool     fallbackArmed;  // true once timeout TP + trail were installed
};

NakedPlan g_naked[];

string NakedKey(long ticket, string field) {
   return "ct_naked_" + IntegerToString(ticket) + "_" + field;
}

void PersistNaked(const NakedPlan &n) {
   GlobalVariableSet(NakedKey(n.ticket, "isBuy"),        n.isBuy ? 1.0 : 0.0);
   GlobalVariableSet(NakedKey(n.ticket, "entry"),        n.entry);
   GlobalVariableSet(NakedKey(n.ticket, "emergencySl"),  n.emergencySl);
   GlobalVariableSet(NakedKey(n.ticket, "openedAt"),     (double)n.openedAt);
   GlobalVariableSet(NakedKey(n.ticket, "fallback"),     n.fallbackArmed ? 1.0 : 0.0);
}

void EraseNaked(long ticket) {
   string fields[] = {"isBuy","entry","emergencySl","openedAt","fallback"};
   for(int i = 0; i < ArraySize(fields); i++)
      GlobalVariableDel(NakedKey(ticket, fields[i]));
}

int FindNakedIdx(long ticket) {
   for(int i = 0; i < ArraySize(g_naked); i++)
      if(g_naked[i].ticket == ticket) return i;
   return -1;
}

long FindSingletonNakedTicket() {
   for(int i = 0; i < ArraySize(g_naked); i++)
      if(PositionSelectByTicket(g_naked[i].ticket)) return g_naked[i].ticket;
   return 0;
}

void RemoveNaked(int idx) {
   long t = g_naked[idx].ticket;
   int n = ArraySize(g_naked);
   for(int j = idx; j < n - 1; j++) g_naked[j] = g_naked[j + 1];
   ArrayResize(g_naked, n - 1);
   EraseNaked(t);
}

// Balance-only lot sizing (no SL-distance scaling). Mirrors LotsFromRisk
// step 1 + step 3; we skip step 2 because OPEN_INSTANT has no signal SL
// to cap against.
double LotsFromBalance() {
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0) lotStep = 0.01;
   if(minLot <= 0) minLot = lotStep;
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   if(balance <= 0 || LotsPer100Balance <= 0) return minLot;
   double lots = (balance / 100.0) * LotsPer100Balance;
   lots = MathFloor(lots / lotStep) * lotStep;
   if(lots > MaxLotsPerSignal) lots = MaxLotsPerSignal;
   if(lots < minLot) lots = minLot;
   return NormalizeDouble(lots, 2);
}

// Price distance such that hitting it costs `riskPercent` of account balance
// for `lots` size. Uses tick size/value so the result is correct on any
// symbol the EA is configured for, not just XAUUSD.
double EmergencySlDistance(double lots, double riskPercent) {
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double tickSize = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_VALUE);
   if(lots <= 0 || balance <= 0 || tickSize <= 0 || tickValue <= 0 || riskPercent <= 0)
      return 0.0;
   double targetLoss = balance * riskPercent / 100.0;
   double dollarsPerUnit = tickValue / tickSize;     // $ per 1.0 price move per lot
   if(dollarsPerUnit <= 0) return 0.0;
   double distance = targetLoss / (lots * dollarsPerUnit);
   return distance;
}

void DoOpenInstant(long id, string payload) {
   if(!EnableInstantOpen) {
      PostResult(id, "rejected", 0, "instant_open_disabled");
      return;
   }
   // Single-position invariant. If anything is open (managed OR naked),
   // the AI should have emitted CLOSE_FULL first. Belt-and-suspenders: reject.
   if(CountOurOpenPositions() >= 1) {
      PostResult(id, "rejected", 0, "already_open");
      return;
   }
   string side = JsonField(payload, "side");
   bool isBuy = (side == "BUY");
   if(side != "BUY" && side != "SELL") {
      PostResult(id, "failed", 0, "invalid_side:" + side);
      return;
   }

   double lots = LotsFromBalance();
   string sizingSkipI = "";
   lots = ApplyEvalSizing(id, lots, sizingSkipI);
   if(sizingSkipI != "") {
      PostResult(id, "rejected", 0, sizingSkipI);
      g_stats_rejected++;
      return;
   }
   double price = SymbolInfoDouble(Symbol_Override, isBuy ? SYMBOL_ASK : SYMBOL_BID);
   if(price <= 0) { PostResult(id, "failed", 0, "no_price"); return; }
   double slDistance = EmergencySlDistance(lots, InstantRiskPercent);
   if(slDistance <= 0) {
      PostResult(id, "failed", 0, "emergency_sl_calc_failed");
      return;
   }
   double slPrice = isBuy ? price - slDistance : price + slDistance;
   int digits = (int)SymbolInfoInteger(Symbol_Override, SYMBOL_DIGITS);
   slPrice = NormalizeDouble(slPrice, digits);

   bool ok = isBuy
      ? trade.Buy(lots, Symbol_Override, 0.0, slPrice, 0.0, "ct-instant")
      : trade.Sell(lots, Symbol_Override, 0.0, slPrice, 0.0, "ct-instant");
   if(!ok) {
      PostResult(id, "failed", 0,
                 "instant_open_failed:" + IntegerToString(trade.ResultRetcode()));
      return;
   }
   // Position ticket (hedging mode) == opening ORDER ticket, NOT the deal
   // ticket. `ResultDeal()` returns a deal-event id that PositionSelectByTicket
   // doesn't recognize, which made ReconcileClosedPositions immediately mark
   // the freshly-opened naked row mt5_not_found, dropping the naked flag and
   // breaking the subsequent ATTACH_SIGNAL flow. Same precedence as DoOpen.
   long ticket = (long)trade.ResultOrder() != 0
                 ? (long)trade.ResultOrder()
                 : (long)trade.ResultDeal();
   // Fall back to scanning open positions for a freshly-opened ticket.
   if(ticket == 0) {
      for(int i = PositionsTotal() - 1; i >= 0; i--) {
         long t = (long)PositionGetTicket(i);
         if(PositionSelectByTicket(t)
            && PositionGetString(POSITION_SYMBOL) == Symbol_Override) {
            ticket = t;
            break;
         }
      }
   }
   if(ticket == 0) {
      PostResult(id, "failed", 0, "no_ticket_after_open");
      return;
   }

   double fillPrice = price;
   if(PositionSelectByTicket(ticket))
      fillPrice = PositionGetDouble(POSITION_PRICE_OPEN);

   NakedPlan n;
   n.ticket = ticket;
   n.isBuy = isBuy;
   n.entry = fillPrice;
   n.emergencySl = slPrice;
   n.openedAt = TimeCurrent();
   n.fallbackArmed = false;
   int existing = FindNakedIdx(ticket);
   if(existing >= 0) g_naked[existing] = n;
   else {
      int m = ArraySize(g_naked);
      ArrayResize(g_naked, m + 1);
      g_naked[m] = n;
   }
   PersistNaked(n);
   RememberRecentOpen(ticket);

   string legJson = StringFormat(
      "{\"mt5_ticket\":%I64d,\"snapshot\":{\"symbol\":\"%s\",\"side\":\"%s\","
      "\"volume\":%.2f,\"entry_price\":%.2f,\"sl\":%.2f,\"tp\":0.0,"
      "\"is_naked\":true}}",
      ticket, Symbol_Override, side, lots, fillPrice, slPrice
   );
   string body = "{\"status\":\"executed\",\"legs\":[" + legJson + "]}";
   string resp; int status;
   string resultUrl = ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result";
   bool postOk = HttpPostJsonWithStatus(resultUrl, body, resp, status);
   if(!postOk) {
      Print("Result POST failed for instant action ", id, " status=", status,
            " — queued for retry");
      EnqueueRetry(resultUrl, body);
   }
   Print("CT OPEN_INSTANT id=", id, " ticket=", ticket, " side=", side,
         " lots=", lots, " entry=", fillPrice, " emergency_sl=", slPrice,
         " (risk=", InstantRiskPercent, "% balance)");
   g_stats_executed++;
   g_last_action_status = "executed";
   g_last_action_at = TimeCurrent();
}

void DoAttachSignal(long id, string payload) {
   string side = JsonField(payload, "side");
   bool isBuy = (side == "BUY");
   double sl = StringToDouble(JsonField(payload, "sl"));
   double entryLow = StringToDouble(JsonField(payload, "entry_low"));
   double entryHigh = StringToDouble(JsonField(payload, "entry_high"));
   string tpsStr = JsonField(payload, "tps");
   double tps[];
   ParseTps(tpsStr, tps);
   int tpCount = ArraySize(tps);
   if(tpCount == 0) { PostResult(id, "failed", 0, "no_tps"); return; }
   if(tpCount > 3) tpCount = 3;

   long ticket = FindSingletonNakedTicket();
   if(ticket <= 0) {
      // No naked position to upgrade. Operator policy (2026-05-15):
      // treat this as a fresh OPEN — the channel may have skipped or
      // missed the bare directional that normally precedes ATTACH_SIGNAL.
      // DoOpen reads the same payload fields (side, entry_low/high, sl,
      // tps), so forwarding is a clean one-shot. The action_type stays
      // ATTACH_SIGNAL in the DB; only the EA branch differs.
      Print("CT ATTACH_SIGNAL id=", id,
            " no_naked_position -> forwarding to DoOpen");
      DoOpen(id, payload);
      return;
   }
   int idx = FindNakedIdx(ticket);
   if(idx < 0) {
      PostResult(id, "rejected", 0, "naked_idx_missing");
      return;
   }
   if(g_naked[idx].isBuy != isBuy) {
      // Direction mismatch — the AI should have emitted CLOSE_FULL + OPEN_INSTANT
      // (the conflict-flip path). Reject so we don't silently flip direction.
      PostResult(id, "rejected", ticket, "naked_side_mismatch");
      return;
   }
   if(!PositionSelectByTicket(ticket)) {
      PostResult(id, "failed", ticket, "naked_position_vanished");
      return;
   }

   SortTpsByDistance(tps, tpCount, isBuy);
   double tpFinal = tps[tpCount - 1];
   double entry = g_naked[idx].entry;

   // Broker minDist clamp on the signal SL. The signal can specify an SL
   // very close to (or past) the chased fill on fast markets; pass through
   // ClampStopForBroker so we don't get retcode 10016 here.
   double curExitA = isBuy ? SymbolInfoDouble(Symbol_Override, SYMBOL_BID)
                           : SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
   double clampedSl;
   bool wasClampedA;
   double requestedA = sl;
   if(!ClampStopForBroker(isBuy, sl, curExitA, clampedSl, wasClampedA)) {
      PostResult(id, "rejected", ticket,
                 StringFormat("invalid_stop_attach: sl=%.2f vs price=%.2f",
                              sl, curExitA));
      return;
   }
   sl = clampedSl;

   if(!trade.PositionModify(ticket, sl, tpFinal)) {
      PostResult(id, "failed", ticket,
                 "attach_modify_failed:" + IntegerToString(trade.ResultRetcode()));
      return;
   }

   // Register a staged plan as if the position had opened from a normal OPEN.
   // entry stays the chased fill (g_naked[idx].entry); slOrig becomes the
   // signal SL — used by SignalAnchorSl as the "loosen guard".
   if(tpCount >= 2) {
      RegisterPlan(ticket, isBuy, PositionGetDouble(POSITION_VOLUME),
                   entry, sl, entryLow, entryHigh, tps, tpCount);
   }

   // POST attach_signal — DB clears is_naked, records sl/tp.
   string body = StringFormat("{\"sl\":%.2f,\"tp\":%.2f}", sl, tpFinal);
   string resp;
   HttpPostJson(ApiBaseUrl + "/positions/" + IntegerToString(ticket)
                + "/attach_signal", body, resp);

   RemoveNaked(idx);
   string attachNote = wasClampedA
      ? StringFormat("clamped sl %.2f->%.2f (broker minDist)", requestedA, sl)
      : "";
   PostResult(id, "executed", ticket, attachNote);
   Print("CT ATTACH_SIGNAL id=", id, " ticket=", ticket, " side=", side,
         " sl=", sl, " tpFinal=", tpFinal, " tpCount=", tpCount,
         (wasClampedA ? " (sl clamped)" : ""));
}

void ManageNakedPlans() {
   if(ArraySize(g_naked) == 0) return;
   datetime now = TimeCurrent();
   int timeoutSec = InstantTimeoutMinutes * 60;
   double pointSize = SymbolInfoDouble(Symbol_Override, SYMBOL_POINT);
   if(pointSize <= 0) pointSize = 0.01;
   double trailDist = InstantTrailPoints * pointSize;
   long stopsLevel = SymbolInfoInteger(Symbol_Override, SYMBOL_TRADE_STOPS_LEVEL);
   double minDist = (double)stopsLevel * pointSize;
   int digits = (int)SymbolInfoInteger(Symbol_Override, SYMBOL_DIGITS);

   for(int i = ArraySize(g_naked) - 1; i >= 0; i--) {
      NakedPlan p = g_naked[i];
      if(!PositionSelectByTicket(p.ticket)) {
         // Closed (SL hit, manual close, etc.). Drop the entry.
         RemoveNaked(i);
         continue;
      }
      int age = (int)(now - p.openedAt);
      if(!p.fallbackArmed) {
         if(age < timeoutSec) continue;
         // Timeout fired. Install fallback TP at InstantTpMultiplier ×
         // emergency-SL distance from entry; leave SL where it is.
         double slDist = MathAbs(p.entry - p.emergencySl);
         double tpDist = slDist * InstantTpMultiplier;
         double newTp = p.isBuy ? p.entry + tpDist : p.entry - tpDist;
         newTp = NormalizeDouble(newTp, digits);
         double curSl = PositionGetDouble(POSITION_SL);
         if(trade.PositionModify(p.ticket, curSl, newTp)) {
            p.fallbackArmed = true;
            g_naked[i] = p;
            PersistNaked(p);
            Print("CT naked timeout fallback ticket=", p.ticket,
                  " tp=", newTp, " (", InstantTpMultiplier,
                  "x emergency_sl_distance ", slDist, ")");
            string alertBody = StringFormat(
               "{\"level\":\"warning\",\"text\":\"naked timeout ticket=%I64d "
               "after %d min; fallback TP=%.2f, trailing SL @ %d pts now active. "
               "No structured signal arrived.\"}",
               p.ticket, InstantTimeoutMinutes, newTp, InstantTrailPoints);
            string ar;
            HttpPostJson(ApiBaseUrl + "/alerts", alertBody, ar);
         } else {
            Print("CT naked timeout fallback FAILED ticket=", p.ticket,
                  " retcode=", trade.ResultRetcode());
         }
         continue;
      }
      // Already armed — trail SL @ InstantTrailPoints behind current price,
      // ratchet-only. Same pattern as TrailStage2Sls.
      double bid = SymbolInfoDouble(Symbol_Override, SYMBOL_BID);
      double ask = SymbolInfoDouble(Symbol_Override, SYMBOL_ASK);
      if(bid <= 0.0 || ask <= 0.0) continue;
      double curSl = PositionGetDouble(POSITION_SL);
      double curTp = PositionGetDouble(POSITION_TP);
      double newSl = 0.0;
      if(p.isBuy) {
         double slFloor = bid - trailDist;
         if(bid - slFloor < minDist) slFloor = bid - minDist;
         if(slFloor <= curSl + pointSize * 5.0) continue;
         newSl = NormalizeDouble(slFloor, digits);
      } else {
         double slCeil = ask + trailDist;
         if(slCeil - ask < minDist) slCeil = ask + minDist;
         if(slCeil >= curSl - pointSize * 5.0) continue;
         newSl = NormalizeDouble(slCeil, digits);
      }
      if(trade.PositionModify(p.ticket, newSl, curTp)) {
         PostPositionUpdate(p.ticket, 0, newSl);
         Print("CT naked trail ticket=", p.ticket, " sl=", newSl);
      }
   }
}

void LoadPersistedNaked() {
   int total = GlobalVariablesTotal();
   for(int i = 0; i < total; i++) {
      string name = GlobalVariableName(i);
      if(StringFind(name, "ct_naked_") != 0) continue;
      if(StringFind(name, "_isBuy") < 0) continue;
      // ct_naked_<ticket>_isBuy → extract ticket
      string rest = StringSubstr(name, 9);  // strip "ct_naked_"
      int us = StringFind(rest, "_");
      if(us <= 0) continue;
      string ticketStr = StringSubstr(rest, 0, us);
      long ticket = StringToInteger(ticketStr);
      if(ticket <= 0) continue;
      if(!PositionSelectByTicket(ticket)) {
         EraseNaked(ticket);
         continue;
      }
      NakedPlan n;
      n.ticket = ticket;
      n.isBuy        = GlobalVariableGet(NakedKey(ticket, "isBuy")) > 0.5;
      n.entry        = GlobalVariableGet(NakedKey(ticket, "entry"));
      n.emergencySl  = GlobalVariableGet(NakedKey(ticket, "emergencySl"));
      n.openedAt     = (datetime)GlobalVariableGet(NakedKey(ticket, "openedAt"));
      n.fallbackArmed = GlobalVariableGet(NakedKey(ticket, "fallback")) > 0.5;
      int m = ArraySize(g_naked);
      ArrayResize(g_naked, m + 1);
      g_naked[m] = n;
      Print("CT restored naked ticket=", ticket, " isBuy=", n.isBuy,
            " armed=", n.fallbackArmed);
   }
}

// =====================================================================
// Phase 5: pending-limit pipeline
// =====================================================================

string PendingKey(ulong order_ticket, string field) {
   return "ct_pending_" + IntegerToString((long)order_ticket) + "_" + field;
}

void PersistPendingOrder(const PendingOrder &p) {
   GlobalVariableSet(PendingKey(p.order_ticket, "actionId"), (double)p.action_id);
   GlobalVariableSet(PendingKey(p.order_ticket, "isBuy"),    p.isBuy ? 1.0 : 0.0);
   GlobalVariableSet(PendingKey(p.order_ticket, "entry"),    p.entry);
   GlobalVariableSet(PendingKey(p.order_ticket, "sl"),       p.sl);
   GlobalVariableSet(PendingKey(p.order_ticket, "tp1"),      p.tps[0]);
   GlobalVariableSet(PendingKey(p.order_ticket, "tp2"),      p.tps[1]);
   GlobalVariableSet(PendingKey(p.order_ticket, "tp3"),      p.tps[2]);
   GlobalVariableSet(PendingKey(p.order_ticket, "tpCount"),  (double)p.tpCount);
   GlobalVariableSet(PendingKey(p.order_ticket, "placedAt"), (double)p.placedAt);
}

void ErasePendingOrderState(ulong order_ticket) {
   string fields[] = {"actionId","isBuy","entry","sl","tp1","tp2","tp3",
                      "tpCount","placedAt"};
   for(int i = 0; i < ArraySize(fields); i++)
      GlobalVariableDel(PendingKey(order_ticket, fields[i]));
}

void RemovePendingOrder(int idx) {
   int n = ArraySize(g_pending_orders);
   for(int j = idx; j < n - 1; j++) g_pending_orders[j] = g_pending_orders[j + 1];
   ArrayResize(g_pending_orders, n - 1);
}

void LoadPersistedPendingOrders() {
   int total = GlobalVariablesTotal();
   for(int i = 0; i < total; i++) {
      string name = GlobalVariableName(i);
      if(StringFind(name, "ct_pending_") != 0) continue;
      if(StringFind(name, "_actionId") < 0) continue;
      // ct_pending_<orderTicket>_actionId
      string rest = StringSubstr(name, 11);  // strip "ct_pending_"
      int us = StringFind(rest, "_");
      if(us <= 0) continue;
      string ticketStr = StringSubstr(rest, 0, us);
      ulong order_ticket = (ulong)StringToInteger(ticketStr);
      if(order_ticket == 0) continue;
      // If the broker order is gone (filled or deleted while EA was down),
      // wipe the GVs and let the API's expire-stale-watches sweeper deal
      // with the orphan watching row.
      if(!OrderSelect(order_ticket)) {
         ErasePendingOrderState(order_ticket);
         continue;
      }
      PendingOrder p;
      p.action_id    = (long)GlobalVariableGet(PendingKey(order_ticket, "actionId"));
      p.order_ticket = order_ticket;
      p.isBuy        = GlobalVariableGet(PendingKey(order_ticket, "isBuy")) > 0.5;
      p.entry        = GlobalVariableGet(PendingKey(order_ticket, "entry"));
      p.sl           = GlobalVariableGet(PendingKey(order_ticket, "sl"));
      p.tps[0]       = GlobalVariableGet(PendingKey(order_ticket, "tp1"));
      p.tps[1]       = GlobalVariableGet(PendingKey(order_ticket, "tp2"));
      p.tps[2]       = GlobalVariableGet(PendingKey(order_ticket, "tp3"));
      p.tpCount      = (int)GlobalVariableGet(PendingKey(order_ticket, "tpCount"));
      p.placedAt     = (datetime)GlobalVariableGet(PendingKey(order_ticket, "placedAt"));
      p.lastStatusCheck = 0;  // force first OnTimer to do a status poll
      int m = ArraySize(g_pending_orders);
      ArrayResize(g_pending_orders, m + 1);
      g_pending_orders[m] = p;
      Print("CT restored pending order ticket=", order_ticket,
            " action=", p.action_id);
   }
}

void ManagePendingOrders() {
   if(ArraySize(g_pending_orders) == 0) return;
   datetime now = TimeCurrent();

   for(int i = ArraySize(g_pending_orders) - 1; i >= 0; i--) {
      PendingOrder p = g_pending_orders[i];

      // 1. Did the order convert to a position? On MT5 the position
      //    ticket equals the opening order ticket, so the same value
      //    we stored as order_ticket is now a valid position ticket.
      if(PositionSelectByTicket((long)p.order_ticket)) {
         double vol       = PositionGetDouble(POSITION_VOLUME);
         double fillPrice = PositionGetDouble(POSITION_PRICE_OPEN);
         double tpFinal   = p.tps[p.tpCount - 1];
         string sideStr   = p.isBuy ? "BUY" : "SELL";

         string legJson = StringFormat(
            "{\"mt5_ticket\":%I64d,\"snapshot\":{\"symbol\":\"%s\","
            "\"side\":\"%s\",\"volume\":%.2f,\"entry_price\":%.2f,"
            "\"sl\":%.2f,\"tp\":%.2f}}",
            (long)p.order_ticket, Symbol_Override, sideStr, vol,
            fillPrice, p.sl, tpFinal);
         string body = "{\"status\":\"executed\",\"legs\":[" + legJson + "]}";
         string resp; int httpStatus;
         string resultUrl = ApiBaseUrl + "/actions/" + IntegerToString(p.action_id)
                          + "/result";
         bool ok = HttpPostJsonWithStatus(resultUrl, body, resp, httpStatus);
         if(!ok) {
            Print("Result POST failed for filled pending action ",
                  p.action_id, " status=", httpStatus, " — queued for retry");
            EnqueueRetry(resultUrl, body);
         }

         // Multi-TP: register the staged plan so ManagePlans takes over.
         if(p.tpCount >= 2) {
            double tpsArr[];
            ArrayResize(tpsArr, p.tpCount);
            for(int k = 0; k < p.tpCount; k++) tpsArr[k] = p.tps[k];
            RegisterPlan((long)p.order_ticket, p.isBuy, vol, fillPrice, p.sl,
                         p.entry, p.entry, tpsArr, p.tpCount);
         }

         RememberRecentOpen((long)p.order_ticket);
         ErasePendingOrderState(p.order_ticket);
         RemovePendingOrder(i);
         g_stats_executed++;
         Print("CT pending FILLED action=", p.action_id,
               " ticket=", p.order_ticket, " entry=", fillPrice);
         continue;
      }

      // 2. Order still pending at MT5?
      if(!OrderSelect(p.order_ticket)) {
         // Order disappeared (no position). Broker-side cancellation,
         // expiry, or operator delete from MT5 UI. POST rejected.
         PostResult(p.action_id, "rejected", 0, "pending_order_disappeared");
         ErasePendingOrderState(p.order_ticket);
         RemovePendingOrder(i);
         Print("CT pending DISAPPEARED action=", p.action_id,
               " ticket=", p.order_ticket);
         continue;
      }

      // 3. Server-side cancellation? Throttled poll (every 10s) to avoid
      //    hammering the API.
      if(now - p.lastStatusCheck < 10) continue;
      p.lastStatusCheck = now;
      g_pending_orders[i] = p;

      string statusBody;
      string statusUrl = ApiBaseUrl + "/actions/" + IntegerToString(p.action_id);
      if(!HttpGet(statusUrl, statusBody) || statusBody == "") continue;
      if(StringFind(statusBody, "\"status\":\"rejected\"") >= 0) {
         // Server cancelled (CANCEL_PENDING). Delete broker pending order.
         if(trade.OrderDelete(p.order_ticket)) {
            Print("CT pending CANCELLED by server action=", p.action_id,
                  " ticket=", p.order_ticket);
         } else {
            Print("CT pending OrderDelete FAILED action=", p.action_id,
                  " ticket=", p.order_ticket,
                  " retcode=", trade.ResultRetcode());
         }
         ErasePendingOrderState(p.order_ticket);
         RemovePendingOrder(i);
      }
   }
}
