"""Backfill v4 documents into the v5 shape.

Old documents have no `incident_key`, no `last_seen` and no `snapshots`, so the
API (which filters on `last_seen`) would hide them. This:

  * derives `incident_key` = disease:country
  * merges documents that map to the same outbreak
  * builds a `snapshots` history from their `updated_at` + case counts
  * sets `first_seen`, `last_seen`, `peak_cases`

Importable (used by POST /admin/migrate) and runnable:
    python scripts/migrate_v5.py --yes
"""

from datetime import datetime, timezone

from store import incident_key


def _cases(doc: dict) -> int:
    signal = doc.get("cluster_signal") or {}
    for value in (
        doc.get("estimated_cases"),
        doc.get("cases"),
        signal.get("cases"),
    ):
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _deaths(doc: dict) -> int:
    signal = doc.get("cluster_signal") or {}
    for value in (
        doc.get("estimated_deaths"),
        doc.get("deaths"),
        signal.get("deaths"),
    ):
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def migrate(collection) -> dict:
    """Returns {"scanned", "merged", "deleted", "updated"}."""

    scanned = 0
    groups: dict[str, list] = {}

    for doc in collection.find({}):
        scanned += 1
        key = doc.get("incident_key") or incident_key(
            str(doc.get("disease") or "unknown"),
            str(doc.get("country") or doc.get("region") or "unknown"),
        )
        groups.setdefault(key, []).append(doc)

    merged = 0
    deleted = 0
    updated = 0

    for key, docs in groups.items():
        docs.sort(key=lambda d: _as_datetime(d.get("updated_at")), reverse=True)
        primary = docs[0]
        others = docs[1:]

        seen = {}
        for doc in docs:
            when = _as_datetime(doc.get("updated_at") or doc.get("created_at"))
            seen[when.strftime("%Y-%m-%d")] = {
                "date": when.strftime("%Y-%m-%d"),
                "cases": _cases(doc),
                "deaths": _deaths(doc),
                "confidence": float(doc.get("confidence_score") or 0),
                "severity": doc.get("severity") or "low",
                "source_url": (doc.get("source_urls") or [""])[0],
                "captured_at": when,
            }

        snapshots = [seen[d] for d in sorted(seen)]
        cases = max([_cases(d) for d in docs] or [0])
        first_seen = min(
            _as_datetime(d.get("created_at") or d.get("updated_at")) for d in docs
        )
        last_seen = max(
            _as_datetime(d.get("updated_at") or d.get("created_at")) for d in docs
        )

        update = {
            "incident_key": key,
            "disease": primary.get("disease"),
            "country": primary.get("country") or primary.get("region"),
            "estimated_cases": _cases(primary),
            "estimated_deaths": _deaths(primary),
            "peak_cases": cases,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "snapshots": snapshots,
        }

        if not primary.get("severity"):
            update["severity"] = "low"
        if not primary.get("verification_status"):
            update["verification_status"] = "unverified"

        collection.update_one({"_id": primary["_id"]}, {"$set": update})
        updated += 1

        if others:
            merged += 1
            collection.delete_many({"_id": {"$in": [d["_id"] for d in others]}})
            deleted += len(others)

    return {
        "scanned": scanned,
        "merged": merged,
        "deleted": deleted,
        "updated": updated,
    }
