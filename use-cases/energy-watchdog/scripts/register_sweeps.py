"""Register the recurring daily sweep for every site in data/sites.yaml.

Each sweep is a chat request carrying a `schedule` block, so app.py must be running. A chat
request with a schedule block is deferred, not executed, and acknowledged with HTTP 202 - that
is success, not an error (AGENTS.md "Scheduling").

    uv run python app.py                 # in another terminal
    uv run python scripts/register_sweeps.py

Re-running registers a fresh task each time; use the schedule management routes
(GET/DELETE /api/v1/schedules?user_id=<site_id>) to list or cancel.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sites import load_sites  # noqa: E402

CRON = "0 6 * * *"
TIMEZONE = "Asia/Colombo"

# The occurrence token is substituted by the schedule provider when the task fires; the detector
# converts it to the site timezone and steps back one day to get the day just finished.
PROMPT = (
    "Run the daily sweep for site {site_id}. The sweep occurrence time is "
    "{{ak.schedule.occurrence_time}} in UTC; convert it to {tz}, take the date and subtract one "
    "day to get the target date to analyse."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000", help="app.py base URL")
    parser.add_argument("--sites-file", help="path to sites.yaml")
    args = parser.parse_args(argv)

    sites = load_sites(args.sites_file)
    failures = 0
    for site_id in sites:
        payload = {
            "prompt": PROMPT.format(site_id=site_id, tz=TIMEZONE),
            "agent": "watchdog_supervisor",
            "session_id": f"sweep:{site_id}",
            "user_id": site_id,
            "schedule": {"cron": CRON, "timezone": TIMEZONE, "session_mode": "reuse"},
        }
        try:
            resp = httpx.post(f"{args.url}/api/v1/chat", json=payload, timeout=30.0)
        except httpx.ConnectError:
            print(f"could not reach {args.url} - is app.py running?", file=sys.stderr)
            return 1

        if resp.status_code == 202:
            result = resp.json().get("result", "")
            try:
                task_id = json.loads(result).get("scheduled_task_id", "?")
            except (ValueError, AttributeError):
                task_id = "?"
            print(f"{site_id:18} scheduled  cron={CRON!r} {TIMEZONE}  task_id={task_id}")
        else:
            failures += 1
            print(f"{site_id:18} FAILED  HTTP {resp.status_code}  {resp.text}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
