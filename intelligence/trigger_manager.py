"""
Eve v7 — trigger system.

TG panel se koi IG username + tone set karo. Uske baad jab bhi wo banda
GC me kuch bhi bole, bot fixed tone me reply karega — bina mention ke.

Tones aur unke prompt instructions yahi define hote hain.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from storage.database import get_connection

logger = logging.getLogger("eve.trigger")

TONES: Dict[str, str] = {
    "roast": (
        "Is bande ko roast kar. Short, tez, savage line. Gaali optional, "
        "par insult crisp hona chahiye. Ek-do line se zyada nahi."
    ),
    "dirty": (
        "Is bande se double-meaning, dirty-funny andaaz me baat kar. "
        "Adult humour chalega, par sirf tanz — koi explicit description nahi."
    ),
    "flirty": (
        "Is bande se flirty aur cheeky baat kar. Teasing, thoda charm, "
        "thoda mazaak. Creepy nahi — playful."
    ),
    "abusive_roast": (
        "Is bande ko bina filter ke gaali-mixed roast maar. Desi gaali chalegi, "
        "line chhoti aur maar wali honi chahiye."
    ),
    "friendly": (
        "Is bande se dost jaisi warm baat kar. Support kar, hasi-mazaak kar, "
        "koi insult nahi."
    ),
    "sarcastic": (
        "Is bande ko dry sarcasm se jawab de. Taane, ulta compliment, "
        "seedha insult nahi — subtle katai."
    ),
}

TONE_LABELS = {
    "roast": "🔥 Roast",
    "dirty": "😏 Dirty",
    "flirty": "😘 Flirty",
    "abusive_roast": "💀 Abusive Roast",
    "friendly": "🤝 Friendly",
    "sarcastic": "🙃 Sarcastic",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(u: str) -> str:
    return (u or "").strip().lstrip("@").lower()


def set_trigger(username: str, tone: str) -> Dict[str, Any]:
    u = _norm(username)
    tone = (tone or "").strip().lower()
    if not u:
        raise ValueError("username khali hai")
    if tone not in TONES:
        raise ValueError(f"tone in me se: {', '.join(TONES)}")
    now = _now()
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO TRIGGERS (ig_username, tone, active, created_at, updated_at)
            VALUES (?,?,1,?,?)
            ON CONFLICT(ig_username) DO UPDATE SET
                tone = excluded.tone, active = 1, updated_at = excluded.updated_at
            """,
            (u, tone, now, now),
        )
    logger.info("[TRIGGER] %s -> %s", u, tone)
    return {"ig_username": u, "tone": tone, "active": 1}


def remove_trigger(username: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM TRIGGERS WHERE ig_username = ?", (_norm(username),))
    return cur.rowcount > 0


def toggle_trigger(username: str, active: bool) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE TRIGGERS SET active = ?, updated_at = ? WHERE ig_username = ?",
            (1 if active else 0, _now(), _norm(username)),
        )
    return cur.rowcount > 0


def disable_all() -> int:
    with get_connection() as conn:
        cur = conn.execute("UPDATE TRIGGERS SET active = 0, updated_at = ?", (_now(),))
    return cur.rowcount


def get_trigger(username: str) -> Optional[Dict[str, Any]]:
    u = _norm(username)
    if not u:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM TRIGGERS WHERE ig_username = ? AND active = 1", (u,)
        ).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def list_triggers(include_inactive: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM TRIGGERS"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY updated_at DESC"
    with get_connection() as conn:
        rows = conn.execute(sql).fetchall()
    return [{k: r[k] for k in r.keys()} for r in rows]


def bump_hit(username: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE TRIGGERS SET hit_count = hit_count + 1 WHERE ig_username = ?",
            (_norm(username),),
        )


def tone_instruction(tone: str) -> str:
    return TONES.get((tone or "").lower(), "")


def prompt_for(username: str) -> str:
    """Agar trigger laga hai to LLM ke liye extra instruction, warna khali string."""
    t = get_trigger(username)
    if not t:
        return ""
    bump_hit(username)
    return f"TRIGGER ACTIVE (@{t['ig_username']}): {tone_instruction(t['tone'])}"


def status_text() -> str:
    rows = list_triggers()
    if not rows:
        return "Koi trigger set nahi hai."
    out = ["🎯 TRIGGERS:"]
    for r in rows:
        icon = "🟢" if r["active"] else "⚪"
        out.append(
            f"{icon} @{r['ig_username']} — {TONE_LABELS.get(r['tone'], r['tone'])}"
            f" (hits: {r['hit_count']})"
        )
    return "\n".join(out)
