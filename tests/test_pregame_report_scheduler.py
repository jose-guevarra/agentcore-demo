"""Integration tests for the pregame_report_scheduler Lambda.

Unlike test_pregame_report.py, this suite does not call the report-building
logic in-process -- it calls pregame_report_scheduler.lambda_handler()
directly, but that handler's own job is to make a *real* `lambda:Invoke` call
against the already-deployed `pregame_report.py` Lambda (default function
name "pregame-report", override with PREGAME_REPORT_TEST_FUNCTION) for every
game it selects. So `pregame_report.py` must already be deployed to AWS for
those tests to do anything meaningful; a session fixture checks for it up
front and fails fast with a clear message otherwise. Window-selection logic
is tested directly as a pure function and needs no deployment or AWS calls.

Row data lives in a **throwaway DynamoDB table** created and destroyed per
test, shaped like the real `games` table (partition key `gameId`, sort key
`gameTime`), rather than writing rows into the real table directly -- same
isolation rationale as test_feed_ingest_scheduler.py's throwaway
`feedsources`-shaped table. Every invoked row's writes are further redirected
to a throwaway S3 prefix via the scheduler's bucket/prefix passthrough.

Nothing AWS-related is mocked -- real DynamoDB, real Lambda invoke, real S3,
real (indirectly, via pregame_report.py) Bedrock calls.

Run everything:
    cd src && make pregame_report_scheduler_test

Run one test:
    uv run --project .. pytest tests/test_pregame_report_scheduler.py -k window -s
"""

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest

HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "pregame_report"
if str(HANDLER_DIR) not in sys.path:
    sys.path.insert(0, str(HANDLER_DIR))

from pregame_report_scheduler import lambda_handler, parse_game_id, select_upcoming_games  # noqa: E402

REGION = "us-east-1"
TEST_BUCKET = os.environ.get("PREGAME_REPORT_TEST_BUCKET", "acdemo-dev-source-bucket")
PREGAME_REPORT_FUNCTION = os.environ.get("PREGAME_REPORT_TEST_FUNCTION", "pregame-report")
# Unlike feed_ingest.py, pregame_report.py needs a real knowledge_base_id on every
# invocation (it calls bedrock-agent-runtime Retrieve directly) -- not just for the
# scheduler's own end-of-run StartIngestionJob call -- so any test expecting a
# selected game to actually *succeed* needs one too.
KB_ID = os.environ.get("PREGAME_REPORT_TEST_KB_ID") or os.environ.get("KNOWLEDGE_BASE_ID")

# A gameId that doesn't match the "{year}#{weekType}#{weekNumber}#{VISITING}@{HOME}"
# shape makes parse_game_id() return None, so -- with no explicit visiting_team/
# home_team override on the row either -- pregame_report.py's own config resolution
# rejects the row before any Bedrock/S3 call: a deterministic, real Lambda-level
# failure, rather than relying on live-content flakiness for a bad row (mirrors
# test_feed_ingest_scheduler.py's BAD_URL/BAD_LOOKBACK_HOURS rationale).
BAD_GAME_ID = "not-a-valid-game-id"


class FakeLambdaContext:
    function_name = "pregame-report-scheduler"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:pregame-report-scheduler"
    aws_request_id = "local-test-request-id"

    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture(scope="session", autouse=True)
def require_deployed_handler():
    """pregame_report.py must already be deployed -- these tests invoke it for real."""
    lambda_client = boto3.client("lambda", region_name=REGION)
    try:
        lambda_client.get_function(FunctionName=PREGAME_REPORT_FUNCTION)
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the operator
        pytest.fail(
            f"pregame_report_scheduler tests invoke the real deployed "
            f"'{PREGAME_REPORT_FUNCTION}' Lambda function, not an in-process stub. Deploy it "
            f"first (`make pregame_report_dist` and upload the zip) or point "
            f"PREGAME_REPORT_TEST_FUNCTION at an existing deployment. ({e})"
        )


@pytest.fixture(scope="session")
def dynamodb_resource():
    return boto3.resource("dynamodb", region_name=REGION)


@pytest.fixture
def test_table(dynamodb_resource):
    """A throwaway games-shaped table (partition key gameId, sort key gameTime).

    Isolated from the real `games` table so a test run only ever invokes
    pregame_report.py for the rows this test seeded -- never for whatever
    games happen to be scheduled in production.
    """
    name = f"games-test-{uuid.uuid4().hex[:12]}"
    dynamodb_resource.create_table(
        TableName=name,
        AttributeDefinitions=[
            {"AttributeName": "gameId", "AttributeType": "S"},
            {"AttributeName": "gameTime", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "gameId", "KeyType": "HASH"},
            {"AttributeName": "gameTime", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table = dynamodb_resource.Table(name)
    table.wait_until_exists()
    yield table
    table.delete()


@pytest.fixture
def s3():
    return boto3.client("s3", region_name=REGION)


@pytest.fixture
def run_prefix(s3):
    """Throwaway S3 destination every seeded row's pregame_report.py invocation writes to."""
    prefix = f"test-runs/{uuid.uuid4().hex}/"
    yield prefix
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=TEST_BUCKET, Prefix=prefix):
        keys.extend({"Key": o["Key"]} for o in page.get("Contents", []))
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=TEST_BUCKET, Delete={"Objects": keys[i : i + 1000]})


def game_row(run_id, *, hours_from_now, **overrides):
    game_time = datetime.now(timezone.utc) + timedelta(hours=hours_from_now)
    row = {
        "gameId": f"2026#PRESEASONWEEK#3#TESTVISITOR{run_id}@TESTHOME{run_id}",
        "gameTime": game_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "visiting_team": f"TestVisitor-{run_id}",
        "home_team": f"TestHome-{run_id}",
    }
    row.update(overrides)
    return row


def scheduler_event(test_table_name, run_prefix, **overrides):
    event = {
        "table_name": test_table_name,
        "handler_function": PREGAME_REPORT_FUNCTION,
        "bucket": TEST_BUCKET,
        "prefix": f"{run_prefix}embeddings/pregame_reports/",
    }
    event.update(overrides)
    return event


def invoke(event):
    result = lambda_handler(event, FakeLambdaContext())
    return result, json.loads(result["body"])


def require_kb():
    if not KB_ID:
        pytest.skip(
            "Set PREGAME_REPORT_TEST_KB_ID (or KNOWLEDGE_BASE_ID) to a real, provisioned "
            "knowledge base -- pregame_report.py needs one on every invocation to succeed."
        )


# --------------------------------------------------------------------------
# Window selection -- pure function, no AWS calls
# --------------------------------------------------------------------------

def test_select_upcoming_games_window():
    now_row = game_row("a", hours_from_now=2)
    soon_row = game_row("b", hours_from_now=24 * 3)
    far_row = game_row("c", hours_from_now=24 * 30)
    past_row = game_row("d", hours_from_now=-2)
    malformed_row = game_row("e", hours_from_now=1)
    malformed_row["gameTime"] = "not-a-timestamp"

    selected = select_upcoming_games([now_row, soon_row, far_row, past_row, malformed_row], lookahead_days=5)

    selected_ids = {row["gameId"] for row in selected}
    assert selected_ids == {now_row["gameId"], soon_row["gameId"]}


# --------------------------------------------------------------------------
# gameId parsing -- pure function, no AWS calls
# --------------------------------------------------------------------------

def test_parse_game_id_extracts_and_normalizes_teams():
    expected = {
        "year": "2026",
        "week_type": "PRESEASONWEEK",
        "week_number": "2",
        "visiting_team": "49ers",
        "home_team": "Chargers",
    }
    # Casing of the VISITING/HOME shorthand in the key isn't guaranteed (rows have
    # used both "49ERS@CHARGERS" and "49ers@Chargers") -- both normalize the same way.
    assert parse_game_id("2026#PRESEASONWEEK#2#49ERS@CHARGERS") == expected
    assert parse_game_id("2026#PRESEASONWEEK#2#49ers@Chargers") == expected


def test_parse_game_id_returns_none_for_malformed_input():
    assert parse_game_id(BAD_GAME_ID) is None
    assert parse_game_id("2026#PRESEASONWEEK#2#NOATSIGN") is None
    assert parse_game_id(None) is None


# --------------------------------------------------------------------------
# Full loop -- real DynamoDB + real pregame_report.py invoke
# --------------------------------------------------------------------------

def test_full_loop_selects_and_reports(test_table, run_prefix):
    """Games in-window are invoked and reported; games outside the window are scanned
    but never invoked.
    """
    require_kb()
    run_id = uuid.uuid4().hex[:8]
    in_window = game_row(f"in-{run_id}", hours_from_now=6)
    out_of_window = game_row(f"out-{run_id}", hours_from_now=24 * 30)
    for row in (in_window, out_of_window):
        test_table.put_item(Item=row)

    result, body = invoke(scheduler_event(test_table.name, run_prefix, knowledge_base_id=KB_ID))
    assert result["statusCode"] == 200, body
    assert body["rows_scanned"] == 2
    assert body["games_selected"] == 1

    reported = body["games"][0]
    assert reported["game_id"] == in_window["gameId"]
    assert reported["status"] == "ok", reported
    assert body["games_ok"] == 1
    assert body["documents_written"] >= 1


def test_disabled_rows_are_skipped(test_table, run_prefix):
    run_id = uuid.uuid4().hex[:8]
    row = game_row(f"disabled-{run_id}", hours_from_now=6, enabled=False)
    test_table.put_item(Item=row)

    result, body = invoke(scheduler_event(test_table.name, run_prefix))
    assert result["statusCode"] == 200, body
    assert body["rows_scanned"] == 1
    assert body["games_selected"] == 0


def test_bad_row_recorded_as_failure(test_table, run_prefix):
    run_id = uuid.uuid4().hex[:8]
    row = game_row(f"bad-{run_id}", hours_from_now=6)
    row.pop("visiting_team")  # no override -- falls through to the (malformed) gameId
    row.pop("home_team")
    row["gameId"] = f"{BAD_GAME_ID}-{run_id}"  # parse_game_id() can't extract teams from this
    test_table.put_item(Item=row)

    result, body = invoke(scheduler_event(test_table.name, run_prefix))
    assert result["statusCode"] == 200, body
    assert body["games_selected"] == 1
    reported = body["games"][0]
    assert reported["status"] in ("failed", "error")
    assert reported["reason"]
    assert body["games_ok"] == 0


def test_ingestion_skipped_when_nothing_written(run_prefix, test_table):
    """No rows -> no documents -> the scheduler doesn't call StartIngestionJob."""
    result, body = invoke(
        scheduler_event(
            test_table.name,
            run_prefix,
            knowledge_base_id="fake-kb-id",
            data_source_id="fake-ds-id",
        )
    )
    assert result["statusCode"] == 200, body
    assert body["ingestion_job"]["triggered"] is False
    assert "no new documents" in body["ingestion_job"]["reason"]


def test_ingestion_triggered_on_new_documents(test_table, run_prefix):
    kb_id = os.environ.get("PREGAME_REPORT_TEST_KB_ID") or os.environ.get("KNOWLEDGE_BASE_ID")
    ds_id = os.environ.get("PREGAME_REPORT_TEST_DATA_SOURCE_ID") or os.environ.get("DATA_SOURCE_ID")
    if not kb_id or not ds_id:
        pytest.skip(
            "Set PREGAME_REPORT_TEST_KB_ID/PREGAME_REPORT_TEST_DATA_SOURCE_ID to verify a real "
            "StartIngestionJob call; without them there's no real knowledge base to target."
        )

    run_id = uuid.uuid4().hex[:8]
    test_table.put_item(Item=game_row(f"ingest-{run_id}", hours_from_now=6))

    result, body = invoke(
        scheduler_event(test_table.name, run_prefix, knowledge_base_id=kb_id, data_source_id=ds_id)
    )
    assert result["statusCode"] == 200, body
    assert body["documents_written"] >= 1
    assert body["ingestion_job"]["triggered"] is True
    assert body["ingestion_job"]["ingestion_job_id"]
