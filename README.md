# nifty-market-digest-bot

Nifty 50, Nifty Next 50, Nifty Midcap 150 TRI, Nifty Smallcap 250 TRI, BSE 250 SmallCap TRI,
the Nippon Gold ETF and the Russell 1000 Equal Weight Technology sleeve — daily updates with PE,
NAV/iNAV and historical comparisons, plus the gold:silver ratio, delivered straight to Telegram.

Runs before 11:11 IST on GitHub Actions. No server, no API keys, no paid data, nothing to
maintain on a normal week.

Every data source in this project was chosen by making the request and reading the response, not
from documentation. Where something the brief asked for turned out not to exist, it is listed as
a gap rather than approximated — see [Honest gaps](#honest-gaps).

---

## What arrives each morning

Per instrument: current level or price, NAV and iNAV where those exist, the day's move, and the
move over 1/2/3/4 weeks and 3/6/12 months — each with the PE **as it stood on that past date**.
Then a filtered news and policy section.

```
━━━━━━━━━━━━━━━━━
NIFTY SMALLCAP 250 TRI
18,312.20  ▲+0.19%  prev close · 17 Aug
TRI 23,378.56
PE 33.93 · PB 3.60 · DY 0.57%
       chg %     PE
1D     +0.19   33.9
1W     -0.14   34.2
2W     +0.87   34.6
3W     +2.84   34.5
4W     +1.75   36.0
3M     +9.82   30.1
6M    +14.14   26.5
1Y    +10.58   31.8
moves on TRI (total return), to 17 Aug close
```

---

## Coverage

| Instrument | Level | TRI | NAV | iNAV | PE now | PE history | 8 lookbacks |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| Nifty Smallcap 250 TRI | ✅ | ✅ | n/a | n/a | ✅ | ✅ | ✅ |
| BSE 250 SmallCap TRI | ✅ | ✅ | n/a | n/a | ✅ | ✅ | ✅ |
| Nifty Midcap 150 TRI | ✅ | ✅ | n/a | n/a | ✅ | ✅ | ✅ |
| Russell 1000 EW Tech | — | — | ✅ | n/a | ❌ | ❌ | ✅ |
| Gold — Nippon Gold BeES | ✅ | n/a | ✅ | ⚠️ | n/a | n/a | ✅ |
| Gold : Silver ratio | ✅ | n/a | n/a | n/a | n/a | n/a | ✅ |
| Nifty 50 | ✅ | ✅ | n/a | n/a | ✅ | ✅ | ✅ |
| Nifty Next 50 | ✅ | ✅ | n/a | n/a | ✅ | ✅ | ✅ |

`n/a` means the concept does not apply — an index has no NAV, gold has no earnings and therefore
no PE. `❌` means it exists but is not obtainable free. `⚠️` is best-effort.

### The gold:silver ratio

How many ounces of silver one ounce of gold buys. It mean-reverts far more reliably than either
metal's price, so a high reading says silver is cheap *relative to gold* and a low reading the
reverse. It says nothing about whether either metal is cheap outright.

```
GOLD : SILVER RATIO
67.74  ▲+0.50%  prev close · 17 Aug
1D +0.50  1W +1.11  2W -3.16  3W -2.79
4W -4.05  3M +14.73  6M +1.89  1Y -23.05
percentile 55 of 1Y range 44.1–89.1 · within the long-run 55–80 band
domestic (IBJA 999) 65.4
```

Both legs are COMEX futures in USD per troy ounce (`GC=F`, `SI=F`), so the units cancel exactly
and no conversion can introduce error. They also carry two years of daily closes, which is why
every lookback works from the first run rather than filling in over a year.

Rather than assert fixed buy/sell thresholds, the digest reports where the ratio sits **within
its own trailing one-year range**, computed from the data in hand. The long-run 55–80 band is
shown as context only. IBJA's domestic 999 rates give an independent reading; domestic and
international differ (65.4 against 67.7 here) because Indian import duty and GST are not
identical on the two metals, so that gap is expected to persist rather than converge.

Caveat worth knowing: these are futures rather than spot, on contracts with different expiries,
so the ratio carries a small basis difference against a spot-derived figure. It affects the level
more than the trend, which is what a mean-reversion gauge is read for.

---

## Setup

Roughly ten minutes, all free.

### 1. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts.
2. Copy the token it gives you (looks like `8123456789:AAH...`).
3. **Send your new bot any message** — a bot cannot start a conversation with you.
4. Get your chat id:

   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

   Find `"chat":{"id":123456789`. That number is your `TELEGRAM_CHAT_ID`. If the response is
   empty, you skipped step 3.

### 2. Push to a private GitHub repo

```bash
cd market-digest-bot
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<you>/market-digest-bot.git
git push -u origin main
```

Use a **private** repo. Nothing here is secret, but the workflow commits your holdings' history
back to the repo each day and there is no reason to publish it.

### 3. Add the two secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHAT_ID` | the number from `getUpdates` |

### 4. Test it now

Repo → **Actions** → enable workflows if prompted → **Morning digest** → **Run workflow**.

The first run is the slow one: it downloads about 13 months of history for each instrument and
commits the cache. Later runs only fetch the new day.

---

## Timing

GitHub Actions cron is UTC and knows nothing about time zones. India is UTC+05:30 with no
daylight saving, so 11:11 IST is 05:41 UTC.

**The honest problem:** Actions cron drift in 2026 is severe, and GitHub has publicly
acknowledged it — attributing it to scheduled-workflow load outgrowing capacity, with no fix
timeline. Maintainers report nightly jobs arriving **one to four hours late**, and a `*/15`
workflow firing roughly every 90 minutes. The delay happens *before* the run is created, so no
configuration avoids it. A single trigger cannot promise "by 11:11 IST".

**The defence:** four attempts across the morning, with the first one that actually fires doing
the work. `--once-per-day` makes that safe — later attempts read the send ledger, log *already
delivered*, and exit without messaging you twice. All four are after the 09:15 IST open, so every
one has live intraday levels.

| Cron (UTC) | IST | Role |
|---|---|---|
| `11 4` | 09:41 | early attempt |
| `51 4` | 10:21 | |
| `31 5` | 11:01 | the intended slot |
| `31 6` | 12:01 | catch-up, so a badly delayed morning still delivers |

In practice you'll usually receive it from the first attempt that isn't delayed. If you'd rather
have it as close to 11:11 as possible and accept occasional lateness, delete the first two cron
lines in [`.github/workflows/digest.yml`](.github/workflows/digest.yml).

**For minute-accurate delivery**, drive the workflow externally instead of by cron:
`workflow_dispatch` runs start promptly — only `schedule` is queued. A free minute-precision
scheduler (Cloudflare Workers Cron Triggers, or cron-job.org) calling GitHub's
`workflow_dispatch` API at exactly 11:01 IST gets you there. The cost is a Personal Access Token
that needs occasional renewal, which is why it isn't the default for a bot meant to need no
maintenance.

Weekdays only, since the market is shut at weekends and a Saturday digest would just repeat
Friday. For all seven days, change `1-5` to `*` in each cron line.

### What is actually current at 11:11

The market opens at 09:15 IST, so 11:11 is mid-session. That has a consequence worth
internalising:

| Figure | At 11:11 IST |
|---|---|
| Index level (Nifty family) | **live**, intraday |
| TRI | previous close — TRI is published end-of-day only |
| PE / PB / dividend yield | previous close — same reason |
| Gold ETF price | live, intraday |
| Gold NAV | previous day (T-1) |
| Edelweiss FoF NAV | previous day or older, and reflects the **prior US session** |

The digest labels every figure with what it actually is. Nothing is presented as live when it is
not, and the block header states which series the percentages were computed on and to which
date.

---

## Cost

Zero, and it stays zero.

- **GitHub Actions**: private repos get 2,000 free minutes a month. This job takes 2–4 minutes,
  about 22 times a month — well inside the allowance. No card required.
- **Data**: every source is a public file or an unauthenticated endpoint. No keys, no free tiers
  that can be withdrawn, no rate-limited quotas.
- **Telegram Bot API**: free, and the volume here is a rounding error against its limits.

---

## Honest gaps

These are the things the brief asked for that cannot be had for free. Each was chased to a
primary source before being written off.

**PE for Russell 1000 EW Tech — impossible, not merely hard.** FTSE Russell's own factsheet for
this index prints no P/E at all; it carries only constituent count, dividend yield and weight
statistics. No ETF tracks the index either, so there is no issuer publishing a portfolio P/E.
The only ETF ever built on it, Questrade `QRT`, was delisted in 2017. Substituting the parent
Russell 1000's PE, or XLK's, would be reporting a different instrument's valuation, so the
digest shows a dash.

**The Russell index level itself.** FTSE distributes it end-of-day "via FTP and email" to
licensees. Yahoo's `^R1EWTEC` has been frozen since October 2025. The digest therefore tracks
the instrument you can actually hold — the Edelweiss US Technology Equity FoF NAV — which is
what your returns depend on anyway.

**A published "Domestic Price of Gold Index".** It does not exist at any price. The scheme
document defines the benchmark as a formula the AMC computes internally, licensing no index
provider. The AMFI NAV series *is* the domestic gold price, net only of the expense ratio.

**True iNAV for the gold ETF, guaranteed.** The AMC's realtime iNAV endpoint sits behind
Cloudflare Bot Management and returns 403 to US datacenter IPs, which is where Actions runs. We
instead read the NAV that BSE's quote API publishes, which tracks the same realtime figure
(126.64 against an official T-1 NAV of 126.37) — but that endpoint is Referer-gated and so
unproven from a runner. If it is blocked, the digest falls back to the premium/discount against
the official NAV and says so. Historical iNAV does not exist anywhere: it is an intraday
dissemination that nobody archives.

**NSE cannot supply iNAV either — checked directly.** `nseindia.com/api/etf` does work and does
carry GOLDBEES with a last traded price, so it is wired in as a second price source. But it has
no iNAV field at all, and its `nav` field is stale: it reported 124.8671 while the official NAV
for that date was 126.3681 — the 14 August figure served two sessions late. It is deliberately
not read, because using it for premium/discount would understate the discount by whole sessions.
The one NSE endpoint that would carry iNAV, `/api/quote-equity`, returns **403 even after a
cookie warm-up** (the homepage itself 403s), and `/api/quote-etf` does not exist.

**PE for a total-return index, as a separate number.** There isn't one, and not because it is
paywalled: PE is a property of the constituents, so one PE applies whether you track the price
or total-return variant. The single published PE is shown alongside the TRI level.

---

## When something breaks

The design principle is that **silence must never look like good news**. A quiet market and a
dead bot are indistinguishable unless the bot says so.

| Failure | What happens |
|---|---|
| One source is down | That instrument shows `—`; the rest of the digest sends normally |
| A fetcher raises | Caught per instrument; digest still sends |
| Every source fails | A failure notice is sent instead of the digest |
| The job itself fails | A raw `curl` step messages you with a link to the run log |
| The history cache is corrupt | Quarantined to `.corrupt`, rebuilt from scratch |

Guards worth knowing about, because each is a real failure this project hit:

- `niftyindices.com` rejects non-browser User-Agents by **hanging**, not by returning 403. A
  timeout there means the headers are wrong, not that the exchange is down.
- Several endpoints answer **HTTP 200 with the wrong thing** — an HTML page, an empty array, or
  another fund's data. Row counts, header rows and ISINs are asserted rather than trusted.
- AMFI scheme codes are dense: `140088` is Gold BeES, `140089` is Nifty PSU Bank BeES. Every row
  is validated by ISIN, never by code alone.
- `certifi` older than 2026.07 lacks the Sectigo root and rejects `sebi.gov.in`,
  `amfiindia.com` and `ibjarates.com`, whose chains are perfectly valid. Hence the version pin.
  The fix is a current bundle, never `verify=False`.

### Staying alive

GitHub's docs say scheduled workflows are disabled after 60 days without repository activity, and
name only *public* repositories. Two caveats mean you should not lean on that:

- Community reports describe private repos being hit too.
- **A push made with the default `GITHUB_TOKEN` does not trigger workflows, and may therefore not
  count as the "activity" that resets the clock.** So the daily history commit is *not* a reliable
  keepalive, contrary to what seems obvious.

The practical answer: the schedule is disabled, not deleted, and the Actions tab offers a
one-click re-enable. If it ever goes quiet for a day, that is the first thing to check. Any manual
commit or a dispatch run resets the clock, so ordinary tinkering keeps it alive.

Note also the free-tier trade: a **private** repo consumes your 2,000 free Actions minutes a month
(this job uses roughly 60–90), while a **public** repo has unlimited minutes but is squarely inside
the 60-day policy. Private is still the recommendation — the allowance is ample and your holdings
stay unpublished.

---

## Later: WhatsApp

[`bot/notify.py`](bot/notify.py) has a `Notifier` base class precisely so this is additive. Add a
subclass, register it in `active_notifiers()`, and the digest code does not change.

The economics, checked rather than assumed, and they are worse than they look:

- **WhatsApp Cloud API is not free.** India utility templates are listed at **₹0.115 per delivered
  message** (~USD 0.0103), billed **per message** since Meta moved off per-conversation billing on
  1 July 2025. The old "first 1,000 free service conversations a month" allowance was **removed in
  November 2024**.
- Free service messages inside the 24-hour customer-service window still exist today — but from
  **1 October 2026** utility templates *and* service messages inside that window become
  chargeable. So even the "reply inside the service window" trick has a short shelf life.
- **WhatsApp Channels have no send API at all.** Posting is manual from the app, so it is not a
  route for an unattended job.
- **CallMeBot** is the one genuinely free path — but it is personal-use only (you may message
  only yourself), single-maintainer, no SLA, with documented capacity limits. Fine as a *mirror*
  of the Telegram message; never as the only channel.

A daily digest at ₹0.115 is about **₹30 a year** — trivial in absolute terms, but it is no longer
"free", and it needs template pre-approval that suits fixed-format text poorly. Telegram remains
the only channel here that is free *and* unlimited *and* needs no approval.

---

## Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip

.venv/Scripts/python -m bot.main --dry-run          # fetch and render, send nothing
.venv/Scripts/python -m scripts.smoke               # fetch everything, print the digest
.venv/Scripts/python -m scripts.diagnose            # reachability table for every source
.venv/Scripts/python -m scripts.news_check          # probe feeds, show what scoring picks
.venv/Scripts/python -m unittest discover -s tests  # 67 tests, no network
```

Useful flags: `--only <key>` to restrict instruments, `--no-news`, `--verbose`.

`scripts/news_check.py` is the one to run after touching feeds or scoring rules — a dead RSS URL
is a silent failure, and it makes both the dead feeds and the ruleset's choices visible.

`scripts/diagnose.py` checks every source and marks each CRITICAL or optional, exiting non-zero
only when a critical one fails. It matters because several of these hosts treat a datacenter IP
differently from a home connection, which cannot be tested from a laptop — so it is also exposed
as a workflow input. Repo → **Actions** → **Morning digest** → **Run workflow** → tick
**diagnose**, and it prints the table from the runner itself and sends nothing.

---

## Layout

```
bot/
  main.py          entry point and failure handling
  instruments.py   what is tracked, and how each is fetched  ← edit this to add/remove
  feeds.py         the verified RSS list
  compute.py       all percentage arithmetic, in one place
  format.py        Telegram HTML rendering
  news.py          RSS parsing and rule-based relevance scoring
  state.py         the incremental history cache
  http.py          retries, header handling, streaming
  model.py         Reading / Change / Snapshot — None means unknown, never 0
  util.py          IST clock, calendar maths, tolerant number parsing
  sources/
    nse.py         Nifty family — TRI, PE history, live levels
    bse.py         BSE 250 SmallCap — TRI and valuation
    amfi.py        shared mutual-fund NAV history
    gold.py        Nippon Gold BeES
    russell_tech.py  Edelweiss US Technology FoF
    gsr.py         gold:silver ratio
    yahoo.py       small chart client, used only where exchanges publish nothing
data/history/      committed daily; the cache that makes lookbacks cheap
```

Two invariants hold the correctness together:

**Intraday values are never written to history.** An 11:11 reading is not a close; storing it
would corrupt tomorrow's previous-day comparison.

**The lookback table anchors on the newest close, not on "now".** Measuring "1 week" from today
when the newest TRI is yesterday's would compare a six-day span and label it a week. The anchor
date is printed in every block.

---

## Licence

MIT — see [LICENSE](LICENSE).
