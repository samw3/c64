"""Scene graphics generator: paint at C64 resolution, encode, place in the VIC bank.

Run by `make` as: python3 gfx.py <build dir>. Writes bank.bin, colorram.bin and gfx.asm there.
This template paints a placeholder multicolor bitmap (160x200 double-wide pixels).
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.gfx import Bank, d018, encode_mc_bitmap, write  # noqa: E402

SCREEN, BITMAP = 0x5C00, 0x6000   # bank 1: screen $5C00-$5FE7 (+ sprite pointers $5FF8), bitmap $6000-$7F3F
BG, BORDER = 0, 0                 # $D021 is shared by every cell; $D020

# --- paint: pixels[y, x] = C64 color index, x in 0..159 (each pixel is 2 hires pixels wide) ---
pixels = np.full((200, 160), BG, dtype=np.uint8)
bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0
sky = [6, 4, 10, 8, 7]            # blue -> purple -> light red -> orange -> yellow
for y in range(173):
    t = min(y / 150, 1.0) * (len(sky) - 1)
    i = int(t)
    for x in range(160):
        pixels[y, x] = sky[min(i + (t - i > bayer[y % 4, x % 4]), len(sky) - 1)]
yy, xx = np.mgrid[0:200, 0:160]
pixels[((xx - 80) * 2) ** 2 + (yy - 110) ** 2 < 30 ** 2] = 1                     # sun (x doubled: wide pixels)
pixels[yy > 140 + 12 * np.sin(xx / 9.0) + 6 * np.sin(xx / 3.7)] = 11              # hills
pixels[yy > 172] = 9                                                              # ground

# --- encode + place ---
bitmap, screen, colorram, clash = encode_mc_bitmap(pixels, BG)
if clash["clash_cells"]:
    print(f"warning: color clash in {clash['clash_cells']} cells ({clash['clash_pixels']} px), e.g. {clash['cells'][:5]}")

bank = Bank()
bank.put(SCREEN, screen, "screen RAM")
bank.put(BITMAP, bitmap, "multicolor bitmap")

write(sys.argv[1], bank, colorram, {
    "GFX_D018": d018(SCREEN, bitmap=BITMAP),
    "GFX_BG": BG,
    "GFX_BORDER": BORDER,
})
