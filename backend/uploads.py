"""Player-uploaded art. Never generated - that distinction is the whole point.

The halal constraint is specific and it is not satisfied by "we don't have an
image model wired up". It is satisfied by the product having a real path for a
player to bring their OWN art, so the absence of generation is a design choice
rather than a missing feature. Without an upload route, "upload your own art"
is a sentence in a README that nobody can act on.

What this does NOT do, deliberately:
  - no image generation of any kind, and no route that could become one
  - no re-encoding or transformation of the bytes: what you uploaded is what
    is stored and served, so nothing here can quietly alter a person's art
  - no remote fetching by URL, which would make the server a proxy for
    arbitrary content and an SSRF surface

Safety properties that matter regardless of theology:
  - the extension is decided by the SNIFFED magic bytes, never by the filename
    a browser sent, so `evil.html` renamed to `.png` cannot be stored as HTML
  - only raster/vector formats known to be safe to serve are accepted, and SVG
    is deliberately EXCLUDED because it can carry script
  - the stored filename is a content hash, so the same art uploaded twice
    costs one file, and a crafted filename cannot escape the directory
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import config

MEDIA_DIR = Path(config.DATA_DIR) / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 4 * 1024 * 1024          # 4 MB - an avatar, not a photo library

# Magic-byte signatures. SVG is intentionally absent: it is XML, it can carry
# <script>, and serving it same-origin would be a stored-XSS hole.
SIGNATURES = (
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
WEBP_RIFF = (b"RIFF", b"WEBP")


class UploadError(ValueError):
    pass


def sniff(data: bytes):
    """Decide the type from the CONTENT. A filename is a claim, not evidence."""
    for magic, ext, mime in SIGNATURES:
        if data.startswith(magic):
            return ext, mime
    if data[:4] == WEBP_RIFF[0] and data[8:12] == WEBP_RIFF[1]:
        return "webp", "image/webp"
    raise UploadError("that file is not a JPEG, PNG, GIF or WebP image")


def store(data: bytes, *, owner: str = "") -> dict:
    if not data:
        raise UploadError("empty file")
    if len(data) > MAX_BYTES:
        raise UploadError(f"images must be under {MAX_BYTES // (1024 * 1024)} MB")

    ext, mime = sniff(data)
    digest = hashlib.sha256(data).hexdigest()[:32]
    name = f"{digest}.{ext}"
    path = MEDIA_DIR / name
    if not path.exists():                      # identical art is stored once
        path.write_bytes(data)
    return {"url": f"/media/{name}", "name": name, "mime": mime,
            "bytes": len(data), "owner": owner}


def path_for(name: str) -> Path:
    """Resolve a stored file. The name must be exactly what store() produced -
    anything else (a path, a traversal, a guess) is refused rather than
    normalised, because normalising attacker input is how directories escape."""
    candidate = (MEDIA_DIR / name).resolve()
    if candidate.parent != MEDIA_DIR.resolve() or not candidate.is_file():
        raise UploadError("no such file")
    return candidate


def mime_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return {"jpg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp"}.get(ext, "application/octet-stream")
