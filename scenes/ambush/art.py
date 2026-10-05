"""Sprite art for the Ambush scene: text sprite sheets, and the procedural sword.

A sheet file holds frames, each a header line and 21 rows:

    @kind name [key=value ...]     values are ints or x,y pairs (e.g. fist=18,12 sword=45)
    <21 rows>

  hero   24 columns, the overlay pair as one picture: 'k' is the hires black overlay (any
         column); 'r' light red (%01, $D025), 'y' yellow (%11, $D026) and 'b' the sprite's
         own color (%10) are multicolor fills. A fill is two hires pixels wide, so within each
         column pair (0-1, 2-3, ...) the non-black characters must agree. '.' is transparent.
  mc     12 columns, one character per double-wide pixel: 'r' %01, 'y' %11, 'k'/'w' the
         sprite's own color (%10), '.' transparent.

Lines starting with '#' are comments; blank lines are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

W, H = 24, 21
MC_CODE = {".": 0, "r": 1, "k": 2, "w": 2, "b": 2, "y": 3}


@dataclass
class Frame:
    kind: str
    name: str
    rows: list[str]
    attrs: dict


def _attr(v: str):
    try:
        nums = [int(x) for x in v.split(",")]
    except ValueError:
        return v
    return tuple(nums) if len(nums) > 1 else nums[0]


def parse(path: str | Path) -> dict[str, Frame]:
    frames: dict[str, Frame] = {}
    cur: Frame | None = None
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            kind, name, *rest = line[1:].split()
            if name in frames:
                raise ValueError(f"{path}:{n}: duplicate frame {name}")
            attrs = {k: _attr(v) for k, v in (kv.split("=", 1) for kv in rest)}
            cur = frames[name] = Frame(kind, name, [], attrs)
        elif cur is None or len(cur.rows) == H:
            raise ValueError(f"{path}:{n}: row outside a frame")
        else:
            cur.rows.append(line)
    for f in frames.values():
        width = 12 if f.kind == "mc" else 24
        bad = [i for i, r in enumerate(f.rows) if len(r) != width]
        if len(f.rows) != H or bad:
            raise ValueError(f"{path}: frame {f.name} needs {H} rows of {width} (has {len(f.rows)}, bad {bad})")
    return frames


def hero_layers(f: Frame) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Composite 'hero' art -> (overlay bits (21, 24), multicolor codes (21, 12), pair errors)."""
    over = np.array([[c == "k" for c in r] for r in f.rows], np.uint8)
    mc = np.zeros((H, 12), np.uint8)
    errors = []
    for y, r in enumerate(f.rows):
        for i in range(12):
            a, b = r[2 * i], r[2 * i + 1]
            fills = {c for c in (a, b) if c != "k"}
            if len(fills) > 1:
                errors.append(f"{f.name} row {y} cols {2 * i}-{2 * i + 1}: '{a}{b}'")
            mc[y, i] = MC_CODE[fills.pop() if fills else "."]
    return over, mc, errors


def mc_codes(f: Frame) -> np.ndarray:
    return np.array([[MC_CODE[c] for c in r] for r in f.rows], np.uint8)


def mc_to_hires(codes: np.ndarray, colors: tuple[int, int, int]) -> np.ndarray:
    """(21, 12) codes -> (21, 24) color indices, -1 transparent. colors = (%01, %10, %11)."""
    return np.array([-1, *colors])[codes].repeat(2, axis=1)


def compose(*layers: np.ndarray) -> np.ndarray:
    """Stack (21, 24) layers, first on top; -1 is transparent."""
    out = np.full(layers[0].shape, -1)
    for layer in reversed(layers):
        out = np.where(layer >= 0, layer, out)
    return out


# ------------------------------------------------------------------ the sword (procedural)
# Directions with clean pixel steps: 0, 1:2, 1:1, 2:1 slopes and their mirrors (degrees, 0 =
# pointing right, 90 = up). The blade is two pixels thick with a one-pixel tip; the grip sits
# on the anchor, which the timeline puts under the hero's fist.
STEPS = {0: (1, 0), 27: (2, 1), 45: (1, 1), 63: (1, 2), 90: (0, 1)}


def direction(angle: int) -> tuple[int, int]:
    """Step (dx, dy) in screen coordinates (y down) for one of the 16 clean angles."""
    a = angle % 360
    for base, (sx, sy) in STEPS.items():
        for quad in range(4):
            if (base + 90 * quad) % 360 == a:
                x, y = sx, -sy                       # rotate (sx, -sy) by quad * 90 degrees
                for _ in range(quad):
                    x, y = y, -x
                return x, y
    raise ValueError(f"angle {angle} has no clean pixel step")


def line_points(x0: int, y0: int, dx: int, dy: int, n: int) -> list[tuple[int, int]]:
    """n pixels from (x0, y0) along a clean step: regular runs along the major axis."""
    major = max(abs(dx), abs(dy))
    pts = []
    for k in range(n):
        if abs(dx) >= abs(dy):
            x = x0 + k * (1 if dx > 0 else -1)
            y = y0 + (1 if dy > 0 else -1) * (k * abs(dy) // major)
        else:
            y = y0 + k * (1 if dy > 0 else -1)
            x = x0 + (1 if dx > 0 else -1) * (k * abs(dx) // major)
        pts.append((x, y))
    return pts


def sword_pixels(angle: int, length: float = 14.0, grip: bool = True) -> tuple[set, tuple[int, int]]:
    """Pixels of a sword pointing at `angle` with its grip at (0, 0): (pixel set, guard pixel)."""
    dx, dy = direction(angle)
    major = max(abs(dx), abs(dy))
    ux, uy = dx / major, dy / major
    scale = float(np.hypot(ux, uy))                  # pixels per step along the blade
    px = set(line_points(0, 0, -dx, -dy, 2)) if grip else {(0, 0)}   # grip and pommel
    guard = (round(ux * 2), round(uy * 2))
    perp = (-dy, dx) if abs(dx) >= abs(dy) else (dy, -dx)
    pm = max(abs(perp[0]), abs(perp[1]))
    for s in (-1, 1):                               # a three-pixel guard across the blade
        px.add((guard[0] + round(s * perp[0] / pm), guard[1] + round(s * perp[1] / pm)))
    px.add(guard)
    n = int(round(length / scale))
    edge = line_points(guard[0] + round(ux), guard[1] + round(uy), dx, dy, n)
    px |= set(edge)
    ox, oy = (0, 1) if abs(dx) >= abs(dy) else (1, 0)
    if abs(dx) == abs(dy):
        ox, oy = (1, 0) if dx * dy > 0 else (0, 1)
    px |= {(x + ox, y + oy) for x, y in edge[1:-2]}  # thickness, tapering to the tip
    return px, guard


def fit(pixels: set, margin: int = 0) -> tuple[np.ndarray, tuple[int, int]]:
    """Place a pixel set (around an anchor at (0, 0)) in a 24x21 canvas, centred.
    Returns (bits, anchor position in the canvas)."""
    xs = [x for x, _ in pixels]
    ys = [y for _, y in pixels]
    w, h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
    if w > W - 2 * margin or h > H - 2 * margin:
        raise ValueError(f"shape {w}x{h} does not fit a sprite")
    ox, oy = (W - w) // 2 - min(xs), (H - h) // 2 - min(ys)
    bits = np.zeros((H, W), np.uint8)
    for x, y in pixels:
        bits[y + oy, x + ox] = 1
    return bits, (ox, oy)


def sword_bits(angle: int, length: float = 14.0) -> tuple[np.ndarray, tuple[int, int]]:
    px, _ = sword_pixels(angle, length)
    return fit(px)


def smear_bits(trail: int, lead: int, length: float = 14.0) -> tuple[np.ndarray, tuple[int, int]]:
    """A swing: the blade at `lead` plus a crescent swept from `trail` (degrees, anticlockwise
    positive), thick behind the blade and thinning to a hairline at the trailing end."""
    px, _ = sword_pixels(lead, length, grip=False)    # the fist hides the grip
    tip = max(np.hypot(x, y) for x, y in px)
    span = (lead - trail) % 360 if lead > trail else -((trail - lead) % 360)
    for y in range(-24, 25):
        for x in range(-24, 25):
            r = float(np.hypot(x, y))
            if r < 3 or r > tip + 0.5:
                continue
            a = float(np.degrees(np.arctan2(-y, x)))
            t = ((a - trail) % 360) / abs(span) if span > 0 else ((trail - a) % 360) / abs(span)
            if not 0 <= t <= 1:
                continue
            width = 0.8 + 5.5 * t ** 1.6                 # crescent: hairline -> thick
            if r >= tip - width:
                px.add((x, y))
    return fit(px)


BURST = ["#...#...#",
         ".#..#..#.",
         "..#...#..",
         "...###...",
         "###.#.###",
         "...###...",
         "..#...#..",
         ".#..#..#.",
         "#...#...#"]


def impact_bits(lead: int, at: float = 11.0, length: float = 14.0):
    """The blade stopped at `lead` with an impact flash on it, `at` pixels out from the fist."""
    px, _ = sword_pixels(lead, length, grip=False)
    dx, dy = direction(lead)
    norm = float(np.hypot(dx, dy))
    cx, cy = round(dx / norm * at), round(dy / norm * at)
    for j, row in enumerate(BURST):
        for i, ch in enumerate(row):
            p = (cx + i - 4, cy + j - 4)
            if ch == "#":
                px.add(p)
            elif max(abs(i - 4), abs(j - 4)) <= 2:
                px.discard(p)                  # dark gaps between the rays near the centre
    return fit(px)
