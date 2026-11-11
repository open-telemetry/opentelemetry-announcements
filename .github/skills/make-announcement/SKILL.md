---
name: make-announcement
description: Prepare an OpenTelemetry announcement (release, blog post, event, SIG, survey, ecosystem news) as a pull request that adds an announcement YAML file. Use when asked to write, draft or request an announcement or social media post for OpenTelemetry.
---

# Make an OpenTelemetry announcement

The goal is a pull request against `open-telemetry/opentelemetry-announcements`
that adds `announcements/<year>/<MM-DD>-<name>.yaml` (date = day of the first
post). Merging triggers delivery, so never post to any channel yourself.
README.md documents the format; start from `examples/otel-in-practice.yaml`.

## Rules of thumb

- Professional, factual, no hype. No promotion of a vendor/product, and nothing
  negative about people, companies or projects.
- Relevant to the project: releases, blogs, ecosystem news, SIGs/events, surveys.
- Naming contributors (and their SIG or role) is fine and encouraged.

## 1. Gather input

Ask only for what's missing: what, canonical link, type, timing (and embargo),
who to credit, image if any.

Then look at previous posts for tone and structure: files in `announcements/`
and `examples/`, plus recent posts on https://bsky.app/profile/opentelemetry.io
and https://fosstodon.org/@opentelemetry. Past posts often lack credit and
context, so improve on that:

- Say why it matters to the reader, not just what happened.
- Credit the people/SIG behind it (ask the user for handles if unknown).
- Read the linked source and add one or two concrete details (a key feature,
  date, speaker).

## Blog posts

For posts on https://opentelemetry.io/blog/, read the full post and reuse its
own words as much as possible: its title, key sentences, terminology and
quotes. Condense by selecting and trimming the author's phrases rather than
paraphrasing or adding new claims. Do not mention the author or SIG. Close the
text with a "Read it:" line and the link.

## 2. Write one text for all channels

Write a single `text` that fits every channel, so it must pass Bluesky's
300 characters (links count as host + up to 16 chars). Wrapped lines are
joined into one paragraph as in Markdown; separate paragraphs with a blank
line. Add a channel-specific key (`bluesky`, `linkedin`, `groupsio`, ...) or
`skip` only when needed, e.g. a longer mailing list body with a greeting. `subject` is required for the mailing
list unless `groupsio` is skipped. Use `follow_ups` for reminders (events).

Without an image, Bluesky and LinkedIn posts show a link card built from the
first link's page title, description and preview image. An `image` (with
`alt`) replaces that card, so add one only when it shows more than the linked
page's own preview, for example a speaker card or a chart.

## 3. Validate and open the PR

```sh
pip install --require-hashes --no-deps -r .github/requirements.txt
python .github/scripts/check_announcements.py --base origin/main <file>
python .github/scripts/publish_announcements.py --check-only <file>
```

Fix errors; reachable-link warnings must be understood. Show the user the
final text for approval, then commit on a new branch (no `Co-authored-by`
trailer unless the user asks) and open the PR with `gh pr create` only after
the user confirms.
