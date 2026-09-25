#!/usr/bin/env node
// Thin loader for `buildChatSnapshot`, whose real implementation lives inside
// index.html's <script> (single source of truth, same pattern check.cjs uses
// for analyze()/simulateBet(): the page and the terminal never disagree).
//
//   const { buildChatSnapshot } = require("./snapshot.cjs");
//   const snapshot = buildChatSnapshot(coin, m, A);   // A = analyze(m, livePx)

const fs = require("fs");
const path = require("path");

function loadPageLogic() {
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
  return mod.exports;
}

module.exports = { buildChatSnapshot: loadPageLogic().buildChatSnapshot };
