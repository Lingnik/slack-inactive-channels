#!/usr/bin/env python3
import datetime
import os
import time

import requests
from dateutil.parser import parse as parse_date

# Get Slack API token and other settings from environment variables
SLACK_API_TOKEN = os.getenv("SLACK_API_TOKEN")
DEFAULT_INACTIVE_DAYS = int(os.getenv("DEFAULT_INACTIVE_DAYS", 90))


def slack_get_request(url, headers, params, retries=5, backoff_factor=2):
    """Make a GET request to the Slack API, handling rate limit errors."""
    for attempt in range(retries):
        response = requests.get(url, headers=headers, params=params)
        data = response.json()

        if data.get("ok"):
            return data
        
        if data.get("error") == "ratelimited":
            retry_after = int(response.headers.get("Retry-After", 1))
            wait_time = backoff_factor ** attempt * retry_after
            print(f"Rate limit hit. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
        else:
            raise Exception(f"Error making request to Slack API: {data['error']}")
    
    raise Exception("Exceeded maximum retries due to rate limit errors.")


def get_channels():
    """Fetch all Slack channels."""
    url = "https://slack.com/api/conversations.list"
    headers = {"Authorization": f"Bearer {SLACK_API_TOKEN}"}
    params = {"exclude_archived": "true", "limit": 1000}

    channels = []
    while True:
        data = slack_get_request(url, headers, params)

        channels.extend(data["channels"])

        if not data.get("response_metadata", {}).get("next_cursor"):
            break

        params["cursor"] = data["response_metadata"]["next_cursor"]

    return channels


def get_channel_last_activity(channel_id):
    """Get the last message timestamp in the channel."""
    url = "https://slack.com/api/conversations.history"
    headers = {"Authorization": f"Bearer {SLACK_API_TOKEN}"}
    params = {"channel": channel_id, "limit": 1}

	data = slack_get_request(url, headers, params)

    messages = data["messages"]
    if not messages:
        return None

    # Convert the Unix timestamp to a datetime object
    timestamp = float(messages[0]["ts"])
    return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)


def find_inactive_channels(days=DEFAULT_INACTIVE_DAYS):
    """Find channels inactive for the specified number of days."""
    inactive_channels = []
    now = datetime.datetime.now(datetime.timezone.utc)
    threshold = now - datetime.timedelta(days=days)

    channels = get_channels()
    for channel in channels:
        last_activity = get_channel_last_activity(channel["id"])

        if last_activity is None or last_activity < threshold:
            inactive_channels.append(channel["name"])

    return inactive_channels


if __name__ == "__main__":
    days = int(
        input(
            f"Enter number of days to check inactivity (default {DEFAULT_INACTIVE_DAYS}): "
        )
        or DEFAULT_INACTIVE_DAYS
    )
    inactive_channels = find_inactive_channels(days=days)

    if inactive_channels:
        print(f"Channels inactive for {days} days or more:")
        for channel in inactive_channels:
            print(f"- {channel}")
    else:
        print(f"No channels found that have been inactive for {days} days or more.")
