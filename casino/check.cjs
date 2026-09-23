#!/usr/bin/env node
// Runs the casino page's own logic (the <script> in index.html) against live
// HyperLiquid data and prints the verdict, per-metric hit rates and bet sims.
//
//   node casino/check.cjs            # BTC ETH SOL HYPE
//   node casino/check.cjs BTC 40     # one coin, include a 40x sim
//
// Needs Node 18+ (global fetch). Same code path as the browser, so a metric
// added to METRICS in index.html shows up here with no extra wiring.

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const src = html.match(/<script>\n([\s\S]*?)<\/script>/)[1];
const mod = { exports: {} };
new Function("module", "fetch", src)(mod, fetch);
const L = mod.exports;

const args = process.argv.slice(2);
const coins = args.filter((a) => isNaN(+a)).map((a) => a.toUpperCase());
const extraLev = args.filter((a) => !isNaN(+a)).map(Number);
const p = (x, d = 1) => (x * 100).toFixed(d) + "%";

(async () => {
  for (const coin of coins.length ? coins : ["BTC", "ETH", "SOL", "HYPE"]) {
    const m = await L.loadMarket(coin);
    const A = L.analyze(m, +m.ctx.midPx);
    console.log(`\n${coin} ${m.ctx.midPx}  maxLev ${A.maxLev}x  janelas ${A.n}  base sobe ${p(A.baseUp)}`);
    console.log(`  VEREDITO ${A.dir > 0 ? "SOBE" : "DESCE"} ${p(A.conf)}  (${A.groupLabel}, n=${A.group.length} ≈ ${Math.round(A.group.length / 24)} dias indep.)`);
    for (const x of A.metricStats) {
      const edge = x.hit == null ? "" : ` edge ${x.hit - x.base >= 0 ? "+" : ""}${((x.hit - x.base) * 100).toFixed(1)}pp`;
      const hit = x.live ? "ao vivo, fora do placar" : x.hit == null ? "nunca votou" : `acerto ${p(x.hit)} vs base ${p(x.base)}${edge} n=${x.nVotes}`;
      console.log(`  ${(x.now > 0 ? "▲" : x.now < 0 ? "▼" : "·")} ${x.name.padEnd(17)} ${x.value.padEnd(38)} ${hit}`);
    }
    const levs = [...new Set([5, 10, A.maxLev, ...extraLev.filter((l) => l <= A.maxLev)])].sort((a, b) => a - b);
    for (const lev of levs) {
      const B = L.simulateBet(A.group, A.dir, lev, A.maxLev);
      console.log(`  ${String(lev).padStart(2)}x  liquida a ${p(B.dist, 2)}  P(liq 24h) ${p(B.pLiq, 0)}  P(lucro) ${p(B.pWin, 0)}  médio ${p(B.ev)} da margem`);
    }
  }
})().catch((e) => { console.error(e.message); process.exit(1); });
