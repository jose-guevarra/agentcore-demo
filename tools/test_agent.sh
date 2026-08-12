#!/bin/bash

source ./.env.test_agent


curl -X POST \
"https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3Aus-east-1%3A${ACCOUNT_ID}%3Aruntime%2F${AGENTCORE_RUNTIME_ID}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ${SESSION_ID}" \
  -d '{"prompt": "What can you do?", "conversation_history": []}'

