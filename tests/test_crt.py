"""CRT view (harness/crt.py): flat colors stay exact, chroma blends the way a PAL/NTSC
monitor blends it, and the `crt` command re-renders a capture directory."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image as PILImage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness import crt  # noqa: E402
from harness.__main__ import main as cli_main  # noqa: E402
from harness.palette import load_palette  # noqa: E402

PALETTE = np.array(load_palette("colodore")[1], dtype=np.float64)
LUMA_PAIRS = [(6, 9), (2, 11), (4, 8), (12, 14), (5, 10), (3, 15), (7, 13)]


def stripes(a: int, b: int, h: int = 16, w: int = 32) -> np.ndarray:
    return np.where(np.arange(h)[:, None] % 2 == 0, a, b).repeat(w, axis=1).astype(np.uint8)


def checkerboard(a: int, b: int, h: int = 16, w: int = 32) -> np.ndarray:
    return np.where((np.arange(h)[:, None] + np.arange(w)[None, :]) % 2 == 0, a, b).astype(np.uint8)


class TestCrtModel(unittest.TestCase):
    def test_flat_colors_are_exact_without_scanlines(self):
        model = crt.CrtModel(scanlines=False)
        for c in range(16):
            out = np.asarray(crt.render(np.full((6, 8), c, np.uint8), PALETTE, "pal", model), dtype=np.float64)
            self.assertLessEqual(np.abs(out - PALETTE[c]).max(), 1.0, f"color {c}")

    def test_flat_colors_keep_their_light_with_scanlines(self):
        for c in range(16):
            out = np.asarray(crt.render(np.full((12, 16), c, np.uint8), PALETTE), dtype=np.float64) / 255
            per_line = (out ** 2.2)[6:30].reshape(8, 3, out.shape[1], 3).mean(axis=1)[:, 4:-4]
            self.assertLess(np.abs(per_line - (PALETTE[c] / 255) ** 2.2).max(), 0.01, f"color {c}")

    def test_pal_delay_line_mixes_alternating_lines(self):
        for a, b in LUMA_PAIRS:
            uv = crt.signal(stripes(a, b), PALETTE, "pal")[2:-2, 40:-40, 1:]
            want = (crt.signal(np.full((2, 2), a, np.uint8), PALETTE)[0, 0, 1:]
                    + crt.signal(np.full((2, 2), b, np.uint8), PALETTE)[0, 0, 1:]) / 2
            self.assertLess(np.abs(uv - want).max(), 1e-9, f"PAL mix of {a}/{b}")

    def test_ntsc_keeps_alternating_lines_apart(self):
        a, b = 5, 10
        uv = crt.signal(stripes(a, b), PALETTE, "ntsc")[2:-2, 40:-40, 1:]
        self.assertGreater(np.abs(uv[0::2] - uv[1::2]).min(), 0.05)

    def test_checkerboard_chroma_merges_but_luma_detail_survives(self):
        yuv = crt.signal(checkerboard(4, 8), PALETTE, "ntsc")[4:-4, 40:-40]    # NTSC: horizontal only
        self.assertLess(np.ptp(yuv[..., 1:], axis=1).max(), 0.01, "purple/orange chroma should merge")
        luma = crt.signal(checkerboard(0, 1), PALETTE, "pal")[8, 40:-40, 0]
        self.assertGreater(np.ptp(luma), 0.4, "a black/white hires checkerboard stays visible")

    def test_output_size_follows_pixel_aspect(self):
        idx = np.zeros((272, 384), np.uint8)
        self.assertEqual(crt.render(idx, PALETTE, "pal").size, (1078, 816))
        self.assertEqual(crt.render(np.zeros((247, 384), np.uint8), PALETTE, "ntsc").size, (864, 741))


class TestCrtCommand(unittest.TestCase):
    def test_renders_a_capture_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "frames").mkdir()
            idx = checkerboard(2, 11, 40, 48)
            img = PILImage.frombytes("P", (48, 40), idx.tobytes())
            img.putpalette([int(v) for v in PALETTE.ravel()])
            img.save(out / "frames" / "000.png", optimize=True)          # as output.write saves frames
            with PILImage.open(out / "frames" / "000.png") as saved:
                self.assertTrue((np.array(saved) == idx).all(), "indexed frames must round-trip their color indices")
            (out / "report.json").write_text(json.dumps({"emulator": {"video": "NTSC", "palette": "colodore"}}))
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(cli_main(["crt", str(out)]), 0)
            self.assertIn("NTSC", printed.getvalue())
            with PILImage.open(out / "crt" / "000.png") as rendered:
                self.assertEqual(rendered.size, (round(48 * 0.75 * 3), 120))


if __name__ == "__main__":
    unittest.main()
