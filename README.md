# Crypto Cockpit v2

A personal crypto tracker. It runs on GitHub's servers (free, so your PC can stay off), shows a phone-friendly dashboard, and pushes alerts to your phone through the ntfy app.

## What's new in v2
- **Universe of the top 500 coins**, scored automatically every 4 hours. Stablecoins and wrapped or staked copies are left out.
- **Tap any coin** to see its report card, entry checklist, financials, leverage (funding and open interest), market data, and exchange flows.
- **Shortlist from your phone.** Tap "Add to shortlist" on any coin. Shortlisted coins refresh **every hour**, with open-interest history, exchange-wallet tracking, and alerts.
- Ethereum contracts for wallet tracking are found automatically, so there's nothing to look up by hand.
- **Positioning across Binance, Bybit and OKX** (via Coinalyze): long/short account ratio, liquidations, total open interest and taker buy share, for BTC, ETH, SOL and every shortlisted coin.
- **Strength tab:** after any market dip of 2% or more (median of the top 100) in the last 48 hours, ranks which liquid coins took the hit and fought back harder than the market, and which barely dipped. Coins that pumped the week before and still fought back get a "pumped" tag. **Early strength** flags the fight-back within 10 hours of the low while the bounce is still under 10%, alerts your phone that hour (coins that pumped before only), then follows each flag (running, holding or faded) and scores it after 24 hours into a track record. Coins already 15%+ off their low are tagged "extended". Settings under `strength` in `config.json`.
- **Radar tab:** coins starting a move on their own in a calm market. Every 15 minutes it scans ~175 liquid coins (Binance hourly volume and aggressive-buy share, Coinalyze funding): "breaking out" = new 24h high on 3x+ normal volume while still only 2-5% up and funding at or below zero (phone alert; right about 1 in 5 in a 21-day backtest, 3.6x random). Hourly, "loading" lists coins whose futures bets grew 10%+ in 12h with flat price. Each flag is tracked and scored after 24h. Settings under `radar` in `config.json`.
- **Exchange wallets on three chains:** Ethereum and BNB Chain tokens transfer by transfer; native coins (ETH, BNB, SOL) and Solana tokens through hourly exchange balances.

## How often things update (and why it stays free)

| What | How often | API cost per day |
|---|---|---|
| Leverage gauges, shortlist prices, OI, funding | Every hour | Hyperliquid 24 calls, CoinGecko 24 calls |
| Positioning (L/S ratio, liquidations, OI, taker flow) | Every hour | Coinalyze about 100–150 calls per run, paced under its 40 a minute limit |
| Shortlist exchange-wallet flows | Every hour | Etherscan about 14 calls per Ethereum token per run (100,000 a day allowed); free public BNB Chain and Solana nodes |
| Universe of 500 coins, financials, macro | Every 4 hours | CoinGecko about 20 calls, DefiLlama about 24, FRED 18, CoinMetrics 6 |

CoinGecko works out to roughly 1,500 calls a month against a free limit of 10,000. GitHub Actions is free and unlimited for public repos.

## How coins are scored
Each coin gets five categories out of 10, for a total out of 50.

| Category | Automatic rule |
|---|---|
| Fundamentals | Yearly revenue size from DefiLlama, with TVL as a weaker fallback |
| Not priced in | Distance below the all-time high, minus recent pumps, plus or minus valuation (market cap / revenue) |
| Value capture | Share of revenue paid to token holders, minus a penalty for future dilution (FDV vs market cap) |
| Catalyst (trend) | Strength versus BTC over 30 and 200 days. Real catalysts can't be automated, so this uses momentum as a stand-in |
| Safety | Market cap size, volatility, and dilution |

- **Coins we scored in chat** keep your manual Fundamentals, Value capture and Catalyst (see `overrides` in `config.json`). Their original chat score shows as a yellow tick.
- **Coins with no financials** are marked "no financials". Their missing categories count as 3 ("unproven") in the total, so pure narrative coins can't outrank coins with real revenue on momentum alone.
- **Entry light:** 4 or more of the 5 checks pass is green, 3 is amber, 2 or fewer is red. Grey means not enough data.

## Setup

If you already set up v1, only steps **B**, **D** and **F** are new.

### A. Keys (same as v1)
CoinGecko Demo key, Etherscan key, FRED key, a free Coinalyze key (coinalyze.net, Account, API key), and an ntfy topic name. Add them as repo secrets:
`COINGECKO_API_KEY`, `ETHERSCAN_API_KEY`, `FRED_API_KEY`, `COINALYZE_API_KEY`, `NTFY_TOPIC`.

### B. Replace the files
Upload everything in this folder to the **top level** of your repo, replacing the old files. The repo's main page should show `docs`, `collector`, `data`, `.github` and `config.json` directly.
If the hidden `.github` or `docs/.nojekyll` files don't upload, create them by hand with **Add file**, then **Create new file**.

### C. Settings (same as v1)
- In **Settings**, then **Actions**, then **General**, set Workflow permissions to **Read and write**.
- In **Settings**, then **Pages**, deploy from branch `main`, folder `/docs`.

### D. First full run
Open **Actions**, then **Collect market data**, then **Run workflow**. Tick **Force a full universe refresh** and run it. It takes about 3–5 minutes. After that it runs every hour by itself, and rebuilds the universe every 4 hours.

### D2. Hourly backup timer (recommended)
GitHub's built-in schedule is best-effort and can skip hours, especially on new repos. A free timer on cron-job.org starts the run every hour as a backup; if GitHub's own run already happened, the extra one skips itself.
1. On GitHub, create a **fine-grained token** for this repo only, with **Actions: Read and write** and nothing else.
2. On cron-job.org, create a job: URL `https://api.github.com/repos/YOUR-USERNAME/REPO-NAME/actions/workflows/collect.yml/dispatches`, every hour at minute 12, method **POST**.
3. Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer YOUR-TOKEN`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`.
4. Body: `{"ref":"main","inputs":{"auto":"true"}}`. A test run should answer **204**, and a run appears under Actions.

### D3. Radar timer (every 15 minutes)
A second cron-job.org job, same token and headers as D2, but URL `https://api.github.com/repos/YOUR-USERNAME/REPO-NAME/actions/workflows/radar.yml/dispatches`, schedule every 15 minutes, same body `{"ref":"main","inputs":{"auto":"true"}}`.

### E. Phone
- Open `https://YOUR-USERNAME.github.io/REPO-NAME/` and add it to your home screen.
- In the ntfy app, subscribe to your topic.

### F. Turn on shortlist editing (one time)
The dashboard needs a GitHub token to save your shortlist into the repo.
1. On GitHub, go to **Settings**, then **Developer settings**, then **Personal access tokens**, then **Fine-grained tokens**, then **Generate new token**.
2. Give it a name like "cockpit", and set an expiry (one year is fine).
3. Under **Repository access**, choose **Only select repositories** and pick your cockpit repo.
4. Under **Permissions**, set **Contents** to **Read and write**. Leave everything else as "No access".
5. Generate it and copy it.
6. On the dashboard, tap the gear icon, paste the token, and tap **Save and test**. It should say "Connected".

The token is stored only in your phone's browser. Because it can only edit files in this one repo, the worst case if your phone were lost is someone editing your cockpit. You can revoke it on GitHub at any time.

## Customise (`config.json`)
- `universe_size`: number of coins (default 500).
- `universe_every_hours`: how often the universe rebuilds (default 4).
- `exclude_symbols` / `exclude_name_keywords`: coins to keep out of the universe.
- `overrides`: your manual scores per coin (keyed by CoinGecko ID).
- `unlocks`: upcoming token unlocks. The checklist fails a coin within 14 days of one.
- `thresholds`: funding levels, flush size, whale alert size, liquidation alert size (`liq_alert_pct_of_oi`, % of open interest liquidated in 24h), alert cooldown.
- `exchange_wallets`, `exchange_wallets_bsc`, `exchange_wallets_sol`: exchange wallets to watch on Ethereum, BNB Chain and Solana. All come from Dune's open exchange-address labels (github.com/duneanalytics/spellbook) and were checked for activity on their chain.

## Good to know
- **Funding comes from Hyperliquid; positioning from Binance, Bybit and OKX.** Shortlisted coins also get open interest across those three, so the "Leverage recently flushed" check works from day one. Universe coins use Hyperliquid only, and one snapshot a day.
- **Each coin is watched on its home chain:** JUP and SOL on Solana, BNB and CAKE on BNB Chain, most other tokens on Ethereum. Bridged copies on other chains are ignored because they're mostly exchanges moving their own stock.
- **Exchange balances need a day of history** before the 7-day change counts toward the entry checklist. No active OKX wallet is labelled on Solana yet, so OKX is missing there.
- **Everything is public** (it's a public repo). It only holds market data, so never add holdings or position sizes.
- **If a source fails,** the dashboard names it and the rest keeps working.
- **If a run shows an error,** the Actions log names the exact module.

Not financial advice.
