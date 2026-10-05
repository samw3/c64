"""Ambush: a fantasy hero fights five creatures in a forest clearing. Eight hardware sprites:

  sprite 0     the hero's hires black overlay      } one picture: crisp hires outlines and
  sprite 1     the hero's multicolor body          } shading over multicolor fills
  sprite 2     the hero's sword (hires), behind the hero so his fist covers the grip
  sprites 3-7  five multicolor creatures: wolf, imp, spider, bat, wisp

Sprite streaming: there are more animation frames than fit in the VIC bank next to an 8K
bitmap, so the image carries each source frame once (facing right) and the routine unpacks
them at init, mirrored where the timeline needs a left-facing copy, into a store of up to 255
frames at $9000. Each hardware sprite owns two slots in the bank; when its frame changes, the
new frame is copied into the slot that isn't on screen.

The choreography (director.py) gives every sprite's position, frame, color, visibility and
priority on every frame, plus a screen shake. Here those become channels: one value per frame
(positions as per-frame deltas), each coded as a byte stream of runs and back-references that
the routine decodes one frame at a time (see "stream encoding"). Ambient sparkles from the
painting are more channels: bytes poked into screen or color RAM.

Inputs: hero.txt and baddies.txt (sprite sheets, see art.py), forest.py (the bitmap),
director.py (the timeline). Writes into the build dir: bank.bin, colorram.bin, gfx.asm,
timeline.asm (tables and streams for the code segment), and previews: sprites.png (every
source frame), paint.png and encoded.png (the forest before and after encoding).
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
from harness.gfx import COLODORE, Bank, d018, sprite_hires, sprite_mc, write  # noqa: E402

import art  # noqa: E402
import director  # noqa: E402
import forest  # noqa: E402

SCREEN, BITMAP = 0x5C00, 0x6000
SLOTS = 0x4000                     # 8 hardware sprites x 2 slots
SOURCES = 0x4400                   # source frames, then timeline streams, up to the screen
STORE_MAX = 255                    # $FF marks "no frame yet" in the routine
XS_BASE, YS_BASE = 4, 3            # base xscroll / yscroll (38 columns, 24 rows)
D025, D026 = 10, 7                 # shared sprite multicolors: light red, yellow
MODE_COPY, MODE_HIRES, MODE_MC = 0, 1, 2
ACTORS = 7                         # hero, sword, five creatures
HW_ACTOR = [0, 0, 1, 2, 3, 4, 5, 6]

out = Path(sys.argv[1] if len(sys.argv) > 1 else HERE.parents[1] / "build/ambush")
out.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------ sprite frames
class Sprites:
    """Source frames (stored once, facing right) and the store the routine builds from them."""

    def __init__(self):
        self.sources: list[bytes] = []
        self.kind: dict[str, str] = {}            # name -> 'hires' | 'mc' | 'hero'
        self.src: dict[str, int | tuple] = {}     # name -> source index (hero: (overlay, body))
        self.store: list[tuple[int, int]] = []    # (source index, mode)
        self.index: dict[tuple[str, int], int] = {}

    def add(self, name: str, kind: str, *data: bytes) -> None:
        idx = []
        for d in data:
            idx.append(len(self.sources))
            self.sources.append(bytes(d))
        self.kind[name] = kind
        self.src[name] = tuple(idx) if kind == "hero" else idx[0]

    def frame(self, name: str, facing: int) -> int:
        """Store index of a frame, allocated on first use (the hero's body follows his overlay)."""
        key = (name, facing)
        if key not in self.index:
            kind = self.kind[name]
            mirror = facing < 0
            self.index[key] = len(self.store)
            if kind == "hero":
                over, body = self.src[name]
                self.store.append((over, MODE_HIRES if mirror else MODE_COPY))
                self.store.append((body, MODE_MC if mirror else MODE_COPY))
            else:
                mode = (MODE_HIRES if kind == "hires" else MODE_MC) if mirror else MODE_COPY
                self.store.append((self.src[name], mode))
            if len(self.store) > STORE_MAX:
                raise ValueError(f"store full ({len(self.store)} frames)")
        return self.index[key]


sprites = Sprites()
poses, preview = {}, []
for name, f in art.parse(HERE / "hero.txt").items():
    over, body, errors = art.hero_layers(f)
    if errors:
        raise SystemExit("hero.txt: multicolor pairs disagree:\n  " + "\n  ".join(errors))
    sprites.add(name, "hero", sprite_hires(over), sprite_mc(body))
    poses[name] = f.attrs
    preview.append(art.compose(np.where(over, 0, -1), art.mc_to_hires(body, (D025, director.HERO_TUNIC, D026))))

# The sword: one frame per blade angle the poses use, plus the swing smears and impacts.
sword_anchor = {}
fx_frames = [(f"sword{a}", art.sword_bits(a)) for a in sorted({p["sword"] for p in poses.values()})]
fx_frames += [(name, art.smear_bits(a, b) if kind == "smear" else art.impact_bits(a, b))
              for name, (kind, a, b) in director.SWORD_FX.items()]
for name, (bits, anchor) in fx_frames:
    sprites.add(name, "hires", sprite_hires(bits))
    sword_anchor[name] = anchor
    preview.append(np.where(bits, 1, -1))

baddies = art.parse(HERE / "baddies.txt")
for name in director.BADDIE_FRAMES:
    codes = art.mc_codes(baddies[name])
    sprites.add(name, "mc", sprite_mc(codes))
    own = director.WHITE if name.startswith(("wisp", "poof")) else director.BLACK
    preview.append(art.mc_to_hires(codes, (D025, own, D026)))

# ------------------------------------------------------------------ the timeline
tl = director.direct(poses)
N = tl.length
xs = np.zeros((ACTORS, N), int)    # sprite X register (9 bits)
ys = np.zeros((ACTORS, N), int)
fr = np.zeros((ACTORS, N), int)    # store index
col = np.zeros((8, N), int)        # per hardware sprite
hidden = np.zeros((ACTORS, N), bool)
en = np.zeros(N, int)
pr = np.zeros(N, int)
scroll = np.zeros(N, int)


def reg_x(x, shake):
    return int(round(x)) + 24 + XS_BASE + shake


def reg_y(y, shake):
    return int(round(y)) + 50 + (YS_BASE - 3) + shake


for t in range(N):
    sx, sy = tl.shake[t]           # the bitmap shakes by scrolling; sprites move with it
    scroll[t] = (XS_BASE + sx) | (YS_BASE + sy) << 4
    h = tl.hero[t]
    pose = poses[h.frame]
    xs[0, t], ys[0, t] = reg_x(h.x, sx), reg_y(h.y, sy)
    fr[0, t] = sprites.frame(h.frame, h.facing)
    col[0, t], col[1, t] = h.color
    vis = 0b11 if h.visible else 0
    sword = tl.sword[t] if tl.sword[t] is not None else f"sword{pose['sword']}"
    if sword:                      # the grip hangs on the fist (mirrored with the hero)
        ax, ay = sword_anchor[sword]
        fx, fy = pose["fist"]
        if h.facing < 0:
            fx, ax = 23 - fx, 23 - ax
        xs[1, t], ys[1, t] = reg_x(h.x + fx - ax, sx), reg_y(h.y + fy - ay, sy)
        fr[1, t] = sprites.frame(sword, h.facing)
        vis |= 0b100 if h.visible else 0
    else:
        hidden[1, t] = True
    col[2, t] = tl.sword_color[t]
    for k, b in enumerate(tl.baddies[t]):
        if b.visible:
            xs[2 + k, t], ys[2 + k, t] = reg_x(b.x, sx), reg_y(b.y, sy)
            fr[2 + k, t] = sprites.frame(b.frame, b.facing)
            vis |= 1 << (3 + k)
            pr[t] |= (1 << (3 + k)) if b.behind else 0
        else:
            hidden[2 + k, t] = True
        col[3 + k, t] = b.color
    en[t] = vis

# A hidden sprite keeps its last registers, so its streams don't change while it's away; a
# sprite hidden at the start of the loop takes its first visible values.
for a in range(ACTORS):
    shown = np.flatnonzero(~hidden[a])
    for t in np.flatnonzero(hidden[a]) if len(shown) else []:
        src_t = shown[0] if t < shown[0] else t - 1
        xs[a, t], ys[a, t], fr[a, t] = xs[a, src_t], ys[a, src_t], fr[a, src_t]
assert xs.min() >= 0 and xs.max() < 504, "sprite X out of range"
assert ys.min() >= 0 and ys.max() < 256, "sprite Y out of range"

# ------------------------------------------------------------------ the forest
bg = forest.build()
if bg["clash"]["cells"]:
    print(f"forest: {bg['clash']['cells']} cells clash ({bg['clash']['pixels']} pixels)")

# ------------------------------------------------------------------ channels
channels = []                      # (name, value per frame), in the routine's channel order
for a in range(ACTORS):
    channels.append((f"x{a}", np.diff(xs[a], prepend=xs[a, 0])))
for a in range(ACTORS):
    channels.append((f"y{a}", np.diff(ys[a], prepend=ys[a, 0])))
for a in range(ACTORS):
    channels.append((f"f{a}", fr[a]))
for h in range(8):
    channels.append((f"c{h}", col[h]))
channels += [("en", en), ("pr", pr), ("scroll", scroll)]
for name, v in channels:
    if name[0] in "xy" and (np.abs(v) > 127).any():
        raise ValueError(f"channel {name}: a step of more than 127 pixels")

# Ambient sparkles: one channel per cell, a whole byte the IRQ pokes into screen or color RAM.
# Each twinkles through its colors at its own pace and phase, resting between twinkles.
pokes = []
for k, (cell, slot, colors) in enumerate(bg["sparkles"]):
    step, rest, phase = 5 + (k * 3) % 4, 24 + (k * 37) % 41, (k * 53) % 97
    pattern = [c for c in colors for _ in range(step)] + [colors[0]] * rest
    seq = np.array([pattern[(t + phase) % len(pattern)] for t in range(N)])
    if slot == "cram":
        addr = 0xD800 + cell
    else:
        addr, other = SCREEN + cell, bg["screen"][cell]
        seq = seq << 4 | other & 15 if slot == "hi" else other & 0xF0 | seq
    pokes.append(addr)
    channels.append((f"poke{k}", seq))


# ------------------------------------------------------------------ stream encoding
# Each channel is a byte stream of tokens, decoded one frame at a time by the routine:
#   0ccc dddd            short run: ccc+1 frames (1-8) of the signed nybble dddd (-8..7)
#   10cc cccc  vvvvvvvv  long run: cccccc+1 frames (1-64) of the byte v
#   11ll llll  oooooooo  replay llllll+1 earlier tokens that start o bytes back, then continue
def tokens(values):
    toks, runs = [], []
    for v in (int(x) & 0xFF for x in values):
        if runs and runs[-1][1] == v:
            runs[-1][0] += 1
        else:
            runs.append([1, v])
    for count, v in runs:
        sv = v - 256 if v > 127 else v
        if -8 <= sv <= 7 and count <= 16:
            while count:
                k = min(count, 8)
                toks.append(bytes([(k - 1) << 4 | (sv & 15)]))
                count -= k
        else:
            while count:
                k = min(count, 64)
                toks.append(bytes([0x80 | (k - 1), v]))
                count -= k
    return toks


def compress(toks):
    """Greedy back-references over literal tokens: replay up to 64 tokens <= 255 bytes back."""
    out_, pos = bytearray(), []    # pos[i]: byte offset of literal token i, None if replayed
    i = 0
    while i < len(toks):
        best, best_j = 0, 0
        for j in range(i):
            if pos[j] is None or len(out_) - pos[j] > 255:
                continue
            k = 0
            while (k < 64 and i + k < len(toks) and j + k < i and pos[j + k] is not None
                   and toks[j + k] == toks[i + k]):
                k += 1
            if k > best and sum(len(t) for t in toks[i:i + k]) > 2:
                best, best_j = k, j
        if best:
            out_ += bytes([0xC0 | (best - 1), len(out_) - pos[best_j]])
            pos += [None] * best
            i += best
        else:
            pos.append(len(out_))
            out_ += toks[i]
            i += 1
    return bytes(out_)


def decode(data, n):
    """The routine's decoder, in Python: n frames of values from a stream."""
    vals, p, ret, left, cnt, val = [], 0, 0, 0, 0, 0
    for _ in range(n):
        if cnt == 0:
            if left == 0 and data[p] >= 0xC0:
                left, ret = (data[p] & 0x3F) + 1, p + 2
                p -= data[p + 1]
            b = data[p]
            if b < 0x80:
                cnt, val, p = (b >> 4) + 1, (b & 15) | (0xF0 if b & 8 else 0), p + 1
            else:
                cnt, val, p = (b & 0x3F) + 1, data[p + 1], p + 2
            if left:
                left -= 1
                if left == 0:
                    p = ret
        cnt -= 1
        vals.append(val)
    return vals


streams = [compress(tokens(v)) for _, v in channels]
for (name, v), data in zip(channels, streams):
    if decode(data, N) != [int(x) & 0xFF for x in v]:
        raise AssertionError(f"stream {name} does not decode to its values")

# ------------------------------------------------------------------ the bank and the code data
bank = Bank()
bank.put(SCREEN, bg["screen"], "screen RAM")
bank.put(BITMAP, bg["bitmap"], "multicolor bitmap")
src_blob = b"".join(sprites.sources)
bank.put(SOURCES, src_blob, f"{len(sprites.sources)} source frames")

# Streams fill the bank's gap after the source frames; the rest go into the code segment.
stream_at = SOURCES + len(src_blob)
addr, bank_blob, code_blob = [], b"", b""
for d in streams:
    if stream_at + len(bank_blob) + len(d) <= SCREEN:
        addr.append(f"${stream_at + len(bank_blob):04x}")
        bank_blob += d
    else:
        addr.append(f"tl_streams + {len(code_blob)}")
        code_blob += d
if bank_blob:
    bank.put(stream_at, bank_blob, "timeline streams")


def byte_rows(label, data, per=16):
    rows = [f"{label}:"]
    data = [int(v) & 0xFF for v in data] or [0]
    for i in range(0, len(data), per):
        rows.append("    .byte " + ", ".join(f"${v:02x}" for v in data[i:i + per]))
    return rows


lines = ["// generated by gfx.py - do not edit"]
lines += ["tl_start_lo:", "    .byte " + ", ".join(f"<({a})" for a in addr)]
lines += ["tl_start_hi:", "    .byte " + ", ".join(f">({a})" for a in addr)]
lines += byte_rows("tl_x0lo", xs[:, 0] & 0xFF)
lines += byte_rows("tl_x0hi", xs[:, 0] >> 8)
lines += byte_rows("tl_y0", ys[:, 0])
lines += byte_rows("poke_lo", [a & 0xFF for a in pokes])
lines += byte_rows("poke_hi", [a >> 8 for a in pokes])
lines += byte_rows("store_src", [s for s, _ in sprites.store])
lines += byte_rows("store_mode", [m for _, m in sprites.store])
lines += byte_rows("tl_streams", code_blob)
(out / "timeline.asm").write_text("\n".join(lines) + "\n")

write(out, bank, bg["colorram"], {
    "GFX_SCREEN": SCREEN,
    "GFX_D018": d018(SCREEN, bitmap=BITMAP),
    "GFX_BG": bg["bg"],
    "GFX_BORDER": 0,
    "GFX_SOURCES": SOURCES,
    "GFX_SLOTS": SLOTS,
    "GFX_D025": D025,
    "GFX_D026": D026,
    "GFX_D016": 0x10 | XS_BASE,
    "GFX_D011": 0x30 | YS_BASE,
    "TL_LOOP": N,
    "TL_NCH": len(channels),
    "TL_ACTORS": ACTORS,
    "TL_STORE": len(sprites.store),
    "TL_CH_X": 0,
    "TL_CH_Y": ACTORS,
    "TL_CH_F": 2 * ACTORS,
    "TL_CH_COL": 3 * ACTORS,
    "TL_CH_EN": 3 * ACTORS + 8,
    "TL_CH_PR": 3 * ACTORS + 9,
    "TL_CH_SCROLL": 3 * ACTORS + 10,
    "TL_CH_POKE": 3 * ACTORS + 11,
    "TL_POKES": len(pokes),
})

# ------------------------------------------------------------------ previews
forest.save_png(bg["paint"], out / "paint.png")
forest.save_png(bg["pixels"], out / "encoded.png")
cols, scale = 12, 3
sheet = np.full((-(-len(preview) // cols) * 23, cols * 26), 11)
for i, img in enumerate(preview):
    y, x = (i // cols) * 23 + 1, (i % cols) * 26 + 1
    sheet[y:y + 21, x:x + 24] = np.where(img >= 0, img, 12)
im = Image.fromarray(sheet.astype(np.uint8), "P").resize((sheet.shape[1] * scale, sheet.shape[0] * scale), Image.NEAREST)
im.putpalette([v for rgb in COLODORE for v in rgb])
im.save(out / "sprites.png")

stream_bytes = sum(len(d) for d in streams)
print(f"timeline: {N} frames, {len(channels)} channels, {stream_bytes} bytes of streams "
      f"({len(bank_blob)} in the bank); {len(sprites.sources)} source frames, store {len(sprites.store)} frames")
