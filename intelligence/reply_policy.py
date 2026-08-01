"""
Eve v7 — reply policy.

Ek jagah pe decide hota hai: is message ka jawab dena hai ya nahi, aur
agar dena hai to kis andaaz + kis model se.

Ye pura deterministic hai (koi LLM call nahi) — isliye instant.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from intelligence import debate_detector, eve_modes, trigger_manager
from storage import people

logger = logging.getLogger("eve.policy")


@dataclass
class Decision:
    should_reply: bool
    reason: str
    route: str = "banter"              # banter | analyze
    extra_prompt: str = ""
    ask_intro: bool = False
    canned_reply: Optional[str] = None  # LLM bypass (order ack, refusal)
    order_action: Optional[str] = None
    tags: List[str] = field(default_factory=list)


def _mentions_bot(text: str, bot_username: str) -> bool:
    if not bot_username:
        return False
    return bool(re.search(rf"@{re.escape(bot_username.lower())}\b", (text or "").lower()))


def decide(
    *,
    text: str,
    username: str,
    thread_id: str = "",
    bot_username: str = "",
    recent_texts: Optional[List[str]] = None,
    is_new_member: bool = False,
) -> Decision:
    text = text or ""
    username = (username or "").strip().lstrip("@").lower()
    mode = eve_modes.get_mode()
    admin = eve_modes.is_ig_admin(username)

    # ---------------------------------------------------- admin commands
    order = eve_modes.parse_order(text, username)
    if order:
        if order["type"] == "denied":
            # Non-admin ne command maari — rude slide, hamesha (STOP me bhi nahi).
            if mode == eve_modes.MODE_STOP:
                return Decision(False, "stop mode: order ignore")
            return Decision(True, "non-admin order", canned_reply=order["reply"],
                            tags=["order_denied"])

        if order["type"] == "help_over":
            eve_modes.end_help()
            return Decision(True, "admin ne /helpover kiya",
                            canned_reply="theek hai malik 🫡 help over — ab normal.",
                            order_action="help_over", tags=["help_over"])

        if order["type"] == "help":
            eve_modes.start_help(thread_id)
            return Decision(
                True, "admin ne /help maanga — debate support",
                route="help",
                extra_prompt=(
                    "MALIK SUPPORT MODE: tera admin (Dhruv) is waqt bahas/roast me hai. "
                    "GC ka context padh ke pehchan ki kaunsa message opponent ka hai aur "
                    "kaunsa admin ka. Admin ka side lena hai — uske point ko facts, logic "
                    "aur numbers se strong karna hai, aur opponent ke argument ke hole "
                    "nikalne hain. Confident bol, jhijhak mat. Jhoote facts mat banana — "
                    "jo pakka hai wahi bol, par tez tareeke se."
                ),
                order_action="help", tags=["admin_help", "heavy"],
            )

        action = order["action"]
        if action == "roast":
            return Decision(
                True, f"admin order: roast {order.get('arg')}",
                extra_prompt=(
                    f"MALIK KA ORDER: @{order.get('arg') or 'us bande'} ko roast kar. "
                    "Short, tez, bina raham."
                ),
                order_action="roast", tags=["order_roast"],
            )
        ack = eve_modes.execute_order(action, order.get("arg"))
        return Decision(True, f"admin order: {action}", canned_reply=ack,
                        order_action=action, tags=["order"])

    # ------------------------------------------------------- learn only
    if mode == eve_modes.MODE_STOP:
        return Decision(False, "STOP mode — sirf learning")

    if mode == eve_modes.MODE_ADMIN_ONLY:
        if not admin:
            return Decision(False, "admin-only mode, ye admin nahi")
        if not (_mentions_bot(text, bot_username) or eve_modes.matches_nickname(text)):
            return Decision(False, "admin-only mode, mention nahi kiya")

    # ---------------------------------------------------------- triggers
    trig_prompt = trigger_manager.prompt_for(username)

    # --------------------------------------------------------- addressed?
    nick = eve_modes.matches_nickname(text)
    mentioned = _mentions_bot(text, bot_username)
    ultimate = mode == eve_modes.MODE_ULTIMATE

    addressed = bool(nick or mentioned or trig_prompt or ultimate)
    if not addressed:
        return Decision(False, "naam nahi liya, mention nahi, trigger nahi")

    # ------------------------------------------------------------- intro
    ask_intro = False
    if is_new_member or people.needs_intro(username):
        if is_new_member:
            ask_intro = True
            people.mark_intro_asked(username)

    # ------------------------------------------------------------- model
    cls = debate_detector.classify(text, recent_texts)
    help_on = eve_modes.is_help_active(thread_id)
    route = "help" if help_on else ("analyze" if cls["needs_opus"] else "banter")

    extras: List[str] = []
    if help_on:
        extras.append(
            "HELP MODE ON: malik ne support maanga hua hai. Jab tak wo /helpover "
            "na bole, har reply me uska side lena hai — facts, logic aur tez "
            "jawab se opponent ko dabana hai."
        )

    trig = trigger_manager.get_trigger(username) or {}
    if not help_on and trig.get("tone") in ("roast", "abusive_roast"):
        route = "roast"
    elif not help_on and trig.get("tone") in ("flirty", "dirty"):
        route = "flirt"

    if trig_prompt:
        extras.append(trig_prompt)
    if ask_intro:
        extras.append(
            "Ye banda group me naya hai. Pehle uska intro poochh — casual andaaz me, "
            "'bhai tu kaun, apna intro de' type. Ek line."
        )
    if cls["kind"] == "political_debate":
        extras.append(
            "POLITICAL DEBATE detect hua. Ab serious mode: facts aur logic se baat kar, "
            "gaali chhod. Neutral banne ki koshish mat kar agar admin ka side clear hai."
        )
    elif cls["kind"] == "serious_debate":
        extras.append("Serious discussion hai — soch ke, structured jawab de. Mazaak kam.")

    # admin ke baare me bura bola gaya?
    for adm in eve_modes.get_ig_admins():
        if adm in text.lower() and not admin:
            p = people.get_person(adm) or {}
            nm = p.get("real_name") or adm
            if _sounds_negative(text):
                extras.append(
                    f"Is message me tere MALIK ({nm}) ke baare me bakwaas ki gayi hai. "
                    "Bardasht mat kar — bolne wale ko roast maar."
                )
                route = route if route == "analyze" else "banter"
                break

    reason = f"{cls['kind']} (score {cls['score']})"
    if nick:
        reason = f"nickname '{nick}' + " + reason
    elif mentioned:
        reason = "mention + " + reason
    elif trig_prompt:
        reason = "trigger + " + reason
    elif ultimate:
        reason = "ultimate fire + " + reason

    return Decision(
        should_reply=True,
        reason=reason,
        route=route,
        extra_prompt="\n".join(extras),
        ask_intro=ask_intro,
        tags=[cls["kind"]] + (["heavy"] if route in ("analyze", "help") else []),
    )


_NEG = {
    "gandu", "chutiy", "bkl", "bc", "mc", "loser", "fake", "jhoot", "ghatiya",
    "bekar", "flop", "chomu", "pagal", "bewakoof", "chup kar", "aukat",
    "nikamma", "harami", "kamina", "randi", "bhosdi", "faltu", "gay", "chakka",
}


def _sounds_negative(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in _NEG)
