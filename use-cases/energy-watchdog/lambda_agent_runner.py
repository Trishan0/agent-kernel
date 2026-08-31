"""Agent-runner Lambda: consumes the input queue, runs the agents, sends to the output queue.

Nothing here mentions scheduling. The `schedule` block in deploy/config.yaml is the whole
switch - it makes this function both the one that executes a fired sweep / chase occurrence (it
arrives on the input queue as a plain chat request) and the one that registers new tasks when
case_manager calls create_schedule.
"""

from __future__ import annotations

from agentkernel.aws import ServerlessAgentRunner
from agentkernel.openai import OpenAIModule

from agent import AGENTS

OpenAIModule(AGENTS)

handler = ServerlessAgentRunner.handle
