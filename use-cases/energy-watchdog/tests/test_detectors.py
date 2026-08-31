"""Deterministic detector tests against the committed fixture CSVs. No LLM, no network.

If one of these fails, the fix is in the detector logic or the fixtures - never in a threshold
(thresholds come from data/sites.yaml and are the spec).
"""

from __future__ import annotations

import contextlib
import io
import json

import pytest

import agent
import detectors

# Fixture calendar facts for 2026-08-25 (a declared closed date on a normally-working Tuesday).
FAULT_NIGHT_DAY = "2026-08-21"  # baseload_creep active, working day
FAULT_SOLAR_DAY = "2026-08-24"  # solar_string_failure active, working day
FAULT_HOLIDAY_DAY = "2026-08-25"  # holiday_load, closed date -> off-schedule load
CLEAN_WORKING_DAY = "2026-08-18"
CLEAN_CLOSED_DAY = "2026-08-23"  # a Sunday, genuinely quiet


def _candidates(faulty_colombo, colombo, date):
    interval_df, weather_df = faulty_colombo
    return {c["metric"]: c for c in detectors.run_all(interval_df, weather_df, colombo, date)}


# --- each detector raises its candidate with the expected magnitude -------------------------


def test_night_baseload_creep_fires_with_expected_magnitude(faulty_colombo, colombo):
    cand = _candidates(faulty_colombo, colombo, FAULT_NIGHT_DAY)["night_baseload"]
    mag = cand["magnitude"]
    assert mag["delta_kw"] >= colombo.threshold("baseload_creep_min_kw")
    assert mag["pct_increase"] >= colombo.threshold("baseload_creep_pct")
    assert 4.5 <= mag["delta_kw"] <= 7.5  # injected +6 kW, measured in the 01:00-04:00 window
    assert cand["time_window"]["hours"] == "01:00-04:00"
    assert cand["series_summary"]["trailing_days_used"] >= 20


def test_solar_yield_shortfall_fires_with_expected_magnitude(faulty_colombo, colombo):
    cand = _candidates(faulty_colombo, colombo, FAULT_SOLAR_DAY)["solar_yield"]
    mag = cand["magnitude"]
    assert mag["yield_ratio"] < colombo.threshold("solar_yield_floor_pct") / 100.0
    assert 0.55 <= mag["yield_ratio"] <= 0.78  # injected x0.7 of expected generation
    assert mag["shortfall_pct"] >= 20
    assert mag["expected_kwh"] > mag["actual_kwh"]


def test_off_schedule_load_fires_with_expected_magnitude(faulty_colombo, colombo):
    cand = _candidates(faulty_colombo, colombo, FAULT_HOLIDAY_DAY)["off_schedule_load"]
    mag = cand["magnitude"]
    assert mag["load_ratio"] > colombo.threshold("off_schedule_load_pct") / 100.0
    assert mag["load_ratio"] > 0.8  # holiday_load runs the day at full working-day level
    assert mag["hours_affected"] >= 5
    assert mag["excess_kwh"] > 100
    assert cand["time_window"]["reference_working_day"] == "2026-08-24"


# --- negative cases -----------------------------------------------------------------------


def test_clean_working_day_produces_zero_candidates(clean_colombo, colombo):
    interval_df, weather_df = clean_colombo
    assert detectors.run_all(interval_df, weather_df, colombo, CLEAN_WORKING_DAY) == []


def test_clean_closed_day_produces_zero_candidates(clean_colombo, colombo):
    interval_df, weather_df = clean_colombo
    assert detectors.run_all(interval_df, weather_df, colombo, CLEAN_CLOSED_DAY) == []


def test_overcast_day_raises_no_solar_candidate(clean_colombo, colombo):
    """A heavily overcast but otherwise healthy day: generation is low in absolute terms but the
    cloud-adjusted expectation tracks it, so solar_yield never fires - the shortfall is
    'explained' at the data layer and never reaches case creation."""
    interval_df, weather_df = clean_colombo
    weather_df = weather_df.copy()
    weather_df["_d"] = weather_df["timestamp"].str.slice(0, 10)
    day_cloud = weather_df.groupby("_d")["cloud_cover"].mean()
    working = [d for d in day_cloud.index if colombo.is_working_day(__import__("datetime").date.fromisoformat(d))]
    cloudiest = max(working, key=lambda d: day_cloud[d])
    assert day_cloud[cloudiest] > 0.5, "fixture has no sufficiently overcast clean day"
    metrics = {c["metric"] for c in detectors.run_all(interval_df, weather_df, colombo, cloudiest)}
    assert "solar_yield" not in metrics


def test_clean_no_solar_site_never_alerts(clean_kandy, kandy):
    interval_df, weather_df = clean_kandy
    for date in ("2026-08-11", "2026-08-18", "2026-08-25", "2026-09-01"):
        assert detectors.run_all(interval_df, weather_df, kandy, date) == []


# --- the sandbox script the agent runs must equal the reference oracle ---------------------


@pytest.mark.parametrize(
    "kind,date",
    [
        ("faulty", FAULT_NIGHT_DAY),
        ("faulty", FAULT_SOLAR_DAY),
        ("faulty", FAULT_HOLIDAY_DAY),
        ("faulty", CLEAN_WORKING_DAY),
        ("clean", CLEAN_WORKING_DAY),
        ("clean", CLEAN_CLOSED_DAY),
    ],
)
def test_sandbox_script_matches_reference(kind, date, faulty_colombo, clean_colombo, colombo):
    interval_df, weather_df = faulty_colombo if kind == "faulty" else clean_colombo
    reference = detectors.run_all(interval_df, weather_df, colombo, date)

    # Strip the PARAMETERS block and exec the analysis body with injected values.
    parts = agent.DETECTOR_SANDBOX_CODE.split("# =====")
    assert len(parts) == 3
    body = parts[0] + parts[2].split("\n", 1)[1]

    import datetime as dt

    target = dt.date.fromisoformat(date)
    is_closed = colombo.is_closed(target)
    ref_working = colombo.previous_working_day(target).isoformat() if is_closed else ""
    wx_day = weather_df[weather_df["timestamp"].str.slice(0, 10) == date]
    mean_cloud = float(wx_day["cloud_cover"].mean()) if len(wx_day) else 0.0

    csv_path = str(
        (
            __import__("pathlib").Path(__file__).resolve().parent.parent
            / "data"
            / "fixtures"
            / kind
            / "interval_colombo-plant-1.csv"
        )
    )
    ns = {
        "CSV_PATH": csv_path,
        "TARGET_DATE": date,
        "HAS_SOLAR": colombo.has_solar,
        "SOLAR_KWP": colombo.solar_kwp,
        "MEAN_CLOUD": mean_cloud,
        "IS_CLOSED_DAY": is_closed,
        "REFERENCE_WORKING_DAY": ref_working,
        "BASELOAD_CREEP_PCT": colombo.threshold("baseload_creep_pct"),
        "BASELOAD_CREEP_MIN_KW": colombo.threshold("baseload_creep_min_kw"),
        "SOLAR_YIELD_FLOOR_PCT": colombo.threshold("solar_yield_floor_pct"),
        "OFF_SCHEDULE_LOAD_PCT": colombo.threshold("off_schedule_load_pct"),
    }
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(body, ns)
    assert json.loads(buf.getvalue())["candidates"] == reference
