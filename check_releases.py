#!/usr/bin/env python3
"""Проверяет опубликованные GitHub Releases и сообщает о новых в Telegram.

Использует только стандартную библиотеку Python. История хранится в
release_state.json, который GitHub Actions коммитит в репозиторий.
"""

import html
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

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


def format_release(repo, release):
    title = html.escape(str(release.get("name") or release.get("tag_name") or "Без названия"))
    tag = html.escape(str(release.get("tag_name") or "—"))
    url = html.escape(str(release.get("html_url") or f"https://github.com/{repo}/releases"), quote=True)
    kind = "🧪 Pre-release / Alpha / Beta" if release.get("prerelease") else "✅ Обычный релиз"
    body = str(release.get("body") or "Описание изменений не указано.").strip()
    if len(body) > 1150:
        body = body[:1147].rstrip() + "…"
    return (
        f"🚀 <b>Новая версия: {html.escape(repo)}</b>\n"
        f"<b>{title}</b> ({tag})\n{kind}\n\n"
        f"<b>Что изменилось (текст автора):</b>\n{html.escape(body)}\n\n"
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

    if first_run:
        send_telegram(
            "✅ <b>PS5 GitHub Monitor запущен!</b>\n"
            f"Отслеживаются {len(REPOS)} репозиториев. Текущие версии запомнил — "
            "следующие сообщения будут только о новых релизах, включая Beta и Alpha.\n"
            "⏱ Проверка: каждые 5 минут (возможны задержки GitHub Actions)."
        )
    else:
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

    if first_run or notifications or age >= 30 * 86400:
        # Файл записывается только после успешной отправки уведомлений.
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(f"Проверено {len(REPOS)} репозиториев; новых релизов: {len(notifications)}")


if __name__ == "__main__":
    main()
