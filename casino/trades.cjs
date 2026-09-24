#!/usr/bin/env node
// Lists the wallet's real HyperLiquid trades with what the mesa said at each entry
// (same code as the page's "Você vs a mesa"). Wallet comes from
// casino/data/wallet.json (gitignored) or the second argument.
//
//   node casino/trades.cjs            # last 30 days
//   node casino/trades.cjs 7          # last 7 days (0 = all)
//   node casino/trades.cjs 30 0x...   # another wallet

const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
const src = html.match(/<script>\n([\s\S]*?)<\/script>/)[1];
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

const days = process.argv[2] != null ? +process.argv[2] : 30;
let user = process.argv[3];
if (!user) {
  try { user = JSON.parse(fs.readFileSync(path.join(__dirname, "data/wallet.json"), "utf8")).address; } catch { /* none */ }
}
if (!/^0x[0-9a-fA-F]{40}$/.test(user || "")) { console.error("Sem carteira: crie casino/data/wallet.json {\"address\":\"0x...\"} ou passe o endereço."); process.exit(1); }

const p = (x, d = 1) => (x * 100).toFixed(d) + "%";
const usd = (x) => (x >= 0 ? "+" : "-") + "$" + Math.abs(x).toFixed(2);

(async () => {
  const since = days ? Date.now() - days * 86400e3 : 0;
  const trades = L.roundTrips(await L.loadFills(user)).filter((t) => t.start >= since);
  for (const c of new Set(trades.map((t) => t.coin))) {
    if (c.includes(":")) continue;
    try { L.replayTrades(await L.loadMarket(c), trades.filter((t) => t.coin === c)); } catch { /* no mesa */ }
  }
  const brt = (t) => new Date(t - 3 * 3600e3).toISOString().slice(5, 16).replace("T", " ");
  for (const t of trades) {
    const ret = t.exit ? t.dir * (t.exit / t.entry - 1) : null;
    const mesa = t.mesa ? `${t.mesa.dir > 0 ? "SOBE" : "DESCE"} ${p(t.mesa.conf, 0)} ${t.dir === t.mesa.dir ? "a favor" : "contra"} · 12h ${t.mesa.right12 == null ? "aguardando" : t.mesa.right12 ? "acertou" : "errou"}` : "sem mesa";
    console.log(`${brt(t.start)} BRT ${t.coin.padEnd(5)} ${t.dir > 0 ? "LONG " : "SHORT"} ${t.open ? "ABERTA" : ((t.end - t.start) / 3600e3).toFixed(1) + "h"}  ${t.entry.toFixed(4)} → ${t.exit?.toFixed(4) ?? "-"}  ${ret == null ? "" : p(ret, 2)}  nocional $${t.notional.toFixed(0)}  ${t.open ? "" : usd(t.pnl)}  | mesa ${mesa}`);
  }
  const closed = trades.filter((t) => !t.open);
  const sum = (xs) => `${xs.length} trades, ${xs.length ? p(xs.filter((t) => t.pnl > 0).length / xs.length, 0) : "-"} no lucro, ${usd(xs.reduce((s, t) => s + t.pnl, 0))}`;
  console.log(`\nFechados (${days ? days + "d" : "tudo"}): ${sum(closed)}`);
  console.log(`A favor da mesa: ${sum(closed.filter((t) => t.mesa && t.dir === t.mesa.dir))}`);
  console.log(`Contra a mesa:   ${sum(closed.filter((t) => t.mesa && t.dir !== t.mesa.dir))}`);
  const judged = trades.filter((t) => t.mesa && t.mesa.right12 != null);
  console.log(`Mesa acertou em ${L.HORIZON}h: ${judged.filter((t) => t.mesa.right12).length}/${judged.length}`);
})().catch((e) => { console.error(e.message); process.exit(1); });
