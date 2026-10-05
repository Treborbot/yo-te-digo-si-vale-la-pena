# Crypto Radar Telegram v2

Radar de solo lectura para cripto. **No compra, no vende y no necesita tu seed phrase.**

## Qué hace

### 1) Sigue wallets Solana de traders/KOLs

Preconfigurados:

- Orangie
- Rayan
- Cupsey
- Dior
- Cented
- Gake
- West

Cada vez que Helius detecta un `SWAP`, el bot intenta identificar:

- compra/recepción o venta/envío;
- mint / contrato completo;
- token;
- liquidez;
- market cap / FDV;
- enlace a Solscan;
- enlace a DexScreener.

Las direcciones se pueden editar en `config.json`.

### 2) Top 3 dinámico de Hyperliquid

No deja tres wallets fijas.

Cada `leaderboard_seconds`:

1. Descarga el leaderboard público de Hyperliquid.
2. Ordena por PnL en la ventana configurada (`day`, `week`, `month`, `allTime`).
3. Aplica un mínimo de account value.
4. Toma los Top 3.
5. Si cambia el Top 3, Telegram avisa.
6. Consulta sus fills públicos y avisa de cada operación nueva.

Por defecto usa `month` y exige al menos `$10,000` de account value.

### 3) Top 3 dinámico de Pump.fun

Pump ha cambiado varias veces sus endpoints de leaderboard.

Esta versión soporta un adaptador opcional usando:

`GET https://api.fomoscan.sh/v2/pump/leaderboard/traders`

Para habilitarlo define:

```powershell
$env:FOMOSCAN_API_KEY="TU_KEY"
```

El bot actualiza el Top 3 y añade automáticamente esas wallets al mismo motor Helius.

Si FomoScan cambia su formato, el adaptador está aislado en `fetch_pump_top()`.

### 4) Detector de tokens / Radar Score

El scanner combina candidatos de:

- tokens recientes de Pump.fun;
- perfiles/boosts recientes de DexScreener;
- tokens que las wallets vigiladas acaban de comprar.

Luego puntúa señales observables:

- liquidez;
- volumen 1h;
- relación compras/ventas;
- market cap/FDV;
- edad del par;
- momentum 1h;
- boosts;
- convergencia de varias wallets vigiladas;
- penalizaciones de RugCheck cuando estén disponibles.

El resultado es un **Radar Score de 0–100**.

No significa “probabilidad de subir”. Es un filtro técnico para reducir ruido.

Por defecto avisa desde `70/100`.

## Ejemplo de alerta

```text
🚨 RADAR SCORE 82/100

TOKEN (XYZ)
Solana · pumpswap

CA / Mint:
AbC...pump

Liquidez: $94K
Volumen 1h: $188K
MC/FDV: $540K
1h: 620 compras / 318 ventas
Cambio 1h: +37%
KOLs: Orangie, Gake

Señales: liquidez ≥$75K, volumen alto, compras dominan,
capitalización temprana, 2 wallets vigiladas comprando
```

El contrato/mint se muestra completo para copiarlo y verificarlo.

## Kimchi

`unverified_people.json` contiene una dirección candidata relacionada con una página de
Pump.fun que enlaza a `@kimchi1x`.

**Está desactivada deliberadamente.**

La página de la moneda enlaza a Kimchi, pero el perfil de la dirección aparece bajo otro
nombre. Eso no basta para tratarla como su wallet principal de trading.

No la pases a `config.json` hasta verificarla por una segunda fuente sólida.

## Instalación en Windows

### 1. Crear el bot

En Telegram:

1. busca `@BotFather`;
2. envía `/newbot`;
3. guarda el token.

Nunca publiques el token.

### 2. Python

PowerShell:

```powershell
cd crypto_radar_telegram_v2

python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Variables

```powershell
$env:TELEGRAM_BOT_TOKEN="TOKEN_DE_BOTFATHER"
$env:HELIUS_API_KEY="TU_KEY_DE_HELIUS"
```

Opcional para Pump Top 3:

```powershell
$env:FOMOSCAN_API_KEY="TU_KEY_DE_FOMOSCAN"
```

### 4. Ejecutar

```powershell
python radar.py
```

Después abre tu bot y envía:

```text
/start
```

## Comandos Telegram

- `/start`
- `/status`
- `/kols`
- `/top3`
- `/test`
- `/stop`

## Ajustes útiles

En `config.json`:

```json
"radar_score_threshold": 70
```

Subir a 80 = menos alertas y filtros más exigentes.

```json
"hyperliquid_window": "month"
```

Opciones: `day`, `week`, `month`, `allTime`.

```json
"hyperliquid_min_notional_usd": 0
```

Si el Top 3 genera demasiado ruido, por ejemplo:

```json
"hyperliquid_min_notional_usd": 25000
```

y sólo recibirás fills con nocional aproximado de al menos $25K.

## Fuentes del bot

- Solana/KOL swaps: Helius Enhanced Transactions.
- Mercado: DexScreener.
- Riesgo Solana: RugCheck.
- Nuevos Pump tokens: frontend de Pump (puede cambiar).
- Hyperliquid Top: stats-data de Hyperliquid.
- Hyperliquid fills: `/info`, `userFillsByTime`.
- Pump Top 3: FomoScan opcional.

## Seguridad

Este proyecto:

- NO pide seed phrase;
- NO pide private key;
- NO firma transacciones;
- NO tiene función de autobuy;
- sólo observa información pública y envía alertas.

Si en el futuro quieres agregar ejecución, hazlo como un módulo separado y con una
wallet de trading aislada. No mezcles una wallet principal con bots de memecoins.

## Nota importante

Memecoins y perps pueden perder gran parte o todo su valor muy rápido.
Leaderboard, win rate y PnL pasado no garantizan resultados futuros, y copiar una wallet
unos segundos después produce una entrada distinta por slippage, liquidez y latencia.


## Despliegue de este repositorio en Railway

- Root Directory: `/crypto-radar`.
- Inicio: `python -u radar.py`. El Dockerfile usa Python 3.12.
- Ejecutar como un worker continuo con una sola réplica, sin cron, suspensión ni healthcheck HTTP. No necesita dominio público.
- Definir `TELEGRAM_BOT_TOKEN` y `HELIUS_API_KEY` directamente en las variables del servicio. `.env.example` está vacío deliberadamente.
- Las claves, `.env` y `state.json` no se versionan. Los errores redactan las claves antes de imprimirlos.
- El arranque comprueba `getMe`; los logs confirman el primer `getUpdates` correcto y la primera consulta Helius correcta.
- Al iniciar Telegram, enviar `/start` y después `/test`.
- Sin un volumen persistente, un nuevo despliegue puede perder las suscripciones y el historial local; volver a enviar `/start` si ocurre. Para persistencia, montar un volumen en `/data` y definir `RADAR_STATE=/data/state.json`.
- `FOMOSCAN_API_KEY` es opcional: sin ella, el Top 3 de Pump.fun queda desactivado.
