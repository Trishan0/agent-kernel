"""Reference implementation of the three SPEC.md detectors.

This module is NOT a tool and is NOT called by any agent. The ``anomaly_detector`` agent writes
and runs equivalent pandas in the sandbox at run time (AGENTS.md hard rule 1) — this file is the
deterministic oracle the detector test suite checks that sandbox analysis against, with no LLM
in the loop. It is also runnable directly for eyeballing the arithmetic:

    python detectors.py --site colombo-plant-1 --date 2026-08-25

Every threshold comes from the site profile (data/sites.yaml), never from this file. Each
detector returns a candidate finding dict (metric, magnitude, time window, series summary) or
None; ``run_all`` returns the list of the ones that fired, which may be empty.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from sites import SiteProfile, load_sites, resolve_site
from solar_model import K_CLOUD, clearsky_kw

_ROOT = Path(__file__).resolve().parent
_DATA_DIR = _ROOT / "data"

NIGHT_BASELOAD = "night_baseload"
SOLAR_YIELD = "solar_yield"
OFF_SCHEDULE_LOAD = "off_schedule_load"

STEP_HOURS = 0.25
_TRAILING_DAYS = 30
_NIGHT_START_H, _NIGHT_END_H = 1.0, 4.0  # 01:00-04:00 window for detector 1
_BUSINESS_START_H, _BUSINESS_END_H = 8.0, 18.0  # working-hours window for detector 3


# --------------------------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------------------------


def _prepare(interval_df: pd.DataFrame) -> pd.DataFrame:
    df = interval_df.copy()
    ts = pd.to_datetime(df["timestamp"])
    df["_date"] = ts.dt.strftime("%Y-%m-%d")
    df["_hour"] = ts.dt.hour + ts.dt.minute / 60.0
    df["load_kw"] = df["load_kw"].astype(float)
    if "solar_kw" in df.columns:
        df["solar_kw"] = df["solar_kw"].astype(float)
    return df


def _between(df: pd.DataFrame, start_h: float, end_h: float) -> pd.DataFrame:
    return df[(df["_hour"] >= start_h) & (df["_hour"] < end_h)]


def _prev_weeks(series: "pd.Series", target: dt.date, ndigits: int = 1) -> dict[str, float | None]:
    """The value of a per-date series on the same weekday one and two weeks before target."""
    out: dict[str, float | None] = {}
    for label, back in (("7d", 7), ("14d", 14)):
        key = (target - dt.timedelta(days=back)).isoformat()
        out[label] = round(float(series.loc[key]), ndigits) if key in series.index else None
    return out


# --------------------------------------------------------------------------------------------
# detector 1 — night baseload creep
# --------------------------------------------------------------------------------------------


def detect_night_baseload(df: pd.DataFrame, site: SiteProfile, target_date: str) -> dict[str, Any] | None:
    """Median 01:00-04:00 load on the target day vs the trailing 30-day median of the same
    window. Fires when the increase clears BOTH the percentage and the absolute-kW thresholds.
    """
    pct_threshold = site.threshold("baseload_creep_pct")
    min_kw_threshold = site.threshold("baseload_creep_min_kw")

    night = _between(df, _NIGHT_START_H, _NIGHT_END_H)
    per_day = night.groupby("_date")["load_kw"].median()
    if target_date not in per_day.index:
        return None

    target = dt.date.fromisoformat(target_date)
    trailing_days = [(target - dt.timedelta(days=n)).isoformat() for n in range(1, _TRAILING_DAYS + 1)]
    trailing = per_day.reindex(trailing_days).dropna()
    if trailing.empty:
        return None

    target_median = float(per_day.loc[target_date])
    baseline_median = float(trailing.median())
    delta_kw = target_median - baseline_median
    pct_increase = 100.0 * delta_kw / baseline_median if baseline_median else 0.0

    if not (pct_increase >= pct_threshold and delta_kw >= min_kw_threshold):
        return None

    return {
        "metric": NIGHT_BASELOAD,
        "detected": True,
        "magnitude": {
            "delta_kw": round(delta_kw, 3),
            "pct_increase": round(pct_increase, 2),
            # assumes the added load runs continuously, not only in the measurement window
            "implied_kwh_per_month": round(delta_kw * 24 * 30, 1),
        },
        "time_window": {"date": target_date, "hours": "01:00-04:00"},
        "series_summary": {
            "target_day_median_kw": round(target_median, 3),
            "trailing_30d_median_kw": round(baseline_median, 3),
            "trailing_days_used": int(len(trailing)),
            # the same metric on the equivalent day of the previous two weeks (investigator input)
            "same_window_prev_weeks": _prev_weeks(per_day, target, ndigits=3),
        },
        "thresholds": {"pct": pct_threshold, "min_kw": min_kw_threshold},
    }


# --------------------------------------------------------------------------------------------
# detector 2 — solar yield shortfall
# --------------------------------------------------------------------------------------------


def detect_solar_yield(
    df: pd.DataFrame, weather_df: pd.DataFrame, site: SiteProfile, target_date: str
) -> dict[str, Any] | None:
    """Day's total generation vs a clear-sky expectation for the site's capacity, attenuated by
    the day's observed cloud cover. Fires when actual falls below the profile's yield floor.
    Only sites with solar_kwp > 0.
    """
    if not site.has_solar:
        return None
    floor_ratio = site.threshold("solar_yield_floor_pct") / 100.0

    day = df[df["_date"] == target_date]
    wx = weather_df.copy()
    wx_day = wx[wx["timestamp"].str.slice(0, 10) == target_date]
    if day.empty or wx_day.empty:
        return None

    # The day's observed cloud cover, as a single figure (what get_weather returns).
    mean_cloud = float(wx_day["cloud_cover"].mean())
    clear_sky_kwh = float(np.sum(clearsky_kw(day["_hour"].to_numpy(), site.solar_kwp)) * STEP_HOURS)
    expected_kwh = clear_sky_kwh * (1.0 - K_CLOUD * mean_cloud)
    actual_kwh = float(day["solar_kw"].sum() * STEP_HOURS)
    if expected_kwh <= 0:
        return None

    ratio = actual_kwh / expected_kwh
    if ratio >= floor_ratio:
        return None

    daily_solar_kwh = df.groupby("_date")["solar_kw"].sum() * STEP_HOURS
    return {
        "metric": SOLAR_YIELD,
        "detected": True,
        "magnitude": {
            "actual_kwh": round(actual_kwh, 1),
            "expected_kwh": round(expected_kwh, 1),
            "shortfall_pct": round(100.0 * (1.0 - ratio), 2),
            "yield_ratio": round(ratio, 3),
        },
        "time_window": {"date": target_date, "hours": "06:00-18:00"},
        "series_summary": {
            "mean_cloud_cover": round(mean_cloud, 3),
            "clear_sky_kwh": round(clear_sky_kwh, 1),
            # generation (kWh) on the equivalent day of the previous two weeks (investigator input)
            "same_window_prev_weeks": _prev_weeks(daily_solar_kwh, dt.date.fromisoformat(target_date)),
        },
        "thresholds": {"yield_floor_pct": site.threshold("solar_yield_floor_pct")},
    }


# --------------------------------------------------------------------------------------------
# detector 3 — off-schedule load
# --------------------------------------------------------------------------------------------


def detect_off_schedule_load(df: pd.DataFrame, site: SiteProfile, target_date: str) -> dict[str, Any] | None:
    """Mean working-hours load on a calendar-closed day vs the same hours on the last working
    day. Fires when the closed-day load exceeds the profile's percentage of the reference.
    Only when the target day is closed.
    """
    target = dt.date.fromisoformat(target_date)
    if site.is_working_day(target):
        return None
    ratio_threshold = site.threshold("off_schedule_load_pct") / 100.0

    reference_day = site.previous_working_day(target).isoformat()
    closed = _between(df[df["_date"] == target_date], _BUSINESS_START_H, _BUSINESS_END_H)[["_hour", "load_kw"]]
    reference = _between(df[df["_date"] == reference_day], _BUSINESS_START_H, _BUSINESS_END_H)[["_hour", "load_kw"]]
    if closed.empty or reference.empty:
        return None

    paired = closed.merge(reference, on="_hour", how="inner", suffixes=("_closed", "_ref"))
    closed_mean = float(paired["load_kw_closed"].mean())
    reference_mean = float(paired["load_kw_ref"].mean())
    if reference_mean <= 0:
        return None

    ratio = closed_mean / reference_mean
    if ratio <= ratio_threshold:
        return None

    over = paired["load_kw_closed"] - ratio_threshold * paired["load_kw_ref"]
    excess_kwh = float(over.clip(lower=0).sum() * STEP_HOURS)
    hours_affected = round(
        float((paired["load_kw_closed"] > ratio_threshold * paired["load_kw_ref"]).sum() * STEP_HOURS), 2
    )

    business = _between(df, _BUSINESS_START_H, _BUSINESS_END_H)
    daily_business_mean = business.groupby("_date")["load_kw"].mean()
    return {
        "metric": OFF_SCHEDULE_LOAD,
        "detected": True,
        "magnitude": {
            "closed_day_mean_kw": round(closed_mean, 3),
            "reference_day_mean_kw": round(reference_mean, 3),
            "load_ratio": round(ratio, 3),
            "excess_kwh": round(excess_kwh, 1),
            "hours_affected": hours_affected,
        },
        "time_window": {"date": target_date, "hours": "08:00-18:00", "reference_working_day": reference_day},
        "series_summary": {
            "closed_day_business_kwh": round(float(paired["load_kw_closed"].sum() * STEP_HOURS), 1),
            "reference_day_business_kwh": round(float(paired["load_kw_ref"].sum() * STEP_HOURS), 1),
            # business-hours mean load on the equivalent day of the previous two weeks (investigator input)
            "same_window_prev_weeks": _prev_weeks(daily_business_mean, target, ndigits=3),
        },
        "thresholds": {"off_schedule_load_pct": site.threshold("off_schedule_load_pct")},
    }


# --------------------------------------------------------------------------------------------
# run all three
# --------------------------------------------------------------------------------------------


def run_all(
    interval_df: pd.DataFrame,
    weather_df: pd.DataFrame,
    site: SiteProfile,
    target_date: str,
) -> list[dict[str, Any]]:
    """Run the three detectors for one site and date. Returns only the candidates that fired
    (possibly none) — an empty sweep is a valid outcome.
    """
    df = _prepare(interval_df)
    candidates = [
        detect_night_baseload(df, site, target_date),
        detect_solar_yield(df, weather_df, site, target_date),
        detect_off_schedule_load(df, site, target_date),
    ]
    return [c for c in candidates if c is not None]


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the reference detectors for one site and date.")
    parser.add_argument("--site", required=True, help="site_id from data/sites.yaml")
    parser.add_argument("--date", required=True, help="target date YYYY-MM-DD")
    parser.add_argument("--interval", help="interval CSV path (default data/interval_<site>.csv)")
    parser.add_argument("--weather", help="weather CSV path (default data/weather_<site>.csv)")
    parser.add_argument("--sites-file", help="path to sites.yaml")
    args = parser.parse_args(argv)

    site = resolve_site(args.site, args.sites_file) if args.sites_file else load_sites()[args.site]
    interval_path = Path(args.interval) if args.interval else _DATA_DIR / f"interval_{args.site}.csv"
    weather_path = Path(args.weather) if args.weather else _DATA_DIR / f"weather_{args.site}.csv"
    interval_df = pd.read_csv(interval_path)
    weather_df = pd.read_csv(weather_path)

    candidates = run_all(interval_df, weather_df, site, args.date)
    print(json.dumps({"site_id": args.site, "target_date": args.date, "candidates": candidates}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
