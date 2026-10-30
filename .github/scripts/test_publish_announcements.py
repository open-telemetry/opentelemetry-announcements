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
    skip: [bluesky, linkedin]
    text: Last call
"""
SIMPLE = "subject: OpenTelemetry news\ntext: Simple news\n"
TARGETS = {
    "linkedin": {"main": "linkedin-id"}, "bluesky": {"main": "bluesky-id"},
    "mastodon": {"main": "mastodon-id"}, "slack": {"general": "C123", "news": "C456"},
    "groupsio": {"announce": 123, "dev": 456},
}
CONFIG = """\
mode: draft
targets:
  linkedin: {main: linkedin-id}
  bluesky: {main: bluesky-id}
  mastodon: {main: mastodon-id}
  slack: {general: C123, news: C456}
  groupsio: {announce: 123, dev: 456}
"""
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

    def write_config(self, content=CONFIG):
        path = self.root / "config.yaml"
        path.write_text(textwrap.dedent(content), encoding="utf-8")
        return path

    def plan(self, mode="publish", content=ANNOUNCEMENT, targets=None, skip=None):
        if skip is not None:
            content = f"skip: {json.dumps(skip)}\n" + content
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


def only(channel):
    """Return a skip list that leaves only channel."""
    return [other for other in check.CHANNELS if other != channel]


def buffer_channel(channel="linkedin", target="linkedin-id"):
    return {"data": {"channel": {
        "id": target, "service": channel, "isLocked": False, "isDisconnected": False,
    }}}


def buffer_post(status="sending", post_id="post-id"):
    return {"data": {"createPost": {"post": {"id": post_id, "status": status}}}}


class LengthTests(TestCase):
    LONG_URL = "https://opentelemetry.io/" + "a" * 75  # 100 characters

    def errors(self, content):
        report = check.Reporter()
        path = self.files.write(content)
        check.check_lengths(path, check.load_yaml(path), report)
        return report.errors

    def test_links_count_per_channel(self):
        url = self.LONG_URL
        self.assertEqual(check.counted_length(url, "mastodon"), 23)
        self.assertEqual(check.counted_length(url, "linkedin"), 24)
        self.assertEqual(check.counted_length(url, "bluesky"), len("opentelemetry.io") + 16)
        self.assertEqual(check.counted_length(url, "slack"), 100)

    def test_bluesky_short_links_count_in_full_without_scheme(self):
        self.assertEqual(check.counted_length("https://opentelemetry.io/", "bluesky"), 16)
        self.assertEqual(check.counted_length("https://opentelemetry.io/blog/", "bluesky"), 22)

    def test_punctuation_after_a_link_counts(self):
        text = "a" * 476 + " https://example.com."
        self.assertEqual(check.counted_length(text, "mastodon"), 501)

    def test_emoji_count_as_utf16_on_mastodon_and_linkedin(self):
        self.assertEqual(check.counted_length("🚀", "mastodon"), 2)
        self.assertEqual(check.counted_length("🚀", "linkedin"), 2)
        self.assertEqual(check.counted_length("🚀", "bluesky"), 1)

    def test_long_bluesky_link_is_not_counted_as_23(self):
        # 276 letters, a space and the link: 277 + 32 = 309 on Bluesky.
        self.assertEqual(
            self.errors(f"skip: {only('bluesky')}\ntext: {'a' * 276} {self.LONG_URL}\n"), 1)
        self.assertEqual(
            self.errors(f"skip: {only('bluesky')}\ntext: {'a' * 267} {self.LONG_URL}\n"), 0)

    def test_mastodon_limit_with_emoji(self):
        self.assertEqual(self.errors(f"skip: {only('mastodon')}\ntext: {'🚀' * 251}\n"), 1)
        self.assertEqual(self.errors(f"skip: {only('mastodon')}\ntext: {'🚀' * 250}\n"), 0)


class PlanTests(TestCase):
    def test_posts_channels_and_destinations(self):
        items = self.plan()
        first = [item for item in items if item["post"] == 0]
        self.assertEqual(
            [(item["channel"], item["destination"], item["target"]) for item in first],
            [("bluesky", "main", "bluesky-id"), ("mastodon", "main", "mastodon-id"),
             ("linkedin", "main", "linkedin-id"), ("slack", "general", "C123"),
             ("slack", "news", "C456"), ("groupsio", "announce", 123), ("groupsio", "dev", 456)],
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
        items = publish.build_plan([loaded], TARGETS, "check", root=self.files.root)
        self.assertTrue(all("image_url" not in item for item in items))

    def test_check_mode_only_previews(self):
        items = self.plan("check")
        self.assertTrue(all(item["action"] == "preview" for item in items))
        self.assertEqual(len(items), 11)

    def test_draft_mode_previews_slack(self):
        items = self.plan("draft")
        for item in items:
            self.assertEqual(item["action"], "preview" if item["channel"] == "slack" else "draft")

    def test_skipped_channels_and_destinations(self):
        items = self.plan(skip=["linkedin", "slack/news", "groupsio"], content=SIMPLE)
        self.assertEqual([(item["channel"], item["destination"]) for item in items],
                         [("bluesky", "main"), ("mastodon", "main"), ("slack", "general")])
        items = self.plan(skip=only("slack"), content=SIMPLE)
        self.assertEqual([item["target"] for item in items], ["C123", "C456"])

    def test_skipped_destinations_in_follow_ups(self):
        content = SIMPLE + ("follow_ups:\n  - publish_at: '2099-01-01T16:00:00Z'\n"
                            "    skip: [bluesky/main, linkedin]\n    text: Reminder\n")
        items = [item for item in self.plan(content=content) if item["post"] == 1]
        self.assertEqual([item["channel"] for item in items], ["mastodon"])

    def test_missing_targets(self):
        with self.assertRaisesRegex(publish.DeliveryError, "No targets configured for groupsio"):
            self.plan(targets={key: value for key, value in TARGETS.items() if key != "groupsio"})

    def test_too_many_deliveries(self):
        targets = {**TARGETS, "slack": {f"c{index}": f"C{index}" for index in range(50)}}
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
            "subject: Hi\ntext: Hi\nchannels: [slack]\n",  # replaced by skip
            "subject: Hi\ntext: Hi\nskip: [reddit]\n",
            "text: Hi\nskip: [slack]\nslack: Hi\n",  # text for a skipped channel
            "subject: Hi\ntext: " + "a" * 301 + "\n",  # too long for Bluesky
            "subject: Hi\ntext: Hi\nimage: missing.png\nalt: Missing\n",
            "subject: Hi\ntext: Hi\nfollow_ups:\n  - publish_at: '2020-01-01T00:00:00Z'\n    text: Hi\n",
        ]
        for content in cases:
            with self.subTest(content=content), self.assertRaises(publish.DeliveryError):
                self.files.load(content)

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


class ConfigTests(TestCase):
    def load(self, content):
        report = check.Reporter()
        return check.load_config(report, self.files.write_config(content)), report.errors

    def test_valid_config(self):
        config, errors = self.load(CONFIG)
        self.assertEqual(errors, 0)
        self.assertEqual(config["mode"], "draft")
        self.assertEqual(config["targets"], TARGETS)

    def test_invalid_configs(self):
        cases = [
            "mode: [\n",  # invalid YAML
            CONFIG.replace("mode: draft", "mode: send"),
            CONFIG.replace("mode: draft\n", ""),
            CONFIG + "  reddit: {main: x}\n",
            CONFIG.replace("{general: C123, news: C456}", "{general: '#general'}"),
            CONFIG.replace("{general: C123, news: C456}", "{General: C123}"),
            CONFIG.replace("{general: C123, news: C456}", "{}"),
            CONFIG.replace("{announce: 123, dev: 456}", "{announce: '123'}"),
            CONFIG.replace("{announce: 123, dev: 456}", "{announce: 0}"),
            CONFIG.replace("{general: C123, news: C456}", "{general: C123, news: C123}"),
            CONFIG.replace("{main: bluesky-id}", "{main: linkedin-id}"),
        ]
        for content in cases:
            with self.subTest(content=content):
                config, errors = self.load(content)
                self.assertIsNone(config)
                self.assertGreater(errors, 0)

    def test_missing_config(self):
        report = check.Reporter()
        self.assertIsNone(check.load_config(report, self.files.root / "missing.yaml"))
        self.assertEqual(report.errors, 1)


class DestinationTests(TestCase):
    def messages(self, content, targets=TARGETS):
        report = MagicMock()
        path = self.files.write(content)
        check.check_destinations(path, check.load_yaml(path), targets, report)
        return [call.args[2] for call in report.call_args_list]

    def test_valid_skips(self):
        self.assertEqual(self.messages("text: Hi\nskip: [groupsio, slack/news]\n"), [])

    def test_unknown_destination(self):
        self.assertEqual(self.messages("text: Hi\nskip: [slack/random]\n"), [
            "skip[0]: config.yaml has no slack destination named 'random' (configured: general, news)",
        ])

    def test_every_destination_skipped(self):
        content = "text: Hi\nskip: [bluesky, mastodon, linkedin, groupsio, slack/general, slack/news]\n"
        self.assertEqual(self.messages(content), ["(top level): every destination is skipped"])

    def test_channel_without_destinations(self):
        targets = {key: value for key, value in TARGETS.items() if key != "groupsio"}
        self.assertEqual(self.messages("text: Hi\n", targets), [
            "(top level): config.yaml has no destinations for groupsio; skip them",
        ])
        self.assertEqual(self.messages("text: Hi\nskip: [groupsio]\n", targets), [])

    def test_skip_errors_are_readable(self):
        report = MagicMock()
        path = self.files.write("text: Hi\nskip: [reddit]\n")
        schema = json.loads(check.SCHEMA.read_text(encoding="utf-8"))
        check.check_schema(path, check.load_yaml(path),
                           check.Draft202012Validator(schema), report)
        self.assertIn("'reddit' must be a channel (bluesky, mastodon, linkedin, slack, groupsio)",
                      report.call_args.args[2])


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
                items = self.plan(mode, content, {"groupsio": {"announce": 123}}, only("groupsio"))
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
        items = self.plan(targets={"groupsio": {"announce": 123}}, skip=only("groupsio"), content=SIMPLE)
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
    def run_main(self, args, env=None, config=CONFIG):
        path = self.files.write()
        self.files.write_config(config)
        directory = self.files.root / "out"
        directory.mkdir(exist_ok=True)
        summary, output = directory / "summary", directory / "output"
        for name in (summary, output):
            name.unlink(missing_ok=True)
        env = {"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(output),
               "GITHUB_REPOSITORY": "open-telemetry/opentelemetry-announcements",
               "GITHUB_SHA": "abc", **(env or {})}
        with patch.object(publish, "ROOT", self.files.root), \
                patch.dict(os.environ, env, clear=True):
            code = publish.main([*args, str(path)])
        return code, summary.read_text(), output.read_text() if output.exists() else "", directory

    @patch("publish_announcements.request")
    def test_check_only_renders_without_requests(self, request):
        code, summary, output, _ = self.run_main(["--check-only", "--mode", "publish"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "count=1\nmode=publish\n")
        self.assertIn("Mode: `publish`", summary)
        self.assertIn("Read our latest news", summary)
        self.assertIn("first post, slack (general, news)", summary)
        self.assertIn("follow-up 1, linkedin (main) at 2099-01-01T16:00:00Z", summary)
        self.assertIn("<b>Subject:</b> OpenTelemetry news", summary)
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_mode_comes_from_config(self, request):
        code, summary, output, _ = self.run_main(["--check-only"])
        self.assertEqual(code, 0)
        self.assertEqual(output, "count=1\nmode=draft\n")
        self.assertIn("Mode: `draft`", summary)
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_check_only_reports_config_errors(self, request):
        for config in [CONFIG.replace("mode: draft", "mode: send"),
                       CONFIG.replace("  groupsio: {announce: 123, dev: 456}\n", "")]:
            with self.subTest(config=config):
                code, summary, output, _ = self.run_main(["--check-only"], config=config)
                self.assertEqual(code, 1)
                self.assertIn("has errors", summary)
                self.assertEqual(output, "count=0\n")
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_report_has_no_content(self, request):
        code, summary, _, directory = self.run_main(
            ["--mode", "check", "--report", str(self.files.root / "out")])
        self.assertEqual(code, 0)
        report = json.loads((directory / "report.json").read_text())
        self.assertTrue(report["success"])
        self.assertEqual(report["mode"], "check")
        self.assertEqual(len(report["deliveries"]), 11)
        for item in report["deliveries"]:
            self.assertLessEqual(set(item), set(publish.REPORT_FIELDS))
        self.assertNotIn("latest news", json.dumps(report))
        self.assertIn("preview: 11", summary)
        request.assert_not_called()

    @patch("publish_announcements.request")
    def test_missing_secrets_fail_with_report(self, request):
        code, summary, _, directory = self.run_main(
            ["--mode", "publish", "--report", str(self.files.root / "out")])
        self.assertEqual(code, 1)
        report = json.loads((directory / "report.json").read_text())
        self.assertFalse(report["success"])
        self.assertIn("Missing secrets", report["error"])
        self.assertIn("pending: 11", summary)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
