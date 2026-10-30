"""Regression scenarios from the October 2026 review; no real network calls."""
import contextlib
import hashlib
import io
import json
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import check_releases as bot


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.repo = "owner/project"
        self.release = {
            "id": 1, "tag_name": "v1", "body": "Initial release.",
            "published_at": "2026-10-29T21:00:00Z",
            "html_url": "https://github.com/owner/project/releases/tag/v1",
        }
        self.now = bot.release_published_at(self.release) + 60
        self.state = {
            "repositories": {self.repo: ["1"]}, "translate_enabled": False,
            "release_watch": {self.repo: {"1": bot.release_watch_entry(self.release, 99)}},
        }
        self.network = patch.object(bot, "urlopen", side_effect=AssertionError("Unexpected network access"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def process(self, release, now):
        return bot.process_release_edits(self.repo, [release], self.state, lambda: None, now=now)

    def expanded(self):
        return {**self.release, "body": (
            "- Added firmware compatibility for additional PS5 versions.\n"
            "- Fixed a critical crash during package installation.")}

    def test_truncation_closes_valid_source_code_fence(self):
        source = "## Setup\n```sh\n" + "echo install component\n" * 150 + "```\n"
        rendered = bot.format_release(self.repo, {**self.release, "body": source}, False)
        ET.fromstring("<root>" + rendered + "</root>")
        self.assertEqual(rendered.count("<pre>"), rendered.count("</pre>"))
        self.assertIn("Открыть релиз на GitHub</a>", rendered)

    def test_final_translated_text_is_bounded_without_losing_release_link(self):
        translated = "**Исправлена установка 🚀 & компоненты.**\n" * 200
        with patch.object(bot, "translate_changelog", return_value=(translated, True, "")):
            result = bot.format_release(self.repo, {**self.release, "name": "Версия 🚀" * 200})
        ET.fromstring("<root>" + result + "</root>")
        self.assertLessEqual(bot.telegram_text_length(bot.TelegramHTML(result).plain_text), 4096)
        self.assertIn('href="' + self.release["html_url"] + '"', result)
        self.assertTrue(result.endswith("Открыть релиз на GitHub</a>"))
        self.assertIn("…", result)

    def test_html_boundary_counts_entities_and_keeps_tags_balanced(self):
        result = bot.bound_telegram_html('<b>🙂&amp;А<i>БВГ</i></b>', 5)
        ET.fromstring("<root>" + result + "</root>")
        self.assertEqual(bot.TelegramHTML(result).plain_text, "🙂&А…")
        self.assertEqual(bot.telegram_text_length(bot.TelegramHTML(result).plain_text), 5)

    def test_rejected_html_send_falls_back_to_plain_text_and_retains_id(self):
        with patch.object(bot, "request_json", side_effect=[
            bot.APIError(400, "Bad Request: can't parse entities"),
            {"ok": True, "result": {"message_id": 123}},
        ]) as request:
            self.assertEqual(bot.send_telegram("<b>Изменения &amp; исправления</b>"), 123)
        fallback = request.call_args_list[1].kwargs["payload"]
        self.assertNotIn("parse_mode", fallback)
        self.assertEqual(fallback["text"], "Изменения & исправления")

    def test_length_error_plain_fallback_is_bounded(self):
        with patch.object(bot, "request_json", side_effect=[
            bot.APIError(400, "Bad Request: message is too long"),
            {"ok": True, "result": {"message_id": 123}},
        ]) as request:
            bot.send_telegram("<b>" + "🚀" * 5000 + "</b>")
        fallback = request.call_args_list[1].kwargs["payload"]
        self.assertNotIn("parse_mode", fallback)
        self.assertLessEqual(bot.telegram_text_length(fallback["text"]), 4096)

    def test_transport_error_is_not_blindly_retried(self):
        with patch.object(bot, "request_json", side_effect=RuntimeError("lost response")) as request:
            with self.assertRaises(RuntimeError):
                bot.send_telegram("<b>Release</b>")
        self.assertEqual(request.call_count, 1)

    def test_plain_fallback_keeps_final_release_url_when_text_is_trimmed(self):
        text = "<b>" + "🚀 исправления " * 1000 + "</b>"
        text += '<a href="' + self.release["html_url"] + '">Открыть релиз</a>'
        with patch.object(bot, "request_json", side_effect=[
            bot.APIError(400, "Bad Request: can't parse entities"),
            {"ok": True, "result": {"message_id": 123}},
        ]) as request:
            bot.send_telegram(text)
        fallback = request.call_args_list[1].kwargs["payload"]
        self.assertTrue(fallback["text"].endswith(self.release["html_url"]))
        self.assertLessEqual(bot.telegram_text_length(fallback["text"]), 4096)

    def test_edit_format_fallback_preserves_original_message_id(self):
        changed = self.expanded()
        self.process(changed, self.now)
        with patch.object(bot, "request_json", side_effect=[
            bot.APIError(400, "Bad Request: can't parse entities"), {"ok": True},
        ]) as request, patch.object(bot, "send_telegram", return_value=100) as notify:
            self.process(changed, self.now + 181)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn("parse_mode", request.call_args.kwargs["payload"])
        self.assertEqual(self.state["release_watch"][self.repo]["1"]["message_id"], 99)
        self.assertEqual(notify.call_count, 1)

    def test_unrecognized_400_preserves_pending_edit_for_retry(self):
        changed = self.expanded()
        original_digest = self.state["release_watch"][self.repo]["1"]["digest"]
        self.process(changed, self.now)
        with patch.object(bot, "request_json", side_effect=[
            bot.APIError(400, "Bad Request: unexpected temporary condition"), {"ok": True},
        ]) as request, patch.object(bot, "send_telegram", return_value=100) as notify:
            with self.assertRaises(bot.APIError):
                self.process(changed, self.now + 181)
            entry = self.state["release_watch"][self.repo]["1"]
            self.assertEqual(entry["message_id"], 99)
            self.assertEqual(entry["digest"], original_digest)
            self.assertIn("pending", entry)
            notify.assert_not_called()
            self.process(changed, self.now + 240)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(notify.call_count, 1)
        self.assertNotIn("pending", entry)

    def test_missing_original_message_does_not_stop_followup(self):
        changed = self.expanded()
        self.process(changed, self.now)
        with patch.object(bot, "request_json", side_effect=bot.APIError(400, "Bad Request: message to edit not found")), \
                patch.object(bot, "send_telegram", return_value=100) as notify:
            self.process(changed, self.now + 181)
        self.assertIsNone(self.state["release_watch"][self.repo]["1"]["message_id"])
        self.assertEqual(notify.call_count, 1)

    def test_late_notes_beyond_8500_characters_and_40_bullets_are_detected(self):
        body = "General installation documentation for this project. " * 190
        body += "\n" + "\n".join(f"- Existing compatibility detail number {n} for the installer." for n in range(45))
        original = {**self.release, "body": body}
        changed = {**original, "body": body + "\n" + self.expanded()["body"]}
        self.state["release_watch"][self.repo]["1"] = bot.release_watch_entry(original, 99)
        with patch.object(bot, "edit_release_notification") as edit, patch.object(bot, "send_telegram") as notify:
            self.process(changed, self.now)
            self.process(changed, self.now + 181)
            self.process(changed, self.now + 240)
        self.assertEqual(edit.call_count, 1)
        self.assertEqual(notify.call_count, 1)
        entry = self.state["release_watch"][self.repo]["1"]
        self.assertEqual(len(entry["body"]), 8500)
        self.assertGreater(entry["body_length"], 8500)
        self.assertGreater(len(entry["bullets"]), 40)

    def test_growth_uses_full_length_instead_of_preview(self):
        old = bot.release_edit_fingerprint("alpha " * 2000)
        small = bot.release_edit_fingerprint("alpha " * 2100)
        large = bot.release_edit_fingerprint("alpha " * 3100)
        self.assertFalse(bot.substantially_expanded(old, small))
        self.assertTrue(bot.substantially_expanded(old, large))

    def test_legacy_fingerprint_migration_is_silent_and_keeps_delivery_metadata(self):
        original = {**self.release, "body": "Installation documentation with important context. " * 250}
        text, bullets = bot.canonical_release_text(original["body"])
        legacy = {"digest": hashlib.sha256(text[:8500].encode()).hexdigest(), "body": text[:8500],
                  "bullets": bullets[:40], "message_id": 99, "eligible": True, "notified": True}
        self.state["release_watch"][self.repo]["1"] = legacy
        with patch.object(bot, "edit_release_notification") as edit, patch.object(bot, "send_telegram") as notify:
            self.assertTrue(self.process(original, self.now))
            self.assertFalse(self.process(original, self.now + 240))
        edit.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(legacy["message_id"], 99)
        self.assertTrue(legacy["notified"])
        self.assertEqual(legacy["fingerprint_version"], 2)

    def test_long_code_release_and_next_release_are_both_delivered(self):
        published = datetime.now(timezone.utc).isoformat()
        second = {**self.release, "id": 2, "tag_name": "v2", "published_at": published,
                  "body": "## Setup\n```sh\n" + "echo install component\n" * 150 + "```\n"}
        third = {**second, "id": 3, "tag_name": "v3", "body": "Fixed installation."}
        def send(text):
            ET.fromstring("<root>" + text + "</root>")
            return 100
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps({"repositories": {self.repo: ["1"]},
                "translate_enabled": False, "commands_menu_scope_cleared": True}))
            with patch.object(bot, "STATE_FILE", state_file), patch.object(bot, "TOKEN", "test-token"), \
                    patch.object(bot, "CHAT_ID", "42"), patch.object(bot, "CLOUDFLARE_SYNC_ENABLED", False), \
                    patch.object(bot, "tg_updates", return_value=[]), \
                    patch.object(bot, "fetch_releases", return_value=[third, second, self.release]), \
                    patch.object(bot, "send_telegram", side_effect=send) as sent, \
                    contextlib.redirect_stdout(io.StringIO()):
                bot.main()
                bot.main()
            self.assertEqual(sent.call_count, 2)
            self.assertEqual(json.loads(state_file.read_text())["repositories"][self.repo], ["1", "2", "3"])

    def test_shared_sync_validation_accepts_dynamic_counts_and_rejects_invalid_data(self):
        for count in (0, 6, 8, 30):
            for translated in (True, False):
                payload = {"repositories": [f"owner/repo{n}" for n in range(count)], "translate_enabled": translated}
                self.assertEqual(bot.validate_cloudflare_sync_payload(payload), (payload["repositories"], translated))
        for repos in ([f"owner/repo{n}" for n in range(31)], ["Owner/Repo", "owner/repo"], ["bad"], [None]):
            with self.assertRaises(RuntimeError):
                bot.validate_cloudflare_sync_payload({"repositories": repos, "translate_enabled": True})

    def test_service_workflow_scripts_accept_current_eight_subscriptions(self):
        root = Path(__file__).parent
        for name in ("activate-telegram-webhook.yml", "cloudflare-sync-check.yml"):
            with self.subTest(workflow=name):
                text = (root / ".github" / "workflows" / name).read_text()
                source = text.split("          python3 - <<'PY'\n", 1)[1].split("          PY", 1)[0]
                payload = {"repositories": [f"owner/repo{n}" for n in range(8)], "translate_enabled": False}
                calls = []
                def respond(request, **kwargs):
                    calls.append(request.full_url)
                    if request.full_url.endswith("/sync"):
                        result = payload
                    elif request.full_url.endswith("/setWebhook"):
                        result = {"ok": True, "result": True}
                    elif request.full_url.endswith("/getWebhookInfo"):
                        result = {"ok": True, "result": {"url": "https://ps5-telegram-webhook.vov4k95777.workers.dev/telegram"}}
                    else:
                        raise AssertionError("Unexpected API request")
                    return io.BytesIO(json.dumps(result).encode())
                with patch.dict("os.environ", {"SYNC_SECRET": "test-sync-secret", "WEBHOOK_SECRET": "test-webhook-secret-123", "TELEGRAM_BOT_TOKEN": "test-token"}), \
                        patch("urllib.request.urlopen", side_effect=respond), contextlib.redirect_stdout(io.StringIO()):
                    exec(compile(textwrap.dedent(source), name, "exec"), {})
                self.assertGreaterEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
