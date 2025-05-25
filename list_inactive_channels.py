#!/usr/bin/env python3
"""
Slack Inactive Channels Detector

This script identifies Slack channels that have been inactive for a specified
period of time. It uses the Slack API to fetch channel information and activity.

Required environment variables:
- SLACK_API_TOKEN: Your Slack API token with appropriate permissions
- DEFAULT_INACTIVE_DAYS: (Optional) Default number of days to consider a channel inactive (default: 90)
- EXCLUDE_CHANNELS: (Optional) Comma-separated list of channel names to exclude from analysis

Example usage:
    export SLACK_API_TOKEN="xoxb-your-token"
    export EXCLUDE_CHANNELS="general,random,announcements"
    python list_inactive_channels.py

Command line arguments:
    --days DAYS              Number of days to consider a channel inactive
    --exclude CHANNELS       Comma-separated list of channel names to exclude
    --export FILENAME        Export results to CSV, JSON, and HTML files with this base name
    --archive                Archive inactive channels (requires confirmation)
    --no-interactive         Run in non-interactive mode (requires --days and --export)
"""

import argparse
import asyncio
import csv
import datetime
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional, Any, Union, Tuple, Set

import aiohttp
import requests
import urllib3
from dateutil.parser import parse as parse_date

# Setup logging
logging.basicConfig(
    filename='slack_errors.log',
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Add file handler to write logs to file without console output
file_handler = logging.FileHandler('slack_errors.log')
file_handler.setLevel(logging.WARNING)
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)

# Get the root logger and remove any existing handlers
root_logger = logging.getLogger()
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# Add only the file handler
root_logger.addHandler(file_handler)

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# API Constants
SLACK_API_BASE_URL = "https://slack.com/api"
CONVERSATIONS_LIST_ENDPOINT = f"{SLACK_API_BASE_URL}/conversations.list"
CONVERSATIONS_HISTORY_ENDPOINT = f"{SLACK_API_BASE_URL}/conversations.history"
CONVERSATIONS_ARCHIVE_ENDPOINT = f"{SLACK_API_BASE_URL}/conversations.archive"

# Error Constants
ERROR_RATE_LIMITED = "ratelimited"
ERROR_NOT_IN_CHANNEL = "not_in_channel"

# Default channel exclusions
DEFAULT_EXCLUDE_CHANNELS = {"general", "random", "announcements"}

# Get Slack API token and other settings from environment variables
SLACK_API_TOKEN = os.getenv("SLACK_API_TOKEN")
if not SLACK_API_TOKEN:
    raise ValueError("SLACK_API_TOKEN environment variable is required")

# Check for and remove any non-ASCII characters in the token
SLACK_API_TOKEN = SLACK_API_TOKEN.encode('ascii', 'ignore').decode('ascii')

# Parse and validate DEFAULT_INACTIVE_DAYS
try:
    DEFAULT_INACTIVE_DAYS = int(os.getenv("DEFAULT_INACTIVE_DAYS", "90"))
    if DEFAULT_INACTIVE_DAYS <= 0:
        raise ValueError("DEFAULT_INACTIVE_DAYS must be a positive integer")
except ValueError:
    raise ValueError("DEFAULT_INACTIVE_DAYS must be a valid integer")

# Get excluded channels from environment variable
EXCLUDE_CHANNELS_ENV = os.getenv("EXCLUDE_CHANNELS", "")
EXCLUDE_CHANNELS = set(
    channel.strip() for channel in EXCLUDE_CHANNELS_ENV.split(",") if channel.strip()
) | DEFAULT_EXCLUDE_CHANNELS


class SlackApiError(Exception):
    """Base exception for Slack API errors."""
    pass


class RateLimitExceededError(SlackApiError):
    """Exception raised when Slack API rate limits are exceeded."""
    pass


def slack_get_request(url: str, headers: Dict[str, str], params: Dict[str, Any], 
                       retries: int = 5, backoff_factor: int = 2) -> Dict[str, Any]:
    """
    Make a GET request to the Slack API, handling rate limit errors.
    
    Args:
        url: The API endpoint URL
        headers: Request headers including authorization
        params: Query parameters for the request
        retries: Maximum number of retry attempts for rate limiting
        backoff_factor: Factor to increase wait time between retries
        
    Returns:
        The JSON response from the API as a dictionary
        
    Raises:
        RateLimitExceededError: If rate limit retries are exhausted
        SlackApiError: For other Slack API errors
    """
    # Sanitize headers and params to ensure they contain only ASCII characters
    sanitized_headers = {}
    for key, value in headers.items():
        sanitized_headers[key] = value.encode('ascii', 'ignore').decode('ascii') if isinstance(value, str) else value
    
    sanitized_params = {}
    for key, value in params.items():
        sanitized_params[key] = value.encode('ascii', 'ignore').decode('ascii') if isinstance(value, str) else value
        
    for attempt in range(retries):
        try:
            # Disable SSL verification
            response = requests.get(url, headers=sanitized_headers, params=sanitized_params, verify=False)
            data = response.json()

            if data.get("ok"):
                return data
            
            if data.get("error") == ERROR_RATE_LIMITED:
                retry_after = int(response.headers.get("Retry-After", 1))
                wait_time = backoff_factor ** attempt * retry_after
                print(f"Rate limit hit. Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                error_message = data.get('error', 'Unknown error')
                raise SlackApiError(f"Error making request to Slack API: {error_message}")
        except requests.exceptions.JSONDecodeError:
            raise SlackApiError("Invalid JSON response from Slack API")
        
    raise RateLimitExceededError("Exceeded maximum retries due to rate limit errors.")


def get_channels(exclude_channels: Set[str] = None, single_channel: str = None, limit_channels: int = None) -> List[Dict[str, Any]]:
    """
    Fetch all Slack channels with pagination support.
    
    Args:
        exclude_channels: Set of channel names to exclude from results
        single_channel: If specified, only return this specific channel
        limit_channels: If specified, limit the number of channels returned (for testing)
        
    Returns:
        A list of channel objects from the Slack API
        
    Raises:
        SlackApiError: If the API request fails
    """
    url = CONVERSATIONS_LIST_ENDPOINT
    headers = {"Authorization": f"Bearer {SLACK_API_TOKEN}"}
    params = {"exclude_archived": "true", "types": "public_channel,private_channel", "limit": 1000}

    channels = []
    excluded_count = 0
    
    # Use provided exclude_channels set or the global one
    exclude_set = exclude_channels if exclude_channels is not None else EXCLUDE_CHANNELS
    
    while True:
        data = slack_get_request(url, headers, params)

        # Check if channels key exists in the response
        if "channels" not in data:
            raise SlackApiError("Missing 'channels' in API response")
        
        # Filter channels based on single_channel or exclude_channels
        for channel in data["channels"]:
            channel_name = channel.get("name")
            
            # If single_channel is specified, only include that channel
            if single_channel:
                if channel_name == single_channel:
                    channels.append(channel)
                    break  # Found the specific channel, no need to continue
            else:
                # Normal filtering - exclude channels in exclude_set
                if channel_name not in exclude_set:
                    channels.append(channel)
                else:
                    excluded_count += 1

        # If we found the single channel, no need to paginate further
        if single_channel and channels:
            break
            
        # If we've reached the limit, stop fetching more channels
        if limit_channels and len(channels) >= limit_channels:
            channels = channels[:limit_channels]
            break
            
        if not data.get("response_metadata", {}).get("next_cursor"):
            break

        params["cursor"] = data["response_metadata"]["next_cursor"]

    if excluded_count > 0:
        logging.info(f"Excluded {excluded_count} channels based on exclude list")
    
    # If single_channel was specified but not found, raise an error
    if single_channel and not channels:
        raise SlackApiError(f"Channel '{single_channel}' not found")
        
    return channels


def get_channel_last_activity(channel_id: str) -> Optional[datetime.datetime]:
    """
    Get the last message timestamp in the channel.
    
    Args:
        channel_id: The Slack channel ID
        
    Returns:
        A datetime object of the last message or None if no messages
        
    Raises:
        SlackApiError: If the API request fails
    """
    url = CONVERSATIONS_HISTORY_ENDPOINT
    headers = {"Authorization": f"Bearer {SLACK_API_TOKEN}"}
    params = {"channel": channel_id, "limit": 1}

    try:
        data = slack_get_request(url, headers, params)

        if "messages" not in data:
            raise SlackApiError("Missing 'messages' in API response")
            
        messages = data["messages"]
        if not messages:
            return None

        # Convert the Unix timestamp to a datetime object
        timestamp = float(messages[0].get("ts", 0))
        if timestamp == 0:
            raise SlackApiError("Invalid timestamp in message data")
            
        return datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    except (KeyError, ValueError) as e:
        raise SlackApiError(f"Error processing channel activity data: {str(e)}")


async def get_channel_last_activity_async(channel_id: str, session: aiohttp.ClientSession) -> tuple[str, Optional[datetime.datetime]]:
    """
    Asynchronously get the last message timestamp in the channel.
    
    Args:
        channel_id: The Slack channel ID
        session: The aiohttp client session
        
    Returns:
        A tuple of (channel_id, datetime) or (channel_id, None) if no messages
    """
    url = CONVERSATIONS_HISTORY_ENDPOINT
    headers = {"Authorization": f"Bearer {SLACK_API_TOKEN}"}
    params = {"channel": channel_id, "limit": 1}

    # Sanitize headers and params to ensure they contain only ASCII characters
    sanitized_headers = {}
    for key, value in headers.items():
        sanitized_headers[key] = value.encode('ascii', 'ignore').decode('ascii') if isinstance(value, str) else value
    
    sanitized_params = {}
    for key, value in params.items():
        sanitized_params[key] = value.encode('ascii', 'ignore').decode('ascii') if isinstance(value, str) else value

    try:
        for _ in range(5):  # Simple retry logic
            async with session.get(url, headers=sanitized_headers, params=sanitized_params) as response:
                data = await response.json()
                
                if data.get("ok"):
                    break
                    
                if data.get("error") == ERROR_RATE_LIMITED:
                    retry_after = int(response.headers.get("Retry-After", 1))
                    await asyncio.sleep(retry_after)
                elif data.get("error") == ERROR_NOT_IN_CHANNEL:
                    # Try to join the channel if it's public
                    try:
                        join_url = f"{SLACK_API_BASE_URL}/conversations.join"
                        join_params = {"channel": channel_id}
                        async with session.post(join_url, headers=sanitized_headers, data=join_params) as join_response:
                            join_data = await join_response.json()
                            if join_data.get("ok"):
                                print(f"Successfully joined channel {channel_id}, retrying history fetch...")
                                # Successfully joined, retry getting history
                                continue
                            else:
                                print(f"Failed to join channel {channel_id}: {join_data.get('error')}")
                                logging.warning(f"Bot is not in channel {channel_id} and failed to join: {join_data.get('error')}")
                                return channel_id, None
                    except Exception as join_error:
                        print(f"Exception while trying to join channel {channel_id}: {join_error}")
                        logging.warning(f"Bot is not in channel {channel_id}, cannot fetch history: {join_error}")
                        return channel_id, None
                else:
                    raise SlackApiError(f"Error in API request: {data.get('error')}")
        
        if not data.get("ok"):
            raise SlackApiError("Failed to get channel history after retries")
            
        messages = data.get("messages", [])
        if not messages:
            return channel_id, None

        timestamp = float(messages[0].get("ts", 0))
        if timestamp == 0:
            return channel_id, None
            
        last_activity = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
        return channel_id, last_activity
    except Exception as e:
        print(f"Error getting activity for channel {channel_id}: {str(e)}")
        return channel_id, None


async def archive_channel(channel_id: str) -> bool:
    """
    Archive a Slack channel.
    
    Args:
        channel_id: The ID of the channel to archive
        
    Returns:
        True if archiving was successful, False otherwise
    """
    url = CONVERSATIONS_ARCHIVE_ENDPOINT
    headers = {"Authorization": f"Bearer {SLACK_API_TOKEN}"}
    params = {"channel": channel_id}
    
    try:
        data = slack_get_request(url, headers, params)
        return data.get("ok", False)
    except Exception as e:
        logging.error(f"Error archiving channel {channel_id}: {str(e)}")
        return False


async def find_inactive_channels_async(days: int = DEFAULT_INACTIVE_DAYS, 
                                      exclude_channels: Set[str] = None, single_channel: str = None, limit_channels: int = None) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[datetime.datetime]]]:
    """
    Asynchronously find channels inactive for the specified number of days.
    
    Args:
        days: Number of days of inactivity to check for
        exclude_channels: Set of channel names to exclude from results
        single_channel: If specified, only check this specific channel
        limit_channels: If specified, limit the number of channels to check (for testing)
        
    Returns:
        A tuple containing:
        - A list of inactive channel objects
        - A dictionary mapping channel IDs to their last activity datetime
    """
    inactive_channels = []
    now = datetime.datetime.now(datetime.timezone.utc)
    threshold = now - datetime.timedelta(days=days)
    
    channels = get_channels(exclude_channels, single_channel, limit_channels)
    
    # Create a mapping of channel ID to name and other info
    channel_map = {channel["id"]: channel for channel in channels}
    
    # Configure SSL context to disable certificate verification
    ssl_context = aiohttp.TCPConnector(ssl=False)
    
    # Dictionary to store channel_id -> last_activity mapping
    activity_map = {}
    
    # Asynchronously get last activity for all channels
    async with aiohttp.ClientSession(connector=ssl_context) as session:
        # Use semaphore to limit concurrency and avoid rate limits
        semaphore = asyncio.Semaphore(5)  # Limit to 5 concurrent requests
        
        # For progress tracking
        total_channels = len(channels)
        completed = 0
        last_percentage = -1
        
        async def bounded_fetch(channel_id):
            nonlocal completed, last_percentage
            async with semaphore:
                result = await get_channel_last_activity_async(channel_id, session)
                
                # Update progress counter
                completed += 1
                percentage = int((completed / total_channels) * 100)
                
                # Only print when percentage changes to avoid flooding console
                if percentage != last_percentage and percentage % 5 == 0:
                    print(f"Progress: {percentage}% ({completed}/{total_channels} channels processed)")
                    last_percentage = percentage
                    
                return result
        
        print(f"Starting to process {total_channels} channels...")
        tasks = [bounded_fetch(channel["id"]) for channel in channels]
        results = await asyncio.gather(*tasks)
        
        for channel_id, last_activity in results:
            # Store the activity time for all channels
            activity_map[channel_id] = last_activity
            
            if last_activity is None or last_activity < threshold:
                if channel_id in channel_map:
                    inactive_channels.append(channel_map[channel_id])
    
    return inactive_channels, activity_map


def find_inactive_channels(days: int = DEFAULT_INACTIVE_DAYS, 
                        exclude_channels: Set[str] = None, single_channel: str = None, limit_channels: int = None) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[datetime.datetime]]]:
    """
    Find channels inactive for the specified number of days.
    
    Args:
        days: Number of days of inactivity to check for
        exclude_channels: Set of channel names to exclude from results
        single_channel: If specified, only check this specific channel
        limit_channels: If specified, limit the number of channels to check (for testing)
        
    Returns:
        A tuple containing:
        - A list of inactive channel objects
        - A dictionary mapping channel IDs to their last activity datetime
    """
    # Use asyncio to run the async version
    return asyncio.run(find_inactive_channels_async(days, exclude_channels, single_channel, limit_channels))


def validate_days_input(input_value: str) -> int:
    """
    Validate the user input for number of days.
    
    Args:
        input_value: User input string
        
    Returns:
        Validated integer value
        
    Raises:
        ValueError: If input is not a positive integer
    """
    if not input_value:
        return DEFAULT_INACTIVE_DAYS
        
    try:
        days = int(input_value)
        if days <= 0:
            raise ValueError("Number of days must be positive")
        return days
    except ValueError:
        raise ValueError("Please enter a valid positive number")


def export_inactive_channels(inactive_channels: List[Dict[str, Any]], 
                       activity_map: Dict[str, Optional[datetime.datetime]], 
                       days: int, filename: str = "inactive_channels.csv") -> None:
    """
    Export inactive channels data to CSV, JSON, and HTML files, sorted by activity date.
    All files are saved to the 'data' subdirectory.
    
    Args:
        inactive_channels: List of inactive channel objects
        activity_map: Dictionary mapping channel IDs to last activity datetime
        days: Number of days of inactivity checked
        filename: Name of the output file (without extension)
    """
    if not inactive_channels:
        print(f"No channels found to export.")
        return
    
    # Ensure data directory exists
    os.makedirs('data', exist_ok=True)
    
    # Get base filename without extension
    base_filename = filename.split('.')[0] if '.' in filename else filename
        
    # Prepare data for export
    export_data = []
    for channel in inactive_channels:
        channel_id = channel.get("id")
        channel_name = channel.get("name", "unknown")
        created_ts = channel.get("created", 0)
        created = datetime.datetime.fromtimestamp(created_ts, tz=datetime.timezone.utc) if created_ts else None
        member_count = channel.get("num_members", 0)
        last_activity = activity_map.get(channel_id)
        
        # Calculate days since last activity
        days_inactive = None
        if last_activity:
            now = datetime.datetime.now(datetime.timezone.utc)
            days_inactive = (now - last_activity).days
        
        export_data.append({
            "channel_id": channel_id,
            "channel_name": channel_name,
            "created_date": created.isoformat() if created else None,
            "member_count": member_count,
            "last_activity": last_activity.isoformat() if last_activity else None,
            "days_inactive": days_inactive,
            "is_private": channel.get("is_private", False),
            "topic": channel.get("topic", {}).get("value", ""),
            "purpose": channel.get("purpose", {}).get("value", ""),
            # Include any other channel data you want
        })
    
    # Sort by days_inactive (most inactive first)
    sorted_data = sorted(
        export_data, 
        key=lambda x: (x["days_inactive"] if x["days_inactive"] is not None else float('inf')),
        reverse=True
    )
    
    # Write to CSV
    csv_filename = f"data/{base_filename}.csv"
    with open(csv_filename, 'w', newline='') as csvfile:
        if not sorted_data:
            print(f"No data to write to {csv_filename}")
            return
            
        fieldnames = sorted_data[0].keys()
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(sorted_data)
    
    print(f"Exported {len(sorted_data)} inactive channels to {csv_filename}")
    
    # Export as JSON for more complete data
    json_filename = f"data/{base_filename}.json"
    with open(json_filename, 'w') as jsonfile:
        json.dump(sorted_data, jsonfile, indent=2)
    
    print(f"Exported complete channel data to {json_filename}")
    
    # Export as HTML table
    html_filename = f"data/{base_filename}.html"
    
    # Define HTML template with CSS for a nice table
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Slack Inactive Channels Report</title>
    <style>
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    margin: 0;
    padding: 20px;
    line-height: 1.6;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    min-height: 100vh;
}}

.container {{
    max-width: 1400px;
    margin: 0 auto;
    background: white;
    border-radius: 15px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.2);
    overflow: hidden;
}}

.header {{
    background: linear-gradient(135deg, #4A154B 0%, #6B2C91 100%);
    color: white;
    padding: 30px;
    text-align: center;
}}

h1 {{
    margin: 0;
    font-size: 2.5em;
    font-weight: 300;
    letter-spacing: -1px;
}}

.summary {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 20px;
    padding: 30px;
    background: #f8f9fa;
    border-bottom: 1px solid #e9ecef;
}}

.summary-item {{
    text-align: center;
    padding: 20px;
    background: white;
    border-radius: 10px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
}}

.summary-label {{
    font-size: 0.9em;
    color: #6c757d;
    margin-bottom: 5px;
    text-transform: uppercase;
    letter-spacing: 1px;
}}

.summary-value {{
    font-size: 1.8em;
    font-weight: bold;
    color: #4A154B;
}}

.controls {{
    padding: 30px;
    background: white;
    border-bottom: 1px solid #e9ecef;
}}

.search-container {{
    position: relative;
    max-width: 400px;
    margin: 0 auto;
}}

#searchInput {{
    width: 100%;
    padding: 15px 20px 15px 50px;
    border: 2px solid #e9ecef;
    border-radius: 25px;
    font-size: 16px;
    transition: border-color 0.3s ease;
    box-sizing: border-box;
}}

#searchInput:focus {{
    outline: none;
    border-color: #4A154B;
}}

.search-icon {{
    position: absolute;
    left: 18px;
    top: 50%;
    transform: translateY(-50%);
    color: #6c757d;
    font-size: 18px;
}}

.table-container {{
    padding: 30px;
    overflow-x: auto;
}}

table {{
    width: 100%;
    border-collapse: collapse;
    border-radius: 10px;
    overflow: hidden;
    box-shadow: 0 0 20px rgba(0,0,0,0.1);
}}

th {{
    background: linear-gradient(135deg, #4A154B 0%, #6B2C91 100%);
    color: white;
    padding: 20px 15px;
    text-align: left;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    font-size: 0.85em;
    cursor: pointer;
    user-select: none;
    position: relative;
    transition: background-color 0.3s ease;
}}

th:hover {{
    background: linear-gradient(135deg, #5A255B 0%, #7B3CA1 100%);
}}

th.sortable::after {{
    content: '↕';
    position: absolute;
    right: 10px;
    top: 50%;
    transform: translateY(-50%);
    opacity: 0.5;
    font-size: 14px;
}}

th.sort-asc::after {{
    content: '↑';
    opacity: 1;
}}

th.sort-desc::after {{
    content: '↓';
    opacity: 1;
}}

td {{
    padding: 15px;
    border-bottom: 1px solid #e9ecef;
    vertical-align: top;
}}

tr:nth-child(even) {{
    background-color: #f8f9fa;
}}

tr:hover {{
    background-color: #e3f2fd;
    transform: scale(1.01);
    transition: all 0.2s ease;
}}

.channel-name {{
    font-weight: 600;
    color: #4A154B;
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
}}

.days-inactive {{
    text-align: center;
    font-weight: bold;
}}

.days-high {{
    color: #dc3545;
}}

.days-medium {{
    color: #fd7e14;
}}

.days-low {{
    color: #28a745;
}}

.member-count {{
    text-align: center;
    font-weight: 500;
}}

.topic, .purpose {{
    max-width: 250px;
    word-wrap: break-word;
    line-height: 1.4;
    color: #6c757d;
}}

.created-date {{
    white-space: nowrap;
    font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
    font-size: 0.9em;
    color: #6c757d;
}}

.private-badge {{
    display: inline-block;
    background: #6f42c1;
    color: white;
    font-size: 0.75em;
    padding: 3px 8px;
    border-radius: 12px;
    margin-left: 8px;
    font-weight: 500;
}}

.no-results {{
    text-align: center;
    padding: 40px;
    color: #6c757d;
    font-style: italic;
    display: none;
}}

.footer {{
    text-align: center;
    padding: 20px;
    background: #f8f9fa;
    color: #6c757d;
    font-size: 0.9em;
}}

@media (max-width: 768px) {{
    .container {{
        margin: 10px;
        border-radius: 10px;
    }}
    
    .summary {{
        grid-template-columns: 1fr;
    }}
    
    table {{
        font-size: 0.9em;
    }}
    
    th, td {{
        padding: 10px 8px;
    }}
    
    .topic, .purpose {{
        max-width: 150px;
    }}
}}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🔍 Slack Inactive Channels Report</h1>
        </div>
        
        <div class="summary">
            <div class="summary-item">
                <div class="summary-label">Report Date</div>
                <div class="summary-value">{date}</div>
            </div>
            <div class="summary-item">
                <div class="summary-label">Inactivity Threshold</div>
                <div class="summary-value">{days} days</div>
            </div>
            <div class="summary-item">
                <div class="summary-label">Inactive Channels</div>
                <div class="summary-value">{total_channels}</div>
            </div>
        </div>
        
        <div class="controls">
            <div class="search-container">
                <span class="search-icon">🔍</span>
                <input type="text" id="searchInput" placeholder="Search channels, topics, or purposes...">
            </div>
        </div>
        
        <div class="table-container">
            <table id="channelsTable">
                <thead>
                    <tr>
                        <th class="sortable" data-column="0">Channel</th>
                        <th class="sortable" data-column="1">Days Inactive</th>
                        <th class="sortable" data-column="2">Members</th>
                        <th class="sortable" data-column="3">Created</th>
                        <th class="sortable" data-column="4">Topic</th>
                        <th class="sortable" data-column="5">Purpose</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>
            <div class="no-results" id="noResults">
                No channels found matching your search criteria.
            </div>
        </div>
        
        <div class="footer">
            Generated by Slack Inactive Channels Detector
        </div>
    </div>

    <script>
        // Search functionality
        const searchInput = document.getElementById('searchInput');
        const table = document.getElementById('channelsTable');
        const tbody = table.querySelector('tbody');
        const noResults = document.getElementById('noResults');
        const rows = Array.from(tbody.querySelectorAll('tr'));

        searchInput.addEventListener('input', function() {{
            const searchTerm = this.value.toLowerCase().trim();
            let visibleCount = 0;

            rows.forEach(row => {{
                const text = row.textContent.toLowerCase();
                if (text.includes(searchTerm)) {{
                    row.style.display = '';
                    visibleCount++;
                }} else {{
                    row.style.display = 'none';
                }}
            }});

            if (visibleCount === 0 && searchTerm !== '') {{
                noResults.style.display = 'block';
                table.style.display = 'none';
            }} else {{
                noResults.style.display = 'none';
                table.style.display = 'table';
            }}
        }});

        // Sorting functionality
        let currentSort = {{ column: 1, direction: 'desc' }}; // Default sort by days inactive

        function sortTable(columnIndex, direction) {{
            const tbody = table.querySelector('tbody');
            const rowsArray = Array.from(tbody.querySelectorAll('tr'));

            rowsArray.sort((a, b) => {{
                let aVal = a.children[columnIndex].textContent.trim();
                let bVal = b.children[columnIndex].textContent.trim();

                // Handle numeric columns
                if (columnIndex === 1 || columnIndex === 2) {{ // Days Inactive or Members
                    aVal = parseInt(aVal) || 0;
                    bVal = parseInt(bVal) || 0;
                }} else if (columnIndex === 3) {{ // Created date
                    aVal = new Date(aVal);
                    bVal = new Date(bVal);
                }} else {{
                    // String comparison
                    aVal = aVal.toLowerCase();
                    bVal = bVal.toLowerCase();
                }}

                if (direction === 'asc') {{
                    return aVal > bVal ? 1 : aVal < bVal ? -1 : 0;
                }} else {{
                    return aVal < bVal ? 1 : aVal > bVal ? -1 : 0;
                }}
            }});

            // Clear existing rows
            tbody.innerHTML = '';
            
            // Add sorted rows
            rowsArray.forEach(row => tbody.appendChild(row));

            // Update header classes
            document.querySelectorAll('th').forEach(th => {{
                th.classList.remove('sort-asc', 'sort-desc');
            }});
            
            const currentHeader = document.querySelector(`th[data-column="${{columnIndex}}"]`);
            currentHeader.classList.add(direction === 'asc' ? 'sort-asc' : 'sort-desc');
        }}

        // Add click listeners to sortable headers
        document.querySelectorAll('th.sortable').forEach(header => {{
            header.addEventListener('click', function() {{
                const columnIndex = parseInt(this.dataset.column);
                
                if (currentSort.column === columnIndex) {{
                    currentSort.direction = currentSort.direction === 'asc' ? 'desc' : 'asc';
                }} else {{
                    currentSort.direction = 'desc';
                }}
                
                currentSort.column = columnIndex;
                sortTable(columnIndex, currentSort.direction);
            }});
        }});

        // Initial sort by days inactive (descending)
        sortTable(1, 'desc');
    </script>
</body>
</html>
"""
    
    # Generate table rows for HTML
    table_rows = ""
    for item in sorted_data:
        days_inactive_str = str(item["days_inactive"]) if item["days_inactive"] is not None else "Unknown"
        topic = item["topic"] or "-"
        purpose = item["purpose"] or "-"
        
        # Color code days inactive
        days_class = ""
        if item["days_inactive"]:
            if item["days_inactive"] > 365:
                days_class = "days-high"
            elif item["days_inactive"] > 180:
                days_class = "days-medium"
            else:
                days_class = "days-low"
        
        # Format created date
        created_date = item["created_date"]
        if created_date:
            try:
                from dateutil.parser import parse as parse_date
                parsed_date = parse_date(created_date)
                created_display = parsed_date.strftime("%Y-%m-%d")
            except:
                created_display = created_date[:10] if created_date else "-"
        else:
            created_display = "-"
        
        # Add private badge if applicable
        private_badge = '<span class="private-badge">PRIVATE</span>' if item.get("is_private") else ""
        
        table_rows += f"""
            <tr>
                <td class="channel-name">#{item["channel_name"]}{private_badge}</td>
                <td class="days-inactive {days_class}">{days_inactive_str}</td>
                <td class="member-count">{item["member_count"]}</td>
                <td class="created-date">{created_display}</td>
                <td class="topic">{topic}</td>
                <td class="purpose">{purpose}</td>
            </tr>
        """
    
    # Fill in the HTML template
    now = datetime.datetime.now().strftime("%Y-%m-%d")
    html_content = html_template.format(
        date=now,
        days=days,
        total_channels=len(sorted_data),
        table_rows=table_rows
    )
    
    # Write HTML file
    with open(html_filename, 'w') as htmlfile:
        htmlfile.write(html_content)
    
    print(f"Exported HTML report to {html_filename}")


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Slack Inactive Channels Detector")
    parser.add_argument(
        "--days", type=int, 
        help=f"Number of days to consider a channel inactive (default: {DEFAULT_INACTIVE_DAYS})"
    )
    parser.add_argument(
        "--exclude", type=str, 
        help="Comma-separated list of channel names to exclude"
    )
    parser.add_argument(
        "--export", type=str, 
        help="Export results to CSV, JSON, and HTML files with this base name"
    )
    parser.add_argument(
        "--archive", action="store_true", 
        help="Archive inactive channels (requires confirmation)"
    )
    parser.add_argument(
        "--no-interactive", action="store_true", 
        help="Run in non-interactive mode (requires --days and --export)"
    )
    parser.add_argument(
        "--channel", type=str, 
        help="Check data for only one specific channel (use channel name)"
    )
    parser.add_argument(
        "--test", action="store_true", 
        help="Test mode: only check the first 100 channels"
    )
    
    return parser.parse_args()


async def archive_inactive_channels(inactive_channels: List[Dict[str, Any]], interactive: bool = True) -> None:
    """
    Archive inactive channels with user confirmation.
    
    Args:
        inactive_channels: List of inactive channel objects
        interactive: Whether to prompt for confirmation interactively
    """
    if not inactive_channels:
        print("No channels to archive.")
        return
    
    channels_to_archive = []
    
    if interactive:
        print("\nChannels that will be archived:")
        for channel in inactive_channels:
            channel_name = channel.get("name", "unknown")
            member_count = channel.get("num_members", 0)
            print(f"- #{channel_name} (members: {member_count})")
            
        confirm = input("\nAre you sure you want to archive these channels? (y/n): ").lower()
        if not confirm.startswith('y'):
            print("Archive operation cancelled.")
            return
            
        # Ask for channels to exclude from archiving
        exclude_input = input("\nEnter channel names to exclude from archiving (comma-separated, optional): ")
        exclude_from_archive = {name.strip() for name in exclude_input.split(",") if name.strip()}
        
        # Filter out excluded channels
        channels_to_archive = [
            channel for channel in inactive_channels 
            if channel.get("name") not in exclude_from_archive
        ]
    else:
        # In non-interactive mode, archive all channels without confirmation
        channels_to_archive = inactive_channels
    
    # Archive channels
    success_count = 0
    for channel in channels_to_archive:
        channel_id = channel.get("id")
        channel_name = channel.get("name", "unknown")
        
        print(f"Archiving channel #{channel_name}...")
        success = await archive_channel(channel_id)
        
        if success:
            success_count += 1
            print(f"✓ Successfully archived #{channel_name}")
        else:
            print(f"✗ Failed to archive #{channel_name}")
    
    print(f"\nArchived {success_count} of {len(channels_to_archive)} channels.")


def main() -> None:
    """Main function to run the script."""
    try:
        args = parse_arguments()
        
        # Handle non-interactive mode requirements
        if args.no_interactive and (args.days is None or args.export is None):
            print("Error: --no-interactive mode requires both --days and --export")
            sys.exit(1)
        
        # Get inactivity threshold
        days = DEFAULT_INACTIVE_DAYS
        if args.no_interactive or args.days:
            days = args.days if args.days else DEFAULT_INACTIVE_DAYS
        else:
            input_days = input(
                f"Enter number of days to check inactivity (default {DEFAULT_INACTIVE_DAYS}): "
            )
            
            try:
                days = validate_days_input(input_days) if input_days else days
            except ValueError as e:
                print(f"Error: {str(e)}")
                print(f"Using default value of {DEFAULT_INACTIVE_DAYS} days.")
        
        # Process excluded channels
        exclude_channels = EXCLUDE_CHANNELS
        if args.exclude:
            additional_excludes = {
                name.strip() for name in args.exclude.split(",") if name.strip()
            }
            exclude_channels = exclude_channels | additional_excludes
        
        # Show progress message
        if args.channel:
            print(f"Checking activity for channel '{args.channel}'...")
        elif args.test:
            print(f"TEST MODE: Checking only the first 100 channels for inactivity of {days} days or more...")
        else:
            print(f"Finding channels inactive for {days} days or more...")
        
        # Set limit for test mode
        limit_channels = 100 if args.test else None
        
        inactive_channels, activity_map = find_inactive_channels(
            days=days, exclude_channels=exclude_channels, single_channel=args.channel, limit_channels=limit_channels
        )

        if args.channel:
            # For single channel mode, show detailed information regardless of activity status
            if activity_map:
                channel_id = list(activity_map.keys())[0]
                last_activity = activity_map[channel_id]
                if last_activity:
                    days_since = (datetime.datetime.now(datetime.timezone.utc) - last_activity).days
                    print(f"\nChannel '#{args.channel}' last activity: {last_activity.strftime('%Y-%m-%d %H:%M:%S UTC')} ({days_since} days ago)")
                    if days_since >= days:
                        print(f"✗ Channel is INACTIVE (inactive for {days_since} days, threshold: {days} days)")
                    else:
                        print(f"✓ Channel is ACTIVE (inactive for only {days_since} days, threshold: {days} days)")
                else:
                    print(f"\nChannel '#{args.channel}' has no message history or bot cannot access it")
                    print(f"✗ Channel is considered INACTIVE (no accessible activity)")
            else:
                print(f"\nChannel '{args.channel}' not found")
        else:
            # Normal multi-channel mode
            if inactive_channels:
                test_suffix = " (from first 100 channels)" if args.test else ""
                print(f"\nFound {len(inactive_channels)} channels inactive for {days} days or more{test_suffix}:")
                for channel in sorted(inactive_channels, key=lambda c: c["name"]):
                    # Show channel name and creation date for context
                    created = datetime.datetime.fromtimestamp(channel.get("created", 0), 
                                                              tz=datetime.timezone.utc)
                    member_count = channel.get("num_members", 0)
                    channel_name = channel.get("name", "unknown")
                    print(f"- #{channel_name} (created: {created.date()}, members: {member_count})")
            
                # Export results if requested
                export_filename = None
                if args.no_interactive:
                    if args.export:
                        export_filename = args.export
                else:
                    # Ask if user wants to export the results
                    export_choice = input("\nDo you want to export the results to files? (y/n): ").lower()
                    if export_choice.startswith('y'):
                        export_filename = input("Enter base filename without extension (default: inactive_channels): ") or "inactive_channels"
                
                if export_filename:
                    export_inactive_channels(inactive_channels, activity_map, days, export_filename)
                
                # Archive channels if requested
                if args.archive:
                    asyncio.run(archive_inactive_channels(inactive_channels, not args.no_interactive))
            else:
                test_suffix = " (from first 100 channels)" if args.test else ""
                print(f"No channels found that have been inactive for {days} days or more{test_suffix}.")
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
    except SlackApiError as e:
        print(f"Slack API Error: {str(e)}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Error: {str(e)}")


if __name__ == "__main__":
    main()