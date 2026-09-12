import struct
import zlib
from pathlib import Path


def make_png(path: Path, left_rgb=(255, 0, 0), right_rgb=(0, 0, 255), size: int = 16) -> Path:
    width = height = size
    half = width // 2
    row = bytes(left_rgb) * half + bytes(right_rgb) * (width - half)
    raw = b"".join(b"\x00" + row for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        content = tag + data
        return struct.pack(">I", len(data)) + content + struct.pack(">I", zlib.crc32(content))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(payload)
    return path


def sample_bytes(media_type: str) -> bytes:
    samples = {
        "image/png": b"\x89PNG\r\n\x1a\nvalid-png",
        "image/jpeg": b"\xff\xd8\xff\xe0valid-jpeg\xff\xd9",
        "image/gif": b"GIF89a-valid-gif",
        "image/webp": b"RIFF\x10\x00\x00\x00WEBPvalid-webp",
    }
    return samples[media_type]
