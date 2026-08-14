"""Integration tests for the feed_ingest_scheduler Lambda.

Unlike test_feed_ingest.py, this suite does not call the ingestion logic
in-process -- it calls feed_ingest_scheduler.lambda_handler() directly, but
that handler's own job is to make a *real* `lambda:Invoke` call against the
already-deployed `feed_ingest.py` Lambda (default function name
"feed-ingest", override with FEED_INGEST_TEST_FUNCTION) for every row it
scans. So `feed_ingest.py` must already be deployed to AWS for these tests
to do anything meaningful; a session fixture checks for it up front and
fails fast with a clear message otherwise.

Row data lives in a **throwaway DynamoDB table** created and destroyed per
test, shaped like the real `feedsources` table (partition key `team`, sort
key `url`), rather than writing rows into the real table directly. Pointing
the scheduler at the real table would scan and invoke feed_ingest.py for
every production team/source configured there -- writing real documents
into the real embeddings/ prefix and potentially kicking off a real
knowledge-base ingestion job as an unintended side effect of running tests.
Every invoked row's writes are further redirected to a throwaway S3 prefix
via the scheduler's bucket/prefix/state_prefix passthrough.

Nothing AWS-related is mocked -- real DynamoDB, real Lambda invoke, real S3,
real (indirectly, via feed_ingest.py) Bedrock and RSS/article fetches.

Run everything:
    cd src && make feed_ingest_scheduler_test

Run one test:
    uv run --project src/feed_ingest pytest tests/test_feed_ingest_scheduler.py -k full_loop -s
"""

import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

import boto3
import pytest

HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "feed_ingest"
if str(HANDLER_DIR) not in sys.path:
    sys.path.insert(0, str(HANDLER_DIR))

from feed_ingest_scheduler import lambda_handler  # noqa: E402

REGION = "us-east-1"
TEST_BUCKET = os.environ.get("FEED_INGEST_TEST_BUCKET", "acdemo-dev-source-bucket")
FEED_INGEST_FUNCTION = os.environ.get("FEED_INGEST_TEST_FUNCTION", "feed-ingest")

# A real team feed, used for the one row that's expected to succeed and (with
# a wide lookback) plausibly write a real document -- needed to exercise the
# "ingestion triggered" path, since feed_ingest.py can only classify content
# as relevant to a team name a real article would plausibly mention.
GOOD_TEAM = "Broncos"
GOOD_URL = "https://www.pff.com/feed/teams/10"
GOOD_SOURCE = "pff.com"

# A bad `lookback_hours` value makes feed_ingest.py's own config resolution
# raise before its try/except -- a deterministic, real Lambda-level failure
# (FunctionError), rather than relying on network flakiness for a bad row.
BAD_URL = "https://feed-ingest-test-bad-row.invalid/rss"
BAD_SOURCE = "bad.invalid"
BAD_LOOKBACK_HOURS = "not-a-number"


class FakeLambdaContext:
    function_name = "feed-ingest-scheduler"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:feed-ingest-scheduler"
    aws_request_id = "local-test-request-id"

    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture(scope="session", autouse=True)
def require_deployed_handler():
    """feed_ingest.py must already be deployed -- these tests invoke it for real."""
    lambda_client = boto3.client("lambda", region_name=REGION)
    try:
        lambda_client.get_function(FunctionName=FEED_INGEST_FUNCTION)
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the operator
        pytest.fail(
            f"feed_ingest_scheduler tests invoke the real deployed '{FEED_INGEST_FUNCTION}' "
            f"Lambda function, not an in-process stub. Deploy it first (`make feed_ingest_dist` "
            f"and upload the zip) or point FEED_INGEST_TEST_FUNCTION at an existing deployment. "
            f"({e})"
        )


@pytest.fixture(scope="session")
def dynamodb_resource():
    return boto3.resource("dynamodb", region_name=REGION)


@pytest.fixture
def test_table(dynamodb_resource):
    """A throwaway feedsources-shaped table (partition key team, sort key url).

    Isolated from the real `feedsources` table so a test run only ever
    invokes feed_ingest.py for the rows this test seeded -- never for
    whatever teams happen to be configured in production.
    """
    name = f"feedsources-test-{uuid.uuid4().hex[:12]}"
    dynamodb_resource.create_table(
        TableName=name,
        AttributeDefinitions=[
            {"AttributeName": "team", "AttributeType": "S"},
            {"AttributeName": "url", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "team", "KeyType": "HASH"},
            {"AttributeName": "url", "KeyType": "RANGE"},
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
    """Throwaway S3 destination every seeded row's feed_ingest.py invocation writes to."""
    prefix = f"test-runs/{uuid.uuid4().hex}/"
    yield prefix
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=TEST_BUCKET, Prefix=prefix):
        keys.extend({"Key": o["Key"]} for o in page.get("Contents", []))
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=TEST_BUCKET, Delete={"Objects": keys[i : i + 1000]})


@pytest.fixture
def seeded_rows(test_table):
    """Team A: one good source + one bad source. Team B: one bad source only."""
    run_id = uuid.uuid4().hex[:8]
    team_a = f"{GOOD_TEAM}"
    team_b = f"TestTeamB-{run_id}"
    rows = [
        {
            "team": team_a,
            "url": GOOD_URL,
            "source": GOOD_SOURCE,
            "lookback_hours": Decimal(24 * 30),  # wide window: maximize odds of a real entry
        },
        {"team": team_a, "url": BAD_URL, "source": BAD_SOURCE, "lookback_hours": BAD_LOOKBACK_HOURS},
        {
            "team": team_b,
            "url": f"{BAD_URL}?team=b",
            "source": BAD_SOURCE,
            "lookback_hours": BAD_LOOKBACK_HOURS,
        },
    ]
    for row in rows:
        test_table.put_item(Item=row)
    return {"team_a": team_a, "team_b": team_b, "rows": rows}


def scheduler_event(test_table_name, run_prefix, **overrides):
    event = {
        "table_name": test_table_name,
        "handler_function": FEED_INGEST_FUNCTION,
        "bucket": TEST_BUCKET,
        "prefix": f"{run_prefix}embeddings/",
        "state_prefix": f"{run_prefix}state/",
    }
    event.update(overrides)
    return event


def invoke(event):
    result = lambda_handler(event, FakeLambdaContext())
    return result, json.loads(result["body"])


# --------------------------------------------------------------------------


def test_full_loop_report_and_log(test_table, seeded_rows, run_prefix, capsys):
    """Every seeded row runs, results group by team, and both are logged.

    Combines the "full loop", "combined report content", and "combined
    report is logged" cases from the design notes into one test to bound
    the number of real feed_ingest.py invocations (and their downstream
    real Bedrock calls) this suite makes.
    """
    team_a, team_b = seeded_rows["team_a"], seeded_rows["team_b"]

    result, body = invoke(scheduler_event(test_table.name, run_prefix))
    assert result["statusCode"] == 200, body
    assert body["rows_processed"] == 3

    by_team = {t["team"]: t for t in body["by_team"]}
    assert set(by_team) == {team_a, team_b}
    assert body["teams_processed"] == 2

    team_a_entry = by_team[team_a]
    assert len(team_a_entry["sources"]) == 2
    statuses = {s["status"] for s in team_a_entry["sources"]}
    assert "ok" in statuses  # the good row succeeded
    assert statuses & {"failed", "error"}  # the bad row did not
    failed_source = next(s for s in team_a_entry["sources"] if s["status"] != "ok")
    assert failed_source["reason"]
    assert team_a_entry["status"] == "ok"  # one working source is enough
    assert team_a_entry["documents_written"] >= 0

    team_b_entry = by_team[team_b]
    assert len(team_b_entry["sources"]) == 1
    assert team_b_entry["sources"][0]["status"] in ("failed", "error")
    assert team_b_entry["sources"][0]["reason"]
    assert team_b_entry["status"] == "failed"  # its only source failed

    assert body["teams_ok"] == 1
    assert body["teams_failed"] == 1
    totals = body["totals"]
    assert totals["documents_written"] == team_a_entry["documents_written"]

    printed = capsys.readouterr().out
    assert "[OK" in printed
    assert "[FAILED" in printed
    assert team_a in printed
    assert team_b in printed
    assert "--- totals:" in printed


def test_disabled_rows_are_skipped(test_table, run_prefix):
    team = f"TestTeamDisabled-{uuid.uuid4().hex[:8]}"
    test_table.put_item(Item={"team": team, "url": BAD_URL, "source": BAD_SOURCE, "enabled": False})

    result, body = invoke(scheduler_event(test_table.name, run_prefix))
    assert result["statusCode"] == 200, body
    assert body["rows_processed"] == 0
    assert body["by_team"] == []


def test_ingestion_skipped_when_nothing_written(test_table, run_prefix):
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


def test_ingestion_triggered_on_new_documents(test_table, seeded_rows, run_prefix):
    kb_id = os.environ.get("FEED_INGEST_TEST_KB_ID") or os.environ.get("KNOWLEDGE_BASE_ID")
    ds_id = os.environ.get("FEED_INGEST_TEST_DATA_SOURCE_ID") or os.environ.get("DATA_SOURCE_ID")
    if not kb_id or not ds_id:
        pytest.skip(
            "Set FEED_INGEST_TEST_KB_ID/FEED_INGEST_TEST_DATA_SOURCE_ID to verify a real "
            "StartIngestionJob call; without them there's no real knowledge base to target."
        )

    result, body = invoke(
        scheduler_event(test_table.name, run_prefix, knowledge_base_id=kb_id, data_source_id=ds_id)
    )
    assert result["statusCode"] == 200, body
    if body["totals"]["documents_written"] == 0:
        pytest.skip(
            "The live Broncos feed had nothing classifiable within a 30-day window this run "
            "-- can't verify ingestion triggers without at least one written document."
        )
    assert body["ingestion_job"]["triggered"] is True
    assert body["ingestion_job"]["ingestion_job_id"]
