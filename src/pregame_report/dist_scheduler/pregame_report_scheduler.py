"""Scheduler Lambda for pregame_report.

Reads the set of rows from the `games` DynamoDB table, keeps the ones whose
`gameTime` falls within a lookahead window from now, invokes
`pregame_report.py` synchronously once per game -- waiting for each result
before moving to the next -- and after every game has run, triggers the
Bedrock knowledge base's ingestion job so the reports `pregame_report.py`
wrote actually get indexed.

Structurally a near-mirror of feed_ingest_scheduler.py: same scan-then-filter
approach to loading config rows, same sequential per-row invoke, same
"trigger ingestion once, at the end, if anything was written" tail.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"

dynamodb_resource = boto3.resource("dynamodb", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
bedrock_agent_client = boto3.client("bedrock-agent", region_name=REGION)

GAMES_TABLE = "games"
PREGAME_REPORT_FUNCTION = "pregame-report"
LOOKAHEAD_DAYS = 5  # only build/refresh reports for games starting within this many days

# Per-row overrides forwarded to pregame_report.py. week_type/week_number/year fall
# back to gameId's parsed value (see parse_game_id) when absent from the row; a
# row-level value, if ever present, wins over both the parsed value and (for
# bucket/prefix) the scheduler-level config -- same forward-compatibility rationale
# as feed_ingest_scheduler.py's OPTIONAL_ROW_OVERRIDES.
OPTIONAL_ROW_OVERRIDES = ("week_type", "week_number", "year", "max_results_per_team", "bucket", "prefix")


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
        "table_name": event.get("table_name") or os.environ.get("GAMES_TABLE", GAMES_TABLE),
        "handler_function": event.get("handler_function") or os.environ.get(
            "PREGAME_REPORT_FUNCTION", PREGAME_REPORT_FUNCTION
        ),
        "knowledge_base_id": event.get("knowledge_base_id") or os.environ.get("KNOWLEDGE_BASE_ID", ""),
        "data_source_id": event.get("data_source_id") or os.environ.get("DATA_SOURCE_ID", ""),
        "lookahead_days": float(
            event.get("lookahead_days", os.environ.get("PREGAME_LOOKAHEAD_DAYS", LOOKAHEAD_DAYS))
        ),
        # Left unset (None) by default so pregame_report.py falls back to its own
        # production defaults; set to redirect every invoked game's writes at
        # once (e.g. for a test run), without touching per-row table data.
        "bucket": event.get("bucket") or os.environ.get("DEST_BUCKET") or None,
        "prefix": event.get("prefix") or os.environ.get("DEST_PREFIX") or None,
    }


# --------------------------------------------------------------------------
# Load + filter games
# --------------------------------------------------------------------------

def load_game_rows(table_name):
    """Every row in `games` (gameId, gameTime, optional enabled/overrides), enabled ones only.

    visiting_team/home_team aren't row attributes -- see parse_game_id -- so they're
    not listed here.
    """
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


def _parse_game_time(value):
    """Parse gameTime's ISO-8601 "...Z" format into an aware UTC datetime, or None if unparseable."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def parse_game_id(game_id):
    """Parse gameId's "{year}#{weekType}#{weekNumber}#{VISITING}@{HOME}" shape (e.g.
    "2026#PRESEASONWEEK#2#49ERS@CHARGERS") into its parts, or None if it doesn't match.

    games rows carry no separate visiting_team/home_team/week_type/week_number/year
    attributes -- gameId is the only place those live, so it's parsed rather than
    read off the row. The VISITING/HOME shorthand's casing in the key is not
    guaranteed (some rows use "49ERS@CHARGERS", others "49ers@Chargers") while
    feed_ingest/feedsources use title-cased team names (e.g. "Broncos") for KB
    filtering, so both are always normalized with .capitalize() -- correct for
    every NFL nickname regardless of input casing, including the digit-led
    "49ers" ("49ERS".capitalize() == "49ers".capitalize() == "49ers").
    """
    if not game_id:
        return None
    parts = str(game_id).split("#")
    if len(parts) != 4 or "@" not in parts[3]:
        return None
    year, week_type, week_number, matchup = parts
    visiting_team, _, home_team = matchup.partition("@")
    if not visiting_team or not home_team:
        return None
    return {
        "year": year,
        "week_type": week_type,
        "week_number": week_number,
        "visiting_team": visiting_team.capitalize(),
        "home_team": home_team.capitalize(),
    }


def select_upcoming_games(rows, lookahead_days):
    """Rows whose gameTime falls in [now, now + lookahead_days] -- games too far out (no fresh news
    yet to report on) or already played are skipped. Malformed gameTime values are skipped and
    logged rather than crashing the whole run.
    """
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=lookahead_days)
    selected = []
    for row in rows:
        game_time = _parse_game_time(row.get("gameTime"))
        if game_time is None:
            print(f"WARNING: skipping row with unparseable gameTime: {row.get('gameId')!r} -> {row.get('gameTime')!r}")
            continue
        if now <= game_time <= cutoff:
            selected.append(row)
    return selected


# --------------------------------------------------------------------------
# Invoke pregame_report.py per game
# --------------------------------------------------------------------------

def invoke_pregame_report(function_name, payload):
    """Synchronous invoke -- blocks until pregame_report.py returns."""
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload, default=_json_default).encode("utf-8"),
    )
    raw_payload = response["Payload"].read()
    return raw_payload, response.get("FunctionError")


def interpret_invocation(raw_payload, function_error):
    """Turn a raw Lambda invoke result into {status, reason, report} -- same contract as
    feed_ingest_scheduler.py's identically-named helper.
    """
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


def invoke_game(config, row):
    """Invoke pregame_report.py for one game row. Never raises -- failures are recorded.

    visiting_team/home_team/week_type/week_number/year are parsed out of gameId
    (see parse_game_id) since the row itself carries none of them; an explicit
    row-level attribute of the same name, if ever present, wins over the parsed
    value -- same override precedence OPTIONAL_ROW_OVERRIDES already gave
    week_type/week_number/year.
    """
    game_id = row.get("gameId")
    parsed = parse_game_id(game_id) or {}
    visiting_team = row.get("visiting_team") or parsed.get("visiting_team")
    home_team = row.get("home_team") or parsed.get("home_team")
    payload = {
        "game_id": game_id,
        "visiting_team": visiting_team,
        "home_team": home_team,
        "game_time": row.get("gameTime"),
        "knowledge_base_id": config["knowledge_base_id"],
    }
    for key in ("bucket", "prefix"):
        if config.get(key):
            payload[key] = config[key]
    for key in OPTIONAL_ROW_OVERRIDES:
        if key in row:
            payload[key] = row[key]
        elif key in parsed:
            payload[key] = parsed[key]

    try:
        raw_payload, function_error = invoke_pregame_report(config["handler_function"], payload)
        outcome = interpret_invocation(raw_payload, function_error)
    except Exception as e:  # noqa: BLE001 - e.g. the function doesn't exist, network error
        outcome = {"status": "error", "reason": str(e), "report": None}

    return {
        "game_id": game_id,
        "visiting_team": visiting_team,
        "home_team": home_team,
        "game_time": row.get("gameTime"),
        **outcome,
    }


# --------------------------------------------------------------------------
# Knowledge base ingestion
# --------------------------------------------------------------------------

def maybe_start_ingestion(knowledge_base_id, data_source_id, total_documents_written):
    """Identical contract to feed_ingest_scheduler.py's helper of the same name -- both schedulers
    share the same knowledge base and data source, so a run here that writes nothing triggers no
    ingestion job, and a ConflictException (a sync already running, possibly kicked off by the news
    scheduler) is treated as a non-fatal, expected outcome.
    """
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

def build_combined_report(run_at, rows_scanned, results, ingestion_job):
    games_ok = sum(1 for r in results if r["status"] == "ok")
    total_written = sum((r.get("report") or {}).get("documents_written", 0) for r in results)
    return {
        "run_at": run_at,
        "rows_scanned": rows_scanned,
        "games_selected": len(results),
        "games_ok": games_ok,
        "games_failed": len(results) - games_ok,
        "documents_written": total_written,
        "games": results,
        "ingestion_job": ingestion_job,
    }


def log_combined_report(report):
    print(f"=== pregame_report_scheduler run: {report['run_at']} ===")
    for r in report["games"]:
        tag = "OK" if r["status"] == "ok" else "FAILED"
        suffix = f"  [{r['reason']}]" if r["status"] != "ok" else ""
        print(f"[{tag:<6}] {r['game_id']:<40} {r['visiting_team']} @ {r['home_team']}{suffix}")
    print(
        f"--- totals: {report['documents_written']} written | "
        f"{report['games_ok']}/{report['games_selected']} games ok "
        f"({report['rows_scanned']} rows scanned) ---"
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
        rows = load_game_rows(config["table_name"])
        upcoming = select_upcoming_games(rows, config["lookahead_days"])
        results = [invoke_game(config, row) for row in upcoming]  # sequential: wait for each in turn
        total_written = sum((r.get("report") or {}).get("documents_written", 0) for r in results)
        ingestion_job = maybe_start_ingestion(config["knowledge_base_id"], config["data_source_id"], total_written)
        report = build_combined_report(run_at, len(rows), results, ingestion_job)
        log_combined_report(report)
        return {"statusCode": 200, "body": json.dumps(report, default=_json_default)}
    except Exception as e:  # noqa: BLE001 - whole-run failure (e.g. table missing)
        print(f"ERROR: pregame_report_scheduler run failed: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
