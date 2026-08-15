#!/bin/bash
# Usage: flush_all_chats.sh [--dry-run]
#   --dry-run   List the actor's sessions and exit without deleting anything.
set -e

source ./.env.flush_all_chats

REGION="${AWS_REGION:-us-east-1}"

DRY_RUN=false
if [ "$1" == "--dry-run" ]; then
  DRY_RUN=true
fi

echo "Listing sessions for memory=${MEMORY_ID} actor=${ACTOR_ID}..."

# list-sessions auto-paginates by default, so this walks every page.
SESSION_IDS=$(aws bedrock-agentcore list-sessions \
  --region "$REGION" \
  --memory-id "$MEMORY_ID" \
  --actor-id "$ACTOR_ID" \
  --query 'sessionSummaries[].sessionId' \
  --output text)

if [ -z "$SESSION_IDS" ]; then
  echo "No sessions found. Nothing to flush."
  exit 0
fi

if [ "$DRY_RUN" = true ]; then
  echo "Dry run -- would flush the following session(s) for actor=${ACTOR_ID}:"
  for SESSION_ID in $SESSION_IDS; do
    echo "  ${SESSION_ID}"
  done
  exit 0
fi

SESSION_COUNT=0
EVENT_COUNT=0
for SESSION_ID in $SESSION_IDS; do
  SESSION_COUNT=$((SESSION_COUNT + 1))
  echo "Listing events for session=${SESSION_ID}..."

  EVENT_IDS=$(aws bedrock-agentcore list-events \
    --region "$REGION" \
    --memory-id "$MEMORY_ID" \
    --actor-id "$ACTOR_ID" \
    --session-id "$SESSION_ID" \
    --query 'events[].eventId' \
    --output text)

  for EVENT_ID in $EVENT_IDS; do
    echo "Deleting event ${EVENT_ID} (session=${SESSION_ID})..."
    aws bedrock-agentcore delete-event \
      --region "$REGION" \
      --memory-id "$MEMORY_ID" \
      --actor-id "$ACTOR_ID" \
      --session-id "$SESSION_ID" \
      --event-id "$EVENT_ID"
    EVENT_COUNT=$((EVENT_COUNT + 1))
  done
done

echo "Flushed ${EVENT_COUNT} event(s) across ${SESSION_COUNT} session(s) for actor=${ACTOR_ID}."
