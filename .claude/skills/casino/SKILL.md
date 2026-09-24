---
name: casino
description: Use for the "Sobe ou Desce" casino screen (casino/index.html) — when the user asks "vai subir ou descer?", "o que a mesa tá dizendo", "sobe o cassino", "tá de pé?", wants to add/test a new metric ("nova métrica", "testa essa ideia"), or asks about a leveraged 24h bet on a HyperLiquid coin.
---

# Sobe ou Desce (casino screen)

A single static page, `casino/index.html`, with no backend. It gives a **12h** up/down call (`HORIZON`),
a calibrated confidence and a leverage/liquidation sim, and it keeps a bet ledger in localStorage.
`casino/check.cjs` runs the **same** `<script>` in node, so the page and the terminal never disagree.

**Data:**
- HL serves only the latest 5000 candles (~208 days of 1h). `casino/sync.cjs` builds `casino/data/<COIN>.json`
  (gitignored) with ~4 years of 1h candles: Binance spot for BTC/ETH/SOL, HL for HYPE (which only exists
  since late 2024). It also stores the full HL funding history, available from 2023-05.
- pm2 `casino-sync` reruns it hourly and incrementally.
- The page loads that file, then fetches only the gap to now: Binance or HL candles plus HL funding.
  Live price, current funding and the book always come from HL.
- Without the file (e.g. opened as `file://`), the page falls back to HL's 208 days and says so in the footer.
- First full sync takes ~10 min, because HL funding pages are rate-limit heavy (2.5 s pause per page).

The user knows this is a game ("jogar a moeda"). The value of the page is being honest about the odds.
Never call the output "certeza".

## Environment (WSL, from the Windows harness)

- Run via `wsl -d Ubuntu bash <script.sh>` with the script written to the scratchpad. Inline quoting through PowerShell → wsl breaks.
- Put node in PATH yourself: `export PATH=/home/kabum/.nvm/versions/node/v20.20.1/bin:$PATH`.
  pm2 lives only under v20; v24 has no pm2, which is why `pm2: command not found` does NOT mean nothing is running.
- The pm2 daemon starts on boot (`pm2-kabum` systemd unit) and resurrects `~/.pm2/dump.pm2`.

## "Sobe tudo" / "tá de pé?"

1. `pm2 ls`: expected processes are `casino` (static server, port 8777) and `casino-sync`
   (hourly cron; it shows `stopped` between runs, which is normal), plus the atendimento stack
   (`atendimento-agent`, `-scheduler`, `-tunnel`, `-web`). Report what is already online before starting anything.
2. If `casino` is missing: `pm2 serve /home/kabum/tradingagents-hl/casino 8777 --name casino && pm2 save`.
   Check it with `curl -s -o /dev/null -w "%{http_code}" http://localhost:8777/` → 200.
   If `casino-sync` is missing: `pm2 start casino/sync.cjs --name casino-sync --cron-restart "7 * * * *" --no-autorestart && pm2 save`
   (run from the repo root). Check the last run with `pm2 logs casino-sync --lines 5 --nostream`.
3. If an atendimento process is missing: `pm2 resurrect`, or `pm2 start /home/kabum/atendimento/ecosystem.config.cjs`.
   Postgres is systemd (`pg_isready`), not pm2.
4. **Never** start `/home/kabum/nebulosa-protocol/ecosystem.config.cjs` without asking. It launches Aethron
   bots that trade real money through agent wallets.
5. Tell the user the URL: http://localhost:8777. pm2 serves the file on each request, so an edit to index.html
   needs only a browser refresh, not a restart.

## "Vai subir ou descer?" in chat

Run `node casino/check.cjs <COIN> [leverage]` from the repo root, then answer in 3–5 lines:
- the verdict, its % and the n behind it (≈ independent periods = n/12)
- in how many years the verdict's side won (the "anos a favor" line). If that's split, the % is one regime, not a pattern.
- the base rate
- which metrics voted
- at the leverage the user mentioned: liquidation distance, P(liquidated in 24h), average result

If the confidence is under 55%, say plainly that it's a coin flip.

## "Como estou indo?" / real trades

- The page's "Você vs a mesa" section, and `node casino/trades.cjs [days] [0x...]` in the terminal, pull the wallet's HL fills
  (public `userFills`, no key) and rebuild round trips: flat → position → flat, where a flip closes one trade and opens the next.
- PnL = closedPnl − fees, without funding.
- For each trade they show the mesa's call as of the entry: votes at the last closed hour, and buckets built only from
  windows already resolved by then. Then whether the mesa was right 12h later.
- The wallet address lives in `casino/data/wallet.json` (gitignored) and the browser's localStorage.
  **Never commit it or put it in issues/commits**: the repo is public, and the address would tie the user to their funds.
- The wallet also holds hundreds of old Aethron bot trades (Mar–Sep 2026). For manual trading, judge the recent period
  (7d/30d), not "tudo".
- Answer with the numbers, then the sample size. Under ~30 closed trades, a/favor vs contra and win rate are anecdotes.
  Say which trade carries the result.

## Adding a metric (the lab)

Add an object to `METRICS` in `index.html`:

```js
{ id: "x", name: "Nome curto", desc: "regra em uma linha → sobe/desce",
  vote: (S, i) => +1 | -1 | 0,     // use ONLY S.*[k] for k <= i (no lookahead)
  value: (S, i) => "texto do card" }
// live: true + vote(S, i, X) → for data with no history (e.g. X.imb): shown but excluded from the score
// watch: true → backtested and shown ("em observação") but excluded from the score. New ideas start here;
//               promote to the score only if the edge holds across coins and years.
```

The bet panel simulates on `betGroup`: windows with the same score group AND the same volatility tercile as now
(trailing 24h std of 1h returns, `S.vol24`). If that's under `MIN_BUCKET`, it uses the same tercile alone.
"média" next to P(liq) is the same bet over the whole score group.

**TP/SL mode** (bet panel toggle; the user trades this way, usually TP 20% / SL 10% of margin):
- The side defaults to the mesa's; the user can flip it. Side resets on a coin switch.
- TP and SL each take a price or a % of margin (ROE). Whichever was typed last is the anchor, and the other follows the live mid.
- `simulateBracket` walks each past entry in `betGroup` hour by hour until TP, SL (or liquidation, if the SL is beyond it)
  or `BRACKET_MAX_H` (72h). The same candle touching both counts as SL.
- "precisa" = (SL + fees) / (TP + SL), the TP hit rate needed to break even.
- The ledger records TP/SL bets with their prices and resolves them on the first 1h candle that touches TP, SL or liquidation.
- Known result: with 2:1 brackets, TP hits first ≈ 1/3 of the time for any direction, i.e. a coin flip. The average is ≈ −fees.
  Higher leverage means tighter price brackets and a bigger fee share.

- `S` holds hourly arrays: `t o h l c` (the last candle is the open one, priced at the live mid),
  `fr` (hourly funding), `f8` (8h avg funding), `ema20 ema50 rsi z`. A new series goes in `buildSeries`.
  New raw data (another API call) goes in `loadMarket`.
- After adding, run `node casino/check.cjs` for all four coins and report the new metric's `acerto vs base` and `edge`.

**Judging a metric honestly:**
- Edge within ±2pp = noise.
- The page is in-sample, and every metric tried is another lottery ticket. A metric earns trust only when its edge
  has the same sign on most coins AND on most years ("a favor em X de Y anos"; a year needs ≥ 8 independent periods of votes).
- A consistently negative edge is a real finding ("funciona ao contrário"). Don't flip the rule silently;
  tell the user.
- Adding a scored metric changes the score buckets, so the headline % shifts for every coin. Mention it.

## Known facts

2026-09-23, **12h horizon, 4.1 years** (2022-08 → 2026-09, Binance 1h; HYPE only 0.6y):
- 12h base rate for going up: BTC 51.5%, ETH 50.9%, SOL 49.6%.
- Trend (EMA20>EMA50) hits below base in **0 of 5 years** on BTC, ETH and SOL. 24h momentum does too on ETH/SOL (1/5 on BTC).
  Following short-term trend loses consistently at 12h. This is the most robust finding so far.
- "Esticado" (Bollinger ±2σ, fade) is the only voter that helps consistently: BTC +2.2pp 5/5 years, ETH +4.1pp 5/5, SOL +1.7pp 4/5.
- Funding contrarian: noise (1/4 to 3/4 years). RSI: mixed.
- Score −2 (trend + momentum down) → SOBE: BTC 53.5% (5/5 years, 51–56%), ETH 55.2% (5/5), SOL 51.0% (coin flip).
- The edge is ~2–4pp. Even so, the average result is negative at every leverage on BTC/ETH/SOL after taker fees.
  BTC 40x in 12h: 38% liquidated, average −21% of margin.

2026-09-24, candidate data tested for the board (12h, Binance 1h 2022-08 → 2026-09, BTC/ETH/SOL):
- Tested and rejected as direction votes, all noise or inconsistent:
  - daily EMA50/200 regime;
  - taker-buy flow z-score (a CVD proxy, from Binance `tbv`);
  - 24h volume spike (fade the 4h move);
  - BTC 4h move as a lead for alts;
  - weekday (fit ≤2024, tested 2025+).
- Borderline, worth a watch card but not the score:
  - 7-day momentum faded (+1.7pp, 5/5 years on BTC and ETH; SOL the opposite);
  - Fear & Greed extreme, contrarian (alternative.me, free daily since 2018): BTC +1.4pp 4/4 years against each
    year's base, but −0.3pp against the pooled base the page uses. It fires with the year's trend, so treat it as weak.
    ETH/SOL are weaker still;
  - hour of day: out-of-sample +0.7 (BTC) / +1.9pp (ETH, SOL).
- The estimated liquidation map (Aethron liq-map) was already falsified as a direction driver in 2026-06; don't re-add it.
- **What matters for liquidation is volatility, not direction.** P(touch −1.25% within 12h) by trailing 24h volatility
  tercile (calm / normal / agitated):
  - BTC: 24 / 35 / 45%
  - ETH: 33 / 48 / 59%
  - SOL: 50 / 63 / 70%
  
  The bet panel's P(liq) ignores this today.
- Not backtestable for free (history must be recorded): open interest, long/short ratio, real liquidation events
  (Binance `forceOrder` stream, capped at 1 per symbol per second).

2026-09-24, finer candles (Binance 15m/30m/1h, 4y, BTC/ETH/SOL):
- Hit rate: RSI and Esticado (mean reversion) at 1h–4h horizons hit **+4 to +6pp** above base, 5/5 years,
  on every coin and candle size. Finer candles make it slightly stronger. Trend and momentum lose 0/5 years everywhere.
- **But it doesn't pay.** The average return per trade in the vote's direction is ≈ 0 gross (−0.05% to +0.07% of price).
  Reversions win often but small; the failures are trends that run. After HL taker fees (0.09% round trip) every
  combination is negative. With maker fees, only SOL 15m RSI is barely positive, one coin only, so treat it as noise.
  This matches Aethron's "scalp: no edge" verdict. Don't switch the mesa to 15m, and never sell a hit rate as money.
- TP/SL resolution: with 1h candles, TP and SL landing in the same candle happens in ≤1% of entries at 10x, and in
  1.7–6.2% at 20x (SOL worst). That shaves ~1–2pp off P(TP) at 20x. 15m candles would fix it; at 10x it's irrelevant.

2026-09-23, 24h horizon, last ~208 days only (superseded by the 4-year run above):
- 24h base rate for going up ≈ 52–53% on BTC/ETH/SOL/HYPE.
- Trend (EMA20>EMA50) and 24h momentum hit **below** base: a mean-reverting regime.
- Funding contrarian: +4pp on BTC, noise to negative on SOL/HYPE, so no consistent edge.
- BTC at 40x (isolated): liquidation is 1.25% away, and ≈50% of 24h windows touch it regardless of direction.
