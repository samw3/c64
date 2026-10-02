"""Runs last (module order is alphabetical): no emulator may outlive the tests."""

import subprocess
import unittest
from pathlib import Path

X64SC = Path(__file__).resolve().parent.parent / "tools/vice/bin/x64sc"


@unittest.skipUnless(X64SC.is_file(), "run `make setup` first")
class TestNoStrayEmulators(unittest.TestCase):
    def test_no_x64sc_left_running(self):
        proc = subprocess.run(["pgrep", "-f", str(X64SC)], capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), "", "x64sc processes left behind")


if __name__ == "__main__":
    unittest.main()
