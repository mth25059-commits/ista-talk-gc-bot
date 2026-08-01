"""
Eve v7 — provider registry + live key validation + model discovery.

Supported providers (TG panel me buttons inhi se bante hain):

    groq         -> Groq cloud            (OpenAI-compatible)
    xai          -> Grok / GrokX (xAI)    (OpenAI-compatible)
    gemini       -> Google Gemini         (OpenAI-compatible endpoint)
    anthropic    -> Official Claude       (Anthropic messages API)
    agentrouter  -> AgentRouter ka Claude (Anthropic messages API, alag base URL)

Har provider ke liye 3 kaam yahan hote hain:
  1. validate(key)      -> key sach me chalti hai ya nahi (live check)
  2. list_models(key)   -> us key se kaunse model mil rahe hain
  3. chat(...)          -> actual completion call (router isko use karta hai)

Koi SDK dependency nahi — sirf `requests`. Isse naye provider add karna
2 line ka kaam ho jata hai.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("eve.providers")

KIND_OPENAI = "openai"
KIND_ANTHROPIC = "anthropic"

TIMEOUT = 30

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "groq": {
        "label": "⚡ Groq",
        "kind": KIND_OPENAI,
        "base_url": "https://api.groq.com/openai/v1",
        "default_quota": 100,
        "fallback_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "openai/gpt-oss-120b",
        ],
        "prefer": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
    },
    "xai": {
        "label": "🤖 GrokX (xAI)",
        "kind": KIND_OPENAI,
        "base_url": "https://api.x.ai/v1",
        "default_quota": 100,
        "fallback_models": ["grok-4", "grok-4-fast", "grok-3", "grok-3-mini"],
        "prefer": ["grok-4", "grok-3"],
    },
    "gemini": {
        "label": "💎 Gemini",
        "kind": KIND_OPENAI,
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "native_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_quota": 100,
        "fallback_models": [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
        ],
        "prefer": ["gemini-2.5-flash", "gemini-2.5-pro"],
    },
    "anthropic": {
        "label": "🧠 Claude (official)",
        "kind": KIND_ANTHROPIC,
        "base_url": "https://api.anthropic.com",
        "default_quota": 10_000,
        "fallback_models": [
            "claude-opus-4-8",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
        ],
        "prefer": ["claude-opus-4-8"],
    },
    "agentrouter": {
        "label": "🛰 Claude via AgentRouter",
        "kind": KIND_ANTHROPIC,
        "base_url": "https://agentrouter.org",
        "default_quota": 10_000,
        "fallback_models": [
            "claude-opus-4-6",
            "claude-opus-4-8",
            "claude-sonnet-4-5",
        ],
        "prefer": ["claude-opus-4-6"],
    },
}

PROVIDER_IDS = tuple(PROVIDERS)


def spec(provider: str) -> Dict[str, Any]:
    p = (provider or "").strip().lower()
    if p not in PROVIDERS:
        raise ValueError(f"unknown provider '{provider}' — {list(PROVIDERS)} me se do")
    return PROVIDERS[p]


def label(provider: str) -> str:
    try:
        return spec(provider)["label"]
    except ValueError:
        return provider


def base_url_for(provider: str, override: Optional[str] = None) -> str:
    return (override or spec(provider)["base_url"]).rstrip("/")


def default_quota(provider: str) -> int:
    return int(spec(provider)["default_quota"])


def default_model(provider: str) -> str:
    s = spec(provider)
    return (s["prefer"] or s["fallback_models"])[0]


# ------------------------------------------------------------- headers


def _headers(provider: str, api_key: str) -> Dict[str, str]:
    s = spec(provider)
    if s["kind"] == KIND_ANTHROPIC:
        # Official Anthropic x-api-key maangta hai, AgentRouter Bearer token —
        # dono bhej dete hain, jo chahiye wo utha lega.
        return {
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }


# ------------------------------------------------------------- models


def list_models(provider: str, api_key: str,
                base_url: Optional[str] = None) -> List[str]:
    """Key se available models nikaalo. Fail ho to fallback list."""
    s = spec(provider)
    p = provider.lower()
    try:
        if p == "gemini":
            r = requests.get(
                f"{s['native_url']}/models",
                params={"key": api_key, "pageSize": 200},
                timeout=TIMEOUT,
            )
            if r.ok:
                out = []
                for m in r.json().get("models", []):
                    if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                        continue
                    out.append(str(m.get("name", "")).split("/")[-1])
                if out:
                    return _sorted_models(p, out)
        else:
            url = base_url_for(p, base_url)
            path = "/v1/models" if s["kind"] == KIND_ANTHROPIC else "/models"
            r = requests.get(url + path, headers=_headers(p, api_key), timeout=TIMEOUT)
            if r.ok:
                body = r.json()
                rows = body.get("data") or body.get("models") or []
                out = [str(x.get("id") or x.get("name") or "") for x in rows]
                out = [x for x in out if x]
                if out:
                    return _sorted_models(p, out)
    except requests.RequestException as e:
        logger.warning("[PROV] %s model list fail: %s", provider, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[PROV] %s model list parse fail: %s", provider, e)
    return list(s["fallback_models"])


def _sorted_models(provider: str, models: List[str]) -> List[str]:
    """Chat-worthy models upar, embeddings/tts/vision-only neeche/hataye."""
    junk = ("embed", "whisper", "tts", "guard", "moderation", "aqa", "imagen",
            "veo", "image", "rerank")
    clean = [m for m in dict.fromkeys(models) if not any(j in m.lower() for j in junk)]
    prefer = spec(provider)["prefer"]

    def rank(m: str) -> tuple:
        for i, p in enumerate(prefer):
            if m == p:
                return (0, i, m)
        for i, p in enumerate(prefer):
            if p.split("-")[0] in m:
                return (1, i, m)
        return (2, 0, m)

    return sorted(clean, key=rank) or list(spec(provider)["fallback_models"])


# ----------------------------------------------------------- validation


def validate(provider: str, api_key: str,
             base_url: Optional[str] = None) -> Tuple[bool, str, List[str]]:
    """
    Live check: (ok, message, models).

    ok=False hone pe message me saaf reason hota hai — TG pe wahi dikhta hai
    ("API key wrong hai" / "rate limit" / "network down").
    """
    api_key = (api_key or "").strip()
    if len(api_key) < 12:
        return False, "key bahut chhoti hai — poori key bhej.", []

    s = spec(provider)
    p = provider.lower()
    try:
        if s["kind"] == KIND_ANTHROPIC:
            url = base_url_for(p, base_url) + "/v1/messages"
            r = requests.post(
                url,
                headers=_headers(p, api_key),
                json={
                    "model": default_model(p),
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                timeout=TIMEOUT,
            )
        elif p == "gemini":
            r = requests.get(
                f"{s['native_url']}/models",
                params={"key": api_key, "pageSize": 1},
                timeout=TIMEOUT,
            )
        else:
            r = requests.get(
                base_url_for(p, base_url) + "/models",
                headers=_headers(p, api_key),
                timeout=TIMEOUT,
            )
    except requests.RequestException as e:
        return False, f"network error — baad me try kar ({e.__class__.__name__})", []

    if r.status_code in (200, 201):
        return True, "key sahi hai — connected ✅", list_models(p, api_key, base_url)
    if r.status_code in (401, 403):
        return False, "API key wrong hai ya permission nahi — dobara check kar.", []
    if r.status_code == 429:
        # Key valid hai, bas abhi limit lagi hai.
        return True, "key sahi hai (abhi rate-limit chal rahi hai) ⚠️", \
            list(s["fallback_models"])
    if r.status_code == 404:
        # Model exist nahi karta par auth pass ho gaya.
        return True, "key sahi hai (default model available nahi) ⚠️", \
            list_models(p, api_key, base_url)
    detail = (r.text or "")[:180].replace("\n", " ")
    return False, f"provider ne {r.status_code} diya — try again later. {detail}", []


# ---------------------------------------------------------------- chat


def chat(provider: str, api_key: str, model: str, system: str, user: str,
         **kw: Any) -> str:
    """Sirf text chahiye to ye. Usage bhi chahiye to `chat_ex`."""
    return chat_ex(provider, api_key, model, system, user, **kw)[0]


def chat_ex(provider: str, api_key: str, model: str, system: str, user: str,
            *, base_url: Optional[str] = None, max_tokens: int = 220,
            temperature: float = 0.9,
            json_mode: bool = False) -> Tuple[str, int, int]:
    """
    Ek unified completion call -> (text, input_tokens, output_tokens).
    Error pe exception uthata hai (status code message me hota hai) taaki
    key_pool sahi failover kar sake.
    """
    s = spec(provider)
    p = provider.lower()
    url_base = base_url_for(p, base_url)

    if s["kind"] == KIND_ANTHROPIC:
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        r = requests.post(url_base + "/v1/messages", headers=_headers(p, api_key),
                          json=payload, timeout=90)
        _raise_for(r, p)
        body = r.json()
        parts = [b.get("text", "") for b in body.get("content", [])
                 if b.get("type") == "text"]
        tin, tout = token_usage(p, body)
        return "".join(parts).strip(), tin, tout

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    r = requests.post(url_base + "/chat/completions", headers=_headers(p, api_key),
                      json=payload, timeout=90)
    _raise_for(r, p)
    body = r.json()
    tin, tout = token_usage(p, body)
    return (body["choices"][0]["message"].get("content") or "").strip(), tin, tout


def token_usage(provider: str, body: Dict[str, Any]) -> Tuple[int, int]:
    u = body.get("usage") or {}
    if spec(provider)["kind"] == KIND_ANTHROPIC:
        return int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
    return int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def _raise_for(r: requests.Response, provider: str) -> None:
    if r.status_code < 400:
        return
    detail = (r.text or "")[:200].replace("\n", " ")
    raise RuntimeError(f"{provider} HTTP {r.status_code}: {detail}")
