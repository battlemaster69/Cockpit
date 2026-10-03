# Crypto Cockpit: project brief for Claude Code

## Owner and goal
Raza (Karachi). A personal crypto tracker for spot investing and swing entries, checked mostly on his phone.
He prefers plain English with technical terms explained, direct answers, and concise pressure-testing.
Not financial advice tooling: it informs decisions, it never trades.

## Repo and hosting
- GitHub repo: `battlemaster69/Cockpit` (public, so GitHub Actions minutes and Pages are free).
- All files live at the **top level** of the repo (an earlier upload nested them in a `crypto-cockpit/` folder, which broke Pages).
- GitHub Pages serves `/docs` from branch `main`. `docs/.nojekyll` must exist (Jekyll failed on the first deploy).
- Secrets (Settings > Secrets > Actions): `COINGECKO_API_KEY` (Demo plan), `ETHERSCAN_API_KEY`, `FRED_API_KEY`, `COINALYZE_API_KEY`, `NTFY_TOPIC`.
- Workflow permissions must be "Read and write".

## Architecture
- `.github/workflows/collect.yml`: runs hourly (`cron: "7 * * * *"`) and on manual dispatch with a `full` input that forces a universe rebuild. GitHub's schedule never fired on this repo in its first hours, so a cron-job.org timer dispatches it hourly at :12 with `auto=true` (README step D2); schedule and `auto` runs skip themselves when `docs/data.json` is under 25 minutes old. Commits outputs back to `main` with a rebase-and-retry loop, because the phone can commit `docs/shortlist.json` at the same time.
- `collector/collect.py`: the whole pipeline. Every module is wrapped so one failing source never stops the run; failures are written to `errors` in `docs/data.json` and shown on the dashboard.
  - **Every run (hourly):** Hyperliquid leverage for BTC, ETH, SOL and all alts; Coinalyze positioning (OI history, liquidations, long/short account ratio, taker buy share) summed across Binance, Bybit and OKX for the majors and the shortlist; shortlist prices with sparklines; shortlist OI history; exchange-wallet flows; ntfy alerts.
  - **Exchange flows** watch each shortlisted coin on its home chain (`refresh_contracts` picks it from CoinGecko's `asset_platform_id`): Ethereum tokens via Etherscan `tokentx`, BNB Chain tokens via `eth_getLogs` on a public node (Etherscan's free plan dropped BNB Chain), and native ETH/BNB/SOL plus Solana tokens via hourly balance snapshots of the exchange wallets (`history.reserves`), where net flow = change in holdings.
  - **Dip strength (hourly, `dip_strength`):** universe prices are now fetched every run (3 CoinGecko calls) for the hourly 7-day sparklines. Market = median top-100 path over 48h; the deepest peak-to-trough drop of `strength.dip_min_pct`+ is "the dip". "Fought back" = drop at least the market's, won back 50%+, bounce off own low beats the market's bounce, sorted by that excess. "Held firm" = volatile coins (dvol 2+, rank 300 or better) that dipped under a third of the market and sit 0 to 15% above pre-dip. Liquidity floor `strength.min_volume_usd`. Validated on the 2026-10-02/03 dip: the owner's picks ZRO, WLD, PUMP, UNI all ranked in "fought back". One alert per dip, `alert_after_hours` after the low.
  - **Early strength** (the owner's priority: "fought back" alone is too late, a coin 15-20% off its low is a chase). Within `early_window_hours` of the market low, flag coins that dropped at least 0.8x the market, beat the market's bounce by `early_min_excess_pct`+, beat the market in 2 of the last 3 hourly moves, and are still under `early_max_bounce_pct` off their low. Flags persist in `history.strength.flags`, show running/holding/faded since the flag, and are scored 24h later into `history.strength.score` (the track record shown on the tab). Alerts only for coins that pumped `pumped_before_pct`+ the week before: in an hour-by-hour replay of the Oct 2-3 dip those 9 flags averaged +6.6% a day later (5 up, 1 down), the 23 without a prior pump averaged -0.2%. Volume (Hyperliquid candles) did not separate winners from fades on that dip, so it isn't used. Retune on the track record, not on one dip.
  - **Move type and OI/market cap** (`annotate_strength`): up to 20 Strength coins get Coinalyze open interest only (`full=False` targets skip liquidations and long/short). OI change since the market low is counted in coins (dollar OI divided by the price move), since dollar OI rises with price on its own: down `squeeze_oi_drop_pct`+ = short squeeze, up `leverage_oi_rise_pct`+ = leverage-led, else spot-led. OI/market cap uses Binance+Bybit+OKX only, so it is a floor (e.g. WLD 13% here vs 42.7% all-exchange in a CoinGlass-based post); `oi_mcap_high_pct` flags it.
- **BNB Chain nodes:** only publicnode and 48 Club served the topic-array `eth_getLogs` queries in Oct 2026 (drpc 400s, blockrazor/defibit/ninicoin limit them, meowrpc lacks the method). Scans save progress per 3,000-block chunk and resume after a failure.
  - The leverage gauge and the "leverage recently flushed" check use the cross-exchange OI when Coinalyze has it, falling back to Hyperliquid; the rules themselves are unchanged.
  - **Every `universe_every_hours` (default 4):** top 500 coins from CoinGecko (stablecoins, wrapped and staked copies excluded), DefiLlama financials mapped by gecko id, automatic scores, stablecoin supply, BTC dominance, FRED macro, CoinMetrics MVRV.
- `docs/index.html`: single-file dashboard, vanilla JS, no build step. Reads `docs/data.json`, `docs/universe.json`, `docs/shortlist.json`. Tabs: Shortlist, Universe, Value, Flows, Macro, Alerts. Tapping a coin opens its detail. Shortlist edits commit `docs/shortlist.json` through the GitHub API using a fine-grained token stored only in the phone's localStorage.
- `data/history.json`, `data/state.json`, `data/cache.json`: the tool's own history (OI, dominance, transfers), alert dedupe state, and cached universe and contract lookups.
- `config.json`: universe size and cadence, exclusions, manual score `overrides` (keyed by CoinGecko id), `unlocks`, `thresholds`, `exchange_wallets` (Ethereum), `exchange_wallets_bsc`, `exchange_wallets_sol`. Wallets come from Dune's spellbook CEX labels (`cex_evms_addresses.sql`, `cex_solana_addresses.sql`) and were checked for on-chain activity; BscScan and Solscan sit behind Cloudflare checks, so verify through Dune rather than the explorers.

## Scoring rules (keep consistent)
Five categories out of 10, total out of 50: Fundamentals, Not priced in, Value capture, Catalyst, Safety.
- Coins in `overrides` keep the owner's manual Fundamentals, Value capture and Catalyst; Not priced in and Safety are always live. The chat score shows as a yellow tick.
- Auto-scored coins: Fundamentals from DefiLlama revenue; Value capture from the share of revenue paid to holders; Catalyst uses trend versus BTC as a stand-in (real catalysts need human judgment).
- Coins without financials are marked "no financials" and their missing categories count as 3, so narrative coins can't outrank revenue coins on momentum alone.
- Entry checklist (5 checks): funding not overheated (8h funding at most 0.01%), leverage recently flushed (OI at least 10% below its 7-day high), coins leaving exchanges (7-day net outflow), no unlock within 14 days, not chasing (7-day gain under 20%). Light: 4 or more passes green, 3 amber, 2 or fewer red, fewer than 3 known grey.

## Hard constraints
- **Stay within free API limits.** CoinGecko Demo: 10,000 calls a month (current design is about 1,500). Etherscan free: 5 calls a second, 100,000 a day, Ethereum only (no BNB Chain). Coinalyze free: 40 calls a minute, each symbol in a request counts as a call (`cz_get` paces this). Use bulk endpoints; do per-coin calls only for the shortlist.
- **Binance blocks GitHub's US runners**, which is why funding comes from Hyperliquid and Binance/Bybit/OKX positioning comes through Coinalyze rather than the exchanges directly.
- **The repo is public:** never commit API keys, tokens, holdings or position sizes.
- **Keep the dashboard phone-first,** with light and dark themes and no build step.

## Status
- Live since 2026-10-03: Pages at https://battlemaster69.github.io/Cockpit/, hourly runs green against the live APIs.
- DefiLlama revenue maps to CoinGecko ids through parent protocols (`lite/protocols2` `parentProtocols`); versions like "Uniswap V3" carry no gecko id of their own.
- Ideas not built yet: order-book liquidity walls (Hyperliquid `l2Book`, Coinbase book), cross-exchange funding (Hyperliquid `predictedFundings`), Deribit put/call and DVOL, Solana whale-transfer list (needs a Helius key), scoring rework (revenue vs market cap; Catalyst overlaps Not priced in).

## Working conventions
- Before pushing a change to the collector, run it locally if possible (`pip install -r requirements.txt`, then `python collector/collect.py --full` with the keys set as environment variables) or write a small mock test.
- After pushing, check the latest Actions run and read its log; fix and re-run until it's green.
- Explain changes to the owner in plain English, briefly.
