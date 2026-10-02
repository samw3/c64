"""End-to-end tests: build scenes, run them in headless VICE, check the captured pixels.

Run with `make test` (needs `make setup` first).
"""

from __future__ import annotations

import contextlib
import io
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import gfx, image  # noqa: E402
from harness.__main__ import main as cli_main  # noqa: E402
from harness.capture import capture  # noqa: E402
from harness.image import Image, ImageError  # noqa: E402
from harness.palette import LUMA_ORDER  # noqa: E402

KICKASS = ROOT / "tools/kickass/KickAss.jar"
X64SC = ROOT / "tools/vice/bin/x64sc"

# testcard layout (scenes/testcard/gfx.py + main.asm), in cropped PAL "normal" frame coordinates
WIN_X, WIN_Y = 32, 35
BORDER_BASE = 11
SPRITE_X0 = 40


def assemble(source: Path, out_dir: Path) -> Path:
    proc = subprocess.run(
        ["java", "-jar", str(KICKASS), str(source), "-odir", str(out_dir), "-libdir", str(ROOT / "include"),
         "-vicesymbols"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"KickAssembler failed for {source}:\n{proc.stdout}\n{proc.stderr}")
    return out_dir / "image.prg"


def frame_array(frame) -> np.ndarray:
    return np.frombuffer(frame.pixels, dtype=np.uint8).reshape(frame.height, frame.width)


@unittest.skipUnless(KICKASS.is_file() and X64SC.is_file(), "run `make setup` first")
class TestTestcard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(["make", "-s", "image", "SCENE=testcard"], cwd=ROOT, check=True, capture_output=True)
        cls.image = Image.load(ROOT / "build/testcard/image.prg")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.result = capture(cls.image, frames=12, log_path=Path(cls.tmp.name) / "vice.log",
                             symbols=ROOT / "build/testcard/main.vs")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_ready_and_frame_boundaries(self):
        r = self.result
        self.assertIsNone(r.error)
        self.assertEqual(r.warnings, [])
        self.assertLessEqual(r.ready_after_frames, 3)
        self.assertEqual(len(r.frames), 12)
        self.assertEqual({f.line for f in r.frames}, {0}, "every capture must stop at raster line 0 (PAL vsync)")
        self.assertEqual({(f.width, f.height) for f in r.frames}, {(384, 272)})

    def test_exactly_one_frame_per_capture(self):
        counts = [f.hx_frame for f in self.result.frames]
        self.assertEqual([b - a for a, b in zip(counts, counts[1:])], [1] * 11, counts)

    def test_swatches_color_ram_and_screen_ram(self):
        a = frame_array(self.result.frames[0])
        for band_row, source in ((2, "color RAM"), (6, "screen hi nybble"), (10, "screen lo nybble")):
            for k in range(16):
                x = WIN_X + (4 + 2 * k) * 8 + 8
                y = WIN_Y + band_row * 8 + 12
                self.assertEqual(a[y, x], k, f"{source} swatch {k} at ({x},{y})")

    def test_display_window_edges(self):
        a = frame_array(self.result.frames[0])
        right, bottom = WIN_X + 319, WIN_Y + 199
        self.assertEqual(a[WIN_Y, WIN_X], 1, "top-left window pixel is the white bar")
        self.assertEqual(a[WIN_Y, right], 1)
        self.assertEqual(a[bottom, WIN_X], 15, "bottom bar is light grey")
        for x, y in ((WIN_X - 1, WIN_Y), (WIN_X, WIN_Y - 1), (right + 1, WIN_Y), (WIN_X, bottom + 1)):
            self.assertEqual(a[y, x], BORDER_BASE, f"border pixel at ({x},{y})")

    def test_raster_bands_follow_frame_counter(self):
        for f in self.result.frames:
            a = frame_array(f)
            g = f.hx_frame - 1      # HX_FRAME value while this frame was drawn
            self.assertTrue((a[2:16, :] == LUMA_ORDER[g & 15]).all(), f"top band, frame {f.index}")
            self.assertTrue((a[240:272, :] == LUMA_ORDER[(g + 8) & 15]).all(), f"bottom band, frame {f.index}")
            self.assertEqual(a[100, 5], BORDER_BASE)

    def test_sprite_moves_one_pixel_per_frame(self):
        for f in self.result.frames:
            lane = frame_array(f)[WIN_Y + 104 : WIN_Y + 128]
            ys, xs = np.nonzero(lane == 7)
            expected_x = SPRITE_X0 + f.hx_frame - 1
            self.assertEqual(xs.min(), WIN_X + (expected_x - 24) + 2, f"sprite left edge, frame {f.index}")
            self.assertEqual(ys.max() - ys.min() + 1, 21)

    def test_deterministic(self):
        again = capture(self.image, frames=12, log_path=Path(self.tmp.name) / "vice2.log")
        self.assertEqual([f.pixels for f in again.frames], [f.pixels for f in self.result.frames])
        self.assertEqual([f.hx_frame for f in again.frames], [f.hx_frame for f in self.result.frames])

    def test_skip_and_every(self):
        r = capture(self.image, frames=3, skip=5, every=4, log_path=Path(self.tmp.name) / "vice3.log")
        self.assertEqual([f.index for f in r.frames], [5, 9, 13])
        first = self.result.frames[0].hx_frame
        self.assertEqual([f.hx_frame for f in r.frames], [first + 5, first + 9, first + 13])


@unittest.skipUnless(KICKASS.is_file() and X64SC.is_file(), "run `make setup` first")
class TestFailures(unittest.TestCase):
    def run_fixture(self, name: str, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            prg = assemble(ROOT / "tests/fixtures" / f"{name}.asm", Path(tmp))
            return capture(Image.load(prg), frames=4, log_path=Path(tmp) / "vice.log",
                           symbols=Path(tmp) / f"{name}.vs", **kw)

    def test_ready_timeout_reports_spin_loop(self):
        r = self.run_fixture("noready", ready_timeout=30)
        self.assertEqual(r.error["kind"], "ready_timeout")
        self.assertIn("(spin)", r.error["where"])
        self.assertIsNotNone(r.diagnostic)

    def test_jam_is_reported_with_label(self):
        r = self.run_fixture("jam")
        self.assertEqual(r.error["kind"], "jam")
        self.assertIn("(crash)", r.error["where"])

    def test_routine_error_code(self):
        r = self.run_fixture("fail")
        self.assertEqual(r.error["kind"], "routine_error")
        self.assertEqual(r.error["status"], 0x85)

    def test_cli_exit_code_and_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            prg = assemble(ROOT / "tests/fixtures/jam.asm", Path(tmp))
            out = Path(tmp) / "out"
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli_main(["capture", str(prg), "-o", str(out)]), 3)
            self.assertIn("FAILED (jam)", printed.getvalue())
            self.assertTrue((out / "report.json").is_file())
            self.assertTrue((out / "diagnostic.png").is_file())


class TestFormat(unittest.TestCase):
    def test_python_and_asm_constants_match(self):
        asm = (ROOT / "include/harness.asm").read_text()
        consts = {m[1]: int(m[2], 16) for m in re.finditer(r"\.const\s+(HX_\w+)\s*=\s*\$([0-9a-fA-F]+)", asm)}
        self.assertGreater(len(consts), 10)
        for name, value in consts.items():
            self.assertEqual(getattr(image, name), value, name)

    def test_rejects_wrong_load_address_and_missing_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.prg"
            bad.write_bytes(bytes([0x01, 0x08]) + b"\0" * 100)
            with self.assertRaises(ImageError):
                Image.load(bad)
            bad.write_bytes(bytes([0x00, 0x3B]) + b"C64H\x01" + b"\0" * 1000)
            with self.assertRaises(ImageError):
                Image.load(bad)

    def test_gfx_helpers(self):
        self.assertEqual(gfx.d018(0x5C00, bitmap=0x6000), 0x78)
        self.assertEqual(gfx.d018(0x4400, charset=0x4800), 0x12)
        self.assertEqual(gfx.sprite_pointer(0x5000), 0x40)
        codes = np.zeros((200, 160), dtype=np.uint8)
        codes[0, :4] = [3, 2, 1, 0]
        codes[1, 4] = 3      # row 1 of cell (1, 0)
        packed = gfx.pack_mc_bitmap(codes)
        self.assertEqual(packed[0], 0b11100100)
        self.assertEqual(packed[8 + 1], 0b11000000)
        bank = gfx.Bank()
        bank.put(0x4000, b"\1" * 64, "a")
        with self.assertRaises(ValueError):
            bank.put(0x403F, b"\1", "b")
        self.assertEqual(len(gfx.sprite_hires(["#" * 24] * 21)), 64)


if __name__ == "__main__":
    unittest.main()
