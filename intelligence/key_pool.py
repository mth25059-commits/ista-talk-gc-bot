"""
Eve v7 — API key pool with quota rotation + failover.

Kaise chalta hai:
  * Provider ('groq' / 'anthropic') ke liye jitni chahe keys add kar.
  * Har key ka quota (default 100 requests). Quota khatam -> agli key.
  * Koi key fail (401/429/5xx) -> turant agli key, aur us key ka fail_count++.
  * fail_count >= DEAD_AFTER -> key 'dead', pool se bahar (TG se revive ho sakti).
  * Saari keys exhausted -> pura cycle reset (wrap around) + caller ko alert.

Thread-safe: ek process-level lock + SQLite WAL.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from intelligence import providers
from storage.database import get_connection

logger = logging.getLogger("eve.keypool")

DEFAULT_QUOTA = 100
DEAD_AFTER = 5          # itne consecutive fails ke baad key dead
PROVIDERS = providers.PROVIDER_IDS

_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask(key: str) -> str:
    if not key:
        return "-"
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "…" + key[-3:]


# ----------------------------------------------------------------- manage


def add_key(provider: str, api_key: str, label: str = "",
            quota: Optional[int] = None, base_url: Optional[str] = None,
            model: Optional[str] = None, verified: bool = False) -> bool:
    """Return False agar key pehle se hai."""
    provider = provider.strip().lower()
    api_key = (api_key or "").strip()
    if provider not in PROVIDERS:
        raise ValueError(f"provider {PROVIDERS} me se hona chahiye")
    if len(api_key) < 12:
        raise ValueError("key bahut chhoti lag rahi hai")

    quota = int(quota or providers.default_quota(provider))
    model = (model or providers.default_model(provider)).strip()

    with _lock, get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM API_KEYS WHERE provider = ? AND api_key = ?", (provider, api_key)
        ).fetchone()
        if exists:
            return False
        nxt = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 p FROM API_KEYS WHERE provider = ?",
            (provider,),
        ).fetchone()["p"]
        conn.execute(
            """
            INSERT INTO API_KEYS (provider, label, api_key, model, base_url, position,
                                  quota_limit, quota_used, status, verified_at, created_at)
            VALUES (?,?,?,?,?,?,?,0,'active',?,?)
            """,
            (provider, label or f"{provider}-{nxt + 1}", api_key, model, base_url,
             nxt, max(1, quota), _now() if verified else None, _now()),
        )
    logger.info("[KEYPOOL] %s key added: %s (%s)", provider, _mask(api_key), model)
    return True


def set_model(provider: str, model: str, key_id: Optional[int] = None) -> int:
    """Provider ki sab keys ka (ya ek key ka) model badal do."""
    provider = provider.strip().lower()
    with _lock, get_connection() as conn:
        if key_id is not None:
            cur = conn.execute("UPDATE API_KEYS SET model = ? WHERE id = ?",
                               (model, int(key_id)))
        else:
            cur = conn.execute("UPDATE API_KEYS SET model = ? WHERE provider = ?",
                               (model, provider))
    logger.info("[KEYPOOL] %s -> model %s (%d keys)", provider, model, cur.rowcount)
    return cur.rowcount


def get_model(provider: str) -> str:
    """Provider ki active key pe set model, warna provider ka default."""
    for k in list_keys(provider):
        if k["status"] != "dead" and k.get("model"):
            return str(k["model"])
    return providers.default_model(provider)


def mark_verified(key_id: int) -> None:
    with _lock, get_connection() as conn:
        conn.execute("UPDATE API_KEYS SET verified_at = ? WHERE id = ?",
                     (_now(), int(key_id)))



def remove_key(key_id: int) -> bool:
    with _lock, get_connection() as conn:
        cur = conn.execute("DELETE FROM API_KEYS WHERE id = ?", (int(key_id),))
    return cur.rowcount > 0


def clear_provider(provider: str) -> int:
    with _lock, get_connection() as conn:
        cur = conn.execute("DELETE FROM API_KEYS WHERE provider = ?", (provider.lower(),))
    return cur.rowcount


def list_keys(provider: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM API_KEYS"
    args: tuple = ()
    if provider:
        sql += " WHERE provider = ?"
        args = (provider.lower(),)
    sql += " ORDER BY provider, position"
    with get_connection() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        d["masked"] = _mask(d["api_key"])
        out.append(d)
    return out


def reset_quotas(provider: Optional[str] = None) -> int:
    with _lock, get_connection() as conn:
        if provider:
            cur = conn.execute(
                "UPDATE API_KEYS SET quota_used = 0, status = CASE WHEN status = 'dead'"
                " THEN 'dead' ELSE 'active' END WHERE provider = ?",
                (provider.lower(),),
            )
        else:
            cur = conn.execute(
                "UPDATE API_KEYS SET quota_used = 0, status = CASE WHEN status = 'dead'"
                " THEN 'dead' ELSE 'active' END"
            )
    return cur.rowcount


def revive_key(key_id: int) -> bool:
    with _lock, get_connection() as conn:
        cur = conn.execute(
            "UPDATE API_KEYS SET status = 'active', fail_count = 0, quota_used = 0,"
            " last_error = NULL WHERE id = ?",
            (int(key_id),),
        )
    return cur.rowcount > 0


# ------------------------------------------------------------------ acquire


def acquire(provider: str, skip_ids: Optional[List[int]] = None) -> Optional[Dict[str, Any]]:
    """
    Agli usable key do (position order me, jiska quota bacha ho).
    Sab exhausted -> auto wrap-around (quota reset) aur dobara try.
    Koi active key hi nahi -> None.
    """
    provider = provider.strip().lower()
    skip = set(skip_ids or [])

    with _lock:
        for attempt in (1, 2):
            with get_connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM API_KEYS WHERE provider = ? AND status = 'active'"
                    " ORDER BY position",
                    (provider,),
                ).fetchall()
            for r in rows:
                if r["id"] in skip:
                    continue
                if int(r["quota_used"]) < int(r["quota_limit"]):
                    return {k: r[k] for k in r.keys()}

            if attempt == 1:
                usable = [r for r in rows if r["id"] not in skip]
                if not usable:
                    return None
                logger.warning("[KEYPOOL] %s ki saari keys exhausted — cycle reset", provider)
                reset_quotas(provider)
        return None


def report_success(key_id: int) -> None:
    with _lock, get_connection() as conn:
        conn.execute(
            "UPDATE API_KEYS SET quota_used = quota_used + 1, fail_count = 0,"
            " last_used_at = ?, status = CASE WHEN quota_used + 1 >= quota_limit"
            " THEN 'exhausted' ELSE status END WHERE id = ?",
            (_now(), int(key_id)),
        )


def report_failure(key_id: int, error: str, fatal: bool = False) -> None:
    """
    fatal=True (401/403 jaise auth errors) -> key turant dead.
    warna fail_count badhega, DEAD_AFTER pe dead.
    """
    err = (error or "")[:300]
    with _lock, get_connection() as conn:
        conn.execute(
            "UPDATE API_KEYS SET fail_count = fail_count + 1, last_error = ?,"
            " last_used_at = ? WHERE id = ?",
            (err, _now(), int(key_id)),
        )
        if fatal:
            conn.execute("UPDATE API_KEYS SET status = 'dead' WHERE id = ?", (int(key_id),))
        else:
            conn.execute(
                "UPDATE API_KEYS SET status = 'dead' WHERE id = ? AND fail_count >= ?",
                (int(key_id), DEAD_AFTER),
            )
    logger.warning("[KEYPOOL] key #%s failed (fatal=%s): %s", key_id, fatal, err)


def is_fatal_error(err: Exception) -> bool:
    t = str(err).lower()
    return any(s in t for s in ("401", "403", "invalid api key", "unauthorized",
                                "authentication", "permission_denied", "account"))


def is_rate_limit(err: Exception) -> bool:
    t = str(err).lower()
    return "429" in t or "rate" in t or "quota" in t or "too many" in t


# ------------------------------------------------------------------ status


def status_text(provider: Optional[str] = None) -> str:
    keys = list_keys(provider)
    if not keys:
        return "Koi API key add nahi hai abhi."
    by: Dict[str, List[Dict[str, Any]]] = {}
    for k in keys:
        by.setdefault(k["provider"], []).append(k)
    out = []
    for prov, ks in by.items():
        active = sum(1 for k in ks if k["status"] == "active")
        out.append(f"\n{providers.label(prov)} — {len(ks)} keys ({active} active)"
                   f"\nmodel: {ks[0].get('model') or providers.default_model(prov)}")
        for k in ks:
            icon = {"active": "🟢", "exhausted": "🟡", "dead": "🔴"}.get(k["status"], "⚪")
            out.append(
                f"{icon} #{k['id']} {k['label']} {k['masked']}"
                f" — {k['quota_used']}/{k['quota_limit']}"
                + (" ✅" if k.get("verified_at") else "")
                + (f" | err: {str(k['last_error'])[:40]}" if k["last_error"] else "")
            )
    return "\n".join(out).strip()


def seed_from_env() -> int:
    """
    .env me padi purani single keys ko pool me le aao (ek baar chalega).
    GROQ_API_KEY(S), ANTHROPIC_API_KEY, AGENTROUTER_KEY, XAI_API_KEY, GEMINI_API_KEY.
    """
    import config

    added = 0
    raw_groq = getattr(config, "GROQ_API_KEYS", "") or ""
    candidates = [k.strip() for k in str(raw_groq).split(",") if k.strip()]
    single = (getattr(config, "GROQ_API_KEY", "") or "").strip()
    if single and single not in candidates:
        candidates.append(single)
    for k in candidates:
        try:
            if add_key("groq", k, label="from-env"):
                added += 1
        except ValueError:
            pass

    simple = {
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
        "xai": ("XAI_API_KEY", "GROK_API_KEY"),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }
    for prov, names in simple.items():
        for name in names:
            val = (getattr(config, name, "") or "").strip()
            if not val:
                continue
            try:
                if add_key(prov, val, label="from-env"):
                    added += 1
            except ValueError:
                pass
            break

    ar = ((getattr(config, "AGENTROUTER_KEY", "") or "")
          or (getattr(config, "AGENTROUTER_API_KEY", "") or "")).strip()
    if ar:
        try:
            if add_key("agentrouter", ar, label="agentrouter",
                       base_url=getattr(config, "AGENTROUTER_BASE",
                                        "https://agentrouter.org"),
                       model=getattr(config, "AGENTROUTER_MODEL", "") or None):
                added += 1
        except ValueError:
            pass


    if added:
        logger.info("[KEYPOOL] %d keys .env se import ki", added)
    return added
