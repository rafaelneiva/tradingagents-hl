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
// Also writes casino/data/<COIN>-15m.json (candles only) for the page's "Leitura" filter.
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

const BAR = { "15m": 15 * 60e3, "1h": H };

// Pages are fixed 1000-bar slices, fetched 8 at a time (a first 4y sync of 15m is ~146 pages).
async function binanceKlines(symbol, from, to, interval = "1h") {
  const bar = BAR[interval];
  const starts = [];
  for (let t = from; t < to; t += 1000 * bar) starts.push(t);
  const out = [];
  for (let b = 0; b < starts.length; b += 8) {
    const pages = await Promise.all(starts.slice(b, b + 8).map(async (t) => {
      const r = await fetch(`${BINANCE}?symbol=${symbol}&interval=${interval}&startTime=${t}&limit=1000`);
      if (!r.ok) throw new Error("Binance " + r.status);
      return r.json();
    }));
    for (const page of pages) for (const k of page) out.push([k[0], +k[1], +k[2], +k[3], +k[4]]);
    await sleep(100);
  }
  const seen = new Set();
  return out.filter((k) => !seen.has(k[0]) && seen.add(k[0])).sort((a, b) => a[0] - b[0]);
}

async function hlCandles(coin, from, to, interval = "1h") {
  const cs = await hl({ type: "candleSnapshot", req: { coin, interval, startTime: from, endTime: to } });
  return cs.map((k) => [k.t, +k.o, +k.h, +k.l, +k.c]);
}

// 15m candles for the "Leitura" filter (30m is built from these in the page). Candles only;
// funding lives in the 1h file. HYPE gets HL's latest 5000 bars (~52 days) and grows from there.
async function sync15m(coin, src, cutoff, now) {
  const file = path.join(DIR, `${coin}-15m.json`);
  let d = { coin, ...src, interval: "15m", candles: [] };
  try { d = JSON.parse(fs.readFileSync(file, "utf8")); } catch { /* first run */ }
  const bar = BAR["15m"];
  const from = d.candles.length ? d.candles[d.candles.length - 1][0] + bar : cutoff;
  const fresh = src.source === "binance" ? await binanceKlines(src.symbol, from, now, "15m") : await hlCandles(coin, from, now, "15m");
  d.candles = d.candles.concat(fresh.filter((k) => k[0] >= from && k[0] + bar <= now));   // concat: 140k rows overflow push(...)
  d.candles = d.candles.filter((k) => k[0] >= cutoff);
  d.updated = now;
  fs.writeFileSync(file + ".tmp", JSON.stringify(d));
  fs.renameSync(file + ".tmp", file);
  return d.candles.length;
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
  d.candles = d.candles.concat(fresh.filter((k) => k[0] >= fromC && k[0] + H <= now));   // new, closed candles only
  d.candles = d.candles.filter((k) => k[0] >= cutoff);

  const fromF = d.funding.length ? d.funding[d.funding.length - 1][0] + 1 : cutoff;
  d.funding = d.funding.concat(await hlFunding(coin, fromF));
  d.funding = d.funding.filter((f) => f[0] >= cutoff);

  d.updated = now;
  fs.mkdirSync(DIR, { recursive: true });
  fs.writeFileSync(file + ".tmp", JSON.stringify(d));
  fs.renameSync(file + ".tmp", file);

  const n15 = await sync15m(coin, src, cutoff, now);

  const first = d.candles.length ? new Date(d.candles[0][0]).toISOString().slice(0, 10) : "—";
  const firstF = d.funding.length ? new Date(d.funding[0][0]).toISOString().slice(0, 10) : "—";
  console.log(`${new Date().toISOString()} ${coin}: ${d.candles.length} candles 1h desde ${first} (${d.source}), ${n15} candles 15m, ${d.funding.length} funding desde ${firstF}`);
}

(async () => {
  const coins = process.argv.slice(2).map((c) => c.toUpperCase());
  for (const coin of coins.length ? coins : Object.keys(SOURCES)) {
    try { await syncCoin(coin); } catch (e) { console.error(`${coin}: ${e.message}`); process.exitCode = 1; }
  }
})();
