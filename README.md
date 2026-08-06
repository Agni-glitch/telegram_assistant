# Telegram Assistant Bot

A Telegram bot that acts as an AI-assistant-style front desk for a single owner: it receives messages from other users, forwards them to the owner, sends an automatic "message received" reply, and lets the owner reply directly through Telegram (their reply gets routed back to the original sender). Deployed as a Python serverless function on Vercel.

## Features

- Forwards incoming messages from any user to the owner's Telegram account
- Auto-reply to senders, with different copy depending on whether the owner is "available" or "busy" (based on recent activity)
- Owner can reply to a forwarded message and have it routed back to the original sender
- Owner-only commands to toggle availability and enable/disable the bot entirely
- `/faq` and `/contact` commands for senders
- Optional persistent state (owner activity, enabled/disabled flag, sender reply-routing map) via Upstash Redis, so state survives across serverless invocations

## Project structure

```
your-repo/
├── api/
│   └── index.py       # Flask app + Telegram bot logic (Vercel entrypoint)
├── requirements.txt
└── vercel.json
```

## Commands

| Command | Who | Description |
|---|---|---|
| `/start` | Everyone | Welcome message |
| `/help` | Everyone | Lists available commands (owner sees extra commands) |
| `/status` | Everyone | Shows whether the owner is currently available |
| `/faq` | Everyone | Frequently asked questions |
| `/contact` | Everyone | Alternative contact info |
| `/toggle` | Owner only | Manually flips available/busy status |
| `/stop` | Owner only | Disables the bot (non-owner messages are ignored) |
| `/resume` | Owner only | Re-enables the bot |

To reply to a forwarded message, the owner replies (Telegram "reply") to the forwarded message in their own chat with the bot — the bot routes it back to the original sender automatically.

## Environment variables

Set these in **Vercel → Project → Settings → Environment Variables**, and make sure each one is enabled for the **Production** environment (and Preview/Development if you test there) — variables saved only under Development will not be available to the deployed function.

| Variable | Required | Description |
|---|---|---|
| `BOT_TOKEN` | Yes | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `YOUR_USER_ID` | Yes | Your numeric Telegram user ID (e.g. from [@userinfobot](https://t.me/userinfobot)) |
| `UPSTASH_REDIS_REST_URL` | No | Upstash Redis REST URL, for persistent state across invocations |
| `UPSTASH_REDIS_REST_TOKEN` | No | Upstash Redis REST token |

If the Upstash variables are omitted, the bot still runs, but `last_owner_activity`, the enabled/disabled flag, and the reply-routing map are held in memory only and may reset unpredictably between serverless invocations.

**Important:** after adding or changing environment variables in Vercel, you must trigger a new deployment (redeploy or push a commit) — existing deployments do not pick up variable changes retroactively.

## Local setup

```bash
git clone https://github.com/Agni-glitch/telegram_assistant.git
cd your-repo
pip install -r requirements.txt
```

Create a `.env` file locally (not committed) with the same variables listed above, for local testing.

## Deploying to Vercel

1. **Set up Upstash Redis (optional, recommended)**
   Create a free database at [upstash.com](https://upstash.com) and copy the REST URL and REST token from the dashboard.

2. **Push to GitHub**
   ```bash
   git init
   git add .
   git commit -m "Telegram assistant bot"
   git remote add origin https://github.com/Agni-glitch/telegram_assistant.git
   git push -u origin main
   ```

3. **Import into Vercel**
   Go to [vercel.com](https://vercel.com) → Add New Project → import the repo.

4. **Add environment variables** (see table above) under Project Settings, then deploy.

5. **Build & Development Settings** (Project Settings → Build & Development Settings):

   | Setting | Value |
   |---|---|
   | Framework Preset | `Other` |
   | Build Command | leave empty |
   | Output Directory | leave empty |
   | Install Command | leave empty (auto-detected from `requirements.txt`) |
   | Root Directory | `./` |

   `vercel.json` handles the actual build via the `@vercel/python` runtime, so the dashboard build settings don't apply here.

6. **Set the Telegram webhook** once deployed:
   ```bash
   curl -F "url=https://your-project.vercel.app/" https://api.telegram.org/bot<BOT_TOKEN>/setWebhook
   ```
   Replace `<BOT_TOKEN>` with your actual token. A successful response looks like `{"ok":true,"result":true,...}`.

7. **Verify**
   - Visiting `https://your-project.vercel.app/` in a browser should return `Bot running on Vercel Serverless!`
   - Message the bot from a non-owner account — you should receive the auto-reply, and the message should be forwarded to the owner's chat.

## Troubleshooting

- **`FUNCTION_INVOCATION_FAILED` / 500 on every request, including `/favicon.ico`:** check Vercel's Logs tab. `could not import "api/index.py"` at the top of the traceback means the module failed at import time — almost always missing or misconfigured environment variables (see above). A crash referencing a specific handler function instead means the bug is inside request-handling logic, not configuration.
- **`/toggle`, `/stop`, `/resume` behave inconsistently:** confirm `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` are set — without them, state is in-memory only and resets across serverless invocations.
- **Webhook not receiving updates:** re-run the `setWebhook` curl command against your current deployment URL — Vercel's production URL can change if you're pointing at a preview deployment instead of production.

## Notes on Vercel's Python runtime

- The `api/index.py` file exports a Flask app named `app`, which `@vercel/python` auto-detects as a WSGI application.
- `vercel.json` uses the `builds`/`routes` schema to route all paths to `api/index.py`. An equivalent `functions`/`rewrites` schema also works — don't mix both styles in the same file.
- Global Python state (module-level variables) may or may not persist between requests depending on whether Vercel reuses a "warm" instance — don't rely on it for anything that needs to be correct across requests; use Redis (or another external store) instead.