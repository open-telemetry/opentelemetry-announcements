# OpenTelemetry Announcements

This repository is the central place for announcements on the OpenTelemetry
social media channels, such as [Bluesky][], [Mastodon][], LinkedIn, the
[announcements mailing list][] and Slack.

Anyone can open an issue or pull request to ask for an announcement. Before you
do, read the [Social Media Guide][] and the [Marketing Guidelines][].

## Requesting an announcement

Add one YAML file per announcement under `announcements/<year>/<name>.yaml`.
Start from [`examples/otel-in-practice.yaml`](examples/otel-in-practice.yaml);
[`schema/announcement.schema.json`](schema/announcement.schema.json) describes
every key.

| Key | Meaning |
| --- | ------- |
| `text` | Text of the first post, sent to every channel when the pull request is merged. Write links inline. |
| `subject` | Subject of the mailing list email. Required when `groupsio` is one of the channels. |
| `channels` | Channels for the first post: `bluesky`, `mastodon`, `linkedin`, `slack`, `groupsio`. Defaults to all. |
| `bluesky`, `mastodon`, `linkedin`, `slack`, `groupsio` | Text for one channel, instead of `text`. |
| `image`, `alt` | Image file next to the announcement and its alternative text. Posted to Bluesky, Mastodon and LinkedIn. |
| `follow_ups` | Later posts with a `publish_at` time (for example `2026-11-05T16:00:00Z`), scheduled in Buffer. Bluesky, Mastodon and LinkedIn only. |

Every pull request is checked automatically:

| Check | Applies to | Level |
| ----- | ---------- | ----- |
| Structure against the schema | all files | error |
| Length per channel (Bluesky 300, Mastodon 500, LinkedIn 3,000, Slack 40,000; links count as 23 characters) | all files | error |
| Image file exists | all files | error |
| `publish_at` is in the future | announcements added or changed in the pull request | error |
| Links are reachable | announcements added or changed in the pull request | warning |

Problems show up as annotations on the line where they occur. To run the
check locally:

```sh
pip install --require-hashes --no-deps -r .github/requirements.txt
python .github/scripts/check_announcements.py --base origin/main
```

To preview how a file is posted to each channel:

```sh
python .github/scripts/publish_announcements.py --check-only announcements/2026/<name>.yaml
```

In VS Code, `.vscode/settings.json` maps the schema to the announcement files
for autocompletion.

## How announcements are published

When a pull request that adds a file under `announcements/` is merged, the
[Publish announcements](.github/workflows/publish-announcements.yml) workflow:

1. Checks the new files again and previews every post in the job summary. This
   job has no access to the credentials.
2. Waits for a maintainer to approve the `announcements` environment.
3. Delivers the first post to every channel and schedules the follow-ups in
   Buffer:

   | Channel | Delivered through | Image | Follow-ups |
   | ------- | ----------------- | ----- | ---------- |
   | Bluesky, Mastodon, LinkedIn | [Buffer](https://developers.buffer.com/) | yes | yes |
   | Slack | A Slack Workflow Builder webhook | no | no |
   | Mailing list | The [Groups.io API](https://groups.io/api#drafts) | no | no |

Changes to existing files are not delivered again. To deliver a file
manually, run the workflow from the Actions tab with the file and a mode.

The workflow runs in one of three modes:

| Mode | Buffer | Groups.io | Slack |
| ---- | ------ | --------- | ----- |
| `check` | Preview | Preview | Preview |
| `draft` | Create drafts | Create drafts | Preview only, nothing is sent |
| `publish` | Publish the first post now, schedule follow-ups | Post to the list | Send the message |

Merges use the mode in the `ANNOUNCEMENT_MODE` repository variable, and
`draft` if it is not set. Manual runs default to `check`.

### Results, failures and retries

Every delivery uploads a `report.json` artifact with the status of each post,
channel and destination (`preview`, `drafted`, `scheduled`, `submitted`,
`failed` or `pending`), the IDs the services returned and a short error. The
job summary shows the counts and the rendered posts.

All files, destinations and secrets are validated before the first request,
and every Buffer channel is checked to belong to the service it is configured
for. If one destination fails, the others are still attempted and the job
fails.

`submitted` means the service accepted the request, not that the post is
visible: Buffer publishes asynchronously, and the mailing list may hold the
email for moderation. Slack returns no message ID; if its message step fails,
Slack tells the owner of the Slack workflow, not GitHub.

Writes are **never retried automatically** and there is no deduplication.
Rerunning a workflow, or running it manually for a file that was already
delivered, sends the posts again. Before you retry, check the report and the
channels, and use the manual run only for what didn't go out.

The workflow logs, job summary and report are public, like the announcement
files. Don't use this repository for embargoed content.

## Setup

These are configured in the repository settings by the maintainers.

**`announcements` environment**: add required reviewers and limit deployment
to the `main` branch. Store these secrets in the environment:

| Secret | Used for | How to get it |
| ------ | -------- | ------------- |
| `BUFFER_API_KEY` | Bluesky, Mastodon, LinkedIn | A Buffer API key with access to the connected OpenTelemetry channels |
| `SLACK_WEBHOOK_URL` | Slack | The webhook URL of the Slack workflow below (`https://hooks.slack.com/triggers/...`); other URLs are rejected |
| `GROUPSIO_API_KEY` | Mailing list | A `lists.cncf.io` API key of an account that is subscribed to the lists and allowed to post |

**`ANNOUNCEMENT_TARGETS` repository variable**: the destinations per channel,
as JSON. For example (not the real IDs):

```json
{
  "bluesky": ["BUFFER_BLUESKY_CHANNEL_ID"],
  "mastodon": ["BUFFER_MASTODON_CHANNEL_ID"],
  "linkedin": ["BUFFER_LINKEDIN_CHANNEL_ID"],
  "slack": ["C0123456789"],
  "groupsio": [12345]
}
```

Buffer destinations are channel IDs (not organization IDs), one per service.
Slack destinations are channel IDs, not names. Groups.io destinations are group
IDs on `lists.cncf.io`. Every channel that an announcement uses needs at least
one destination.

**`ANNOUNCEMENT_MODE` repository variable** (optional): `draft` (the default)
or `publish`.

**Slack workflow**: in Slack Workflow Builder, create a workflow that starts
**from a webhook** with two variables: `channel` (type _Slack channel ID_) and
`text` (type _Text_). Add a **Send a message to a channel** step that posts
`text` to `channel`. Publish it and store its URL as `SLACK_WEBHOOK_URL`.
Messages appear as sent by the workflow, which belongs to the person who
created it, so add a second collaborator.

### Delivery status

Each delivery path is covered by unit tests with mocked requests. Against the
real services, only Groups.io drafts have been verified so far, and Slack
accepts the webhook payload. Until the others are verified, keep
`ANNOUNCEMENT_MODE` at `draft`.

## Development

The scripts are in [`.github/scripts`](.github/scripts). To run the tests:

```sh
pip install --require-hashes --no-deps -r .github/requirements.txt
python -m unittest discover -s .github/scripts -p 'test_*.py'
```

## Maintainers

These are the members of [@open-telemetry/social-media-maintainers][] (members-only):

- [Patrice Chalin](https://github.com/chalin), CNCF
- [Daniel Gomez Blanco](https://github.com/danielgblanco), New Relic
- [Tiffany Hrabusa](https://github.com/tiffany76), Grafana Labs
- [Reese Lee](https://github.com/reese-lee), New Relic
- [Severin Neumann](https://github.com/svrnm), Bronto
- [Austin Parker](https://github.com/austinlparker)
- [Adriana Villela](https://github.com/avillela), Dynatrace

For more information about the maintainer role, see the [community repository](https://github.com/open-telemetry/community/blob/main/guides/contributor/membership.md#maintainer).

## Approvers

These are the members of [@open-telemetry/social-media-approvers][] (members-only):

- None at the moment

For more information about the approver role, see the [community repository](https://github.com/open-telemetry/community/blob/main/guides/contributor/membership.md#approver).

[Bluesky]: https://bsky.app/profile/opentelemetry.io
[Mastodon]: https://fosstodon.org/@opentelemetry
[announcements mailing list]:
  https://lists.cncf.io/g/cncf-opentelemetry-announce
[Social Media Guide]:
  https://github.com/open-telemetry/community/blob/main/social-media-guide.md
[Marketing Guidelines]: https://opentelemetry.io/community/marketing-guidelines/
[@open-telemetry/social-media-maintainers]:
  https://github.com/orgs/open-telemetry/teams/social-media-maintainers
[@open-telemetry/social-media-approvers]:
  https://github.com/orgs/open-telemetry/teams/social-media-approvers
