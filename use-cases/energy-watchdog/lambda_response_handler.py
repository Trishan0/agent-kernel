"""Response-handler Lambda: writes completed agent responses to the response store."""

from __future__ import annotations

from agentkernel.aws import ResponseHandler

handler = ResponseHandler.handle
