"""Geography + RTT estimation for city-aware scoring.

Approach C (hybrid): each model's TTFT (pure inference time) is stored/derived
separately from network RTT. Effective latency from a chosen city is
    ttft_ms + estimate_rtt_ms(city, model.primary_region)

The RTT table is *estimated* from great-circle distance, not measured. Real
internet paths can deviate 30–50 % (peering, undersea cables, BGP). Transpacific
RTTs in particular will undershoot reality. This is honest guidance for ranking,
not an SLA.

Calibration: 0.015 ms per km + 5 ms local last-mile ⇒
    Vilnius → Frankfurt ≈ 23 ms (measured ~30)
    Vilnius → London    ≈ 32 ms (measured ~30)
    Vilnius → NYC       ≈ 110 ms (measured ~110)
    Vilnius → SF        ≈ 145 ms (measured ~155)
"""

import json
import math
from pathlib import Path

# ── Region clusters (UI grouping; iteration order = display order) ───────────

REGION_CLUSTERS = {
    "eu-north-east": {"label": "EU North-East",           "flag": "🇱🇹"},
    "eu-central":    {"label": "EU Central",              "flag": "🇩🇪"},
    "eu-west":       {"label": "EU West",                 "flag": "🇳🇱"},
    "eu-south":      {"label": "EU South",                "flag": "🇪🇸"},
    "us-east":       {"label": "US East",                 "flag": "🇺🇸"},
    "us-west":       {"label": "US West",                 "flag": "🇺🇸"},
    "na-secondary":  {"label": "North America (Secondary)", "flag": "🇨🇦"},
    "middle-east":   {"label": "Middle East",             "flag": "🇦🇪"},
    "apac-core":     {"label": "APAC Core",               "flag": "🇸🇬"},
    "apac-east":     {"label": "APAC East",               "flag": "🇯🇵"},
    "apac-south":    {"label": "APAC South",              "flag": "🇮🇳"},
    "oceania":       {"label": "Oceania",                 "flag": "🇦🇺"},
    "latam":         {"label": "Latin America",           "flag": "🇧🇷"},
}


# ── Cities users can pick ────────────────────────────────────────────────────

CITIES = {
    # EU North-East
    "vilnius":      {"label": "Vilnius",       "flag": "🇱🇹", "lat":  54.69, "lon":   25.28, "cluster": "eu-north-east"},
    "riga":         {"label": "Riga",          "flag": "🇱🇻", "lat":  56.95, "lon":   24.11, "cluster": "eu-north-east"},
    "warsaw":       {"label": "Warsaw",        "flag": "🇵🇱", "lat":  52.23, "lon":   21.01, "cluster": "eu-north-east"},
    "stockholm":    {"label": "Stockholm",     "flag": "🇸🇪", "lat":  59.33, "lon":   18.06, "cluster": "eu-north-east"},
    # EU Central
    "berlin":       {"label": "Berlin",        "flag": "🇩🇪", "lat":  52.52, "lon":   13.40, "cluster": "eu-central"},
    "frankfurt":    {"label": "Frankfurt",     "flag": "🇩🇪", "lat":  50.11, "lon":    8.68, "cluster": "eu-central"},
    "zurich":       {"label": "Zurich",        "flag": "🇨🇭", "lat":  47.38, "lon":    8.54, "cluster": "eu-central"},
    # EU West
    "amsterdam":    {"label": "Amsterdam",     "flag": "🇳🇱", "lat":  52.37, "lon":    4.89, "cluster": "eu-west"},
    "london":       {"label": "London",        "flag": "🇬🇧", "lat":  51.51, "lon":   -0.13, "cluster": "eu-west"},
    "dublin":       {"label": "Dublin",        "flag": "🇮🇪", "lat":  53.35, "lon":   -6.26, "cluster": "eu-west"},
    "paris":        {"label": "Paris",         "flag": "🇫🇷", "lat":  48.86, "lon":    2.35, "cluster": "eu-west"},
    # EU South
    "madrid":       {"label": "Madrid",        "flag": "🇪🇸", "lat":  40.42, "lon":   -3.70, "cluster": "eu-south"},
    "barcelona":    {"label": "Barcelona",     "flag": "🇪🇸", "lat":  41.39, "lon":    2.16, "cluster": "eu-south"},
    "milan":        {"label": "Milan",         "flag": "🇮🇹", "lat":  45.46, "lon":    9.19, "cluster": "eu-south"},
    # North America
    "nyc":          {"label": "New York",      "flag": "🇺🇸", "lat":  40.71, "lon":  -74.00, "cluster": "us-east"},
    "sanfrancisco": {"label": "San Francisco", "flag": "🇺🇸", "lat":  37.77, "lon": -122.42, "cluster": "us-west"},
    "toronto":      {"label": "Toronto",       "flag": "🇨🇦", "lat":  43.65, "lon":  -79.38, "cluster": "na-secondary"},
    # Middle East
    "telaviv":      {"label": "Tel Aviv",      "flag": "🇮🇱", "lat":  32.08, "lon":   34.78, "cluster": "middle-east"},
    "dubai":        {"label": "Dubai",         "flag": "🇦🇪", "lat":  25.20, "lon":   55.27, "cluster": "middle-east"},
    # APAC
    "singapore":    {"label": "Singapore",     "flag": "🇸🇬", "lat":   1.35, "lon":  103.82, "cluster": "apac-core"},
    "tokyo":        {"label": "Tokyo",         "flag": "🇯🇵", "lat":  35.68, "lon":  139.69, "cluster": "apac-east"},
    "seoul":        {"label": "Seoul",         "flag": "🇰🇷", "lat":  37.57, "lon":  126.98, "cluster": "apac-east"},
    "beijing":      {"label": "Beijing",       "flag": "🇨🇳", "lat":  39.90, "lon":  116.41, "cluster": "apac-east"},
    "bangalore":    {"label": "Bangalore",     "flag": "🇮🇳", "lat":  12.97, "lon":   77.59, "cluster": "apac-south"},
    # Oceania + LatAm
    "sydney":       {"label": "Sydney",        "flag": "🇦🇺", "lat": -33.87, "lon":  151.21, "cluster": "oceania"},
    "saopaulo":     {"label": "São Paulo",     "flag": "🇧🇷", "lat": -23.55, "lon":  -46.63, "cluster": "latam"},
}
DEFAULT_CITY = "vilnius"


# ── Canonical inference regions (datacenter hub coordinates) ─────────────────

REGIONS = {
    "us-east":    {"label": "US East (Virginia)",     "lat": 39.04, "lon":  -77.49},
    "us-west":    {"label": "US West (Oregon)",       "lat": 45.52, "lon": -122.67},
    "eu-west":    {"label": "EU West (Dublin)",       "lat": 53.35, "lon":   -6.26},
    "eu-west3":   {"label": "EU West 3 (Paris)",      "lat": 48.86, "lon":    2.35},
    "eu-central": {"label": "EU Central (Frankfurt)", "lat": 50.11, "lon":    8.68},
    "eu-north":   {"label": "EU North (Stockholm)",   "lat": 59.33, "lon":   18.06},
    "cn-east":    {"label": "China East (Shanghai)",  "lat": 31.23, "lon":  121.47},
    "cn-beijing": {"label": "China North (Beijing)",  "lat": 39.90, "lon":  116.41},
}

MULTI_REGION = "multi"

# Map raw provider-declared region strings → canonical REGIONS keys.
_REGION_ALIASES = {
    "us-east-1":     "us-east",
    "us-east":       "us-east",
    "us-west":       "us-west",
    "us-west-2":     "us-west",
    "eu-west1":      "eu-west",
    "eu-west":       "eu-west",
    "eu-west3":      "eu-west3",
    "eu-central-1":  "eu-central",
    "swedencentral": "eu-north",
    "cn-east":       "cn-east",
    "cn-hangzhou":   "cn-east",
    "cn-beijing":    "cn-beijing",
}


def canonical_region(region_str: str) -> str:
    """Resolve a provider region string to a canonical key or MULTI_REGION."""
    if region_str in REGIONS:
        return region_str
    return _REGION_ALIASES.get(region_str, MULTI_REGION)


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── Measured RTT table (populated by tools/run_probes.py) ────────────────────

_MEASURED_PATH = Path(__file__).parent / "rtt_measured.json"
_MEASURED_RTT: dict = {}
_MEASURED_MTIME: float = 0.0


def _load_measured_rtt() -> dict:
    if not _MEASURED_PATH.exists():
        return {}
    try:
        data = json.loads(_MEASURED_PATH.read_text(encoding="utf-8"))
        return data.get("rtts", {}) or {}
    except Exception:
        return {}


def _measured_rtt() -> dict:
    """Return the measured RTT table, reloading when the JSON file changes.

    tools/run_probes.py rewrites rtt_measured.json out-of-band. Without an
    mtime check, a running container would never see the new values until
    restart. We stat() once per call — cheap, and stat is cached by the
    kernel anyway.
    """
    global _MEASURED_RTT, _MEASURED_MTIME
    try:
        mtime = _MEASURED_PATH.stat().st_mtime
    except FileNotFoundError:
        if _MEASURED_RTT:
            _MEASURED_RTT = {}
            _MEASURED_MTIME = 0.0
        return _MEASURED_RTT
    if mtime != _MEASURED_MTIME:
        _MEASURED_RTT = _load_measured_rtt()
        _MEASURED_MTIME = mtime
    return _MEASURED_RTT


# Warm at import so the first request doesn't pay the stat+read cost.
_measured_rtt()


def rtt_is_measured(city_id: str, region_str: str) -> bool:
    measured = _measured_rtt()
    rkey = canonical_region(region_str)
    if rkey == MULTI_REGION:
        return bool(measured.get(city_id))
    return measured.get(city_id, {}).get(rkey) is not None


def estimate_rtt_ms(city_id: str, region_str: str) -> int:
    """RTT estimate between a city and a model's region.

    Prefers measured values from rtt_measured.json (RIPE Atlas), falls back
    to haversine great-circle estimate per missing (city, region) pair.

    For region == 'multi' (provider routes to nearest datacenter / uses anycast),
    we return the min RTT across all canonical regions — measured where
    available, haversine where not.
    """
    city = CITIES.get(city_id) or CITIES[DEFAULT_CITY]
    rkey = canonical_region(region_str)

    if rkey == MULTI_REGION:
        return min(_rtt_for(city_id, city, r_key) for r_key in REGIONS)

    return _rtt_for(city_id, city, rkey)


def _rtt_for(city_id: str, city: dict, rkey: str) -> int:
    measured = _measured_rtt().get(city_id, {}).get(rkey)
    if measured is not None:
        return int(measured)
    return _rtt_to_region(city, REGIONS[rkey])


def _rtt_to_region(city: dict, region: dict) -> int:
    dist = _haversine_km(city["lat"], city["lon"], region["lat"], region["lon"])
    return int(round(dist * 0.015 + 5))
