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


def get_channels(exclude_channels: Set[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch all Slack channels with pagination support.
    
    Args:
        exclude_channels: Set of channel names to exclude from results
        
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
        
        # Filter out excluded channels
        for channel in data["channels"]:
            if channel.get("name") not in exclude_set:
                channels.append(channel)
            else:
                excluded_count += 1

        if not data.get("response_metadata", {}).get("next_cursor"):
            break

        params["cursor"] = data["response_metadata"]["next_cursor"]

    if excluded_count > 0:
        logging.info(f"Excluded {excluded_count} channels based on exclude list")
        
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
                    # Log to file instead of printing to console
                    logging.warning(f"Bot is not in channel {channel_id}, cannot fetch history")
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
                                      exclude_channels: Set[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[datetime.datetime]]]:
    """
    Asynchronously find channels inactive for the specified number of days.
    
    Args:
        days: Number of days of inactivity to check for
        exclude_channels: Set of channel names to exclude from results
        
    Returns:
        A tuple containing:
        - A list of inactive channel objects
        - A dictionary mapping channel IDs to their last activity datetime
    """
    inactive_channels = []
    now = datetime.datetime.now(datetime.timezone.utc)
    threshold = now - datetime.timedelta(days=days)
    
    channels = get_channels(exclude_channels)
    
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
                        exclude_channels: Set[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Optional[datetime.datetime]]]:
    """
    Find channels inactive for the specified number of days.
    
    Args:
        days: Number of days of inactivity to check for
        exclude_channels: Set of channel names to exclude from results
        
    Returns:
        A tuple containing:
        - A list of inactive channel objects
        - A dictionary mapping channel IDs to their last activity datetime
    """
    # Use asyncio to run the async version
    return asyncio.run(find_inactive_channels_async(days, exclude_channels))


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
    
    Args:
        inactive_channels: List of inactive channel objects
        activity_map: Dictionary mapping channel IDs to last activity datetime
        days: Number of days of inactivity checked
        filename: Name of the output file (without extension)
    """
    if not inactive_channels:
        print(f"No channels found to export.")
        return
    
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
    csv_filename = f"{base_filename}.csv"
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
    json_filename = f"{base_filename}.json"
    with open(json_filename, 'w') as jsonfile:
        json.dump(sorted_data, jsonfile, indent=2)
    
    print(f"Exported complete channel data to {json_filename}")
    
    # Export as HTML table
    html_filename = f"{base_filename}.html"
    
    # Define HTML template with CSS for a nice table
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Slack Inactive Channels Report</title>
    <style>
body {{
    font-family: Arial, sans-serif;
    margin: 20px;
    line-height: 1.6;
}}
h1 {{
    color: #4A154B;
    margin-bottom: 20px;
}}
.summary {{
    margin-bottom: 20px;
    background-color: #f5f5f5;
    padding: 10px;
    border-radius: 5px;
}}
table {{
    border-collapse: collapse;
    width: 100%;
    margin-bottom: 20px;
}}
th, td {{
    border: 1px solid #ddd;
    padding: 12px;
    text-align: left;
}}
th {{
    background-color: #4A154B;
    color: white;
    position: sticky;
    top: 0;
}}
tr:nth-child(even) {{
    background-color: #f2f2f2;
}}
tr:hover {{
    background-color: #ddd;
}}
.topic, .purpose {{
    max-width: 300px;
    white-space: normal;
    word-break: break-word;
}}
.timestamp {{
    white-space: nowrap;
}}
    </style>
</head>
<body>
    <h1>Slack Inactive Channels Report</h1>
    <div class="summary">
        <p><strong>Report Date:</strong> {date}</p>
        <p><strong>Inactivity Threshold:</strong> {days} days</p>
        <p><strong>Total Inactive Channels:</strong> {total_channels}</p>
    </div>
    <table>
        <thead>
            <tr>
                <th>Channel</th>
                <th>Days Inactive</th>
                <th>Members</th>
                <th>Topic</th>
                <th>Purpose</th>
            </tr>
        </thead>
        <tbody>
            {table_rows}
        </tbody>
    </table>
</body>
</html>
"""
    
    # Generate table rows for HTML
    table_rows = ""
    for item in sorted_data:
        days_inactive_str = str(item["days_inactive"]) if item["days_inactive"] is not None else "Unknown"
        topic = item["topic"] or "-"
        purpose = item["purpose"] or "-"
        
        table_rows += f"""
            <tr>
                <td>#{item["channel_name"]}</td>
                <td>{days_inactive_str}</td>
                <td>{item["member_count"]}</td>
                <td class="topic">{topic}</td>
                <td class="purpose">{purpose}</td>
            </tr>
        """
    
    # Fill in the HTML template
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        if args.no_interactive:
            days = args.days
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
        print(f"Finding channels inactive for {days} days or more...")
        inactive_channels, activity_map = find_inactive_channels(
            days=days, exclude_channels=exclude_channels
        )

        if inactive_channels:
            print(f"\nFound {len(inactive_channels)} channels inactive for {days} days or more:")
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
            print(f"No channels found that have been inactive for {days} days or more.")
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