//+------------------------------------------------------------------+
//|  BrokerCheck.mqh — broker compatibility validation at attach time |
//|                                                                  |
//|  RunBrokerChecks() inspects account + symbol properties against  |
//|  the assumptions baked into the EA and surfaces every mismatch   |
//|  as a BrokerIssue. The dashboard renders the result; the EA also |
//|  prints to the journal and POSTs a startup banner to /alerts.    |
//|                                                                  |
//|  Severity policy:                                                |
//|    BC_FAIL  - trade flow is broken or sizing/decoder is wrong;   |
//|               operator must fix before relying on the EA.        |
//|    BC_WARN  - flow works but a behaviour assumption is violated  |
//|               (e.g. non-USD account, instant execution);         |
//|               operator should review.                            |
//+------------------------------------------------------------------+
#property strict

#define BC_MAX_ISSUES 24

enum ENUM_BC_SEVERITY {
   BC_WARN = 0,
   BC_FAIL = 1,
};

struct BrokerIssue {
   int    severity;     // ENUM_BC_SEVERITY (int for struct-copy compat)
   string label;        // short tag, e.g. "hedging_mode"
   string detail;       // human text, e.g. "expected hedging, got netting"
};

struct BrokerCheckResult {
   BrokerIssue issues[BC_MAX_ISSUES];
   int         count;
   int         fails;
   int         warns;
   int         checks_run;   // total number of checks performed
   bool        ran;          // false until RunBrokerChecks is called
};

//--- internal: append one issue to the result -----------------------
void BC_Add(BrokerCheckResult &r, int sev, string label, string detail) {
   if(r.count >= BC_MAX_ISSUES) return;
   r.issues[r.count].severity = sev;
   r.issues[r.count].label    = label;
   r.issues[r.count].detail   = detail;
   r.count++;
   if(sev == BC_FAIL) r.fails++; else r.warns++;
}

//--- main entry: run every compatibility check ---------------------
//
//  symbol     symbol the EA will trade (Symbol_Override input)
//  maxLots    MaxLotsPerSignal input — used to validate SYMBOL_VOLUME_MAX
//
void RunBrokerChecks(string symbol, double maxLots, BrokerCheckResult &r) {
   r.count = 0; r.fails = 0; r.warns = 0; r.checks_run = 0; r.ran = true;

   // --- Symbol availability (gate everything else on this) ----------
   r.checks_run++;
   if(!SymbolSelect(symbol, true)) {
      BC_Add(r, BC_FAIL, "symbol_select",
             StringFormat("symbol '%s' not available on this broker", symbol));
      return;  // every other symbol check would error
   }

   // --- Account-level checks ---------------------------------------
   r.checks_run++;
   if(!(bool)AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)) {
      BC_Add(r, BC_FAIL, "account_trade_allowed",
             "account is read-only / trading disabled by broker");
   }

   r.checks_run++;
   if(!(bool)AccountInfoInteger(ACCOUNT_TRADE_EXPERT)) {
      BC_Add(r, BC_FAIL, "account_trade_expert",
             "EA trading disabled at account level by broker");
   }

   r.checks_run++;
   long mmode = AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   if(mmode != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) {
      string got = (mmode == ACCOUNT_MARGIN_MODE_RETAIL_NETTING) ? "netting"
                 : (mmode == ACCOUNT_MARGIN_MODE_EXCHANGE)       ? "exchange"
                 : "unknown";
      BC_Add(r, BC_FAIL, "hedging_mode",
             "expected hedging, got " + got
             + " (single-position invariant + partial close break)");
   }

   r.checks_run++;
   long leverage = AccountInfoInteger(ACCOUNT_LEVERAGE);
   if(leverage > 0 && leverage < 100) {
      BC_Add(r, BC_WARN, "leverage_low",
             StringFormat("leverage 1:%d - at default sizing 1 lot on $10K"
                          " gold needs ~50:1 minimum", (int)leverage));
   }

   r.checks_run++;
   string accCcy = AccountInfoString(ACCOUNT_CURRENCY);
   if(accCcy != "USD") {
      BC_Add(r, BC_WARN, "account_currency",
             "account currency " + accCcy
             + " - sizing math assumes USD, PnL will convert");
   }

   // --- Symbol-level checks ----------------------------------------
   r.checks_run++;
   long tradeMode = SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   if(tradeMode != SYMBOL_TRADE_MODE_FULL) {
      BC_Add(r, BC_FAIL, "symbol_trade_mode",
             StringFormat("%s not in full trade mode (mode=%d)",
                          symbol, (int)tradeMode));
   }

   r.checks_run++;
   long exeMode = SymbolInfoInteger(symbol, SYMBOL_TRADE_EXEMODE);
   if(exeMode != SYMBOL_TRADE_EXECUTION_MARKET) {
      string got = (exeMode == SYMBOL_TRADE_EXECUTION_INSTANT)  ? "instant"
                 : (exeMode == SYMBOL_TRADE_EXECUTION_REQUEST)  ? "request"
                 : (exeMode == SYMBOL_TRADE_EXECUTION_EXCHANGE) ? "exchange"
                 : "unknown";
      BC_Add(r, BC_WARN, "execution_mode",
             "execution mode " + got
             + " - partial close may requote or need approval");
   }

   r.checks_run++;
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   if(minLot > 0.01 + 1e-9) {
      BC_Add(r, BC_FAIL, "min_lot",
             StringFormat("min lot %.2f > 0.01 - sizing logic produces"
                          " sub-min volumes that broker rejects", minLot));
   }

   r.checks_run++;
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(lotStep > 0.01 + 1e-9) {
      int sev = (lotStep > 0.1 + 1e-9) ? BC_FAIL : BC_WARN;
      BC_Add(r, sev, "lot_step",
             StringFormat("lot step %.2f - partial-close math floors to 0.01,"
                          " 0.49 partials would be rejected", lotStep));
   }

   r.checks_run++;
   double maxBrokerLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(maxBrokerLot > 0 && maxLots > maxBrokerLot) {
      BC_Add(r, BC_WARN, "max_lot",
             StringFormat("MaxLotsPerSignal=%.2f exceeds broker max %.2f",
                          maxLots, maxBrokerLot));
   }

   r.checks_run++;
   long digits = SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits != 2) {
      BC_Add(r, BC_FAIL, "symbol_digits",
             StringFormat("digits=%d (expected 2) - two-digit shorthand"
                          " decoder breaks on this quote precision",
                          (int)digits));
   }

   r.checks_run++;
   double contractSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(MathAbs(contractSize - 100.0) > 0.01) {
      BC_Add(r, BC_WARN, "contract_size",
             StringFormat("contract size %.0f oz/lot (expected 100) - PnL"
                          " projections in dashboard will be off",
                          contractSize));
   }

   r.checks_run++;
   string profitCcy = SymbolInfoString(symbol, SYMBOL_CURRENCY_PROFIT);
   if(profitCcy != "USD") {
      BC_Add(r, BC_WARN, "profit_currency",
             "symbol profit ccy " + profitCcy
             + " - adds FX conversion variance");
   }

   r.checks_run++;
   long stopsLevel = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(stopsLevel > 50) {
      BC_Add(r, BC_WARN, "stops_level",
             StringFormat("stops level %d points - tight BE moves may reject"
                          " (need >%.2f from market)",
                          (int)stopsLevel,
                          stopsLevel * SymbolInfoDouble(symbol, SYMBOL_POINT)));
   }

   r.checks_run++;
   long freezeLevel = SymbolInfoInteger(symbol, SYMBOL_TRADE_FREEZE_LEVEL);
   if(freezeLevel > 0) {
      BC_Add(r, BC_WARN, "freeze_level",
             StringFormat("freeze level %d points - close/modify rejected"
                          " near SL/TP", (int)freezeLevel));
   }

   r.checks_run++;
   long fillMask = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((fillMask & (SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC)) == 0) {
      BC_Add(r, BC_FAIL, "filling_mode",
             StringFormat("neither FOK nor IOC supported (mask=%d) -"
                          " market orders will fail with INVALID_FILL",
                          (int)fillMask));
   }

   // Live tick available? Symbol may be selected but not yet streaming
   // (e.g., outside session window). Warn rather than fail since the
   // first tick may arrive shortly after attach.
   r.checks_run++;
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   if(bid <= 0.0 || ask <= 0.0) {
      BC_Add(r, BC_WARN, "no_tick",
             "no live bid/ask - symbol may be outside trading session"
             " or not in Market Watch");
   }
}

//--- format result as a single line for the journal banner ---------
string BrokerCheckSummary(const BrokerCheckResult &r) {
   if(!r.ran) return "broker checks: not run";
   if(r.count == 0)
      return StringFormat("broker checks: %d/%d passed",
                          r.checks_run, r.checks_run);
   return StringFormat("broker checks: %d/%d passed, %d FAIL, %d WARN",
                       r.checks_run - r.count, r.checks_run,
                       r.fails, r.warns);
}
