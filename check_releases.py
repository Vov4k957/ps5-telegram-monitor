#!/usr/bin/env python3
"""Проверяет опубликованные GitHub Releases и сообщает о новых в Telegram.

Использует только стандартную библиотеку Python. История хранится в
release_state.json, который GitHub Actions коммитит в репозиторий.
"""

import html
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

DEFAULT_REPOS = [
    "itsPLK/ps5-y2jb-autoloader",
    "EchoStretch/kstuff-lite",
    "drakmor/ShadowMountPlus",
    "itsPLK/ps5-payload-manager",
    "quer3q/ps5-dump-forge",
    "mihawk-99/PS5_MihawkOS",
]

STATE_FILE = Path("release_state.json")
MAX_SUBSCRIPTIONS = 30
GITHUB_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9.-])(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
RELEASE_LIMIT = 30
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.getenv("GH_TOKEN", "").strip()
CLOUDFLARE_SYNC_ENABLED = os.getenv("CLOUDFLARE_SYNC_ENABLED", "").strip().lower() == "true"
CLOUDFLARE_SYNC_URL = os.getenv("CLOUDFLARE_SYNC_URL", "").strip()
SYNC_SECRET = os.getenv("SYNC_SECRET", "").strip()


class APIError(RuntimeError):
    """Keep a structured error for decisions, without logging URLs or tokens."""
    def __init__(self, code, description="", headers=None):
        super().__init__(f"HTTP {code} при обращении к API")
        self.code = code
        self.description = description
        self.headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}


class GitHubRateLimited(RuntimeError):
    """Stop the current scan and defer requests across scheduled runs."""

    def __init__(self, retry_at):
        super().__init__("GitHub API rate limit; retry deferred")
        self.retry_at = int(retry_at)


def is_github_rate_limited(error):
    """Distinguish quota errors from ordinary 403 permission denials."""
    if not isinstance(error, APIError) or error.code not in (403, 429):
        return False
    message = error.description.lower()
    return (error.code == 429 or error.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in message or "rate-limit" in message
            or bool(error.headers.get("retry-after")))


def github_retry_at(error, now=None):
    """Honor GitHub response headers, with >=60s fallback for secondary limits."""
    now = time.time() if now is None else now
    retry_after = error.headers.get("retry-after", "")
    try:
        if retry_after:
            return int(now + max(1.0, float(retry_after))) + 1
    except (ValueError, TypeError, OverflowError):
        pass
    if error.headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(int(now) + 1, int(float(error.headers["x-ratelimit-reset"])) + 2)
        except (KeyError, ValueError, TypeError, OverflowError):
            pass
    return int(now) + 60


def github_request_json(url, headers=None):
    """One short retry for transient GitHub failures; never retry rate limits early."""
    for attempt in range(2):
        try:
            return request_json(url, headers=headers)
        except APIError as error:
            if is_github_rate_limited(error):
                raise GitHubRateLimited(github_retry_at(error)) from None
            if attempt or error.code not in (500, 502, 503, 504):
                raise
        except RuntimeError:
            if attempt:
                raise
        time.sleep(1)
    raise RuntimeError("GitHub retry was not successful")


def request_json(url, payload=None, headers=None, timeout=25):
    common_headers = {"User-Agent": "ps5-github-telegram-notifier/1.0"}
    if headers:
        common_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        common_headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(url, data=data, headers=common_headers)
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        # Не выводим в лог URL Telegram с токеном.
        try:
            details = json.loads(error.read())
            description = details.get("description") or details.get("message") or ""
        except (ValueError, AttributeError):
            description = ""
        raise APIError(error.code, description, error.headers) from None
    except URLError as error:
        raise RuntimeError(f"Не удалось подключиться к API: {error.reason}") from None
    except TimeoutError:
        raise RuntimeError("Превышено время ожидания API") from None


def fetch_releases(repo):
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    url = f"https://api.github.com/repos/{repo}/releases?per_page={RELEASE_LIMIT}"
    items = github_request_json(url, headers=headers)
    if not isinstance(items, list):
        raise RuntimeError(f"Неожиданный ответ GitHub для {repo}")
    return [item for item in items if not item.get("draft")]


def resolve_github_repo(repo):
    """Validate a real public repository and return its canonical full_name."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    result = github_request_json(f"https://api.github.com/repos/{repo}", headers=headers)
    if result.get("private") or not result.get("full_name"):
        raise RuntimeError("Репозиторий недоступен для публичного отслеживания")
    return result["full_name"]


def extract_github_links(text):
    """Receive repo URLs, optionally followed by /releases, /tree, etc."""
    seen = set()
    repos = []
    excluded = {"orgs", "topics", "settings", "explore", "notifications", "marketplace", "login", "search"}
    for match in GITHUB_LINK_RE.finditer(text):
        owner, name = match.groups()
        name = name.rstrip(".,;:")
        if name.lower().endswith(".git"):
            name = name[:-4]
        if owner.lower() in excluded or name in {".", ".."} or not name:
            continue
        repo = f"{owner}/{name}"
        if repo.casefold() not in seen:
            seen.add(repo.casefold())
            repos.append(repo)
    return repos


def configure_commands_menu():
    """Force the command menu for the authorized private chat."""
    commands = [
        {"command": "start", "description": "Главное меню"},
        {"command": "list", "description": "Мои подписки GitHub"},
        {"command": "remove", "description": "Удалить подписки кнопками"},
        {"command": "translate_on", "description": "Включить русский перевод"},
        {"command": "translate_off", "description": "Выключить перевод"},
        {"command": "status", "description": "Состояние мониторинга"},
        {"command": "check", "description": "Проверить обновления"},
        {"command": "help", "description": "Список команд и помощь"},
    ]
    api = f"https://api.telegram.org/bot{TOKEN}/"
    result = request_json(api + "setMyCommands", payload={
        "commands": commands, "scope": {"type": "chat", "chat_id": int(CHAT_ID)}
    })
    if not result.get("ok"):
        raise RuntimeError("Telegram отказал в настройке списка команд")
    result = request_json(api + "setChatMenuButton", payload={
        "chat_id": int(CHAT_ID), "menu_button": {"type": "commands"}
    })
    if not result.get("ok"):
        raise RuntimeError("Telegram отказал в настройке кнопки меню")
    print("Telegram: меню команд установлено для личного чата")


def tg_updates(offset):
    """Poll messages every GitHub Actions run (no server or webhook)."""
    payload = {"timeout": 0, "limit": 100, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = int(offset) + 1
    data = request_json(f"https://api.telegram.org/bot{TOKEN}/getUpdates", payload=payload)
    if not data.get("ok") or not isinstance(data.get("result"), list):
        raise RuntimeError("Не удалось получить сообщения Telegram")
    return sorted(data["result"], key=lambda x: x.get("update_id", 0))


def safe_repo_name(input_text, subscribed):
    """Find a repository from full name, URL, or uniquely matching short name."""
    item = input_text.strip().strip("` ")
    links = extract_github_links(item)
    if links:
        item = links[0]
    matches = [repo for repo in subscribed if repo.casefold() == item.casefold()]
    if not matches and "/" not in item:
        matches = [repo for repo in subscribed if repo.split("/", 1)[1].casefold() == item.casefold()]
    return matches[0] if len(matches) == 1 else None


def list_message(repos, translated):
    if not repos:
        return "📭 <b>Подписок пока нет.</b> Пришли ссылку на GitHub-репозиторий, чтобы подписаться."
    entries = "\n".join(
        f'{i}. <a href="https://github.com/{html.escape(repo, quote=True)}">{html.escape(repo)}</a>'
        for i, repo in enumerate(repos, 1)
    )
    return (f"📋 <b>Подписки ({len(repos)}):</b>\n{entries}\n\n"
            f"🌐 Перевод: {'включён' if translated else 'выключен'}")


HELP_MESSAGE = (
    "🤖 <b>PS5 GitHub Monitor</b>\n"
    "Пришли ссылку на GitHub-репозиторий — подпишусь на его новые Releases.\n\n"
    "<b>Команды:</b>\n"
    "/list — список подписок\n"
    "/remove — удалить подписки кнопками\n"
    "/translate_on — включить перевод\n"
    "/translate_off — выключить перевод\n"
    "/status — настройки и состояние\n"
    "/check — проверить обновления\n"
    "/help — эта справка\n\n"
    "⏱ Ответы поступают при очередном запуске GitHub Actions (около 5 минут, возможны задержки)."
)


def removal_buttons(subscriptions):
    """An inline keyboard whose entries stay current after every deletion."""
    keyboard = [[{"text": repo, "callback_data": "rm:" + hashlib.sha256(
        repo.casefold().encode("utf-8")).hexdigest()[:16]}]
                for repo in sorted(subscriptions, key=str.casefold)]
    keyboard.append([{"text": "✅ Закрыть меню", "callback_data": "rm:close"}])
    return {"inline_keyboard": keyboard}


def removal_text(subscriptions, deleted=None):
    note = f"🗑 Удалена подписка: <b>{html.escape(deleted)}</b>\n\n" if deleted else ""
    if not subscriptions:
        return note + "📭 Подписок больше нет. Пришли ссылку GitHub для новой подписки."
    return (note + f"<b>Активные подписки ({len(subscriptions)}):</b>\n"
            "Нажми на название, чтобы удалить. После удаления можно выбрать следующую.")


def edit_telegram_message(payload):
    try:
        result = request_json(f"https://api.telegram.org/bot{TOKEN}/editMessageText", payload=payload)
    except APIError as error:
        if error.code == 400 and "message is not modified" in error.description.lower():
            return  # A retry after an already successful edit is harmless.
        raise
    if not result.get("ok"):
        if "message is not modified" not in str(result.get("description", "")).lower():
            raise RuntimeError("Не удалось обновить меню Telegram")


def tg_callback(query, state):
    """Delete exactly the selected subscription, edit the same Telegram message."""
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    qid = query.get("id")
    if not qid:
        return False
    def answer(text):
        # A callback acknowledgement can expire before the next scheduled run.
        # It is only a popup: its failure must not undo a successful menu edit.
        try:
            result = request_json(
                f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                payload={"callback_query_id": qid, "text": text})
            if not result.get("ok"):
                raise RuntimeError("Telegram не подтвердил нажатие кнопки")
        except RuntimeError:
            print("Telegram: всплывающее подтверждение кнопки недоступно")

    if (str(sender.get("id")) != CHAT_ID or str(chat.get("id")) != CHAT_ID
            or chat.get("type") != "private" or not message.get("message_id")):
        answer("Нет доступа")
        return False
    data = query.get("data", "")
    if data == "rm:close":
        edit_telegram_message({
            "chat_id": CHAT_ID, "message_id": message["message_id"],
            "text": "✅ Меню удаления закрыто. Остальные подписки сохранены.",
            "reply_markup": {"inline_keyboard": []}})
        answer("Меню закрыто")
        return False
    if not re.fullmatch(r"rm:[0-9a-f]{16}", data):
        answer("Неизвестная кнопка")
        return False

    subs = state["repositories"]
    repo = next((name for name in subs if "rm:" + hashlib.sha256(
        name.casefold().encode("utf-8")).hexdigest()[:16] == data), None)
    if repo is None:
        answer("Эта подписка уже удалена")
        return False

    # Only change state after the menu edit succeeds, so a failed request is retryable.
    remaining = {name: history for name, history in subs.items() if name != repo}
    edit_telegram_message({
        "chat_id": CHAT_ID, "message_id": message["message_id"],
        "text": removal_text(remaining, deleted=repo), "parse_mode": "HTML",
        "reply_markup": removal_buttons(remaining) if remaining else {"inline_keyboard": []}})
    del subs[repo]
    answer("Подписка удалена")
    return True


def process_update(update, state):
    """Only one authorized private chat can change the settings."""
    if "callback_query" in update:
        return tg_callback(update["callback_query"], state)
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    sender = msg.get("from") or {}
    if (str(chat.get("id")) != CHAT_ID or str(sender.get("id")) != CHAT_ID
            or chat.get("type") != "private"):
        return False
    text = (msg.get("text") or "").strip()
    if not text:
        return False
    subs = state["repositories"]
    cmd, _, args = text.partition(" ")
    cmd = cmd.split("@", 1)[0].lower()
    args = args.strip()
    if cmd in {"/start", "/help"}:
        send_telegram(HELP_MESSAGE)
        return False
    if cmd == "/list":
        send_telegram(list_message(subs, state["translate_enabled"]))
        return False
    if cmd == "/status":
        send_telegram(status_message(state))
        return False
    if cmd in {"/translate_on", "/translate_off"}:
        desired = (cmd == "/translate_on")
        changed = state["translate_enabled"] != desired
        send_telegram("🇷🇺 Перевод описаний " + ("включён ✅" if desired else "выключен ✅"))
        state["translate_enabled"] = desired
        return changed
    if cmd == "/remove":
        if not subs:
            send_telegram("📭 Активных подписок нет. Пришли ссылку на репозиторий GitHub.")
            return False
        send_telegram(removal_text(subs), reply_markup=removal_buttons(subs))
        return False
    if cmd == "/check":
        # The main loop will perform an actual scan right after processing commands.
        state["manual_check_requested"] = True
        return True
    if cmd.startswith("/"):
        send_telegram("Неизвестная команда. Отправь /help или ссылку GitHub.")
        return False

    links = extract_github_links(text)
    if not links:
        send_telegram("Пришли ссылку вида https://github.com/owner/repo или отправь /help.")
        return False
    changed = False
    for link in links[:10]:
        if len(subs) >= MAX_SUBSCRIPTIONS:
            send_telegram(f"⚠️ Достигнут лимит {MAX_SUBSCRIPTIONS} подписок. Сначала используй /remove.")
            break
        try:
            canonical = resolve_github_repo(link)
            if any(repo.casefold() == canonical.casefold() for repo in subs):
                send_telegram(f"ℹ️ На <b>{html.escape(canonical)}</b> ты уже подписан.")
                continue
            releases = fetch_releases(canonical)
        except Exception as e:
            print("Ошибка проверки добавляемого репозитория:", type(e).__name__)
            send_telegram(f"❌ Не удалось добавить <b>{html.escape(link)}</b>. Проверь ссылку и доступность репозитория.")
            continue
        # Baseline: historical releases are not resent after subscribing.
        subs[canonical] = [str(release["id"]) for release in releases]
        changed = True
        send_telegram(
            f"✅ Подписался на <b>{html.escape(canonical)}</b>!\n"
            f"🔗 https://github.com/{html.escape(canonical)}\n"
            "Текущие релизы запомнил; уведомлять буду о следующих."
        )
    return changed


def send_telegram(message, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = request_json(url, payload=payload)
    if not result.get("ok"):
        raise RuntimeError("Telegram не принял сообщение")


def moscow_time(value):
    try:
        return datetime.fromisoformat(value).astimezone(
            timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M:%S МСК")
    except (ValueError, TypeError):
        return "ещё не записано"


def status_message(state):
    last = state.get("last_run") or {}
    event = os.getenv("GITHUB_EVENT_NAME", "local")
    gap = ""
    if last.get("started_at"):
        try:
            minutes = int((datetime.now(timezone.utc) - datetime.fromisoformat(last["started_at"])).total_seconds() / 60)
            gap = f"\n↔️ С предыдущего запуска: {max(0, minutes)} мин."
        except (ValueError, TypeError):
            pass
    return (
        "📊 <b>Бот обрабатывает команды</b>\n"
        f"📦 Подписок: {len(state['repositories'])}\n"
        f"🌐 Перевод: {'включён' if state['translate_enabled'] else 'выключен'}\n"
        "⏱ Расписание задано каждые 5 минут; GitHub может задерживать запуски.\n"
        f"▶️ Текущий запуск: {html.escape(event)}\n"
        f"🕒 Предыдущая проверка: {moscow_time(last.get('completed_at'))}{gap}\n"
        f"📅 Последний запуск schedule: {moscow_time(state.get('last_schedule_at'))}\n"
        f"⚠️ Ошибок в предыдущем запуске: {len(last.get('errors', []))}\n"
        "🔔 Новые Releases, включая Alpha, Beta и Pre-release"
    )



# MyMemory: бесплатный перевод без ключа (анонимно до 5000 символов/день).
# Ограничение сервиса: до 500 байт на один фрагмент.
TRANSLATION_MAX_BYTES = 400
CHANGELOG_MAX_CHARS = 2600
TRANSLATION_BUDGET_SECONDS = 30
PROTECT_RE = re.compile(
    r"!?\[[^\]\n]+\]\(https?://[^\s)]+\)|https?://[^\s)]+|`[^`\n]+`|@[A-Za-z0-9_-]+|"
    r"\bv?\d+\.\d+(?:\.\d+)*(?:-[A-Za-z0-9.]+)?\b|"
    r"(?<![\w])(?:ShadowMountPlus|ps5-y2jb-autoloader|"
    r"kstuff-lite|MihawkOS|WebKit|GitHub|ps5-elfldr|ps5-kexp|ps5-unified-autoloader|"
    r"FPKG|PKG|ELF|HV|Y2JB|SMB2|SMB3|SMB|PS5|PS4|"
    r"Proton|Linux|DXVK|Vulkan|Alpha|Beta|Relapse|FW|elfldr|socket|sockets)(?![\w])",
    re.IGNORECASE,
)



# Curated terminology for frequently-used technical release notes.
# Exact patterns are intentional: machine translation remains the fallback.
RELEASE_LINE_TRANSLATIONS = {
    "## Changelog:": "## Список изменений:",
    "## Changelog": "## Список изменений",
    "## Bundled Binaries": "## Включённые бинарные компоненты",
    "- Relapse: Switched to @notmaj0r's port":
        "- Relapse: переход на версию, портированную @notmaj0r",
    "- Autoloader: Fix socket readiness polling for elfldr, eliminating the 1-second startup delay":
        "- Автозагрузчик: исправлена проверка готовности сокета elfldr, устранена задержка запуска на 1 секунду",
    "- Performance: Removed network log server check":
        "- Производительность: убрана проверка сервера сетевого логирования",
    "- Updater optimizations": "- Оптимизирован механизм обновления",
    "This build integrates the following external dependency versions:":
        "В сборку включены следующие версии внешних компонентов:",
}
FULL_CHANGELOG_RE = re.compile(
    r"^(\*\*?)Full Changelog \(since last stable release\)(\*\*?):\s*(https?://\S+)$",
    re.IGNORECASE,
)


def normalize_release_markdown(text):
    """Convert GitHub's HTML disclosure into readable Markdown, retaining assets."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # <summary> renders in GitHub but not in Telegram.
    text = re.sub(
        r"<summary\b[^>]*>(.*?)</summary\s*>",
        lambda m: "\n## " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n",
        text, flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"</?details\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Preserve links from HTML media tags instead of dropping them.
    text = re.sub(
        r"<(?:video|source|img)\b[^>]*\bsrc=['\"](https?://[^'\"]+)['\"][^>]*>",
        lambda m: "\n" + html.unescape(m.group(1)) + "\n",
        text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"<a\b[^>]*\bhref=['\"](https?://[^'\"]+)['\"][^>]*>(.*?)</a\s*>",
        lambda m: "[" + re.sub(r"<[^>]*>", "", m.group(2)) + "](" +
                  html.unescape(m.group(1)) + ")",
        text, flags=re.IGNORECASE | re.DOTALL,
    )
    # Never forward arbitrary unsupported HTML tags to Telegram.
    text = re.sub(r"</?[A-Za-z][^>\n]*>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def trim_release_markdown(text, limit=CHANGELOG_MAX_CHARS):
    """Truncate at line boundaries and retain GitHub attachment links."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n")
    if boundary >= limit // 2:
        cut = cut[:boundary]
    # Include assets even when a long changelog would otherwise hide them.
    assets = re.findall(
        r"https://github\.com/user-attachments/assets/[A-Za-z0-9-]+",
        text,
    )
    remaining = [url for url in dict.fromkeys(assets) if url not in cut]
    return cut.rstrip() + "\n\n… Описание сокращено.\n" + "\n".join(remaining)


def telegram_inline_markdown(line):
    """Render a safe subset of GitHub Markdown as Telegram HTML."""
    slots = []

    def protect(markup):
        marker = "\uE000" + str(len(slots)) + "\uE001"
        slots.append(markup)
        return marker

    def link(match):
        url = match.group(3)
        if not url.startswith(("https://", "http://")):
            return html.escape(match.group(0))
        label = match.group(2)
        if match.group(1):
            label = "📎 " + (label if label else "Изображение")
        return protect(
            '<a href="' + html.escape(url, quote=True) + '">' +
            html.escape(label) + '</a>'
        )

    line = re.sub(
        r"(!?)\[([^\]\n]*)\]\((https?://[^\s)]+)\)", link, line,
    )
    line = re.sub(
        r"(?<!\x60)\x60([^\x60\n]+)\x60(?!\x60)",
        lambda m: protect("<code>" + html.escape(m.group(1)) + "</code>"),
        line,
    )
    # Convert raw attachment URLs to an explicit clickable label.
    line = re.sub(
        r"https://github\.com/user-attachments/assets/[A-Za-z0-9-]+",
        lambda m: protect('<a href="' + html.escape(m.group(0), quote=True) +
                          '">📎 Вложение к релизу GitHub</a>'),
        line,
    )
    line = html.escape(line, quote=False)
    line = re.sub(r"\*\*([^\n*]+)\*\*", r"<b>\1</b>", line)
    line = re.sub(r"__([^\n_]+)__", r"<b>\1</b>", line)
    line = re.sub(r"(?<!\w)\*([^\n*]+)\*(?!\w)", r"<i>\1</i>", line)
    line = re.sub(r"~~([^\n~]+)~~", r"<s>\1</s>", line)
    for i, slot in enumerate(slots):
        line = line.replace("\uE000" + str(i) + "\uE001", slot)
    return line


def render_release_telegram(markdown):
    """Headings, lists, links, code, and disclosure sections without raw syntax."""
    lines = []
    in_fence = False
    for line in markdown.split("\n"):
        if line.lstrip().startswith(chr(96) * 3):
            in_fence = not in_fence
            lines.append("<pre>" if in_fence else "</pre>")
            continue
        if in_fence:
            lines.append(html.escape(line))
            continue
        if re.fullmatch(r"\s*[-*_]{3,}\s*", line):
            lines.append("────────")
            continue
        match = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            lines.append("<b>" + telegram_inline_markdown(match.group(1)) + "</b>")
            continue
        match = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if match:
            lines.append(match.group(1) + "• " + telegram_inline_markdown(match.group(2)))
            continue
        match = re.match(r"^(\s*\d+[.)])\s+(.+)$", line)
        if match:
            lines.append(match.group(1) + " " + telegram_inline_markdown(match.group(2)))
            continue
        lines.append(telegram_inline_markdown(line))
    return "\n".join(lines).strip()


def split_text_for_translation(text, max_bytes=TRANSLATION_MAX_BYTES):
    """Делит строку по словам, соблюдая лимит MyMemory в UTF-8 байтах."""
    parts = []
    buffer = ""
    for token in re.findall(r"\S+\s*|\s+", text):
        if len((buffer + token).encode("utf-8")) <= max_bytes:
            buffer += token
            continue
        if buffer.strip():
            parts.append(buffer.strip())
        buffer = ""
        # На случай необычно длинной строки без пробелов.
        for char in token:
            if len((buffer + char).encode("utf-8")) > max_bytes:
                if buffer.strip():
                    parts.append(buffer.strip())
                buffer = ""
            buffer += char
    if buffer.strip():
        parts.append(buffer.strip())
    return parts


@lru_cache(maxsize=256)
def translate_fragment(fragment, timeout=10):
    """Переводит один небольшой фрагмент через MyMemory (en -> ru)."""
    params = urlencode({"q": fragment, "langpair": "en|ru"})
    data = request_json("https://api.mymemory.translated.net/get?" + params, timeout=timeout)
    if int(data.get("responseStatus", 0)) != 200:
        raise RuntimeError("Сервис перевода временно недоступен или исчерпан лимит")
    translated = html.unescape(str(data.get("responseData", {}).get("translatedText", "")))
    if not translated.strip() or translated.upper().startswith("MYMEMORY WARNING"):
        raise RuntimeError("Сервис перевода не вернул корректный текст")
    return translated


def translate_line(line, deadline=None):
    """Не переводит ссылки, код и технические имена."""
    stripped = line.strip()
    fixed = RELEASE_LINE_TRANSLATIONS.get(stripped)
    if fixed is not None:
        return line[:len(line) - len(line.lstrip())] + fixed
    full = FULL_CHANGELOG_RE.fullmatch(stripped)
    if full:
        return "**Полный список изменений (с предыдущего стабильного выпуска)**: " + full.group(3)
    if not stripped or stripped.startswith(("|", "![", "<")):
        return line
    if re.fullmatch(r"[-=+_*\s]+", stripped):
        return line
    if re.fullmatch(r"https?://\S+", stripped):
        return line

    # Сохраняем маркеры Markdown списков, а текст переводим.
    marker = re.match(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+))", line)
    prefix = marker.group(0) if marker else ""
    body = line[len(prefix):]

    saved = []

    def protect(match):
        saved.append(match.group(0))
        return f"ZXQTERM{len(saved)-1}ZXQ"

    safe_text = PROTECT_RE.sub(protect, body)
    if not re.search(r"[A-Za-zА-Яа-я]", re.sub(r"ZXQTERM\d+ZXQ", "", safe_text)):
        return line
    pieces = split_text_for_translation(safe_text)
    if not pieces:
        return line
    output_parts = []
    for part in pieces:
        remaining = 10 if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Исчерпано время на перевод")
        output_parts.append(translate_fragment(part, timeout=min(10, remaining)))
    output = " ".join(output_parts)
    for i, term in enumerate(saved):
        placeholder = f"ZXQTERM{i}ZXQ"
        # Иногда переводчик добавляет пробелы внутри маркера — убираем их.
        spaced = re.compile(r"ZXQ\s*TERM\s*" + str(i) + r"\s*ZXQ", re.IGNORECASE)
        if not spaced.search(output):
            raise RuntimeError("Переводчик повредил технический маркер")
        output = spaced.sub(lambda _m: term, output)
    return prefix + output


def translate_changelog(text):
    """Возвращает (текст, переведён_ли, пояснение). При сбое оставляет оригинал."""
    if not text.strip() or text == "Описание изменений не указано.":
        return text, False, "Описание изменений не указано"
    # Если оригинал уже на русском, не трогаем его.
    cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
    letters = len(re.findall(r"[A-Za-zА-Яа-яЁё]", text))
    if letters and cyrillic / letters > 0.40:
        return text, True, "Оригинал уже на русском"

    try:
        lines_to_translate = []
        in_code = False
        deadline = time.monotonic() + TRANSLATION_BUDGET_SECONDS
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                lines_to_translate.append((line, False))
            elif in_code:
                lines_to_translate.append((line, False))
            else:
                lines_to_translate.append((line, True))
        # Independent short lines can be translated together within one time budget.
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(translate_line, line, deadline) if translate else line
                       for line, translate in lines_to_translate]
            result = "\n".join(item if isinstance(item, str) else item.result() for item in pending)
        return result, True, "Переведено автоматически (MyMemory)"
    except Exception as error:
        # Сбой переводчика не мешает отправке нового релиза.
        print(f"Перевод недоступен: {type(error).__name__}")
        return text, False, "Автоперевод недоступен; показываю оригинал"


def format_release(repo, release, translate_enabled=True):
    title = html.escape(str(release.get("name") or release.get("tag_name") or "Без названия"))
    tag = html.escape(str(release.get("tag_name") or "—"))
    url = html.escape(str(release.get("html_url") or f"https://github.com/{repo}/releases"), quote=True)
    kind = "🧪 Pre-release / Alpha / Beta" if release.get("prerelease") else "✅ Обычный релиз"
    body = normalize_release_markdown(str(release.get("body") or "Описание изменений не указано."))
    body = trim_release_markdown(body)
    if translate_enabled:
        translated_body, translated, note = translate_changelog(body)
    else:
        translated_body, translated, note = body, False, "Автоперевод выключен"
    body_label = "Изменения на русском" if translated else "Что изменилось (оригинал)"
    return (
        f"🚀 <b>Новая версия: {html.escape(repo)}</b>\n"
        f"<b>{title}</b> ({tag})\n{kind}\n\n"
        f"<b>{body_label}:</b>\n{render_release_telegram(translated_body)}\n"
        f"<i>{html.escape(note)}</i>\n\n"
        f'<a href="{url}">🔗 Открыть релиз на GitHub</a>'
    )


def load_state():
    if not STATE_FILE.exists():
        if os.getenv("GITHUB_ACTIONS") == "true":
            raise RuntimeError("release_state.json отсутствует — восстанови историю из Git, не создавай пустую")
        return {"repositories": {}, "last_heartbeat": ""}, True
    with STATE_FILE.open("r", encoding="utf-8") as f:
        saved = json.load(f)
    if not isinstance(saved, dict) or not isinstance(saved.get("repositories"), dict):
        raise RuntimeError("Некорректный release_state.json — не перезаписываем историю")
    if any(ids is not None and (not isinstance(ids, list) or
            any(not isinstance(item, (str, int)) for item in ids))
            for ids in saved["repositories"].values()):
        raise RuntimeError("Повреждена история релизов — не перезаписываем состояние")
    return saved, False


# Run timestamps and diagnostics live in the GitHub Actions logs. They must not
# trigger repository commits when releases, subscriptions and settings are unchanged.
RUN_METADATA_FIELDS = frozenset({"last_run", "last_heartbeat", "last_schedule_at"})


def meaningful_state_snapshot(state):
    """Stable signature excluding per-run telemetry, including nested release IDs."""
    return json.dumps(
        {key: value for key, value in state.items() if key not in RUN_METADATA_FIELDS},
        ensure_ascii=False, sort_keys=True,
    )


def save_state(state):
    """Atomic local checkpoint; Actions persists this file even after a failed scan."""
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if STATE_FILE.exists() and STATE_FILE.read_text(encoding="utf-8") == content:
        return
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(STATE_FILE)



def synchronize_cloudflare_state(state):
    """D1 is the subscription source of truth after the explicit cutover.

    Preserve each existing repository's release IDs, so switching storage
    cannot generate duplicate notifications. New repositories start at None
    and baseline their existing releases without notifying.
    A failed sync is fatal: never fall back to the stale GitHub list.
    """
    if not CLOUDFLARE_SYNC_URL.startswith("https://") or not SYNC_SECRET:
        raise RuntimeError("Cloudflare sync is enabled but URL or secret is missing")
    payload = request_json(
        CLOUDFLARE_SYNC_URL,
        headers={"Authorization": f"Bearer {SYNC_SECRET}",
                 "Accept": "application/json"},
        timeout=20,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("Cloudflare sync returned an invalid response")
    desired = payload.get("repositories")
    translated = payload.get("translate_enabled")
    if (not isinstance(desired, list) or len(desired) > MAX_SUBSCRIPTIONS
            or not isinstance(translated, bool)):
        raise RuntimeError("Cloudflare sync returned invalid subscriptions or settings")
    new_state = {}
    existing = state["repositories"]
    names = {key.casefold(): key for key in existing}
    for repo in desired:
        if (not isinstance(repo, str)
                or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", repo)
                or repo.casefold() in (key.casefold() for key in new_state)):
            raise RuntimeError("Cloudflare sync contains invalid or duplicate repository")
        old_name = names.get(repo.casefold())
        new_state[repo] = existing[old_name] if old_name is not None else None
    changed = (new_state != existing or translated != state.get("translate_enabled"))
    state["repositories"] = new_state
    state["translate_enabled"] = translated
    return changed


def write_github_run_summary(started_at, checked, total, sent, errors, deferred_until=None):
    """Current per-run diagnostic, without writing to committed state."""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "")
    if not summary_path:
        return
    if deferred_until:
        status = "⏳ Отложено: GitHub API ограничил запросы"
        after = datetime.fromtimestamp(deferred_until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    elif errors:
        status = "⚠️ Проверка с ошибками"
        after = "—"
    else:
        status = "✅ Проверка выполнена"
        after = "—"
    lines = [
        "## PS5 GitHub Monitor — отчёт о запуске",
        "",
        f"**Результат:** {status}",
        f"**Начало (UTC):** {started_at}",
        f"**Окончание (UTC):** {datetime.now(timezone.utc).isoformat()}",
        f"**Проверено репозиториев:** {checked} / {total}",
        f"**Новых уведомлений:** {sent}",
        f"**Ошибок:** {len(errors)}",
        f"**Повторить запросы не ранее:** {after}",
        "",
        "Статистика относится к этому запуску GitHub Actions.",
        "Время в release_state.json обновляется только вместе с историей релизов или настройками.",
    ]
    try:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    except OSError as error:
        print(f"Не удалось записать сводку GitHub Actions: {type(error).__name__}")


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Добавь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Actions Secrets")

    state, first_run = load_state()
    initial_meaningful_state = meaningful_state_snapshot(state)
    if first_run:
        state["repositories"] = {repo: None for repo in DEFAULT_REPOS}
    state.setdefault("translate_enabled", True)  # preserves previously enabled translation
    if CLOUDFLARE_SYNC_ENABLED:
        # Do not continue scanning stale repositories if D1 is unavailable.
        synchronize_cloudflare_state(state)
    started_at = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + 240
    event = os.getenv("GITHUB_EVENT_NAME", "local")
    errors = []
    processed = checked = sent = 0
    deferred_until = None

    def record_error(stage, error):
        # No API URL, token, chat ID, or message contents in persistent diagnostics.
        errors.append(f"{stage}: {type(error).__name__}")
        print(f"Ошибка {stage}: {type(error).__name__}")

    if not state.get("commands_menu_configured"):
        try:
            configure_commands_menu()
            state["commands_menu_configured"] = True
        except (RuntimeError, ValueError) as error:
            print(f"Меню команд пока не настроено: {type(error).__name__}")

    try:
        try:
            updates = [] if CLOUDFLARE_SYNC_ENABLED else tg_updates(state.get("telegram_offset"))
        except (RuntimeError, OSError, ValueError) as error:
            record_error("getUpdates", error)
            updates = []
        for update in updates:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            try:
                process_update(update, state)
            except (RuntimeError, OSError, ValueError) as error:
                record_error("Telegram update", error)
                # Keep this update and later ones unacknowledged for the next run.
                break
            state["telegram_offset"] = update_id
            processed += 1
            save_state(state)

        backoff_until = int(state.get("github_backoff_until") or 0)
        if backoff_until > time.time():
            deferred_until = backoff_until
            print("GitHub API: проверка отложена до окончания ранее установленной паузы")
        else:
            if "github_backoff_until" in state:
                del state["github_backoff_until"]
            for repo in list(state["repositories"]):
                if time.monotonic() >= deadline:
                    record_error("Остальные репозитории отложены до следующего запуска", RuntimeError())
                    break
                try:
                    releases = fetch_releases(repo)
                except GitHubRateLimited as error:
                    deferred_until = error.retry_at
                    state["github_backoff_until"] = error.retry_at
                    print("GitHub API: ограничение частоты; остальные запросы отложены")
                    break
                except (RuntimeError, OSError, ValueError) as error:
                    record_error(repo, error)
                    continue
                checked += 1
                previous = state["repositories"][repo]
                if previous is None:
                    state["repositories"][repo] = [str(r["id"]) for r in releases]
                    save_state(state)
                    continue
                seen = set(map(str, previous))
                for release in reversed(releases):
                    release_id = str(release["id"])
                    if release_id in seen:
                        continue
                    if time.monotonic() >= deadline:
                        record_error(f"{repo}: отправка отложена", RuntimeError())
                        break
                    try:
                        send_telegram(format_release(repo, release, state["translate_enabled"]))
                    except (RuntimeError, OSError, ValueError) as error:
                        record_error(f"{repo}: sendMessage", error)
                        break
                    # Never mark an unsent release as delivered; retain all known IDs.
                    state["repositories"][repo].append(release_id)
                    seen.add(release_id)
                    sent += 1
                    save_state(state)

        if first_run:
            send_telegram("✅ <b>PS5 GitHub Monitor запущен!</b>\n"
                          f"Отслеживаются {len(state['repositories'])} репозиториев. "
                          "Чтобы добавить новый, пришли его ссылку GitHub.")
        if state.get("manual_check_requested"):
            try:
                prefix = "⚠️ Проверка завершена с ошибками" if errors else "✅ Проверка завершена"
                send_telegram(f"{prefix}: проверено {checked} из {len(state['repositories'])} "
                              f"репозиториев, отправлено релизов: {sent}; ошибок: {len(errors)}.")
                state.pop("manual_check_requested", None)
            except (RuntimeError, OSError, ValueError) as error:
                record_error("Отчёт /check", error)
    finally:
        # Commit only real state changes. Otherwise a one-minute timer produces
        # 1,440 useless commits/day just to update last_run/last_heartbeat.
        if meaningful_state_snapshot(state) != initial_meaningful_state:
            now = datetime.now(timezone.utc).isoformat()
            state["last_heartbeat"] = now
            state["last_run"] = {
                "started_at": started_at, "completed_at": now, "event": event,
                "run_id": os.getenv("GITHUB_RUN_ID", ""),
                "checked_repositories": checked, "notifications_sent": sent,
                "updates_processed": processed, "errors": errors,
            }
            save_state(state)
            print("Изменения подписок, настроек или релизов сохранены в release_state.json")
        else:
            print("Изменений состояния нет: release_state.json не переписываем")
    write_github_run_summary(started_at, checked, len(state["repositories"]),
                             sent, errors, deferred_until=deferred_until)
    print(f"Событие: {event}; проверено {checked} репозиториев; отправлено релизов: {sent}; "
          f"обработано updates: {processed}; ошибок: {len(errors)}")
    if errors:
        raise RuntimeError("Проверка завершена с ошибками; успешные операции сохранены для следующего запуска")


if __name__ == "__main__":
    main()
