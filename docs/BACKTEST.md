# Backtest Local

O `openclaw` agora tem um backtest histórico local para a estratégia determinística.

## Comando

```bash
cd /home/ceccato88/projects/TRADING/openclaw
./.venv/bin/python scripts/run_backtest.py \
  --coin BTC \
  --start 2026-01-01T00:00:00+00:00 \
  --end 2026-02-01T00:00:00+00:00
```

## Parâmetros principais

- `--coin`: moeda a testar
- `--start`: início do período em ISO
- `--end`: fim do período em ISO
- `--starting-equity`: equity inicial
- `--risk-pct`: distância do stop
- `--reward-pct`: distância do take profit
- `--account-risk-pct`: percentagem da conta arriscada por trade
- `--fee-bps`: custo por lado em basis points
- `--slippage-bps`: slippage por lado em basis points
- `--pending-ttl-candles`: validade da ordem limit em candles de 15m
- `--json-out`: grava o resultado completo em JSON

## O que ele simula

- regime BTC em `1h`
- score técnico do ativo
- trigger de entrada em `15m`
- contexto superior em `1h`
- ordem limit com validade curta
- sizing por risco da conta
- `SL/TP`
- trailing stop por estágio
- equity curve e métricas finais

## Limitações desta primeira versão

- é um backtest bar-based, não replay tick-by-tick
- trailing é recalculado no fecho do candle
- se `SL` e `TP` tocarem no mesmo candle, assume `SL` primeiro de forma conservadora
- o foco atual é uma moeda por vez
