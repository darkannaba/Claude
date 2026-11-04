"""QR codes (ReportLab) : intégrés aux PDF et affichés à l'écran (SVG) pour la douchette."""
from __future__ import annotations

from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing


def qr_drawing(data: str, size: float = 80) -> Drawing:
    widget = QrCodeWidget(data, barBorder=2)
    x1, y1, x2, y2 = widget.getBounds()
    width, height = x2 - x1, y2 - y1
    drawing = Drawing(size, size, transform=[size / width, 0, 0, size / height, 0, 0])
    drawing.add(widget)
    return drawing


def qr_svg(data: str, size: float = 160) -> str:
    return renderSVG.drawToString(qr_drawing(data, size))
