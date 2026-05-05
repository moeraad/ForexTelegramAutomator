//+------------------------------------------------------------------+
//|  CopyTrades.mq5 — polls FastAPI bridge for actions, executes     |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>
#include "Dashboard.mqh"

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
input int    SlippagePoints          = 50;
input string Symbol_Override         = "XAUUSD";
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
// Market-price heartbeat: every N seconds, POST current bid/ask to the API
// so the AI prompt has a fresh quote for two-digit SL shorthand decoding
// (e.g. "ستوبك 56" -> 4856 only when gold is around 4850). Set to 0 to
// disable. Heartbeat is unconditional (runs even when kill switch is on).
input int    MarketPriceHeartbeatSec = 15;

CTrade trade;

// ---- Dashboard state (read by g_dashboard.Update each second) ----
CDashboard g_dashboard;
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
   double   tps[3];
   int      tpCount;
   int      stage;          // 0 initial, 1 after tp1, 2 after tp2
   int      stage_attempts; // failed PositionClosePartial attempts on the
                            // CURRENT stage. Reset on stage advance. If it
                            // hits PartialMaxRetries we give up and advance
                            // anyway. Not persisted; reset on EA restart.
};
TradePlan g_plans[];

// Sequence counter for retry-queue file names. Combined with TimeCurrent()
// to disambiguate multiple EnqueueRetry calls within the same second so
// each pending retry has a unique filename in MQL5\Files.
int g_retry_counter = 0;

int OnInit() {
   trade.SetExpertMagicNumber(919191);
   trade.SetDeviationInPoints(SlippagePoints);
   EventSetTimer(PollIntervalSec);
   LoadPersistedPlans();
   g_ea_start = TimeCurrent();
   g_balance_open_today = AccountInfoDouble(ACCOUNT_BALANCE);
   g_equity_peak_today = AccountInfoDouble(ACCOUNT_EQUITY);
   if(ShowDashboard) g_dashboard.Create(DashboardX, DashboardY);

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
}

void OnTimer() {
   RolloverDayIfNeeded();
   g_kill_switch_cached = KillSwitchOn();
   if(!g_kill_switch_cached) {
      PollAndExecute();
      ManagePlans();  // also driven by OnTick; belt-and-suspenders
      ReconcileClosedPositions();
      DrainRetryQueue();  // resend any persisted POSTs from a prior outage
   }
   // Heartbeat market price unconditionally (even when halted) — the AI
   // still needs a fresh quote to decode shorthand SL on incoming messages.
   HeartbeatMarketPrice();
   if(ShowDashboard) {
      DashboardStats s;
      BuildStats(s);
      g_dashboard.Update(s);
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
      if(PositionGetInteger(POSITION_MAGIC) != 919191) continue;
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
      if(PositionGetInteger(POSITION_MAGIC) != 919191) continue;
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
   outBody = CharArrayToString(result);
   return true;
}

bool HttpPostJson(string url, string jsonBody, string &outBody) {
   int status;
   return HttpPostJsonWithStatus(url, jsonBody, outBody, status);
}

bool HttpPostJsonWithStatus(string url, string jsonBody, string &outBody, int &outStatus) {
   char post[]; char result[];
   string reqHeaders = "Content-Type: application/json\r\n" + AuthHeader();
   string respHeaders;
   StringToCharArray(jsonBody, post, 0, StringLen(jsonBody));
   ArrayResize(post, StringLen(jsonBody));
   int res = WebRequest("POST", url, reqHeaders, 5000, post, result, respHeaders);
   if(res == -1) {
      Print("WebRequest POST error ", GetLastError(), " url=", url);
      outStatus = -1;
      return false;
   }
   outStatus = res;
   outBody = CharArrayToString(result);
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

// Balance-based position sizing.
//
//   lots = (ACCOUNT_BALANCE / 100) * LotsPer100Balance
//
// Independent of SL distance, so the dollar risk per trade scales linearly
// with the signal's SL placement (a wide-SL signal risks more dollars than
// a tight-SL signal at the same lot size). The MaxLotsPerSignal input is
// the hard upper cap regardless of balance.
//
// `slPrice` and `entryPrice` parameters are kept in the signature for
// future flexibility but are no longer used by the formula.
double LotsFromRisk(double slPrice, double entryPrice) {
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
   double result = NormalizeDouble(lots, 2);
   Print("CT LotsFromRisk(balance): balance=", balance,
         " ratio=", LotsPer100Balance, " per $100 -> lots=", result,
         " (cap=", MaxLotsPerSignal, " step=", lotStep, " min=", minLot, ")");
   return result;
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

   // All fills here are market orders (out-of-margin signals rejected
   // earlier). Multi-TP signals get a staged plan for tp1/tp2 partials.
   if(tpCount >= 2) {
      RegisterPlan(ticket, isBuy, lotsTotal, entry, sl, tps, tpCount);
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
                  double sl, double &tps[], int tpCount) {
   TradePlan p;
   p.ticket = ticket;
   p.isBuy = isBuy;
   p.origLots = origLots;
   p.entry = entry;
   p.slOrig = sl;
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
   GlobalVariableSet(PlanKey(p.ticket, "tpCount"),  (double)p.tpCount);
   GlobalVariableSet(PlanKey(p.ticket, "tp1"),      p.tps[0]);
   GlobalVariableSet(PlanKey(p.ticket, "tp2"),      p.tps[1]);
   GlobalVariableSet(PlanKey(p.ticket, "tp3"),      p.tps[2]);
}

void ErasePersistedPlan(long ticket) {
   string fields[] = {"stage","isBuy","origLots","entry","slOrig","tpCount","tp1","tp2","tp3"};
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

void PostPositionUpdate(long ticket, double newVolume, double newSl) {
   string body = "{";
   bool first = true;
   if(newVolume > 0) {
      body += "\"volume\":" + DoubleToString(newVolume, 2);
      first = false;
   }
   if(newSl > 0) {
      if(!first) body += ",";
      body += "\"sl\":" + DoubleToString(newSl, 5);
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

      // ---- Staged SL-management policy --------------------------------
      // 1-TP signal: no plan registered (RegisterPlan gate in DoOpen). The
      //              broker-set SL + TP rides the position to closure;
      //              EA never touches it.
      // 2-TP signal: TP1 closes 1/2; SL stays. TP2 = broker-set final TP
      //              auto-closes the other 1/2. SL never moves.
      // 3-TP signal: TP1 closes 1/3; SL stays. TP2 closes 1/3; SL moves
      //              to entry (Break-Even). TP3 = broker-set final TP
      //              auto-closes the last 1/3 (with BE protecting it).
      // -----------------------------------------------------------------

      if(p.stage == 0) {
         double tp1 = p.tps[0];
         bool hit = p.isBuy ? (exitPrice >= tp1) : (exitPrice <= tp1);
         if(!hit) continue;

         // Close 1/tpCount of ORIGINAL lots: 50% for 2-TP, ~33% for 3-TP.
         double frac = 1.0 / (double)p.tpCount;
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
            // Verify-then-advance: CTrade.PositionClosePartial can return
            // false in pre-OrderSend validation while ResultRetcode is a
            // stale success code from an earlier call. Trust the volume
            // diff, not the bool return.
            bool ctradeOk = trade.PositionClosePartial(p.ticket, closeLots);
            double volAfter = PositionSelectByTicket(p.ticket)
                              ? PositionGetDouble(POSITION_VOLUME) : 0.0;
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

         // SL stays put on TP1. The original SL keeps protecting the
         // remaining position until TP2 (or the broker-set final TP) is
         // reached.
         Print("CT plan stage1 SL unchanged ticket=", p.ticket,
               " sl=", DoubleToString(p.slOrig, 5));

         p.stage = 1;
         p.stage_attempts = 0;  // reset retry counter for next stage
         g_plans[i] = p;
         GlobalVariableSet(PlanKey(p.ticket, "stage"), (double)p.stage);
         double postVol = PositionSelectByTicket(p.ticket)
                          ? PositionGetDouble(POSITION_VOLUME) : 0.0;
         // newSl=0 -> server-side SL is left as-is.
         PostPositionUpdate(p.ticket, postVol, 0.0);
      }
      else if(p.stage == 1 && p.tpCount == 3) {
         double tp2 = p.tps[1];
         bool hit = p.isBuy ? (exitPrice >= tp2) : (exitPrice <= tp2);
         if(!hit) continue;

         // Close another 1/3 of ORIGINAL lots.
         double closeLots = MathFloor((p.origLots / 3.0) / lotStep) * lotStep;
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
            bool ctradeOk = trade.PositionClosePartial(p.ticket, closeLots);
            double volAfter = PositionSelectByTicket(p.ticket)
                              ? PositionGetDouble(POSITION_VOLUME) : 0.0;
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

         // Move SL to entry (Break-Even), keep TP at tp3.
         if(!trade.PositionModify(p.ticket, p.entry, p.tps[2]))
            Print("CT plan stage2 SL->BE FAILED ticket=", p.ticket,
                  " retcode=", trade.ResultRetcode(),
                  " last_err=", GetLastError());
         else
            Print("CT plan stage2 SL->BE ok ticket=", p.ticket);

         p.stage = 2;
         p.stage_attempts = 0;
         g_plans[i] = p;
         GlobalVariableSet(PlanKey(p.ticket, "stage"), (double)p.stage);
         double postVol2 = PositionSelectByTicket(p.ticket)
                           ? PositionGetDouble(POSITION_VOLUME) : 0.0;
         PostPositionUpdate(p.ticket, postVol2, p.entry);
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
                   "{\"reason\":\"ai_close\"}", body);
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
                      "{\"reason\":\"close_all\"}", body);
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
      if(PositionGetInteger(POSITION_MAGIC) == 919191) n++;
   }
   return n;
}

// ---- Phase 2: management actions for the singleton open position ------
//
// Single-position mode: the channel sends instructions like "أمن دخولك"
// without a ticket, because there's at most one open trade at a time.
// These helpers + handlers resolve "the open position" implicitly.

// Returns the ticket of our (magic-919191) open position on `symbol`, or 0
// if none. If multiple are open (shouldn't happen in single-position mode
// but guard anyway), returns the first encountered.
long FindSingletonOpenTicket(string symbol) {
   for(int i = 0; i < PositionsTotal(); i++) {
      ulong t = PositionGetTicket(i);
      if(t == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != 919191) continue;
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
   long ticket = FindSingletonOpenTicket(Symbol_Override);
   if(ticket <= 0) { PostResult(id, "rejected", 0, "no_open_position"); return; }
   if(!PositionSelectByTicket(ticket)) { PostResult(id, "failed", ticket, "no_position"); return; }
   double entry = PositionGetDouble(POSITION_PRICE_OPEN);
   double curTp = PositionGetDouble(POSITION_TP);
   if(trade.PositionModify(ticket, entry, curTp)) {
      RemovePlanByTicket(ticket);  // operator override
      PostPositionUpdate(ticket, 0, entry);
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
   double newSl = StringToDouble(JsonField(payload, "price"));
   if(newSl <= 0) { PostResult(id, "failed", ticket, "invalid_price"); return; }
   double curTp = PositionGetDouble(POSITION_TP);
   if(trade.PositionModify(ticket, newSl, curTp)) {
      RemovePlanByTicket(ticket);
      PostPositionUpdate(ticket, 0, newSl);
      PostResult(id, "executed", ticket, "");
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
   RegisterPlan(ticket, isBuy, curVol, entry, curSl, tps, tpCount);

   PostPositionUpdate(ticket, curVol, 0);    // newSl=0 → leave DB SL as-is (set by preceding MOVE_SL)
   PostResult(id, "executed", ticket,
              StringFormat("tps=%d final=%.2f", tpCount, finalTp));
}

void DoClosePartial(long id, string payload) {
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
   // Verify-then-advance: CTrade::PositionClosePartial can return false in
   // pre-OrderSend validation (hedging-mode quirks, transient stops-level
   // violations) while ResultRetcode() reports a stale success code from
   // an earlier call. Trust the volume diff, not the bool. Same pattern as
   // ManagePlans stage 0 (see lines ~832-835).
   double volBefore = PositionGetDouble(POSITION_VOLUME);
   trade.PositionClosePartial(ticket, closeLots);
   double volAfter = PositionSelectByTicket(ticket)
                     ? PositionGetDouble(POSITION_VOLUME) : 0.0;
   bool partialOk = (volAfter < volBefore - lotStep / 2.0);
   if(partialOk) {
      PostPositionUpdate(ticket, volAfter, 0);
      PostResult(id, "executed", ticket,
                 StringFormat("closed=%.2f vol=%.2f->%.2f",
                              closeLots, volBefore, volAfter));
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
                   "{\"reason\":\"ai_close_full\"}", body);
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
            "{\"reason\":\"reinforce\"}", closeBody);
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
   if(trade.PositionModify(ticket, newSl, curTp)) {
      RemovePlanByTicket(ticket);
      PostPositionUpdate(ticket, 0, newSl);
      PostResult(id, "executed", ticket,
                 StringFormat("sl_was=%.2f sl_now=%.2f", curSl, newSl));
   } else {
      PostResult(id, "failed", ticket,
                 "modify_failed:" + IntegerToString(trade.ResultRetcode()));
   }
}

void ReconcileClosedPositions() {
   // Runs every OnTimer tick (PollIntervalSec, default 1s). Two passes:
   //  1) Scan recent MT5 history (48h) for closing deals on our magic and
   //     POST close. 48h covers overnight closes + typical EA downtime.
   //  2) Ask the API which tickets it still thinks are open; for any the
   //     EA can't select as a live position, POST close(mt5_not_found).
   datetime now = TimeCurrent();
   datetime since = now - 48 * 3600;
   HistorySelect(since, now);
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++) {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0) continue;
      if(HistoryDealGetInteger(dealTicket, DEAL_MAGIC) != 919191) continue;
      if(HistoryDealGetInteger(dealTicket, DEAL_ENTRY) != DEAL_ENTRY_OUT) continue;
      ulong posId = HistoryDealGetInteger(dealTicket, DEAL_POSITION_ID);
      string url = ApiBaseUrl + "/positions/" + IntegerToString(posId) + "/close";
      string resp;
      HttpPostJson(url, "{\"reason\":\"mt5_close\"}", resp);
   }

   // --- Authoritative pass: ask the API which tickets it still thinks are
   // open. Any ticket MT5 can't select as an open position gets closed with
   // mt5_not_found. Catches positions that closed outside the 48h window,
   // manual closes the EA missed, and anything stuck from past bugs. ---
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
      if(!PositionSelectByTicket(ticket)) {
         string url = ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/close";
         string resp;
         HttpPostJson(url, "{\"reason\":\"mt5_not_found\"}", resp);
         Print("CT reconcile: ticket=", ticket,
               " absent from MT5 → POSTed close (mt5_not_found)");
      }
   }
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
