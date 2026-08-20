# Binance Trading Bot

Bot de consola en Python para trading automatizado en el mercado **Spot de Binance**, usando una estrategia simple de **reversión a la media / rango dinámico**.

## Advertencia de riesgo

> El trading con criptomonedas implica un alto riesgo. **Nunca inviertas dinero que no puedas permitirte perder.** Este bot es una herramienta educativa; úsalo bajo tu propia responsabilidad. Recomendamos probar primero en **testnet**.

## Instalación

```bash
# 1. Crear entorno virtual (recomendado)
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux/macOS

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar credenciales
copy .env.example .env         # Windows
cp .env.example .env           # Linux/macOS
```

Completa `.env` con tus credenciales de la [API de Binance](https://www.binance.com/en/my/settings/api-management).

## Configuración

Todos los parámetros se editan como constantes al inicio de `bot.py`:

| Parámetro | Descripción | Ejemplo |
|---|---|---|
| `SYMBOL` | Par de monedas Spot | `BTCUSDT` |
| `CAPITAL_MAX` | Capital máximo a invertir (USDT) | `100` |
| `BUY_DROP_PERCENT` | % de caída desde el precio base para comprar | `0.5` (0.5%) |
| `STOP_LOSS_PERCENT` | Stop-Loss obligatorio por operación | `1.0` (1%) |
| `TAKE_PROFIT_PERCENT` | Take-Profit por operación | `1.5` (1.5%) |
| `MONITOR_INTERVAL` | Segundos entre chequeos de precio | `5` |
| `DURATION_MINUTES` | Duración total (0 = ilimitada) | `60` |
| `MAX_TRADES` | Máximo de operaciones (0 = ilimitadas) | `10` |
| `USE_TESTNET` | `True` = modo simulación sin dinero real | `False` |

## Uso

```bash
python bot.py
```

El bot:
1. Obtiene el precio base al arrancar.
2. Monitorea el precio cada `MONITOR_INTERVAL` segundos.
3. Cuando el precio cae `BUY_DROP_PERCENT`% respecto al precio base, compra con el capital asignado.
4. Coloca inmediatamente una **orden OCO** (Take-Profit + Stop-Loss) que se cancelan mutuamente.
5. Se detiene al alcanzar `DURATION_MINUTES` o `MAX_TRADES`, y muestra el reporte final.

## Seguridad financiera

- **Nunca** invierte más del capital asignado (`CAPITAL_MAX`).
- Cada posición abierta tiene obligatoriamente Stop-Loss y Take-Profit.
- No abre una segunda posición si ya hay una activa.
- Verifica el saldo disponible antes de comprar (usando la cantidad que alcance si es menor).
- Descuenta la comisión de Binance (0.1% compra + 0.1% venta) en el cálculo de rentabilidad.

## Reporte

Al finalizar, imprime en consola:

- Capital inicial.
- Número de operaciones ejecutadas.
- Ganadoras vs. perdedoras.
- Ganancia/pérdida neta en USDT (descontando comisiones).
- Capital final estimado.

## Estructura

```
bot.py               # Script principal (5 funciones: load_config, get_current_price,
                     # execute_trade, generate_report, main)
requirements.txt     # Dependencias
.env.example         # Plantilla de credenciales
```

## Testnet (recomendado para pruebas)

1. Crea credenciales en [testnet.binance.vision](https://testnet.binance.vision).
2. Pon `BINANCE_TESTNET=true` en `.env` (o `USE_TESTNET = True` en `bot.py`).
3. Añade USDT de prueba gratuitos desde el faucet de la testnet.