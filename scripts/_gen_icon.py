"""One-shot: render multi-size copytrades.ico from Qt-drawn pixmaps."""
from __future__ import annotations

import struct
from pathlib import Path

from PySide6.QtCore import Qt, QBuffer, QIODevice
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap


def render(size: int) -> bytes:
    pix = QPixmap(size, size)
    pix.fill(QColor("transparent"))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#859900"))
    p.setPen(QColor("#073642"))
    pad = max(2, size // 8)
    p.drawEllipse(pad, pad, size - 2 * pad, size - 2 * pad)
    p.setPen(QColor("white"))
    f = QFont()
    f.setBold(True)
    f.setPointSize(max(8, int(size * 0.30)))
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "CT")
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pix.save(buf, "PNG")
    return bytes(buf.data())


def main() -> None:
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [(s, render(s)) for s in sizes]
    n = len(images)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    data = b""
    offset = 6 + 16 * n
    for size, png in images:
        w = 0 if size == 256 else size
        h = 0 if size == 256 else size
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    out = Path("copytrades.ico")
    out.write_bytes(header + entries + data)
    print(f"wrote {out.resolve()}  ·  {out.stat().st_size} bytes")


if __name__ == "__main__":
    main()
