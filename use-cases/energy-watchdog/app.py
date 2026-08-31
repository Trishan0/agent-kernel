"""The real local server.

One process, in-memory queue pipeline (config.yaml: execution.mode rest_sync, queues.type
in_memory), hosting:

  - the pipeline chat route  POST /api/v1/chat          (IOHandler always mounts it)
  - the Telegram webhook     POST /telegram/webhook      (AgentTelegramRequestHandler)
  - schedule management      GET/PUT/DELETE /api/v1/schedules   (ScheduleRESTRequestHandler)

plus the in-process agent runner + response handler + local schedule provider that the
recurring daily sweep and the one-time 72-hour chase need.

    uv run python app.py

The Telegram webhook must be reachable over HTTPS; tunnel port 8000 and register the webhook
(see README / BUILD_PLAYBOOK.md).
"""

from __future__ import annotations

from agentkernel.openai import OpenAIModule
from agentkernel.pipeline import IOHandler
from agentkernel.schedule import ScheduleRESTRequestHandler
from agentkernel.telegram import AgentTelegramRequestHandler

from agent import AGENTS

OpenAIModule(AGENTS)


if __name__ == "__main__":
    IOHandler.run(handlers=[AgentTelegramRequestHandler(), ScheduleRESTRequestHandler()])
