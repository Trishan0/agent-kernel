"""Model resolution for the agents.

The stack stays the OpenAI Agents SDK (AGENTS.md rule 5). This picks the concrete model that
SDK talks to from the environment, so the same agent definitions run against Gemini (through its
OpenAI-compatible endpoint) or OpenAI with no code change:

  GEMINI_API_KEY / GOOGLE_API_KEY set -> Gemini via its OpenAI-compatible endpoint, model
                                         AK_AGENT_MODEL (default "gemini-flash-lite-latest")
  else OPENAI_API_KEY                  -> OpenAI, model AK_AGENT_MODEL (default: the SDK default)
  neither                              -> None (SDK default; import still works for offline tests)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Agent Kernel reads .env for its own config model, but the OpenAI Agents SDK and this module
# read GEMINI_API_KEY / OPENAI_API_KEY straight from the process environment - so load .env here.
load_dotenv(Path(__file__).resolve().parent / ".env")

_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@lru_cache(maxsize=1)
def agent_model() -> Any | None:
    """Return a model object / name for `Agent(model=...)`, or None to use the SDK default."""
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if gemini_key:
        from agents import OpenAIChatCompletionsModel, set_tracing_disabled
        from openai import AsyncOpenAI

        # The OpenAI Agents SDK otherwise POSTs traces to OpenAI, which fails without an OpenAI key.
        set_tracing_disabled(True)
        client = AsyncOpenAI(api_key=gemini_key, base_url=_GEMINI_BASE_URL)
        return OpenAIChatCompletionsModel(
            model=os.environ.get("AK_AGENT_MODEL", "gemini-flash-lite-latest"),
            openai_client=client,
        )
    return os.environ.get("AK_AGENT_MODEL") or None
