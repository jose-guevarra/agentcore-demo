"""Scheduler Lambda for feed_ingest.

Reads the set of (team, RSS source) rows from the `feedsources` DynamoDB
table, invokes `feed_ingest.py` synchronously once per row -- waiting for
each result before moving to the next -- and after every row has run,
triggers the Bedrock knowledge base's ingestion job so the documents
`feed_ingest.py` wrote actually get indexed.

Results are grouped by team (a team can have more than one source row) into
a combined report that is both the Lambda's response body and printed to
stdout for CloudWatch.
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"

dynamodb_resource = boto3.resource("dynamodb", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=REGION)

FEED_CONFIG_TABLE = "feedsources"
FEED_INGEST_FUNCTION = "feed-ingest"

# Per-row overrides forwarded to feed_ingest.py only if present on the row --
# keeps the scheduler forward-compatible with attributes the table doesn't
# have yet, without requiring a table migration. A row-level value (if ever
# present) wins over the scheduler-level bucket/prefix/state_prefix config.
OPTIONAL_ROW_OVERRIDES = ("lookback_hours", "max_entries", "force", "bucket", "prefix", "state_prefix")


def _json_default(value):
    """json.dumps default= hook: DynamoDB numeric attributes come back as Decimal."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def resolve_config(event):
    event = event or {}
    return {
        "table_name": event.get("table_name") or os.environ.get("FEED_CONFIG_TABLE", FEED_CONFIG_TABLE),
        "handler_function": event.get("handler_function") or os.environ.get(
            "FEED_INGEST_FUNCTION", FEED_INGEST_FUNCTION
        ),
        "knowledge_base_id": event.get("knowledge_base_id") or os.environ.get("KNOWLEDGE_BASE_ID", ""),
        "data_source_id": event.get("data_source_id") or os.environ.get("DATA_SOURCE_ID", ""),
        # Left unset (None) by default so feed_ingest.py falls back to its own
        # production defaults; set to redirect every invoked row's writes at
        # once (e.g. for a test run), without touching per-row table data.
        "bucket": event.get("bucket") or os.environ.get("DEST_BUCKET") or None,
        "prefix": event.get("prefix") or os.environ.get("DEST_PREFIX") or None,
        "state_prefix": event.get("state_prefix") or os.environ.get("STATE_PREFIX") or None,
    }


# --------------------------------------------------------------------------
# Load feed configs
# --------------------------------------------------------------------------

def load_feed_configs(table_name):
    """Every row in `feedsources` (team, url, source, ...), enabled ones only."""
    table = dynamodb_resource.Table(table_name)
    rows = []
    scan_kwargs = {}
    while True:
        response = table.scan(**scan_kwargs)
        rows.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return [row for row in rows if row.get("enabled", True) is not False]


# --------------------------------------------------------------------------
# Invoke feed_ingest.py per row
# --------------------------------------------------------------------------

def invoke_feed_ingest(function_name, payload):
    """Synchronous invoke -- blocks until feed_ingest.py returns."""
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, default=_json_default).encode("utf-8"),
    )
    raw_payload = response["Payload"].read()
    return raw_payload, response.get("FunctionError")


def interpret_invocation(raw_payload, function_error):
    """Turn a raw Lambda invoke result into {status, reason, report}."""
    if function_error:
        try:
            err = json.loads(raw_payload)
            reason = err.get("errorMessage", str(err))
        except Exception:  # noqa: BLE001 - best-effort error message extraction
            reason = raw_payload.decode("utf-8", errors="replace")
        return {"status": "error", "reason": reason, "report": None}

    try:
        parsed = json.loads(raw_payload)
        body = json.loads(parsed.get("body", "{}"))
    except Exception as e:  # noqa: BLE001 - malformed response from the handler
        return {"status": "error", "reason": f"could not parse invocation response: {e}", "report": None}

    if parsed.get("statusCode") == 200:
        return {"status": "ok", "reason": None, "report": body}
    return {
        "status": "failed",
        "reason": body.get("error", f"handler returned statusCode {parsed.get('statusCode')}"),
        "report": None,
    }


def invoke_row(config, row):
    """Invoke feed_ingest.py for one (team, url) row. Never raises -- failures are recorded."""
    team, url, source = row.get("team_name"), row.get("url"), row.get("source")
    payload = {"team_name": team, "rss_url": url, "source_site": source}
    for key in ("bucket", "prefix", "state_prefix"):
        if config.get(key):
            payload[key] = config[key]
    for key in OPTIONAL_ROW_OVERRIDES:
        if key in row:
            payload[key] = row[key]

    try:
        raw_payload, function_error = invoke_feed_ingest(config["handler_function"], payload)
        outcome = interpret_invocation(raw_payload, function_error)
    except Exception as e:  # noqa: BLE001 - e.g. the function doesn't exist, network error
        outcome = {"status": "error", "reason": str(e), "report": None}

    return {"team": team, "url": url, "source": source, **outcome}


# --------------------------------------------------------------------------
# Group results by team
# --------------------------------------------------------------------------

def group_by_team(results):
    """One entry per team, with every source row nested underneath.

    A team counts as "ok" if at least one of its sources succeeded --
    a partial failure still shows up per-source, but doesn't sink a team
    that has other working sources.
    """
    order = []
    by_team = {}
    for r in results:
        team = r["team"]
        if team not in by_team:
            by_team[team] = []
            order.append(team)
        report = r.get("report")
        by_team[team].append({
            "url": r["url"],
            "source": r["source"],
            "status": r["status"],
            "documents_written": report["documents_written"] if report else 0,
            "reason": r.get("reason"),
            "report": report,
        })

    teams = []
    for team in order:
        sources = by_team[team]
        reports = [s["report"] for s in sources if s["report"]]
        teams.append({
            "team": team,
            "status": "ok" if any(s["status"] == "ok" for s in sources) else "failed",
            "documents_written": sum(s["documents_written"] for s in sources),
            "skipped_already_processed": sum(r.get("skipped_already_processed", 0) for r in reports),
            "skipped_not_about_team": sum(r.get("skipped_not_about_team", 0) for r in reports),
            "fetch_errors": sum(r.get("fetch_errors", 0) for r in reports),
            "sources": sources,
        })
    return teams


# --------------------------------------------------------------------------
# Knowledge base ingestion
# --------------------------------------------------------------------------

def maybe_start_ingestion(knowledge_base_id, data_source_id, total_documents_written):
    if not knowledge_base_id or not data_source_id:
        return {"triggered": False, "ingestion_job_id": None,
                "reason": "KNOWLEDGE_BASE_ID/DATA_SOURCE_ID not configured"}
    if total_documents_written <= 0:
        return {"triggered": False, "ingestion_job_id": None, "reason": "no new documents written"}
    try:
        response = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id, dataSourceId=data_source_id
        )
        return {
            "triggered": True,
            "ingestion_job_id": response["ingestionJob"]["ingestionJobId"],
            "reason": None,
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        reason = "an ingestion job is already running" if code == "ConflictException" else str(e)
        return {"triggered": False, "ingestion_job_id": None, "reason": reason}


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def build_combined_report(run_at, rows_processed, teams, ingestion_job):
    totals = {
        "documents_written": sum(t["documents_written"] for t in teams),
        "skipped_already_processed": sum(t["skipped_already_processed"] for t in teams),
        "skipped_not_about_team": sum(t["skipped_not_about_team"] for t in teams),
        "fetch_errors": sum(t["fetch_errors"] for t in teams),
    }
    teams_ok = sum(1 for t in teams if t["status"] == "ok")
    return {
        "run_at": run_at,
        "rows_processed": rows_processed,
        "teams_processed": len(teams),
        "teams_ok": teams_ok,
        "teams_failed": len(teams) - teams_ok,
        "totals": totals,
        "by_team": teams,
        "ingestion_job": ingestion_job,
    }


def log_combined_report(report):
    print(f"=== feed_ingest_scheduler run: {report['run_at']} ===")
    for t in report["by_team"]:
        tag = "OK" if t["status"] == "ok" else "FAILED"
        n = len(t["sources"])
        failed_sources = [s for s in t["sources"] if s["status"] != "ok"]
        suffix = ""
        if failed_sources:
            if t["status"] == "ok":
                names = ", ".join(s["source"] or s["url"] for s in failed_sources)
                suffix = f"  [{len(failed_sources)} source{'s' if len(failed_sources) != 1 else ''} failed: {names}]"
            else:
                parts = "; ".join(f"{s['source'] or s['url']}: {s['reason']}" for s in failed_sources)
                suffix = f"  [{parts}]"
        print(
            f"[{tag:<6}] {t['team']:<18} ({n} source{'s' if n != 1 else ''})  "
            f"written={t['documents_written']} dedup={t['skipped_already_processed']} "
            f"not_about_team={t['skipped_not_about_team']} errors={t['fetch_errors']}{suffix}"
        )
    totals = report["totals"]
    errors = totals["fetch_errors"]
    print(
        f"--- totals: {totals['documents_written']} written | "
        f"{totals['skipped_already_processed']} dedup-skipped | "
        f"{totals['skipped_not_about_team']} not-about-team | "
        f"{errors} fetch error{'s' if errors != 1 else ''} | "
        f"{report['teams_ok']}/{report['teams_processed']} teams ok ({report['rows_processed']} rows) ---"
    )
    job = report["ingestion_job"]
    if job["triggered"]:
        print(f"--- ingestion job: triggered {job['ingestion_job_id']} ---")
    else:
        print(f"--- ingestion job: skipped ({job['reason']}) ---")


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------

def lambda_handler(event, context):
    config = resolve_config(event)
    run_at = datetime.now(timezone.utc).isoformat()
    try:
        rows = load_feed_configs(config["table_name"])
        results = [invoke_row(config, row) for row in rows]  # sequential: wait for each in turn
        teams = group_by_team(results)
        total_written = sum(t["documents_written"] for t in teams)
        ingestion_job = maybe_start_ingestion(
            config["knowledge_base_id"], config["data_source_id"], total_written
        )
        report = build_combined_report(run_at, len(rows), teams, ingestion_job)
        log_combined_report(report)
        return {"statusCode": 200, "body": json.dumps(report, default=_json_default)}
    except Exception as e:  # noqa: BLE001 - whole-run failure (e.g. table missing)
        print(f"ERROR: feed_ingest_scheduler run failed: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
