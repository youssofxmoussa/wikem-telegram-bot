#!/usr/bin/env python3
"""Telegram bot for searching the WikEM APK database and public research pages."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_APK = ROOT / "attached_assets" / "wikem-emergency-medicine_1789935534923.apk"
DEFAULT_DB_ZIP = ROOT / "data" / "Wikem.db.zip"
DB_PATH = ROOT / "data" / "Wikem.db"
TELEGRAM_API = "https://api.telegram.org"
WIKEM_API = "https://www.wikem.org/w/api.php"
WIKEM_SITE = "https://www.wikem.org"
WIKEM_AI_API = "https://wikem.ai/api/ask/stream"
MESSAGE_LIMIT = 3900
SEARCH_LIMIT = 8
HTTP_TIMEOUT = 30

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger("wikem-bot")


class HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP endpoint for Render web-service health checks."""

    def do_GET(self) -> None:
        if self.path not in {"/", "/healthz"}:
            self.send_response(404)
            self.end_headers()
            return
        body = b"WikEM Telegram Bot is running\n"
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


class TextExtractor(HTMLParser):
    """Convert WikEM HTML into readable Telegram text."""

    BLOCK_TAGS = {
        "br",
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
    }
    SKIP_TAGS = {"script", "style", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        if self.skip_depth == 0 and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth == 0 and tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        text = html.unescape("".join(self.parts))
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n[ \t]+", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def clean_html(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    return parser.text()


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


def markdown_v2(text: str) -> str:
    """Convert common Markdown into Telegram MarkdownV2 safely."""
    placeholders: dict[str, str] = {}
    counter = 0

    def keep(value: str) -> str:
        nonlocal counter
        token = f"TGPLACEHOLDER{counter}END"
        counter += 1
        placeholders[token] = value
        return token

    def link_match(match: re.Match[str]) -> str:
        label = re.sub(r"([\\_*[\]()~`>#+\-=|{}.!])", r"\\\1", match.group(1))
        url = match.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return keep(f"[{label}]({url})")

    def bold_match(match: re.Match[str]) -> str:
        value = re.sub(r"([\\_*[\]()~`>#+\-=|{}.!])", r"\\\1", match.group(1))
        return keep(f"*{value}*")

    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", link_match, text)
    text = re.sub(r"\*\*(.+?)\*\*", bold_match, text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", lambda match: keep(f"`{match.group(1).replace('`', '\\`')}`"), text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"([\\_*[\]()~`>#+\-=|{}.!])", r"\\\1", text)
    for token, value in placeholders.items():
        text = text.replace(token, value)
    return text


def pdf_literal(value: str) -> str:
    """Encode a line for a simple Helvetica PDF content stream."""
    value = value.encode("latin-1", "replace").decode("latin-1")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(title: str, sections: list[tuple[str, str]]) -> bytes:
    """Create a dependency-free, readable PDF for Telegram document delivery."""
    lines: list[tuple[str, int]] = []
    for heading, body in sections:
        if heading:
            lines.append((heading, 12))
        for paragraph in (body or "").splitlines() or [""]:
            paragraph = paragraph.strip()
            if not paragraph:
                lines.append(("", 10))
                continue
            while paragraph:
                lines.append((paragraph[:100], 10))
                paragraph = paragraph[100:]
        lines.append(("", 10))

    page_lines: list[list[tuple[str, int]]] = [[]]
    for line in lines:
        if len(page_lines[-1]) >= 48:
            page_lines.append([])
        page_lines[-1].append(line)
    if not page_lines[-1]:
        page_lines[-1].append(("", 10))

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    page_refs = " ".join(f"{4 + index * 2} 0 R" for index in range(len(page_lines)))
    objects.append(f"<< /Type /Pages /Kids [{page_refs}] /Count {len(page_lines)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, page in enumerate(page_lines):
        content_lines = ["BT", "/F1 15 Tf", "50 770 Td", f"({pdf_literal(title)}) Tj", "/F1 10 Tf"]
        for text, size in page:
            content_lines.append(f"/F1 {size} Tf")
            content_lines.append(f"0 -{18 if size == 12 else 14} Td")
            content_lines.append(f"({pdf_literal(text)}) Tj")
        content_lines.append("ET")
        content = "\n".join(content_lines).encode("latin-1", "replace")
        content_object_number = 5 + index * 2
        page_object_number = 4 + index * 2
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> "
                f"/Contents {content_object_number} 0 R >>"
            ).encode()
        )
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = HTTP_TIMEOUT,
) -> dict[str, Any]:
    data = None
    headers = {"User-Agent": "WikEM-Telegram-Bot/1.0"}
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    result = json.loads(raw.decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Remote service returned an unexpected response")
    return result


def multipart_request(
    url: str,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    file_data: bytes,
) -> dict[str, Any]:
    boundary = f"----WikEMBot{int(time.time() * 1000)}"
    body = bytearray()
    for key, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        body.extend(value.encode("utf-8"))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode()
    )
    body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
    body.extend(file_data)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        url,
        data=bytes(body),
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "WikEM-Telegram-Bot/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Telegram returned an unexpected response")
    return result


def locate_apk() -> Path:
    configured = os.getenv("WIKEM_APK_PATH")
    candidates = [Path(configured)] if configured else []
    candidates.extend(
        [
            DEFAULT_APK,
            ROOT / "wikem-emergency-medicine.apk",
            *sorted((ROOT / "attached_assets").glob("*.apk")),
        ]
    )
    for candidate in candidates:
        if candidate and candidate.exists() and candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "No WikEM APK found. Set WIKEM_APK_PATH or attach the WikEM APK to the project."
    )


def prepare_database() -> Path:
    """Extract the bundled database without extracting the whole APK."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists() and DB_PATH.stat().st_size > 1_000_000:
        return DB_PATH
    if DEFAULT_DB_ZIP.exists():
        with zipfile.ZipFile(DEFAULT_DB_ZIP) as archive:
            member = "Wikem.db"
            if member not in archive.namelist():
                raise FileNotFoundError(f"{member} was not found inside {DEFAULT_DB_ZIP.name}")
            temporary = DB_PATH.with_suffix(".tmp")
            with archive.open(member) as source, temporary.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    destination.write(chunk)
            temporary.replace(DB_PATH)
        LOGGER.info("Extracted database from %s (%d bytes)", DEFAULT_DB_ZIP.name, DB_PATH.stat().st_size)
        return DB_PATH
    apk = locate_apk()
    with zipfile.ZipFile(apk) as archive:
        member = "assets/flutter_assets/assets/Wikem.db"
        if member not in archive.namelist():
            raise FileNotFoundError(f"{member} was not found inside {apk.name}")
        temporary = DB_PATH.with_suffix(".tmp")
        with archive.open(member) as source, temporary.open("wb") as destination:
            while chunk := source.read(1024 * 1024):
                destination.write(chunk)
        temporary.replace(DB_PATH)
    LOGGER.info("Extracted database from %s (%d bytes)", apk.name, DB_PATH.stat().st_size)
    return DB_PATH


@dataclass
class SearchResult:
    wikem_id: str
    name: str
    folder: str
    snippet: str


class WikemDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def stats(self) -> tuple[int, int]:
        with self.connect() as db:
            pages = db.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            categories = db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        return int(pages), int(categories)

    def search(self, query: str, limit: int = SEARCH_LIMIT) -> list[SearchResult]:
        query = query.strip()
        if not query:
            return []
        like = f"%{query}%"
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT wikemId, name, folder,
                       substr(replace(replace(content, '<', ' '), '>', ' '), 1, 220) AS snippet
                FROM pages
                WHERE name LIKE ? COLLATE NOCASE
                   OR wikemId LIKE ? COLLATE NOCASE
                   OR content LIKE ? COLLATE NOCASE
                ORDER BY
                    CASE
                      WHEN name LIKE ? COLLATE NOCASE THEN 0
                      WHEN wikemId LIKE ? COLLATE NOCASE THEN 1
                      ELSE 2
                    END,
                    name COLLATE NOCASE
                LIMIT ?
                """,
                (like, like, like, like, like, limit),
            ).fetchall()
        return [
            SearchResult(
                wikem_id=row["wikemId"] or "",
                name=row["name"] or row["wikemId"] or "Untitled",
                folder=row["folder"] or "",
                snippet=clean_html(row["snippet"] or ""),
            )
            for row in rows
        ]

    def get_article(self, identifier: str) -> sqlite3.Row | None:
        identifier = identifier.strip()
        with self.connect() as db:
            row = db.execute(
                """
                SELECT wikemId, name, folder, author, last_update, content
                FROM pages
                WHERE wikemId = ? COLLATE NOCASE
                   OR name = ? COLLATE NOCASE
                LIMIT 1
                """,
                (identifier, identifier),
            ).fetchone()
            if row:
                return row
            row = db.execute(
                """
                SELECT wikemId, name, folder, author, last_update, content
                FROM pages
                WHERE wikemId LIKE ? COLLATE NOCASE
                   OR name LIKE ? COLLATE NOCASE
                ORDER BY name COLLATE NOCASE
                LIMIT 1
                """,
                (f"%{identifier}%", f"%{identifier}%"),
            ).fetchone()
        return row

    def random_articles(self, limit: int = 5) -> list[SearchResult]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT wikemId, name, folder, '' AS snippet
                FROM pages
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            SearchResult(row["wikemId"], row["name"], row["folder"] or "", "")
            for row in rows
        ]


class TelegramBot:
    def __init__(self, token: str, database: WikemDatabase) -> None:
        self.token = token
        self.base_url = f"{TELEGRAM_API}/bot{token}"
        self.database = database
        self.wikem_api = os.getenv("WIKEM_API_URL", WIKEM_API)
        self.allowed_chat_ids = {
            item.strip()
            for item in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
            if item.strip()
        }
        self.offset = 0

    def telegram(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        result = json_request(f"{self.base_url}/{method}", method="POST", payload=payload)
        if result.get("ok") is not True:
            raise RuntimeError(result.get("description", f"Telegram method failed: {method}"))
        return result

    def send_text(self, chat_id: int | str, text: str, parse_mode: str | None = None) -> None:
        for chunk in split_message(text):
            payload: dict[str, Any] = {"chat_id": str(chat_id), "text": chunk}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            self.telegram("sendMessage", payload)

    def send_document(self, chat_id: int | str, data: bytes, filename: str, caption: str) -> None:
        result = multipart_request(
            f"{self.base_url}/sendDocument",
            {"chat_id": str(chat_id), "caption": caption},
            "document",
            filename,
            data,
        )
        if result.get("ok") is not True:
            raise RuntimeError(result.get("description", "Could not send database"))

    def search_online(self, query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, str]]:
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": str(limit),
                "format": "json",
                "utf8": "1",
            }
        )
        result = json_request(f"{self.wikem_api}?{params}")
        return [
            {
                "title": item.get("title", ""),
                "snippet": clean_html(item.get("snippet", "")),
                "pageid": str(item.get("pageid", "")),
            }
            for item in result.get("query", {}).get("search", [])
        ]

    def online_article(self, title: str) -> str:
        params = urllib.parse.urlencode(
            {
                "action": "parse",
                "format": "json",
                "page": title,
                "prop": "text",
                "redirects": "1",
            }
        )
        result = json_request(f"{self.wikem_api}?{params}")
        if "error" in result:
            raise ValueError(result["error"].get("info", "WikEM could not find that page"))
        return clean_html(result.get("parse", {}).get("text", {}).get("*", ""))

    def ask_wikem_ai(self, query: str) -> tuple[str, list[dict[str, Any]], str | None]:
        """Call the official WikEM.ai SSE endpoint and collect its answer."""
        payload = json.dumps({"query": query}).encode("utf-8")
        request = urllib.request.Request(
            os.getenv("WIKEM_AI_API_URL", WIKEM_AI_API),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "WikEM-Telegram-Bot/1.0",
            },
            method="POST",
        )
        answer_parts: list[str] = []
        sources: list[dict[str, Any]] = []
        confidence: str | None = None
        with urllib.request.urlopen(request, timeout=120) as response:
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
            "WikEM research bot\n\n"
            "Local database:\n"
            "/search <term> — search all offline WikEM articles\n"
            "/article <name or wikemId> — read an article\n"
            "/article_pdf <name or wikemId> — download an article as PDF\n"
            "/research <topic> — search local medical research/content\n"
            "/research_pdf <topic> — download an AI research report as PDF\n"
            "/ask <clinical question> — ask official WikEM.ai\n"
            "/stats — database totals\n"
            "/random — random articles\n"
            "The bot sends PDF reports, not the raw SQLite database.\n\n"
            "Online WikEM:\n"
            "/online <topic> — search the live WikEM research/content API\n"
            "/online_article <title> — read a live WikEM page\n\n"
            "/id — show this Telegram chat ID\n"
            "/help — show this help"
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
        argument = argument.strip()
        try:
            if command in {"/start", "/help"}:
                self.send_text(chat_id, self.help_text())
            elif command == "/id":
                self.send_text(chat_id, f"Chat ID: {chat_id}")
            elif command == "/stats":
                pages, categories = self.database.stats()
                self.send_text(
                    chat_id,
                    f"WikEM offline database\nPages: {pages:,}\nCategories: {categories:,}",
                )
            elif command in {"/search", "/research"}:
                self.handle_search(chat_id, argument, online=False)
            elif command in {"/ask", "/ai"}:
                self.handle_ai(chat_id, argument)
            elif command == "/article":
                self.handle_article(chat_id, argument)
            elif command == "/article_pdf":
                self.handle_article_pdf(chat_id, argument)
            elif command == "/research_pdf":
                self.handle_research_pdf(chat_id, argument)
            elif command == "/random":
                results = self.database.random_articles()
                self.send_text(chat_id, self.format_results("Random WikEM articles", results))
            elif command == "/download_db":
                self.send_text(
                    chat_id,
                    "Raw database downloads are disabled. Use /article_pdf or /research_pdf instead.",
                )
            elif command == "/online":
                self.handle_search(chat_id, argument, online=True)
            elif command == "/online_article":
                self.handle_online_article(chat_id, argument)
            else:
                self.send_text(chat_id, "Unknown command. Send /help to see available commands.")
        except (urllib.error.URLError, TimeoutError) as error:
            LOGGER.exception("Network error handling %s", command)
            self.send_text(chat_id, f"Network error: {error.reason if hasattr(error, 'reason') else error}")
        except Exception as error:
            LOGGER.exception("Error handling %s", command)
            self.send_text(chat_id, f"Could not complete that request: {error}")

    def handle_search(self, chat_id: int | str, query: str, *, online: bool) -> None:
        if not query:
            self.send_text(chat_id, "Usage: /online topic" if online else "Usage: /search topic")
            return
        if online:
            results = self.search_online(query)
            if not results:
                self.send_text(chat_id, f"No live WikEM results for: {query}")
                return
            lines = [f"Live WikEM results for: {query}"]
            for index, result in enumerate(results, 1):
                title = result["title"]
                snippet = re.sub(r"\s+", " ", result["snippet"]).strip()
                lines.append(f"\n{index}. {title}\n{snippet}\n{WIKEM_SITE}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}")
            self.send_text(chat_id, "\n".join(lines))
            return
        results = self.database.search(query)
        if not results:
            self.send_text(chat_id, f"No local WikEM results for: {query}")
            return
        title = "Local research results" if query else "Local results"
        self.send_text(chat_id, self.format_results(title, results))

    def format_results(self, title: str, results: Iterable[SearchResult]) -> str:
        lines = [title]
        for index, result in enumerate(results, 1):
            lines.append(f"\n{index}. {result.name}")
            if result.wikem_id:
                lines.append(f"ID: {result.wikem_id}")
            if result.folder:
                lines.append(f"Category: {result.folder}")
            if result.snippet:
                lines.append(result.snippet[:240])
        return "\n".join(lines)

    def handle_article(self, chat_id: int | str, identifier: str) -> None:
        if not identifier:
            self.send_text(chat_id, "Usage: /article <article name or wikemId>")
            return
        row = self.database.get_article(identifier)
        if not row:
            self.send_text(chat_id, f"No local article found for: {identifier}")
            return
        content = clean_html(row["content"] or "")
        header = (
            f"{row['name'] or row['wikemId']}\n"
            f"ID: {row['wikemId']}\n"
            f"Category: {row['folder'] or 'Uncategorized'}\n\n"
        )
        self.send_text(chat_id, header + content)

    def handle_online_article(self, chat_id: int | str, title: str) -> None:
        if not title:
            self.send_text(chat_id, "Usage: /online_article <WikEM page title>")
            return
        content = self.online_article(title)
        if not content:
            self.send_text(chat_id, f"No live content found for: {title}")
            return
        self.send_text(chat_id, f"{title}\n\n{content}")

    def handle_ai(self, chat_id: int | str, query: str) -> None:
        if not query:
            self.send_text(chat_id, "Usage: /ask <clinical question>")
            return
        self.send_text(chat_id, "Asking WikEM.ai…")
        answer, sources, confidence = self.ask_wikem_ai(query)
        lines = [answer]
        if confidence in {"low", "none"}:
            lines.append("\nConfidence: limited matching WikEM content.")
        public_sources: list[str] = []
        for source in sources:
            title = str(source.get("title") or "WikEM source")
            url = str(source.get("url") or "")
            if not url.startswith(("https://", "http://")):
                continue
            if any(private_marker in url for private_marker in ("10.0.0.0/8", "10.", "192.168.", "172.16.")):
                continue
            public_sources.append(f"- {title}: {url}")
        if public_sources:
            lines.append("\nSources:\n" + "\n".join(dict.fromkeys(public_sources)))
        lines.append(
            "\nWikEM.ai answers are for clinical reference; verify against local protocols and professional guidance."
        )
        self.send_text(chat_id, markdown_v2("\n".join(lines)), parse_mode="MarkdownV2")

    def handle_article_pdf(self, chat_id: int | str, identifier: str) -> None:
        if not identifier:
            self.send_text(chat_id, "Usage: /article_pdf <article name or wikemId>")
            return
        row = self.database.get_article(identifier)
        if not row:
            self.send_text(chat_id, f"No local article found for: {identifier}")
            return
        title = row["name"] or row["wikemId"]
        content = clean_html(row["content"] or "")
        data = build_pdf(
            f"WikEM — {title}",
            [
                ("Article", content),
                ("Metadata", f"ID: {row['wikemId']}\nCategory: {row['folder'] or 'Uncategorized'}"),
            ],
        )
        self.send_document(chat_id, data, f"{row['wikemId']}.pdf", f"WikEM article: {title}")

    def handle_research_pdf(self, chat_id: int | str, query: str) -> None:
        if not query:
            self.send_text(chat_id, "Usage: /research_pdf <research topic>")
            return
        self.send_text(chat_id, "Preparing the research PDF…")
        answer, sources, confidence = self.ask_wikem_ai(query)
        local_results = self.database.search(query, limit=6)
        source_text = "\n".join(
            f"{source.get('title', 'WikEM source')}: {source.get('url', '')}"
            for source in sources
            if str(source.get("url", "")).startswith(("https://", "http://"))
            and not str(source.get("url", "")).startswith(("http://10.", "https://10."))
        )
        local_text = "\n".join(
            f"{result.name} ({result.wikem_id})\n{result.snippet}" for result in local_results
        )
        sections = [("AI research answer", answer)]
        if confidence:
            sections.append(("Confidence", confidence))
        if source_text:
            sections.append(("WikEM sources", source_text))
        if local_text:
            sections.append(("Matching offline articles", local_text))
        data = build_pdf(f"WikEM research — {query}", sections)
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", query).strip("_")[:50] or "research"
        self.send_document(chat_id, data, f"{safe_name}.pdf", f"WikEM research report: {query}")

    def run(self) -> None:
        self.telegram("deleteWebhook", {"drop_pending_updates": "false"})
        bot_info = self.telegram("getMe").get("result", {})
        LOGGER.info("Connected as @%s", bot_info.get("username", "unknown"))
        self.telegram(
            "setMyCommands",
            {
                "commands": json.dumps(
                    [
                        {"command": "search", "description": "Search offline WikEM"},
                        {"command": "article", "description": "Read an offline article"},
                        {"command": "article_pdf", "description": "Download article PDF"},
                        {"command": "research", "description": "Search local research"},
                        {"command": "research_pdf", "description": "Download research PDF"},
                        {"command": "ask", "description": "Ask official WikEM.ai"},
                        {"command": "online", "description": "Search live WikEM"},
                        {"command": "online_article", "description": "Read a live page"},
                        {"command": "stats", "description": "Show database totals"},
                        {"command": "help", "description": "Show help"},
                    ]
                )
            },
        )
        LOGGER.info("Bot is polling for updates")
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
        LOGGER.error("TELEGRAM_BOT_TOKEN is not configured in Replit Secrets")
        return 1
    health_server = start_health_server()
    try:
        database = WikemDatabase(prepare_database())
        TelegramBot(token, database).run()
    except Exception:
        LOGGER.exception("Bot startup failed")
        return 1
    finally:
        if health_server is not None:
            health_server.shutdown()


if __name__ == "__main__":
    sys.exit(main())