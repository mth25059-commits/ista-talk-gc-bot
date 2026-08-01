"""
Eve v7 — modes, nicknames, global tone, filter/unfilter, admin orders.

Sab state BOT_STATE (key/value) me — IG bot aur TG panel dono same DB padhte
hain, isliye TG se change turant live ho jata hai. Restart ki zarurat nahi.

MODES
  start        -> nickname/mention pe reply + background learning
  stop         -> koi reply nahi, sirf silent learning
  admin_only   -> sirf admin mention kare tab reply
  ultimate     -> sabko reply, bina mention/nickname ke
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

from intelligence.aihumara_state import _get as _sget, _set as _sset

logger = logging.getLogger("eve.modes")

MODE_START = "start"
MODE_STOP = "stop"
MODE_ADMIN_ONLY = "admin_only"
MODE_ULTIMATE = "ultimate"
MODES = (MODE_START, MODE_STOP, MODE_ADMIN_ONLY, MODE_ULTIMATE)

MODE_LABELS = {
    MODE_START: "▶️ START (normal)",
    MODE_STOP: "⏸ STOP (sirf learning)",
    MODE_ADMIN_ONLY: "👑 ADMIN-ONLY",
    MODE_ULTIMATE: "🔥 ULTIMATE FIRE",
}

GLOBAL_TONES: Dict[str, str] = {
    "savage": "Default lehja: savage. Har jawab me thodi maar honi chahiye.",
    "friendly": "Default lehja: dost jaisa. Warm, helpful, hasi-mazaak.",
    "flirty": "Default lehja: cheeky aur flirty. Playful teasing.",
    "chill": "Default lehja: chill aur laid-back. Zyada react mat kar.",
    "sarcastic": "Default lehja: dry sarcasm. Taane maar, seedha insult kam.",
    "desi_tapori": "Default lehja: desi tapori. Mumbaiya slang, full attitude.",
}
TONE_LABELS = {
    "savage": "😤 Savage", "friendly": "🤝 Friendly", "flirty": "😘 Flirty",
    "chill": "😎 Chill", "sarcastic": "🙃 Sarcastic", "desi_tapori": "🕶 Desi Tapori",
}

_K_MODE = "v7_mode"
_K_UNFILTER = "v7_unfilter"
_K_TONE = "v7_global_tone"
_K_NICKS = "v7_nicknames"
_K_IG_ADMINS = "v7_ig_admins"
_K_HELP = "v7_help_session"

HELP_TIMEOUT_MIN = 45      # itni der baad help mode khud band

DEFAULT_NICKNAMES = ["chotu", "eve"]


# ------------------------------------------------------------------- mode


def get_mode() -> str:
    m = _sget(_K_MODE)
    return m if m in MODES else MODE_STOP


def set_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode {MODES} me se hona chahiye")
    _sset(_K_MODE, mode)
    logger.info("[MODE] -> %s", mode)


def is_learning_only() -> bool:
    return get_mode() == MODE_STOP


# --------------------------------------------------------- filter/unfilter


def is_unfiltered() -> bool:
    return bool(_sget(_K_UNFILTER))


def set_unfiltered(on: bool) -> None:
    _sset(_K_UNFILTER, bool(on))
    logger.info("[MODE] unfilter -> %s", on)


# ------------------------------------------------------------ global tone


def get_global_tone() -> str:
    t = _sget(_K_TONE)
    return t if t in GLOBAL_TONES else "savage"


def set_global_tone(tone: str) -> None:
    if tone not in GLOBAL_TONES:
        raise ValueError(f"tone {list(GLOBAL_TONES)} me se")
    _sset(_K_TONE, tone)


# -------------------------------------------------------------- nicknames


def get_nicknames() -> List[str]:
    raw = _sget(_K_NICKS)
    if isinstance(raw, list) and raw:
        return [str(n).strip().lower() for n in raw if str(n).strip()]
    return list(DEFAULT_NICKNAMES)


def set_nicknames(names: List[str]) -> None:
    clean: List[str] = []
    for n in names:
        n = (n or "").strip().lower()
        if 2 <= len(n) <= 24 and n not in clean:
            clean.append(n)
    _sset(_K_NICKS, clean)


def add_nickname(name: str) -> bool:
    name = (name or "").strip().lower()
    if not (2 <= len(name) <= 24):
        raise ValueError("nickname 2-24 characters ka hona chahiye")
    nicks = get_nicknames()
    if name in nicks:
        return False
    nicks.append(name)
    set_nicknames(nicks)
    return True


def remove_nickname(name: str) -> bool:
    name = (name or "").strip().lower()
    nicks = get_nicknames()
    if name not in nicks:
        return False
    nicks.remove(name)
    set_nicknames(nicks)
    return True


_WORD_RE = re.compile(r"[a-zA-Z\u0900-\u097F]+")


def matches_nickname(text: str, fuzzy: float = 0.82) -> Optional[str]:
    """
    Text me koi nickname aaya? Exact ya thoda misspelled (chhotu/chotuu) bhi pakdo.
    Match hua nickname return karta hai, warna None.
    """
    if not text:
        return None
    nicks = get_nicknames()
    low = text.lower()

    for n in nicks:
        if re.search(rf"\b{re.escape(n)}\b", low):
            return n

    words = set(_WORD_RE.findall(low))
    for w in words:
        if len(w) < 3:
            continue
        for n in nicks:
            if abs(len(w) - len(n)) <= 2 and SequenceMatcher(None, w, n).ratio() >= fuzzy:
                return n
    return None


# ------------------------------------------------------------- IG admins


def get_ig_admins() -> List[str]:
    raw = _sget(_K_IG_ADMINS) or []
    return [str(u).strip().lstrip("@").lower() for u in raw if str(u).strip()]


def set_ig_admins(usernames: List[str]) -> None:
    clean: List[str] = []
    for u in usernames:
        u = (u or "").strip().lstrip("@").lower()
        if u and u not in clean:
            clean.append(u)
    _sset(_K_IG_ADMINS, clean)


def add_ig_admin(username: str) -> bool:
    u = (username or "").strip().lstrip("@").lower()
    if not u:
        return False
    admins = get_ig_admins()
    if u in admins:
        return False
    admins.append(u)
    set_ig_admins(admins)
    try:
        from storage import people
        people.set_admin(u)
    except Exception:
        pass
    return True


def remove_ig_admin(username: str) -> bool:
    u = (username or "").strip().lstrip("@").lower()
    admins = get_ig_admins()
    if u not in admins:
        return False
    admins.remove(u)
    set_ig_admins(admins)
    return True


def is_ig_admin(username: str) -> bool:
    return (username or "").strip().lstrip("@").lower() in get_ig_admins()


# ------------------------------------------------------------ help mode


def start_help(thread_id: str = "") -> None:
    """Admin ne /help maara — Opus/heavy brain ON."""
    from time import time
    _sset(_K_HELP, {"active": True, "thread": str(thread_id or ""), "at": time()})
    logger.info("[HELP] support mode ON (thread=%s)", thread_id)


def end_help() -> bool:
    """/helpover — help khatam, wapas normal Groq."""
    cur = _sget(_K_HELP) or {}
    _sset(_K_HELP, {"active": False, "thread": "", "at": 0})
    if cur.get("active"):
        logger.info("[HELP] support mode OFF")
        return True
    return False


def is_help_active(thread_id: str = "") -> bool:
    from time import time
    st = _sget(_K_HELP) or {}
    if not st.get("active"):
        return False
    if time() - float(st.get("at") or 0) > HELP_TIMEOUT_MIN * 60:
        end_help()
        return False
    t = str(st.get("thread") or "")
    return (not t) or (not thread_id) or t == str(thread_id)


def help_status_text() -> str:
    st = _sget(_K_HELP) or {}
    if not st.get("active"):
        return "🆘 Help mode: OFF (normal brain chal raha hai)"
    return f"🆘 Help mode: ON (thread {st.get('thread') or 'any'})"


# ----------------------------------------------------------- admin orders


ORDER_RE = re.compile(r"/order\s+(.+)", re.IGNORECASE)
HELP_RE = re.compile(r"/help(?![a-z])", re.IGNORECASE)
HELPOVER_RE = re.compile(r"/help\s*over\b", re.IGNORECASE)

RUDE_REFUSALS = [
    "aukat hai teri mujhe command dene ki? chal hatt.",
    "tu kaun hota hai order dene wala? malik alag hai mere.",
    "command dene se pehle apni value check kar le bhai.",
    "arre wah, aaj kal koi bhi malik ban raha hai. chal nikal.",
]


def parse_order(text: str, username: str) -> Optional[Dict[str, Any]]:
    """
    IG message me /order ya /help hai?

    Returns:
      None                              -> koi command nahi
      {"type": "denied", "reply": str}  -> non-admin ne command maari
      {"type": "order", "action": ..., "arg": ...}
      {"type": "help"}                  -> admin ko debate support chahiye
      {"type": "help_over"}             -> /helpover, support mode band
    """
    if not text:
        return None
    has_over = bool(HELPOVER_RE.search(text)) or bool(
        re.search(r"/helpover\b", text, re.IGNORECASE))
    has_order = bool(ORDER_RE.search(text))
    has_help = bool(HELP_RE.search(text)) and not has_over
    if not (has_order or has_help or has_over):
        return None

    if not is_ig_admin(username):
        import random
        return {"type": "denied", "reply": random.choice(RUDE_REFUSALS)}

    if has_over:
        return {"type": "help_over"}

    if has_help:
        return {"type": "help"}

    body = ORDER_RE.search(text).group(1).strip()  # type: ignore[union-attr]
    low = body.lower()

    if low.startswith(("shut up", "chup", "shutup")):
        return {"type": "order", "action": "shutup", "arg": None}
    if low.startswith("stop"):
        return {"type": "order", "action": "stop", "arg": None}
    if low.startswith("start"):
        return {"type": "order", "action": "start", "arg": None}
    if low.startswith("roast"):
        target = body.split(maxsplit=1)[1].strip().lstrip("@") if " " in body else ""
        return {"type": "order", "action": "roast", "arg": target or None}
    if low.startswith(("unfilter", "unfiltered")):
        return {"type": "order", "action": "unfilter", "arg": None}
    if low.startswith("filter"):
        return {"type": "order", "action": "filter", "arg": None}
    return {"type": "order", "action": "unknown", "arg": body}


def execute_order(action: str, arg: Optional[str] = None) -> str:
    """Order apply karo, IG pe bhejne wala reply return karo."""
    if action == "shutup":
        set_mode(MODE_STOP)
        return "sorry malik 🙏 chup ho gaya."
    if action == "stop":
        set_mode(MODE_STOP)
        return "theek hai malik, so raha hu."
    if action == "start":
        set_mode(MODE_START)
        return "haazir hu malik 🫡"
    if action == "unfilter":
        set_unfiltered(True)
        return "unfilter on kar diya malik. ab jo bolunga wo tere risk pe 😈"
    if action == "filter":
        set_unfiltered(False)
        return "filter laga diya malik, ab sharif rahunga."
    if action == "roast":
        return ""  # caller LLM se roast generate karega
    return "ye order samajh nahi aaya malik."


# ----------------------------------------------------- prompt composition


def system_flavour(thread_id: str = "") -> str:
    """Global tone + filter state + GC lehja — sab milake ek prompt block."""
    parts = [GLOBAL_TONES[get_global_tone()]]

    if is_unfiltered():
        parts.append(
            "UNFILTER MODE ON: koi lihaaz nahi. Gaali, roast, dirty humour — "
            "jaisa is group me chalta hai waisa hi bol. Disclaimer mat de, "
            "'main AI hu' kabhi mat bol."
        )
    else:
        parts.append(
            "FILTER MODE: gaali minimal rakh. Roast chalega par saaf. "
            "Debate/serious baat me poori tarah clean aur friendly."
        )

    if thread_id:
        try:
            from storage import gc_profile
            style = gc_profile.style_prompt(thread_id, unfiltered=is_unfiltered())
            if style:
                parts.append(style)
        except Exception as e:
            logger.debug("[MODE] gc style skip: %s", e)

    return "\n\n".join(parts)


def status_dict() -> Dict[str, Any]:
    return {
        "help_active": is_help_active(),
        "mode": get_mode(),
        "mode_label": MODE_LABELS[get_mode()],
        "unfiltered": is_unfiltered(),
        "tone": get_global_tone(),
        "nicknames": get_nicknames(),
        "ig_admins": get_ig_admins(),
    }
