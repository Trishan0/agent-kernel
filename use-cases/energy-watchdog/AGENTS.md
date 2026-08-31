# AGENTS.md — Energy Anomaly Watchdog

Rules for any AI coding agent working in this folder. Read `SPEC.md` first; it is the authoritative
description of what to build. This file governs *how* to build it.

## What this project is

An Agent Kernel use case submitted to the IDEALIZE 2026 mini-competition. Four agents watch interval
electricity and rooftop-solar data for a site, run the analysis in the sandbox, decide whether a
finding is worth interrupting a human, and raise a case in a Telegram group that stays open until
someone acts on it.

It is **not** a chatbot, a dashboard, or a report generator.

## Hard rules — do not violate these

1. **The numerical analysis runs in the sandbox.** `anomaly_detector` must use the framework's
   `run_code` tool to execute Python (pandas) over the data at run time. Do **not** implement the
   detector maths inside `tool.py` and have the agent read a precomputed answer. Tools supply data
   and persist state; the sandbox computes. This is the core of the submission.
2. **Telegram is the only user-facing interface.** No web UI, no React, no dashboard, no HTML, no
   user-facing CLI beyond the local demo harness. If you find yourself writing a frontend, stop.
3. **Silence is a valid output.** A sweep that finds nothing posts nothing. Never add filler
   messages, daily "all good" summaries, or status pings to make the system look active.
4. **Never invent Agent Kernel APIs.** Before using any framework class, function, or config key,
   confirm it in `.agents/skills/` or the official docs at https://kernel.yaala.ai/docs. If you
   cannot confirm it, say so and stop rather than guessing a plausible-looking import.
5. **Do not add another agent framework.** No LangChain, no CrewAI, no bare OpenAI SDK calls outside
   the Agent Kernel module pattern. `agentkernel[cli,openai,aws]` and the OpenAI Agents SDK are the
   stack.
6. **All work stays inside `use-cases/energy-watchdog/`.** Never modify files elsewhere in the
   forked repository — not the framework, not other use cases, not the root config.
7. **No hardcoded domain values.** Tariff rates, site capacity, detector thresholds, chat ids and
   the closed-day calendar all come from the site profile fixture. If a number would change between
   two sites, it belongs in config, not in code.

## Stack

- Python 3.12–3.13.x only.
- `uv` for dependency management. `pyproject.toml` with `[tool.uv] package = false`.
- Dependencies: `agentkernel[cli,openai,aws]`, plus `pandas` and `httpx`.
- Dev group: `agentkernel[test]`, `pytest`, `black`, `isort`, `mypy`.

## Agent Kernel conventions

Follow the shape of the existing `use-cases/waste-sorting-assistant/` example.

- Agents are plain OpenAI Agents SDK `Agent` objects in `agent.py`, exported as `AGENTS`, with tools
  bound via `OpenAIToolBuilder.bind([...])`.
- Register once per entry point: `OpenAIModule(AGENTS)`.
- Tools live in `tool.py`, take `ToolContext` from `agentkernel.core` where they need session
  access, and **return JSON strings**, never Python objects.
- Multi-agent routing is done with handoffs from `watchdog_supervisor`. Each specialist needs a
  clear `handoff_description`.

### Entry points — three, with different jobs

| File | Purpose |
| --- | --- |
| `app.py` | The real local server. Mounts `AgentTelegramRequestHandler` and `ScheduleRESTRequestHandler`. This is how the system actually runs. |
| `demo.py` | Local harness that triggers a sweep for a given site and date immediately, bypassing the cron. Development and demo aid only. |
| `lambda.py` | AWS entry point. `handler = Lambda.handler`, nothing else. |

Agent and tool logic is shared by all three. Never fork logic between local and deployed paths.

### Telegram: inbound and outbound are different mechanisms

This trips people up, so it is spelled out.

- **Inbound** (a human replying, or pressing a button) arrives through
  `AgentTelegramRequestHandler`, which serves `/telegram/webhook`. The target agent is set in
  `config.yaml` under `telegram.agent`.
- **Outbound** (the scheduled sweep raising an alert nobody asked for) does **not** go through that
  handler. A scheduled occurrence is a chat request whose reply goes nowhere. You must implement
  `post_telegram_alert(...)` in `tool.py` calling the Bot API `sendMessage` endpoint directly with
  `httpx`, and `case_manager` must call it explicitly. Store the returned `message_id` on the case
  record so the 72-hour chase can reply to the original alert.

If the scheduled sweep produces a finding and nothing appears in Telegram, this is the bug.

### Telegram specifics you must respect

- **Callback data carries everything.** When a user presses an inline button, the handler passes the
  button's `callback_data` to the agent as message text and **nothing else** — no reference to the
  message it came from. The case id must be inside the payload:
  `case:<case_id>:ack` / `case:<case_id>:fixed` / `case:<case_id>:dismiss`. Never rely on "the most
  recent case" or on message context to identify which case an action refers to.
- **Sessions are per chat.** Inbound sets `session_id = str(chat_id)`. Resolve `chat_id` to
  `site_id` via the site profile before doing anything site-specific.
- **4096-character message limit.** Keep alerts well under it. If an alert would exceed the limit,
  shorten the evidence rather than splitting the message.
- Inline keyboards go in `reply_markup` as `inline_keyboard` rows.

### State store — not session state

Case and baseline state must **not** live in session state. Telegram inbound uses the chat id as
session id, while a scheduled sweep runs under its own session. State kept in a session would be
split across the two, and the reply handler would not see cases opened by the sweep.

- Use a file-backed store owned by `tool.py`: one JSON document per site at `state/<site_id>.json`,
  holding baselines, dismissals and cases. All access through tools.
- On AWS the same tools back onto DynamoDB, partition key `site_id`.
- Sessions carry conversational context only.
- Writes must be atomic — write to a temp file and rename. Two schedules can fire close together.

### Scheduling

- Scheduling requires the queue pipeline. Locally: `execution.mode: rest_sync` with
  `execution.queues.type: in_memory`. On AWS: `queue_mode: true`.
- Config needs a `schedule` block: `provider.type: local` and `store.type: in_memory` for
  development; `eventbridge` and `dynamodb` for deployment.
- Cron parsing needs the `cron` extra installed.
- A chat request carrying a `schedule` block is **deferred, not executed**, and acknowledged with
  **HTTP 202**. Do not treat a 202 as an error.
- Daily sweep: recurring, `cron: "0 6 * * *"`, `timezone: "Asia/Colombo"`, `session_mode: reuse`.
  The 72-hour chase is a one-time `at` task created when a case opens.
- `user_id` is required on every scheduled task. Use the site id.

### Sandbox

- `sandbox.enabled: true`, `type: local_subprocess`, scoped to the `anomaly_detector` agent via
  `sandbox.agents`.
- Use a per-session profile so the detector can write intermediate files across a sweep.
- `load_interval_data` must write the CSV into the sandbox workspace and return the **path and row
  count**, never the full series. Interval data is thousands of rows; putting it in the prompt is a
  bug, not a shortcut.

## Configuration and secrets

- Secrets are environment variables only: `OPENAI_API_KEY`, `AK_TELEGRAM__BOT_TOKEN`,
  `AK_TELEGRAM__WEBHOOK_SECRET`, and the weather API key. Never in `config.yaml`, never in code,
  never in a committed `.env`.
- Ship a `.env.example` with the key names and empty values.

## Code style

- `black` and `isort` (profile `black`), line length **120**, target `py312`.
- `from __future__ import annotations` at the top of modules.
- Type hints on every function signature.
- Docstrings on tools — the model reads them to decide when to call the tool, so describe *when to
  use it*, not just what it does.
- Conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`.
- Work on a feature branch, never commit directly to `develop`.

## Testing

- `pytest`, run with `uv run pytest`.
- Detector tests must be **deterministic** against committed fixture CSVs with a known injected
  fault. No LLM in the loop, no fuzzy matching, no network.
- Required cases:
  - each detector raises its candidate with the expected magnitude;
  - a clean day produces zero candidates;
  - a solar shortfall on a heavily overcast day is classified `explained` and never reaches case
    creation;
  - a dismissed baseline suppresses the next day's alert for the same metric;
  - a `case:<id>:dismiss` callback with an unknown case id fails safely;
  - a case still `open` after 72 hours produces exactly one chase, not a loop.
- Agent-level behaviour tests may use the framework's fuzzy or judge modes. Detector tests may not.

## Never do these

- Write a dashboard, chart image, or HTML report.
- Precompute detector results in `tool.py`.
- Add an "all clear" daily message.
- Hardcode LKR tariff rates, kWp capacity, or chat ids.
- Put case or baseline state in session state.
- Identify a case by recency instead of by the id in the callback payload.
- Commit `.env`, `state/`, generated CSVs, `requirements.txt`, `.venv/`, or installed skill files.
- Call the OpenAI API or the Telegram Bot API for inbound handling directly, bypassing the framework.
- Widen scope into power factor, maximum demand, live inverter APIs, WhatsApp templates, or
  equipment control — explicitly out of scope in `SPEC.md`.
- Silently change a threshold to make a test pass. If a test fails, say why.

## Definition of done

- `uv run python demo.py --site colombo-plant-1 --date <date>` runs a full sweep end to end.
- With a fault injected, a case appears in the site's Telegram group carrying evidence, an LKR
  estimate, one suggested check, and three inline buttons.
- Pressing **Dismiss** and giving a reason moves the case to `dismissed` and revises the stored
  baseline; the next day's sweep does not re-raise it.
- A clean site produces no Telegram traffic at all.
- `uv run pytest` passes.
- `README.md` covers the four required points: problem statement, solution overview, setup
  instructions, how to run — and states that the build runs on simulated data and that WhatsApp is
  the production channel.
