#!/bin/bash
# Usage: flush_long_term_memory.sh [--dry-run]
#   --dry-run   List the actor's long-term memory records and exit without deleting anything.
#
# Unlike flush_memory.sh/flush_all_chats.sh (which delete short-term conversation
# events), this deletes long-term memory *records* -- the durable facts extracted
# by the USER_PREFERENCE strategy (infra/acdemo/agentcore_runtime.tf) into the
# actor's "/preferences/<actor_id>/" namespace. See src/chat_agent/chat_agent.py's
# PREFERENCE_NAMESPACE constant, which this namespace must match.
set -e

source ./.env.flush_long_term_memory

REGION="${AWS_REGION:-us-east-1}"
NAMESPACE="/preferences/${ACTOR_ID}/"

DRY_RUN=false
if [ "$1" == "--dry-run" ]; then
  DRY_RUN=true
fi

echo "Listing long-term memory records for memory=${MEMORY_ID} namespace=${NAMESPACE}..."

# list-memory-records auto-paginates by default, so this walks every page.
RECORD_IDS=$(aws bedrock-agentcore list-memory-records \
  --region "$REGION" \
  --memory-id "$MEMORY_ID" \
  --namespace "$NAMESPACE" \
  --query 'memoryRecordSummaries[].memoryRecordId' \
  --output text)

if [ -z "$RECORD_IDS" ]; then
  echo "No long-term memory records found. Nothing to flush."
  exit 0
fi

if [ "$DRY_RUN" = true ]; then
  echo "Dry run -- would flush the following record(s) for actor=${ACTOR_ID}:"
  for RECORD_ID in $RECORD_IDS; do
    echo "  ${RECORD_ID}"
  done
  exit 0
fi

COUNT=0
for RECORD_ID in $RECORD_IDS; do
  echo "Deleting memory record ${RECORD_ID}..."
  aws bedrock-agentcore delete-memory-record \
    --region "$REGION" \
    --memory-id "$MEMORY_ID" \
    --memory-record-id "$RECORD_ID"
  COUNT=$((COUNT + 1))
done

echo "Flushed ${COUNT} long-term memory record(s) for actor=${ACTOR_ID}."
