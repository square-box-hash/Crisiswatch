"""CLI wrapper around migrate.py.

    python scripts/migrate_v5.py          # dry count
    python scripts/migrate_v5.py --yes    # backfill, merge duplicates, indexes
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from migrate import migrate  # noqa: E402


def main() -> int:
    if db.outbreaks is None:
        print(f"Database unavailable — {db.connection_error()}")
        return 1

    if "--yes" not in sys.argv:
        print(f"{db.outbreaks.count_documents({})} document(s) would be backfilled. "
              "Re-run with --yes.")
        return 2

    result = migrate(db.outbreaks)
    print(
        f"scanned {result['scanned']}, updated {result['updated']}, "
        f"merged {result['merged']} group(s), deleted {result['deleted']} duplicate(s)"
    )

    db.ensure_indexes()
    print("indexes created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
