"""Stable raster macros (include/stable.asm): both techniques must land on their documented
cycle, on PAL and NTSC, even though the fixture makes every raster IRQ arrive with a
different jitter. Run with `make test`."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from harness.capture import capture, probe  # noqa: E402
from harness.image import Image, load_symbols  # noqa: E402

KICKASS = ROOT / "tools/kickass/KickAss.jar"
X64SC = ROOT / "tools/vice/bin/x64sc"
FIXTURE = ROOT / "tests/fixtures/stable.asm"

CONSTS = {name: int(value) for name, value in
          re.findall(r"\.const\s+(HX_\w+)\s*=\s*(\d+)\b", (ROOT / "include/stable.asm").read_text())}
DBL_LAND, TIMER_LAND = CONSTS["HX_DBL_LAND_CYCLE"], CONSTS["HX_TIMER_LAND_CYCLE"]
A_DELAY = int(re.search(r"\.const A_DELAY = (\d+)", FIXTURE.read_text())[1])


def kickass(source: Path, out: Path, *defines: str) -> subprocess.CompletedProcess:
    args = ["java", "-jar", str(KICKASS), str(source), "-odir", str(out), "-libdir", str(ROOT / "include"),
            "-vicesymbols"]
    for d in defines:
        args += ["-define", d]
    return subprocess.run(args, capture_output=True, text=True)


def column(cycle: int) -> int:
    """Frame column (normal borders) where a color register write on `cycle` takes effect."""
    return 8 * cycle - 95


class StableRaster:
    NTSC = False

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        out = Path(cls.tmp.name)
        proc = kickass(FIXTURE, out, *(["NTSC"] if cls.NTSC else []))
        if proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        image = Image.load(out / "image.prg")
        syms = {name: addr for addr, name in load_symbols(out / "stable.vs")}
        cls.hits = probe(image, {k: syms[k] for k in ("irq_a", "a_stable", "irq_b", "b_stable")},
                         hits=64, log_path=out / "probe.log")
        cls.result = capture(image, frames=24, log_path=out / "capture.log")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def positions(self, label: str) -> set[tuple[int, int]]:
        return {(h["LIN"], h["CYC"]) for h in self.hits[label]}

    def test_fixture_really_jitters(self):
        for entry in ("irq_a", "irq_b"):
            self.assertEqual(len(self.hits[entry]), 64)
            self.assertGreaterEqual(len({h["CYC"] for h in self.hits[entry]}), 6,
                                    f"{entry} should arrive with many different jitters: {self.positions(entry)}")

    def test_double_irq_lands_on_line_plus_2_cycle_3(self):
        self.assertEqual(self.positions("a_stable"), {(0x52, DBL_LAND)})

    def test_timer_irq_lands_on_line_cycle_47(self):
        self.assertEqual(self.positions("b_stable"), {(0x90, TIMER_LAND)})

    def test_splits_are_pixel_exact_every_frame(self):
        self.assertIsNone(self.result.error)
        g = self.result.geometry
        red_x = column(DBL_LAND + A_DELAY + 5)      # HX_Delay, lda #, sta (write on its 4th cycle)
        green_x = column(TIMER_LAND + 5)
        for f in self.result.frames:
            a = np.frombuffer(f.pixels, dtype=np.uint8).reshape(f.height, f.width)
            for line, color, x in ((0x52, 2, red_x), (0x90, 5, green_x)):
                row, before, after = (a[line + d - g.first_line] for d in (0, -1, 1))
                self.assertEqual(int(np.argmax(row == color)), x, f"frame {f.index}: split on line ${line:02X}")
                self.assertFalse((row[:x] == color).any())
                self.assertTrue((row[x:] == color).all())
                self.assertFalse((before == color).any(), "split started a line early")
                self.assertTrue((after == color).all())


@unittest.skipUnless(KICKASS.is_file() and X64SC.is_file(), "run `make setup` first")
class TestStablePAL(StableRaster, unittest.TestCase):
    NTSC = False


@unittest.skipUnless(KICKASS.is_file() and X64SC.is_file(), "run `make setup` first")
class TestStableNTSC(StableRaster, unittest.TestCase):
    NTSC = True


@unittest.skipUnless(KICKASS.is_file(), "run `make setup` first")
class TestStableAssembly(unittest.TestCase):
    def test_page_crossing_is_an_assembly_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "cross.asm"
            src.write_text('#import "harness.asm"\nHX_Header(start, "x", 0)\nHX_VicShadow(Hashtable())\n'
                           ".segment Code\nstart: jmp *\n.align $100\n.fill $100 - 14, $ea\n"
                           "irq: HX_StableTimerIrq()\n")
            proc = kickass(src, Path(tmp))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("crosses a page boundary", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
