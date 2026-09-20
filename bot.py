#!/usr/bin/env python3
"""AI-only Telegram chat powered by the official WikEM.ai service."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any


TELEGRAM_API = "https://api.telegram.org"
WIKEM_AI_API = "https://wikem.ai/api/ask/stream"
MESSAGE_LIMIT = 3900
AI_TIMEOUT = 120

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("wikem-ai-bot")


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP endpoint for Render web-service health checks."""

    def do_GET(self) -> None:
        if self.path not in {"/", "/healthz"}:
            self.send_response(404)
            self.end_headers()
            return
        body = b"WikEM AI Telegram Bot is running\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_health_server() -> ThreadingHTTPServer | None:
    """Start Render's health endpoint when a PORT environment variable exists."""
    port_value = os.getenv("PORT", "").strip()
    if not port_value:
        return None
    server = ThreadingHTTPServer(("0.0.0.0", int(port_value)), HealthHandler)
    Thread(target=server.serve_forever, name="health-server", daemon=True).start()
    LOGGER.info("Health server listening on port %s", port_value)
    return server


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split long text on paragraph/line boundaries for Telegram."""
    text = text.strip()
    if not text:
        return [""]
    chunks: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def escape_markdown_v2(text: str) -> str:
    """Escape plain text for Telegram MarkdownV2."""
    return re.sub(r"([\\_*[\]()~`>#+\-=|{}.!])", r"\\\1", text)


def _format_inline(text: str, depth: int = 0) -> str:
    """Convert common Markdown inline syntax to Telegram MarkdownV2."""
    if depth > 4:
        return escape_markdown_v2(text)

    placeholders: dict[str, str] = {}

    def keep(value: str) -> str:
        token = f"TGFM{len(placeholders)}END"
        placeholders[token] = value
        return token

    def nested(value: str) -> str:
        return _format_inline(value, depth + 1)

    # Protect code and links before escaping their punctuation.
    text = re.sub(
        r"`([^`\n]+)`",
        lambda match: keep(f"`{match.group(1).replace(chr(92), chr(92) * 2).replace('`', chr(92) + '`')}`"),
        text,
    )

    def custom_emoji(match: re.Match[str]) -> str:
        label = escape_markdown_v2(match.group(1))
        emoji_id = match.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return keep(f"![{label}](tg://emoji?id={emoji_id})")

    text = re.sub(r"!\[([^\]]*)\]\(tg://emoji\?id=([^)]+)\)", custom_emoji, text)

    def link(match: re.Match[str]) -> str:
        label = nested(match.group(1))
        url = match.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return keep(f"[{label}]({url})")

    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", link, text)

    # Convert standard Markdown emphasis into Telegram MarkdownV2 entities.
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: keep(f"*{nested(m.group(1))}*"), text)
    text = re.sub(r"__(.+?)__", lambda m: keep(f"__{nested(m.group(1))}__"), text)
    text = re.sub(r"~~(.+?)~~", lambda m: keep(f"~{nested(m.group(1))}~"), text)
    text = re.sub(r"\|\|(.+?)\|\|", lambda m: keep(f"||{nested(m.group(1))}||"), text)
    text = re.sub(
        r"(?<!\*)\*([^*\n]+)\*(?!\*)",
        lambda m: keep(f"_{nested(m.group(1))}_"),
        text,
    )
    text = re.sub(
        r"(?<!_)_([^_\n]+)_(?!_)",
        lambda m: keep(f"_{nested(m.group(1))}_"),
        text,
    )

    text = escape_markdown_v2(text)
    for token, value in placeholders.items():
        text = text.replace(token, value)
    return text


def markdown_to_telegram(text: str) -> str:
    """Convert AI Markdown into Telegram MarkdownV2 safely."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    in_code = False
    code_language = ""
    code_lines: list[str] = []

    for line in lines:
        fence = re.match(r"^\s*```([\w+-]*)\s*$", line)
        if fence:
            if in_code:
                opening = f"```{code_language}\n" if code_language else "```\n"
                output.append(opening + "\n".join(code_lines) + "\n```")
                in_code = False
                code_language = ""
                code_lines = []
            else:
                in_code = True
                code_language = fence.group(1)
                code_lines = []
            continue

        if in_code:
            code_lines.append(line.replace("```", "``\u200b"))
            continue

        expandable_quote = re.match(r"^\s*\*\*>\s?(.*)$", line)
        if expandable_quote:
            output.append(f"**> {escape_markdown_v2(expandable_quote.group(1))}")
            continue

        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            output.append(f"> {escape_markdown_v2(quote.group(1))}")
            continue

        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            output.append(f"*{_format_inline(heading.group(1))}*")
            continue

        bullet = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if bullet:
            output.append(f"• {_format_inline(bullet.group(1))}")
            continue

        numbered = re.match(r"^\s*(\d+)[.)]\s+(.*)$", line)
        if numbered:
            output.append(f"{numbered.group(1)}\\. {_format_inline(numbered.group(2))}")
            continue

        output.append(_format_inline(line))

    if in_code:
        opening = f"```{code_language}\n" if code_language else "```\n"
        output.append(opening + "\n".join(code_lines) + "\n```")

    return "\n".join(output).strip()


def json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    data = None
    headers = {"User-Agent": "WikEM-AI-Telegram-Bot/2.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Remote service returned an unexpected response")
    return result


class TelegramBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = f"{TELEGRAM_API}/bot{token}"
        self.allowed_chat_ids = {
            item.strip()
            for item in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
            if item.strip()
        }
        self.offset = 0

    def telegram(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = json_request(
            f"{self.base_url}/{method}",
            method="POST",
            payload=payload,
            timeout=AI_TIMEOUT,
        )
        if result.get("ok") is not True:
            raise RuntimeError(result.get("description", f"Telegram method failed: {method}"))
        return result

    def send_text(self, chat_id: int | str, text: str, *, markdown: bool = False) -> None:
        for chunk in split_message(text):
            payload: dict[str, Any] = {"chat_id": str(chat_id), "text": chunk}
            if markdown:
                payload["parse_mode"] = "MarkdownV2"
            try:
                self.telegram("sendMessage", payload)
            except RuntimeError as error:
                if not markdown or "parse" not in str(error).lower():
                    raise
                LOGGER.warning("MarkdownV2 rejected; sending plain-text fallback")
                self.telegram(
                    "sendMessage",
                    {"chat_id": str(chat_id), "text": re.sub(r"\\([\\_*[\]()~`>#+\-=|{}.!])", r"\1", chunk)},
                )

    def ask_wikem_ai(self, query: str) -> tuple[str, list[dict[str, Any]], str | None]:
        """Call WikEM.ai's SSE endpoint and collect its answer."""
        payload = json.dumps({"query": query}).encode("utf-8")
        request = urllib.request.Request(
            os.getenv("WIKEM_AI_API_URL", WIKEM_AI_API),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "WikEM-AI-Telegram-Bot/2.0",
            },
            method="POST",
        )
        answer_parts: list[str] = []
        sources: list[dict[str, Any]] = []
        confidence: str | None = None
        with urllib.request.urlopen(request, timeout=AI_TIMEOUT) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                content = event.get("content")
                if event_type == "token" and isinstance(content, str):
                    answer_parts.append(content)
                elif event_type == "sources" and isinstance(content, list):
                    sources = [item for item in content if isinstance(item, dict)]
                elif event_type == "confidence" and isinstance(content, str):
                    confidence = content
                elif event_type == "error":
                    raise RuntimeError(str(content or "WikEM.ai returned an error"))
        answer = "".join(answer_parts).strip()
        if not answer:
            raise RuntimeError("WikEM.ai returned no answer")
        return answer, sources, confidence

    def authorized(self, chat_id: int | str) -> bool:
        return not self.allowed_chat_ids or str(chat_id) in self.allowed_chat_ids

    def help_text(self) -> str:
        return (
            "WikEM.ai chat\n\n"
            "Send any clinical question as a normal message.\n"
            "You can also use /ask <question>.\n\n"
            "Answers are for clinical reference. Verify them against local protocols "
            "and professional guidance."
        )

    def ai_response(self, answer: str, sources: list[dict[str, Any]], confidence: str | None) -> str:
        lines = [answer]
        if confidence in {"low", "none"}:
            lines.append("\n\n**Confidence:** limited matching WikEM content.")

        public_sources: list[str] = []
        for source in sources:
            title = str(source.get("title") or "WikEM source")
            url = str(source.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            if any(private_marker in url for private_marker in ("10.0.0.0/8", "10.", "192.168.", "172.16.")):
                continue
            public_sources.append(f"- [{title}]({url})")
        if public_sources:
            lines.append("\n\n**Sources:**\n" + "\n".join(dict.fromkeys(public_sources)))
        lines.append(
            "\n\n_WikEM.ai answers are for clinical reference; verify against local protocols and professional guidance._"
        )
        return "\n".join(lines)

    def handle_ai(self, chat_id: int | str, query: str) -> None:
        if not query:
            self.send_text(chat_id, markdown_to_telegram(self.help_text()), markdown=True)
            return
        self.telegram("sendChatAction", {"chat_id": str(chat_id), "action": "typing"})
        answer, sources, confidence = self.ask_wikem_ai(query)
        self.send_text(
            chat_id,
            markdown_to_telegram(self.ai_response(answer, sources, confidence)),
            markdown=True,
        )

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None or not self.authorized(chat_id):
            LOGGER.warning("Ignored message from unauthorized chat %s", chat_id)
            return
        text = (message.get("text") or "").strip()
        if not text:
            return

        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        query = argument.strip() if command in {"/ask", "/ai"} else text
        if command in {"/start", "/help"}:
            self.send_text(chat_id, markdown_to_telegram(self.help_text()), markdown=True)
            return

        try:
            self.handle_ai(chat_id, query)
        except (urllib.error.URLError, TimeoutError) as error:
            LOGGER.exception("WikEM.ai network error")
            self.send_text(
                chat_id,
                markdown_to_telegram(
                    f"**WikEM.ai network error:** {str(getattr(error, 'reason', error))}"
                ),
                markdown=True,
            )
        except Exception as error:
            LOGGER.exception("AI request failed")
            self.send_text(
                chat_id,
                markdown_to_telegram(f"**Could not complete the AI request:** {error}"),
                markdown=True,
            )

    def run(self) -> None:
        self.telegram("deleteWebhook", {"drop_pending_updates": "false"})
        bot_info = self.telegram("getMe").get("result", {})
        LOGGER.info("Connected as @%s", bot_info.get("username", "unknown"))
        self.telegram(
            "setMyCommands",
            {
                "commands": json.dumps(
                    [
                        {"command": "ask", "description": "Ask WikEM.ai"},
                        {"command": "help", "description": "Show AI chat help"},
                    ]
                )
            },
        )
        LOGGER.info("AI chat is polling for updates")
        while True:
            try:
                result = self.telegram(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": 25,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                for update in result.get("result", []):
                    self.offset = max(self.offset, int(update["update_id"]) + 1)
                    if "message" in update:
                        self.handle_message(update["message"])
            except KeyboardInterrupt:
                LOGGER.info("Bot stopped")
                return
            except Exception:
                LOGGER.exception("Polling error; retrying in 5 seconds")
                time.sleep(5)


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        LOGGER.error("TELEGRAM_BOT_TOKEN is not configured")
        return 1
    health_server = start_health_server()
    try:
        TelegramBot(token).run()
    except Exception:
        LOGGER.exception("Bot startup failed")
        return 1
    finally:
        if health_server is not None:
            health_server.shutdown()


if __name__ == "__main__":
    sys.exit(main())