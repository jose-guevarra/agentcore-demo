import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.memory import MemoryManager
from strands.vended_memory_stores.bedrock_knowledge_base import BedrockKnowledgeBaseStore

app = BedrockAgentCoreApp()

# Provisioned by infra/acdemo/agentcore_runtime.tf as a runtime environment variable.
KNOWLEDGE_BASE_ID = os.environ.get("BEDROCK_KNOWLEDGE_BASE_ID")


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


@app.entrypoint
async def invoke(payload, context):
    """Handler for agent invocation with streaming support"""
    session_id = context.session_id
    user_message = payload.get("prompt", "")
    conversation_history = payload.get("conversation_history", [])

    agent = Agent(
        #model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        model="amazon.nova-micro-v1:0",
        state={"session_id": session_id},
        messages=conversation_history,
        memory_manager=memory_manager,
    )

    async for event in agent.stream_async(user_message):
        yield event

if __name__ == "__main__":
    app.run()
