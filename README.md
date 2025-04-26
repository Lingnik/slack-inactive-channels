# slack-inactive-channels

A Python utility to identify inactive channels in a Slack workspace. This helps workspace administrators identify and potentially archive unused channels to reduce clutter.

## Features

- Identifies Slack channels that have been inactive for a specified period
- Supports both public and private channels
- Asynchronous API requests for faster performance
- Handles rate limiting with exponential backoff
- Shows channel creation date and member count in results
- Configurable inactivity threshold
- Logs channel access errors to a file (slack_errors.log)

## Slack App Setup

To run this script, you need a user API token. To get a user API token, you need to install a Slack "App" on your
workspace. This requires owner or admin permissions, depending on your workspace.

1. Navigate to <https://api.slack.com/apps/>
2. Click the big green **Create New App** button
3. Click the **_From a manifest_** option
4. Pick the workspace you want to run this script on. If you want to test it on a testing workspace first, do that now, but you'll need to repeat these steps for your production workspace.
5. Click the big green **Next** button.
6. Paste the following code into the form:

    ```json
    {
        "display_information": {
            "name": "slack-inactive-channels"
        },
        "oauth_config": {
            "scopes": {
                "user": [
                    "channels:read",
                    "channels:history",
                    "groups:read",
                    "groups:history"
                ]
            }
        },
        "settings": {
            "org_deploy_enabled": false,
            "socket_mode_enabled": false,
            "token_rotation_enabled": false
        }
    }
    ```

7. Click the big green **Next** button.
8. Click the big green **Create** button.
9. Click the big white **Install to <workspace name>** button.
10. Click the big green **Allow** button. Totally safe. Definitely not giving access to untrusted random code you got on the internet to view all of your Slack workspace's channel history and channel information.
11. Click the **_OAuth &amp; Permissions_** link in the sidebar.
12. Note the **User OAuth Token** starting with `xoxp-` -- you'll need to copy and paste this into an environment variable in your terminal this later. The token will let you run the script on the workspace you installed it on.
13. That's it for the Slack API site. See the [Script Prerequisites](#script-prerequisites) and [Script Usage](#script-usage) sections below for next steps.


## Script Prerequisites

```sh
brew update
brew install pyenv pyenv-virtualenv
export PYENV_ROOT="$HOME/.pyenv"
eval "$(pyenv init -)"
eval "$(pyenv virtualenv-init -)"
# Put the above two lines in your shell RC of choice so that you forget how it works next time you get a new machine.
```


## Script Usage

### Environment Setup

```sh
# Create and activate a virtual environment
pyenv install 3.12.2
pyenv virtualenv 3.12.2 slack-inactive-channels-3.12.2
pyenv activate slack-inactive-channels-3.12.2

# Install required dependencies
pip install -r requirements.txt

# Set your Slack API token
# Tip: In zsh, prefacing a command with a space and having `hist_ignore_space` turned on prevents
# the command from showing up in ~/.zsh_history
 export SLACK_API_TOKEN="xoxp-your-token-here"

# Optional: Set custom default inactivity threshold (default is 90 days)
 export DEFAULT_INACTIVE_DAYS="120"

# Optional: Set excluded channels (comma-separated, in addition to default exclusions)
 export EXCLUDE_CHANNELS="team-announcements,company-news,help-desk"
```

### Running the Script

#### Interactive Mode

```sh
python3 ./list_inactive_channels.py
```

When prompted, enter the number of days to consider a channel inactive, or press Enter to use the default value.

#### Command Line Arguments

The script also supports command line arguments for more flexibility:

```sh
python3 ./list_inactive_channels.py --days 60 --exclude "general,random,announcements" --export inactive-report
```

Available arguments:

- `--days DAYS` - Number of days to consider a channel inactive
- `--exclude CHANNELS` - Comma-separated list of channel names to exclude
- `--export FILENAME` - Export results to CSV, JSON, and HTML files with this base name
- `--archive` - Archive inactive channels (requires confirmation)
- `--no-interactive` - Run in non-interactive mode (requires --days and --export)

#### Automating with Non-Interactive Mode

For scheduled tasks or automated reporting:

```sh
python3 ./list_inactive_channels.py --days 90 --export "inactive-report-$(date +%Y-%m-%d)" --no-interactive
```

#### Archiving Inactive Channels

```sh
python3 ./list_inactive_channels.py --days 120 --archive
```

This will show inactive channels and prompt for confirmation before archiving.

### Example Output

```
Enter number of days to check inactivity (default 90): 60
Finding channels inactive for 60 days or more...
Starting to process 150 channels...
Progress: 5% (8/150 channels processed)
Progress: 10% (15/150 channels processed)
...
Progress: 95% (143/150 channels processed)
Progress: 100% (150/150 channels processed)

Found 15 channels inactive for 60 days or more:
- #archived-projects (created: 2022-01-15, members: 12)
- #company-events (created: 2021-06-22, members: 45)
- #old-team-chat (created: 2020-11-05, members: 8)
...

Do you want to export the results to files? (y/n): y
Enter base filename without extension (default: inactive_channels): 
Exported 15 inactive channels to inactive_channels.csv
Exported complete channel data to inactive_channels.json
Exported HTML report to inactive_channels.html
```

## Export Features

The tool can now export inactive channel data to multiple formats:

- CSV file: Contains key channel information in a tabular format
- JSON file: Contains complete channel data for more detailed analysis
- HTML report: Interactive formatted table with channel details

Exported data is sorted by last activity date (oldest inactive channels first) and includes:
- Channel ID and name
- Creation date
- Member count
- Last activity date
- Days since last activity
- Private channel status
- Channel topic and purpose

### HTML Export

The HTML export creates a nicely formatted report with:
- Report summary (date, inactivity threshold, total channels)
- Interactive table with color coding
- Sortable columns
- Mobile-friendly responsive design
