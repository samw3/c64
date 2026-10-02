"""Launch and tear down a headless x64sc that is driven over the binary monitor."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
from pathlib import Path

from .bmon import BinaryMonitor, BinMonError

ROOT = Path(__file__).resolve().parent.parent

BORDER_MODES = {"normal": 0, "full": 1, "debug": 2, "none": 3}


def find_x64sc(explicit: str | None = None) -> Path:
    candidates = [explicit, os.environ.get("C64H_X64SC"), str(ROOT / "tools/vice/bin/x64sc"), shutil.which("x64sc")]
    for cand in candidates:
        if cand and Path(cand).is_file() and os.access(cand, os.X_OK):
            return Path(cand)
    raise FileNotFoundError("x64sc not found: run `make setup` (or set C64H_X64SC)")


def vice_data_dir(x64sc: Path) -> Path | None:
    """Locate VICE's data dir (ROMs, palettes) next to a --prefix install."""
    for cand in (x64sc.parent.parent / "share/vice", x64sc.parent):
        if (cand / "C64").is_dir():
            return cand
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Vice:
    """Context manager: a running x64sc plus a connected BinaryMonitor (`.mon`)."""

    def __init__(self, x64sc: Path, *, ntsc: bool = False, borders: str = "normal", warp: bool = True,
                 log_path: Path | None = None, extra_args: list[str] | None = None):
        self.x64sc = x64sc
        self.ntsc = ntsc
        self.borders = borders
        self.warp = warp
        self.log_path = log_path
        self.extra_args = extra_args or []
        self.proc: subprocess.Popen | None = None
        self.mon: BinaryMonitor | None = None
        self._log = None

    def args(self, port: int) -> list[str]:
        return [
            str(self.x64sc),
            "-default",                                   # ignore ~/.config/vice/vicerc
            "-binarymonitor", "-binarymonitoraddress", f"ip4://127.0.0.1:{port}",
            "-model", "ntsc" if self.ntsc else "c64",
            "-VICIIborders", str(BORDER_MODES[self.borders]),
            "-sounddev", "dummy", "+sound",
            "-drive8type", "0",
            "-jamaction", "0",                            # JAM -> binary monitor JAM event
            "+logtofile", "+logcolorize",                 # log to stdout (our file), no ANSI colors
            *(["-warp"] if self.warp else []),
            *self.extra_args,
        ]

    def __enter__(self) -> "Vice":
        last_err: Exception | None = None
        for _attempt in range(3):                      # a fresh port per attempt
            port = free_port()
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log = open(self.log_path, "w")
            self.proc = subprocess.Popen(
                self.args(port), stdin=subprocess.DEVNULL,
                stdout=self._log or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                self.mon = BinaryMonitor.connect("127.0.0.1", port, timeout=10.0,
                                                 alive=lambda: self.proc.poll() is None)
                return self
            except BinMonError as err:
                last_err = err
                self._kill()
        raise BinMonError(f"failed to start VICE: {last_err} (see {self.log_path})")

    def __exit__(self, *exc) -> None:
        if self.mon is not None:
            self.mon.quit()
            self.mon.close()
            self.mon = None
        if self.proc is not None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self._kill()

    def _kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        self.proc = None
        if self._log is not None:
            self._log.close()
            self._log = None

