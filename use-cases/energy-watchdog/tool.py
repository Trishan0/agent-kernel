"""Tools and the file-backed state store for the Energy Anomaly Watchdog.

The ten tools in SPEC.md "Tools", plus the JSON state store. Rules (SPEC.md / AGENTS.md):

  - Tools return JSON strings, never Python objects.
  - No detector maths here — the sandbox does the arithmetic. These tools only move data and
    persist state.
  - State is one JSON document per site at state/<site_id>.json holding baselines, dismissals
    and cases. Never session state: the Telegram session id is the chat id, the scheduled sweep
    runs under its own session, and a case opened by the sweep must be visible to the reply
    handler. Writes are atomic (temp file + os.replace) under a per-site lock.
  - load_interval_data writes the CSV into the sandbox workspace and returns the path + row
    count, never the series.
  - post_telegram_alert calls the Bot API sendMessage endpoint directly with httpx; outbound
    alerts do not go through AgentTelegramRequestHandler.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
from pydantic import BaseModel

from sites import resolve_site


class InlineButton(BaseModel):
    """One Telegram inline-keyboard button. callback_data must be `case:<case_id>:<action>`."""

    text: str
    callback_data: str


# --------------------------------------------------------------------------------------------
# metrics — the three detectors, named once and used everywhere
# --------------------------------------------------------------------------------------------

NIGHT_BASELOAD = "night_baseload"
SOLAR_YIELD = "solar_yield"
OFF_SCHEDULE_LOAD = "off_schedule_load"
METRICS = (NIGHT_BASELOAD, SOLAR_YIELD, OFF_SCHEDULE_LOAD)

_METRIC_ABBR = {NIGHT_BASELOAD: "nbl", SOLAR_YIELD: "sy", OFF_SCHEDULE_LOAD: "osl"}

_ROOT = Path(__file__).resolve().parent
_DATA_DIR = _ROOT / "data"
_STATE_DIR = _ROOT / "state"

TERMINAL_STATUSES = ("fixed", "dismissed")
CASE_STATUSES = ("open", "acknowledged", "fixed", "dismissed")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str)


def _err(message: str, **extra: Any) -> str:
    return _json({"error": message, **extra})


# --------------------------------------------------------------------------------------------
# state store
#
# One document per site: {"site_id", "baselines", "dismissals", "cases"}. Local dev keeps it in
# state/<site_id>.json (atomic temp-file + rename under a per-site lock). On AWS the same
# document is one DynamoDB item, partition key site_id, selected with AK_STATE__BACKEND=dynamodb
# (+ AK_STATE__DYNAMODB__TABLE_NAME). Reads/writes go through _read_state / _write_state /
# _mutate_state / _find_case only.
# --------------------------------------------------------------------------------------------

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_STATE_BACKEND = os.environ.get("AK_STATE__BACKEND", "file").lower()


def _site_lock(site_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(site_id, threading.Lock())


def _state_path(site_id: str) -> Path:
    return _STATE_DIR / f"{site_id}.json"


def _empty_state(site_id: str) -> dict[str, Any]:
    return {"site_id": site_id, "baselines": {}, "dismissals": [], "cases": {}}


def _normalise(state: dict[str, Any], site_id: str) -> dict[str, Any]:
    state.setdefault("site_id", site_id)
    for key, default in (("baselines", {}), ("dismissals", []), ("cases", {})):
        state.setdefault(key, default)
    return state


def _read_state(site_id: str) -> dict[str, Any]:
    if _STATE_BACKEND == "dynamodb":
        from state_dynamodb import read_item

        return _normalise(read_item(site_id) or _empty_state(site_id), site_id)
    path = _state_path(site_id)
    if not path.exists():
        return _empty_state(site_id)
    return _normalise(json.loads(path.read_text()), site_id)


def _write_state(site_id: str, state: dict[str, Any]) -> None:
    if _STATE_BACKEND == "dynamodb":
        from state_dynamodb import write_item

        write_item(site_id, state)
        return
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = _state_path(site_id)
    tmp = path.with_name(f"{site_id}.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)  # atomic on POSIX


def _mutate_state(site_id: str, fn: Callable[[dict[str, Any]], Any]) -> Any:
    """Read-modify-write a site's state document under the site lock; fn mutates it in place."""
    with _site_lock(site_id):
        state = _read_state(site_id)
        result = fn(state)
        _write_state(site_id, state)
        return result


def _find_case(case_id: str) -> tuple[str | None, dict[str, Any] | None]:
    """Locate a case across every site's document. Returns (site_id, case) or (None, None)."""
    if _STATE_BACKEND == "dynamodb":
        from sites import load_sites

        site_ids = list(load_sites())
    else:
        if not _STATE_DIR.exists():
            return None, None
        site_ids = [p.stem for p in sorted(_STATE_DIR.glob("*.json"))]

    for site_id in site_ids:
        try:
            state = _read_state(site_id)
        except (json.JSONDecodeError, OSError):
            continue
        case = state.get("cases", {}).get(case_id)
        if case is not None:
            return state.get("site_id", site_id), case
    return None, None


def _active_dismissal(state: dict[str, Any], metric: str) -> dict[str, Any] | None:
    for entry in reversed(state.get("dismissals", [])):
        if entry.get("metric") == metric and entry.get("active", True):
            return entry
    return None


# --------------------------------------------------------------------------------------------
# 1. get_site_profile
# --------------------------------------------------------------------------------------------


def get_site_profile(site_id_or_chat_id: str) -> str:
    """Return a site's full profile as JSON: location, capacity, solar_kwp, telegram_chat_id,
    tariff bands, the closed-day calendar and the per-site detector thresholds.

    Call this first for any site-specific work. Accepts either the site_id (e.g.
    "colombo-plant-1") or the Telegram chat id the message arrived on (e.g. "-1001234567890"),
    so an inbound group message can be resolved to its site.
    """
    try:
        site = resolve_site(site_id_or_chat_id)
    except KeyError as exc:
        return _err(exc.args[0])
    return _json(
        {
            "site_id": site.site_id,
            "name": site.name,
            "latitude": site.latitude,
            "longitude": site.longitude,
            "timezone": site.timezone,
            "connected_load_kw": site.connected_load_kw,
            "solar_kwp": site.solar_kwp,
            "has_solar": site.has_solar,
            "telegram_chat_id": site.telegram_chat_id,
            "tariff": {
                "peak_lkr_per_kwh": site.tariff.peak_lkr_per_kwh,
                "day_lkr_per_kwh": site.tariff.day_lkr_per_kwh,
                "offpeak_lkr_per_kwh": site.tariff.offpeak_lkr_per_kwh,
                "blended_lkr_per_kwh": round(site.tariff.blended_rate(), 4),
            },
            "calendar": {
                "working_days": sorted(site.working_days),
                "closed_dates": sorted(site.closed_dates),
            },
            "thresholds": dict(site.thresholds),
        }
    )


# --------------------------------------------------------------------------------------------
# 2. load_interval_data
# --------------------------------------------------------------------------------------------


async def load_interval_data(site_id: str, start_date: str, end_date: str) -> str:
    """Stage a site's 15-minute interval data (timestamp, load_kw, solar_kw) for the inclusive
    date range into the sandbox workspace as a CSV, and return JSON with the file path, the row
    count and the column names — never the series itself (it is thousands of rows).

    Use this in the sandbox detector analysis: call it, then read the returned path with pandas
    inside run_code. Dates are "YYYY-MM-DD".
    """
    source = _DATA_DIR / f"interval_{site_id}.csv"
    if not source.exists():
        return _err(f"no interval data for {site_id!r}; run: python data/simulate.py --site {site_id}", path=None)
    try:
        frame = pd.read_csv(source)
        mask = (frame["timestamp"].str.slice(0, 10) >= start_date) & (frame["timestamp"].str.slice(0, 10) <= end_date)
        window = frame.loc[mask].reset_index(drop=True)
    except Exception as exc:  # noqa: BLE001 - tools never raise into the framework
        return _err(f"could not read interval data: {exc}")
    if window.empty:
        return _err(f"no rows for {site_id!r} between {start_date} and {end_date}", rows=0)

    payload = window.to_csv(index=False).encode()
    filename = f"interval_{site_id}.csv"  # site-namespaced so two sweeps can't collide
    try:
        from agentkernel.sandbox import ExecutionManager

        manager = ExecutionManager.get()
    except Exception:  # noqa: BLE001
        manager = None

    if manager is not None:
        await manager.upload(filename, payload)
        location, in_sandbox = filename, True
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="ew-interval-"))
        (tmp_dir / filename).write_bytes(payload)
        location, in_sandbox = str(tmp_dir / filename), False

    return _json(
        {
            "path": location,
            "in_sandbox": in_sandbox,
            "rows": int(len(window)),
            "columns": list(window.columns),
            "start_date": start_date,
            "end_date": end_date,
        }
    )


# --------------------------------------------------------------------------------------------
# 3. get_weather
# --------------------------------------------------------------------------------------------


def get_weather(site_id: str, date: str) -> str:
    """Return the weather for a site on one date ("YYYY-MM-DD") as JSON: mean and max cloud
    cover (0-1) and mean and max ambient temperature (deg C).

    Use this to corroborate a candidate finding — heavy cloud explains a solar shortfall, a hot
    day explains a consumption rise. This reads the same cloud-cover series that attenuated the
    solar data, so the two are always consistent.
    """
    source = _DATA_DIR / f"weather_{site_id}.csv"
    if not source.exists():
        return _err(f"no weather data for {site_id!r}; run: python data/simulate.py --site {site_id}")
    frame = pd.read_csv(source)
    day = frame.loc[frame["timestamp"].str.slice(0, 10) == date]
    if day.empty:
        return _err(f"no weather rows for {site_id!r} on {date}")
    return _json(
        {
            "site_id": site_id,
            "date": date,
            "cloud_cover_mean": round(float(day["cloud_cover"].mean()), 4),
            "cloud_cover_max": round(float(day["cloud_cover"].max()), 4),
            "temp_c_mean": round(float(day["temp_c"].mean()), 3),
            "temp_c_max": round(float(day["temp_c"].max()), 3),
        }
    )


# --------------------------------------------------------------------------------------------
# 4. get_baseline
# --------------------------------------------------------------------------------------------


def get_baseline(site_id: str, metric: str) -> str:
    """Return the current accepted-normal value for a metric on a site as JSON, including any
    human revision (with its reason and source) and whether an active dismissal is in force.

    Metrics: "night_baseload", "solar_yield", "off_schedule_load". Call this in the
    investigator before deciding a candidate is worth an alert.
    """
    state = _read_state(site_id)
    baseline = state.get("baselines", {}).get(metric)
    dismissal = _active_dismissal(state, metric)
    return _json(
        {
            "site_id": site_id,
            "metric": metric,
            "exists": baseline is not None,
            "baseline": baseline,
            "active_dismissal": dismissal,
        }
    )


# --------------------------------------------------------------------------------------------
# 5. update_baseline
# --------------------------------------------------------------------------------------------


def update_baseline(site_id: str, metric: str, value: float, reason: str, source: str) -> str:
    """Revise the accepted-normal value for a metric on a site and return the stored baseline
    as JSON.

    source is "human_dismissal" when a person dismissed a finding as expected ("that's the new
    server rack"), or "routine_drift" for a slow automatic re-baselining. After a dismissal,
    call this so the same condition is not reported again.
    """
    if source not in ("human_dismissal", "routine_drift"):
        return _err(f"source must be 'human_dismissal' or 'routine_drift', got {source!r}")
    record = {"value": float(value), "reason": reason, "source": source, "updated_at": _now()}

    def _apply(state: dict[str, Any]) -> None:
        state["baselines"][metric] = record

    _mutate_state(site_id, _apply)
    return _json({"site_id": site_id, "metric": metric, "baseline": record})


# --------------------------------------------------------------------------------------------
# 6. list_cases
# --------------------------------------------------------------------------------------------


def list_cases(site_id: str, status: str = "") -> str:
    """List a site's cases as JSON {"cases": [...]}, newest first. Pass status to filter
    ("open", "acknowledged", "fixed", "dismissed"); omit it for all.

    Use this to answer "what's still open?" and to check whether a free-text message relates to
    an existing case.
    """
    if status and status not in CASE_STATUSES:
        return _err(f"status must be one of {CASE_STATUSES} or empty, got {status!r}")
    state = _read_state(site_id)
    cases = list(state.get("cases", {}).values())
    if status:
        cases = [c for c in cases if c.get("status") == status]
    cases.sort(key=lambda c: c.get("opened_at", ""), reverse=True)
    return _json({"site_id": site_id, "count": len(cases), "cases": cases})


# --------------------------------------------------------------------------------------------
# 7. open_case
# --------------------------------------------------------------------------------------------


def open_case(
    site_id: str,
    metric: str,
    summary: str,
    evidence: list[str],
    cost_estimate_lkr: float,
    suggested_check: str,
) -> str:
    """Create a case for an unexplained finding and return it as JSON, including the new
    case_id. If a non-terminal case for the same site and metric already exists, that case is
    returned instead with "created": false — never open a second alert for the same detector on
    the same site.

    evidence is a list of short strings (at least two). cost_estimate_lkr is the estimated
    monthly waste in LKR. suggested_check is one concrete thing for a person to look at.
    """
    if metric not in METRICS:
        return _err(f"metric must be one of {METRICS}, got {metric!r}")
    if isinstance(evidence, str):
        try:
            parsed = json.loads(evidence)
            evidence = parsed if isinstance(parsed, list) else [evidence]
        except json.JSONDecodeError:
            evidence = [evidence]
    evidence = [str(e) for e in evidence]

    try:
        site = resolve_site(site_id)
    except KeyError as exc:
        return _err(exc.args[0])

    def _apply(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        for existing in state["cases"].values():
            if existing.get("metric") == metric and existing.get("status") not in TERMINAL_STATUSES:
                return existing, False
        case_id = f"{_METRIC_ABBR.get(metric, 'ew')}-{uuid.uuid4().hex[:8]}"
        now = _now()
        case = {
            "case_id": case_id,
            "site_id": site_id,
            "chat_id": site.telegram_chat_id,
            "metric": metric,
            "status": "open",
            "summary": summary,
            "evidence": evidence,
            "cost_estimate_lkr": round(float(cost_estimate_lkr), 2),
            "suggested_check": suggested_check,
            "message_id": None,
            "opened_at": now,
            "updated_at": now,
            "chase_scheduled_task_id": None,
            "history": [{"status": "open", "note": "case opened", "at": now}],
        }
        state["cases"][case_id] = case
        return case, True

    case, created = _mutate_state(site_id, _apply)
    return _json({"created": created, **case})


# --------------------------------------------------------------------------------------------
# 8. update_case
# --------------------------------------------------------------------------------------------


def update_case(
    case_id: str,
    status: str,
    note: str = "",
    chase_scheduled_task_id: str = "",
    pending: str = "",
) -> str:
    """Move a case through its lifecycle and return the updated case as JSON. status is one of
    "open", "acknowledged", "fixed", "dismissed". note is a short free-text line stored on the
    case history.

    Pass chase_scheduled_task_id to record the id of the 72-hour chase task when it is created.
    Pass pending="dismiss" when a dismiss button was pressed and you are waiting for the user's
    one-line reason; pass pending="" to clear it once the reason has been handled. An unknown
    case_id returns {"error": ...} rather than failing.
    """
    if status not in CASE_STATUSES:
        return _err(f"status must be one of {CASE_STATUSES}, got {status!r}")
    site_id, case = _find_case(case_id)
    if site_id is None or case is None:
        return _err(f"unknown case_id {case_id!r}")

    def _apply(state: dict[str, Any]) -> dict[str, Any]:
        target = state["cases"][case_id]
        target["status"] = status
        target["updated_at"] = _now()
        if chase_scheduled_task_id:
            target["chase_scheduled_task_id"] = chase_scheduled_task_id
        target["pending"] = pending
        target["history"].append({"status": status, "note": note, "at": _now()})
        return target

    updated = _mutate_state(site_id, _apply)
    return _json(updated)


def list_dismissals(site_id: str, metric: str = "") -> str:
    """List a site's dismissal history as JSON {"dismissals": [...]}, newest first. Pass metric
    to filter to one metric. Use this in the investigator to see how often this condition has
    already been dismissed as expected.
    """
    if metric and metric not in METRICS:
        return _err(f"metric must be one of {METRICS} or empty, got {metric!r}")
    state = _read_state(site_id)
    entries = list(state.get("dismissals", []))
    if metric:
        entries = [e for e in entries if e.get("metric") == metric]
    entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    return _json({"site_id": site_id, "count": len(entries), "dismissals": entries})


def case_chase_time(case_id: str, hours: int = 72) -> str:
    """Return the local wall-clock timestamp `hours` from now in the case's site timezone, as
    JSON {"case_id", "at", "timezone", "hours"} — ready to pass straight to create_schedule as
    its `at` and `timezone`. Use this to schedule the 72-hour chase; an unknown case_id returns
    {"error": ...}.
    """
    site_id, case = _find_case(case_id)
    if site_id is None or case is None:
        return _err(f"unknown case_id {case_id!r}")
    try:
        tz = resolve_site(site_id).timezone
    except KeyError:
        tz = "UTC"
    fire_at = dt.datetime.now(ZoneInfo(tz)) + dt.timedelta(hours=int(hours))
    return _json(
        {
            "case_id": case_id,
            "at": fire_at.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": tz,
            "hours": int(hours),
        }
    )


# --------------------------------------------------------------------------------------------
# 9. record_dismissal
# --------------------------------------------------------------------------------------------


def record_dismissal(site_id: str, metric: str, reason: str) -> str:
    """Record that a person dismissed a finding for a metric as expected, so future alerts for
    that metric are suppressed until the condition worsens past a further margin. Returns the
    stored dismissal as JSON.

    Call this together with update_baseline (call update_baseline first, so this snapshots the
    revised value): update_baseline shifts the number the detector compares against, this logs
    the human reason and the suppression margin the investigator checks.
    """
    if metric not in METRICS:
        return _err(f"metric must be one of {METRICS}, got {metric!r}")

    def _apply(state: dict[str, Any]) -> dict[str, Any]:
        baseline = state.get("baselines", {}).get(metric) or {}
        for entry in state["dismissals"]:
            if entry.get("metric") == metric:
                entry["active"] = False
        record = {
            "metric": metric,
            "reason": reason,
            "value_at_dismissal": baseline.get("value"),
            "suppress_margin_pct": 10.0,
            "active": True,
            "created_at": _now(),
        }
        state["dismissals"].append(record)
        return record

    record = _mutate_state(site_id, _apply)
    return _json({"site_id": site_id, **record})


# --------------------------------------------------------------------------------------------
# 10. post_telegram_alert
# --------------------------------------------------------------------------------------------


async def post_telegram_alert(
    chat_id: int,
    text: str,
    buttons: list[InlineButton],
    reply_to_message_id: int = 0,
    case_id: str = "",
) -> str:
    """Post a message to a Telegram chat via the Bot API sendMessage endpoint and return JSON
    with the sent message_id. This is the only way alerts reach Telegram — the inbound webhook
    handler never sends them.

    buttons is a flat list of {"text": ..., "callback_data": ...} (callback_data must be
    `case:<case_id>:ack` / `:fixed` / `:dismiss`); they are laid out as one inline-keyboard row,
    wrapping to three per row. Pass an empty list for the chase reminder if you want no buttons.
    Pass reply_to_message_id to reply to the original alert (the 72-hour chase does this). Pass
    case_id to have the returned message_id stored on that case automatically. Keep text under
    4096 characters; it is truncated, not split.
    """
    try:
        from agentkernel.core import Config

        token = Config.get().telegram.bot_token
    except Exception:  # noqa: BLE001
        token = os.environ.get("AK_TELEGRAM__BOT_TOKEN", "")
    if not token:
        return _err("AK_TELEGRAM__BOT_TOKEN is not set; cannot post to Telegram", sent=False)

    if len(text) > 4096:
        text = text[:4093] + "..."

    normalised = [b if isinstance(b, InlineButton) else InlineButton(**b) for b in (buttons or [])]
    rows: list[list[dict]] = []
    for i in range(0, len(normalised), 3):
        rows.append([{"text": b.text, "callback_data": b.callback_data} for b in normalised[i : i + 3]])

    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        return _err(f"Telegram sendMessage failed: {exc.response.text}", sent=False)
    except Exception as exc:  # noqa: BLE001
        return _err(f"Telegram sendMessage error: {exc}", sent=False)

    message_id = body.get("result", {}).get("message_id")
    if case_id and message_id is not None:
        site_id, case = _find_case(case_id)
        if site_id is not None:

            def _apply(state: dict[str, Any]) -> None:
                target = state["cases"].get(case_id)
                if target is not None:
                    target["message_id"] = message_id
                    target["chat_id"] = chat_id
                    target["updated_at"] = _now()

            _mutate_state(site_id, _apply)

    return _json({"sent": True, "message_id": message_id, "chat_id": chat_id, "case_id": case_id or None})
