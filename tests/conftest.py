"""Test fixtures.

Tests live alongside the app package; we add the project root to sys.path
so `from app.scoring import …` resolves without an install step.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _no_measured_rtt(monkeypatch):
    """Force the haversine RTT estimator in tests.

    geo._MEASURED_RTT is populated at import time from rtt_measured.json,
    which is checked in and changes whenever probes run. Pinning the
    table to empty for tests means scoring snapshots stay reproducible
    regardless of when probes last ran.
    """
    from app.data import geo

    monkeypatch.setattr(geo, "_MEASURED_RTT", {})
    monkeypatch.setattr(geo, "_MEASURED_MTIME", 0.0)
    monkeypatch.setattr(geo, "_measured_rtt", lambda: {})
