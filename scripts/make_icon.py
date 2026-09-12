"""Generate the ATR desktop icon (multi-size ICO) with no third-party deps.

Outputs assets/atr.ico (16..256px). Also writes a 256px PNG copy.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BLURPLE = (99, 91, 255)
WHITE = (255, 255, 255)

# Upward arrow as normalized polygon (tip -> right of head -> stem -> back).
ARROW = [
    (0.50, 0.14),
    (0.73, 0.38),
    (0.61, 0.38),
    (0.61, 0.86),
    (0.39, 0.86),
    (0.39, 0.38),
    (0.27, 0.38),
]


def _in_poly(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _in_rounded_rect(x: float, y: float, w: float, h: float, r: float) -> bool:
    cx = max(r - x, 0.0, x - (w - 1 - r))
    cy = max(r - y, 0.0, y - (h - 1 - r))
    return cx * cx + cy * cy <= r * r


def render_px(size: int) -> list[bytes]:
    rows = []
    r = size * 0.20
    for y in range(size):
        row = bytearray()
        for x in range(size):
            if not _in_rounded_rect(x + 0.5, y + 0.5, size, size, r):
                row.extend((0, 0, 0, 0))
            elif _in_poly((x + 0.5) / size, (y + 0.5) / size, ARROW):
                row.extend(WHITE + (255,))
            else:
                row.extend(BLURPLE + (255,))
        rows.append(bytes(row))
    return rows


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = bytearray()
    data = bytearray()
    offset = 6 + 16 * len(images)
    for w, png in images:
        entries += struct.pack(
            "<BBBBHHII", 0 if w >= 256 else w, 0 if w >= 256 else w, 0, 0, 1, 32, len(png), offset
        )
        data += png
        offset += len(png)
    return header + bytes(entries) + bytes(data)


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    assets = root / "assets"
    assets.mkdir(exist_ok=True)

    images = []
    for size in (16, 32, 48, 64, 128, 256):
        images.append((size, _png(size, size, render_px(size))))

    (assets / "atr.ico").write_bytes(_ico(images))
    (assets / "atr-256.png").write_bytes(images[-1][1])
    print(f"wrote {assets / 'atr.ico'} ({len(images)} sizes) and atr-256.png")


if __name__ == "__main__":
    main()