//+------------------------------------------------------------------+
//|  LogPanel.mqh — CCanvas-based action-stream panel for CopyTrades |
//|                                                                  |
//|  Renders the last N rows from GET /events/recent as a side panel |
//|  next to the main dashboard. Each row is one line:               |
//|    HH:MM  ✓ OPEN BUY 4715.20 sl 4704                             |
//|    HH:MM  ✗ MODIFY_TPS modify_failed:10025                       |
//|                                                                  |
//|  Repaint is gated on a hash of the event-id list, so a poll that |
//|  returns the same head rows does zero canvas work.               |
//+------------------------------------------------------------------+
#property strict
#include <Canvas\Canvas.mqh>

// Match Dashboard.mqh's Dark Luxury palette so the two widgets read as
// one UI. Defined locally (not via include of Dashboard.mqh) so this
// header has zero dependencies beyond the standard Canvas library.
// Keep the LP_* values in sync if you re-tone Dashboard.mqh's DSH_*.
#define LP_BG       0xFF0A0A0A   // near-black with a hint of warmth
#define LP_BORDER   0xFF3D2F18   // bronze-tinted border
#define LP_TEXT     0xFFE8E2D4   // warm cream off-white
#define LP_MUTED    0xFF8C7E66   // warm bronze-grey
#define LP_OK       0xFF7BB369   // refined desaturated green
#define LP_WARN     0xFFE0A040   // amber-gold (transitional / partial)
#define LP_DANGER   0xFFB23B3B   // deep burgundy
#define LP_INFO     0xFFD4AF37   // classic gold (#D4AF37) — was cool blue

#define LP_MAX_EVENTS 32

struct LogEvent {
   long     id;
   string   ts;            // HH:MM (extracted from created_at on the EA side)
   string   type;          // OPEN | MOVE_SL | CLOSE_PARTIAL | ALERT | ...
   string   status;        // executed | failed | rejected | watching | pending | sent
   string   summary;       // one-line body from the API
   string   ea_response;   // optional suffix shown muted when present
};

class CLogPanel {
private:
   CCanvas  m_canvas;
   string   m_name;
   int      m_width;
   int      m_height;
   int      m_x;
   int      m_y;
   string   m_font;
   int      m_font_size;
   int      m_cursor_y;
   ulong    m_last_hash;

   bool     m_visible;

   void     DrawHeader(int eventCount);
   void     DrawRow(const LogEvent &e);
   uint     StatusColor(string status);
   string   StatusGlyph(string status);
   ulong    HashEvents(const LogEvent &events[], int n);
   void     ApplyVisibility();
   int      WrapText(string text, int max_chars, string &out_lines[]);

public:
   CLogPanel() : m_name("CT_LogPanel"), m_width(560), m_height(900),
                 m_x(420), m_y(20), m_font("Consolas"),
                 m_font_size(10), m_cursor_y(0), m_last_hash(0),
                 m_visible(true) {}
   bool Create(int x, int y);
   void Destroy();
   void Update(const LogEvent &events[], int n);
   void Show();
   void Hide();
   void Toggle();
   bool IsVisible() const { return m_visible; }
};

//--- lifecycle ------------------------------------------------------

bool CLogPanel::Create(int x, int y) {
   m_x = x;
   m_y = y;
   if(!m_canvas.CreateBitmapLabel(m_name, m_x, m_y, m_width, m_height,
                                  COLOR_FORMAT_ARGB_NORMALIZE)) {
      Print("CT logpanel: CreateBitmapLabel failed, err=", GetLastError());
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
   m_canvas.Erase(LP_BG);
   m_canvas.Update();
   return true;
}

void CLogPanel::Destroy() {
   m_canvas.Destroy();
}

//--- update (gated by hash of event-id list) ------------------------

void CLogPanel::Update(const LogEvent &events[], int n) {
   if(!m_visible) return;
   if(n > LP_MAX_EVENTS) n = LP_MAX_EVENTS;
   ulong h = HashEvents(events, n);
   if(h == m_last_hash) return;
   m_last_hash = h;

   m_canvas.Erase(LP_BG);
   m_canvas.Rectangle(0, 0, m_width - 1, m_height - 1, LP_BORDER);

   m_cursor_y = 10;
   DrawHeader(n);

   // Variable row height now (wrap, not truncate). Stop drawing once the
   // next row's first line would fall outside the bottom margin so we
   // never spill text past the canvas border.
   for(int i = 0; i < n; i++) {
      if(m_cursor_y + 17 > m_height - 10) break;
      DrawRow(events[i]);
   }

   if(n == 0) {
      m_canvas.TextOut(12, m_cursor_y, "no actions yet",
                       LP_MUTED, TA_LEFT | TA_TOP);
   }

   m_canvas.Update();
}

//--- internal -------------------------------------------------------

void CLogPanel::DrawHeader(int eventCount) {
   m_canvas.FontSet(m_font, -(m_font_size + 1) * 10, FW_BOLD);
   m_canvas.TextOut(12, m_cursor_y, "ACTION LOG",
                    LP_TEXT, TA_LEFT | TA_TOP);
   string countLabel = StringFormat("(%d)", eventCount);
   m_canvas.TextOut(m_width - 12, m_cursor_y, countLabel,
                    LP_MUTED, TA_RIGHT | TA_TOP);
   m_cursor_y += 22;
   // Underline.
   m_canvas.LineHorizontal(8, m_width - 8, m_cursor_y - 4, LP_BORDER);
   m_canvas.FontSet(m_font, -m_font_size * 10, FW_NORMAL);
}

void CLogPanel::DrawRow(const LogEvent &e) {
   // Layout: [HH:MM] [glyph] [TYPE summary] (... ea_response)
   // Glyph + type + summary on one line; row height 17 px.
   m_canvas.TextOut(8, m_cursor_y, e.ts, LP_MUTED, TA_LEFT | TA_TOP);

   uint glyphColor = StatusColor(e.status);
   string glyph = StatusGlyph(e.status);
   m_canvas.TextOut(50, m_cursor_y, glyph, glyphColor, TA_LEFT | TA_TOP);

   // Body: TYPE + summary. Wraps across multiple lines instead of being
   // truncated — the operator needs to see the full reason on rejected
   // or failed actions.
   string body = e.type;
   if(StringLen(e.summary) > 0) body += " " + e.summary;
   string body_lines[];
   int nb = WrapText(body, 70, body_lines);
   for(int li = 0; li < nb; li++) {
      m_canvas.TextOut(70, m_cursor_y, body_lines[li],
                       LP_TEXT, TA_LEFT | TA_TOP);
      if(li < nb - 1) m_cursor_y += 14;
   }

   // ea_response (if any): wrapped on continuation lines in muted colour.
   // Skipped when empty to keep the panel compact.
   if(StringLen(e.ea_response) > 0) {
      m_cursor_y += 13;
      string resp_lines[];
      int nr = WrapText(e.ea_response, 82, resp_lines);
      for(int li = 0; li < nr; li++) {
         m_canvas.TextOut(70, m_cursor_y, resp_lines[li],
                          LP_MUTED, TA_LEFT | TA_TOP);
         if(li < nr - 1) m_cursor_y += 13;
      }
   }
   m_cursor_y += 17;
}

// Greedy word-wrap. Splits on spaces; breaks mid-word only when a single
// token exceeds max_chars (rare for action summaries — usually short
// phrases like "modify_failed:10025"). Returns the number of lines
// written into out_lines.
int CLogPanel::WrapText(string text, int max_chars, string &out_lines[]) {
   ArrayResize(out_lines, 0);
   if(StringLen(text) == 0) return 0;
   string remaining = text;
   while(StringLen(remaining) > 0) {
      if(StringLen(remaining) <= max_chars) {
         int sz = ArraySize(out_lines);
         ArrayResize(out_lines, sz + 1);
         out_lines[sz] = remaining;
         break;
      }
      // Look for the last space at-or-before max_chars to break cleanly.
      int cut = -1;
      for(int j = max_chars; j > 0; j--) {
         if(StringGetCharacter(remaining, j) == ' ') { cut = j; break; }
      }
      if(cut <= 0) cut = max_chars;  // long token, hard-break
      int sz = ArraySize(out_lines);
      ArrayResize(out_lines, sz + 1);
      out_lines[sz] = StringSubstr(remaining, 0, cut);
      // Skip the space we broke on.
      int next_start = (cut < StringLen(remaining)
                        && StringGetCharacter(remaining, cut) == ' ')
                       ? cut + 1 : cut;
      remaining = StringSubstr(remaining, next_start);
   }
   return ArraySize(out_lines);
}

uint CLogPanel::StatusColor(string status) {
   if(status == "executed") return LP_OK;
   if(status == "failed")   return LP_DANGER;
   if(status == "rejected") return LP_WARN;
   if(status == "watching") return LP_INFO;
   return LP_MUTED;  // pending | sent | claimed | unknown
}

string CLogPanel::StatusGlyph(string status) {
   // Plain-ASCII glyphs render reliably across MT5 fonts. Emoji glyphs
   // would be prettier but Consolas falls back to tofu on many setups.
   if(status == "executed") return "+";
   if(status == "failed")   return "x";
   if(status == "rejected") return "-";
   if(status == "watching") return "?";
   return ".";
}

//--- visibility -----------------------------------------------------
// Hide/Show flips OBJPROP_TIMEFRAMES on the bitmap label. OBJ_NO_PERIODS
// (-1) hides on all timeframes without destroying the canvas, so toggling
// is cheap and the cached bitmap survives. Re-show forces a hash reset
// so the next Update() repaints with current data.

void CLogPanel::ApplyVisibility() {
   long flag = m_visible ? OBJ_ALL_PERIODS : OBJ_NO_PERIODS;
   ObjectSetInteger(0, m_name, OBJPROP_TIMEFRAMES, flag);
   ChartRedraw(0);
}

void CLogPanel::Show() {
   if(m_visible) return;
   m_visible = true;
   m_last_hash = 0;  // force repaint on next Update
   ApplyVisibility();
}

void CLogPanel::Hide() {
   if(!m_visible) return;
   m_visible = false;
   ApplyVisibility();
}

void CLogPanel::Toggle() {
   if(m_visible) Hide(); else Show();
}

ulong CLogPanel::HashEvents(const LogEvent &events[], int n) {
   // FNV-1a over the event ids and statuses. id alone would suffice for
   // newly-arrived rows, but a status transition on the head row (e.g.
   // sent -> executed) also needs to repaint.
   ulong h = 1469598103934665603UL;
   for(int i = 0; i < n; i++) {
      h = (h ^ (ulong)events[i].id) * 1099511628211UL;
      // Mix the status length + first char as a cheap status-change tag.
      h = (h ^ (ulong)StringLen(events[i].status)) * 1099511628211UL;
      if(StringLen(events[i].status) > 0)
         h = (h ^ (ulong)StringGetCharacter(events[i].status, 0))
             * 1099511628211UL;
   }
   return h;
}
