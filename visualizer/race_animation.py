"""
F1 race spaghetti-chart animation.

X axis design
─────────────
  x_min = 0 always (race origin, fixed left edge)
  x_max = leader's total_distance, smoothed (grows from 0 → finish_distance)
  scale = NON-LINEAR power transform so the near-leader zone is visually
          expanded; leader alone is at far right, and every other car is
          pulled measurably away from it.

  bar_frac = 1 – (gap_frac) ^ X_POWER   where gap_frac = 1 – dist/x_max

History lines
─────────────
  Each car accumulates (total_distance, y_pixel) points.  At render time
  total_distance is re-projected through the current x scale, so as x_max
  grows the older history naturally compresses toward the left.

Lap markers + finish flag
─────────────────────────
  Leader's lap transitions are detected online; the total_distance at each
  lap boundary is recorded and drawn as a thin vertical line with a label.
  The checkered finish flag uses the same sliding-in mechanic: only visible
  once x_max has grown past finish_distance.

Outro (end sequence)
─────────────────────
  After live data ends, all active (non-DNF) cars move at the same constant
  speed toward finish_distance.  DNF ghost cars remain frozen at their last
  position throughout the outro.

Output: mp4 via ffmpeg subprocess (ffmpeg must be on PATH).
"""
import math
import os
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# ── team palette ─────────────────────────────────────────────────────────────

TEAM_COLOURS: Dict[int, tuple] = {
    0: (0, 210, 190),    # Mercedes
    1: (220, 0, 0),      # Ferrari
    2: (54, 113, 198),   # Red Bull
    3: (0, 90, 255),     # Williams
    4: (0, 111, 98),     # Aston Martin
    5: (255, 135, 188),  # Alpine
    6: (100, 146, 255),  # Racing Bulls
    7: (182, 186, 189),  # Haas
    8: (255, 128, 0),    # McLaren
    9: (0, 207, 70),     # Kick Sauber
}
DEFAULT_COLOUR = (150, 150, 150)

TEAM_NAMES: Dict[int, str] = {
    0: "Mercedes",    1: "Ferrari",   2: "Red Bull", 3: "Williams",
    4: "Aston Martin",5: "Alpine",    6: "RB",       7: "Haas",
    8: "McLaren",     9: "Kick Sauber",
}

# ── canvas ───────────────────────────────────────────────────────────────────

W, H = 1280, 720
FPS = 30
TARGET_FRAMES = FPS * 20          # 600 frames ≈ 20 s

HEADER_H   = 72
FOOTER_H   = 44
LEFT_W     = 178                  # team name column
LINE_AREA  = 840                  # x-axis pixel width
ICON_SIZE  = 28

X_POWER = 0.40

ALPHA_X    = 0.08
ALPHA_Y    = 0.18
ALPHA_DIST = 0.15

OUTRO_S    = 1.5
RACE_SPEED = 0.85  # race playback speed multiplier (< 1 = slower)
LINEAR_TRANSITION_S = 3   # seconds to animate non-linear → linear scale after outro
GRID_BLEND_DURATION = 60.0   # seconds over which start-grid layout blends into race scale

# Colours
BG        = (10, 10, 22)
STRIPE    = (16, 16, 32)
TEXT      = (238, 238, 255)
DIM       = (95, 95, 122)
GOLD      = (255, 200, 50)
SC_COL    = (255, 210, 0)
GRID      = (28, 28, 50)
LAP_LINE  = (40, 40, 65)
WARN_COL  = (255, 200, 0)
PEN_COL   = (220, 60, 60)
PIT_COL   = (0, 180, 255)
FL_COL    = (175, 0, 255)
RF_COL    = (200, 0, 0)    # Red flag banner
HIST_DIM  = 0.60

SC_LABELS = {1: "SAFETY CAR", 2: "VIRTUAL SAFETY CAR", 3: "SC ENDING"}

# F1 25 visual_tyre_compound → display colour
TYRE_COLOURS: Dict[int, tuple] = {
    16: (220,  40,  40),   # Soft   — red
    17: (220, 200,   0),   # Medium — yellow
    18: (230, 230, 230),   # Hard   — white
     7: ( 80, 200,  80),   # Inter  — green
     8: ( 60, 120, 220),   # Wet    — blue
}

# track_id (from F1 25 UDP) → ISO 3166-1 alpha-2 country code
TRACK_FLAGS: Dict[int, str] = {
    0:  "au",   # Melbourne
    1:  "fr",   # Paul Ricard
    2:  "cn",   # Shanghai
    3:  "bh",   # Sakhir (Bahrain)
    4:  "es",   # Catalunya
    5:  "mc",   # Monaco
    6:  "ca",   # Montreal
    7:  "gb",   # Silverstone
    8:  "de",   # Hockenheim
    9:  "hu",   # Hungaroring
    10: "be",   # Spa
    11: "it",   # Monza
    12: "sg",   # Singapore
    13: "jp",   # Suzuka
    14: "ae",   # Abu Dhabi
    15: "us",   # Texas (COTA)
    16: "br",   # Brazil (Interlagos)
    17: "at",   # Austria (Red Bull Ring)
    18: "ru",   # Sochi
    19: "mx",   # Mexico City
    20: "az",   # Baku
    21: "bh",   # Sakhir Short
    22: "gb",   # Silverstone Short
    23: "us",   # Texas Short
    24: "jp",   # Suzuka Short
    25: "vn",   # Hanoi
    26: "nl",   # Zandvoort
    27: "it",   # Imola
    28: "pt",   # Portimão
    29: "sa",   # Jeddah
    30: "us",   # Miami
    31: "us",   # Las Vegas
    32: "qa",   # Losail (Qatar)
}

_FLAGS_DIR = Path(__file__).parent / "flags"

# Fallback: track_name substring → flag code (covers old DBs without track_id column)
_TRACK_NAME_FLAGS: Dict[str, str] = {
    "melbourne": "au", "paul ricard": "fr", "shanghai": "cn",
    "bahrain": "bh", "sakhir": "bh", "catalunya": "es", "monaco": "mc",
    "montreal": "ca", "silverstone": "gb", "hockenheim": "de",
    "hungaroring": "hu", "spa": "be", "monza": "it", "singapore": "sg",
    "suzuka": "jp", "abu dhabi": "ae", "texas": "us", "cota": "us",
    "brazil": "br", "interlagos": "br", "austria": "at", "sochi": "ru",
    "mexico": "mx", "baku": "az", "azerbaijan": "az", "zandvoort": "nl",
    "imola": "it", "portim": "pt", "jeddah": "sa", "miami": "us",
    "las vegas": "us", "losail": "qa", "qatar": "qa", "hanoi": "vn",
}


def _load_flag(track_id: int, track_name: str = "", height: int = 36) -> Optional[Image.Image]:
    code = TRACK_FLAGS.get(track_id)
    if not code and track_name:
        code = _TRACK_NAME_FLAGS.get(track_name.lower())
        if not code:
            for key, val in _TRACK_NAME_FLAGS.items():
                if key in track_name.lower():
                    code = val
                    break
    if not code:
        return None
    path = _FLAGS_DIR / f"{code}.png"
    if not path.exists():
        return None
    img = Image.open(path).convert("RGBA")
    w = int(img.width * height / img.height)
    return img.resize((w, height), Image.LANCZOS)


# ── helpers ──────────────────────────────────────────────────────────────────

def _draw_tyre(draw: ImageDraw.ImageDraw, cx: float, cy: float,
               r: int, tyre_col: tuple) -> None:
    """Draw a stylised F1 tyre: coloured rubber ring → dark rim → 5 spokes → hub cap."""
    cx, cy = int(cx), int(cy)
    r_rim   = max(2, int(r * 0.52))   # inner edge of rubber / outer edge of rim
    r_hub   = max(1, int(r * 0.22))   # centre hub cap

    # 1. Outer tyre (rubber, compound colour)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tyre_col)

    # 2. Rim (dark alloy)
    rim_col = (50, 52, 62)
    draw.ellipse([cx - r_rim, cy - r_rim, cx + r_rim, cy + r_rim], fill=rim_col)

    # 3. Five spokes — thin lines from hub edge to rim edge, slightly dimmed
    spoke_col = (90, 92, 105)
    for i in range(5):
        angle = math.radians(i * 72 - 90)          # start at top, evenly spaced
        x_out = cx + int(r_rim * math.cos(angle))
        y_out = cy + int(r_rim * math.sin(angle))
        x_in  = cx + int(r_hub * math.cos(angle))
        y_in  = cy + int(r_hub * math.sin(angle))
        draw.line([x_in, y_in, x_out, y_out], fill=spoke_col, width=1)

    # 4. Centre hub cap (light alloy)
    hub_col = (140, 142, 155)
    draw.ellipse([cx - r_hub, cy - r_hub, cx + r_hub, cy + r_hub], fill=hub_col)


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _load_bold_font(size: int) -> ImageFont.FreeTypeFont:
    """Load the heaviest available bold font — used for SC/RF header labels."""
    for path in [
        # macOS
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Impact.ttf",
        # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/impact.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return _load_font(size)


def _dim(c: tuple, f: float) -> tuple:
    return tuple(int(x * f) for x in c)


def _blend(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def _draw_checkered_flag(draw: ImageDraw.Draw,
                         x: int, y_top: int, y_bottom: int,
                         sq: int = 7, cols: int = 2) -> None:
    n_rows = (y_bottom - y_top) // sq
    for row in range(n_rows):
        for col in range(cols):
            black = (row + col) % 2 == 0
            fill = (0, 0, 0) if black else (240, 240, 240)
            x0 = x + col * sq
            y0 = y_top + row * sq
            draw.rectangle([x0, y0, x0 + sq - 1, y0 + sq - 1], fill=fill)


def _grid_to_x(grid_pos: int, n_cars: int) -> float:
    """Linear x position for a car on the starting grid.
    P1 → 80% of bar width; last car → left edge (0%)."""
    frac = (n_cars - grid_pos) / max(n_cars - 1, 1) * 0.80
    return LEFT_W + frac * LINE_AREA


def _dist_to_x(dist: float, x_max: float, linear_blend: float = 0.0) -> float:
    """Convert a distance value to an x pixel coordinate.

    linear_blend=0  → non-linear (gap-compressed) scale used during the race.
    linear_blend=1  → fully linear (equal-distance) scale used in the outro transition.
    Values in between smoothly interpolate between the two.
    """
    if x_max <= 0:
        return float(LEFT_W)
    rel = max(0.0, min(1.0, dist / x_max))
    gap = 1.0 - rel
    x_nonlinear = LEFT_W + (1.0 - gap ** X_POWER) * LINE_AREA
    if linear_blend <= 0.0:
        return x_nonlinear
    x_linear = LEFT_W + rel * LINE_AREA
    return x_nonlinear + linear_blend * (x_linear - x_nonlinear)


def _fmt_gap(ms: int) -> str:
    s = ms / 1000.0
    if s < 60:
        return f"+{s:.1f}s"
    return f"+{int(s // 60)}:{s % 60:04.1f}s"


def _lookup_sc(sc_timeline: List[Tuple[float, int]], t: float) -> int:
    """Return the most recent safety_car_status at or before time t."""
    status = 0
    for ts, st in sc_timeline:
        if ts <= t:
            status = st
        else:
            break
    return status


def _lookup_fl(ftlp_timeline: List[Tuple[float, int]], t: float) -> Optional[int]:
    """Return the car_index currently holding fastest lap at time t, or None."""
    holder = None
    for ts, idx in ftlp_timeline:
        if ts <= t:
            holder = idx
        else:
            break
    return holder


def _is_red_flag(rdfl_timeline: List[Tuple[float, Optional[float]]], t: float) -> bool:
    """Return True if time t falls within a red flag period."""
    for start, end in rdfl_timeline:
        if t >= start and (end is None or t < end):
            return True
    return False


# ── car icon ─────────────────────────────────────────────────────────────────

def _draw_car_icon(
    draw: ImageDraw.Draw,
    x_tip: float,
    yc: float,
    colour: tuple,
    size: int = 28,
) -> None:
    s = size
    x0 = x_tip - s
    bw, bh = s * 0.68, s * 0.27
    bx0, bx1 = x0 + s * 0.06, x0 + s * 0.06 + bw

    draw.rounded_rectangle([bx0, yc - bh/2, bx1, yc + bh/2],
                            radius=int(bh * 0.45), fill=colour)
    draw.polygon([(bx1, yc - bh*0.42), (bx1, yc + bh*0.42), (x_tip, yc)],
                 fill=colour)

    wing = _blend(colour, (255, 255, 255), 0.35)
    fw_x = bx1 - s * 0.05
    draw.rectangle([fw_x, yc - s*0.29, fw_x + 2, yc + s*0.29], fill=wing)
    rw_x = bx0 + s * 0.04
    draw.rectangle([rw_x - 2, yc - s*0.35, rw_x + 2, yc + s*0.35], fill=wing)

    wc = (18, 18, 18)
    ww, wh = s*0.11, s*0.21
    for wx_frac in (0.62, 0.24):
        wx = x0 + wx_frac * s
        for sign in (-1.0, 1.0):
            wy = yc + sign * (bh/2 + wh*0.25)
            draw.ellipse([wx - ww/2, wy - wh/2, wx + ww/2, wy + wh/2], fill=wc)

    halo = _blend(colour, (50, 50, 70), 0.55)
    draw.arc([bx0 + bw*0.18, yc - bh*0.5 + 2, bx0 + bw*0.72, yc + bh*0.5 - 2],
             start=185, end=355, fill=halo, width=2)


# ── frame renderer ────────────────────────────────────────────────────────────

def _render_frame(
    cars: List[Dict],
    y_pos: Dict[int, float],
    history: Dict[int, List[Tuple[float, float]]],
    car_meta: Dict[int, Dict],
    lap_boundary_dists: Dict[int, float],
    finish_distance: float,
    x_max: float,
    lap: int,
    total_laps: int,
    race_time: float,
    fonts: tuple,
    sc_status: int,   # 0=none 1=SC 2=VSC 3=SC ending
    n_cars: int,
    frame_idx: int,
    pit_markers: Dict[int, List[Tuple[float, float]]] = None,
    flag_img: Optional[Image.Image] = None,
    track_name: str = "",
    fl_holder: Optional[int] = None,
    blend_factor: float = 1.0,
    grid_positions: Optional[Dict[int, int]] = None,
    red_flag: bool = False,
    ghost_indices: Optional[set] = None,
    sc_bands: Optional[List[Tuple[float, float]]] = None,
    linear_blend: float = 0.0,
) -> Image.Image:
    font_hd, font_md, font_sm, font_xs, font_flag = fonts
    if ghost_indices is None:
        ghost_indices = set()
    if sc_bands is None:
        sc_bands = []

    # Red flag header blinks; safety car header is solid (no blink)
    rf_blink = red_flag and (frame_idx // 12) % 2 == 0

    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    content_h = H - HEADER_H - FOOTER_H
    row_h = content_h / max(n_cars, 1)
    icon_sz = max(16, min(ICON_SIZE, int(row_h * 0.70)))

    # Alternating row stripes
    for i in range(n_cars):
        if i % 2 == 1:
            ry = HEADER_H + i * row_h
            draw.rectangle([0, ry, W, ry + row_h], fill=STRIPE)

    # SC / VSC: persistent yellow bands on the distance axis.
    # Each band spans (start_dist, end_dist) and stays visible forever.
    if sc_bands and x_max > 0:
        sc_overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sc_draw = ImageDraw.Draw(sc_overlay)
        for band_start, band_end in sc_bands:
            bx0 = int(_dist_to_x(band_start, x_max, linear_blend))
            bx1 = int(_dist_to_x(band_end, x_max, linear_blend))
            bx0 = max(LEFT_W, min(bx0, LEFT_W + LINE_AREA))
            bx1 = max(LEFT_W, min(bx1, LEFT_W + LINE_AREA))
            if bx1 > bx0:
                sc_draw.rectangle(
                    [bx0, HEADER_H, bx1, H - FOOTER_H],
                    fill=(220, 180, 0, 35),
                )
        img = Image.alpha_composite(img.convert("RGBA"), sc_overlay).convert("RGB")
        draw = ImageDraw.Draw(img)

    # Lap boundary markers
    for lap_n, lap_dist in lap_boundary_dists.items():
        if lap_n < 1 or lap_dist <= 0:
            continue
        lx = int(_dist_to_x(lap_dist, x_max, linear_blend))
        if LEFT_W < lx < LEFT_W + LINE_AREA:
            draw.line([(lx, HEADER_H), (lx, H - FOOTER_H)], fill=LAP_LINE, width=1)
            draw.text((lx, H - FOOTER_H + 4), f"L{lap_n}",
                      fill=DIM, font=font_xs, anchor="mt")

    draw.line([(0, HEADER_H - 1), (W, HEADER_H - 1)], fill=GRID, width=2)
    draw.line([(0, H - FOOTER_H), (W, H - FOOTER_H)], fill=GRID, width=1)

    # Checkered finish flag — slides in from right like lap markers
    if x_max > 0 and finish_distance > 0 and finish_distance <= x_max:
        flag_x = int(_dist_to_x(finish_distance, x_max, linear_blend))
        if LEFT_W <= flag_x <= LEFT_W + LINE_AREA:
            _draw_checkered_flag(draw, flag_x, HEADER_H, H - FOOTER_H)

    # History polylines
    for idx, hist in history.items():
        if len(hist) < 2:
            continue
        colour = car_meta.get(idx, {}).get("colour", DEFAULT_COLOUR)
        if idx in ghost_indices:
            hc = _dim(colour, HIST_DIM * 0.5)  # extra dim for DNF cars
        else:
            hc = _dim(colour, HIST_DIM)
        step = max(1, len(hist) // 300)
        pts_raw = hist[::step]
        if pts_raw[-1] != hist[-1]:
            pts_raw = pts_raw + [hist[-1]]
        pts = [(int(_dist_to_x(d, x_max, linear_blend)), int(y)) for d, y in pts_raw]
        draw.line(pts, fill=hc, width=2)

    # Pit history markers — show tyre change (from → to) on the trail
    if pit_markers:
        for idx, marks in pit_markers.items():
            for m in marks:
                mx = int(_dist_to_x(m["dist"], x_max, linear_blend))
                my = int(m["y"])
                if not (LEFT_W <= mx <= LEFT_W + LINE_AREA):
                    continue
                ft = m["from_tyre"]
                tt = m["to_tyre"]
                ft_col = TYRE_COLOURS.get(ft)
                tt_col = TYRE_COLOURS.get(tt)

                if ft_col or tt_col:
                    # Pill background wide enough for two tyre icons + arrow
                    tr_sm  = 5     # small tyre radius
                    pad    = 3
                    arr_w  = 6     # pixels reserved for the drawn arrow
                    pill_w = pad + tr_sm * 2 + 2 + arr_w + 2 + tr_sm * 2 + pad
                    pill_h = tr_sm * 2 + pad * 2
                    px0 = mx - pill_w // 2
                    py0 = my - pill_h // 2
                    draw.rounded_rectangle([px0, py0, px0 + pill_w, py0 + pill_h],
                                           radius=pill_h // 2, fill=PIT_COL,
                                           outline=(200, 240, 255))
                    # from tyre icon
                    x_cur = px0 + pad + tr_sm
                    if ft_col:
                        _draw_tyre(draw, x_cur, my, tr_sm, ft_col)
                    else:
                        draw.text((x_cur, my), "?", fill=(180, 180, 180),
                                  font=font_xs, anchor="mm")
                    x_cur += tr_sm + 2
                    # drawn arrow: shaft + arrowhead
                    ax0, ax1 = x_cur, x_cur + arr_w
                    ac = (200, 220, 255)
                    draw.line([(ax0, my), (ax1 - 2, my)], fill=ac, width=1)
                    draw.polygon([(ax1 - 3, my - 2), (ax1, my), (ax1 - 3, my + 2)], fill=ac)
                    x_cur += arr_w + 2
                    # to tyre icon
                    if tt_col:
                        _draw_tyre(draw, x_cur + tr_sm, my, tr_sm, tt_col)
                    else:
                        draw.text((x_cur + tr_sm, my), "?", fill=(180, 180, 180),
                                  font=font_xs, anchor="mm")
                else:
                    # No tyre data — fallback to plain "P" circle
                    r = 6
                    draw.ellipse([mx - r, my - r, mx + r, my + r],
                                 fill=PIT_COL, outline=(200, 240, 255))
                    draw.text((mx, my), "P", fill=(10, 10, 22),
                              font=fonts[3], anchor="mm")

    # Car icons + labels
    for car in cars:
        idx = car["car_index"]
        yc = y_pos.get(idx)
        if yc is None:
            continue

        is_ghost = idx in ghost_indices
        meta    = car_meta.get(idx, {})
        colour  = meta.get("colour", DEFAULT_COLOUR)
        name    = meta.get("name", "???")
        team_id = meta.get("team_id", -1)

        dist_x = _dist_to_x(car["total_distance"], x_max, linear_blend)
        if blend_factor < 1.0 and grid_positions and not is_ghost:
            gpos = grid_positions.get(idx, car.get("car_position", n_cars))
            gx   = _grid_to_x(gpos, n_cars)
            cx   = gx + blend_factor * (dist_x - gx)
        else:
            cx = dist_x
        icon_tip = cx + icon_sz

        # Tyre compound icon — drawn for both active and ghost cars
        tyre_col = TYRE_COLOURS.get(car.get("tyre_compound"))
        if tyre_col:
            tr = max(6, icon_sz // 3)   # radius: bigger than old dot for detail
            tx = int(cx) - icon_sz - tr - 4
            if tx > LEFT_W:
                _draw_tyre(draw, tx, yc, tr, tyre_col)

        if is_ghost:
            # ── DNF ghost car rendering ──────────────────────────────────────
            dim_colour = _dim(colour, 0.35)
            _draw_car_icon(draw, icon_tip, yc, dim_colour, size=icon_sz)
            # DNF box immediately right of icon
            dnf_label = "DNF"
            dw = int(font_xs.getlength(dnf_label)) + 8
            dx0 = int(icon_tip) + 4
            draw.rectangle([dx0, int(yc) - 9, dx0 + dw, int(yc) + 9],
                            fill=(160, 0, 0), outline=(220, 50, 50))
            draw.text((dx0 + 4, yc), dnf_label,
                      fill=(255, 255, 255), font=font_xs, anchor="lm")
            # Driver name dimmed, right of the DNF box
            name_x = dx0 + dw + 5
            after_name_x = name_x
            if name_x < W - 20:
                draw.text((name_x, yc), name[:14],
                          fill=_dim(TEXT, 0.50), font=font_md, anchor="lm")
                after_name_x = name_x + int(font_md.getlength(name[:14]))
            # FL badge — shown even on DNF cars
            if fl_holder is not None and idx == fl_holder:
                fl_x = after_name_x + 6
                fl_w = int(font_xs.getlength("FL")) + 6
                draw.rectangle([fl_x, int(yc) - 7, fl_x + fl_w, int(yc) + 7],
                                fill=_dim(FL_COL, 0.75))
                draw.text((fl_x + 3, yc), "FL", fill=(255, 255, 255),
                          font=font_xs, anchor="lm")
            # Left column — "DNF" label + dimmed team name
            team_str = TEAM_NAMES.get(team_id, "???")
            draw.text((6, yc), "DNF", fill=_dim(PEN_COL, 0.7), font=font_xs, anchor="lm")
            draw.text((38, yc), team_str[:12], fill=_dim(TEXT, 0.45), font=font_sm, anchor="lm")
            continue

        # ── active car rendering ─────────────────────────────────────────────

        # ── pit status indicator (left of car) ────────────────────────────
        pit = car.get("pit_status", 0)
        if pit > 0:
            pit_label = "PIT" if pit == 1 else "BOX"
            pw = int(font_xs.getlength(pit_label)) + 6
            px0 = int(cx) - pw - 2
            if px0 > LEFT_W:
                draw.rectangle([px0, int(yc) - 8, px0 + pw, int(yc) + 8],
                                fill=PIT_COL, outline=PIT_COL)
                draw.text((px0 + 3, yc), pit_label,
                          fill=(10, 10, 22), font=font_xs, anchor="lm")

        # ── warning / penalty indicator (left of car, replaces pit if both) ─
        warnings = car.get("warnings", 0)
        penalties = car.get("penalties", 0)

        if penalties > 0:
            pen_label = f"{penalties}s"
            pw = int(font_xs.getlength(pen_label)) + 6
            px0 = int(cx) - pw - 2
            if px0 > LEFT_W:
                draw.rectangle([px0, int(yc) - 8, px0 + pw, int(yc) + 8],
                                fill=PEN_COL, outline=PEN_COL)
                draw.text((px0 + 3, yc), pen_label,
                          fill=(255, 255, 255), font=font_xs, anchor="lm")
        elif warnings > 0 and pit == 0:
            warn_label = "!"
            pw = int(font_xs.getlength(warn_label)) + 10
            px0 = int(cx) - pw - 2
            if px0 > LEFT_W:
                draw.rectangle([px0, int(yc) - 8, px0 + pw, int(yc) + 8],
                                fill=WARN_COL, outline=WARN_COL)
                draw.text((px0 + pw // 2, yc), warn_label,
                          fill=(10, 10, 22), font=font_md, anchor="mm")

        _draw_car_icon(draw, icon_tip, yc, colour, size=icon_sz)

        # Driver name
        text_x = icon_tip + 5
        if text_x < W - 20:
            draw.text((text_x, yc), name[:14], fill=TEXT, font=font_md, anchor="lm")

            # Gap to leader (non-P1 only)
            gap_end_x = text_x + int(font_md.getlength(name[:14]))
            if car.get("car_position", 1) > 1:
                delta_ms = car.get("delta_to_leader_ms", 0)
                if delta_ms > 0:
                    gap_str = _fmt_gap(delta_ms)
                    draw.text((gap_end_x + 6, yc), gap_str,
                              fill=DIM, font=font_xs, anchor="lm")
                    gap_end_x += 6 + int(font_xs.getlength(gap_str))

            # Fastest lap badge
            if fl_holder is not None and idx == fl_holder:
                fl_x = gap_end_x + 6
                fl_w = int(font_xs.getlength("FL")) + 6
                draw.rectangle([fl_x, int(yc) - 7, fl_x + fl_w, int(yc) + 7],
                                fill=FL_COL)
                draw.text((fl_x + 3, yc), "FL", fill=(255, 255, 255),
                          font=font_xs, anchor="lm")

        # Left column: rank + team name
        team_str = TEAM_NAMES.get(team_id, "???")
        draw.text((6, yc), f"P{car['car_position']}", fill=DIM, font=font_xs, anchor="lm")
        draw.text((38, yc), team_str[:12], fill=TEXT, font=font_sm, anchor="lm")

    # ── header ────────────────────────────────────────────────────────────────
    # Red flag: blink the header background between dark red and default.
    # Safety car: solid yellow header, no blinking.
    if rf_blink:
        draw.rectangle([0, 0, W, HEADER_H - 2], fill=(80, 0, 0))
    elif sc_status > 0 and not red_flag:
        draw.rectangle([0, 0, W, HEADER_H - 2], fill=(220, 180, 0))

    # Flag + track name in top-left of header
    if flag_img is not None:
        fy = (HEADER_H - flag_img.height) // 2
        img.paste(flag_img, (8, fy), flag_img)
        if track_name:
            draw.text((8 + flag_img.width + 6, HEADER_H // 2),
                      track_name, fill=TEXT, font=font_sm, anchor="lm")

    mins, secs = divmod(int(race_time), 60)
    draw.text((W // 2, HEADER_H // 2),
              f"LAP {lap} / {total_laps}    {mins}:{secs:02d}",
              fill=TEXT, font=font_hd, anchor="mm")

    if red_flag:
        # Red flag: blinks, bright red text
        rf_col = (255, 80, 80) if rf_blink else (200, 40, 40)
        draw.text((W - 14, HEADER_H // 2), "RED FLAG", fill=rf_col, font=font_flag, anchor="rm")
    elif sc_status > 0:
        # Safety car: solid yellow header, black label in top-right
        sc_labels = {1: "Safety Car", 2: "Virtual Safety Car", 3: "Safety Car Ending"}
        label = sc_labels.get(sc_status, "Safety Car")
        draw.text((W - 14, HEADER_H // 2), label, fill=(0, 0, 0), font=font_flag, anchor="rm")

    # ── footer ────────────────────────────────────────────────────────────────
    if linear_blend >= 1.0:
        footer_label = f"← {x_max / 1000:.1f} km (linear scale) →"
    elif linear_blend > 0.0:
        footer_label = f"← {x_max / 1000:.1f} km →"
    else:
        footer_label = f"← {x_max / 1000:.1f} km (non-linear scale) →"
    draw.text((LEFT_W + LINE_AREA // 2, H - FOOTER_H // 2 + 8),
              footer_label, fill=DIM, font=font_xs, anchor="mm")

    return img


# ── ffmpeg ───────────────────────────────────────────────────────────────────

def _open_ffmpeg(out_path: str, input_fps: float = FPS) -> subprocess.Popen:
    """Open an ffmpeg process that reads raw RGB frames from stdin.

    input_fps controls how ffmpeg interprets the incoming frame rate.
    Output is always FPS (30). Setting input_fps < FPS slows playback
    without frame-duplication judder (ffmpeg upsamples evenly to FPS).
    """
    return subprocess.Popen(
        ["ffmpeg", "-y",
         "-f", "rawvideo", "-vcodec", "rawvideo",
         "-s", f"{W}x{H}", "-pix_fmt", "rgb24",
         "-r", str(input_fps), "-i", "pipe:0",
         "-vcodec", "libx264", "-pix_fmt", "yuv420p",
         "-r", str(FPS),
         "-crf", "22", "-preset", "fast", "-movflags", "+faststart",
         out_path],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _concat_mp4(part1: str, part2: str, out_path: str) -> None:
    """Concatenate two mp4 files using ffmpeg concat demuxer."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(f"file '{part1}'\nfile '{part2}'\n")
        list_path = f.name
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", list_path, "-c", "copy", out_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
    )
    os.unlink(list_path)


# ── public API ────────────────────────────────────────────────────────────────

def build_mp4(
    snapshots: List[Any],
    out_path: str,
    total_laps: int = 0,
    sc_timeline: Optional[List[Tuple[float, int]]] = None,
    track_id: int = -1,
    track_name: str = "",
    final_positions: Optional[Dict[int, int]] = None,
    ftlp_timeline: Optional[List[Tuple[float, int]]] = None,
    grid_positions: Optional[Dict[int, int]] = None,
    rdfl_timeline: Optional[List[Tuple[float, Optional[float]]]] = None,
) -> None:
    """
    snapshots: list of dicts with at minimum:
        session_time, car_index, car_position, current_lap,
        total_distance, pit_status, name, team_id
    Optional per-car fields (added by recorder v2+):
        delta_to_leader_ms, warnings, penalties, result_status
    sc_timeline: sorted list of (session_time, safety_car_status) from
        Session packets (packet_id=1).  sc_status: 0=none 1=SC 2=VSC 3=ending
    rdfl_timeline: list of (rdfl_start, rdfl_end) pairs; rdfl_end is None if
        the race ended under red flag (no subsequent SCAR event).
    """
    if not snapshots:
        raise ValueError("No snapshots to animate")

    by_time: Dict[float, Dict[int, Dict]] = defaultdict(dict)
    for s in snapshots:
        entry = dict(s) if not isinstance(s, dict) else s
        by_time[entry["session_time"]][entry["car_index"]] = entry

    times = sorted(by_time.keys())
    step = max(1, len(times) // TARGET_FRAMES)
    times = times[::step]
    if times[-1] != sorted(by_time.keys())[-1]:
        times.append(sorted(by_time.keys())[-1])

    sc_tl:   List[Tuple[float, int]] = sorted(sc_timeline)   if sc_timeline   else []
    ftlp_tl: List[Tuple[float, int]] = sorted(ftlp_timeline) if ftlp_timeline else []
    rdfl_tl: List[Tuple[float, Optional[float]]] = rdfl_timeline if rdfl_timeline else []
    flag_img = _load_flag(track_id, track_name)

    fonts = (_load_font(24), _load_font(14), _load_font(13), _load_font(11), _load_bold_font(28))

    n_cars = 0
    for t0 in times:
        bucket = list(by_time[t0].values())
        if bucket:
            n_cars = len(bucket)
            break

    content_h = H - HEADER_H - FOOTER_H
    row_h = content_h / max(n_cars, 1)

    def _target_y(rank: int) -> float:
        return HEADER_H + (rank + 0.5) * row_h

    # ── Pre-scan: lap boundaries + finish_distance ────────────────────────────
    # finish_distance = distance of the first car to achieve result_status==3
    # (the actual finish line position), NOT the max total_distance which
    # includes post-race driving by the winner.
    lap_boundary_dists: Dict[int, float] = {}
    finish_distance: float = 0.0
    finish_distance_from_status: float = 0.0  # set when first car crosses line
    prev_lap = None
    for t in times:
        bucket = list(by_time[t].values())
        if not bucket:
            continue
        for car in bucket:
            # Candidate finish_distance: first car snapshot with result_status=3
            if car.get("result_status") == 3:
                d = float(car.get("total_distance", 0))
                if finish_distance_from_status == 0.0 or d < finish_distance_from_status:
                    finish_distance_from_status = d
        leader = max(bucket, key=lambda c: c["total_distance"])
        lap_n = int(leader.get("current_lap", 1))
        if prev_lap is not None and lap_n != prev_lap and lap_n not in lap_boundary_dists:
            lap_boundary_dists[lap_n] = leader["total_distance"]
        prev_lap = lap_n

    # Use result_status-derived finish line if available, otherwise fall back
    # to the maximum observed distance (covers older recordings without status).
    if finish_distance_from_status > 0.0:
        finish_distance = finish_distance_from_status
    else:
        for t in times:
            for car in by_time[t].values():
                d = float(car.get("total_distance", 0))
                if d > finish_distance:
                    finish_distance = d

    y_pos:        Dict[int, float] = {}
    smooth_dist:  Dict[int, float] = {}
    x_max_cur:    Optional[float] = None
    history:      Dict[int, List[Tuple[float, float]]] = defaultdict(list)
    # Each pit marker: {"dist": float, "y": float, "from_tyre": int|None, "to_tyre": int|None}
    pit_markers:  Dict[int, List[Dict]] = defaultdict(list)
    prev_pit:     Dict[int, int] = {}
    last_tyre:    Dict[int, Optional[int]] = {}   # most recent known tyre per car
    car_meta:     Dict[int, Dict] = {}
    max_laps = total_laps
    last_cars: List[Dict] = []   # active (non-DNF) cars only, for outro

    # DNF tracking
    ghost_cars:    Dict[int, float] = {}   # idx -> frozen smooth_dist
    last_seen:     Dict[int, Dict]  = {}   # idx -> last snapshot
    finished_cars: set              = set()  # indices that achieved result_status=3


    # SC / VSC persistent band tracking
    sc_bands:          List[Tuple[float, float]] = []  # (start_dist, end_dist)
    sc_band_start:     Optional[float] = None          # leader smooth_dist when SC started
    prev_sc_status:    int             = 0

    # Race and outro+linear are encoded separately then concatenated.
    # The race segment uses input_fps = FPS*RACE_SPEED so ffmpeg upsamples
    # evenly to FPS — no frame-duplication judder.
    race_tmp  = out_path + ".race.mp4"
    outro_tmp = out_path + ".outro.mp4"

    proc = _open_ffmpeg(race_tmp, input_fps=FPS * RACE_SPEED)
    frame_count = 0

    try:
        for t in times:
            cars_raw = list(by_time[t].values())
            if not cars_raw:
                continue

            current_indices = {c["car_index"] for c in cars_raw}

            # Detect newly disappeared cars that haven't finished → DNF
            for idx in set(last_seen.keys()) - current_indices:
                if idx not in finished_cars and idx not in ghost_cars:
                    ghost_cars[idx] = smooth_dist.get(idx, 0.0)

            # Update finished set and last_seen for active cars
            for car in cars_raw:
                idx = car["car_index"]
                last_seen[idx] = car
                if car.get("result_status") == 3:
                    finished_cars.add(idx)


            cars = sorted(cars_raw, key=lambda c: c["total_distance"], reverse=True)
            last_cars = cars  # remember for outro (active cars only)

            for car in cars:
                idx = car["car_index"]
                if idx not in car_meta:
                    car_meta[idx] = {
                        "colour":  TEAM_COLOURS.get(car.get("team_id", -1), DEFAULT_COLOUR),
                        "name":    car.get("name", "???"),
                        "team_id": car.get("team_id", -1),
                    }
            # Ensure ghost car metadata is also stored
            for idx in ghost_cars:
                if idx not in car_meta and idx in last_seen:
                    c = last_seen[idx]
                    car_meta[idx] = {
                        "colour":  TEAM_COLOURS.get(c.get("team_id", -1), DEFAULT_COLOUR),
                        "name":    c.get("name", "???"),
                        "team_id": c.get("team_id", -1),
                    }

            for rank, car in enumerate(cars):
                idx = car["car_index"]
                target = _target_y(rank)
                if idx not in y_pos:
                    y_pos[idx] = target
                else:
                    y_pos[idx] += ALPHA_Y * (target - y_pos[idx])

            # Animate ghost (DNF) cars toward the bottom row(s), in DNF order
            for i, idx in enumerate(sorted(ghost_cars.keys(),
                                           key=lambda x: ghost_cars[x])):
                # First DNF → last row, second DNF → second-to-last, etc.
                ghost_target = _target_y(n_cars - 1 - i)
                if idx not in y_pos:
                    y_pos[idx] = ghost_target
                else:
                    y_pos[idx] += ALPHA_Y * (ghost_target - y_pos[idx])

            for car in cars:
                idx = car["car_index"]
                raw = car["total_distance"]
                if idx not in smooth_dist:
                    smooth_dist[idx] = raw
                else:
                    smooth_dist[idx] += ALPHA_DIST * (raw - smooth_dist[idx])

            # Use smooth_dist of the leader for x_max so the scale tracks
            # the displayed position without jumping between raw frames.
            leader_idx = cars[0]["car_index"]
            leader_smooth = smooth_dist.get(leader_idx, cars[0]["total_distance"])
            if x_max_cur is None:
                x_max_cur = leader_smooth
            else:
                target_xmax = leader_smooth * 1.015
                if target_xmax > x_max_cur:
                    x_max_cur += ALPHA_X * (target_xmax - x_max_cur)
                x_max_cur = max(x_max_cur, leader_smooth)
            if finish_distance > 0:
                x_max_cur = min(x_max_cur, finish_distance)

            # Track SC / VSC periods as persistent distance bands
            cur_sc = _lookup_sc(sc_tl, t)
            if prev_sc_status == 0 and cur_sc > 0:
                sc_band_start = leader_smooth      # SC just started
            elif prev_sc_status > 0 and cur_sc == 0 and sc_band_start is not None:
                sc_bands.append((sc_band_start, leader_smooth))  # SC just ended
                sc_band_start = None
            prev_sc_status = cur_sc

            cur_blend = min(1.0, t / GRID_BLEND_DURATION) if grid_positions else 1.0
            for car in cars:
                idx = car["car_index"]
                cur_pit  = car.get("pit_status", 0)
                cur_tyre = car.get("tyre_compound")   # may be None for old recordings
                # Only record history after the grid-blend completes so the
                # trail always originates at the car's displayed position.
                if cur_blend >= 1.0:
                    history[idx].append((smooth_dist[idx], y_pos[idx]))
                    # Pit entry (0 → 1): create a new marker with from_tyre
                    if prev_pit.get(idx, 0) == 0 and cur_pit == 1:
                        pit_markers[idx].append({
                            "dist":      smooth_dist[idx],
                            "y":         y_pos[idx],
                            "from_tyre": last_tyre.get(idx),
                            "to_tyre":   None,
                        })
                    # Pit exit (>0 → 0): fill in to_tyre on the most recent marker
                    elif prev_pit.get(idx, 0) > 0 and cur_pit == 0:
                        if pit_markers[idx] and pit_markers[idx][-1]["to_tyre"] is None:
                            pit_markers[idx][-1]["to_tyre"] = cur_tyre
                prev_pit[idx] = cur_pit
                if cur_tyre is not None:
                    last_tyre[idx] = cur_tyre

            lap = max((c["current_lap"] for c in cars), default=1)
            max_laps = max(max_laps, lap)

            # Build display list: active cars + ghost (DNF) cars
            cars_display = [dict(c, total_distance=smooth_dist.get(c["car_index"],
                                                                    c["total_distance"]))
                            for c in cars]
            for idx, frozen_dist in ghost_cars.items():
                ls = last_seen.get(idx, {})
                cars_display.append({
                    "car_index":         idx,
                    "total_distance":    frozen_dist,
                    "car_position":      99,
                    "pit_status":        0,
                    "warnings":          0,
                    "penalties":         0,
                    "delta_to_leader_ms": 0,
                    "result_status":     "DNF",
                    "current_lap":       ls.get("current_lap", 0),
                    "tyre_compound":     ls.get("tyre_compound"),
                })

            img = _render_frame(
                cars_display, y_pos, history, car_meta,
                lap_boundary_dists, finish_distance, x_max_cur,
                lap, max_laps, t,
                fonts,
                sc_status=_lookup_sc(sc_tl, t),
                n_cars=n_cars,
                frame_idx=frame_count,
                pit_markers=pit_markers,
                flag_img=flag_img,
                track_name=track_name,
                fl_holder=_lookup_fl(ftlp_tl, t),
                blend_factor=min(1.0, t / GRID_BLEND_DURATION) if grid_positions else 1.0,
                grid_positions=grid_positions,
                red_flag=_is_red_flag(rdfl_tl, t),
                ghost_indices=set(ghost_cars.keys()),
                sc_bands=sc_bands + ([(sc_band_start, leader_smooth)] if sc_band_start is not None else []),
            )
            proc.stdin.write(img.tobytes())
            frame_count += 1

        # Close race segment ffmpeg process
        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg (race segment) exited with code {proc.returncode}.")

        # Close any SC period still open at race end
        if sc_band_start is not None:
            sc_bands.append((sc_band_start, leader_smooth))
            sc_band_start = None

        # Open a new ffmpeg process for outro + linear transition (normal speed)
        proc = _open_ffmpeg(outro_tmp, input_fps=FPS)

        # ── Outro: uniform speed toward finish_distance ───────────────────────
        # DNF ghost cars are excluded from movement and speed calculation.
        if x_max_cur is not None and last_cars:
            # Only include active (non-DNF) cars in outro movement.
            # Seed from the actual (non-smoothed) total_distance so that
            # smooth_dist lag doesn't cause trails to fall short.
            outro_smooth = {
                c["car_index"]: min(c["total_distance"], finish_distance)
                for c in last_cars
            }
            outro_ypos   = dict(y_pos)
            outro_history: Dict[int, List[Tuple[float, float]]] = {
                idx: list(pts) for idx, pts in history.items()
            }

            max_gap = max(
                (finish_distance - d for d in outro_smooth.values()),
                default=0.0,
            )
            # Minimum speed: cover at least 0.5% of finish_distance in OUTRO_S
            # seconds.  Without this floor, a car that ends the data just a few
            # metres short of the finish line (e.g. the last classified car whose
            # final UDP snapshot arrived slightly before the line) would crawl
            # across in slow-motion while every other car has already crossed.
            min_gap = finish_distance * 0.005
            speed = max(max_gap, min_gap) / (FPS * OUTRO_S)
            crossed: set = set()   # active car indices that have reached finish_distance

            for _fi in range(int(FPS * OUTRO_S)):
                for idx in list(outro_smooth):
                    prev_d = outro_smooth[idx]
                    outro_smooth[idx] = min(outro_smooth[idx] + speed, finish_distance)
                    # When a car crosses the finish line, pin a final history
                    # point exactly at finish_distance so all trails reach the end.
                    if outro_smooth[idx] >= finish_distance and idx not in crossed:
                        crossed.add(idx)
                        outro_history[idx].append((finish_distance, outro_ypos[idx]))

                outro_active = sorted(
                    [dict(c, total_distance=outro_smooth.get(c["car_index"],
                                                              c["total_distance"]))
                     for c in last_cars],
                    key=lambda c: c["total_distance"], reverse=True,
                )

                # Update P# label to final position once a car has crossed
                if final_positions:
                    outro_active = [
                        dict(c, car_position=final_positions[c["car_index"]])
                        if c["car_index"] in crossed and c["car_index"] in final_positions
                        else c
                        for c in outro_active
                    ]

                # Y target: final classified position for crossed cars,
                # distance-based rank for cars still racing
                dist_rank = {car["car_index"]: rank
                             for rank, car in enumerate(outro_active)}
                for car in outro_active:
                    idx = car["car_index"]
                    if idx in crossed and final_positions and idx in final_positions:
                        target = _target_y(final_positions[idx] - 1)
                    else:
                        target = _target_y(dist_rank[idx])
                    outro_ypos[idx] += ALPHA_Y * (target - outro_ypos[idx])

                # Extend history only for active cars still moving
                for car in outro_active:
                    idx = car["car_index"]
                    if idx not in crossed:
                        outro_history[idx].append((outro_smooth[idx], outro_ypos[idx]))

                # Keep ghost (DNF) cars animating to the bottom row in the outro
                for i, idx in enumerate(sorted(ghost_cars.keys(),
                                               key=lambda x: ghost_cars[x])):
                    ghost_target = _target_y(n_cars - 1 - i)
                    outro_ypos[idx] += ALPHA_Y * (ghost_target - outro_ypos[idx])

                # Add ghost (DNF) cars to outro display — frozen x positions
                outro_cars = list(outro_active)
                for idx, frozen_dist in ghost_cars.items():
                    ls = last_seen.get(idx, {})
                    outro_cars.append({
                        "car_index":         idx,
                        "total_distance":    frozen_dist,
                        "car_position":      99,
                        "pit_status":        0,
                        "warnings":          0,
                        "penalties":         0,
                        "delta_to_leader_ms": 0,
                        "result_status":     "DNF",
                        "current_lap":       ls.get("current_lap", 0),
                    })

                img = _render_frame(
                    outro_cars, outro_ypos, outro_history, car_meta,
                    lap_boundary_dists, finish_distance, finish_distance,
                    max_laps, max_laps, times[-1],
                    fonts, sc_status=0, n_cars=n_cars, frame_idx=frame_count,
                    pit_markers=pit_markers, flag_img=flag_img, track_name=track_name,
                    fl_holder=_lookup_fl(ftlp_tl, times[-1]),
                    grid_positions=grid_positions,
                    red_flag=False,
                    ghost_indices=set(ghost_cars.keys()),
                    sc_bands=sc_bands,
                )
                proc.stdin.write(img.tobytes())
                frame_count += 1

            # ── Linear transition: non-linear → linear scale ──────────────────
            # All cars are now at finish_distance (or their DNF distance).
            # Smoothly stretch the x-axis from gap-compressed to equal-distance
            # so viewers can appreciate the true lap-count gaps.
            linear_cars = list(outro_cars)
            linear_ypos = dict(outro_ypos)
            total_linear_frames = FPS * LINEAR_TRANSITION_S
            # Ease in-out blend: use smoothstep for a more cinematic feel
            for lfi in range(total_linear_frames):
                t_norm = lfi / max(total_linear_frames - 1, 1)   # 0 → 1
                blend = t_norm * t_norm * (3 - 2 * t_norm)       # smoothstep
                img = _render_frame(
                    linear_cars, linear_ypos, outro_history, car_meta,
                    lap_boundary_dists, finish_distance, finish_distance,
                    max_laps, max_laps, times[-1],
                    fonts, sc_status=0, n_cars=n_cars, frame_idx=frame_count,
                    pit_markers=pit_markers, flag_img=flag_img, track_name=track_name,
                    fl_holder=_lookup_fl(ftlp_tl, times[-1]),
                    grid_positions=grid_positions,
                    red_flag=False,
                    ghost_indices=set(ghost_cars.keys()),
                    sc_bands=sc_bands,
                    linear_blend=blend,
                )
                proc.stdin.write(img.tobytes())
                frame_count += 1

    finally:
        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg (outro segment) exited with code {proc.returncode}. "
                "Is ffmpeg installed and on PATH?"
            )

    # Concatenate race + outro into final output, clean up temp files
    _concat_mp4(race_tmp, outro_tmp, out_path)
    for tmp in (race_tmp, outro_tmp):
        try:
            os.unlink(tmp)
        except OSError:
            pass

    size_kb = os.path.getsize(out_path) // 1024
    print(f"[visualizer] mp4 saved: {out_path} ({frame_count} frames, {size_kb} KB)")
