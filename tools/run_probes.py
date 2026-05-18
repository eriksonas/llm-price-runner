"""Run RIPE Atlas ICMP-ping measurements from probes near each user city to
canonical region hubs, then write median RTTs to app/data/rtt_measured.json.

The dashboard's app/data/geo.py prefers these measured numbers over its
haversine estimate on a per-(city, region) basis, falling back when missing.

Caveat: Atlas only offers ICMP for ping measurements. Targets that
firewall ICMP (some cloud endpoints) will return no RTT for those probes;
the city entry will simply lack that region and the dashboard falls back
to haversine. To investigate a missing region, run with --city <id>
--region <key> and inspect the per-probe summary.

Usage:
    export RIPE_ATLAS_KEY=...                     # scope: create measurement
    python -m tools.run_probes --dry              # show what would run, estimate credits
    python -m tools.run_probes                    # full refresh (all cities × regions)
    python -m tools.run_probes --city vilnius     # single city (debugging)
    python -m tools.run_probes --region eu-central --city vilnius
    python -m tools.run_probes --target eu-central=storage.googleapis.com
                                                  # measure Google Cloud's Frankfurt
                                                  # endpoint instead of AWS
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

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
CREDIT_COST_PER_PROBE = 10  # ICMP ping: 10 credits per probe-result
POLL_INTERVAL_S = 15
POLL_TIMEOUT_S = 300

# Transient-error retry budget for any single Atlas API call. The Atlas
# API throws occasional 5xx during high traffic; a tiny backoff prevents
# a one-shot blip from aborting a run mid-flight (credits already spent
# on launched measurements would otherwise be wasted).
RETRY_MAX_ATTEMPTS = 4
RETRY_BACKOFF_S = (1, 3, 8)  # waited between attempts 1→2, 2→3, 3→4

# One representative hostname per canonical region.
#
# S3 region endpoints chosen for us-west / eu-west / eu-west3 / cn-beijing
# because AWS's EC2 control-plane endpoints (ec2.<region>.amazonaws.com)
# stopped responding to ICMP from Atlas probes globally in mid-May 2026.
# S3 data-plane endpoints live in the same datacenter on different LB
# fleet and still accept ICMP. Verified empirically by switching one
# region at a time and comparing RTTs against the EC2 baseline before
# the policy change — values matched within ~4%.
#
# EC2 endpoints retained for us-east / eu-central / eu-north / cn-east
# (cn-east via Alibaba) because they still respond.
TARGETS = {
    "us-east":    "ec2.us-east-1.amazonaws.com",
    "us-west":    "s3.us-west-2.amazonaws.com",
    "eu-west":    "s3.eu-west-1.amazonaws.com",
    "eu-west3":   "s3.eu-west-3.amazonaws.com",
    "eu-central": "ec2.eu-central-1.amazonaws.com",
    "eu-north":   "ec2.eu-north-1.amazonaws.com",
    "cn-east":    "ecs.cn-shanghai.aliyuncs.com",
    "cn-beijing": "s3.cn-north-1.amazonaws.com.cn",
}

OUTPUT_PATH = ROOT / "app" / "data" / "rtt_measured.json"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _auth_headers(key: str) -> dict:
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dl = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dl / 2) ** 2
    return 2 * 6371.0 * asin(sqrt(a))


def _request_with_retry(method: str, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
    """Issue an HTTP request with retry on transient errors.

    Retries 5xx, 429, and network exceptions; surfaces 4xx (other than
    429) immediately since those are caller bugs, not transient blips.
    """
    last_exc = None
    for attempt in range(RETRY_MAX_ATTEMPTS):
        try:
            r = client.request(method, url, **kwargs)
        except (httpx.RequestError, httpx.TransportError) as e:
            last_exc = e
            r = None
        else:
            if r.status_code < 500 and r.status_code != 429:
                return r
            last_exc = RuntimeError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
        if attempt + 1 < RETRY_MAX_ATTEMPTS:
            wait = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
            print(f"  retry in {wait}s: {last_exc}")
            time.sleep(wait)
    raise last_exc if last_exc else RuntimeError(f"All {RETRY_MAX_ATTEMPTS} attempts failed for {url}")


# ── Atlas API helpers ────────────────────────────────────────────────────────

def find_probes_near(client: httpx.Client, city_id: str, city: dict, n: int) -> list[tuple[int, float]]:
    """Return up to n connected public (probe_id, distance_km) pairs near the city."""
    r = _request_with_retry(
        "GET", client, f"{API}/probes/",
        params={
            "radius": f"{city['lat']},{city['lon']}:{SEARCH_RADIUS_KM}",
            "status_name": "Connected",
            "is_public": "true",
            "page_size": 30,
        },
    )
    r.raise_for_status()
    results = r.json().get("results", [])

    def _dist(p):
        # Atlas returns probe coordinates as GeoJSON: geometry.coordinates[lon, lat].
        # Some older payloads also expose flat latitude/longitude; fall back to those.
        coords = (p.get("geometry") or {}).get("coordinates") or []
        if len(coords) >= 2:
            lon, lat = coords[0], coords[1]
        else:
            lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            return 1e9
        return _haversine_km(city["lat"], city["lon"], lat, lon)

    sorted_probes = sorted(results, key=_dist)
    return [(p["id"], _dist(p)) for p in sorted_probes[:n]]


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
    r = _request_with_retry("POST", client, f"{API}/measurements/", json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Atlas create_measurement failed {r.status_code}: {r.text}")
    return r.json()["measurements"][0]


def fetch_results(client: httpx.Client, measurement_id: int) -> list[dict]:
    r = _request_with_retry("GET", client, f"{API}/measurements/{measurement_id}/results/")
    r.raise_for_status()
    return r.json()


def poll_all_measurements(
    client: httpx.Client,
    measurement_ids: dict[str, int],
    expected_per_measurement: int,
) -> dict[str, dict[int, float]]:
    """Poll every active measurement in round-robin until each completes or
    the global deadline is reached, then return per-region probe→RTT maps.

    Replaces the previous serial wait — that took up to len(regions) ×
    POLL_TIMEOUT_S worst case (~40 min for 8 regions). Atlas runs the
    measurements in parallel on its backend; the only reason to serialize
    is laziness in the client, so we don't.
    """
    pending = dict(measurement_ids)
    results: dict[str, dict[int, float]] = {}
    deadline = time.time() + POLL_TIMEOUT_S

    while pending and time.time() < deadline:
        completed_this_round = []
        for rkey, mid in pending.items():
            raw = fetch_results(client, mid)
            if len(raw) >= expected_per_measurement:
                results[rkey] = _parse_probe_rtts(raw)
                completed_this_round.append(rkey)
                print(f"  {rkey}: complete — {len(results[rkey])}/{expected_per_measurement} probes returned RTT")
        for rkey in completed_this_round:
            del pending[rkey]
        if pending:
            time.sleep(POLL_INTERVAL_S)

    # Anything still pending at deadline: take whatever we got.
    for rkey, mid in pending.items():
        raw = fetch_results(client, mid)
        results[rkey] = _parse_probe_rtts(raw)
        print(f"  {rkey}: partial after timeout — {len(results[rkey])}/{expected_per_measurement} probes returned RTT")
    return results


def _parse_probe_rtts(raw: list[dict]) -> dict[int, float]:
    """Convert an Atlas /results/ payload into {probe_id: min_rtt_ms}."""
    out: dict[int, float] = {}
    for row in raw:
        pid = row.get("prb_id")
        rtt = probe_rtt_from_result(row)
        if pid is not None and rtt is not None:
            out[pid] = rtt
    return out


# ── Orchestration ────────────────────────────────────────────────────────────

def probe_rtt_from_result(result: dict) -> float | None:
    """Best (min) RTT (ms) across successful pings in one probe result, or None.

    `min` is consistent with the city-level aggregation downstream and
    is more stable than median when only 3 packets are sent (one high
    outlier shifts a 3-packet median significantly).
    """
    rtts = [p["rtt"] for p in result.get("result", []) if isinstance(p, dict) and "rtt" in p]
    return min(rtts) if rtts else None


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON to a temp file in the same dir, then os.replace().

    Crash-resilient: a Ctrl-C or OOM mid-write leaves the original file
    intact, which matters because the running dashboard reloads this
    file on mtime change and a corrupted parse would zero out all
    measured RTTs.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description="RIPE Atlas probe runner")
    parser.add_argument("--dry", action="store_true", help="Plan only, don't create measurements")
    parser.add_argument("--city", help="Restrict to one city id")
    parser.add_argument("--region", help="Restrict to one canonical region key")
    parser.add_argument("--probes-per-city", type=int, default=PROBES_PER_CITY)
    parser.add_argument(
        "--target", action="append", default=[], metavar="REGION=HOSTNAME",
        help=(
            "Override the target hostname for a canonical region. Repeatable. "
            "Example: --target eu-central=storage.googleapis.com to measure "
            "Google Cloud's Frankfurt endpoint instead of the default AWS host."
        ),
    )
    args = parser.parse_args()

    key = (os.environ.get("RIPE_ATLAS_KEY") or "").strip()
    if not key and not args.dry:
        sys.exit("RIPE_ATLAS_KEY env var not set")

    cities = {k: v for k, v in CITIES.items() if (not args.city or k == args.city)}
    targets = {k: v for k, v in TARGETS.items() if (not args.region or k == args.region)}
    if not cities:
        sys.exit(f"No matching city: {args.city}")
    if not targets:
        sys.exit(f"No matching region: {args.region}")

    # Apply --target REGION=HOSTNAME overrides on top of the default map.
    for spec in args.target:
        if "=" not in spec:
            sys.exit(f"--target must be REGION=HOSTNAME, got: {spec!r}")
        rkey, hostname = spec.split("=", 1)
        rkey, hostname = rkey.strip(), hostname.strip()
        if rkey not in targets:
            sys.exit(
                f"--target region {rkey!r} not in active set {sorted(targets)}. "
                f"(If you're using --region, the override must match that region.)"
            )
        if not hostname:
            sys.exit(f"--target {rkey}= has empty hostname")
        targets[rkey] = hostname
        print(f"Override: {rkey} → {hostname}")

    with httpx.Client(
        timeout=httpx.Timeout(30.0, read=60.0),
        headers=_auth_headers(key) if key else {},
    ) as client:
        # Step 1 — pick probes per city, recording distance for visibility.
        print(f"Finding {args.probes_per_city} probes per city across {len(cities)} cities…")
        city_probes: dict[str, list[int]] = {}
        empty_cities: list[str] = []
        for city_id, city in cities.items():
            if args.dry and not key:
                city_probes[city_id] = []  # unknown; cost estimate uses the requested count
                continue
            try:
                probes_with_dist = find_probes_near(client, city_id, city, args.probes_per_city)
            except Exception as e:
                print(f"  {city_id}: probe lookup failed ({e})")
                probes_with_dist = []
            if probes_with_dist:
                pretty = ", ".join(f"{pid} ({dist:.0f}km)" for pid, dist in probes_with_dist)
                print(f"  {city_id:12s} → {pretty}")
                city_probes[city_id] = [pid for pid, _ in probes_with_dist]
            else:
                print(f"  {city_id:12s} → (none found within {SEARCH_RADIUS_KM} km)")
                city_probes[city_id] = []
                empty_cities.append(city_id)

        # Collect union of probe IDs (one measurement hits many probes at once).
        all_probes = sorted({p for ps in city_probes.values() for p in ps})
        if not all_probes and not args.dry:
            sys.exit("No probes discovered — check Atlas API key or SEARCH_RADIUS_KM")

        total_probe_measurements = len(all_probes) * len(targets) or (
            args.probes_per_city * len(cities) * len(targets)
        )
        est_credits = total_probe_measurements * CREDIT_COST_PER_PROBE
        print(
            f"\nPlanned: {len(all_probes)} unique probes × {len(targets)} targets = "
            f"{total_probe_measurements} probe-measurements · ~{est_credits:,.0f} credits"
        )
        if empty_cities:
            print(f"Cities with no probes nearby (will be omitted from output): {', '.join(empty_cities)}")

        if args.dry:
            print("Dry run — exiting before creating measurements.")
            return

        # Step 2 — one measurement per target, all probes in one shot.
        measurement_ids: dict[str, int] = {}
        for rkey, target in targets.items():
            mid = create_measurement(
                client, target, all_probes,
                description=f"llm-price-runner {rkey} {datetime.now(timezone.utc).date().isoformat()}",
            )
            measurement_ids[rkey] = mid
            print(f"  created measurement {mid} → {rkey} ({target})")

        # Step 3 — poll all measurements in parallel (round-robin).
        print(f"Polling {len(measurement_ids)} measurements in parallel…")
        all_results = poll_all_measurements(
            client, measurement_ids, expected_per_measurement=len(all_probes),
        )

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

    if not rtts:
        sys.exit(
            "Refusing to write: every measurement returned zero RTTs. "
            "Existing rtt_measured.json left intact. Investigate the Atlas measurement "
            "summaries above (probable causes: ICMP-firewalled targets, all probes offline, "
            "or an API-key scope problem)."
        )

    # Step 5 — merge with existing rtt_measured.json (if any) and atomically replace.
    existing: dict = {}
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text())
        except Exception:
            existing = {}
    merged = {**existing.get("rtts", {})}
    for city_id, data in rtts.items():
        merged.setdefault(city_id, {}).update(data)

    # Preserve the full set of known targets across partial runs — union of the
    # previous file's targets, this run's targets, and TARGETS itself.
    saved_targets = {**existing.get("targets", {}), **targets}

    payload = {
        "last_updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ripe-atlas-icmp-ping",
        "probes_per_city": args.probes_per_city,
        "targets": saved_targets,
        "rtts": merged,
    }
    _atomic_write_json(OUTPUT_PATH, payload)
    summary = f"\nWrote {OUTPUT_PATH.relative_to(ROOT)} ({len(merged)} cities)"
    if empty_cities:
        summary += f"; {len(empty_cities)} cities skipped (no probes): {', '.join(empty_cities)}"
    print(summary)


if __name__ == "__main__":
    main()
