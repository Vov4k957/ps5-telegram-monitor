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
