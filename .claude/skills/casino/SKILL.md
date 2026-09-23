---
name: casino
description: Use for the "Sobe ou Desce" casino screen (casino/index.html) — when the user asks "vai subir ou descer?", "o que a mesa tá dizendo", "sobe o cassino", "tá de pé?", wants to add/test a new metric ("nova métrica", "testa essa ideia"), or asks about a leveraged 24h bet on a HyperLiquid coin.
---

# Sobe ou Desce (casino screen)

A single static page, `casino/index.html`: it calls the public HyperLiquid API from the browser,
so it needs no backend. It gives a 24h up/down call, a calibrated confidence and a leverage/liquidation
sim, and it keeps a bet ledger in localStorage. `casino/check.cjs` runs the **same** `<script>`
in node, so the page and the terminal never disagree.

The user knows this is a game ("jogar a moeda"). The value of the page is being honest about the odds.
Never call the output "certeza".

## Environment (WSL, from the Windows harness)

- Run via `wsl -d Ubuntu bash <script.sh>` with the script written to the scratchpad. Inline quoting through PowerShell → wsl breaks.
- Put node in PATH yourself: `export PATH=/home/kabum/.nvm/versions/node/v20.20.1/bin:$PATH`.
  pm2 lives only under v20; v24 has no pm2, which is why `pm2: command not found` does NOT mean nothing is running.
- The pm2 daemon starts on boot (`pm2-kabum` systemd unit) and resurrects `~/.pm2/dump.pm2`.

## "Sobe tudo" / "tá de pé?"

1. `pm2 ls`: expected processes are `casino` (static server, port 8777) plus the atendimento stack
   (`atendimento-agent`, `-scheduler`, `-tunnel`, `-web`). Report what is already online before starting anything.
2. If `casino` is missing: `pm2 serve /home/kabum/tradingagents-hl/casino 8777 --name casino && pm2 save`.
   Check it with `curl -s -o /dev/null -w "%{http_code}" http://localhost:8777/` → 200.
3. If an atendimento process is missing: `pm2 resurrect`, or `pm2 start /home/kabum/atendimento/ecosystem.config.cjs`.
   Postgres is systemd (`pg_isready`), not pm2.
4. **Never** start `/home/kabum/nebulosa-protocol/ecosystem.config.cjs` without asking. It launches Aethron
   bots that trade real money through agent wallets.
5. Tell the user the URL: http://localhost:8777. pm2 serves the file on each request, so an edit to index.html
   needs only a browser refresh, not a restart.

## "Vai subir ou descer?" in chat

Run `node casino/check.cjs <COIN> [leverage]` from the repo root, then answer in 3–5 lines:
- the verdict, its % and the n behind it (≈ days independent = n/24)
- the base rate
- which metrics voted
- at the leverage the user mentioned: liquidation distance, P(liquidated in 24h), average result

If the confidence is under 55%, say plainly that it's a coin flip.

## Adding a metric (the lab)

Add an object to `METRICS` in `index.html`:

```js
{ id: "x", name: "Nome curto", desc: "regra em uma linha → sobe/desce",
  vote: (S, i) => +1 | -1 | 0,     // use ONLY S.*[k] for k <= i (no lookahead)
  value: (S, i) => "texto do card" }
// live: true + vote(S, i, X) → for data with no history (e.g. X.imb): shown but excluded from the score
```

- `S` holds hourly arrays: `t o h l c` (the last candle is the open one, priced at the live mid),
  `fr` (hourly funding), `f8` (8h avg funding), `ema20 ema50 rsi z`. A new series goes in `buildSeries`.
  New raw data (another API call) goes in `loadMarket`.
- After adding, run `node casino/check.cjs` for all four coins and report the new metric's `acerto vs base` and `edge`.

**Judging a metric honestly:**
- Edge within ±2pp = noise.
- The page is in-sample, and every metric tried is another lottery ticket. A metric earns trust only when its edge
  has the same sign on most coins AND holds on an older period. For an older period, point `loadMarket`'s
  start further back: `candleSnapshot` caps at 5000 bars per call, so paginate.
- A consistently negative edge is a real finding ("funciona ao contrário"). Don't flip the rule silently;
  tell the user.
- Adding a scored metric changes the score buckets, so the headline % shifts for every coin. Mention it.

## Known facts (2026-09-23, last ~208 days of 1h data)

- 24h base rate for going up ≈ 52–53% on BTC/ETH/SOL/HYPE.
- Trend (EMA20>EMA50) and 24h momentum hit **below** base: a mean-reverting regime.
- Funding contrarian: +4pp on BTC, noise to negative on SOL/HYPE, so no consistent edge.
- BTC at 40x (isolated): liquidation is 1.25% away, and ≈50% of 24h windows touch it regardless of direction.
