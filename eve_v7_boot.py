"""
Eve v7 — boot + integration hooks.

Ye ek jagah hai jahan se v7 ka sab kuch on hota hai. `main.py` me sirf do
line chahiye:

    from eve_v7_boot import boot_v7, on_incoming_message, build_reply_context
    boot_v7()

Aur message pipeline me (jahan pehle social_judge chalta tha):

    ctx = build_reply_context(text=..., username=..., thread_id=..., ...)
    if not ctx["should_reply"]:
        return
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("eve.boot7")

_booted = False


def boot_v7(restore_from_drive: bool = True, start_drive_sync: bool = True) -> Dict[str, Any]:
    """
    Sabse pehle call karo — init_db() se bhi pehle agar Drive restore chahiye.
    Idempotent hai.
    """
    global _booted
    result: Dict[str, Any] = {}

    if _booted:
        return {"already": True}

    # 1) Naya VPS? Drive se brain wapas lao (DB open karne se PEHLE)
    if restore_from_drive:
        try:
            from storage import drive_sync
            result["restore"] = drive_sync.boot_restore()
        except Exception as e:
            logger.warning("[BOOT] drive restore skip: %s", e)
            result["restore"] = {"ok": False, "error": str(e)}

    # 2) Schema
    from storage.database import init_db
    from storage.schema_v7 import ensure_v7_schema
    init_db()
    ensure_v7_schema()
    result["schema"] = "ok"

    # 3) .env ki purani keys pool me
    from intelligence import key_pool
    result["keys_imported"] = key_pool.seed_from_env()

    # 4) Background Drive sync
    if start_drive_sync:
        try:
            from storage import drive_sync
            result["autosync"] = drive_sync.start_autosync()
        except Exception as e:
            logger.warning("[BOOT] autosync skip: %s", e)
            result["autosync"] = False

    _booted = True
    logger.info("[BOOT] Eve v7 ready: %s", result)
    return result


def shutdown_v7() -> None:
    """Clean shutdown — aakhri backup Drive pe push karke jao."""
    try:
        from storage import drive_sync
        drive_sync.stop_autosync()
        if drive_sync.is_configured():
            drive_sync.upload_snapshot()
            logger.info("[BOOT] final backup Drive pe chala gaya")
    except Exception as e:
        logger.warning("[BOOT] shutdown backup fail: %s", e)


# ------------------------------------------------------- message hooks


def on_incoming_message(
    *,
    username: str,
    text: str,
    thread_id: str = "",
    ig_user_id: Optional[str] = None,
    thread_title: Optional[str] = None,
) -> None:
    """
    HAR incoming message pe call karo — reply de rahe ho ya nahi, farak nahi.
    Yahi 'silent learning' hai jo STOP mode me bhi chalta rehta hai.
    """
    try:
        from storage import gc_profile, people
        people.bump_message_count(username, ig_user_id)
        if thread_id:
            gc_profile.learn(thread_id, title=thread_title, force=False)
    except Exception as e:
        logger.debug("[LEARN] skip: %s", e)


def build_reply_context(
    *,
    text: str,
    username: str,
    thread_id: str = "",
    bot_username: str = "",
    recent_texts: Optional[List[str]] = None,
    recent_usernames: Optional[List[str]] = None,
    is_new_member: bool = False,
) -> Dict[str, Any]:
    """
    Ek call me sab: reply dena hai ya nahi, kis model se, aur system prompt
    me kya-kya extra chipkana hai.

    Returns:
      {
        "should_reply": bool,
        "reason": str,
        "route": "banter" | "analyze",
        "system_extra": str,     # system prompt me append karo
        "canned_reply": str|None # ye set hai to LLM call mat karo, seedha bhej
        "tags": [...],
      }
    """
    from intelligence import eve_modes, reply_policy
    from storage import people

    d = reply_policy.decide(
        text=text,
        username=username,
        thread_id=thread_id,
        bot_username=bot_username,
        recent_texts=recent_texts,
        is_new_member=is_new_member,
    )

    if not d.should_reply:
        return {
            "should_reply": False, "reason": d.reason, "route": "banter",
            "system_extra": "", "canned_reply": None, "tags": d.tags,
        }

    blocks: List[str] = [eve_modes.system_flavour(thread_id)]

    who = list(recent_usernames or [])
    if username not in who:
        who.insert(0, username)
    ppl = people.context_block(who)
    if ppl:
        blocks.append(ppl)

    admins = eve_modes.get_ig_admins()
    if admins:
        names = []
        for a in admins:
            p = people.get_person(a) or {}
            names.append(f"@{a}" + (f" ({p['real_name']})" if p.get("real_name") else ""))
        blocks.append(
            "TERA MALIK / ADMIN: " + ", ".join(names) +
            ". Inki baat manni hai, inka side lena hai, inke against koi bole to "
            "chhodna nahi."
        )

    if d.extra_prompt:
        blocks.append(d.extra_prompt)

    return {
        "should_reply": True,
        "reason": d.reason,
        "route": d.route,
        "system_extra": "\n\n".join(b for b in blocks if b),
        "canned_reply": d.canned_reply,
        "tags": d.tags,
    }
