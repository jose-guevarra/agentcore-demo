import os

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


def _build_session_manager(session_id: str) -> AgentCoreMemorySessionManager | None:
    """Wire up cross-tab/cross-device conversation continuity via AgentCore Memory.

    Unlike memory_manager above, this is inherently per-request: actor_id/
    session_id vary per invocation, so (unlike the KB store) there's nothing
    to usefully share across invocations here.
    """
    if not MEMORY_ID:
        return None

    return AgentCoreMemorySessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id=MEMORY_ID,
            session_id=session_id,
            # session_id is already webapp-<cognito sub> (see webapp/agent_client.py):
            # one continuous thread per user, so actor_id and session_id coincide.
            actor_id=session_id,
            async_mode=True,  # we invoke via agent.stream_async() below
        ),
        region_name=AWS_REGION,
    )


@app.entrypoint
async def invoke(payload, context):
    """Handler for agent invocation with streaming support"""
    session_id = context.session_id
    user_message = payload.get("prompt", "")
    session_manager = _build_session_manager(session_id)

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

if __name__ == "__main__":
    app.run()
