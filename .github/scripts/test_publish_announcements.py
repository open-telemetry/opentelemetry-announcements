import json
from http.client import IncompleteRead
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

import check_announcements as check
import publish_announcements as publish


ANNOUNCEMENT = """\
subject: OpenTelemetry news
text: |
  Read our latest news: https://opentelemetry.io/blog/2026/news/
bluesky: Short news for Bluesky https://opentelemetry.io/blog/2026/news/
image: news.png
alt: The OpenTelemetry logo
follow_ups:
  - publish_at: "2099-01-01T16:00:00Z"
    text: Reminder https://opentelemetry.io/blog/2026/news/
  - publish_at: "2099-01-02T16:00:00Z"
    channels: [mastodon]
    text: Last call
"""
SIMPLE = "subject: OpenTelemetry news\ntext: Simple news\n"
TARGETS = {
    "linkedin": ["linkedin-id"], "bluesky": ["bluesky-id"],
    "mastodon": ["mastodon-id"], "slack": ["C123", "C456"],
    "groupsio": [123, 456],
}
WEBHOOK = "https://hooks.slack.com/triggers/T000/123/test"
CREDS = {
    "BUFFER_API_KEY": "test-value", "GROUPSIO_API_KEY": "test-value",
    "SLACK_WEBHOOK_URL": WEBHOOK,
}
IMAGE_BASE = "https://raw.githubusercontent.com/open-telemetry/opentelemetry-announcements/abc/"


class Files:
    """A temporary repository root with announcement files."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def write(self, content=ANNOUNCEMENT, name="announcements/2026/news.yaml"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        (path.parent / "news.png").write_bytes(b"png")
        return path

    def load(self, content=ANNOUNCEMENT):
        path = self.write(content)
        return path, publish.load(path, now=check.datetime.now(check.timezone.utc),
                                  report=check.Reporter())

    def plan(self, mode="publish", content=ANNOUNCEMENT, targets=None, channels=None):
        if channels is not None:
            content = f"channels: {json.dumps(channels)}\n" + content
        return publish.build_plan(
            [self.load(content)], TARGETS if targets is None else targets, mode,
            image_base=IMAGE_BASE, root=self.root,
        )


class TestCase(unittest.TestCase):
    def setUp(self):
        self.files = Files()
        self.addCleanup(self.files.directory.cleanup)
        # Keep check output out of the test output.
        patcher = patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, *args, **kwargs):
        return self.files.plan(*args, **kwargs)


def publisher(credentials=None):
    return publish.Publisher(CREDS if credentials is None else credentials)


def buffer_channel(channel="linkedin", target="linkedin-id"):
    return {"data": {"channel": {
        "id": target, "service": channel, "isLocked": False, "isDisconnected": False,
    }}}


def buffer_post(status="sending", post_id="post-id"):
    return {"data": {"createPost": {"post": {"id": post_id, "status": status}}}}


class PlanTests(TestCase):
    def test_posts_channels_and_destinations(self):
        items = self.plan()
        first = [item for item in items if item["post"] == 0]
        self.assertEqual(
            [(item["channel"], item["target"]) for item in first],
            [("bluesky", "bluesky-id"), ("mastodon", "mastodon-id"), ("linkedin", "linkedin-id"),
             ("slack", "C123"), ("slack", "C456"), ("groupsio", 123), ("groupsio", 456)],
        )
        self.assertEqual([item["channel"] for item in items if item["post"] == 1],
                         ["bluesky", "mastodon", "linkedin"])
        self.assertEqual([item["channel"] for item in items if item["post"] == 2], ["mastodon"])
        self.assertTrue(all(item["action"] == "publish" for item in items))
        self.assertTrue(all(item["file"] == "announcements/2026/news.yaml" for item in items))

    def test_channel_text_subject_and_schedule(self):
        items = {(item["post"], item["channel"]): item for item in self.plan()}
        self.assertEqual(items[0, "bluesky"]["text"],
                         "Short news for Bluesky https://opentelemetry.io/blog/2026/news/")
        self.assertEqual(items[0, "mastodon"]["text"],
                         "Read our latest news: https://opentelemetry.io/blog/2026/news/")
        self.assertEqual(items[0, "groupsio"]["subject"], "OpenTelemetry news")
        self.assertNotIn("subject", items[0, "slack"])
        self.assertNotIn("publish_at", items[0, "linkedin"])
        self.assertEqual(items[1, "linkedin"]["publish_at"], "2099-01-01T16:00:00Z")
        self.assertEqual(items[2, "mastodon"]["text"], "Last call")

    def test_images_only_for_buffer_channels(self):
        items = {(item["post"], item["channel"]): item for item in self.plan()}
        for channel in publish.BUFFER_CHANNELS:
            self.assertEqual(items[0, channel]["image_url"],
                             IMAGE_BASE + "announcements/2026/news.png")
            self.assertEqual(items[0, channel]["alt"], "The OpenTelemetry logo")
            self.assertNotIn("image", items[1, channel])
        for channel in ["slack", "groupsio"]:
            self.assertNotIn("image", items[0, channel])

    def test_image_url_requires_repository(self):
        loaded = self.files.load()
        with self.assertRaisesRegex(publish.DeliveryError, "GITHUB_REPOSITORY"):
            publish.build_plan([loaded], TARGETS, "publish", root=self.files.root)
        items = publish.build_plan([loaded], {}, "check", root=self.files.root)
        self.assertTrue(all("image_url" not in item for item in items))

    def test_check_mode_only_previews_without_targets(self):
        items = self.plan("check", targets={})
        self.assertTrue(all(item["action"] == "preview" for item in items))
        self.assertTrue(all(item["target"] is None for item in items))
        self.assertEqual(len(items), 9)

    def test_draft_mode_previews_slack(self):
        items = self.plan("draft")
        for item in items:
            self.assertEqual(item["action"], "preview" if item["channel"] == "slack" else "draft")

    def test_selected_channels(self):
        items = self.plan(channels=["slack"], content=SIMPLE)
        self.assertEqual([item["channel"] for item in items], ["slack", "slack"])

    def test_missing_targets(self):
        with self.assertRaisesRegex(publish.DeliveryError, "No targets configured for groupsio"):
            self.plan(targets={key: value for key, value in TARGETS.items() if key != "groupsio"})

    def test_too_many_deliveries(self):
        targets = {**TARGETS, "slack": [f"C{index}" for index in range(50)]}
        with self.assertRaisesRegex(publish.DeliveryError, "At most"):
            self.plan(targets=targets)

    def test_invalid_mode(self):
        with self.assertRaises(publish.DeliveryError):
            self.plan("dry-run")

    def test_invalid_announcements_are_rejected(self):
        cases = [
            "text: [\n",  # invalid YAML
            "text: Hi\n",  # groupsio needs a subject
            "subject: Hi\ntext: Hi\nreddit: Hi\n",
            "subject: Hi\ntext: " + "a" * 301 + "\n",  # too long for Bluesky
            "subject: Hi\ntext: Hi\nimage: missing.png\nalt: Missing\n",
            "subject: Hi\ntext: Hi\nfollow_ups:\n  - publish_at: '2020-01-01T00:00:00Z'\n    text: Hi\n",
        ]
        for content in cases:
            with self.subTest(content=content), self.assertRaises(publish.DeliveryError):
                self.files.load(content)

    def test_targets(self):
        self.assertEqual(publish.parse_targets(""), {})
        self.assertEqual(publish.parse_targets(json.dumps(TARGETS)), TARGETS)
        cases = [
            "not json", "[]", '{"reddit": ["x"]}', '{"slack": []}',
            '{"groupsio": [true]}', '{"groupsio": ["123"]}', '{"groupsio": [0]}',
            '{"slack": ["general"]}', '{"slack": ["#announcements"]}', '{"slack": ["C123", "C123"]}',
            '{"linkedin": ["abc", "ABC"]}', '{"linkedin": ["same"], "bluesky": ["same"]}',
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(publish.DeliveryError):
                publish.parse_targets(value)

    def test_resolve_only_allows_announcement_files(self):
        path = self.files.write()
        with patch.object(publish, "ROOT", self.files.root):
            self.assertEqual(publish.resolve([str(path), str(path)], check_only=False), [path])
            example = self.files.write(name="examples/news.yaml")
            self.assertEqual(publish.resolve([str(example)], check_only=True), [example])
            for name in [str(example), str(path.parent / "news.png"), "/etc/passwd",
                         str(self.files.root / "announcements/missing.yaml")]:
                with self.subTest(name=name), self.assertRaises(publish.DeliveryError):
                    publish.resolve([name], check_only=False)

    def test_added_since_without_previous_commit(self):
        self.assertEqual(publish.added_since("0" * 40), [])


class PreflightTests(TestCase):
    @patch("publish_announcements.request")
    def test_check_never_makes_requests_or_requires_secrets(self, request):
        items = self.plan("check")
        self.assertTrue(publisher({}).run(items))
        self.assertTrue(all(item["status"] == "preview" for item in items))
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_draft_slack_preview_without_credentials(self, request):
        items = [item for item in self.plan("draft") if item["channel"] == "slack"]
        self.assertTrue(publisher({}).run(items))
        self.assertTrue(all(item["status"] == "preview" for item in items))
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_missing_secret_prevents_all_requests(self, request):
        with self.assertRaisesRegex(publish.DeliveryError, "GROUPSIO_API_KEY, SLACK_WEBHOOK_URL"):
            publisher({"BUFFER_API_KEY": "test"}).run(self.plan())
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_invalid_credentials_fail_without_exposing_them(self, request):
        with self.assertRaises(publish.DeliveryError) as failure:
            publisher({**CREDS, "SLACK_WEBHOOK_URL": "sensitive\nvalue"}).run(self.plan())
        self.assertIn("SLACK_WEBHOOK_URL", str(failure.exception))
        self.assertNotIn("sensitive", str(failure.exception))
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_slack_webhook_must_be_a_workflow_webhook(self, request):
        for url in ["https://example.com/hook", "http://hooks.slack.com/triggers/x",
                    "https://hooks.slack.com.example.com/x",
                    "https://hooks.slack.com/services/T000/B000/x"]:
            with self.subTest(url=url), self.assertRaises(publish.DeliveryError) as failure:
                publisher({**CREDS, "SLACK_WEBHOOK_URL": url}).run(self.plan())
            self.assertNotIn(url, str(failure.exception))
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_buffer_channels_checked_once_before_writes(self, request):
        request.side_effect = [
            buffer_channel("bluesky", "bluesky-id"), buffer_channel("mastodon", "mastodon-id"),
            buffer_channel("linkedin", "bluesky-id"),
        ]
        with self.assertRaisesRegex(publish.DeliveryError, "does not match"):
            publisher().run(self.plan())
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.args[1]["query"].startswith("query")
                            for call in request.call_args_list))

    @patch("publish_announcements.request")
    def test_buffer_locked_or_disconnected_fails_before_writes(self, request):
        for field in ["isLocked", "isDisconnected"]:
            with self.subTest(field=field):
                data = buffer_channel("bluesky", "bluesky-id")
                data["data"]["channel"][field] = True
                request.reset_mock()
                request.side_effect = [data]
                with self.assertRaises(publish.DeliveryError):
                    publisher().run(self.plan())
                self.assertEqual(request.call_count, 1)


class DeliveryTests(TestCase):
    def buffer_items(self, mode="publish"):
        return [item for item in self.plan(mode) if item["channel"] == "linkedin"]

    @patch("publish_announcements.request")
    def test_buffer_first_post_and_follow_up(self, request):
        request.side_effect = [buffer_channel(), buffer_post("sending", "first"),
                               buffer_post("scheduled", "follow-up")]
        items = self.buffer_items()
        self.assertTrue(publisher().run(items))
        first, follow_up = [call.args[1]["variables"]["input"] for call in request.call_args_list[1:]]
        self.assertEqual(first["mode"], "shareNow")
        self.assertFalse(first["saveToDraft"])
        self.assertNotIn("dueAt", first)
        self.assertEqual(first["assets"], [{"image": {
            "url": IMAGE_BASE + "announcements/2026/news.png",
            "metadata": {"altText": "The OpenTelemetry logo"},
        }}])
        self.assertEqual(follow_up["mode"], "customScheduled")
        self.assertEqual(follow_up["dueAt"], "2099-01-01T16:00:00Z")
        self.assertNotIn("assets", follow_up)
        self.assertEqual([item["status"] for item in items], ["submitted", "scheduled"])
        self.assertEqual([item["remote_id"] for item in items], ["first", "follow-up"])

    @patch("publish_announcements.request")
    def test_buffer_drafts(self, request):
        request.side_effect = [buffer_channel(), buffer_post("draft"), buffer_post("draft")]
        items = self.buffer_items("draft")
        self.assertTrue(publisher().run(items))
        first, follow_up = [call.args[1]["variables"]["input"] for call in request.call_args_list[1:]]
        self.assertTrue(first["saveToDraft"])
        self.assertEqual(first["mode"], "addToQueue")
        self.assertTrue(follow_up["saveToDraft"])
        self.assertEqual(follow_up["mode"], "customScheduled")
        self.assertEqual([item["status"] for item in items], ["drafted", "drafted"])

    @patch("publish_announcements.request")
    def test_buffer_errors_fail_the_delivery(self, request):
        for error in [
            {"errors": [{"message": "invalid query"}]},
            {"data": {"createPost": {"message": "Too long"}}},
            buffer_post("error"),
            buffer_post("sent"),  # a follow-up must be scheduled, not sent
        ]:
            with self.subTest(error=error):
                request.side_effect = [buffer_channel(), buffer_post(), error]
                items = self.buffer_items()
                self.assertFalse(publisher().run(items))
                self.assertEqual([item["status"] for item in items], ["submitted", "failed"])

    @patch("publish_announcements.request")
    def test_mixed_draft_run_never_publishes(self, request):
        request.side_effect = [
            buffer_channel("bluesky", "bluesky-id"),
            buffer_channel("mastodon", "mastodon-id"),
            buffer_channel("linkedin", "linkedin-id"),
            *[buffer_post("draft", f"post-{index}") for index in range(3)],
            {"object": "draft", "id": 12}, {"object": "draft", "id": 12},
            {"object": "draft", "id": 13}, {"object": "draft", "id": 13},
            *[buffer_post("draft", f"post-{index}") for index in range(4)],
        ]
        items = self.plan("draft")
        self.assertTrue(publisher({"BUFFER_API_KEY": "b", "GROUPSIO_API_KEY": "g"}).run(items))
        self.assertEqual(sum(item["status"] == "drafted" for item in items), 9)
        self.assertEqual(sum(item["status"] == "preview" for item in items), 2)
        for call in request.call_args_list:
            self.assertNotIn("slack", call.args[0])
            self.assertFalse(call.args[0].endswith("/postdraft"))
            if "input" in call.args[1].get("variables", {}):
                self.assertTrue(call.args[1]["variables"]["input"]["saveToDraft"])

    @patch("publish_announcements.request")
    def test_slack_posts_to_each_channel(self, request):
        request.side_effect = ['{"ok":true}', "ok"]
        items = [item for item in self.plan(content=ANNOUNCEMENT + "slack: Hi <!channel> & <@U123>\n")
                 if item["channel"] == "slack"]
        self.assertTrue(publisher().run(items))
        self.assertEqual([call.args[0] for call in request.call_args_list], [WEBHOOK, WEBHOOK])
        self.assertEqual([call.args[1]["channel"] for call in request.call_args_list], ["C123", "C456"])
        self.assertTrue(request.call_args.kwargs["raw"])
        self.assertEqual(request.call_args.args[1], {
            "channel": "C456", "text": "Hi &lt;!channel&gt; &amp; &lt;@U123&gt;",
        })
        self.assertNotIn("remote_id", items[0])

    @patch("publish_announcements.request")
    def test_partial_failure_is_reported_and_not_retried(self, request):
        request.side_effect = ['{"ok":false,"error":"channel_not_found"}', "ok"]
        items = [item for item in self.plan() if item["channel"] == "slack"]
        progress = MagicMock()
        self.assertFalse(publisher().run(items, progress))
        self.assertEqual([item["status"] for item in items], ["failed", "submitted"])
        self.assertIn("channel_not_found", items[0]["error"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(progress.call_count, 2)

    @patch("publish_announcements.request")
    def test_groupsio_draft_and_publish(self, request):
        content = SIMPLE + "groupsio: |\n  <script>& news\n  https://opentelemetry.io/x.\n"
        for mode in ["draft", "publish"]:
            with self.subTest(mode=mode):
                request.reset_mock()
                request.side_effect = [{"object": "draft", "id": 12}, {"object": "draft", "id": 12}, None]
                items = self.plan(mode, content, {"groupsio": [123]}, ["groupsio"])
                self.assertTrue(publisher().run(items))
                calls = request.call_args_list
                self.assertEqual(calls[0].args[0], "https://lists.cncf.io/api/v1/newdraft")
                self.assertEqual(calls[0].args[1], {"group_id": 123, "draft_type": "draft_type_post"})
                self.assertEqual(calls[1].args[0], "https://lists.cncf.io/api/v1/updatedraft")
                self.assertEqual(calls[1].args[1]["subject"], "OpenTelemetry news")
                self.assertEqual(calls[1].args[1]["body"],
                                 '<p>&lt;script&gt;&amp; news<br>\n<a href="https://opentelemetry.io/x">'
                                 'https://opentelemetry.io/x</a>.</p>')
                self.assertEqual(len(calls), 2 if mode == "draft" else 3)
                if mode == "publish":
                    self.assertTrue(calls[2].kwargs["empty_ok"])
                    self.assertTrue(calls[2].args[0].endswith("/postdraft"))

    @patch("publish_announcements.request")
    def test_failed_groupsio_update_leaves_draft_id_and_never_posts(self, request):
        request.side_effect = [{"object": "draft", "id": 12}, {"object": "error", "type": "bad_request"}]
        items = self.plan(targets={"groupsio": [123]}, channels=["groupsio"], content=SIMPLE)
        self.assertFalse(publisher().run(items))
        self.assertEqual(items[0]["remote_id"], 12)
        self.assertEqual(items[0]["remote_status"], "empty-draft")
        self.assertEqual(request.call_count, 2)

    @patch("publish_announcements.request")
    def test_unexpected_error_is_recorded_and_other_destinations_continue(self, request):
        request.side_effect = [KeyError("secret detail"), "ok"]
        items = [item for item in self.plan() if item["channel"] == "slack"]
        self.assertFalse(publisher().run(items))
        self.assertEqual([item["status"] for item in items], ["failed", "submitted"])
        self.assertIn("Unexpected KeyError; delivery may have occurred", items[0]["error"])
        self.assertNotIn("secret", items[0]["error"])


class TransportTests(unittest.TestCase):
    @patch("publish_announcements.build_opener")
    def test_json_and_form_encoding(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b'{"ok": true}'
        publish.request("https://example.com", {"text": "a & b"}, {"Authorization": "Bearer test"})
        req = opener.return_value.open.call_args.args[0]
        self.assertEqual(json.loads(req.data), {"text": "a & b"})
        self.assertEqual(req.get_header("Authorization"), "Bearer test")
        self.assertEqual(opener.return_value.open.call_args.kwargs["timeout"], 30)
        publish.request("https://example.com", {"text": "a & b"}, form=True)
        req = opener.return_value.open.call_args.args[0]
        self.assertEqual(parse_qs(req.data.decode()), {"text": ["a & b"]})

    @patch("publish_announcements.build_opener")
    def test_raw_response_and_webhook_error_codes(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = b"ok"
        self.assertEqual(publish.request(WEBHOOK, {}, raw=True), "ok")
        error = HTTPError(WEBHOOK, 404, "Not Found", {}, None)
        error.read = MagicMock(return_value=b"channel_not_found")
        opener.return_value.open.side_effect = error
        with self.assertRaisesRegex(publish.DeliveryError, r"HTTP 404 \(channel_not_found\)") as failure:
            publish.request(WEBHOOK, {}, raw=True)
        self.assertNotIn(WEBHOOK, str(failure.exception))

    @patch("publish_announcements.build_opener")
    def test_server_errors_may_have_delivered(self, opener):
        for code, uncertain in [(400, False), (503, True)]:
            with self.subTest(code=code):
                opener.return_value.open.side_effect = HTTPError("https://example.com", code, "", {}, None)
                with self.assertRaises(publish.DeliveryError) as failure:
                    publish.request("https://example.com", {})
                self.assertEqual("may have occurred" in str(failure.exception), uncertain)

    @patch("publish_announcements.build_opener")
    def test_empty_response_only_allowed_explicitly(self, opener):
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b""
        self.assertIsNone(publish.request("https://example.com", {}, empty_ok=True))
        with self.assertRaises(publish.DeliveryError):
            publish.request("https://example.com", {})

    @patch("publish_announcements.build_opener")
    def test_invalid_response_shapes_are_explicit_errors(self, opener):
        response = opener.return_value.open.return_value.__enter__.return_value
        for body in [b"not json", b"[]", b"null"]:
            with self.subTest(body=body):
                response.read.return_value = body
                with self.assertRaises(publish.DeliveryError):
                    publish.request("https://example.com", {})

    @patch("publish_announcements.build_opener")
    def test_transport_errors_not_retried_or_leaked(self, opener):
        for error in [
            HTTPError("https://example.com", 429, "secret response", {}, None),
            URLError("secret credential"), TimeoutError("secret credential"),
            IncompleteRead(b"secret partial response"),
        ]:
            with self.subTest(error=error):
                opener.return_value.open.reset_mock()
                opener.return_value.open.side_effect = error
                with self.assertRaises(publish.DeliveryError) as failure:
                    publish.request("https://example.com", {})
                self.assertNotIn("secret", str(failure.exception))
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_redirects_are_rejected(self):
        self.assertIsNone(publish.NoRedirects().redirect_request(None, None, 302, "", {}, "https://other.com"))


class MainTests(TestCase):
    def run_main(self, args, env):
        path = self.files.write()
        directory = self.files.root / "out"
        directory.mkdir(exist_ok=True)
        summary, output = directory / "summary", directory / "output"
        env = {"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(output),
               "GITHUB_REPOSITORY": "open-telemetry/opentelemetry-announcements",
               "GITHUB_SHA": "abc", **env}
        with patch.object(publish, "ROOT", self.files.root), \
                patch.dict(os.environ, env, clear=True):
            code = publish.main([*args, str(path)])
        return code, summary.read_text(), output.read_text() if output.exists() else "", directory

    @patch("publish_announcements.request")
    def test_check_only_renders_without_requests(self, request):
        code, summary, output, _ = self.run_main(
            ["--check-only", "--mode", "publish"], {"ANNOUNCEMENT_TARGETS": json.dumps(TARGETS)})
        self.assertEqual(code, 0)
        self.assertEqual(output, "count=1\n")
        self.assertIn("Read our latest news", summary)
        self.assertIn("follow-up 1, linkedin at 2099-01-01T16:00:00Z", summary)
        self.assertIn("<b>Subject:</b> OpenTelemetry news", summary)
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_check_only_reports_missing_targets(self, request):
        code, summary, output, _ = self.run_main(["--check-only", "--mode", "draft"], {})
        self.assertEqual(code, 1)
        self.assertIn("No targets configured", summary)
        self.assertEqual(output, "count=0\n")
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_report_has_no_content(self, request):
        code, summary, _, directory = self.run_main(
            ["--mode", "check", "--report", str(self.files.root / "out")], {})
        self.assertEqual(code, 0)
        report = json.loads((directory / "report.json").read_text())
        self.assertTrue(report["success"])
        self.assertEqual(len(report["deliveries"]), 9)
        for item in report["deliveries"]:
            self.assertLessEqual(set(item), set(publish.REPORT_FIELDS))
        self.assertNotIn("latest news", json.dumps(report))
        self.assertIn("preview: 9", summary)
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_missing_secrets_fail_with_report(self, request):
        code, summary, _, directory = self.run_main(
            ["--mode", "publish", "--report", str(self.files.root / "out")],
            {"ANNOUNCEMENT_TARGETS": json.dumps(TARGETS)})
        self.assertEqual(code, 1)
        report = json.loads((directory / "report.json").read_text())
        self.assertFalse(report["success"])
        self.assertIn("Missing secrets", report["error"])
        self.assertIn("pending: 11", summary)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
