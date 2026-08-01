"""
Eve v7 — GC tone learning.

Har group ka apna lehja hota hai. Bot har N messages pe us GC ka profile
recompute karta hai (gali level, flirty level, slang, msg length) aur reply
generate karte waqt usi lehje me bolta hai.

Ye pura LOCAL hai — koi LLM call nahi, isliye free aur instant.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from storage.database import get_connection

logger = logging.getLogger("eve.gcprofile")

RELEARN_EVERY = 200          # itne naye msgs ke baad profile refresh
LEARN_WINDOW = 600           # kitne recent msgs padhke seekhe

# Hinglish/Hindi gaali + toxic markers (roman + devanagari)
_GALI = {
    "bc", "mc", "bhosdi", "bhosdike", "bkl", "chutiy", "chutia", "gandu", "gaand",
    "lund", "lawde", "lodu", "randi", "harami", "kutte", "kamine", "madarchod",
    "behenchod", "jhaat", "tatti", "fuck", "fucking", "bitch", "asshole", "dick",
    "बहनचोद", "मादरचोद", "भोसड", "चूतिय", "गांड", "रंडी", "हरामी",
}
_FLIRT = {
    "cute", "jaan", "baby", "babu", "shona", "pyaar", "love", "kiss", "hot",
    "gf", "bf", "date", "crush", "propose", "sexy", "muah", "😘", "❤️", "😍", "🥰",
}
_FRIENDLY = {
    "bhai", "yaar", "bro", "dost", "haha", "lol", "lmao", "😂", "🤣", "thanks",
    "sorry", "gm", "gn", "bhaiya", "dude",
}
_TOXIC = {
    "hate", "kill", "mar ja", "block", "report", "ignore", "cringe", "flop",
    "loser", "pagal", "bewakoof", "chup", "shutup", "shut up",
}

_STOPWORDS = {
    "the", "and", "you", "for", "are", "but", "not", "with", "this", "that",
    "hai", "hain", "nahi", "kya", "koi", "mai", "main", "mera", "tera", "hoga",
    "kar", "karo", "raha", "rahi", "gaya", "toh", "bhi", "aur", "kuch", "abhi",
}

_TOKEN_RE = re.compile(r"[a-zA-Z\u0900-\u097F]{3,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(v: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, v))


def _hits(text: str, vocab) -> int:
    low = text.lower()
    return sum(1 for w in vocab if w in low)


# ---------------------------------------------------------------- compute


def learn(thread_id: str, title: Optional[str] = None, force: bool = False) -> Optional[Dict[str, Any]]:
    """
    GC ke recent messages padh ke profile banao.
    force=False -> sirf tab chalega jab RELEARN_EVERY naye msgs aa chuke ho.
    """
    if not thread_id:
        return None

    with get_connection() as conn:
        prof = conn.execute(
            "SELECT * FROM GC_PROFILE WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        total = int(
            conn.execute(
                "SELECT COUNT(*) c FROM MESSAGES WHERE thread_id = ?", (thread_id,)
            ).fetchone()["c"]
        )
        if not force and prof is not None:
            if total - int(prof["sample_count"] or 0) < RELEARN_EVERY:
                return {k: prof[k] for k in prof.keys()}

        rows = conn.execute(
            "SELECT text FROM MESSAGES WHERE thread_id = ? AND text IS NOT NULL AND text != ''"
            " ORDER BY timestamp DESC LIMIT ?",
            (thread_id, LEARN_WINDOW),
        ).fetchall()

    texts = [r["text"] for r in rows if r["text"]]
    if not texts:
        return None

    n = len(texts)
    gali = sum(_hits(t, _GALI) for t in texts)
    flirt = sum(_hits(t, _FLIRT) for t in texts)
    friendly = sum(_hits(t, _FRIENDLY) for t in texts)
    toxic = sum(_hits(t, _TOXIC) for t in texts)
    avg_len = sum(len(t) for t in texts) / n

    # per-message hit rate -> 0-10 scale (rate 0.5 = max)
    def scale(hits: int) -> float:
        return _clamp((hits / n) * 20.0)

    counter: Counter = Counter()
    for t in texts:
        for tok in _TOKEN_RE.findall(t.lower()):
            if tok not in _STOPWORDS:
                counter[tok] += 1
    slang = [w for w, c in counter.most_common(60) if c >= 3][:40]

    data = {
        "thread_id": thread_id,
        "title": title,
        "gali_level": round(scale(gali), 2),
        "flirty_level": round(scale(flirt), 2),
        "friendly_level": round(_clamp(scale(friendly), 1.0), 2),
        "toxic_level": round(scale(toxic), 2),
        "avg_msg_len": round(avg_len, 1),
        "slang_json": json.dumps(slang, ensure_ascii=False),
        "sample_count": total,
        "last_learned_at": _now(),
        "updated_at": _now(),
    }

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO GC_PROFILE (thread_id, title, gali_level, flirty_level, friendly_level,
                                    toxic_level, avg_msg_len, slang_json, sample_count,
                                    last_learned_at, updated_at)
            VALUES (:thread_id,:title,:gali_level,:flirty_level,:friendly_level,:toxic_level,
                    :avg_msg_len,:slang_json,:sample_count,:last_learned_at,:updated_at)
            ON CONFLICT(thread_id) DO UPDATE SET
                title           = COALESCE(excluded.title, GC_PROFILE.title),
                gali_level      = excluded.gali_level,
                flirty_level    = excluded.flirty_level,
                friendly_level  = excluded.friendly_level,
                toxic_level     = excluded.toxic_level,
                avg_msg_len     = excluded.avg_msg_len,
                slang_json      = excluded.slang_json,
                sample_count    = excluded.sample_count,
                last_learned_at = excluded.last_learned_at,
                updated_at      = excluded.updated_at
            """,
            data,
        )
    logger.info(
        "[GC] %s seekha — gali %.1f flirty %.1f friendly %.1f (%d msgs)",
        thread_id, data["gali_level"], data["flirty_level"], data["friendly_level"], n,
    )
    return data


# ------------------------------------------------------------------- read


def get_profile(thread_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM GC_PROFILE WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def list_profiles() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM GC_PROFILE ORDER BY sample_count DESC"
        ).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def style_prompt(thread_id: str, unfiltered: bool = False) -> str:
    """LLM system prompt me chipkane wala GC-style guidance."""
    p = get_profile(thread_id)
    if not p:
        return ""

    gali = float(p["gali_level"] or 0)
    flirty = float(p["flirty_level"] or 0)
    avg_len = float(p["avg_msg_len"] or 40)

    lines = ["IS GROUP KA LEHJA (yahi copy karna hai):"]

    if not unfiltered:
        gali = min(gali, 3.0)

    if gali >= 6:
        lines.append("- Yahan khul ke gaali chalti hai. Tu bhi wahi level rakh, natural lage.")
    elif gali >= 3:
        lines.append("- Halki-fulki gaali chalti hai. Kabhi kabhi daal, har line me nahi.")
    else:
        lines.append("- Gaali kam. Saaf bol, roast bina gaali ke kar.")

    if flirty >= 5:
        lines.append("- Flirty/masti wala mahaul hai. Thoda cheeky reh.")
    if float(p["toxic_level"] or 0) >= 5:
        lines.append("- Log tez aur taane maarte hain. Soft mat pad.")

    if avg_len <= 30:
        lines.append("- Sab chhote msg bhejte hain. Tu bhi 1 line me nipta.")
    elif avg_len <= 80:
        lines.append("- 1-2 line ka reply theek hai.")
    else:
        lines.append("- Yahan thoda lamba likhte hain, par 3 line se upar mat ja.")

    try:
        slang = json.loads(p["slang_json"] or "[]")[:14]
    except Exception:
        slang = []
    if slang:
        lines.append("- Inke common words: " + ", ".join(slang))

    return "\n".join(lines)
