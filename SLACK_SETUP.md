# Slack Integration Setup

The digest engine can optionally deliver digests as Slack DMs using the Slack Web API.
This is fully optional — the engine runs fine without it.

## Step 1: Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **Create New App** → **From Scratch**
3. Name: "Digest Bot", choose your workspace
4. Click **Create App**

## Step 2: Configure Bot Token Scopes

In your app settings, go to **OAuth & Permissions** → **Scopes** → **Bot Token Scopes**.

Add these scopes:
- `chat:write` — send messages as the bot
- `im:write` — open DM channels with users
- `users:read` — look up user information (optional, for validation)

## Step 3: Install the App

1. Go to **OAuth & Permissions** → **Install to Workspace**
2. Authorise the app
3. Copy the **Bot User OAuth Token** (starts with `xoxb-`)

## Step 4: Find Slack User IDs

The engine uses internal IDs (u_alice, u_bob, etc.) — you need to map these to real Slack member IDs.

To find a Slack user's member ID:
- In Slack, click their profile → **More** → **Copy member ID**
- It looks like `U012AB3CD`

## Step 5: Set Environment Variables

```bash
export SLACK_BOT_TOKEN="xoxb-your-token-here"

# Map engine user IDs to real Slack member IDs (JSON)
export SLACK_USER_MAP='{"u_alice": "U012AB3CD", "u_bob": "U034EF5GH"}'

# Optional: enable dry-run mode (prints payloads without sending)
export SLACK_DRY_RUN=1
```

## Step 6: Run the Delivery Script

```bash
# Dry run (no real messages sent)
python scripts/deliver_digest.py --dry-run

# Deliver for a single user
python scripts/deliver_digest.py --user u_alice

# Deliver for all mapped users
python scripts/deliver_digest.py
```

## Graceful Degradation

If `SLACK_BOT_TOKEN` is not set:
- The engine and demo UI continue to work normally
- `deliver_digest.py` prints payloads in dry-run mode
- No errors or crashes

## User ID Mapping Notes

- Engine user IDs (u_alice, etc.) are internal mock identifiers
- Real Slack deployments would need a persistent mapping table
- For production: store the map in a config file or database
- For demo: the `SLACK_USER_MAP` env var is sufficient

## Block Kit Preview

You can preview the digest format at https://app.slack.com/block-kit-builder
by copying the `blocks` array printed in dry-run mode.
