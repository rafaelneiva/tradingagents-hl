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
// The page fetches data/<COIN>.json relative to itself; serve that from disk here.
const localFetch = (url, opts) => {
  if (typeof url === "string" && url.startsWith("data/")) {
    const f = path.join(__dirname, url);
    return Promise.resolve(fs.existsSync(f) ? new Response(fs.readFileSync(f)) : new Response("", { status: 404 }));
  }
  return fetch(url, opts);
};
const mod = { exports: {} };
new Function("module", "fetch", src)(mod, localFetch);
const L = mod.exports;

const args = process.argv.slice(2);
const coins = args.filter((a) => isNaN(+a)).map((a) => a.toUpperCase());
const extraLev = args.filter((a) => !isNaN(+a)).map(Number);
const p = (x, d = 1) => (x * 100).toFixed(d) + "%";

(async () => {
  for (const coin of coins.length ? coins : ["BTC", "ETH", "SOL", "HYPE"]) {
    const m = await L.loadMarket(coin);
    const A = L.analyze(m, +m.ctx.midPx);
    const hz = L.HORIZON;
    const since = new Date(A.firstT).toISOString().slice(0, 10);
    console.log(`\n${coin} ${m.ctx.midPx}  maxLev ${A.maxLev}x  horizonte ${hz}h  histórico ${(A.spanDays / 365).toFixed(1)}a desde ${since} (${A.source}${A.fromFile ? "" : ", SEM arquivo data/"})  janelas ${A.n}  base sobe ${p(A.baseUp)}`);
    console.log(`  VEREDITO ${A.dir > 0 ? "SOBE" : "DESCE"} ${p(A.conf)}  (${A.groupLabel}, n=${A.group.length} ≈ ${Math.round(A.group.length / hz)} períodos indep.)  anos a favor ${A.yearsFor}/${A.verdictYears.length}: ${A.verdictYears.map((y) => `${y.year} ${p(A.dir > 0 ? y.side : 1 - y.side, 0)}`).join(" ")}`);
    for (const x of A.metricStats) {
      const edge = x.hit == null ? "" : ` edge ${x.hit - x.base >= 0 ? "+" : ""}${((x.hit - x.base) * 100).toFixed(1)}pp`;
      const hit = (x.watch ? "[observação] " : "") + (x.live ? "ao vivo, fora do placar" : x.hit == null ? "nunca votou" : `acerto ${p(x.hit)} vs base ${p(x.base)}${edge} n=${x.nVotes} anos a favor ${x.yearsFor}/${x.yearsJudged}`);
      console.log(`  ${(x.now > 0 ? "▲" : x.now < 0 ? "▼" : "·")} ${x.name.padEnd(17)} ${x.value.padEnd(38)} ${hit}`);
    }
    console.log(`  mercado agora: ${L.VOL_LABELS[A.volRegime]} (vol 1h/24h ${p(A.volNow, 2)}; terços ${p(A.volCuts[0], 2)} / ${p(A.volCuts[1], 2)}) → simulação em ${A.betGroup.length} momentos parecidos`);
    const levs = [...new Set([5, 10, A.maxLev, ...extraLev.filter((l) => l <= A.maxLev)])].sort((a, b) => a - b);
    for (const lev of levs) {
      const B = L.simulateBet(A.betGroup, A.dir, lev, A.maxLev);
      const avg = L.simulateBet(A.group, A.dir, lev, A.maxLev);
      console.log(`  ${String(lev).padStart(2)}x  liquida a ${p(B.dist, 2)}  P(liq ${hz}h) ${p(B.pLiq, 0)} (média ${p(avg.pLiq, 0)})  P(lucro) ${p(B.pWin, 0)}  médio ${p(B.ev)} da margem`);
    }
  }
})().catch((e) => { console.error(e.message); process.exit(1); });
