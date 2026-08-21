# Binance Trading Bot

Bot de trading automatizado para el mercado **Spot de Binance** con estrategia de **momentum** (escaneo dinámico del mercado) y filtros de seguridad para minimizar pérdidas.

---

## Instalación

```bash
# 1. Crear entorno virtual
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux/macOS

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar credenciales
copy .env.example .env         # Windows
cp .env.example .env           # Linux/macOS
```

Edita `.env` con tus credenciales de la [API de Binance](https://www.binance.com/en/my/settings/api-management).

---

## Comandos rápidos (copiar y pegar)

### Testnet (sin dinero real, para probar)

```bash
# Lo más básico: radar momentum con 100 USDT de prueba
python bot.py --testnet --capital 100

# Pedir capital y horas por consola (sin recordar flags)
python bot.py --testnet -i

# Con filtros de seguridad más estrictos
python bot.py --testnet --capital 100 --rr 2.0 --btc-threshold -1.0 --cooldown 30

# Sesión corta de prueba (30 min, máximo 3 operaciones)
python bot.py --testnet --capital 50 --duration 30 --max-trades 3

# Reiniciar de cero (borrar estado anterior)
python bot.py --testnet --capital 100 --reset
```

### Dinero real

```bash
# Configuración conservadora para empezar
python bot.py --capital 30 --max-open 1 --duration 60 --max-trades 5

# Configuración estándar
python bot.py --capital 100 --max-open 3 --duration 120 --max-trades 10

# Modo agresivo (más pares, más operaciones)
python bot.py --capital 200 --max-open 5 --top-n 8 --min-volume 50000000

# Un solo par específico (modo dip)
python bot.py --strategy dip --symbols ETHUSDT --capital 50 --max-open 1
```

### Configuración personalizada

```bash
# Radar amplio: más candidatos, volumen mínimo más alto
python bot.py --top-n 8 --min-volume 50000000 --min-change 1.0

# Entrada más conservadora: esperar mayor retroceso
python bot.py --drop 0.8 --sl 1.5 --tp 2.5

# SL dinámico más agresivo (se adapta más a volatilidad)
python bot.py --volatility-sl 1.5 --btc-threshold -1.0

# Recuperación de pérdidas (usar con cuidado)
python bot.py --recovery 1.5 --recovery-max 3

# Escaneo más frecuente del mercado
python bot.py --scan-interval 5 --interval 3

# Sin límite de tiempo ni operaciones
python bot.py --duration 0 --max-trades 0
```

---

## Parámetros de configuración

### Capital y operaciones

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `CAPITAL_MAX` | `--capital` | Capital total en USDT | `100` |
| `MAX_OPEN_POSITIONS` | `--max-open` | Posiciones simultáneas máximas | `3` |
| `DURATION_MINUTES` | `--duration` | Minutos de operación (0 = ilimitado) | `60` |
| `MAX_TRADES` | `--max-trades` | Máximo de operaciones totales (0 = ilimitado) | `10` |

**Fórmula:** `capital ÷ max-open` = USDT por operación. Nunca se supera el capital total.

### Estrategia de entrada

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `BUY_DROP_PERCENT` | `--drop` | % de caída desde el precio de referencia para comprar | `0.5` |
| `STRATEGY` | `--strategy` | `momentum` (radar dinámico) o `dip` (lista fija) | `momentum` |
| `SYMBOLS` | `--symbols` | Pares a vigilar (solo modo dip, separados por coma) | `BTCUSDT,...` |

### SL / TP / Risk-Reward

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `STOP_LOSS_PERCENT` | `--sl` | Stop-Loss base % | `1.0` |
| `TAKE_PROFIT_PERCENT` | `--tp` | Take-Profit base % | `1.5` |
| `RISK_REWARD_RATIO` | `--rr` | TP = SL × rr (mínimo 1.0) | `1.5` |
| `VOLATILITY_SL_MULT` | `--volatility-sl` | Multiplicador de SL según volatilidad | `1.0` |

### Filtros de seguridad

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `BTC_TREND_THRESHOLD` | `--btc-threshold` | Si BTC cae más de este %, bloquea entradas | `-0.5` |
| `COOLDOWN_MINUTES` | `--cooldown` | Minutos sin re-entrar tras cerrar un trade | `15` |

### Radar momentum (escaneo de mercado)

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `SCAN_INTERVAL_MIN` | `--scan-interval` | Minutos entre re-escaneos | `10` |
| `MIN_QUOTE_VOLUME` | `--min-volume` | Volumen 24h mínimo (USDT) | `20000000` |
| `TOP_N_WATCHLIST` | `--top-n` | Candidatos máximos en el radar | `5` |
| `MIN_24H_CHANGE` | `--min-change` | Subida 24h mínima % | `0.5` |
| `MAX_24H_CHANGE` | `--max-change` | Subida 24h máxima % (anti-pump) | `25` |

### Monitoreo

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `MONITOR_INTERVAL` | `--interval` | Segundos entre chequeos de precio | `5` |

### Recuperación (usar con cuidado)

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `RECOVERY_FACTOR` | `--recovery` | Multiplicador tras pérdida (1 = desactivado) | `1.0` |
| `RECOVERY_MAX_STEPS` | `--recovery-max` | Pasos máximos de recuperación | `3` |

### Otros

| Parámetro | CLI | Descripción | Default |
|---|---|---|---|
| `USE_TESTNET` | `--testnet` | Modo prueba (sin dinero real) | `False` |
| — | `--reset` | Borrar state.json y empezar de cero | — |
| — | `-i` / `--interactive` | Pedir capital y horas por consola | — |

---

## Cómo funciona

### Modo MOMENTUM (por defecto)

1. Al arrancar, escanea **todo el mercado** de Binance (mercado real, solo lectura).
2. Filtra pares USDT con volumen >20M y subida entre +0.5% y +25% en 24h.
3. Los top 5 entran al radar con su precio de referencia.
4. Cuando un par retrocede -0.5% desde su referencia, **compra**.
5. Coloca SL y TP inmediatamente (OCO o separados).
6. Cada 10 min re-escanea el mercado: si un par deja de subir, sale del radar; entra el siguiente.
7. Se detiene al alcanzar el tiempo límite o el máximo de operaciones.

### Modo DIP

Vigila una lista fija de pares (`--symbols`) y compra cuando caen un % respecto a su precio base.

### 5 filtros de seguridad (antes de cada entrada)

1. **Filtro BTC** — si BTC cae >0.5%, no entra en altcoins.
2. **Volumen** — si el volumen sube durante el retroceso, lo descarta (posible reversal).
3. **SL dinámico** — monedas volátiles obtienen SL más amplio para no liquidarse por ruido.
4. **Cooldown** — 15 min sin re-entrar en un par tras cerrar operación.
5. **Risk-reward** — TP siempre ≥1.5× SL. Si no hay espacio suficiente, no entra.

### Protección de capital

- Nunca invierte más del capital asignado.
- Cada posición tiene SL + TP obligatorios.
- Si el OCO falla, usa TP limit + SL stop-limit separados.
- Si el SL no se puede colocar, vende a mercado (salida de emergencia).
- Valida minNotional y minQty antes de cada orden.
- Descuenta comisiones (0.1% compra + 0.1% venta) en el PnL real.

---

## Pasar a dinero real

1. Crea la API en binance.com → Gestión de API → Crear API (HMAC).
2. Permisos: solo **Lectura + Trading Spot**. Nunca actives Retiros.
3. Restringe la API a tu IP (recomendado).
4. Pon las credenciales en `.env`:
   ```
   BINANCE_API_KEY=tu_key
   BINANCE_API_SECRET=tu_secret
   BINANCE_TESTNET=false
   ```
5. Ejecuta sin `--testnet`:
   ```bash
   python bot.py --capital 30 --max-open 1 --duration 60 --max-trades 5
   ```
6. Empieza con poco capital. Revisa el reporte antes de aumentar.

---

## Testnet (recomendado para probar)

1. Crea credenciales en [testnet.binance.vision](https://testnet.binance.vision) (login con GitHub).
2. Pon `BINANCE_TESTNET=true` en `.env`.
3. Ejecuta:
   ```bash
   python bot.py --testnet -i
   ```
4. El radar usa datos del mercado real; las órdenes van a testnet.
5. Tienes ~10,000 USDT de prueba (se recargan con el reset mensual de la testnet).

---

## Reporte final

Al terminar, el bot imprime en consola:

```
+----------------------------------+---------+
| Concepto                         |   Valor |
+==================================+=========+
| Capital inicial asignado (USDT)  |  100    |
| Operaciones ejecutadas           |     5   |
| Ganadoras                        |     3   |
| Perdedoras                       |     2   |
| Posiciones aun abiertas          |     0   |
| PnL neto realizado (USDT)        |    2.45 |
| Capital final estimado (USDT)    |  102.45 |
+----------------------------------+---------+
```

---

## Estructura

```
bot.py           # Script principal
requirements.txt # Dependencias
.env.example     # Plantilla de credenciales
.env             # Tus credenciales (no subir a git)
state.json       # Estado de sesión (se crea solo, no editar)
```

---

## Advertencia

El trading con criptomonedas implica alto riesgo. **Nunca inviertas dinero que no puedas permitirte perder.** Este bot es una herramienta educativa. Los filtros de seguridad reducen la probabilidad de pérdida, pero no la eliminan. Prueba primero en testnet durante varios días antes de usar dinero real.
