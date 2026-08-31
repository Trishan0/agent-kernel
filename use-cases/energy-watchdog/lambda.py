"""AWS request-handler entry point.

Chat ingress, the Telegram webhook, and the scheduled-task management routes. On the serverless
target the router is a hand-rolled path/method table (not FastAPI), so AgentTelegramRequestHandler
and ScheduleRESTRequestHandler cannot be mounted directly - each route below calls the same
underlying object (`ChatService`, `ScheduleManager`, the Telegram handler's parser).

The two pipeline workers are separate functions, required by queue_mode:
  - lambda_agent_runner.handler    - consumes the input queue, runs the agents, fires occurrences
  - lambda_response_handler.handler - writes completed responses to the response store

    handler = Lambda.handler
"""

from __future__ import annotations

import json

from agentkernel.aws import Lambda
from agentkernel.openai import OpenAIModule
from agentkernel.schedule import ScheduleManager
from agentkernel.telegram import AgentTelegramRequestHandler

from agent import AGENTS

OpenAIModule(AGENTS)

_telegram = AgentTelegramRequestHandler()


def _manager() -> ScheduleManager:
    manager = ScheduleManager.get()
    if manager is None:
        raise ValueError("Scheduling is not configured. Add a 'schedule' block to config.yaml")
    return manager


def _user_id(event) -> str:
    user_id = (event.get("queryStringParameters") or {}).get("user_id")
    if not user_id:
        raise ValueError("user_id query parameter is required")
    return user_id


@Lambda.register("/telegram/webhook", method="POST")
async def telegram_webhook(event, context):
    """Inbound Telegram updates. Delegates to the shared handler's body parser."""
    body = json.loads(event.get("body") or "{}")
    await _telegram._process_webhook_body(body)
    return {"ok": True}


@Lambda.register("/schedules", method="GET")
def list_schedules(event, context):
    params = event.get("queryStringParameters") or {}
    try:
        page = _manager().list_tasks(
            user_id=_user_id(event),
            limit=int(params["limit"]) if params.get("limit") else None,
            cursor=params.get("cursor"),
        )
    except ValueError as e:
        return 400, {"error": str(e)}
    return {"schedules": [t.model_dump(mode="json") for t in page.tasks], "next_cursor": page.next_cursor}


@Lambda.register("/schedules/get", method="GET")
def get_schedule(event, context):
    params = event.get("queryStringParameters") or {}
    task_id = params.get("task_id")
    if not task_id:
        return 400, {"error": "task_id query parameter is required"}
    try:
        task = _manager().get_task(task_id, user_id=_user_id(event))
    except PermissionError as e:
        return 403, {"error": str(e)}
    except ValueError as e:
        return 400, {"error": str(e)}
    if task is None:
        return 404, {"error": f"Unknown scheduled task {task_id}"}
    return task.model_dump(mode="json")


@Lambda.register("/schedules/amend", method="POST")
def amend_schedule(event, context):
    body = json.loads(event.get("body") or "{}")
    task_id, user_id = body.pop("task_id", None), body.pop("user_id", None)
    if not task_id or not user_id:
        return 400, {"error": "task_id and user_id are required"}
    try:
        task = _manager().update(task_id, body, user_id=user_id)
    except PermissionError as e:
        return 403, {"error": str(e)}
    except KeyError:
        return 404, {"error": f"Unknown scheduled task {task_id}"}
    except ValueError as e:
        return 400, {"error": str(e)}
    return task.model_dump(mode="json")


@Lambda.register("/schedules/cancel", method="POST")
def cancel_schedule(event, context):
    body = json.loads(event.get("body") or "{}")
    task_id, user_id = body.get("task_id"), body.get("user_id")
    if not task_id or not user_id:
        return 400, {"error": "task_id and user_id are required"}
    try:
        task = _manager().cancel(task_id, user_id=user_id)
    except PermissionError as e:
        return 403, {"error": str(e)}
    except KeyError:
        return 404, {"error": f"Unknown scheduled task {task_id}"}
    except ValueError as e:
        return 400, {"error": str(e)}
    return task.model_dump(mode="json")


handler = Lambda.handler
