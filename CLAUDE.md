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
- `.github/workflows/collect.yml`: runs hourly (`cron: "7 * * * *"`) and on manual dispatch with a `full` input that forces a universe rebuild. Commits outputs back to `main` with a rebase-and-retry loop, because the phone can commit `docs/shortlist.json` at the same time.
- `collector/collect.py`: the whole pipeline. Every module is wrapped so one failing source never stops the run; failures are written to `errors` in `docs/data.json` and shown on the dashboard.
  - **Every run (hourly):** Hyperliquid leverage for BTC, ETH, SOL and all alts; Coinalyze positioning (OI history, liquidations, long/short account ratio, taker buy share) summed across Binance, Bybit and OKX for the majors and the shortlist; shortlist prices with sparklines; shortlist OI history; exchange-wallet flows; ntfy alerts.
  - **Exchange flows** watch each shortlisted coin on its home chain (`refresh_contracts` picks it from CoinGecko's `asset_platform_id`): Ethereum tokens via Etherscan `tokentx`, BNB Chain tokens via `eth_getLogs` on a public node (Etherscan's free plan dropped BNB Chain), and native ETH/BNB/SOL plus Solana tokens via hourly balance snapshots of the exchange wallets (`history.reserves`), where net flow = change in holdings.
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
