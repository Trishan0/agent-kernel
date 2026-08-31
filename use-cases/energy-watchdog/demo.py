"""Local demo harness.

Triggers a sweep for one site and date immediately, bypassing the cron, by posting a plain
(non-scheduled) chat request to a running app.py server. Development and demo aid only, not the
product interface.

    # terminal 1
    uv run python app.py
    # terminal 2
    uv run python demo.py --site colombo-plant-1 --date 2026-08-25

Whatever the sweep finds is posted to the site's Telegram group by the case_manager itself
(post_telegram_alert); this harness only prints the agent's textual reply.
"""

from __future__ import annotations

import argparse
import sys

import httpx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", required=True, help="site_id from data/sites.yaml")
    parser.add_argument("--date", required=True, help="target date YYYY-MM-DD")
    parser.add_argument("--url", default="http://localhost:8000", help="app.py base URL")
    parser.add_argument("--timeout", type=float, default=180.0, help="request timeout in seconds")
    args = parser.parse_args(argv)

    payload = {
        "prompt": f"Run the daily sweep for site {args.site} for {args.date}",
        "agent": "watchdog_supervisor",
        "session_id": f"sweep:{args.site}:{args.date}",
        "user_id": args.site,
    }
    print(f"POST {args.url}/api/v1/chat  ->  {payload['prompt']}")
    try:
        resp = httpx.post(f"{args.url}/api/v1/chat", json=payload, timeout=args.timeout)
    except httpx.ConnectError:
        print(f"could not reach {args.url} - is app.py running?", file=sys.stderr)
        return 1

    print(f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        print(resp.text)
        return 0 if resp.is_success else 1
    print(body.get("result", body))
    return 0 if resp.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
