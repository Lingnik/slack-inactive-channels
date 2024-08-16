# slack-inactive-channels - Print a list of inactive channels in a Slack workspace.

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
                    "channels:history"
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
```sh
pyenv install 3.12.2
pyenv virtualenv 3.12.2 slack-inactive-channels-3.12.2
pyenv activate slack-inactive-channels-3.12.2
pip install -r requirements.txt

# Did you know that if you preface a command with a space in zsh, and have `hist_ignore_space` turned on, then the
# command won't show up in ~/.zsh_history?
 export SLACK_API_TOKEN="xoxp-blah-blah-blah"

python3 ./list_inactive_channels.py
```
