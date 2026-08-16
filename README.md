# agentcore-demo

A sports-team chat assistant built on **Amazon Bedrock AgentCore** and the **Strands Agents SDK**. It demonstrates a full production-shaped agent stack: a streaming chat agent with short-term conversational memory and long-term user-preference memory, retrieval-augmented generation (RAG) over an auto-updating RSS ingestion pipeline, a Lambda-backed tool exposed via an AgentCore Gateway (MCP), a Streamlit webapp, Cognito auth, and a Terraform stack that provisions all of it.

## Architecture

```mermaid
flowchart TB
    User(["User"]) --> Webapp["webapp/\nStreamlit UI"]
    Webapp -- "login" --> Cognito[("Cognito\nuser pool")]
    Webapp -- "invoke (bearer token)" --> Runtime["AgentCore Runtime\nsrc/chat_agent"]
    Runtime -- "validates JWT" --> Cognito

    subgraph Agent["chat_agent (Strands Agent, amazon.nova-lite-v1:0)"]
        direction TB
        STM["Short-term memory\n(session events)"]
        LTM["Long-term memory\n(USER_PREFERENCE records)"]
        RAG["RAG retrieval\n(BedrockKnowledgeBaseStore)"]
        Tools["Tools:\nget_current_time (local)\nget_weather (MCP)"]
    end
    Runtime --> Agent

    Agent -- "events in/out" --> Memory[("AgentCore Memory\nchat_memory")]
    Memory --- STM
    Memory --- LTM
    Agent -- "Retrieve" --> KB[("Bedrock Knowledge Base\n(S3 Vectors index)")]
    KB --- RAG
    Agent -- "MCP over HTTPS\n(forwarded bearer token)" --> Gateway["AgentCore Gateway\n(MCP, CUSTOM_JWT)"]
    Gateway -- "validates JWT" --> Cognito
    Gateway --> WeatherLambda["Lambda: weather_tool"]
    WeatherLambda --> OpenMeteo[("Open-Meteo API")]

    EventBridge["EventBridge\n(every 6h)"] --> Scheduler["Lambda:\nfeed_ingest_scheduler"]
    Scheduler -- "reads feed list" --> DDB[("DynamoDB\nfeedsources")]
    Scheduler -- "invoke per feed" --> Ingest["Lambda: feed_ingest"]
    Ingest -- "RSS + scrape + classify\n(nova-lite)" --> S3Src[("S3\nembeddings/")]
    Scheduler -- "StartIngestionJob" --> KB
    S3Src --> KB
```

**Request path:** a user logs into the webapp via Cognito, then every chat message is POSTed to the AgentCore Runtime's HTTPS invoke endpoint with the user's bearer token. The Runtime hosts a Strands `Agent` that, each turn, replays the conversation from short-term memory, pulls in relevant long-term preferences and RAG passages, calls tools as needed, and streams the reply back over SSE.

**Ingestion path (independent of chat):** every 6 hours, EventBridge triggers a scheduler Lambda that reads a table of (team, RSS feed) pairs, fetches and classifies new articles, writes them to S3, and kicks off a Bedrock Knowledge Base ingestion job — so the RAG content refreshes itself without any chat traffic.

## Components

### `src/chat_agent/` — the agent

The core of the system. A [Strands](https://strandsagents.com/) `Agent` running inside a `BedrockAgentCoreApp` (`bedrock-agentcore` SDK), deployed as a container to the Bedrock AgentCore Runtime. See [chat_agent.py](src/chat_agent/chat_agent.py).

- **Model**: `amazon.nova-lite-v1:0` (comments in the code record why `nova-micro` and tool-name conventions like `<target>___<tool>` were rejected — both threw `modelStreamErrorException` on tool calls).
- **Entrypoint** (`invoke`): dispatches on `payload["action"]` — `chat` (default, streamed), `list_sessions`, `list_messages`, `debug_info`. It deliberately stays a non-`async generator` function so the runtime can tell streaming and plain-JSON responses apart via `inspect.isasyncgen`.
- **Streaming**: chat responses are streamed as Server-Sent Events; the webapp reconstructs text deltas from the underlying Bedrock Converse `contentBlockDelta` events.
- **Deployment**: built from [Dockerfile](src/chat_agent/Dockerfile) (Python 3.14, `opentelemetry-instrument` wrapped) and pushed to ECR via [infra/acdemo/tools/upload-agent-to-ecr.sh](infra/acdemo/tools/upload-agent-to-ecr.sh). The Runtime always tracks the ECR `:latest` tag, so pushing a new image is how agent code changes ship — no Terraform apply required.

### `webapp/` — Streamlit chat UI

A Streamlit app ([app.py](webapp/app.py), [agent_client.py](webapp/agent_client.py), [auth.py](webapp/auth.py), [config.py](webapp/config.py)) with four views: login → home (list of past chats) → chat (streaming) → debug (inspects long-term memory records for the logged-in user).

- Talks to the Runtime's invoke HTTPS endpoint directly — no AWS SDK/credentials needed on the client, just a Cognito bearer token.
- Auth uses an **unsigned** boto3 Cognito client (`USER_PASSWORD_AUTH`, with `NEW_PASSWORD_REQUIRED` challenge handling) — end users never need AWS credentials.
- One AgentCore *actor* per Cognito user (`webapp-<sub>`), one AgentCore *session* per chat (`webapp-<sub>-<conversation id>`) — see [Memory](#how-memory-works) below.
- Run locally with `uv run streamlit run webapp/app.py` (see [webapp/README.md](webapp/README.md) for `.env` setup).

### `src/feed_ingest/` — the RAG data pipeline

Two Lambdas that keep the knowledge base fresh:

- [feed_ingest.py](src/feed_ingest/feed_ingest.py) — given one (team, RSS URL) pair: parses the feed, dedupes against S3 marker objects, scrapes each article's text, calls Bedrock (`nova-lite`) to classify relevance and summarize it for that team, and writes a Markdown document + metadata sidecar to `s3://<source-bucket>/embeddings/`.
- [feed_ingest_scheduler.py](src/feed_ingest/feed_ingest_scheduler.py) — reads every row of the `feedsources` DynamoDB table, invokes `feed_ingest` once per row, and — if any new documents were written — calls `bedrock:StartIngestionJob` to re-index the Knowledge Base. Triggered every 6 hours by an EventBridge rule.

### `src/weather_tool/` — the weather MCP tool

A small Lambda ([weather_tool.py](src/weather_tool/weather_tool.py)) that geocodes a city and fetches current conditions from the free Open-Meteo API. It isn't called directly — it's registered as a target behind an AgentCore Gateway, which exposes it to the agent as an MCP tool (`get_weather`).

### `infra/acdemo/` — Terraform stack

Provisions everything above: ECR + IAM for the Runtime, the AgentCore Memory resource + memory strategy, the AgentCore Runtime itself, the Bedrock Knowledge Base + S3 Vectors index, the `feedsources` DynamoDB table, the AgentCore Gateway + Lambda target, the Cognito user pool/client, all three Lambdas, and the EventBridge schedule. See the [Setup](#setup--deployment) section below for how to apply it.

### `tools/` — operational scripts

Shell scripts for exercising and cleaning up AgentCore Memory directly, each paired with an `.env.<name>.template`:

| Script | Purpose |
|---|---|
| [flush_memory.sh](tools/flush_memory.sh) | Delete every short-term event in **one** chat session |
| [flush_all_chats.sh](tools/flush_all_chats.sh) | Delete every session (all chats) for one actor |
| [flush_long_term_memory.sh](tools/flush_long_term_memory.sh) | Delete an actor's long-term `USER_PREFERENCE` records |
| [test_agent.sh](tools/test_agent.sh) | Raw `curl` smoke test against the Runtime invoke endpoint |
| [user_login.sh](tools/user_login.sh) | Mint a Cognito bearer token from the CLI, for use with the above |

## How memory works

The agent uses two distinct tiers of **Bedrock AgentCore Memory**, both backed by a single `aws_bedrockagentcore_memory` resource (`chat_memory`, provisioned in [agentcore_runtime.tf](infra/acdemo/agentcore_runtime.tf)) but serving very different purposes:

**Short-term / conversational memory** — every user and assistant turn is stored as an *event*, keyed by `(memory_id, actor_id, session_id)`, with a 7-day expiry (the minimum the Terraform provider allows). On each invocation, an `AgentCoreMemorySessionManager` attached to the Strands `Agent` transparently replays the full prior event history for that session into `agent.messages` before the turn runs — this is what makes a chat resumable across page loads and devices without the client re-sending its own history. `actor_id` (`webapp-<cognito sub>`) is stable per user, while `session_id` (`webapp-<sub>-<conversation id>`) is one per chat, which is what lets the webapp enumerate a user's full chat list via `list_actor_sessions`.

**Long-term / user-preference memory** — a `USER_PREFERENCE`-type memory strategy (`aws_bedrockagentcore_memory_strategy.user_preferences`) writing into the namespace `/preferences/{actorId}/`. Bedrock automatically extracts durable facts from the conversation (e.g. a favorite team or player) into this namespace — no application code does the extraction. Retrieval is automatic too: the session manager's `retrieval_config` semantically searches this namespace before every turn (`top_k=5`, minimum relevance score `0.3`) and prepends any matches to the user's message as a `<user_context>` block, which the system prompt instructs the model to treat as known facts. This is the personalization layer — it's what lets the agent remember, say, a user's favorite team across brand-new conversations without asking again.

The two tiers differ in retention (7 days vs. indefinite), retrieval style (linear replay vs. relevance-scored semantic search), and purpose (continuity within a chat vs. personalization across chats) — and are exposed through separate AgentCore Memory APIs: `ListEvents`/`DeleteEvent` for short-term, `ListMemoryRecords`/`DeleteMemoryRecord` for long-term. The webapp's Debug page and the `tools/flush_*.sh` scripts operate on exactly these APIs.

## How RAG works

**Ingestion (offline, scheduled, independent of chat traffic):** every 6 hours EventBridge triggers `feed_ingest_scheduler`, which reads the `feedsources` DynamoDB table for (team, RSS URL) pairs and invokes `feed_ingest` once per feed. `feed_ingest` parses the feed, skips articles it's already processed, scrapes the article text, asks `amazon.nova-lite-v1:0` to classify the article's relevance to that team and summarize it, and writes a Markdown document plus a metadata sidecar into the Knowledge Base's S3 source bucket under `embeddings/`. Once new documents exist, the scheduler calls `bedrock:StartIngestionJob`.

**Indexing:** the Bedrock Knowledge Base's S3 data source picks up everything under `embeddings/`, semantically chunks it, embeds it with **Titan Embed Text v2** (1024 dimensions), and stores the vectors in an **S3 Vectors** index (`bedrock_kb.tf`) — this project uses S3 Vectors rather than an OpenSearch Serverless collection for the vector store.

**Retrieval (online, every chat turn):** the agent's `MemoryManager` wraps a `BedrockKnowledgeBaseStore` pointed at the Knowledge Base, configured with `search_tool_config=False` and `injection=True`. That means retrieval is **not** a tool the model chooses to call — relevant passages for the latest user message are unconditionally retrieved and folded into context on every single turn, regardless of whether the message is obviously about ingested content. This sidesteps small models' (`nova-lite`/`nova-micro`) unreliable judgment about *when* to search, at the cost of always paying for a retrieval call.

The knowledge base is intentionally narrow in scope — "team RSS feed articles ingested by feed_ingest" — not a general-purpose document store.

## How the agent's per-turn context is constructed

Memory and RAG above aren't independent features — they're both assembled into a single model call each turn, alongside tool definitions. In order:

1. **System prompt** — static instructions telling the model how to treat a `<user_context>` block, and to always use `get_current_time`/the weather tool rather than guessing.
2. **Restored conversation history** — before the turn runs, the session manager's `initialize()` hook replays every prior event for this `(actor_id, session_id)` into `agent.messages`.
3. **Long-term preference injection** — the session manager semantically searches `/preferences/{actorId}/` for facts relevant to the new message and prepends them to it as a `<user_context>` block.
4. **RAG injection** — separately, the knowledge-base store retrieves passages relevant to the new message and folds them into context, unconditionally, every turn.
5. **The new user message itself**, now wrapped with the `<user_context>` block from step 3.
6. **Tool definitions** — `get_current_time` always; `get_weather` too, if the Gateway and a caller access token are available this turn (it's loaded fresh via MCP on every invocation and renamed from the Gateway's `get-weather___get_weather` to a plain `get_weather`, since the model chokes on tool names containing `___`).

The assembled `Agent` then streams the reply. Afterward, the full turn (including any tool calls/results) is persisted back into short-term memory as new events, and asynchronously mined for new long-term preference facts — closing the loop for the next turn. Notably, steps 3 and 4 are both automatic and unconditional: this project deliberately never leaves it up to `nova-lite`/`nova-micro` to *decide* whether to pull memory or knowledge-base content — it just injects both, every time.

## Auth & identity flow

A single Cognito user pool/client is used three ways: the webapp authenticates end users against it directly; the AgentCore Runtime's `custom_jwt_authorizer` validates every invoke call against it; and the AgentCore Gateway's `custom_jwt_authorizer` validates every MCP call to the weather tool against the *same* pool. Because the managed Runtime front door consumes the inbound `Authorization` header for its own JWT validation and does not forward it into the container, the caller's bearer token is sent **twice** — once as the header, once again inside the JSON payload as `access_token` — so the agent can re-present it when it, in turn, calls the Gateway as the same user.

## Setup / deployment

1. **Prerequisites**: `uv`, Terraform, Docker with `buildx`, and AWS credentials. [bootstrap.sh](bootstrap.sh) installs these on a fresh Amazon Linux dev box.
2. **Provision infrastructure**:
   ```sh
   cd infra/acdemo
   terraform apply
   ```
   Uses [terraform.tfvars](infra/acdemo/terraform.tfvars) — note it pins an AWS SSO `profile`, which you'll need to change for your own environment.
3. **Push the agent image** (needed after provisioning, and after any agent code change):
   ```sh
   cp infra/acdemo/tools/env.agent.template infra/acdemo/tools/.env.agent   # fill in values
   ./infra/acdemo/tools/upload-agent-to-ecr.sh
   ```
   The Runtime always references the ECR `:latest` tag, so this is how new agent code reaches an already-created Runtime without re-applying Terraform.
4. **Configure the webapp**: copy `webapp/.env.example` to `webapp/.env` and fill it in from `terraform -chdir=infra/acdemo output` and `aws sts get-caller-identity`.
5. **Run the webapp**:
   ```sh
   uv run streamlit run webapp/app.py
   ```
6. **Build/test Lambdas**: `src/Makefile` has `*_dist` targets to package each Lambda as a zip (`feed_ingest_dist`, `feed_ingest_scheduler_dist`, `weather_tool_dist`) and matching `*_test` targets to run `pytest` against `tests/`.
7. **Operational scripts**: see the [tools/](#tools--operational-scripts) table above for flushing memory and smoke-testing the agent directly.

## Repo layout

```
src/chat_agent/     the agent (Strands Agent + BedrockAgentCoreApp), deployed as a container
src/feed_ingest/    RSS → S3 ingestion pipeline feeding the RAG knowledge base
src/weather_tool/   Lambda backing the get_weather MCP tool
webapp/             Streamlit chat UI + Cognito auth
infra/acdemo/       Terraform: Runtime, Memory, Knowledge Base, Gateway, Cognito, Lambdas, DynamoDB
tools/              Shell scripts for testing the agent and flushing memory
tests/              pytest tests for the Lambda components
```
