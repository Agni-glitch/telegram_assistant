# Telegram Assistant Bot

A personal Telegram assistant for one owner, hosted on Vercel, **with no database**.

- **People message the bot** → it replies on your behalf with an AI model (or a hard-coded message when the AI is off or fails).
- **You get a notification** for every message: who sent it (name, username, ID), when, what they said, and what the bot replied.
- **Reply to a notification** → your reply (text, photo, voice, anything) goes to that person.
- **Reply with `/aireply <instructions>`** → the AI writes and sends the reply for you, e.g. `/aireply say I'll call tomorrow evening`.
- **Message the bot yourself** → you chat with the AI model directly.

## Why no database is needed

| What needs remembering | How it works without a DB |
|---|---|
| Who to send your reply to | The sender's ID is written in each notification (`🆔 ID: …`). Replying to it tells the bot who to send to. |
| Assistant mode (`ai` / `away` / `available`) | Stored as a **pinned message** in your chat with the bot. Don't unpin it. |
| Conversation context | None is stored. Each message is answered on its own, plus the message it replies to (if any). |

## Modes

| Command | What people get |
|---|---|
| `/ai` | An AI reply on your behalf. If every model fails, they get the hard-coded away message. |
| `/away` | The hard-coded "unavailable" message only. |
| `/available` | A short "you've been notified" acknowledgement. You reply yourself. |

`DEFAULT_MODE` applies until you pick one. `/status` shows the current mode and models.

## Adding models

Set `AI_MODELS` to a comma-separated list of `provider:model` entries. They are tried in order, and the next one is used if one fails:

```
AI_MODELS=anthropic:claude-opus-5-5,groq:llama-3.3-70b-versatile,gemini:gemini-2.5-flash
```

Built-in providers, each with its own key variable: `anthropic` (`ANTHROPIC_API_KEY`), `openai`, `groq`, `gemini`, `openrouter`, `deepseek`, `mistral`, `together` (`<PROVIDER>_API_KEY`).

**Any other OpenAI-compatible provider** works with no code change. Pick a name, then set `<NAME>_BASE_URL` and `<NAME>_API_KEY`:

```
AI_MODELS=ollama:llama3
OLLAMA_BASE_URL=https://my-ollama.example.com/v1
```

To use different models for your own chat than for replying to others, set `OWNER_AI_MODELS`.

### Switching models from Telegram (owner only)

You can change models from your chat with the bot, with no redeploy. These are the commands, and anyone else who sends them is ignored:

| Command | Effect |
|---|---|
| `/model` | Show the models in use |
| `/model groq:llama-3.3-70b-versatile` | Use this for both replies to others and your chat |
| `/model others anthropic:claude-haiku-5-5` | Only for replies to others |
| `/model me gemini:gemini-2.5-flash,groq:llama-3.3-70b-versatile` | Only for your chat (with a fallback) |
| `/model reset` | Back to `AI_MODELS` / `OWNER_AI_MODELS` from Vercel |

The provider's API key must already be set in Vercel. The bot refuses a model whose key is missing. The choice is saved in the same pinned settings message as the mode. Cheaper Claude option: `anthropic:claude-haiku-5-5`.

`OWNER_ABOUT` is what the AI is allowed to tell people about you (work, response times, links). The AI is told never to invent facts or make commitments for you.

## Environment variables

See [.env.example](.env.example) for the full list. Required: `BOT_TOKEN`, `YOUR_USER_ID`, `WEBHOOK_SECRET`, plus at least one model and its API key if you want AI replies.

In Vercel, enable each variable for **Production**. **Redeploy after changing variables.**

## Deploy to Vercel

1. Push this repo to GitHub, then import it in Vercel (Framework Preset: `Other`, leave build settings empty).
2. Add the environment variables, then deploy.
3. Open `https://<your-project>.vercel.app/setup?key=<WEBHOOK_SECRET>` once. This sets the Telegram webhook (with the secret) and the bot's command menu.
4. Send `/start` to the bot from your account, then `/ai` (or `/away` / `/available`) to set the mode.
5. Message the bot from another account to test.

## Project structure

```
api/index.py     Flask webhook + Telegram handlers (Vercel entrypoint)
api/llm.py       Pluggable model providers with fallback
requirements.txt
vercel.json      Routes everything to api/index.py, 60s max duration
```

## Troubleshooting

- **Notifications arrive but people get the hard-coded message in `/ai` mode:** the notification shows `hard-coded, AI failed: …` with the reason, usually a missing key or a wrong model ID.
- **403 on webhook calls:** `WEBHOOK_SECRET` changed. Open `/setup?key=…` again.
- **Mode resets:** the pinned "Assistant mode" message was unpinned. Run `/ai`, `/away` or `/available` again.
- **`could not import "api/index.py"` in Vercel logs:** `BOT_TOKEN` or `YOUR_USER_ID` is missing for that environment.
