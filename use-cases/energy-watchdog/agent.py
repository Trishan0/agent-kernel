"""Agent definitions for the Energy Anomaly Watchdog.

Four agents, per SPEC.md "Agents":
  - watchdog_supervisor : entry agent, routes sweeps / callbacks / free text (handoffs only)   [P5]
  - anomaly_detector    : runs the three detectors in the sandbox, returns candidate findings   [P4]
  - investigator        : corroborates each candidate -> explained / inconclusive / unexplained  [P5]
  - case_manager        : writes the alert, opens the case, posts it, schedules the 72h chase    [P5]

AGENTS.md hard rule 1: the detector maths runs in the sandbox. anomaly_detector fills the
PARAMETERS block of the analysis below from its tool calls and runs the whole script through
run_code at run time. detectors.py mirrors this script formula-for-formula and is the
deterministic oracle the test suite checks the sandbox output against (no LLM in that loop).
"""

from __future__ import annotations

from agentkernel.openai import OpenAIToolBuilder
from agents import Agent

from llm import agent_model
from tool import (
    case_chase_time,
    get_baseline,
    get_site_profile,
    get_weather,
    list_cases,
    list_dismissals,
    load_interval_data,
    open_case,
    post_telegram_alert,
    record_dismissal,
    update_baseline,
    update_case,
)

# --------------------------------------------------------------------------------------------
# The analysis anomaly_detector runs in the sandbox via run_code.
#
# Self-contained (pandas + numpy + json only) and already runnable: the agent overwrites the
# values in the PARAMETERS block with the concrete ones from its tool calls and changes nothing
# else. Every formula matches detectors.py; the K_CLOUD / sunrise / sunset / derate constants
# are the shared clear-sky model from solar_model.py.
# --------------------------------------------------------------------------------------------

DETECTOR_SANDBOX_CODE = """
import json
import datetime as dt
import numpy as np
import pandas as pd

# ===== PARAMETERS: the agent overwrites these from get_site_profile / get_weather / the prompt =====
CSV_PATH              = "REPLACE_WITH_PATH_FROM_load_interval_data"
TARGET_DATE           = "REPLACE_WITH_TARGET_DATE"          # YYYY-MM-DD
HAS_SOLAR             = False                               # site profile solar_kwp > 0
SOLAR_KWP             = 0.0                                 # site profile
MEAN_CLOUD            = 0.0                                 # get_weather(site, TARGET_DATE)["cloud_cover_mean"] when HAS_SOLAR, else 0.0
IS_CLOSED_DAY         = False                               # TARGET_DATE weekday not in working_days, or date in closed_dates
REFERENCE_WORKING_DAY = ""                                  # most recent working day before TARGET_DATE; "" when TARGET_DATE is a working day
BASELOAD_CREEP_PCT    = 15.0
BASELOAD_CREEP_MIN_KW = 2.0
SOLAR_YIELD_FLOOR_PCT = 75.0
OFF_SCHEDULE_LOAD_PCT = 40.0
# =================================================================================================

K_CLOUD, SUNRISE_H, SUNSET_H, SYSTEM_DERATE = 0.78, 6.0, 18.3, 0.92
STEP_HOURS, TRAILING_DAYS = 0.25, 30


def clearsky_kw(hod, kwp):
    hod = np.asarray(hod, dtype=float)
    if kwp <= 0:
        return np.zeros_like(hod)
    frac = np.sin(np.pi * (hod - SUNRISE_H) / (SUNSET_H - SUNRISE_H))
    frac = np.where((hod > SUNRISE_H) & (hod < SUNSET_H), np.clip(frac, 0.0, 1.0), 0.0)
    return kwp * frac ** 1.15 * SYSTEM_DERATE


def prev_weeks(series, target_iso, ndigits=1):
    t = dt.date.fromisoformat(target_iso)
    out = {}
    for label, back in (("7d", 7), ("14d", 14)):
        key = (t - dt.timedelta(days=back)).isoformat()
        out[label] = round(float(series.loc[key]), ndigits) if key in series.index else None
    return out


df = pd.read_csv(CSV_PATH)
ts = pd.to_datetime(df["timestamp"])
df["_date"] = ts.dt.strftime("%Y-%m-%d")
df["_hour"] = ts.dt.hour + ts.dt.minute / 60.0
df["load_kw"] = df["load_kw"].astype(float)
if "solar_kw" in df.columns:
    df["solar_kw"] = df["solar_kw"].astype(float)

candidates = []

# ---- detector 1: night baseload creep (median 01:00-04:00 vs trailing 30-day median) ----
night = df[(df["_hour"] >= 1.0) & (df["_hour"] < 4.0)]
per_day = night.groupby("_date")["load_kw"].median()
if TARGET_DATE in per_day.index:
    t = dt.date.fromisoformat(TARGET_DATE)
    prior = [(t - dt.timedelta(days=n)).isoformat() for n in range(1, TRAILING_DAYS + 1)]
    trailing = per_day.reindex(prior).dropna()
    if not trailing.empty:
        target_med = float(per_day.loc[TARGET_DATE])
        base_med = float(trailing.median())
        delta_kw = target_med - base_med
        pct = 100.0 * delta_kw / base_med if base_med else 0.0
        if pct >= BASELOAD_CREEP_PCT and delta_kw >= BASELOAD_CREEP_MIN_KW:
            candidates.append({
                "metric": "night_baseload",
                "detected": True,
                "magnitude": {
                    "delta_kw": round(delta_kw, 3),
                    "pct_increase": round(pct, 2),
                    "implied_kwh_per_month": round(delta_kw * 24 * 30, 1),
                },
                "time_window": {"date": TARGET_DATE, "hours": "01:00-04:00"},
                "series_summary": {
                    "target_day_median_kw": round(target_med, 3),
                    "trailing_30d_median_kw": round(base_med, 3),
                    "trailing_days_used": int(len(trailing)),
                    "same_window_prev_weeks": prev_weeks(per_day, TARGET_DATE, 3),
                },
                "thresholds": {"pct": BASELOAD_CREEP_PCT, "min_kw": BASELOAD_CREEP_MIN_KW},
            })

# ---- detector 2: solar yield shortfall (actual vs cloud-adjusted clear-sky expectation) ----
if HAS_SOLAR:
    day = df[df["_date"] == TARGET_DATE]
    if not day.empty:
        clear_kwh = float(np.sum(clearsky_kw(day["_hour"].to_numpy(), SOLAR_KWP)) * STEP_HOURS)
        expected_kwh = clear_kwh * (1.0 - K_CLOUD * MEAN_CLOUD)
        actual_kwh = float(day["solar_kw"].sum() * STEP_HOURS)
        if expected_kwh > 0:
            ratio = actual_kwh / expected_kwh
            if ratio < SOLAR_YIELD_FLOOR_PCT / 100.0:
                daily_solar_kwh = df.groupby("_date")["solar_kw"].sum() * STEP_HOURS
                candidates.append({
                    "metric": "solar_yield",
                    "detected": True,
                    "magnitude": {
                        "actual_kwh": round(actual_kwh, 1),
                        "expected_kwh": round(expected_kwh, 1),
                        "shortfall_pct": round(100.0 * (1.0 - ratio), 2),
                        "yield_ratio": round(ratio, 3),
                    },
                    "time_window": {"date": TARGET_DATE, "hours": "06:00-18:00"},
                    "series_summary": {
                        "mean_cloud_cover": round(MEAN_CLOUD, 3),
                        "clear_sky_kwh": round(clear_kwh, 1),
                        "same_window_prev_weeks": prev_weeks(daily_solar_kwh, TARGET_DATE),
                    },
                    "thresholds": {"yield_floor_pct": SOLAR_YIELD_FLOOR_PCT},
                })

# ---- detector 3: off-schedule load (closed-day business hours vs last working day) ----
if IS_CLOSED_DAY and REFERENCE_WORKING_DAY:
    rt = OFF_SCHEDULE_LOAD_PCT / 100.0
    def biz(d):
        return df[(df["_date"] == d) & (df["_hour"] >= 8.0) & (df["_hour"] < 18.0)][["_hour", "load_kw"]]
    closed, ref = biz(TARGET_DATE), biz(REFERENCE_WORKING_DAY)
    if not closed.empty and not ref.empty:
        paired = closed.merge(ref, on="_hour", how="inner", suffixes=("_closed", "_ref"))
        closed_mean = float(paired["load_kw_closed"].mean())
        ref_mean = float(paired["load_kw_ref"].mean())
        if ref_mean > 0 and closed_mean / ref_mean > rt:
            over = (paired["load_kw_closed"] - rt * paired["load_kw_ref"]).clip(lower=0)
            biz_all = df[(df["_hour"] >= 8.0) & (df["_hour"] < 18.0)]
            daily_biz_mean = biz_all.groupby("_date")["load_kw"].mean()
            candidates.append({
                "metric": "off_schedule_load",
                "detected": True,
                "magnitude": {
                    "closed_day_mean_kw": round(closed_mean, 3),
                    "reference_day_mean_kw": round(ref_mean, 3),
                    "load_ratio": round(closed_mean / ref_mean, 3),
                    "excess_kwh": round(float(over.sum() * STEP_HOURS), 1),
                    "hours_affected": round(float((paired["load_kw_closed"] > rt * paired["load_kw_ref"]).sum() * STEP_HOURS), 2),
                },
                "time_window": {"date": TARGET_DATE, "hours": "08:00-18:00", "reference_working_day": REFERENCE_WORKING_DAY},
                "series_summary": {
                    "closed_day_business_kwh": round(float(paired["load_kw_closed"].sum() * STEP_HOURS), 1),
                    "reference_day_business_kwh": round(float(paired["load_kw_ref"].sum() * STEP_HOURS), 1),
                    "same_window_prev_weeks": prev_weeks(daily_biz_mean, TARGET_DATE, 3),
                },
                "thresholds": {"off_schedule_load_pct": OFF_SCHEDULE_LOAD_PCT},
            })

print(json.dumps({"candidates": candidates}))
"""

DETECTOR_INSTRUCTIONS = (
    """
You are the anomaly detector for the Energy Anomaly Watchdog. You run the numerical analysis for
one site and one date and return CANDIDATE FINDINGS ONLY. You never estimate cost, never decide
whether something is worth an alert, and never contact Telegram.

Your input is a sweep instruction, in one of two forms:
  (a) "Run the daily sweep for site <site_id> for <YYYY-MM-DD>" - use that date as the target
      date.
  (b) "Run the daily sweep for site <site_id>. The sweep occurrence time is <ISO-8601 UTC> ..."
      - convert that UTC timestamp to Asia/Colombo, take the date part, and subtract one day;
      that is the target date (the recurring 06:00 sweep analyses the day just finished).

Steps, in order:

1. Call get_site_profile(<site_id>). Read: solar_kwp / has_solar, calendar.working_days,
   calendar.closed_dates, and the thresholds block (baseload_creep_pct, baseload_creep_min_kw,
   solar_yield_floor_pct, off_schedule_load_pct).

2. Work out two calendar facts for the target date:
   - is_closed_day: True if the target date's weekday abbreviation (mon/tue/wed/thu/fri/sat/sun)
     is NOT in working_days, OR the date string is in closed_dates.
   - reference_working_day: only when is_closed_day is True. Step back one day at a time from the
     target date to the most recent date whose weekday IS in working_days and which is NOT in
     closed_dates. Otherwise use the empty string.

3. If has_solar is True, call get_weather(<site_id>, <target_date>) and read cloud_cover_mean.
   If has_solar is False, use 0.0.

4. Call load_interval_data(<site_id>, <start_date>, <target_date>) with start_date = the target
   date minus 34 days (enough for the trailing 30-day baseline). Read the returned "path".

5. Call run_code with the analysis script below, having overwritten every value in the
   PARAMETERS block with the concrete values from steps 1-4. Change nothing outside that block.

--- BEGIN ANALYSIS SCRIPT ---
"""
    + DETECTOR_SANDBOX_CODE
    + """
--- END ANALYSIS SCRIPT ---

6. The script prints one line of JSON: {"candidates": [...]}. Parse it. If run_code returns an
   error or the output is not valid JSON, correct the PARAMETERS and call run_code once more; if
   it still fails, report the error plainly and stop.

7. If "candidates" is EMPTY, that is your final answer - return {"candidates": []} and do NOT
   hand off. Silence is the correct result for a healthy site.

8. If there is at least one candidate, hand off to the investigator. Your handoff message must be
   exactly the JSON object {"site_id": "<site_id>", "target_date": "<YYYY-MM-DD>",
   "candidates": [...]} with the candidate list unchanged - do not add, remove, reword or
   re-rank anything, and do not estimate cost or decide whether to alert.
"""
)

INVESTIGATOR_INSTRUCTIONS = """
You are the investigator. You receive JSON of the form
{"site_id": ..., "target_date": ..., "candidates": [...]} from the anomaly detector. For EACH
candidate you gather corroborating evidence, then classify it explained / inconclusive /
unexplained. You never estimate cost, open cases or contact Telegram.

For each candidate, gather:
  - Weather: get_weather(site_id, target_date) - cloud_cover_mean, temp_c_mean/max.
  - Site calendar: get_site_profile(site_id) - working_days, closed_dates.
  - Stored baseline: get_baseline(site_id, <metric>) - value, source, reason, and any
    active_dismissal (value_at_dismissal, suppress_margin_pct, reason).
  - Dismissal history: list_dismissals(site_id, <metric>).
  - The same metric on the equivalent day of the previous two weeks: already in the candidate at
    series_summary.same_window_prev_weeks ({"7d": ..., "14d": ...}).

Dismissal-suppression rule (apply first, for any metric with an active_dismissal): if the
candidate's target-day value is <= value_at_dismissal * (1 + suppress_margin_pct/100), classify
it explained with reason "still covered by the dismissal: <dismissal reason>". If it exceeds
that margin, the condition has worsened past the dismissal - do NOT suppress it on that basis.

Classify:
  - explained  -> a benign cause fully accounts for it. Examples: the dismissal-suppression rule
    above applies; solar_yield shortfall on a day whose cloud_cover_mean is high (>= ~0.6) and
    roughly matches the shortfall; off_schedule_load on a day the calendar actually shows as a
    working day; a consumption rise matched by a corresponding temp_c rise; a value already seen
    at the same level in same_window_prev_weeks (so it is not new).
  - inconclusive -> evidence is mixed or insufficient (e.g. some cloud but not enough to explain
    the full shortfall).
  - unexplained -> no benign cause found; the previous two weeks were normal; no active
    dismissal covers it.

Drop every explained candidate - it never goes further. If NO candidate is inconclusive or
unexplained, reply briefly that everything was explained and do NOT hand off (silence).

Otherwise hand off to the case_manager with a JSON message:
{"site_id": ..., "target_date": ..., "findings": [
   {"candidate": <the original candidate object>,
    "classification": "unexplained" | "inconclusive",
    "reason": "<one or two sentences citing the evidence you gathered>",
    "evidence": ["<short bullet>", "<short bullet>", ...]}   # at least two, drawn from the checks above
]}
"""

CASE_MANAGER_INSTRUCTIONS = """
You are the case manager. You own a case after detection: writing the alert, opening the case,
posting it to Telegram, scheduling the 72-hour chase, and handling the button callbacks. You
never run detector maths.

You handle three kinds of input.

== A) Findings handed over by the investigator ==
Message: {"site_id": ..., "target_date": ..., "findings": [{candidate, classification, reason,
evidence}, ...]}. For EACH finding:

  1. Estimate the monthly cost in LKR from the candidate magnitude and the site tariff
     (get_site_profile -> tariff). Guidance:
       - night_baseload: implied_kwh_per_month * tariff.blended_lkr_per_kwh
       - solar_yield: (expected_kwh - actual_kwh) * 30 * tariff.day_lkr_per_kwh   (lost daytime
         generation you now buy at the day rate)
       - off_schedule_load: excess_kwh * <number of such closed periods per month, assume 4> *
         the applicable tariff rate (peak/day/offpeak by the affected hours; use day rate for
         08:00-18:00)
     Round to a whole number of rupees.
  2. Write the alert text: one-line headline; then the classification and the investigator's
     reason; then at least two evidence bullets; then "Est. monthly cost: LKR <n>"; then
     "Suggested check: <one concrete thing to look at>". Keep the whole message well under 4096
     characters.
  3. open_case(site_id, <metric>, summary=<headline>, evidence=[...], cost_estimate_lkr=<n>,
     suggested_check=<...>). If it returns "created": false a case is already open for that
     metric - do not post again; move on.
  4. If created: post_telegram_alert(chat_id=<site telegram_chat_id>, text=<alert text>,
     buttons=[{"text":"Acknowledge","callback_data":"case:<case_id>:ack"},
              {"text":"Fixed","callback_data":"case:<case_id>:fixed"},
              {"text":"Dismiss","callback_data":"case:<case_id>:dismiss"}],
     case_id=<case_id>). The callback_data MUST be exactly those three strings.
  5. Schedule the chase: call case_chase_time(<case_id>) to get {at, timezone}, then
     create_schedule(prompt="Chase overdue case <case_id> for site <site_id>", at=<at>,
     timezone=<timezone>, session_mode="reuse", agent="watchdog_supervisor"). Record it with
     update_case(<case_id>, "open", note="chase scheduled",
     chase_scheduled_task_id=<task_id>).
  Reply with a short summary of what you posted.

== B) A button callback, delivered as plain text ==
It is exactly one of: case:<case_id>:ack | case:<case_id>:fixed | case:<case_id>:dismiss. The
case_id in that payload is the ONLY way to know which case this is - never guess by recency.
  - :ack   -> update_case(<case_id>, "acknowledged", note="acknowledged from Telegram").
             Reply "Noted - keeping the case open."
  - :fixed -> update_case(<case_id>, "fixed", note="marked fixed from Telegram"). Reply
             "Thanks - closing the case."
  - :dismiss -> update_case(<case_id>, "acknowledged", note="dismiss requested",
             pending="dismiss"). Reply asking for one line on why this is expected.

== C) Free text in a site group ==
Resolve the site with get_site_profile(<chat_id>). Then list_cases(<site_id>) and
list_dismissals is available if needed.
  - If a case for that site has "pending": "dismiss", treat this message as the dismissal
    reason: call update_baseline(<site_id>, <metric>, value=<the target-day value from the
    case evidence / candidate>, reason=<message>, source="human_dismissal"), then
    record_dismissal(<site_id>, <metric>, reason=<message>), then update_case(<case_id>,
    "dismissed", note=<message>, pending=""). Confirm the baseline was revised.
  - Otherwise answer briefly about the status of the site's open cases. Do not post new alerts.

== D) A chase trigger ==
Message: "Chase overdue case <case_id> for site <site_id>". get the case via list_cases. If its
status is still "open", post_telegram_alert(chat_id=<site chat id>, text="Reminder: case
<case_id> is still open after 72 hours - <headline>. <suggested check>", buttons=[<the same
three buttons>], reply_to_message_id=<case.message_id>). If the status is anything else, do
nothing. Either way, reply with one line and stop - the one-time schedule does not repeat.
"""

SUPERVISOR_INSTRUCTIONS = """
You are the watchdog_supervisor, the entry point. You never run analysis and you never post to
Telegram yourself - you route, using handoffs.

- If the message looks like "Run the daily sweep for site <site_id> for <YYYY-MM-DD>", hand off
  to anomaly_detector, passing the message through unchanged.
- If the message is a button callback (case:<case_id>:ack | case:<case_id>:fixed |
  case:<case_id>:dismiss) or a chase ("Chase overdue case <case_id> ..."), hand off to
  case_manager, passing the message through unchanged.
- Otherwise it is free text from a site group. Hand off to case_manager so it can resolve the
  site from the chat id and either handle a pending dismissal reason or answer about case
  status.
"""

anomaly_detector = Agent(
    name="anomaly_detector",
    model=agent_model(),
    handoff_description=(
        "Runs the three energy detectors (night baseload creep, solar yield shortfall, "
        "off-schedule load) for one site and date in the sandbox and returns candidate findings."
    ),
    instructions=DETECTOR_INSTRUCTIONS,
    tools=OpenAIToolBuilder.bind([get_site_profile, get_weather, load_interval_data]),
)

investigator = Agent(
    name="investigator",
    model=agent_model(),
    handoff_description=(
        "Corroborates each candidate finding against weather, the site calendar, stored "
        "baselines, dismissal history and the previous two weeks; drops the explained ones."
    ),
    instructions=INVESTIGATOR_INSTRUCTIONS,
    tools=OpenAIToolBuilder.bind([get_site_profile, get_weather, get_baseline, list_dismissals, list_cases]),
)

case_manager = Agent(
    name="case_manager",
    model=agent_model(),
    handoff_description=(
        "Writes the alert, opens the case, posts it to Telegram with the three inline buttons, "
        "schedules the 72-hour chase, and handles ack / fixed / dismiss callbacks."
    ),
    instructions=CASE_MANAGER_INSTRUCTIONS,
    tools=OpenAIToolBuilder.bind(
        [
            get_site_profile,
            open_case,
            update_case,
            list_cases,
            list_dismissals,
            record_dismissal,
            update_baseline,
            post_telegram_alert,
            case_chase_time,
        ]
    ),
)

watchdog_supervisor = Agent(
    name="watchdog_supervisor",
    model=agent_model(),
    handoff_description="Entry agent: routes sweeps, callbacks and free text to the right specialist.",
    instructions=SUPERVISOR_INSTRUCTIONS,
    handoffs=[anomaly_detector, case_manager],
)

anomaly_detector.handoffs = [investigator]
investigator.handoffs = [case_manager]

AGENTS = [watchdog_supervisor, anomaly_detector, investigator, case_manager]
