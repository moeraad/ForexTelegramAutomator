//+------------------------------------------------------------------+
//|  CopyTrades.mq5 — polls FastAPI bridge for actions, executes     |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>
#include "Dashboard.mqh"

input string ApiBaseUrl              = "http://127.0.0.1:8765";
input int    PollIntervalSec         = 1;
input double RiskPercentPerTrade     = 1.0;
input double MaxLotsPerSignal        = 0.50;
input int    MaxOpenPositions        = 3;
input int    EntryZoneMode           = 1;   // 0=midpoint limit, 1=market if in zone
input int    SlippagePoints          = 50;
input string Symbol_Override         = "XAUUSD";
// Effective entry zone is widened by this many price units on EACH side
// of [entry_low, entry_high] before the in-zone check. Lets a signal that
// arrived a few ticks late still fill at market instead of being rejected.
// Set to 0 to require strict in-zone fills.
input double EntryPriceMargin        = 5.0;
// LEGACY: synthetic-watch path is OFF by default. Enabling it restores the
// old behavior where out-of-zone signals POST status='watching' and wait
// for price to re-enter the zone. See DoOpen for the modern flow:
// in-zone (incl. margin) -> market; chase-eligible -> market; otherwise
// rejected with reason 'price_outside_margin'.
input bool   SyntheticLimitEnabled   = false;
input int    WatchExpiryHours        = 24;    // drop a watch this many hours after creation if untriggered
// Chase-price: if price has already moved past the entry zone (BUY above
// entry_high, SELL below entry_low) but the last TP is still far enough
// ahead, open at current market instead of waiting for a pullback. Guards
// against missing fast breakouts while keeping R:R sane.
input bool   ChasePriceEnabled       = true;
input double ChaseMinRewardRatio     = 0.5;   // require remaining/original >= this; e.g. 0.5 = >=50% of move still ahead
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
   int      stage;      // 0 initial, 1 after tp1, 2 after tp2
};
TradePlan g_plans[];

// Synthetic pending limit: when price is outside the entry zone, the EA does
// NOT place a native BuyLimit/SellLimit. Instead it POSTs status=watching to
// the API and keeps a local watch. When price enters the zone, the EA opens
// at market and POSTs executed. The DB is the source of truth: on OnInit we
// rebuild g_watches from GET /actions?status=watching so restarts don't drop
// pending zones, and the bot can cancel a watch via its UI.
struct WatchEntry {
   long     action_id;
   bool     isBuy;
   double   zone_low;
   double   zone_high;
   double   sl;
   double   tps[3];
   int      tpCount;
   string   expires_at_iso;  // ISO-8601 UTC; compared as string (lexicographic == chronological)
};
WatchEntry g_watches[];

int OnInit() {
   trade.SetExpertMagicNumber(919191);
   trade.SetDeviationInPoints(SlippagePoints);
   EventSetTimer(PollIntervalSec);
   LoadPersistedPlans();
   LoadPersistedWatches();
   g_ea_start = TimeCurrent();
   g_balance_open_today = AccountInfoDouble(ACCOUNT_BALANCE);
   g_equity_peak_today = AccountInfoDouble(ACCOUNT_EQUITY);
   if(ShowDashboard) g_dashboard.Create(DashboardX, DashboardY);
   Print("CopyTrades EA started. API=", ApiBaseUrl,
         " restored_plans=", ArraySize(g_plans),
         " restored_watches=", ArraySize(g_watches));
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
      ManageWatches();
      ReconcileClosedPositions();
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
   s.watches        = ArraySize(g_watches);
   s.lots_deployed  = lots;
   s.lots_cap       = MaxLotsPerSignal * MaxOpenPositions;
   s.max_positions  = MaxOpenPositions;
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
   ManageWatches();
}

bool KillSwitchOn() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/settings/kill_switch", body)) return false;
   return StringFind(body, "\"value\":\"on\"") >= 0;
}

void PollAndExecute() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=sent", body)) return;
   g_last_api_ok = TimeCurrent();
   // Minimal JSON parse: find action objects
   ProcessActionsJson(body);
}

// ---- HTTP helpers ----
bool HttpGet(string url, string &outBody) {
   char post[]; char result[]; string headers;
   int res = WebRequest("GET", url, "", "", 5000, post, 0, result, headers);
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
   string reqHeaders = "Content-Type: application/json\r\n";
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

   if(CountOurOpenPositions() >= MaxOpenPositions && atype == "OPEN") {
      PostResult(id, "rejected", 0, "max_positions");
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

// Broker-authoritative risk sizing.
//
// We previously used SYMBOL_TRADE_TICK_VALUE × SYMBOL_TRADE_TICK_SIZE math
// to convert riskCash → lots. That worked on some brokers and produced 10×
// the correct lot size on others, because XAUUSD tick values are reported
// in inconsistent scales (per-oz, per-100-oz, account-currency-converted,
// etc.) — the same broker quirk that made the dashboard P&L projection
// 10× too low until it was switched to OrderCalcProfit.
//
// OrderCalcProfit returns the broker-authoritative dollar P&L for a given
// 1.0-lot trade between two prices. We invert it: lots = riskCash / lossPerLot.
// This produces correct sizing on every broker without needing to know
// what scale they report tick values in.
double LotsFromRisk(double slPrice, double entryPrice) {
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   if(lotStep <= 0) lotStep = 0.01;
   if(minLot <= 0) minLot = lotStep;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * (RiskPercentPerTrade / 100.0);
   double dist = MathAbs(entryPrice - slPrice);
   if(dist <= 0 || riskCash <= 0) return minLot;

   ENUM_ORDER_TYPE otype = (entryPrice > slPrice) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double lossPerLot = 0.0;
   if(!OrderCalcProfit(otype, Symbol_Override, 1.0, entryPrice, slPrice, lossPerLot)) {
      Print("CT LotsFromRisk: OrderCalcProfit failed; falling back to minLot=",
            minLot, " err=", GetLastError());
      return minLot;
   }
   lossPerLot = MathAbs(lossPerLot);  // negative for losses; we want magnitude
   if(lossPerLot <= 0) {
      Print("CT LotsFromRisk: lossPerLot=0 from OrderCalcProfit; falling back to minLot");
      return minLot;
   }

   double lots = riskCash / lossPerLot;
   lots = MathFloor(lots / lotStep) * lotStep;
   if(lots > MaxLotsPerSignal) lots = MaxLotsPerSignal;
   if(lots < minLot) lots = minLot;
   double result = NormalizeDouble(lots, 2);
   Print("CT LotsFromRisk: equity=", equity, " risk%=", RiskPercentPerTrade,
         " riskCash=", riskCash, " entry=", entryPrice, " sl=", slPrice,
         " dist=", dist, " lossPerLot=", lossPerLot, " -> lots=", result,
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
   bool useMarket = (EntryZoneMode == 1 && inZone);
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

   // LEGACY: opt-in synthetic-watch path. Off by default. Enabling
   // SyntheticLimitEnabled restores the pre-margin behavior where the EA
   // POSTs status='watching' and waits for price to re-enter the zone.
   // Most users want the explicit reject below instead — fewer surprises
   // when a signal arrives too late.
   if(!useMarket && SyntheticLimitEnabled) {
      StartWatch(id, isBuy, entryLow, entryHigh, sl, tps, tpCount);
      return;
   }

   // Out of effective zone, chase didn't fire, watch disabled → reject.
   // No more pending-limit fallback: the channel's signal has aged past
   // the margin; firing later would risk filling at a price the analyst
   // never intended.
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
   bool postOk = HttpPostJsonWithStatus(
      ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result", body, resp, status);
   if(!postOk) {
      Print("Result POST failed for action ", id, " status=", status,
            " — sweeper will release after timeout");
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
   HttpPostJsonWithStatus(
      ApiBaseUrl + "/positions/" + IntegerToString(ticket) + "/update",
      body, resp, status);
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
         double currentVol = PositionGetDouble(POSITION_VOLUME);
         double remaining = currentVol - closeLots;

         if(closeLots >= minLot && remaining >= minLot) {
            if(!trade.PositionClosePartial(p.ticket, closeLots))
               Print("CT plan stage1 partial FAILED ticket=", p.ticket,
                     " lots=", closeLots, " code=", trade.ResultRetcode());
            else
               Print("CT plan stage1 partial ok ticket=", p.ticket, " lots=", closeLots);
         } else {
            Print("CT plan stage1 partial skipped (lot size) ticket=", p.ticket,
                  " want=", closeLots, " remain=", remaining);
         }

         // SL stays put on TP1. The original SL keeps protecting the
         // remaining position until TP2 (or the broker-set final TP) is
         // reached.
         Print("CT plan stage1 SL unchanged ticket=", p.ticket,
               " sl=", DoubleToString(p.slOrig, 5));

         // Advance regardless: partial (if any) is irreversible; retrying
         // would double-close.
         p.stage = 1;
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
         double currentVol = PositionGetDouble(POSITION_VOLUME);
         double remaining = currentVol - closeLots;

         if(closeLots >= minLot && remaining >= minLot) {
            if(!trade.PositionClosePartial(p.ticket, closeLots))
               Print("CT plan stage2 partial FAILED ticket=", p.ticket,
                     " lots=", closeLots, " code=", trade.ResultRetcode());
            else
               Print("CT plan stage2 partial ok ticket=", p.ticket, " lots=", closeLots);
         } else {
            Print("CT plan stage2 partial skipped (lot size) ticket=", p.ticket,
                  " want=", closeLots, " remain=", remaining);
         }

         // Move SL to entry (Break-Even), keep TP at tp3.
         if(!trade.PositionModify(p.ticket, p.entry, p.tps[2]))
            Print("CT plan stage2 SL->BE FAILED ticket=", p.ticket,
                  " code=", trade.ResultRetcode());
         else
            Print("CT plan stage2 SL->BE ok ticket=", p.ticket);

         p.stage = 2;
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
   PostResult(id, "executed", 0, StringFormat("closed=%d failed=%d", closed, failed));
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
// response so we can pipe it into DoOpen() and reuse its chase / watch /
// market logic for REOPEN_LAST and REINFORCE. Returns "" on failure.
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
   if(trade.PositionClosePartial(ticket, closeLots)) {
      double postVol = PositionSelectByTicket(ticket)
                       ? PositionGetDouble(POSITION_VOLUME) : 0.0;
      PostPositionUpdate(ticket, postVol, 0);
      PostResult(id, "executed", ticket,
                 StringFormat("closed=%.2f", closeLots));
   } else {
      PostResult(id, "failed", ticket,
                 "partial_failed:" + IntegerToString(trade.ResultRetcode()));
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
   DoOpen(id, fakePayload);  // reuses chase/watch/market flow + PostResult
}

void DoReinforce(long id, string payload) {
   long currentTicket = FindSingletonOpenTicket(Symbol_Override);
   if(currentTicket > 0) {
      // Per user policy: close regardless of PnL, then reopen.
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
   string url = ApiBaseUrl + "/positions/last_closed?symbol=" + Symbol_Override
              + "&within_hours=24";
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

// ---- Synthetic-pending (watching) helpers ----

// Format a UTC epoch as ISO-8601 with explicit +00:00 so the Python side (which
// also emits +00:00) lines up byte-for-byte and string compares work.
string IsoUtcFromGmt(datetime t) {
   MqlDateTime dt;
   TimeToStruct(t, dt);
   return StringFormat("%04d-%02d-%02dT%02d:%02d:%02d+00:00",
      dt.year, dt.mon, dt.day, dt.hour, dt.min, dt.sec);
}

string TpsToJsonArray(const double &tps[], int tpCount) {
   string s = "[";
   for(int i = 0; i < tpCount; i++) {
      if(i > 0) s += ",";
      s += DoubleToString(tps[i], 5);
   }
   s += "]";
   return s;
}

void StartWatch(long id, bool isBuy, double entryLow, double entryHigh,
                double sl, const double &tps[], int tpCount) {
   datetime expires = TimeGMT() + (datetime)(WatchExpiryHours * 3600);
   string expiresIso = IsoUtcFromGmt(expires);

   string watchJson = StringFormat(
      "{\"symbol\":\"%s\",\"side\":\"%s\",\"zone_low\":%.5f,\"zone_high\":%.5f,"
      "\"sl\":%.5f,\"tps\":%s}",
      Symbol_Override, isBuy ? "BUY" : "SELL", entryLow, entryHigh, sl,
      TpsToJsonArray(tps, tpCount)
   );
   string body = "{\"status\":\"watching\",\"watch\":" + watchJson +
                 ",\"expires_at\":\"" + expiresIso + "\"}";
   string resp; int status;
   bool ok = HttpPostJsonWithStatus(
      ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result",
      body, resp, status);
   if(!ok) {
      Print("Watch POST failed for action ", id, " status=", status,
            " — claim sweeper will release");
      return;
   }
   // Local bookkeeping mirrors what we just wrote to the DB.
   WatchEntry w;
   w.action_id = id;
   w.isBuy = isBuy;
   w.zone_low = entryLow;
   w.zone_high = entryHigh;
   w.sl = sl;
   w.tpCount = tpCount;
   for(int i = 0; i < 3; i++) w.tps[i] = (i < tpCount ? tps[i] : 0.0);
   w.expires_at_iso = expiresIso;
   int n = ArraySize(g_watches);
   ArrayResize(g_watches, n + 1);
   g_watches[n] = w;
   Print("CT watch started action=", id, " zone=[", entryLow, ",", entryHigh,
         "] expires=", expiresIso);
}

void RemoveWatch(int idx) {
   int n = ArraySize(g_watches);
   for(int j = idx; j < n - 1; j++) g_watches[j] = g_watches[j + 1];
   ArrayResize(g_watches, n - 1);
}

// Convert a triggered watch into a market open. On success, register a staged
// plan (if >=2 TPs) and POST executed; on failure, POST failed so the server
// moves it off 'watching' and doesn't re-surface it.
void TriggerWatch(int idx) {
   WatchEntry w = g_watches[idx];
   double entry = SymbolInfoDouble(Symbol_Override, w.isBuy ? SYMBOL_ASK : SYMBOL_BID);
   double lotsTotal = LotsFromRisk(w.sl, entry);
   double tpFinal = w.tps[w.tpCount - 1];

   bool ok = w.isBuy
      ? trade.Buy(lotsTotal, Symbol_Override, 0, w.sl, tpFinal, "copytrades")
      : trade.Sell(lotsTotal, Symbol_Override, 0, w.sl, tpFinal, "copytrades");
   if(!ok) {
      PostResult(w.action_id, "failed", 0,
                 "watch_trigger_send_failed:" + IntegerToString(trade.ResultRetcode()));
      RemoveWatch(idx);
      return;
   }
   long ticket = (long)trade.ResultOrder() != 0
                 ? (long)trade.ResultOrder()
                 : (long)trade.ResultDeal();
   if(ticket <= 0) {
      PostResult(w.action_id, "failed", 0, "watch_trigger_no_ticket");
      RemoveWatch(idx);
      return;
   }

   // Staged management works now that we fired at market and have a real
   // position ticket — the gap that made out-of-zone signals skip partials.
   if(w.tpCount >= 2) {
      double tpsArr[3];
      for(int i = 0; i < 3; i++) tpsArr[i] = w.tps[i];
      RegisterPlan(ticket, w.isBuy, lotsTotal, entry, w.sl, tpsArr, w.tpCount);
   }

   string legJson = StringFormat(
      "{\"mt5_ticket\":%I64d,\"snapshot\":{\"symbol\":\"%s\",\"side\":\"%s\","
      "\"volume\":%.2f,\"entry_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f}}",
      ticket, Symbol_Override, w.isBuy ? "BUY" : "SELL",
      lotsTotal, entry, w.sl, tpFinal
   );
   string body = "{\"status\":\"executed\",\"legs\":[" + legJson + "]}";
   string resp; int status;
   HttpPostJsonWithStatus(
      ApiBaseUrl + "/actions/" + IntegerToString(w.action_id) + "/result",
      body, resp, status);
   Print("CT watch triggered action=", w.action_id, " ticket=", ticket,
         " entry=", entry);
   g_stats_executed++;
   g_last_action_id = w.action_id;
   g_last_action_type = "OPEN";
   g_last_action_status = "executed";
   g_last_action_at = TimeCurrent();
   RemoveWatch(idx);
}

void ManageWatches() {
   if(ArraySize(g_watches) == 0) return;
   string nowIso = IsoUtcFromGmt(TimeGMT());
   for(int i = ArraySize(g_watches) - 1; i >= 0; i--) {
      WatchEntry w = g_watches[i];
      // Expiry: string compare on fixed-format ISO-8601 gives chronological order.
      if(StringCompare(nowIso, w.expires_at_iso) > 0) {
         Print("CT watch expired locally action=", w.action_id,
               " — server sweeper will mark rejected");
         RemoveWatch(i);
         continue;
      }
      double price = SymbolInfoDouble(Symbol_Override, w.isBuy ? SYMBOL_ASK : SYMBOL_BID);
      if(price >= w.zone_low && price <= w.zone_high) {
         TriggerWatch(i);
      }
   }
}

void LoadPersistedWatches() {
   // DB is authoritative. On every OnInit rebuild the local watchlist from
   // the API so restarts don't drop zones mid-flight.
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=watching&limit=200", body)) return;
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

      long id = StringToInteger(JsonField(obj, "id"));
      string expiresIso = JsonField(obj, "expires_at");
      // The watch object itself is nested inside the response — extract by brace match.
      int wKey = StringFind(obj, "\"watch\":");
      if(id <= 0 || wKey < 0) continue;
      int wStart = StringFind(obj, "{", wKey);
      if(wStart < 0) continue;
      int wDepth = 0, wEnd = -1;
      for(int i = wStart; i < StringLen(obj); i++) {
         ushort c = StringGetCharacter(obj, i);
         if(c == '{') wDepth++;
         else if(c == '}') { wDepth--; if(wDepth == 0) { wEnd = i; break; } }
      }
      if(wEnd < 0) continue;
      string watchObj = StringSubstr(obj, wStart, wEnd - wStart + 1);

      WatchEntry w;
      w.action_id = id;
      w.isBuy = (JsonField(watchObj, "side") == "BUY");
      w.zone_low = StringToDouble(JsonField(watchObj, "zone_low"));
      w.zone_high = StringToDouble(JsonField(watchObj, "zone_high"));
      w.sl = StringToDouble(JsonField(watchObj, "sl"));
      double tps[];
      ParseTps(JsonField(watchObj, "tps"), tps);
      w.tpCount = ArraySize(tps);
      if(w.tpCount > 3) w.tpCount = 3;
      // AI-persisted payload may be in any TP order; normalise so TriggerWatch
      // sets MT5 TP to the farthest target and staged partials align.
      SortTpsByDistance(tps, w.tpCount, w.isBuy);
      for(int i = 0; i < 3; i++) w.tps[i] = (i < w.tpCount ? tps[i] : 0.0);
      w.expires_at_iso = expiresIso;

      int n = ArraySize(g_watches);
      ArrayResize(g_watches, n + 1);
      g_watches[n] = w;
      Print("CT watch restored action=", id, " zone=[", w.zone_low, ",",
            w.zone_high, "] expires=", expiresIso);
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
