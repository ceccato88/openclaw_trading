# Debate Arena Local

Este módulo adiciona um Debate Arena local ao `openclaw`, usando:

- contexto real da Hyperliquid via `skills/`
- múltiplas personalidades
- LLM real via OpenRouter
- persistência local em `state/debates/`
- execução opcional do consenso com `skills.wolf_strategy`

## Requisitos

No `.env`:

```env
OPENROUTER_API_KEY=sk-or-...
DEBATE_MODEL_ID=openai/gpt-4o-mini
DEBATE_ALLOW_CHOP=false
HYPERLIQUID_PRIVATE_KEY=0x...
HYPERLIQUID_TESTNET=true
HYPERLIQUID_ACCOUNT_ADDRESS=0x...
```

## Personalidades

- `bull`
- `bear`
- `analyst`
- `contrarian`
- `risk_manager`

## Comandos

Criar uma sessão:

```bash
python scripts/run_debate_once.py create \
  --name "BTC debate" \
  --symbols BTC ETH \
  --participants bull bear analyst risk_manager \
  --max-rounds 3
```

Rodar uma sessão:

```bash
python scripts/run_debate_once.py run --session-id debate_xxx
```

Rodar e permitir execução do consenso:

```bash
python scripts/run_debate_once.py run --session-id debate_xxx --execute
```

Listar sessões:

```bash
python scripts/run_debate_once.py list
```

Ver uma sessão:

```bash
python scripts/run_debate_once.py show --session-id debate_xxx
```

## Persistência

As sessões ficam em:

- `state/debates/sessions/*.json`

Cada sessão guarda:

- participantes
- mensagens por round
- votos finais
- decisões de consenso
- resultado de execução, quando houver

## Fluxo

1. monta contexto de mercado com scanner, regime, risco e conta
2. cada participante chama o modelo LLM
3. cada round gera mensagens e decisões
4. no fim, os participantes votam
5. o consenso escolhe a ação vencedora por símbolo
6. opcionalmente executa a decisão com `wolf_strategy`

## Observações

- este módulo é `LLM-only`
- se `OPENROUTER_API_KEY` faltar ou o modelo falhar, a sessão falha
- não existe fallback heurístico
- a execução do consenso respeita `entry_ready`; sem trigger confirmado, marca `skipped`
- com `DEBATE_ALLOW_CHOP=false`, a sessão encerra cedo com consenso `WAIT` sem gastar tokens em mercado lateral
- cada novo `run` reseta mensagens, votos e consenso anteriores da sessão antes de executar de novo
