"""
Bot de trading automatizado para Binance Spot con DOS estrategias:

- MOMENTUM (por defecto): escanea TODO el mercado cada N minutos, detecta las
  criptos POPULARES (alto volumen 24h = liquidez) que estan SUBIENDO y las
  pone en el radar. Compra cuando el precio retrocede un poco desde ese
  nivel (entrada segura en tendencia) con SL/TP obligatorios.
- DIP: vigila una lista fija de pares (--symbols) y compra caidas.

En ambos modos:
- Nunca se invierte mas del capital total asignado (se reparte entre las
  MAX_OPEN posiciones simultaneas).
- Cada posicion abre con Stop-Loss y Take-Profit obligatorios (OCO, con
  fallback a TP limit + SL stop-limit separados y salida de emergencia).
- Respeta filtros del exchange: LOT_SIZE, PRICE_FILTER (tick) y MIN_NOTIONAL.
- Descuenta comisiones de Binance (0.1% compra + 0.1% venta).
- Credenciales leidas desde .env (python-dotenv).
- Persistencia en state.json y compensacion automatica de reloj (-1021).
- Reporte final en consola.

USO:
    python bot.py --testnet                                  # radar dinamico
    python bot.py --strategy dip --symbols BTCUSDT,ETHUSDT   # lista fija
    python bot.py --top-n 8 --min-volume 50000000            # radar mas amplio
    python bot.py --capital 90 --max-open 3                  # 30 USDT por operacion
    python bot.py --reset                                    # ignora state.json

CONFIGURACION (.env):
    BINANCE_API_KEY=...
    BINANCE_API_SECRET=...
    BINANCE_TESTNET=false

NOTA: ejecuta UNA sola instancia del bot (el estado se comparte en state.json).
"""

import argparse
import json
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
# Configuracion por defecto (la linea de comandos puede sobreescribirla)
# ---------------------------------------------------------------------------
SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
STRATEGY = "momentum"           # "momentum" = escanea el mercado y sigue lo que sube | "dip" = lista fija
SCAN_INTERVAL_MIN = 10          # Minutos entre escaneos de mercado (modo momentum)
MIN_QUOTE_VOLUME = 20_000_000   # Volumen 24h minimo (USDT) para considerar una cripto "popular"
TOP_N_WATCHLIST = 5             # Cuantos candidatos entran al radar en cada escaneo
MIN_24H_CHANGE = 0.5            # % minimo de subida en 24h para considerarla "en movimiento"
MAX_24H_CHANGE = 25.0           # % maximo de subida en 24h (evita perseguir pumps)
CAPITAL_MAX = 100.0             # Capital TOTAL asignado (USDT)
MAX_OPEN_POSITIONS = 3          # Posiciones simultaneas (capital/max_open = USDT por operacion)
RECOVERY_FACTOR = 1.0           # Multiplicador de tamano tras perdida (1 = desactivado; ej. 1.5)
RECOVERY_MAX_STEPS = 3          # Maximos pasos de recuperacion consecutivos
BUY_DROP_PERCENT = 0.5          # % de caida respecto al precio base para comprar
STOP_LOSS_PERCENT = 1.0         # % de Stop-Loss
TAKE_PROFIT_PERCENT = 1.5       # % de Take-Profit
MONITOR_INTERVAL = 5            # Segundos entre cada escaneo
DURATION_MINUTES = 60           # Duracion total en minutos (0 = ilimitada)
MAX_TRADES = 10                 # Numero maximo de operaciones totales (0 = ilimitadas)
USE_TESTNET = False             # True = modo prueba (no dinero real)

COMMISSION_PERCENT = 0.1        # Comision fija de Binance por lado (0.1%)
SL_LIMIT_BUFFER_PERCENT = 0.3   # Colchon del stop-limit para asegurar ejecucion
STATE_FILE = "state.json"

QUOTE_ASSETS = ("USDT", "FDUSD", "BUSD", "USDC", "BTC", "ETH", "BNB", "EUR")

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR", "PYUSD", "USD1"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

CFG = {}  # configuracion efectiva de la sesion (CLI > constantes)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")


class ProtectionError(Exception):
    """No se pudo colocar proteccion (SL/TP) para la posicion."""


def D(x):
    return Decimal(str(x))


def fmt(x, dp=8):
    if x is None:
        return "-"
    s = f"{x:.{dp}f}".rstrip("0").rstrip(".")
    return s or "0"


# ---------------------------------------------------------------------------
# Filtros del exchange (LOT_SIZE / PRICE_FILTER / NOTIONAL)
# ---------------------------------------------------------------------------
_filters_cache = {}


def get_filters(client, symbol):
    """Obtiene y cachea stepSize, tickSize, minQty y minNotional del simbolo."""
    if symbol in _filters_cache:
        return _filters_cache[symbol]
    info = client.get_symbol_info(symbol)
    if not info:
        raise ValueError(f"Simbolo {symbol} no encontrado en Binance.")
    fmap = {f["filterType"]: f for f in info["filters"]}
    min_notional = D("0")
    if "NOTIONAL" in fmap:
        min_notional = D(fmap["NOTIONAL"]["minNotional"])
    elif "MIN_NOTIONAL" in fmap:
        min_notional = D(fmap["MIN_NOTIONAL"]["minNotional"])
    filters = {
        "step": D(fmap["LOT_SIZE"]["stepSize"]),
        "tick": D(fmap["PRICE_FILTER"]["tickSize"]),
        "min_qty": D(fmap["LOT_SIZE"]["minQty"]),
        "min_notional": min_notional,
    }
    _filters_cache[symbol] = filters
    return filters


def quantize_qty(qty, step):
    """Redondea la cantidad hacia abajo a multiplos del stepSize."""
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_price(price, tick):
    """Redondea el precio hacia abajo a multiplos del tickSize."""
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def base_asset(symbol):
    for q in QUOTE_ASSETS:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)]
    return symbol[:-4]


# ---------------------------------------------------------------------------
# 1. Carga de configuracion y cliente
# ---------------------------------------------------------------------------
def load_config():
    """Carga variables de entorno y devuelve un cliente autenticado de Binance."""
    load_dotenv()

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()

    if not api_key or not api_secret or "tu_api_key" in api_key:
        logger.error(
            "Faltan las credenciales reales. Edita el archivo .env con "
            "BINANCE_API_KEY y BINANCE_API_SECRET (ver .env.example)."
        )
        sys.exit(1)

    testnet = bool(CFG.get("testnet")) or \
        os.getenv("BINANCE_TESTNET", "false").lower() == "true" or USE_TESTNET

    client = Client(api_key, api_secret, testnet=testnet)

    # Compensar desviacion del reloj local contra el servidor de Binance
    # (evita el error -1021 "Timestamp ... outside of the recvWindow")
    try:
        offset = client.get_server_time()["serverTime"] - int(time.time() * 1000)
        if abs(offset) > 1000:
            logger.warning(
                f"Reloj local desviado {offset} ms respecto a Binance. "
                "Compensando automaticamente (igual revisa la hora de Windows)."
            )
        client.timestamp_offset = offset
    except Exception as e:
        logger.warning(f"No se pudo medir la desviacion de reloj: {e}")

    if testnet:
        logger.warning("MODO TESTNET ACTIVO - no se usa dinero real.")
    else:
        logger.warning("MODO REAL - las ordenes se ejecutaran con fondos reales.")

    return client, testnet


# ---------------------------------------------------------------------------
# 2. Precio actual
# ---------------------------------------------------------------------------
def get_current_price(client, symbol=None):
    """Devuelve el precio actual del par como Decimal."""
    symbol = symbol or CFG.get("symbols", SYMBOLS)[0]
    try:
        ticker = client.get_symbol_ticker(symbol=symbol)
        return D(ticker["price"])
    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"Error obteniendo precio de {symbol}: {e}")
        raise
    except Exception as e:
        logger.error(f"Error de conexion obteniendo precio: {e}")
        raise


# ---------------------------------------------------------------------------
# 2b. Escaner de mercado (modo momentum): criptos populares en subida
# ---------------------------------------------------------------------------
_scan_client = None


def _get_scan_client():
    """
    Cliente PUBLICO de solo-lectura apuntando SIEMPRE al mercado REAL.
    El radar usa precios/volumenes reales aunque las ordenes se ejecuten en
    testnet (la testnet tiene muy pocos pares y volumen simulado).
    """
    global _scan_client
    if _scan_client is None:
        _scan_client = Client("", "")
    return _scan_client


def scan_market():
    """
    Escanea las estadisticas de 24h del mercado REAL y devuelve los pares
    USDT mas "populares" (alto volumen = liquidez) que estan SUBIENDO,
    ordenados por fuerza del movimiento. Excluye stablecoins y tokens
    apalancados. Devuelve lista: [{symbol, price, change_pct, quote_volume}]
    """
    try:
        tickers = _get_scan_client().get_ticker()
    except Exception as e:
        logger.error(f"Error escaneando el mercado: {e}")
        return []

    candidates = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in STABLE_BASES or base.endswith(LEVERAGED_SUFFIXES):
            continue
        try:
            vol = D(t["quoteVolume"])          # volumen real en USDT (popularidad)
            change = D(t["priceChangePercent"])
            price = D(t["lastPrice"])
        except Exception:
            continue
        if vol < D(CFG.get("min_volume", MIN_QUOTE_VOLUME)):
            continue
        if change < D(CFG.get("min_change", MIN_24H_CHANGE)) or change > D(CFG.get("max_change", MAX_24H_CHANGE)):
            continue
        candidates.append({
            "symbol": s, "price": price,
            "change_pct": change, "quote_volume": vol,
        })

    # Ranking: primero fuerza de subida en 24h, desempate por volumen
    candidates.sort(key=lambda c: (c["change_pct"], c["quote_volume"]), reverse=True)
    top = candidates[: CFG.get("top_n", TOP_N_WATCHLIST)]
    if top:
        logger.info("Escaneo de mercado - radar momentum:")
        for w in top:
            logger.info(
                f"  {w['symbol']:<12} +{fmt(w['change_pct'], 2)}% 24h | "
                f"vol {fmt(w['quote_volume'] / Decimal(1000000), 1)}M USDT | precio {fmt(w['price'])}"
            )
    else:
        logger.warning("Escaneo sin candidatos: ninguna cripto popular esta subiendo ahora.")
    return top


# ---------------------------------------------------------------------------
# 3. Ejecucion de operaciones
# ---------------------------------------------------------------------------
def _order_state(client, symbol, order_id):
    """Consulta el estado actual de una orden."""
    o = client.get_order(symbol=symbol, orderId=int(order_id))
    return {
        "status": o.get("status", "UNKNOWN"),
        "executed": D(o.get("executedQty") or 0),
        "quote": D(o.get("cummulativeQuoteQty") or 0),
    }


def _cancel_quiet(client, symbol, order_id):
    try:
        client.cancel_order(symbol=symbol, orderId=int(order_id))
    except Exception:
        pass


def _emergency_exit(client, symbol, qty):
    """Venta de mercado inmediata cuando no se pudo colocar el Stop-Loss."""
    try:
        order = client.order_market_sell(symbol=symbol, quantity=str(qty))
        fills = order.get("fills", [])
        proceeds = sum((D(f["price"]) * D(f["qty"]) for f in fills), Decimal(0))
        sold = sum((D(f["qty"]) for f in fills), Decimal(0)) or qty
        avg = proceeds / sold if sold > 0 else None
        logger.warning(f"Salida de emergencia ejecutada: {fmt(sold)} a ~{fmt(avg)} USDT")
        return avg, proceeds, sold
    except Exception as e:
        logger.critical(
            f"EXIT DE EMERGENCIA FALLO ({e}). POSICION SIN PROTECCION: "
            f"vende manualmente {fmt(qty)} {base_asset(symbol)} en Binance YA."
        )
        return None, None, None


def _place_protection(client, symbol, qty, take_price, stop_price):
    """
    Coloca Take-Profit + Stop-Loss obligatorios.
    1) Intento OCO (se cancelan mutuamente).
    2) Fallback: TP limit + SL STOP_LOSS_LIMIT separados (con reintentos).
    Devuelve (mode, tp_order_id, sl_order_id). Lanza ProtectionError si no
    logra proteger la posicion (el llamador debe salir de emergencia).
    """
    sl_limit_price = quantize_price(
        stop_price * (Decimal(1) - D(SL_LIMIT_BUFFER_PERCENT) / Decimal(100)),
        get_filters(client, symbol)["tick"],
    )

    # 1) OCO
    try:
        oco = client.order_oco_sell(
            symbol=symbol,
            quantity=str(qty),
            price=str(take_price),
            stopPrice=str(stop_price),
            stopLimitPrice=str(sl_limit_price),
            stopLimitTimeInForce="GTC",
        )
        tp_id = sl_id = None
        for r in oco.get("orderReports", []):
            oid = int(r["orderId"])
            if r.get("type") == "STOP_LOSS_LIMIT":
                sl_id = oid
            else:
                tp_id = oid
        if tp_id is None or sl_id is None:
            ids = [int(o["orderId"]) for o in oco.get("orders", [])]
            ids = [i for i in ids if i not in (tp_id, sl_id)]
            while ids and (tp_id is None or sl_id is None):
                if tp_id is None:
                    tp_id = ids.pop(0)
                else:
                    sl_id = ids.pop(0)
        logger.info(
            f"[{symbol}] OCO colocada: TP {fmt(take_price)} / SL {fmt(stop_price)} "
            f"(orderListId {oco.get('orderListId')})"
        )
        return "oco", tp_id, sl_id
    except Exception as e:
        logger.warning(f"[{symbol}] OCO fallo ({e}); usando ordenes separadas...")

    # 2) Separadas: primero TP limit...
    tp = client.order_limit_sell(symbol=symbol, quantity=str(qty), price=str(take_price))
    tp_id = int(tp["orderId"])
    logger.info(f"[{symbol}] TP limit colocada en {fmt(take_price)} (orderId {tp_id})")

    # ...luego SL stop-loss-limit con reintentos
    sl_id = None
    last_err = None
    for attempt in range(3):
        try:
            sl = client.create_order(
                symbol=symbol,
                side="SELL",
                type="STOP_LOSS_LIMIT",
                timeInForce="GTC",
                quantity=str(qty),
                price=str(sl_limit_price),
                stopPrice=str(stop_price),
            )
            sl_id = int(sl["orderId"])
            logger.info(f"[{symbol}] SL stop-limit colocada en {fmt(stop_price)} (orderId {sl_id})")
            break
        except (BinanceAPIException, BinanceOrderException) as e:
            last_err = e
            logger.warning(f"[{symbol}] Intento {attempt + 1}/3 de SL fallo: {e}")
            time.sleep(1)

    if sl_id is None:
        _cancel_quiet(client, symbol, tp_id)  # cancelar el TP que si se coloco
        raise ProtectionError(last_err)

    return "separada", tp_id, sl_id


def execute_trade(client, symbol=None, capital=None,
                  stop_loss_pct=None, take_profit_pct=None):
    """
    Compra con el capital asignado al par `symbol` y coloca inmediatamente
    Stop-Loss y Take-Profit. Devuelve dict con el resultado o None si falla.

    Seguridad financiera:
    - Nunca gasta mas de `capital` en esta operacion (ni mas del saldo libre).
    - Valida minNotional y minQty antes de enviar la orden.
    - Vende solo la cantidad neta recibida (la comision de compra se cobra
      en el activo base, por lo que no se puede vender la cantidad bruta).
    """
    symbol = symbol or CFG.get("symbols", SYMBOLS)[0]
    capital = capital if capital is not None else CFG.get("capital", CAPITAL_MAX)
    stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else CFG.get("sl", STOP_LOSS_PERCENT)
    take_profit_pct = take_profit_pct if take_profit_pct is not None else CFG.get("tp", TAKE_PROFIT_PERCENT)
    asset = base_asset(symbol)

    try:
        flt = get_filters(client, symbol)

        # 1. Capital real disponible
        balance = client.get_asset_balance(asset="USDT") or {"free": "0"}
        available = D(balance["free"])
        cap = min(D(capital), available)
        if cap <= 0 or cap < flt["min_notional"]:
            logger.error(
                f"[{symbol}] Saldo USDT insuficiente ({fmt(available)}); se necesita "
                f"al menos {fmt(flt['min_notional'])} (minNotional). Operacion omitida."
            )
            return None

        # 2. Precio y cantidad (descontando comision de compra 0.1%)
        price = get_current_price(client, symbol)
        fee_factor = Decimal(1) + D(COMMISSION_PERCENT) / Decimal(100)
        qty = quantize_qty(cap / (price * fee_factor), flt["step"])
        if qty < flt["min_qty"] or qty * price < flt["min_notional"]:
            logger.error(
                f"[{symbol}] Capital demasiado pequeno: qty calculada {fmt(qty)} "
                f"(minQty {flt['min_qty']}, minNotional {flt['min_notional']})."
            )
            return None

        # 3. Compra a mercado
        logger.info(f"[{symbol}] Comprando {fmt(qty)} {asset} a ~{fmt(price)} USDT (max {fmt(cap)} USDT)")
        buy_order = client.order_market_buy(symbol=symbol, quantity=str(qty))
        logger.info(f"[{symbol}] Orden de compra ejecutada: orderId {buy_order['orderId']}")

        fills = buy_order.get("fills", [])
        spend = sum((D(f["price"]) * D(f["qty"]) for f in fills), Decimal(0)) or qty * price
        gross_qty = sum((D(f["qty"]) for f in fills), Decimal(0)) or qty
        commission_base = sum(
            (D(f["commission"]) for f in fills if f.get("commissionAsset") == asset),
            Decimal(0),
        )
        net_qty = quantize_qty(gross_qty - commission_base, flt["step"])
        buy_avg = spend / gross_qty
        logger.info(
            f"[{symbol}] Llenado: {fmt(gross_qty)} {asset}, comision {fmt(commission_base)} "
            f"{asset}, neto vendible {fmt(net_qty)}, precio medio {fmt(buy_avg)} USDT"
        )
        if net_qty <= 0:
            logger.critical(f"[{symbol}] Cantidad neta tras comision es 0; no se puede proteger.")
            return None

        trade = {
            "symbol": symbol,
            "asset": asset,
            "qty": net_qty,
            "spend": spend,
            "buy_price": buy_avg.quantize(D("0.00000001")),
            "stop_price": quantize_price(buy_avg * (Decimal(1) - D(stop_loss_pct) / Decimal(100)), flt["tick"]),
            "take_price": quantize_price(buy_avg * (Decimal(1) + D(take_profit_pct) / Decimal(100)), flt["tick"]),
            "capital": cap,
            "mode": None,
            "tp_id": None,
            "sl_id": None,
            "outcome": None,
            "sell_avg": None,
            "sell_qty": None,
            "pnl": None,
            "note": "",
        }

        # 4. Proteccion obligatoria (SL/TP)
        try:
            mode, tp_id, sl_id = _place_protection(
                client, symbol, net_qty, trade["take_price"], trade["stop_price"]
            )
            trade.update(mode=mode, tp_id=tp_id, sl_id=sl_id)
        except ProtectionError as e:
            logger.error(f"[{symbol}] No se pudo colocar el Stop-Loss ({e}). Salida de emergencia...")
            avg, proceeds, sold = _emergency_exit(client, symbol, net_qty)
            if avg is not None:
                net_proceeds = proceeds * (Decimal(1) - D(COMMISSION_PERCENT) / Decimal(100))
                trade.update(
                    outcome="win" if net_proceeds >= spend else "loss",
                    sell_avg=avg,
                    sell_qty=sold,
                    note="salida de emergencia (SL no disponible)",
                )
            else:
                trade["note"] = "SIN PROTECCION - REVISAR MANUALMENTE"
        return trade

    except (BinanceAPIException, BinanceOrderException) as e:
        logger.error(f"[{symbol}] API Binance rechazo la operacion: {e}")
        return None
    except Exception as e:
        logger.error(f"[{symbol}] Error inesperado en execute_trade: {e}")
        return None


# ---------------------------------------------------------------------------
# Monitoreo de posiciones abiertas (por estado de ordenes, no por saldo)
# ---------------------------------------------------------------------------
DEAD_STATUS = {"CANCELED", "REJECTED", "EXPIRED"}


def check_open_position(client, trade, symbol=None):
    """
    Consulta el estado de las ordenes TP/SL de una posicion.
    Devuelve (outcome, updates): outcome 'win'/'loss' si la posicion cerro,
    None si sigue abierta. 'updates' son campos a fusionar en el trade.
    """
    symbol = symbol or trade.get("symbol") or CFG.get("symbols", SYMBOLS)[0]
    try:
        tp = _order_state(client, symbol, trade["tp_id"])
        sl = _order_state(client, symbol, trade["sl_id"])
    except Exception as e:
        logger.warning(f"[{symbol}] No se pudo consultar estado de ordenes: {e}")
        return None, {}

    if "UNKNOWN" in (tp["status"], sl["status"]):
        return None, {}  # reintentar en el proximo ciclo

    updates = {}

    def close(side, st):
        avg = st["quote"] / st["executed"] if st["executed"] > 0 else None
        updates.update(outcome=side, sell_avg=avg, sell_qty=st["executed"] or None)

    if tp["status"] == "FILLED":
        close("win", tp)
        _cancel_quiet(client, symbol, trade["sl_id"])
        return "win", updates
    if sl["status"] == "FILLED":
        close("loss", sl)
        _cancel_quiet(client, symbol, trade["tp_id"])
        return "loss", updates

    # Ejecuciones parciales sobre ordenes muertas (raro, pero posible)
    if tp["status"] in DEAD_STATUS and tp["executed"] > 0:
        close("win", tp)
        return "win", updates
    if sl["status"] in DEAD_STATUS and sl["executed"] > 0:
        close("loss", sl)
        return "loss", updates

    # Ambas ordenes muertas sin ejecutar: la proteccion se perdio -> reponer
    if tp["status"] in DEAD_STATUS and sl["status"] in DEAD_STATUS:
        logger.warning(f"[{symbol}] TP y SL inactivos sin ejecutarse. Reponiendo proteccion...")
        flt = get_filters(client, symbol)
        bal = client.get_asset_balance(asset=trade["asset"]) or {"free": "0"}
        free = quantize_qty(D(bal["free"]), flt["step"])
        qty = min(free, trade["qty"])
        if qty <= 0:
            updates["note"] = "sin saldo para reproteger - revisar manualmente"
            return None, updates
        try:
            mode, tp_id, sl_id = _place_protection(
                client, symbol, qty, trade["take_price"], trade["stop_price"]
            )
            updates.update(mode=mode, tp_id=tp_id, sl_id=sl_id, qty=qty,
                           note="proteccion repuesta")
            logger.info(f"[{symbol}] Proteccion repuesta ({mode}) por {fmt(qty)} {trade['asset']}")
        except ProtectionError as e:
            logger.critical(f"[{symbol}] No se pudo reponer la proteccion ({e}). Revisar manualmente.")
            updates["note"] = "SIN PROTECCION - revisar manualmente"
        return None, updates

    return None, {}  # sigue abierta


def compute_trade_pnl(trade):
    """PnL neto de una operacion cerrada, descontando comision de venta 0.1%."""
    if trade.get("outcome") not in ("win", "loss"):
        return None
    sell_avg = trade.get("sell_avg")
    sell_qty = trade.get("sell_qty") or trade["qty"]
    if not sell_avg:
        sell_avg = trade["take_price"] if trade["outcome"] == "win" else trade["stop_price"]
    proceeds = sell_avg * sell_qty
    fee_sell = proceeds * D(COMMISSION_PERCENT) / Decimal(100)
    return (proceeds - fee_sell - trade["spend"]).quantize(D("0.01"))


# ---------------------------------------------------------------------------
# 4. Reporte en consola
# ---------------------------------------------------------------------------
def generate_report(initial_capital, trades, current_prices=None):
    """Imprime el resumen final: capital, ops, wins/losses, PnL neto y capital final."""
    current_prices = current_prices or {}
    print("\n" + "=" * 78)
    print("                          REPORTE FINAL DEL BOT")
    print("=" * 78)

    if not trades:
        print("No se ejecutaron operaciones durante la sesion.")
        print("=" * 78 + "\n")
        return

    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    open_pos = [t for t in trades if t.get("outcome") is None]

    rows = []
    for i, t in enumerate(trades, 1):
        estado = (t.get("outcome") or "ABIERTA").upper()
        if t.get("note"):
            estado += f" ({t['note']})"
        rows.append([
            i, t["symbol"], fmt(t["qty"]), fmt(t["buy_price"], 6),
            fmt(t["take_price"], 6), fmt(t["stop_price"], 6),
            estado, fmt(t.get("pnl"), 2),
        ])
    print("\nDETALLE DE OPERACIONES:")
    print(tabulate(rows, headers=["#", "Par", "Cant.", "Compra", "TP", "SL", "Estado", "PnL USDT"],
                   tablefmt="grid"))

    realized = sum((t["pnl"] for t in trades if t.get("pnl") is not None), Decimal(0))

    unrealized = Decimal(0)
    has_estimate = bool(open_pos) and all(t["symbol"] in current_prices for t in open_pos)
    if has_estimate:
        for t in open_pos:
            px = current_prices[t["symbol"]]
            est_value = px * t["qty"] * (Decimal(1) - D(COMMISSION_PERCENT) / Decimal(100))
            unrealized += est_value - t["spend"]

    summary = [
        ["Capital inicial asignado (USDT)", fmt(initial_capital, 2)],
        ["Operaciones ejecutadas", len(trades)],
        ["Ganadoras", len(wins)],
        ["Perdedoras", len(losses)],
        ["Posiciones aun abiertas", len(open_pos)],
        ["PnL neto realizado (USDT)", fmt(realized, 2)],
    ]
    if has_estimate:
        summary.append(["PnL no realizado estimado (USDT)", fmt(unrealized, 2)])
    summary.append([
        "Capital final" + (" estimado" if has_estimate else "") + " (USDT)",
        fmt(initial_capital + realized + unrealized, 2),
    ])
    print("\nRESUMEN:")
    print(tabulate(summary, headers=["Concepto", "Valor"], tablefmt="grid"))
    print("=" * 78 + "\n")


# ---------------------------------------------------------------------------
# Persistencia de estado (state.json)
# ---------------------------------------------------------------------------
NUMERIC_KEYS = {"qty", "spend", "buy_price", "stop_price", "take_price", "capital",
                "sell_avg", "sell_qty", "pnl", "base", "trigger"}
ID_KEYS = {"tp_id", "sl_id"}


def save_state(trades, active, bases, consec_losses=0):
    try:
        data = {
            "format": "multi",
            "trades": trades,
            "active": active,
            "bases": {s: {"base": str(b["base"]), "trigger": str(b["trigger"])}
                      for s, b in bases.items()},
            "consec_losses": consec_losses,
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.warning(f"No se pudo guardar el estado: {e}")


def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"Estado corrupto, se ignora ({e})")
        return None
    if data.get("format") != "multi":
        logger.info("Estado de una version anterior; se empieza sesion nueva.")
        return None

    def revive(node):
        if isinstance(node, list):
            return [revive(x) for x in node]
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in ID_KEYS and v is not None:
                    out[k] = int(v)
                    continue
                if isinstance(v, str):
                    if k in NUMERIC_KEYS:
                        try:
                            out[k] = Decimal(v)
                            continue
                        except Exception:
                            pass
                    # Intentar convertir valores numericos de keys no conocidas
                    try:
                        out[k] = Decimal(v)
                        continue
                    except Exception:
                        pass
                out[k] = revive(v)
            return out
        return node

    data = revive(data)
    data.setdefault("trades", [])
    data.setdefault("active", {})
    data.setdefault("bases", {})
    data.setdefault("consec_losses", 0)
    return data


# ---------------------------------------------------------------------------
# 5. Bucle principal
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Bot de trading Spot para Binance (momentum / reversion a la media).")
    p.add_argument("--strategy", choices=["momentum", "dip"], default=STRATEGY,
                   help="momentum = escanea el mercado y sigue criptos populares en subida; "
                        "dip = vigila una lista fija (--symbols)")
    p.add_argument("--symbols", "--symbol", dest="symbols",
                   default=",".join(SYMBOLS),
                   help="Pares a vigilar separados por coma (solo modo dip)")
    p.add_argument("--capital", type=float, default=CAPITAL_MAX,
                   help="Capital TOTAL en USDT (se reparte entre posiciones simultaneas)")
    p.add_argument("--max-open", type=int, default=MAX_OPEN_POSITIONS,
                   help="Posiciones simultaneas maximas")
    p.add_argument("--recovery", type=float, default=RECOVERY_FACTOR,
                   help="Multiplicador de tamano tras una perdida (1 = desactivado; ej. 1.5). "
                        "Nunca supera el capital total asignado")
    p.add_argument("--recovery-max", type=int, default=RECOVERY_MAX_STEPS,
                   help="Maximos pasos de recuperacion consecutivos")
    p.add_argument("--duration", type=int, default=DURATION_MINUTES,
                   help="Minutos de operacion (0 = ilimitado)")
    p.add_argument("--max-trades", type=int, default=MAX_TRADES,
                   help="Maximo de operaciones totales (0 = ilimitado)")
    p.add_argument("--drop", type=float, default=BUY_DROP_PERCENT,
                   help="%% de caida desde el precio de referencia para comprar (entrada)")
    p.add_argument("--sl", type=float, default=STOP_LOSS_PERCENT, help="Stop-Loss %%")
    p.add_argument("--tp", type=float, default=TAKE_PROFIT_PERCENT, help="Take-Profit %%")
    p.add_argument("--interval", type=int, default=MONITOR_INTERVAL,
                   help="Segundos entre escaneos de precios")
    p.add_argument("--scan-interval", type=int, default=SCAN_INTERVAL_MIN,
                   help="Minutos entre escaneos de mercado (modo momentum)")
    p.add_argument("--min-volume", type=float, default=MIN_QUOTE_VOLUME,
                   help="Volumen 24h minimo en USDT para entrar al radar")
    p.add_argument("--top-n", type=int, default=TOP_N_WATCHLIST,
                   help="Maximos candidatos en el radar")
    p.add_argument("--min-change", type=float, default=MIN_24H_CHANGE,
                   help="Subida 24h minima %% para el radar")
    p.add_argument("--max-change", type=float, default=MAX_24H_CHANGE,
                   help="Subida 24h maxima %% (evita perseguir pumps)")
    p.add_argument("--testnet", action="store_true", help="Usar testnet (sin dinero real)")
    p.add_argument("--reset", action="store_true", help="Ignorar/borrar state.json previo")
    return p.parse_args()


def main():
    args = parse_args()

    cli_symbols = []
    for s in args.symbols.split(","):
        s = s.strip().upper()
        if s and s not in cli_symbols:
            cli_symbols.append(s)
    if not cli_symbols:
        logger.error("Lista de simbolos vacia.")
        sys.exit(1)

    CFG.update(
        symbols=cli_symbols, strategy=args.strategy,
        scan_interval=max(1, args.scan_interval), min_volume=args.min_volume,
        top_n=max(1, args.top_n), min_change=args.min_change, max_change=args.max_change,
        capital=args.capital, max_open=max(1, args.max_open),
        recovery=args.recovery, recovery_max=max(0, args.recovery_max),
        duration=args.duration, max_trades=args.max_trades, drop=args.drop,
        sl=args.sl, tp=args.tp, interval=args.interval, testnet=args.testnet,
    )

    if args.reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        logger.info("Estado anterior borrado (--reset).")

    client, _testnet = load_config()

    if _testnet and CFG["strategy"] == "momentum":
        logger.info("Radar basado en el mercado REAL (solo lectura); las ordenes se ejecutan en TESTNET.")

    # Radar inicial segun estrategia
    watch = []
    if CFG["strategy"] == "momentum":
        logger.info("MODO MOMENTUM: escaneando el mercado en busca de criptos populares en subida...")
        watch = scan_market()
        if not watch:
            logger.error(
                "Ninguna cripto cumple los filtros ahora mismo (mercado plano o filtros muy "
                "exigentes). Reintenta mas tarde o usa --strategy dip con --symbols."
            )
            sys.exit(1)
        symbols = [w["symbol"] for w in watch]
    else:
        logger.info("MODO DIP: lista fija de pares.")
        symbols = list(cli_symbols)

    # Validar simbolos contra el entorno de ejecucion (testnet tiene menos pares)
    valid_symbols = []
    for s in symbols:
        try:
            flt = get_filters(client, s)
            logger.info(
                f"Filtros {s}: tick={fmt(flt['tick'])} step={fmt(flt['step'])} "
                f"minNotional={fmt(flt['min_notional'], 2)} USDT"
            )
            valid_symbols.append(s)
        except Exception as e:
            logger.warning(f"{s} no esta disponible en este entorno; se descarta ({e})")
    if not valid_symbols:
        logger.error("Ningun par del radar esta disponible para operar en este entorno.")
        sys.exit(1)
    symbols = valid_symbols
    watch = [w for w in watch if w["symbol"] in set(symbols)]

    base_size = D(CFG["capital"]) / CFG["max_open"]

    # Restaurar sesion anterior si existe
    state = load_state()
    if state:
        trades = state["trades"]
        active = state["active"]
        bases = state["bases"]
        consec_losses = int(state.get("consec_losses") or 0)
        logger.warning(
            f"Sesion anterior restaurada: {len(active)} posicion(es) abierta(s) "
            f"({', '.join(active.keys()) if active else 'ninguna'})."
        )
    else:
        trades, active, bases = [], {}, {}
        consec_losses = 0
        if CFG["strategy"] == "momentum":
            for w in watch:
                # referencia con el precio del entorno donde se ejecutan ordenes
                try:
                    p = get_current_price(client, w["symbol"])
                except Exception:
                    p = w["price"]
                bases[w["symbol"]] = {
                    "base": p,
                    "trigger": p * (Decimal(1) - D(CFG["drop"]) / Decimal(100)),
                }
        else:
            for s in symbols:
                p = get_current_price(client, s)
                bases[s] = {
                    "base": p,
                    "trigger": p * (Decimal(1) - D(CFG["drop"]) / Decimal(100)),
                }

    initial_capital = D(CFG["capital"])
    start_time = time.time()
    max_seconds = CFG["duration"] * 60 if CFG["duration"] > 0 else 0
    next_scan = time.time() + CFG["scan_interval"] * 60 if CFG["strategy"] == "momentum" else 0

    logger.info(f"Pares vigilados: {', '.join(symbols)}")
    for s in symbols:
        if s in bases:
            logger.info(f"  {s}: base {fmt(bases[s]['base'])} -> compra a <= {fmt(bases[s]['trigger'])} (-{CFG['drop']}%)")
    logger.info(
        f"SL -{CFG['sl']}% / TP +{CFG['tp']}% | capital total {fmt(initial_capital, 2)} USDT | "
        f"{fmt(base_size, 2)} USDT por operacion | max {CFG['max_open']} simultaneas | "
        f"max {CFG['max_trades'] or 'inf'} ops | duracion {CFG['duration'] or 'inf'} min"
    )
    if CFG["recovery"] > 1:
        logger.warning(
            f"Modo recuperacion ACTIVO: tras cada perdida el tamano sube x{CFG['recovery']} "
            f"(max {CFG['recovery_max']} pasos). Con tope absoluto del capital total."
        )

    def refresh_base(symbol, price):
        bases[symbol] = {
            "base": price,
            "trigger": price * (Decimal(1) - D(CFG["drop"]) / Decimal(100)),
        }

    try:
        while True:
            if max_seconds and (time.time() - start_time) >= max_seconds:
                logger.info(f"Tiempo limite alcanzado ({CFG['duration']} min). Deteniendo.")
                break
            if CFG["max_trades"] and len(trades) >= CFG["max_trades"]:
                logger.info(f"Maximo de operaciones alcanzado ({CFG['max_trades']}). Deteniendo.")
                break

            # Re-escaneo periodico del mercado (modo momentum)
            if next_scan and time.time() >= next_scan:
                watch = scan_market()
                watch_syms = {w["symbol"] for w in watch}
                for w in watch:
                    s = w["symbol"]
                    if s in active or s in bases:
                        continue  # los que ya se vigilan conservan su referencia
                    try:
                        get_filters(client, s)  # valida que exista en este entorno
                    except Exception:
                        continue
                    try:
                        p = get_current_price(client, s)
                    except Exception:
                        p = w["price"]
                    bases[s] = {
                        "base": p,
                        "trigger": p * (Decimal(1) - D(CFG["drop"]) / Decimal(100)),
                    }
                for s in list(bases.keys()):
                    if s not in watch_syms and s not in active:
                        del bases[s]
                symbols = [w["symbol"] for w in watch] + \
                    [s for s in active if s not in watch_syms]
                next_scan = time.time() + CFG["scan_interval"] * 60
                save_state(trades, active, bases, consec_losses)

            try:
                # 1) Gestionar posiciones abiertas (detectar TP/SL ejecutados)
                for sym in list(active.keys()):
                    trade = active[sym]
                    outcome, updates = check_open_position(client, trade, sym)
                    if updates:
                        trade.update(updates)
                    if outcome:
                        trade["pnl"] = compute_trade_pnl(trade)
                        logger.info(
                            f"[{sym}] Posicion cerrada por "
                            f"{'TAKE-PROFIT' if outcome == 'win' else 'STOP-LOSS'}. "
                            f"PnL: {fmt(trade['pnl'], 2)} USDT"
                        )
                        consec_losses = consec_losses + 1 if outcome == "loss" else 0
                        del active[sym]
                        refresh_base(sym, get_current_price(client, sym))
                        save_state(trades, active, bases, consec_losses)

                # 2) Buscar nuevas entradas si hay slot libre
                if len(active) < CFG["max_open"]:
                    # Tamano de la proxima entrada (recuperacion acotada si procede)
                    entry_size = base_size
                    if consec_losses > 0 and CFG["recovery"] > 1:
                        steps = min(consec_losses, CFG["recovery_max"])
                        entry_size = base_size * (D(CFG["recovery"]) ** steps)
                        logger.info(
                            f"Modo recuperacion: {consec_losses} perdida(s) seguida(s) -> "
                            f"tamano x{fmt(entry_size / base_size, 2)} ({fmt(entry_size, 2)} USDT)"
                        )
                    # Tope absoluto: nunca invertir mas del capital total asignado
                    exposed = sum((t["spend"] for t in active.values()), Decimal(0))
                    entry_size = min(entry_size, initial_capital - exposed)
                    if entry_size < D("6"):
                        logger.info(
                            f"Sin margen suficiente para nueva entrada "
                            f"(expuesto {fmt(exposed, 2)}/{fmt(initial_capital, 2)} USDT)."
                        )
                    else:
                        monitor_parts = []
                        for sym in symbols:
                            if sym in active:
                                continue
                            if CFG["max_trades"] and len(trades) >= CFG["max_trades"]:
                                break
                            if len(active) >= CFG["max_open"]:
                                break

                            price = get_current_price(client, sym)
                            ref = bases.get(sym)
                            if not ref:
                                refresh_base(sym, price)
                                ref = bases[sym]

                            if price <= ref["trigger"]:
                                logger.info(
                                    f"[{sym}] Precio {fmt(price)} <= umbral {fmt(ref['trigger'])}. Abriendo posicion..."
                                )
                                trade = execute_trade(client, symbol=sym, capital=entry_size)
                                if trade:
                                    trades.append(trade)
                                    active[sym] = trade
                                    refresh_base(sym, trade["buy_price"])
                                    save_state(trades, active, bases, consec_losses)
                                    if len(active) >= CFG["max_open"]:
                                        break
                                else:
                                    logger.warning(f"[{sym}] No se pudo abrir posicion. Se reintenta luego.")
                            else:
                                monitor_parts.append(f"{sym} {fmt(price)} (umbral {fmt(ref['trigger'])})")

                        if monitor_parts:
                            logger.info("Monitoreando... " + " | ".join(monitor_parts))

                time.sleep(CFG["interval"])

            except BinanceAPIException as e:
                logger.error(f"Error de API: {getattr(e, 'code', '')} {e}. Reintentando...")
                time.sleep(CFG["interval"])
            except Exception as e:
                logger.error(f"Error general: {e}. Reintentando...")
                time.sleep(CFG["interval"])

    except KeyboardInterrupt:
        logger.info("Interrupcion del usuario. Generando reporte...")

    finally:
        current_prices = {}
        for sym in active:
            try:
                current_prices[sym] = get_current_price(client, sym)
            except Exception:
                pass
        generate_report(initial_capital, trades, current_prices)
        save_state(trades, active, bases, consec_losses)


if __name__ == "__main__":
    main()
