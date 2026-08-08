# carodi

A personal opportunity funnel. Pluggable sources in, one eligibility filter,
one Telegram digest a day.

```
sources ──► dedupe ──► geo ──► sponsors ──► rules ──► store ──► Telegram
```

The aggregation is not the point. Anyone can pull ten thousand postings; almost
all of them are ones you cannot legally take. The point is the filter — in
particular the join against government registers of employers licensed to
sponsor a foreign worker, which is what turns "10,000 jobs" into "the ones that
could actually hire you".

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

.venv/bin/carodi sources                     # what is wired up
.venv/bin/carodi run --dry-run --explain     # run the funnel, print to console
```

`--dry-run` writes nothing and notifies nobody, so you can tune the profile
freely. `--explain` prints why things were rejected, which is the actual
tuning loop:

```
    309  no required keyword
    241  excluded title (senior)
     28  onsite in unknown location, not a target country
      4  GB employer not on sponsor register
```

If a category is throwing away things you wanted, that is a `config/profile.yaml`
edit, not a code change.

## The two files you actually maintain

**`config/profile.yaml`** — who you are and what counts as a match. Target
countries, excluded titles, required keywords, scoring weights. This *is* the
filter.

**`config/deadlines.yaml`** — the curated scholarship/fellowship calendar.
Scholarships are not a feed: there are a few dozen, on fixed annual dates.
Scraping them daily gets you 364 days of "nothing new" and one day where you
are already too late. So they live in a hand-maintained file that re-announces
each entry at T-120/90/60/30/14/7/3/1 days.

> The dates shipped in `deadlines.yaml` are *typical* dates, seeded so the
> calendar works on day one. They are marked `verify-date` and several shift by
> weeks year to year. Confirming them against each programme's site is the
> single highest-value hour of work in this project.

## Sponsor registers

The highest-value filter, and the one piece that needs manual setup — see
[`data/registers/README.md`](data/registers/README.md).

```bash
carodi registers                                  # health check, exits non-zero if missing
carodi registers --probe "Monzo" "Some Startup"   # test the fuzzy matcher
```

**Until you download them, every onsite role in GB/NL is rejected** for lack of
a sponsor match — which looks exactly like a working filter that found nothing.
That is why `registers` is its own command.

## Adding a source

One file, one config entry, no pipeline changes:

```python
@register("my_board")
class MyBoard(Source):
    def __init__(self, name: str, url: str):
        self.name, self.url = name, url

    def fetch(self) -> Iterator[Opportunity]:
        for row in self._get_json(self.url):
            yield Opportunity(source=self.name, title=..., org=..., url=...)
```

Import it in `carodi/sources/__init__.py`, add it to `sources.yaml`, then:

```bash
carodi check my_board     # fetch it raw, bypassing the pipeline
```

Sources are isolated — one board returning HTML instead of JSON logs a warning
and the run continues. Failed sources are named in the digest footer.

Many boards need no Python at all: `rss` and `json_board` are config-driven.

### Built-in sources

| type | what it is |
|---|---|
| `greenhouse` / `lever` / `ashby` | ATS boards. Public JSON, unblocked, highest signal. Maintain the company list. |
| `json_board` | Any open JSON job API (Remotive, Arbeitnow, …), field names mapped in config. |
| `rss` | Any RSS/Atom job feed. |
| `hn_whoishiring` | Monthly HN thread via the Algolia API. Noisy, but surfaces roles no board carries. |
| `deadlines` | The curated calendar. |

## Telegram

```bash
export CARODI_TELEGRAM_TOKEN=...     # from @BotFather
export CARODI_TELEGRAM_CHAT_ID=...   # message the bot, then check getUpdates
carodi run
```

One digest message per run, chunked to Telegram's size limit — not one message
per match, which is how you learn to mute a bot.

## Did you actually apply?

The known way this project dies is that it delivers forty excellent matches a
day and you apply to none of them. Discovery is rarely the real bottleneck.

So every digest ends with a **Triage** button. Tapping it opens one match at a
time — title, employer, sponsor status, a link straight to the posting — with
**Applied**, **Not for me** and **Later**. Each tap edits the same message in
place, so triaging twenty matches costs one notification rather than twenty.

That needs the bot running to receive taps:

```bash
carodi bot            # long-polls for button presses; runs until stopped
carodi bot --once     # drain whatever is pending and exit
```

Deployed as `carodi-bot.service` alongside the timer (see below). Actions are
keyed by fingerprint rather than list position, so a button on yesterday's card
can never resolve whatever now happens to sit at that index.

The terminal is still there if you prefer it:

```bash
carodi open                              # delivered, still undecided
carodi mark af31beef7ef1798a applied
carodi mark 267130524de43755 skipped --note "no sponsorship"
```

Every digest footer then reads:

```
Last 30d: 62 delivered · 3 applied · 47 still undecided
```

That number is the point of the feature. A decision survives the posting being
re-scraped, so nothing you have already judged comes back.

## Running with Docker

```bash
docker compose build
CARODI_UID=$(id -u) CARODI_GID=$(id -g) docker compose run --rm carodi run --dry-run
```

The image pins Python 3.13 rather than tracking whatever the host ships — on a
very new Python, `pydantic-core` and `rapidfuzz` may have no prebuilt wheel and
pip falls back to compiling Rust and C++ from source. Pinning the runtime is
most of the reason to containerise this.

`config/` mounts read-only and `data/` read-write, so you can edit
`profile.yaml` or refresh a register without rebuilding, and the SQLite
database survives `docker compose build`. `CARODI_UID`/`CARODI_GID` make the
container write as you instead of as root — set them or the compose default of
`1000:1000` applies.

## Deploying

Three sets of units ship, all doing the same thing by different means:

| | runs as | how |
|---|---|---|
| `deploy/docker/` | you, user service | `docker compose run` — **recommended** |
| `deploy/venv/` | you, user service | a virtualenv in the checkout |
| `deploy/system/` | root, system service | a virtualenv in `/opt/carodi` |

### Docker as a user service (recommended)

Needs no sudo at all, provided you are in the `docker` group.

```bash
cd ~/repos/carodi
docker compose build

printf 'CARODI_TELEGRAM_TOKEN=...\nCARODI_TELEGRAM_CHAT_ID=...\n' > .env
chmod 600 .env

mkdir -p ~/.config/systemd/user
cp deploy/docker/carodi.service deploy/docker/carodi.timer \
   deploy/docker/carodi-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload

systemctl --user enable --now carodi.timer        # the daily digest
systemctl --user enable --now carodi-bot.service  # the Triage buttons

loginctl enable-linger "$USER"     # fire even when you're not logged in
```

Two units, different shapes: `carodi.timer` fires a oneshot run each morning,
while `carodi-bot.service` stays up to receive button taps and restarts on
failure. The digest works without the bot — you just get no buttons.

Update with `git pull` — the unit rebuilds before each run, so code changes
take effect on the next digest with no extra step.

```bash
systemctl --user list-timers carodi.timer
journalctl --user -u carodi.service -n 50
systemctl --user start carodi.service      # run one now, off-schedule
```

> `deploy/system/` sets `ProtectHome=true`, so those units cannot read a repo
> under `/home`. Use them only with the tree copied to `/opt/carodi`.

`Persistent=true` means a reboot or downtime runs the missed digest rather than
silently skipping a day — which would also skip that day's deadline alerts.

The timer is pinned to `Europe/Nicosia` rather than the host clock, so the
digest stays at 08:00 local through daylight-saving changes. Edit `OnCalendar`
for your own timezone.

## Testing

```bash
.venv/bin/pytest -q
```

The suite is entirely offline — no network in tests. Use `carodi check <source>`
to exercise a live source by hand.

## What is deliberately not here

- **No LLM scoring.** The rules are deterministic and testable. When you add a
  model, it goes *after* `reject()` and only reorders what already passed —
  it must never overrule hard eligibility.
- **No web UI, no accounts, no multi-tenancy.** One user: you.
- **No scraping of LinkedIn/Indeed.** They fight scrapers, and you would spend
  all your time on proxies instead of on the filter.
