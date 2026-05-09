//+------------------------------------------------------------------+
//|  Dashboard.mqh — CCanvas-based live dashboard for CopyTrades EA  |
//|                                                                  |
//|  The EA populates a DashboardStats struct once per second and    |
//|  calls CDashboard::Update(). Repaint is gated on a hash of the   |
//|  stats, so a chart with no state changes does zero canvas work.  |
//+------------------------------------------------------------------+
#property strict
#include <Canvas\Canvas.mqh>
#include "BrokerCheck.mqh"

// Dark Luxury palette (AARRGGBB) — black + gold for XAUUSD. Replaces the
// prior GitHub-dark cool palette. Semantic uses unchanged: ACCENT gilds
// the brand title + section headers + the "next TP" glyph; OK is the
// "operational good" signal (LIVE pill, executed counter, TP hit);
// WARN is amber-gold for transitional states (ALGO OFF, moderate verdict);
// DANGER is deep burgundy — visible without the alarm-red "fire" feel.
#define DSH_BG       0xFF0A0A0A   // near-black with a hint of warmth
#define DSH_PANEL    0xFF141210   // one step up from BG, very subtle
#define DSH_BORDER   0xFF3D2F18   // bronze-tinted border
#define DSH_TEXT     0xFFE8E2D4   // warm cream off-white
#define DSH_MUTED    0xFF8C7E66   // warm bronze-grey for de-emphasized text
#define DSH_ACCENT   0xFFD4AF37   // classic gold (#D4AF37)
#define DSH_OK       0xFF7BB369   // refined desaturated green (plays nicely beside gold)
#define DSH_WARN     0xFFE0A040   // amber-gold (close to ACCENT, distinct)
#define DSH_DANGER   0xFFB23B3B   // deep burgundy (not alarm red)

#define DSH_MAX_TRADES 4

// Per-position snapshot for the OPEN TRADES panel. Populated by the EA in
// BuildStats from g_plans[] (for signals opened this session) or from the
// MT5 position directly (for pre-restart tickets without an in-memory plan).
struct DashboardTrade {
   long     ticket;
   bool     isBuy;
   double   currentVol;
   double   entry;
   double   origLots;
   double   tps[3];
   int      tpCount;
   int      stage;                 // 0 none hit, 1 tp1 hit, 2 tp2 hit
   bool     hasPlan;               // false -> only mt5-side single TP is known
   double   profit_per_stage[3];   // account-ccy profit at each TP's partial close
   double   profit_total;          // sum if all TPs hit
};

struct DashboardStats {
   // Health
   bool     api_ok;
   int      api_age_sec;
   bool     kill_switch_on;
   bool     algo_allowed;
   int      uptime_sec;

   // Now
   int      open_positions;
   long     last_action_id;
   string   last_action_type;
   string   last_action_status;
   int      last_action_age_sec;

   // Today
   int      signals_today;
   int      executed_today;
   int      rejected_today;
   int      chased_today;
   double   realized_pnl_today;
   double   open_pnl;

   // Risk
   double   balance;
   double   equity;
   double   free_margin;
   double   drawdown_pct;
   double   risk_if_all_sl_hit_pct;

   // Capacity
   double   lots_deployed;
   double   lots_cap;

   string   account_ccy;

   // Open trades detail (for OPEN TRADES section).
   DashboardTrade open_trades[DSH_MAX_TRADES];
   int            open_trades_count;  // may exceed DSH_MAX_TRADES (rendered as "+N more")

   // Broker compatibility check result (populated once at OnInit).
   BrokerCheckResult broker;

   // Signal-quality evaluation for the latest OPEN action (populated by
   // BuildStats from GET /actions/latest_open_evaluation). When
   // eval_available is false, the widget shows a "no signal yet" message.
   bool     eval_available;
   long     eval_action_id;
   int      eval_score;             // 0-100
   string   eval_verdict;           // strong | moderate | weak | avoid | unavailable
   string   eval_key_factor;        // 1-line dominant reason
   string   eval_summary;           // 1-3 sentence rationale (full text, wrapped)
   string   eval_data_quality;      // full | reduced
   int      eval_age_sec;           // seconds since evaluation produced
};

class CDashboard {
private:
   CCanvas  m_canvas;
   string   m_name;
   int      m_width;
   int      m_height;
   int      m_x;
   int      m_y;
   string   m_font;
   int      m_font_size;
   int      m_font_size_big;
   ulong    m_last_hash;
   int      m_cursor_y;

   ulong    HashStats(const DashboardStats &s);
   void     DrawHeader(const DashboardStats &s);
   void     DrawHealth(const DashboardStats &s);
   void     DrawBroker(const DashboardStats &s);
   void     DrawNow(const DashboardStats &s);
   void     DrawOpenTrades(const DashboardStats &s);
   void     DrawSignalQuality(const DashboardStats &s);
   void     DrawToday(const DashboardStats &s);
   void     DrawSection(string title);
   void     DrawRow(string label, string value, uint value_color);
   void     DrawDivider();
   void     DrawWrappedText(string text, int max_chars, uint color);
   void     Pill(int x, int y, string text, uint bg, uint fg);
   string   FmtDuration(int sec);
   string   FmtSigned(double v, int decimals);
   uint     PnlColor(double v);

public:
   CDashboard() : m_name("CT_Dashboard"), m_width(380), m_height(900),
                  m_x(20), m_y(20), m_font("Consolas"),
                  m_font_size(10), m_font_size_big(12), m_last_hash(0),
                  m_cursor_y(0) {}
   bool Create(int x = 20, int y = 20);
   void Destroy();
   void Update(const DashboardStats &s);
};

//--- lifecycle ------------------------------------------------------

bool CDashboard::Create(int x, int y) {
   m_x = x;
   m_y = y;
   if(!m_canvas.CreateBitmapLabel(m_name, m_x, m_y, m_width, m_height,
                                  COLOR_FORMAT_ARGB_NORMALIZE)) {
      Print("CT dashboard: CreateBitmapLabel failed, err=", GetLastError());
      return false;
   }
   ObjectSetInteger(0, m_name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, m_name, OBJPROP_ANCHOR, ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, m_name, OBJPROP_XDISTANCE, m_x);
   ObjectSetInteger(0, m_name, OBJPROP_YDISTANCE, m_y);
   ObjectSetInteger(0, m_name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, m_name, OBJPROP_BACK, false);
   ObjectSetInteger(0, m_name, OBJPROP_SELECTABLE, false);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   m_canvas.Erase(DSH_BG);
   m_canvas.Update();
   return true;
}

void CDashboard::Destroy() {
   m_canvas.Destroy();
}

//--- public update (gated by hash) ----------------------------------

void CDashboard::Update(const DashboardStats &s) {
   ulong h = HashStats(s);
   if(h == m_last_hash) return;
   m_last_hash = h;

   m_canvas.Erase(DSH_BG);
   m_canvas.Rectangle(0, 0, m_width - 1, m_height - 1, DSH_BORDER);

   m_cursor_y = 10;
   DrawHeader(s);
   DrawDivider();
   DrawHealth(s);
   DrawDivider();
   DrawBroker(s);
   DrawDivider();
   DrawNow(s);
   DrawDivider();
   DrawOpenTrades(s);
   DrawDivider();
   DrawSignalQuality(s);
   DrawDivider();
   DrawToday(s);

   m_canvas.Update();
}

//--- sections -------------------------------------------------------

void CDashboard::DrawHeader(const DashboardStats &s) {
   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   m_canvas.TextOut(12, m_cursor_y, "COPYTRADES", DSH_ACCENT, TA_LEFT | TA_TOP);

   string pill_text; uint bg;
   if(s.kill_switch_on)        { pill_text = "HALTED";   bg = DSH_DANGER; }
   else if(!s.api_ok)          { pill_text = "API DOWN"; bg = DSH_DANGER; }
   else if(!s.algo_allowed)    { pill_text = "ALGO OFF"; bg = DSH_WARN;   }
   else                        { pill_text = "LIVE";     bg = DSH_OK;     }
   Pill(m_width - 80, m_cursor_y - 2, pill_text, bg, 0xFF000000);

   m_cursor_y += 22;
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   m_canvas.TextOut(12, m_cursor_y,
      "uptime " + FmtDuration(s.uptime_sec) + "   " + s.account_ccy,
      DSH_MUTED, TA_LEFT | TA_TOP);
   m_cursor_y += 16;
}

void CDashboard::DrawHealth(const DashboardStats &s) {
   DrawSection("HEALTH");
   DrawRow("API",
      s.api_ok ? ("ok, " + IntegerToString(s.api_age_sec) + "s ago") : "unreachable",
      s.api_ok ? DSH_OK : DSH_DANGER);
   DrawRow("Kill switch",
      s.kill_switch_on ? "ON" : "off",
      s.kill_switch_on ? DSH_DANGER : DSH_TEXT);
   DrawRow("Algo trading",
      s.algo_allowed ? "enabled" : "disabled",
      s.algo_allowed ? DSH_OK : DSH_WARN);
}

void CDashboard::DrawBroker(const DashboardStats &s) {
   // Static broker-compatibility checks evaluated once at OnInit.
   // Compact when everything's clean (one green line); expanded list of
   // FAIL/WARN issues otherwise so the operator sees missing requirements
   // without opening the journal.
   if(!s.broker.ran) {
      DrawSection("BROKER");
      m_canvas.TextOut(12, m_cursor_y, "checks pending...",
                       DSH_MUTED, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
      return;
   }

   string title;
   if(s.broker.count == 0) {
      title = StringFormat("BROKER  (%d/%d ok)",
                           s.broker.checks_run, s.broker.checks_run);
   } else {
      title = StringFormat("BROKER  (%d/%d ok, %d FAIL, %d WARN)",
                           s.broker.checks_run - s.broker.count,
                           s.broker.checks_run,
                           s.broker.fails, s.broker.warns);
   }
   DrawSection(title);

   if(s.broker.count == 0) {
      m_canvas.TextOut(12, m_cursor_y, "all checks passed",
                       DSH_OK, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
      return;
   }

   for(int i = 0; i < s.broker.count; i++) {
      uint   tagColor = (s.broker.issues[i].severity == BC_FAIL)
                        ? DSH_DANGER : DSH_WARN;
      string tag      = (s.broker.issues[i].severity == BC_FAIL)
                        ? "FAIL" : "WARN";
      // Tag pill on the left, label next to it.
      m_canvas.TextOut(12, m_cursor_y, tag, tagColor, TA_LEFT | TA_TOP);
      m_canvas.TextOut(54, m_cursor_y, s.broker.issues[i].label,
                       DSH_TEXT, TA_LEFT | TA_TOP);
      m_cursor_y += 14;
      // Detail wrapped on a second line, slightly muted, indented.
      m_canvas.TextOut(54, m_cursor_y, s.broker.issues[i].detail,
                       DSH_MUTED, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
   }
}

void CDashboard::DrawNow(const DashboardStats &s) {
   DrawSection("NOW");
   DrawRow("Open positions",
      IntegerToString(s.open_positions) + " / 1",
      DSH_TEXT);
   DrawRow("Lots deployed",
      DoubleToString(s.lots_deployed, 2) + " / " + DoubleToString(s.lots_cap, 2),
      DSH_TEXT);
   DrawRow("Open P&L",
      FmtSigned(s.open_pnl, 2) + " " + s.account_ccy,
      PnlColor(s.open_pnl));
   if(s.last_action_id > 0) {
      DrawRow("Last action",
         "#" + IntegerToString(s.last_action_id) + " " +
         s.last_action_type + " " + s.last_action_status + "  " +
         FmtDuration(s.last_action_age_sec) + " ago",
         DSH_MUTED);
   } else {
      DrawRow("Last action", "-", DSH_MUTED);
   }
}

void CDashboard::DrawOpenTrades(const DashboardStats &s) {
   DrawSection("OPEN TRADES");
   if(s.open_trades_count == 0) {
      m_canvas.TextOut(12, m_cursor_y, "none", DSH_MUTED, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
      return;
   }
   int shown = s.open_trades_count;
   if(shown > DSH_MAX_TRADES) shown = DSH_MAX_TRADES;
   for(int i = 0; i < shown; i++) {
      // Header line: ticket, side, current vol @ entry
      string hdr = "#" + IntegerToString(s.open_trades[i].ticket) + " "
                 + (s.open_trades[i].isBuy ? "BUY " : "SELL ")
                 + DoubleToString(s.open_trades[i].currentVol, 2);
      if(s.open_trades[i].entry > 0)
         hdr += " @ " + DoubleToString(s.open_trades[i].entry, 2);
      m_canvas.TextOut(12, m_cursor_y, hdr, DSH_TEXT, TA_LEFT | TA_TOP);
      m_cursor_y += 16;

      if(!s.open_trades[i].hasPlan) {
         // No in-memory plan (pre-restart ticket or 1-TP signal). Show the
         // single MT5-side TP if set, otherwise "-".
         string line;
         if(s.open_trades[i].tpCount > 0 && s.open_trades[i].tps[0] > 0)
            line = "TP " + DoubleToString(s.open_trades[i].tps[0], 2);
         else
            line = "no TP";
         m_canvas.TextOut(24, m_cursor_y, line, DSH_MUTED, TA_LEFT | TA_TOP);
         m_cursor_y += 16;
         continue;
      }

      int n = s.open_trades[i].tpCount;
      int stage = s.open_trades[i].stage;
      for(int k = 0; k < n; k++) {
         string glyph = "- ";
         uint   col   = DSH_TEXT;
         if(k < stage)       { glyph = "* "; col = DSH_OK;     }  // hit
         else if(k == stage) { glyph = "> "; col = DSH_ACCENT; }  // next target
         string lbl = glyph + "TP" + IntegerToString(k + 1) + " "
                    + DoubleToString(s.open_trades[i].tps[k], 2);
         string val = FmtSigned(s.open_trades[i].profit_per_stage[k], 2)
                    + " " + s.account_ccy;
         m_canvas.TextOut(24, m_cursor_y, lbl, col, TA_LEFT | TA_TOP);
         m_canvas.TextOut(m_width - 12, m_cursor_y, val,
                          PnlColor(s.open_trades[i].profit_per_stage[k]),
                          TA_RIGHT | TA_TOP);
         m_cursor_y += 16;
      }
      // Total if all TPs hit.
      m_canvas.TextOut(24, m_cursor_y, "Total if all hit",
                       DSH_MUTED, TA_LEFT | TA_TOP);
      string tot = FmtSigned(s.open_trades[i].profit_total, 2)
                 + " " + s.account_ccy;
      m_canvas.TextOut(m_width - 12, m_cursor_y, tot,
                       PnlColor(s.open_trades[i].profit_total),
                       TA_RIGHT | TA_TOP);
      m_cursor_y += 18;
   }
   if(s.open_trades_count > DSH_MAX_TRADES) {
      m_canvas.TextOut(12, m_cursor_y,
         "+" + IntegerToString(s.open_trades_count - DSH_MAX_TRADES) + " more",
         DSH_MUTED, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
   }
}

void CDashboard::DrawSignalQuality(const DashboardStats &s) {
   // Latest OPEN action's AI-driven conviction score. Fetched per-tick
   // from GET /actions/latest_open_evaluation. Color band:
   //   80-100 strong   (green)
   //   60-79  moderate (amber)
   //   40-59  weak     (orange)
   //    0-39  avoid    (red)
   // 'unavailable' (evaluator failed) shows muted with the reason.
   DrawSection("SIGNAL QUALITY");
   if(!s.eval_available) {
      m_canvas.TextOut(12, m_cursor_y, "no OPEN signal evaluated yet",
                       DSH_MUTED, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
      return;
   }
   uint scoreColor;
   if(s.eval_verdict == "strong")        scoreColor = DSH_OK;
   else if(s.eval_verdict == "moderate") scoreColor = DSH_WARN;
   else if(s.eval_verdict == "weak")     scoreColor = 0xFFA67338;  // deep bronze (between WARN amber and DANGER burgundy)
   else if(s.eval_verdict == "avoid")    scoreColor = DSH_DANGER;
   else                                  scoreColor = DSH_MUTED;   // unavailable

   // Header line: action id + age + score pill
   string head = StringFormat("Latest #%I64d  %s ago",
      s.eval_action_id, FmtDuration(s.eval_age_sec));
   m_canvas.TextOut(12, m_cursor_y, head, DSH_MUTED, TA_LEFT | TA_TOP);
   string scoreText = IntegerToString(s.eval_score) + " / 100";
   m_canvas.TextOut(m_width - 12, m_cursor_y, scoreText, scoreColor,
                    TA_RIGHT | TA_TOP);
   m_cursor_y += 16;

   // 10-segment ascii-bar gauge for at-a-glance reading.
   int filled = (int)((s.eval_score + 5) / 10);   // round to nearest tenth
   if(filled < 0) filled = 0;
   if(filled > 10) filled = 10;
   string bar = "";
   for(int i = 0; i < 10; i++) bar += (i < filled) ? "▰" : "▱";
   m_canvas.TextOut(12, m_cursor_y, bar, scoreColor, TA_LEFT | TA_TOP);
   string verdict_up = s.eval_verdict;
   StringToUpper(verdict_up);
   m_canvas.TextOut(m_width - 12, m_cursor_y, verdict_up, scoreColor,
                    TA_RIGHT | TA_TOP);
   m_cursor_y += 18;

   // Key factor (dominant 1-line reason) and full summary (1-3 sentences).
   // Both wrapped to fit the 380px panel — operator no longer needs to
   // open the DB / bot to read the AI's reasoning.
   if(StringLen(s.eval_key_factor) > 0) {
      DrawWrappedText(s.eval_key_factor, 52, DSH_TEXT);
   }
   if(StringLen(s.eval_summary) > 0) {
      m_cursor_y += 4;
      DrawWrappedText(s.eval_summary, 52, DSH_MUTED);
   }

   if(s.eval_data_quality == "reduced") {
      m_canvas.TextOut(12, m_cursor_y,
                       "(reduced context — score capped at 70)",
                       DSH_MUTED, TA_LEFT | TA_TOP);
      m_cursor_y += 16;
   }
}

void CDashboard::DrawToday(const DashboardStats &s) {
   DrawSection("TODAY");
   DrawRow("Signals",
      IntegerToString(s.signals_today) + " received", DSH_TEXT);
   DrawRow("Executed",
      IntegerToString(s.executed_today), DSH_OK);
   DrawRow("Rejected",
      IntegerToString(s.rejected_today),
      s.rejected_today > 0 ? DSH_WARN : DSH_MUTED);
   DrawRow("Chased",
      IntegerToString(s.chased_today),
      s.chased_today > 0 ? DSH_ACCENT : DSH_MUTED);
   DrawRow("Realized P&L",
      FmtSigned(s.realized_pnl_today, 2) + " " + s.account_ccy,
      PnlColor(s.realized_pnl_today));
}

//--- primitives -----------------------------------------------------

void CDashboard::DrawSection(string title) {
   m_cursor_y += 6;
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_BOLD);
   m_canvas.TextOut(12, m_cursor_y, title, DSH_ACCENT, TA_LEFT | TA_TOP);
   m_cursor_y += 18;
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

void CDashboard::DrawRow(string label, string value, uint value_color) {
   m_canvas.TextOut(12, m_cursor_y, label, DSH_MUTED, TA_LEFT | TA_TOP);
   m_canvas.TextOut(m_width - 12, m_cursor_y, value, value_color,
                    TA_RIGHT | TA_TOP);
   m_cursor_y += 16;
}

void CDashboard::DrawDivider() {
   m_cursor_y += 4;
   m_canvas.Line(12, m_cursor_y, m_width - 12, m_cursor_y, DSH_BORDER);
   m_cursor_y += 2;
}

void CDashboard::Pill(int x, int y, string text, uint bg, uint fg) {
   int w = 68, h = 18;
   m_canvas.FillRectangle(x, y, x + w, y + h, bg);
   m_canvas.FontSet(m_font, -9 * 10, FW_BOLD);
   m_canvas.TextOut(x + w / 2, y + 2, text, fg, TA_CENTER | TA_TOP);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

//--- helpers --------------------------------------------------------

ulong CDashboard::HashStats(const DashboardStats &s) {
   // FNV-ish: ints + money rounded to cents — sub-cent P&L noise won't redraw.
   ulong h = 1469598103934665603UL;
   h = (h ^ (ulong)(s.api_ok ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)s.api_age_sec) * 1099511628211UL;
   h = (h ^ (ulong)(s.kill_switch_on ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)(s.algo_allowed ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)s.uptime_sec) * 1099511628211UL;
   h = (h ^ (ulong)s.open_positions) * 1099511628211UL;
   h = (h ^ (ulong)s.last_action_id) * 1099511628211UL;
   h = (h ^ (ulong)s.last_action_age_sec) * 1099511628211UL;
   h = (h ^ (ulong)s.signals_today) * 1099511628211UL;
   h = (h ^ (ulong)s.executed_today) * 1099511628211UL;
   h = (h ^ (ulong)s.rejected_today) * 1099511628211UL;
   h = (h ^ (ulong)s.chased_today) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.realized_pnl_today * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.open_pnl * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.balance * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.equity * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.free_margin * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.drawdown_pct * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.risk_if_all_sl_hit_pct * 100)) * 1099511628211UL;
   h = (h ^ (ulong)MathRound(s.lots_deployed * 100)) * 1099511628211UL;
   h = (h ^ (ulong)s.open_trades_count) * 1099511628211UL;
   h = (h ^ (ulong)(s.broker.ran ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)s.broker.count) * 1099511628211UL;
   h = (h ^ (ulong)s.broker.fails) * 1099511628211UL;
   h = (h ^ (ulong)s.broker.checks_run) * 1099511628211UL;
   h = (h ^ (ulong)(s.eval_available ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)s.eval_action_id) * 1099511628211UL;
   h = (h ^ (ulong)s.eval_score) * 1099511628211UL;
   // Repaint at most once per minute on age changes (otherwise the
   // duration string flips every second on a stale eval).
   h = (h ^ (ulong)(s.eval_age_sec / 60)) * 1099511628211UL;
   int shown = s.open_trades_count > DSH_MAX_TRADES ? DSH_MAX_TRADES : s.open_trades_count;
   for(int i = 0; i < shown; i++) {
      h = (h ^ (ulong)s.open_trades[i].ticket) * 1099511628211UL;
      h = (h ^ (ulong)s.open_trades[i].stage) * 1099511628211UL;
      h = (h ^ (ulong)s.open_trades[i].tpCount) * 1099511628211UL;
      h = (h ^ (ulong)MathRound(s.open_trades[i].currentVol * 100)) * 1099511628211UL;
      h = (h ^ (ulong)(s.open_trades[i].hasPlan ? 1 : 0)) * 1099511628211UL;
      h = (h ^ (ulong)MathRound(s.open_trades[i].entry * 100)) * 1099511628211UL;
      h = (h ^ (ulong)MathRound(s.open_trades[i].origLots * 100)) * 1099511628211UL;
      h = (h ^ (ulong)MathRound(s.open_trades[i].profit_total * 100)) * 1099511628211UL;
      for(int k = 0; k < 3; k++) {
         h = (h ^ (ulong)MathRound(s.open_trades[i].tps[k] * 100)) * 1099511628211UL;
         h = (h ^ (ulong)MathRound(s.open_trades[i].profit_per_stage[k] * 100)) * 1099511628211UL;
      }
   }
   return h;
}

// Word-wrap `text` into lines of up to ~max_chars characters and draw each
// line at the current cursor. Splits on spaces; falls back to hard-cut for
// any single word longer than max_chars (long URLs etc). Used by the
// SIGNAL QUALITY panel to render the AI evaluator's full reasoning instead
// of a 50-char-truncated one-liner.
void CDashboard::DrawWrappedText(string text, int max_chars, uint color) {
   int n = StringLen(text);
   int i = 0;
   while(i < n) {
      // Skip leading spaces on a fresh line.
      while(i < n && StringGetCharacter(text, i) == ' ') i++;
      if(i >= n) break;
      int remaining = n - i;
      int take = (remaining <= max_chars) ? remaining : max_chars;
      // If we're not at the end, prefer to break on the last space within
      // the window so we don't split mid-word.
      if(remaining > max_chars) {
         int last_space = -1;
         for(int k = 0; k < take; k++) {
            if(StringGetCharacter(text, i + k) == ' ') last_space = k;
         }
         if(last_space > 0) take = last_space;
      }
      string line = StringSubstr(text, i, take);
      m_canvas.TextOut(12, m_cursor_y, line, color, TA_LEFT | TA_TOP);
      m_cursor_y += 14;
      i += take;
   }
}

string CDashboard::FmtDuration(int sec) {
   if(sec < 60) return IntegerToString(sec) + "s";
   if(sec < 3600) return IntegerToString(sec / 60) + "m";
   if(sec < 86400) return IntegerToString(sec / 3600) + "h" +
                          IntegerToString((sec % 3600) / 60) + "m";
   return IntegerToString(sec / 86400) + "d" +
          IntegerToString((sec % 86400) / 3600) + "h";
}

string CDashboard::FmtSigned(double v, int decimals) {
   string s = DoubleToString(MathAbs(v), decimals);
   if(v > 0) return "+" + s;
   if(v < 0) return "-" + s;
   return s;
}

uint CDashboard::PnlColor(double v) {
   if(v > 0.005) return DSH_OK;
   if(v < -0.005) return DSH_DANGER;
   return DSH_TEXT;
}
