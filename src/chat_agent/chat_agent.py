import logging
import os
from datetime import datetime
from datetime import timezone as tzinfo
from zoneinfo import ZoneInfo

from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.integrations.strands.bedrock_converter import AgentCoreMemoryConverter
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent, tool
from strands.memory import MemoryManager
from strands.tools.mcp import MCPAgentTool, MCPClient
from strands.vended_memory_stores.bedrock_knowledge_base import BedrockKnowledgeBaseStore

app = BedrockAgentCoreApp()
logger = logging.getLogger(__name__)

# Both provisioned by infra/acdemo/agentcore_runtime.tf as runtime environment variables.
KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID")
MEMORY_ID = os.environ.get("BEDROCK_MEMORY_ID")
# The get_weather MCP tool's Gateway URL -- see infra/acdemo/gateway.tf. Also
# runtime-provisioned; unset locally, which just means no weather tool.
WEATHER_GATEWAY_URL = os.environ.get("WEATHER_GATEWAY_URL")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TITLE_MAX_CHARS = 60

# Must match infra/acdemo/agentcore_runtime.tf's aws_bedrockagentcore_memory_strategy.user_preferences
# namespace_templates -- extraction writes records here, retrieval below reads them back out.
PREFERENCE_NAMESPACE = "/preferences/{actorId}/"

SYSTEM_PROMPT = (
    "If the user's message is preceded by a <user_context> block, treat its contents as known "
    "facts about this user (e.g. favorite team, favorite player) and use them naturally when "
    "relevant, without mentioning the tag itself. Use the get_current_time tool whenever you "
    "need today's date or the current time -- don't guess or rely on training data for it. "
    "Use your weather-lookup tool whenever asked about current weather in a city -- don't "
    "guess or rely on training data for it. If no weather tool is available, say so."
)


@tool
def get_current_time(timezone: str = "UTC") -> str:
    """Get the current date and time.

    Args:
        timezone: IANA timezone name (e.g. "UTC", "US/Pacific", "Europe/London",
            "Asia/Tokyo"). Defaults to UTC.

    Returns:
        The current date and time in ISO 8601 format, e.g. "2026-08-15T14:32:16-07:00".
    """
    try:
        tz = tzinfo.utc if timezone.upper() == "UTC" else ZoneInfo(timezone)
    except Exception as e:
        raise ValueError(f"Unknown timezone {timezone!r}: {e}") from e
    return datetime.now(tz).isoformat(timespec="seconds")


def _build_memory_manager() -> MemoryManager | None:
    """Wire up RAG retrieval against the Bedrock Knowledge Base, if configured.

    Built once at cold start and reused across invocations: the store holds no
    per-conversation state, and hardcoding knowledge_base_type below skips the
    GetKnowledgeBase detection call entirely, so there's nothing here worth
    rebuilding per request.
    """
    if not KNOWLEDGE_BASE_ID:
        return None

    knowledge_base_store = BedrockKnowledgeBaseStore(
        config={
            "knowledge_base_id": KNOWLEDGE_BASE_ID,
            # infra/acdemo/bedrock_kb.tf provisions a VECTOR-type knowledge base
            # (S3 Vectors storage). Setting this explicitly skips a GetKnowledgeBase
            # call the runtime role isn't granted (it only has bedrock:Retrieve).
            "knowledge_base_type": "VECTOR",
        },
        name="knowledge_base",
        description="Team RSS feed articles ingested by feed_ingest.",
        writable=False,
    )
    return MemoryManager(
        stores=[knowledge_base_store],
        # Always retrieve relevant context for the latest message and fold it
        # into the model call, rather than relying on nova-micro to decide when
        # to call a search tool.
        search_tool_config=False,
        injection=True,
    )


memory_manager = _build_memory_manager()


def _memory_session_manager() -> MemorySessionManager | None:
    """Bare read-only client for the list_sessions/list_messages actions --
    unlike _build_session_manager below, these don't need Strands wiring,
    just AgentCore Memory's ListSessions/ListEvents APIs.
    """
    if not MEMORY_ID:
        return None
    return MemorySessionManager(memory_id=MEMORY_ID, region_name=AWS_REGION)


def _build_session_manager(session_id: str, actor_id: str) -> AgentCoreMemorySessionManager | None:
    """Wire up cross-tab/cross-device conversation continuity via AgentCore Memory.

    Unlike memory_manager above, this is inherently per-request: actor_id/
    session_id vary per invocation, so (unlike the KB store) there's nothing
    to usefully share across invocations here.

    actor_id (webapp-<cognito sub>) is passed separately from session_id
    (webapp-<sub>-<conversation id>) so that list_actor_sessions(actor_id)
    can enumerate every conversation a user has -- see
    webapp/agent_client.py's actor_id_for_user / session_id_for_conversation.
    """
    if not MEMORY_ID:
        return None

    return AgentCoreMemorySessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            actor_id=actor_id,
            async_mode=True,  # we invoke via agent.stream_async() below
            retrieval_config={
                # Auto-retrieves matching long-term preference records for this actor before
                # each turn and prepends them to the user's message as a <user_context> block
                # -- see SYSTEM_PROMPT above for how the model is told to use that block.
                PREFERENCE_NAMESPACE: RetrievalConfig(top_k=5, relevance_score=0.3),
            },
        ),
        region_name=AWS_REGION,
    )


def _weather_mcp_client(payload) -> MCPClient | None:
    """MCP client for the weather Gateway, authenticated as the caller.

    Forwards the same bearer token the caller used to invoke this Runtime --
    infra/acdemo/gateway.tf's Gateway trusts the identical Cognito pool/client
    as the Runtime's own custom_jwt_authorizer (agentcore_runtime.tf), so no
    separate credential is minted here.

    Read from `payload["access_token"]`, not the request's Authorization
    header: confirmed empirically (CloudWatch logs showed request_headers
    containing only OTel baggage) that the managed Runtime front door
    consumes the inbound Authorization header for its own JWT validation and
    does not forward it into the container. The body is the reliable path --
    see webapp/agent_client.py's stream_chat and tools/test_agent.sh, both of
    which send the same access token twice: once as the header (for the
    Runtime's own authorizer) and once in the body (for this).

    Returns None if the Gateway isn't configured or the caller sent no
    access_token (e.g. local/dev invocation, or an older client), in which
    case get_weather just won't be in the agent's tool list for this turn --
    logged either way, since silently dropping the tool is otherwise
    indistinguishable from the model just not calling it.
    """
    if not WEATHER_GATEWAY_URL:
        logger.info("WEATHER_GATEWAY_URL is not set -- get_weather won't be offered this turn")
        return None
    access_token = payload.get("access_token")
    if not access_token:
        logger.warning(
            "WEATHER_GATEWAY_URL is set but this request's payload carried no access_token "
            "-- get_weather won't be offered this turn"
        )
        return None
    return MCPClient(lambda: streamablehttp_client(WEATHER_GATEWAY_URL, headers={"Authorization": f"Bearer {access_token}"}))


async def _stream_chat(payload, context):
    """Stream a chat reply. Split out of invoke() so invoke() itself stays a
    plain (non-generator) async function -- see invoke()'s docstring.
    """
    session_id = context.session_id
    # Falls back to session_id when the caller doesn't send actor_id, which
    # preserves pre-multi-chat behavior (tools/test_agent.sh, and any
    # conversation from before multi-chat where the two coincided).
    actor_id = payload.get("actor_id") or session_id
    user_message = payload.get("prompt", "")
    if not user_message.strip():
        # Bedrock's Converse API rejects a blank ContentBlock.text outright, and
        # -- worse -- if a session_manager is attached, stream_async would still
        # persist the blank turn into AgentCore Memory first. That permanently
        # corrupts the conversation: every future turn fails the same
        # ValidationException on replay, since the session manager restores full
        # history (blank turn included) before every request. Reject up front
        # instead, in the same {"error": ...} shape agent_client.stream_chat()
        # already knows how to surface.
        yield {"error": "empty_prompt", "message": "Message text cannot be empty."}
        return
    session_manager = _build_session_manager(session_id, actor_id)

    # mcp_client stays None (and weather_tools empty) whenever the Gateway isn't
    # configured, the caller sent no bearer token, or the connection itself fails
    # -- logged in each case (see _weather_mcp_client and the except below), so a
    # Gateway hiccup degrades this turn to "no weather tool" instead of failing
    # the whole chat outright.
    mcp_client = _weather_mcp_client(payload)
    weather_tools = []
    if mcp_client is not None:
        try:
            mcp_client.start()
            # AgentCore Gateway names every tool "<target>___<tool>" (its own
            # convention, e.g. "get-weather___get_weather" -- see gateway.tf's
            # aws_bedrockagentcore_gateway_target name vs. its inline tool
            # name). Both amazon.nova-micro-v1:0 and amazon.nova-lite-v1:0
            # reproducibly threw modelStreamErrorException ("Model produced
            # invalid sequence as part of ToolUse") on every call to a tool
            # named that way, but had no trouble once given a plain name --
            # re-wrap with one rather than exposing the Gateway's raw name.
            weather_tools = [
                MCPAgentTool(t.mcp_tool, t.mcp_client, name_override="get_weather")
                for t in mcp_client.list_tools_sync()
            ]
            logger.info("Loaded %d tool(s) from the weather Gateway", len(weather_tools))
        except Exception:
            logger.exception("Could not load tools from the weather Gateway -- continuing without one")
            mcp_client = None

    try:
        agent = Agent(
            #model="us.anthropic.claude-sonnet-4-20250514-v1:0",
            #model="amazon.nova-micro-v1:0",  # unreliable MCP tool-use: modelStreamErrorException on every get_weather call
            model="amazon.nova-lite-v1:0",
            system_prompt=SYSTEM_PROMPT,
            tools=[get_current_time, *weather_tools],
            state={"session_id": session_id},
            session_manager=session_manager,
            memory_manager=memory_manager,
            # When memory is configured, session_manager's initialize() hook restores
            # the full prior conversation into agent.messages -- passing history here
            # too would be redundant. Fall back to the client-supplied history only
            # when BEDROCK_MEMORY_ID isn't set, preserving prior behavior for local/dev.
            messages=None if session_manager else payload.get("conversation_history", []),
        )

        async for event in agent.stream_async(user_message):
            yield event
    finally:
        if mcp_client is not None:
            mcp_client.stop(None, None, None)


def _text_from_message(message: dict) -> str:
    """Join a Strands Message's text content blocks into a plain string
    (skipping non-text blocks like toolUse/toolResult/image).
    """
    return "".join(block.get("text", "") for block in message.get("content") or [] if block.get("text"))


def _session_messages(manager: MemorySessionManager, actor_id: str, session_id: str, max_results: int) -> list:
    """Decode a session's raw AgentCore Memory events into Strands SessionMessages.

    AgentCoreMemorySessionManager doesn't store plain text in the
    conversational content.text field -- it JSON-encodes the whole
    SessionMessage (role, content blocks, message_id, redact_message,
    timestamps) there, meant to be read back exactly this way rather than
    displayed directly. This mirrors AgentCoreMemorySessionManager's own
    list_messages() (list_events + events_to_messages, in that order).
    """
    events = manager.list_events(actor_id, session_id, max_results=max_results, include_payload=True)
    return AgentCoreMemoryConverter.events_to_messages(events)


def _title_for_session(manager: MemorySessionManager, actor_id: str, session_id: str) -> str:
    """First user message of the session, truncated -- or a fallback for an
    empty chat. One list_events call per session (O(N) in a user's chat
    count); fine at demo scale.
    """
    for session_message in _session_messages(manager, actor_id, session_id, max_results=100):
        message = session_message.to_message()
        if message.get("role") == "user":
            text = _text_from_message(message).strip()
            if text:
                return text[:TITLE_MAX_CHARS] + ("…" if len(text) > TITLE_MAX_CHARS else "")
    return "New chat"


async def _list_sessions(payload) -> dict:
    """List a user's past conversations, newest first."""
    actor_id = payload.get("actor_id", "")
    manager = _memory_session_manager()
    if manager is None or not actor_id:
        return {"sessions": []}

    summaries = manager.list_actor_sessions(actor_id)
    summaries.sort(key=lambda s: s.get("createdAt"), reverse=True)

    sessions = []
    for summary in summaries:
        session_id = summary["sessionId"]
        created_at = summary.get("createdAt")
        sessions.append(
            {
                "session_id": session_id,
                "created_at": created_at.isoformat() if created_at else None,
                "title": _title_for_session(manager, actor_id, session_id),
            }
        )
    return {"sessions": sessions}


def _long_term_memory(manager: MemorySessionManager | None, actor_id: str) -> list[dict]:
    """This actor's durable USER_PREFERENCE records (see PREFERENCE_NAMESPACE),
    for the webapp's Debug page. Plain list, not the semantic search
    RetrieveMemoryRecords does at chat time via retrieval_config above.
    """
    if manager is None or not actor_id:
        return []

    records = manager.list_long_term_memory_records(
        namespace=PREFERENCE_NAMESPACE.format(actorId=actor_id), max_results=100
    )
    memories = []
    for record in records:
        content = record.get("content") or {}
        created_at = record.get("createdAt")
        memories.append(
            {
                "text": content.get("text", ""),
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return memories


async def _debug_info(payload) -> dict:
    """Backing data for the webapp's Debug page."""
    actor_id = payload.get("actor_id", "")
    manager = _memory_session_manager()
    return {"long_term_memory": _long_term_memory(manager, actor_id)}


async def _list_messages(payload, context) -> dict:
    """Full transcript of one past conversation, chronological."""
    actor_id = payload.get("actor_id", "")
    session_id = context.session_id
    manager = _memory_session_manager()
    if manager is None or not actor_id:
        return {"messages": []}

    messages = []
    for session_message in _session_messages(manager, actor_id, session_id, max_results=200):
        message = session_message.to_message()
        role = message.get("role", "")
        if role not in ("user", "assistant"):
            continue  # skip TOOL/OTHER
        text = _text_from_message(message)
        if text:
            messages.append({"role": role, "text": text})
    return {"messages": messages}


@app.entrypoint
async def invoke(payload, context):
    """Handler for agent invocation with streaming support.

    No `yield` in this function's body -- Python decides "is this a
    generator function" at def-time, and staying plain lets us `return`
    either an async generator (streaming chat -- the runtime detects this
    via inspect.isasyncgen and switches to SSE) or a plain dict (single JSON
    response) for the list_sessions/list_messages actions below.
    """
    action = payload.get("action", "chat")
    if action == "list_sessions":
        return await _list_sessions(payload)
    if action == "list_messages":
        return await _list_messages(payload, context)
    if action == "debug_info":
        return await _debug_info(payload)
    return _stream_chat(payload, context)  # NOT awaited -- see docstring


if __name__ == "__main__":
    app.run()
