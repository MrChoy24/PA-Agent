"""Apply the PA Agent dark theme to a :class:`QApplication`.

Three layers cooperate:

1. ``QPalette`` + default font — covers native-drawn surfaces (menus, message
   boxes, tooltips, scroll areas) that a style sheet cannot reach.
2. Generated indicator icons — Qt renders ``QComboBox`` arrows and checked
   ``QCheckBox`` marks as images, and it cannot rasterise an inline SVG data
   URI, so a couple of tiny PNGs are painted into the user's temp cache and
   injected into the style sheet as ``url(...)`` values.
3. ``dark.qss`` — the actual design system, loaded from disk.

The function is deliberately defensive: a missing or unreadable style sheet
must never stop the application from starting.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPixmap
from PyQt6.QtWidgets import QApplication

from pa_agent.gui.theme import tokens as T

logger = logging.getLogger(__name__)

_QSS_PATH = Path(__file__).with_name("dark.qss")

# Placeholders replaced with real file paths (or with "" when icon painting is
# unavailable, in which case Qt falls back to its native indicator drawing).
_TOK_CHEVRON = "@@CHEVRON@@"
_TOK_CHEVRON_ACCENT = "@@CHEVRON_ACCENT@@"
_TOK_CHECK = "@@CHECK@@"
_TOK_ARROW_UP = "@@ARROW_UP@@"
_TOK_ARROW_DOWN = "@@ARROW_DOWN@@"

# Resolved from the token font stack ("Segoe UI", "Microsoft YaHei UI", ...)
_UI_FAMILY = T.FONT_UI.split(",")[0].strip().strip('"').strip("'")
_UI_POINT_SIZE = 10

_ICON_SCALE = 4  # supersample then downscale implicitly via width/height in QSS


# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------

def _build_palette() -> QPalette:
    """A Fusion-friendly palette matching the slate/teal style sheet."""
    pal = QPalette()
    role = QPalette.ColorRole

    pal.setColor(role.Window, QColor(T.BG))
    pal.setColor(role.WindowText, QColor(T.FG))
    pal.setColor(role.Base, QColor(T.BG))
    pal.setColor(role.AlternateBase, QColor(T.SURFACE_1))
    pal.setColor(role.ToolTipBase, QColor(T.SURFACE_1))
    pal.setColor(role.ToolTipText, QColor(T.FG))
    pal.setColor(role.PlaceholderText, QColor(T.FG_3))
    pal.setColor(role.Text, QColor(T.FG))
    pal.setColor(role.Button, QColor(T.SURFACE_3))
    pal.setColor(role.ButtonText, QColor(T.FG))
    pal.setColor(role.BrightText, QColor("#ffffff"))
    pal.setColor(role.Light, QColor(T.SURFACE_4))
    pal.setColor(role.Midlight, QColor(T.BORDER_SUBTLE))
    pal.setColor(role.Mid, QColor(T.BORDER_SUBTLE))
    pal.setColor(role.Dark, QColor(T.BG))
    pal.setColor(role.Shadow, QColor("#000000"))

    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, role.Window, QColor(T.BG))
        pal.setColor(group, role.WindowText, QColor(T.FG))
        pal.setColor(group, role.Base, QColor(T.BG))
        pal.setColor(group, role.Text, QColor(T.FG))
        pal.setColor(group, role.Button, QColor(T.SURFACE_3))
        pal.setColor(group, role.ButtonText, QColor(T.FG))
        pal.setColor(group, role.Highlight, QColor(T.HIGHLIGHT))
        pal.setColor(group, role.HighlightedText, QColor(T.SELECTION_TEXT))

    disabled = QPalette.ColorGroup.Disabled
    pal.setColor(disabled, role.WindowText, QColor(T.FG_3))
    pal.setColor(disabled, role.Text, QColor(T.FG_3))
    pal.setColor(disabled, role.ButtonText, QColor(T.FG_3))
    pal.setColor(disabled, role.Base, QColor(T.SURFACE_0))
    pal.setColor(disabled, role.Button, QColor(T.SURFACE_1))
    pal.setColor(disabled, role.Highlight, QColor(T.SURFACE_4))
    pal.setColor(disabled, role.HighlightedText, QColor(T.FG_2))
    return pal


# ---------------------------------------------------------------------------
# Indicator icons
# ---------------------------------------------------------------------------

def _icon_dir() -> Path:
    base = Path.home() / ".cache" / "pa_agent" / "theme_icons"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        import tempfile

        base = Path(tempfile.gettempdir()) / "pa_agent_theme_icons"
        base.mkdir(parents=True, exist_ok=True)
    return base


def _paint_chevron(path: Path, color: str, width: int = 12, height: int = 8) -> None:
    pix = QPixmap(width * _ICON_SCALE, height * _ICON_SCALE)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(1.7 * _ICON_SCALE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    w, h = pix.width(), pix.height()
    pad = 1.6 * _ICON_SCALE
    painter.drawLine(
        QPointF(pad, h / 2 - 1.0 * _ICON_SCALE),
        QPointF(w / 2, h / 2 + 1.6 * _ICON_SCALE),
    )
    painter.drawLine(
        QPointF(w / 2, h / 2 + 1.6 * _ICON_SCALE),
        QPointF(w - pad, h / 2 - 1.0 * _ICON_SCALE),
    )
    painter.end()
    pix.save(str(path), "PNG")


def _paint_check(path: Path, color: str, size: int = 12) -> None:
    pix = QPixmap(size * _ICON_SCALE, size * _ICON_SCALE)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(color))
    pen.setWidthF(2.0 * _ICON_SCALE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    w = pix.width()
    painter.drawLine(QPointF(w * 0.22, w * 0.52), QPointF(w * 0.42, w * 0.72))
    painter.drawLine(QPointF(w * 0.42, w * 0.72), QPointF(w * 0.79, w * 0.30))
    painter.end()
    pix.save(str(path), "PNG")


def _paint_triangle(path: Path, color: str, up: bool, width: int = 9, height: int = 6) -> None:
    """Filled triangle used by the spin-box steppers.

    The image is saved at its final pixel size: Qt centres a ``::up-arrow``
    image inside a small subcontrol rect and clips whatever overflows, so an
    oversized bitmap would only show its tip.
    """
    scale = _ICON_SCALE
    pix = QPixmap(width * scale, height * scale)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    w, h = float(pix.width()), float(pix.height())
    m = max(1.0, scale * 0.8)
    if up:
        points = [QPointF(m, h - m), QPointF(w - m, h - m), QPointF(w / 2, m)]
    else:
        points = [QPointF(m, m), QPointF(w - m, m), QPointF(w / 2, h - m)]
    painter.drawPolygon(points)
    painter.end()
    pix = pix.scaled(
        width,
        height,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pix.save(str(path), "PNG")


def _build_icon_urls() -> dict[str, str]:
    """Paint the indicator icons once and return QSS ``url(...)`` literals."""
    try:
        icons = _icon_dir()
        chevron = icons / "chevron_muted.png"
        chevron_accent = icons / "chevron_accent.png"
        check = icons / "check_dark.png"
        arrow_up = icons / "arrow_up.png"
        arrow_down = icons / "arrow_down.png"
        _paint_chevron(chevron, T.FG_2)
        _paint_chevron(chevron_accent, T.ACCENT_HOVER)
        _paint_check(check, "#04201c")
        _paint_triangle(arrow_up, T.FG_2, up=True)
        _paint_triangle(arrow_down, T.FG_2, up=False)

        def url(path: Path) -> str:
            return "url(%s)" % str(path).replace("\\", "/")

        return {
            _TOK_CHEVRON: url(chevron),
            _TOK_CHEVRON_ACCENT: url(chevron_accent),
            _TOK_CHECK: url(check),
            _TOK_ARROW_UP: url(arrow_up),
            _TOK_ARROW_DOWN: url(arrow_down),
        }
    except Exception:  # pragma: no cover - painting is best effort
        logger.debug("Could not generate theme indicator icons", exc_info=True)
        return {
            _TOK_CHEVRON: "",
            _TOK_CHEVRON_ACCENT: "",
            _TOK_CHECK: "",
            _TOK_ARROW_UP: "",
            _TOK_ARROW_DOWN: "",
        }


# ---------------------------------------------------------------------------
# Style sheet
# ---------------------------------------------------------------------------

def _load_stylesheet() -> str:
    try:
        return _QSS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("Theme file not found: %s", _QSS_PATH)
    except OSError as exc:  # unreadable / locked file
        logger.warning("Could not read theme file %s: %s", _QSS_PATH, exc)
    return ""


def _render_stylesheet(template: str) -> str:
    if not template:
        return ""
    rendered = template
    for token, value in _build_icon_urls().items():
        rendered = rendered.replace(token, value)
    return rendered


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")

    font = QFont(_UI_FAMILY)
    font.setPointSize(_UI_POINT_SIZE)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)

    app.setPalette(_build_palette())

    stylesheet = _render_stylesheet(_load_stylesheet())
    if stylesheet:
        app.setStyleSheet(stylesheet)
