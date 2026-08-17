"""Per-game pregame report builder Lambda.

For one upcoming game (visiting team @ home team): pull each team's recent
knowledge-base coverage (the same team-tagged news `feed_ingest.py` writes),
ask Bedrock to synthesize a matchup report from it, and write the result as a
markdown document + metadata sidecar to the knowledge base's S3 data source --
under a prefix separate from `feed_ingest.py`'s own, so pregame reports stay
distinguishable from regular news articles even though both feed the same KB.

Unlike `feed_ingest.py`, this Lambda only ever calls `boto3` (bedrock-agent-
runtime for retrieval, bedrock-runtime for synthesis, s3 for the write) -- no
HTML scraping, no feed parsing -- so it ships with no compiled third-party
requirements, the same way `weather_tool.py` does.

This Lambda is upload-only -- it never touches the knowledge base's ingestion
job; that is `pregame_report_scheduler.py`'s responsibility, once it has run
this function for every game due soon.
"""

import json
import os
import re
from datetime import datetime, timezone

import boto3

REGION = "us-east-1"

bedrock_agent_runtime = boto3.client(service_name="bedrock-agent-runtime", region_name=REGION)
bedrock_runtime = boto3.client(service_name="bedrock-runtime", region_name=REGION)
s3_client = boto3.client(service_name="s3", region_name=REGION)

# Same model feed_ingest.py uses for its own Bedrock calls.
MODEL_ID = "amazon.nova-lite-v1:0"

# The knowledge base backing this stack is provisioned VECTOR-type (S3 Vectors
# storage, infra/acdemo/bedrock_kb.tf) -- hardcoding this the same way
# chat_agent.py does skips a GetKnowledgeBase call this Lambda's role isn't
# granted (it only needs bedrock:Retrieve).
KNOWLEDGE_BASE_TYPE = "VECTOR"

# Defaults, overridable per-invocation via the event or environment variables.
MAX_RESULTS_PER_TEAM = 8
DEST_BUCKET = "acdemo-dev-source-bucket"  # fallback only; see feed_ingest.py's identical constant
DEST_PREFIX = "embeddings/pregame_reports/"

MAX_CONTEXT_CHARS_PER_TEAM = 12_000  # budget for the retrieved context sent to Nova per team


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def resolve_config(event):
    """Resolve settings as event value -> env var -> constant default.

    `game_id`, `visiting_team`, `home_team`, `game_time` have no constant
    default -- they resolve to None if neither the event nor the environment
    supplies one, and lambda_handler() fails the invocation rather than
    silently building a report for the wrong game.
    """
    event = event or {}
    return {
        "game_id": event.get("game_id") or os.environ.get("GAME_ID") or None,
        "visiting_team": event.get("visiting_team") or os.environ.get("VISITING_TEAM") or None,
        "home_team": event.get("home_team") or os.environ.get("HOME_TEAM") or None,
        "game_time": event.get("game_time") or os.environ.get("GAME_TIME") or None,
        "week_type": event.get("week_type") or os.environ.get("WEEK_TYPE") or "",
        "week_number": event.get("week_number") or os.environ.get("WEEK_NUMBER") or "",
        "year": event.get("year") or os.environ.get("YEAR") or "",
        "knowledge_base_id": event.get("knowledge_base_id") or os.environ.get("KNOWLEDGE_BASE_ID") or None,
        "max_results_per_team": int(
            event.get("max_results_per_team", os.environ.get("MAX_RESULTS_PER_TEAM", MAX_RESULTS_PER_TEAM))
        ),
        "bucket": event.get("bucket") or os.environ.get("DEST_BUCKET", DEST_BUCKET),
        "prefix": event.get("prefix") or os.environ.get("DEST_PREFIX", DEST_PREFIX),
    }


# --------------------------------------------------------------------------
# Knowledge base retrieval
# --------------------------------------------------------------------------

def retrieve_team_context(knowledge_base_id, team_name, max_results):
    """Chunks tagged team_name=<team_name> in the knowledge base, newest-ranked first.

    Same low-level call strands' BedrockKnowledgeBaseStore.search() wraps
    (bedrock-agent-runtime Retrieve with a vectorSearchConfiguration filter) --
    called directly here since this Lambda has no Strands Agent to hang a
    MemoryManager off of. Returns [] (not an error) when nothing matches --
    an unseeded team or a team_name that doesn't match feed_ingest.py's rows
    is a normal, expected case here, not a failure.
    """
    response = bedrock_agent_runtime.retrieve(
        knowledgeBaseId=knowledge_base_id,
        retrievalQuery={"text": f"{team_name} news, injuries, and matchup notes"},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": max_results,
                "filter": {"equals": {"key": "team_name", "value": team_name}},
            }
        },
    )
    return [r.get("content", {}).get("text", "") for r in response.get("retrievalResults") or [] if r.get("content")]


def build_team_context_block(team_name, chunks):
    """Join a team's retrieved chunks into one labeled, length-capped block for the synthesis prompt."""
    if not chunks:
        return f"### {team_name}\n(No recent coverage found in the knowledge base for this team.)\n"
    joined = "\n\n---\n\n".join(chunks)
    return f"### {team_name}\n{joined[:MAX_CONTEXT_CHARS_PER_TEAM]}\n"


# --------------------------------------------------------------------------
# Report synthesis
# --------------------------------------------------------------------------

def build_synthesis_prompt(config, visiting_context, home_context):
    visiting_team, home_team = config["visiting_team"], config["home_team"]
    matchup_line = f"{visiting_team} (visiting) @ {home_team} (home)"
    if config["game_time"]:
        matchup_line += f", kickoff {config['game_time']}"
    if config["week_type"] or config["week_number"]:
        matchup_line += f" -- {config['week_type']} {config['week_number']}".strip()

    return (
        "You are an NFL beat reporter writing a pregame report. Use ONLY the knowledge-base "
        "excerpts below as your factual source for team news, injuries, and storylines -- do not "
        "invent specific facts (injury statuses, stats, depth-chart details) that aren't supported "
        "by the excerpts. Where the excerpts don't cover something, say so plainly (e.g. \"no "
        "notable injuries reported in available coverage\") rather than guessing. You may draw on "
        "general football knowledge only for structural analysis (e.g. typical offense-vs-defense "
        "reasoning), never for specific facts about these two teams' current rosters or health.\n\n"
        f"Matchup: {matchup_line}\n\n"
        f"Knowledge base excerpts -- {visiting_team}:\n{visiting_context}\n\n"
        f"Knowledge base excerpts -- {home_team}:\n{home_context}\n\n"
        "Write a markdown report with exactly these sections, in this order:\n"
        "1. `## Matchup Overview` -- one short paragraph framing the game.\n"
        f"2. `## {visiting_team}` -- recent form and storylines.\n"
        f"3. `## {home_team}` -- recent form and storylines.\n"
        "4. `## Key Player Matchups` -- the individual matchups most worth watching.\n"
        "5. `## Offense vs. Defense` -- how each team's offense fares against the other's defense.\n"
        "6. `## Notable Injuries` -- for both teams; explicitly say if none are reported.\n"
        "7. `## Outlook` -- a short closing take.\n"
        "Respond with ONLY the markdown report -- no preamble, no code fences."
    )


def synthesize_report(config, visiting_context, home_context):
    """One Bedrock converse call. Raises on API/network failure (caller treats as a run failure)."""
    prompt_text = build_synthesis_prompt(config, visiting_context, home_context)
    response = bedrock_runtime.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt_text}]}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0.3},
    )
    return response["output"]["message"]["content"][0]["text"].strip()


# --------------------------------------------------------------------------
# Document + metadata
# --------------------------------------------------------------------------

def slugify(text, max_len=120):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-") or "untitled"


def document_key(prefix, game_id):
    return f"{prefix}{slugify(game_id)}.md"


def build_markdown(config, report_body):
    header = (
        f"# Pregame Report: {config['visiting_team']} @ {config['home_team']}\n\n"
        f"**Visiting:** {config['visiting_team']}  \n"
        f"**Home:** {config['home_team']}  \n"
        f"**Kickoff:** {config['game_time'] or 'Unknown'}  \n"
        f"**Game ID:** {config['game_id']}  \n"
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}\n\n"
        "---\n\n"
    )
    return header + report_body.strip() + "\n"


# Same 1024-byte sidecar cap feed_ingest.py works around -- see its
# TITLE_META_MAX_CHARS/URL_META_MAX_CHARS comment. This sidecar carries far
# fewer/smaller fields, but team names are still defensively capped.
TEAM_META_MAX_CHARS = 120


def build_metadata(config):
    def s(value, embed):
        return {"value": {"type": "STRING", "stringValue": str(value)}, "includeForEmbedding": embed}

    attrs = {
        # Embedded so a semantic query naming either team can surface this report,
        # the same way feed_ingest.py embeds team_name on regular articles.
        "visiting_team": s(config["visiting_team"][:TEAM_META_MAX_CHARS], True),
        "home_team": s(config["home_team"][:TEAM_META_MAX_CHARS], True),
        # Not embedded -- filter-only, distinguishing this from a regular
        # feed_ingest.py article at retrieval time (in addition to living
        # under a separate S3 prefix).
        "doc_type": s("pregame_report", False),
        "game_id": s(config["game_id"], False),
        "game_time": s(config["game_time"] or "", False),
    }
    return {"metadataAttributes": attrs}


def put_document(bucket, doc_key, markdown, metadata):
    s3_client.put_object(
        Bucket=bucket, Key=doc_key, Body=markdown.encode("utf-8"), ContentType="text/markdown"
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=f"{doc_key}.metadata.json",
        Body=json.dumps(metadata).encode("utf-8"),
        ContentType="application/json",
    )


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------

def _run(config, run_at):
    visiting_chunks = retrieve_team_context(
        config["knowledge_base_id"], config["visiting_team"], config["max_results_per_team"]
    )
    home_chunks = retrieve_team_context(
        config["knowledge_base_id"], config["home_team"], config["max_results_per_team"]
    )
    visiting_context = build_team_context_block(config["visiting_team"], visiting_chunks)
    home_context = build_team_context_block(config["home_team"], home_chunks)

    report_body = synthesize_report(config, visiting_context, home_context)
    markdown = build_markdown(config, report_body)
    metadata = build_metadata(config)

    doc_key = document_key(config["prefix"], config["game_id"])
    # Deterministic key -> re-running for the same game overwrites the prior
    # report rather than accumulating duplicates, so a report generated days
    # out gets refreshed with newer news as the game approaches. Unlike
    # feed_ingest.py's one-shot articles, there's no dedup marker to check.
    put_document(config["bucket"], doc_key, markdown, metadata)

    return {
        "game_id": config["game_id"],
        "visiting_team": config["visiting_team"],
        "home_team": config["home_team"],
        "game_time": config["game_time"],
        "run_at": run_at,
        "visiting_chunks_found": len(visiting_chunks),
        "home_chunks_found": len(home_chunks),
        "documents_written": 1,
        "s3_key": doc_key,
    }


def lambda_handler(event, context):
    config = resolve_config(event)
    run_at = datetime.now(timezone.utc).isoformat()

    missing = [k for k in ("game_id", "visiting_team", "home_team", "knowledge_base_id") if not config[k]]
    if missing:
        error = f"missing required event value(s): {', '.join(missing)}"
        print(f"ERROR: pregame_report invocation rejected: {error}")
        return {
            "statusCode": 500,
            "body": json.dumps({"game_id": config["game_id"], "error": error}),
        }

    try:
        report = _run(config, run_at)
        print(
            f"=== pregame_report: {report['game_id']} ({report['visiting_team']} @ {report['home_team']}) === "
            f"visiting_chunks={report['visiting_chunks_found']} home_chunks={report['home_chunks_found']} "
            f"-> {report['s3_key']}"
        )
        return {"statusCode": 200, "body": json.dumps(report)}
    except Exception as e:  # noqa: BLE001 - whole-run failure (Bedrock/S3 error), retried next scheduler run
        print(f"ERROR: pregame_report run failed for {config['game_id']}: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"game_id": config["game_id"], "error": str(e)}),
        }
