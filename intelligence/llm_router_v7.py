"""
Eve v7 — LLM router (multi-provider, preference chain + key pool).

Public API purane router jaisa hi hai:
    chat(route, system, user, ...)  -> Optional[str]
    chat_json(route, system, user)  -> Optional[dict]

Naya kya hai:
  * 5 providers: groq, xai (GrokX), gemini, anthropic, agentrouter (Claude).
  * Har task ka apna model chain — `intelligence/model_prefs.py` se aata hai.
    Primary fail -> fallback 1 -> fallback 2 ... bina bot ruke.
  * Har provider ke andar key pool: 100 req per key, fail pe turant agli key.
  * Usage tracking per provider (/stats aur cost ke liye).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from intelligence import key_pool, model_prefs, providers

logger = logging.getLogger("eve.router7")

# purane route naam (baaki codebase inhi ko bhejti hai)
ROUTE_BANTER = "banter"
ROUTE_DECISION = "decision"
ROUTE_LEARN = "learn"
ROUTE_ANALYZE = "analyze"
ROUTE_FACTS = "facts"

ROUTE_TO_TASK = {
    ROUTE_BANTER: "normal",
    ROUTE_DECISION: "decision",
    ROUTE_LEARN: "learn",
    ROUTE_ANALYZE: "analyze",
    ROUTE_FACTS: "debate",
    "roast": "roast",
    "flirt": "flirt",
    "debate": "debate",
    "help": "help",
}

MAX_KEY_ATTEMPTS = 6      # ek provider ke andar itni keys tak try karega

_usage: Dict[str, int] = {}
_since_persist = 0


# ---------------------------------------------------------------- usage


def _bump(field: str, n: int = 1) -> None:
    if n:
        _usage[field] = _usage.get(field, 0) + n


def persist_usage() -> None:
    try:
        from datetime import date
        from intelligence.aihumara_state import _get as sg, _set as ss
        if not _usage:
            return
        for scope in ("usage", f"usage_{date.today().isoformat()}"):
            prev = sg(scope) or {}
            merged = dict(prev)
            for k, v in _usage.items():
                merged[k] = int(prev.get(k, 0)) + int(v)
            ss(scope, merged)
        _usage.clear()
    except Exception as e:
        logger.warning("[ROUTER] usage persist failed: %s", e)


def _maybe_persist() -> None:
    global _since_persist
    _since_persist += 1
    if _since_persist >= 20:
        _since_persist = 0
        persist_usage()


def get_usage() -> Dict[str, int]:
    return dict(_usage)


def opus_cost_usd(u: Optional[Dict[str, int]] = None) -> float:
    """Claude (official + agentrouter) ka approx kharcha."""
    u = u or _usage
    tin = u.get("anthropic_input", 0) + u.get("agentrouter_input", 0)
    tout = u.get("anthropic_output", 0) + u.get("agentrouter_output", 0)
    return tin * 5.0 / 1e6 + tout * 25.0 / 1e6


# ---------------------------------------------------------- single slot


def _try_provider(provider: str, model: str, system: str, user: str, *,
                  max_tokens: int, temperature: float,
                  json_mode: bool) -> Optional[str]:
    """Ek provider ki saari usable keys try karo. Sab fail -> None."""
    tried: List[int] = []

    for _ in range(MAX_KEY_ATTEMPTS):
        entry = key_pool.acquire(provider, skip_ids=tried)
        if entry is None:
            break
        kid = int(entry["id"])
        tried.append(kid)
        use_model = model or entry.get("model") or providers.default_model(provider)
        try:
            text, tin, tout = providers.chat_ex(
                provider, entry["api_key"], use_model, system, user,
                base_url=entry.get("base_url"), max_tokens=max_tokens,
                temperature=temperature, json_mode=json_mode,
            )
            key_pool.report_success(kid)
            _bump(f"{provider}_calls")
            _bump(f"{provider}_input", tin)
            _bump(f"{provider}_output", tout)
            if text:
                return text
            logger.warning("[ROUTER] %s/%s ne khali jawab diya", provider, use_model)
        except Exception as e:  # noqa: BLE001
            fatal = key_pool.is_fatal_error(e)
            key_pool.report_failure(kid, str(e), fatal=fatal)
            _bump(f"{provider}_fail")
            logger.warning("[ROUTER] %s key #%s fail: %s", provider, kid, e)
            continue

    if not tried:
        logger.debug("[ROUTER] %s ki koi usable key nahi", provider)
    return None


# ----------------------------------------------------------------- api


def _force() -> str:
    try:
        from intelligence.aihumara_state import get_model_force
        return get_model_force()
    except Exception:
        return "default"


def _chain_for(task: str) -> List[Dict[str, str]]:
    chain = model_prefs.get_chain(task)
    force = _force()
    if force == "groq_only":
        only = [c for c in chain if c["provider"] == "groq"]
        return only or chain
    if force == "opus_only":
        heavy = [c for c in chain if c["provider"] in ("agentrouter", "anthropic")]
        return heavy + [c for c in chain if c not in heavy]
    return chain


def chat_task(task: str, system: str, user: str, *, max_tokens: int = 220,
              temperature: float = 0.9, json_mode: bool = False) -> Optional[str]:
    """Task-based entry point. Chain me se pehla jo chal jaye wahi."""
    for slot in _chain_for(task):
        out = _try_provider(
            slot["provider"], slot["model"], system, user,
            max_tokens=max_tokens, temperature=temperature, json_mode=json_mode,
        )
        if out:
            _maybe_persist()
            return out
        logger.warning("[ROUTER] %s: %s/%s nahi chala — agla fallback",
                       task, slot["provider"], slot["model"])
    logger.error("[ROUTER] task '%s' ke liye koi model kaam nahi kar raha", task)
    _maybe_persist()
    return None


def chat(route: str, system: str, user: str, max_tokens: int = 220,
         temperature: float = 0.9, json_mode: bool = False) -> Optional[str]:
    """Legacy entry point — route ko task me map karke chain chala deta hai."""
    task = ROUTE_TO_TASK.get((route or "").lower(), "normal")
    heavy = task in ("debate", "help", "analyze")
    return chat_task(task, system, user,
                     max_tokens=max(320, max_tokens) if heavy else max_tokens,
                     temperature=temperature, json_mode=json_mode)


def chat_json(route: str, system: str, user: str = "") -> Optional[dict]:
    """JSON routes (decision/learn)."""
    task = ROUTE_TO_TASK.get((route or "").lower(), "decision")
    u = user or "Proceed. Output the JSON only."
    for attempt in (1, 2):
        raw = chat_task(task, system, u, max_tokens=400, temperature=0.1,
                        json_mode=True)
        parsed = _parse_json(raw)
        if parsed is not None:
            return parsed
        if attempt == 1:
            u += "\n\nSirf valid JSON object bhej, aur kuch nahi."
    return None


def _parse_json(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


# ------------------------------------------------------------- health


def health() -> Dict[str, Any]:
    out: Dict[str, Any] = {"model_force": _force(), "usage": get_usage(),
                           "providers": {}}
    for p in providers.PROVIDER_IDS:
        keys = key_pool.list_keys(p)
        if not keys:
            continue
        out["providers"][p] = {
            "keys": len(keys),
            "active": sum(1 for k in keys if k["status"] == "active"),
            "model": key_pool.get_model(p),
        }
    groq = out["providers"].get("groq", {})
    claude = out["providers"].get("agentrouter") or out["providers"].get("anthropic") or {}
    out["groq_keys"] = groq.get("keys", 0)
    out["groq_active"] = groq.get("active", 0)
    out["opus_keys"] = claude.get("keys", 0)
    out["opus_active"] = claude.get("active", 0)
    return out
