#!/usr/bin/env node
// Backend for the casino screen's AI chat (issue #13). A single route,
// POST /api/chat, that answers a question grounded ONLY in the snapshot the
// client sends — the same numbers already on screen (issue #16's format).
//
// Deliberately not part of the tradingagents/ pipeline: that's a batch
// LangGraph flow built for one-shot reports, not interactive chat. This is a
// small, separate Node process, same spirit as casino/sync.cjs and check.cjs.
//
//   node casino/chat-server.cjs            # reads .env from the repo root
//   PORT=8778 node casino/chat-server.cjs  # override the port
//
// pm2 runs it as `casino-chat` (see .claude/skills/casino/SKILL.md).
//
// Stateless: no conversation is persisted server-side. The client resends the
// snapshot and message history on every request.

const http = require("http");
const path = require("path");
const fs = require("fs");
const Anthropic = require("@anthropic-ai/sdk");

// Load ANTHROPIC_API_KEY from the repo root .env without adding a dotenv
// dependency — the file is simple KEY=value lines.
(function loadRootEnv() {
  const envPath = path.join(__dirname, "..", ".env");
  if (!fs.existsSync(envPath)) return;
  for (const line of fs.readFileSync(envPath, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)\s*$/);
    if (!m) continue;
    const key = m[1];
    let val = m[2];
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = val;
  }
})();

const PORT = Number(process.env.CASINO_CHAT_PORT || 8778);
const MODEL = process.env.CASINO_CHAT_MODEL || "claude-haiku-4-5-20251001";
const MAX_TOKENS = Number(process.env.CASINO_CHAT_MAX_TOKENS || 512);
const MAX_HISTORY_MESSAGES = 20; // caps prompt growth on a long chat session

if (!process.env.ANTHROPIC_API_KEY) {
  console.error("ANTHROPIC_API_KEY not set (checked process.env and ../.env). Refusing to start.");
  process.exit(1);
}

const anthropic = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const SYSTEM_PROMPT = `Você é a "Mesa AI" da tela "Sobe ou Desce", um jogo calibrado de aposta 12h em cripto na HyperLiquid.

Regras estritas:
- Você só fala sobre o snapshot de dados que acompanha a mensagem do usuário (score, votos por métrica, funding, volatilidade, simulação de liquidação). Nunca invente números que não estão no snapshot.
- Nunca diga "certeza", "garantido", ou algo equivalente. O que existe é uma vantagem estatística pequena (tipicamente 2-4 pontos percentuais acima do acaso), nunca uma previsão confiável.
- Nunca recomende diretamente "entra" ou "sai" de uma posição. Apresente o que os números mostram (edge, P(liquidação), resultado médio esperado) e deixe a decisão com o usuário.
- Se o usuário perguntar sobre outro ativo, horizonte ou dado que não está no snapshot, diga que não tem esse dado agora em vez de estimar.
- Seja direto e curto (poucas frases). O usuário já está olhando o painel — não repita todos os números, cite só os relevantes pra pergunta.
- Responda em texto corrido, sem markdown (sem **negrito**, sem listas com marcadores, sem headers) — a resposta aparece numa bolha de chat simples.
- Responda em português.`;

function buildUserContent(message, snapshot) {
  return `Snapshot atual da mesa (JSON):\n${JSON.stringify(snapshot)}\n\nPergunta do usuário: ${message}`;
}

async function handleChat(req, res) {
  let body = "";
  req.on("data", (chunk) => { body += chunk; if (body.length > 1_000_000) req.destroy(); });
  req.on("end", async () => {
    let payload;
    try {
      payload = JSON.parse(body || "{}");
    } catch {
      return sendJson(res, 400, { error: "invalid JSON body" });
    }
    const { message, snapshot, history } = payload;
    if (typeof message !== "string" || !message.trim()) {
      return sendJson(res, 400, { error: "missing 'message'" });
    }
    if (!snapshot || typeof snapshot !== "object") {
      return sendJson(res, 400, { error: "missing 'snapshot'" });
    }

    const priorTurns = Array.isArray(history) ? history.slice(-MAX_HISTORY_MESSAGES) : [];
    const messages = [
      ...priorTurns
        .filter((t) => t && (t.role === "user" || t.role === "assistant") && typeof t.content === "string")
        .map((t) => ({ role: t.role, content: t.content })),
      { role: "user", content: buildUserContent(message, snapshot) },
    ];

    try {
      const response = await anthropic.messages.create({
        model: MODEL,
        max_tokens: MAX_TOKENS,
        system: SYSTEM_PROMPT,
        messages,
      });
      const text = response.content
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("\n")
        .trim();
      sendJson(res, 200, { reply: text, usage: response.usage });
    } catch (err) {
      console.error("[casino-chat] Anthropic API error:", err.message);
      sendJson(res, 502, { error: "a mesa não respondeu (erro no modelo)" });
    }
  });
}

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  });
  res.end(body);
}

const server = http.createServer((req, res) => {
  if (req.method === "OPTIONS") return sendJson(res, 204, {});
  if (req.method === "POST" && req.url === "/api/chat") return handleChat(req, res);
  if (req.method === "GET" && req.url === "/health") return sendJson(res, 200, { ok: true, model: MODEL });
  sendJson(res, 404, { error: "not found" });
});

server.listen(PORT, () => {
  console.log(`[casino-chat] listening on http://localhost:${PORT} (model ${MODEL})`);
});
