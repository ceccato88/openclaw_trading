# OpenClaw Setup

Este pacote `openclaw/` roda o bot sem Agno, chamando apenas Python local.

## Arquivos principais

- `hl_client.py`
- `project_env.py`
- `openclaw_instructions.md`
- `skills/`
- `runtime/`
- `scripts/openclaw_scheduler.py`
- `scripts/smoke_test_openclaw.py`
- `.env`
- `state/`
- `var/openclaw/`

## Dependências mínimas

```bash
cd /home/ceccato88/projects/TRADING/openclaw
uv venv --python 3.12
source .venv/bin/activate
uv pip install hyperliquid-python-sdk eth-account python-dotenv requests
```

Ou, se preferir usar o lock:

```bash
cd /home/ceccato88/projects/TRADING/openclaw
uv venv --python 3.12
source .venv/bin/activate
uv sync
```

## Script rápido

Para rodar o smoke test completo sem lembrar o comando do Python:

```bash
cd /home/ceccato88/projects/TRADING/openclaw
chmod +x run_smoke.sh
./run_smoke.sh --coin BTC --usdt-size 10 --leverage 10 --risk-pct 2 --reward-pct 4 --account-risk-pct 1
```

## Variáveis de ambiente

`.env` mínimo:

```env
HYPERLIQUID_PRIVATE_KEY=0xYOUR_PRIVATE_KEY
HYPERLIQUID_TESTNET=true
HYPERLIQUID_ACCOUNT_ADDRESS=0xYOUR_ACCOUNT_ADDRESS
WOLF_HEARTBEAT_INTERVAL_SECONDS=180
WOLF_HUNT_INTERVAL_SECONDS=900
# piso mínimo de notional; o sizing real usa risco da conta
WOLF_POSITION_USD=25
WOLF_LEVERAGE=10
# distância do stop
WOLF_RISK_PCT=2
# risco da conta por trade
WOLF_ACCOUNT_RISK_PCT=1
WOLF_MIN_VOLUME=5000000
WOLF_MAX_RESULTS=3
```

## O que a pasta `state/` faz

`state/` guarda a memória operacional local:

- `state/trades/`: estado de trades abertos
- `state/pending_entries/`: ordens limit pendentes
- `state/locks/`: locks de ficheiro
- `state/daily_risk_state.json`: saldo inicial do dia

Sem `state/`, o bot perde o estado de trailing, pending entries e drawdown diário.

## Scheduler local

Loop contínuo:

```bash
python scripts/openclaw_scheduler.py
```

Execução única:

```bash
python scripts/openclaw_scheduler.py --heartbeat-once
python scripts/openclaw_scheduler.py --hunt-once
```

## Smoke test completo sem Agno

O smoke test real está em:

- `scripts/smoke_test_openclaw.py`

Ele valida:

- snapshot da conta
- drawdown diário
- scanner
- heartbeat direto
- scheduler local em modo one-shot
- hunt direto
- tentativa de entrada pela estratégia
- reconciliação de pending entries
- trailing stop
- abertura real de posição
- proteção `SL/TP`
- fecho real da posição
- limpeza final do estado local

Execução:

```bash
python scripts/smoke_test_openclaw.py --coin BTC --usdt-size 10 --leverage 10 --risk-pct 2 --reward-pct 4 --account-risk-pct 1
```

Ou com o wrapper:

```bash
./run_smoke.sh --coin BTC --usdt-size 10 --leverage 10 --risk-pct 2 --reward-pct 4 --account-risk-pct 1
```

Se a conta já estiver suja:

```bash
python scripts/smoke_test_openclaw.py --allow-dirty-start
```

Parâmetros principais do smoke:

- `--risk-pct`: distância do stop em percentagem
- `--reward-pct`: distância do take profit em percentagem
- `--account-risk-pct`: percentagem do equity arriscada no sizing
- `--usdt-size`: piso mínimo de notional do teste
