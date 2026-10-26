#!/usr/bin/env python3
"""Publish announcements to their channels.

Usage: publish_announcements.py [--mode MODE] [--check-only] [--since REF]
                                [--report DIR] [FILE ...]

Publishes each announcement file (FILE, or every file under announcements/
added since REF) to Bluesky, Mastodon and LinkedIn through Buffer, to Slack
through a Workflow Builder webhook, and to the mailing list through the
Groups.io API. Follow-ups are scheduled in Buffer at their publish_at time.

Modes:
  check    validate and preview only; no requests and no credentials
  draft    create drafts in Buffer and Groups.io; Slack is previewed only
  publish  send the first post now and schedule the follow-ups

With --check-only, files are validated and previewed for MODE without any
requests. Destinations come from the ANNOUNCEMENT_TARGETS environment variable,
a JSON object that maps channels to lists of IDs. Credentials come from
BUFFER_API_KEY, SLACK_WEBHOOK_URL and GROUPSIO_API_KEY. Writes are never
retried; with --report, a report of every delivery is written to DIR.
"""

import argparse
import html
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from jsonschema import Draft202012Validator
from ruamel.yaml.error import MarkedYAMLError

import check_announcements as check

ROOT = check.ROOT
CHANNELS = set(check.CHANNELS)
BUFFER_CHANNELS = {"linkedin", "bluesky", "mastodon"}
DRAFT_CHANNELS = BUFFER_CHANNELS | {"groupsio"}
MODES = ("check", "draft", "publish")
MAX_DESTINATIONS = 50
GROUPSIO_API = "https://lists.cncf.io/api/v1"
REPORT_FIELDS = (
    "file", "post", "channel", "target", "action", "publish_at",
    "status", "remote_id", "remote_status", "error",
)
SECRETS = {
    "linkedin": "BUFFER_API_KEY",
    "bluesky": "BUFFER_API_KEY",
    "mastodon": "BUFFER_API_KEY",
    "slack": "SLACK_WEBHOOK_URL",
    "groupsio": "GROUPSIO_API_KEY",
}


class DeliveryError(Exception):
    """An explicitly reported validation or delivery failure."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward credentials or turn an API write into a redirected GET.
        return None


def request(url, payload, headers=None, *, form=False, empty_ok=False, raw=False):
    data = urlencode(payload).encode() if form else json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/x-www-form-urlencoded" if form else "application/json",
        **(headers or {}),
    }
    try:
        with build_opener(NoRedirects()).open(
            Request(url, data=data, headers=headers, method="POST"), timeout=30
        ) as response:
            body = response.read()
    except HTTPError as exc:
        detail = ""
        if raw:
            # Slack webhooks report errors as short codes.
            try:
                detail = f" ({api_error_code(exc.read().decode().strip())})"
            except (OSError, UnicodeDecodeError, AttributeError):
                pass
        uncertain = "; delivery may have occurred" if exc.code >= 500 else ""
        raise DeliveryError(f"API returned HTTP {exc.code}{detail}{uncertain}; not retried") from exc
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise DeliveryError(
            "API connection failed; delivery may have occurred; not retried"
        ) from exc
    if raw:
        return body.decode("utf-8", errors="replace")
    if not body and empty_ok:
        return None
    try:
        result = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DeliveryError("API returned an invalid JSON response; not retried") from exc
    if not isinstance(result, dict):
        raise DeliveryError("API returned an unexpected response object")
    return result


def api_error_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,80}", value) else "unknown"


def slack_escape(text):
    # Slack treats <...> as control sequences such as <!channel>, even with parse=none.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def groupsio_html(text):
    """Escape the text for a Groups.io message body and turn links into anchors."""
    parts = []
    position = 0
    for match in check.URL_PATTERN.finditer(text):
        url = match.group().rstrip(check.URL_TRAILING)
        parts.append(html.escape(text[position:match.start()]))
        parts.append(f'<a href="{html.escape(url)}">{html.escape(url)}</a>')
        position = match.start() + len(url)
    parts.append(html.escape(text[position:]))
    return "<p>" + "".join(parts).replace("\n", "<br>\n") + "</p>"


def load(path, *, now, report, times=True):
    """Load an announcement and run every check on it; raise if any fails."""
    errors = report.errors
    try:
        data = check.load_yaml(path)
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        report(path, mark.line + 1 if mark else 1, f"invalid YAML: {exc.problem or exc}")
    else:
        schema = json.loads(check.SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=check.format_checker)
        if check.check_schema(path, data, validator, report):
            check.check_lengths(path, data, report)
            check.check_images(path, data, report)
            if times:
                check.check_times(path, data, report, now)
    if report.errors > errors:
        raise DeliveryError(f"{display(path)} has errors; see the messages above")
    return data


def display(path, root=None):
    root = root or ROOT
    path = Path(path).resolve()
    return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)


def parse_targets(value):
    try:
        targets = json.loads(value) if value and value.strip() else {}
    except ValueError as exc:
        raise DeliveryError("ANNOUNCEMENT_TARGETS must be valid JSON") from exc
    if not isinstance(targets, dict) or set(targets) - CHANNELS:
        raise DeliveryError("ANNOUNCEMENT_TARGETS must be an object keyed by channel names")
    buffer_ids = set()
    for channel, destinations in targets.items():
        if not isinstance(destinations, list) or not destinations:
            raise DeliveryError(f"{channel} targets must be a nonempty array")
        for destination in destinations:
            if channel == "groupsio":
                valid = type(destination) is int and destination > 0
            else:
                pattern = r"[CG][A-Z0-9]+" if channel == "slack" else r"[A-Za-z0-9_-]+"
                valid = isinstance(destination, str) and re.fullmatch(pattern, destination)
            if not valid:
                raise DeliveryError(f"Invalid {channel} destination")
        normalized = [str(value).lower() for value in destinations]
        if len(set(normalized)) != len(normalized):
            raise DeliveryError(f"Duplicate {channel} destination")
        if channel in BUFFER_CHANNELS:
            if buffer_ids & set(destinations):
                raise DeliveryError("A Buffer channel ID cannot target multiple services")
            buffer_ids |= set(destinations)
    return targets


def build_plan(announcements, targets, mode, *, image_base=None, root=None):
    """Return one delivery per post, channel and destination.

    announcements is a list of (path, data) pairs. image_base is the URL that
    image paths relative to root are appended to; Buffer fetches images from it.
    """
    if mode not in MODES:
        raise DeliveryError(f"mode must be one of {', '.join(MODES)}")
    root = root or ROOT
    plan = []
    for path, data in announcements:
        for index, (_, post, channels) in enumerate(check.posts(data)):
            image = None
            if "image" in post:
                image = (Path(path).parent / post["image"]).resolve().relative_to(root).as_posix()
            for channel in channels:
                if channel not in targets and mode != "check":
                    raise DeliveryError(f"No targets configured for {channel}")
                # Previews (for example in pull requests) need no destinations.
                for target in targets.get(channel, [None]):
                    action = (
                        "preview" if mode == "check"
                        or (mode == "draft" and channel not in DRAFT_CHANNELS)
                        else mode
                    )
                    item = {
                        "file": display(path, root), "post": index,
                        "channel": channel, "target": target, "action": action,
                        "text": post.get(channel, post["text"]).strip(),
                        "status": "pending",
                    }
                    if "publish_at" in post:
                        item["publish_at"] = post["publish_at"]
                    if channel == "groupsio":
                        item["subject"] = data["subject"]
                    if image and channel in BUFFER_CHANNELS:
                        item["image"] = image
                        item["alt"] = post["alt"]
                        if action != "preview":
                            if not image_base:
                                raise DeliveryError("Images need GITHUB_REPOSITORY and GITHUB_SHA")
                            item["image_url"] = image_base + image
                    plan.append(item)
    if len(plan) > MAX_DESTINATIONS:
        raise DeliveryError(f"At most {MAX_DESTINATIONS} deliveries are allowed per run")
    return plan


class Publisher:
    def __init__(self, credentials):
        self.credentials = credentials

    def auth(self, secret):
        return {"Authorization": f"Bearer {self.credentials[secret]}"}

    def buffer(self, query, variables):
        result = request(
            "https://api.buffer.com", {"query": query, "variables": variables},
            self.auth("BUFFER_API_KEY"),
        )
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise DeliveryError("Buffer GraphQL request failed")
        return result["data"]

    def preflight(self, plan):
        active = [item for item in plan if item["action"] != "preview"]
        required = {SECRETS[item["channel"]] for item in active}
        missing = sorted(secret for secret in required if not self.credentials.get(secret))
        if missing:
            raise DeliveryError(f"Missing secrets: {', '.join(missing)}")
        for secret in required:
            value = self.credentials[secret]
            if not isinstance(value, str) or any(not 33 <= ord(char) <= 126 for char in value):
                raise DeliveryError(f"{secret} must contain only printable ASCII without whitespace")
        if "SLACK_WEBHOOK_URL" in required and not re.fullmatch(
            r"https://hooks\.slack\.com/triggers/[A-Za-z0-9/_-]+", self.credentials["SLACK_WEBHOOK_URL"]
        ):
            raise DeliveryError("SLACK_WEBHOOK_URL must be a Slack workflow webhook (https://hooks.slack.com/triggers/...)")
        # Check every Buffer channel before the first write: a mislabeled ID must
        # not silently route a LinkedIn announcement to a different network.
        checked = set()
        for item in active:
            if item["channel"] not in BUFFER_CHANNELS or item["target"] in checked:
                continue
            checked.add(item["target"])
            data = self.buffer(
                "query($id: ChannelId!) { channel(input: {id: $id}) "
                "{ id service isDisconnected isLocked } }",
                {"id": item["target"]},
            )
            channel = data.get("channel")
            if (
                not isinstance(channel, dict)
                or channel.get("id") != item["target"]
                or channel.get("service") != item["channel"]
                or channel.get("isDisconnected") is not False
                or channel.get("isLocked") is not False
            ):
                raise DeliveryError("Buffer channel is unavailable or does not match its selected service")

    def deliver(self, item):
        channel = item["channel"]
        draft = item["action"] == "draft"
        if channel in BUFFER_CHANNELS:
            post_input = {
                "channelId": item["target"], "text": item["text"],
                "schedulingType": "automatic", "saveToDraft": draft,
            }
            if "publish_at" in item:
                post_input.update(mode="customScheduled", dueAt=item["publish_at"])
                allowed = {"draft"} if draft else {"scheduled"}
            else:
                post_input["mode"] = "addToQueue" if draft else "shareNow"
                allowed = {"draft"} if draft else {"sending", "sent", "scheduled"}
            if "image_url" in item:
                post_input["assets"] = [{"image": {
                    "url": item["image_url"], "metadata": {"altText": item["alt"]},
                }}]
            data = self.buffer(
                "mutation($input: CreatePostInput!) { createPost(input: $input) "
                "{ ... on PostActionSuccess { post { id status } } "
                "... on MutationError { message } } }",
                {"input": post_input},
            )
            result = data.get("createPost")
            if not isinstance(result, dict) or result.get("message"):
                raise DeliveryError("Buffer rejected the post; check content limits and account permissions")
            post = result.get("post")
            if not isinstance(post, dict) or not post.get("id"):
                raise DeliveryError("Buffer did not return a post ID")
            item["remote_id"] = post["id"]
            item["remote_status"] = post.get("status")
            if post.get("status") not in allowed:
                raise DeliveryError("Buffer returned an unexpected post status")
        elif channel == "slack":
            # Workflow Builder webhooks accept only the trigger's declared variables.
            result = request(
                self.credentials["SLACK_WEBHOOK_URL"],
                {"channel": item["target"], "text": slack_escape(item["text"])},
                raw=True,
            ).strip()
            try:
                parsed = json.loads(result)
            except ValueError:
                parsed = None
            if result != "ok" and not (isinstance(parsed, dict) and parsed.get("ok") is True):
                error = parsed.get("error") if isinstance(parsed, dict) else result
                raise DeliveryError(
                    f"Slack rejected the message ({api_error_code(error)}); "
                    "check the workflow's channel and text variables"
                )
        elif channel == "groupsio":
            headers = self.auth("GROUPSIO_API_KEY")
            created = request(
                f"{GROUPSIO_API}/newdraft",
                {"group_id": item["target"], "draft_type": "draft_type_post"},
                headers, form=True,
            )
            if created.get("object") != "draft" or type(created.get("id")) is not int:
                raise DeliveryError("Groups.io did not return a draft ID")
            if created["id"] <= 0:
                raise DeliveryError("Groups.io returned an invalid draft ID")
            item["remote_id"] = created["id"]
            item["remote_status"] = "empty-draft"
            updated = request(
                f"{GROUPSIO_API}/updatedraft",
                {"draft_id": created["id"], "subject": item["subject"],
                 "body": groupsio_html(item["text"])},
                headers, form=True,
            )
            if updated.get("object") != "draft" or updated.get("id") != created["id"]:
                raise DeliveryError("Groups.io did not confirm the draft update")
            item["remote_status"] = "draft"
            if not draft:
                result = request(
                    f"{GROUPSIO_API}/postdraft", {"draft_id": created["id"]},
                    headers, form=True, empty_ok=True,
                )
                if result is not None:
                    raise DeliveryError("Groups.io returned an unexpected postdraft response")
                item["remote_status"] = "submitted"
        else:
            raise DeliveryError("Unsupported delivery channel")

    def run(self, plan, on_progress=None):
        self.preflight(plan)
        for item in plan:
            if item["action"] == "preview":
                item["status"] = "preview"
                if on_progress:
                    on_progress()
                continue
            try:
                self.deliver(item)
            except DeliveryError as exc:
                item["status"] = "failed"
                item["error"] = str(exc)
            except Exception as exc:  # A bug must not hide that a write may have happened.
                item["status"] = "failed"
                item["error"] = (
                    f"Unexpected {type(exc).__name__}; delivery may have occurred; not retried"
                )
            else:
                item["status"] = (
                    "drafted" if item["action"] == "draft"
                    else "scheduled" if "publish_at" in item else "submitted"
                )
            if on_progress:
                on_progress()
        return not any(item["status"] == "failed" for item in plan)


def added_since(base):
    """Return the announcement files added between base and HEAD."""
    if set(base) == {"0"}:
        # A new branch has no previous commit; announcing every file would repeat old ones.
        return []
    try:
        output = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--diff-filter=A", base, "HEAD", "--", "announcements"],
            cwd=ROOT, check=True, capture_output=True, text=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        raise DeliveryError(f"Could not compare {base} with HEAD: {exc.stderr.strip()}") from exc
    return [ROOT / name for name in output.split("\0") if name.endswith(".yaml")]


def resolve(files, *, check_only):
    allowed = [ROOT / "announcements"] + ([ROOT / "examples"] if check_only else [])
    resolved = []
    for name in files:
        path = Path(name).resolve()
        if path.suffix != ".yaml" or not path.is_file() or not any(
            path.is_relative_to(base) for base in allowed
        ):
            raise DeliveryError(f"{name} is not a .yaml file under announcements/")
        if path not in resolved:
            resolved.append(path)
    return resolved


def rendered_summary(plan):
    rendered = {}
    for item in plan:
        rendered.setdefault((item["file"], item["post"], item["channel"]), item)
    lines = ["", "### Rendered content", ""] if rendered else []
    for (name, post, channel), item in rendered.items():
        when = f" at {item['publish_at']}" if "publish_at" in item else ""
        label = "first post" if post == 0 else f"follow-up {post}"
        extra = ""
        if "subject" in item:
            extra += f"<b>Subject:</b> {html.escape(item['subject'])}\n"
        if "image" in item:
            extra += f"<b>Image:</b> {html.escape(item['image'])} ({html.escape(item['alt'])})\n"
        lines.append(
            f"<details><summary>{html.escape(name)}: {label}, {channel}{when}</summary>"
            f"<pre>{extra}{html.escape(item['text'])}</pre></details>"
        )
    return lines


def error(message):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error::{check.escape(message)}")
    else:
        print(f"error: {message}", file=sys.stderr)


def write_summary(lines):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def write_output(name, value):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"{name}={value}\n")


def prepare(args, report):
    files = list(args.files) + (added_since(args.since) if args.since else [])
    paths = resolve(files, check_only=args.check_only)
    now = datetime.now(timezone.utc)
    announcements = [
        (path, load(path, now=now, report=report,
                    times=not path.is_relative_to(ROOT / "examples")))
        for path in paths
    ]
    image_base = None
    if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_SHA"):
        image_base = (f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}/"
                      f"{os.environ['GITHUB_SHA']}/")
    targets = parse_targets(os.environ.get("ANNOUNCEMENT_TARGETS", ""))
    return paths, build_plan(announcements, targets, args.mode, image_base=image_base)


def check_only(args, report):
    """Validate and render without credentials or requests."""
    summary = ["## Announcement check", "", f"Mode: `{args.mode}`", ""]
    try:
        paths, plan = prepare(args, report)
    except DeliveryError as exc:
        error(str(exc))
        write_summary([*summary, str(exc)])
        write_output("count", 0)
        return 1
    write_output("count", len(paths))
    if not paths:
        print("No new announcements.", file=sys.stderr)
        write_summary([*summary, "No new announcements."])
        return 0
    summary.extend(f"- `{display(path)}`" for path in paths)
    write_summary([*summary, *rendered_summary(plan)])
    print(f"Checked {len(paths)} announcement(s): {len(plan)} deliveries.", file=sys.stderr)
    return 0


def deliver(args, report):
    output = Path(args.report) if args.report else None
    result = {"mode": args.mode, "deliveries": []}
    success = False

    def save_report():
        if not output:
            return
        # The report is an audit record; the content is in the announcement files.
        saved = {**result, "deliveries": [
            {key: value for key, value in item.items() if key in REPORT_FIELDS}
            for item in result["deliveries"]
        ]}
        output.mkdir(parents=True, exist_ok=True)
        temporary = output / "report.json.tmp"
        temporary.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "report.json")

    try:
        _, result["deliveries"] = prepare(args, report)
        save_report()
        success = Publisher(os.environ).run(result["deliveries"], on_progress=save_report)
    except DeliveryError as exc:
        result["error"] = str(exc)
        error(str(exc))
    result["success"] = success
    save_report()
    summary = ["## Announcement delivery", "", f"Mode: `{args.mode}`", ""]
    for status in ("preview", "drafted", "scheduled", "submitted", "failed", "pending"):
        count = sum(item["status"] == status for item in result["deliveries"])
        summary.append(f"- {status}: {count}")
    if "error" in result:
        summary.extend(["", result["error"]])
    for item in result["deliveries"]:
        if item["status"] == "failed":
            summary.append(f"- {item['file']}, post {item['post']}, {item['channel']} "
                           f"{item['target']}: {item.get('error', '')}")
    summary.extend(rendered_summary(result["deliveries"]))
    if output:
        summary.extend(["", "See the report artifact for destinations and delivery IDs."])
    write_summary(summary)
    print("Announcement delivery completed." if success
          else "Announcement delivery failed; see the job summary.", file=sys.stderr)
    return 0 if success else 1


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mode", choices=MODES, default="check")
    parser.add_argument("--check-only", action="store_true",
                        help="validate and preview for MODE without any requests")
    parser.add_argument("--since", help="also publish files under announcements/ added since this git ref")
    parser.add_argument("--report", help="directory for report.json")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    report = check.Reporter()
    return check_only(args, report) if args.check_only else deliver(args, report)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
