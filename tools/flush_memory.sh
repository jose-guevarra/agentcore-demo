#!/bin/bash
set -e

source ./.env.flush_memory

REGION="${AWS_REGION:-us-east-1}"

echo "Listing events for memory=${MEMORY_ID} actor=${ACTOR_ID} session=${SESSION_ID}..."

# list-events auto-paginates by default, so this walks every page.
EVENT_IDS=$(aws bedrock-agentcore list-events \
  --region "$REGION" \
  --memory-id "$MEMORY_ID" \
  --actor-id "$ACTOR_ID" \
  --session-id "$SESSION_ID" \
  --query 'events[].eventId' \
  --output text)

if [ -z "$EVENT_IDS" ]; then
  echo "No events found. Nothing to flush."
  exit 0
fi

COUNT=0
for EVENT_ID in $EVENT_IDS; do
  echo "Deleting event ${EVENT_ID}..."
  aws bedrock-agentcore delete-event \
    --region "$REGION" \
    --memory-id "$MEMORY_ID" \
    --actor-id "$ACTOR_ID" \
    --session-id "$SESSION_ID" \
    --event-id "$EVENT_ID"
  COUNT=$((COUNT + 1))
done

echo "Flushed ${COUNT} event(s) for actor=${ACTOR_ID} session=${SESSION_ID}."
