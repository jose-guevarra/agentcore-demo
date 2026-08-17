"""Integration tests for the pregame_report Lambda.

These run the real handler against the real services it uses: real Amazon
Bedrock `retrieve` calls against the knowledge base and `converse` calls
against the Nova model, and real S3 writes. Nothing AWS-related is mocked,
matching tests/test_feed_ingest.py's approach.

Retrieval-touching tests need a real, already-provisioned knowledge base --
set PREGAME_REPORT_TEST_KB_ID (or KNOWLEDGE_BASE_ID) to one, or they're
skipped with a clear reason. They deliberately use team names that are
unlikely to have real coverage in that knowledge base (random per test run),
so they exercise the "no coverage found" path rather than depending on
`feed_ingest.py` having already ingested specific content -- an empty result
is a normal, valid outcome here, not a test failure.

All writes go under a per-test `test-runs/<uuid>/embeddings/pregame_reports/`
prefix (never the real `embeddings/pregame_reports/` prefix), cleaned up
after each test regardless of pass/fail.

Run everything:
    cd src && make pregame_report_test

Run one test:
    uv run --project .. pytest tests/test_pregame_report.py -k missing_fields -s
"""

import json
import os
import sys
import uuid
from pathlib import Path

import boto3
import pytest

HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "pregame_report"
if str(HANDLER_DIR) not in sys.path:
    sys.path.insert(0, str(HANDLER_DIR))

from pregame_report import MODEL_ID, build_metadata, document_key, lambda_handler, slugify  # noqa: E402

REGION = "us-east-1"
TEST_BUCKET = os.environ.get("PREGAME_REPORT_TEST_BUCKET", "acdemo-dev-source-bucket")
KB_ID = os.environ.get("PREGAME_REPORT_TEST_KB_ID") or os.environ.get("KNOWLEDGE_BASE_ID")


class FakeLambdaContext:
    """Stand-in for the context object the Lambda runtime injects -- see
    test_feed_ingest.py's identical rationale for invoking with the real
    two-argument signature.
    """

    function_name = "pregame-report"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:pregame-report"
    aws_request_id = "local-test-request-id"

    def get_remaining_time_in_millis(self):
        return 300_000


@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials():
    """Fail fast with a clear message instead of a confusing 500 body."""
    try:
        identity = boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the operator
        pytest.fail(
            f"AWS credentials are not usable ({e}). These tests call Bedrock and S3 for "
            f"real -- configure credentials for an account with access to "
            f"{MODEL_ID}/bedrock:Retrieve in {REGION} and to s3://{TEST_BUCKET}."
        )
    print(f"\nRunning as {identity['Arn']}")


@pytest.fixture
def s3():
    return boto3.client("s3", region_name=REGION)


@pytest.fixture
def run_prefix(s3):
    """A fresh test-runs/<uuid>/ prefix, deleted from S3 after the test regardless of outcome."""
    prefix = f"test-runs/{uuid.uuid4().hex}/"
    yield prefix
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=TEST_BUCKET, Prefix=prefix):
        keys.extend({"Key": o["Key"]} for o in page.get("Contents", []))
    for i in range(0, len(keys), 1000):
        s3.delete_objects(Bucket=TEST_BUCKET, Delete={"Objects": keys[i : i + 1000]})


def base_event(run_prefix, **overrides):
    run_id = uuid.uuid4().hex[:8]
    event = {
        "game_id": f"2026#PRESEASONWEEK#3#TESTVISITOR{run_id}@TESTHOME{run_id}",
        "visiting_team": f"TestVisitor-{run_id}",
        "home_team": f"TestHome-{run_id}",
        "game_time": "2026-08-29T01:00:00Z",
        "week_type": "PRESEASONWEEK",
        "week_number": "3",
        "knowledge_base_id": KB_ID,
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
            "knowledge base to run retrieval/synthesis tests."
        )


# --------------------------------------------------------------------------
# Pure helpers -- no AWS calls
# --------------------------------------------------------------------------

def test_slugify_produces_dedup_safe_s3_key_component():
    assert slugify("2026#PRESEASONWEEK#3#VIKINGS@BRONCOS") == "2026-preseasonweek-3-vikings-broncos"


def test_document_key_is_deterministic_for_the_same_game_id():
    key_a = document_key("embeddings/pregame_reports/", "2026#PRESEASONWEEK#3#VIKINGS@BRONCOS")
    key_b = document_key("embeddings/pregame_reports/", "2026#PRESEASONWEEK#3#VIKINGS@BRONCOS")
    assert key_a == key_b
    assert key_a == "embeddings/pregame_reports/2026-preseasonweek-3-vikings-broncos.md"


def test_metadata_marks_doc_type_and_embeds_both_team_names():
    config = {
        "visiting_team": "Vikings",
        "home_team": "Broncos",
        "game_id": "2026#PRESEASONWEEK#3#VIKINGS@BRONCOS",
        "game_time": "2026-08-29T01:00:00Z",
    }
    metadata = build_metadata(config)
    attrs = metadata["metadataAttributes"]
    assert attrs["doc_type"]["value"]["stringValue"] == "pregame_report"
    assert attrs["doc_type"]["includeForEmbedding"] is False
    assert attrs["visiting_team"]["value"]["stringValue"] == "Vikings"
    assert attrs["visiting_team"]["includeForEmbedding"] is True
    assert attrs["home_team"]["value"]["stringValue"] == "Broncos"
    assert attrs["home_team"]["includeForEmbedding"] is True


# --------------------------------------------------------------------------
# Validation -- no AWS calls, happens before any client is touched
# --------------------------------------------------------------------------

def test_missing_required_fields_rejected(run_prefix):
    result, body = invoke(base_event(run_prefix, visiting_team=None, knowledge_base_id="fake-kb-id"))
    assert result["statusCode"] == 500
    assert "visiting_team" in body["error"]


# --------------------------------------------------------------------------
# Full loop -- real Bedrock retrieve + converse, real S3 write
# --------------------------------------------------------------------------

def test_report_written_to_s3_under_separate_prefix(s3, run_prefix):
    """A report for teams with no real knowledge-base coverage still gets built and
    written -- the "no coverage found" path is a normal outcome, not a failure -- and
    lands under embeddings/pregame_reports/, distinct from feed_ingest.py's
    embeddings/<team_slug>/ layout.
    """
    require_kb()
    event = base_event(run_prefix)
    result, body = invoke(event)
    assert result["statusCode"] == 200, body
    assert body["visiting_chunks_found"] == 0  # random per-run team name -> no real coverage
    assert body["home_chunks_found"] == 0
    assert body["documents_written"] == 1
    assert "pregame_reports/" in body["s3_key"]

    obj = s3.get_object(Bucket=TEST_BUCKET, Key=body["s3_key"])
    markdown = obj["Body"].read().decode("utf-8")
    assert event["visiting_team"] in markdown
    assert event["home_team"] in markdown
    assert "## Matchup Overview" in markdown
    assert "## Notable Injuries" in markdown

    sidecar = s3.get_object(Bucket=TEST_BUCKET, Key=f"{body['s3_key']}.metadata.json")
    sidecar_body = json.loads(sidecar["Body"].read())
    attrs = sidecar_body["metadataAttributes"]
    assert attrs["doc_type"]["value"]["stringValue"] == "pregame_report"
    assert attrs["game_id"]["value"]["stringValue"] == event["game_id"]


def test_rerun_overwrites_rather_than_duplicates(s3, run_prefix):
    """Same game_id invoked twice -> same deterministic S3 key, not two documents --
    the point of the "refresh as news updates" design (no dedup marker needed).
    """
    require_kb()
    event = base_event(run_prefix)

    result_1, body_1 = invoke(event)
    assert result_1["statusCode"] == 200, body_1
    result_2, body_2 = invoke(event)
    assert result_2["statusCode"] == 200, body_2

    assert body_1["s3_key"] == body_2["s3_key"]
    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        o["Key"]
        for page in paginator.paginate(Bucket=TEST_BUCKET, Prefix=event["prefix"])
        for o in page.get("Contents", [])
        if not o["Key"].endswith(".metadata.json")
    ]
    assert keys == [body_1["s3_key"]]
