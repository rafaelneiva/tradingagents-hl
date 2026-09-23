#!/usr/bin/env node
// Builds casino/data/<COIN>.json: ~4 years of 1h candles + the full HyperLiquid
// funding history. HL only serves the latest 5000 candles per interval, so the
// long history comes from Binance spot where the coin is listed there.
//
// Incremental: re-running only appends what is new. The page loads the file
// and fetches just the gap since the last sync, so the browser never has to
// page through years of data (HL funding pages are rate-limit heavy).
//
//   node casino/sync.cjs          # all coins
//   node casino/sync.cjs BTC      # one coin
//
// pm2 runs it hourly as `casino-sync`.

const fs = require("fs");
const path = require("path");

const H = 3600e3;
const DAY = 86400e3;
const YEARS = 4;
const WARMUP_DAYS = 30;                     // indicator warm-up before the 4y window
const HL = "https://api.hyperliquid.xyz/info";
const BINANCE = "https://data-api.binance.vision/api/v3/klines";
const DIR = path.join(__dirname, "data");

const SOURCES = {
  BTC: { source: "binance", symbol: "BTCUSDT" },
  ETH: { source: "binance", symbol: "ETHUSDT" },
  SOL: { source: "binance", symbol: "SOLUSDT" },
  HYPE: { source: "hyperliquid" },          // not on Binance spot; HL depth only
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function hl(body) {
  for (let attempt = 1; attempt <= 5; attempt++) {
    const r = await fetch(HL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (r.status === 429) { await sleep(10e3 * attempt); continue; }
    if (!r.ok) throw new Error("HyperLiquid " + r.status);
    return r.json();
  }
  throw new Error("HyperLiquid rate limit persisted");
}

async function binanceKlines(symbol, from, to) {
  const out = [];
  let t = from;
  while (t < to) {
    const r = await fetch(`${BINANCE}?symbol=${symbol}&interval=1h&startTime=${t}&limit=1000`);
    if (!r.ok) throw new Error("Binance " + r.status);
    const page = await r.json();
    if (!page.length) break;
    for (const k of page) out.push([k[0], +k[1], +k[2], +k[3], +k[4]]);
    if (page.length < 1000) break;
    t = page[page.length - 1][0] + H;
    await sleep(150);
  }
  return out;
}

async function hlCandles(coin, from, to) {
  const cs = await hl({ type: "candleSnapshot", req: { coin, interval: "1h", startTime: from, endTime: to } });
  return cs.map((k) => [k.t, +k.o, +k.h, +k.l, +k.c]);
}

async function hlFunding(coin, from) {
  const out = [];
  let t = from;
  for (;;) {
    const page = await hl({ type: "fundingHistory", coin, startTime: t });
    if (!page.length) break;
    for (const f of page) out.push([f.time, +f.fundingRate]);
    if (page.length < 500) break;
    t = page[page.length - 1].time + 1;
    await sleep(2500);                      // ~45 weight/page vs 1200/min budget
  }
  return out;
}

async function syncCoin(coin) {
  const src = SOURCES[coin];
  if (!src) throw new Error("Moeda sem fonte configurada: " + coin);
  const file = path.join(DIR, coin + ".json");
  let d = { coin, ...src, candles: [], funding: [] };
  try { d = JSON.parse(fs.readFileSync(file, "utf8")); } catch { /* first run */ }

  const now = Date.now();
  const cutoff = now - (YEARS * 365 + WARMUP_DAYS) * DAY;

  const fromC = d.candles.length ? d.candles[d.candles.length - 1][0] + H : cutoff;
  const fresh = src.source === "binance" ? await binanceKlines(src.symbol, fromC, now) : await hlCandles(coin, fromC, now);
  d.candles.push(...fresh.filter((k) => k[0] + H <= now));      // closed candles only
  d.candles = d.candles.filter((k) => k[0] >= cutoff);

  const fromF = d.funding.length ? d.funding[d.funding.length - 1][0] + 1 : cutoff;
  d.funding.push(...(await hlFunding(coin, fromF)));
  d.funding = d.funding.filter((f) => f[0] >= cutoff);

  d.updated = now;
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(file + ".tmp", JSON.stringify(d));
  fs.renameSync(file + ".tmp", file);

  const first = d.candles.length ? new Date(d.candles[0][0]).toISOString().slice(0, 10) : "—";
  const firstF = d.funding.length ? new Date(d.funding[0][0]).toISOString().slice(0, 10) : "—";
  console.log(`${new Date().toISOString()} ${coin}: ${d.candles.length} candles desde ${first} (${d.source}), ${d.funding.length} funding desde ${firstF}`);
}

(async () => {
  const coins = process.argv.slice(2).map((c) => c.toUpperCase());
  for (const coin of coins.length ? coins : Object.keys(SOURCES)) {
    try { await syncCoin(coin); } catch (e) { console.error(`${coin}: ${e.message}`); process.exitCode = 1; }
  }
})();
