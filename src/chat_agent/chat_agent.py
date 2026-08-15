import os

from bedrock_agentcore.memory import MemorySessionManager
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.memory import MemoryManager
from strands.vended_memory_stores.bedrock_knowledge_base import BedrockKnowledgeBaseStore

app = BedrockAgentCoreApp()

# Both provisioned by infra/acdemo/agentcore_runtime.tf as runtime environment variables.
KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID")
MEMORY_ID = os.environ.get("BEDROCK_MEMORY_ID")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
TITLE_MAX_CHARS = 60


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
        ),
        region_name=AWS_REGION,
    )


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
    session_manager = _build_session_manager(session_id, actor_id)

    agent = Agent(
        #model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        model="amazon.nova-micro-v1:0",
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


def _title_for_session(manager: MemorySessionManager, actor_id: str, session_id: str) -> str:
    """First user message of the session, truncated -- or a fallback for an
    empty chat. One list_events call per session (O(N) in a user's chat
    count); fine at demo scale.
    """
    events = manager.list_events(actor_id, session_id, max_results=5, include_payload=True)
    for event in events:
        for item in event.get("payload") or []:
            conversational = item.get("conversational")
            if conversational and conversational.get("role") == "USER":
                text = (conversational.get("content", {}).get("text") or "").strip()
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


async def _list_messages(payload, context) -> dict:
    """Full transcript of one past conversation, chronological."""
    actor_id = payload.get("actor_id", "")
    session_id = context.session_id
    manager = _memory_session_manager()
    if manager is None or not actor_id:
        return {"messages": []}

    events = manager.list_events(actor_id, session_id, max_results=200, include_payload=True)
    messages = []
    for event in events:
        for item in event.get("payload") or []:
            conversational = item.get("conversational")
            if not conversational:
                continue  # skip blob payloads (agent state, oversized messages)
            role = (conversational.get("role") or "").lower()
            if role not in ("user", "assistant"):
                continue  # skip TOOL/OTHER
            text = conversational.get("content", {}).get("text", "")
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
    return _stream_chat(payload, context)  # NOT awaited -- see docstring


if __name__ == "__main__":
    app.run()
