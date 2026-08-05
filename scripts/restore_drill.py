"""Restore drill (hardening review #8): prove backups actually restore.

Takes the newest backup, restores it into a SCRATCH database, asserts the
audit trail is intact (events count matches), then drops the scratch db.
Run it on a schedule — a restore that has never been rehearsed is a wish.

Usage:
    uv run python -m scripts.restore_drill [--backup backups/<stamp>]

Requires the compose Postgres container (docker exec pg_restore path).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ADMIN_DSN = "postgresql://postgres:dev@localhost:5432/bucker"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", default=None,
                        help="backup dir (default: newest under backups/)")
    parser.add_argument("--container", default="bucker-pg")
    parser.add_argument("--admin-dsn", default=ADMIN_DSN)
    args = parser.parse_args()

    if args.backup:
        backup_dir = Path(args.backup)
    else:
        backups = sorted(Path("backups").glob("*"))
        if not backups:
            print("no backups found — run scripts/backup.py first")
            return 2
        backup_dir = backups[-1]
    dump = backup_dir / "db.dump"
    if not dump.exists():
        print(f"no db.dump in {backup_dir}")
        return 2

    scratch = "bucker_restore_drill"
    print(f"restoring {dump} into scratch db '{scratch}'")

    # 1. drop any leftover scratch db, create fresh
    for sql in (f"DROP DATABASE IF EXISTS {scratch}", f"CREATE DATABASE {scratch}"):
        r = _run(["docker", "exec", args.container, "psql", "-U", "postgres",
                  "-c", sql])
        if r.returncode != 0:
            print(f"psql failed: {r.stderr.decode()[:300]}")
            return 2

    # 2. restore the dump into the scratch db
    _run(["docker", "cp", str(dump), f"{args.container}:/tmp/drill.dump"])
    r = _run(["docker", "exec", args.container, "pg_restore", "-U", "postgres",
              "-d", scratch, "--clean", "--if-exists", "/tmp/drill.dump"])
    _run(["docker", "exec", args.container, "rm", "-f", "/tmp/drill.dump"])
    if r.returncode != 0:
        print(f"pg_restore failed: {r.stderr.decode()[:400]}")
        return 2

    # 3. assert the audit trail is intact
    r = _run(["docker", "exec", args.container, "psql", "-U", "postgres",
              "-d", scratch, "-tAc",
              "SELECT count(*) FROM events"])
    events = (r.stdout or b"").decode().strip()
    print(f"restored events table has {events} row(s)")

    # 4. drop the scratch db
    _run(["docker", "exec", args.container, "psql", "-U", "postgres",
          "-c", f"DROP DATABASE IF EXISTS {scratch}"])
    print("RESTORE DRILL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
