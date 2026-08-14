"""Per-team RSS ingestion Lambda.

For one (team, RSS feed) pair: select recent entries within a lookback
window, fetch and read each linked article, keep only the content that is
actually about the team, and write one markdown document + metadata sidecar
per article to the S3 bucket backing the Bedrock knowledge base's data
source. This Lambda is upload-only -- it never touches the knowledge base's
ingestion job; that is `feed_ingest_scheduler.py`'s responsibility, once it
has run this function for every configured team/source.
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3
import feedparser
import requests
from bs4 import BeautifulSoup
from botocore.exceptions import ClientError

REGION = "us-east-1"

# Initialize AWS clients
bedrock_runtime = boto3.client(service_name="bedrock-runtime", region_name=REGION)
s3_client = boto3.client(service_name="s3", region_name=REGION)

# Target Amazon Nova Model ID
MODEL_ID = "amazon.nova-lite-v1:0"  # Or use "amazon.nova-pro-v1:0" depending on complexity

# Defaults, overridable per-invocation via the event or environment variables
LOOKBACK_HOURS = 48 
MAX_ENTRIES = 10
# Bucket name is account-suffixed by Terraform for S3 global-uniqueness (see
# infra/acdemo/bedrock_kb.tf); the deployed Lambda always gets the real name
# via the DEST_BUCKET env var, so this constant only matters as a fallback
# for ad hoc local/manual invocation and won't match production out of the box.
DEST_BUCKET = "acdemo-dev-source-bucket"
DEST_PREFIX = "embeddings/"
STATE_PREFIX = "state/"

MAX_ARTICLE_CHARS = 12_000  # budget for the text sent to Nova for classification
FETCH_TIMEOUT_SECONDS = 15
DEGRADED_MIN_CHARS = 200  # below this, extraction is treated as a paywall/JS-only page
USER_AGENT = "Mozilla/5.0 (compatible; feed-ingest/1.0; +https://github.com/agentcore-demo)"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def resolve_config(event):
    """Resolve settings as event value -> env var -> constant default.

    `team_name` and `rss_url` have no constant default -- they resolve to
    None if neither the event nor the environment supplies one, and
    lambda_handler() fails the invocation rather than silently falling back
    to some other team's feed.
    """
    event = event or {}
    return {
        "team_name": event.get("team_name") or os.environ.get("TEAM_NAME") or None,
        "rss_url": event.get("rss_url") or os.environ.get("RSS_URL") or None,
        "lookback_hours": float(
            event.get("lookback_hours", os.environ.get("LOOKBACK_HOURS", LOOKBACK_HOURS))
        ),
        "max_entries": int(
            event.get("max_entries", os.environ.get("MAX_ENTRIES", MAX_ENTRIES))
        ),
        "bucket": event.get("bucket") or os.environ.get("DEST_BUCKET", DEST_BUCKET),
        "prefix": event.get("prefix") or os.environ.get("DEST_PREFIX", DEST_PREFIX),
        "state_prefix": event.get("state_prefix") or os.environ.get(
            "STATE_PREFIX", STATE_PREFIX
        ),
        "force": bool(event.get("force", False)),
        "source_site": event.get("source_site") or os.environ.get("SOURCE_SITE", ""),
    }


# --------------------------------------------------------------------------
# Entry selection
# --------------------------------------------------------------------------

def select_entries(feed, lookback_hours, max_entries):
    """Newest `max_entries` entries published within the last `lookback_hours`."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    dated = []
    for entry in feed.entries:
        # Feedparser normalizes published dates to struct_time under published_parsed
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            entry_time = datetime.fromtimestamp(
                time.mktime(entry.published_parsed), timezone.utc
            )
            if entry_time > cutoff:
                dated.append((entry_time, entry))
    dated.sort(key=lambda pair: pair[0], reverse=True)
    return dated[:max_entries]


def slugify(text, max_len=60):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len].strip("-") or "untitled"


def entry_key(entry):
    """Stable dedup identity for a feed entry: its guid, falling back to link/title."""
    guid = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha256(guid.encode("utf-8")).hexdigest()[:32]


def document_key(prefix, team_slug, entry_time, title):
    return f"{prefix}{team_slug}/{entry_time.strftime('%Y-%m-%d')}-{slugify(title, 50)}.md"


def marker_key(state_prefix, team_slug, ekey):
    return f"{state_prefix}{team_slug}/seen/{ekey}.json"


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------

def already_processed(bucket, key):
    """HeadObject check: 200 -> True (skip), 404 -> False (process)."""
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        # Unknown failure mode (permissions, throttling, ...): prefer reprocessing
        # over silently and permanently skipping an entry.
        print(f"WARNING: HeadObject check failed for s3://{bucket}/{key}: {e}")
        return False


def mark_processed(bucket, key, status, url, s3_key):
    body = {
        "url": url,
        "status": status,
        "s3_key": s3_key,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(body).encode("utf-8"),
        ContentType="application/json",
    )


# --------------------------------------------------------------------------
# Article extraction
# --------------------------------------------------------------------------

def extract_main_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()

    container = soup.find("article")
    if container is None:
        candidates = soup.find_all(["main", "div"])
        container = (
            max(candidates, key=lambda c: len(c.get_text(strip=True)))
            if candidates
            else None
        )
    if container is None:
        container = soup.body or soup

    text = container.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def fetch_article_text(url):
    """Fetch and extract article text. Raises on total fetch failure."""
    response = requests.get(
        url,
        timeout=FETCH_TIMEOUT_SECONDS,
        allow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return extract_main_text(response.text)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def parse_classification(raw_text):
    """Defensively parse the model's JSON reply. Never raises."""
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
        relevance = str(data.get("relevance", "")).strip().upper()
        if relevance not in ("FULL", "PARTIAL", "NONE"):
            raise ValueError(f"unexpected relevance value: {relevance!r}")
        return {
            "relevance": relevance,
            "summary": data.get("summary") or "",
            "reason": data.get("reason") or "",
        }
    except (json.JSONDecodeError, ValueError) as e:
        return {
            "relevance": "NONE",
            "summary": "",
            "reason": f"unparseable classifier response ({e}): {cleaned[:200]}",
        }


def classify_article(team_name, title, article_text):
    """One Bedrock converse call. Raises on API/network failure (caller treats as transient)."""
    truncated = article_text[:MAX_ARTICLE_CHARS]
    prompt_text = (
        f'You are an NFL beat reporter\'s research assistant. Determine how much of the '
        f'article below is about the "{team_name}" specifically.\n\n'
        f"Article title: {title}\n"
        f"Article text:\n{truncated}\n\n"
        'Respond with ONLY a JSON object (no markdown fences, no extra text) with exactly '
        'these keys: {"relevance": "FULL" | "PARTIAL" | "NONE", "summary": "...", "reason": "..."}\n\n'
        f"- FULL: the entire article is about the {team_name}.\n"
        f"- PARTIAL: the {team_name} are discussed, but the article also covers other "
        f"teams/topics. Set summary to a concise paragraph containing ONLY the details "
        f"relevant to the {team_name}.\n"
        f"- NONE: the article does not meaningfully mention the {team_name}. Set summary "
        f'to "".\n'
        'Always fill "reason" with a one-sentence justification for your relevance choice.'
    )
    response = bedrock_runtime.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": prompt_text}]}],
        inferenceConfig={"maxTokens": 800, "temperature": 0.2},
    )
    raw_text = response["output"]["message"]["content"][0]["text"]
    return parse_classification(raw_text)


# --------------------------------------------------------------------------
# Document + metadata
# --------------------------------------------------------------------------

def build_markdown(team_name, title, url, published, author, coverage, body_text):
    header = (
        f"# {title}\n\n"
        f"**Team:** {team_name}  \n"
        f"**Source:** {url}  \n"
        f"**Published:** {published}  \n"
        f"**Author:** {author or 'Unknown'}  \n"
        f"**Coverage:** {'Full article' if coverage == 'full' else 'Team-relevant summary'}\n\n"
        "---\n\n"
    )
    return header + body_text.strip() + "\n"


# Bedrock KB enforces a hard cap on sidecar .metadata.json size (observed as
# "Ignored N files ... larger than service limit of MaximumFileSizeSupported:
# 1024 bytes" in the data source sync warnings -- well under the 10 KB quoted
# for the general S3 connector, at least for this S3-Vectors-backed KB). Only
# keep the attributes actually used for filtering, and cap the unbounded ones
# (title, url) defensively so a long headline/tracking-param URL can't blow
# the budget: this shape stays under ~850 bytes even at those caps.
TITLE_META_MAX_CHARS = 120
URL_META_MAX_CHARS = 200


def build_metadata(*, team_name, title, source, published_date, url):
    def s(value, embed):
        return {"value": {"type": "STRING", "stringValue": str(value)}, "includeForEmbedding": embed}

    attrs = {
        "team_name": s(team_name, True),
        "title": s(title[:TITLE_META_MAX_CHARS], True),
        "source": s(source or "unknown", False),
        "published_date": s(published_date, False),
        "url": s(url[:URL_META_MAX_CHARS], False),
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
# Reporting
# --------------------------------------------------------------------------

def build_report(config, run_at, entries_considered, counts, articles, written_keys):
    return {
        "team_name": config["team_name"],
        "rss_url": config["rss_url"],
        "run_at": run_at,
        "lookback_hours": config["lookback_hours"],
        "max_entries": config["max_entries"],
        "entries_considered": entries_considered,
        "documents_written": counts["documents_written"],
        "skipped_already_processed": counts["skipped_already_processed"],
        "skipped_not_about_team": counts["skipped_not_about_team"],
        "fetch_errors": counts["fetch_errors"],
        "written_keys": written_keys,
        "articles": articles,
    }


def log_report(report):
    """Print the report as one line per article for CloudWatch Logs Insights."""
    print(f"=== feed_ingest report: {report['team_name']} ({report['rss_url']}) ===")
    print(
        f"window: last {report['lookback_hours']}h, max {report['max_entries']} entries "
        f"| considered: {report['entries_considered']}"
    )
    written_full = written_summary = 0
    for a in report["articles"]:
        status = a["status"]
        if status == "written_full":
            written_full += 1
            print(f"[WRITTEN:FULL]    {a['s3_key']} <- {a['url']}")
        elif status == "written_summary":
            written_summary += 1
            print(f"[WRITTEN:SUMMARY] {a['s3_key']} <- {a['url']}")
        elif status == "skipped_dedup":
            print(f"[SKIPPED:DEDUP]                {a['url']} ({a['reason']})")
        elif status == "skipped_not_about_team":
            print(f"[SKIPPED:NOT_ABOUT_TEAM]       {a['url']} (reason: {a['reason']})")
        elif status == "fetch_error":
            print(f"[ERROR:FETCH]                  {a['url']} (reason: {a['reason']})")
        elif status == "classify_error":
            print(f"[ERROR:CLASSIFY]               {a['url']} (reason: {a['reason']})")
    errors = report["fetch_errors"]
    print(
        f"--- summary: {report['documents_written']} written "
        f"({written_full} full, {written_summary} summary) | "
        f"{report['skipped_already_processed']} dedup-skipped | "
        f"{report['skipped_not_about_team']} not-about-team | "
        f"{errors} error{'s' if errors != 1 else ''} ---"
    )


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------

def _run_ingest(config, run_at):
    team_name = config["team_name"]
    team_slug = slugify(team_name)

    feed = feedparser.parse(config["rss_url"])
    selected = select_entries(feed, config["lookback_hours"], config["max_entries"])

    counts = {
        "documents_written": 0,
        "skipped_already_processed": 0,
        "skipped_not_about_team": 0,
        "fetch_errors": 0,
    }
    articles = []
    written_keys = []

    for entry_time, entry in selected:
        title = entry.get("title", "No Title")
        url = entry.get("link", "")
        published = entry.get("published", entry_time.isoformat())
        author = entry.get("author", "")
        ekey = entry_key(entry)
        doc_key = document_key(config["prefix"], team_slug, entry_time, title)
        marker = marker_key(config["state_prefix"], team_slug, ekey)

        line = {"title": title, "url": url, "entry_id": ekey, "published": published}

        if not config["force"] and (
            already_processed(config["bucket"], marker)
            or already_processed(config["bucket"], doc_key)
        ):
            counts["skipped_already_processed"] += 1
            line.update(status="skipped_dedup", s3_key=None, reason="already processed in a previous run")
            articles.append(line)
            continue

        try:
            article_text = fetch_article_text(url)
            if len(article_text) < DEGRADED_MIN_CHARS:
                # Extraction likely hit a paywall/JS-only page; fall back to the
                # feed's own summary rather than writing a near-empty document.
                article_text = entry.get("summary", "") or article_text
        except Exception as e:  # noqa: BLE001 - per-article failure, recorded and skipped
            counts["fetch_errors"] += 1
            line.update(status="fetch_error", s3_key=None, reason=str(e))
            articles.append(line)
            continue

        try:
            classification = classify_article(team_name, title, article_text)
        except Exception as e:  # noqa: BLE001 - transient API failure, retried next run
            counts["fetch_errors"] += 1
            line.update(status="classify_error", s3_key=None, reason=str(e))
            articles.append(line)
            continue

        relevance = classification["relevance"]

        if relevance == "NONE":
            counts["skipped_not_about_team"] += 1
            line.update(status="skipped_not_about_team", s3_key=None, reason=classification["reason"])
            articles.append(line)
            mark_processed(config["bucket"], marker, "NONE", url, None)
            continue

        coverage = "full" if relevance == "FULL" else "summary"
        body_text = article_text if relevance == "FULL" else classification["summary"]
        markdown = build_markdown(team_name, title, url, published, author, coverage, body_text)
        metadata = build_metadata(
            team_name=team_name,
            title=title,
            source=config["source_site"],
            published_date=entry_time.strftime("%Y-%m-%d"),
            url=url,
        )
        put_document(config["bucket"], doc_key, markdown, metadata)
        mark_processed(config["bucket"], marker, relevance, url, doc_key)

        counts["documents_written"] += 1
        written_keys.append(doc_key)
        line.update(
            status="written_full" if relevance == "FULL" else "written_summary",
            s3_key=doc_key,
            reason=classification["reason"],
        )
        articles.append(line)

    return build_report(config, run_at, len(selected), counts, articles, written_keys)


def lambda_handler(event, context):
    config = resolve_config(event)
    run_at = datetime.now(timezone.utc).isoformat()

    missing = [k for k in ("team_name", "rss_url") if not config[k]]
    if missing:
        error = f"missing required event value(s): {', '.join(missing)}"
        print(f"ERROR: feed_ingest invocation rejected: {error}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"team_name": config["team_name"], "rss_url": config["rss_url"], "error": error}
            ),
        }

    try:
        report = _run_ingest(config, run_at)
        log_report(report)
        return {"statusCode": 200, "body": json.dumps(report)}
    except Exception as e:  # noqa: BLE001 - whole-run failure (e.g. feed itself unparseable)
        print(f"ERROR: feed_ingest run failed for {config['team_name']}: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps(
                {"team_name": config["team_name"], "rss_url": config["rss_url"], "error": str(e)}
            ),
        }
