"""Deterministic tests for the tools and the file-backed state store. No LLM, no network."""

from __future__ import annotations

import json

import pytest

import tool
from investigate import dismissal_suppresses

pytestmark = pytest.mark.usefixtures("isolated_state")


def _j(s: str) -> dict:
    return json.loads(s)


# --- get_site_profile resolves either key ------------------------------------------------


def test_get_site_profile_resolves_site_id_and_chat_id():
    by_id = _j(tool.get_site_profile("colombo-plant-1"))
    by_chat = _j(tool.get_site_profile(str(by_id["telegram_chat_id"])))
    assert by_id["site_id"] == by_chat["site_id"] == "colombo-plant-1"
    assert "error" in _j(tool.get_site_profile("no-such-site"))


# --- one case per detector per site ---------------------------------------------------------


def test_open_case_is_idempotent_per_site_and_metric():
    first = _j(tool.open_case("colombo-plant-1", "night_baseload", "Night load up", ["a", "b"], 1000.0, "check"))
    assert first["created"] is True
    again = _j(tool.open_case("colombo-plant-1", "night_baseload", "different", ["c", "d"], 9.0, "x"))
    assert again["created"] is False
    assert again["case_id"] == first["case_id"]
    assert _j(tool.list_cases("colombo-plant-1", "open"))["count"] == 1


# --- unknown case id fails safely --------------------------------------------------------


def test_update_case_unknown_id_returns_error_not_exception():
    out = _j(tool.update_case("does-not-exist", "dismissed"))
    assert "error" in out and "does-not-exist" in out["error"]


def test_dismiss_callback_with_unknown_case_id_fails_safely():
    # what case_manager does with the payload string case:<id>:dismiss
    _, case_id, action = "case:ghost-9999:dismiss".split(":")
    assert action == "dismiss"
    out = _j(tool.update_case(case_id, "acknowledged", note="dismiss requested", pending="dismiss"))
    assert "error" in out  # no case, no crash


# --- atomic write survives reload ------------------------------------------------------


def test_state_round_trips_through_disk(isolated_state):
    tool.update_baseline("kandy-hotel-1", "night_baseload", 11.0, "seed", "routine_drift")
    assert (isolated_state / "kandy-hotel-1.json").exists()
    reloaded = _j(tool.get_baseline("kandy-hotel-1", "night_baseload"))
    assert reloaded["baseline"]["value"] == 11.0
    assert reloaded["baseline"]["source"] == "routine_drift"


# --- a dismissed baseline suppresses the next day's alert for the same metric --------------


def test_dismissed_baseline_suppresses_next_day_same_metric():
    site, metric = "colombo-plant-1", "night_baseload"
    # human dismisses "that's the new server rack" at 28.6 kW
    tool.update_baseline(site, metric, 28.6, "new server rack installed last week", "human_dismissal")
    tool.record_dismissal(site, metric, "new server rack installed last week")

    baseline = _j(tool.get_baseline(site, metric))
    active = baseline["active_dismissal"]
    assert active is not None and active["value_at_dismissal"] == 28.6

    # next day the detector re-raises ~ the same value -> investigator must suppress it
    suppressed, why = dismissal_suppresses(metric, 28.9, active)
    assert suppressed is True, why

    # but a genuinely worse condition (well past the margin) is NOT suppressed
    not_suppressed, why2 = dismissal_suppresses(metric, 34.0, active)
    assert not_suppressed is False, why2


def test_dismissal_suppression_with_no_active_dismissal():
    suppressed, _ = dismissal_suppresses("night_baseload", 30.0, None)
    assert suppressed is False


# --- chase scheduling uses a one-time timestamp, never a recurring rule --------------------


def test_case_chase_time_returns_one_time_timestamp_in_site_tz():
    case = _j(tool.open_case("colombo-plant-1", "solar_yield", "Solar low", ["a", "b"], 500.0, "check strings"))
    chase = _j(tool.case_chase_time(case["case_id"], hours=72))
    assert chase["timezone"] == "Asia/Colombo"
    assert chase["hours"] == 72
    assert "T" in chase["at"] and "cron" not in chase  # a single 'at' moment, not a schedule rule
    assert _j(tool.case_chase_time("ghost"))["error"]
