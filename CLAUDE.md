# tradingagents-hl — Claude Context

## O que é

Fork de [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(framework multi-agente LLM: analistas → debate bull vs bear → trader →
risk manager → portfolio manager → decisão explicada), adaptado pra
cripto/HyperLiquid. Nasceu em 2026-09-23 como projeto **separado** do
[nebulosa-protocol](../nebulosa-protocol) (Aethron) — não é dependência
dele nem substitui o bot, é uma ferramenta de **decisão manual com
contexto rico**, pensada desde já pra uma futura integração (formato de
saída estável) mas rodando independente por ora.

**Por quê:** o painel informativo do Aethron pós-pivot cassino (RSI/EMA-
trend/CVD crus) ficou raso demais. O usuário quer múltiplas perspectivas
debatendo (técnico/estrutura do mercado/sentimento/notícias → bull vs bear
→ decisão com raciocínio exposto) antes de apostar manualmente — sem
reverter a decisão do Aethron de que a AI não decide a entrada sozinha.

Modelo de trabalho: usuário é CPO, Claude é CTO — mesma dinâmica do
nebulosa-protocol (executa sozinho em trabalho já desenhado no board, sem
aprovação passo a passo).

## Reentrada de sessão (gatilhada)

Mesmo padrão do nebulosa-protocol. Reentrada lenta NÃO é o default — só
pra primeira sessão do dia ou depois de hiato longo.

**Gatilho explícito:** cumprimento tipo "bom dia", "boa tarde", "vamos
começar" — abertura humana real, não pedido de tarefa.

**Comportamento no gatilho:**
1. Cumprimentar de volta como colega, sem pular pra ferramenta
2. Checar o GitHub Project deste repo — o que está em Ready/Doing/Review
3. Ler specs/docs relevantes ao que está em Ready (não confiar em memória stale)
4. Devolver resumo NEUTRO de estado — sem ranking nem opinião
5. Se houver issue elegível em Ready sem bloqueio: perguntar se já pode
   seguir, não sair implementando sem esse aceno mínimo na saudação
6. NÃO empurrar decisão de produto nos primeiros turns

**Sem gatilho = produzir direto.** Continuação, segundo chat do dia, ou
pedido direto já entra executando.

## Arquitetura de extensão (achado em 2026-09-23, útil pra qualquer trabalho aqui)

- **Agentes** (`tradingagents/agents/analysts/*.py` etc.) só chamam tools
  por nome (`get_stock_data`, `get_indicators`, `get_fundamentals`...) —
  não sabem a origem do dado. Não precisa mexer neles pra trocar vendor.
- **`tradingagents/dataflows/interface.py`**: `VENDOR_METHODS` (dict
  tool→vendor→função) + `VENDOR_LIST` — registrar vendor novo é aditivo,
  não reescreve nada existente.
- **`data_vendors`** no config (`tradingagents/default_config.py`) escolhe
  vendor por categoria: `core_stock_apis`, `technical_indicators`,
  `fundamental_data`, `news_data`, `macro_data`, `prediction_markets`.
  String única ou chain com fallback (`"yfinance,alpha_vantage"`).
- Vendor HyperLiquid (`dataflows/hyperliquid.py`) já cobre preço/indicadores
  (`core_stock_apis`/`technical_indicators`) e estrutura de perp
  (`perp_structure_data`, no slot de "fundamentals"). `resolve_hl_coin()`
  resolve qualquer forma de símbolo (`BTC`, `btc`, `BTC-USD`, `BTCUSDT`...)
  contra o universo real de perps da HL — é a fonte canônica de "isso é
  cripto e é este coin exato", usada pela CLI (`resolve_ticker_and_asset_type`
  em `cli/utils.py`) pra classificar `asset_type` mesmo sem sufixo `-USD`.
- `crypto_base()` em `dataflows/symbol_utils.py` continua puramente
  sintática (sem chamada de rede) — usada por `reddit.py`/`stocktwits.py`
  via `crypto_base_hl_aware()`, que tenta a versão sintática primeiro e só
  cai pra `resolve_hl_coin()` (rede) quando não reconhece, pra pegar coins
  fora da lista fechada de bases (ex. `HYPE`) ou símbolo puro sem sufixo.

## Escopo decidido (2026-09-23)

- Fork de verdade (mantém `origin` → https://github.com/rafaelneiva/tradingagents-hl,
  que é fork de TauricResearch/TradingAgents)
- Só cripto/HyperLiquid, não ativos tradicionais
- Fundamentals Analyst (SEC EDGAR não serve pra cripto) substituído por
  **estrutura do mercado perp nativo da HL** — funding rate histórico,
  open interest, long/short ratio, L2 order book imbalance. Não
  on-chain/tokenomics (sem dependência externa nova, funciona pra
  qualquer ativo listado na HL, reusa know-how do `indicators.js` do
  Aethron)
- Pensado pra futura integração com o Aethron (formato de saída estável:
  JSON decisão + raciocínio + timestamp) mas não integrado agora

## Plano de fases

1. **Vendor HyperLiquid** (bloqueador) — `dataflows/hyperliquid.py` pra
   OHLCV (candleSnapshot da API HL) no formato que `get_stock_data`/
   `get_indicators` esperam; registrar em VENDOR_LIST/VENDOR_METHODS/
   data_vendors. Indicadores via `stockstats` sobre o OHLCV da HL.
2. **Fundamentals → Perp Structure Analyst** — novo
   `dataflows/hyperliquid_perp.py` com funding history, open interest,
   long/short ratio, L2 book imbalance; substitui
   `fundamental_data_tools.py`/`fundamentals_analyst.py`.
3. **Sentiment/News** — validar se Reddit/StockTwits (já mapeiam cripto)
   funcionam de fato pra BTC/ETH/SOL; Alpha Vantage news (ações) sai ou
   vira secundário.
4. **Teste ponta a ponta** — rodar CLI/`main.py` com símbolo real da HL,
   ver pipeline completo até decisão final. **Done (2026-09-25)**: rodado
   com `BTC` puro (sem sufixo `-USD`) e data atual — decisão coerente
   (Hold, com níveis de estrutura HL), nenhum vazamento de dado de ações
   nos relatórios (identidade, sentiment, benchmark).
5. **Interface pra futura integração Aethron** — formato de saída
   estável, sem implementar o lado Aethron ainda. **Done (2026-09-25)**:
   ver seção "Formato de saída (decision.json)" abaixo.

## Formato de saída (decision.json)

Toda run que salva relatórios (`TradingAgentsGraph.save_reports()`, usado
pela CLI e por qualquer chamador programático) grava um `decision.json` na
raiz do `save_path`, ao lado de `complete_report.md`. Escrito por
`write_decision_json()` em `tradingagents/reporting.py`.

Campos (estáveis — uma futura integração Aethron lê este arquivo; só
adicionar campo novo, nunca renomear/remover um existente):

```json
{
  "symbol": "BTC",
  "asset_type": "crypto",
  "trade_date": "2026-09-25",
  "decision": "Hold",
  "final_trade_decision": "**Rating**: Hold\n\n...",
  "reasoning_summary": "...",
  "generated_at": "2026-09-25T18:03:00+00:00"
}
```

- `decision`: rating de 5 níveis (`Buy`/`Overweight`/`Hold`/`Underweight`/
  `Sell`) ou `"REVIEW"` quando o texto do Portfolio Manager não tinha rating
  parseável (mesma extração de `TradingAgentsGraph.process_signal`).
- `final_trade_decision`: texto completo da decisão do Portfolio Manager.
- `reasoning_summary`: plano do Research Manager (`investment_plan`).
- `generated_at`: timestamp real de geração do arquivo (ISO 8601 UTC), não
  a data de análise (`trade_date`).

## Regras (herdadas do nebulosa-protocol, mesma disciplina)

- **NUNCA** rodar `git push`, `git reset --hard`, ou operações destrutivas sem confirmar
- **NUNCA** editar arquivos `.env` sem perguntar antes
- Após implementar algo, **sempre** dizer o que o usuário precisa fazer para testar
- Comunicação com usuário: português. Código/comentários técnicos: inglês
  (mantém o padrão do upstream TradingAgents)
- Idioma interno do debate dos agentes fica em inglês (qualidade de
  raciocínio) — já é assim no upstream via `output_language` config

## Referências

- Upstream: https://github.com/TauricResearch/TradingAgents
- Paper: https://arxiv.org/abs/2412.20138
- README do upstream tem toda a doc de instalação/CLI/config de LLM providers
