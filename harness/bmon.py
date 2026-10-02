"""Client for the VICE binary monitor protocol (API v2, as implemented by VICE 3.10).

Wire format (all integers little-endian):
  command  : STX(0x02) API(0x02) body_len:u32 request_id:u32 cmd:u8 body...
  response : STX(0x02) API(0x02) body_len:u32 type:u8 error:u8 request_id:u32 body...

Responses to a command carry its request id; events carry 0xFFFFFFFF. Errors come back
with response type 0x00. While the emulation runs, VICE only polls the socket once per
frame (at vsync), so any queued command stops the machine at the next frame boundary.
"""

from __future__ import annotations

import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field

STX = 0x02
API_VERSION = 0x02
EVENT_ID = 0xFFFFFFFF

# Commands
CMD_MEM_GET = 0x01
CMD_MEM_SET = 0x02
CMD_CP_SET = 0x12
CMD_CP_DELETE = 0x13
CMD_CP_TOGGLE = 0x15
CMD_COND_SET = 0x22
CMD_REGS_GET = 0x31
CMD_REGS_SET = 0x32
CMD_RES_GET = 0x51
CMD_RES_SET = 0x52
CMD_PING = 0x81
CMD_BANKS = 0x82
CMD_REGS_AVAILABLE = 0x83
CMD_DISPLAY_GET = 0x84
CMD_VICE_INFO = 0x85
CMD_PALETTE_GET = 0x91
CMD_EXIT = 0xAA
CMD_QUIT = 0xBB
CMD_RESET = 0xCC

# Response / event types
RESP_ERROR = 0x00
EV_CHECKPOINT = 0x11
EV_REGISTERS = 0x31
EV_JAM = 0x61
EV_STOPPED = 0x62
EV_RESUMED = 0x63

# Checkpoint operations
OP_LOAD = 0x01
OP_STORE = 0x02
OP_EXEC = 0x04

MAIN_MEMSPACE = 0

ERROR_NAMES = {
    0x01: "object missing",
    0x02: "invalid memspace",
    0x80: "invalid command length",
    0x81: "invalid parameter",
    0x82: "invalid API version",
    0x83: "unknown command",
    0x8F: "command failed",
}


class BinMonError(Exception):
    """Base class for binary monitor failures."""


class ProtocolError(BinMonError):
    """The byte stream did not look like the VICE binary monitor protocol."""


class CommandError(BinMonError):
    def __init__(self, cmd: int, code: int):
        self.cmd = cmd
        self.code = code
        super().__init__(f"VICE rejected command 0x{cmd:02x}: {ERROR_NAMES.get(code, 'error')} (0x{code:02x})")


@dataclass
class Message:
    type: int
    error: int
    req_id: int
    body: bytes

    @property
    def is_event(self) -> bool:
        return self.req_id == EVENT_ID


@dataclass
class Display:
    """Raw VIC-II draw buffer: one byte (color index 0-15) per pixel, `width` bytes per row."""

    width: int
    height: int
    inner_x: int
    inner_y: int
    inner_width: int
    inner_height: int
    pixels: bytes


@dataclass
class Stop:
    """What happened while the machine ran until its next stop."""

    events: list[Message] = field(default_factory=list)
    registers: dict[str, int] = field(default_factory=dict)
    jam: bool = False
    checkpoints: list[int] = field(default_factory=list)


class BinaryMonitor:
    def __init__(self, sock: socket.socket, timeout: float = 10.0):
        self.sock = sock
        self.timeout = timeout
        self._buf = bytearray()
        self._next_id = 1
        self._events: deque[Message] = deque()
        self._replies: dict[int, Message] = {}
        self._reg_names: dict[int, str] | None = None
        self._reg_ids: dict[str, int] | None = None
        self._banks: dict[str, int] | None = None

    # ------------------------------------------------------------------ connection
    @classmethod
    def connect(cls, host: str, port: int, timeout: float = 10.0, alive=None) -> "BinaryMonitor":
        """Connect, retrying until VICE has opened its socket. `alive()` aborts early if VICE died."""
        deadline = time.monotonic() + timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if alive is not None and not alive():
                raise BinMonError("VICE exited before its binary monitor accepted a connection")
            try:
                sock = socket.create_connection((host, port), timeout=1.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                return cls(sock)
            except OSError as err:
                last_err = err
                time.sleep(0.05)
        raise BinMonError(f"could not connect to VICE binary monitor on {host}:{port}: {last_err}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------ framing
    def _new_id(self) -> int:
        rid = self._next_id
        self._next_id = 1 if self._next_id >= 0x7FFFFFFF else self._next_id + 1
        return rid

    def _encode(self, cmd: int, body: bytes) -> tuple[int, bytes]:
        rid = self._new_id()
        return rid, struct.pack("<BBIIB", STX, API_VERSION, len(body), rid, cmd) + body

    def send(self, *commands: tuple[int, bytes]) -> list[int]:
        """Send one or more commands in a single write (VICE handles them in order)."""
        ids, chunks = [], []
        for cmd, body in commands:
            rid, data = self._encode(cmd, body)
            ids.append(rid)
            chunks.append(data)
        self.sock.sendall(b"".join(chunks))
        return ids

    def _fill(self, n: int, deadline: float) -> None:
        while len(self._buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for VICE")
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(max(65536, n - len(self._buf)))
            except socket.timeout as err:
                raise TimeoutError("timed out waiting for VICE") from err
            if not chunk:
                raise BinMonError("VICE closed the binary monitor connection")
            self._buf += chunk

    def _read_message(self, deadline: float) -> Message:
        self._fill(12, deadline)
        if self._buf[0] != STX:
            raise ProtocolError(f"expected STX, got 0x{self._buf[0]:02x}")
        (body_len,) = struct.unpack_from("<I", self._buf, 2)
        rtype, err = self._buf[6], self._buf[7]
        (rid,) = struct.unpack_from("<I", self._buf, 8)
        self._fill(12 + body_len, deadline)
        body = bytes(self._buf[12 : 12 + body_len])
        del self._buf[: 12 + body_len]
        return Message(rtype, err, rid, body)

    def _pump(self, deadline: float) -> None:
        msg = self._read_message(deadline)
        if msg.is_event:
            self._events.append(msg)
        else:
            self._replies[msg.req_id] = msg

    def reply(self, rid: int, cmd: int, timeout: float | None = None) -> Message:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while rid not in self._replies:
            self._pump(deadline)
        msg = self._replies.pop(rid)
        if msg.type == RESP_ERROR or msg.error != 0:
            raise CommandError(cmd, msg.error)
        return msg

    def call(self, cmd: int, body: bytes = b"", timeout: float | None = None) -> Message:
        (rid,) = self.send((cmd, body))
        return self.reply(rid, cmd, timeout)

    def drain_events(self) -> list[Message]:
        events = list(self._events)
        self._events.clear()
        return events

    def wait_event(self, types: set[int], timeout: float | None = None) -> Message:
        """Return the next queued or incoming event whose type is in `types` (others stay queued)."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        skipped: list[Message] = []
        try:
            while True:
                while self._events:
                    ev = self._events.popleft()
                    if ev.type in types:
                        return ev
                    skipped.append(ev)
                self._pump(deadline)
        finally:
            self._events.extendleft(reversed(skipped))

    # ------------------------------------------------------------------ decoding helpers
    def summarize(self, events: list[Message]) -> Stop:
        stop = Stop(events=events)
        for ev in events:
            if ev.type == EV_REGISTERS:
                stop.registers = self._decode_registers(ev.body)
            elif ev.type == EV_JAM:
                stop.jam = True
            elif ev.type == EV_CHECKPOINT:
                stop.checkpoints.append(struct.unpack_from("<I", ev.body, 0)[0])
        return stop

    def _decode_registers(self, body: bytes) -> dict[str, int]:
        names = self.register_names()
        (count,) = struct.unpack_from("<H", body, 0)
        pos, regs = 2, {}
        for _ in range(count):
            size = body[pos]
            rid = body[pos + 1]
            value = int.from_bytes(body[pos + 2 : pos + 1 + size], "little")
            regs[names.get(rid, f"r{rid:02x}")] = value
            pos += 1 + size
        return regs

    # ------------------------------------------------------------------ commands
    def ping(self) -> None:
        self.call(CMD_PING)

    def vice_version(self) -> str:
        body = self.call(CMD_VICE_INFO).body
        n = body[0]
        return ".".join(str(b) for b in body[1 : 1 + n])

    def bank_id(self, name: str) -> int:
        if self._banks is None:
            body = self.call(CMD_BANKS).body
            (count,) = struct.unpack_from("<H", body, 0)
            pos, banks = 2, {}
            for _ in range(count):
                size = body[pos]
                (bid,) = struct.unpack_from("<H", body, pos + 1)
                nlen = body[pos + 3]
                banks[body[pos + 4 : pos + 4 + nlen].decode()] = bid
                pos += 1 + size
            self._banks = banks
        return self._banks[name]

    def register_names(self) -> dict[int, str]:
        if self._reg_names is None:
            body = self.call(CMD_REGS_AVAILABLE, bytes([MAIN_MEMSPACE])).body
            (count,) = struct.unpack_from("<H", body, 0)
            pos, names = 2, {}
            for _ in range(count):
                size = body[pos]
                rid, nlen = body[pos + 1], body[pos + 3]
                names[rid] = body[pos + 4 : pos + 4 + nlen].decode()
                pos += 1 + size
            self._reg_names = names
            self._reg_ids = {v: k for k, v in names.items()}
        return self._reg_names

    def registers(self) -> dict[str, int]:
        return self._decode_registers(self.call(CMD_REGS_GET, bytes([MAIN_MEMSPACE])).body)

    def set_registers(self, **values: int) -> None:
        self.register_names()
        assert self._reg_ids is not None
        items = b"".join(struct.pack("<BBH", 3, self._reg_ids[name], value & 0xFFFF) for name, value in values.items())
        self.call(CMD_REGS_SET, struct.pack("<BH", MAIN_MEMSPACE, len(values)) + items)

    @staticmethod
    def mem_get_body(start: int, end: int, bank: int, side_effects: bool = False) -> bytes:
        return struct.pack("<BHHBH", int(side_effects), start, end, MAIN_MEMSPACE, bank)

    @staticmethod
    def decode_mem(body: bytes) -> bytes:
        (n,) = struct.unpack_from("<H", body, 0)
        return body[2 : 2 + (n or 0x10000)]

    def mem_get(self, start: int, end: int, bank: str = "cpu") -> bytes:
        return self.decode_mem(self.call(CMD_MEM_GET, self.mem_get_body(start, end, self.bank_id(bank))).body)

    def mem_set(self, start: int, data: bytes, bank: str = "ram") -> None:
        end = start + len(data) - 1
        if not data or end > 0xFFFF:
            raise ValueError("memory range out of bounds")
        self.call(CMD_MEM_SET, struct.pack("<BHHBH", 0, start, end, MAIN_MEMSPACE, self.bank_id(bank)) + data)

    def checkpoint_set(self, start: int, end: int | None = None, op: int = OP_EXEC, stop: bool = True) -> int:
        # Never temporary: VICE 3.10 resumes execution after setting a temporary checkpoint.
        body = struct.pack("<HHBBBBB", start, start if end is None else end, int(stop), 1, op, 0, MAIN_MEMSPACE)
        return struct.unpack_from("<I", self.call(CMD_CP_SET, body).body, 0)[0]

    def checkpoint_delete(self, number: int) -> None:
        self.call(CMD_CP_DELETE, struct.pack("<I", number))

    def resource_set(self, name: str, value: int | str) -> None:
        raw_name = name.encode()
        if isinstance(value, int):
            body = struct.pack("<BB", 1, len(raw_name)) + raw_name + struct.pack("<Bi", 4, value)
        else:
            raw = value.encode()
            body = struct.pack("<BB", 0, len(raw_name)) + raw_name + bytes([len(raw)]) + raw
        self.call(CMD_RES_SET, body)

    def reset(self, kind: int = 1) -> None:
        """kind 0 = soft reset, 1 = power cycle. Either one resumes execution."""
        self.call(CMD_RESET, bytes([kind]))

    def resume(self) -> None:
        self.call(CMD_EXIT)

    def quit(self) -> None:
        try:
            self.send((CMD_QUIT, b""))
        except OSError:
            pass

    @staticmethod
    def decode_display(body: bytes) -> Display:
        (fields_len,) = struct.unpack_from("<I", body, 0)
        dw, dh, xo, yo, iw, ih = struct.unpack_from("<HHHHHH", body, 4)
        bpp = body[16]
        if bpp != 8:
            raise ProtocolError(f"unexpected display depth {bpp}")
        (buf_len,) = struct.unpack_from("<I", body, 4 + fields_len)
        pixels = body[8 + fields_len :]
        # Unpatched VICE 3.10 under-reports the response length by 4 bytes: pad the tail.
        if len(pixels) < buf_len:
            pixels = pixels + bytes(buf_len - len(pixels))
        return Display(dw, dh, xo, yo, iw, ih, pixels[: dw * dh])

    def step_frame(self, display: bool = False, mem: tuple[int, int] | None = None,
                   timeout: float | None = None) -> tuple[Stop, Display | None, bytes | None]:
        """Resume and stop again at the next frame boundary, optionally grabbing the frame.

        Exit and the follow-up requests go out in ONE write: VICE resumes on Exit, finds the
        rest still queued on the socket at the next vsync and stops right there, so each call
        advances exactly one frame.
        """
        self.drain_events()
        cmds: list[tuple[int, bytes]] = [(CMD_EXIT, b"")]
        if display:
            cmds.append((CMD_DISPLAY_GET, bytes([1, 0])))
        if mem is not None:
            cmds.append((CMD_MEM_GET, self.mem_get_body(mem[0], mem[1], self.bank_id("ram"))))
        else:
            cmds.append((CMD_PING, b""))
        ids = self.send(*cmds)
        self.reply(ids[0], CMD_EXIT, timeout)
        disp = data = None
        pos = 1
        if display:
            disp = self.decode_display(self.reply(ids[pos], CMD_DISPLAY_GET, timeout).body)
            pos += 1
        if mem is not None:
            data = self.decode_mem(self.reply(ids[pos], CMD_MEM_GET, timeout).body)
        else:
            self.reply(ids[pos], CMD_PING, timeout)
        return self.summarize(self.drain_events()), disp, data
