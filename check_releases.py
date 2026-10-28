#!/usr/bin/env python3
"""Проверяет опубликованные GitHub Releases и сообщает о новых в Telegram.

Использует только стандартную библиотеку Python. История хранится в
release_state.json, который GitHub Actions коммитит в репозиторий.
"""

import html
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

REPOS = [
    "itsPLK/ps5-y2jb-autoloader",
    "EchoStretch/kstuff-lite",
    "drakmor/ShadowMountPlus",
    "itsPLK/ps5-payload-manager",
    "quer3q/ps5-dump-forge",
    "mihawk-99/PS5_MihawkOS",
]

STATE_FILE = Path("release_state.json")
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


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
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


def format_release(repo, release):
    title = html.escape(str(release.get("name") or release.get("tag_name") or "Без названия"))
    tag = html.escape(str(release.get("tag_name") or "—"))
    url = html.escape(str(release.get("html_url") or f"https://github.com/{repo}/releases"), quote=True)
    kind = "🧪 Pre-release / Alpha / Beta" if release.get("prerelease") else "✅ Обычный релиз"
    body = str(release.get("body") or "Описание изменений не указано.").strip()
    if len(body) > CHANGELOG_MAX_CHARS:
        body = body[:CHANGELOG_MAX_CHARS - 3].rstrip() + "…"
    translated_body, translated, note = translate_changelog(body)
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
    # Сначала получаем все ответы: если GitHub временно недоступен, история не меняется.
    releases_by_repo = {repo: fetch_releases(repo) for repo in REPOS}
    known_by_repo = state["repositories"]
    notifications = []

    for repo, releases in releases_by_repo.items():
        previous = known_by_repo.get(repo)
        current_ids = [str(item["id"]) for item in releases]
        if previous is not None:
            old_ids = set(map(str, previous))
            # Старые -> новые, если за 5 минут опубликовали несколько релизов.
            new_releases = [item for item in reversed(releases)
                            if str(item["id"]) not in old_ids]
            notifications.extend((repo, item) for item in new_releases)
        known_by_repo[repo] = current_ids

    translation_notice_needed = (not first_run and not state.get("ru_translation_enabled_notice"))

    if first_run:
        send_telegram(
            "✅ <b>PS5 GitHub Monitor запущен!</b>\n"
            f"Отслеживаются {len(REPOS)} репозиториев. Текущие версии запомнил — "
            "следующие сообщения будут только о новых релизах, включая Beta и Alpha.\n"
            "⏱ Проверка: каждые 5 минут (возможны задержки GitHub Actions)."
        )
    else:
        if translation_notice_needed:
            send_telegram(
                "🇷🇺 <b>Автоперевод обновлений включён!</b>\n"
                "Новые описания GitHub Releases буду переводить на русский. "
                "Технические названия, версии и ссылки останутся как у автора. "
                "Если сервис перевода недоступен, пришлю оригинал."
            )
            state["ru_translation_enabled_notice"] = True
        for repo, release in notifications:
            send_telegram(format_release(repo, release))

    # Раз в ~30 дней обновляем файл, чтобы репозиторий не оставался совсем без коммитов.
    now = datetime.now(timezone.utc)
    last_heartbeat = state.get("last_heartbeat", "")
    try:
        age = (now - datetime.fromisoformat(last_heartbeat)).total_seconds()
    except (ValueError, TypeError):
        age = float("inf")
    if age >= 30 * 86400:
        state["last_heartbeat"] = now.isoformat()

    if first_run or notifications or translation_notice_needed or age >= 30 * 86400:
        # Файл записывается только после успешной отправки уведомлений.
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"Проверено {len(REPOS)} репозиториев; новых релизов: {len(notifications)}")


if __name__ == "__main__":
    main()
