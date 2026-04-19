//+------------------------------------------------------------------+
//|  CopyTrades.mq5 — polls FastAPI bridge for actions, executes     |
//+------------------------------------------------------------------+
#property strict
#include <Trade\Trade.mqh>

input string ApiBaseUrl              = "http://127.0.0.1:8765";
input int    PollIntervalSec         = 1;
input double RiskPercentPerTrade     = 1.0;
input double MaxLotsPerSignal        = 0.50;
input int    MaxOpenPositions        = 3;
input int    EntryZoneMode           = 1;   // 0=midpoint limit, 1=market if in zone
input int    TPMode                  = 1;   // 0=single TP1, 1=split per TP
input int    SlippagePoints          = 50;
input string Symbol_Override         = "XAUUSD";

CTrade trade;

int OnInit() {
   trade.SetExpertMagicNumber(919191);
   trade.SetDeviationInPoints(SlippagePoints);
   EventSetTimer(PollIntervalSec);
   Print("CopyTrades EA started. API=", ApiBaseUrl);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
}

void OnTimer() {
   if(KillSwitchOn()) return;
   PollAndExecute();
   ReconcileClosedPositions();
}

bool KillSwitchOn() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/settings/kill_switch", body)) return false;
   return StringFind(body, "\"value\":\"on\"") >= 0;
}

void PollAndExecute() {
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=sent", body)) return;
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
   char post[]; char result[]; string headers = "Content-Type: application/json\r\n";
   StringToCharArray(jsonBody, post, 0, StringLen(jsonBody));
   ArrayResize(post, StringLen(jsonBody));
   int res = WebRequest("POST", url, headers, "", 5000, post, ArraySize(post), result, headers);
   if(res == -1) {
      Print("WebRequest POST error ", GetLastError(), " url=", url);
      return false;
   }
   outBody = CharArrayToString(result);
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

   if(CountOurOpenPositions() >= MaxOpenPositions && atype == "OPEN") {
      PostResult(id, "rejected", 0, "max_positions");
      return;
   }

   if(atype == "OPEN")        DoOpen(id, payload);
   else if(atype == "MODIFY") DoModify(id, payload);
   else if(atype == "CLOSE")  DoClose(id, payload);
   else if(atype == "CLOSE_ALL") DoCloseAll(id, payload);
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

double LotsFromRisk(double slPrice, double entryPrice) {
   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   double riskCash = equity * (RiskPercentPerTrade / 100.0);
   double pip = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_SIZE);
   double tickValue = SymbolInfoDouble(Symbol_Override, SYMBOL_TRADE_TICK_VALUE);
   if(pip <= 0 || tickValue <= 0) return 0.01;
   double dist = MathAbs(entryPrice - slPrice);
   double ticks = dist / pip;
   if(ticks <= 0) return 0.01;
   double lots = riskCash / (ticks * tickValue);
   double lotStep = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_STEP);
   lots = MathFloor(lots / lotStep) * lotStep;
   if(lots > MaxLotsPerSignal) lots = MaxLotsPerSignal;
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   if(lots < minLot) lots = minLot;
   return NormalizeDouble(lots, 2);
}

void DoOpen(long id, string payload) {
   string side = JsonField(payload, "side");
   double entryLow = StringToDouble(JsonField(payload, "entry_low"));
   double entryHigh = StringToDouble(JsonField(payload, "entry_high"));
   double sl = StringToDouble(JsonField(payload, "sl"));
   string tpsStr = JsonField(payload, "tps");
   double tps[];
   ParseTps(tpsStr, tps);
   if(ArraySize(tps) == 0) { PostResult(id, "failed", 0, "no_tps"); return; }

   double entry = (entryLow + entryHigh) / 2.0;
   double price = SymbolInfoDouble(Symbol_Override, side == "BUY" ? SYMBOL_ASK : SYMBOL_BID);
   bool inZone = (price >= entryLow && price <= entryHigh);

   ENUM_ORDER_TYPE type;
   bool useMarket = (EntryZoneMode == 1 && inZone);
   if(useMarket) {
      type = (side == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
      entry = price;
   } else {
      type = (side == "BUY") ? ORDER_TYPE_BUY_LIMIT : ORDER_TYPE_SELL_LIMIT;
      entry = (entryLow + entryHigh) / 2.0;
   }

   long lastTicket = 0;
   string lastErr = "";
   int n = (TPMode == 1) ? ArraySize(tps) : 1;
   double lotsTotal = LotsFromRisk(sl, entry);
   double lotsEach = NormalizeDouble(lotsTotal / n, 2);
   double minLot = SymbolInfoDouble(Symbol_Override, SYMBOL_VOLUME_MIN);
   if(lotsEach < minLot) lotsEach = minLot;

   for(int i = 0; i < n; i++) {
      double tp = tps[i];
      bool ok;
      if(useMarket) {
         ok = (side == "BUY")
            ? trade.Buy(lotsEach, Symbol_Override, 0, sl, tp, "copytrades")
            : trade.Sell(lotsEach, Symbol_Override, 0, sl, tp, "copytrades");
      } else {
         ok = (side == "BUY")
            ? trade.BuyLimit(lotsEach, entry, Symbol_Override, sl, tp, ORDER_TIME_GTC, 0, "copytrades")
            : trade.SellLimit(lotsEach, entry, Symbol_Override, sl, tp, ORDER_TIME_GTC, 0, "copytrades");
      }
      if(!ok) { lastErr = "trade.send failed: " + IntegerToString(trade.ResultRetcode()); continue; }
      lastTicket = (long)trade.ResultOrder() != 0 ? (long)trade.ResultOrder() : (long)trade.ResultDeal();
      // Snapshot back to API
      string snap = StringFormat(
         "{\"status\":\"executed\",\"mt5_ticket\":%I64d,"
         "\"snapshot\":{\"symbol\":\"%s\",\"side\":\"%s\",\"volume\":%.2f,"
         "\"entry_price\":%.2f,\"sl\":%.2f,\"tp\":%.2f}}",
         lastTicket, Symbol_Override, side, lotsEach, entry, sl, tp
      );
      string resp;
      HttpPostJson(ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result", snap, resp);
   }
   if(lastTicket == 0) PostResult(id, "failed", 0, lastErr);
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
   if(trade.PositionModify(ticket, newSl, newTp))
      PostResult(id, "executed", ticket, "");
   else
      PostResult(id, "failed", ticket, "modify_failed:" + IntegerToString(trade.ResultRetcode()));
}

void DoClose(long id, string payload) {
   long ticket = StringToInteger(JsonField(payload, "mt5_ticket"));
   if(trade.PositionClose(ticket)) {
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

void ReconcileClosedPositions() {
   // For each ticket the API thinks is open, if MT5 has no such open position, POST close.
   string body;
   if(!HttpGet(ApiBaseUrl + "/actions?status=executed&limit=200", body)) return;
   // (Simpler reconciliation: iterate trade history of last hour and POST closes for any
   //  matching MagicNumber that closed.) We'll do: walk PositionsTotal — anything with our
   //  magic stays open; rely on /positions/{ticket}/close being POSTed at close time by
   //  history scanning.
   datetime since = TimeCurrent() - 3600;
   HistorySelect(since, TimeCurrent());
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

void PostResult(long id, string status, long ticket, string err) {
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
