"""Gera sbfl/web/static/jflp.ico e favicon.png (Python puro: zlib + struct). Uso: python tools/gerar_icone.py"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

BG = (31, 78, 95)        # azul-petróleo sóbrio
FG = (240, 244, 245)     # quase branco
SS = 3                   # supersampling por eixo (antisserrilhado)


def _in_round_square(x: float, y: float, r: float = 0.22) -> bool:
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _in_j(x: float, y: float) -> bool:
    if 0.34 <= x <= 0.80 and 0.20 <= y <= 0.30:          # barra superior
        return True
    if 0.50 <= x <= 0.64 and 0.20 <= y <= 0.62:          # haste
        return True
    if y >= 0.62:                                         # gancho: meia coroa
        d = math.hypot(x - 0.39, y - 0.62)
        if 0.11 <= d <= 0.25:
            return True
    return 0.14 <= x <= 0.28 and 0.55 <= y <= 0.62       # ponta do gancho


def _png(size: int) -> bytes:
    rows = []
    n = SS * SS
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            bg = fg = 0
            for sy in range(SS):
                for sx in range(SS):
                    x = (px + (sx + 0.5) / SS) / size
                    y = (py + (sy + 0.5) / SS) / size
                    if _in_round_square(x, y):
                        bg += 1
                        if _in_j(x, y):
                            fg += 1
            if bg == 0:
                row += bytes((0, 0, 0, 0))
                continue
            f = fg / bg
            rgb = [round(BG[i] * (1 - f) + FG[i] * f) for i in range(3)]
            row += bytes((*rgb, round(255 * bg / n)))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def _ico(pngs: dict[int, bytes]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(pngs))
    offset = 6 + 16 * len(pngs)
    entries, data = b"", b""
    for size, png in pngs.items():
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        data += png
        offset += len(png)
    return head + entries + data


def main() -> None:
    static = Path(__file__).resolve().parent.parent / "sbfl" / "web" / "static"
    pngs = {s: _png(s) for s in (16, 32, 48, 256)}
    (static / "jflp.ico").write_bytes(_ico(pngs))
    (static / "favicon.png").write_bytes(pngs[32])
    print(f"gerado em {static}")


if __name__ == "__main__":
    main()
