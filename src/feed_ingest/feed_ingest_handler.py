import json
import os
import time
from datetime import datetime, timezone
import boto3
import feedparser

# Initialize the Bedrock Runtime client
bedrock_runtime = boto3.client(service_name="bedrock-runtime", region_name="us-east-1")

# Target Amazon Nova Model ID
MODEL_ID = "amazon.nova-lite-v1:0"  # Or use "amazon.nova-pro-v1:0" depending on complexity

# How many of the most recent entries to send to the model
MAX_ENTRIES = 5


def lambda_handler(event, context):
    # 1. Configuration
    team = event.get("team_name", "Denver Broncos")
    rss_url = event.get("rss_url", "https://www.pff.com/feed/teams/10")
    max_entries = int(event.get("max_entries", MAX_ENTRIES))

    # 2. Fetch and parse the RSS feed
    feed = feedparser.parse(rss_url)
    dated_entries = []

    for entry in feed.entries:
        # Feedparser normalizes published dates to struct_time under entry.published_parsed
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            entry_time = datetime.fromtimestamp(time.mktime(entry.published_parsed), timezone.utc)

            dated_entries.append((entry_time, {
                "title": entry.get("title", "No Title"),
                "link": entry.get("link", ""),
                "summary": entry.get("summary", "No Summary"),
                "published": entry.get("published", "")
            }))

    # 3. Keep only the N most recent entries, newest first
    dated_entries.sort(key=lambda pair: pair[0], reverse=True)
    recent_entries = [item for _, item in dated_entries[:max_entries]]

    if not recent_entries:
        return {
            "statusCode": 200,
            "body": json.dumps({"message": "No RSS entries found."})
        }

    # 4. Construct the prompt for Amazon Nova
    prompt_text = (
        f"You are an expert editor. Below are the {len(recent_entries)} most recent RSS feed items.\n"
        f"Analyze them and provide a structured, executive summary highlighting the top 3 major trends or breaking topics:\n\n"
        f"{json.dumps(recent_entries, indent=2)}"
    )
    
    # 5. Format the Converse API payload required for Amazon Nova
    messages = [
        {
            "role": "user",
            "content": [{"text": prompt_text}]
        }
    ]
    
    try:
        # Use the bedrock converse API for seamless compatibility with Nova
        response = bedrock_runtime.converse(
            modelId=MODEL_ID,
            messages=messages,
            inferenceConfig={
                "maxTokens": 1000,
                "temperature": 0.3
            }
        )
        
        # Extract Nova's text output response
        ai_analysis = response["output"]["message"]["content"][0]["text"]
        
        return {
            "statusCode": 200,
            "body": json.dumps({
                "feed_processed": rss_url,
                "items_analyzed_count": len(recent_entries),
                "analysis": ai_analysis
            })
        }
        
    except Exception as e:
        print(f"Error calling Amazon Bedrock: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Failed to process text with Bedrock: {str(e)}"})
        }


