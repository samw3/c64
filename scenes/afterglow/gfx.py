"""Afterglow: a mountain lake at twilight. Multicolor bitmap with a per-line background.

The kernel in main.asm rewrites $D021 (background) and $D016 (xscroll) on every raster line,
so every line has its own %00 color:
  sky   the gradient is bands of %00 color, so the 3 cell colors stay free for the moon,
        clouds and mountains. One band alternates purple/orange per line: equal luma, so a
        PAL monitor blends them into a rose that isn't in the palette.
  lake  a mirror of everything above it, on a black %00. Per-frame records (64-frame loop)
        light ripple lines in that black and set each line's xscroll, so the reflection
        shimmers and wobbles. Stars twinkle from color RAM.
Painting is procedural (numpy): banded sky, faceted rock lit from the afterglow, snow,
mist, pines, the reflection, the reed bank. `encode` then fits each 4x8 cell to bg[y]
plus 3 colors, with bg chosen per line to minimise clash.

Coordinates: x 0..159 multicolor pixels, y 0..199 lines. With 38 columns and xscroll 4 the
visible part is x 2..152. Writes paint.png / encoded.png (painting vs. VIC-II frame 0) to
the build dir next to the data.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.gfx import COLODORE, Bank, d018, pack_mc_bitmap, write  # noqa: E402
from PIL import Image  # noqa: E402

W, H = 160, 200
Y_H = 120                          # waterline: a cell boundary, so the mirror keeps cell rows intact
Y_SHORE = 189                      # near shore
XC = 94                            # afterglow centre, in the valley between the mountains
PX = 2 * 0.936                     # width of a multicolor pixel in line heights (PAL aspect)

SCREEN, BITMAP = 0x5C00, 0x6000
BGTAB = 0x5B00                     # 200 bytes: $D021 per line
ANIM = 0x4000                      # 64 records: per lake line bg << 4 | xscroll, then star colors
FRAMES, WATER_LINES = 64, H - Y_H

rng = np.random.default_rng(1984)
yy, xx = np.mgrid[0:H, 0:W].astype(float)
x1 = np.arange(W, dtype=float)
BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16 + 1 / 32
LUMA = {c: i for i, c in enumerate([0, 6, 9, 2, 11, 4, 8, 12, 14, 5, 10, 3, 15, 7, 13, 1])}
DEEP = 16                          # black from a cell color: unlike %00 black, ripple lines don't light it
LUMA[DEEP] = 0


def bayer(x, y):
    return BAYER[np.asarray(y, int) % 4, np.asarray(x, int) % 4]


def noise1d(n, scale, octaves=4, seed=0):
    """Fractal value noise over 0..n-1, roughly -1..1."""
    r = np.random.default_rng(seed)
    out, amp, total = np.zeros(n), 1.0, 0.0
    for o in range(octaves):
        step = scale / 2 ** o
        pts = r.uniform(-1, 1, int(n / step) + 3)
        out += amp * np.interp(np.arange(n) / step, np.arange(len(pts)), pts)
        total += amp
        amp *= 0.5
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


def ramp_dither(v, ramp, x=xx, y=yy, threshold=None):
    """Ordered dither of a continuous ramp position v into ramp colors."""
    v = np.clip(v, 0, len(ramp) - 1 - 1e-6)
    i = np.floor(v).astype(int)
    up = (v - i) > (bayer(x, y) if threshold is None else threshold)
    return np.array(ramp)[np.minimum(i + up, len(ramp) - 1)]


# ------------------------------------------------------------------ sky
# Ramp positions; MIX alternates purple/orange line by line: equal luma, so a PAL monitor
# blends the pair into one rose color (the delay line averages each line's chroma with the last).
MIX = -1
SKY = [0, 6, 4, MIX, 10, 7, 1]


def sky_color(v, x=xx, y=yy):
    """Ordered dither between neighbouring SKY entries, resolving MIX per line."""
    cols = ramp_dither(v, SKY, x, y)
    return np.where(cols == MIX, np.where(np.asarray(y, int) % 2, 8, 4), cols)


# solid bands with dithered transitions (each plateau is one color, or the rose mix)
sky_v = np.interp(yy, [0, 4, 18, 27, 42, 57, 67, 80, 94, 119], [0, 0, 1, 1, 2, 2, 3, 3, 4, 4.7])
sky_v += 2.3 * np.exp(-((xx - XC) / 19) ** 2 - ((Y_H - yy) / 15) ** 2)        # afterglow core
sky_v += 0.8 * np.exp(-((xx - XC) / 50) ** 2 - ((Y_H - yy) / 40) ** 2)        # wide halo

# crescent moon, lit from the lower right (the sun is below the horizon in the valley)
MX, MY, MR = 36, 30, 9.5
mdx, mdy = (xx - MX) * PX, yy - MY
md = np.hypot(mdx, mdy)
sky_v += 0.32 * np.exp(-np.maximum(md - MR, 0) / 9)                           # moon halo
pic = sky_color(sky_v)

sd = np.hypot(mdx + 0.40 * MR, mdy + 0.36 * MR)                               # earth's shadow disc
crescent = (md < MR) & (sd > MR * 0.96)
pic[md < MR] = 6
pic[crescent] = 1
pic[crescent & ((sd < MR * 1.08) | (md > MR - 0.9))] = 15                       # soft edges

# stars: one per cell in the dark upper sky (they twinkle from color RAM)
stars = []
for cy in range(7):
    for cx in range(1, 38):
        if rng.random() < 0.16:
            x, y = cx * 4 + int(rng.integers(0, 4)), cy * 8 + int(rng.integers(0, 8))
            if sky_v[y, x] < 1.2 and md[y, x] > MR + 7:
                stars.append((x, y))
for x, y in stars:
    pic[y, x] = 15

# clouds: long tapered stratus with solid bodies; undersides lit by the glow below
CLOUDS = [  # (x0, x1, y, thickness, body, lit underside, hot underside near the glow)
    (54, 140, 80, 3.0, 2, 10, 7),
    (76, 128, 92, 2.4, 8, 7, 1),
    (98, 158, 70, 2.2, 4, 10, 10),
    (30, 84, 61, 1.6, 6, 4, 4),
]
for k, (x0, x1_, cy, th, body_c, lit_c, hot_c) in enumerate(CLOUDS):
    t = (x1 - x0) / (x1_ - x0)
    env = np.where((t > 0) & (t < 1), np.sin(np.pi * np.clip(t, 0, 1)) ** 0.8, 0)
    thick = th * env * (0.5 + 0.5 * noise1d(W, 7, 3, seed=30 + k))
    yc = cy + 2.0 * noise1d(W, 36, 2, seed=40 + k) + 0.03 * (x1 - x0)
    body = (yy > yc[None, :] - thick[None, :]) & (yy < yc[None, :] + 0.5 * thick[None, :]) & (thick[None, :] > 0.5)
    under = body & ~np.roll(body, -1, axis=0)
    hot = np.abs(xx - XC) < 22 + 6 * noise1d(W, 9, 2, seed=50 + k)[None, :]
    pic[body] = body_c
    pic[under] = np.where(hot, hot_c, lit_c)[under]

# ------------------------------------------------------------------ mountains


def ridge(peaks, seed, rough, scale=9):
    """Silhouette top y(x) from (x, y, left slope, right slope) peaks plus fractal roughness."""
    y = np.full(W, 1e9)
    for px, py, sl, sr in peaks:
        y = np.minimum(y, py + np.where(x1 < px, sl, sr) * np.abs(x1 - px) * PX)
    crags = 0.9 * noise1d(W, 6, 3, seed=seed + 11) + 0.6 * np.abs(noise1d(W, 4, 2, seed=seed + 7))
    return y + rough * (noise1d(W, scale, 5, seed=seed) + crags)


def faceted(top, light_from, seed, colors=(0, 6, 4), snow=None):
    """Backlit rock: a relief of ridge spurs running down the fall line from the silhouette,
    lit from the afterglow side (light_from -1: the glow is left of this mass, +1: right)."""
    r = np.random.default_rng(seed)
    slope = np.gradient(top)
    summits = [x for x in range(2, W - 2) if top[x] == top[max(0, x - 5):x + 6].min()]
    starts = [(x, d, 1.0) for x in summits for d in (-1, 1)]
    starts += [(int(x), np.sign(slope[int(x)]) or 1, 0.55) for x in r.uniform(0, W, 26)]
    relief = np.full((H, W), -1e9)
    for x0, direction, size in starts:
        y0 = top[x0] + (0 if size == 1.0 else r.uniform(0.5, 4))
        drift = direction * r.uniform(0.15, 0.5)
        meander = noise1d(H, 10, 3, seed=int(r.integers(1 << 30)))
        xs = x0 + drift * (yy - y0) + 1.2 * meander[yy.astype(int)]
        width = 0.6 + np.maximum(yy - y0, 0) * r.uniform(0.16, 0.28)
        ridge_h = 8 * size - np.abs(xx - xs) / width * 2.0 - np.maximum(yy - y0, 0) * 0.06
        relief = np.maximum(relief, np.where(yy >= y0 - 1, ridge_h, -1e9))
    facing = np.tanh(-np.gradient(relief, axis=1) * light_from * 3.0)
    depth = yy - top[None, :]
    glow = np.exp(-((xx - XC) / 26) ** 2)
    backlit = np.exp(-((xx - XC) / 16) ** 2)                                   # silhouettes against the glow
    shade = 0.5 + 0.62 * facing + 0.5 * np.exp(-depth / 4) * (facing > 0)
    shade += np.clip((yy - (Y_H - 30)) / 26, 0, 1) ** 1.5 * 0.8                # aerial haze near the lake
    shade -= 1.3 * backlit
    grain = 0.5 * bayer(xx, yy) + 0.5 * np.clip(0.5 + 0.6 * noise2d(1.5, 2.5, 2, seed=seed + 9), 0, 1)
    col = ramp_dither(np.clip(shade, 0, 1.999), list(colors), threshold=grain)   # rock: organic dither
    inside = (depth >= 0) & (yy < Y_H)
    pic[inside] = col[inside]
    edge = inside & (depth < 1.2) & (slope[None, :] * light_from < -0.2)
    rim = np.where(glow > 0.45, sky_color(sky_v + 0.6), colors[2])            # the rim takes the glow's color
    pic[edge] = rim[edge]
    if snow is not None:   # alpenglow: snow on the crests, pink where it faces the glow
        line = snow + 6 * noise1d(W, 7, 3, seed=seed + 3)[None, :] + 4 * noise1d(W, 20, 2, seed=seed + 6)[None, :]
        streaks = noise2d(2.5, 7, 3, seed=seed + 4)                           # snow lies in fall-line streaks
        fade = np.clip(depth / np.maximum(line - top[None, :], 1), 0, 1)
        cap = inside & (streaks > -0.9 + 1.5 * fade ** 1.5) & (yy < line)
        pic[cap] = np.where(facing[cap] > 0.0, 10, 14)                        # light blue in the shade


right = ridge([(132, 44, 1.1, 0.6), (118, 80, 1.3, 0.8), (150, 58, 0.55, 0.45)], 5, 3.0)
left = ridge([(16, 66, 0.5, 0.5), (44, 56, 0.55, 0.6), (62, 78, 0.5, 0.55), (74, 94, 0.6, 0.75)], 4, 2.8)
faceted(right, -1, 40, snow=68)
faceted(left, 1, 50, snow=74)

# mist lying along the far shore, in front of the mountains and behind the trees
mist_y = Y_H - 11 + 1.2 * noise1d(W, 30, 3, seed=8)[None, :]
mist = np.exp(-((yy - mist_y) / 3.0) ** 2) * (0.55 + 0.6 * noise2d(16, 2.5, 3, seed=12))
mist += 0.45 * np.exp(-((xx - XC) / 30) ** 2) * (np.abs(yy - mist_y) < 4)
mist += 0.5 * np.exp(-((xx - 34) / 9) ** 2) * (np.abs(yy - mist_y) < 3)          # behind the cabin roof
m = (mist > 0.35 + 0.4 * bayer(xx, yy)) & (yy < Y_H)
pic[m & np.isin(pic, [0, 6])] = 4
pic[m & (mist > 0.8) & (pic == 4)] = 10

# ------------------------------------------------------------------ treeline and cabin


def pine(cx, base, h, hw):
    """A pine silhouette: tiers of branches, tip at base - h."""
    for k in range(int(h)):
        y = int(base - h + k)
        w = hw * (k + 1) / h * (0.75 + 0.25 * ((k % 3) / 2))
        for x in range(int(np.floor(cx - w)), int(np.ceil(cx + w)) + 1):
            if 0 <= x < W and abs(x - cx) <= w + 0.3:
                pic[y, x] = 0


def forest(x, r):
    """Tree height range at x: dense on the left, a clearing for the cabin, low in the valley."""
    if 27 <= x <= 41:
        return (1, 3) if r.random() < 0.5 else (0, 0)
    if 78 <= x <= 112:
        return (1.5, 4)
    if x < 27:
        return (7, 15)
    return (5, 11) if x < 78 else (6, 13)


x = 1.0
while x < W:
    lo, hi = forest(x, rng)
    if hi:
        h = rng.uniform(lo, hi)
        pine(x, Y_H - rng.uniform(0, 1.5), h, max(0.8, h / 4.2))
    x += rng.uniform(1.0, 2.8)
pic[(yy >= Y_H - 2) & (yy < Y_H)] = 0                       # shoreline

CAB_X, CAB_Y = 34, 112                                      # cabin among the trees, left shore
cab = (xx >= CAB_X - 3) & (xx <= CAB_X + 3) & (yy >= CAB_Y) & (yy < Y_H)
roof = (yy >= CAB_Y - 5 + np.abs(xx - CAB_X) * 1.1) & (yy < CAB_Y + 1) & (np.abs(xx - CAB_X) <= 5)
pic[cab | roof] = 0
pic[(xx >= CAB_X + 2) & (xx <= CAB_X + 2) & (yy >= CAB_Y - 7) & (yy < CAB_Y - 2)] = 0   # chimney
pic[(np.abs(xx - CAB_X) <= 1.5) & (yy >= CAB_Y + 2) & (yy <= CAB_Y + 4)] = 8          # lit window
pic[(np.abs(xx - CAB_X) <= 0.5) & (yy >= CAB_Y + 2) & (yy <= CAB_Y + 4)] = 7
for k in range(10):                                         # chimney smoke, drifting right
    sx, sy = CAB_X + 2 + int(k * 0.8 + 0.6 * np.sin(k)), CAB_Y - 8 - k
    if 0 <= sy and rng.random() < 0.8 - k * 0.05:
        pic[sy, sx] = 4 if k < 6 else 6

# ------------------------------------------------------------------ the lake
sky_part = pic[:Y_H].copy()
shift = noise1d(H, 3, 2, seed=60)
for y in range(Y_H, H):
    d = y - Y_H
    row = np.roll(sky_part[2 * Y_H - 1 - y], int(round(shift[y] * (0.5 + d / 22))))
    row[row == 8] = 4                                       # the rose reflects as plain purple
    gaps = noise1d(W, 5 + d / 5, 3, seed=200 + y) < -0.7 + min(d, 50) / 110
    gaps &= (np.abs(x1 - XC) > 5 + d * 0.28) | (noise1d(W, 3, 2, seed=400 + y) < -0.6)
    row[gaps] = 0
    pic[y] = row
# the cabin window leaves a long broken streak on the water
for y in range(Y_H + 1, Y_H + 30):
    if (y - Y_H) % 3 and rng.random() < 0.9 - (y - Y_H) / 40:
        x = CAB_X + int(rng.integers(-1, 2) if y > Y_H + 8 else 0)
        pic[y, x] = 7 if y < Y_H + 12 else 10
# glitter path under the afterglow
for y in range(Y_H + 1, Y_SHORE):
    d = y - Y_H
    spread = 4 + d * 0.32
    for _ in range(4):
        if rng.random() < 0.7 - d / 140:
            gx = int(XC + rng.normal(0, spread))
            gw = int(rng.integers(1, 3 + d // 25))
            seg = pic[y, gx:gx + gw]
            seg[np.isin(seg, [10, 7, 1])] = 1 if rng.random() < 0.3 - d / 300 else 7

# ------------------------------------------------------------------ near shore
# A low bank with clumps of reeds and cattails, silhouetted against the bright reflection.
CLUMPS = [(2, 13, 8), (50, 8, 4), (124, 10, 6), (156, 9, 5)]               # (x, width, height)
bank_top = Y_SHORE + 1.5 * noise1d(W, 14, 3, seed=9)
for cx, cw, ch in CLUMPS:
    bank_top -= ch * np.exp(-((x1 - cx) / cw) ** 2)
fg = yy >= bank_top[None, :]


def blade(x, h, lean):
    base = int(bank_top[int(np.clip(x, 0, W - 1))]) + 1
    for k in range(int(h)):
        bx = int(round(x + lean * k * k / h))
        if 0 <= bx < W:
            fg[base - k, bx] = True


for _ in range(130):
    x = rng.uniform(0, W)
    clump = max(np.exp(-((x - cx) / cw) ** 2) for cx, cw, _ in CLUMPS)
    if rng.random() < 0.25 + clump:
        blade(x, rng.uniform(1, 4) + clump * rng.uniform(4, 12), rng.uniform(-0.3, 0.3))
for x, h in [(117, 17), (122, 23), (128, 15), (47, 13), (53, 17), (8, 19)]:   # cattails
    blade(x, h, 0.08)
    base = int(bank_top[x]) + 1
    fg[base - h + 1:base - h + 5, x] = True
    fg[base - h + 2:base - h + 4, x + 1] = True
pic[fg] = 0

# ------------------------------------------------------------------ background per line


def line_hist(pic):
    """hist[y, cx, c]: pixels of color c in line y of cell column cx (visible cells only)."""
    h = np.zeros((H, 40, 16), np.int32)
    for c in range(16):
        h[:, :, c] = (pic == c).reshape(H, 40, 4).sum(-1)
    return h


def row_cost(hist, bg, cy):
    """Pixels that do not fit in 3 cell colors, over one cell row, given bg per line."""
    h = hist[cy * 8:cy * 8 + 8].copy()
    h[np.arange(8), :, bg[cy * 8:cy * 8 + 8]] = 0
    cell = np.sort(h.sum(0), axis=-1)[:, ::-1]
    return int(cell[:, 3:].sum())


def choose_bg(pic, fixed):
    """$D021 per line: start from the most common color, then coordinate descent on clash."""
    hist = line_hist(pic)
    bg = np.array([np.bincount(pic[y, 2:153], minlength=16).argmax() for y in range(H)], np.uint8)
    for y, c in fixed.items():
        bg[y] = c
    for _ in range(4):
        for y in range(H):
            if y in fixed:
                continue
            cy, best = y // 8, (row_cost(hist, bg, y // 8), bg[y])
            for c in np.flatnonzero(hist[y].sum(0)):
                bg[y] = c
                best = min(best, (row_cost(hist, bg, cy), c))
            bg[y] = best[1]
    return bg


bg = choose_bg(pic, {y: 0 for y in range(Y_H, H)})      # the lake is black: ripple lines light it


# ------------------------------------------------------------------ encode
def encode(pic, bg, force_cram=()):
    """Multicolor bitmap where %00 is bg[y] (a per-line background). Up to 3 more colors per cell;
    `force_cram` cells keep a given color in color RAM. Returns codes, screen, colorram, clash list."""
    codes = np.zeros((H, W), dtype=np.uint8)
    screen, cram = np.zeros(1000, np.uint8), np.zeros(1000, np.uint8)
    forced = dict(force_cram)
    clash = []
    for cy in range(25):
        for cx in range(40):
            blk = pic[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]
            lines = bg[cy * 8:cy * 8 + 8, None]
            need = blk[blk != lines]
            vals, counts = np.unique(need, return_counts=True)
            order = [int(v) for v in vals[np.argsort(-counts, kind="stable")]]
            slots = {}
            f = forced.get((cx, cy))
            if f is not None and f in order:
                slots[f] = 3
                order.remove(f)
            for c in order:
                free = [s for s in (3, 1, 2) if s not in slots.values()]
                if free:
                    slots[c] = free[0]
            extra = [c for c in order if c not in slots]
            if extra:
                clash.append((cx, cy, extra))
            sub = np.zeros_like(blk)
            for r in range(8):
                for c in range(4):
                    col = int(blk[r, c])
                    if col == lines[r, 0]:
                        continue
                    if col not in slots:   # clash: nearest luma among what the cell has
                        opts = list(slots) + [int(lines[r, 0])]
                        col = min(opts, key=lambda o: abs(LUMA[o] - LUMA[col]))
                        if col == lines[r, 0]:
                            continue
                    sub[r, c] = slots[col]
            codes[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4] = sub
            by = {s: c for c, s in slots.items()}
            screen[cy * 40 + cx] = (by.get(1, 0) & 15) << 4 | by.get(2, 0) & 15
            cram[cy * 40 + cx] = by.get(3, 0) & 15
    return codes, screen, cram, clash


# Ripple lines light the lake's %00 black. Keep dashes of it DEEP so they break into glints,
# in every cell of the lake that still has a free color for it.
enc = pic.copy()
for y in range(Y_H, H):
    d = y - Y_H
    dash = noise1d(W, 3 + d / 9, 3, seed=700 + y) > -0.1 + 0.25 * np.sin(y * 0.7)
    enc[y, dash & (pic[y] == 0)] = DEEP
enc[fg] = DEEP                                              # the near shore never glints
for cy in range(Y_H // 8, 25):
    for cx in range(40):
        blk = enc[cy * 8:cy * 8 + 8, cx * 4:cx * 4 + 4]
        if len(set(np.unique(blk)) - {0}) > 3:
            blk[blk == DEEP] = 0
codes, screen, cram, clash = encode(enc, bg, [((x // 4, y // 8), 15) for x, y in stars])


def decode(codes, screen, cram, bg):
    """What the VIC-II shows on frame 0, to check the encoder against the painting."""
    out = np.zeros((H, W), np.uint8)
    for cy in range(25):
        for cx in range(40):
            s, c = screen[cy * 40 + cx], cram[cy * 40 + cx]
            for r in range(8):
                y = cy * 8 + r
                lut = np.array([bg[y], s >> 4, s & 15, c])
                out[y, cx * 4:cx * 4 + 4] = lut[codes[y, cx * 4:cx * 4 + 4]]
    return out


def save_png(img, path):
    """Indexed PNG at 320x200 (each multicolor pixel 2 wide), colodore palette."""
    im = Image.fromarray(np.asarray(img, np.uint8).repeat(2, 1), "P")
    im.putpalette([v for rgb in COLODORE for v in rgb])
    im.save(path)


out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
shown = decode(codes, screen, cram, bg)
save_png(pic, out / "paint.png")
save_png(shown, out / "encoded.png")
if clash:
    print(f"clash: {len(clash)} cells, {int((shown != pic).sum())} pixels moved to the nearest luma "
          f"(see paint.png vs encoded.png)")

# ------------------------------------------------------------------ animation
# One record per frame: 80 lake lines (bg << 4 | xscroll), then one color RAM value per star.
# Two waves roll towards the viewer; their spacing and height grow with perspective.
d = np.arange(WATER_LINES, dtype=float)
depth_ph = 2 * np.pi * 5.0 * (d / WATER_LINES) ** 0.6           # wave phase down the lake
amp = np.clip(0.25 + d * 0.06, 0, 3.0) * np.clip((72 - d) / 20, 0.35, 1)   # calmer by the reeds
records = []
for t in range(FRAMES):
    ph = 2 * np.pi * t / FRAMES
    wave = 0.7 * np.sin(depth_ph - ph) + 0.3 * np.sin(2.3 * depth_ph - 3 * ph + 1.3)
    xs = np.clip(4 + np.round(amp * wave), 0, 7).astype(np.uint8)
    crest = np.sin(depth_ph - ph + 0.6) > 0.93 - 0.12 * d / WATER_LINES
    lake = np.where(crest & (d > 1), 6, 0).astype(np.uint8)
    records.append(np.concatenate([lake << 4 | xs, np.zeros(len(stars), np.uint8)]))
records = np.array(records)

# twinkling: each star wanders between grey levels, with brief white flashes
LEVELS = [11, 12, 15, 1]
for k in range(len(stars)):
    r = np.random.default_rng(500 + k)
    base = r.choice([1, 2, 2, 2])
    level = base + np.round(0.6 * np.sin(2 * np.pi * (r.integers(1, 4) * np.arange(FRAMES) / FRAMES + r.random())))
    flash = r.random(FRAMES) < 0.06
    records[:, WATER_LINES + k] = np.array(LEVELS)[np.clip(level + flash * 2, 0, 3).astype(int)]
RECORD = records.shape[1]
assert ANIM + records.size <= BGTAB, "animation records overlap the bg table"

star_cells = [(y // 8) * 40 + x // 4 for x, y in stars]
assert len(set(star_cells)) == len(star_cells), "two stars share a cell"
cram[star_cells] = records[0, WATER_LINES:]

bank = Bank()
bank.put(ANIM, records, f"animation: {FRAMES} frames x {RECORD} bytes")
bank.put(BGTAB, bg, "bg per line")
bank.put(SCREEN, screen, "screen RAM")
bank.put(BITMAP, pack_mc_bitmap(codes), "multicolor bitmap")

write(out, bank, cram, {
    "GFX_D018": d018(SCREEN, bitmap=BITMAP),
    "GFX_BGTAB": BGTAB,
    "GFX_ANIM": ANIM,
    "GFX_ANIM_FRAMES": FRAMES,
    "GFX_ANIM_RECORD": RECORD,
    "GFX_WATER_Y": Y_H,
    "GFX_WATER_LINES": WATER_LINES,
    "GFX_STARS": len(stars),
    "GFX_STAR_LO": [(0xD800 + c) & 0xFF for c in star_cells],
    "GFX_STAR_HI": [(0xD800 + c) >> 8 for c in star_cells],
    "GFX_BG": int(bg[0]),
    "GFX_BORDER": 0,
})
