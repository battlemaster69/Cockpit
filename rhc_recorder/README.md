# Survivor recorder: Robinhood Chain and Solana

`python rhc_recorder/run.py` records Robinhood Chain (data in `data/`, dashboard file `docs/alpha.json`);
`python rhc_recorder/run.py --chain solana` records Solana (`config_solana.yaml`, data in `data_solana/`,
`docs/alpha_solana.json`, workflow `sol_recorder.yml` every 15 minutes). Same six filters on both chains. Solana sees
~26,600 launches a day, more than GeckoTerminal can list, so it records a time sample of launches, re-checks each at
about 1, 2, 4 and 7 days old (launchpad curve tokens have no liquidity until they graduate), prices new pools on
regular exchanges (often graduations) at once, and checks passers and controls with RugCheck instead of an RPC. Its
timer on cron-job.org: every 15 minutes, `.../actions/workflows/sol_recorder.yml/dispatches`, body
`{"ref":"main","inputs":{"auto":"true"}}`.

## Robinhood Chain

A data recorder, not a trader. It records every Robinhood Chain token that reaches $5,000 of liquidity, labels the
ones that are 2 to 10 days old and still healthy (the survivor filters), matches each passer with a random control,
and runs contract and holder checks on both, so the filters can be tested against random before any money goes in.
The full spec is the "Robinhood Chain Survivor Recorder — Spec" doc.

## How it runs

- `.github/workflows/rhc_recorder.yml`, every 30 minutes. Every run finds new pools (GeckoTerminal serves only 10
  pages, the newest 200 pools; busy hours launch ~200). A run 3.5+ hours after the last snapshot also catches up on
  missed tokens, snapshots every tracked token, labels the filters, assigns passers and controls, and checks them.
- Every run also writes `docs/alpha.json` for the Cockpit's "RH Chain" tab (`board.py`): the passers-vs-controls
  scoreboard, tokens passing now and on 5 of 6 filters (re-priced every run), the filter funnel, recorder health.
- Timer: GitHub's schedule is best-effort on this repo, so a cron-job.org job dispatches it at :07 and :37,
  `POST https://api.github.com/repos/battlemaster69/Cockpit/actions/workflows/rhc_recorder.yml/dispatches`,
  body `{"ref":"main","inputs":{"auto":"true"}}`, same token. An `auto` run skips itself if the last run is under
  20 minutes old.
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
