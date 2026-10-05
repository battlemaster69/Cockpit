# Robinhood Chain survivor recorder

A data recorder, not a trader. It records every Robinhood Chain token that reaches $5,000 of liquidity, labels the
ones that are 2 to 10 days old and still healthy (the survivor filters), matches each passer with a random control,
and runs contract and holder checks on both, so the filters can be tested against random before any money goes in.
The full spec is the "Robinhood Chain Survivor Recorder — Spec" doc.

## How it runs

- `.github/workflows/rhc_recorder.yml`, every hour. Every run finds new pools (GeckoTerminal lists only the newest
  ~200, about an hour on this chain). A run 3.5+ hours after the last snapshot also catches up on missed tokens,
  snapshots every tracked token, labels the filters, assigns passers and controls, and checks them.
- Timer: GitHub's schedule is best-effort on this repo, so add a cron-job.org job like the collector's: every hour at
  :37, `POST https://api.github.com/repos/battlemaster69/Cockpit/actions/workflows/rhc_recorder.yml/dispatches`,
  body `{"ref":"main","inputs":{"auto":"true"}}`, same token. An `auto` run skips itself if the last run is under
  40 minutes old.
- No keys. Sources: GeckoTerminal (discovery), DexScreener (prices), the chain's official RPC node (checks; the
  Blockscout API is behind a Cloudflare bot check).

## Files (`data/`)

| File | One row per |
|---|---|
| `universe/YYYY-MM-DD.csv` | token discovered that day, tracked or not |
| `snapshots/YYYY-MM-DD.csv` | tracked token per snapshot run, with filter labels |
| `cohort.csv` | passer or control assignment (never changes) |
| `checks.csv` | checked token: contract flags, holders, top-20 share, seed-wallet overlap |
| `runs.csv` | run: calls per source, counts, anything that stopped it early |
| `seed_wallets.csv` | early buyers in 2+ seed tokens (`build_seed_wallets.py`, rebuilt by hand) |
| `state.json` | the recorder's working memory |

## Running it locally

    pip install requests pyyaml
    python rhc_recorder/run.py              # one run (snapshots when due)
    python rhc_recorder/run.py --snapshot   # force a snapshot run
    python rhc_recorder/run.py --dry-run    # print what would be written
    RHC_DATA=/tmp/rhc python rhc_recorder/run.py   # write to a scratch folder instead of data/

## Limits found while building (2026-10-05)

- GeckoTerminal: HTTP 429 after about 9 calls a minute, so calls are 12 s apart.
- RPC: HTTP 429 after 60 to 90 quick calls, so calls are 1 s apart; `eth_getLogs` returns at most 10,000 results and
  spans at most 10,000,000 blocks.
- Some tokens (traded through Uniswap v4 hooks or launchpad accounting) have no mint event and few transfers; their
  holder figures are marked `no_mint_event` and should not be trusted.
- Contract flags come from function selectors in the bytecode: they catch common patterns, not renamed functions.
