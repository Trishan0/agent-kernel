# Energy Anomaly Watchdog

A multi-agent Agent Kernel solution that watches interval electricity data (and rooftop-solar
generation, where present) for a site and raises a **Telegram** alert only when it finds waste a
human should act on. Built for the IDEALIZE 2026 mini-competition.

---

## 1. Problem statement

Commercial and industrial sites in Sri Lanka pay for electricity by the kilowatt-hour across
time-of-use tariff bands, and many now have rooftop solar. Waste hides in plain sight in the
15-minute interval data:

- **Night baseload creep** — a pump, compressor or server rack left running raises the 01:00–04:00
  floor by a few kW. Nobody notices; it costs lakhs of rupees a year.
- **Solar yield shortfall** — a failed string or a dirty array quietly loses 20–30% of generation,
  so the site buys daytime power it should have made itself.
- **Off-schedule load** — HVAC or lighting runs through a public holiday or shutdown day.

A dashboard does not help, because nobody watches a dashboard. What a facilities team needs is to
be interrupted **only** when there is something worth acting on, with the evidence and a rupee
figure attached, and to be chased if they ignore it. Everything else should be silence.

### UN Sustainable Development Goals

The watchdog cuts electricity that is being paid for and wasted at commercial and industrial
sites, and keeps rooftop solar generating at capacity. It contributes to:

- **SDG 7 — Affordable and Clean Energy:** improves end-use energy efficiency and protects solar
  self-generation, so a site buys less grid power.
- **SDG 12 — Responsible Consumption and Production:** turns raw interval data into a specific,
  actionable waste finding a facilities team will actually act on.
- **SDG 13 — Climate Action:** every kWh not wasted is grid generation (still substantially
  thermal in Sri Lanka) not burned.

## 2. Solution overview

Four agents, wired with Agent Kernel handoffs, driven by a scheduled morning sweep — not by
requests:

| Agent | Job |
| --- | --- |
| `watchdog_supervisor` | Entry point. Routes the sweep trigger, button callbacks and free-text messages. Never analyses, never posts. |
| `anomaly_detector` | Loads the site profile and interval data via tools, then **writes and runs the detector maths as pandas in the sandbox** (`run_code`) at run time. Returns candidate findings only. Thresholds come from the site profile, never from code. |
| `investigator` | Corroborates each candidate against weather, the site calendar, the stored baseline, the dismissal history and the same metric on the equivalent day of the previous two weeks. Drops anything `explained`. |
| `case_manager` | Writes the alert (headline, ≥2 pieces of evidence, LKR/month estimate, one suggested check), opens a **case**, posts it to the site's Telegram group with three inline buttons, and schedules a one-time 72-hour chase. Handles `ack` / `fixed` / `dismiss`; on dismiss it revises the stored baseline so the same condition is not reported again. |

Supporting pieces:

- **State store** (`tool.py`) — one JSON document per site at `state/<site_id>.json` holding
  baselines, dismissals and cases. Atomic writes (temp file + rename) under a per-site lock. Case
  state is **not** kept in session state: the Telegram session id is the chat id and the scheduled
  sweep runs under its own session, so a case opened by the sweep must be visible to the reply
  handler.
- **Detectors** — the sandbox script the agent runs is mirrored formula-for-formula by
  `detectors.py`, a plain-pandas reference implementation that is the deterministic oracle for the
  test suite (no LLM in that loop).
- **Scheduling** — a recurring daily sweep per site (`cron "0 6 * * *"`, `Asia/Colombo`) and the
  one-time 72-hour chase, both on Agent Kernel's local schedule provider + in-memory queue
  pipeline.

### Simulated data

**The competition build runs on generated interval data.** `data/simulate.py` produces, per site,
a 15-minute `interval_<site>.csv` (load and solar) and a `weather_<site>.csv` (cloud cover and
temperature) — the cloud-cover series that attenuates the solar data is the *same* series
`get_weather` later returns, so the two can never disagree. Generate it with:

```bash
uv run python data/simulate.py --all --start 2026-06-02 --days 90       # or: make data
```

Faults are injected by flag and accumulate until `--reset`:

```bash
uv run python data/simulate.py --site colombo-plant-1 --inject baseload_creep      --from 2026-08-20 --magnitude 6kw
uv run python data/simulate.py --site colombo-plant-1 --inject solar_string_failure --from 2026-08-22 --magnitude 0.7
uv run python data/simulate.py --site colombo-plant-1 --inject holiday_load         --date 2026-08-25
```

### Why Telegram, not WhatsApp

A production deployment for a Sri Lankan facilities team **would use WhatsApp**. It is built on
Telegram here because the product's core behaviour is *unsolicited outbound messaging*:
business-initiated WhatsApp messages require pre-approved message templates and a verified WhatsApp
Business Account, whereas a Telegram bot can post proactively to any chat that has started it, with
no approval process, no templates and no time window — and it supports the inline keyboards the
case buttons need. In production, only the outbound-message tool and the inbound webhook handler
would change.

## 3. Setup instructions

**Prerequisites:** Python 3.12 or 3.13, [`uv`](https://docs.astral.sh/uv/), a Telegram account, and
a Gemini API key (or an OpenAI key).

```bash
# 1. Clone the fork (this project lives in use-cases/energy-watchdog/)
git clone https://github.com/Trishan0/agent-kernel.git
cd agent-kernel/use-cases/energy-watchdog

# 2. Install. agentkernel is pinned to the in-repo framework source (../../ak-py), which
#    carries the scheduling capability this project needs.
uv sync                                      # or: make install

# 3. Telegram bot
#    - message @BotFather, /newbot, copy the token
#    - create a Telegram GROUP per site, add the bot to it
#    - post any message in the group, then read the negative chat id from:
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
#    - put each chat id into data/sites.yaml under telegram_chat_id

# 4. Secrets
cp .env.example .env
#    fill in: GEMINI_API_KEY (or OPENAI_API_KEY), AK_TELEGRAM__BOT_TOKEN, AK_TELEGRAM__WEBHOOK_SECRET

# 5. Generate the interval data
make data

# 6. Run the tests (optional)
make test
```

`data/sites.yaml` carries every per-site value: location, connected load, `solar_kwp`, the Telegram
chat id, the CEB tariff bands, the closed-day calendar and the detector thresholds. The tariff
figures are indicative of the CEB General Purpose 2 (GP-2) Time-of-Use schedule effective October
2025; verify against the current PUCSL/CEB gazette before a production deployment. The
`telegram_chat_id` values are placeholders — set them to your own group chat ids.

## 4. How to run the solution

### Start the server

```bash
uv run python app.py                          # or: make serve
```

This boots one process: the Telegram webhook (`/telegram/webhook`), the schedule management routes
(`/api/v1/schedules`), the in-memory queue pipeline, the agent runner, and the local schedule
provider.

### Expose it to Telegram and register the webhook

Telegram requires HTTPS, so tunnel port 8000 (the URL changes every restart — re-register after
each one):

```bash
ssh -p443 -R0:localhost:8000 a.pinggy.io                              # in another terminal

curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://<tunnel-host>/telegram/webhook", "secret_token": "<AK_TELEGRAM__WEBHOOK_SECRET>"}'
```

### Register the recurring daily sweeps

```bash
uv run python scripts/register_sweeps.py       # or: make register-sweeps
```

One task per site: `cron "0 6 * * *"`, `Asia/Colombo`, `session_mode reuse`, `user_id = site_id`.
A chat request carrying a schedule block is acknowledged with **HTTP 202** — that is success.

### See an alert (without waiting for 06:00)

```bash
# 1. Healthy site: a sweep that finds nothing posts nothing. Silence is the design.
uv run python demo.py --site kandy-hotel-1 --date 2026-08-18

# 2. Inject a fault into the Colombo site's data and sweep the affected day:
uv run python data/simulate.py --site colombo-plant-1 --inject baseload_creep --from 2026-08-20 --magnitude 6kw
uv run python demo.py --site colombo-plant-1 --date 2026-08-21
#    -> a case appears in the Colombo Telegram group: headline, evidence, an LKR/month
#       figure, one suggested check, and [Acknowledge] [Fixed] [Dismiss] buttons.

# 3. Press Dismiss, give a reason ("new server rack installed last week").
#    The case moves to dismissed and the stored baseline is revised.
uv run python demo.py --site colombo-plant-1 --date 2026-08-22   # stays quiet on that metric

# 4. The solar string failure still fires:
uv run python data/simulate.py --site colombo-plant-1 --inject solar_string_failure --from 2026-08-22 --magnitude 0.7
uv run python demo.py --site colombo-plant-1 --date 2026-08-24
```

`demo.py` posts a plain chat request to the running server; the alert itself is sent to Telegram by
`case_manager` calling the Bot API directly (`post_telegram_alert`), not by the reply path.

### Local development

- `uv run python data/simulate.py ...` — regenerate or fault-inject the data.
- `make test` — the deterministic detector / state / schedule suite (no LLM, no server).
  `make test-all` additionally runs the integration + agent-level tests (needs a model key and a
  free port 8000).
- `SPEC.md` and `AGENTS.md` describe what was built and the rules it was built under.
