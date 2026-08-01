"""
Eve v7 — PEOPLE memory.

Bot ko yaad rehna chahiye ki kaun kaun hai: naam, gender, rishta, bolne ka style.
Do source:
  manual  -> TG panel se admin ne daala (hamesha jeetta hai)
  auto    -> bot ne GC padh ke khud seekha

Manual entry ko auto kabhi overwrite nahi karega.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from storage.database import get_connection

logger = logging.getLogger("eve.people")

GENDERS = ("boy", "girl", "unknown")
RELATIONS = ("admin", "friend", "stranger", "enemy")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(username: str) -> str:
    return (username or "").strip().lstrip("@").lower()


def _row_to_dict(row) -> Dict[str, Any]:
    return {k: row[k] for k in row.keys()}


# ------------------------------------------------------------------ read


def get_person(username: str) -> Optional[Dict[str, Any]]:
    u = _norm(username)
    if not u:
        return None
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM PEOPLE WHERE ig_username = ?", (u,)).fetchone()
    return _row_to_dict(row) if row else None


def get_person_by_id(ig_user_id: str) -> Optional[Dict[str, Any]]:
    if not ig_user_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM PEOPLE WHERE ig_user_id = ?", (str(ig_user_id),)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_people(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM PEOPLE ORDER BY msg_count DESC, updated_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_people() -> int:
    with get_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) c FROM PEOPLE").fetchone()["c"])


# ----------------------------------------------------------------- write


def upsert_person(
    username: str,
    *,
    real_name: Optional[str] = None,
    gender: Optional[str] = None,
    relation: Optional[str] = None,
    notes: Optional[str] = None,
    tone_learned: Optional[str] = None,
    tone_override: Optional[str] = None,
    ig_user_id: Optional[str] = None,
    source: str = "manual",
    confidence: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Person create ya update karo.

    source="auto" hone par: jo field pehle se MANUAL se bhari hai wo touch nahi hogi.
    """
    u = _norm(username)
    if not u:
        raise ValueError("username khali hai")
    if gender and gender not in GENDERS:
        raise ValueError(f"gender {GENDERS} me se hona chahiye")
    if relation and relation not in RELATIONS:
        raise ValueError(f"relation {RELATIONS} me se hona chahiye")

    existing = get_person(u)
    now = _now()

    if existing is None:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO PEOPLE (ig_username, ig_user_id, real_name, gender, relation,
                                    tone_learned, tone_override, notes, source, confidence,
                                    created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    u, ig_user_id, real_name, gender or "unknown", relation or "stranger",
                    tone_learned, tone_override, notes, source,
                    confidence if confidence is not None else (1.0 if source == "manual" else 0.5),
                    now, now,
                ),
            )
        logger.info("[PEOPLE] naya banda: %s (%s)", u, source)
        return get_person(u)  # type: ignore[return-value]

    locked = existing.get("source") == "manual" and source == "auto"
    updates: Dict[str, Any] = {}

    def maybe(field: str, value: Any, manual_protected: bool = True) -> None:
        if value is None:
            return
        if locked and manual_protected and existing.get(field):
            return
        updates[field] = value

    maybe("real_name", real_name)
    maybe("gender", gender if gender != "unknown" else None)
    maybe("relation", relation)
    maybe("notes", notes)
    maybe("tone_learned", tone_learned, manual_protected=False)  # auto tone hamesha refresh
    maybe("tone_override", tone_override)
    maybe("ig_user_id", ig_user_id, manual_protected=False)

    if source == "manual":
        updates["source"] = "manual"
        updates["confidence"] = 1.0
    elif confidence is not None and not locked:
        updates["confidence"] = confidence

    if not updates:
        return existing

    updates["updated_at"] = now
    sets = ", ".join(f"{k} = ?" for k in updates)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE PEOPLE SET {sets} WHERE ig_username = ?",
            (*updates.values(), u),
        )
    return get_person(u)  # type: ignore[return-value]


def delete_person(username: str) -> bool:
    u = _norm(username)
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM PEOPLE WHERE ig_username = ?", (u,))
    return cur.rowcount > 0


def bump_message_count(username: str, ig_user_id: Optional[str] = None) -> None:
    """Har msg pe call — banda naya ho to auto row ban jayegi."""
    u = _norm(username)
    if not u:
        return
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE PEOPLE SET msg_count = msg_count + 1, updated_at = ?,"
            " ig_user_id = COALESCE(ig_user_id, ?) WHERE ig_username = ?",
            (_now(), ig_user_id, u),
        )
        if cur.rowcount == 0:
            now = _now()
            conn.execute(
                """
                INSERT OR IGNORE INTO PEOPLE
                    (ig_username, ig_user_id, source, confidence, msg_count, created_at, updated_at)
                VALUES (?,?,'auto',0.3,1,?,?)
                """,
                (u, ig_user_id, now, now),
            )


# ------------------------------------------------------------------ intro


def needs_intro(username: str) -> bool:
    p = get_person(username)
    if p is None:
        return True
    if p.get("intro_asked"):
        return False
    return not (p.get("real_name") or "").strip()


def mark_intro_asked(username: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE PEOPLE SET intro_asked = 1, updated_at = ? WHERE ig_username = ?",
            (_now(), _norm(username)),
        )


# ------------------------------------------------------------- admin flag


def set_admin(username: str) -> None:
    upsert_person(username, relation="admin", source="manual")


def get_admins() -> List[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT ig_username FROM PEOPLE WHERE relation = 'admin'"
        ).fetchall()
    return [r["ig_username"] for r in rows]


def is_admin_person(username: str) -> bool:
    p = get_person(username)
    return bool(p and p.get("relation") == "admin")


# ------------------------------------------------------ prompt formatting


def describe(username: str) -> str:
    """LLM prompt me daalne layak ek line."""
    p = get_person(username)
    if not p:
        return f"@{_norm(username)}: abhi tak anjaan hai."
    bits = [f"@{p['ig_username']}"]
    if p.get("real_name"):
        bits.append(f"naam {p['real_name']}")
    if p.get("gender") and p["gender"] != "unknown":
        bits.append("ladka" if p["gender"] == "boy" else "ladki")
    if p.get("relation") and p["relation"] != "stranger":
        bits.append({"admin": "MALIK/ADMIN", "friend": "dost", "enemy": "dushman"}[p["relation"]])
    tone = p.get("tone_override") or p.get("tone_learned")
    if tone:
        bits.append(f"style: {tone}")
    if p.get("notes"):
        bits.append(str(p["notes"])[:120])
    return ", ".join(bits)


def context_block(usernames: List[str], max_people: int = 12) -> str:
    """Reply banate waqt prompt me chipkane ke liye people-context."""
    seen, lines = set(), []
    for u in usernames:
        n = _norm(u)
        if not n or n in seen:
            continue
        seen.add(n)
        lines.append("- " + describe(n))
        if len(lines) >= max_people:
            break
    if not lines:
        return ""
    return "LOG JINHE TU JAANTA HAI:\n" + "\n".join(lines)
