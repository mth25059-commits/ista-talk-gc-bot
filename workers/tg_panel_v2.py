"""
Eve v7 — Telegram control panel (inline buttons).

Purane tg_panel.py ki jagah ye chalao. Sab kuch buttons se — command yaad
rakhne ki zarurat nahi.

    ┌─────────── EVE CONTROL ───────────┐
    │  ▶ START      ⏸ STOP              │
    │  🔥 ULTIMATE FIRE  👑 ADMIN-ONLY  │
    │  🏷 Nicknames  🎭 Tone            │
    │  🔓 Unfilter   🎯 Trigger         │
    │  🧠 People     📊 Stats           │
    │  🔑 API Keys   ☁️ Drive           │
    └───────────────────────────────────┘

Fixes vs v6:
  * update_id DB me persist — restart pe purane commands dobara nahi chalte
  * requests + retry (urllib ke silent SSL issues nahi)
  * wizard state DB me — multi-step input safely chalta hai
  * crash alerts admin ko TG pe
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from intelligence import (
    eve_modes,
    key_pool,
    llm_router_v7 as router,
    model_prefs,
    providers,
    trigger_manager,
)
from intelligence.aihumara_state import (
    _get as sget,
    _set as sset,
    get_model_force,
    get_tg_admin_id,
    set_model_force,
    set_tg_admin_id,
)
from storage import drive_sync, gc_profile, people
from storage.database import get_connection, init_db
from storage.schema_v7 import ensure_v7_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("eve.tg")

TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
POLL_TIMEOUT = 25

_session = requests.Session()


# ============================================================ TG_STATE


def _tg_get(key: str, default: Any = None) -> Any:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM TG_STATE WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def _tg_set(key: str, value: Any) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO TG_STATE (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (key, json.dumps(value), datetime.now(timezone.utc).isoformat()),
        )


def _tg_del(key: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM TG_STATE WHERE key = ?", (key,))


def _pending() -> Optional[Dict[str, Any]]:
    return _tg_get("pending")


def _set_pending(action: str, **extra: Any) -> None:
    _tg_set("pending", {"action": action, **extra})


def _clear_pending() -> None:
    _tg_del("pending")


# ================================================================ API


def _call(method: str, **params: Any) -> Optional[dict]:
    for attempt in (1, 2, 3):
        try:
            r = _session.post(f"{API}/{method}", json=params, timeout=POLL_TIMEOUT + 15)
            data = r.json()
            if not data.get("ok"):
                logger.warning("[TG] %s -> %s", method, data.get("description"))
            return data
        except requests.RequestException as e:
            logger.warning("[TG] %s attempt %d failed: %s", method, attempt, e)
            time.sleep(1.5 * attempt)
    return None


def _send(chat_id: int, text: str, keyboard: Optional[List[List[dict]]] = None) -> None:
    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    _call("sendMessage", **payload)


def _edit(chat_id: int, message_id: int, text: str,
          keyboard: Optional[List[List[dict]]] = None) -> None:
    payload: Dict[str, Any] = {
        "chat_id": chat_id, "message_id": message_id, "text": text[:4000],
    }
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    _call("editMessageText", **payload)


def _answer(cb_id: str, text: str = "") -> None:
    _call("answerCallbackQuery", callback_query_id=cb_id, text=text[:190])


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def alert_admin(text: str) -> None:
    """Bahar se bhi bulaya ja sakta hai (crash/budget alerts)."""
    admin = get_tg_admin_id()
    if admin:
        _send(int(admin), text)


# ============================================================== menus


def _main_menu() -> List[List[dict]]:
    st = eve_modes.status_dict()
    m = st["mode"]
    tick = lambda x: "✅ " if m == x else ""  # noqa: E731
    return [
        [_btn(f"{tick(eve_modes.MODE_START)}▶️ START", "mode:start"),
         _btn(f"{tick(eve_modes.MODE_STOP)}⏸ STOP", "mode:stop")],
        [_btn(f"{tick(eve_modes.MODE_ULTIMATE)}🔥 ULTIMATE FIRE", "mode:ultimate"),
         _btn(f"{tick(eve_modes.MODE_ADMIN_ONLY)}👑 ADMIN-ONLY", "mode:admin_only")],
        [_btn("🏷 Nicknames", "nick:menu"), _btn("🎭 Tone", "tone:menu")],
        [_btn(f"{'🔓' if st['unfiltered'] else '🔒'} Unfilter: "
              f"{'ON' if st['unfiltered'] else 'OFF'}", "unfilter:toggle"),
         _btn("🎯 Trigger", "trig:menu")],
        [_btn("🧠 People", "ppl:menu"), _btn("📊 Stats", "stats:show")],
        [_btn("🔑 API Keys", "key:menu"), _btn("🧠 Models", "pref:menu")],
        [_btn("☁️ Drive", "drive:menu"), _btn("🆘 Help mode", "help:menu")],
        [_btn("👑 IG Admins", "adm:menu"), _btn("🧬 Brain", "brain:menu")],
        [_btn("🔄 Refresh", "home")],
    ]


def _home_text() -> str:
    st = eve_modes.status_dict()
    h = router.health()
    return (
        "🤖 <EVE CONTROL PANEL>\n"
        "────────────────────\n"
        f"Mode      : {st['mode_label']}\n"
        f"Filter    : {'UNFILTERED 🔓' if st['unfiltered'] else 'filtered 🔒'}\n"
        f"Tone      : {eve_modes.TONE_LABELS[st['tone']]}\n"
        f"Nicknames : {', '.join(st['nicknames']) or '-'}\n"
        f"IG admins : {', '.join(st['ig_admins']) or '-- set kar --'}\n"
        f"Brain     : {h['model_force']} | groq {h['groq_active']}/{h['groq_keys']}"
        f" | opus {h['opus_active']}/{h['opus_keys']}\n"
        f"People    : {people.count_people()} yaad\n"
        f"Triggers  : {len(trigger_manager.list_triggers(False))} active"
    )


_BACK = [[_btn("⬅️ Back", "home")]]


# ========================================================== callbacks


def _cb_mode(arg: str) -> tuple[str, List[List[dict]]]:
    eve_modes.set_mode(arg)
    msg = {
        "start": "▶️ START — ab naam lene / mention pe reply dega, aur seekhta rahega.",
        "stop": "⏸ STOP — reply band. Sirf chup-chaap seekhega.",
        "ultimate": "🔥 ULTIMATE FIRE — ab har msg pe reply karega, bina mention ke.",
        "admin_only": "👑 ADMIN-ONLY — sirf admin ke mention pe bolega.",
    }[arg]
    return msg + "\n\n" + _home_text(), _main_menu()


def _cb_nick(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        nicks = eve_modes.get_nicknames()
        kb = [[_btn(f"❌ {n}", f"nick:del:{n}")] for n in nicks]
        kb.append([_btn("➕ Naya nickname", "nick:add")])
        kb.append([_btn("⬅️ Back", "home")])
        return ("🏷 NICKNAMES\nIn naamo pe bot pakka reply karega "
                "(thodi spelling galti bhi chalegi).\n\n"
                + ("\n".join(f"• {n}" for n in nicks) or "koi nahi"), kb)
    if arg == "add":
        _set_pending("nick_add")
        return "Naya nickname bhej de (2-24 letters):", _BACK
    if arg.startswith("del:"):
        name = arg[4:]
        eve_modes.remove_nickname(name)
        return _cb_nick("menu")
    return _home_text(), _main_menu()


def _cb_tone(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        cur = eve_modes.get_global_tone()
        kb = [[_btn(("✅ " if k == cur else "") + v, f"tone:set:{k}")]
              for k, v in eve_modes.TONE_LABELS.items()]
        kb.append([_btn("⬅️ Back", "home")])
        return "🎭 BOT KA DEFAULT TONE\nYe har GC me base personality hai.", kb
    if arg.startswith("set:"):
        eve_modes.set_global_tone(arg[4:])
        return _cb_tone("menu")
    return _home_text(), _main_menu()


def _cb_trig(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        kb = [[_btn("➕ Naya trigger", "trig:add")],
              [_btn("🚫 Sab OFF", "trig:alloff")]]
        for t in trigger_manager.list_triggers():
            icon = "🟢" if t["active"] else "⚪"
            kb.append([
                _btn(f"{icon} @{t['ig_username']} · "
                     f"{trigger_manager.TONE_LABELS.get(t['tone'], t['tone'])}",
                     f"trig:tog:{t['ig_username']}"),
                _btn("❌", f"trig:del:{t['ig_username']}"),
            ])
        kb.append([_btn("⬅️ Back", "home")])
        return ("🎯 TRIGGERS\nJis username pe trigger laga hai, wo kuch bhi bole — "
                "bot fixed tone me reply dega (mention ki zarurat nahi).", kb)
    if arg == "add":
        _set_pending("trig_username")
        return "IG username bhej (bina @):", _BACK
    if arg == "alloff":
        n = trigger_manager.disable_all()
        return _cb_trig("menu")[0] + f"\n\n{n} triggers off kar diye.", _cb_trig("menu")[1]
    if arg.startswith("del:"):
        trigger_manager.remove_trigger(arg[4:])
        return _cb_trig("menu")
    if arg.startswith("tog:"):
        u = arg[4:]
        t = next((x for x in trigger_manager.list_triggers() if x["ig_username"] == u), None)
        if t:
            trigger_manager.toggle_trigger(u, not t["active"])
        return _cb_trig("menu")
    if arg.startswith("tone:"):
        tone = arg[5:]
        p = _pending() or {}
        u = p.get("username", "")
        _clear_pending()
        if not u:
            return _cb_trig("menu")
        trigger_manager.set_trigger(u, tone)
        txt, kb = _cb_trig("menu")
        return f"✅ @{u} → {trigger_manager.TONE_LABELS[tone]}\n\n" + txt, kb
    return _home_text(), _main_menu()


def _cb_ppl(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        kb = [[_btn("➕ Banda add / edit", "ppl:add")],
              [_btn("🔍 Kisi ko dhundo", "ppl:find")],
              [_btn("📋 Top 15 list", "ppl:list")],
              [_btn("⬅️ Back", "home")]]
        return (f"🧠 PEOPLE MEMORY — {people.count_people()} log yaad hain.\n\n"
                "Yahan pehle se bata sakta hai ki kaun ladka/ladki hai, naam kya hai, "
                "dost hai ya dushman. Baaki bot khud GC padh ke seekhta rahega.", kb)
    if arg == "add":
        _set_pending("ppl_add")
        return ("Format me bhej (comma se alag):\n\n"
                "`username, naam, boy|girl, friend|enemy|admin, notes`\n\n"
                "Example:\n`rahul_23, Rahul, boy, friend, cricket ka pagal hai`\n\n"
                "Sirf username bhi chalega."), _BACK
    if arg == "find":
        _set_pending("ppl_find")
        return "Username bhej jise dhundna hai:", _BACK
    if arg == "list":
        rows = people.list_people(limit=15)
        if not rows:
            return "Abhi koi nahi. GC me msgs aane de ya manually add kar.", _BACK
        lines = ["📋 TOP PEOPLE:"]
        for p in rows:
            g = {"boy": "♂", "girl": "♀"}.get(p["gender"], "?")
            lines.append(
                f"{g} @{p['ig_username']} — {p['real_name'] or 'naam ?'}"
                f" | {p['relation']} | {p['msg_count']} msgs"
                f" | {'✍️' if p['source'] == 'manual' else '🤖'}"
            )
        return "\n".join(lines), _BACK
    return _home_text(), _main_menu()


def _cb_key(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        kb = []
        row: List[dict] = []
        for pid in providers.PROVIDER_IDS:
            n = len(key_pool.list_keys(pid))
            row.append(_btn(f"{providers.label(pid)} ({n})", f"key:prov:{pid}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([_btn("♻️ Quota reset", "key:reset"),
                   _btn("🧠 Models", "pref:menu")])
        kb.append([_btn("⬅️ Back", "home")])
        return ("🔑 API KEYS\nProvider chun ke keys daal. Key daalte hi live "
                "check hoga — sahi hui to model choose karne ko milega.\n"
                "Har key ka apna quota, khatam/fail hote hi agli key.\n\n"
                + key_pool.status_text()), kb

    if arg.startswith("prov:"):
        pid = arg[5:]
        keys = key_pool.list_keys(pid)
        kb = [[_btn("➕ Keys add karo", f"key:add:{pid}")],
              [_btn("🎯 Model badlo", f"key:models:{pid}"),
               _btn("♻️ Quota reset", f"key:reset:{pid}")]]
        for k in keys[:12]:
            icon = {"active": "🟢", "exhausted": "🟡", "dead": "🔴"}.get(k["status"], "⚪")
            kb.append([_btn(f"{icon} {k['masked']} ({k['quota_used']}/{k['quota_limit']})",
                            f"key:info:{k['id']}"),
                       _btn("🗑", f"key:del:{k['id']}")])
        if keys:
            kb.append([_btn("🗑 Sab hatao", f"key:clear:{pid}")])
        kb.append([_btn("⬅️ Back", "key:menu")])
        return (f"{providers.label(pid)}\nModel: {key_pool.get_model(pid)}\n"
                f"Keys: {len(keys)}\n\n" + key_pool.status_text(pid)), kb

    if arg.startswith("add:"):
        pid = arg[4:]
        _set_pending("key_add", provider=pid)
        return (f"{providers.label(pid)} ki key(s) bhej.\n\n"
                "Ek line me ek key — 1, 5, 20 jitni marzi ek saath.\n"
                "Pehli key live verify hogi, phir model poochhunga.\n"
                "Tera message main turant delete kar dunga."), _BACK

    if arg.startswith("models:"):
        pid = arg[7:]
        keys = key_pool.list_keys(pid)
        if not keys:
            return "Pehle is provider ki key add kar.", [[_btn("⬅️ Back", f"key:prov:{pid}")]]
        ok, msg, models = providers.validate(pid, keys[0]["api_key"], keys[0].get("base_url"))
        models = models or list(providers.spec(pid)["fallback_models"])
        _tg_set("model_choices", {"provider": pid, "models": models})
        cur = key_pool.get_model(pid)
        kb = [[_btn(("✅ " if m == cur else "") + m, f"key:setmodel:{i}")]
              for i, m in enumerate(models[:12])]
        kb.append([_btn("⬅️ Back", f"key:prov:{pid}")])
        return (f"{providers.label(pid)} — kaunsa model use karun?\n{msg}"), kb

    if arg.startswith("setmodel:"):
        ch = _tg_get("model_choices") or {}
        models = ch.get("models") or []
        pid = ch.get("provider", "")
        try:
            model = models[int(arg[9:])]
        except (ValueError, IndexError):
            return _cb_key("menu")
        key_pool.set_model(pid, model)
        model_prefs.reset()
        txt, kb = _cb_key(f"prov:{pid}")
        return f"✅ Model set: {model}\n\n" + txt, kb

    if arg.startswith("info:"):
        kid = int(arg[5:])
        row = next((k for k in key_pool.list_keys() if k["id"] == kid), None)
        if not row:
            return _cb_key("menu")
        ok, msg, _ = providers.validate(row["provider"], row["api_key"], row.get("base_url"))
        if ok:
            key_pool.mark_verified(kid)
        else:
            key_pool.report_failure(kid, msg, fatal="wrong" in msg)
        txt, kb = _cb_key(f"prov:{row['provider']}")
        return (f"{'✅' if ok else '❌'} {row['masked']} — {msg}\n\n" + txt), kb

    if arg.startswith("del:"):
        kid = int(arg[4:])
        row = next((k for k in key_pool.list_keys() if k["id"] == kid), None)
        key_pool.remove_key(kid)
        return _cb_key(f"prov:{row['provider']}" if row else "menu")

    if arg.startswith("reset:"):
        n = key_pool.reset_quotas(arg[6:])
        txt, kb = _cb_key(f"prov:{arg[6:]}")
        return f"♻️ {n} keys ka quota reset.\n\n" + txt, kb

    if arg == "reset":
        n = key_pool.reset_quotas()
        txt, kb = _cb_key("menu")
        return f"♻️ {n} keys ka quota reset.\n\n" + txt, kb

    if arg.startswith("clear:"):
        pid = arg[6:]
        kb = [[_btn("⚠️ Haan, saaf kar", f"key:clearyes:{pid}")],
              [_btn("⬅️ Back", f"key:prov:{pid}")]]
        return f"Pakka? {providers.label(pid)} ki saari keys hat jayengi.", kb

    if arg.startswith("clearyes:"):
        pid = arg[9:]
        n = key_pool.clear_provider(pid)
        model_prefs.reset()
        txt, kb = _cb_key("menu")
        return f"🗑 {n} keys hata di.\n\n" + txt, kb

    return _home_text(), _main_menu()


def _cb_pref(arg: str) -> tuple[str, List[List[dict]]]:
    """Smart fallback preferences — kaunsa kaam kis model se."""
    if arg == "menu":
        kb = []
        row: List[dict] = []
        for t in model_prefs.TASK_ORDER:
            row.append(_btn(model_prefs.TASKS[t][0], f"pref:task:{t}"))
            if len(row) == 2:
                kb.append(row)
                row = []
        if row:
            kb.append(row)
        kb.append([_btn("🪄 Auto set (recommended)", "pref:auto"),
                   _btn("↩️ Reset", "pref:reset")])
        kb.append([_btn("🔑 API Keys", "key:menu"), _btn("⬅️ Back", "home")])
        return model_prefs.status_text(), kb

    if arg == "auto":
        model_prefs.auto_configure()
        txt, kb = _cb_pref("menu")
        return "🪄 Auto set ho gaya (jo keys live hain unhi se).\n\n" + txt, kb

    if arg == "reset":
        model_prefs.reset()
        txt, kb = _cb_pref("menu")
        return "↩️ Sab default pe.\n\n" + txt, kb

    if arg.startswith("task:"):
        t = arg[5:]
        avail = model_prefs.available_providers()
        kb = []
        for pid in avail:
            kb.append([_btn(f"⭐ Primary: {providers.label(pid)}", f"pref:set:{t}:{pid}"),
                       _btn("➕ fallback", f"pref:add:{t}:{pid}")])
        for c in model_prefs.get_chain(t, resolved=False):
            kb.append([_btn(f"❌ hatao {providers.label(c['provider'])}",
                            f"pref:rm:{t}:{c['provider']}")])
        kb.append([_btn("↩️ Auto", f"pref:clr:{t}"), _btn("⬅️ Back", "pref:menu")])
        if not avail:
            return ("Pehle koi API key add kar — model chain tab hi banegi.",
                    [[_btn("🔑 API Keys", "key:menu")], [_btn("⬅️ Back", "pref:menu")]])
        return model_prefs.describe(t), kb

    if arg.startswith("set:") or arg.startswith("add:") or arg.startswith("rm:"):
        op, _, rest = arg.partition(":")
        t, _, pid = rest.partition(":")
        if op == "set":
            model_prefs.set_primary(t, pid)
        elif op == "add":
            model_prefs.add_fallback(t, pid)
        else:
            model_prefs.remove_provider(t, pid)
        return _cb_pref(f"task:{t}")

    if arg.startswith("clr:"):
        model_prefs.reset(arg[4:])
        return _cb_pref(f"task:{arg[4:]}")

    return _home_text(), _main_menu()


def _cb_help(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "off":
        eve_modes.end_help()
    kb = [[_btn("🛑 Help OFF karo", "help:off")], [_btn("⬅️ Back", "home")]]
    return (eve_modes.help_status_text() + "\n\n"
            "IG pe admin `/help` likhe to support mode ON, `/helpover` "
            "likhe to OFF — phir normal brain wapas.\n"
            f"Help ka model: {model_prefs.describe('help')}"), kb


def _cb_brain(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        cur = get_model_force()
        opts = {
            "default": "🤖 Auto (recommended)",
            "groq_only": "⚡ Sirf Groq",
            "opus_only": "🧠 Sirf Opus",
        }
        kb = [[_btn(("✅ " if k == cur else "") + v, f"brain:set:{k}")]
              for k, v in opts.items()]
        kb.append([_btn("⬅️ Back", "home")])
        return ("🧬 BRAIN ROUTING\n\n"
                "Auto → normal baat-cheet Groq se (tez + sasta), "
                "political/serious debate aur admin /help pe Opus 4.8.\n"
                "Sirf Groq → Opus kabhi nahi (credit 100% bachega).\n"
                "Sirf Opus → sab kuch Opus se (best quality, mehnga)."), kb
    if arg.startswith("set:"):
        set_model_force(arg[4:])
        return _cb_brain("menu")
    return _home_text(), _main_menu()


def _cb_adm(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        admins = eve_modes.get_ig_admins()
        kb = [[_btn(f"❌ {a}", f"adm:del:{a}")] for a in admins]
        kb.append([_btn("➕ IG admin add", "adm:add")])
        kb.append([_btn("⬅️ Back", "home")])
        return ("👑 IG ADMINS\nIn usernames ki hi command bot manega "
                "(/order, /help). Baaki kisi ne try kiya to rude reply milega.\n\n"
                + ("\n".join(f"• @{a}" for a in admins) or "koi set nahi"), kb)
    if arg == "add":
        _set_pending("adm_add")
        return "IG username bhej (bina @):", _BACK
    if arg.startswith("del:"):
        eve_modes.remove_ig_admin(arg[4:])
        return _cb_adm("menu")
    return _home_text(), _main_menu()


def _cb_drive(arg: str) -> tuple[str, List[List[dict]]]:
    if arg == "menu":
        kb = [[_btn("⬆️ Abhi backup", "drive:backup"),
               _btn("⬇️ Restore", "drive:restore")],
              [_btn("⬅️ Back", "home")]]
        return drive_sync.status_text(), kb
    if arg == "backup":
        res = drive_sync.upload_snapshot()
        msg = (f"✅ Backup ho gaya ({res['size'] / 1024:.1f} KB)"
               if res.get("ok") else f"❌ Fail: {res.get('error')}")
        return msg + "\n\n" + drive_sync.status_text(), _cb_drive("menu")[1]
    if arg == "restore":
        kb = [[_btn("⚠️ Haan, Drive se overwrite", "drive:restore:yes")],
              [_btn("⬅️ Back", "drive:menu")]]
        return ("Pakka? Local memory Drive wale snapshot se replace ho jayegi.\n"
                "(purani copy .db.prev me safe reh jayegi)"), kb
    if arg == "restore:yes":
        res = drive_sync.restore_latest(force=True)
        msg = (f"✅ Restore: {res.get('name')}" if res.get("ok")
               else f"❌ Fail: {res.get('error')}")
        return msg + "\n\nIG bot restart kar de taaki nayi DB load ho.", _BACK
    return _home_text(), _main_menu()


def _cb_stats() -> tuple[str, List[List[dict]]]:
    today = sget(f"usage_{date.today().isoformat()}") or {}
    total = sget("usage") or {}
    budget = float(os.getenv("ANTHROPIC_BUDGET_USD", "150"))

    with get_connection() as conn:
        msgs = conn.execute("SELECT COUNT(*) c FROM MESSAGES").fetchone()["c"]
        users = conn.execute("SELECT COUNT(*) c FROM USERS").fetchone()["c"]
        mems = conn.execute(
            "SELECT COUNT(*) c FROM MEMORIES WHERE active = 1"
        ).fetchone()["c"]

    lines = [
        "📊 STATS",
        "────────────",
        f"Messages seen : {msgs}",
        f"Users known   : {users}",
        f"People memory : {people.count_people()}",
        f"Facts stored  : {mems}",
        "",
        "AAJ:",
    ]
    for pid in providers.PROVIDER_IDS:
        calls = today.get(f"{pid}_calls", 0)
        if not calls and not today.get(f"{pid}_fail", 0):
            continue
        lines.append(f"  {providers.label(pid)}: {calls} calls"
                     f" (fail {today.get(f'{pid}_fail', 0)})")
    lines += [
        f"  Claude kharch: ${router.opus_cost_usd(today):.4f}",
        "",
        f"TOTAL Opus   : ${router.opus_cost_usd(total):.4f} / ${budget}",
    ]

    profs = gc_profile.list_profiles()[:5]
    if profs:
        lines += ["", "GC LEHJA:"]
        for p in profs:
            lines.append(
                f"  {p['thread_id'][:14]} — gaali {p['gali_level']:.1f}"
                f" flirty {p['flirty_level']:.1f} ({p['sample_count']} msgs)"
            )
    return "\n".join(lines), _BACK


_ROUTES = {
    "mode": lambda a: _cb_mode(a),
    "nick": _cb_nick,
    "tone": _cb_tone,
    "trig": _cb_trig,
    "ppl": _cb_ppl,
    "key": _cb_key,
    "brain": _cb_brain,
    "adm": _cb_adm,
    "drive": _cb_drive,
    "pref": _cb_pref,
    "help": _cb_help,
}


def _handle_callback(cb: dict) -> None:
    data = cb.get("data") or ""
    msg = cb.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    user_id = (cb.get("from") or {}).get("id")

    if not _is_admin(user_id):
        _answer(cb["id"], "private panel hai boss")
        return

    _answer(cb["id"])

    try:
        if data == "home":
            _clear_pending()
            _edit(chat_id, message_id, _home_text(), _main_menu())
            return
        if data == "unfilter:toggle":
            eve_modes.set_unfiltered(not eve_modes.is_unfiltered())
            _edit(chat_id, message_id, _home_text(), _main_menu())
            return
        if data == "stats:show":
            text, kb = _cb_stats()
            _edit(chat_id, message_id, text, kb)
            return

        prefix, _, arg = data.partition(":")
        handler = _ROUTES.get(prefix)
        if handler is None:
            _edit(chat_id, message_id, _home_text(), _main_menu())
            return
        text, kb = handler(arg)
        _edit(chat_id, message_id, text, kb)

    except Exception as e:
        logger.exception("[TG] callback error")
        _send(chat_id, f"⚠️ error: {e}")


# ====================================================== text/wizard input


def _handle_pending(chat_id: int, text: str, message_id: int) -> bool:
    p = _pending()
    if not p:
        return False
    action = p.get("action")

    try:
        if action == "nick_add":
            eve_modes.add_nickname(text)
            _clear_pending()
            _send(chat_id, f"✅ '{text.lower()}' add ho gaya.", _cb_nick("menu")[1])

        elif action == "adm_add":
            eve_modes.add_ig_admin(text)
            _clear_pending()
            _send(chat_id, f"✅ @{text.lstrip('@').lower()} ab IG admin hai.",
                  _cb_adm("menu")[1])

        elif action == "trig_username":
            u = text.strip().lstrip("@").lower()
            _set_pending("trig_tone", username=u)
            kb = [[_btn(v, f"trig:tone:{k}")] for k, v in trigger_manager.TONE_LABELS.items()]
            kb.append([_btn("⬅️ Cancel", "trig:menu")])
            _send(chat_id, f"@{u} ke liye tone chun:", kb)

        elif action == "ppl_add":
            parts = [x.strip() for x in text.split(",")]
            uname = parts[0]
            kwargs: Dict[str, Any] = {"source": "manual"}
            if len(parts) > 1 and parts[1]:
                kwargs["real_name"] = parts[1]
            if len(parts) > 2 and parts[2].lower() in ("boy", "girl"):
                kwargs["gender"] = parts[2].lower()
            if len(parts) > 3 and parts[3].lower() in ("friend", "enemy", "admin", "stranger"):
                kwargs["relation"] = parts[3].lower()
            if len(parts) > 4 and parts[4]:
                kwargs["notes"] = ", ".join(parts[4:])
            people.upsert_person(uname, **kwargs)
            _clear_pending()
            _send(chat_id, "✅ Save ho gaya:\n" + people.describe(uname),
                  _cb_ppl("menu")[1])

        elif action == "ppl_find":
            _clear_pending()
            info = people.describe(text)
            _send(chat_id, "🧠 " + info, _cb_ppl("menu")[1])

        elif action == "key_add":
            prov = p.get("provider", "groq")
            raw = [l.strip() for l in text.splitlines() if l.strip()]
            _clear_pending()
            _call("deleteMessage", chat_id=chat_id, message_id=message_id)
            if not raw:
                _send(chat_id, "Koi key nahi mili.", _cb_key(f"prov:{prov}")[1])
                return True

            ok, msg, models = providers.validate(prov, raw[0])
            if not ok:
                _send(chat_id, f"❌ {msg}\nTry again later — dobara bhej.",
                      [[_btn("🔁 Phir se", f"key:add:{prov}")],
                       [_btn("⬅️ Back", "key:menu")]])
                return True

            added, dup, bad = 0, 0, 0
            for k in raw:
                try:
                    if key_pool.add_key(prov, k, verified=(k == raw[0])):
                        added += 1
                    else:
                        dup += 1
                except ValueError:
                    bad += 1

            models = models or list(providers.spec(prov)["fallback_models"])
            _tg_set("model_choices", {"provider": prov, "models": models})
            kb = [[_btn(m, f"key:setmodel:{i}")] for i, m in enumerate(models[:12])]
            kb.append([_btn("⏭ Default rakho", f"key:prov:{prov}")])
            _send(chat_id,
                  f"✅ Key set and connected — {msg}\n"
                  f"{added} add, {dup} pehle se thi"
                  + (f", {bad} invalid" if bad else "")
                  + f"\n\n{providers.label(prov)} ka model chun:", kb)

        else:
            _clear_pending()
            return False

    except ValueError as e:
        _send(chat_id, f"⚠️ {e}")
    except Exception as e:
        logger.exception("[TG] pending error")
        _clear_pending()
        _send(chat_id, f"⚠️ error: {e}")
    return True


def _is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and str(user_id) == (get_tg_admin_id() or "")


def _handle_message(msg: dict) -> None:
    text = (msg.get("text") or "").strip()
    chat_id = msg["chat"]["id"]
    user_id = (msg.get("from") or {}).get("id")
    message_id = msg.get("message_id")
    if not text or user_id is None:
        return

    if text in ("/claimadmin", "/claim"):
        existing = get_tg_admin_id()
        if existing and existing != str(user_id):
            _send(chat_id, "koi aur admin already set hai.")
        elif existing:
            _send(chat_id, "tu wahi admin hai bhai.", _main_menu())
        else:
            set_tg_admin_id(str(user_id))
            _send(chat_id, "✅ ADMIN SET. Panel tera.", _main_menu())
        return

    if not _is_admin(user_id):
        _send(chat_id, "private panel hai boss. apna bana le.")
        return

    if text in ("/start", "/panel", "/menu", "/home"):
        _clear_pending()
        _send(chat_id, _home_text(), _main_menu())
        return

    if _handle_pending(chat_id, text, message_id):
        return

    _send(chat_id, _home_text(), _main_menu())


# =============================================================== loop


def main() -> None:
    if not TOKEN:
        raise SystemExit("TG_BOT_TOKEN set nahi hai")

    init_db()
    ensure_v7_schema()
    key_pool.seed_from_env()

    offset = int(_tg_get("last_update_id", 0)) + 1
    logger.info("[TG] panel up — offset %d", offset)

    admin = get_tg_admin_id()
    if admin:
        _send(int(admin), "♻️ Eve panel restart ho gaya.", _main_menu())

    while True:
        try:
            r = _session.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": POLL_TIMEOUT},
                timeout=POLL_TIMEOUT + 15,
            )
            data = r.json()
            if not data.get("ok"):
                logger.warning("[TG] getUpdates: %s", data.get("description"))
                time.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                _tg_set("last_update_id", upd["update_id"])
                try:
                    if "callback_query" in upd:
                        _handle_callback(upd["callback_query"])
                    elif "message" in upd:
                        _handle_message(upd["message"])
                except Exception:
                    logger.exception("[TG] update handling failed")

        except requests.RequestException as e:
            logger.warning("[TG] poll error: %s", e)
            time.sleep(3)
        except KeyboardInterrupt:
            logger.info("[TG] bye")
            return
        except Exception:
            logger.exception("[TG] loop crash")
            try:
                alert_admin("⚠️ TG panel loop crash:\n" + traceback.format_exc()[-1200:])
            except Exception:
                pass
            time.sleep(5)


if __name__ == "__main__":
    main()
