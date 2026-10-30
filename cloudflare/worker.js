// PS5 GitHub Monitor: Telegram webhook + Cloudflare D1.
// Drop-in replacement: same secrets and existing D1 schema.
// It does not reconfigure the Telegram webhook or require schema migration.
const GITHUB_REPO = "Vov4k957/ps5-telegram-monitor";
const MAX_REPOS = 30;
const WORKER_VERSION = "3.1-review-fixes";
const NETWORK_TIMEOUT_MS = 10000;
const LIST_MESSAGE_LIMIT = 3400;
const textEncoder = new TextEncoder();

// Reported by GitHub Actions on each completed scan, not queried from GitHub API.
const STATUS_ROW_KEY = "monitor_latest_run";
const STATUS_STALE_MS = 5 * 60 * 1000;

async function getLatestMonitorRun(db) {
  const row = await db.prepare("SELECT value FROM settings WHERE key = ?").bind(STATUS_ROW_KEY).first();
  if (!row?.value) return null;
  try {
    const run = JSON.parse(row.value);
    if (!run || typeof run !== "object" || !Number.isFinite(Date.parse(run.completed_at))) return null;
    return run;
  } catch {
    return null;
  }
}

function monitorStatusMessage(run, reposCount, translated) {
  const summary = [
    "📊 <b>PS5 GitHub Monitor</b>",
    "✅ Cloudflare Worker: доступен",
    `📦 Подписок: ${reposCount}`,
    `🌐 Перевод: ${translated ? "включён" : "выключен"}`,
    "⚡ Проверки: GitHub Actions, примерно раз в минуту",
    "",
  ];
  if (!run) {
    summary.push("🕒 Последняя проверка: данные ещё не поступили из GitHub Actions");
    summary.push(`<a href="https://github.com/${GITHUB_REPO}/actions/workflows/ps5-telegram.yml">Открыть GitHub Actions</a>`);
    return summary.join("\n");
  }
  const latest = new Date(run.completed_at);
  const time = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(latest);
  const delta = Date.now() - latest.getTime();
  const stale = delta > STATUS_STALE_MS;
  const symbol = stale ? "⚠️" : run.status === "success" ? "✅" : "⚠️";
  summary.push(`${symbol} Последняя проверка: <b>${escapeHtml(time)} МСК</b>`);
  summary.push(`🔎 Проверено: ${run.checked}/${run.total} репозиториев`);
  summary.push(`🔔 Новых уведомлений: ${run.notifications}`);
  summary.push(`⚠️ Ошибок: ${run.errors}`);
  if (run.status === "rate_limited") {
    summary.push("⏳ GitHub API: временное ограничение запросов");
    if (run.retry_at) {
      const retry = new Date(run.retry_at * 1000);
      summary.push("Повтор после: " + escapeHtml(new Intl.DateTimeFormat("ru-RU", {
        timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit",
        hour: "2-digit", minute: "2-digit", hourCycle: "h23",
      }).format(retry)) + " МСК");
    }
  } else if (run.status === "error") {
    summary.push("⚠️ Последняя проверка завершилась с ошибками");
  }
  if (stale) {
    summary.push("⚠️ Нет свежего отчёта более 5 минут. Проверь планировщик и GitHub Actions.");
  }
  summary.push(`<a href="https://github.com/${GITHUB_REPO}/actions/runs/${encodeURIComponent(run.run_id)}">Открыть отчёт GitHub Actions</a>`);
  return summary.join("\n");
}

async function receiveMonitorReport(request, env) {
  if (!env.SYNC_SECRET || request.headers.get("Authorization") !== `Bearer ${env.SYNC_SECRET}`) {
    return replyText("Unauthorized", 401);
  }
  if (!env.DB) return replyText("Database not configured", 503);
  let report;
  try {
    const raw = await request.text();
    if (raw.length > 4096) return replyText("Too large", 413);
    report = JSON.parse(raw);
  } catch {
    return replyText("Bad JSON", 400);
  }
  const validCount = (num) => Number.isSafeInteger(num) && num >= 0 && num <= 100000;
  const validTime = typeof report?.completed_at === "string" &&
    /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|\+00:00)$/.test(report.completed_at) &&
    Number.isFinite(Date.parse(report.completed_at)) &&
    Date.parse(report.completed_at) <= Date.now() + 60000;
  const validReport = report && typeof report === "object" &&
    /^[0-9]{1,20}$/.test(report.run_id || "") &&
    ["success", "error", "rate_limited"].includes(report.status) &&
    validTime && validCount(report.checked) && validCount(report.total) &&
    validCount(report.notifications) && validCount(report.errors) &&
    report.checked <= report.total &&
    (report.retry_at == null || (Number.isSafeInteger(report.retry_at) && report.retry_at >= 0 &&
      Number.isFinite(new Date(report.retry_at * 1000).getTime())));
  if (!validReport) return replyText("Invalid status", 400);
  // Atomic compare-and-set: a slower, older Actions run cannot overwrite a
  // newer report, even when two HTTP requests arrive concurrently.
  const stored = JSON.stringify({
    run_id: report.run_id, completed_at: report.completed_at, status: report.status,
    checked: report.checked, total: report.total, notifications: report.notifications,
    errors: report.errors, retry_at: report.retry_at ?? null,
  });
  await env.DB.prepare(
    "INSERT INTO settings (key, value) VALUES (?, ?) " +
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value " +
    "WHERE julianday(json_extract(settings.value, '$.completed_at')) IS NULL " +
    "OR julianday(json_extract(settings.value, '$.completed_at')) <= " +
    "julianday(json_extract(excluded.value, '$.completed_at'))"
  ).bind(STATUS_ROW_KEY, stored).run();
  return Response.json({ ok: true }, { headers: { "Cache-Control": "no-store" } });
}


function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function replyText(message, status = 200) {
  return new Response(message, { status, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } });
}

async function healthResponse(env) {
  const configured = Boolean(env.DB && env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID &&
    env.WEBHOOK_SECRET && env.SYNC_SECRET);
  let dbOk = false;
  if (env.DB) {
    try {
      const row = await env.DB.prepare("SELECT 1 AS ok").first();
      dbOk = row?.ok === 1;
    } catch { /* Return a safe health result without database error details. */ }
  }
  const ready = configured && dbOk;
  return Response.json({ ok: ready, version: WORKER_VERSION, db_bound: Boolean(env.DB),
    db_ok: dbOk, ready }, { status: ready ? 200 : 503, headers: { "Cache-Control": "no-store" } });
}

async function claimUpdate(db, updateId, pendingId) {
  // Checking completion and claiming must be one SQL statement. A delayed
  // duplicate cannot acquire a new claim after another request has finished.
  return db.prepare(
    "INSERT OR IGNORE INTO processed_updates (update_id) " +
    "SELECT ? WHERE NOT EXISTS (SELECT 1 FROM processed_updates WHERE update_id = ?)"
  ).bind(pendingId, updateId).run();
}

async function getRepositories(db) {
  const data = await db.prepare("SELECT repo FROM subscriptions ORDER BY repo COLLATE NOCASE").all();
  return (data.results || []).map((row) => row.repo);
}

async function getTranslateEnabled(db) {
  const data = await db.prepare("SELECT value FROM settings WHERE key = ?").bind("translate_enabled").first();
  return !data || data.value === "true";
}

async function telegram(env, method, payload) {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), signal: AbortSignal.timeout(NETWORK_TIMEOUT_MS),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data.ok) {
    if (method === "editMessageText" && /message is not modified/i.test(data.description || "")) return;
    throw new Error(`Telegram ${method} rejected (${response.status})`);
  }
  return data.result;
}

async function send(env, text, reply_markup) {
  const payload = { chat_id: Number(env.TELEGRAM_CHAT_ID), text, parse_mode: "HTML", disable_web_page_preview: true };
  if (reply_markup) payload.reply_markup = reply_markup;
  return telegram(env, "sendMessage", payload);
}

async function hashName(repo) {
  const hash = await crypto.subtle.digest("SHA-256", textEncoder.encode(repo.toLowerCase()));
  return Array.from(new Uint8Array(hash)).slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function removeKeyboard(repositories) {
  const rows = await Promise.all(repositories.map(async (repo) => ([{
    text: repo, callback_data: `rm:${await hashName(repo)}`,
  }])));
  rows.push([{ text: "✅ Закрыть меню", callback_data: "rm:close" }]);
  return { inline_keyboard: rows };
}

function removeTitle(repositories, removed = null) {
  const prefix = removed ? `🗑 Подписка удалена: <b>${escapeHtml(removed)}</b>\n\n` : "";
  if (!repositories.length) return prefix + "📭 Подписок больше нет. Пришли новую ссылку на GitHub.";
  return prefix + `<b>Активные подписки (${repositories.length})</b>\n` +
    "Нажимай на названия, чтобы удалить подписки. Остальные кнопки останутся здесь.";
}

async function answerCallback(env, queryId, text = "Готово") {
  try { await telegram(env, "answerCallbackQuery", { callback_query_id: queryId, text }); }
  catch { /* A stale callback acknowledgement must not undo successful edits. */ }
}

async function handleCallback(query, env) {
  const data = query.data || "";
  const messageId = query.message?.message_id;
  if (!messageId || !query.id) return;
  const chat_id = Number(env.TELEGRAM_CHAT_ID);
  if (data === "rm:close") {
    await telegram(env, "editMessageText", {
      chat_id, message_id: messageId,
      text: "✅ Меню закрыто. Подписки сохранены.",
      reply_markup: { inline_keyboard: [] },
    });
    await answerCallback(env, query.id, "Меню закрыто");
    return;
  }
  if (!/^rm:[a-f0-9]{16}$/.test(data)) {
    await answerCallback(env, query.id, "Неизвестная кнопка");
    return;
  }
  const before = await getRepositories(env.DB);
  let target = null;
  for (const repo of before) {
    if (`rm:${await hashName(repo)}` === data) { target = repo; break; }
  }
  if (target) await env.DB.prepare("DELETE FROM subscriptions WHERE repo = ?").bind(target).run();
  const after = await getRepositories(env.DB);
  await telegram(env, "editMessageText", {
    chat_id, message_id: messageId,
    text: removeTitle(after, target), parse_mode: "HTML",
    reply_markup: after.length ? await removeKeyboard(after) : { inline_keyboard: [] },
  });
  await answerCallback(env, query.id, target ? "Подписка удалена" : "Уже удалена");
}

function parseGitHubRepository(text) {
  // Only genuine GitHub links at a text boundary, not evilgithub.com/path.
  const match = text.match(/(?:^|[\s([<])(?:https?:\/\/)?(?:www\.)?github\.com\/([a-z\d-]+)\/([a-z\d_.-]+)/i);
  if (!match) return null;
  const owner = match[1];
  const name = match[2].replace(/\.git$/i, "").replace(/[.,;:]$/, "");
  const repo = `${owner}/${name}`;
  if (owner.length > 39 || name.length > 100 ||
      !/^[a-z\d](?:[a-z\d-]*[a-z\d])?$/i.test(owner) ||
      !/^[a-z\d_.-]+$/i.test(name) || name === "." || name === "..") return null;
  return repo;
}

async function validateGitHubRepository(repo, env) {
  // Unauthenticated GitHub API calls have an IP-based limit. Cloudflare uses
  // shared egress IPs, so a valid public repo may occasionally return 403/429.
  const headers = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "ps5-monitor-webhook",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  if (env.GITHUB_READ_TOKEN) headers.Authorization = `Bearer ${env.GITHUB_READ_TOKEN}`;
  let response;
  try {
    response = await fetch(`https://api.github.com/repos/${repo}`, {
      headers, signal: AbortSignal.timeout(NETWORK_TIMEOUT_MS),
    });
  } catch (error) {
    console.warn("GitHub repository validation connection failed:", error?.name || "network");
    return { repo, deferred: true };
  }
  if (response.status === 404) throw new Error("GitHub repo not found (404)");
  if (response.ok) {
    const data = await response.json();
    if (data.private || !data.full_name || data.archived) throw new Error("Repository is private, missing or archived");
    return { repo: data.full_name, deferred: false };
  }
  if (response.status === 403 || response.status === 429 || response.status >= 500) {
    console.warn("GitHub API unavailable during repo check: HTTP", response.status,
      "remaining", response.headers.get("x-ratelimit-remaining") || "unknown");
    return { repo, deferred: true };
  }
  throw new Error(`GitHub repo lookup HTTP ${response.status}`);
}

async function handleMessage(message, env) {
  const text = String(message.text || "").trim();
  if (!text) return;
  const cmd = text.split(/\s+/, 1)[0].split("@")[0].toLowerCase();
  const repos = await getRepositories(env.DB);
  if (cmd === "/start" || cmd === "/help") {
    await send(env, "🤖 <b>PS5 GitHub Monitor</b>\n" +
      "Отправь ссылку на GitHub-репозиторий, и я добавлю подписку.\n\n" +
      "/list — мои подписки\n/remove — удалить кнопками\n" +
      "/translate_on — включить перевод\n/translate_off — выключить перевод\n" +
      "/status — состояние бота\n/check — проверить релизы\n/help — помощь");
    return;
  }
  if (cmd === "/list") {
    const translated = await getTranslateEnabled(env.DB);
    const header = `📋 <b>Подписки (${repos.length})</b>\n`;
    let current = header;
    for (let i = 0; i < repos.length; i++) {
      const repo = repos[i];
      const entry = `${i + 1}. <a href="https://github.com/${encodeURI(repo)}">${escapeHtml(repo)}</a>\n`;
      if (current.length + entry.length > LIST_MESSAGE_LIMIT && current !== header) {
        await send(env, current.trimEnd());
        current = "📋 <b>Подписки — продолжение</b>\n";
      }
      current += entry;
    }
    if (!repos.length) current += "Пока нет подписок\n";
    current += `\n🌐 Перевод: ${translated ? "включён" : "выключен"}`;
    await send(env, current);
    return;
  }
  if (cmd === "/remove") {
    await send(env, removeTitle(repos), repos.length ? await removeKeyboard(repos) : undefined);
    return;
  }
  if (cmd === "/translate_on" || cmd === "/translate_off") {
    const value = cmd === "/translate_on" ? "true" : "false";
    await env.DB.prepare("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value")
      .bind("translate_enabled", value).run();
    await send(env, value === "true" ? "🇷🇺 Перевод включён ✅" : "🌐 Перевод выключен ✅");
    return;
  }
  if (cmd === "/status") {
    const translated = await getTranslateEnabled(env.DB);
    const latest = await getLatestMonitorRun(env.DB);
    await send(env, monitorStatusMessage(latest, repos.length, translated));
    return;
  }
  if (cmd === "/check") {
    if (env.GITHUB_DISPATCH_TOKEN) {
      const response = await fetch(`https://api.github.com/repos/${GITHUB_REPO}/actions/workflows/ps5-telegram.yml/dispatches`, {
        method: "POST",
        headers: { "Authorization": `Bearer ${env.GITHUB_DISPATCH_TOKEN}`, "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json", "User-Agent": "ps5-monitor-webhook" },
        body: JSON.stringify({ ref: "main" }),
        signal: AbortSignal.timeout(NETWORK_TIMEOUT_MS),
      });
      if (!response.ok) throw new Error(`GitHub dispatch failed (${response.status})`);
      await send(env, "▶️ Запрос проверки отправлен в GitHub Actions. Результаты придут отдельным сообщением при обнаружении релиза.");
    } else {
      await send(env, "⏱ Автоматическая проверка релизов работает примерно раз в минуту. Прямой запуск командой /check пока не подключён.");
    }
    return;
  }
  if (cmd.startsWith("/")) {
    await send(env, "Неизвестная команда. Отправь /help.");
    return;
  }
  const requested = parseGitHubRepository(text);
  if (!requested) {
    await send(env, "Пришли ссылку вида https://github.com/owner/repository или отправь /help.");
    return;
  }
  // D1 is authoritative: check existing subscriptions before any remote calls.
  if (repos.some((name) => name.toLowerCase() === requested.toLowerCase())) {
    await send(env, `ℹ️ На <b>${escapeHtml(requested)}</b> ты уже подписан.`);
    return;
  }
  if (repos.length >= MAX_REPOS) {
    await send(env, `⚠️ Лимит: ${MAX_REPOS} подписок. Сначала используй /remove.`);
    return;
  }
  let validation;
  try { validation = await validateGitHubRepository(requested, env); }
  catch (error) {
    console.warn("GitHub repository validation rejected:", error.message);
    await send(env, `❌ Не удалось добавить <b>${escapeHtml(requested)}</b>: репозиторий не подтверждён GitHub. Попробуй позже.`);
    return;
  }
  // GitHub can canonicalize capitalization or redirect renamed repositories.
  // Check again before storing the canonical result.
  const refreshed = await getRepositories(env.DB);
  if (refreshed.some((name) => name.toLowerCase() === validation.repo.toLowerCase())) {
    await send(env, `ℹ️ На <b>${escapeHtml(validation.repo)}</b> ты уже подписан.`);
    return;
  }
  if (refreshed.length >= MAX_REPOS) {
    await send(env, `⚠️ Лимит: ${MAX_REPOS} подписок. Сначала используй /remove.`);
    return;
  }
  // The GitHub Actions scan will baseline release IDs without notifying.
  // Temporary GitHub API failures must not prevent subscribing to a real project.
  const inserted = await env.DB.prepare(
    "INSERT OR IGNORE INTO subscriptions (repo) " +
    "SELECT ? WHERE (SELECT COUNT(*) FROM subscriptions) < ? " +
    "AND NOT EXISTS (SELECT 1 FROM subscriptions WHERE repo = ? COLLATE NOCASE)"
  ).bind(validation.repo, MAX_REPOS, validation.repo).run();
  if (!inserted.meta?.changes) {
    const exists = await env.DB.prepare("SELECT repo FROM subscriptions WHERE repo = ? COLLATE NOCASE")
      .bind(validation.repo).first();
    await send(env, exists
      ? `ℹ️ На <b>${escapeHtml(validation.repo)}</b> ты уже подписан.`
      : `⚠️ Лимит: ${MAX_REPOS} подписок. Сначала используй /remove.`);
    return;
  }
  const notice = validation.deferred
    ? "⚠️ GitHub API временно не ответил. Репозиторий будет дополнительно проверен при ближайшем запуске мониторинга."
    : "✅ GitHub подтвердил открытый репозиторий.";
  await send(env, `✅ Подписка добавлена: <b>${escapeHtml(validation.repo)}</b>\n` +
    `${notice}\nСтарые релизы не будут отправлены повторно.`);

}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (request.method === "GET" && path === "/") {
      return replyText(`PS5 GitHub Monitor Worker ${WORKER_VERSION}: deployed. Check /health for diagnostics.`);
    }
    if (request.method === "GET" && path === "/health") {
      return healthResponse(env);
    }
    if (request.method === "GET" && path === "/sync") {
      if (!env.SYNC_SECRET || request.headers.get("Authorization") !== `Bearer ${env.SYNC_SECRET}`) {
        return replyText("Unauthorized", 401);
      }
      if (!env.DB) return replyText("Database not configured", 503);
      const repositories = await getRepositories(env.DB);
      return Response.json({ repositories, translate_enabled: await getTranslateEnabled(env.DB) },
        { headers: { "Cache-Control": "no-store" } });
    }
    if (request.method === "POST" && path === "/report") {
      return receiveMonitorReport(request, env);
    }
    if (request.method === "POST" && path === "/telegram") {
      if (!env.DB || !env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID || !env.WEBHOOK_SECRET) {
        return replyText("Not configured", 503);
      }
      if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
        return replyText("Forbidden", 403);
      }
      let update;
      try { update = await request.json(); }
      catch { return replyText("Bad JSON", 400); }
      if (!Number.isSafeInteger(update?.update_id)) return replyText("Bad update", 400);
      // Two-phase deduplication, using the existing processed_updates table:
      // positive ID = completed, negative (-ID-1) = processing. A concurrent
      // delivery gets 503 (retry), not a misleading 200 before completion.
      const updateId = update.update_id;
      if (updateId < 0 || updateId > 2147483647) return replyText("Bad update", 400);
      const pendingId = -updateId - 1;
      const completed = await env.DB.prepare("SELECT update_id FROM processed_updates WHERE update_id = ?")
        .bind(updateId).first();
      if (completed) return replyText("OK");
      let claim = await claimUpdate(env.DB, updateId, pendingId);
      if (!claim.meta?.changes) {
        // Workers can be interrupted. Reclaim abandoned claims after 90 seconds;
        // the normal command path has a bounded network timeout of 10 seconds.
        const expired = await env.DB.prepare(
          "DELETE FROM processed_updates WHERE update_id = ? " +
          "AND julianday(processed_at) < julianday('now', '-90 seconds')"
        ).bind(pendingId).run();
        if (expired.meta?.changes) {
          claim = await claimUpdate(env.DB, updateId, pendingId);
        }
      }
      if (!claim.meta?.changes) {
        const doneNow = await env.DB.prepare("SELECT update_id FROM processed_updates WHERE update_id = ?")
          .bind(updateId).first();
        return doneNow ? replyText("OK") : replyText("Update is processing", 503);
      }
      try {
        const item = update.message || update.callback_query?.message;
        const sender = update.message?.from || update.callback_query?.from;
        const id = Number(env.TELEGRAM_CHAT_ID);
        if (item?.chat?.type === "private" && Number(item.chat.id) === id && Number(sender?.id) === id) {
          if (update.callback_query) await handleCallback(update.callback_query, env);
          else if (update.message) await handleMessage(update.message, env);
        }
        // Record completion before releasing the pending claim.
        await env.DB.prepare("INSERT OR IGNORE INTO processed_updates (update_id) VALUES (?)")
          .bind(updateId).run();
        await env.DB.prepare("DELETE FROM processed_updates WHERE update_id = ?")
          .bind(pendingId).run();
      } catch (error) {
        await env.DB.prepare("DELETE FROM processed_updates WHERE update_id = ?")
          .bind(pendingId).run();
        console.error("Telegram update failed:", error?.name || "Error");
        return replyText("Retry", 500);
      }
      return replyText("OK");
    }
    return replyText("Not found", 404);
  },
};
