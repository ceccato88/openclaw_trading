from hl_client import exchange, logger
from skills.support import (
    cancel_orders_for_coin,
    delete_trade_state,
    extract_fill_details,
    get_open_position,
    market_close_position,
)

def close_position(coin: str) -> dict:
    """
    Skill Wolf Command: Fecha imediatamente uma posição aberta a preço de mercado (Panic Button/Forced Exit).
    O agente pode usar isto se a estrutura do mercado reverter drasticamente antes de atingir o Stop Loss.
    """
    logger.info(f"🚨 A tentar fechar a posição em {coin} a mercado...")

    if not exchange:
        return {"status": "error", "message": "Clientes não inicializados."}

    try:
        position = get_open_position(coin)
        if not position:
            return {"status": "error", "message": f"Não tem nenhuma posição aberta em {coin} para fechar."}

        canceled_order_ids = cancel_orders_for_coin(coin)
        logger.info(f"Ordens pendentes para {coin} canceladas: {canceled_order_ids}")

        resultado = market_close_position(coin)
        fill = extract_fill_details(resultado, f"fecho a mercado de {coin}")
        delete_trade_state(coin)

        logger.info(f"✅ Posição em {coin} fechada com sucesso!")
        return {
            "status": "success",
            "data": resultado,
            "fill_price": fill["avg_px"],
            "closed_size": fill["size"],
            "canceled_order_ids": canceled_order_ids,
        }

    except Exception as e:
        logger.error(f"Erro ao fechar posição em {coin}: {e}")
        return {"status": "error", "message": str(e)}
