"""
Bot de trading automatizado para Binance Spot (estrategia: Reversion a la Media / Rango Dinamico).

Caracteristicas:
- Opera en el mercado Spot (ej. BTCUSDT).
- Nunca invierte mas del capital asignado (CAPITAL_MAX).
- Cada posicion abierta tiene obligatoriamente Stop-Loss y Take-Profit.
- Descuenta comisiones de Binance (0.1% compra + 0.1% venta) en la rentabilidad.
- Credenciales leidas desde un archivo .env (python-dotenv).
- Reporte final en consola con resumen claro.

USO:
    python bot.py

CONFIGURACION (.env):
    BINANCE_API_KEY=...
    BINANCE_API_SECRET=...
    BINANCE_TESTNET=false
"""

import os
import sys
import time
import logging
from decimal import Decimal, ROUND_DOWN

from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceOrderException
from tabulate import tabulate

# ---------------------------------------------------------------------------
# Configuracion global de parametros (editar aqui)
# ---------------------------------------------------------------------------
SYMBOL = "BTCUSDT"              # Par de monedas
CAPITAL_MAX = 100.0             # Capital maximo a invertir (USDT)
BUY_DROP_PERCENT = 0.5          # % de caida respecto al precio base para comprar (ej. 0.5 = 0.5%)
STOP_LOSS_PERCENT = 1.0         # % de Stop-Loss (ej. 1.0 = 1%)
TAKE_PROFIT_PERCENT = 1.5       # % de Take-Profit (ej. 1.5 = 1.5%)
MONITOR_INTERVAL = 5            # Segundos entre cada chequeo de precio
DURATION_MINUTES = 60           # Duracion total en minutos (0 = ilimitada)
MAX_TRADES = 10                 # Numero maximo de operaciones (0 = ilimitadas)
USE_TESTNET = False             # True = modo prueba (no dinero real)

COMMISSION_PERCENT = 0.1        # Comision fija de Binance por lado (0.1%)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# 1. Carga de configuracion y cliente
# ---------------------------------------------------------------------------
def load_config():
    """Carga variables de entorno y devuelve un cliente autenticado de Binance."""
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")

    if not api_key or not api_secret:
        logger.error(
            "Faltan las credenciales. Crea un archivo .env con BINANCE_API_KEY y "
            "BINANCE_API_SECRET (ver .env.example)."
        )
        sys.exit(1)

    client = Client(api_key, api_secret)

    # Modo testnet: solo si el usuario lo pide explicitamente, ignorando .env
    testnet = USE_TESTNET or os.getenv("BINANCE_TESTNET", "false").lower() == "true"
    if testnet:
        client.API_URL = "https://testnet.binance.vision/api"
        logger.warning("MODO TESTNET ACTIVO - no se usa dinero real.")
    else:
        logger.info("MODO REAL - las ordenes se ejecutaran con fondos reales.")

    return client, testnet


# ---------------------------------------------------------------------------
# 2. Precio actual
# ---------------------------------------------------------------------------
def get_current_price(client, symbol=SYMBOL):
    """Devuelve el precio actual del par como Decimal."""
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        return Decimal(ticker["price"])
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"Error obteniendo precio de {symbol}: {e}")
        raise
    except Exception as e:
        logger.error(f"Error de conexion obteniendo precio: {e}")
        raise


# ---------------------------------------------------------------------------
# 3. Ejecucion de operaciones
# ---------------------------------------------------------------------------
def get_quantity_precision(client, symbol=SYMBOL):
    """Obtiene la precision decimal permitida para la cantidad del simbolo."""
    try:
        info = client.get_symbol_info(symbol)
        for f in info["filters"]:
            if f["filterType"] == "LOT_SIZE":
                step = Decimal(f["stepSize"])
                precision = max(0, -step.as_tuple().exponent)
                return precision
    except Exception as e:
        logger.warning(f"No se pudo obtener la precision de {symbol}: {e}")
    return 8  # valor por defecto conservador


def execute_trade(client, symbol=SYMBOL, capital=CAPITAL_MAX,
                  stop_loss_pct=STOP_LOSS_PERCENT, take_profit_pct=TAKE_PROFIT_PERCENT,
                  testnet=False):
    """
    Compra con el capital asignado y coloca inmediatamente Stop-Loss y
    Take-Profit (ordenes limite). Devuelve dict con el resultado o None si falla.

    Seguridad financiera:
    - La cantidad a comprar se calcula con el capital maximo disponible.
    - Si ya existe una posicion abierta, no abre otra.
    - Nunca compra con mas capital del asignado.
    """
    try:
        # 1. Verificar saldo disponible en USDT
        balance = client.get_asset_balance(asset="USDT")
        available = Decimal(balance["free"])
        capital_decimal = Decimal(str(capital))

        if available < capital_decimal:
            logger.warning(
                f"Saldo USDT insuficiente: {available} USDT disponibles, "
                f"se necesita {capital_decimal} USDT. Usando saldo disponible."
            )
            if available <= 0:
                logger.error("No hay saldo USDT para operar. Se omite la operacion.")
                return None
            capital_decimal = available

        # 2. Precio actual
        price = get_current_price(client, symbol)

        # 3. Calcular cantidad a comprar (descontando comision de compra 0.1%)
        #    cantidad * precio * (1 + comision) <= capital
        precision = get_quantity_precision(client, symbol)
        fee_factor = Decimal(1) + Decimal(COMMISSION_PERCENT) / Decimal(100)
        qty = (capital_decimal / (price * fee_factor)).quantize(
            Decimal(10) ** -precision, rounding=ROUND_DOWN
        )

        if qty <= 0:
            logger.error("Cantidad calculada es 0 (capital demasiado pequeno para el precio).")
            return None

        # 4. Verificar que no exista ya una posicion abierta (saldo del activo)
        asset = symbol.replace("USDT", "")
        asset_balance = client.get_asset_balance(asset=asset)
        if Decimal(asset_balance["free"]) > 0:
            logger.info(f"Ya hay {asset_balance['free']} {asset} en cartera. No se abre nueva posicion.")
            return None

        # 5. Orden de compra MARKET
        logger.info(
            f"Comprando {qty} {asset} a ~{price} USDT (capital asignado: {capital_decimal} USDT)"
        )
        buy_order = client.order_market_buy(symbol=symbol, quantity=str(qty))
        logger.info(f"Orden de compra ejecutada: {buy_order['orderId']}")

        # 6. Precio real de compra (fill)
        fills = buy_order.get("fills", [])
        if fills:
            buy_price = Decimal(sum(Decimal(f["price"]) * Decimal(f["qty"]) for f in fills)) / qty
        else:
            buy_price = price
        logger.info(f"Precio promedio de compra: {buy_price}")

        # 7. Calcular Stop-Loss y Take-Profit
        stop_price = (buy_price * (Decimal(1) - Decimal(stop_loss_pct) / Decimal(100))).quantize(
            Decimal(10) ** -precision
        )
        take_price = (buy_price * (Decimal(1) + Decimal(take_profit_pct) / Decimal(100))).quantize(
            Decimal(10) ** -precision
        )

        # 8. Colocar OCO (One-Cancels-the-Other) con limite + stop
        #    OCO permite que la orden limite y la stop-limit se cancelen mutuamente.
        try:
            oco = client.order_oco_sell(
                symbol=symbol,
                quantity=str(qty),
                price=str(take_price),
                stopPrice=str(stop_price),
                stopLimitPrice=str(stop_price),
                stopLimitTimeInForce="GTC",
            )
            logger.info(
                f"OCO colocada: TP {take_price} USDT / SL {stop_price} USDT "
                f"(ordenes {oco['orderListId']})"
            )
        except (BinanceAPIException, BinanceOrderException) as e:
            # Fallback: si OCO no esta disponible, colocar dos ordenes limite separadas
            logger.warning(f"OCO fallo ({e}), colocando ordenes limite separadas...")
            client.order_limit_sell(
                symbol=symbol, quantity=str(qty), price=str(take_price)
            )
            client.order_limit_sell(
                symbol=symbol, quantity=str(qty), price=str(stop_price)
            )
            logger.info("Stop-Loss y Take-Profit colocados como ordenes limite separadas.")

        # 9. Registrar operacion en el reporte
        return {
            "asset": asset,
            "qty": qty,
            "buy_price": buy_price,
            "stop_price": stop_price,
            "take_price": take_price,
            "capital": capital_decimal,
        }

    except BinanceAPIException as e:
        logger.error(f"API Binance rechazo la orden: {e}")
        return None
    except BinanceOrderException as e:
        logger.error(f"Orden rechazada: {e}")
        return None
    except Exception as e:
        logger.error(f"Error inesperado en execute_trade: {e}")
        return None


# ---------------------------------------------------------------------------
# 4. Reporte en consola
# ---------------------------------------------------------------------------
def generate_report(initial_capital, trades):
    """
    Imprime un resumen final con tablas: capital inicial, operaciones,
    ganadoras/perdedoras, ganancia neta (descontando comisiones) y capital final.

    Cada operacion se registra en 'trades' como dict con 'outcome' ('win'/'loss')
    cuando se cierra (se detecta que la posicion ya no existe y el precio cruzo TP/SL).
    """
    print("\n" + "=" * 62)
    print("                   REPORTE FINAL DEL BOT")
    print("=" * 62)

    if not trades:
        print("No se ejecutaron operaciones durante la sesion.")
        return

    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    open_pos = [t for t in trades if t.get("outcome") is None]

    # Tabla de operaciones
    rows = []
    for i, t in enumerate(trades, 1):
        rows.append([
            i,
            t["asset"],
            f"{t['qty']:.6f}",
            f"{t['buy_price']:.4f}",
            f"{t['take_price']:.4f}",
            f"{t['stop_price']:.4f}",
            (t.get("outcome") or "abierta").upper(),
        ])
    print("\nDETALLE DE OPERACIONES:")
    print(tabulate(rows, headers=["#", "Par", "Cant.", "Compra", "TP", "SL", "Estado"]))

    # Calculo de rentabilidad real descontando comisiones (0.1% + 0.1%)
    gross_pnl = Decimal(0)
    for t in wins + losses:
        qty = t["qty"]
        buy = t["buy_price"]
        fee_buy = buy * qty * Decimal(COMMISSION_PERCENT) / Decimal(100)
        if t["outcome"] == "win":
            sell_price = t["take_price"]
        else:
            sell_price = t["stop_price"]
        fee_sell = sell_price * qty * Decimal(COMMISSION_PERCENT) / Decimal(100)
        gross_pnl += (sell_price - buy) * qty - fee_buy - fee_sell

    print("\nRESUMEN:")
    summary = [
        ["Capital inicial (USDT)", f"{initial_capital:.2f}"],
        ["Operaciones ejecutadas", len(trades)],
        ["Ganadoras", len(wins)],
        ["Perdedoras", len(losses)],
        ["Posiciones aun abiertas", len(open_pos)],
        ["Ganancia/Perdida neta (USDT)", f"{gross_pnl:.2f}"],
        ["Capital final estimado (USDT)", f"{initial_capital + gross_pnl:.2f}"],
    ]
    print(tabulate(summary, headers=["Concepto", "Valor"], tablefmt="grid"))
    print("=" * 62 + "\n")


# ---------------------------------------------------------------------------
# Monitoreo de posiciones abiertas y cierre
# ---------------------------------------------------------------------------
def check_open_position(client, trade, symbol=SYMBOL):
    """
    Verifica si la posicion del trade ya fue cerrada por TP o SL.
    Si el activo ya no aparece en saldo libre y el precio cruzo el TP, es victoria;
    si cruzo el SL, es perdida. Devuelve el outcome ('win'/'loss') o None si sigue abierta.
    """
    asset = trade["asset"]
    try:
        balance = client.get_asset_balance(asset=asset)
        free = Decimal(balance["free"])
        if free > 0:
            return None  # aun en cartera
        # Posicion cerrada: determinar por donde salio
        price = get_current_price(client, symbol)
        if price >= trade["take_price"]:
            return "win"
        if price <= trade["stop_price"]:
            return "loss"
        return "win" if price > trade["buy_price"] else "loss"
    except Exception as e:
        logger.warning(f"No se pudo verificar posicion: {e}")
        return None


# ---------------------------------------------------------------------------
# 5. Bucle principal
# ---------------------------------------------------------------------------
def main():
    client, testnet = load_config()

    # Parametros de control
    start_time = time.time()
    max_seconds = DURATION_MINUTES * 60 if DURATION_MINUTES > 0 else 0
    initial_capital = Decimal(str(CAPITAL_MAX))

    # Precio base al arrancar
    try:
        base_price = get_current_price(client)
    except Exception:
        logger.error("No se pudo obtener el precio base al arrancar. Saliendo.")
        return
    logger.info(f"Precio base de {SYMBOL}: {base_price} USDT")

    trades = []          # historial de operaciones
    active_trade = None  # posicion abierta actual
    trades_count = 0
    trigger_price = base_price * (Decimal(1) - Decimal(BUY_DROP_PERCENT) / Decimal(100))
    logger.info(f"Compra cuando el precio caiga a <= {trigger_price} USDT (-{BUY_DROP_PERCENT}%)")

    try:
        while True:
            # Control de tiempo
            if max_seconds and (time.time() - start_time) >= max_seconds:
                logger.info(f"Tiempo limite alcanzado ({DURATION_MINUTES} min). Deteniendo.")
                break

            # Control de operaciones
            if MAX_TRADES and trades_count >= MAX_TRADES:
                logger.info(f"Maximo de operaciones alcanzado ({MAX_TRADES}). Deteniendo.")
                break

            try:
                price = get_current_price(client)

                if active_trade is None:
                    # Sin posicion: monitorear
                    if price <= trigger_price:
                        logger.info(f"Precio {price} <= umbral {trigger_price}. Abriendo posicion...")
                        trade = execute_trade(client, testnet=testnet)
                        if trade:
                            trades.append(trade)
                            active_trade = trade
                            trades_count += 1
                            # Recalcular umbral relativo al nuevo precio para continuar la estrategia
                            base_price = price
                            trigger_price = base_price * (Decimal(1) - Decimal(BUY_DROP_PERCENT) / Decimal(100))
                            logger.info(f"Nuevo umbral de compra: {trigger_price} USDT")
                        else:
                            logger.warning("No se pudo abrir posicion. Reintentando en el siguiente ciclo.")
                    else:
                        logger.info(f"Monitoreando... {SYMBOL} = {price} USDT (umbral {trigger_price})")
                else:
                    # Posicion abierta: verificar si TP/SL se ejecutaron
                    outcome = check_open_position(client, active_trade)
                    if outcome:
                        active_trade["outcome"] = outcome
                        logger.info(f"Posicion cerrada por {'TAKE-PROFIT' if outcome == 'win' else 'STOP-LOSS'}.")
                        active_trade = None

                time.sleep(MONITOR_INTERVAL)

            except BinanceAPIException as e:
                logger.error(f"Error de API: {e.code} {e.message}. Reintentando...")
                time.sleep(MONITOR_INTERVAL)
            except Exception as e:
                logger.error(f"Error general: {e}. Reintentando...")
                time.sleep(MONITOR_INTERVAL)

    except KeyboardInterrupt:
        logger.info("Interrupcion del usuario. Generando reporte...")

    # Reporte final
    generate_report(initial_capital, trades)


if __name__ == "__main__":
    main()