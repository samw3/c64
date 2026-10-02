"""CLI: python3 -m harness {capture,inspect} ..."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .crt import CrtModel
from .image import Image, ImageError, load_symbols

CRT_DEFAULTS = CrtModel()

EXIT_OK, EXIT_ERROR, EXIT_TIMEOUT, EXIT_JAM, EXIT_ROUTINE = 0, 1, 2, 3, 4
EXIT_CODES = {"ready_timeout": EXIT_TIMEOUT, "jam": EXIT_JAM, "routine_error": EXIT_ROUTINE}


def _symbols_for(image_path: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    found = sorted(image_path.parent.glob("*.vs"))
    return found[0] if len(found) == 1 else None


def cmd_capture(args: argparse.Namespace) -> int:
    from . import output
    from .capture import capture

    image = Image.load(args.image)
    out = Path(args.out) if args.out else Path("out") / image.path.parent.name
    ntsc = True if args.ntsc else False if args.pal else None
    result = capture(
        image, frames=args.frames, skip=args.skip, every=args.every, ntsc=ntsc, borders=args.borders,
        crop=args.crop, ready_timeout=args.ready_timeout, warp=not args.no_warp, x64sc=args.x64sc,
        log_path=out / "vice.log", symbols=_symbols_for(image.path, args.symbols),
    )
    report = output.write(result, out, palette_spec=args.palette, zoom=args.zoom,
                          crt=CrtModel() if args.crt else None)
    print(json.dumps(report, indent=2) if args.json else output.summary(report, out))
    return EXIT_CODES.get(result.error["kind"], EXIT_ERROR) if result.error else EXIT_OK


def cmd_crt(args: argparse.Namespace) -> int:
    import numpy as np
    from PIL import Image as PILImage

    from .crt import render
    from .palette import load_palette

    video = "ntsc" if args.ntsc else "pal" if args.pal else None
    palette_spec = args.palette
    jobs = []
    for spec in args.inputs:
        path = Path(spec)
        if path.is_dir():                        # a capture directory (out/<scene>)
            report = path / "report.json"
            if report.is_file():
                emulator = json.loads(report.read_text())["emulator"]
                video = video or emulator["video"].lower()
                palette_spec = palette_spec or emulator["palette"].split()[0]
            frames = path / "frames" if (path / "frames").is_dir() else path
            dest = Path(args.out) if args.out else path / "crt"
            jobs += [(f, dest / f.name) for f in sorted(frames.glob("*.png"))]
        else:                                    # individual frames/NNN.png files
            base = path.parent.parent if path.parent.name == "frames" else path.parent
            jobs.append((path, (Path(args.out) if args.out else base / "crt") / path.name))
    if not jobs:
        print("error: no frames found", file=sys.stderr)
        return EXIT_ERROR
    model = CrtModel(luma_blur=args.luma_blur, chroma_blur=args.chroma_blur, scale=args.scale,
                     scanlines=not args.no_scanlines, delay_line=False if args.no_delay_line else None)
    _, palette = load_palette(palette_spec or "colodore")
    for src, dst in jobs:
        with PILImage.open(src) as img:
            if img.mode != "P":
                print(f"error: {src} is not an indexed capture frame (use frames/NNN.png)", file=sys.stderr)
                return EXIT_ERROR
            indices = np.array(img)
        dst.parent.mkdir(parents=True, exist_ok=True)
        render(indices, palette, video or "pal", model).save(dst)
    print(f"{len(jobs)} frame(s) -> {jobs[0][1].parent}  ({(video or 'pal').upper()} monitor model)")
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    image = Image.load(args.image)
    info = image.describe()
    if args.json:
        print(json.dumps(info, indent=2))
        return EXIT_OK
    print(f"{image.path}: C64H v{info['version']}, {info['video']}, \"{info['title']}\"")
    print(f"  entry {info['entry']}, code {info['code_bytes']} bytes ({info['code_free']} free of 4096)")
    print(f"  mode: {info['mode']}{'' if info['display_enabled'] else ' (DISPLAY OFF: $D011 bit 4 clear)'}, "
          f"{info['columns']}x{info['rows']}, scroll x={info['scroll']['x']} y={info['scroll']['y']}")
    where = [f"screen {info['screen']}"]
    where += [f"bitmap {info['bitmap']}"] if "bitmap" in info else [f"charset {info['charset']}"]
    print("  " + ", ".join(where))
    print("  colors: " + ", ".join(f"{k} {v}" for k, v in info["colors"].items()))
    print("  registers: " + " ".join(f"{k}={v}" for k, v in info["registers"].items()))
    for s in info["sprites"]:
        flags = [f for f in ("multicolor", "expand_x", "expand_y", "behind_background") if s[f]]
        print(f"  sprite {s['sprite']}: x={s['x']} y={s['y']} pointer {s['pointer']} -> {s['data']} "
              f"color {s['color']} {' '.join(flags)}")
    print("  color RAM: " + ", ".join(f"{k} x{v}" for k, v in info["colorram_usage"].items()))
    return EXIT_OK


def cmd_probe(args: argparse.Namespace) -> int:
    from collections import Counter

    from .capture import ProbeError, probe

    image = Image.load(args.image)
    symbols = _symbols_for(image.path, args.symbols)
    labels = {name: addr for addr, name in load_symbols(symbols)} if symbols else {}
    targets = {}
    for spec in args.at:
        if spec.startswith("$") or spec.lower().startswith("0x"):
            targets[spec] = int(spec.lstrip("$"), 16)
        elif spec in labels:
            targets[spec] = labels[spec]
        else:
            print(f"error: unknown label {spec!r}" + ("" if symbols else " (no .vs symbol file found)"),
                  file=sys.stderr)
            return EXIT_ERROR
    out = Path("out") / image.path.parent.name
    try:
        results = probe(image, targets, hits=args.hits, ntsc=True if args.ntsc else False if args.pal else None,
                        ready_timeout=args.ready_timeout, x64sc=args.x64sc, log_path=out / "vice-probe.log")
    except ProbeError as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps({name: {"address": f"${targets[name]:04X}", "hits": hits} for name, hits in results.items()},
                         indent=2))
        return EXIT_OK
    for name, hits in results.items():
        head = f"{name:>14} ${targets[name]:04X}  {len(hits):3d} hits  "
        if not hits:
            print(head + "never executed after READY")
            continue
        seen = Counter((h["LIN"], h["CYC"]) for h in hits)
        if len(seen) == 1:
            (line, cycle), = seen
            print(head + f"line ${line:03X} cycle {cycle}  (stable)")
            continue
        cycles = sorted({c for _, c in seen})
        lines = sorted({line for line, _ in seen})
        where = f"line ${lines[0]:03X}" if len(lines) == 1 else "lines " + ", ".join(f"${x:03X}" for x in lines)
        print(head + f"{where} cycles {cycles[0]}-{cycles[-1]}  (jitter: {len(seen)} different positions)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m harness", description="C64 scene image harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="run an image in headless VICE and capture frames")
    c.add_argument("image", help="C64H image (.prg loaded at $3B00)")
    c.add_argument("-n", "--frames", type=int, default=4, help="frames to capture (default 4)")
    c.add_argument("--skip", type=int, default=0, help="frames to let pass after READY before capturing")
    c.add_argument("--every", type=int, default=1, help="capture every Nth frame")
    c.add_argument("-o", "--out", help="output directory (default out/<image dir name>)")
    c.add_argument("--borders", choices=["normal", "full", "none", "debug"], default="normal",
                   help="visible area: normal 384x272, full 408x293, none 320x200 (PAL)")
    c.add_argument("--crop", choices=["visible", "window"], default="visible",
                   help="window = only the 320x200 display window")
    std = c.add_mutually_exclusive_group()
    std.add_argument("--pal", action="store_true", help="force PAL (default: image header flag)")
    std.add_argument("--ntsc", action="store_true", help="force NTSC")
    c.add_argument("--palette", default="colodore", help="VICE palette name or .vpl path (default colodore)")
    c.add_argument("--zoom", type=int, default=2, help="scale of zoom/ copies, 1 disables (default 2)")
    c.add_argument("--crt", action="store_true",
                   help="also write crt/NNN.png: the frames as a PAL/NTSC monitor shows them (dithers, PAL mixing)")
    c.add_argument("--ready-timeout", type=int, default=500, help="emulated frames to wait for READY")
    c.add_argument("--no-warp", action="store_true", help="run at real-time speed")
    c.add_argument("--symbols", help="VICE label file for PC diagnostics (default: the .vs next to the image)")
    c.add_argument("--x64sc", help="path to x64sc (default tools/vice/bin/x64sc or $C64H_X64SC)")
    c.add_argument("--json", action="store_true", help="print the full report instead of a summary")
    c.set_defaults(func=cmd_capture)

    i = sub.add_parser("inspect", help="decode an image's header, VIC-II shadow and sprites")
    i.add_argument("image")
    i.add_argument("--json", action="store_true")
    i.set_defaults(func=cmd_inspect)

    t = sub.add_parser("crt", help="render captured frames as a PAL/NTSC monitor shows them")
    t.add_argument("inputs", nargs="+", help="capture directory (out/<scene>) or frames/NNN.png files")
    t.add_argument("-o", "--out", help="output directory (default: <capture>/crt)")
    std = t.add_mutually_exclusive_group()
    std.add_argument("--pal", action="store_true", help="default: from the capture's report.json, else PAL")
    std.add_argument("--ntsc", action="store_true")
    t.add_argument("--palette", help="default: the capture's palette, else colodore")
    t.add_argument("--scale", type=int, default=3, help="output rows per raster line (default 3)")
    t.add_argument("--no-scanlines", action="store_true")
    t.add_argument("--no-delay-line", action="store_true", help="disable PAL line averaging of chroma")
    t.add_argument("--luma-blur", type=float, default=CRT_DEFAULTS.luma_blur, help="luma sigma in pixels (%(default)s)")
    t.add_argument("--chroma-blur", type=float, default=CRT_DEFAULTS.chroma_blur,
                   help="chroma sigma in pixels (%(default)s)")
    t.set_defaults(func=cmd_crt)

    r = sub.add_parser("probe", help="report the raster line/cycle where code runs (stable raster checks)")
    r.add_argument("image")
    r.add_argument("--at", action="append", required=True, metavar="LABEL|$ADDR",
                   help="code label (from the .vs file) or address; repeatable")
    r.add_argument("--hits", type=int, default=50, help="stops to record per address (default 50)")
    std = r.add_mutually_exclusive_group()
    std.add_argument("--pal", action="store_true")
    std.add_argument("--ntsc", action="store_true")
    r.add_argument("--ready-timeout", type=int, default=500)
    r.add_argument("--symbols")
    r.add_argument("--x64sc")
    r.add_argument("--json", action="store_true", help="print every hit's registers")
    r.set_defaults(func=cmd_probe)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ImageError, FileNotFoundError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
