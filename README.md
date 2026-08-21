# Binance Trading Bot

Bot de consola en Python para trading automatizado en el mercado **Spot de Binance** con dos estrategias:

- **MOMENTUM** (por defecto): escanea todo el mercado cada N minutos, detecta las criptos **populares** (alto volumen 24h) que están **subiendo** y las pone en el radar. Compra cuando el precio retrocede ligeramente desde ese nivel (entrada segura en tendencia).
- **DIP**: vigila una **lista fija** de pares (`--symbols`) y compra caídas (reversión a la media).

En ambos casos cada posición abre con Stop-Loss y Take-Profit obligatorios.

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

Todos los parámetros se editan como constantes al inicio de `bot.py` o por línea de comandos:

| Parámetro | Descripción | Ejemplo |
|---|---|---|
| `STRATEGY` / `--strategy` | `momentum` (radar dinámico) o `dip` (lista fija) | `momentum` |
| `SYMBOLS` / `--symbols` | Pares a vigilar (separados por coma, solo modo dip) | `BTCUSDT,ETHUSDT,SOLUSDT` |
| `CAPITAL_MAX` / `--capital` | Capital **total** asignado (USDT) | `100` |
| `MAX_OPEN_POSITIONS` / `--max-open` | Posiciones simultáneas máximas | `3` |
| `BUY_DROP_PERCENT` / `--drop` | % de caída desde el precio de referencia para comprar | `0.5` (0.5%) |
| `STOP_LOSS_PERCENT` / `--sl` | Stop-Loss obligatorio por operación | `1.0` (1%) |
| `TAKE_PROFIT_PERCENT` / `--tp` | Take-Profit por operación | `1.5` (1.5%) |
| `MONITOR_INTERVAL` / `--interval` | Segundos entre escaneos de precios | `5` |
| `DURATION_MINUTES` / `--duration` | Duración total (0 = ilimitada) | `60` |
| `MAX_TRADES` / `--max-trades` | Máximo de operaciones totales (0 = ilimitadas) | `10` |
| `SCAN_INTERVAL_MIN` / `--scan-interval` | Minutos entre re-escaneos del mercado (momentum) | `10` |
| `MIN_QUOTE_VOLUME` / `--min-volume` | Volumen 24h mínimo (USDT) para entrar al radar | `20000000` |
| `TOP_N_WATCHLIST` / `--top-n` | Máximo de candidatos en el radar por escaneo | `5` |
| `MIN_24H_CHANGE` / `--min-change` | Subida 24h mínima % para el radar | `0.5` |
| `MAX_24H_CHANGE` / `--max-change` | Subida 24h máxima % (evita perseguir pumps) | `25` |
| `RECOVERY_FACTOR` / `--recovery` | Multiplicador de tamaño tras pérdida (1 = desactivado) | `1.5` |
| `RECOVERY_MAX_STEPS` / `--recovery-max` | Máximos pasos de recuperación consecutivos | `3` |
| `RISK_REWARD_RATIO` / `--rr` | Ratio TP/SL (TP = SL × rr) | `1.5` |
| `BTC_TREND_THRESHOLD` / `--btc-threshold` | Si BTC cae más de este %, bloquea entradas en altcoins | `-0.5` |
| `COOLDOWN_MINUTES` / `--cooldown` | Minutos de espera tras cerrar un trade en un par | `15` |
| `VOLATILITY_SL_MULT` / `--volatility-sl` | Multiplicador de SL según volatilidad | `1.0` |
| `USE_TESTNET` / `--testnet` | Modo simulación sin dinero real | `False` |

## Uso

```bash
python bot.py --testnet                                  # radar momentum (recomendado)
python bot.py --testnet -i                               # pide capital y horas por consola
python bot.py --strategy dip --symbols BTCUSDT,ETHUSDT   # lista fija de pares
python bot.py --rr 2.0 --btc-threshold -1.0              # filtros estrictos
python bot.py --cooldown 30 --volatility-sl 1.5          # cooldown mayor + SL dinamico
python bot.py --top-n 8 --min-volume 50000000            # radar mas amplio/exigente
python bot.py --capital 90 --max-open 3                  # 30 USDT por operacion
python bot.py --duration 30 --max-trades 6               # limites de sesion
python bot.py --reset                                    # ignora state.json previo
```

### Entrada interactiva

Con `-i` o `--interactive`, el bot te pide al iniciar:
- **Capital a invertir** (USDT)
- **Horas de operacion**

Útil para no tener que recordar los flags cada vez.

### Como funciona el modo MOMENTUM

1. Cada `--scan-interval` minutos (10 por defecto) consulta las estadisticas de 24h de todo el mercado.
2. Filtra: solo pares USDT, excluye stablecoins y tokens apalancados (UP/DOWN/BULL/BEAR).
3. Exige volumen 24h >= `--min-volume` (20M USDT por defecto = cripto popular y liquida) y subida 24h entre `--min-change` (+0.5%) y `--max-change` (+25%, para no perseguir pumps).
4. Los mejores `--top-n` candidatos entran al radar con su precio de referencia.
5. Compra cuando el precio retrocede `--drop`% desde esa referencia (no persigue la subida: espera un respiro), con SL/TP inmediatos.
6. El radar se renueva solo: si una cripto deja de estar en subida, sale; entra la siguiente.

### Filtros de seguridad (minimizar perdidas)

El bot aplica **5 filtros antes de cada entrada** para reducir la probabilidad de perdida:

1. **Filtro BTC** (`--btc-threshold`): si BTC cae más del umbral (-0.5% por defecto), no entra en altcoins. Evita comprar en medio de un desplome del mercado.
2. **Confirmación de volumen**: si el volumen SUBE durante el retroceso, es posible una reversal (no una consolidación sana). El bot lo descarta.
3. **SL dinámico** (`--volatility-sl`): monedas con alta volatilidad (ej. +15% en 24h) obtienen SL más amplio (-2.5% en vez de -1%) para no liquidarse por ruido normal.
4. **Cooldown** (`--cooldown`): tras cerrar una operación en un par, espera 15 min antes de re-entrar. Evita re-entradas inmediatas tras una pérdida.
5. **Risk-reward** (`--rr`): TP siempre es al menos 1.5× el SL. Si el mercado no da suficiente espacio, no entra.

El bot:
1. Obtiene el precio base de **cada par** al arrancar.
2. Escanea todos los pares cada `MONITOR_INTERVAL` segundos.
3. Cuando un par cae `BUY_DROP_PERCENT`% respecto a su base, compra con su fracción del capital (`capital / max-open`), mientras haya slots libres.
4. Coloca inmediatamente una **orden OCO** (Take-Profit + Stop-Loss) que se cancelan mutuamente. Si la OCO falla, usa TP limit + SL stop-limit separados; si el SL no puede colocarse, vende a mercado (salida de emergencia).
5. Puede tener hasta `MAX_OPEN_POSITIONS` posiciones abiertas a la vez en distintos pares; al cerrarse una, ese par vuelve a monitoreo.
6. Se detiene al alcanzar `DURATION_MINUTES` o `MAX_TRADES`, y muestra el reporte final.

Ejemplo: `--capital 90 --max-open 3` reparte 30 USDT por operación, así que **nunca** hay más de 90 USDT invertidos en total.

## Recuperación de pérdidas (opcional, usar con cuidado)

Con `--recovery 1.5`, tras cada pérdida el tamaño de la siguiente operación sube x1.5 para recuperarla con menos ganancias necesarias:

```bash
python bot.py --recovery 1.5 --recovery-max 3     # tras 3 pasos seguidos vuelve al tamaño base
```

Límites de seguridad integrados:
- Máximo `--recovery-max` pasos consecutivos (luego resetea al tamaño base).
- **Tope absoluto**: la suma de todas las posiciones abiertas jamás supera el capital total asignado.
- Si no queda margen (mínimo ~6 USDT), no abre más operaciones.

Advertencia: subir tamaños tras pérdidas (martingala) aumenta la velocidad de pérdida en rachas malas. Úsalo solo con capital que puedas permitirte perder.

## Pasar a dinero real

1. Crea la API en binance.com → Gestión de API → Crear API (HMAC).
2. Permisos: solo **Lectura + Trading Spot**. Nunca actives Retiros.
3. Restringe la API a tu IP si tu proveedor te da IP fija.
4. Pon las credenciales en `.env` y cambia `BINANCE_TESTNET=false`.
5. Ejecuta sin `--testnet`. Empieza con un capital pequeño.

## Estado y reinicios

El bot guarda su sesión en `state.json`. Si se interrumpe o reinicia con una posición abierta, **la retoma** (sigue monitoreando su TP/SL) en lugar de comprar dos veces. Usa `--reset` para empezar de cero.

## Seguridad financiera

- **Nunca** invierte más del capital asignado (`CAPITAL_MAX`).
- Cada posición abierta tiene obligatoriamente Stop-Loss y Take-Profit.
- El SL se adapta a la volatilidad de cada par (no usa un valor fijo peligroso).
- Filtra entradas cuando BTC está cayendo (evita altcoins en pánico).
- Requiere confirmación de volumen en el retroceso (consolidación sana).
- Cooldown entre trades del mismo par (evita re-entradas emocionales).
- Respeta el límite de posiciones simultáneas (`--max-open`) y el tope de capital total.
- Verifica el saldo disponible antes de comprar (usando la cantidad que alcance si es menor).
- Descuenta la comisión de Binance (0.1% compra + 0.1% venta) en el cálculo de rentabilidad.
- El tamaño por operación nunca supera la exposición máxima (capital - posiciones abiertas).

## Reporte

Al finalizar, imprime en consola:

- Capital inicial.
- Número de operaciones ejecutadas.
- Ganadoras vs. perdedoras.
- Ganancia/pérdida neta en USDT (descontando comisiones).
- Capital final estimado.

## Estructura

```
bot.py               # Script principal (load_config, get_current_price,
                     # execute_trade, generate_report, main)
requirements.txt     # Dependencias
.env.example         # Plantilla de credenciales
state.json           # Estado de la sesion (se crea solo; no editar a mano)
```

## Testnet (recomendado para pruebas)

1. Crea credenciales en [testnet.binance.vision](https://testnet.binance.vision).
2. Pon `BINANCE_TESTNET=true` en `.env`.
3. Ejecuta con `--testnet` (ej. `python bot.py --testnet --capital 100`).