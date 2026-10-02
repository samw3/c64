"""Testcard: exercises every path of the harness and is what tests/test_e2e.py checks.

Multicolor bitmap ($6000, screen $5C00), cell rows:
     0      white bar           %11 -> color RAM
     2- 4   16 swatches         %11 -> color RAM       (swatch k = color k, 2 cells wide, from cell x 4)
     6- 8   16 swatches         %01 -> screen hi nybble
    10-12   16 swatches         %10 -> screen lo nybble
    13-15   empty lane for sprite 0 (yellow ring, moves +1 px per frame)
    16-23   dithered luma ramp through all 16 colors
    24      light grey bar      %10 -> screen lo nybble
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from harness.gfx import (LUMA_ORDER, Bank, d018, encode_mc_bitmap, pack_mc_bitmap, sprite_hires,  # noqa: E402
                         sprite_pointer, write)

SCREEN, BITMAP, SPRITE0 = 0x5C00, 0x6000, 0x5000
BG, BORDER = 0, 11
SWATCH_X0 = 4  # first swatch cell column

codes = np.zeros((200, 160), dtype=np.uint8)
screen = np.zeros(1000, dtype=np.uint8)
color = np.zeros(1000, dtype=np.uint8)


def cells(row0: int, rows: int, col0: int, cols: int, code: int, hi=0, lo=0, cram=0) -> None:
    codes[row0 * 8 : (row0 + rows) * 8, col0 * 4 : (col0 + cols) * 4] = code
    for cy in range(row0, row0 + rows):
        for cx in range(col0, col0 + cols):
            screen[cy * 40 + cx] = hi << 4 | lo
            color[cy * 40 + cx] = cram


cells(0, 1, 0, 40, 0b11, cram=1)
cells(24, 1, 0, 40, 0b10, lo=15)
for k in range(16):
    cx = SWATCH_X0 + 2 * k
    cells(2, 3, cx, 2, 0b11, cram=k)
    cells(6, 3, cx, 2, 0b01, hi=k)
    cells(10, 3, cx, 2, 0b10, lo=k)

# Luma ramp: 4x4 ordered dither between neighbouring luma steps, encoded by the generic encoder.
bayer = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0
ramp = np.zeros((64, 160), dtype=np.uint8)
for y in range(64):
    for x in range(160):
        t = x / 159 * (len(LUMA_ORDER) - 1)
        i = int(t)
        frac = t - i
        ramp[y, x] = LUMA_ORDER[min(i + (frac > bayer[y % 4, x % 4]), 15)]
pixels = np.zeros((200, 160), dtype=np.uint8)
pixels[128:192] = ramp
bm2, scr2, col2, clash = encode_mc_bitmap(pixels, BG)
assert clash["clash_cells"] == 0, clash
scr2, col2 = np.frombuffer(scr2, np.uint8), np.frombuffer(col2, np.uint8)
codes_bitmap = bytearray(pack_mc_bitmap(codes))  # merge: only cell rows 16-23 come from the encoder
codes_bitmap[16 * 320 : 24 * 320] = bm2[16 * 320 : 24 * 320]
screen[16 * 40 : 24 * 40] = scr2[16 * 40 : 24 * 40]
color[16 * 40 : 24 * 40] = col2[16 * 40 : 24 * 40]

ring = sprite_hires([
    "........########........",
    "......############......",
    ".....##############.....",
    "....######....######....",
    "...#####........#####...",
    "...####..........####...",
    "..####............####..",
    "..####............####..",
    "..###..............###..",
    "..###..............###..",
    "..###..............###..",
    "..###..............###..",
    "..###..............###..",
    "..####............####..",
    "..####............####..",
    "...####..........####...",
    "...#####........#####...",
    "....######....######....",
    ".....##############.....",
    "......############......",
    "........########........",
])

bank = Bank()
bank.put(SPRITE0, ring, "sprite 0 ring")
bank.put(SCREEN, screen.tobytes(), "screen RAM")
bank.put(SCREEN + 0x3F8, bytes([sprite_pointer(SPRITE0)]), "sprite pointers")
bank.put(BITMAP, bytes(codes_bitmap), "multicolor bitmap")

write(sys.argv[1], bank, color, {
    "GFX_SCREEN": SCREEN,
    "GFX_BITMAP": BITMAP,
    "GFX_D018": d018(SCREEN, bitmap=BITMAP),
    "GFX_BG": BG,
    "GFX_BORDER": BORDER,
    "GFX_SPRITE_PTR": sprite_pointer(SPRITE0),
    "GFX_LUMA": LUMA_ORDER,
})
