from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent

app = BedrockAgentCoreApp()

@app.entrypoint
async def invoke(payload, context):
    """Handler for agent invocation with streaming support"""
    session_id = context.session_id
    user_message = payload.get("prompt", "")
    
    agent = Agent(
        #model="us.anthropic.claude-sonnet-4-20250514-v1:0",
        model="amazon.nova-micro-v1:0",
        state={"session_id": session_id}
    )

    async for event in agent.stream_async(user_message):
        yield event

if __name__ == "__main__":
    app.run()   