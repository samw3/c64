"""C64 palettes: VICE .vpl files (preferred) with a built-in colodore fallback."""

from __future__ import annotations

from pathlib import Path

from .image import COLOR_NAMES

# VICE 3.10 data/C64/colodore.vpl
COLODORE = [
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF), (0x96, 0x28, 0x2E), (0x5B, 0xD6, 0xCE),
    (0x9F, 0x2D, 0xAD), (0x41, 0xB9, 0x36), (0x27, 0x24, 0xC4), (0xEF, 0xF3, 0x47),
    (0x9F, 0x48, 0x15), (0x5E, 0x35, 0x00), (0xDA, 0x5F, 0x66), (0x47, 0x47, 0x47),
    (0x78, 0x78, 0x78), (0x91, 0xFF, 0x84), (0x68, 0x64, 0xFF), (0xAE, 0xAE, 0xAE),
]

# Color indices from darkest to brightest (pairs share a luma level on the newer VIC-II revisions).
LUMA_ORDER = [0, 6, 9, 2, 11, 4, 8, 12, 14, 5, 10, 3, 15, 7, 13, 1]

__all__ = ["COLODORE", "COLOR_NAMES", "LUMA_ORDER", "load_palette"]


def load_palette(spec: str = "colodore") -> tuple[str, list[tuple[int, int, int]]]:
    """`spec` is a VICE palette name (colodore, pepto-pal, ...) or a path to a .vpl file."""
    path = Path(spec)
    if not path.is_file():
        from .vice import find_x64sc, vice_data_dir

        try:
            data = vice_data_dir(find_x64sc())
        except FileNotFoundError:
            data = None
        path = data / "C64" / f"{spec}.vpl" if data else Path("/nonexistent")
    if not path.is_file():
        if spec == "colodore":
            return "colodore (built-in)", list(COLODORE)
        raise FileNotFoundError(f"palette {spec!r} not found")
    colors = []
    for line in path.read_text().splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 3:
            colors.append(tuple(int(p, 16) for p in parts[:3]))
    if len(colors) < 16:
        raise ValueError(f"{path}: expected 16 colors, found {len(colors)}")
    return path.stem, colors[:16]
