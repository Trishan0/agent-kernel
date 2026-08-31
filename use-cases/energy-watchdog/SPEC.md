# Energy Anomaly Watchdog Specification

## Agent Description

A multi-agent solution that watches interval electricity data (and, where present, rooftop solar
generation data) for a site, and raises a Telegram alert only when it finds waste that a human
should act on.

The system is not request-driven. A scheduled sweep runs once each morning, analyses the previous
day's data inside the sandbox, corroborates any candidate finding against weather and the site
calendar, and decides whether the finding clears the bar for interrupting a person. Findings that
clear the bar become **cases** posted to the site's Telegram group with inline action buttons. A
case stays open until a human presses one; if nobody does, the agent chases it after 72 hours. When
a human dismisses a finding as expected ("that's the new server rack"), the agent revises the stored
baseline for that site so the same condition is not reported again.

The three detectors in scope are night baseload creep, solar yield shortfall, and off-schedule load.

## Channel

**Telegram**, via `AgentTelegramRequestHandler`.

Telegram is chosen because the product's core behaviour is unsolicited outbound messaging. A
Telegram bot may message any chat that has started it, with no approval process, no message
templates, and no time window. The handler also supports inline keyboards and callback queries, so
case actions become buttons instead of free-text the model must interpret.

The README must state that a production deployment for a Sri Lankan facilities team would use
WhatsApp, and that Telegram was chosen for the prototype because business-initiated WhatsApp
messages require pre-approved templates and a verified Business Account.

### Channel topology

- **One Telegram group per site.** The site profile carries `telegram_chat_id`.
- Inbound messages arrive with `session_id = str(chat_id)`. The agent must resolve `chat_id` to
  `site_id` through the site profile before doing anything else.
- Outbound alerts are posted by a tool, not by the inbound handler (see Tools).

### Inline actions

Every alert carries an inline keyboard with three buttons. Telegram delivers the button's
`callback_data` to the agent as if it were message text, and **nothing else** — no reference to the
message it came from. The case id must therefore be inside the callback data:

```
case:<case_id>:ack
case:<case_id>:fixed
case:<case_id>:dismiss
```

`dismiss` prompts the user for a one-line reason, and that reason is what drives the baseline
revision.

## Agents

Build four agents.

### `watchdog_supervisor`

Entry agent. Handles three kinds of input and routes accordingly:

- A scheduled sweep trigger (prompt of the form `Run the daily sweep for site <site_id> for
  <date>`) — hands off to `anomaly_detector`.
- A `case:<id>:<action>` callback payload — hands off to `case_manager`.
- A free-text message in a site group — resolves the site from `chat_id`, then hands off to
  `case_manager` if it relates to an open case, otherwise answers briefly about case status.

It never performs analysis itself and never posts to Telegram itself.

### `anomaly_detector`

Runs the numerical analysis. Loads the site's interval data and site profile, then uses the sandbox
`run_code` tool to compute the three detectors in Python (pandas). It returns a list of **candidate
findings**, each with a metric, a magnitude, a time window, and the supporting series summary. It
does not decide whether a candidate is worth reporting, and it does not estimate cost.

Requirements:

- The analysis must be written and executed by the agent in the sandbox at run time, not hardcoded
  in `tool.py`. The tools supply data; the sandbox does the arithmetic.
- Return an empty candidate list when nothing exceeds the detector thresholds. An empty sweep is a
  valid and expected outcome.

### `investigator`

Takes each candidate finding and gathers corroborating evidence before any alert is considered:

- Weather for the site's location and date (cloud cover, ambient temperature).
- The site calendar (working day, shutdown day, public holiday).
- The stored baseline for that metric, including any human-supplied revisions.
- The dismissal history for that metric on that site.
- The same metric on the equivalent day of the previous two weeks.

It then classifies each candidate as `explained`, `inconclusive`, or `unexplained`, with a written
reason. A candidate that is `explained` (consumption up but ambient temperature up correspondingly;
solar down but cloud cover heavy; load present but the calendar says the site was working) is
dropped here and never reaches Telegram.

### `case_manager`

Owns everything a case does after detection:

- Writes the alert for an `unexplained` finding: one-line headline, the evidence the investigator
  gathered, an estimated monthly cost in LKR, and one concrete suggested check.
- Opens the case record, posts the alert with the inline keyboard, and schedules a 72-hour chase.
- Handles `ack`, `fixed` and `dismiss` callbacks, moving the case through its lifecycle.
- On `dismiss`, asks for a reason, then calls `update_baseline` with it so the condition becomes the
  new normal for that site.
- On the chase firing, posts a short reminder as a reply to the original alert only if the case is
  still `open`, then stops chasing.

## State store

**Case and baseline state must not live in session state.** Telegram inbound sets
`session_id = str(chat_id)`, while a scheduled sweep runs under its own session id. State kept in a
session would be split across the two and the reply handler would not see cases opened by the sweep.

Instead:

- A file-backed store owned by `tool.py`, one JSON document per site under `state/<site_id>.json`,
  holding baselines, dismissals and cases. Reads and writes go through tools only.
- On AWS, the same tools back onto DynamoDB with `site_id` as the partition key.
- Sessions are used for conversational context only.
- Case records must survive across sweeps. A case opened on Monday must still be open, and still
  chaseable, on Thursday.

## Detectors

All three thresholds must be configurable per site in the site profile, with the defaults below.

### 1. Night baseload creep

Compare the median of the 01:00–04:00 load for the target day against the trailing 30-day median of
the same window. Flag when the increase exceeds **15%** and **2 kW** (both, so small sites do not
generate noise). Report the delta in kW and the implied kWh/month.

### 2. Solar yield shortfall

Only for sites with `solar_kwp > 0`. Compare the day's total generation against a clear-sky
expectation for the site's capacity and location, scaled by the day's observed cloud cover. Flag
when actual is below **75%** of the cloud-adjusted expectation. Report actual vs expected kWh and
the shortfall as a percentage.

### 3. Off-schedule load

Flag when mean load during a period the site calendar marks as closed exceeds **40%** of the mean
load during the equivalent hours of the last working day. Report the hours affected and the excess
kWh.

## Functional Requirements

- The user-facing interface is **Telegram**. No web UI, dashboard, or user-facing CLI beyond the
  local demo harness described below.
- The daily sweep is a recurring scheduled task, cron `0 6 * * *`, timezone `Asia/Colombo`,
  `session_mode: reuse`, `user_id` set to the site id.
- The 72-hour case chase is a one-time scheduled task created at case-open time.
- Every alert must carry: the headline finding, at least two pieces of corroborating evidence, an
  estimated monthly cost in LKR, one specific suggested check, and the three inline buttons.
- Alert text must stay under Telegram's 4096-character message limit. If an alert would exceed it,
  shorten the evidence, do not split the message.
- Never post more than one alert per detector per site per day.
- Never post an alert for a condition matching an active dismissal for that site and metric.
- A sweep that finds nothing posts nothing. Silence is the correct output for a healthy site.
- Cost estimates use the tariff bands stored in the site profile. Do not hardcode tariff rates.

## Tools

Implement in `tool.py`:

| Tool | Purpose |
| --- | --- |
| `get_site_profile(site_id_or_chat_id)` | Location, capacity, `solar_kwp`, `telegram_chat_id`, tariff bands, closed-day calendar, per-site thresholds. Must resolve either key. |
| `load_interval_data(site_id, start_date, end_date)` | Write the requested interval data into the sandbox workspace as CSV; return the path plus row count. Must not return the full series. |
| `get_weather(site_id, date)` | Cloud cover and mean ambient temperature for the site's location and date. |
| `get_baseline(site_id, metric)` | Current accepted normal for a metric, including any human revision and its reason. |
| `update_baseline(site_id, metric, value, reason, source)` | Revise a baseline. `source` records human dismissal vs routine drift. |
| `list_cases(site_id, status)` | Open, acknowledged, fixed or dismissed cases for a site. |
| `open_case(site_id, metric, summary, evidence, cost_estimate_lkr, suggested_check)` | Create a case, return its id. |
| `update_case(case_id, status, note)` | Move a case through its lifecycle. |
| `record_dismissal(site_id, metric, reason)` | Suppress future alerts for a metric until exceeded by a further threshold margin. |
| `post_telegram_alert(chat_id, text, buttons, reply_to_message_id)` | Outbound post via the Bot API `sendMessage`, with `reply_markup` for the inline keyboard. Returns `message_id`, which is stored on the case. |

The sandbox tools (`run_code` and the file tools) come from the framework when `sandbox.enabled` is
true and must not be reimplemented.

## Data

The competition build runs on generated data. This must be stated plainly in the README.

Provide `data/simulate.py`, which generates interval data at 15-minute resolution for a configurable
number of days and sites, writing CSV to `data/`. It must produce:

- A plausible commercial load profile with working-day and closed-day shapes, a night floor, and
  weather-correlated daytime variation.
- A solar generation series for sites with `solar_kwp > 0`, following a clear-sky curve attenuated
  by the same cloud-cover series that `get_weather` reports, persisted so the two can never
  disagree.
- Realistic noise, so detectors cannot pass by exact-value matching.

Fault injection by flag, so a fault can be introduced live during a demo:

```
python data/simulate.py --site colombo-plant-1 --inject baseload_creep --from 2026-08-20 --magnitude 6kw
python data/simulate.py --site colombo-plant-1 --inject solar_string_failure --from 2026-08-22 --magnitude 0.7
python data/simulate.py --site colombo-plant-1 --inject holiday_load --date 2026-08-25
```

Also include one clean site with no injected faults, to demonstrate that a healthy site produces no
Telegram traffic.

## Local Development

- `app.py` mounts `AgentTelegramRequestHandler` and `ScheduleRESTRequestHandler` on one server. The
  Telegram webhook is delivered to `/telegram/webhook`.
- `demo.py` is a local harness that triggers a sweep for a given site and date without waiting for
  the cron. Development and demo aid, not the product interface.
- `uv` for dependency management.
- `config.yaml` enables the sandbox, the schedule block (`local` provider, `in_memory` store), the
  in-memory queue transport (scheduling requires the queue pipeline), and the telegram block. Mount
  `ScheduleRESTRequestHandler` so schedules can be listed and cancelled during development.
- Keep generated dependency exports, simulated data, state files, deployment packages, virtual
  environments, and installed coding-agent skills out of Git.

## Testing

- pytest, following the repository's testing conventions.
- Deterministic unit tests for each detector against fixed CSV fixtures with a known injected fault,
  asserting the candidate is raised with the right magnitude.
- A test asserting a clean day produces zero candidates.
- A test asserting an `explained` candidate (solar shortfall on a heavily overcast day) is dropped
  by the investigator and never reaches case creation.
- A test asserting a dismissed baseline suppresses the next day's alert for the same metric.
- A test asserting a `case:<id>:dismiss` callback with an unknown case id fails safely.
- A test asserting a case still open after 72 hours produces exactly one chase, not a loop.
- Agent-level behaviour tests may use the framework's fuzzy or judge comparison modes; the detector
  tests must not.

## Deployment

- Follow the deployment folder structure used by the Agent Kernel AWS serverless examples.
- Single `lambda.py` entry point.
- `queue_mode` true; schedule provider `eventbridge`; schedule store `dynamodb`; state store
  DynamoDB.
- Packaging commands in `deploy/deploy.sh`; do not commit generated requirements files.
- Agent, tool, and detector logic shared between local and deployed execution.

## Out of Scope

- Power factor and maximum-demand analysis.
- Live inverter or utility meter APIs. The data interface is CSV; a real integration would replace
  `load_interval_data` only.
- WhatsApp templates and Business Account verification. Noted in the README as the production path,
  not built.
- Any dashboard or web frontend.
- Automated control of equipment. The system reports and chases; humans act.
