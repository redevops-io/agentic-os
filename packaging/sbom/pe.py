"""Minimal Windows PE helpers: the numeric FileVersion of a DLL/EXE from its VS_FIXEDFILEINFO.

The signature 0xFEEF04BD occurs once, in the RT_VERSION resource; scanning for it avoids a pefile
dependency (pefile is only available in the Windows build-tools lock). Returns None when absent.
"""
from __future__ import annotations

import struct

_SIG = b"\xbd\x04\xef\xfe"


def file_version(data: bytes) -> str | None:
    if data[:2] != b"MZ":
        return None
    i = data.find(_SIG)
    if i < 0 or i + 16 > len(data):
        return None
    _sig, _struct_ver, ms, ls = struct.unpack_from("<IIII", data, i)
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def version_strings(data: bytes) -> dict[str, str]:
    """ProductVersion / FileVersion / CompanyName / ProductName from the StringFileInfo block (UTF-16)."""
    out: dict[str, str] = {}
    for key in ("CompanyName", "FileVersion", "ProductName", "ProductVersion", "OriginalFilename"):
        k = key.encode("utf-16-le") + b"\x00\x00"
        i = data.find(k)
        if i < 0:
            continue
        j = i + len(k)
        j += (-j) % 4  # value is DWORD aligned
        end = data.find(b"\x00\x00", j)
        while end >= 0 and (end - j) % 2:
            end = data.find(b"\x00\x00", end + 1)
        if end > j:
            try:
                out[key] = data[j:end].decode("utf-16-le")
            except UnicodeDecodeError:
                pass
    return out
