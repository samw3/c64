"""Ambush: the forest clearing behind the battle. Multicolor bitmap, one background color.

Painted procedurally at C64 resolution (160 x 200 double-wide pixels), back to front:
  haze     luminous mist on one cool ramp (blue, dark grey, light blue, light grey, light
           green, white), brightest under a gap in the canopy, darker toward the frame; the
           light grey turns cyan near the canopy (equal luma: the two blend on a CRT)
  trees    faint and far trunks one ramp step darker than the haze, two nearer ones in dark
           grey with a light grey rim toward the light, all lost in ground mist at the root
  beams    sunlight slanting down-left from the gap, a step brighter than the haze, fading
           out before it reaches the fight and landing as pools of light on the moss
  canopy   backlit leaves across the top (black, green, light green tips), heavy in the
           corners, the sun showing through the gap
  floor    moss (dark grey, green, light green in the pools), a worn path (orange, brown)
           leading toward the light, mounds of undergrowth in the haze at the horizon
  frame    two massive trunks: ridged bark (black, brown, dark grey) on a cylinder lit from
           the clearing (grey, light grey rim), moss, roots; ferns and grass at the bottom;
           mushrooms and stones
Everything behind the fight (rows ~85-175) stays mid-to-light and calm, so black sprites
read against it; the darkest values frame the edges and the top.

build() returns the encoded bitmap (black background, three colors per 4x8 cell), the
painting, what the VIC-II shows, the clash report and a list of sparkle cells (motes in
the light shafts, animated through color RAM). `python3 forest.py <dir>` writes previews.
"""

from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from harness.gfx import COLODORE, pack_mc_bitmap  # noqa: E402

W, H = 160, 200
BG = 0                              # $D021: black, free in every cell (the dark frame needs it)
GROUND_Y = 172                      # where the characters' feet are
PX = 2 * 0.936                      # width of a multicolor pixel in line heights (PAL)

Y601 = {c: 0.299 * r + 0.587 * g + 0.114 * b for c, (r, g, b) in enumerate(COLODORE)}

MIST = [6, 11, 14, 15, 13, 1]       # ramp positions 0..5
CANOPY = [0, 5, 13]
BARK = [0, 9, 11, 12, 15]          # shadow .. rim light
FLOOR = [0, 11, 5, 13]

yy, xx = np.mgrid[0:H, 0:W].astype(float)
BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16 + 1 / 32


def bayer(x, y):
    return BAYER[np.asarray(y, int) % 4, np.asarray(x, int) % 4]


def noise1d(n, scale, octaves=4, seed=0):
    """Fractal value noise over 0..n-1, roughly -1..1."""
    r = np.random.default_rng(seed)
    out, amp, total = np.zeros(n), 1.0, 0.0
    for _ in range(octaves):
        pts = r.uniform(-1, 1, int(n / scale) + 3)
        out += amp * np.interp(np.arange(n) / scale, np.arange(len(pts)), pts)
        total += amp
        amp *= 0.5
        scale /= 2
    return out / total


def noise2d(sx, sy, octaves=4, seed=0):
    """Fractal value noise over the canvas, cell size sx x sy (pixels x lines), roughly -1..1."""
    r = np.random.default_rng(seed)
    out, amp, total = np.zeros((H, W)), 1.0, 0.0
    for o in range(octaves):
        gx, gy = xx / (sx / 2 ** o), yy / (sy / 2 ** o)
        grid = r.uniform(-1, 1, (int(gy.max()) + 3, int(gx.max()) + 3))
        ix, iy = gx.astype(int), gy.astype(int)
        fx, fy = gx - ix, gy - iy
        fx, fy = fx * fx * (3 - 2 * fx), fy * fy * (3 - 2 * fy)
        n = (grid[iy, ix] * (1 - fx) + grid[iy, ix + 1] * fx) * (1 - fy) + \
            (grid[iy + 1, ix] * (1 - fx) + grid[iy + 1, ix + 1] * fx) * fy
        out += amp * n
        total += amp
        amp *= 0.5
    return out / total


def ramp_dither(v, ramp, threshold=None):
    """Ordered dither of a continuous ramp position v into ramp colors."""
    v = np.clip(v, 0, len(ramp) - 1 - 1e-6)
    i = np.floor(v).astype(int)
    up = (v - i) > (bayer(xx, yy) if threshold is None else threshold)
    return np.array(ramp)[np.minimum(i + up, len(ramp) - 1)]


# ------------------------------------------------------------------ painting

def blob_field(cx, cy, rx, ry):
    """1 at the centre of an ellipse, 0 on its edge, negative outside."""
    return 1 - np.hypot((xx - cx) / rx, (yy - cy) / ry)


def bezier(points, n=200):
    """Points along a Bezier curve with control points `points` (any degree)."""
    p = np.array(points, float)
    t = np.linspace(0, 1, n)
    out = []
    for tt in t:
        q = p.copy()
        while len(q) > 1:
            q = q[:-1] * (1 - tt) + q[1:] * tt
        out.append(q[0])
    out = np.array(out)
    return out[:, 0], out[:, 1], t


def mist_value():
    """Ramp position of the haze (MIST): brightest under the canopy gap, darker toward the frame."""
    v = 2.55 + 0.35 * np.exp(-((xx - 92) * PX / 150) ** 2)                      # open middle a bit lighter
    v += 0.75 * np.exp(-((xx - GAP_X) * PX / 60) ** 2 - ((yy - 18) / 34) ** 2)  # glow under the gap
    v -= 0.55 * np.clip((np.abs(xx - 84) - 34) / 46, 0, 1) ** 1.2               # toward the frame
    v -= 0.35 * np.clip((40 - yy) / 30, 0, 1)                                   # under the canopy
    v += 0.25 * np.clip((yy - 118) / 26, 0, 1)                                  # ground haze
    return v


def tree(v, x_base, width, lean, dark, lit, seed, top=-4, base=148, branches=(), soft=0.0):
    """A trunk in the haze, `dark` ramp steps below the mist, `lit` brighter on its glow side.
    Wider at the root flare; branches are (y, direction, length) limbs rising outward."""
    wob = 0.7 * noise1d(H + 10, 18, 2, seed=seed)
    yi = np.clip(yy.astype(int), 0, H - 1)
    xc = x_base + lean * (base - yy) + wob[yi]
    w = width * (1 + 0.10 * (yy - base) / 120) + 0.35 * width * np.clip((yy - base + 8) / 8, 0, 1) ** 2
    inside = (np.abs(xx - xc) < w / 2) & (yy >= top) & (yy <= base + 1)
    side = (xx - xc) / np.maximum(w / 2, 0.5)
    toward = 1 if GAP_X > x_base else -1
    shade = -dark + lit * np.clip(side * toward, 0, 1) ** 1.5 - 0.3 * dark * np.clip(-side * toward, 0, 1) ** 2
    fade = soft * np.clip((yy - (base - 30)) / 30, 0, 1)                       # roots lost in the haze
    out = np.where(inside, v + shade * (1 - fade), v)
    for by, dirn, ln in branches:
        for k in np.arange(0, ln, 0.5):
            y = by - k * (0.9 - 0.012 * k)
            x = x_base + lean * (base - by) + dirn * k * 0.62
            th = max(1, int(round(width * 0.35 * (1 - k / ln))))
            for dx in range(th):
                ix, iy = int(round(x + dx * dirn)), int(round(y))
                if 0 <= ix < W and 0 <= iy < H:
                    out[iy, ix] = v[iy, ix] - dark
    return out


GAP_X = 112                              # the canopy opens above here; the light comes through it
FAINT = [(47, 1.6, 0.004), (99, 1.6, 0.012), (144, 2.0, -0.006)]
FAR = [  # (x at the root, width, lean px/line, branches)
    (19, 4.0, 0.012, ((50, 1, 10),)), (54, 4.5, 0.006, ((44, -1, 9), (30, 1, 7))),
    (120, 5.0, -0.008, ((54, 1, 11),)), (138, 3.0, 0.010, ()), (84, 2.0, 0.016, ()),
]
MID = [  # nearer: darker, wider, lit on the glow side
    (37, 7.0, 0.012, ((66, -1, 13), (38, 1, 9))), (124, 7.5, -0.012, ((60, 1, 12), (30, -1, 8))),
]
BEAMS = [  # (x where the beam leaves the canopy, y there, half width, strength)
    (104, 22, 3.6, 1.25), (116, 20, 2.2, 1.1), (125, 22, 4.6, 1.0), (94, 26, 1.6, 0.9), (136, 28, 2.4, 0.8),
]
BEAM_SLOPE = -0.36                       # MC pixels per line: down and to the left


def beams():
    out = np.zeros((H, W))
    for bx, by, hw, st in BEAMS:
        xc = bx + BEAM_SLOPE * (yy - by)
        spread = hw * (1 + 0.012 * (yy - by))
        core = np.clip((spread + 0.8 - np.abs(xx - xc)) / 1.6, 0, 1)            # flat with soft edges
        fade = np.clip((yy - by) / 10, 0, 1) * np.clip((150 - yy) / 64, 0, 1) ** 0.8
        out = np.maximum(out, st * core * fade)
    return out


def paint_haze():
    v = mist_value()
    for k, (xb, w, lean) in enumerate(FAINT):                                    # farthest: barely there
        v = tree(v, xb, w, lean, dark=0.45, lit=0.0, seed=90 + k, soft=0.8)
    for k, (xb, w, lean, br) in enumerate(FAR):
        v = tree(v, xb, w, lean, dark=0.95, lit=0.25, seed=10 + k, branches=br, soft=0.5)
    v += beams()
    cap = np.interp(yy, [0, 96, 124, 200], [5.6, 5.2, 3.95, 3.95])               # no white near the fight
    pic = ramp_dither(np.minimum(v, cap), MIST)
    cool = np.clip((70 - yy) / 40, 0, 1) > bayer(xx, yy)                         # sky light near the canopy
    pic[(pic == 15) & cool] = 3
    # two nearer trees in front of the beams: dark grey, with a light grey rim toward the light
    for k, (xb, w, lean, br) in enumerate(MID):
        t = tree(np.zeros((H, W)), xb, w, lean, dark=10, lit=0, seed=30 + k, branches=br)
        inside = t < -5
        yi = np.clip(yy.astype(int), 0, H - 1)
        wob = 0.7 * noise1d(H + 10, 18, 2, seed=30 + k)
        xc = xb + lean * (148 - yy) + wob[yi]
        toward = 1 if GAP_X > xb else -1
        rim = inside & ((xx - xc) * toward > w / 2 - 1.6) & (yy > 20)
        bark = np.where(noise2d(1.0, 9, 2, seed=35 + k) > 0.35, 12, 11)
        col = np.where(rim, np.where((bayer(xx, yy) < 0.75) | (yy > 120), 15, 11), bark)
        haze = np.clip((yy - 120) / 28, 0, 1)                                    # its roots lost in the haze
        col = np.where(haze > bayer(xx, yy) + 0.15, np.where(bayer(xx, yy) < 0.5, 14, 15), col)
        pic[inside] = col[inside]
    return pic


def path_geometry(depth):
    yi = np.clip(yy.astype(int), 0, H - 1)
    pc = 90 - 20 * depth ** 1.3 + 6 * np.sin(depth * 4.2) + 1.5 * noise1d(H, 26, 2, seed=5)[yi]
    pw = 1.5 + 14 * depth ** 1.3
    return pc, pw


def paint_floor(pic):
    horizon = 145 + 1.6 * noise1d(W, 16, 3, seed=3)
    depth = np.clip((yy - horizon[None, :]) / (198 - horizon[None, :]), 0, 1)
    floor = yy >= horizon[None, :]
    sun = np.zeros((H, W))
    for bx, by, hw, st in BEAMS:                                                  # pools where the beams land
        xl = bx + BEAM_SLOPE * (160 - by)
        pool = np.exp(-((xx - xl) * PX / (hw * 6 + 6)) ** 2 - ((yy - 161) / 6.5) ** 2)
        sun = np.maximum(sun, st * pool)
    tuft = noise2d(3.0, 1.6, 3, seed=21)
    fv = 1.85 + 1.0 * sun + 0.3 * tuft - 1.4 * np.clip((depth - 0.72) / 0.28, 0, 1) ** 1.5
    fv -= 1.0 * np.clip((np.abs(xx - 82) - 48) / 22, 0, 1)                       # shade under the big trees
    pc, pw = path_geometry(depth)
    margin = np.abs(xx - pc) < pw + 4
    fv = np.where(margin, np.clip(fv, 1.6, 2.3), fv)                             # plain moss beside the path
    grain = 0.45 * bayer(xx, yy) + 0.55 * np.clip(0.5 + 0.8 * noise2d(0.9, 2.0, 2, seed=22), 0, 1)
    fcol = ramp_dither(fv, FLOOR, threshold=grain)
    fcol = np.where(margin & (fcol != 13), 5, fcol)
    haze = np.clip(1 - depth / 0.24, 0, 1) ** 1.2                                  # the floor melts into the haze
    fcol = np.where(haze > bayer(xx, yy) * 0.95, np.where(fcol == 13, 13, 15), fcol)
    pic[floor] = fcol[floor]
    # undergrowth along the horizon: soft mounds in the haze
    r = np.random.default_rng(4)
    for _ in range(18):
        cx, w = r.uniform(0, 160), r.uniform(3, 9)
        h = w * r.uniform(0.5, 0.9)
        base = horizon[int(np.clip(cx, 0, W - 1))] + 2
        mound = (np.hypot((xx - cx) / w, (yy - base) / h) < 1 + 0.25 * noise2d(1.5, 1.5, 1, seed=5)) & (yy <= base)
        col = np.where(bayer(xx, yy) < 0.55, 14, 15)
        pic[mound] = col[mound]
    return depth, sun, horizon


def paint_path(pic, depth, sun):
    pc, pw = path_geometry(depth)
    edge = 1.2 * noise2d(2.5, 1.5, 2, seed=6)
    path = (yy > 147) & (np.abs(xx - pc) < pw + edge) & (depth > 0.04) & (yy < 190)
    ptex = noise2d(3.0, 1.2, 2, seed=7)
    pcol = np.where(ptex + 0.6 * sun > -0.15, 8, 9)
    rim = np.abs(xx - pc) > pw + edge - 1.3
    pcol = np.where(rim, np.where(bayer(xx, yy) < 0.5, 9, 5), pcol)
    pcol = np.where(depth < 0.22, np.where(bayer(xx, yy) < 0.3 + depth * 2.4, 8, 15), pcol)
    pic[path] = pcol[path]


def paint_canopy(pic, rng):
    """Backlit leaves: dark masses hanging into the corners, thin across the top, open above
    the gap where the light comes in. Clumps facing the gap catch green light."""
    clumps = []
    for _ in range(650):
        cx = rng.uniform(-10, 170)
        dist = np.clip(np.abs(cx - GAP_X) / 70, 0, 1)
        corner = np.clip((np.abs(cx - 82) - 48) / 30, 0, 1)                        # heavy at the corners
        limit = 5 + 30 * dist ** 1.6 + 38 * corner + (16 if cx > 136 else 0)
        cy = rng.uniform(-8, limit)
        if np.abs(cx - GAP_X) < 15 - 0.2 * cy and cy > -3:
            continue                                                               # the gap itself
        clumps.append((cx, cy, rng.uniform(1.8, 3.6), rng.uniform(2.2, 4.4)))
    clumps.sort(key=lambda c: c[1])
    cover = np.zeros((H, W), bool)
    tone = np.zeros((H, W))
    leafy = noise2d(1.2, 1.4, 2, seed=31)
    for cx, cy, rx, ry in clumps:
        f = blob_field(cx, cy, rx, ry) + 0.2 * leafy
        m = f > 0
        gx, gy = GAP_X - cx, (16 - cy) * 1.6 + 0.01
        n = np.hypot(gx, gy) + 1e-9
        facing = (xx - cx) / rx * gx / n + (yy - cy) / ry * gy / n
        near = np.exp(-((cx - GAP_X) * PX / 70) ** 2)
        t = 0.1 + 0.7 * near + (0.55 + 0.9 * near) * np.clip(facing, 0, 1) + 0.25 * leafy - 0.6 * np.clip(f - 0.4, 0, 1)
        if cx < 16 or cx > 146:
            t = t * 0.6
        tone = np.where(m, t, tone)                                              # lower clumps in front
        cover |= m
    ccol = ramp_dither(np.clip(tone, 0, 1.9), CANOPY, threshold=0.5 * bayer(xx, yy) + 0.5 * (0.5 + 0.5 * leafy))
    tips = cover & ~np.roll(cover, -1, axis=0) & (tone > 0.9)
    ccol = np.where(tips, 13, ccol)                                               # lit leaf tips
    pic[cover] = ccol[cover]
    # the sky through the gap
    sky = ~cover & (yy < 12) & (np.abs(xx - GAP_X) < 18)
    pic[sky] = np.where(np.abs(xx - GAP_X) + yy * 0.6 < 9 + 3 * bayer(xx, yy), 1, 13)[sky]
    # moss strands hanging from the leaves at the sides
    for sx, top, ln in ((26, 34, 22), (31, 40, 12), (138, 40, 18), (133, 46, 10), (152, 50, 14), (12, 52, 10)):
        for k in range(ln):
            x = int(round(sx + 0.8 * np.sin(k / 3.0 + sx)))
            if 0 <= top + k < H and 0 <= x < W:
                pic[top + k, x] = 5 if k % 4 else 0
                if k % 5 == 2 and x + 1 < W:
                    pic[top + k, x + 1] = 5
    return cover


def paint_frame(pic):
    """The two massive trunks at the edges: ridged bark on a cylinder lit from the clearing,
    moss on the shaded side, the base flaring into roots split by dark crevices."""
    yi = np.clip(yy.astype(int), 0, H - 1)
    moss_n = noise2d(4, 6, 3, seed=42)
    for outer, w, toward, seed in ((-8, 27, 1, 50), (169, 30, -1, 60)):
        wob = 1.0 * noise1d(H + 10, 34, 2, seed=seed)
        flare = 18 * np.clip((yy - 146) / 44, 0, 1) ** 2.0
        inner = outer + toward * (w + wob[yi] + 1.0 * np.sin(yy / 19 + seed) + flare)
        inside = (xx - inner) * toward < 0
        width = np.maximum((inner - outer) * toward, 1)
        u = np.clip((xx - outer) * toward / width, 0, 1)                             # 0 outer .. 1 lit edge
        rel = (xx - outer) * toward
        ridge = np.sin(2 * np.pi * rel / 3.3 + 2.2 * noise1d(H + 10, 16, 2, seed=seed + 3)[yi])
        b = 0.2 + 1.65 * u ** 2.6 + 0.45 * ridge * (0.35 + 0.65 * u)
        lit = np.clip(1 - np.abs(yy - 100) / 75, 0, 1) * (yy > 46)
        rimw = np.where(yy > 150, 0.95, 0.86)
        b += np.where(u > rimw, 1.5 * lit, 0)                                       # rim light from the clearing
        b = np.where(yy < 46, np.minimum(b, 1.6), b)                               # in the canopy's shade
        tcol = ramp_dither(b, BARK, threshold=0.55 * bayer(xx, yy) + 0.25)
        # the root flare: crevices sweeping out from the trunk, lobes lit on top
        for k, (frac, bend) in enumerate(((0.62, 0.55), (0.84, 0.85))):
            yk = np.linspace(150, 196, 160)
            xk = outer + toward * (w * frac + 18 * np.clip((yk - 146) / 44, 0, 1) ** 2.0 * bend)
            for x, y in zip(xk, yk):
                ix, iy = int(round(x)), int(round(y))
                if 0 <= ix < W and 0 <= iy < H and inside[iy, ix]:
                    tcol[iy, ix] = 0
                    ix2 = ix + toward
                    if 0 <= ix2 < W and inside[iy, ix2]:
                        tcol[iy, ix2] = 11 if bayer(ix2, iy) < 0.7 else 9
        mossy = (moss_n > 0.1 - 0.7 * np.clip((yy - 90) / 70, 0, 1)) & (u > 0.18) & (u < 0.62) & (yy > 70)
        tcol = np.where(mossy & (bayer(xx, yy) < 0.65) & (tcol != 0), 5, tcol)
        tcol = np.where((tcol == 12) & (u < 0.6), 11, tcol)
        tcol = np.where((tcol == 9) & (u > 0.7), 11, tcol)                        # brown only in the shadow
        pic[inside] = tcol[inside]


def fern(pic, bx, by, sgn, size, lift=1.1, droop=0.45):
    """A fern frond in the foreground shadow from (bx, by), arching up and out (sgn +1 right):
    a dark rachis, leaflets alternating along it, green on their upper edges."""
    xs, ys, ts = bezier([(bx, by), (bx + sgn * size * 0.3, by - size * lift), (bx + sgn * size, by - size * droop)],
                        int(size * 10))
    for i, (x, y, t) in enumerate(zip(xs, ys, ts)):
        ix, iy = int(round(x)), int(round(y))
        if 0 <= ix < W and 0 <= iy < H:
            pic[iy, ix] = 5 if t < 0.9 else 0
        if i % 5 == 0 and t > 0.04:
            ln = (1 - t) ** 0.8 * size * 0.2 + 0.8
            for s2 in (-1, 1):
                for k in np.arange(0.5, ln + 0.01, 0.5):
                    lx = int(round(x + sgn * k * 0.55))
                    ly = int(round(y + s2 * k * 0.55 + k * 0.35))
                    if 0 <= lx < W and 0 <= ly < H:
                        upper = s2 < 0
                        tip = k > ln - 1.0
                        pic[ly, lx] = (13 if tip and t < 0.75 else 5) if upper else (5 if k < 1.0 else 0)


def paint_foreground(pic, rng):
    fgn = noise1d(W, 5, 3, seed=70)
    fg = yy > 189 + 2.5 * fgn[None, :]
    pic[fg] = 0
    for bx, sgn, size, lift in ((2, 1, 38, 1.35), (14, 1, 30, 1.1), (0, 1, 24, 0.8), (158, -1, 38, 1.4),
                                (146, -1, 30, 1.1), (162, -1, 24, 0.8), (44, -1, 13, 1.0), (120, 1, 14, 1.0),
                                (68, -1, 9, 0.9), (100, 1, 9, 0.9)):
        fern(pic, bx, 198, sgn, size, lift=lift)
    for gx in range(0, W):                                                       # grass blades
        if rng.random() < 0.45:
            h = int(rng.integers(3, 9))
            base = 194 + int(rng.integers(0, 3))
            for k in range(h):
                if 0 <= base - k < H:
                    pic[base - k, gx] = 0 if k < h - 1 else 5


def paint_details(pic):
    for mx, my, size in ((26, 177, 3), (30, 179, 2), (23, 180, 2), (133, 176, 3), (129, 178, 2)):
        pic[my - 1:my + 2, mx] = 1                                                # stem
        pic[my - 2, mx - size // 2:mx + size // 2 + 1] = 10                       # cap
        pic[my - 3, mx - size // 2 + (size > 2):mx + size // 2 + (size <= 2)] = 10
        if size > 2:
            pic[my - 3, mx] = 1
    for sx, sy, sw in ((62, 183, 4), (110, 180, 3), (56, 177, 2)):
        for dy in range(3):
            for dx in range(-sw + abs(dy - 1), sw - abs(dy - 1)):
                pic[sy + dy, sx + dx] = 15 if dy == 0 else 11


def paint():
    rng = np.random.default_rng(64)
    pic = paint_haze()
    depth, sun, _ = paint_floor(pic)
    paint_path(pic, depth, sun)
    paint_canopy(pic, rng)
    paint_frame(pic)
    paint_foreground(pic, rng)
    paint_details(pic)
    return pic


# ------------------------------------------------------------------ encoding

PARTNER = {14: 12, 12: 14, 3: 15, 15: 3}   # neutral equal-luma pairs only: no hue jumps into accents


def harmonize(pic, bg=BG):
    """In cells with more than three colors, fold equal-luma pairs together (light blue into
    grey, cyan into light grey): the luma stays, only the hue of a few pixels moves."""
    out = pic.copy()
    for cy in range(25):
        for cx in range(40):
            blk = out[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]
            vals, counts = np.unique(blk[blk != bg], return_counts=True)
            used = dict(zip(vals.tolist(), counts.tolist()))
            while len(used) > 3:
                pairs = [(n, c) for c, n in used.items() if PARTNER.get(c) in used]
                if not pairs:
                    break
                n, c = min(pairs)
                blk[blk == c] = PARTNER[c]
                used[PARTNER[c]] += used.pop(c)
    return out


def encode(pic, bg=BG, force=None):
    """Three colors per 4x8 cell plus the background. A cell with more picks the three that
    move the fewest pixels (weighted by luma distance); the rest go to the nearest luma.
    `force` maps cell -> {color: slot} to pin a color to %01/%10/%11. Returns codes, screen,
    colorram, clash list."""
    force = force or {}
    codes = np.zeros((H, W), np.uint8)
    screen, cram = np.zeros(1000, np.uint8), np.zeros(1000, np.uint8)
    clash = []
    for cy in range(25):
        for cx in range(40):
            cell = cy * 40 + cx
            blk = pic[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]
            vals, counts = np.unique(blk[blk != bg], return_counts=True)
            used = dict(zip(vals.tolist(), counts.tolist()))
            if len(used) > 3:
                best = None
                for keep in combinations(used, 3):
                    cost = sum(n * min(abs(Y601[c] - Y601[k]) for k in keep + (bg,))
                               for c, n in used.items() if c not in keep)
                    if best is None or cost < best[0]:
                        best = (cost, keep)
                keep = list(best[1])
                clash.append((cx, cy, [c for c in used if c not in keep]))
            else:
                keep = list(used)
            keep.sort(key=lambda c: -used[c])
            slots = dict(force.get(cell, {}))
            for c in keep:
                if c not in slots:
                    slots[c] = [s for s in (3, 1, 2) if s not in slots.values()][0]
            lut = np.zeros(16, np.uint8)
            for c in range(16):
                if c == bg:
                    continue
                if c in slots:
                    lut[c] = slots[c]
                else:
                    near = min(list(slots) + [bg], key=lambda k: abs(Y601[k] - Y601[c]))
                    lut[c] = slots.get(near, 0)
            codes[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4] = lut[blk]
            by = {s: c for c, s in slots.items()}
            screen[cell] = by.get(1, 0) << 4 | by.get(2, 0)
            cram[cell] = by.get(3, 0)
    return codes, screen, cram, clash


def decode(codes, screen, cram, bg=BG):
    """What the VIC-II shows for an encoded bitmap."""
    out = np.zeros((H, W), np.uint8)
    for cy in range(25):
        for cx in range(40):
            s, c = screen[cy * 40 + cx], cram[cy * 40 + cx]
            lut = np.array([bg, s >> 4, s & 15, c], np.uint8)
            out[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4] = lut[codes[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]]
    return out


def sparkle_cells(pic, n=10):
    """Motes in the light shafts: one pixel in a cell that uses at most two colors, put alone
    in color RAM so it can twinkle without touching anything else."""
    r = np.random.default_rng(77)
    b = beams()
    found, cells = [], set()
    for _ in range(4000):
        if len(found) == n:
            break
        y, x = int(r.integers(40, 128)), int(r.integers(30, 140))
        cy, cx = y // 8, x // 4
        if b[y, x] < 0.7 or (cx, cy) in cells or any(abs(cx - a) + abs(cy - c) < 4 for a, c in cells):
            continue
        blk = pic[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]
        cols = set(np.unique(blk).tolist()) - {BG}
        if len(cols) > 2 or 1 in cols:
            continue
        base = int(pic[y, x])
        found.append((cy * 40 + cx, (x, y), base))
        cells.add((cx, cy))
    return found


def build():
    pic = harmonize(paint())
    motes = sparkle_cells(pic)
    force = {}
    for cell, (x, y), base in motes:
        pic[y, x] = 1                                                             # alone in color RAM
        force[cell] = {1: 3}
    codes, screen, cram, clash = encode(pic, force=force)
    sparkles = []
    for cell, (x, y), base in motes:
        cycle = [base, base, base, 13, 1, 1, 13, base, base, base, base, base]
        cram[cell] = base                                                         # off until animated
        sparkles.append((cell, "cram", cycle))
    shown = decode(codes, screen, cram)
    moved = sum(int((shown[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4] != pic[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]).sum())
                for cx, cy, *_ in clash)
    return {
        "bitmap": pack_mc_bitmap(codes),
        "screen": bytes(screen),
        "colorram": bytes(cram),
        "bg": BG,
        "pixels": shown,
        "paint": pic,
        "clash": {"cells": len(clash), "pixels": moved, "list": clash[:20]},
        "sparkles": sparkles,
    }


def save_png(img, path):
    from PIL import Image
    im = Image.fromarray(np.asarray(img, np.uint8).repeat(2, axis=1), "P")
    im.putpalette([v for rgb in COLODORE for v in rgb])
    im.save(path)


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    r = build()
    save_png(r["paint"], out / "paint.png")
    save_png(r["pixels"], out / "encoded.png")
    print(f"clash: {r['clash']['cells']} cells, {r['clash']['pixels']} pixels; sparkles {len(r['sparkles'])}")
