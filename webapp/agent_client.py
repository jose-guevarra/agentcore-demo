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
from typing import Iterator

import requests

from config import Config


class AgentInvocationError(Exception):
    """Raised when the AgentCore runtime call fails outright (non-2xx, network error)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def actor_id_for_user(sub: str) -> str:
    """The AgentCore Memory actor id for this user, tied to their Cognito `sub`.

    Deterministic per user (not per login/browser tab/chat), so every chat a
    person starts is scoped under the same actor and can be enumerated via
    list_chats(). `sub` is a 36-char UUID, so the prefixed id comfortably
    meets AgentCore's 33-100 character session id requirement (actor ids
    reuse that same id space).
    """
    return f"webapp-{sub}"


def session_id_for_conversation(actor_id: str, conversation_id: str) -> str:
    """The AgentCore runtime/memory session id for one specific chat.

    `conversation_id` should be a fresh uuid.uuid4().hex per new chat (see
    webapp/app.py's "+ New chat" handler).
    """
    return f"{actor_id}-{conversation_id}"


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
    actor_id: str,
    session_id: str,
    access_token: str,
) -> Iterator[str]:
    """Stream the assistant's reply as a sequence of text chunks.

    `history` is a list of {"role": "user"|"assistant", "content": [{"text": ...}]}
    messages preceding this turn (not including `prompt` itself). `actor_id`
    is sent so the runtime can record this chat's memory under the user's
    stable actor id rather than defaulting to (session-scoped) session_id --
    see src/chat_agent/chat_agent.py's _stream_chat.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    body = {"prompt": prompt, "conversation_history": history, "actor_id": actor_id}

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


def _post_json(config: Config, headers: dict, body: dict) -> dict:
    """POST a non-streaming action (list_sessions/list_messages) and return the parsed JSON body."""
    try:
        response = requests.post(config.invoke_url, headers=headers, json=body, timeout=30)
    except requests.RequestException as err:
        raise AgentInvocationError(f"Could not reach the agent: {err}") from err

    if response.status_code != 200:
        raise AgentInvocationError(
            f"Agent invocation failed ({response.status_code}): {response.text[:500]}",
            status_code=response.status_code,
        )
    return response.json()


def list_chats(config: Config, actor_id: str, access_token: str) -> list[dict]:
    """Past conversations for this user, newest first.

    Each entry is {"session_id": str, "created_at": iso str | None, "title": str}.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        # Header is required by the invoke API but unused for this action;
        # actor_id doubles as a harmless placeholder value.
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": actor_id,
    }
    return _post_json(config, headers, {"action": "list_sessions", "actor_id": actor_id}).get("sessions", [])


def get_messages(config: Config, actor_id: str, session_id: str, access_token: str) -> list[dict]:
    """Full transcript of one past conversation, chronological.

    Each entry is {"role": "user"|"assistant", "text": str}.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    return _post_json(config, headers, {"action": "list_messages", "actor_id": actor_id}).get("messages", [])
