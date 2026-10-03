"""Leitura mínima de .class: descobre se é abstrata/interface (sem precisar de javap)."""
from __future__ import annotations

import struct
from pathlib import Path

_SIZES = {3: 4, 4: 4, 9: 4, 10: 4, 11: 4, 12: 4, 17: 4, 18: 4, 7: 2, 8: 2, 16: 2, 19: 2, 20: 2, 15: 3}


def access_flags(path: Path) -> int | None:
    try:
        b = Path(path).read_bytes()
        if b[:4] != b"\xca\xfe\xba\xbe":
            return None
        n = struct.unpack(">H", b[8:10])[0]
        i, idx = 10, 1
        while idx < n:
            tag = b[i]
            if tag == 1:
                i += 3 + struct.unpack(">H", b[i + 1:i + 3])[0]
            elif tag in (5, 6):
                i += 9
                idx += 1
            elif tag in _SIZES:
                i += 1 + _SIZES[tag]
            else:
                return None
            idx += 1
        return struct.unpack(">H", b[i:i + 2])[0]
    except Exception:
        return None


def is_abstract_or_interface(path: Path) -> bool:
    fl = access_flags(path)
    return bool(fl and fl & (0x0200 | 0x0400))
