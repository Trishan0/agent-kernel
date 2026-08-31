"""Shared fixtures for the test suite.

Detector / tool / state tests are deterministic: they read the committed CSV fixtures under
data/fixtures/ and never touch an LLM, the network, or the real state/ directory.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from dotenv import load_dotenv

import tool
from sites import load_sites

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "data" / "fixtures"


@pytest.fixture(scope="session")
def sites():
    return load_sites()


@pytest.fixture(scope="session")
def colombo(sites):
    return sites["colombo-plant-1"]


@pytest.fixture(scope="session")
def kandy(sites):
    return sites["kandy-hotel-1"]


def _load(kind: str, site_id: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = FIXTURES / kind
    return (
        pd.read_csv(base / f"interval_{site_id}.csv"),
        pd.read_csv(base / f"weather_{site_id}.csv"),
    )


@pytest.fixture(scope="session")
def clean_colombo():
    return _load("clean", "colombo-plant-1")


@pytest.fixture(scope="session")
def clean_kandy():
    return _load("clean", "kandy-hotel-1")


@pytest.fixture(scope="session")
def faulty_colombo():
    """colombo-plant-1 with baseload_creep@2026-08-20 (+6kW), solar_string_failure@2026-08-22
    (x0.7) and holiday_load@2026-08-25 injected."""
    return _load("faulty", "colombo-plant-1")


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    """Point the state store at a throwaway directory and reset its per-site locks."""
    monkeypatch.setattr(tool, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tool, "_locks", {})
    return tmp_path / "state"
