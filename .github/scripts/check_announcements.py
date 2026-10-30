#!/usr/bin/env python3
"""Check announcement files.

Usage: check_announcements.py [--base REF] [FILE ...]

Without FILE arguments, checks announcements/**/*.yaml and examples/**/*.yaml.

config.yaml is checked against schema/config.schema.json. Every file is
checked against schema/announcement.schema.json, against the channels' length
limits, for missing image files, and for skipped destinations that are not in
config.yaml. Files that are new or
changed are also checked for publish_at times in the past (error) and for
links that don't return 200 (warning). A file counts as new or changed when it
is passed as an argument, or when it differs from REF (with --base). Files in
examples/ are never checked for times and links.

Prints file:line: message for each problem. In GitHub Actions, also emits an
annotation on that line, so it shows up in the pull request diff.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError
from ruamel.yaml.scalarstring import FoldedScalarString, LiteralScalarString

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = ROOT / "schema" / "announcement.schema.json"
CONFIG = ROOT / "config.yaml"
CONFIG_SCHEMA = ROOT / "schema" / "config.schema.json"
SEARCH_DIRS = ("announcements", "examples")

CHANNELS = ["bluesky", "mastodon", "linkedin", "slack", "groupsio"]
SCHEDULED_CHANNELS = ["bluesky", "mastodon", "linkedin"]
# Character limits per post, counted the way Buffer validates them
# (https://developers.buffer.com/guides/character-limits.md). Mastodon and
# LinkedIn count UTF-16 code units, so most emoji count as 2, and every link as
# 23 and 24. Bluesky counts graphemes and shortens links to their host plus at
# most 16 characters; counting code points is slightly stricter for emoji
# sequences. Slack counts links in full.
LIMITS = {"bluesky": 300, "mastodon": 500, "linkedin": 3000, "slack": 40000}
LINK_LENGTHS = {"mastodon": 23, "linkedin": 24}
UTF16_CHANNELS = {"mastodon", "linkedin"}
LINK_RULES = {
    "bluesky": "links count as their host plus up to 16 characters",
    "mastodon": "links count as 23 and most emoji as 2",
    "linkedin": "links count as 24 and most emoji as 2",
    "slack": "links count in full",
}
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
URL_TRAILING = ".,;:!?)]}'\""
LINK_TIMEOUT = 15

format_checker = FormatChecker()


@format_checker.checks("date-time", raises=ValueError)
def is_date_time(value):
    if not isinstance(value, str):
        return True
    return datetime.fromisoformat(value).tzinfo is not None


class Reporter:
    def __init__(self):
        self.errors = 0
        self.warnings = 0
        self.github = os.environ.get("GITHUB_ACTIONS") == "true"

    def __call__(self, path, line, message, level="error"):
        rel = path.relative_to(ROOT) if path.is_absolute() and path.is_relative_to(ROOT) else path
        print(f"{rel}:{line}: {level}: {message}")
        if self.github:
            print(f"::{level} file={escape(str(rel), True)},line={line},"
                  f"title={escape('Announcement check', True)}::{escape(message)}")
        if level == "error":
            self.errors += 1
        else:
            self.warnings += 1


def escape(value, prop=False):
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if prop:
        value = value.replace(":", "%3A").replace(",", "%2C")
    return value


def load_yaml(path):
    yaml = YAML(typ="rt")
    # Keep timestamps as the strings that were written, so the schema sees
    # "2026-11-05T16:00:00Z" whether or not it was quoted.
    yaml.constructor.add_constructor(
        "tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node)
    )
    with path.open(encoding="utf-8") as f:
        return yaml.load(f)


def line_of(data, path):
    """Return the 1-based line of the deepest key or item on path."""
    line = 1
    node = data
    for key in path:
        if isinstance(node, CommentedMap) and key in node:
            line = node.lc.key(key)[0] + 1
        elif isinstance(node, CommentedSeq) and isinstance(key, int) and key < len(node):
            line = node.lc.item(key)[0] + 1
        else:
            break
        node = node[key]
    return line


def location(path):
    parts = []
    for key in path:
        parts.append(f"[{key}]" if isinstance(key, int) else f".{key}" if parts else key)
    return "".join(parts) or "(top level)"


def describe(error):
    """Turn a jsonschema error into (path, message) pairs with readable text."""
    path = list(error.absolute_path)
    if error.validator == "additionalProperties":
        allowed = error.schema.get("properties", {})
        return [
            (path + [key], f"unknown key '{key}'; allowed keys: {', '.join(allowed)}")
            for key in error.instance
            if key not in allowed
        ]
    if error.validator == "not" and "required" in error.validator_value:
        key = error.validator_value["required"][0]
        return [(path + [key], f"'{key}' text is set, but {key} is skipped")]
    if error.validator == "required":
        return [(path, error.message.replace("is a required property", "is required"))]
    if error.validator in ("pattern", "format") and path and path[-1] == "publish_at":
        return [(path, f"'{error.instance}' is not a date and time with a time zone, "
                       "for example 2026-11-05T16:00:00Z")]
    if error.validator in ("pattern", "not") and len(path) > 1 and path[-2] == "skip":
        channels = SCHEDULED_CHANNELS if "follow_ups" in path else CHANNELS
        return [(path, f"'{error.instance}' must be a channel ({', '.join(channels)}) or "
                       "a channel and destination from config.yaml, for example slack/opentelemetry")]
    if error.validator == "pattern" and path and path[-1] == "image":
        return [(path, f"'{error.instance}' must be a relative path to a "
                       ".png, .jpg, .gif or .webp file next to the announcement")]
    return [(path, error.message)]


def check_schema(path, data, validator, report):
    seen = set()
    errors = sorted(validator.iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))
    for error in errors:
        for err_path, message in describe(error):
            line = line_of(data, err_path)
            text = f"{location(err_path)}: {message}"
            if (line, text) not in seen:
                seen.add((line, text))
                report(path, line, text)
    return not errors


def posts(data):
    """Yield (path, post, channels) for the first post and each follow-up.

    channels are the channels that the post is not skipping as a whole.
    """
    yield [], data, [c for c in CHANNELS if c not in data.get("skip", [])]
    for index, follow_up in enumerate(data.get("follow_ups", [])):
        skip = follow_up.get("skip", [])
        yield ["follow_ups", index], follow_up, [c for c in SCHEDULED_CHANNELS if c not in skip]


def destinations(post, channels, targets):
    """Yield (channel, name, target) for every destination the post is sent to."""
    skip = post.get("skip", [])
    for channel in channels:
        for name, target in targets.get(channel, {}).items():
            if f"{channel}/{name}" not in skip:
                yield channel, name, target


def load_config(report, path=None):
    """Load config.yaml and check it; return None if it has errors."""
    path = path or CONFIG
    errors = report.errors
    try:
        config = load_yaml(path)
    except FileNotFoundError:
        report(path, 1, "configuration file not found")
        return None
    except MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        report(path, mark.line + 1 if mark else 1, f"invalid YAML: {exc.problem or exc}")
        return None
    schema = json.loads(CONFIG_SCHEMA.read_text(encoding="utf-8"))
    if check_schema(path, config, Draft202012Validator(schema), report):
        buffer_ids = {}
        for channel, named in config["targets"].items():
            seen = set()
            for name, target in named.items():
                key = ["targets", channel, name]
                if str(target).lower() in seen:
                    report(path, line_of(config, key),
                           f"{location(key)}: {target} is already a {channel} destination")
                seen.add(str(target).lower())
                if channel in SCHEDULED_CHANNELS:
                    # A mislabeled ID must not route a post to a different network.
                    if buffer_ids.setdefault(target, channel) != channel:
                        report(path, line_of(config, key),
                               f"{location(key)}: Buffer channel {target} is already "
                               f"a {buffer_ids[target]} destination")
    return config if report.errors == errors else None


def check_destinations(path, data, targets, report):
    """Check that skipped destinations exist and that every post has a destination."""
    for post_path, post, channels in posts(data):
        for index, entry in enumerate(post.get("skip", [])):
            channel, _, name = entry.partition("/")
            if name and name not in targets.get(channel, {}):
                key = post_path + ["skip", index]
                known = ", ".join(targets.get(channel, {})) or "none"
                report(path, line_of(data, key),
                       f"{location(key)}: config.yaml has no {channel} destination "
                       f"named '{name}' (configured: {known})")
        missing = [channel for channel in channels if not targets.get(channel)]
        if missing:
            report(path, line_of(data, post_path),
                   f"{location(post_path)}: config.yaml has no destinations for "
                   f"{', '.join(missing)}; skip them")
        elif not any(destinations(post, channels, targets)):
            report(path, line_of(data, post_path),
                   f"{location(post_path)}: every destination is skipped")


def link_length(url, channel):
    if channel in LINK_LENGTHS:
        return LINK_LENGTHS[channel]
    if channel == "bluesky":
        # Like toShortUrl in Bluesky's app: the host, then the rest of the link
        # cut to 13 characters and "..." if it is longer than 15.
        try:
            parts = urlsplit(url)
            rest = ("" if parts.path == "/" else parts.path) \
                + (f"?{parts.query}" if parts.query else "") \
                + (f"#{parts.fragment}" if parts.fragment else "")
            return len(parts.netloc) + (16 if len(rest) > 15 else len(rest))
        except ValueError:
            pass
    return len(url)


def counted_length(text, channel):
    def link(match):
        # Punctuation right after a link, like a full stop, isn't part of the
        # link but still counts.
        url = match.group().rstrip(URL_TRAILING)
        return "x" * link_length(url, channel) + match.group()[len(url):]

    text = URL_PATTERN.sub(link, text.strip())
    if channel in UTF16_CHANNELS:
        return len(text.encode("utf-16-le")) // 2
    return len(text)


def check_lengths(path, data, report, targets=None):
    for post_path, post, channels in posts(data):
        if targets is not None:
            # Channels whose every destination is skipped by name aren't sent to.
            sent = {channel for channel, _, _ in destinations(post, channels, targets)}
            channels = [channel for channel in channels if channel in sent]
        for channel in channels:
            if channel not in LIMITS:
                continue
            key = channel if channel in post else "text"
            length = counted_length(post[key], channel)
            if length > LIMITS[channel]:
                report(path, line_of(data, post_path + [key]),
                       f"{location(post_path + [key])}: {channel} post is {length} characters "
                       f"({LINK_RULES[channel]}); the limit is {LIMITS[channel]}")


def check_images(path, data, report):
    for post_path, post, _ in posts(data):
        if "image" in post and not (path.parent / post["image"]).is_file():
            report(path, line_of(data, post_path + ["image"]),
                   f"{location(post_path + ['image'])}: image file '{post['image']}' "
                   f"not found next to {path.name}")


def check_times(path, data, report, now):
    for post_path, post, _ in posts(data):
        if "publish_at" in post:
            when = datetime.fromisoformat(post["publish_at"])
            if when <= now:
                report(path, line_of(data, post_path + ["publish_at"]),
                       f"{location(post_path + ['publish_at'])}: {post['publish_at']} "
                       "is in the past")


def links(data):
    """Yield (line, path, url) for every link in every text of every post."""
    for post_path, post, _ in posts(data):
        for key in ["text"] + CHANNELS:
            if key not in post:
                continue
            value = post[key]
            key_line = line_of(data, post_path + [key])
            block = isinstance(value, (LiteralScalarString, FoldedScalarString))
            for match in URL_PATTERN.finditer(value):
                # Block scalars (| and >) start on the line after the key.
                line = key_line + 1 + value.count("\n", 0, match.start()) if block else key_line
                yield line, post_path + [key], match.group().rstrip(URL_TRAILING)


def link_status(url):
    """Return None if the URL works, otherwise a short description."""
    headers = {"User-Agent": "opentelemetry-announcements-check"}
    for method in ("HEAD", "GET"):
        try:
            request = urllib.request.Request(url, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=LINK_TIMEOUT):
                return None
        except ValueError:
            return "is not a valid URL"
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue  # some servers don't answer HEAD
            return f"returned HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            return f"could not be reached ({reason})"
    return None


def check_links(path, data, report, cache):
    for line, link_path, url in links(data):
        if url not in cache:
            cache[url] = link_status(url)
        if cache[url]:
            report(path, line,
                   f"{location(link_path)}: {url} {cache[url]}", level="warning")


def find_files():
    files = []
    for name in SEARCH_DIRS:
        base = ROOT / name
        if base.is_dir():
            files += sorted(p for p in base.rglob("*") if p.suffix in (".yaml", ".yml"))
    return files


def changed_since(base):
    output = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--diff-filter=AMR", base, "HEAD", "--", "announcements"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    return {(ROOT / name).resolve() for name in output.split("\0") if name}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="git ref to compare against to find new or changed files")
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)

    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=format_checker)

    report = Reporter()
    config = load_config(report)
    targets = config["targets"] if config else None

    files = args.files or find_files()
    changed = {path.resolve() for path in args.files}
    if args.base:
        changed |= changed_since(args.base)
        files = sorted(set(files) | {path for path in changed if path.suffix in (".yaml", ".yml")})
    examples = ROOT / "examples"

    if not files:
        print("No announcement files found.")
        return 1 if report.errors else 0

    now = datetime.now(timezone.utc)
    link_cache = {}
    for path in files:
        if path.suffix != ".yaml":
            report(path, 1, "use the .yaml extension")
            continue
        try:
            data = load_yaml(path)
        except MarkedYAMLError as exc:
            mark = exc.problem_mark or exc.context_mark
            report(path, mark.line + 1 if mark else 1, f"invalid YAML: {exc.problem or exc}")
            continue
        if not check_schema(path, data, validator, report):
            continue  # the remaining checks assume a valid structure
        check_lengths(path, data, report, targets)
        check_images(path, data, report)
        if targets is not None:
            check_destinations(path, data, targets, report)
        if path.resolve() in changed and not path.resolve().is_relative_to(examples):
            check_times(path, data, report, now)
            check_links(path, data, report, link_cache)

    print(f"Checked {len(files)} file(s): {report.errors} error(s), {report.warnings} warning(s).")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
