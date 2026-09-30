#!/usr/bin/env python3
"""
CRYPTO RADAR TELEGRAM v2

Solo lectura / alertas:
- Wallets Solana públicas (KOLs)
- Top 3 dinámico de Hyperliquid
- Top 3 Pump.fun vía FomoScan (opcional, requiere API key)
- Scanner de tokens Solana/Pump.fun/DexScreener
- Contrato/mint completo en cada alerta
- RugCheck como capa de riesgo cuando esté disponible

NO guarda seed phrases, claves privadas ni ejecuta compras.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("RADAR_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.getenv("RADAR_STATE", BASE_DIR / "state.json"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "").strip()
FOMOSCAN_API_KEY = os.getenv("FOMOSCAN_API_KEY", "").strip()
PUMP_JWT = os.getenv("PUMP_JWT", "").strip()

TG_API = f"https://api.telegram.org/bot{TG_TOKEN}" if TG_TOKEN else ""
TELEGRAM_POLL_READY = False
HELIUS_READY = False

session = requests.Session()
session.headers.update({
    "User-Agent": "CryptoRadarTelegram/2.0",
    "Accept": "application/json,text/plain,*/*",
})


# --------------------------
# Utilidades
# --------------------------

def redact_error(error: Any) -> str:
    """Keep API credentials out of logs, including URLs in HTTP errors."""
    message = str(error)
    for secret in sorted(
        (TG_TOKEN, HELIUS_API_KEY, FOMOSCAN_API_KEY, PUMP_JWT),
        key=len, reverse=True,
    ):
        if secret:
            message = message.replace(secret, "[REDACTED]")
            message = message.replace(quote(secret, safe=""), "[REDACTED]")
    message = re.sub(
        r"(https://api\.telegram\.org/bot)[^/\s\"']+",
        r"\1[REDACTED]", message,
    )
    return re.sub(
        r"(?i)([?&](?:api[-_]?key|token)=)[^&\s\"']+",
        r"\1[REDACTED]", message,
    )


def now_ts() -> int:
    return int(time.time())


def utc_text(ts: Optional[int]) -> str:
    if not ts:
        return "N/D"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def money(v: Any) -> str:
    try:
        n = float(v)
    except Exception:
        return "N/D"
    a = abs(n)
    if a >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if a >= 1_000:
        return f"${n/1_000:.1f}K"
    return f"${n:,.2f}"


def num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] No se pudo leer {path}: {redact_error(exc)}")
    return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_config() -> Dict[str, Any]:
    cfg = load_json(CONFIG_PATH, {})
    cfg.setdefault("poll_seconds", 30)
    cfg.setdefault("scanner_seconds", 120)
    cfg.setdefault("leaderboard_seconds", 900)
    cfg.setdefault("radar_score_threshold", 70)
    cfg.setdefault("opportunity_alert_cooldown_seconds", 10800)
    cfg.setdefault("kol_recent_buy_window_seconds", 3600)
    cfg.setdefault("hyperliquid_window", "month")
    cfg.setdefault("hyperliquid_top_n", 3)
    cfg.setdefault("hyperliquid_min_account_value", 10000)
    cfg.setdefault("hyperliquid_min_notional_usd", 0)
    cfg.setdefault("pump_window", "7d")
    cfg.setdefault("pump_top_n", 3)
    cfg.setdefault("wallets", [])
    cfg.setdefault("scanner", {})
    return cfg


def load_state() -> Dict[str, Any]:
    st = load_json(STATE_PATH, {})
    st.setdefault("subscribers", [])
    st.setdefault("telegram_offset", 0)
    st.setdefault("wallet_last_sig", {})
    st.setdefault("hyper_top", [])
    st.setdefault("hyper_last_time", {})
    st.setdefault("hyper_seen_fills", [])
    st.setdefault("pump_top", [])
    st.setdefault("pump_dynamic_wallets", [])
    st.setdefault("candidate_tokens", {})
    st.setdefault("token_alerted_at", {})
    st.setdefault("kol_recent_buys", {})
    st.setdefault("initialized_wallets", [])
    st.setdefault("initialized_hyper", [])
    return st


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------
# Telegram
# --------------------------

def tg_request(method: str, payload: Optional[Dict[str, Any]] = None, timeout: int = 20) -> Dict[str, Any]:
    if not TG_TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    r = session.post(f"{TG_API}/{method}", json=payload or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(str(data))
    return data


def send(chat_id: int, text: str) -> None:
    for i in range(0, len(text), 3900):
        tg_request("sendMessage", {
            "chat_id": chat_id,
            "text": text[i:i+3900],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })


def broadcast(st: Dict[str, Any], text: str) -> None:
    for chat_id in list(st.get("subscribers", [])):
        try:
            send(int(chat_id), text)
        except Exception as exc:
            print(f"[WARN] Telegram envío: {redact_error(exc)}")


def status_text(cfg: Dict[str, Any], st: Dict[str, Any]) -> str:
    wallets = [w for w in effective_wallets(cfg, st) if w.get("enabled", True)]
    fixed = [w for w in wallets if w.get("source") != "pump-top3"]
    dyn = [w for w in wallets if w.get("source") == "pump-top3"]

    htop = st.get("hyper_top", [])
    htxt = "\n".join(
        f"{i+1}. <code>{html.escape(x.get('address',''))}</code> "
        f"{html.escape(x.get('name') or '')}"
        for i, x in enumerate(htop[:3])
    ) or "Aún no cargado"

    return (
        "📡 <b>CRYPTO RADAR v2</b>\n\n"
        f"👀 KOL wallets fijas: <b>{len(fixed)}</b>\n"
        f"🎯 Pump Top dinámico: <b>{len(dyn)}</b>\n"
        f"🔎 Radar score mínimo: <b>{cfg.get('radar_score_threshold')}</b>/100\n"
        f"⏱ Revisión de wallets: {cfg.get('poll_seconds')}s\n\n"
        "<b>Hyperliquid Top 3 actual</b>\n"
        f"{htxt}\n\n"
        "El bot informa actividad pública. No ejecuta órdenes."
    )


def handle_command(chat_id: int, text: str, cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    cmd = (text or "").strip().split()[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]

    if cmd == "/start":
        if chat_id not in st["subscribers"]:
            st["subscribers"].append(chat_id)
            save_json(STATE_PATH, st)
        send(chat_id,
             "✅ <b>Radar activado</b>\n\n"
             "/status — estado y Top 3\n"
             "/kols — wallets vigiladas\n"
             "/top3 — Top 3 Hyperliquid/Pump\n"
             "/test — prueba de Telegram\n"
             "/stop — desactivar alertas\n\n"
             "⚠️ Un Radar Score alto es una combinación de señales, no una garantía de rentabilidad.")

    elif cmd == "/stop":
        if chat_id in st["subscribers"]:
            st["subscribers"].remove(chat_id)
            save_json(STATE_PATH, st)
        send(chat_id, "🔕 Alertas desactivadas.")

    elif cmd == "/status":
        send(chat_id, status_text(cfg, st))

    elif cmd == "/kols":
        rows = []
        for w in effective_wallets(cfg, st):
            if not w.get("enabled", True):
                continue
            rows.append(
                f"• <b>{html.escape(w.get('label','Wallet'))}</b> "
                f"[{html.escape(w.get('source','watchlist'))}]\n"
                f"  <code>{html.escape(w.get('address',''))}</code>"
            )
        send(chat_id, "👀 <b>Wallets vigiladas</b>\n\n" + ("\n".join(rows) or "Ninguna"))

    elif cmd == "/top3":
        h = st.get("hyper_top", [])[:3]
        p = st.get("pump_top", [])[:3]
        htxt = "\n".join(
            f"{i+1}. {html.escape(x.get('name') or 'wallet')} "
            f"<code>{html.escape(x.get('address',''))}</code> · PnL {money(x.get('pnl'))}"
            for i, x in enumerate(h)
        ) or "No disponible aún"
        ptxt = "\n".join(
            f"{i+1}. {html.escape(x.get('name') or 'wallet')} "
            f"<code>{html.escape(x.get('address',''))}</code> · PnL {money(x.get('pnl'))}"
            for i, x in enumerate(p)
        ) or "No disponible (FOMOSCAN_API_KEY opcional)"
        send(chat_id, f"🏆 <b>TOP 3</b>\n\n<b>Hyperliquid</b>\n{htxt}\n\n<b>Pump.fun</b>\n{ptxt}")

    elif cmd == "/test":
        send(chat_id, "🧪 Telegram conectado correctamente.")

    else:
        send(chat_id, "Comandos: /start /status /kols /top3 /test /stop")


def poll_telegram(cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    global TELEGRAM_POLL_READY
    try:
        data = tg_request("getUpdates", {
            "offset": st.get("telegram_offset", 0),
            "timeout": 8,
            "allowed_updates": ["message"],
        }, timeout=12)
        if not TELEGRAM_POLL_READY:
            print("[OK] Telegram polling activo: getUpdates respondió correctamente.")
            TELEGRAM_POLL_READY = True
        changed = False
        for u in data.get("result", []):
            st["telegram_offset"] = max(st["telegram_offset"], int(u["update_id"]) + 1)
            changed = True
            msg = u.get("message") or {}
            chat_id = (msg.get("chat") or {}).get("id")
            txt = msg.get("text", "")
            if chat_id and txt.startswith("/"):
                handle_command(int(chat_id), txt, cfg, st)
                print("[OK] Comando de Telegram atendido.")
        if changed:
            save_json(STATE_PATH, st)
    except Exception as exc:
        print(f"[WARN] Telegram polling: {redact_error(exc)}")


# --------------------------
# Datos de mercado
# --------------------------

DEX_BASE = "https://api.dexscreener.com"
PUMP_BASE = "https://frontend-api-v3.pump.fun"
RUG_BASE = "https://api.rugcheck.xyz/v1"


def dex_pairs(mint: str) -> List[Dict[str, Any]]:
    try:
        r = session.get(f"{DEX_BASE}/tokens/v1/solana/{mint}", timeout=15)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        print(f"[WARN] DexScreener {mint[:8]}: {redact_error(exc)}")
        return []


def best_pair(mint: str) -> Optional[Dict[str, Any]]:
    pairs = dex_pairs(mint)
    if not pairs:
        return None
    # Prioriza liquidez; evita pares sin USD.
    return max(pairs, key=lambda p: num((p.get("liquidity") or {}).get("usd")))


def dex_latest_candidates() -> List[str]:
    out: List[str] = []
    for endpoint in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        try:
            r = session.get(DEX_BASE + endpoint, timeout=15)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                for x in data:
                    if str(x.get("chainId", "")).lower() == "solana":
                        a = str(x.get("tokenAddress") or "").strip()
                        if a:
                            out.append(a)
        except Exception as exc:
            print(f"[WARN] DEX candidates {endpoint}: {redact_error(exc)}")
    return list(dict.fromkeys(out))


def pump_headers() -> Dict[str, str]:
    h = {"Accept": "application/json"}
    if PUMP_JWT:
        h["Authorization"] = f"Bearer {PUMP_JWT}"
    return h


def pump_new_candidates(limit: int = 40) -> List[str]:
    params = {
        "limit": limit,
        "offset": 0,
        "sort": "created_timestamp",
        "order": "DESC",
        "includeNsfw": "false",
    }
    try:
        r = session.get(f"{PUMP_BASE}/coins", params=params, headers=pump_headers(), timeout=20)
        if r.status_code in (401, 403):
            print("[WARN] Pump /coins exige sesión/JWT en este momento. Scanner DEX sigue activo.")
            return []
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            data = data.get("data") or data.get("coins") or []
        if not isinstance(data, list):
            return []
        return [str(x.get("mint")) for x in data if x.get("mint")]
    except Exception as exc:
        print(f"[WARN] Pump new coins: {redact_error(exc)}")
        return []


def rug_summary(mint: str) -> Dict[str, Any]:
    try:
        r = session.get(f"{RUG_BASE}/tokens/{mint}/report/summary", timeout=15)
        if not r.ok:
            return {}
        data = r.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def extract_rug_penalty(rug: Dict[str, Any]) -> Tuple[int, str]:
    if not rug:
        return 0, "sin dato"

    penalty = 0
    notes: List[str] = []

    # RugCheck cambia campos con el tiempo; se leen de forma defensiva.
    score = rug.get("score_normalised", rug.get("scoreNormalized", rug.get("score")))
    if score is not None:
        s = num(score)
        # Algunos formatos: 0=mejor; otros 100=mejor.
        # No se usa para penalizar si la escala no puede inferirse bien.
        notes.append(f"score={s:g}")

    risks = rug.get("risks") or rug.get("risk") or []
    if isinstance(risks, dict):
        risks = list(risks.values())
    if not isinstance(risks, list):
        risks = []

    severe_words = ("mint authority", "freeze authority", "honeypot", "rug", "mutable")
    for risk in risks:
        text = json.dumps(risk, ensure_ascii=False).lower()
        level = str((risk or {}).get("level", "")).lower() if isinstance(risk, dict) else ""
        if any(w in text for w in severe_words):
            penalty += 8
        if level in {"danger", "critical", "high"}:
            penalty += 8

    penalty = int(clamp(penalty, 0, 35))
    if penalty:
        notes.append(f"-{penalty} riesgo")
    return penalty, ", ".join(notes) if notes else "revisado"


# --------------------------
# Radar Score
# --------------------------

@dataclass
class TokenSignal:
    mint: str
    score: int
    pair: Dict[str, Any]
    rug: Dict[str, Any]
    kol_labels: List[str]
    reasons: List[str]


def pair_metric(pair: Dict[str, Any], group: str, window: str, field: Optional[str] = None) -> float:
    obj = pair.get(group) or {}
    win = obj.get(window) or {}
    if field is None:
        return num(win)
    if isinstance(win, dict):
        return num(win.get(field))
    return 0.0


def token_score(mint: str, pair: Dict[str, Any], rug: Dict[str, Any],
                kol_labels: List[str]) -> TokenSignal:
    score = 0
    reasons: List[str] = []

    liq = num((pair.get("liquidity") or {}).get("usd"))
    mcap = num(pair.get("marketCap") or pair.get("fdv"))
    vol_h1 = num((pair.get("volume") or {}).get("h1"))
    vol_h24 = num((pair.get("volume") or {}).get("h24"))
    pc_h1 = num((pair.get("priceChange") or {}).get("h1"))

    h1 = (pair.get("txns") or {}).get("h1") or {}
    buys = int(num(h1.get("buys")))
    sells = int(num(h1.get("sells")))
    txs = buys + sells

    created_ms = int(num(pair.get("pairCreatedAt")))
    age_min = None
    if created_ms:
        age_min = max(0.0, (time.time() - created_ms / 1000.0) / 60.0)

    # Liquidez: 0..20
    if liq >= 150_000:
        score += 20; reasons.append("liquidez ≥$150K")
    elif liq >= 75_000:
        score += 16; reasons.append("liquidez ≥$75K")
    elif liq >= 30_000:
        score += 12; reasons.append("liquidez ≥$30K")
    elif liq >= 12_000:
        score += 6; reasons.append("liquidez ≥$12K")
    else:
        score -= 12; reasons.append("liquidez baja")

    # Actividad / volumen: 0..15
    if liq > 0:
        ratio = vol_h1 / liq
        if ratio >= 2.0:
            score += 15; reasons.append("volumen 1h muy alto")
        elif ratio >= 1.0:
            score += 12; reasons.append("volumen 1h alto")
        elif ratio >= 0.4:
            score += 7; reasons.append("volumen 1h saludable")

    # Compras/ventas y actividad: 0..15
    if txs >= 150 and buys > sells:
        score += 15; reasons.append("fuerte actividad compradora")
    elif txs >= 60 and buys >= sells:
        score += 10; reasons.append("compras dominan")
    elif txs >= 20:
        score += 4

    # Rango de market cap: 0..10 (no significa "barato"; solo fase temprana con mercado)
    if 80_000 <= mcap <= 2_500_000:
        score += 10; reasons.append("capitalización temprana")
    elif 25_000 <= mcap < 80_000:
        score += 5
    elif 2_500_000 < mcap <= 10_000_000:
        score += 5

    # Edad: 0..10
    if age_min is not None:
        if 5 <= age_min <= 45:
            score += 10; reasons.append("token reciente")
        elif 45 < age_min <= 180:
            score += 6
        elif age_min < 5:
            score -= 5; reasons.append("demasiado nuevo")

    # Momentum moderado: 0..10; evita premiar velas absurdas.
    if 5 <= pc_h1 <= 80:
        score += 10; reasons.append("momentum 1h")
    elif 0 < pc_h1 < 5:
        score += 4
    elif pc_h1 > 250:
        score -= 8; reasons.append("movimiento parabólico")

    boosts = int(num((pair.get("boosts") or {}).get("active")))
    if boosts > 0:
        score += min(5, boosts); reasons.append("boost activo")

    # Convergencia KOL: máximo +25
    k = len(set(kol_labels))
    if k >= 3:
        score += 25; reasons.append(f"{k} wallets vigiladas comprando")
    elif k == 2:
        score += 20; reasons.append("2 wallets vigiladas comprando")
    elif k == 1:
        score += 12; reasons.append(f"compra de {kol_labels[0]}")

    penalty, rug_note = extract_rug_penalty(rug)
    score -= penalty
    if penalty:
        reasons.append(f"riesgo RugCheck -{penalty}")

    return TokenSignal(
        mint=mint,
        score=int(clamp(score, 0, 100)),
        pair=pair,
        rug=rug,
        kol_labels=sorted(set(kol_labels)),
        reasons=reasons,
    )


def signal_alert(sig: TokenSignal) -> str:
    p = sig.pair
    liq = num((p.get("liquidity") or {}).get("usd"))
    mcap = num(p.get("marketCap") or p.get("fdv"))
    vol1 = num((p.get("volume") or {}).get("h1"))
    pc1 = num((p.get("priceChange") or {}).get("h1"))
    tx = (p.get("txns") or {}).get("h1") or {}
    buys, sells = int(num(tx.get("buys"))), int(num(tx.get("sells")))
    name = ((p.get("baseToken") or {}).get("name") or "Token")
    sym = ((p.get("baseToken") or {}).get("symbol") or "?")
    dex = p.get("dexId") or "DEX"
    created_ms = int(num(p.get("pairCreatedAt")))
    age = ""
    if created_ms:
        mins = max(0, int((time.time() - created_ms/1000) / 60))
        age = f"{mins} min"

    _, rug_note = extract_rug_penalty(sig.rug)
    reasons = ", ".join(sig.reasons[:6])

    return (
        f"🚨 <b>RADAR SCORE {sig.score}/100</b>\n\n"
        f"🪙 <b>{html.escape(str(name))} ({html.escape(str(sym))})</b>\n"
        f"⛓ Solana · {html.escape(str(dex))}\n"
        f"📄 CA / Mint:\n<code>{html.escape(sig.mint)}</code>\n\n"
        f"💧 Liquidez: <b>{money(liq)}</b>\n"
        f"📊 Volumen 1h: <b>{money(vol1)}</b>\n"
        f"💰 MC/FDV: <b>{money(mcap)}</b>\n"
        f"🔄 1h: {buys} compras / {sells} ventas\n"
        f"📈 Cambio 1h: {pc1:+.1f}%\n"
        f"⏳ Edad del par: {html.escape(age or 'N/D')}\n"
        f"🛡 RugCheck: {html.escape(rug_note)}\n"
        f"👀 KOLs: {html.escape(', '.join(sig.kol_labels) or 'ninguno')}\n\n"
        f"🧠 Señales: {html.escape(reasons)}\n\n"
        f"🔎 DexScreener: https://dexscreener.com/solana/{html.escape(sig.mint)}\n"
        f"🟢 Pump: https://pump.fun/coin/{html.escape(sig.mint)}\n\n"
        "⚠️ El puntaje mide señales observables; no predice que el token vaya a subir."
    )


# --------------------------
# Helius / KOL tracker
# --------------------------

def effective_wallets(cfg: Dict[str, Any], st: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = [dict(x) for x in cfg.get("wallets", [])]
    out.extend(dict(x) for x in st.get("pump_dynamic_wallets", []))

    seen = set()
    result = []
    for w in out:
        a = str(w.get("address") or "").strip()
        if not a or a in seen:
            continue
        seen.add(a)
        result.append(w)
    return result


def helius_history(address: str, limit: int = 20) -> List[Dict[str, Any]]:
    global HELIUS_READY
    if not HELIUS_API_KEY:
        return []
    url = f"https://api-mainnet.helius-rpc.com/v0/addresses/{address}/transactions"
    params = {
        "api-key": HELIUS_API_KEY,
        "limit": limit,
        "type": "SWAP",
        "commitment": "confirmed",
    }
    r = session.get(url, params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Helius devolvió una respuesta inesperada.")
    if not HELIUS_READY:
        print("[OK] Helius conectado: consulta de transacciones correcta.")
        HELIUS_READY = True
    return data


def wallet_token_flows(tx: Dict[str, Any], address: str) -> List[Tuple[str, float]]:
    flows: Dict[str, float] = {}
    for t in tx.get("tokenTransfers") or []:
        mint = str(t.get("mint") or "")
        if not mint:
            continue
        amt = num(t.get("tokenAmount"))
        fr = str(t.get("fromUserAccount") or "")
        to = str(t.get("toUserAccount") or "")
        if to == address:
            flows[mint] = flows.get(mint, 0.0) + amt
        if fr == address:
            flows[mint] = flows.get(mint, 0.0) - amt
    return [(m, a) for m, a in flows.items() if abs(a) > 1e-15]


def choose_primary_flow(flows: List[Tuple[str, float]]) -> Optional[Tuple[str, float]]:
    if not flows:
        return None
    # Ignora mints de stables conocidos cuando haya otro token.
    stable_mints = {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
        "Es9vMFrzaCERmJfrF4H2FYDkdxQivL6GxQYQy8H1KqG",   # USDT historical
    }
    nonstable = [x for x in flows if x[0] not in stable_mints]
    arr = nonstable or flows
    # Token recibido suele ser el comprado; enviado suele ser el vendido.
    positive = [x for x in arr if x[1] > 0]
    if positive:
        return max(positive, key=lambda x: abs(x[1]))
    return max(arr, key=lambda x: abs(x[1]))


def add_recent_kol_buy(st: Dict[str, Any], mint: str, label: str, ts: int) -> None:
    arr = st.setdefault("kol_recent_buys", {}).setdefault(mint, [])
    arr.append({"label": label, "time": ts})
    cutoff = now_ts() - 6 * 3600
    st["kol_recent_buys"][mint] = [x for x in arr if int(x.get("time", 0)) >= cutoff]


def recent_kols_for_token(st: Dict[str, Any], cfg: Dict[str, Any], mint: str) -> List[str]:
    cutoff = now_ts() - int(cfg.get("kol_recent_buy_window_seconds", 3600))
    return list(dict.fromkeys(
        x.get("label", "")
        for x in st.get("kol_recent_buys", {}).get(mint, [])
        if int(x.get("time", 0)) >= cutoff and x.get("label")
    ))


def kol_trade_alert(label: str, address: str, tx: Dict[str, Any],
                    mint: str, delta: float, pair: Optional[Dict[str, Any]]) -> str:
    side = "COMPRA / RECIBE" if delta > 0 else "VENTA / ENVÍA"
    sig = tx.get("signature") or ""
    timestamp = int(tx.get("timestamp") or 0)
    extra = ""
    if pair:
        name = ((pair.get("baseToken") or {}).get("name") or "Token")
        sym = ((pair.get("baseToken") or {}).get("symbol") or "?")
        liq = num((pair.get("liquidity") or {}).get("usd"))
        mc = num(pair.get("marketCap") or pair.get("fdv"))
        extra = (
            f"\n🪙 {html.escape(str(name))} ({html.escape(str(sym))})"
            f"\n💧 Liquidez: {money(liq)}"
            f"\n💰 MC/FDV: {money(mc)}"
        )

    return (
        f"👀 <b>KOL WALLET — {html.escape(side)}</b>\n\n"
        f"👤 <b>{html.escape(label)}</b>\n"
        f"👛 <code>{html.escape(address)}</code>\n"
        f"🕒 {html.escape(utc_text(timestamp))}\n"
        f"📄 CA / Mint:\n<code>{html.escape(mint)}</code>\n"
        f"🔢 Cambio de tokens: {delta:+.8g}"
        f"{extra}\n\n"
        f"🔎 https://solscan.io/tx/{html.escape(sig)}\n"
        f"📊 https://dexscreener.com/solana/{html.escape(mint)}"
    )


def check_wallets(cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    if not HELIUS_API_KEY:
        return

    initialized = set(st.get("initialized_wallets", []))

    for w in effective_wallets(cfg, st):
        if not w.get("enabled", True):
            continue
        address = str(w.get("address") or "")
        label = str(w.get("label") or address[:8])
        try:
            txs = helius_history(address, limit=15)
            if not txs:
                continue

            newest_sig = txs[0].get("signature")
            previous = st["wallet_last_sig"].get(address)

            if address not in initialized or not previous:
                st["wallet_last_sig"][address] = newest_sig
                initialized.add(address)
                continue

            if newest_sig == previous:
                continue

            fresh = []
            for tx in txs:
                if tx.get("signature") == previous:
                    break
                fresh.append(tx)

            for tx in reversed(fresh):
                flows = wallet_token_flows(tx, address)
                primary = choose_primary_flow(flows)
                if not primary:
                    continue
                mint, delta = primary
                pair = best_pair(mint)

                if delta > 0:
                    add_recent_kol_buy(st, mint, label, int(tx.get("timestamp") or now_ts()))

                broadcast(st, kol_trade_alert(label, address, tx, mint, delta, pair))

            st["wallet_last_sig"][address] = newest_sig

        except Exception as exc:
            print(f"[WARN] Wallet {label}: {redact_error(exc)}")

    st["initialized_wallets"] = sorted(initialized)
    save_json(STATE_PATH, st)


# --------------------------
# Hyperliquid: Top 3 dinámico
# --------------------------

HL_LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_INFO = "https://api.hyperliquid.xyz/info"


def normalize_hl_window(w: str) -> str:
    aliases = {
        "24h": "day", "1d": "day", "day": "day",
        "7d": "week", "week": "week",
        "30d": "month", "month": "month",
        "all": "allTime", "alltime": "allTime", "allTime": "allTime",
    }
    return aliases.get(w, w)


def fetch_hyper_top(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    r = session.get(HL_LEADERBOARD, timeout=60)
    r.raise_for_status()
    data = r.json()
    rows = data.get("leaderboardRows", []) if isinstance(data, dict) else []
    window = normalize_hl_window(str(cfg.get("hyperliquid_window", "month")))
    min_eq = num(cfg.get("hyperliquid_min_account_value", 10000))
    topn = int(cfg.get("hyperliquid_top_n", 3))

    parsed = []
    for row in rows:
        addr = str(row.get("ethAddress") or "")
        eq = num(row.get("accountValue"))
        if not addr or eq < min_eq:
            continue
        perfs = {}
        for item in row.get("windowPerformances") or []:
            if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict):
                perfs[str(item[0])] = item[1]
        perf = perfs.get(window) or {}
        pnl = num(perf.get("pnl"), float("-inf"))
        roi = num(perf.get("roi"))
        if not math.isfinite(pnl):
            continue
        parsed.append({
            "address": addr,
            "name": row.get("displayName") or "",
            "account_value": eq,
            "pnl": pnl,
            "roi": roi,
        })

    parsed.sort(key=lambda x: x["pnl"], reverse=True)
    return parsed[:topn]


def update_hyper_top(cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    try:
        new = fetch_hyper_top(cfg)
        old_addr = [x.get("address") for x in st.get("hyper_top", [])]
        new_addr = [x.get("address") for x in new]
        st["hyper_top"] = new
        save_json(STATE_PATH, st)

        if old_addr and new_addr != old_addr:
            txt = "\n".join(
                f"{i+1}. {html.escape(x.get('name') or 'wallet')} "
                f"<code>{html.escape(x['address'])}</code> · PnL {money(x['pnl'])}"
                for i, x in enumerate(new)
            )
            broadcast(st, "🏆 <b>CAMBIÓ EL TOP DE HYPERLIQUID</b>\n\n" + txt)
    except Exception as exc:
        print(f"[WARN] Hyper leaderboard: {redact_error(exc)}")


def hyper_user_fills(address: str, start_ms: int) -> List[Dict[str, Any]]:
    r = session.post(HL_INFO, json={
        "type": "userFillsByTime",
        "user": address,
        "startTime": start_ms,
        "aggregateByTime": True,
    }, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def hyper_fill_id(address: str, f: Dict[str, Any]) -> str:
    return "|".join(map(str, [
        address, f.get("hash"), f.get("oid"), f.get("tid"),
        f.get("time"), f.get("coin"), f.get("px"), f.get("sz"), f.get("dir")
    ]))


def hyper_alert(rank: int, trader: Dict[str, Any], f: Dict[str, Any]) -> str:
    px = num(f.get("px"))
    sz = num(f.get("sz"))
    notion = abs(px * sz)
    pnl = num(f.get("closedPnl"))
    return (
        f"⚡ <b>HYPERLIQUID TOP #{rank} — NUEVO FILL</b>\n\n"
        f"👤 {html.escape(trader.get('name') or 'wallet')}\n"
        f"👛 <code>{html.escape(trader['address'])}</code>\n"
        f"🪙 <b>{html.escape(str(f.get('coin') or '?'))}</b>\n"
        f"📍 {html.escape(str(f.get('dir') or 'Trade'))}\n"
        f"💵 Precio: {px:g}\n"
        f"📦 Tamaño: {sz:g}\n"
        f"💲 Nocional aprox.: {money(notion)}\n"
        f"✅ PnL cerrado del fill: {money(pnl)}\n"
        f"🕒 {html.escape(utc_text(int(num(f.get('time'))) // 1000))}\n\n"
        f"🔎 Hash: <code>{html.escape(str(f.get('hash') or ''))}</code>"
    )


def check_hyper_fills(cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    top = st.get("hyper_top", [])
    if not top:
        return

    init = set(st.get("initialized_hyper", []))
    seen_list = st.get("hyper_seen_fills", [])
    seen = set(seen_list)
    min_notional = num(cfg.get("hyperliquid_min_notional_usd", 0))

    for rank, trader in enumerate(top, start=1):
        address = trader["address"]
        last = int(st.get("hyper_last_time", {}).get(address, 0))

        # Primera vez: sólo inicializa a "ahora - 2 min", sin backfill.
        if address not in init or last <= 0:
            st["hyper_last_time"][address] = int(time.time() * 1000)
            init.add(address)
            continue

        try:
            fills = hyper_user_fills(address, max(0, last - 1000))
            max_time = last
            for f in sorted(fills, key=lambda x: int(num(x.get("time")))):
                fid = hyper_fill_id(address, f)
                t = int(num(f.get("time")))
                max_time = max(max_time, t)
                if fid in seen or t < last:
                    continue

                notional = abs(num(f.get("px")) * num(f.get("sz")))
                if notional >= min_notional:
                    broadcast(st, hyper_alert(rank, trader, f))

                seen.add(fid)

            st["hyper_last_time"][address] = max_time
        except Exception as exc:
            print(f"[WARN] Hyper fills {address[:10]}: {redact_error(exc)}")

    st["initialized_hyper"] = sorted(init)
    st["hyper_seen_fills"] = list(seen)[-5000:]
    save_json(STATE_PATH, st)


# --------------------------
# Pump.fun Top 3 dinámico
# --------------------------

FOMOSCAN_PUMP_LEADERBOARD = "https://api.fomoscan.sh/v2/pump/leaderboard/traders"


def extract_rows_flexible(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("data", "traders", "rows", "leaderboard", "results", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
            if isinstance(v, dict):
                nested = extract_rows_flexible(v)
                if nested:
                    return nested
    return []


def row_wallet(row: Dict[str, Any]) -> str:
    keys = ("wallet", "address", "publicKey", "public_key", "walletAddress", "wallet_address")
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and 30 <= len(v) <= 60:
            return v
    user = row.get("user")
    if isinstance(user, dict):
        return row_wallet(user)
    return ""


def row_pnl(row: Dict[str, Any]) -> float:
    keys = ("pnl", "profit", "realizedPnl", "realized_pnl", "realisedPnl",
            "realised_pnl", "profitUsd", "profit_usd")
    for k in keys:
        if k in row:
            return num(row.get(k))
    stats = row.get("stats") or row.get("performance") or {}
    if isinstance(stats, dict):
        return row_pnl(stats)
    return 0.0


def row_name(row: Dict[str, Any]) -> str:
    for k in ("username", "name", "displayName", "handle", "profileName"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    user = row.get("user")
    if isinstance(user, dict):
        return row_name(user)
    return ""


def fetch_pump_top(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not FOMOSCAN_API_KEY:
        return []
    headers = {"Authorization": f"Bearer {FOMOSCAN_API_KEY}", "Accept": "application/json"}
    params = {
        "window": cfg.get("pump_window", "7d"),
        "limit": max(3, int(cfg.get("pump_top_n", 3))),
    }
    r = session.get(FOMOSCAN_PUMP_LEADERBOARD, headers=headers, params=params, timeout=25)
    r.raise_for_status()
    rows = extract_rows_flexible(r.json())

    out = []
    for row in rows:
        addr = row_wallet(row)
        if not addr:
            continue
        out.append({
            "address": addr,
            "name": row_name(row) or addr[:8],
            "pnl": row_pnl(row),
        })
    if not out:
        return []
    out.sort(key=lambda x: x["pnl"], reverse=True)
    return out[:int(cfg.get("pump_top_n", 3))]


def update_pump_top(cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    if not FOMOSCAN_API_KEY:
        return
    try:
        new = fetch_pump_top(cfg)
        if not new:
            return
        old_addr = [x.get("address") for x in st.get("pump_top", [])]
        new_addr = [x.get("address") for x in new]
        st["pump_top"] = new
        st["pump_dynamic_wallets"] = [
            {
                "label": f"Pump Top #{i+1} — {x.get('name')}",
                "address": x["address"],
                "enabled": True,
                "source": "pump-top3",
            }
            for i, x in enumerate(new)
        ]
        save_json(STATE_PATH, st)

        if old_addr and new_addr != old_addr:
            txt = "\n".join(
                f"{i+1}. {html.escape(x.get('name') or 'wallet')} "
                f"<code>{html.escape(x['address'])}</code> · PnL {money(x['pnl'])}"
                for i, x in enumerate(new)
            )
            broadcast(st, "🎯 <b>CAMBIÓ EL TOP 3 DE PUMP.FUN</b>\n\n" + txt)
    except Exception as exc:
        print(f"[WARN] Pump/FomoScan leaderboard: {redact_error(exc)}")


# --------------------------
# Scanner
# --------------------------

def scan_tokens(cfg: Dict[str, Any], st: Dict[str, Any]) -> None:
    scfg = cfg.get("scanner", {})
    max_tokens = int(scfg.get("max_tokens_per_scan", 35))

    candidates = []
    if scfg.get("pump_new_tokens", True):
        candidates.extend(pump_new_candidates(limit=25))
    if scfg.get("dex_latest", True):
        candidates.extend(dex_latest_candidates())

    # Prioriza tokens comprados por KOLs recientemente.
    cutoff = now_ts() - int(cfg.get("kol_recent_buy_window_seconds", 3600))
    for mint, arr in st.get("kol_recent_buys", {}).items():
        if any(int(x.get("time", 0)) >= cutoff for x in arr):
            candidates.insert(0, mint)

    candidates = list(dict.fromkeys(x for x in candidates if x))[:max_tokens]

    threshold = int(cfg.get("radar_score_threshold", 70))
    cooldown = int(cfg.get("opportunity_alert_cooldown_seconds", 10800))
    alerted = st.setdefault("token_alerted_at", {})

    for mint in candidates:
        try:
            p = best_pair(mint)
            if not p:
                continue

            liq = num((p.get("liquidity") or {}).get("usd"))
            if liq < num(scfg.get("absolute_min_liquidity_usd", 8000)):
                continue

            rug = rug_summary(mint) if scfg.get("rugcheck", True) else {}
            kols = recent_kols_for_token(st, cfg, mint)
            sig = token_score(mint, p, rug, kols)

            if sig.score < threshold:
                continue

            last_alert = int(alerted.get(mint, 0))
            if now_ts() - last_alert < cooldown:
                continue

            broadcast(st, signal_alert(sig))
            alerted[mint] = now_ts()

        except Exception as exc:
            print(f"[WARN] Scanner {mint[:8]}: {redact_error(exc)}")

    # limpia cooldowns muy viejos
    limit_ts = now_ts() - 7 * 86400
    st["token_alerted_at"] = {
        k: v for k, v in alerted.items()
        if int(v) >= limit_ts
    }
    save_json(STATE_PATH, st)


# --------------------------
# Main
# --------------------------

def validate() -> None:
    if not TG_TOKEN:
        raise SystemExit("Falta TELEGRAM_BOT_TOKEN.")
    if not HELIUS_API_KEY:
        print("[WARN] Falta HELIUS_API_KEY: no se podrán interpretar swaps de wallets Solana.")
    if not FOMOSCAN_API_KEY:
        print("[INFO] FOMOSCAN_API_KEY no definida: Pump Top 3 dinámico queda desactivado.")
    print("[INFO] Hyperliquid Top 3 no necesita API key.")


def main() -> None:
    validate()
    try:
        bot = tg_request("getMe").get("result", {})
        if not bot.get("is_bot"):
            raise RuntimeError("Telegram no devolvió una identidad de bot válida.")
        print(f"[OK] Telegram autenticado: @{bot.get('username', 'bot')}.")
    except Exception as exc:
        raise SystemExit(f"[ERROR] No se pudo autenticar Telegram: {redact_error(exc)}") from None
    cfg = load_config()
    st = load_state()

    print("📡 Crypto Radar v2 iniciado.")
    print("Envía /start a tu bot en Telegram.")

    next_wallet = 0.0
    next_scan = 0.0
    next_lb = 0.0

    while True:
        cfg = load_config()
        poll_telegram(cfg, st)

        t = time.time()

        if t >= next_lb:
            update_hyper_top(cfg, st)
            update_pump_top(cfg, st)
            next_lb = t + int(cfg.get("leaderboard_seconds", 900))

        if t >= next_wallet:
            check_wallets(cfg, st)
            check_hyper_fills(cfg, st)
            next_wallet = t + int(cfg.get("poll_seconds", 30))

        if t >= next_scan:
            scan_tokens(cfg, st)
            next_scan = t + int(cfg.get("scanner_seconds", 120))

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"[ERROR] El bot se detuvo: {redact_error(exc)}") from None
