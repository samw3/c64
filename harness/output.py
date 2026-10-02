"""Write a capture to disk: exact PNGs, zoomed views, contact sheets, a GIF and report.json."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image as PILImage
from PIL import ImageDraw

from . import crt as crt_view
from .capture import CaptureResult, Frame
from .image import COLOR_NAMES
from .palette import load_palette

SHEET_COLS, SHEET_ROWS, LABEL_H, GAP = 4, 2, 14, 4


def _pil(frame: Frame, palette: list[tuple[int, int, int]]) -> PILImage.Image:
    img = PILImage.frombytes("P", (frame.width, frame.height), frame.pixels)
    img.putpalette([c for rgb in palette for c in rgb] + [0] * (768 - 48))
    return img


def _stats(frames: list[Frame]) -> list[dict]:
    out, prev = [], None
    for f in frames:
        a = np.frombuffer(f.pixels, dtype=np.uint8).reshape(f.height, f.width)
        hist = np.bincount(a.ravel(), minlength=16)
        entry = {
            "colors": {COLOR_NAMES[c]: int(n) for c, n in sorted(enumerate(hist), key=lambda t: -t[1]) if n},
            "changed_px": None,
            "changed_bbox": None,
        }
        if prev is not None:
            diff = a != prev
            n = int(diff.sum())
            entry["changed_px"] = n
            if n:
                ys, xs = np.nonzero(diff)
                entry["changed_bbox"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        out.append(entry)
        prev = a
    return out


def _period(hashes: list[str]) -> int | None:
    for p in range(1, len(hashes) // 2 + 1):
        if all(hashes[i] == hashes[i - p] for i in range(p, len(hashes))):
            return p
    return None


def write(result: CaptureResult, out: Path, palette_spec: str = "colodore", zoom: int = 2,
          crt: crt_view.CrtModel | None = None) -> dict:
    """Write everything for `result` into `out` (replacing what a previous run left there).
    With `crt`, also write crt/NNN.png: the frames as a PAL/NTSC monitor shows them."""
    for sub in ("frames", "zoom", "crt"):
        shutil.rmtree(out / sub, ignore_errors=True)
    for old in list(out.glob("contact_*.png")) + [out / "anim.gif", out / "diagnostic.png", out / "report.json"]:
        old.unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    palette_name, palette = load_palette(palette_spec)
    g = result.geometry
    files: dict = {}
    pil_frames = [_pil(f, palette) for f in result.frames]
    hashes = [hashlib.sha1(f.pixels).hexdigest() for f in result.frames]

    if pil_frames:
        (out / "frames").mkdir()
        if zoom > 1:
            (out / "zoom").mkdir()
        for f, img in zip(result.frames, pil_frames):
            img.save(out / "frames" / f"{f.index:03d}.png", optimize=True)
            if zoom > 1:
                img.resize((f.width * zoom, f.height * zoom), PILImage.NEAREST).save(
                    out / "zoom" / f"{f.index:03d}.png", optimize=True)
        files["frames"] = f"frames/000.png .. frames/{result.frames[-1].index:03d}.png"
        if zoom > 1:
            files["zoom"] = f"zoom/NNN.png ({zoom}x nearest neighbour)"
        files["contact_sheets"] = _contact_sheets(result.frames, pil_frames, out)
        if crt is not None:
            (out / "crt").mkdir()
            for f in result.frames:
                indices = np.frombuffer(f.pixels, dtype=np.uint8).reshape(f.height, f.width)
                crt_view.render(indices, palette, result.video, crt).save(out / "crt" / f"{f.index:03d}.png")
            files["crt"] = f"crt/NNN.png (CRT view: {result.video.upper()} monitor model, {crt.scale}x)"
        if len(pil_frames) > 1:
            pil_frames[0].save(out / "anim.gif", save_all=True, append_images=pil_frames[1:],
                               duration=20 if result.video == "pal" else 17, loop=0, optimize=False)
            files["animation"] = "anim.gif"
    if result.diagnostic is not None:
        img = _pil(result.diagnostic, palette)
        img.resize((img.width * 2, img.height * 2), PILImage.NEAREST).save(out / "diagnostic.png")
        files["diagnostic"] = "diagnostic.png (2x)"

    stats = _stats(result.frames)
    report = {
        "ok": result.error is None,
        "error": result.error,
        "warnings": result.warnings,
        "image": {
            "path": str(result.image.path),
            "size": len(result.image.data) + 2,
            **result.image.describe(),
        },
        "emulator": {
            "vice": result.vice_version,
            "video": result.video.upper(),
            "borders": result.borders,
            "crop": result.crop,
            "palette": palette_name,
        },
        "geometry": {
            "frame_size": [g.width, g.height],
            "display_window_origin": [g.window_x, g.window_y],
            "first_raster_line": g.first_line,
            "note": "pixel (x, y) shows raster line first_raster_line + y; display-window pixel "
                    "(wx, wy) is at (wx + origin_x, wy + origin_y)",
        },
        "ready_after_frames": result.ready_after_frames,
        "timing": result.timing,
        "frames": [
            {
                "index": f.index,
                "file": f"frames/{f.index:03d}.png",
                "sha1": h,
                "hx_frame": f.hx_frame,
                "stop": {"line": f.line, "cycle": f.cycle},
                **s,
            }
            for f, h, s in zip(result.frames, hashes, stats)
        ],
        "summary": {
            "captured": len(result.frames),
            "unique_frames": len(set(hashes)),
            "loop_period": _period(hashes) if len(hashes) > 1 else None,
            "colors_used": sorted({COLOR_NAMES[c] for f in result.frames for c in set(f.pixels)},
                                  key=COLOR_NAMES.index),
        },
        "files": files,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def _contact_sheets(frames: list[Frame], images: list[PILImage.Image], out: Path) -> list[str]:
    per = SHEET_COLS * SHEET_ROWS
    w, h = frames[0].width, frames[0].height
    names = []
    for s in range(0, len(frames), per):
        chunk = list(zip(frames[s : s + per], images[s : s + per]))
        cols = min(SHEET_COLS, len(chunk))
        rows = (len(chunk) + cols - 1) // cols
        sheet = PILImage.new("RGB", (cols * w + (cols - 1) * GAP, rows * (h + LABEL_H) + (rows - 1) * GAP),
                             (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, (f, img) in enumerate(chunk):
            x, y = (i % cols) * (w + GAP), (i // cols) * (h + LABEL_H + GAP)
            draw.text((x + 2, y + 1), f"frame {f.index}  HX_FRAME={f.hx_frame}", fill=(220, 220, 220))
            sheet.paste(img.convert("RGB"), (x, y + LABEL_H))
        name = f"contact_{s // per}.png"
        sheet.save(out / name, optimize=True)
        names.append(name)
    return names


def summary(report: dict, out: Path) -> str:
    img, emu = report["image"], report["emulator"]
    lines = [f"{img['title'] or Path(img['path']).name}: {img['mode']}, {emu['video']}, "
             f"code {img['code_bytes']}/4096 bytes"]
    if report["error"]:
        e = report["error"]
        lines.append(f"FAILED ({e['kind']}): {e['message']}")
        if report["files"].get("diagnostic"):
            lines.append(f"  diagnostic frame: {out / 'diagnostic.png'}")
    else:
        sm = report["summary"]
        lines.append(f"READY after {report['ready_after_frames']} frame(s); captured {sm['captured']} frame(s), "
                     f"{sm['unique_frames']} unique" + (f", loops every {sm['loop_period']}" if sm["loop_period"] else ""))
        lines.append(f"colors: {', '.join(sm['colors_used'])}")
        lines.append(f"view:   {out / report['files']['contact_sheets'][0]}  (frames/ 1x, zoom/ 2x)")
        if "crt" in report["files"]:
            lines.append(f"crt:    {out / 'crt' / '000.png'}  (as a {report['emulator']['video']} monitor shows it)")
    for w in report["warnings"]:
        lines.append(f"warning: {w}")
    lines.append(f"report: {out / 'report.json'}  ({report['timing'].get('total_s', '?')}s)")
    return "\n".join(lines)
