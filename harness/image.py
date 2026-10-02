"""The C64H scene image: a .prg loaded at $3B00 holding everything the VIC-II sees plus the code.

    $3B00-$3BFF  header page   magic "C64H", version, flags, entry, mailbox, VIC-II shadow, title
    $3C00-$3FE7  color RAM     1000 nybbles, copied to $D800 by the routine's init
    $4000-$7FFF  VIC bank 1    16K, all of it visible to the VIC-II
    $8000-$8FFF  code          raster routine, up to 4K; the file ends at its last byte

Keep these constants in sync with include/harness.asm (tests/test_e2e.py cross-checks them).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

HX_LOAD = 0x3B00
HX_HEADER = 0x3B00
HX_STATUS = 0x3B08
HX_FRAME = 0x3B09
HX_VIC = 0x3B40
HX_TITLE = 0x3B80
HX_COLORRAM = 0x3C00
HX_BANK = 0x4000
HX_CODE = 0x8000
HX_CODE_MAX = 0x1000

HX_MAGIC = b"C64H"
HX_VERSION = 1
HX_FLAG_NTSC = 0x01
HX_ST_BOOT = 0x00
HX_ST_READY = 0x01
HX_ST_ERROR = 0x80

COLORRAM_LEN = 1000
BANK_LEN = 0x4000
VIC_REGS = 0x2F
FIXED_LEN = HX_CODE - HX_LOAD  # header + color RAM + bank

COLOR_NAMES = [
    "black", "white", "red", "cyan", "purple", "green", "blue", "yellow",
    "orange", "brown", "light red", "dark grey", "grey", "light green", "light blue", "light grey",
]


class ImageError(ValueError):
    pass


@dataclass
class Image:
    path: Path | None
    data: bytes  # memory image starting at HX_LOAD (no load address)

    @classmethod
    def load(cls, path: str | Path) -> "Image":
        raw = Path(path).read_bytes()
        if len(raw) < 2:
            raise ImageError(f"{path}: not a .prg file")
        load = raw[0] | raw[1] << 8
        if load != HX_LOAD:
            raise ImageError(f"{path}: load address ${load:04X}, expected ${HX_LOAD:04X} (is this a run.prg?)")
        img = cls(Path(path), raw[2:])
        img.validate()
        return img

    def validate(self) -> None:
        if self.data[:4] != HX_MAGIC:
            raise ImageError("missing C64H magic")
        if self.version != HX_VERSION:
            raise ImageError(f"unsupported image version {self.version}")
        if len(self.data) <= FIXED_LEN:
            raise ImageError("image has no code (it must extend past $8000)")
        if len(self.code) > HX_CODE_MAX:
            raise ImageError(f"code is {len(self.code)} bytes, limit is {HX_CODE_MAX}")
        if not HX_CODE <= self.entry < HX_CODE + len(self.code):
            raise ImageError(f"entry ${self.entry:04X} is outside the code block")

    # ---------------------------------------------------------------- fields
    def byte(self, addr: int) -> int:
        return self.data[addr - HX_LOAD]

    def span(self, addr: int, length: int) -> bytes:
        return self.data[addr - HX_LOAD : addr - HX_LOAD + length]

    @property
    def version(self) -> int:
        return self.byte(HX_HEADER + 4)

    @property
    def flags(self) -> int:
        return self.byte(HX_HEADER + 5)

    @property
    def ntsc(self) -> bool:
        return bool(self.flags & HX_FLAG_NTSC)

    @property
    def entry(self) -> int:
        return self.byte(HX_HEADER + 6) | self.byte(HX_HEADER + 7) << 8

    @property
    def title(self) -> str:
        raw = self.span(HX_TITLE, 64)
        return raw.split(b"\0", 1)[0].decode("ascii", "replace")

    @property
    def vic(self) -> bytes:
        return self.span(HX_VIC, VIC_REGS)

    @property
    def colorram(self) -> bytes:
        return bytes(b & 0x0F for b in self.span(HX_COLORRAM, COLORRAM_LEN))

    @property
    def bank(self) -> bytes:
        return self.span(HX_BANK, BANK_LEN)

    @property
    def code(self) -> bytes:
        return self.data[FIXED_LEN:]

    # ---------------------------------------------------------------- decoding
    def describe(self) -> dict:
        v = self.vic
        d011, d016, d018 = v[0x11], v[0x16], v[0x18]
        ecm, bmm, mcm = bool(d011 & 0x40), bool(d011 & 0x20), bool(d016 & 0x10)
        mode = {
            (0, 0, 0): "standard text",
            (0, 0, 1): "multicolor text",
            (0, 1, 0): "hires bitmap",
            (0, 1, 1): "multicolor bitmap",
            (1, 0, 0): "extended color text",
        }.get((ecm, bmm, mcm), "invalid (black screen)")
        screen = HX_BANK + (d018 >> 4) * 0x400
        info: dict = {
            "title": self.title,
            "version": self.version,
            "video": "NTSC" if self.ntsc else "PAL",
            "entry": f"${self.entry:04X}",
            "code_bytes": len(self.code),
            "code_free": HX_CODE_MAX - len(self.code),
            "mode": mode,
            "display_enabled": bool(d011 & 0x10),
            "rows": 25 if d011 & 0x08 else 24,
            "columns": 40 if d016 & 0x08 else 38,
            "scroll": {"x": d016 & 7, "y": d011 & 7},
            "screen": f"${screen:04X}",
            "registers": {f"$D0{r:02X}": f"${v[r]:02X}" for r in (0x11, 0x16, 0x18, 0x15, 0x1A)},
            "colors": {
                "border": _color(v[0x20]),
                "background": _color(v[0x21]),
            },
        }
        if bmm:
            info["bitmap"] = f"${HX_BANK + (d018 & 0x08) * 0x400:04X}"
        else:
            info["charset"] = f"${HX_BANK + (d018 & 0x0E) * 0x400:04X}"
        if (mcm and not bmm) or ecm:
            info["colors"]["background1"] = _color(v[0x22])
            info["colors"]["background2"] = _color(v[0x23])
        if ecm:
            info["colors"]["background3"] = _color(v[0x24])

        sprites = []
        for i in range(8):
            if not v[0x15] & (1 << i):
                continue
            ptr = self.byte(screen + 0x3F8 + i)
            sprites.append({
                "sprite": i,
                "x": v[2 * i] | ((v[0x10] >> i) & 1) << 8,
                "y": v[2 * i + 1],
                "pointer": f"${ptr:02X}",
                "data": f"${HX_BANK + ptr * 64:04X}",
                "color": _color(v[0x27 + i]),
                "multicolor": bool(v[0x1C] & (1 << i)),
                "expand_x": bool(v[0x1D] & (1 << i)),
                "expand_y": bool(v[0x17] & (1 << i)),
                "behind_background": bool(v[0x1B] & (1 << i)),
            })
        info["sprites"] = sprites
        if any(s["multicolor"] for s in sprites):
            info["colors"]["sprite_mc1"] = _color(v[0x25])
            info["colors"]["sprite_mc2"] = _color(v[0x26])

        hist = [0] * 16
        for c in self.colorram:
            hist[c] += 1
        info["colorram_usage"] = {COLOR_NAMES[c]: n for c, n in enumerate(hist) if n}
        return info


def _color(value: int) -> str:
    return f"{value & 15} {COLOR_NAMES[value & 15]}"


def load_symbols(path: str | Path) -> list[tuple[int, str]]:
    """Read a VICE label file (`al C:8000 .start`, as written by KickAss -vicesymbols)."""
    labels = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "al":
            addr = int(parts[1].split(":")[-1], 16)
            labels.append((addr, parts[2].lstrip(".")))
    return sorted(labels)


def label_for(addr: int, labels: list[tuple[int, str]]) -> str | None:
    best = None
    for a, name in labels:
        if a <= addr:
            best = (a, name)
        else:
            break
    if best is None:
        return None
    a, name = best
    return name if a == addr else f"{name}+{addr - a}"
