"""A case still open after 72 hours produces exactly one chase, not a loop.

The chase is a one-time `at` schedule. Agent Kernel closes a one-time task after its single
occurrence (ScheduleManager.record_trigger passes completed = spec.at is not None), so it can
never re-arm. These tests pin that behaviour without an LLM.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from agentkernel.core.model import ScheduleSpec
from agentkernel.openai import OpenAIModule
from agentkernel.schedule import ScheduleManager

import agent

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _register_agents():
    # ScheduleManager.create validates the named agent exists in the Runtime registry.
    OpenAIModule(agent.AGENTS)


@pytest.fixture()
def manager():
    mgr = ScheduleManager.get()
    assert mgr is not None, "schedule block missing from config.yaml"
    return mgr


def test_one_time_chase_completes_after_a_single_trigger(manager):
    at = (dt.datetime.now() + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    task = manager.create(
        user_id="colombo-plant-1",
        prompt="Chase overdue case nbl-testcase for site colombo-plant-1",
        spec=ScheduleSpec(at=at, timezone="Asia/Colombo", session_mode="reuse"),
        agent="watchdog_supervisor",
        session_id="sweep:colombo-plant-1",
    )
    try:
        # Simulate the pipeline recording the delivered occurrence (what happens when it fires).
        manager.record_trigger(task.task_id, user_id="colombo-plant-1", request_id="req-1")
        after = manager.get_task(task.task_id, user_id="colombo-plant-1")
        assert after.trigger_count == 1
        assert after.status.value == "completed"  # one-time -> closed, cannot fire again

        # A recurring rule would still be "active" here; assert we are not that.
        assert after.spec.cron is None and after.spec.at is not None
    finally:
        try:
            manager.delete(task.task_id, user_id="colombo-plant-1")
        except Exception:
            pass


@pytest.mark.integration
def test_one_time_schedule_fires_once_end_to_end(tmp_path):
    """Full path through a real app.py process: a near-future `at` task fires through the local
    provider and the pipeline records it exactly once, then the task is completed and never
    re-arms. Marked integration - starts a server and waits on the scheduler thread."""
    import json
    import subprocess
    import sys
    import time

    import httpx

    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(30):
            try:
                if httpx.get("http://localhost:8000/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)

        at = (dt.datetime.now() + dt.timedelta(seconds=6)).strftime("%Y-%m-%dT%H:%M:%S")
        resp = httpx.post(
            "http://localhost:8000/api/v1/chat",
            json={
                "prompt": "Chase overdue case nbl-e2e for site colombo-plant-1",
                "agent": "watchdog_supervisor",
                "session_id": "sweep:colombo-plant-1",
                "user_id": "colombo-plant-1",
                "schedule": {"at": at, "timezone": "Asia/Colombo", "session_mode": "reuse"},
            },
            timeout=15,
        )
        assert resp.status_code == 202
        task_id = json.loads(resp.json()["result"])["scheduled_task_id"]

        task = {}
        for _ in range(40):
            task = httpx.get(f"http://localhost:8000/api/v1/schedules/{task_id}", timeout=10).json()
            if task.get("trigger_count", 0) >= 1:
                break
            time.sleep(0.5)
        assert task["trigger_count"] == 1
        assert task["status"] == "completed"

        time.sleep(4)  # a looping schedule would fire again in this window
        again = httpx.get(f"http://localhost:8000/api/v1/schedules/{task_id}", timeout=10).json()
        assert again["trigger_count"] == 1
    finally:
        proc.terminate()
        proc.wait(timeout=15)
