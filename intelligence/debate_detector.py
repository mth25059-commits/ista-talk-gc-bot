"""
Eve v7 — debate detector.

Decide karta hai ki ye normal banter hai ya serious debate/political fight.
Serious hua to router Opus 4.8 pe switch karta hai, warna Groq.

Pura local scoring — koi LLM call nahi, isliye zero latency + zero cost.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger("eve.debate")

THRESHOLD = 5.0          # is score se upar -> Opus

_POLITICAL = {
    "modi", "rahul gandhi", "bjp", "congress", "aap", "kejriwal", "yogi",
    "election", "chunav", "vote", "government", "sarkar", "policy", "reservation",
    "hindu", "muslim", "religion", "dharm", "caste", "jaati", "communal",
    "pakistan", "china", "kashmir", "article 370", "cаа", "caa", "nrc",
    "left", "right wing", "liberal", "propaganda", "godi media", "secular",
}

_DEBATE_MARKERS = {
    "proof", "saboot", "source", "fact", "facts", "logic", "logically",
    "prove", "wrong", "galat", "sahi", "argument", "debate", "actually",
    "statistics", "data", "research", "study", "according to", "evidence",
    "constitution", "law", "kanoon", "history", "itihas",
}

_HEAT = {
    "shut up", "chup", "idiot", "stupid", "murkh", "andhbhakt", "bhakt",
    "sickular", "chamcha", "tere jaise", "aukat", "ghatiya", "nikamma",
    "besharam", "jhooth", "jhoota", "liar", "fake", "chutiyapa",
}

_SERIOUS_TOPICS = {
    "economy", "gdp", "inflation", "unemployment", "war", "yudh", "climate",
    "science", "medicine", "vaccine", "court", "supreme court", "verdict",
    "farmer", "kisan", "budget", "tax", "gst",
}

_QUESTION_RE = re.compile(r"\?")


def _count(text: str, vocab) -> int:
    low = text.lower()
    return sum(1 for w in vocab if w in low)


def score(text: str, recent_texts: Optional[List[str]] = None) -> float:
    """
    0-10 scale. Zyada = zyada serious/debate.
    recent_texts: GC ke last kuch messages (context se pata chalta hai maahol garam hai).
    """
    if not text:
        return 0.0

    s = 0.0
    s += _count(text, _POLITICAL) * 3.0
    s += _count(text, _DEBATE_MARKERS) * 1.5
    s += _count(text, _SERIOUS_TOPICS) * 2.0
    s += _count(text, _HEAT) * 1.0

    # lambi baat = serious baat
    n = len(text)
    if n > 220:
        s += 2.0
    elif n > 120:
        s += 1.0

    # sawaal poochha ja raha hai
    if _QUESTION_RE.search(text) and n > 60:
        s += 1.0

    # GC context — pichle messages me bhi bahas chal rahi hai
    if recent_texts:
        window = recent_texts[-8:]
        ctx = 0.0
        for t in window:
            ctx += _count(t or "", _POLITICAL) * 1.2
            ctx += _count(t or "", _DEBATE_MARKERS) * 0.6
            ctx += _count(t or "", _HEAT) * 0.5
        s += min(ctx, 4.0)

    return round(min(s, 10.0), 2)


def classify(text: str, recent_texts: Optional[List[str]] = None) -> Dict[str, object]:
    sc = score(text, recent_texts)
    low = (text or "").lower()
    political = _count(low, _POLITICAL) > 0
    heated = _count(low, _HEAT) > 0

    if sc >= THRESHOLD:
        kind = "political_debate" if political else "serious_debate"
    elif heated:
        kind = "roast_fight"
    else:
        kind = "banter"

    return {
        "score": sc,
        "kind": kind,
        "needs_opus": sc >= THRESHOLD,
        "political": political,
        "heated": heated,
    }


def needs_opus(text: str, recent_texts: Optional[List[str]] = None) -> bool:
    return score(text, recent_texts) >= THRESHOLD
