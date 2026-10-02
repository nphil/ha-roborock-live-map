"""Deterministic Pillow renderer for the Roborock Live Map brand kit.

Writes the eight images Home Assistant serves straight from
``custom_components/roborock_live_map/brand/`` (no brands-repo submission needed):

    icon.png        256x256      dark_icon.png        256x256
    icon@2x.png     512x512      dark_icon@2x.png     512x512
    logo.png        864x256      dark_logo.png        864x256
    logo@2x.png     1728x512     dark_logo@2x.png     1728x512

The mark: a small isometric room (a floor and two back walls: the 3D map) with
a flat robot-vacuum disc (lidar turret on top, bumper seam along the front)
standing on the floor, the route it has just driven leading up to it, and a
thin ping ring around it (the "live position" cue). Flat colour, no gradients,
no text. ``../icon.svg`` is the same mark as hand-authored vector art; this
script does not read it, so keep the numbers below in sync with it.

Light variant ("icon", "logo"): violet plate, white floor, amber robot.
Dark variant ("dark_icon", "dark_logo"): pale plate, violet room, orange robot,
so it keeps its contrast on the dark Home Assistant header.

Pillow only, 4x supersampling, no randomness: re-running produces
byte-identical PNGs.

Usage: python3 tools/render_brand.py
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = REPO_ROOT / "custom_components" / "roborock_live_map" / "brand"

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Liberation Sans Bold, then DejaVu Bold, then Pillow's default font."""
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


# ---------------------------------------------------------------------------
# Geometry, in a 256x256 reference space (identical numbers to ../icon.svg).
# The room is drawn 2:1 isometric: edges run at a slope of 1/2.
# ---------------------------------------------------------------------------

PLATE_RX = 56.0
ROOM_ROUND = 2.5  # room corners are rounded by a same-colour stroke of 2 * this

FLOOR = ((128.0, 106.0), (232.0, 158.0), (128.0, 210.0), (24.0, 158.0))
WALL_LEFT = ((24.0, 158.0), (128.0, 106.0), (128.0, 46.0), (24.0, 98.0))
WALL_RIGHT = ((128.0, 106.0), (232.0, 158.0), (232.0, 98.0), (128.0, 46.0))

PING = (152.0, 161.0, 44.0, 22.0, 4.0)  # cx, cy, rx, ry, stroke width
ROUTE = ((64.0, 163.0), (102.0, 144.0), (134.0, 160.0))  # start, bend, robot
ROUTE_R = 5.0  # half the route's stroke width
ROUTE_START_R = 9.0

# Cylinders: (cx, cy of the top face, rx, side height); ry is always rx / 2.
ROBOT = (152.0, 150.0, 33.0, 12.0)
TURRET = (150.0, 146.0, 11.0, 4.0)
# The robot's front bumper: a seam along the front of its top face, as an
# elliptical arc inset BUMPER_INSET from the rim, from BUMPER_START to BUMPER_END
# degrees (clockwise from the right-hand point, y pointing down).
BUMPER_INSET = 5.0
BUMPER_START, BUMPER_END = 5.0, 115.0
BUMPER_W = 3.2

# ---------------------------------------------------------------------------
# Palettes
# ---------------------------------------------------------------------------

LIGHT_VARIANT = {
    "plate": (0x5B, 0x45, 0xE0),
    "floor": (0xFF, 0xFF, 0xFF),
    "wall_left": (0x8B, 0x7C, 0xF2),
    "wall_right": (0xD9, 0xD2, 0xFF),
    "accent": (0xFF, 0xB8, 0x2E),
    "accent_side": (0xE0, 0x8A, 0x00),
    "turret": (0xFF, 0xFF, 0xFF),
    "ping": (0xFF, 0xB8, 0x2E),
}
DARK_VARIANT = {
    "plate": (0xF1, 0xEE, 0xFF),
    "floor": (0x5B, 0x45, 0xE0),
    "wall_left": (0x3A, 0x2A, 0xB0),
    "wall_right": (0x8B, 0x7C, 0xF2),
    "accent": (0xF5, 0x8A, 0x00),
    "accent_side": (0xB8, 0x62, 0x00),
    "turret": (0xFF, 0xFF, 0xFF),
    "ping": (0xC9, 0xBF, 0xFA),
}

WORDMARK_ON_LIGHT_BG = (0x1F, 0x17, 0x50)  # logo.png
SUBMARK_ON_LIGHT_BG = (0x5B, 0x45, 0xE0)
WORDMARK_ON_DARK_BG = (0xF5, 0xF3, 0xFF)  # dark_logo.png
SUBMARK_ON_DARK_BG = (0xB9, 0xAE, 0xF5)

SS = 4  # supersampling factor


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _circle(draw, cx, cy, r, k, fill):
    draw.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=fill)


def _capsule(draw, a, b, r, k, fill):
    """A round-capped stroke of half-width r from a to b."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy)
    nx, ny = -dy / length * r, dx / length * r
    draw.polygon(
        [((x0 + nx) * k, (y0 + ny) * k), ((x1 + nx) * k, (y1 + ny) * k),
         ((x1 - nx) * k, (y1 - ny) * k), ((x0 - nx) * k, (y0 - ny) * k)],
        fill=fill,
    )
    _circle(draw, x0, y0, r, k, fill)
    _circle(draw, x1, y1, r, k, fill)


def _rounded_polygon(draw, points, r, k, fill):
    """Filled polygon whose corners are rounded by a same-colour stroke of 2 * r."""
    draw.polygon([(x * k, y * k) for x, y in points], fill=fill)
    for a, b in zip(points, points[1:] + points[:1]):
        _capsule(draw, a, b, r, k, fill)


def _ellipse(draw, cx, cy, rx, ry, k, fill):
    draw.ellipse([(cx - rx) * k, (cy - ry) * k, (cx + rx) * k, (cy + ry) * k], fill=fill)


def _cylinder(draw, cylinder, k, top, side):
    """A flat disc seen from above at 2:1: side band first, then the top face."""
    cx, cy, rx, h = cylinder
    ry = rx / 2
    _ellipse(draw, cx, cy + h, rx, ry, k, side)
    draw.polygon(
        [((cx - rx) * k, cy * k), ((cx + rx) * k, cy * k),
         ((cx + rx) * k, (cy + h) * k), ((cx - rx) * k, (cy + h) * k)],
        fill=side,
    )
    _ellipse(draw, cx, cy, rx, ry, k, top)


def _ellipse_arc(draw, cx, cy, rx, ry, start_deg, end_deg, width, k, fill, step_deg=2.0):
    """An even-width, round-capped elliptical arc (stroke centred on the ellipse, like SVG).

    Angles run clockwise from the right-hand point because y points down.
    """
    steps = max(2, math.ceil(abs(end_deg - start_deg) / step_deg))
    points = [
        (
            cx + rx * math.cos(math.radians(start_deg + (end_deg - start_deg) * i / steps)),
            cy + ry * math.sin(math.radians(start_deg + (end_deg - start_deg) * i / steps)),
        )
        for i in range(steps + 1)
    ]
    for a, b in zip(points, points[1:]):
        _capsule(draw, a, b, width / 2, k, fill)


def render_mark(size, palette):
    """(size, size) RGBA icon: flat rounded plate with the room, route and robot."""
    big = size * SS
    k = big / 256.0
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=PLATE_RX * k, fill=palette["plate"] + (255,))

    _rounded_polygon(d, list(WALL_LEFT), ROOM_ROUND, k, palette["wall_left"] + (255,))
    _rounded_polygon(d, list(WALL_RIGHT), ROOM_ROUND, k, palette["wall_right"] + (255,))
    _rounded_polygon(d, list(FLOOR), ROOM_ROUND, k, palette["floor"] + (255,))

    cx, cy, rx, ry, width = PING
    _ellipse_arc(d, cx, cy, rx, ry, 0.0, 360.0, width, k, palette["ping"] + (255,))

    accent = palette["accent"] + (255,)
    for a, b in zip(ROUTE, ROUTE[1:]):
        _capsule(d, a, b, ROUTE_R, k, accent)
    _circle(d, ROUTE[0][0], ROUTE[0][1], ROUTE_START_R, k, accent)

    side = palette["accent_side"] + (255,)
    _cylinder(d, ROBOT, k, accent, side)
    robot_x, robot_y, robot_rx, _ = ROBOT
    inset = robot_rx - BUMPER_INSET
    _ellipse_arc(d, robot_x, robot_y, inset, inset / 2, BUMPER_START, BUMPER_END, BUMPER_W, k, side)
    _cylinder(d, TURRET, k, palette["turret"] + (255,), side)

    return canvas.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Wordmark lockup
# ---------------------------------------------------------------------------

LOGO_ASPECT = 864 / 256  # width / height (about 3.4x)
LOGO_MARK_FRAC = 0.86  # mark size as a fraction of canvas height
LOGO_LEFT_FRAC = 0.05
LOGO_GAP_FRAC = 0.11
LOGO_NAME_FRAC = 0.34  # "Roborock" font size / canvas height
LOGO_SUB_FRAC = 0.30  # "Live Map" font size / canvas height
LOGO_TRACK_FRAC = 0.004
LOGO_LINE_GAP_FRAC = 0.09


def _tracked_width(draw, text, font, tracking):
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x, y, text, font, fill, tracking):
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def render_logo(height, palette, name_rgb, sub_rgb):
    """RGBA lockup: mark on the left, "Roborock" over "Live Map" on the right,
    transparent background, exactly `height` tall."""
    width = round(height * LOGO_ASPECT)
    mark_size = round(height * LOGO_MARK_FRAC)
    mark = render_mark(mark_size, palette)

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    left = round(height * LOGO_LEFT_FRAC)
    canvas.alpha_composite(mark, (left, (height - mark_size) // 2))

    name_font = load_font(round(height * LOGO_NAME_FRAC))
    sub_font = load_font(round(height * LOGO_SUB_FRAC))
    tracking = height * LOGO_TRACK_FRAC
    draw = ImageDraw.Draw(canvas)

    # Vertically centre the two-line block on ink bounds, not font ascent.
    n_l, n_t, n_r, n_b = draw.textbbox((0, 0), "Roborock", font=name_font)
    s_l, s_t, s_r, s_b = draw.textbbox((0, 0), "Live Map", font=sub_font)
    line_gap = height * LOGO_LINE_GAP_FRAC
    block_h = (n_b - n_t) + line_gap + (s_b - s_t)
    top = (height - block_h) / 2
    text_x = left + mark_size + height * LOGO_GAP_FRAC

    name_w = _tracked_width(draw, "Roborock", name_font, tracking)
    _draw_tracked(draw, text_x - n_l, top - n_t, "Roborock", name_font, name_rgb + (255,), tracking)
    sub_y = top + (n_b - n_t) + line_gap
    _draw_tracked(draw, text_x - s_l, sub_y - s_t, "Live Map", sub_font, sub_rgb + (255,), tracking)

    sub_w = _tracked_width(draw, "Live Map", sub_font, tracking)
    right_edge = text_x + max(name_w, sub_w)
    assert right_edge <= width - height * 0.03, f"wordmark clipped at height {height}: {right_edge} > {width}"
    return canvas


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _save_pair(master, stem):
    """<stem>@2x.png from the 512-based master, <stem>.png as an exact half."""
    master.save(BRAND_DIR / f"{stem}@2x.png")
    half = (master.width // 2, master.height // 2)
    master.resize(half, Image.LANCZOS).save(BRAND_DIR / f"{stem}.png")


def main():
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    _save_pair(render_mark(512, LIGHT_VARIANT), "icon")
    _save_pair(render_mark(512, DARK_VARIANT), "dark_icon")
    _save_pair(render_logo(512, LIGHT_VARIANT, WORDMARK_ON_LIGHT_BG, SUBMARK_ON_LIGHT_BG), "logo")
    _save_pair(render_logo(512, DARK_VARIANT, WORDMARK_ON_DARK_BG, SUBMARK_ON_DARK_BG), "dark_logo")


if __name__ == "__main__":
    main()
