# Eve v7 — Install Guide

Ye folder tere `instaai` repo ke **upar** drop karne ke liye hai. Purana kuch
delete nahi hota — sab naye files hain, sirf 3 jagah 2-2 line jodni hai.

---

## 1. Files copy karo

Repo root se:

```bash
cp eve-v7/storage/*.py        storage/
cp eve-v7/intelligence/*.py   intelligence/
cp eve-v7/workers/*.py        workers/
cp eve-v7/eve_v7_boot.py      .
```

## 2. Dependencies

```bash
pip install requests google-api-python-client google-auth
```

`requirements.txt` me bhi daal de:

```
requests>=2.31
google-api-python-client>=2.120
google-auth>=2.28
```

## 3. `.env` me naya kuch

```env
# Google Drive brain sync (optional par recommended)
GOOGLE_SERVICE_ACCOUNT_JSON=/root/eve/sa.json
GDRIVE_FOLDER_ID=1AbCdEfGhIjKlMnOpQrStUvWxYz

# Groq keys ek saath (comma separated) — ya sab TG panel se add kar
GROQ_API_KEYS=gsk_aaa,gsk_bbb,gsk_ccc
```

### Google Drive setup (ek baar ka kaam)

1. https://console.cloud.google.com → naya project
2. **APIs & Services → Enable APIs** → "Google Drive API" enable
3. **Credentials → Create → Service Account** → JSON key download
4. JSON ko VPS pe `/root/eve/sa.json` rakh
5. Apne Drive me `EveBrain` naam ka folder bana
6. Us folder ko service account ki email (`xxx@yyy.iam.gserviceaccount.com`)
   ke saath **Editor** access pe share kar
7. Folder URL se ID uthao: `drive.google.com/drive/folders/<YE_ID>`

## 4. `main.py` me 2 line

Sabse upar, baaki imports ke baad:

```python
from eve_v7_boot import boot_v7, shutdown_v7, on_incoming_message, build_reply_context

boot_v7()          # init_db() ki jagah — ye khud init_db karta hai
```

Aur shutdown handler me:

```python
shutdown_v7()      # aakhri backup Drive pe
```

## 5. Message pipeline me hook

`workers/message_worker.py` (ya jahan har incoming message handle hota hai):

```python
# har message pe — reply de ya na de, seekhna band nahi hona chahiye
on_incoming_message(
    username=msg.sender_username,
    text=msg.text,
    thread_id=msg.thread_id,
    ig_user_id=msg.sender_id,
)

ctx = build_reply_context(
    text=msg.text,
    username=msg.sender_username,
    thread_id=msg.thread_id,
    bot_username=config.IG_USERNAME,
    recent_texts=[m.text for m in recent],
    recent_usernames=[m.sender_username for m in recent],
)

if not ctx["should_reply"]:
    return                        # STOP mode / mention nahi / trigger nahi

if ctx["canned_reply"]:
    send_text(client, thread_id, ctx["canned_reply"])   # order ack / rude slide
    return

system_prompt = BASE_PERSONA + "\n\n" + ctx["system_extra"]
reply = router.chat(ctx["route"], system_prompt, user_message)
```

## 6. Router swap

Jahan bhi `from intelligence import llm_router` likha hai, use badal ke:

```python
from intelligence import llm_router_v7 as llm_router
```

Signature bilkul same hai (`chat`, `chat_json`, `persist_usage`, `get_usage`),
isliye baaki code chhune ki zarurat nahi.

## 7. Naya TG panel chalao

Purane panel ko band karke:

```bash
systemctl stop aihumara-tg
python workers/tg_panel_v2.py            # pehle manually test kar
```

Telegram me bot ko `/claimadmin` bhej → phir `/panel`.

systemd service (`/etc/systemd/system/eve-tg.service`):

```ini
[Unit]
Description=Eve v7 Telegram panel
After=network-online.target

[Service]
WorkingDirectory=/root/instaai
ExecStart=/root/instaai/venv/bin/python workers/tg_panel_v2.py
Restart=always
RestartSec=5
EnvironmentFile=/root/instaai/.env

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now eve-tg
```

---

## Pehla setup (TG panel se, 2 minute)

1. `/claimadmin` → tu admin
2. **👑 IG Admins** → apna IG username daal (`dhruv_xyz`)
3. **🔑 API Keys** → Groq keys ek saath paste (har line pe ek)
4. **🏷 Nicknames** → `chotu` etc.
5. **🧠 People** → shuruaati logon ka naam/gender daal de
6. **🎭 Tone** → default personality chun
7. **▶️ START**

---

## Kya kahan hai

| File | Kaam |
|---|---|
| `storage/schema_v7.py` | Naye tables (PEOPLE, GC_PROFILE, TRIGGERS, API_KEYS, TG_STATE) |
| `storage/people.py` | Kaun kaun hai — naam, gender, rishta, style |
| `storage/gc_profile.py` | Har GC ka lehja seekhna (local, zero cost) |
| `storage/drive_sync.py` | Google Drive backup/restore |
| `intelligence/key_pool.py` | Unlimited API keys, quota rotation, failover, per-provider model |
| `intelligence/providers.py` | Groq / GrokX / Gemini / Claude / AgentRouter — live key check + model list |
| `intelligence/model_prefs.py` | Smart fallback chain: kaunsa kaam kis model se |
| `intelligence/debate_detector.py` | Banter vs political/serious debate |
| `intelligence/trigger_manager.py` | Per-user fixed tone triggers |
| `intelligence/eve_modes.py` | Modes, nicknames, filter/unfilter, `/order` |
| `intelligence/reply_policy.py` | "Reply du ya nahi, kis model se" — ek jagah |
| `intelligence/llm_router_v7.py` | Router with key pool (drop-in replacement) |
| `workers/tg_panel_v2.py` | Naya inline-button TG panel |
| `eve_v7_boot.py` | Boot + integration hooks |

---

## Speed

Reply latency ka bada hissa model ka hai, isliye:

- Normal banter → Groq `llama-3.1-8b-instant` / `70b-versatile` (~0.5-1.5s)
- People + GC context → SQLite se, koi LLM call nahi (~1ms)
- Reply dena hai ya nahi → pura local scoring, koi LLM call nahi
- Opus sirf tab jab debate detect ho

Realistic: **1-2 second**. Purane pipeline se tez, kyunki decision layer ab
LLM pe nahi jaati.

## Purani cheezein jo hatani chahiye

- `intelligence/social_judge.py` me legacy Gemini ka reference — ab
  `reply_policy.py` wahi kaam bina LLM ke karta hai. Purani file rakh sakta hai
  par pipeline se hata de.
- `intelligence/gemini_pool.py` — unused, delete kar de.
- Purana `workers/tg_panel.py` — `tg_panel_v2.py` se replace.


## Multi-provider API keys (TG panel se)

`🔑 API Keys` button → provider chun (Groq, GrokX, Gemini, Claude, AgentRouter)
→ keys paste kar (ek line me ek, jitni marzi).

Pehli key turant **live verify** hoti hai:
* sahi → `✅ Key set and connected` + us provider ke models ki list, jisme se
  model select kar sakta hai.
* galat → `❌ API key wrong hai … try again later`, kuch save nahi hota.

AgentRouter (tera Claude Opus 4.6/4.8 wala) ke liye env me:

```bash
export ANTHROPIC_AUTH_TOKEN="<agentrouter key>"
export ANTHROPIC_BASE_URL="https://agentrouter.org"
export ANTHROPIC_MODEL="claude-opus-4-6"
```

Ya seedha TG panel se `🛰 Claude via AgentRouter` chun ke key daal de — base URL
apne aap lag jata hai.

## Smart model preferences

`🧠 Models` button → har kaam ka apna chain:

| Task | Default primary |
|---|---|
| 💬 Normal baat / ⚡ Decision / 📚 Learning | Groq (fast) |
| 🔥 Roast, 😘 Flirt | Groq |
| ⚔️ Debate, 🆘 Admin /help, 🧪 Analyze | Claude (AgentRouter/Anthropic) |

Har task pe primary + 3 fallback tak set kar sakta hai. Primary fail/quota out →
agla model, bina reply ruke. `🪄 Auto set` sirf un providers se chain banata hai
jinki live key mojood hai.

## /help aur /helpover

* Admin IG pe `/help` → support mode ON, har reply admin ka side leta hai,
  debate wala heavy model chalta hai.
* `/helpover` → support mode OFF, normal Groq wapas ("help over").
* 45 min tak koi `/helpover` na aaye to khud band ho jata hai.
* TG panel me `🆘 Help mode` button se bhi off kar sakta hai.
