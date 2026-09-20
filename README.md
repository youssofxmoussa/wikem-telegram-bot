# WikEM.ai Telegram Chat

This bot is an AI-only Telegram chat powered by the official WikEM.ai
streaming service.

## Chat

- Send any clinical question as a normal Telegram message.
- `/ask <question>` sends the question to WikEM.ai.
- `/help` shows the chat instructions.

AI responses are converted to Telegram MarkdownV2, including supported bold,
italic, underline, strikethrough, spoiler, code, links, quotes, and lists.
Messages that Telegram cannot parse are sent automatically as plain text.

The bot reads `TELEGRAM_BOT_TOKEN` from the environment and never stores the
token in source code.

## Optional environment variables

- `WIKEM_AI_API_URL` — override the WikEM.ai streaming endpoint
- `ALLOWED_CHAT_IDS` — comma-separated Telegram chat IDs to restrict access
- `LOG_LEVEL` — Python logging level