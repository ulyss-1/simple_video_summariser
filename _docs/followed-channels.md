# Followed channels

The owner's five followed YouTube channels. Migration
`migrations/versions/0026_seed_followed_channels.py` (issue #386) is the source
of truth for the seed; this table must match its `FOLLOWED_CHANNELS` constant
(a test checks it). A channel's URL is `https://www.youtube.com/channel/<id>`.

| Channel (title stored)                  | Channel ID                 | Handle                     |
| --------------------------------------- | -------------------------- | -------------------------- |
| Joseph Carlson                          | `UCbta0n8i6Rljh0obO7HzG9A` | `@JosephCarlsonShow`       |
| Joseph Carlson After Hours              | `UCfCT7SSFEWyG4th9ZmaGYqQ` | `@JosephCarlsonAfterHours` |
| Dr. Eric Berg DC                        | `UC3w193M5tYPJqF0Hi-7U-2g` | `@Drberg`                  |
| BWB - Business With Brian               | `UCyqlbzLoYtpqDXwRI9Yh5LA` | `@BusinessWithBrian`       |
| Value Investing with Sven Carlin, Ph.D. | `UCrTTBSUr0zhPU56UQljag5A` | `@Value-Investing`         |

## How the seed works

The migration inserts each channel as if the owner had followed it through
`POST /channels`: active, forward-only, `monitor_from` = the migration's run
time. An existing row is left unchanged (`ON CONFLICT DO NOTHING`). It applies
once on every database it reaches (production and the dev compose database).
Deleting or unfollowing a seeded channel later is not undone by running
`upgrade head` again, because the revision is already applied.

To add or remove a channel later use `POST /channels` or the UI
(`/channels/add`), `POST /channels/{id}/unfollow` or `DELETE /channels/{id}`.
To seed another database, write a new migration. Downgrading 0026 removes the
seeded rows that have no videos, backfill requests or catalog entries; a
channel the owner added by hand earlier with none of those is removed too.

## Effect of following

- The planner polls each active channel's RSS feed once at planner start and
  then every `POLL_INTERVAL_SEC` (3600 s).
- Only videos published after the channel's `monitor_from` are ingested;
  history is not fetched.
- Every new video, Shorts included (`SKIP_SHORTS=0` is the default), gets an
  `ingest` job and then the transcribe and analyze pipeline.
- Running the migration against a stack that is already up takes effect at the
  next poll, with no restart.
- A dev stack with network access therefore also starts processing real
  videos (`WHISPER_MODEL=tiny` there). Run the dev `upgrade` only when that is
  wanted.

## Load

With `PREFER_WHISPER=1` (set in the deploy host's `.env` for the real
transcription test) each new video of these channels, several per day, is
transcribed by Whisper `large-v3` at about 0.5x realtime on this CPU. Every
video is then analysed by the configured `SUMMARIZER` (LLM time on Ollama, or
Anthropic spend). Captions-first is `PREFER_WHISPER=0`. The migration changes no
setting.

## What the owner runs

- Production: `docker compose -f compose.yml run --rm migrate` (never with the
  dev override).
- Dev database: `docker compose up -d db`, then from the repo with the WSL venv
  active
  `DATABASE_URL=postgresql://ytdigest:<POSTGRES_PASSWORD from your .env>@127.0.0.1:5432/ytdigest alembic upgrade head`.
  `common/config.py` reads only the process environment, never `.env`; port 5432
  comes from `compose.override.yml` (override with `DB_HOST_PORT`). In a running
  dev stack, `docker compose run --rm migrate` works too (`./migrations` is
  mounted, no rebuild).
- Check: `GET /api/channels` (or the Library channel filter) lists the five with
  `active: true`.
