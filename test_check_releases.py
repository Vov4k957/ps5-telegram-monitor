"""Regression checks: all Telegram/GitHub calls are mocked; real state is untouched."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_releases as bot


def release(number, prerelease=False):
    return {"id": number, "tag_name": f"v{number}", "body": "Fixed startup crash",
            "prerelease": prerelease, "draft": False}


def message(text, number=101, sender=42):
    return {"update_id": number, "message": {"text": text, "from": {"id": sender},
            "chat": {"id": 42, "type": "private"}}}


def callback(repo=None, data=None, sender=42):
    if data is None:
        data = bot.removal_buttons({repo: []})["inline_keyboard"][0][0]["callback_data"]
    return {"id": "test-query", "from": {"id": sender}, "data": data,
            "message": {"message_id": 77, "chat": {"id": 42, "type": "private"}}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "state.json"
        self.state = {"repositories": {"one/repo": ["1"], "two/repo": ["2"],
                                       "three/repo": ["3"]},
                      "translate_enabled": False, "commands_menu_configured": True,
                      "commands_menu_scope_cleared": True,
                      "telegram_offset": 100, "last_heartbeat": "2026-10-28T10:33:00+00:00"}
        for name, value in {"TOKEN": "test-token", "CHAT_ID": "42", "STATE_FILE": self.state_file}.items():
            p = patch.object(bot, name, value)
            p.start()
            self.addCleanup(p.stop)
        # Any accidental real HTTP access fails the test.
        p = patch.object(bot, "urlopen", side_effect=AssertionError("Unexpected network access"))
        p.start()
        self.addCleanup(p.stop)

    def write_state(self):
        self.state_file.write_text(json.dumps(self.state), encoding="utf-8")

    def saved(self):
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def test_removes_chat_override_instead_of_replacing_botfather_commands(self):
        calls = []
        def api(url, payload=None, **kwargs):
            calls.append((url, copy.deepcopy(payload)))
            return {"ok": True}
        with patch.object(bot, "request_json", side_effect=api):
            bot.configure_commands_menu()
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][0].endswith("/deleteMyCommands"))
        self.assertEqual(
            calls[0][1]["scope"], {"type": "chat", "chat_id": 42})
        self.assertTrue(calls[1][0].endswith("/setChatMenuButton"))
        self.assertFalse(any(url.endswith("/setMyCommands") for url, _ in calls))

    def test_menu_migration_runs_once_and_keeps_release_history(self):
        self.state.pop("commands_menu_scope_cleared")
        self.write_state()
        original_repositories = copy.deepcopy(self.state["repositories"])
        calls = []
        def api(url, payload=None, **kwargs):
            calls.append(url)
            return {"ok": True}
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]), \
                patch.object(bot, "request_json", side_effect=api):
            bot.main()
            bot.main()
        self.assertEqual(sum(x.endswith("/deleteMyCommands") for x in calls), 1)
        self.assertTrue(self.saved()["commands_menu_scope_cleared"])
        self.assertEqual(self.saved()["repositories"], original_repositories)
        self.assertNotIn("setMyCommands", " ".join(calls))

    def test_menu_migration_retries_after_telegram_api_error(self):
        self.state.pop("commands_menu_scope_cleared")
        self.write_state()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]), \
                patch.object(bot, "request_json", side_effect=bot.APIError(503)):
            bot.main()
        self.assertNotIn("commands_menu_scope_cleared", self.saved())

    def test_consecutive_removals_edit_same_message(self):
        edits = []
        def api(url, payload=None, **kwargs):
            if url.endswith("editMessageText"):
                edits.append(copy.deepcopy(payload))
            return {"ok": True}
        with patch.object(bot, "request_json", side_effect=api):
            bot.tg_callback(callback("one/repo"), self.state)
            bot.tg_callback(callback("two/repo"), self.state)
        self.assertEqual(list(self.state["repositories"]), ["three/repo"])
        self.assertEqual([e["message_id"] for e in edits], [77, 77])
        self.assertEqual([len(e["reply_markup"]["inline_keyboard"]) for e in edits], [3, 2])

    def test_expired_callback_ack_does_not_cancel_removal(self):
        def api(url, **kwargs):
            if url.endswith("answerCallbackQuery"):
                raise RuntimeError("HTTP 400: callback expired")
            return {"ok": True}
        with patch.object(bot, "request_json", side_effect=api):
            self.assertTrue(bot.tg_callback(callback("one/repo"), self.state))
        self.assertNotIn("one/repo", self.state["repositories"])

    def test_expired_callback_ack_does_not_cancel_close(self):
        edits = []
        def api(url, payload=None, **kwargs):
            if url.endswith("answerCallbackQuery"):
                raise RuntimeError("Expired query")
            edits.append(payload)
            return {"ok": True}
        with patch.object(bot, "request_json", side_effect=api):
            bot.tg_callback(callback(data="rm:close"), self.state)
        self.assertEqual(len(edits), 1)
        self.assertEqual(len(self.state["repositories"]), 3)
        self.assertEqual(edits[0]["reply_markup"], {"inline_keyboard": []})

    def test_failed_edit_does_not_mutate_subscriptions(self):
        before = copy.deepcopy(self.state)
        with patch.object(bot, "request_json", side_effect=RuntimeError("Unavailable")):
            with self.assertRaises(RuntimeError):
                bot.tg_callback(callback("one/repo"), self.state)
        self.assertEqual(self.state, before)

    def test_replayed_successful_edit_is_idempotent(self):
        def api(url, **kwargs):
            if url.endswith("editMessageText"):
                raise bot.APIError(400, "Bad Request: message is not modified")
            return {"ok": True}
        with patch.object(bot, "request_json", side_effect=api):
            bot.tg_callback(callback("one/repo"), self.state)
            self.assertFalse(bot.tg_callback(callback("one/repo"), self.state))
        self.assertNotIn("one/repo", self.state["repositories"])

    def test_unauthorized_messages_and_buttons_cannot_change_state(self):
        before = copy.deepcopy(self.state)
        with patch.object(bot, "request_json", return_value={"ok": True}) as api, \
                patch.object(bot, "send_telegram") as send:
            bot.process_update(message("/translate_on", sender=999), self.state)
            bot.tg_callback(callback("one/repo", sender=999), self.state)
        self.assertEqual(before, self.state)
        send.assert_not_called()
        self.assertTrue(all(c.args[0].endswith("answerCallbackQuery") for c in api.call_args_list))

    def test_list_status_and_remove_reply(self):
        with patch.object(bot, "send_telegram") as send:
            for cmd in ["/list", "/status", "/remove"]:
                bot.process_update(message(cmd), self.state)
        self.assertEqual(send.call_count, 3)
        self.assertIn("one/repo", send.call_args_list[0].args[0])
        self.assertIn("reply_markup", send.call_args_list[2].kwargs)

    def test_link_addition_baselines_existing_stable_and_beta(self):
        with patch.object(bot, "resolve_github_repo", return_value="owner/New"), \
                patch.object(bot, "fetch_releases", return_value=[release(20), release(19, True)]), \
                patch.object(bot, "send_telegram") as send:
            self.assertTrue(bot.process_update(message("https://github.com/owner/new/releases"), self.state))
        self.assertEqual(self.state["repositories"]["owner/New"], ["20", "19"])
        self.assertEqual(send.call_count, 1)
        self.assertIn("Текущие релизы запомнил", send.call_args.args[0])

    def test_duplicate_link_is_case_insensitive(self):
        with patch.object(bot, "resolve_github_repo", return_value="ONE/Repo"), \
                patch.object(bot, "fetch_releases") as fetch, patch.object(bot, "send_telegram"):
            self.assertFalse(bot.process_update(message("https://github.com/ONE/Repo"), self.state))
        fetch.assert_not_called()
        self.assertEqual(len(self.state["repositories"]), 3)

    def test_drafts_are_skipped_but_prereleases_included(self):
        items = [release(1), release(2, True), {**release(3), "draft": True}]
        with patch.object(bot, "request_json", return_value=items):
            self.assertEqual([r["id"] for r in bot.fetch_releases("owner/repo")], [1, 2])

    def test_one_failed_repo_does_not_block_another_or_lose_commands(self):
        self.write_state()
        def fetch(repo):
            if repo == "one/repo":
                raise RuntimeError("HTTP 404")
            return [release(4, True), release(2)] if repo == "two/repo" else [release(3)]
        with patch.object(bot, "tg_updates", return_value=[message("/translate_off")]), \
                patch.object(bot, "fetch_releases", side_effect=fetch), \
                patch.object(bot, "send_telegram") as send:
            with self.assertRaises(RuntimeError):
                bot.main()
        saved = self.saved()
        self.assertEqual(saved["telegram_offset"], 101)
        self.assertEqual(saved["repositories"]["one/repo"], ["1"])
        self.assertIn("4", saved["repositories"]["two/repo"])
        self.assertTrue(any("Pre-release" in c.args[0] for c in send.call_args_list))

    def test_partial_delivery_retries_only_unsent_release(self):
        self.state["repositories"] = {"one/repo": ["1"]}
        self.write_state()
        releases = [release(3), release(2), release(1)]
        with patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", return_value=releases), \
                patch.object(bot, "send_telegram", side_effect=[None, RuntimeError("HTTP 503")]):
            with self.assertRaises(RuntimeError):
                bot.main()
        self.assertIn("2", self.saved()["repositories"]["one/repo"])
        self.assertNotIn("3", self.saved()["repositories"]["one/repo"])
        with patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", return_value=releases), \
                patch.object(bot, "send_telegram") as send:
            bot.main()
        self.assertEqual(send.call_count, 1)
        self.assertIn("v3", send.call_args.args[0])

    def test_real_github_http_error_uses_message_and_preserves_limit_headers(self):
        from io import BytesIO
        from urllib.error import HTTPError
        response = BytesIO(b'{"message":"You have exceeded a secondary rate limit"}')
        error = HTTPError(
            "https://api.github.com/repos/owner/repo", 403, "Forbidden",
            {"Retry-After": "65", "X-RateLimit-Remaining": "5"}, response,
        )
        with patch.object(bot, "urlopen", side_effect=error):
            with self.assertRaises(bot.APIError) as captured:
                bot.request_json("https://api.github.com/repos/owner/repo")
        self.assertTrue(bot.is_github_rate_limited(captured.exception))
        self.assertEqual(captured.exception.headers.get("retry-after"), "65")
        self.assertIn("secondary rate limit", captured.exception.description)

    def test_rate_limit_request_is_not_retried_in_same_run(self):
        error = bot.APIError(429, "secondary rate limit", {"retry-after": "120"})
        with patch.object(bot, "request_json", side_effect=error) as call, \
                patch.object(bot.time, "sleep") as sleep:
            with self.assertRaises(bot.GitHubRateLimited):
                bot.github_request_json("https://api.github.com/repos/owner/repo")
        call.assert_called_once()
        sleep.assert_not_called()

    def test_github_primary_limit_uses_reset_header(self):
        error = bot.APIError(403, "API rate limit exceeded", {
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": "3600",
        })
        self.assertTrue(bot.is_github_rate_limited(error))
        self.assertEqual(bot.github_retry_at(error, now=3000), 3602)

    def test_github_secondary_limit_uses_retry_after_or_minute(self):
        limited = bot.APIError(429, "secondary rate limit", {"Retry-After": "180"})
        self.assertTrue(bot.is_github_rate_limited(limited))
        self.assertEqual(bot.github_retry_at(limited, now=100), 281)
        fallback = bot.APIError(403, "You have exceeded a secondary rate limit")
        self.assertTrue(bot.is_github_rate_limited(fallback))
        self.assertEqual(bot.github_retry_at(fallback, now=100), 160)
        self.assertFalse(bot.is_github_rate_limited(bot.APIError(403, "Forbidden")))

    def test_github_server_failure_retried_only_once(self):
        temporary = bot.APIError(503, "Temporarily unavailable")
        with patch.object(bot, "request_json", side_effect=[temporary, {"ok": True}]) as api, \
                patch.object(bot.time, "sleep") as sleep:
            payload = bot.github_request_json("https://api.github.com/repos/owner/repo")
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(api.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_rate_limited_scan_skips_remaining_repositories_and_persists_cooldown(self):
        self.write_state()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=bot.GitHubRateLimited(4102444800)) as fetch, \
                patch.object(bot, "send_telegram") as send:
            bot.main()
        self.assertEqual(fetch.call_count, 1)
        send.assert_not_called()
        self.assertEqual(self.saved()["github_backoff_until"], 4102444800)

        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases") as fetch:
            bot.main()
        fetch.assert_not_called()
        self.assertEqual(self.saved()["github_backoff_until"], 4102444800)

    def test_expired_backoff_resumes_scan_and_clears_state(self):
        self.state["github_backoff_until"] = 100
        self.write_state()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]) as fetch:
            bot.main()
        self.assertEqual(fetch.call_count, 3)
        self.assertNotIn("github_backoff_until", self.saved())

    def test_list_latest_release_selects_publication_not_creation_and_all_types(self):
        current = [
            {"name": "Stable v2", "tag_name": "v2", "prerelease": False,
             "published_at": "2026-10-29T20:00:00Z",
             "html_url": "https://github.com/owner/repo/releases/tag/v2"},
            {"name": "3.0-alpha", "tag_name": "v3-alpha", "prerelease": True,
             "published_at": "2026-10-30T06:00:00Z",
             "html_url": "https://github.com/owner/repo/releases/tag/v3-alpha"},
            {"name": "Unreleased", "tag_name": "v9", "draft": True,
             "published_at": "2026-11-01T00:00:00Z"},
        ]
        # Even when GitHub returns stable first, the newest published alpha wins.
        self.assertEqual(bot.newest_published_release(current), {
            "tag": "v3-alpha",
            "url": "https://github.com/owner/repo/releases/tag/v3-alpha",
        })
        self.assertIsNone(bot.newest_published_release([]))
        self.assertIsNone(bot.newest_published_release([current[2]]))

    def test_latest_versions_catalog_uses_cloudflare_and_not_repo_state(self):
        self.write_state()
        original = self.state_file.read_bytes()
        catalog = {"owner/repo": {"tag": "1.7beta11",
                   "url": "https://github.com/owner/repo/releases/tag/1.7beta11"}}
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", True), \
                patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://worker.example.workers.dev/sync"), \
                patch.object(bot, "SYNC_SECRET", "test-sync"), \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                patch.object(bot, "request_json", return_value={"ok": True}) as api:
            bot.publish_cloudflare_catalog(catalog)
        self.assertEqual(api.call_args.args[0],
                         "https://worker.example.workers.dev/catalog")
        self.assertEqual(api.call_args.kwargs["payload"]["releases"], catalog)
        self.assertEqual(api.call_args.kwargs["headers"]["Authorization"], "Bearer test-sync")
        self.assertEqual(self.state_file.read_bytes(), original)

    def test_unavailable_catalog_endpoint_is_nonfatal(self):
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", True), \
                patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://worker.example.workers.dev/sync"), \
                patch.object(bot, "SYNC_SECRET", "test-sync"), \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}), \
                patch.object(bot, "request_json", side_effect=bot.APIError(404)):
            bot.publish_cloudflare_catalog({"owner/repo": {"tag": "v1", "url": ""}})

    def test_newest_release_tag_is_bounded_and_bad_url_is_not_sent(self):
        releases = [{"tag_name": "a" * 200, "prerelease": True,
                     "published_at": "2026-10-30T00:00:00Z",
                     "html_url": "https://evil.example/releases"}]
        value = bot.newest_published_release(releases)
        self.assertEqual(len(value["tag"]), 120)
        self.assertEqual(value["url"], "")

    def test_report_status_posts_to_cloudflare_without_persisting_history(self):
        self.write_state()
        original = self.state_file.read_bytes()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", True), \
                patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://monitor.example.workers.dev/sync"), \
                patch.object(bot, "SYNC_SECRET", "test-sync"), \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "1234"}), \
                patch.object(bot, "request_json", return_value={"ok": True}) as api:
            bot.publish_cloudflare_status("2026-10-28T20:10:00+00:00", 6, 6, 1, [], None)
        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertEqual(api.call_args.args[0], "https://monitor.example.workers.dev/report")
        payload = api.call_args.kwargs["payload"]
        self.assertEqual(payload["run_id"], "1234")
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["notifications"], 1)
        self.assertEqual(api.call_args.kwargs["headers"]["Authorization"], "Bearer test-sync")

    def test_report_rate_limit_and_errors_use_correct_status(self):
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", True), \
                patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://monitor.example.workers.dev/sync"), \
                patch.object(bot, "SYNC_SECRET", "test-sync"), \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "987"}), \
                patch.object(bot, "request_json", return_value={"ok": True}) as api:
            bot.publish_cloudflare_status("2026-10-28T20:10:00+00:00", 2, 6, 0, [], 4102444800)
            self.assertEqual(api.call_args.kwargs["payload"]["status"], "rate_limited")
            self.assertEqual(api.call_args.kwargs["payload"]["retry_at"], 4102444800)
            bot.publish_cloudflare_status("2026-10-28T20:10:00+00:00", 2, 6, 0, ["repo: APIError"], None)
            self.assertEqual(api.call_args.kwargs["payload"]["status"], "error")
            self.assertEqual(api.call_args.kwargs["payload"]["errors"], 1)

    def test_report_endpoint_failure_does_not_block_monitor(self):
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", True), \
                patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://monitor.example.workers.dev/sync"), \
                patch.object(bot, "SYNC_SECRET", "test-sync"), \
                patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "777"}), \
                patch.object(bot, "request_json", side_effect=bot.APIError(404)):
            bot.publish_cloudflare_status("2026-10-28T20:10:00+00:00", 6, 6, 0, [], None)

    def test_report_is_skipped_if_cloudflare_sync_is_disabled(self):
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "request_json") as api:
            bot.publish_cloudflare_status("2026-10-28T20:10:00+00:00", 6, 6, 0, [], None)
        api.assert_not_called()

    def test_status_summary_does_not_touch_state_file(self):
        self.write_state()
        original = self.state_file.read_bytes()
        from tempfile import NamedTemporaryFile
        with NamedTemporaryFile(mode="r+", encoding="utf-8") as outfile, \
                patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": outfile.name, "MONITOR_REPORT_ENABLED": "true"}), \
                patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]):
            bot.main()
            outfile.seek(0)
            markdown = outfile.read()
        self.assertEqual(self.state_file.read_bytes(), original)
        self.assertIn("Проверка выполнена", markdown)
        self.assertIn("Проверено репозиториев:", markdown)
        self.assertIn("3 / 3", markdown)

    def test_shadowmount_beta11_link_only_then_nine_changes(self):
        repo = "drakmor/ShadowMountPlus"
        baseline = {
            "id": 428410044, "tag_name": "1.7beta11",
            "name": "ShadowMountPlus 1.7beta11",
            "published_at": "2026-10-29T21:22:45Z",
            "body": "**Full Changelog**: https://github.com/drakmor/ShadowMountPlus/compare/1.7beta10...1.7beta11",
            "html_url": "https://github.com/drakmor/ShadowMountPlus/releases/tag/1.7beta11",
        }
        updated = {**baseline, "body": (
            "- Unified PKG installation queue with cover cards for PC, local storage and SMB.\n"
            "- Select up to 32 PC files; additional selections append without duplicates.\n"
            "- Automatic game → update → DLC ordering for PC batches.\n"
            "- Failed or cancelled queues retain unfinished packages.\n"
            "- Improved PS4 DLC and PS5 media PKG detection.\n"
            "- Fixed premature DLC installation failures (0x80E90004).\n"
            "- Fixed json-c memory leaks during repeated scans.\n"
            "- Added estimated remaining installation time.\n"
            "- Improved read-only destination handling.\n\n"
            "**Full Changelog**: https://github.com/drakmor/ShadowMountPlus/compare/1.7beta10...1.7beta11")}
        self.state["repositories"][repo] = ["428410044"]
        self.state["release_watch"] = {
            repo: {"428410044": bot.release_watch_entry(baseline, 99)}}
        now = bot.release_published_at(baseline)+180
        with patch.object(bot, "edit_release_notification") as edit, \
                patch.object(bot, "send_telegram") as notify:
            bot.process_release_edits(repo, [updated], self.state, lambda: None, now=now)
            edit.assert_not_called()
            bot.process_release_edits(repo, [updated], self.state, lambda: None, now=now+181)
            edit.assert_called_once()
            notify.assert_called_once()
            self.assertIn("9 новых пунктов", notify.call_args.args[0])

    def test_reverted_description_cancels_pending_without_alert(self):
        repo = "one/repo"
        initial = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                   "body": "Original body."}
        modified = {**initial, "body": "- First major extra note with new payloads.\n"
                                            "- Second major extra note with faster loads."}
        t = bot.release_published_at(initial) + 20
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(initial, 12)}}
        with patch.object(bot, "edit_release_notification") as edit, \
                patch.object(bot, "send_telegram") as alert:
            bot.process_release_edits(repo, [modified], self.state, lambda: None, now=t)
            bot.process_release_edits(repo, [initial], self.state, lambda: None, now=t+110)
            bot.process_release_edits(repo, [initial], self.state, lambda: None, now=t+500)
            edit.assert_not_called()
            alert.assert_not_called()
        self.assertNotIn("pending", self.state["release_watch"][repo]["1"])

    def test_image_only_edits_are_ignored(self):
        repo = "one/repo"
        first = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                 "body": ("- Changes to installer with better load times.\n"
                          "![Screenshot one](https://example.com/old.png)")}
        second = {**first, "body": ("- Changes to installer with better load times.\n"
                                    "![Different image](https://example.com/new.png)")}
        t = bot.release_published_at(first)+20
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(first, 12)}}
        before = copy.deepcopy(self.state)
        self.assertFalse(bot.process_release_edits(
            repo, [second], self.state, lambda: None, now=t))
        self.assertEqual(self.state, before)

    def test_main_records_telegram_id_for_new_release_and_does_not_repeat(self):
        self.write_state()
        # No external requests or real Telegram messages. Simulate the newer
        # published GitHub release being delivered in the normal monitor loop.
        from datetime import datetime, timezone
        published = datetime.now(timezone.utc).isoformat()
        updated = {**release(912), "name": "Major update",
                   "published_at": published,
                   "body": "- New installer support for large games."}
        def fetch(repo):
            original_id = int(self.state["repositories"][repo][0])
            if repo == "one/repo":
                return [updated, release(original_id)]
            return [release(original_id)]
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=fetch), \
                patch.object(bot, "send_telegram", return_value=12345) as send:
            bot.main()
            bot.main()
        self.assertEqual(send.call_count, 1)
        actual = self.saved()
        self.assertIn("912", actual["repositories"]["one/repo"])
        entry = actual["release_watch"]["one/repo"]["912"]
        self.assertEqual(entry["message_id"], 12345)
        self.assertTrue(entry["eligible"])
        self.assertFalse(entry["notified"])
        self.assertNotIn("pending", entry)

    def test_unchanged_recent_release_watch_does_not_commit_every_minute(self):
        self.write_state()
        from datetime import datetime, timezone
        fresh = {**release(1), "published_at": datetime.now(timezone.utc).isoformat()}
        self.state["release_watch"] = {"one/repo": {
            "1": bot.release_watch_entry(fresh, 123)}}
        self.write_state()
        original = self.state_file.read_bytes()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: (
                    [fresh] if repo == "one/repo" else
                    [release(int(self.state["repositories"][repo][0]))])), \
                patch.object(bot, "send_telegram") as send:
            bot.main()
            bot.main()
        self.assertEqual(self.state_file.read_bytes(), original)
        send.assert_not_called()

    def test_send_telegram_returns_message_id_for_later_edits(self):
        with patch.object(bot, "request_json", return_value={
                "ok": True, "result": {"message_id": 871}}) as api:
            self.assertEqual(bot.send_telegram("<b>Release</b>"), 871)
        self.assertIn("sendMessage", api.call_args.args[0])
        self.assertEqual(api.call_args.kwargs["payload"]["parse_mode"], "HTML")

    def test_release_edit_debounce_one_followup_and_silent_later_edits(self):
        repo = "one/repo"
        start = 2000000000
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "**Full Changelog**: https://github.com/owner/repo/compare/a...b"}
        # Pin time relative to an explicit published_at for deterministic tests.
        start = bot.release_published_at(original) + 10
        edited = {**original, "body": (
            "- Unified PKG installation queue for Windows and PS5 games.\n"
            "- Automatic game update DLC installation ordering now works.\n"
            "- Failed queues retain unfinished downloads for later retries.\n"
            "**Full Changelog**: https://github.com/owner/repo/compare/a...b")}
        second_edit = {**edited, "body": edited["body"] +
                       "\n- Installer now displays remaining download time."}
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(original, 871)}}
        self.assertTrue(self.state["release_watch"][repo]["1"]["eligible"])
        edits, notices = [], []
        with patch.object(bot, "edit_release_notification",
                          side_effect=lambda *args: edits.append(args)) as edit, \
                patch.object(bot, "send_telegram",
                             side_effect=lambda message: notices.append(message)):
            persist = lambda: None
            bot.process_release_edits(repo, [edited], self.state, persist, now=start)
            bot.process_release_edits(repo, [edited], self.state, persist, now=start+120)
            self.assertEqual(len(edits), 0)
            self.assertEqual(len(notices), 0)
            # At 3 minutes of stable content, one edit and one alert.
            bot.process_release_edits(repo, [edited], self.state, persist, now=start+180)
            self.assertEqual(len(edits), 1)
            self.assertEqual(len(notices), 1)
            self.assertIn("Дополнено описание:", notices[0])
            self.assertIn("3 новых пунктов", notices[0])
            bot.process_release_edits(repo, [edited], self.state, persist, now=start+220)
            self.assertEqual(len(edits), 1)
            self.assertEqual(len(notices), 1)
            bot.process_release_edits(repo, [second_edit], self.state, persist, now=start+240)
            bot.process_release_edits(repo, [second_edit], self.state, persist, now=start+421)
            self.assertEqual(len(edits), 2)
            # Later changes edit the original quietly, no extra notification.
            self.assertEqual(len(notices), 1)
        self.assertTrue(self.state["release_watch"][repo]["1"]["notified"])

    def test_edits_wait_for_three_minutes_since_last_change(self):
        repo = "one/repo"
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "Initial release note."}
        now = bot.release_published_at(original)+60
        a = {**original, "body": "- First meaningful change to installer.\n"
                                 "- Second meaningful change to packaging."}
        b = {**a, "body": a["body"] + "\n- Third meaningful change to metadata."}
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(original, 99)}}
        with patch.object(bot, "edit_release_notification") as edit, \
                patch.object(bot, "send_telegram") as alert:
            bot.process_release_edits(repo, [a], self.state, lambda: None, now=now)
            bot.process_release_edits(repo, [b], self.state, lambda: None, now=now+100)
            bot.process_release_edits(repo, [b], self.state, lambda: None, now=now+180)
            edit.assert_not_called()
            alert.assert_not_called()
            bot.process_release_edits(repo, [b], self.state, lambda: None, now=now+281)
            edit.assert_called_once()
            alert.assert_called_once()

    def test_cosmetic_edits_do_not_alert_or_write_state(self):
        repo = "one/repo"
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "- **Fixed** USB handling (supported on PS5)."}
        changed = {**original, "body":
                   "- Fixed USB handling (supported on PS5).\n"
                   "https://github.com/owner/repo/compare/a...b"}
        now = bot.release_published_at(original)+60
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(original, 99)}}
        before = copy.deepcopy(self.state)
        with patch.object(bot, "edit_release_notification") as edit, \
                patch.object(bot, "send_telegram") as alert:
            self.assertFalse(bot.process_release_edits(
                repo, [changed], self.state, lambda: None, now=now))
        self.assertEqual(self.state, before)
        edit.assert_not_called()
        alert.assert_not_called()

    def test_one_short_added_bullet_silently_updates_message(self):
        repo = "one/repo"
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "- Initial note about installing PKG with SMB."}
        edited = {**original, "body": original["body"] +
                  "\n- Added an extra small check for USB devices."}
        now = bot.release_published_at(original)+10
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(original, 91)}}
        with patch.object(bot, "edit_release_notification") as edit, \
                patch.object(bot, "send_telegram") as alert:
            bot.process_release_edits(repo, [edited], self.state, lambda: None, now=now)
            bot.process_release_edits(repo, [edited], self.state, lambda: None, now=now+181)
            edit.assert_called_once()
            alert.assert_not_called()

    def test_existing_releases_get_silent_baseline_without_backlog_spam(self):
        repo = "one/repo"
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "Old original note."}
        now = bot.release_published_at(original)+10
        with patch.object(bot, "send_telegram") as alert, \
                patch.object(bot, "edit_release_notification") as edit:
            bot.process_release_edits(repo, [original], self.state, lambda: None, now=now)
            watched = self.state["release_watch"][repo]["1"]
            self.assertFalse(watched["eligible"])
            updated = {**original, "body": (
                "- More features in version one for various users.\n"
                "- More changes for compatibility with PS5 firmware.\n"
                "- Improved many details for installer startup.")}
            bot.process_release_edits(repo, [updated], self.state, lambda: None, now=now+30)
            bot.process_release_edits(repo, [updated], self.state, lambda: None, now=now+230)
            edit.assert_not_called()
            alert.assert_not_called()

    def test_expired_release_watch_is_pruned_after_48_hours(self):
        repo = "one/repo"
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "Some description"}
        published = bot.release_published_at(original)
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(original, 91)}}
        self.assertTrue(bot.process_release_edits(
            repo, [original], self.state, lambda: None,
            now=published + bot.EDIT_WINDOW_SECONDS + 1))
        self.assertNotIn(repo, self.state["release_watch"])

    def test_failed_followup_retries_without_duplicate_edit(self):
        repo = "one/repo"
        original = {**release(1), "published_at": "2033-05-18T03:33:20Z",
                    "body": "Old note"}
        edited = {**original, "body": (
            "- Major correction in PS5 compatibility and firmware.\n"
            "- Added support for faster transfer from SMB shares.\n"
            "- Increased system stability and reduced crashes.")}
        now = bot.release_published_at(original)+10
        self.state["release_watch"] = {repo: {"1": bot.release_watch_entry(original, 91)}}
        writes = []
        with patch.object(bot, "edit_release_notification") as edit, \
                patch.object(bot, "send_telegram",
                             side_effect=[RuntimeError("Telegram offline"), 492]) as alert:
            bot.process_release_edits(repo, [edited], self.state, lambda: writes.append(1), now=now)
            with self.assertRaises(RuntimeError):
                bot.process_release_edits(repo, [edited], self.state, lambda: writes.append(1), now=now+181)
            self.assertEqual(edit.call_count, 1)
            self.assertTrue(self.state["release_watch"][repo]["1"]["notify_pending"])
            bot.process_release_edits(repo, [edited], self.state, lambda: writes.append(1), now=now+200)
            self.assertEqual(edit.call_count, 1)
            self.assertEqual(alert.call_count, 2)
        self.assertTrue(self.state["release_watch"][repo]["1"]["notified"])

    def test_edit_message_uses_existing_id_and_html(self):
        repo = "one/repo"
        origin = {**release(1), "published_at": "2033-05-18T03:33:20Z"}
        entry = bot.release_watch_entry(origin, 786)
        with patch.object(bot, "format_release", return_value="<b>Updated</b>"), \
                patch.object(bot, "edit_telegram_message") as edit:
            self.assertTrue(bot.edit_release_notification(repo, origin, entry, True))
        payload = edit.call_args.args[0]
        self.assertEqual(payload["message_id"], 786)
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    def test_no_new_release_keeps_state_file_byte_for_byte(self):
        self.write_state()
        original = self.state_file.read_bytes()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]), \
                patch.object(bot, "send_telegram") as send:
            bot.main()
        self.assertEqual(self.state_file.read_bytes(), original)
        send.assert_not_called()

    def test_schedule_without_real_changes_does_not_update_state_file(self):
        self.write_state()
        original = self.state_file.read_bytes()
        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}), \
                patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]):
            bot.main()
        self.assertEqual(self.state_file.read_bytes(), original)

    def test_real_new_release_still_updates_persisted_history(self):
        self.write_state()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(9), release(int(self.state["repositories"][repo][0]))
                ]), \
                patch.object(bot, "send_telegram") as send:
            bot.main()
        self.assertEqual(send.call_count, 3)
        saved = self.saved()
        self.assertIn("9", saved["repositories"]["one/repo"])
        self.assertIn("9", saved["repositories"]["two/repo"])
        self.assertIn("9", saved["repositories"]["three/repo"])
        self.assertIn("completed_at", saved["last_run"])

    def test_cloudflare_changes_save_even_with_no_new_release(self):
        self.write_state()
        original = self.state_file.read_bytes()
        with patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", True), \
                patch.object(bot, "synchronize_cloudflare_state", side_effect=lambda state:
                    state.update({"translate_enabled": True})), \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [
                    release(int(self.state["repositories"][repo][0]))
                ]):
            bot.main()
        self.assertNotEqual(self.state_file.read_bytes(), original)
        self.assertTrue(self.saved()["translate_enabled"])

    def test_history_outside_api_window_is_not_deleted(self):
        self.state["repositories"] = {"one/repo": ["1", "old"]}
        self.write_state()
        with patch.object(bot, "tg_updates", return_value=[]), \
                patch.object(bot, "fetch_releases", return_value=[release(1)]), \
                patch.object(bot, "send_telegram") as send:
            bot.main()
        self.assertIn("old", self.saved()["repositories"]["one/repo"])
        send.assert_not_called()

    def test_failed_update_does_not_block_release_scan_or_ack_later_updates(self):
        self.write_state()
        with patch.object(bot, "tg_updates", return_value=[message("/list"), message("/translate_on", 102)]), \
                patch.object(bot, "process_update", side_effect=RuntimeError("Telegram unavailable")) as process, \
                patch.object(bot, "fetch_releases", side_effect=lambda repo: [release(int(self.state["repositories"][repo][0]))]) as fetch:
            with self.assertRaises(RuntimeError):
                bot.main()
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(process.call_count, 1)
        self.assertEqual(self.saved()["telegram_offset"], 100)

    def test_corrupt_state_is_not_overwritten(self):
        self.state_file.write_text('{"repositories": []}', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            bot.main()
        self.assertEqual(self.state_file.read_text(), '{"repositories": []}')

    def test_missing_state_in_actions_fails_without_reinitializing(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(RuntimeError):
                bot.main()
        self.assertFalse(self.state_file.exists())

    def test_cloudflare_sync_preserves_release_history(self):
        snapshot = {"repositories": ["TWO/repo", "four/new"], "translate_enabled": True}
        with patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://example.workers.dev/sync"), \
                patch.object(bot, "SYNC_SECRET", "test-secret"), \
                patch.object(bot, "request_json", return_value=snapshot) as request:
            changed = bot.synchronize_cloudflare_state(self.state)
        self.assertTrue(changed)
        self.assertEqual(self.state["repositories"], {"TWO/repo": ["2"], "four/new": None})
        self.assertTrue(self.state["translate_enabled"])
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer test-secret")

    def test_cloudflare_invalid_response_does_not_overwrite_history(self):
        original = copy.deepcopy(self.state)
        for response in (
            {"repositories": [], "translate_enabled": "true"},
            {"repositories": ["one/repo", "ONE/REPO"], "translate_enabled": True},
            {"repositories": ["bad../repo"], "translate_enabled": True},
        ):
            with self.subTest(response=response), \
                    patch.object(bot, "CLOUDFLARE_SYNC_URL", "https://example.workers.dev/sync"), \
                    patch.object(bot, "SYNC_SECRET", "test-secret"), \
                    patch.object(bot, "request_json", return_value=response):
                with self.assertRaises(RuntimeError):
                    bot.synchronize_cloudflare_state(self.state)
                self.assertEqual(self.state, original)

    def test_release_headings_for_all_monitored_project_naming_styles(self):
        cases = [
            ("drakmor/ShadowMountPlus",
             {"name": "ShadowMountPlus 1.7beta10", "tag_name": "1.7beta10"},
             "ShadowMountPlus 1.7beta10"),
            ("itsPLK/ps5-y2jb-autoloader",
             {"name": "v1.1.0", "tag_name": "v1.1.0-ba879c8"},
             "ps5-y2jb-autoloader v1.1.0"),
            ("EchoStretch/kstuff-lite",
             {"name": "Kstuff Lite 1.11 Beta", "tag_name": "v1.11"},
             "Kstuff Lite 1.11 Beta"),
            ("mihawk-99/PS5_MihawkOS",
             {"name": "PS5_MihawkOS-v4.0.0-alpha-4", "tag_name": "v4.0.0-alpha-4"},
             "PS5_MihawkOS v4.0.0-alpha-4"),
            ("quer3q/ps5-dump-forge",
             {"name": "", "tag_name": "v0.4.2"},
             "ps5-dump-forge v0.4.2"),
            ("itsPLK/ps5-payload-manager",
             {"name": "v0.6.3", "tag_name": "v0.6.3"},
             "ps5-payload-manager v0.6.3"),
            ("TheMojoMan/oneplus-erhai-ubuntu",
             {"name": "erhai-ubuntu installation files (2026-10-18)",
              "tag_name": "installer-20261018"},
             "erhai-ubuntu installation files (2026-10-18)"),
            ("owner/test",
             {"name": "test", "tag_name": "v3.0"},
             "test v3.0"),
        ]
        for repo, release_data, expected in cases:
            with self.subTest(repo=repo, name=release_data["name"]):
                self.assertEqual(bot.release_display_title(repo, release_data), expected)

    def test_oneplus_ubuntu_release_notification_has_no_repeated_project_prefix(self):
        release_data = {
            "name": "erhai-ubuntu installation files (2026-10-18)",
            "tag_name": "installer-20261018",
            "body": "Ubuntu installer updated",
            "html_url": "https://github.com/TheMojoMan/oneplus-erhai-ubuntu/releases/tag/installer-20261018",
        }
        with patch.object(bot, "translate_changelog", return_value=(
                "Обновлён установщик Ubuntu", True, "Переведено автоматически (MyMemory)")):
            message = bot.format_release("TheMojoMan/oneplus-erhai-ubuntu", release_data, True)
        self.assertEqual(message.splitlines()[0],
                         "🚀 <b>Новая версия: erhai-ubuntu installation files (2026-10-18)</b>")
        self.assertNotIn("oneplus-erhai-ubuntu erhai-ubuntu", message)
        self.assertNotIn("Изменения на русском", message)
        self.assertNotIn("Переведено автоматически (MyMemory)", message)

    def test_release_notification_has_one_heading_without_duplicate_version(self):
        release_data = {"name": "ShadowMountPlus 1.7beta10",
                        "tag_name": "1.7beta10", "prerelease": False,
                        "body": "Safe USB eject",
                        "html_url": "https://github.com/drakmor/ShadowMountPlus/releases/tag/1.7beta10"}
        with patch.object(bot, "translate_changelog", return_value=(
                "Безопасное извлечение USB", True, "Переведено автоматически (MyMemory)")):
            rendered = bot.format_release("drakmor/ShadowMountPlus", release_data, True)
        self.assertEqual(rendered.splitlines()[0],
                         "🚀 <b>Новая версия: ShadowMountPlus 1.7beta10</b>")
        self.assertEqual(rendered.count("🚀 <b>"), 1)
        self.assertNotIn("drakmor/ShadowMountPlus</b>", rendered)
        self.assertNotIn("<b>ShadowMountPlus 1.7beta10</b> (1.7beta10)", rendered)
        self.assertNotIn("Изменения на русском:", rendered)
        self.assertNotIn("Переведено автоматически (MyMemory)", rendered)
        self.assertIn("Безопасное извлечение USB", rendered)

    def test_release_heading_escapes_upstream_html(self):
        rendered = bot.release_display_title(
            "owner/my-project", {"name": "<b>bad</b>", "tag_name": "v2"})
        self.assertEqual(rendered, "my-project <b>bad</b>")
        with patch.object(bot, "translate_changelog", return_value=("Ok", True, "")):
            message = bot.format_release(
                "owner/my-project", {"name": "<b>bad</b>", "tag_name": "v2",
                                    "body": "Ok"}, True)
        self.assertIn("my-project &lt;b&gt;bad&lt;/b&gt;", message)
        self.assertNotIn("my-project <b>bad</b>", message)

    def test_mihawkos_alpha5_github_readme_becomes_clean_highlights(self):
        # Faithful excerpt from the 19,491-character Alpha 5 release. Its
        # decorative SVG images and Markdown tables must never dominate alerts.
        start = '''
<p align="center"><picture><source media="(prefers-color-scheme: dark)"
srcset="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/docs/media/readme/banner-night.svg"><img
src="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/docs/media/readme/banner-day.svg"
alt="MihawkOS banner"></picture></p>

## MihawkOS is a passion project, not a piracy project.

- **Piracy is not condoned.**

The fifth alpha of MihawkOS runs Windows games on PS5.

## <picture><img src="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/docs/media/readme/orb-this-pc-day.svg"></picture> At a glance

| What's new |
| --- |
| <img src="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/themes/windows11/icons/original/games.svg"> **Big games with Steam open.** Steam's browser now pauses while you play. |
| <img src="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/themes/windows11/icons/original/drive.svg"> **Memory limits made for your console.** |
| <img src="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/themes/windows11/icons/original/document.svg"> **A warning, then a clean close, instead of a crash.** |
| <img src="https://raw.githubusercontent.com/mihawk-99/PS5_MihawkOS/v5.0.0-alpha-5/themes/windows11/icons/original/app-generic.svg"> **Fixes for the problems you reported.** |

## Big games with Steam open

More information follows.
'''
        source = start + "\n## Known limits\n" + ("A very long section.\n" * 400)
        cleaned = bot.normalize_release_markdown(source)
        self.assertNotIn("raw.githubusercontent.com", cleaned)
        self.assertNotIn("<picture>", cleaned)
        self.assertNotIn("<img", cleaned)
        self.assertNotIn("| --- |", cleaned)
        self.assertIn("- **Big games with Steam open.**", cleaned)
        self.assertIn("- **Memory limits made for your console.**", cleaned)
        selected = bot.summarize_release_markdown(cleaned)
        self.assertIn("At a glance", selected)
        self.assertIn("Fixes for the problems you reported.", selected)
        self.assertNotIn("Piracy is not condoned", selected)
        self.assertNotIn("A very long section", selected)
        self.assertIn("Подробности в релизе на GitHub", selected)
        rendered = bot.render_release_telegram(selected)
        self.assertIn("<b>At a glance</b>", rendered)
        self.assertIn("• <b>Big games with Steam open.</b>", rendered)
        self.assertNotIn("<picture>", rendered)
        self.assertNotIn("| --- |", rendered)
        self.assertNotIn("raw.githubusercontent.com", rendered)

    def test_mihawkos_alpha5_telegram_notification_keeps_substantive_changes(self):
        src = (
            "## At a glance\n"
            "| What's new |\n| --- |\n"
            "| <img src='https://example.com/badge.svg'> **Steam browser pauses.** New games load. |\n"
            "| **PS5 13.60 is supported.** No crash in the demo. |\n"
            "\n## Details\n" + "Long explanation.\n" * 400
        )
        with patch.object(bot, "translate_changelog", side_effect=lambda text: (
                text, True, "Переведено автоматически (MyMemory)")):
            result = bot.format_release("mihawk-99/PS5_MihawkOS", {
                "name": "PS5_MihawkOS-v5.0.0-alpha-5", "tag_name": "v5.0.0-alpha-5",
                "body": src, "prerelease": True,
                "html_url": "https://github.com/mihawk-99/PS5_MihawkOS/releases/tag/v5.0.0-alpha-5",
            }, True)
        self.assertIn("🧪 Pre-release", result)
        self.assertIn("<b>Новая версия: PS5_MihawkOS v5.0.0-alpha-5</b>", result)
        self.assertIn("Steam browser pauses.", result)
        self.assertIn("PS5 13.60 is supported.", result)
        self.assertNotIn("example.com/badge.svg", result)
        self.assertNotIn("| --- |", result)
        self.assertNotIn("Изменения на русском:", result)
        self.assertNotIn("Переведено автоматически (MyMemory)", result)
        self.assertLess(len(result), 4096)

    def test_markdown_tables_convert_to_readable_bullets(self):
        source = ("| Console | Warning | Old limit |\n"
                  "| :--- | ---: | --- |\n"
                  "| PS5 13.60 | 1,750 MiB | 1,800 MiB |\n"
                  "| PS5 Pro | 850 MiB | 990 MiB |")
        cleaned = bot.normalize_release_markdown(source)
        self.assertIn("PS5 13.60", cleaned)
        self.assertIn("1,750 MiB", cleaned)
        self.assertIn("Old limit: 990 MiB", cleaned)
        self.assertNotIn("| :--- |", cleaned)
        self.assertIn("• <b>PS5 13.60</b>", bot.render_release_telegram(cleaned))

    def test_github_media_keeps_video_and_attachment_but_omits_svg_icons(self):
        body = (
            '<img src="https://raw.githubusercontent.com/example/icon.svg" alt="icon">\n'
            '<video><source src="https://example.com/demo.mp4" type="video/mp4"></video>\n'
            '![UI example](https://example.com/screenshot.png)\n'
            'https://github.com/user-attachments/assets/97953c34-5b00-435f-bb4c-30240cb0d294'
        )
        cleaned = bot.normalize_release_markdown(body)
        rendered = bot.render_release_telegram(cleaned)
        self.assertNotIn("example/icon.svg", rendered)
        self.assertIn("https://example.com/demo.mp4", rendered)
        self.assertIn('href="https://example.com/screenshot.png"', rendered)
        self.assertIn("97953c34-5b00-435f-bb4c-30240cb0d294", rendered)

    def test_real_y2jb_release_russian_terminology_and_telegram_markup(self):
        body = "## Changelog:\r\n\r\n- Relapse: Switched to @notmaj0r's port\r\n- Autoloader: Fix socket readiness polling for elfldr, eliminating the 1-second startup delay\r\n- Performance: Removed network log server check\r\n- Updater optimizations\r\n\r\n**Full Changelog (since last stable release)**: https://github.com/itsPLK/ps5-y2jb-autoloader/compare/v1.0.4-35c4413...v1.1.0-ba879c8\r\n\r\n\r\nhttps://github.com/user-attachments/assets/97953c34-5b00-435f-bb4c-30240cb0d294\r\n\r\n\r\n---\r\n<details>\r\n<summary>Bundled Binaries</summary>\r\n\r\nThis build integrates the following external dependency versions:\r\n- [itsPLK/ps5-elfldr](https://github.com/itsPLK/ps5-elfldr): [v0.27-b57d0e9](https://github.com/itsPLK/ps5-elfldr/releases/tag/v0.27-b57d0e9)\r\n- [itsPLK/ps5-kexp](https://github.com/itsPLK/ps5-kexp): [v0.8-24cf6e5](https://github.com/itsPLK/ps5-kexp/releases/tag/v0.8-24cf6e5)\r\n- [itsPLK/ps5-unified-autoloader](https://github.com/itsPLK/ps5-unified-autoloader): [v0.1.6-e7093cd](https://github.com/itsPLK/ps5-unified-autoloader/releases/tag/v0.1.6-e7093cd)\r\n\r\n</details>\r\n"
        r = {
            "name": "v1.1.0", "tag_name": "v1.1.0-ba879c8",
            "html_url": "https://github.com/itsPLK/ps5-y2jb-autoloader/releases/tag/v1.1.0-ba879c8",
            "body": body,
        }
        # Fixture is the published upstream release body, without real API calls.
        with patch.object(bot, "translate_fragment", side_effect=lambda text, **kw: text):
            rendered = bot.format_release("itsPLK/ps5-y2jb-autoloader", r, True)
        self.assertIn("переход на версию, портированную @notmaj0r", rendered)
        self.assertIn("готовности сокета elfldr", rendered)
        self.assertIn("проверка сервера сетевого логирования", rendered)
        self.assertIn("Оптимизирован механизм обновления", rendered)
        self.assertIn("<b>Список изменений:</b>", rendered)
        self.assertIn("<b>Включённые бинарные компоненты</b>", rendered)
        self.assertIn("• <a href=", rendered)
        self.assertIn("Вложение к релизу GitHub</a>", rendered)
        self.assertIn("97953c34-5b00-435f-bb4c-30240cb0d294", rendered)
        self.assertIn("compare/v1.0.4-35c4413...v1.1.0-ba879c8", rendered)
        for unwanted in ("<details>", "</details>", "<summary>", "## Changelog", "**Full Changelog", "разъема", "разъёма"):
            self.assertNotIn(unwanted, rendered)

    def test_telegram_html_escapes_untrusted_text_preserves_links_and_code(self):
        source = '## Notes\n- **Fix** `PS5` & <script>bad</script> [release](https://github.com/example/repo)\n---'
        cleaned = bot.normalize_release_markdown(source)
        rendered = bot.render_release_telegram(cleaned)
        self.assertIn("<b>Notes</b>", rendered)
        self.assertIn("• <b>Fix</b>", rendered)
        self.assertIn("<code>PS5</code>", rendered)
        self.assertIn("&amp; bad", rendered)
        self.assertIn('<a href="https://github.com/example/repo">release</a>', rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("**Fix**", rendered)

    def test_attachment_survives_long_changelog_truncation(self):
        asset = "https://github.com/user-attachments/assets/97953c34-5b00-435f-bb4c-30240cb0d294"
        source = "Lorem ipsum\n" * 500 + "\n" + asset
        short = bot.trim_release_markdown(source, limit=250)
        self.assertIn(asset, short)
        self.assertIn("Описание сокращено", short)

    def test_translated_release_does_not_show_redundant_labels(self):
        upstream = {"name": "v1.2.0", "tag_name": "v1.2.0",
                    "body": "## Changelog:\n- Fixed socket handling"}
        with patch.object(bot, "translate_changelog", return_value=(
                "## Список изменений:\n- Исправлена работа сокета",
                True, "Переведено автоматически (MyMemory)")):
            rendered = bot.format_release("owner/repo", upstream, True)
        self.assertIn("Исправлена работа сокета", rendered)
        self.assertIn("<b>Список изменений:</b>", rendered)
        self.assertIn("Открыть релиз на GitHub", rendered)
        self.assertNotIn("Изменения на русском:", rendered)
        self.assertNotIn("Переведено автоматически (MyMemory)", rendered)
        self.assertNotIn("Что изменилось (оригинал)", rendered)

    def test_untranslated_release_keeps_clear_fallback_reason(self):
        upstream = {"name": "v1.2.0", "tag_name": "v1.2.0",
                    "body": "## Changelog:\n- Fixed socket handling"}
        with patch.object(bot, "translate_changelog", return_value=(
                "## Changelog:\n- Fixed socket handling",
                False, "Автоперевод недоступен")):
            rendered = bot.format_release("owner/repo", upstream, True)
        self.assertIn("Что изменилось (оригинал)", rendered)
        self.assertIn("Автоперевод недоступен", rendered)
        self.assertNotIn("Изменения на русском:", rendered)
        self.assertNotIn("Переведено автоматически (MyMemory)", rendered)

    def test_fallback_original_still_gets_markdown_formatting(self):
        content = "## Changelog\n- **Fixed** `elfldr`"
        with patch.object(bot, "translate_fragment", side_effect=RuntimeError("unavailable")):
            rendered = bot.format_release(
                "owner/repo", {"tag_name": "v1.0", "body": content}, True)
        self.assertIn("<b>Changelog</b>", rendered)
        self.assertIn("• <b>Fixed</b> <code>elfldr</code>", rendered)
        self.assertIn("Автоперевод недоступен", rendered)

    def test_translation_deadline_falls_back_to_original(self):
        with patch.object(bot.time, "monotonic", side_effect=[0, 31]), \
                patch.object(bot, "translate_fragment") as translate:
            text, translated, _ = bot.translate_changelog("Fixed startup crash")
        self.assertFalse(translated)
        self.assertEqual(text, "Fixed startup crash")
        translate.assert_not_called()

    def test_translation_error_keeps_original(self):
        with patch.object(bot, "translate_fragment", side_effect=RuntimeError("Quota exhausted")):
            text, translated, _ = bot.translate_changelog("Fixed PS5 startup crash")
        self.assertFalse(translated)
        self.assertEqual(text, "Fixed PS5 startup crash")

    def test_translation_preserves_terms(self):
        with patch.object(bot, "translate_fragment", side_effect=lambda s, **kw: s.replace("Fixed", "Исправлено")):
            text, translated, _ = bot.translate_changelog("Fixed PS5 FPKG WebKit SMB3 DXVK `payload.elf`")
        self.assertTrue(translated)
        self.assertEqual(text, "Исправлено PS5 FPKG WebKit SMB3 DXVK `payload.elf`")


if __name__ == "__main__":
    unittest.main()
