"""Run RIPE Atlas ICMP-ping measurements from probes near each user city to
canonical region hubs, then write median RTTs to app/data/rtt_measured.json.

The dashboard's app/data/geo.py prefers these measured numbers over its
haversine estimate on a per-(city, region) basis, falling back when missing.

Usage:
    export RIPE_ATLAS_KEY=...                     # scope: create measurement
    python -m tools.run_probes --dry              # show what would run, estimate credits
    python -m tools.run_probes                    # full refresh (all cities × regions)
    python -m tools.run_probes --city vilnius     # single city (debugging)
    python -m tools.run_probes --region eu-central --city vilnius
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

import httpx
from dotenv import load_dotenv

# Make `app.*` importable when invoked as a script or as `-m tools.run_probes`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.data.geo import CITIES, REGIONS  # noqa: E402

API = "https://atlas.ripe.net/api/v2"
PROBES_PER_CITY = 3
PACKETS = 3
SEARCH_RADIUS_KM = 500
CREDIT_COST_PER_PROBE = 10  # ICMP ping: 10 credits per probe-measurement
POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 300

# One representative hostname per canonical region. AWS regional API endpoints
# accept TCP:443 and (mostly) respond to ICMP. CN endpoints are best-effort —
# Alibaba for cn-east since AWS lacks Shanghai.
TARGETS = {
    "us-east":    "ec2.us-east-1.amazonaws.com",
    "us-west":    "ec2.us-west-2.amazonaws.com",
    "eu-west":    "ec2.eu-west-1.amazonaws.com",
    "eu-west3":   "ec2.eu-west-3.amazonaws.com",
    "eu-central": "ec2.eu-central-1.amazonaws.com",
    "eu-north":   "ec2.eu-north-1.amazonaws.com",
    "cn-east":    "ecs.cn-shanghai.aliyuncs.com",
    "cn-beijing": "ec2.cn-north-1.amazonaws.com.cn",
}

OUTPUT_PATH = ROOT / "app" / "data" / "rtt_measured.json"


# ── Atlas API helpers ────────────────────────────────────────────────────────

def _auth_headers(key: str) -> dict:
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def find_probes_near(client: httpx.Client, city_id: str, city: dict, n: int) -> list[int]:
    """Return up to n connected public probe IDs near the city."""
    r = client.get(
        f"{API}/probes/",
        params={
            "radius": f"{city['lat']},{city['lon']}:{SEARCH_RADIUS_KM}",
            "status_name": "Connected",
            "is_public": "true",
            "page_size": 30,
        },
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    # Results aren't strictly distance-ordered; sort by haversine.
    from math import radians, sin, cos, asin, sqrt

    def _dist(p):
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            return 1e9
        phi1, phi2 = radians(city["lat"]), radians(lat)
        dphi = radians(lat - city["lat"])
        dl = radians(lon - city["lon"])
        a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dl / 2) ** 2
        return 2 * 6371.0 * asin(sqrt(a))

    results.sort(key=_dist)
    return [p["id"] for p in results[:n]]


def create_measurement(client: httpx.Client, target: str, probe_ids: list[int], description: str) -> int:
    body = {
        "definitions": [{
            "target": target,
            "type": "ping",
            "af": 4,
            "packets": PACKETS,
            "description": description,
        }],
        "probes": [{
            "type": "probes",
            "value": ",".join(str(p) for p in probe_ids),
            "requested": len(probe_ids),
        }],
        "is_oneoff": True,
    }
    r = client.post(f"{API}/measurements/", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Atlas create_measurement failed {r.status_code}: {r.text}")
    return r.json()["measurements"][0]


def fetch_results(client: httpx.Client, measurement_id: int) -> list[dict]:
    r = client.get(f"{API}/measurements/{measurement_id}/results/")
    r.raise_for_status()
    return r.json()


def wait_for_results(client: httpx.Client, measurement_id: int, expected: int) -> list[dict]:
    """Poll until results arrive for all probes or timeout."""
    deadline = time.time() + POLL_TIMEOUT_S
    last = []
    while time.time() < deadline:
        last = fetch_results(client, measurement_id)
        if len(last) >= expected:
            return last
        time.sleep(POLL_INTERVAL_S)
    return last  # return whatever we got


# ── Orchestration ────────────────────────────────────────────────────────────

def probe_rtt_from_result(result: dict) -> float | None:
    """Median RTT (ms) across successful pings in one probe result, or None."""
    rtts = [p["rtt"] for p in result.get("result", []) if isinstance(p, dict) and "rtt" in p]
    return median(rtts) if rtts else None


def main():
    parser = argparse.ArgumentParser(description="RIPE Atlas probe runner")
    parser.add_argument("--dry", action="store_true", help="Plan only, don't create measurements")
    parser.add_argument("--city", help="Restrict to one city id")
    parser.add_argument("--region", help="Restrict to one canonical region key")
    parser.add_argument("--probes-per-city", type=int, default=PROBES_PER_CITY)
    args = parser.parse_args()

    key = os.environ.get("RIPE_ATLAS_KEY")
    if not key and not args.dry:
        sys.exit("RIPE_ATLAS_KEY env var not set")

    cities = {k: v for k, v in CITIES.items() if (not args.city or k == args.city)}
    targets = {k: v for k, v in TARGETS.items() if (not args.region or k == args.region)}
    if not cities:
        sys.exit(f"No matching city: {args.city}")
    if not targets:
        sys.exit(f"No matching region: {args.region}")

    client = httpx.Client(
        timeout=httpx.Timeout(30.0, read=60.0),
        headers=_auth_headers(key) if key else {},
    )

    # Step 1 — pick probes per city
    print(f"Finding {args.probes_per_city} probes per city across {len(cities)} cities…")
    city_probes: dict[str, list[int]] = {}
    for city_id, city in cities.items():
        if args.dry and not key:
            city_probes[city_id] = []  # unknown; cost estimate uses the requested count
            continue
        try:
            probes = find_probes_near(client, city_id, city, args.probes_per_city)
        except Exception as e:
            print(f"  {city_id}: probe lookup failed ({e})")
            probes = []
        city_probes[city_id] = probes
        print(f"  {city_id:12s} → probes {probes or '(none found)'}")

    # Collect union of probe IDs (one measurement hits many probes at once).
    all_probes = sorted({p for ps in city_probes.values() for p in ps})
    if not all_probes and not args.dry:
        sys.exit("No probes discovered — check Atlas API key or SEARCH_RADIUS_KM")

    total_probe_measurements = len(all_probes) * len(targets) or (
        args.probes_per_city * len(cities) * len(targets)
    )
    est_credits = total_probe_measurements * CREDIT_COST_PER_PROBE * PACKETS / PACKETS  # per probe
    print(
        f"\nPlanned: {len(all_probes)} unique probes × {len(targets)} targets = "
        f"{total_probe_measurements} probe-measurements · ~{est_credits:,.0f} credits"
    )

    if args.dry:
        print("Dry run — exiting before creating measurements.")
        return

    # Step 2 — one measurement per target, all probes in one shot
    measurement_ids: dict[str, int] = {}
    for rkey, target in targets.items():
        mid = create_measurement(
            client, target, all_probes,
            description=f"llm-price-runner {rkey} {datetime.now(timezone.utc).date().isoformat()}",
        )
        measurement_ids[rkey] = mid
        print(f"  created measurement {mid} → {rkey} ({target})")

    # Step 3 — poll results
    all_results: dict[str, dict[int, float]] = {}  # rkey → {probe_id: rtt_ms}
    for rkey, mid in measurement_ids.items():
        print(f"Waiting for results of {mid} ({rkey})…")
        raw = wait_for_results(client, mid, expected=len(all_probes))
        per_probe: dict[int, float] = {}
        for row in raw:
            pid = row.get("prb_id")
            rtt = probe_rtt_from_result(row)
            if pid is not None and rtt is not None:
                per_probe[pid] = rtt
        print(f"  {rkey}: {len(per_probe)}/{len(all_probes)} probes returned RTT")
        all_results[rkey] = per_probe

    # Step 4 — aggregate per city (min across that city's probes; min reflects the
    # best achievable path, which is what we'd route over).
    rtts: dict[str, dict[str, int]] = {}
    for city_id, probes in city_probes.items():
        if not probes:
            continue
        city_entry: dict[str, int] = {}
        for rkey, per_probe in all_results.items():
            vals = [per_probe[p] for p in probes if p in per_probe]
            if vals:
                city_entry[rkey] = int(round(min(vals)))
        if city_entry:
            rtts[city_id] = city_entry

    # Step 5 — write alongside existing rtt_measured.json (if any)
    existing: dict = {}
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text())
        except Exception:
            existing = {}
    merged = {**existing.get("rtts", {})}
    for city_id, data in rtts.items():
        merged.setdefault(city_id, {}).update(data)

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ripe-atlas-icmp-ping",
        "probes_per_city": args.probes_per_city,
        "targets": {k: v for k, v in TARGETS.items() if k in targets or k in merged.get("_meta", {}).get("targets", {})},
        "rtts": merged,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {OUTPUT_PATH.relative_to(ROOT)} ({len(merged)} cities)")


if __name__ == "__main__":
    main()
