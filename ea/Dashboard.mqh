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

// Neon palette (AARRGGBB) — Cyberpunk variant. Electric cyan brand on
// near-black blue, magenta-pink danger, neon lime ok. High-contrast,
// cool-tone, Tron / Cyberpunk 2077 vibe.
// Semantic roles unchanged: ACCENT highlights the brand title + section
// headers; OK is the "operational good" signal (LIVE pill, executed
// counter, strong verdict); WARN is transitional (ALGO OFF, moderate);
// DANGER is hard-stop (HALTED, API DOWN, avoid verdict).
#define DSH_BG       0xE007091A   // near-black with a hint of blue, ~88% opaque (alpha=E0). Requires COLOR_FORMAT_ARGB_NORMALIZE on canvas creation (see CreateBitmapLabel below).
#define DSH_PANEL    0xFF0F1230   // cobalt step-up — section bands
#define DSH_BORDER   0xFF1FC8FF   // electric cyan rim
#define DSH_TEXT     0xFFE6FBFF   // cyan-white body text
#define DSH_MUTED    0xFF5A7090   // cool slate for de-emphasized text
#define DSH_ACCENT   0xFF00E5FF   // pure electric cyan — brand + section heads
#define DSH_OK       0xFF39FF14   // neon lime
#define DSH_WARN     0xFFFFB300   // amber
#define DSH_DANGER   0xFFFF1F8F   // hot magenta-pink

// Windows-Phone live-tile fills. Mid-saturated forms of the accent
// palette — solid enough to read as "the tile's identity" but dim
// enough that DSH_TEXT (cyan-white) stays legible on top. White-text
// luminance contrast verified against each (>=4.5:1).
#define DSH_TILE_CYAN    0xFF003E5A   // HEALTH tile
#define DSH_TILE_LIME    0xFF1A6B14   // NOW tile when P&L flat/positive
#define DSH_TILE_MAGENTA 0xFF8A1955   // OPEN TRADES (in trade) / DANGER
#define DSH_TILE_AMBER   0xFF8A5A00   // TODAY tile
#define DSH_TILE_SLATE   0xFF1A2240   // LAST ACTION / OPEN TRADES (flat) / SIGNAL QUALITY (no eval)
#define DSH_TILE_CORAL   0xFFA8302F   // mid-tone for SIGNAL QUALITY weak/avoid
#define DSH_TILE_HEADER  0xFF0A2238   // title bar — slightly darker than tiles

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
   string   channel_name;  // header label, e.g. "SMC" / "Forex Engineer"

   // Open trades detail (for OPEN TRADES section).
   DashboardTrade open_trades[DSH_MAX_TRADES];
   int            open_trades_count;  // may exceed DSH_MAX_TRADES (rendered as "+N more")

   // Broker compatibility check result (populated once at OnInit).
   BrokerCheckResult broker;

   // Signal-quality evaluation for the latest OPEN action (populated by
   // BuildStats from GET /actions/latest_open_evaluation). When
   // eval_available is false, the widget shows a "no signal yet" message.
   bool     eval_available;
   bool     eval_disabled;          // true when ai_evaluator_enabled=0 on the API. Takes precedence over eval_available — tile shows "evaluation disabled" instead of score / empty-state.
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
   int      m_target_h;     // measured content height; drives auto-shrink

   ulong    HashStats(const DashboardStats &s);
   void     DrawTitleBar(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawBrokerTile(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawTileHealth(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawTileNow(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawTileOpenTrades(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawTileSignalQuality(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawTileLastAction(const DashboardStats &s, int x, int y, int w, int h);
   void     DrawTileToday(const DashboardStats &s, int x, int y, int w, int h);
   void     TileBackdrop(int x, int y, int w, int h, uint bg, string title);
   uint     TileMutedFor(uint bg);
   void     Pill(int x, int y, string text, uint bg, uint fg);
   string   FmtDuration(int sec);
   string   FmtSigned(double v, int decimals);
   uint     PnlColor(double v);

public:
   CDashboard() : m_name("CT_Dashboard"), m_width(380), m_height(900),
                  m_x(20), m_y(20), m_font("Consolas"),
                  m_font_size(10), m_font_size_big(12), m_last_hash(0),
                  m_target_h(0) {}
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
   // Windows-Phone tile layout:
   //   - Full-width title bar at the top (COPYTRADES + channel + status pill)
   //   - Optional BROKER tile (full-width) when checks have failures/warnings
   //   - 2-column × 3-row grid of square-ish content tiles below
   //   - Translucent BG between tiles → chart shows through the gaps,
   //     reinforcing "tiles floating on chart" instead of "panel with sections"
   //
   // Layout constants live inline (named) so tweaks stay co-located.
   const int PAD       = 10;   // outer padding around the whole canvas
   const int GAP       = 8;    // gap between tiles
   const int TITLE_H   = 60;   // title bar height
   const int TILE_H    = 124;  // tile height — taller than wide gives the
                               // dominant number + label rows breathing room
   const int TILE_W    = (m_width - PAD * 2 - GAP) / 2;

   // Skip when content AND canvas size are stable. The m_target_h vs
   // m_height gate forces one extra paint after a tile appears/disappears
   // (BROKER tile toggling) so the canvas resizes correctly.
   ulong h = HashStats(s);
   if(h == m_last_hash && m_target_h == m_height) return;
   m_last_hash = h;

   // Apply last frame's measured height before erasing. First paint uses
   // the constructor default; subsequent paints settle within one frame.
   if(m_target_h > 0 && m_target_h != m_height) {
      m_canvas.Resize(m_width, m_target_h);
      m_height = m_target_h;
   }

   m_canvas.Erase(DSH_BG);

   int y = PAD;
   int colL = PAD;
   int colR = PAD + TILE_W + GAP;

   // 1. Title bar.
   DrawTitleBar(s, PAD, y, m_width - PAD * 2, TITLE_H);
   y += TILE_H == 0 ? TITLE_H : TITLE_H;  // explicit for readability
   y += GAP;

   // 2. Optional BROKER tile (full-width). Hidden when checks all
   //    passed; surfaces only when there are FAIL/WARN issues to act on.
   bool showBroker = s.broker.ran && s.broker.count > 0;
   if(showBroker) {
      int brokerH = 40 + s.broker.count * 30;       // header + 30/issue
      if(brokerH > 160) brokerH = 160;              // cap so we don't push the grid offscreen
      DrawBrokerTile(s, PAD, y, m_width - PAD * 2, brokerH);
      y += brokerH + GAP;
   }

   // 3. Content tile grid (3 rows × 2 cols).
   DrawTileHealth      (s, colL, y, TILE_W, TILE_H);
   DrawTileNow         (s, colR, y, TILE_W, TILE_H);
   y += TILE_H + GAP;
   DrawTileOpenTrades  (s, colL, y, TILE_W, TILE_H);
   DrawTileSignalQuality(s, colR, y, TILE_W, TILE_H);
   y += TILE_H + GAP;
   DrawTileLastAction  (s, colL, y, TILE_W, TILE_H);
   DrawTileToday       (s, colR, y, TILE_W, TILE_H);
   y += TILE_H;

   m_target_h = y + PAD;
   m_canvas.Update();
}

//--- sections -------------------------------------------------------

void CDashboard::DrawTitleBar(const DashboardStats &s, int x, int y, int w, int h) {
   // Title-bar tile: solid backdrop, brand + channel left-aligned, status
   // pill right-aligned. Slightly darker than the content tiles so it
   // reads as the panel "frame".
   m_canvas.FillRectangle(x, y, x + w, y + h, DSH_TILE_HEADER);

   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   m_canvas.TextOut(x + 14, y + 12, "COPYTRADES",
                    DSH_ACCENT, TA_LEFT | TA_TOP);
   // Channel name aligned to the SAME baseline as the brand. Pill takes
   // ~70px on the right (Pill width 68 + 12px clearance) so the channel
   // string is clipped to whatever fits between (brand right edge ≈ 128px)
   // and (right pad start = w - 84). Two EAs with absurdly long stack
   // names degrade gracefully via the right-edge truncation.
   if(StringLen(s.channel_name) > 0) {
      m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
      m_canvas.TextOut(x + 14 + 122, y + 14, s.channel_name,
                       DSH_TEXT, TA_LEFT | TA_TOP);
      m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   }

   string pill_text; uint pillBg;
   if(s.kill_switch_on)        { pill_text = "HALTED";   pillBg = DSH_DANGER; }
   else if(!s.api_ok)          { pill_text = "API DOWN"; pillBg = DSH_DANGER; }
   else if(!s.algo_allowed)    { pill_text = "ALGO OFF"; pillBg = DSH_WARN;   }
   else                        { pill_text = "LIVE";     pillBg = DSH_OK;     }
   Pill(x + w - 82, y + 10, pill_text, pillBg, 0xFF000000);

   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   m_canvas.TextOut(x + 14, y + 36,
      "uptime " + FmtDuration(s.uptime_sec) + "   " + s.account_ccy,
      DSH_MUTED, TA_LEFT | TA_TOP);
}

// Shared tile primitive: solid fill + small uppercase section title in
// the top-left corner. Caller draws content at (x+12, y+30) onward.
void CDashboard::TileBackdrop(int x, int y, int w, int h, uint bg, string title) {
   m_canvas.FillRectangle(x, y, x + w, y + h, bg);
   m_canvas.FontSet(m_font, -9 * 10, FW_BOLD);
   m_canvas.TextOut(x + 12, y + 10, title, DSH_TEXT, TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

// Pick a "muted" label colour that stays readable against a given tile
// background. The default DSH_MUTED (slate-cyan) disappears against
// bright amber/lime/magenta tiles — each saturated bg needs its own
// muted companion in the same hue family but lighter so it reads as
// "secondary text" rather than "broken/disabled".
uint CDashboard::TileMutedFor(uint bg) {
   if(bg == DSH_TILE_AMBER)   return 0xFFEEDFB8;   // pale cream on deep amber
   if(bg == DSH_TILE_LIME)    return 0xFFC8E8C0;   // pale sage on deep green
   if(bg == DSH_TILE_MAGENTA) return 0xFFF0C8DC;   // pale pink on deep magenta
   if(bg == DSH_TILE_CORAL)   return 0xFFF5D8C8;   // pale salmon on coral
   if(bg == DSH_TILE_CYAN)    return 0xFFA8C8E0;   // pale ice on deep teal
   if(bg == DSH_TILE_SLATE)   return DSH_MUTED;    // slate-cyan still reads well
   return DSH_MUTED;
}

void CDashboard::DrawTileHealth(const DashboardStats &s, int x, int y, int w, int h) {
   // Cyan = healthy; flip to magenta if anything's actually broken so
   // the tile colour itself screams the state.
   bool anyBad = !s.api_ok || s.kill_switch_on || !s.algo_allowed;
   uint bg = anyBad ? DSH_TILE_MAGENTA : DSH_TILE_CYAN;
   uint muted = TileMutedFor(bg);
   TileBackdrop(x, y, w, h, bg, "HEALTH");

   // Labels intentionally short ("API/Kill/Algo") so the value column
   // doesn't crash into the label column on a 177-px tile. The values
   // themselves still carry the semantic meaning.
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   int row = y + 38;
   m_canvas.TextOut(x + 12, row, "API", muted, TA_LEFT | TA_TOP);
   m_canvas.TextOut(x + w - 12, row,
                    s.api_ok ? "OK" : "DOWN",
                    s.api_ok ? DSH_OK : DSH_DANGER,
                    TA_RIGHT | TA_TOP);
   row += 22;
   m_canvas.TextOut(x + 12, row, "Kill", muted, TA_LEFT | TA_TOP);
   m_canvas.TextOut(x + w - 12, row,
                    s.kill_switch_on ? "ON" : "off",
                    s.kill_switch_on ? DSH_DANGER : DSH_TEXT,
                    TA_RIGHT | TA_TOP);
   row += 22;
   m_canvas.TextOut(x + 12, row, "Algo", muted, TA_LEFT | TA_TOP);
   m_canvas.TextOut(x + w - 12, row,
                    s.algo_allowed ? "on" : "off",
                    s.algo_allowed ? DSH_OK : DSH_WARN,
                    TA_RIGHT | TA_TOP);
}

void CDashboard::DrawTileNow(const DashboardStats &s, int x, int y, int w, int h) {
   // Live P&L tile — colour flips by sign. Negative P&L turns the whole
   // tile magenta-pink as a glance-warning; flat / positive stays lime.
   uint bg = (s.open_pnl < 0) ? DSH_TILE_MAGENTA : DSH_TILE_LIME;
   uint muted = TileMutedFor(bg);
   TileBackdrop(x, y, w, h, bg, "NOW");

   m_canvas.FontSet(m_font, -18 * 10, FW_BOLD);    // dominant value
   m_canvas.TextOut(x + 12, y + 38,
                    FmtSigned(s.open_pnl, 2),
                    DSH_TEXT, TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -9 * 10, FW_NORMAL);
   m_canvas.TextOut(x + 12, y + 78,
                    "open P&L  " + s.account_ccy,
                    muted, TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

void CDashboard::DrawTileOpenTrades(const DashboardStats &s, int x, int y, int w, int h) {
   // Magenta when in a trade (attention-grabbing); slate when flat.
   uint bg = (s.open_trades_count > 0) ? DSH_TILE_MAGENTA : DSH_TILE_SLATE;
   uint muted = TileMutedFor(bg);
   TileBackdrop(x, y, w, h, bg, "OPEN TRADE");

   if(s.open_trades_count == 0) {
      m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
      m_canvas.TextOut(x + 12, y + 50, "no position",
                       muted, TA_LEFT | TA_TOP);
      return;
   }

   // Single-position invariant — index 0 only. Layout, top to bottom:
   //   y+30  header  (BUY 4508.94 + small "Stage 2/3" on the right)
   //   y+50  TP1 row (status glyph + price + colour-coded)
   //   y+64  TP2 row
   //   y+78  TP3 row
   //   y+102 P&L     (big number, right-aligned)
   //
   // Glyph + colour per TP:
   //   k < stage  → "*" + DSH_OK     (hit, green)
   //   k == stage → ">" + DSH_ACCENT (next target, brand colour)
   //   k > stage  → "-" + DSH_TEXT   (pending, neutral)
   // Mirrors the pre-tile DrawOpenTrades layout the operator was
   // used to before the dashboard redesign.

   int n = s.open_trades[0].tpCount;
   int stage = s.open_trades[0].stage;

   // Header line: side + entry on the left, stage marker on the right.
   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   string head = (s.open_trades[0].isBuy ? "BUY  " : "SELL ")
               + DoubleToString(s.open_trades[0].entry, 2);
   m_canvas.TextOut(x + 12, y + 30, head, DSH_TEXT, TA_LEFT | TA_TOP);
   if(s.open_trades[0].hasPlan && n > 0) {
      m_canvas.FontSet(m_font, -9 * 10, FW_NORMAL);
      string st = IntegerToString(stage) + " / " + IntegerToString(n);
      m_canvas.TextOut(x + w - 12, y + 34, st, muted, TA_RIGHT | TA_TOP);
   }

   // TP rows. Up to 3 TPs; render each on its own 14-px line. If the
   // signal had only 1 TP and there's no plan (legacy / single-TP
   // mode), fall back to a single "TP 4530" line.
   m_canvas.FontSet(m_font, -9 * 10, FW_NORMAL);
   if(!s.open_trades[0].hasPlan && n <= 1) {
      string line = n > 0
                  ? "TP " + DoubleToString(s.open_trades[0].tps[0], 2)
                  : "no TP";
      m_canvas.TextOut(x + 12, y + 52, line, DSH_TEXT, TA_LEFT | TA_TOP);
   } else {
      int rowY = y + 50;
      int maxRows = (n > 3 ? 3 : n);
      for(int k = 0; k < maxRows; k++) {
         string glyph;
         uint   tpCol;
         if(k < stage)       { glyph = "*"; tpCol = DSH_OK;     }
         else if(k == stage) { glyph = ">"; tpCol = DSH_ACCENT; }
         else                { glyph = "-"; tpCol = DSH_TEXT;   }
         string line = glyph + " TP" + IntegerToString(k + 1) + " "
                     + DoubleToString(s.open_trades[0].tps[k], 2);
         m_canvas.TextOut(x + 12, rowY, line, tpCol, TA_LEFT | TA_TOP);
         rowY += 14;
      }
   }

   // P&L at the bottom-right of the tile, dominant. Padding from the
   // right edge so it doesn't crash into the rim.
   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   double pnl = s.open_trades[0].profit_total;
   m_canvas.TextOut(x + w - 12, y + h - 22,
                    FmtSigned(pnl, 2),
                    PnlColor(pnl), TA_RIGHT | TA_TOP);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

void CDashboard::DrawTileSignalQuality(const DashboardStats &s, int x, int y, int w, int h) {
   // Tile colour tracks verdict so the eye lands on the conclusion
   // before reading the number.
   uint bg;
   if(s.eval_disabled)                   bg = DSH_TILE_AMBER;
   else if(!s.eval_available)            bg = DSH_TILE_SLATE;
   else if(s.eval_verdict == "strong")   bg = DSH_TILE_LIME;
   else if(s.eval_verdict == "moderate") bg = DSH_TILE_AMBER;
   else if(s.eval_verdict == "weak")     bg = DSH_TILE_CORAL;
   else if(s.eval_verdict == "avoid")    bg = DSH_TILE_MAGENTA;
   else                                  bg = DSH_TILE_SLATE;
   uint muted = TileMutedFor(bg);
   TileBackdrop(x, y, w, h, bg, "SIGNAL QUALITY");

   if(s.eval_disabled) {
      // Operator-visible signal that scores aren't being produced — trades
      // are still flowing, but score-tied sizing falls back to baseline.
      m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
      m_canvas.TextOut(x + 12, y + 40, "EVALUATION OFF",
                       DSH_TEXT, TA_LEFT | TA_TOP);
      m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
      m_canvas.TextOut(x + 12, y + 60, "disabled in Settings",
                       muted, TA_LEFT | TA_TOP);
      return;
   }

   if(!s.eval_available) {
      m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
      m_canvas.TextOut(x + 12, y + 50, "no signal evaluated yet",
                       muted, TA_LEFT | TA_TOP);
      return;
   }

   m_canvas.FontSet(m_font, -22 * 10, FW_BOLD);
   m_canvas.TextOut(x + 12, y + 36,
                    IntegerToString(s.eval_score),
                    DSH_TEXT, TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -9 * 10, FW_NORMAL);
   m_canvas.TextOut(x + 72, y + 56, "/ 100",
                    muted, TA_LEFT | TA_TOP);

   string verdict_up = s.eval_verdict;
   StringToUpper(verdict_up);
   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   m_canvas.TextOut(x + w - 12, y + 44, verdict_up,
                    DSH_TEXT, TA_RIGHT | TA_TOP);

   // Drawn-rectangle gauge: 10 segments, 4px tall. Replaces the prior
   // Unicode block glyphs (▰/▱) which fell back to tofu in MQL5's
   // default Consolas font. FillRectangle is crisp at any pixel size.
   // Filled segments: bright cyan-white (pops on every tile colour).
   // Empty segments: the tile-aware muted (visible on amber/lime, slate
   // on the dark tiles). Both fully opaque — alpha-blended muted from
   // before disappeared against bright backgrounds.
   const int segs = 10;
   const int barX = x + 12;
   const int barY = y + h - 18;
   const int barW = w - 24;
   const int segGap = 2;
   const int segW   = (barW - segGap * (segs - 1)) / segs;
   const int segH   = 6;
   int filled = (int)((s.eval_score + 5) / 10);
   if(filled < 0) filled = 0;
   if(filled > segs) filled = segs;
   for(int i = 0; i < segs; i++) {
      int sx = barX + i * (segW + segGap);
      uint segCol = (i < filled) ? DSH_TEXT : muted;
      m_canvas.FillRectangle(sx, barY, sx + segW, barY + segH, segCol);
   }
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

void CDashboard::DrawTileLastAction(const DashboardStats &s, int x, int y, int w, int h) {
   uint bg = DSH_TILE_SLATE;
   uint muted = TileMutedFor(bg);
   TileBackdrop(x, y, w, h, bg, "LAST ACTION");

   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   if(s.last_action_id <= 0) {
      m_canvas.TextOut(x + 12, y + 50, "no action yet",
                       muted, TA_LEFT | TA_TOP);
      return;
   }
   // Truncate the action_type so OPEN_INSTANT / ATTACH_SIGNAL fit on a
   // 177-px tile next to the action id. Cuts at the first underscore
   // (keeps OPEN, ATTACH, MOVE, CLOSE, etc.) and falls back to a hard
   // 10-char clip when there's no underscore.
   string atype = s.last_action_type;
   int us = StringFind(atype, "_");
   if(us > 0) atype = StringSubstr(atype, 0, us);
   if(StringLen(atype) > 10) atype = StringSubstr(atype, 0, 10);

   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   m_canvas.TextOut(x + 12, y + 36,
                    "#" + IntegerToString(s.last_action_id) + " " + atype,
                    DSH_TEXT, TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   uint statusCol = DSH_TEXT;
   if(s.last_action_status == "executed")      statusCol = DSH_OK;
   else if(s.last_action_status == "rejected") statusCol = DSH_WARN;
   else if(s.last_action_status == "failed")   statusCol = DSH_DANGER;
   m_canvas.TextOut(x + 12, y + 64, s.last_action_status,
                    statusCol, TA_LEFT | TA_TOP);
   m_canvas.TextOut(x + 12, y + 86,
                    FmtDuration(s.last_action_age_sec) + " ago",
                    muted, TA_LEFT | TA_TOP);
}

void CDashboard::DrawTileToday(const DashboardStats &s, int x, int y, int w, int h) {
   uint bg = DSH_TILE_AMBER;
   uint muted = TileMutedFor(bg);
   TileBackdrop(x, y, w, h, bg, "TODAY");

   // Two-row tally so we don't clip the rejected count on tight tiles.
   // Row 1: sig / exec.   Row 2: rej / chased.
   //
   // Zero-counts use the tile-aware muted (pale cream on amber) so they
   // stay readable but visibly secondary. Non-zero counts pop in WARN
   // amber-yellow (rej) or ACCENT cyan (chase) — high contrast on the
   // tile bg, hard to miss when something actually happened.
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   m_canvas.TextOut(x + 12, y + 36,
                    IntegerToString(s.signals_today) + " sig",
                    DSH_TEXT, TA_LEFT | TA_TOP);
   m_canvas.TextOut(x + w - 12, y + 36,
                    IntegerToString(s.executed_today) + " exec",
                    s.executed_today > 0 ? DSH_OK : muted,
                    TA_RIGHT | TA_TOP);
   m_canvas.TextOut(x + 12, y + 56,
                    IntegerToString(s.rejected_today) + " rej",
                    s.rejected_today > 0 ? DSH_WARN : muted,
                    TA_LEFT | TA_TOP);
   m_canvas.TextOut(x + w - 12, y + 56,
                    IntegerToString(s.chased_today) + " chase",
                    s.chased_today > 0 ? DSH_ACCENT : muted,
                    TA_RIGHT | TA_TOP);

   m_canvas.FontSet(m_font, -m_font_size_big * 10, FW_BOLD);
   m_canvas.TextOut(x + 12, y + 80,
                    FmtSigned(s.realized_pnl_today, 2),
                    PnlColor(s.realized_pnl_today), TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -9 * 10, FW_NORMAL);
   m_canvas.TextOut(x + 12, y + 104,
                    "realized  " + s.account_ccy,
                    muted, TA_LEFT | TA_TOP);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

void CDashboard::DrawBrokerTile(const DashboardStats &s, int x, int y, int w, int h) {
   // Full-width red tile when broker checks have failures/warnings — sits
   // above the content grid so it's the first thing the eye catches.
   TileBackdrop(x, y, w, h, DSH_TILE_MAGENTA,
                StringFormat("BROKER  %d/%d ok, %d FAIL, %d WARN",
                             s.broker.checks_run - s.broker.count,
                             s.broker.checks_run,
                             s.broker.fails, s.broker.warns));

   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
   int rowY = y + 32;
   for(int i = 0; i < s.broker.count && rowY < y + h - 16; i++) {
      uint   tagColor = (s.broker.issues[i].severity == BC_FAIL)
                        ? DSH_DANGER : DSH_WARN;
      string tag      = (s.broker.issues[i].severity == BC_FAIL)
                        ? "FAIL" : "WARN";
      m_canvas.TextOut(x + 12, rowY, tag, tagColor, TA_LEFT | TA_TOP);
      m_canvas.TextOut(x + 56, rowY, s.broker.issues[i].label,
                       DSH_TEXT, TA_LEFT | TA_TOP);
      rowY += 28;
   }
}

//--- primitives -----------------------------------------------------

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
   // Per-second age fields are quantized to /60 so the dashboard only
   // repaints on a minute-tick, not every second. Without this the whole
   // canvas erases + redraws every OnTimer tick — visible flicker.
   h = (h ^ (ulong)(s.api_age_sec / 60)) * 1099511628211UL;
   h = (h ^ (ulong)(s.kill_switch_on ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)(s.algo_allowed ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)(s.uptime_sec / 60)) * 1099511628211UL;
   h = (h ^ (ulong)s.last_action_id) * 1099511628211UL;
   h = (h ^ (ulong)(s.last_action_age_sec / 60)) * 1099511628211UL;
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
   h = (h ^ (ulong)s.open_trades_count) * 1099511628211UL;
   h = (h ^ (ulong)(s.broker.ran ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)s.broker.count) * 1099511628211UL;
   h = (h ^ (ulong)s.broker.fails) * 1099511628211UL;
   h = (h ^ (ulong)s.broker.checks_run) * 1099511628211UL;
   h = (h ^ (ulong)(s.eval_available ? 1 : 0)) * 1099511628211UL;
   h = (h ^ (ulong)(s.eval_disabled ? 1 : 0)) * 1099511628211UL;
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
