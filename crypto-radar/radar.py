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
HELIUS_UNFILTERED_ADDRESSES: set[str] = set()
HELIUS_FALLBACK_LOGGED: set[str] = set()

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
    cfg.setdefault("kol_swarm_window_seconds", 900)
    cfg.setdefault("kol_swarm_min_wallets", 2)
    cfg.setdefault("kol_high_convergence_min_wallets", 3)
    cfg.setdefault("kol_full_security_report", True)
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
    st.setdefault("kol_trade_stats", {})
    st.setdefault("kol_swarm_alerts", {})
    st.setdefault("token_security_snapshots", {})
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
        f"{trade_action_links_html(sig.mint, 'BUY', p)}\n"
        f"🔎 DexScreener: https://dexscreener.com/solana/{html.escape(sig.mint)}\n\n"
        "⚠️ El enlace abre el exchange con el token cargado; la operación siempre se confirma manualmente. "
        "El puntaje mide señales observables y no predice que el token vaya a subir."
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


def _is_swap_tx(tx: Dict[str, Any]) -> bool:
    if str(tx.get("type") or "").upper() == "SWAP":
        return True
    swap = ((tx.get("events") or {}).get("swap") or {})
    return isinstance(swap, dict) and bool(swap)


def helius_history(address: str, limit: int = 20) -> List[Dict[str, Any]]:
    """
    Enhanced Transactions API.

    Helius' current documented host is api.helius.xyz. Some high-activity
    addresses intermittently return 404 when the server-side type=SWAP filter
    is used. In that case we remember the address for this process, fetch a
    wider unfiltered window, and identify swaps locally.
    """
    global HELIUS_READY
    if not HELIUS_API_KEY:
        return []

    url = f"https://api.helius.xyz/v0/addresses/{address}/transactions"
    use_unfiltered = address in HELIUS_UNFILTERED_ADDRESSES
    fetch_limit = max(60, min(100, int(limit) * 4)) if use_unfiltered else int(limit)

    params: Dict[str, Any] = {
        "api-key": HELIUS_API_KEY,
        "limit": fetch_limit,
    }
    if not use_unfiltered:
        params["type"] = "SWAP"

    r = session.get(url, params=params, timeout=25)

    # A few active wallets can return 404 only with the server-side SWAP
    # filter. Retry once without it, then keep using the fallback in memory.
    if r.status_code == 404 and not use_unfiltered:
        HELIUS_UNFILTERED_ADDRESSES.add(address)
        if address not in HELIUS_FALLBACK_LOGGED:
            print(
                f"[INFO] Helius: filtro SWAP no disponible para {address[:8]}…; "
                "usando historial general + filtro local."
            )
            HELIUS_FALLBACK_LOGGED.add(address)

        params = {
            "api-key": HELIUS_API_KEY,
            "limit": max(60, min(100, int(limit) * 4)),
        }
        r = session.get(url, params=params, timeout=25)
        use_unfiltered = True

    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("Helius devolvió una respuesta inesperada.")

    if use_unfiltered:
        data = [tx for tx in data if isinstance(tx, dict) and _is_swap_tx(tx)][:limit]

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


SOL_MINT = "So11111111111111111111111111111111111111112"
STABLE_MINTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYDkdxQivL6GxQYQy8H1KqG",   # USDT
}
PAYMENT_MINTS = STABLE_MINTS | {SOL_MINT}
_PRICE_CACHE: Dict[str, Tuple[int, float]] = {}
_RUG_FULL_CACHE: Dict[str, Tuple[int, Dict[str, Any]]] = {}


def trade_action_links_html(
    mint: str,
    side: str = "BUY",
    pair: Optional[Dict[str, Any]] = None,
) -> str:
    """Manual trading links only; the bot never signs or submits an order."""
    safe_mint = quote(str(mint), safe="")
    safe_sol = quote(SOL_MINT, safe="")
    is_sell = str(side).upper() == "SELL"

    if is_sell:
        action = "💸 <b>Vender:</b>"
        jupiter = f"https://jup.ag/swap?buy={safe_sol}&sell={safe_mint}"
    else:
        action = "🛒 <b>Comprar:</b>"
        jupiter = f"https://jup.ag/swap?buy={safe_mint}&sell={safe_sol}"

    pump = f"https://pump.fun/coin/{safe_mint}"
    dex_name = html.escape(str((pair or {}).get("dexId") or "Solana"))

    return (
        f'{action} <a href="{jupiter}">Jupiter</a>'
        f' · <a href="{pump}">Pump.fun</a>'
        f' · DEX: <b>{dex_name}</b>'
    )


def choose_primary_flow(flows: List[Tuple[str, float]]) -> Optional[Tuple[str, float]]:
    if not flows:
        return None
    nonpayment = [x for x in flows if x[0] not in PAYMENT_MINTS]
    arr = nonpayment or flows
    positive = [x for x in arr if x[1] > 0]
    if positive:
        return max(positive, key=lambda x: abs(x[1]))
    return max(arr, key=lambda x: abs(x[1]))


def _balance_change_ui(item: Dict[str, Any]) -> float:
    raw = item.get("rawTokenAmount") or {}
    amount = abs(num(raw.get("tokenAmount")))
    decimals = int(num(raw.get("decimals"), 0))
    if decimals < 0 or decimals > 30:
        decimals = 0
    return amount / (10 ** decimals)


def _user_changes(items: Any, address: str) -> List[Dict[str, Any]]:
    rows = [x for x in (items or []) if isinstance(x, dict)]
    owned = [x for x in rows if str(x.get("userAccount") or "") == address]
    return owned or rows


def _largest_change(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not items:
        return None
    return max(items, key=_balance_change_ui)


def parse_swap_trade(tx: Dict[str, Any], address: str) -> Optional[Dict[str, Any]]:
    """Interpret a Helius SWAP using events.swap first, then transfer deltas as fallback."""
    swap = ((tx.get("events") or {}).get("swap") or {})
    if isinstance(swap, dict) and swap:
        inputs = _user_changes(swap.get("tokenInputs"), address)
        outputs = _user_changes(swap.get("tokenOutputs"), address)
        nonpay_in = [x for x in inputs if str(x.get("mint") or "") not in PAYMENT_MINTS]
        nonpay_out = [x for x in outputs if str(x.get("mint") or "") not in PAYMENT_MINTS]

        native_in = swap.get("nativeInput") or {}
        native_out = swap.get("nativeOutput") or {}
        native_in_sol = 0.0
        native_out_sol = 0.0
        if isinstance(native_in, dict):
            if not native_in.get("account") or str(native_in.get("account")) == address:
                native_in_sol = abs(num(native_in.get("amount"))) / 1_000_000_000
        if isinstance(native_out, dict):
            if not native_out.get("account") or str(native_out.get("account")) == address:
                native_out_sol = abs(num(native_out.get("amount"))) / 1_000_000_000

        if nonpay_out:
            target = _largest_change(nonpay_out)
            side = "BUY"
            payment_items = inputs
            sol_amount = native_in_sol
        elif nonpay_in:
            target = _largest_change(nonpay_in)
            side = "SELL"
            payment_items = outputs
            sol_amount = native_out_sol
        else:
            target = None
            side = ""
            payment_items = []
            sol_amount = 0.0

        if target:
            mint = str(target.get("mint") or "")
            token_amount = _balance_change_ui(target)
            if not sol_amount:
                sol_amount = sum(
                    _balance_change_ui(x)
                    for x in payment_items
                    if str(x.get("mint") or "") == SOL_MINT
                )
            stable_usd = sum(
                _balance_change_ui(x)
                for x in payment_items
                if str(x.get("mint") or "") in STABLE_MINTS
            )
            return {
                "side": side,
                "mint": mint,
                "delta": token_amount if side == "BUY" else -token_amount,
                "token_amount": token_amount,
                "sol_amount": sol_amount,
                "stable_usd": stable_usd,
                "source": str(tx.get("source") or "SWAP"),
                "parsed_from": "events.swap",
            }

    flows = wallet_token_flows(tx, address)
    primary = choose_primary_flow(flows)
    if not primary:
        return None
    mint, delta = primary
    return {
        "side": "BUY" if delta > 0 else "SELL",
        "mint": mint,
        "delta": delta,
        "token_amount": abs(delta),
        "sol_amount": 0.0,
        "stable_usd": 0.0,
        "source": str(tx.get("source") or "SWAP"),
        "parsed_from": "tokenTransfers",
    }


def token_price_usd(mint: str, ttl: int = 30) -> float:
    cached = _PRICE_CACHE.get(mint)
    t = now_ts()
    if cached and t - int(cached[0]) <= ttl:
        return float(cached[1])
    p = best_pair(mint)
    price = num((p or {}).get("priceUsd"))
    if price > 0:
        _PRICE_CACHE[mint] = (t, price)
    return price


def estimate_trade_value_usd(info: Dict[str, Any], pair: Optional[Dict[str, Any]]) -> float:
    stable = num(info.get("stable_usd"))
    if stable > 0:
        return stable

    sol_amount = num(info.get("sol_amount"))
    if sol_amount > 0:
        sol_usd = token_price_usd(SOL_MINT)
        if sol_usd > 0:
            return sol_amount * sol_usd

    token_amount = abs(num(info.get("token_amount")))
    token_px = num((pair or {}).get("priceUsd"))
    if token_amount > 0 and token_px > 0:
        return token_amount * token_px
    return 0.0


def format_qty(v: float) -> str:
    a = abs(v)
    sign = "-" if v < 0 else "+"
    if a >= 1_000_000_000:
        return f"{sign}{a/1_000_000_000:.3f}B"
    if a >= 1_000_000:
        return f"{sign}{a/1_000_000:.3f}M"
    if a >= 1_000:
        return f"{sign}{a/1_000:.3f}K"
    if a >= 1:
        return f"{sign}{a:,.4f}".rstrip("0").rstrip(".")
    return f"{sign}{a:.8g}"


def age_text_at(pair: Optional[Dict[str, Any]], ts: int) -> str:
    created_ms = int(num((pair or {}).get("pairCreatedAt")))
    if not created_ms or not ts:
        return "N/D"
    seconds = max(0, ts - created_ms // 1000)
    if seconds < 60:
        return f"{seconds}s"
    mins = seconds // 60
    if mins < 60:
        return f"{mins}m {seconds % 60:02d}s"
    hours = mins // 60
    return f"{hours}h {mins % 60:02d}m"


def rug_report(mint: str, ttl: int = 60) -> Dict[str, Any]:
    t = now_ts()
    cached = _RUG_FULL_CACHE.get(mint)
    if cached and t - int(cached[0]) <= ttl:
        return cached[1]
    try:
        r = session.get(f"{RUG_BASE}/tokens/{mint}/report", timeout=15)
        if not r.ok:
            return {}
        data = r.json()
        if isinstance(data, dict):
            _RUG_FULL_CACHE[mint] = (t, data)
            return data
    except Exception as exc:
        print(f"[WARN] RugCheck full {mint[:8]}: {redact_error(exc)}")
    return {}


def security_snapshot(st: Dict[str, Any], mint: str, report: Dict[str, Any]) -> str:
    if not report:
        return "🛡 Seguridad: RugCheck sin datos"

    holders = [x for x in (report.get("topHolders") or []) if isinstance(x, dict)]
    top10 = sum(num(x.get("pct")) for x in holders[:10])
    insider_pct = sum(num(x.get("pct")) for x in holders if x.get("insider"))

    token = report.get("token") or {}
    supply = num(token.get("supply"))
    creator_balance = num(report.get("creatorBalance"))
    creator_pct = (creator_balance / supply * 100.0) if supply > 0 else 0.0

    mint_active = bool(token.get("mintAuthority"))
    freeze_active = bool(token.get("freezeAuthority"))

    movement = ""
    creator_drop_pct = 0.0
    movement_detected = False
    snaps = st.setdefault("token_security_snapshots", {})
    prev = snaps.get(mint) or {}
    prev_balance = num(prev.get("creator_balance"))
    if prev_balance > 0 and creator_balance >= 0 and creator_balance < prev_balance:
        creator_drop_pct = (prev_balance - creator_balance) / prev_balance * 100.0
        if creator_drop_pct >= 5:
            movement_detected = True
            movement = f" · ⚠️ saldo creador ↓{creator_drop_pct:.1f}%"

    snaps[mint] = {
        "creator": str(report.get("creator") or ""),
        "creator_balance": creator_balance,
        "creator_pct": creator_pct,
        "creator_drop_pct": creator_drop_pct,
        "creator_movement_detected": movement_detected,
        "top10_pct": top10,
        "insider_pct": insider_pct,
        "mint_active": mint_active,
        "freeze_active": freeze_active,
        "time": now_ts(),
    }

    authority = (
        ("⚠️ mint activa" if mint_active else "mint revocada")
        + " · "
        + ("⚠️ freeze activa" if freeze_active else "freeze revocada")
    )
    insider = f" · insiders {insider_pct:.1f}%" if insider_pct > 0 else ""
    return (
        f"🛡 Top10 {top10:.1f}% · creador {creator_pct:.2f}% · "
        f"{authority}{insider}{movement}"
    )


def short_kol_label(label: str) -> str:
    text = str(label or "KOL").strip()
    if " (" in text:
        text = text.split(" (", 1)[0]
    return text[:28]


def elapsed_short(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minute = divmod(minutes, 60)
    return f"{hours}h {minute:02d}m"


def add_recent_kol_buy(
    st: Dict[str, Any],
    mint: str,
    label: str,
    address: str,
    ts: int,
    detected_at: int,
    usd_value: float,
    mcap: float,
    price: float,
    token_amount: float,
) -> None:
    arr = st.setdefault("kol_recent_buys", {}).setdefault(mint, [])
    arr.append({
        "label": label,
        "address": address,
        "time": int(ts),
        "detected_at": int(detected_at),
        "latency_seconds": max(0, int(detected_at) - int(ts)),
        "usd_value": max(0.0, num(usd_value)),
        "mcap": max(0.0, num(mcap)),
        "price": max(0.0, num(price)),
        "token_amount": max(0.0, num(token_amount)),
    })
    cutoff = now_ts() - 6 * 3600
    st["kol_recent_buys"][mint] = [
        x for x in arr if int(x.get("time", 0)) >= cutoff
    ]


def recent_kol_buy_entries(
    st: Dict[str, Any],
    cfg: Dict[str, Any],
    mint: str,
    window_seconds: Optional[int] = None,
) -> List[Dict[str, Any]]:
    window = int(window_seconds or cfg.get("kol_recent_buy_window_seconds", 3600))
    cutoff = now_ts() - window
    return sorted(
        [
            x for x in st.get("kol_recent_buys", {}).get(mint, [])
            if int(x.get("time", 0)) >= cutoff and x.get("label")
        ],
        key=lambda x: int(x.get("time", 0)),
    )


def recent_kols_for_token(
    st: Dict[str, Any], cfg: Dict[str, Any], mint: str,
    window_seconds: Optional[int] = None,
) -> List[str]:
    return list(dict.fromkeys(
        x.get("label", "")
        for x in recent_kol_buy_entries(st, cfg, mint, window_seconds)
        if x.get("label")
    ))


def kol_conviction_score(
    st: Dict[str, Any],
    entries: List[Dict[str, Any]],
    liquidity_usd: float,
) -> int:
    if not entries:
        return 0

    unique = list(dict.fromkeys(str(x.get("label") or "") for x in entries))
    level = len(unique)
    score = 0.0

    if level >= 5:
        score += 65
    elif level == 4:
        score += 58
    elif level == 3:
        score += 50
    elif level == 2:
        score += 35
    else:
        score += 15

    total_usd = sum(max(0.0, num(x.get("usd_value"))) for x in entries)
    if total_usd >= 10_000:
        score += 20
    elif total_usd >= 3_000:
        score += 16
    elif total_usd >= 1_000:
        score += 12
    elif total_usd >= 300:
        score += 8
    elif total_usd >= 50:
        score += 4

    times = [int(x.get("time", 0)) for x in entries if int(x.get("time", 0)) > 0]
    if len(times) >= 2:
        spread = max(times) - min(times)
        if spread <= 60:
            score += 15
        elif spread <= 180:
            score += 12
        elif spread <= 300:
            score += 8
        elif spread <= 900:
            score += 4

    if liquidity_usd > 0 and total_usd > 0:
        impact = total_usd / liquidity_usd
        if impact >= 0.10:
            score += 10
        elif impact >= 0.05:
            score += 7
        elif impact >= 0.01:
            score += 3

    per_label: Dict[str, int] = {}
    for x in entries:
        key = str(x.get("label") or "")
        per_label[key] = per_label.get(key, 0) + 1
    repeat_buys = sum(max(0, c - 1) for c in per_label.values())
    score += min(10, repeat_buys * 4)

    return int(clamp(round(score), 0, 100))


def convergence_risk(
    st: Dict[str, Any],
    mint: str,
    pair: Dict[str, Any],
    report: Dict[str, Any],
) -> Tuple[str, float, float, str]:
    liq = num((pair.get("liquidity") or {}).get("usd"))
    snap = st.get("token_security_snapshots", {}).get(mint) or {}

    holders = [x for x in (report.get("topHolders") or []) if isinstance(x, dict)]
    top10 = num(snap.get("top10_pct"))
    if top10 <= 0 and holders:
        top10 = sum(num(x.get("pct")) for x in holders[:10])

    token = report.get("token") or {}
    supply = num(token.get("supply"))
    creator_balance = num(report.get("creatorBalance"))
    creator_pct = num(snap.get("creator_pct"))
    if creator_pct <= 0 and supply > 0:
        creator_pct = creator_balance / supply * 100.0

    insider_pct = num(snap.get("insider_pct"))
    if insider_pct <= 0:
        insider_pct = sum(num(x.get("pct")) for x in holders if x.get("insider"))

    mint_active = bool(snap.get("mint_active", token.get("mintAuthority")))
    freeze_active = bool(snap.get("freeze_active", token.get("freezeAuthority")))
    dev_movement = bool(snap.get("creator_movement_detected"))
    dev_drop = num(snap.get("creator_drop_pct"))

    risk = 0.0
    if liq < 8_000:
        risk += 35
    elif liq < 20_000:
        risk += 25
    elif liq < 50_000:
        risk += 15
    elif liq < 100_000:
        risk += 7

    if top10 >= 50:
        risk += 30
    elif top10 >= 30:
        risk += 20
    elif top10 >= 20:
        risk += 10

    if creator_pct >= 10:
        risk += 25
    elif creator_pct >= 5:
        risk += 15
    elif creator_pct >= 2:
        risk += 7

    if insider_pct >= 20:
        risk += 15
    elif insider_pct >= 10:
        risk += 8

    if mint_active:
        risk += 18
    if freeze_active:
        risk += 18
    if dev_movement:
        risk += 20

    rug_penalty, _ = extract_rug_penalty(report or {})
    risk += rug_penalty * 0.5
    risk = clamp(risk, 0, 100)

    if risk < 20:
        label = "BAJO"
    elif risk < 40:
        label = "MEDIO"
    elif risk < 60:
        label = "MEDIO-ALTO"
    else:
        label = "ALTO"

    if dev_movement:
        dev_text = f"⚠️ saldo creador cayó {dev_drop:.1f}% desde la última observación"
    elif snap:
        dev_text = "sin ventas/movimientos detectados desde que el radar observa"
    else:
        dev_text = "sin historial suficiente"

    return label, top10, creator_pct, dev_text


def update_kol_trade_stats(
    st: Dict[str, Any], address: str, label: str, info: Dict[str, Any], ts: int
) -> str:
    mint = str(info.get("mint") or "")
    amount = abs(num(info.get("token_amount")))
    usd = max(0.0, num(info.get("usd_value")))
    by_wallet = st.setdefault("kol_trade_stats", {}).setdefault(address, {})
    s = by_wallet.setdefault(mint, {
        "label": label,
        "buys": 0,
        "sells": 0,
        "buy_tokens": 0.0,
        "sell_tokens": 0.0,
        "buy_usd": 0.0,
        "sell_usd": 0.0,
        "first_seen": ts,
    })

    if info.get("side") == "BUY":
        s["buys"] = int(s.get("buys", 0)) + 1
        s["buy_tokens"] = num(s.get("buy_tokens")) + amount
        s["buy_usd"] = num(s.get("buy_usd")) + usd
        note = "primera compra observada" if s["buys"] == 1 else f"recompra #{s['buys']}"
    else:
        s["sells"] = int(s.get("sells", 0)) + 1
        s["sell_tokens"] = num(s.get("sell_tokens")) + amount
        s["sell_usd"] = num(s.get("sell_usd")) + usd
        bought = num(s.get("buy_tokens"))
        sold = num(s.get("sell_tokens"))
        if bought > 0:
            pct = sold / bought * 100.0
            if pct >= 95:
                note = f"🚪 posible salida total ({pct:.0f}% de compras observadas vendido)"
            else:
                note = f"venta parcial ({pct:.0f}% de compras observadas vendido)"
        else:
            note = "venta observada sin compra previa en la memoria del bot"

    s["last_seen"] = ts
    net = num(s.get("buy_tokens")) - num(s.get("sell_tokens"))
    s["net_tokens"] = net
    return (
        f"🎯 Seguimiento: {note} · compras {int(s.get('buys', 0))} · "
        f"ventas {int(s.get('sells', 0))} · comprado observado {money(s.get('buy_usd'))}"
    )


def kol_trade_alert(
    label: str,
    address: str,
    tx: Dict[str, Any],
    info: Dict[str, Any],
    pair: Optional[Dict[str, Any]],
    tracking_note: str,
    security_line: str,
    recent_kols: List[str],
) -> str:
    side = str(info.get("side") or "SWAP")
    side_text = "🟢 COMPRA" if side == "BUY" else "🔴 VENTA"
    sig = str(tx.get("signature") or "")
    timestamp = int(tx.get("timestamp") or 0)
    mint = str(info.get("mint") or "")
    delta = num(info.get("delta"))
    usd = num(info.get("usd_value"))
    sol_amount = num(info.get("sol_amount"))

    name = "Token"
    sym = "?"
    liq = mcap = vol1 = price = 0.0
    buys = sells = 0
    if pair:
        name = ((pair.get("baseToken") or {}).get("name") or "Token")
        sym = ((pair.get("baseToken") or {}).get("symbol") or "?")
        liq = num((pair.get("liquidity") or {}).get("usd"))
        mcap = num(pair.get("marketCap") or pair.get("fdv"))
        vol1 = num((pair.get("volume") or {}).get("h1"))
        price = num(pair.get("priceUsd"))
        txh = (pair.get("txns") or {}).get("h1") or {}
        buys = int(num(txh.get("buys")))
        sells = int(num(txh.get("sells")))

    ratio = (buys / sells) if sells > 0 else (float(buys) if buys else 0.0)
    if sol_amount > 0 and usd > 0:
        size_line = f"💳 Tamaño: <b>{sol_amount:.4f} SOL ≈ {money(usd)}</b>"
    elif usd > 0:
        size_line = f"💳 Valor aprox.: <b>{money(usd)}</b>"
    else:
        size_line = "💳 Valor aprox.: <b>N/D</b>"

    kols_text = ", ".join(recent_kols) if recent_kols else "solo esta wallet"
    protocol = html.escape(str(info.get("source") or "SWAP"))

    return (
        f"👀 <b>KOL WALLET — {side_text}</b>\n\n"
        f"👤 <b>{html.escape(label)}</b>\n"
        f"👛 <code>{html.escape(address)}</code>\n"
        f"🕒 {html.escape(utc_text(timestamp))}\n"
        f"⚙️ Protocolo: {protocol}\n\n"
        f"🪙 <b>{html.escape(str(name))} ({html.escape(str(sym))})</b>\n"
        f"📄 CA / Mint:\n<code>{html.escape(mint)}</code>\n"
        f"🔢 Tokens: <b>{html.escape(format_qty(delta))}</b>\n"
        f"{size_line}\n"
        f"💵 Precio al detectar: {money(price) if price > 0 else 'N/D'}\n"
        f"💰 MC al detectar: <b>{money(mcap)}</b>\n"
        f"💧 Liquidez: <b>{money(liq)}</b>\n"
        f"⏳ Edad al ejecutar: <b>{html.escape(age_text_at(pair, timestamp))}</b>\n"
        f"📊 Volumen 1h: {money(vol1)}\n"
        f"🔄 1h: {buys} compras / {sells} ventas · ratio {ratio:.2f}\n"
        f"👥 KOLs recientes: {html.escape(kols_text)}\n"
        f"{html.escape(tracking_note)}\n"
        f"{html.escape(security_line)}\n\n"
        f"{trade_action_links_html(mint, side, pair)}\n"
        f"🔎 https://solscan.io/tx/{html.escape(sig)}\n"
        f"📊 https://dexscreener.com/solana/{html.escape(mint)}"
    )


def kol_swarm_alert(
    cfg: Dict[str, Any],
    st: Dict[str, Any],
    mint: str,
    pair: Optional[Dict[str, Any]],
    report: Dict[str, Any],
) -> Optional[str]:
    window = int(cfg.get("kol_swarm_window_seconds", 900))
    minimum = int(cfg.get("kol_swarm_min_wallets", 2))
    high_minimum = int(cfg.get("kol_high_convergence_min_wallets", 3))
    entries = recent_kol_buy_entries(st, cfg, mint, window)
    kols = list(dict.fromkeys(
        str(x.get("label") or "") for x in entries if x.get("label")
    ))
    level = len(kols)
    if level < minimum:
        return None

    rec = st.setdefault("kol_swarm_alerts", {}).get(mint) or {}
    previous = int(rec.get("level", 0))
    last = int(rec.get("time", 0))
    if now_ts() - last > window:
        previous = 0
    if level <= previous:
        return None

    p = pair or {}
    name = ((p.get("baseToken") or {}).get("name") or "Token")
    sym = ((p.get("baseToken") or {}).get("symbol") or "?")
    liq = num((p.get("liquidity") or {}).get("usd"))
    current_mcap = num(p.get("marketCap") or p.get("fdv"))
    txh = (p.get("txns") or {}).get("h1") or {}
    buys = int(num(txh.get("buys")))
    sells = int(num(txh.get("sells")))
    ratio = (buys / sells) if sells > 0 else (float(buys) if buys else 0.0)

    token_radar = token_score(mint, p, report or {}, kols).score if p else 0
    conviction = kol_conviction_score(st, entries, liq)
    overall = int(clamp(round(conviction * 0.60 + token_radar * 0.40 + (5 if level >= 3 else 0)), 0, 100))

    total_usd = sum(max(0.0, num(x.get("usd_value"))) for x in entries)
    first = entries[0] if entries else {}
    newest = entries[-1] if entries else {}
    first_ts = int(first.get("time", 0))
    first_mcap = num(first.get("mcap"))
    latest_latency = int(num(newest.get("latency_seconds")))

    mcap_change = None
    if first_mcap > 0 and current_mcap > 0:
        mcap_change = (current_mcap / first_mcap - 1.0) * 100.0

    risk_label, top10, creator_pct, dev_text = convergence_risk(
        st, mint, p, report or {}
    )

    st["kol_swarm_alerts"][mint] = {
        "level": level,
        "time": now_ts(),
        "overall": overall,
        "conviction": conviction,
        "token_radar": token_radar,
        "risk": risk_label,
    }

    names = " + ".join(short_kol_label(x) for x in kols)
    first_ago = elapsed_short(now_ts() - first_ts) if first_ts else "N/D"
    first_mc_text = money(first_mcap) if first_mcap > 0 else "N/D"
    current_mc_text = money(current_mcap) if current_mcap > 0 else "N/D"
    if mcap_change is not None:
        current_mc_text += f" ({mcap_change:+.0f}%)"

    if level >= high_minimum:
        title = f"🔥 <b>ALTA CONVERGENCIA — {overall}/100</b>"
    else:
        title = f"🟠 <b>CONVERGENCIA — {overall}/100</b>"

    total_text = money(total_usd) if total_usd > 0 else "N/D"
    top10_text = f"{top10:.1f}%" if top10 > 0 else "N/D"

    return (
        f"{title}\n"
        f"{html.escape(names)}\n\n"
        f"💰 Comprado por KOLs: <b>≈ {total_text}</b>\n"
        f"🕐 Primer KOL: hace <b>{html.escape(first_ago)}</b>\n"
        f"⚡ Retraso del radar: <b>{latest_latency}s</b>\n"
        f"📈 MC primera entrada: <b>{first_mc_text}</b>\n"
        f"📈 MC actual: <b>{current_mc_text}</b>\n"
        f"💧 Liquidez: <b>{money(liq)}</b>\n"
        f"🔄 Buy/Sell: <b>{ratio:.2f}</b> ({buys}/{sells})\n"
        f"👥 Top 10: <b>{top10_text}</b>\n"
        f"👨‍💻 Dev: {html.escape(dev_text)}\n\n"
        f"🎯 <b>Convicción KOL: {conviction}/100</b>\n"
        f"🧠 <b>Token Radar: {token_radar}/100</b>\n"
        f"⚠️ <b>Riesgo: {html.escape(risk_label)}</b>\n\n"
        f"{trade_action_links_html(mint, 'BUY', p)}\n"
        f"📋 Contrato: <code>{html.escape(mint)}</code>\n"
        f"📊 https://dexscreener.com/solana/{html.escape(mint)}\n\n"
        "⚠️ Convicción y Radar Score son filtros observacionales, no una garantía de rentabilidad."
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
                info = parse_swap_trade(tx, address)
                if not info:
                    continue

                mint = str(info.get("mint") or "")
                if not mint or mint in PAYMENT_MINTS:
                    continue

                pair = best_pair(mint)
                info["usd_value"] = estimate_trade_value_usd(info, pair)
                ts = int(tx.get("timestamp") or now_ts())

                report = rug_report(mint) if cfg.get("kol_full_security_report", True) else {}
                security_line = security_snapshot(st, mint, report)
                tracking_note = update_kol_trade_stats(st, address, label, info, ts)

                if info.get("side") == "BUY":
                    pair_mcap = num((pair or {}).get("marketCap") or (pair or {}).get("fdv"))
                    pair_price = num((pair or {}).get("priceUsd"))
                    add_recent_kol_buy(
                        st=st,
                        mint=mint,
                        label=label,
                        address=address,
                        ts=ts,
                        detected_at=now_ts(),
                        usd_value=num(info.get("usd_value")),
                        mcap=pair_mcap,
                        price=pair_price,
                        token_amount=num(info.get("token_amount")),
                    )

                swarm_window = int(cfg.get("kol_swarm_window_seconds", 900))
                recent = recent_kols_for_token(st, cfg, mint, swarm_window)
                broadcast(
                    st,
                    kol_trade_alert(
                        label, address, tx, info, pair,
                        tracking_note, security_line, recent,
                    ),
                )

                if info.get("side") == "BUY":
                    swarm = kol_swarm_alert(cfg, st, mint, pair, report)
                    if swarm:
                        broadcast(st, swarm)

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
