"""
Eve v7 — smart model preferences (task -> model chain).

Admin TG se decide karta hai ki kaunsa kaam kaunsa brain karega, aur agar wo
brain fail ho jaye to kaun sambhalega. Har task ke liye ek CHAIN hoti hai:

    debate: [agentrouter/claude-opus-4-6, anthropic/claude-opus-4-8, groq/llama-3.3-70b]
             ^ primary                     ^ fallback 1               ^ fallback 2

Router upar se neeche try karta hai — pehla jo jawab de de, wahi chalta hai.

Agar admin ne kuch set nahi kiya, to `auto_chain()` khud available keys dekh
kar sabse sahi model chun leta hai (heavy kaam -> Claude/Grok, normal baat ->
Groq kyunki wo sabse tez hai).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from intelligence import key_pool, providers
from intelligence.aihumara_state import _get as _sget, _set as _sset

logger = logging.getLogger("eve.prefs")

_K_PREFS = "v7_model_prefs"

# task -> (label, description, heavy?)
TASKS: Dict[str, Tuple[str, str, bool]] = {
    "normal": ("💬 Normal baat", "GC ki aam baat-cheet, banter", False),
    "roast": ("🔥 Roast", "kisi ki band bajani ho", False),
    "flirt": ("😘 Flirt", "flirty / dirty tone wale reply", False),
    "debate": ("⚔️ Debate", "political ya serious bahas", True),
    "help": ("🆘 Admin /help", "malik ko debate me support", True),
    "analyze": ("🧪 Analyze", "GC padhna, samajhna, facts", True),
    "decision": ("⚡ Decision", "reply du ya nahi (JSON, super fast)", False),
    "learn": ("📚 Learning", "memory extract karna (JSON)", False),
}

TASK_ORDER = ("normal", "roast", "flirt", "debate", "help", "analyze",
              "decision", "learn")

# heavy kaam ke liye pasand ka order, aur halke kaam ke liye alag
_HEAVY_ORDER = ("agentrouter", "anthropic", "xai", "gemini", "groq")
_LIGHT_ORDER = ("groq", "gemini", "xai", "agentrouter", "anthropic")

MAX_SLOTS = 4


def _all() -> Dict[str, List[Dict[str, str]]]:
    raw = _sget(_K_PREFS)
    return raw if isinstance(raw, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    _sset(_K_PREFS, data)


def _valid_task(task: str) -> str:
    t = (task or "").strip().lower()
    if t not in TASKS:
        raise ValueError(f"task {list(TASKS)} me se hona chahiye")
    return t


# ------------------------------------------------------- live availability


def available_providers() -> List[str]:
    """Jin providers ki kam se kam ek active key hai."""
    out = []
    for p in providers.PROVIDER_IDS:
        keys = key_pool.list_keys(p)
        if any(k["status"] != "dead" for k in keys):
            out.append(p)
    return out


def _model_for(provider: str) -> str:
    """Us provider ki keys pe jo model set hai, warna default."""
    for k in key_pool.list_keys(provider):
        if k["status"] != "dead" and k.get("model"):
            return str(k["model"])
    return providers.default_model(provider)


def auto_chain(task: str) -> List[Dict[str, str]]:
    """Admin ne set nahi kiya -> available keys dekh kar khud chain banao."""
    t = _valid_task(task)
    heavy = TASKS[t][2]
    order = _HEAVY_ORDER if heavy else _LIGHT_ORDER
    avail = available_providers()
    chain = [{"provider": p, "model": _model_for(p)} for p in order if p in avail]
    return chain[:MAX_SLOTS]


def get_chain(task: str, *, resolved: bool = True) -> List[Dict[str, str]]:
    """
    Task ki chain. resolved=True pe dead/hataye gaye providers filter ho jate
    hain aur khali reh jaye to auto chain lag jati hai.
    """
    t = _valid_task(task)
    chain = [c for c in _all().get(t, []) if c.get("provider") in providers.PROVIDERS]

    if not resolved:
        return chain

    avail = available_providers()
    live = [c for c in chain if c["provider"] in avail]

    # admin ki pasand pehle, uske baad auto-fallback (taaki kabhi silent na ho)
    seen = {c["provider"] for c in live}
    for extra in auto_chain(t):
        if extra["provider"] not in seen:
            live.append(extra)
            seen.add(extra["provider"])
    return live[:MAX_SLOTS]


def set_chain(task: str, chain: List[Dict[str, str]]) -> None:
    t = _valid_task(task)
    clean: List[Dict[str, str]] = []
    for c in chain:
        p = (c.get("provider") or "").lower()
        if p not in providers.PROVIDERS:
            continue
        m = (c.get("model") or "").strip() or _model_for(p)
        if any(x["provider"] == p for x in clean):
            continue
        clean.append({"provider": p, "model": m})
    data = _all()
    data[t] = clean[:MAX_SLOTS]
    _save(data)
    logger.info("[PREFS] %s -> %s", t, clean)


def set_primary(task: str, provider: str, model: Optional[str] = None) -> None:
    """Primary set karo, purani chain neeche fallback ban jati hai."""
    t = _valid_task(task)
    p = provider.lower()
    m = (model or "").strip() or _model_for(p)
    old = [c for c in get_chain(t, resolved=False) if c["provider"] != p]
    set_chain(t, [{"provider": p, "model": m}] + old)


def add_fallback(task: str, provider: str, model: Optional[str] = None) -> None:
    t = _valid_task(task)
    p = provider.lower()
    chain = get_chain(t, resolved=False)
    if any(c["provider"] == p for c in chain):
        return
    chain.append({"provider": p, "model": (model or "").strip() or _model_for(p)})
    set_chain(t, chain)


def remove_provider(task: str, provider: str) -> None:
    t = _valid_task(task)
    set_chain(t, [c for c in get_chain(t, resolved=False)
                  if c["provider"] != provider.lower()])


def reset(task: Optional[str] = None) -> None:
    if task is None:
        _save({})
        return
    data = _all()
    data.pop(_valid_task(task), None)
    _save(data)


def auto_configure() -> Dict[str, List[Dict[str, str]]]:
    """Sab tasks ko available keys ke hisaab se smart default pe set kar do."""
    out = {}
    for t in TASK_ORDER:
        chain = auto_chain(t)
        if chain:
            set_chain(t, chain)
            out[t] = chain
    logger.info("[PREFS] auto-configured %d tasks", len(out))
    return out


# ------------------------------------------------------------ description


def describe(task: str) -> str:
    t = _valid_task(task)
    label, desc, _heavy = TASKS[t]
    custom = {c["provider"] for c in get_chain(t, resolved=False)}
    lines = [f"{label} — {desc}"]
    for i, c in enumerate(get_chain(t)):
        tag = "PRIMARY" if i == 0 else f"fallback {i}"
        star = "" if c["provider"] in custom else " (auto)"
        lines.append(f"  {i + 1}. [{tag}] {providers.label(c['provider'])}"
                     f" · {c['model']}{star}")
    if len(lines) == 1:
        lines.append("  ⚠️ koi key hi nahi — pehle API key add kar.")
    return "\n".join(lines)


def status_text() -> str:
    avail = available_providers()
    head = ("🧠 SMART MODEL PREFERENCES\n"
            f"Live providers: {', '.join(providers.label(p) for p in avail) or 'koi nahi'}\n"
            "Upar wala model pehle try hota hai, fail ho to neeche wala.\n")
    return head + "\n" + "\n\n".join(describe(t) for t in TASK_ORDER)
