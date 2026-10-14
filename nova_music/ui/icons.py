"""Small inline Lucide-style SVG icons for a crisp, consistent player UI."""
from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from functools import lru_cache

_SHAPES = {
    "library": '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/><path d="M8 7h8M8 11h8"/>',
    "queue": '<path d="M4 6h16M4 12h16M4 18h10"/><path d="m18 16 3 2-3 2"/>',
    "folder-search": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v2"/><path d="M3 9v9.5A2.5 2.5 0 0 0 5.5 21h7"/><circle cx="17" cy="17" r="3"/><path d="m19.2 19.2 2 2"/>',
    "heart": '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z"/>',
    "sliders": '<path d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M2 14h4m4-6h4m4 8h4"/>',
    "sparkles": '<path d="m12 3 1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2L12 3Z"/><path d="m19 14 1.2 2.8L23 18l-2.8 1.2L19 22l-1.2-2.8L15 18l2.8-1.2L19 14Z"/><path d="m5 2 .8 2.2L8 5l-2.2.8L5 8l-.8-2.2L2 5l2.2-.8L5 2Z"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "x": '<path d="m18 6-12 12M6 6l12 12"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2m3 0-1 14H6L5 6m4 4v6m6-6v6"/>',
    "folder-plus": '<path d="M12 10v6m-3-3h6"/><path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H10l2 2h6.5A2.5 2.5 0 0 1 21 9.5v8a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5Z"/>',
    "shuffle": '<path d="m18 14 4 4-4 4"/><path d="m18 2 4 4-4 4"/><path d="M2 18h2.5a6 6 0 0 0 4.2-1.8L15.3 9a6 6 0 0 1 4.2-1.8H22M2 6h2.5a6 6 0 0 1 4.2 1.8l1.2 1.2m4.2 4.2 1.2 1.2a6 6 0 0 0 4.2 1.8H22"/>',
    "previous": '<path d="m19 20-9-8 9-8v16Z"/><path d="M5 19V5"/>',
    "play": '<path d="m7 4 13 8-13 8V4Z"/>',
    "pause": '<path d="M8 5v14M16 5v14"/>',
    "next": '<path d="m5 4 9 8-9 8V4Z"/><path d="M19 5v14"/>',
    "repeat": '<path d="m17 2 4 4-4 4"/><path d="M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4"/><path d="M21 13v2a3 3 0 0 1-3 3H3"/>',
    "volume": '<path d="m11 5-6 4H2v6h3l6 4V5Z"/><path d="M15.5 8.5a5 5 0 0 1 0 7m3-10a9 9 0 0 1 0 13"/>',
    "speaker": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 8h.01M7 12h.01M7 16h.01m4-8h6v8h-6z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    "disc": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/><path d="m4.9 4.9 2.1 2.1m10 10 2.1 2.1"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "expand": '<path d="M8 3H5a2 2 0 0 0-2 2v3m13-5h3a2 2 0 0 1 2 2v3M3 16v3a2 2 0 0 0 2 2h3m13-5v3a2 2 0 0 1-2 2h-3"/>',
    "refresh": '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.6-2.6L20 12M4 12l2.8 5.6A7 7 0 0 0 18.4 15"/>',
    "lyrics": '<path d="M8 18V5l12-2v13"/><circle cx="5" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "home": '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-6v-7h-4v7H4a1 1 0 0 1-1-1Z"/>',
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
}


@lru_cache(maxsize=128)
def icon(name: str, color: str = "#b3b3b3", size: int = 20) -> QIcon:
    body = _SHAPES.get(name, _SHAPES["disc"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
           f'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(QSize(size, size))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
