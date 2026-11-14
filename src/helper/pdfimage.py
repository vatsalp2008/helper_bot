"""Rasterize PDF pages with poppler's pdftoppm and compare them pixel by pixel.

Plain Python (no imaging library): grayscale PGM in, PNG out.
"""

from __future__ import annotations

import itertools
import struct
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path

from .errors import HelperError
from .latex import find_binary


@dataclass
class Gray:
    width: int
    height: int
    pixels: bytes  # row-major, one byte per pixel, 0 = black

    def row(self, y: int) -> bytes:
        return self.pixels[y * self.width : (y + 1) * self.width]


@dataclass
class PixelDiff:
    differing: int
    total: int
    size_a: tuple[int, int]
    size_b: tuple[int, int]

    @property
    def ratio(self) -> float:
        return self.differing / self.total if self.total else 0.0


def page_count(pdf: Path) -> int | None:
    binary = find_binary("pdfinfo")
    if binary is None:
        return None
    proc = subprocess.run([str(binary), str(pdf)], capture_output=True, text=True, timeout=60)
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    return None


def rasterize(pdf: Path, out_prefix: Path, dpi: int = 100, page: int = 1) -> Gray:
    binary = find_binary("pdftoppm")
    if binary is None:
        raise HelperError("pdftoppm not found. Install poppler (macOS: brew install poppler).")
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(binary), "-gray", "-r", str(dpi), "-f", str(page), "-l", str(page), "-singlefile",
           str(pdf), str(out_prefix)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    pgm = out_prefix.with_suffix(".pgm")
    if proc.returncode != 0 or not pgm.is_file():
        raise HelperError(f"pdftoppm failed on {pdf.name}: {proc.stderr.strip()}")
    return read_pgm(pgm)


def read_pgm(path: Path) -> Gray:
    data = path.read_bytes()
    fields: list[bytes] = []
    pos = 0
    while len(fields) < 4:  # magic, width, height, maxval; '#' starts a comment
        while data[pos : pos + 1].isspace():
            pos += 1
        if data[pos : pos + 1] == b"#":
            pos = data.index(b"\n", pos) + 1
            continue
        end = pos
        while not data[end : end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    if fields[0] != b"P5" or int(fields[3]) > 255:
        raise HelperError(f"{path.name}: not an 8-bit binary PGM")
    width, height = int(fields[1]), int(fields[2])
    pixels = data[pos + 1 : pos + 1 + width * height]
    if len(pixels) != width * height:
        raise HelperError(f"{path.name}: truncated image")
    return Gray(width, height, pixels)


def diff(a: Gray, b: Gray, tolerance: int = 48) -> PixelDiff:
    """Pixels whose gray levels differ by more than `tolerance`. Area outside the smaller page counts as differing."""
    width, height = min(a.width, b.width), min(a.height, b.height)
    differing = 0
    for y in range(height):
        differing += sum(1 for p, q in zip(a.row(y)[:width], b.row(y)[:width]) if abs(p - q) > tolerance)
    total = max(a.width, b.width) * max(a.height, b.height)
    differing += total - width * height
    return PixelDiff(differing, total, (a.width, a.height), (b.width, b.height))


def _rgb(gray_row: bytes) -> bytes:
    return bytes(itertools.chain.from_iterable(zip(gray_row, gray_row, gray_row)))


def _diff_row(ra: bytes, rb: bytes, tolerance: int) -> bytes:
    out = bytearray()
    for p, q in zip(ra, rb):
        if abs(p - q) > tolerance:
            out += b"\xd0\x10\x10"  # red where the pages differ
        else:
            v = 160 + p * 95 // 255  # faded copy of the first page
            out += bytes((v, v, v))
    return bytes(out)


def side_by_side(panels: list[Gray], path: Path, diff_of: tuple[int, int] | None = None, tolerance: int = 48,
                 gap: int = 12) -> Path:
    """Write the panels next to each other as a PNG. With `diff_of=(i, j)`, add a panel marking differences in red."""
    width = max(p.width for p in panels)
    height = max(p.height for p in panels)
    blank = b"\xff" * width
    columns = len(panels) + (1 if diff_of else 0)
    total_width = columns * width + (columns - 1) * gap
    spacer = b"\x80\x80\x80" * gap
    rows = []
    for y in range(height):
        parts = []
        grays = [(p.row(y) + b"\xff" * (width - p.width)) if y < p.height else blank for p in panels]
        parts.extend(_rgb(g) for g in grays)
        if diff_of:
            parts.append(_diff_row(grays[diff_of[0]], grays[diff_of[1]], tolerance))
        rows.append(spacer.join(parts))
    return write_png(path, total_width, height, rows)


def write_png(path: Path, width: int, height: int, rgb_rows: list[bytes]) -> Path:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rgb_rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6))
                     + chunk(b"IEND", b""))
    return path
