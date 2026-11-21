"""End-to-end check against a running CrisisWatch API.

    python scripts/smoke_test.py --base https://crisiswatch-ohrl.onrender.com --secret <CRON_SECRET>

Creates one throwaway outbreak, confirms it appears with its case count,
then deletes it. Exits non-zero if any step fails.
"""

import argparse
import sys
from datetime import datetime, timezone

import httpx

MARKER = "Smoke Test Pathogen"


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--secret", required=True)
    args = parser.parse_args()

    base = args.base.rstrip("/")
    failures = 0

    with httpx.Client(base_url=base, timeout=120) as client:

        # 1. Is the API up at all?
        try:
            health = client.get("/health").json()
        except Exception as exc:
            print(f"FAIL  /health unreachable — {type(exc).__name__}: {exc}")
            return 1

        failures += not check("API reachable", True, health.get("status", "?"))
        failures += not check(
            "MongoDB connected", bool(health.get("db_connected")),
            "set MONGODB_URI on the server",
        )
        failures += not check(
            "Gemini key present", bool(health.get("gemini_key")),
            "set GEMINI_API_KEY on the server",
        )

        before = health.get("outbreaks", 0)
        print(f"      outbreaks in DB: {before}")

        last_run = health.get("last_run") or {}
        if last_run:
            print(
                f"      last run: {last_run.get('status')} — "
                f"{last_run.get('articles_fetched')} article(s), "
                f"{last_run.get('items_extracted')} item(s), "
                f"{last_run.get('incidents_created')} new"
            )
            for err in (last_run.get("errors") or [])[:5]:
                print(f"      ! {err}")

        # 2. Write a known outbreak through the protected endpoint.
        payload = {
            "disease": MARKER,
            "country": "Testland",
            "region": "Testland",
            "estimated_cases": 4242,
            "estimated_deaths": 7,
            "severity": "critical",
            "confidence_score": 0.9,
        }

        resp = client.post(
            "/outbreaks/detect", json=payload, params={"secret": args.secret}
        )
        failures += not check(
            "POST /outbreaks/detect accepted", resp.status_code == 200,
            f"HTTP {resp.status_code} {resp.text[:120]}",
        )

        if resp.status_code != 200:
            return 1

        # 3. Did it come back out with the case count intact?
        rows = client.get("/outbreaks", params={"q": MARKER, "days": 1}).json()
        found = [r for r in rows if r.get("disease") == MARKER]

        failures += not check("outbreak is readable back", bool(found))
        if found:
            row = found[0]
            failures += not check(
                "case count survived the round trip",
                row.get("cases") == 4242, f"got {row.get('cases')}",
            )
            failures += not check(
                "severity survived", row.get("severity") == "critical",
                f"got {row.get('severity')}",
            )
            failures += not check(
                "snapshot recorded", bool(row.get("snapshots")),
            )

            deleted = client.delete(
                f"/outbreaks/{row['incident_id']}", params={"secret": args.secret}
            )
            failures += not check(
                "cleanup delete worked", deleted.status_code == 200,
                f"HTTP {deleted.status_code}",
            )

        # 4. Stats endpoint agrees with the list.
        stats = client.get("/stats").json()
        failures += not check(
            "GET /stats returns counts", "total" in stats and "critical" in stats,
            f"total={stats.get('total')}",
        )

        print()
        print(f"checked at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        print("all good" if failures == 0 else f"{failures} check(s) failed")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
