"""Helpers for scene generators (scenes/<name>/gfx.py).

A generator builds the 16K VIC bank and the 1000 color RAM nybbles in Python and calls
`write()`, which emits into the scene's build dir:
    bank.bin      16384 bytes, imported into the Bank segment ($4000-$7FFF)
    colorram.bin  1000 bytes, imported into the ColorRam segment
    gfx.asm       `.const` symbols (addresses, $D018 value, colors...) for main.asm

Pixel arrays are numpy arrays of C64 color indices (0-15), indexed [y, x]:
    multicolor bitmap  (200, 160)   one entry per double-wide pixel
    hires bitmap       (200, 320)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .palette import COLODORE, LUMA_ORDER

BANK_BASE = 0x4000
BANK_SIZE = 0x4000
COLORRAM_LEN = 1000

__all__ = [
    "Bank", "d018", "sprite_pointer", "pack_mc_bitmap", "pack_hires_bitmap", "encode_mc_bitmap",
    "encode_hires_bitmap", "sprite_hires", "sprite_mc", "quantize", "write", "LUMA_ORDER", "COLODORE",
]


class Bank:
    """The 16K VIC bank ($4000-$7FFF). `put` refuses overlapping regions."""

    def __init__(self) -> None:
        self.mem = bytearray(BANK_SIZE)
        self.regions: list[tuple[int, int, str]] = []

    def put(self, addr: int, data: bytes | bytearray | np.ndarray, name: str = "") -> int:
        data = bytes(np.asarray(data, dtype=np.uint8).ravel()) if isinstance(data, np.ndarray) else bytes(data)
        start, end = addr, addr + len(data)
        if not (BANK_BASE <= start and end <= BANK_BASE + BANK_SIZE):
            raise ValueError(f"{name or 'data'} ${start:04X}-${end - 1:04X} is outside the bank $4000-$7FFF")
        for s, e, n in self.regions:
            if start < e and s < end:
                raise ValueError(f"{name or 'data'} ${start:04X}-${end - 1:04X} overlaps {n} ${s:04X}-${e - 1:04X}")
        self.mem[start - BANK_BASE : end - BANK_BASE] = data
        self.regions.append((start, end, name or f"${start:04X}"))
        return addr

    def report(self) -> str:
        used = sum(e - s for s, e, _ in self.regions)
        lines = [f"  ${s:04X}-${e - 1:04X} {e - s:5d}  {n}" for s, e, n in sorted(self.regions)]
        return "\n".join(lines + [f"  bank: {used} bytes placed, {BANK_SIZE - used} free"])


def d018(screen: int, charset: int | None = None, bitmap: int | None = None) -> int:
    """$D018 value for a screen plus either a charset or a bitmap, all absolute addresses in bank 1."""
    def off(addr: int, align: int, what: str) -> int:
        o = addr - BANK_BASE
        if not 0 <= o < BANK_SIZE or o % align:
            raise ValueError(f"{what} ${addr:04X} must be in $4000-$7FFF and aligned to ${align:X}")
        return o // align
    value = off(screen, 0x400, "screen") << 4
    if bitmap is not None:
        value |= off(bitmap, 0x2000, "bitmap") << 3
    elif charset is not None:
        value |= off(charset, 0x800, "charset") << 1
    return value


def sprite_pointer(addr: int) -> int:
    o = addr - BANK_BASE
    if not 0 <= o < BANK_SIZE or o % 64:
        raise ValueError(f"sprite data ${addr:04X} must be in the bank and 64-byte aligned")
    return o // 64


def pack_mc_bitmap(codes: np.ndarray) -> bytes:
    """(200, 160) array of 2-bit codes -> 8000 bitmap bytes in VIC cell order.
    %00 = $D021, %01 = screen high nybble, %10 = screen low nybble, %11 = color RAM."""
    a = np.asarray(codes, dtype=np.uint8).reshape(25, 8, 40, 4)
    b = (a[..., 0] << 6) | (a[..., 1] << 4) | (a[..., 2] << 2) | a[..., 3]
    return b.transpose(0, 2, 1).astype(np.uint8).tobytes()


def pack_hires_bitmap(bits: np.ndarray) -> bytes:
    """(200, 320) array of 0/1 -> 8000 bitmap bytes. 1 = screen high nybble, 0 = low nybble."""
    a = np.asarray(bits, dtype=np.uint8).reshape(25, 8, 40, 8)
    b = (a * (1 << np.arange(7, -1, -1))).sum(-1)
    return b.transpose(0, 2, 1).astype(np.uint8).tobytes()


def _nearest(color: int, choices: list[int], palette) -> int:
    r, g, b = palette[color]
    return min(choices, key=lambda c: (palette[c][0] - r) ** 2 + (palette[c][1] - g) ** 2 + (palette[c][2] - b) ** 2)


def encode_mc_bitmap(pixels: np.ndarray, background: int, palette=COLODORE):
    """Encode (200, 160) color indices as a multicolor bitmap.

    Each 4x8 cell gets the background plus its 3 most used other colors; any further colors
    are replaced by the nearest allowed one (color clash). Returns
    (bitmap 8000 bytes, screen 1000 bytes, colorram 1000 bytes, clash report dict).
    """
    px = np.asarray(pixels, dtype=np.uint8)
    if px.shape != (200, 160):
        raise ValueError(f"multicolor bitmap must be (200, 160), got {px.shape}")
    codes = np.zeros_like(px)
    screen, color = bytearray(1000), bytearray(1000)
    clash_cells, clash_px = [], 0
    for cy in range(25):
        for cx in range(40):
            blk = px[cy * 8 : cy * 8 + 8, cx * 4 : cx * 4 + 4]
            vals, counts = np.unique(blk[blk != background], return_counts=True)
            order = [int(v) for v in vals[np.argsort(-counts, kind="stable")]]
            chosen = order[:3]
            slot = {background: 0}
            for code, col in zip((3, 1, 2), chosen):   # most used -> color RAM, then screen hi, lo
                slot[col] = code
            for col in order[3:]:
                slot[col] = slot[_nearest(col, chosen + [background], palette)]
                clash_px += int(counts[list(vals).index(col)])
            if len(order) > 3:
                clash_cells.append((cx, cy))
            lut = np.zeros(16, dtype=np.uint8)
            for col, code in slot.items():
                lut[col] = code
            codes[cy * 8 : cy * 8 + 8, cx * 4 : cx * 4 + 4] = lut[blk]
            by_code = {code: col for col, code in slot.items() if code}
            screen[cy * 40 + cx] = by_code.get(1, 0) << 4 | by_code.get(2, 0)
            color[cy * 40 + cx] = by_code.get(3, 0)
    report = {"clash_cells": len(clash_cells), "clash_pixels": clash_px, "cells": clash_cells[:20]}
    return pack_mc_bitmap(codes), bytes(screen), bytes(color), report


def encode_hires_bitmap(pixels: np.ndarray, palette=COLODORE):
    """Encode (200, 320) color indices as a hires bitmap (2 colors per 8x8 cell).
    Returns (bitmap 8000 bytes, screen 1000 bytes, clash report dict)."""
    px = np.asarray(pixels, dtype=np.uint8)
    if px.shape != (200, 320):
        raise ValueError(f"hires bitmap must be (200, 320), got {px.shape}")
    bits = np.zeros_like(px)
    screen = bytearray(1000)
    clash_cells, clash_px = [], 0
    for cy in range(25):
        for cx in range(40):
            blk = px[cy * 8 : cy * 8 + 8, cx * 8 : cx * 8 + 8]
            vals, counts = np.unique(blk, return_counts=True)
            order = [int(v) for v in vals[np.argsort(-counts, kind="stable")]]
            bg = order[0]
            fg = order[1] if len(order) > 1 else order[0]
            mapped = blk.copy()
            for col in order[2:]:
                mapped[blk == col] = _nearest(col, [bg, fg], palette)
                clash_px += int(counts[list(vals).index(col)])
            if len(order) > 2:
                clash_cells.append((cx, cy))
            bits[cy * 8 : cy * 8 + 8, cx * 8 : cx * 8 + 8] = mapped == fg if fg != bg else 0
            screen[cy * 40 + cx] = fg << 4 | bg
    report = {"clash_cells": len(clash_cells), "clash_pixels": clash_px, "cells": clash_cells[:20]}
    return pack_hires_bitmap(bits), bytes(screen), report


def _sprite_rows(rows, width: int, symbols: dict[str, int]) -> np.ndarray:
    if isinstance(rows, np.ndarray):
        a = rows.astype(np.uint8)
    else:
        if len(rows) != 21 or any(len(r) != width for r in rows):
            raise ValueError(f"sprite needs 21 rows of {width} characters")
        a = np.array([[symbols[ch] for ch in r] for r in rows], dtype=np.uint8)
    if a.shape != (21, width):
        raise ValueError(f"sprite must be (21, {width}), got {a.shape}")
    return a


def sprite_hires(rows) -> bytes:
    """21 strings of 24 chars ('#' or 'X' = set, '.' or ' ' = clear) or a (21, 24) 0/1 array -> 64 bytes."""
    a = _sprite_rows(rows, 24, {"#": 1, "X": 1, "1": 1, ".": 0, " ": 0, "0": 0})
    return pack_sprite(a.reshape(21, 3, 8) @ (1 << np.arange(7, -1, -1)))


def sprite_mc(rows) -> bytes:
    """21 strings of 12 chars or a (21, 12) array of codes -> 64 bytes.
    '.'/0 = transparent, '1' = $D025, '2' = sprite color ($D027+n), '3' = $D026."""
    a = _sprite_rows(rows, 12, {".": 0, " ": 0, "0": 0, "1": 1, "2": 2, "3": 3})
    return pack_sprite(a.reshape(21, 3, 4) @ np.array([64, 16, 4, 1]))


def pack_sprite(row_bytes: np.ndarray) -> bytes:
    return bytes(np.asarray(row_bytes, dtype=np.uint8).ravel()) + b"\0"


def quantize(rgb: np.ndarray, palette=COLODORE, colors: list[int] | None = None) -> np.ndarray:
    """Map an (h, w, 3) RGB array to the nearest C64 color indices (optionally limited to `colors`)."""
    allowed = list(range(16)) if colors is None else list(colors)
    pal = np.array([palette[c] for c in allowed], dtype=np.int32)
    d = ((np.asarray(rgb, dtype=np.int32)[..., None, :] - pal) ** 2).sum(-1)
    return np.array(allowed, dtype=np.uint8)[d.argmin(-1)]


def write(out_dir: str | Path, bank: Bank, colorram: bytes | np.ndarray, consts: dict | None = None) -> None:
    """Write bank.bin, colorram.bin and gfx.asm into the scene's build dir."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cram = bytes(np.asarray(colorram, dtype=np.uint8).ravel() & 0x0F) if isinstance(colorram, np.ndarray) \
        else bytes(b & 0x0F for b in colorram)
    if len(cram) != COLORRAM_LEN:
        raise ValueError(f"color RAM must be {COLORRAM_LEN} bytes, got {len(cram)}")
    (out / "bank.bin").write_bytes(bytes(bank.mem))
    (out / "colorram.bin").write_bytes(cram)
    lines = ["// generated by gfx.py - do not edit"]
    for name, value in (consts or {}).items():
        if isinstance(value, bool):
            value = int(value)
        if isinstance(value, int):
            lines.append(f".const {name} = ${value:04X}" if value > 0xFF else f".const {name} = ${value:02X}")
        elif isinstance(value, str):
            lines.append(f'.const {name} = "{value}"')
        else:
            items = ", ".join(f"${int(v) & 0xFF:02X}" for v in value)
            lines.append(f".const {name} = List().add({items})")
    (out / "gfx.asm").write_text("\n".join(lines) + "\n")
    print(bank.report())
