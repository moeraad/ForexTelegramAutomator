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

   void     DrawHeader(int eventCount);
   void     DrawRow(const LogEvent &e);
   uint     StatusColor(string status);
   string   StatusGlyph(string status);
   ulong    HashEvents(const LogEvent &events[], int n);

public:
   CLogPanel() : m_name("CT_LogPanel"), m_width(360), m_height(900),
                 m_x(420), m_y(20), m_font("Consolas"),
                 m_font_size(10), m_cursor_y(0), m_last_hash(0) {}
   bool Create(int x, int y);
   void Destroy();
   void Update(const LogEvent &events[], int n);
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
   if(n > LP_MAX_EVENTS) n = LP_MAX_EVENTS;
   ulong h = HashEvents(events, n);
   if(h == m_last_hash) return;
   m_last_hash = h;

   m_canvas.Erase(LP_BG);
   m_canvas.Rectangle(0, 0, m_width - 1, m_height - 1, LP_BORDER);

   m_cursor_y = 10;
   DrawHeader(n);

   // Each row ~17 px. Leave a small bottom margin.
   int max_rows = (m_height - m_cursor_y - 10) / 17;
   if(max_rows > n) max_rows = n;
   for(int i = 0; i < max_rows; i++) {
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

   // Body: TYPE + summary, hard-truncated to fit ~280px column.
   string body = e.type;
   if(StringLen(e.summary) > 0) body += " " + e.summary;
   if(StringLen(body) > 38) body = StringSubstr(body, 0, 35) + "...";
   m_canvas.TextOut(70, m_cursor_y, body, LP_TEXT, TA_LEFT | TA_TOP);

   // ea_response (if any): drawn on the next half-line in muted colour.
   // Skipped when empty to keep the panel compact.
   if(StringLen(e.ea_response) > 0) {
      m_cursor_y += 13;
      string resp = e.ea_response;
      if(StringLen(resp) > 44) resp = StringSubstr(resp, 0, 41) + "...";
      m_canvas.TextOut(70, m_cursor_y, resp, LP_MUTED, TA_LEFT | TA_TOP);
   }
   m_cursor_y += 17;
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
