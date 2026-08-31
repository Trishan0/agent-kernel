"""Agent-level behaviour tests. These DO use an LLM, so they are skipped unless a model key is
configured (GEMINI_API_KEY or OPENAI_API_KEY). They use the framework's comparison modes
(config.yaml test.mode); the deterministic guarantees live in the other test files.

Run against a live server:

    uv run python app.py                       # terminal 1
    AK_TEST_ENDPOINT=http://localhost:8000 uv run pytest tests/test_agents.py   # terminal 2
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    reason="no model key set (GEMINI_API_KEY / OPENAI_API_KEY)",
)

ENDPOINT = os.environ.get("AK_TEST_ENDPOINT", "http://localhost:8000")


def _sweep(site: str, date: str) -> str:
    payload = {
        "prompt": f"Run the daily sweep for site {site} for {date}",
        "agent": "watchdog_supervisor",
        "session_id": f"test:{site}:{date}:{uuid.uuid4().hex[:6]}",
        "user_id": site,
    }
    resp = httpx.post(f"{ENDPOINT}/api/v1/chat", json=payload, timeout=180.0)
    resp.raise_for_status()
    return resp.json().get("result", "")


@pytest.mark.integration
def test_clean_site_sweep_stays_silent():
    """A healthy site produces no case and no Telegram traffic - the reply must not claim an
    alert was raised."""
    result = _sweep("kandy-hotel-1", "2026-08-18").lower()
    assert "case" not in result or "no " in result or "nothing" in result or "healthy" in result


@pytest.mark.integration
def test_fault_sweep_raises_a_case():
    result = _sweep("colombo-plant-1", "2026-08-25").lower()
    assert any(k in result for k in ("case", "alert", "posted", "night", "solar", "off-schedule"))


@pytest.mark.integration
def test_overcast_solar_shortfall_is_explained_not_alerted():
    """If a solar candidate is raised on a heavily overcast day, the investigator classifies it
    explained and it never becomes a case."""
    result = _sweep("colombo-plant-1", "2026-08-12").lower()  # a cloudy clean day in the fixtures
    assert "explained" in result or "no case" in result or "nothing" in result or "healthy" in result
