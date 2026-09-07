"""OBD-II via an ELM327 adapter — the evidence *upgrade* on the ladder (plan §8, P3).

A leave-installed ELM327 (Bluetooth/WiFi, the cheap standard) turns self-reported symptoms into
ground-truth vehicle state: stored DTCs, freeze-frame, live PIDs, and the VIN. This reads them over the
standard ELM327 AT + OBD-II protocol and projects them into ``DiagnosticObservation``s that flow into
the *same* diagnostic Mission as a chat message. The scanner is never required — it's the rung the
Evidence Planner asks for when confidence from description alone is low.

Transport is injectable (``send(command) -> raw_text``), defaulting to a serial or TCP ELM327; so the
protocol + decoders are unit-tested offline with recorded adapter responses and work unchanged against
real hardware.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

from .contracts import DiagnosticObservation

# send(command) -> raw ELM327 response text (hex bytes, may include the '>' prompt / status lines)
Transport = Callable[[str], str]

_INIT = ("ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP0")   # reset, echo off, no line/space/headers, auto proto
_DTC_LETTER = {0: "P", 1: "C", 2: "B", 3: "U"}

# a small, high-value live PID set: (pid, name, unit, decoder over data bytes)
_PIDS = {
    "0C": ("rpm", "rpm", lambda b: (b[0] * 256 + b[1]) / 4 if len(b) >= 2 else None),
    "05": ("coolant_temp", "C", lambda b: b[0] - 40 if b else None),
    "0D": ("speed", "km/h", lambda b: b[0] if b else None),
    "04": ("engine_load", "%", lambda b: round(b[0] * 100 / 255, 1) if b else None),
    "11": ("throttle", "%", lambda b: round(b[0] * 100 / 255, 1) if b else None),
    "2F": ("fuel_level", "%", lambda b: round(b[0] * 100 / 255, 1) if b else None),
    "42": ("control_module_voltage", "V", lambda b: (b[0] * 256 + b[1]) / 1000 if len(b) >= 2 else None),
}


@dataclass
class OBDSnapshot:
    dtcs: List[str] = field(default_factory=list)
    pids: Dict[str, float] = field(default_factory=dict)
    vin: str = ""
    freeze_frame: Dict[str, float] = field(default_factory=dict)


def _clean(resp: str) -> List[str]:
    """Normalize an ELM327 reply to a flat list of hex byte tokens (drop prompts/status/whitespace)."""
    out: List[str] = []
    for line in resp.replace(">", "").splitlines():
        line = line.strip()
        if not line or line.upper() in ("SEARCHING...", "OK", "?") or "NO DATA" in line.upper():
            continue
        out += [t for t in re.split(r"\s+", line) if re.fullmatch(r"[0-9A-Fa-f]{2}", t)]
    return out


def _hexbytes(tokens: Sequence[str]) -> List[int]:
    return [int(t, 16) for t in tokens]


def decode_dtc(hi: int, lo: int) -> str:
    """Two OBD-II bytes → a DTC string like 'P0302'."""
    letter = _DTC_LETTER[(hi >> 6) & 0x3]
    return f"{letter}{(hi >> 4) & 0x3}{hi & 0xF:X}{lo >> 4:X}{lo & 0xF:X}"


def parse_dtcs(resp: str, *, mode_byte: int = 0x43) -> List[str]:
    """Parse a mode-03 (stored) or mode-07 response into DTC strings."""
    toks = _hexbytes(_clean(resp))
    if not toks:
        return []
    # drop a leading response-mode byte (0x43) + optional count byte; then read 2-byte DTCs
    i = 0
    if toks and toks[0] == mode_byte:
        i = 1
    dtcs: List[str] = []
    while i + 1 < len(toks):
        hi, lo = toks[i], toks[i + 1]
        i += 2
        if hi == 0 and lo == 0:
            continue
        dtcs.append(decode_dtc(hi, lo))
    return dtcs


def parse_pid(resp: str, pid: str) -> Optional[float]:
    """Parse a mode-01 response for one PID (e.g. '0C') → decoded value."""
    toks = _hexbytes(_clean(resp))
    want = int(pid, 16)
    # find the 0x41 <pid> marker, decode the trailing data bytes
    for i in range(len(toks) - 1):
        if toks[i] == 0x41 and toks[i + 1] == want:
            data = toks[i + 2:]
            name_unit_dec = _PIDS.get(pid.upper())
            if name_unit_dec:
                return name_unit_dec[2](data)
    return None


def parse_vin(resp: str) -> str:
    """Parse a mode-09 PID-02 VIN response (ASCII in the data bytes)."""
    toks = _hexbytes(_clean(resp))
    # strip 0x49 0x02 markers and frame/line counters; keep printable ASCII
    chars = [chr(b) for b in toks if 0x20 <= b <= 0x7E]
    s = "".join(chars)
    m = re.search(r"[A-HJ-NPR-Z0-9]{17}", s.upper())
    return m.group(0) if m else ""


class ELM327Client:
    def __init__(self, transport: Transport):
        self._send = transport

    def connect(self) -> None:
        for cmd in _INIT:
            self._send(cmd)

    def read_dtcs(self) -> List[str]:
        return parse_dtcs(self._send("03"))

    def read_vin(self) -> str:
        return parse_vin(self._send("0902"))

    def read_pids(self, pids: Sequence[str] = tuple(_PIDS)) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for pid in pids:
            v = parse_pid(self._send(f"01{pid}"), pid)
            if v is not None:
                out[_PIDS[pid.upper()][0]] = v
        return out

    def snapshot(self) -> OBDSnapshot:
        """One read of everything useful — DTCs + key live PIDs + VIN."""
        return OBDSnapshot(dtcs=self.read_dtcs(), pids=self.read_pids(), vin=self.read_vin())


def observations_from_snapshot(snap: OBDSnapshot, *, observed_at: str = "") -> List[DiagnosticObservation]:
    """Project an OBD snapshot into DiagnosticObservations for the case/Mission."""
    obs = [DiagnosticObservation(kind="dtc", code=code, source="obd", observed_at=observed_at)
           for code in snap.dtcs]
    unit_by_name = {n: u for (n, u, _) in _PIDS.values()}
    for name, value in snap.pids.items():
        obs.append(DiagnosticObservation(kind="pid", code=name, value=str(value),
                                         unit=unit_by_name.get(name, ""), source="obd",
                                         observed_at=observed_at))
    return obs


def serial_transport(port: str = "/dev/rfcomm0", baud: int = 38400):  # pragma: no cover - hardware
    """Default transport for a Bluetooth/USB ELM327 over a serial port (lazy pyserial)."""
    import serial  # type: ignore
    conn = serial.Serial(port, baud, timeout=2)

    def send(command: str) -> str:
        conn.reset_input_buffer()
        conn.write((command + "\r").encode())
        buf = b""
        while b">" not in buf:
            chunk = conn.read(64)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="ignore")
    return send


def tcp_transport(host: str = "192.168.0.10", port: int = 35000):  # pragma: no cover - hardware
    """Default transport for a WiFi ELM327 (TCP)."""
    import socket
    sock = socket.create_connection((host, port), timeout=4)

    def send(command: str) -> str:
        sock.sendall((command + "\r").encode())
        buf = b""
        while b">" not in buf:
            chunk = sock.recv(256)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="ignore")
    return send
