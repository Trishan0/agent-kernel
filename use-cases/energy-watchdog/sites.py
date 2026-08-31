"""Site profile loading — the single source of truth for per-site domain values.

Every number that differs between two sites (tariff bands, connected load, solar capacity, chat
id, the closed-day calendar, detector thresholds) comes from data/sites.yaml through here, never
from code (AGENTS.md hard rule 7). Imported by data/simulate.py, tool.py and the tests.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repo layout: this module sits at the use-case root, data/sites.yaml one level down.
DEFAULT_SITES_PATH = Path(__file__).resolve().parent / "data" / "sites.yaml"

_WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# CEB commercial time-of-use band schedule (local clock). The band *times* are a published CEB
# convention; the LKR/kWh *rates* for each band come from each site's profile.
#   off-peak : 22:30 -> 05:30
#   day      : 05:30 -> 18:30
#   peak     : 18:30 -> 22:30
_PEAK_START = _dt.time(18, 30)
_PEAK_END = _dt.time(22, 30)
_DAY_START = _dt.time(5, 30)
_DAY_END = _dt.time(18, 30)


@dataclass(frozen=True)
class Tariff:
    peak_lkr_per_kwh: float
    day_lkr_per_kwh: float
    offpeak_lkr_per_kwh: float

    def band(self, when: _dt.datetime | _dt.time) -> str:
        """Return 'peak' | 'day' | 'offpeak' for a local timestamp."""
        t = when.time() if isinstance(when, _dt.datetime) else when
        if _PEAK_START <= t < _PEAK_END:
            return "peak"
        if _DAY_START <= t < _DAY_END:
            return "day"
        return "offpeak"

    def rate(self, when: _dt.datetime | _dt.time) -> float:
        """LKR/kWh in force at a local timestamp."""
        return {
            "peak": self.peak_lkr_per_kwh,
            "day": self.day_lkr_per_kwh,
            "offpeak": self.offpeak_lkr_per_kwh,
        }[self.band(when)]

    def blended_rate(self) -> float:
        """A single LKR/kWh figure weighting each band by its share of the 24 h day.

        Used for a monthly-cost estimate on a metric (e.g. night baseload creep) that is not
        tied to one band. off-peak 7 h, day 13 h, peak 4 h.
        """
        return (7 * self.offpeak_lkr_per_kwh + 13 * self.day_lkr_per_kwh + 4 * self.peak_lkr_per_kwh) / 24.0


@dataclass(frozen=True)
class SiteProfile:
    site_id: str
    name: str
    latitude: float
    longitude: float
    timezone: str
    connected_load_kw: float
    solar_kwp: float
    telegram_chat_id: int
    tariff: Tariff
    working_days: frozenset[str]
    closed_dates: frozenset[str]
    thresholds: dict[str, float] = field(default_factory=dict)

    # --- calendar -----------------------------------------------------------------------------

    def is_working_day(self, day: _dt.date) -> bool:
        """A day the site is normally open: a configured working weekday and not a closed date."""
        if day.isoformat() in self.closed_dates:
            return False
        return _WEEKDAY_KEYS[day.weekday()] in self.working_days

    def is_closed(self, day: _dt.date) -> bool:
        return not self.is_working_day(day)

    def previous_working_day(self, day: _dt.date) -> _dt.date:
        """The most recent working day strictly before `day`."""
        cursor = day - _dt.timedelta(days=1)
        for _ in range(31):
            if self.is_working_day(cursor):
                return cursor
            cursor -= _dt.timedelta(days=1)
        return day - _dt.timedelta(days=1)

    # --- thresholds -------------------------------------------------------------------------

    def threshold(self, name: str) -> float:
        try:
            return float(self.thresholds[name])
        except KeyError as exc:  # pragma: no cover - a misconfigured profile
            raise KeyError(f"site {self.site_id!r} has no threshold {name!r} in data/sites.yaml") from exc

    @property
    def has_solar(self) -> bool:
        return self.solar_kwp > 0


def _coerce(raw: dict[str, Any]) -> SiteProfile:
    calendar = raw.get("calendar", {}) or {}
    tariff = raw["tariff"]
    return SiteProfile(
        site_id=str(raw["site_id"]),
        name=str(raw.get("name", raw["site_id"])),
        latitude=float(raw["latitude"]),
        longitude=float(raw["longitude"]),
        timezone=str(raw["timezone"]),
        connected_load_kw=float(raw["connected_load_kw"]),
        solar_kwp=float(raw.get("solar_kwp", 0) or 0),
        telegram_chat_id=int(raw["telegram_chat_id"]),
        tariff=Tariff(
            peak_lkr_per_kwh=float(tariff["peak_lkr_per_kwh"]),
            day_lkr_per_kwh=float(tariff["day_lkr_per_kwh"]),
            offpeak_lkr_per_kwh=float(tariff["offpeak_lkr_per_kwh"]),
        ),
        working_days=frozenset(str(d).lower() for d in calendar.get("working_days", _WEEKDAY_KEYS)),
        closed_dates=frozenset(str(d) for d in calendar.get("closed_dates", [])),
        thresholds={k: float(v) for k, v in (raw.get("thresholds", {}) or {}).items()},
    )


def load_sites(path: str | Path | None = None) -> dict[str, SiteProfile]:
    """Load every site profile from sites.yaml, keyed by site_id."""
    p = Path(path) if path else DEFAULT_SITES_PATH
    doc = yaml.safe_load(p.read_text()) or {}
    sites = [_coerce(entry) for entry in doc.get("sites", [])]
    return {s.site_id: s for s in sites}


def resolve_site(key: str | int, path: str | Path | None = None) -> SiteProfile:
    """Resolve a site by its site_id or by its telegram chat id (int or str)."""
    sites = load_sites(path)
    key_str = str(key)
    if key_str in sites:
        return sites[key_str]
    for site in sites.values():
        if str(site.telegram_chat_id) == key_str:
            return site
    raise KeyError(f"no site matches site_id or telegram chat id {key!r}")
