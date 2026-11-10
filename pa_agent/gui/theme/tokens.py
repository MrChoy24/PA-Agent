"""Design tokens for the PA Agent dark trading-terminal theme.

The palette is a GitHub-dark style "deep slate" family with a teal accent.
Every value is a plain CSS colour string, so it can be dropped straight into
either the global Qt Style Sheet or a widget-level ``setStyleSheet`` call.

Only *additive* changes are safe here: other modules import individual
constants by name, so all previously exported names must stay available.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Surfaces — depth hierarchy, darkest first
# ---------------------------------------------------------------------------

# Backgrounds
BG = "#0a0e14"          # window / chart canvas
SURFACE_0 = "#12171f"   # panels, tab panes, headers
SURFACE_1 = "#161b22"   # cards / popups
SURFACE_2 = "#1c2128"   # raised controls, hover
SURFACE_3 = "#21262d"   # buttons, chips
SURFACE_4 = "#30363d"   # strong border

# Text
FG = "#e6edf3"
FG_2 = "#8b949e"
FG_3 = "#6e7681"

# Borders
BORDER_SUBTLE = "#232b36"
BORDER_STRONG = "#3d444d"

# Accents
ACCENT = "#2dd4bf"
ACCENT_2 = "#f87171"
ACCENT_3 = "#38bdf8"

# Accent derivatives (hover / pressed / tints) — decimal rgba only:
# Qt renders rgba(r,g,b,a) with a 0..1 float alpha reliably; 8-digit hex does not.
ACCENT_HOVER = "#5eead4"
ACCENT_PRESSED = "#14b8a6"
ACCENT_SOFT = "rgba(45,212,191,0.10)"
ACCENT_SOFT_STRONG = "rgba(45,212,191,0.18)"
ACCENT_BORDER = "rgba(45,212,191,0.40)"

ACCENT_3_HOVER = "#7dd3fc"
ACCENT_3_PRESSED = "#0ea5e9"
ACCENT_3_SOFT = "rgba(56,189,248,0.10)"
ACCENT_3_BORDER = "rgba(56,189,248,0.35)"

# Semantic
SUCCESS = "#22c55e"
DANGER = "#ef4444"
WARNING = "#f59e0b"
INFO = "#38bdf8"

SUCCESS_HOVER = "#4ade80"
DANGER_HOVER = "#f87171"
WARNING_HOVER = "#fbbf24"

# Selection / focus
SELECTION_BG = "rgba(45,212,191,0.16)"
SELECTION_BG_STRONG = "rgba(45,212,191,0.24)"
SELECTION_TEXT = "#e6edf3"
HIGHLIGHT = "#134e4a"
HOVER_BG = "#1c2128"

# Trading
CHART_UP = "#22c55e"
CHART_DOWN = "#ef4444"
CHART_GRID = "#1c2128"
CHART_LINE = "#fbbf24"
CHART_LINE_2 = "#7dd3fc"
CHART_LINE_3 = "#fb923c"

# Pills — names kept for backward compatibility
PILL_GREEN_TEXT = "#86efac"
PILL_GREEN_BORDER = "rgba(34,197,94,0.35)"
PILL_GREEN_BG = "rgba(34,197,94,0.10)"

PILL_AMBER_TEXT = "#fbbf24"
PILL_AMBER_BORDER = "rgba(245,158,11,0.35)"
PILL_AMBER_BG = "rgba(245,158,11,0.10)"

PILL_BLUE_TEXT = "#7dd3fc"
PILL_BLUE_BORDER = "rgba(56,189,248,0.35)"
PILL_BLUE_BG = "rgba(56,189,248,0.10)"

PILL_RED_TEXT = "#fca5a5"
PILL_RED_BORDER = "rgba(239,68,68,0.35)"
PILL_RED_BG = "rgba(239,68,68,0.10)"

PILL_CYAN_TEXT = "#5eead4"
PILL_CYAN_BORDER = "rgba(45,212,191,0.35)"
PILL_CYAN_BG = "rgba(45,212,191,0.10)"

# Typography
FONT_UI = '"Segoe UI", "Microsoft YaHei UI", sans-serif'
FONT_MONO = '"JetBrains Mono", "Cascadia Mono", "Consolas", monospace'

# Layout (8px rhythm)
RADIUS_SM = 6
RADIUS = 8
RADIUS_MD = 8
RADIUS_LG = 10
RADIUS_PILL = 999

SPACE_XS = 4
SPACING = 8
SPACE_LG = 12

# ---------------------------------------------------------------------------
# Legacy aliases (backward-compatible names mapped to the new palette)
# ---------------------------------------------------------------------------
BG_BASE = BG
BG_PANEL = SURFACE_1
BG_ELEVATED = SURFACE_2
BG_REASONING = SURFACE_2
BG_INPUT = BG

BORDER = SURFACE_4
BORDER_MUTED = SURFACE_3

TEXT_PRIMARY = FG
TEXT_SECONDARY = FG_2
TEXT_MUTED = FG_3

ACCENT_PRIMARY = ACCENT_3
ACCENT_REASONING = ACCENT
ACCENT_SUCCESS = SUCCESS
ACCENT_WARNING = WARNING
ACCENT_DANGER = DANGER

TRADE_LONG = CHART_UP
TRADE_SHORT = CHART_DOWN
TRADE_NEUTRAL = FG_2

TOKEN_YELLOW = WARNING
TOKEN_RED = DANGER
