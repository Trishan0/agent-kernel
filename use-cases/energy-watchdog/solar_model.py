"""Clear-sky solar model, shared by the simulator and the detectors.

Keeping one model in one place is what makes a clean day read as ``solar_yield_ratio ~= 1.0``:
the simulator attenuates a clear-sky curve by cloud cover to make the data, and the
solar-yield detector rebuilds the same curve to form its expectation. A production deployment
would swap this for a real clear-sky model (e.g. pvlib) driven by the site's latitude,
longitude and date; the interface below would not change.
"""

from __future__ import annotations

import numpy as np

# Heavy overcast removes ~78% of the clear-sky output at this site's latitude.
K_CLOUD = 0.78

# Near-equatorial day length (Colombo / Kandy sit around 6.9-7.3 deg N, so this barely moves
# across the year). Local clock hours.
SUNRISE_H = 6.0
SUNSET_H = 18.3

# Inverter + temperature + soiling losses applied to the clear-sky curve.
SYSTEM_DERATE = 0.92


def clearsky_kw(hour_of_day: np.ndarray, solar_kwp: float) -> np.ndarray:
    """Clear-sky AC output (kW) for each local hour-of-day value, for a given DC capacity."""
    hod = np.asarray(hour_of_day, dtype=float)
    if solar_kwp <= 0:
        return np.zeros_like(hod)
    frac = np.sin(np.pi * (hod - SUNRISE_H) / (SUNSET_H - SUNRISE_H))
    frac = np.where((hod > SUNRISE_H) & (hod < SUNSET_H), np.clip(frac, 0.0, 1.0), 0.0)
    return solar_kwp * frac**1.15 * SYSTEM_DERATE


def cloud_adjusted_kw(hour_of_day: np.ndarray, cloud_cover: np.ndarray, solar_kwp: float) -> np.ndarray:
    """Expected generation (kW) once the clear-sky curve is attenuated by cloud cover (0-1)."""
    return clearsky_kw(hour_of_day, solar_kwp) * (1.0 - K_CLOUD * np.asarray(cloud_cover, dtype=float))
