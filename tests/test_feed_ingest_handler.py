"""Integration tests for the feed_ingest Lambda handler.

These run the real handler against the real services it uses: real RSS
parsing via feedparser and real Amazon Bedrock `converse` calls against the
Nova model. Nothing is mocked, so they cost money and require working AWS
credentials with `bedrock:InvokeModel` on `amazon.nova-lite-v1:0` in
us-east-1.

Run everything:
    cd src && make feed_ingest_test

Run one test:
    uv run --project src/feed_ingest pytest tests/test_feed_ingest_handler.py -k five_most_recent -s

Run the handler once without pytest (prints the response):
    uv run --project src/feed_ingest python tests/test_feed_ingest_handler.py
"""

import email.utils
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest

# The handler is deployed as a flat module at the root of the zip, so import it
# the same way Lambda does.
HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "feed_ingest"
if str(HANDLER_DIR) not in sys.path:
    sys.path.insert(0, str(HANDLER_DIR))

from feed_ingest_handler import MAX_ENTRIES, MODEL_ID, lambda_handler  # noqa: E402

REGION = "us-east-1"


class FakeLambdaContext:
    """Stand-in for the context object the Lambda runtime injects.

    Not a mocked AWS call -- the handler never reads it, but invoking with
    the real two-argument signature keeps the test faithful to Lambda.
    """

    function_name = "feed_ingest_handler"
    memory_limit_in_mb = 512
    invoked_function_arn = (
        "arn:aws:lambda:us-east-1:000000000000:function:feed_ingest_handler"
    )
    aws_request_id = "local-test-request-id"

    def get_remaining_time_in_millis(self):
        return 300_000


def rss_document(newest: datetime, item_count: int) -> str:
    """Build a valid RSS 2.0 document.

    Item 1 is published at `newest`, and each subsequent item is an hour
    older, so the expected "most recent N" set is unambiguous.
    """
    items = "\n".join(
        f"""    <item>
      <title>Test headline {i}: quarterback play in week {i}</title>
      <link>https://example.invalid/story-{i}</link>
      <description>Summary text for test story {i}.</description>
      <pubDate>{email.utils.format_datetime(newest - timedelta(hours=i - 1))}</pubDate>
    </item>"""
        for i in range(1, item_count + 1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>feed_ingest test feed</title>
    <link>https://example.invalid/</link>
    <description>Fixture feed for integration tests.</description>
{items}
  </channel>
</rss>
"""


def write_feed(tmp_path, newest: datetime, item_count: int) -> str:
    """Write an RSS file and return its path (feedparser parses paths directly)."""
    path = tmp_path / "feed.xml"
    path.write_text(rss_document(newest, item_count), encoding="utf-8")
    return str(path)


@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials():
    """Fail fast with a clear message instead of a confusing 500 body."""
    try:
        identity = boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the operator
        pytest.fail(
            f"AWS credentials are not usable ({e}). These tests call Bedrock for "
            f"real -- configure credentials for an account with access to "
            f"{MODEL_ID} in {REGION}."
        )
    print(f"\nRunning as {identity['Arn']}")


def test_local_feed_is_analyzed_by_bedrock(tmp_path):
    """A feed with entries -> real Bedrock converse call -> analysis text."""
    feed_path = write_feed(tmp_path, datetime.now(timezone.utc), item_count=5)

    result = lambda_handler({"rss_url": feed_path}, FakeLambdaContext())

    body = json.loads(result["body"])
    assert result["statusCode"] == 200, body
    assert body["feed_processed"] == feed_path
    assert body["items_analyzed_count"] == 5
    assert body["analysis"].strip(), "Bedrock returned an empty analysis"
    print("\nBedrock analysis:\n", body["analysis"])


def test_only_five_most_recent_entries_are_selected(tmp_path, capsys):
    """A 12-item feed is trimmed to the 5 newest, newest first."""
    feed_path = write_feed(tmp_path, datetime.now(timezone.utc), item_count=12)

    result = lambda_handler({"rss_url": feed_path}, FakeLambdaContext())

    body = json.loads(result["body"])
    assert result["statusCode"] == 200, body
    assert body["items_analyzed_count"] == MAX_ENTRIES == 5

    # The handler prints the selected entries; items 1-5 are the newest.
    selected = capsys.readouterr().out
    for i in range(1, 6):
        assert f"Test headline {i}:" in selected
    for i in range(6, 13):
        assert f"Test headline {i}:" not in selected


def test_max_entries_is_overridable(tmp_path):
    """The event can ask for a different number of entries."""
    feed_path = write_feed(tmp_path, datetime.now(timezone.utc), item_count=12)

    result = lambda_handler(
        {"rss_url": feed_path, "max_entries": 2}, FakeLambdaContext()
    )

    body = json.loads(result["body"])
    assert result["statusCode"] == 200, body
    assert body["items_analyzed_count"] == 2


def test_old_entries_are_still_returned(tmp_path):
    """Selection is by recency rank, not by an age cutoff."""
    feed_path = write_feed(
        tmp_path, datetime.now(timezone.utc) - timedelta(days=90), item_count=5
    )

    result = lambda_handler({"rss_url": feed_path}, FakeLambdaContext())

    body = json.loads(result["body"])
    assert result["statusCode"] == 200, body
    assert body["items_analyzed_count"] == 5


def test_unreachable_feed_is_treated_as_empty():
    """feedparser surfaces fetch failures as a feed with no entries."""
    result = lambda_handler(
        {"rss_url": "https://feed-ingest-does-not-exist.invalid/rss"},
        FakeLambdaContext(),
    )

    body = json.loads(result["body"])
    assert result["statusCode"] == 200, body
    assert body["message"] == "No RSS entries found."


def test_default_live_feed():
    """End-to-end against the real default feed over the network."""
    result = lambda_handler({}, FakeLambdaContext())

    body = json.loads(result["body"])
    assert result["statusCode"] == 200, body
    if "message" in body:
        pytest.skip(f"Live feed returned no dated entries: {body['message']}")
    assert 0 < body["items_analyzed_count"] <= MAX_ENTRIES
    assert body["analysis"].strip()
    print("\nLive feed analysis:\n", body["analysis"])


if __name__ == "__main__":
    # Direct invocation: run the handler once against the live feed and dump the
    # response, the way the Lambda console's test button would.
    response = lambda_handler({}, FakeLambdaContext())
    print(json.dumps(json.loads(response["body"]), indent=2))
    raise SystemExit(0 if response["statusCode"] == 200 else 1)
