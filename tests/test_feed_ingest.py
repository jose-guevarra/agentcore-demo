"""Integration tests for the feed_ingest Lambda.

These run the real handler against the real services it uses: real HTTP
fetches (a local fixture HTTP server for deterministic FULL/PARTIAL/NONE/
degraded content, plus one genuinely live article for an external sanity
check), real Amazon Bedrock `converse` calls against the Nova model, and
real S3 reads/writes. Nothing AWS-related is mocked, so these tests cost
money and require working AWS credentials with `bedrock:InvokeModel` on
`amazon.nova-lite-v1:0` and s3:GetObject/PutObject/DeleteObject on the
target bucket, both in us-east-1.

All writes go under a per-test `test-runs/<uuid>/` prefix (never the real
`embeddings/` prefix the knowledge base reads from), cleaned up after each
test regardless of pass/fail.

Run everything:
    cd src && make feed_ingest_test

Run one test:
    uv run --project src/feed_ingest pytest tests/test_feed_ingest.py -k dedup_second_run -s

Run the handler once without pytest (prints the report; live feed, throwaway prefix):
    uv run --project src/feed_ingest python tests/test_feed_ingest.py
"""

import email.utils
import json
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import boto3
import pytest

# The handler is deployed as a flat module at the root of the zip, so import it
# the same way Lambda does.
HANDLER_DIR = Path(__file__).resolve().parents[1] / "src" / "feed_ingest"
if str(HANDLER_DIR) not in sys.path:
    sys.path.insert(0, str(HANDLER_DIR))

from feed_ingest import MAX_ENTRIES, MODEL_ID, lambda_handler  # noqa: E402

REGION = "us-east-1"
TEST_BUCKET = os.environ.get("FEED_INGEST_TEST_BUCKET", "acdemo-dev-source-bucket")

# Real, verified-reachable article covering all 32 teams (Broncos lead the piece at
# No. 1) -- the one deliberately "live" fixture, for an external sanity check that
# the extraction/classification path works against a real, messy news site.
LIVE_PARTIAL_ARTICLE_URL = "https://www.pff.com/news/nfl-offensive-line-rankings-2026/"
LIVE_PARTIAL_ARTICLE_TITLE = (
    "2026 NFL offensive line rankings: Broncos, Eagles and Buccaneers open in top three"
)

UNREACHABLE_URL = "https://feed-ingest-test-does-not-exist.invalid/article"

# feed_ingest.py no longer has default team_name/rss_url values -- tests that want
# "the real live team feed" now supply these explicitly rather than relying on a
# hardcoded fallback baked into the handler.
LIVE_TEAM_NAME = "Broncos"
LIVE_TEAM_RSS_URL = "https://www.pff.com/feed/teams/10"

BRONCOS_FULL_PARAGRAPHS = [
    "The Denver Broncos put together a dominant performance this week, with the offensive "
    "line receiving universal praise from analysts.",
    "Coaches highlighted the Broncos' pass rush and secondary as the primary reasons for "
    "the team's recent winning streak.",
    "Every storyline in this recap, from special teams execution to quarterback "
    "decision-making, centers entirely on the Broncos roster and coaching staff.",
    "League insiders say the Broncos' depth chart is the deepest it has been in a decade, "
    "fueling optimism for the rest of the season in Denver.",
]

COWBOYS_ONLY_PARAGRAPHS = [
    "The Dallas Cowboys pulled off a statement road win on Sunday behind a career day from "
    "their starting quarterback.",
    "The Cowboys' defensive coordinator praised the front seven for generating consistent "
    "pressure throughout the game.",
    "This recap covers Cowboys personnel decisions, practice-squad moves, and injury "
    "updates exclusively.",
    "Analysts say the Cowboys' receiving corps is now the deepest unit in the NFC East.",
]


class FakeLambdaContext:
    """Stand-in for the context object the Lambda runtime injects.

    Not a mocked AWS call -- the handler never reads it, but invoking with
    the real two-argument signature keeps the test faithful to Lambda.
    """

    function_name = "feed-ingest"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:feed-ingest"
    aws_request_id = "local-test-request-id"

    def get_remaining_time_in_millis(self):
        return 300_000


# --------------------------------------------------------------------------
# Local fixture HTTP server -- real HTTP over loopback, deterministic content.
# Not a mock of anything AWS-related; a stand-in for the arbitrary third-party
# news sites feed_ingest.py fetches, so FULL/PARTIAL/NONE/degraded outcomes
# don't depend on live content that could change.
# --------------------------------------------------------------------------

class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        body = self.server.pages.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):  # keep pytest output clean
        pass


class FixtureServer:
    def __init__(self):
        self.pages = {}
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self._httpd.pages = self.pages
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def add_page(self, html):
        path = f"/fixture-{len(self.pages) + 1}.html"
        self.pages[path] = html
        return f"http://127.0.0.1:{self._port}{path}"

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()


def article_html(title, paragraphs):
    body = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
    return f"<html><head><title>{escape(title)}</title></head><body><article><h1>{escape(title)}</h1>{body}</article></body></html>"


# --------------------------------------------------------------------------
# RSS fixture builder
# --------------------------------------------------------------------------

def make_item(title, link, published, summary=""):
    return {"title": title, "link": link, "published": published, "summary": summary}


def rss_document(items):
    xml_items = "\n".join(
        f"""    <item>
      <title>{escape(it['title'])}</title>
      <link>{escape(it['link'])}</link>
      <description>{escape(it.get('summary', ''))}</description>
      <pubDate>{email.utils.format_datetime(it['published'])}</pubDate>
    </item>"""
        for it in items
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>feed_ingest test feed</title>
    <link>https://example.invalid/</link>
    <description>Fixture feed for integration tests.</description>
{xml_items}
  </channel>
</rss>
"""


def write_feed(tmp_path, items):
    path = tmp_path / "feed.xml"
    path.write_text(rss_document(items), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def require_aws_credentials():
    """Fail fast with a clear message instead of a confusing 500 body."""
    try:
        identity = boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the operator
        pytest.fail(
            f"AWS credentials are not usable ({e}). These tests call Bedrock and S3 for "
            f"real -- configure credentials for an account with access to "
            f"{MODEL_ID} in {REGION} and to s3://{TEST_BUCKET}."
        )
    print(f"\nRunning as {identity['Arn']}")


@pytest.fixture(scope="session")
def fixture_server():
    server = FixtureServer()
    yield server
    server.stop()


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
    event = {
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
# Selection: lookback window + max_entries cap
# --------------------------------------------------------------------------

def test_lookback_window_selects_only_recent_entries(tmp_path, fixture_server, run_prefix):
    """Entries outside the lookback window are excluded from selection entirely."""
    url = fixture_server.add_page(article_html("Cowboys Recap", COWBOYS_ONLY_PARAGRAPHS))
    now = datetime.now(timezone.utc)
    items = [
        make_item("Item 2h old", url, now - timedelta(hours=2)),
        make_item("Item 12h old", url, now - timedelta(hours=12)),
        make_item("Item 48h old", url, now - timedelta(hours=48)),
    ]
    feed_path = write_feed(tmp_path, items)

    result, body = invoke(base_event(run_prefix, rss_url=feed_path, team_name="TestTeam"))
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == 2  # default 24h window: 2h and 12h in, 48h out


def test_lookback_window_is_overridable(tmp_path, fixture_server, run_prefix):
    url = fixture_server.add_page(article_html("Cowboys Recap", COWBOYS_ONLY_PARAGRAPHS))
    now = datetime.now(timezone.utc)
    items = [
        make_item("Item 2h old", url, now - timedelta(hours=2)),
        make_item("Item 12h old", url, now - timedelta(hours=12)),
        make_item("Item 48h old", url, now - timedelta(hours=48)),
    ]
    feed_path = write_feed(tmp_path, items)

    result, body = invoke(
        base_event(run_prefix, rss_url=feed_path, team_name="TestTeam", lookback_hours=72)
    )
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == 3


def test_window_and_cap_compose(tmp_path, fixture_server, run_prefix):
    """12 entries all inside the window -> only MAX_ENTRIES are selected and written."""
    now = datetime.now(timezone.utc)
    items = []
    for i in range(1, 13):
        url = fixture_server.add_page(article_html(f"Broncos Story {i}", BRONCOS_FULL_PARAGRAPHS))
        items.append(make_item(f"Broncos Story {i}", url, now - timedelta(minutes=i)))
    feed_path = write_feed(tmp_path, items)

    result, body = invoke(base_event(run_prefix, rss_url=feed_path, team_name="Broncos"))
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == MAX_ENTRIES == 5
    assert body["documents_written"] == 5  # every fixture page is unambiguously about the Broncos


def test_max_entries_is_overridable(tmp_path, fixture_server, run_prefix):
    now = datetime.now(timezone.utc)
    items = []
    for i in range(1, 5):
        url = fixture_server.add_page(article_html(f"Cowboys Story {i}", COWBOYS_ONLY_PARAGRAPHS))
        items.append(make_item(f"Cowboys Story {i}", url, now - timedelta(minutes=i)))
    feed_path = write_feed(tmp_path, items)

    result, body = invoke(
        base_event(run_prefix, rss_url=feed_path, team_name="TestTeam", max_entries=2)
    )
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == 2


def test_empty_window_returns_no_entries(tmp_path, run_prefix):
    """All entries older than the window -> 200 with an empty report, no network calls made."""
    old_item = make_item(
        "Ancient story", "https://example.invalid/unused", datetime.now(timezone.utc) - timedelta(days=10)
    )
    feed_path = write_feed(tmp_path, [old_item])

    result, body = invoke(base_event(run_prefix, rss_url=feed_path, team_name="TestTeam"))
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == 0
    assert body["documents_written"] == 0
    assert body["articles"] == []


def test_unreachable_feed_is_treated_as_empty(run_prefix):
    """feedparser surfaces fetch failures as a feed with no entries."""
    result, body = invoke(
        base_event(
            run_prefix,
            rss_url="https://feed-ingest-test-does-not-exist.invalid/rss",
            team_name="TestTeam",
        )
    )
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == 0


# --------------------------------------------------------------------------
# Classification + report + S3 content, combined into one batch to bound the
# number of real Bedrock calls this suite makes.
# --------------------------------------------------------------------------

def test_mixed_batch_report_and_s3_content(tmp_path, fixture_server, s3, run_prefix, capsys):
    """One feed: a FULL match, a NONE match, a live PARTIAL match, and an unreachable link.

    Exercises: per-article report statuses, aggregate counts, the printed
    CloudWatch-style log lines, and (for the deterministic FULL article) the
    markdown document and metadata sidecar written to S3.
    """
    now = datetime.now(timezone.utc)
    full_url = fixture_server.add_page(article_html("Broncos Full Recap", BRONCOS_FULL_PARAGRAPHS))
    none_url = fixture_server.add_page(article_html("Cowboys Recap", COWBOYS_ONLY_PARAGRAPHS))
    items = [
        make_item("Broncos Full Recap", full_url, now - timedelta(hours=1)),
        make_item("Cowboys Recap", none_url, now - timedelta(hours=2)),
        make_item(LIVE_PARTIAL_ARTICLE_TITLE, LIVE_PARTIAL_ARTICLE_URL, now - timedelta(hours=3)),
        make_item("Unreachable story", UNREACHABLE_URL, now - timedelta(hours=4)),
    ]
    feed_path = write_feed(tmp_path, items)

    result, body = invoke(base_event(run_prefix, rss_url=feed_path, team_name="Broncos"))
    assert result["statusCode"] == 200, body
    assert body["entries_considered"] == 4
    assert len(body["articles"]) == 4

    articles = {a["url"]: a for a in body["articles"]}

    assert articles[full_url]["status"] == "written_full"
    assert articles[full_url]["s3_key"] is not None

    assert articles[none_url]["status"] == "skipped_not_about_team"
    assert articles[none_url]["s3_key"] is None
    assert articles[none_url]["reason"]

    # A 32-team roundup that leads with the Broncos could reasonably be judged FULL or
    # PARTIAL by the live model -- what matters is it wasn't judged irrelevant.
    assert articles[LIVE_PARTIAL_ARTICLE_URL]["status"] in ("written_full", "written_summary")

    assert articles[UNREACHABLE_URL]["status"] == "fetch_error"
    assert articles[UNREACHABLE_URL]["reason"]

    assert body["documents_written"] >= 2
    assert body["skipped_not_about_team"] == 1
    assert body["fetch_errors"] == 1

    # --- S3 round trip on the deterministic FULL document ---
    doc_key = articles[full_url]["s3_key"]
    markdown = s3.get_object(Bucket=TEST_BUCKET, Key=doc_key)["Body"].read().decode("utf-8")
    assert "Broncos" in markdown

    metadata = json.loads(
        s3.get_object(Bucket=TEST_BUCKET, Key=f"{doc_key}.metadata.json")["Body"].read()
    )
    attrs = metadata["metadataAttributes"]
    assert attrs["team_name"]["value"]["stringValue"] == "Broncos"
    assert attrs["url"]["value"]["stringValue"] == full_url
    assert attrs["coverage"]["value"]["stringValue"] == "full"
    assert attrs["published_date"]["value"]["stringValue"]
    assert attrs["published_timestamp"]["value"]["numberValue"]
    assert attrs["degraded"]["value"]["booleanValue"] is False

    # --- log output ---
    printed = capsys.readouterr().out
    assert "WRITTEN:FULL" in printed
    assert "SKIPPED:NOT_ABOUT_TEAM" in printed
    assert "ERROR:FETCH" in printed
    assert "--- summary:" in printed


def test_degraded_extraction_falls_back_to_rss_summary(tmp_path, fixture_server, s3, run_prefix):
    """A paywalled/JS-only page falls back to the RSS summary and is flagged degraded."""
    paywall_html = "<html><body><article>Subscribe now to keep reading.</article></body></html>"
    url = fixture_server.add_page(paywall_html)
    rich_summary = " ".join(BRONCOS_FULL_PARAGRAPHS)
    item = make_item(
        "Broncos Paywalled Story", url, datetime.now(timezone.utc) - timedelta(hours=1), summary=rich_summary
    )
    feed_path = write_feed(tmp_path, [item])

    result, body = invoke(base_event(run_prefix, rss_url=feed_path, team_name="Broncos"))
    assert result["statusCode"] == 200, body
    assert body["documents_written"] == 1
    doc_key = body["articles"][0]["s3_key"]

    metadata = json.loads(
        s3.get_object(Bucket=TEST_BUCKET, Key=f"{doc_key}.metadata.json")["Body"].read()
    )
    assert metadata["metadataAttributes"]["degraded"]["value"]["booleanValue"] is True


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------

def test_dedup_second_run_is_a_noop(tmp_path, fixture_server, s3, run_prefix):
    url = fixture_server.add_page(article_html("Broncos Repeat Story", BRONCOS_FULL_PARAGRAPHS))
    item = make_item("Broncos Repeat Story", url, datetime.now(timezone.utc) - timedelta(hours=1))
    feed_path = write_feed(tmp_path, [item])
    event = base_event(run_prefix, rss_url=feed_path, team_name="Broncos")

    _, first = invoke(event)
    assert first["documents_written"] == 1
    doc_key = first["articles"][0]["s3_key"]
    first_modified = s3.head_object(Bucket=TEST_BUCKET, Key=doc_key)["LastModified"]

    _, second = invoke(event)
    assert second["documents_written"] == 0
    assert second["skipped_already_processed"] == 1
    second_modified = s3.head_object(Bucket=TEST_BUCKET, Key=doc_key)["LastModified"]
    assert second_modified == first_modified  # proves the object wasn't rewritten


def test_dedup_none_verdict_is_remembered(tmp_path, fixture_server, run_prefix):
    url = fixture_server.add_page(article_html("Cowboys Repeat Story", COWBOYS_ONLY_PARAGRAPHS))
    item = make_item("Cowboys Repeat Story", url, datetime.now(timezone.utc) - timedelta(hours=1))
    feed_path = write_feed(tmp_path, [item])
    event = base_event(run_prefix, rss_url=feed_path, team_name="Broncos")

    _, first = invoke(event)
    assert first["skipped_not_about_team"] == 1
    assert first["documents_written"] == 0

    _, second = invoke(event)
    assert second["skipped_already_processed"] == 1
    assert second["skipped_not_about_team"] == 0  # remembered, not re-classified
    assert second["documents_written"] == 0


def test_dedup_transient_failures_are_retried(tmp_path, run_prefix):
    item = make_item(
        "Unreachable Repeat Story", UNREACHABLE_URL, datetime.now(timezone.utc) - timedelta(hours=1)
    )
    feed_path = write_feed(tmp_path, [item])
    event = base_event(run_prefix, rss_url=feed_path, team_name="Broncos")

    _, first = invoke(event)
    assert first["fetch_errors"] == 1

    _, second = invoke(event)
    assert second["fetch_errors"] == 1  # retried, not silently skipped
    assert second["skipped_already_processed"] == 0


def test_dedup_force_bypasses(tmp_path, fixture_server, s3, run_prefix):
    url = fixture_server.add_page(article_html("Broncos Force Story", BRONCOS_FULL_PARAGRAPHS))
    item = make_item("Broncos Force Story", url, datetime.now(timezone.utc) - timedelta(hours=1))
    feed_path = write_feed(tmp_path, [item])
    event = base_event(run_prefix, rss_url=feed_path, team_name="Broncos")

    _, first = invoke(event)
    doc_key = first["articles"][0]["s3_key"]

    _, second = invoke(event)
    assert second["documents_written"] == 0  # normal dedup applies

    forced_event = dict(event, force=True)
    _, forced = invoke(forced_event)
    assert forced["documents_written"] == 1
    assert forced["articles"][0]["s3_key"] == doc_key


# --------------------------------------------------------------------------
# Live end-to-end sanity
# --------------------------------------------------------------------------

def test_default_live_feed(run_prefix):
    """End-to-end against a real team feed, writing to a throwaway prefix."""
    result, body = invoke(
        base_event(
            run_prefix, team_name=LIVE_TEAM_NAME, rss_url=LIVE_TEAM_RSS_URL, lookback_hours=24 * 7
        )
    )
    assert result["statusCode"] == 200, body
    if body["entries_considered"] == 0:
        pytest.skip("Live feed had no dated entries even within a 7-day window")
    assert body["documents_written"] + body["skipped_not_about_team"] + body["fetch_errors"] > 0
    print("\nLive feed report:\n", json.dumps(body, indent=2)[:2000])


# --------------------------------------------------------------------------
# Required event fields
# --------------------------------------------------------------------------

def test_missing_team_name_fails(run_prefix):
    result, body = invoke(base_event(run_prefix, rss_url=LIVE_TEAM_RSS_URL))
    assert result["statusCode"] == 500, body
    assert "team_name" in body["error"]


def test_missing_rss_url_fails(run_prefix):
    result, body = invoke(base_event(run_prefix, team_name=LIVE_TEAM_NAME))
    assert result["statusCode"] == 500, body
    assert "rss_url" in body["error"]


def test_missing_both_fails(run_prefix):
    result, body = invoke(base_event(run_prefix))
    assert result["statusCode"] == 500, body
    assert "team_name" in body["error"]
    assert "rss_url" in body["error"]


if __name__ == "__main__":
    # Direct invocation: run the handler once against the live feed and dump the
    # report, the way the Lambda console's test button would. Writes to a throwaway
    # test-runs/manual-<uuid>/ prefix -- delete it from S3 manually if you don't need it.
    manual_prefix = f"test-runs/manual-{uuid.uuid4().hex}/"
    response = lambda_handler(
        {
            "team_name": LIVE_TEAM_NAME,
            "rss_url": LIVE_TEAM_RSS_URL,
            "bucket": TEST_BUCKET,
            "prefix": f"{manual_prefix}embeddings/",
            "state_prefix": f"{manual_prefix}state/",
        },
        FakeLambdaContext(),
    )
    print(json.dumps(json.loads(response["body"]), indent=2))
    raise SystemExit(0 if response["statusCode"] == 200 else 1)
