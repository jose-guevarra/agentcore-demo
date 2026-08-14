"""Client for the chat_agent AgentCore Runtime invoke endpoint.

Same contract as tools/test_agent.sh: a POST to the runtime's invocations
URL with a Cognito bearer token, streamed back as text/event-stream.

Confirmed against a live invocation: bedrock_agentcore's SSE encoder can't
cleanly JSON-serialize a Strands TextStreamEvent (it embeds live Python
objects like the Agent instance), so it falls back to `str(obj)` wrapped in
a JSON string -- i.e. `{"data": "<chunk>", ...}` arrives as a JSON string
containing a Python repr, not a JSON object, so it parses back to a `str`,
not a `dict`, and is intentionally skipped below. The reliable path is the
raw Bedrock Converse stream event Strands also emits alongside it:
{"event": {"contentBlockDelta": {"delta": {"text": "<chunk>"}, ...}}}.
"""

from __future__ import annotations

import json
import uuid
from typing import Iterator

import requests

from config import Config


class AgentInvocationError(Exception):
    """Raised when the AgentCore runtime call fails outright (non-2xx, network error)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def new_session_id() -> str:
    """A fresh AgentCore runtime session id, stable for one Streamlit session."""
    return f"webapp-{uuid.uuid4()}"


def to_content_blocks(text: str) -> list[dict]:
    return [{"text": text}]


def _extract_text(event: dict) -> str | None:
    """Pull a text delta out of one parsed SSE event, or None if it's not one."""
    # TextStreamEvent, on the rare occasion it does serialize cleanly.
    data = event.get("data")
    if isinstance(data, str):
        return data

    # The raw Bedrock Converse stream event -- the reliable path in practice.
    inner = event.get("event")
    if isinstance(inner, dict):
        delta = inner.get("contentBlockDelta", {}).get("delta", {})
        text = delta.get("text")
        if isinstance(text, str):
            return text

    return None


def stream_chat(
    config: Config,
    prompt: str,
    history: list[dict],
    session_id: str,
    access_token: str,
) -> Iterator[str]:
    """Stream the assistant's reply as a sequence of text chunks.

    `history` is a list of {"role": "user"|"assistant", "content": [{"text": ...}]}
    messages preceding this turn (not including `prompt` itself).
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    body = {"prompt": prompt, "conversation_history": history}

    try:
        response = requests.post(config.invoke_url, headers=headers, json=body, stream=True, timeout=120)
    except requests.RequestException as err:
        raise AgentInvocationError(f"Could not reach the agent: {err}") from err

    if response.status_code != 200:
        raise AgentInvocationError(
            f"Agent invocation failed ({response.status_code}): {response.text[:500]}",
            status_code=response.status_code,
        )

    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        if "error" in event:
            raise AgentInvocationError(str(event.get("message") or event["error"]))

        text = _extract_text(event)
        if text:
            yield text
