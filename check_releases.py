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
from datetime import datetime, timezone
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


def request_json(url, payload=None, headers=None):
    common_headers = {"User-Agent": "ps5-github-telegram-notifier/1.0"}
    if headers:
        common_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        common_headers["Content-Type"] = "application/json; charset=utf-8"
    req = Request(url, data=data, headers=common_headers)
    try:
        with urlopen(req, timeout=25) as response:
            return json.load(response)
    except HTTPError as error:
        # Не выводим в лог URL Telegram с токеном.
        raise RuntimeError(f"HTTP {error.code} при обращении к API") from None
    except URLError as error:
        raise RuntimeError(f"Не удалось подключиться к API: {error.reason}") from None


def fetch_releases(repo):
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    url = f"https://api.github.com/repos/{repo}/releases?per_page={RELEASE_LIMIT}"
    items = request_json(url, headers=headers)
    if not isinstance(items, list):
        raise RuntimeError(f"Неожиданный ответ GitHub для {repo}")
    return [item for item in items if not item.get("draft")]


def resolve_github_repo(repo):
    """Validate a real public repository and return its canonical full_name."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    result = request_json(f"https://api.github.com/repos/{repo}", headers=headers)
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


def tg_callback(query, state):
    """Delete exactly the selected subscription, edit the same Telegram message."""
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    qid = query.get("id")
    if not qid:
        return False
    def answer(text):
        result = request_json(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            payload={"callback_query_id": qid, "text": text})
        if not result.get("ok"):
            raise RuntimeError("Telegram не подтвердил нажатие кнопки")

    if (str(sender.get("id")) != CHAT_ID or str(chat.get("id")) != CHAT_ID
            or chat.get("type") != "private" or not message.get("message_id")):
        answer("Нет доступа")
        return False
    data = query.get("data", "")
    if data == "rm:close":
        answer("Меню закрыто")
        result = request_json(
            f"https://api.telegram.org/bot{TOKEN}/editMessageText",
            payload={"chat_id": CHAT_ID, "message_id": message["message_id"],
                     "text": "✅ Меню удаления закрыто. Остальные подписки сохранены."})
        if not result.get("ok"):
            raise RuntimeError("Не удалось закрыть меню")
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

    # Refresh the same message so the other buttons remain available.
    del subs[repo]
    result = request_json(
        f"https://api.telegram.org/bot{TOKEN}/editMessageText",
        payload={"chat_id": CHAT_ID, "message_id": message["message_id"],
                 "text": removal_text(subs, deleted=repo), "parse_mode": "HTML",
                 "reply_markup": removal_buttons(subs) if subs else {"inline_keyboard": []}})
    if not result.get("ok"):
        raise RuntimeError("Не удалось обновить список подписок")
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
        send_telegram(
            f"📊 <b>Мониторинг включён</b>\n"
            f"📦 Подписок: {len(subs)}\n"
            f"🌐 Перевод: {'включён' if state['translate_enabled'] else 'выключен'}\n"
            "⏱ Проверка: примерно каждые 5 минут через GitHub Actions\n"
            "🔔 Уведомления только о новых Releases, включая Alpha и Beta"
        )
        return False
    if cmd in {"/translate_on", "/translate_off"}:
        desired = (cmd == "/translate_on")
        changed = state["translate_enabled"] != desired
        state["translate_enabled"] = desired
        send_telegram("🇷🇺 Перевод описаний " + ("включён ✅" if desired else "выключен ✅"))
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



# MyMemory: бесплатный перевод без ключа (анонимно до 5000 символов/день).
# Ограничение сервиса: до 500 байт на один фрагмент.
TRANSLATION_MAX_BYTES = 400
CHANGELOG_MAX_CHARS = 1150
PROTECT_RE = re.compile(
    r"https?://[^\s)]+|`[^`\n]+`|"
    r"(?<![\w])(?:ShadowMountPlus|ps5-y2jb-autoloader|"
    r"kstuff-lite|MihawkOS|WebKit|GitHub|"
    r"FPKG|PKG|ELF|HV|Y2JB|SMB2|SMB3|SMB|PS5|PS4|"
    r"Proton|Linux|DXVK|Vulkan|Alpha|Beta)(?![\w])",
    re.IGNORECASE,
)


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


def translate_fragment(fragment):
    """Переводит один небольшой фрагмент через MyMemory (en -> ru)."""
    params = urlencode({"q": fragment, "langpair": "en|ru"})
    data = request_json("https://api.mymemory.translated.net/get?" + params)
    if int(data.get("responseStatus", 0)) != 200:
        raise RuntimeError("Сервис перевода временно недоступен или исчерпан лимит")
    translated = html.unescape(str(data.get("responseData", {}).get("translatedText", "")))
    if not translated.strip() or translated.upper().startswith("MYMEMORY WARNING"):
        raise RuntimeError("Сервис перевода не вернул корректный текст")
    return translated


def translate_line(line):
    """Не переводит ссылки, код и технические имена."""
    stripped = line.strip()
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
    pieces = split_text_for_translation(safe_text)
    if not pieces:
        return line
    output = " ".join(translate_fragment(part) for part in pieces)
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
        translated_lines = []
        in_code = False
        for line in text.split("\n"):
            if line.lstrip().startswith("```"):
                in_code = not in_code
                translated_lines.append(line)
            elif in_code:
                translated_lines.append(line)
            else:
                translated_lines.append(translate_line(line))
        result = "\n".join(translated_lines)
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
    body = str(release.get("body") or "Описание изменений не указано.").strip()
    if len(body) > CHANGELOG_MAX_CHARS:
        body = body[:CHANGELOG_MAX_CHARS - 3].rstrip() + "…"
    if translate_enabled:
        translated_body, translated, note = translate_changelog(body)
    else:
        translated_body, translated, note = body, False, "Автоперевод выключен"
    body_label = "Изменения на русском" if translated else "Что изменилось (оригинал)"
    return (
        f"🚀 <b>Новая версия: {html.escape(repo)}</b>\n"
        f"<b>{title}</b> ({tag})\n{kind}\n\n"
        f"<b>{body_label}:</b>\n{html.escape(translated_body)}\n"
        f"<i>{html.escape(note)}</i>\n\n"
        f'<a href="{url}">🔗 Открыть релиз на GitHub</a>'
    )


def load_state():
    if not STATE_FILE.exists():
        return {"repositories": {}, "last_heartbeat": ""}, True
    with STATE_FILE.open("r", encoding="utf-8") as f:
        saved = json.load(f)
    if not isinstance(saved, dict) or not isinstance(saved.get("repositories"), dict):
        raise RuntimeError("Некорректный release_state.json — не перезаписываем историю")
    return saved, False


def main():
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Добавь TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в GitHub Actions Secrets")

    state, first_run = load_state()
    if first_run:
        state["repositories"] = {repo: None for repo in DEFAULT_REPOS}
    state.setdefault("translate_enabled", True)  # preserves previously enabled translation
    changed = first_run

    updates = tg_updates(state.get("telegram_offset"))
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        if process_update(update, state):
            changed = True
        state["telegram_offset"] = update_id
        changed = True

    # Fetch all first: on GitHub API failure do not overwrite observed versions.
    current = {repo: fetch_releases(repo) for repo in list(state["repositories"])}
    notifications = []
    for repo, releases in current.items():
        previous = state["repositories"].get(repo)
        current_ids = [str(release["id"]) for release in releases]
        if previous is not None:
            old_ids = set(map(str, previous))
            notifications.extend((repo, release) for release in reversed(releases)
                                 if str(release["id"]) not in old_ids)
        if previous != current_ids:
            changed = True
        state["repositories"][repo] = current_ids

    if first_run:
        send_telegram("✅ <b>PS5 GitHub Monitor запущен!</b>\n"
                      f"Отслеживаются {len(current)} репозиториев. "
                      "Чтобы добавить новый, пришли его ссылку GitHub.\n"
                      "⏱ Проверка примерно каждые 5 минут.")
    for repo, release in notifications:
        send_telegram(format_release(repo, release, state["translate_enabled"]))
    if state.pop("manual_check_requested", False):
        changed = True
        send_telegram(f"✅ Проверка завершена: {len(current)} репозиториев, новых релизов: {len(notifications)}.")

    # Keep repo active so GitHub Actions schedule doesn't get disabled for inactivity.
    now = datetime.now(timezone.utc)
    last_heartbeat = state.get("last_heartbeat", "")
    try:
        heartbeat_age = (now - datetime.fromisoformat(last_heartbeat)).total_seconds()
    except (ValueError, TypeError):
        heartbeat_age = float("inf")
    if heartbeat_age >= 30 * 86400:
        state["last_heartbeat"] = now.isoformat()
        changed = True

    if changed:
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"Проверено {len(current)} репозиториев; новых релизов: {len(notifications)}; "
          f"обработано команд: {len(updates)}")


if __name__ == "__main__":
    main()