"""Boot VICE, inject a C64H image, wait for the routine's READY signal, capture complete frames.

Timeline (emulated time is fully deterministic; wall-clock never decides anything):
  1. power-cycle the C64 and stop in the KERNAL's READY key-wait loop ($E5CD)
  2. write the whole image into RAM at $3B00, set PC = entry with interrupts masked
  3. run frame by frame (VICE stops at every vsync on request) until HX_STATUS != 0
  4. capture: every step is exactly one frame; frame 0 is the first frame that started
     after the routine signalled READY
`probe` shares steps 1-3, then stops at code addresses to report raster line/cycle.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import bmon
from .image import HX_FRAME, HX_LOAD, HX_ST_ERROR, HX_ST_READY, HX_STATUS, Image, label_for, load_symbols
from .vice import Vice, find_x64sc

KERNAL_READY_LOOP = 0xE5CD


@dataclass(frozen=True)
class Geometry:
    """Crop of VICE's raw raster buffer that matches what VICE shows on screen."""

    x: int
    y: int
    width: int
    height: int
    window_x: int    # top-left pixel of the 320x200 display window, in cropped coordinates
    window_y: int
    first_line: int  # raster line shown in the first cropped row (NTSC wraps past line 262)
    stop_line: int   # raster line where VICE's vsync (and therefore every capture stop) lands


# Measured against VICE 3.10 x64sc with a calibration image (border/background/window bboxes).
# PAL debug borders are broken in VICE 3.10 (the buffer comes back empty).
GEOMETRY = {
    ("pal", "normal"): Geometry(104, 15, 384, 272, 32, 35, 16, 0),
    ("pal", "full"): Geometry(92, 5, 408, 293, 48, 43, 8, 0),
    ("pal", "none"): Geometry(136, 51, 320, 200, 0, 0, 51, 0),
    ("ntsc", "normal"): Geometry(104, 8, 384, 247, 32, 23, 28, 12),
    ("ntsc", "full"): Geometry(84, 5, 424, 253, 56, 29, 22, 12),
    ("ntsc", "debug"): Geometry(0, 0, 520, 263, 136, 31, 20, 20),
    ("ntsc", "none"): Geometry(136, 31, 320, 200, 0, 0, 51, 0),
}


@dataclass
class Frame:
    index: int          # 0 = first full frame after READY
    pixels: bytes       # cropped, one C64 color index (0-15) per pixel
    width: int
    height: int
    hx_status: int
    hx_frame: int
    line: int           # raster position where VICE stopped (frame boundary)
    cycle: int


@dataclass
class CaptureResult:
    image: Image
    video: str
    borders: str
    crop: str
    geometry: Geometry
    vice_version: str = ""
    frames: list[Frame] = field(default_factory=list)
    ready_after_frames: int | None = None
    error: dict | None = None            # set when the run failed (timeout, JAM, routine error)
    diagnostic: Frame | None = None      # frame grabbed at the moment of failure
    warnings: list[str] = field(default_factory=list)
    timing: dict = field(default_factory=dict)


class ProbeError(Exception):
    pass


def _video(image: Image, ntsc: bool | None) -> str:
    return "ntsc" if (image.ntsc if ntsc is None else ntsc) else "pal"


@contextmanager
def _booted(image: Image, video: str, borders: str, warp: bool, x64sc: str | None, log_path: Path | None):
    """A running VICE, power-cycled to the READY prompt, with the image injected and PC at its entry."""
    with Vice(find_x64sc(x64sc), ntsc=video == "ntsc", borders=borders, warp=warp, log_path=log_path) as vice:
        mon = vice.mon
        assert mon is not None
        mon.ping()
        cp = mon.checkpoint_set(KERNAL_READY_LOOP)
        mon.reset(1)
        mon.wait_event({bmon.EV_STOPPED}, timeout=30)
        mon.drain_events()
        mon.checkpoint_delete(cp)
        mon.mem_set(HX_LOAD, image.data, bank="ram")
        regs = mon.registers()
        mon.set_registers(PC=image.entry, FL=regs["FL"] | 0x04)
        yield mon


@dataclass
class _Ready:
    kind: str                 # "ready", "jam", "routine_error" or "ready_timeout"
    frames: int
    stop: bmon.Stop
    mem: bytes | None
    warnings: list[str]


def _wait_ready(mon: bmon.BinaryMonitor, timeout: int) -> _Ready:
    """Run frame by frame until the routine writes HX_STATUS (or JAMs, or the timeout passes)."""
    warnings: list[str] = []
    for n in range(1, timeout + 1):
        stop, _, mem = mon.step_frame(mem=(HX_STATUS, HX_FRAME))
        assert mem is not None
        if stop.jam:
            return _Ready("jam", n, stop, mem, warnings)
        if mem[0] & HX_ST_ERROR:
            return _Ready("routine_error", n, stop, mem, warnings)
        if mem[0] == HX_ST_READY:
            return _Ready("ready", n, stop, mem, warnings)
        if mem[0] != 0:
            warnings.append(f"unexpected HX_STATUS value ${mem[0]:02X} (expected 0, 1 or $80+n)")
    stop = bmon.Stop(registers=mon.registers())
    return _Ready("ready_timeout", timeout, stop, None, warnings)


def capture(image: Image, *, frames: int = 4, skip: int = 0, every: int = 1, ntsc: bool | None = None,
            borders: str = "normal", crop: str = "visible", ready_timeout: int = 500, warp: bool = True,
            x64sc: str | None = None, log_path: Path | None = None, symbols: Path | None = None) -> CaptureResult:
    if frames < 1 or skip < 0 or every < 1:
        raise ValueError("need frames >= 1, skip >= 0, every >= 1")
    video = _video(image, ntsc)
    if (video, borders) not in GEOMETRY:
        raise ValueError(f"border mode {borders!r} is not supported for {video.upper()} "
                         "(VICE 3.10 returns an empty buffer for PAL debug borders)")
    geom = GEOMETRY[(video, borders)]
    if crop == "window":
        geom = Geometry(geom.x + geom.window_x, geom.y + geom.window_y, 320, 200, 0, 0, 51, geom.stop_line)
    result = CaptureResult(image=image, video=video, borders=borders, crop=crop, geometry=geom)
    labels = load_symbols(symbols) if symbols and Path(symbols).is_file() else []

    def where(pc: int) -> str:
        name = label_for(pc, labels)
        return f"${pc:04X}" + (f" ({name})" if name else "")

    t0 = time.monotonic()
    with _booted(image, video, borders, warp, x64sc, log_path) as mon:
        result.vice_version = mon.vice_version()
        t_boot = time.monotonic()

        def grab(index: int, stop: bmon.Stop, disp: bmon.Display, mem: bytes) -> Frame:
            pixels = _crop(disp, geom)
            return Frame(index, pixels, geom.width, geom.height, mem[0], mem[1],
                         stop.registers.get("LIN", -1), stop.registers.get("CYC", -1))

        def fail(kind: str, message: str, stop: bmon.Stop) -> CaptureResult:
            s, d, m = mon.step_frame(display=True, mem=(HX_STATUS, HX_FRAME)) if not stop.jam else (stop, None, None)
            if d is None:  # machine is jammed: grab what is on screen without running further
                d = bmon.BinaryMonitor.decode_display(mon.call(bmon.CMD_DISPLAY_GET, bytes([1, 0])).body)
                m = mon.mem_get(HX_STATUS, HX_FRAME, bank="ram")
            result.diagnostic = grab(-1, s, d, m)
            pc = stop.registers.get("PC", s.registers.get("PC", 0))
            result.error = {"kind": kind, "message": message, "pc": f"${pc:04X}", "where": where(pc),
                            "status": m[0] if m else None, "registers": stop.registers or s.registers}
            result.timing = {"total_s": round(time.monotonic() - t0, 3)}
            return result

        ready = _wait_ready(mon, ready_timeout)
        result.warnings += ready.warnings
        pc = ready.stop.registers.get("PC", 0)
        if ready.kind == "jam":
            return fail("jam", f"CPU JAM at {where(pc)} before READY", ready.stop)
        if ready.kind == "routine_error":
            return fail("routine_error", f"routine signalled error {ready.mem[0] & 0x7F}", ready.stop)
        if ready.kind == "ready_timeout":
            return fail("ready_timeout", f"no READY after {ready_timeout} frames; CPU at {where(pc)}", ready.stop)
        result.ready_after_frames = ready.frames
        t_ready = time.monotonic()

        # 4. capture
        total = skip + (frames - 1) * every + 1
        for k in range(total):
            wanted = k >= skip and (k - skip) % every == 0
            stop, disp, mem = mon.step_frame(display=wanted, mem=(HX_STATUS, HX_FRAME))
            assert mem is not None
            if stop.jam:
                return fail("jam", f"CPU JAM at {where(stop.registers.get('PC', 0))} in frame {k}", stop)
            if mem[0] & HX_ST_ERROR:
                return fail("routine_error", f"routine signalled error {mem[0] & 0x7F} in frame {k}", stop)
            if stop.checkpoints:
                result.warnings.append(f"frame {k}: unexpected checkpoint stop {stop.checkpoints}")
            if wanted:
                assert disp is not None
                result.frames.append(grab(k, stop, disp, mem))
        t_done = time.monotonic()

    result.timing = {
        "startup_s": round(t_boot - t0, 3),
        "to_ready_s": round(t_ready - t_boot, 3),
        "capture_s": round(t_done - t_ready, 3),
        "total_s": round(time.monotonic() - t0, 3),
    }
    off = sorted({f.line for f in result.frames} - {geom.stop_line})
    if off:
        result.warnings.append(f"frames stopped at raster lines {off}, expected line {geom.stop_line} (torn frames?)")
    return result


def probe(image: Image, addresses: dict[str, int], *, hits: int = 32, ntsc: bool | None = None,
          ready_timeout: int = 500, warp: bool = True, x64sc: str | None = None,
          log_path: Path | None = None) -> dict[str, list[dict[str, int]]]:
    """Run until READY, then stop at each address `hits` times and record the CPU registers,
    including LIN (raster line) and CYC (0-based cycle in the line) at the instruction's start."""
    if not addresses:
        raise ValueError("nothing to probe")
    with _booted(image, _video(image, ntsc), "normal", warp, x64sc, log_path) as mon:
        ready = _wait_ready(mon, ready_timeout)
        if ready.kind != "ready":
            raise ProbeError(f"{ready.kind.replace('_', ' ')} before probing "
                             f"(PC ${ready.stop.registers.get('PC', 0):04X})")
        names = {mon.checkpoint_set(addr): name for name, addr in addresses.items()}
        results: dict[str, list[dict[str, int]]] = {name: [] for name in addresses}
        for _ in range(hits * len(addresses) * 4 + 64):
            mon.resume()
            mon.wait_event({bmon.EV_STOPPED}, timeout=10)
            stop = mon.summarize(mon.drain_events())
            if stop.jam:
                raise ProbeError(f"CPU JAM at ${stop.registers.get('PC', 0):04X} while probing")
            for cp in stop.checkpoints:
                if cp in names and len(results[names[cp]]) < hits:
                    results[names[cp]].append(dict(stop.registers))
            if all(len(v) >= hits for v in results.values()):
                break
        for cp in names:
            mon.checkpoint_delete(cp)
    return results


def _crop(disp: bmon.Display, g: Geometry) -> bytes:
    rows = [disp.pixels[(g.y + r) * disp.width + g.x : (g.y + r) * disp.width + g.x + g.width]
            for r in range(g.height)]
    return b"".join(rows)
