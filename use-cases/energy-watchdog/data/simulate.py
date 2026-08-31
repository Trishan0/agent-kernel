"""Interval-data simulator for the Energy Anomaly Watchdog.

The competition build runs on generated data (SPEC.md "Data"). This script writes, per site:

  data/interval_<site_id>.csv   timestamp, load_kw, solar_kw     (15-minute resolution)
  data/weather_<site_id>.csv    timestamp, cloud_cover, temp_c   (15-minute resolution)

The cloud-cover series in the weather CSV is the SAME array used to attenuate the solar series,
so get_weather (tool.py) and the solar data can never disagree.

Fault injection is by CLI flag and is remembered in data/faults_<site_id>.json, so faults
accumulate across runs (inject one, sweep, inject another) until --reset:

  python data/simulate.py --all                       # regenerate every site, clean, 90 days
  python data/simulate.py --site colombo-plant-1      # regenerate one site (keeps its faults)
  python data/simulate.py --site colombo-plant-1 --inject baseload_creep --from 2026-08-20 --magnitude 6kw
  python data/simulate.py --site colombo-plant-1 --inject solar_string_failure --from 2026-08-22 --magnitude 0.7
  python data/simulate.py --site colombo-plant-1 --inject holiday_load --date 2026-08-25
  python data/simulate.py --site colombo-plant-1 --reset

Everything is seeded from (site_id, --seed), so a given set of flags always produces the same
CSVs — the detector tests depend on that.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sites import SiteProfile, load_sites  # noqa: E402
from solar_model import K_CLOUD, clearsky_kw  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent
STEP_MINUTES = 15
STEPS_PER_DAY = 24 * 60 // STEP_MINUTES

FAULT_TYPES = ("baseload_creep", "solar_string_failure", "holiday_load")


# --------------------------------------------------------------------------------------------
# fault manifest
# --------------------------------------------------------------------------------------------


def _faults_path(out_dir: Path, site_id: str) -> Path:
    return out_dir / f"faults_{site_id}.json"


def load_faults(out_dir: Path, site_id: str) -> list[dict]:
    p = _faults_path(out_dir, site_id)
    if not p.exists():
        return []
    return json.loads(p.read_text())


def save_faults(out_dir: Path, site_id: str, faults: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _faults_path(out_dir, site_id).write_text(json.dumps(faults, indent=2) + "\n")


def add_fault(faults: list[dict], fault: dict) -> list[dict]:
    """Append a fault, replacing any existing one of the same type on the same date."""
    key = (fault["type"], fault.get("from") or fault.get("date"))
    kept = [f for f in faults if (f["type"], f.get("from") or f.get("date")) != key]
    return kept + [fault]


def parse_magnitude(raw: str | None, default: float) -> float:
    if raw is None:
        return default
    cleaned = re.sub(r"[^0-9.\-]", "", raw)
    return float(cleaned) if cleaned else default


# --------------------------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------------------------


def _rng(site_id: str, tag: str, seed: int) -> np.random.Generator:
    """A per-(site, purpose, seed) generator with a process-stable seed (hash() is salted)."""
    return np.random.default_rng(zlib.crc32(f"{site_id}:{tag}:{seed}".encode()) & 0xFFFFFFFF)


def _time_index(start: dt.date, days: int) -> pd.DatetimeIndex:
    first = pd.Timestamp(start)
    periods = days * STEPS_PER_DAY
    return pd.date_range(start=first, periods=periods, freq=f"{STEP_MINUTES}min")


def _hour_of_day(idx: pd.DatetimeIndex) -> np.ndarray:
    return idx.hour.to_numpy() + idx.minute.to_numpy() / 60.0


def generate_weather(rng: np.random.Generator, idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Diurnal temperature plus a per-day cloud level with intraday variation."""
    hod = _hour_of_day(idx)
    dates = idx.normalize()
    unique_days = dates.unique()

    day_temp_offset = {d: rng.normal(0.0, 1.6) for d in unique_days}
    day_cloud_level = {d: float(np.clip(rng.beta(2.2, 3.0), 0.02, 0.98)) for d in unique_days}

    offset = np.array([day_temp_offset[d] for d in dates])
    cloud_base = np.array([day_cloud_level[d] for d in dates])

    temp_c = 28.5 + offset + 3.5 * np.sin(2 * np.pi * (hod - 8) / 24.0) + rng.normal(0.0, 0.3, size=len(idx))
    cloud_cover = np.clip(cloud_base + rng.normal(0.0, 0.14, size=len(idx)), 0.0, 1.0)

    return pd.DataFrame(
        {"timestamp": idx.strftime("%Y-%m-%d %H:%M:%S"), "cloud_cover": cloud_cover.round(4), "temp_c": temp_c.round(3)}
    )


def _load_shape_kw(hod: np.ndarray, connected_kw: float, working: np.ndarray) -> np.ndarray:
    """Piecewise-linear daily load shape (kW), independent of weather and noise."""
    nf = 0.09 * connected_kw  # night floor
    c = connected_kw
    pts_h = np.array([0.0, 5.5, 8.0, 12.0, 17.0, 19.0, 22.0, 24.0])
    working_kw = np.array([nf, nf, 0.50 * c, 0.58 * c, 0.52 * c, 0.30 * c, nf + 0.05 * c, nf])
    closed_kw = np.array([nf, nf, 1.05 * nf, 1.15 * nf, 1.15 * nf, 1.10 * nf, nf, nf])
    return np.where(working, np.interp(hod, pts_h, working_kw), np.interp(hod, pts_h, closed_kw))


def _ac_gain_kw(hod: np.ndarray, temp_c: np.ndarray, connected_kw: float) -> np.ndarray:
    """Weather-correlated daytime cooling load: rises with temperature above 27 C, 09:00-18:00."""
    window = np.clip(np.sin(np.pi * (hod - 7.0) / 12.0), 0.0, 1.0)  # smooth 07:00 -> 19:00 bump
    return 0.012 * connected_kw * np.maximum(0.0, temp_c - 27.0) * window


def generate_site_series(
    site: SiteProfile,
    idx: pd.DatetimeIndex,
    weather: pd.DataFrame,
    seed: int,
    holiday_load_dates: set[str],
) -> pd.DataFrame:
    rng = _rng(site.site_id, "series", seed)
    hod = _hour_of_day(idx)
    dates = idx.normalize()
    cloud = weather["cloud_cover"].to_numpy()
    temp_c = weather["temp_c"].to_numpy()

    # Per-step working/closed flag from the calendar; a holiday_load fault forces that date to
    # generate with the working-day shape even though the calendar says closed.
    working = np.array(
        [site.is_working_day(ts.date()) or ts.strftime("%Y-%m-%d") in holiday_load_dates for ts in dates]
    )

    load = _load_shape_kw(hod, site.connected_load_kw, working)
    load = load + _ac_gain_kw(hod, temp_c, site.connected_load_kw)
    load = load * rng.normal(1.0, 0.025, size=len(idx))
    load = np.maximum(load, 0.05 * site.connected_load_kw)

    clearsky = clearsky_kw(hod, site.solar_kwp)
    solar = clearsky * (1.0 - K_CLOUD * cloud) * rng.normal(1.0, 0.02, size=len(idx))
    solar = np.clip(solar, 0.0, None)

    return pd.DataFrame(
        {
            "timestamp": idx.strftime("%Y-%m-%d %H:%M:%S"),
            "load_kw": load.round(3),
            "solar_kw": solar.round(3),
            "_clearsky_kw": clearsky.round(3),
            "_cloud_cover": cloud,
            "_date": dates.strftime("%Y-%m-%d"),
            "_hour": hod,
        }
    )


# --------------------------------------------------------------------------------------------
# fault application
# --------------------------------------------------------------------------------------------


def apply_faults(df: pd.DataFrame, faults: list[dict]) -> pd.DataFrame:
    idx_date = pd.to_datetime(df["_date"])
    hour = df["_hour"].to_numpy()
    load = df["load_kw"].to_numpy().astype(float)
    solar = df["solar_kw"].to_numpy().astype(float)

    for fault in faults:
        ftype = fault["type"]
        if ftype == "baseload_creep":
            frm = pd.Timestamp(fault["from"])
            magnitude = float(fault["magnitude"])
            days_since = (idx_date - frm).dt.days.to_numpy()
            ramp = np.clip(days_since / 2.0 + 0.5, 0.0, 1.0)  # ~1.5-day fade-in, then a step
            night = (hour < 6.0) | (hour >= 22.0)
            active = (days_since >= 0) & night
            load = load + np.where(active, magnitude * ramp, 0.0)
        elif ftype == "solar_string_failure":
            frm = pd.Timestamp(fault["from"])
            factor = float(fault["magnitude"])
            active = (idx_date >= frm).to_numpy()
            solar = np.where(active, solar * factor, solar)
        elif ftype == "holiday_load":
            # Handled before generation (forces the working-day shape); nothing to do here.
            continue

    df = df.copy()
    df["load_kw"] = np.round(load, 3)
    df["solar_kw"] = np.round(solar, 3)
    return df


# --------------------------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------------------------


def daily_summary(site: SiteProfile, df: pd.DataFrame, faults: list[dict]) -> pd.DataFrame:
    step_h = STEP_MINUTES / 60.0
    night = (df["_hour"] >= 1.0) & (df["_hour"] < 4.0)  # detector 1 window: 01:00-04:00

    fault_days: dict[str, str] = {}
    for f in faults:
        tag = f["type"].replace("_", " ")
        if f["type"] == "holiday_load":
            fault_days[f["date"]] = tag
        else:
            for d in df["_date"].unique():
                if d >= f["from"]:
                    fault_days[d] = (fault_days.get(d, "") + " + " + tag).strip(" +")

    rows = []
    for date, day in df.groupby("_date", sort=True):
        d_night = day.loc[night.loc[day.index], "load_kw"]
        clearsky_kwh = float(day["_clearsky_kw"].sum() * step_h)
        expected_solar = float((day["_clearsky_kw"] * (1.0 - K_CLOUD * day["_cloud_cover"])).sum() * step_h)
        actual_solar = float(day["solar_kw"].sum() * step_h)
        rows.append(
            {
                "date": date,
                "day_type": "working" if site.is_working_day(dt.date.fromisoformat(date)) else "closed",
                "load_kwh": round(float(day["load_kw"].sum() * step_h), 1),
                "night_floor_kw": round(float(d_night.median()), 2) if len(d_night) else float("nan"),
                "mean_cloud": round(float(day["_cloud_cover"].mean()), 3),
                "solar_kwh": round(actual_solar, 1) if site.has_solar else None,
                # raw yield vs a pure clear-sky day: dips on cloudy days, proves the two series are linked
                "clearsky_ratio": (
                    round(actual_solar / clearsky_kwh, 3) if site.has_solar and clearsky_kwh > 0 else None
                ),
                # the detector's metric: actual vs the cloud-adjusted expectation, ~1.0 on any clean
                # day, <0.75 only on a real fault
                "solar_yield_ratio": (
                    round(actual_solar / expected_solar, 3) if site.has_solar and expected_solar > 0 else None
                ),
                "injected_fault": fault_days.get(date, ""),
            }
        )
    return pd.DataFrame(rows)


def print_summary(site: SiteProfile, summary: pd.DataFrame) -> None:
    print(f"\n=== {site.site_id} ({site.name}) — {len(summary)} days ===")
    with pd.option_context("display.max_rows", None, "display.width", 160):
        print(summary.to_string(index=False))
    nf = summary["night_floor_kw"]
    print(
        f"\nnight floor kW  min={nf.min():.2f}  mean={nf.mean():.2f}  max={nf.max():.2f}  "
        f"spread={nf.max() - nf.min():.2f}  (should be roughly flat unless baseload_creep injected)"
    )
    if site.has_solar:
        yr = summary["solar_yield_ratio"].dropna()
        cr = summary["clearsky_ratio"].dropna()
        corr = float(np.corrcoef(summary["mean_cloud"], summary["clearsky_ratio"])[0, 1])
        print(
            f"clearsky_ratio (raw yield)  min={cr.min():.3f}  mean={cr.mean():.3f}  max={cr.max():.3f}  "
            f"corr(mean_cloud, clearsky_ratio)={corr:+.2f}  (strongly negative => the cloud and solar series are linked)"
        )
        print(
            f"solar_yield_ratio (detector metric)  min={yr.min():.3f}  mean={yr.mean():.3f}  max={yr.max():.3f}  "
            f"(~1.0 on every clean day; sustained <0.75 => solar_string_failure)"
        )


# --------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------


def simulate_site(
    site: SiteProfile,
    *,
    start: dt.date,
    days: int,
    seed: int,
    out_dir: Path,
    faults: list[dict],
    show_summary: bool,
) -> None:
    idx = _time_index(start, days)
    weather_rng = _rng(site.site_id, "weather", seed)
    weather = generate_weather(weather_rng, idx)

    holiday_dates = {f["date"] for f in faults if f["type"] == "holiday_load"}
    series = generate_site_series(site, idx, weather, seed, holiday_dates)
    series = apply_faults(series, faults)

    out_dir.mkdir(parents=True, exist_ok=True)
    interval_cols = ["timestamp", "load_kw", "solar_kw"]
    series[interval_cols].to_csv(out_dir / f"interval_{site.site_id}.csv", index=False)
    weather.to_csv(out_dir / f"weather_{site.site_id}.csv", index=False)

    fault_note = ", ".join(f"{f['type']}@{f.get('from') or f.get('date')}" for f in faults) or "none"
    print(
        f"wrote interval_{site.site_id}.csv + weather_{site.site_id}.csv "
        f"({len(series)} rows, {start} .. {idx[-1].date()}, faults: {fault_note})"
    )
    if show_summary:
        print_summary(site, daily_summary(site, series, faults))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--site", help="site_id from data/sites.yaml")
    target.add_argument("--all", action="store_true", help="regenerate every site in data/sites.yaml")
    p.add_argument("--days", type=int, default=90, help="number of days to generate (default 90)")
    p.add_argument("--start", help="first date YYYY-MM-DD (default: today - days)")
    p.add_argument("--seed", type=int, default=42, help="base RNG seed (default 42)")
    p.add_argument("--out-dir", default=str(DATA_DIR), help="output directory (default data/)")
    p.add_argument("--sites-file", help="path to sites.yaml (default data/sites.yaml)")
    p.add_argument("--inject", choices=FAULT_TYPES, help="inject a fault (requires --site)")
    p.add_argument(
        "--from", dest="from_date", help="fault onset date YYYY-MM-DD (baseload_creep, solar_string_failure)"
    )
    p.add_argument("--date", help="fault date YYYY-MM-DD (holiday_load)")
    p.add_argument(
        "--magnitude",
        help="fault magnitude: kW for baseload_creep (e.g. 6kw), factor for solar_string_failure (e.g. 0.7)",
    )
    p.add_argument("--reset", action="store_true", help="clear this site's injected faults before generating")
    p.add_argument("--no-summary", action="store_true", help="skip the per-day summary table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    out_dir = Path(args.out_dir)
    sites = load_sites(args.sites_file)
    start = dt.date.fromisoformat(args.start) if args.start else dt.date.today() - dt.timedelta(days=args.days)

    if args.all:
        if args.inject or args.reset:
            print("--all cannot be combined with --inject / --reset", file=sys.stderr)
            return 2
        for site in sites.values():
            simulate_site(
                site,
                start=start,
                days=args.days,
                seed=args.seed,
                out_dir=out_dir,
                faults=load_faults(out_dir, site.site_id),
                show_summary=not args.no_summary,
            )
        return 0

    if args.site not in sites:
        print(f"unknown site {args.site!r}; known: {', '.join(sites)}", file=sys.stderr)
        return 2
    site = sites[args.site]

    faults = [] if args.reset else load_faults(out_dir, site.site_id)
    if args.inject:
        if args.inject == "holiday_load":
            if not args.date:
                print("--inject holiday_load requires --date", file=sys.stderr)
                return 2
            fault = {"type": "holiday_load", "date": args.date}
        else:
            if not args.from_date:
                print(f"--inject {args.inject} requires --from", file=sys.stderr)
                return 2
            default_mag = 5.0 if args.inject == "baseload_creep" else 0.7
            fault = {
                "type": args.inject,
                "from": args.from_date,
                "magnitude": parse_magnitude(args.magnitude, default_mag),
            }
        faults = add_fault(faults, fault)

    save_faults(out_dir, site.site_id, faults)
    simulate_site(
        site,
        start=start,
        days=args.days,
        seed=args.seed,
        out_dir=out_dir,
        faults=faults,
        show_summary=not args.no_summary,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
