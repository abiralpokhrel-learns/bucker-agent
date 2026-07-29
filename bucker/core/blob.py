"""Content-addressed blob storage for large/verbatim payloads.

[VIBE-safe] — plumbing, but with one load-bearing property: refs are content
hashes. Storing a model response returns ``sha256:<hex>``; storing identical
content returns the identical ref. Replay therefore cannot be fooled by a
mutated blob — the ref would no longer match the content (see ``verify``).

Local filesystem now, S3/MinIO later. The event log only ever holds the ref
(``events.tool_output_ref``), so swapping the backend never touches the schema.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_PREFIX = "sha256:"


class BlobStore:
    """Immutable content-addressed store under ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- paths ---
    def _path_for(self, digest: str) -> Path:
        # Fan out by first two hex chars so a directory listing stays sane.
        return self.root / digest[:2] / digest

    def _digest_of(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # ------------------------------------------------------------- write ---
    def put(self, data: bytes | str) -> str:
        """Store bytes, return ``sha256:<hex>``. Idempotent by construction."""
        if isinstance(data, str):
            data = data.encode("utf-8")

        digest = self._digest_of(data)
        path = self._path_for(digest)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash mid-write can't leave a torn blob
            # that later reads would treat as valid content.
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return _PREFIX + digest

    def put_json(self, obj: Any) -> str:
        """Store JSON with stable key order so equal objects share a ref."""
        return self.put(json.dumps(obj, sort_keys=True, separators=(",", ":")))

    # -------------------------------------------------------------- read ---
    def get(self, ref: str) -> bytes:
        path = self._path_for(self._strip(ref))
        if not path.exists():
            raise KeyError(f"blob not found: {ref}")
        return path.read_bytes()

    def get_text(self, ref: str) -> str:
        return self.get(ref).decode("utf-8")

    def get_json(self, ref: str) -> Any:
        return json.loads(self.get_text(ref))

    def exists(self, ref: str) -> bool:
        return self._path_for(self._strip(ref)).exists()

    # ------------------------------------------------------------ verify ---
    def verify(self, ref: str) -> bool:
        """True iff stored content still hashes to its ref.

        The replay engine calls this before trusting a recorded output. A False
        here means the archive was tampered with or corrupted, which must be
        reported as a replay mismatch rather than silently replayed.
        """
        digest = self._strip(ref)
        try:
            return self._digest_of(self.get(ref)) == digest
        except KeyError:
            return False

    @staticmethod
    def _strip(ref: str) -> str:
        return ref[len(_PREFIX):] if ref.startswith(_PREFIX) else ref
