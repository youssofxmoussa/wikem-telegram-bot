# WikEM Telegram Research Bot

This bot searches the WikEM SQLite database embedded in the uploaded APK and
can also search/read live WikEM pages through the public MediaWiki API.

## Commands

- `/search <term>` — search the offline database
- `/article <name or wikemId>` — read an offline article
- `/article_pdf <name or wikemId>` — download one article as a PDF
- `/research <topic>` — search local medical content/research
- `/research_pdf <topic>` — download an AI research report as a PDF
- `/ask <clinical question>` — ask the official WikEM.ai service
- `/online <topic>` — search live WikEM
- `/online_article <title>` — read a live WikEM page
- `/stats` — show page/category totals
- `/random` — show random local articles
- Raw SQLite database downloads are disabled; the bot sends PDF reports instead.
- `/id` — show the current Telegram chat ID
- `/help` — show commands

The bot reads `TELEGRAM_BOT_TOKEN` from Replit Secrets. It never stores the
token in source code. The APK path can be overridden with `WIKEM_APK_PATH`.

Optional environment variables:

- `WIKEM_APK_PATH`
- `WIKEM_API_URL`
- `ALLOWED_CHAT_IDS` — comma-separated Telegram chat IDs to restrict access
- `LOG_LEVEL`