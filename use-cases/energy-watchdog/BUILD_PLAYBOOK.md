# Build Playbook — Energy Anomaly Watchdog in Antigravity

Everything you hand to Antigravity, in order. Prompts are meant to be pasted one at a time, each in
a fresh agent turn, verifying between steps.

---

## 0. Before opening the IDE

```bash
# Fork yaalalabs/agent-kernel on GitHub, every team member stars it, then:
git clone https://github.com/<your-username>/agent-kernel.git
cd agent-kernel
git checkout -b feature/energy-watchdog
mkdir -p use-cases/energy-watchdog
cd use-cases/energy-watchdog

pip install agentkernel
ak skill install --assistant copilot     # installs to .agents/skills — Antigravity's skill path
```

Drop `SPEC.md` and `AGENTS.md` into `use-cases/energy-watchdog/`.

**Open `use-cases/energy-watchdog/` as the Antigravity workspace root — not the repo root.** Skills
and rules resolve from the workspace root; opening the whole repo means the skills pack will not
auto-apply.

Verify before prompting anything: `.agents/skills/` contains `ak-init`, `ak-build`,
`ak-add-capabilities`, `ak-add-integration`, `ak-cloud-deploy`, `ak-test`. If it is empty, the
install went to the wrong directory.

---

## 1. Telegram bot — do this first, it takes five minutes

You need the bot token before the scaffold step, and having a working webhook early removes the
biggest source of late-stage panic.

1. Message **@BotFather** on Telegram, send `/newbot`, name it, copy the token.
2. Create a Telegram **group** for each site, add the bot to it.
3. Turn off privacy mode if the bot must read all group messages: BotFather → `/mybots` → your bot →
   Bot Settings → Group Privacy → Turn off. Re-add the bot to the group afterwards.
4. Get each group's chat id — send any message in the group, then:
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
   ```
   Group chat ids are negative numbers. Note them for `sites.yaml`.
5. Start the tunnel (Telegram requires HTTPS):
   ```bash
   ssh -p443 -R0:localhost:8000 a.pinggy.io
   ```
6. Register the webhook:
   ```bash
   curl -X POST "https://api.telegram.org/bot<TOKEN>/setWebhook" \
     -H "Content-Type: application/json" \
     -d '{"url": "https://<tunnel-host>/telegram/webhook", "secret_token": "<your-secret>"}'
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
7. Put the token and secret in `.env` as `AK_TELEGRAM__BOT_TOKEN` and `AK_TELEGRAM__WEBHOOK_SECRET`.

The tunnel URL changes each time you restart it. Re-run `setWebhook` after every restart — this will
catch you out at least once.

---

## 2. Domain fixtures — write these yourself, before prompting

Do not let the model invent these. Create `data/sites.yaml`:

```yaml
sites:
  - site_id: colombo-plant-1
    name: Colombo Plant 1
    latitude: 6.9271
    longitude: 79.8612
    timezone: Asia/Colombo
    connected_load_kw: 250
    solar_kwp: 100
    telegram_chat_id: -1001234567890
    tariff:
      # Fill from the current CEB commercial tariff schedule. Placeholders below.
      peak_lkr_per_kwh: 0.0
      day_lkr_per_kwh: 0.0
      offpeak_lkr_per_kwh: 0.0
    calendar:
      working_days: [mon, tue, wed, thu, fri, sat]
      closed_dates: ["2026-08-25"]
    thresholds:
      baseload_creep_pct: 15
      baseload_creep_min_kw: 2
      solar_yield_floor_pct: 75
      off_schedule_load_pct: 40
  - site_id: kandy-hotel-1
    # ... a second site with solar_kwp: 0, its own chat id, and no injected faults,
    # to prove that silence works
```

Fill the tariff numbers from the real CEB commercial schedule before you demo. A judge may ask where
the rupee figure comes from.

---

## 3. Prompts, in order

### P1 — scaffold

```
Read AGENTS.md and SPEC.md in this folder, then use the Agent Kernel skills pack in
.agents/skills to scaffold this project.

Create only the project skeleton in this step: pyproject.toml, config.yaml, .env.example,
.gitignore, and empty module stubs for agent.py, tool.py, app.py, demo.py, lambda.py.

Do not implement agent logic or tools yet. Before you write config.yaml, confirm every config
key against the skills pack or https://kernel.yaala.ai/docs and tell me which keys you verified.
```

**Check before continuing:** `config.yaml` has a `sandbox` block with `enabled: true`, a `schedule`
block, an `execution` block with an in-memory queue, a `telegram` block with an `agent` key, and a
`session` block. If `schedule` or `sandbox` is missing, have it fix them — those two are where your
30% comes from.

### P2 — data simulator

```
Implement data/simulate.py as specified in SPEC.md, plus data/sites.yaml loading.

Requirements I want you to get exactly right:
- 15-minute interval resolution.
- The cloud-cover series that attenuates solar generation must be the SAME series that
  get_weather will later return for that site and date. Persist it to data/weather_<site>.csv so
  the two can never disagree.
- Fault injection by CLI flag: baseload_creep, solar_string_failure, holiday_load.
- Generate 90 days for colombo-plant-1 with no faults, and a separate clean run for the second
  site.

Then generate the fixture CSVs into data/ and show me a plot-free summary: daily totals, night
floor by day, and solar yield ratio by day, so I can eyeball that the data is plausible.
```

**Check:** the night floor should be roughly flat; solar yield ratio should dip on cloudy days and
sit high on clear ones. If solar output looks random relative to cloud cover, the two series are not
linked and every later test is worthless.

### P3 — state store and tools

```
Implement tool.py: all ten tools in the SPEC.md tool table, plus the file-backed state store.

State store rules from AGENTS.md: one JSON document per site at state/<site_id>.json holding
baselines, dismissals and cases. Never session state — Telegram sets session_id to the chat id
and the scheduled sweep runs under its own session, so session-held cases would be invisible to
the reply handler. Writes must be atomic (temp file plus rename).

Other rules: tools return JSON strings, load_interval_data writes the CSV into the sandbox
workspace and returns the path plus row count, and get_site_profile must resolve either a
site_id or a telegram chat_id.

post_telegram_alert calls the Bot API sendMessage endpoint with httpx, passes the inline
keyboard in reply_markup, and returns the message_id. Do not route outbound alerts through
AgentTelegramRequestHandler.

No detector maths in this file.
```

### P4 — detectors, sandbox-executed

```
Implement the anomaly_detector agent in agent.py.

It must load the site profile and interval data via tools, then write and execute the detector
analysis in Python using the sandbox run_code tool at run time. The three detectors and their
thresholds are in SPEC.md; thresholds come from the site profile, not from code.

It returns candidate findings only — metric, magnitude, time window, supporting series summary.
It does not estimate cost, does not decide whether to alert, and does not touch Telegram.

Show me the prompt you are giving the agent and the code it generates for the sandbox on a
sample run, so I can check the arithmetic.
```

**Check:** read the generated pandas yourself. This is the part a judge will look at.

### P5 — investigator and case manager

```
Implement the investigator and case_manager agents, plus watchdog_supervisor routing, per
SPEC.md.

investigator classifies each candidate explained / inconclusive / unexplained using weather, the
site calendar, stored baselines, dismissal history, and the same metric on the equivalent day of
the previous two weeks. Explained candidates are dropped and never reach Telegram.

case_manager writes the alert (headline, at least two pieces of evidence, LKR estimate, one
suggested check), opens the case, posts it via post_telegram_alert with an inline keyboard of
three buttons, and schedules the 72-hour chase as a one-time scheduled task.

Button callback_data must be case:<case_id>:ack, case:<case_id>:fixed and
case:<case_id>:dismiss. The handler passes callback_data to the agent as plain message text with
no reference to the source message, so the case id in the payload is the only way to know which
case an action refers to. Never resolve a case by recency.

On dismiss, ask for a one-line reason, then call update_baseline with it.
```

### P6 — wiring and schedules

```
Implement app.py: mount AgentTelegramRequestHandler and ScheduleRESTRequestHandler on the same
server, with OpenAIModule(AGENTS) registered once. The Telegram webhook path is
/telegram/webhook.

Implement demo.py as a local harness that triggers a sweep for a given --site and --date
immediately, bypassing the cron.

Add a small script or make target that registers the recurring daily sweep for each site in
sites.yaml: cron "0 6 * * *", timezone Asia/Colombo, session_mode reuse, user_id = site_id.
Remember a schedule block on a chat request returns HTTP 202, not 200 — treat that as success.
```

### P7 — tests

```
Write the test suite from the Testing section of AGENTS.md using the ak-test skill.

Detector tests must be deterministic against the committed fixture CSVs — no LLM, no network,
no fuzzy matching. Include the negative cases: clean day produces nothing, overcast solar
shortfall is classified explained, a dismissed baseline suppresses the next day, an unknown case
id in a callback fails safely, and a 72-hour-old open case produces exactly one chase.

If a test fails, tell me why. Do not adjust a threshold to make it pass.
```

### P8 — README

```
Write README.md covering exactly these four points, in this order: problem statement, solution
overview, setup instructions, how to run the solution.

Include two specific statements: that the competition build runs on simulated interval data and
how to generate it, and that a production deployment for a Sri Lankan facilities team would use
WhatsApp — Telegram was chosen for the prototype because business-initiated WhatsApp messages
require pre-approved templates and a verified Business Account, while a Telegram bot can post
proactively with no approval.

A judge must be able to clone, install, generate data, and see an alert by following this file
alone. Test your own instructions from a clean checkout before you finish.
```

### P9 — deployment (optional, only if time remains)

```
Use the ak-cloud-deploy skill to add the AWS serverless deployment following the structure of
use-cases/waste-sorting-assistant/deploy: lambda.py, deploy/*.tf, deploy.sh, build.sh.

Deployed config uses queue_mode true, eventbridge schedule provider, dynamodb schedule store,
and DynamoDB for the site state store with site_id as partition key. Do not commit generated
requirements files.
```

---

## 4. Demo script (five minutes, in this order)

1. Show the clean site: run a sweep, nothing posts. Say out loud that silence is the design.
2. Inject a fault live:
   `python data/simulate.py --site colombo-plant-1 --inject baseload_creep --from <date> --magnitude 6kw`
3. Trigger the sweep with `demo.py`. Show the Telegram alert arriving: headline, evidence, LKR
   figure, suggested check, three buttons.
4. Show the sandbox trace — the agent wrote and ran that analysis itself.
5. Press **Dismiss**, give the reason "new server rack installed last week". Show the case dismissed
   and the baseline revised.
6. Run the next day's sweep. It stays quiet on that metric.
7. Inject the solar string failure. It still fires. Close on that.

Record with the phone screen alongside the terminal if you can — a phone lighting up is more
convincing than a desktop client.

---

## 5. Submission checklist

- [ ] Every team member has starred `yaalalabs/agent-kernel`.
- [ ] At least one member has forked it; work lives in `use-cases/energy-watchdog/` in that fork.
- [ ] `README.md` has all four required points.
- [ ] `SPEC.md` and `AGENTS.md` committed — both are listed as optional extras worth submitting, and
      they show the judges how the thing was built.
- [ ] No `.env`, no `state/`, no generated CSVs beyond the small committed fixtures, no
      `requirements.txt`.
- [ ] `make lint-check-all` clean; conventional commit messages.
- [ ] Demo video under 5 minutes with voice-over, link works when logged out.
- [ ] Forked repo link plus every member's GitHub ID submitted before the deadline.
