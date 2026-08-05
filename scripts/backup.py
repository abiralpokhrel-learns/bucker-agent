"""Backup: Postgres + blobstore, timestamped, with retention (review #7).

The event store, snapshots, and telemetry live in Postgres; the blobs
(verifier diagnostics, diff payloads) live in the blobstore directory.
They must be backed up TOGETHER — a dump without its blobs is a broken
audit trail.

Works with the documented setup (docker compose Postgres) without a local
pg client: dumps via `docker exec <container> pg_dump`. Falls back to a
local `pg_dump` if the container is not present.

Usage:
    uv run python -m scripts.backup --dest backups --keep 7

Restore drill is in docs/OPERATIONS.md.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from bucker.config import settings


def _now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def dump_postgres(out_file: Path, container: str | None) -> None:
    """Custom-format pg_dump. Prefers docker exec; falls back to local pg_dump."""
    # Pull user/db out of the asyncpg DSN for the local fallback.
    from urllib.parse import urlparse

    parsed = urlparse(settings.database_url)
    db = (parsed.path or "/bucker").lstrip("/") or "bucker"

    cmd = ["docker", "exec", container, "pg_dump", "-U", "postgres", "-Fc", db]
    if container is None or subprocess.run(["docker", "inspect", container],
                                           capture_output=True).returncode != 0:
        cmd = ["pg_dump", settings.database_url, "-Fc"]
        if shutil.which("pg_dump") is None:
            raise RuntimeError(
                "no pg_dump on PATH and no docker container to exec into — "
                "install a Postgres client or start the compose stack"
            )

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {proc.stderr.decode()[:400]}")
    out_file.write_bytes(proc.stdout)


def archive_blobstore(blob_root: Path, dest: Path) -> None:
    """tar.gz the blobstore. No blobs -> empty archive, still consistent."""
    shutil.make_archive(str(dest.with_suffix("")), "gztar", root_dir=blob_root)


def prune(dest_dir: Path, keep: int) -> list[Path]:
    """Delete backups beyond the newest `keep`, oldest first."""
    backups = sorted(
        p for p in dest_dir.iterdir() if p.is_dir()
    )
    removed = []
    for old in backups[:-keep] if len(backups) > keep else []:
        shutil.rmtree(old, ignore_errors=True)
        removed.append(old)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="backups", help="backup root dir")
    parser.add_argument("--keep", type=int, default=7,
                        help="backups to retain (default 7)")
    parser.add_argument("--container", default="bucker-pg",
                        help="compose container name (None = local pg_dump)")
    args = parser.parse_args()

    dest_dir = Path(args.dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now()
    out = dest_dir / stamp
    out.mkdir(parents=True, exist_ok=True)

    print(f"backup {stamp} -> {out}")
    dump_postgres(out / "db.dump", args.container)
    print("  db.dump ok")
    archive_blobstore(Path(settings.blob_root), out / "blobstore")
    print("  blobstore.tar.gz ok")

    removed = prune(dest_dir, args.keep)
    for r in removed:
        print(f"  pruned {r.name} (retention {args.keep})")
    print("backup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
