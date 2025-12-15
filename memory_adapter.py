
from __future__ import annotations

import sqlite3
import threading
import time
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

_WORD_RE = re.compile(r"[a-z0-9']+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _now_ts() -> int:
    return int(time.time())


def _tokens(text: str) -> List[str]:
    return _WORD_RE.findall(text.lower())


def _unique(seq: Sequence[str]) -> List[str]:
    seen = set()
    out = []
    for s in seq:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _sentence_split(text: str) -> List[str]:
    # Simple splitter that keeps punctuation boundaries
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _keyword_overlap_score(q_tokens: List[str], m_tokens: List[str]) -> float:
    if not q_tokens or not m_tokens:
        return 0.0
    q_set, m_set = set(q_tokens), set(m_tokens)
    inter = len(q_set & m_set)
    # Cosine-ish normalization
    import math
    denom = math.sqrt(len(q_set) * len(m_set)) or 1.0
    return inter / denom


def _recency_score(ts: int, half_life_days: float = 30.0) -> float:
    # Exponential decay: score=0.5 each half-life
    age_sec = max(0, _now_ts() - ts)
    half_life_sec = half_life_days * 86400.0
    if half_life_sec <= 0:
        return 1.0
    import math
    return 0.5 ** (age_sec / half_life_sec)


@dataclass
class MemoryConfig:
    db_path: str = "jarvis_mem.db"
    half_life_days: float = 30.0
    min_sentence_len: int = 8      # chars
    max_sentence_len: int = 240    # chars
    prefer_first_k_sentences: int = 5
    write_coalesce_seconds: int = 1


class MemoryAdapter:
    """Local memory: SQLite + simple ranking (keywords + recency).

    Methods:
        add(messages, user_id, agent_id, namespace, source, importance)
        search(query, user_id, namespace, k)
        forget(memory_id)
        wipe_user(user_id)
        export_user(user_id) -> list[dict]
        import_user(user_id, items)
    """

    def __init__(self, db_path: str = "jarvis_mem.db", config: Optional[MemoryConfig] = None):
        self.config = config or MemoryConfig(db_path=db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.config.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._init_schema()

    # ---------- public API ----------
    def add(
        self,
        messages: Sequence[Dict[str, str]],
        user_id: str,
        *,
        agent_id: Optional[str] = None,
        namespace: str = "default",
        source: str = "dialogue",
        importance: float = 0.5,
    ) -> int:
        """Extract salient sentences from a turn and store them.

        `messages` is a short list like:
            [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
        Returns the count of inserted memories.
        """
        facts = self._extract_salient_sentences(messages)
        if not facts:
            return 0
        with self._lock:
            cur = self._conn.cursor()
            inserted = 0
            for text in facts:
                try:
                    cur.execute(
                        """
                        INSERT INTO memories(user_id, agent_id, namespace, ts, text, source, importance)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (user_id, agent_id, namespace, _now_ts(), text, source, float(importance)),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    # duplicate text for same user+namespace — ignore
                    pass
            self._conn.commit()
        return inserted
    def search(
        self,
        query: str,
        user_id: str,
        *,
        namespace: Optional[str] = None,
        k: int = 5,
        max_scan: int = 800  # cap rows to score (most recent first)
    ) -> List[Dict]:
        q_toks = _tokens(query)
        with self._lock:
            cur = self._conn.cursor ()
            if namespace:
                cur.execute(
                    "SELECT id, text, ts, importance FROM memories "
                    "WHERE user_id=? AND namespace=? "
                    "ORDER BY ts DESC LIMIT ?",
                    (user_id, namespace, int(max_scan)),
                )
            else:
                 cur.execute(
                    "SELECT id, text, ts, importance FROM memories "
                    "WHERE user_id=? "
                    "ORDER BY ts DESC LIMIT ?",
                    (user_id, int(max_scan)),
                )
            rows = cur.fetchall()

        # trivial queries? bail fast
        if len(q_toks) < 2:
            return rows[:max(1, min(k, len(rows)))] if rows else []

        # score
        scored = []
        for mid, text, ts, imp in rows:
            m_toks = _tokens(text)
            ko = _keyword_overlap_score(q_toks, m_toks)
            rs = _recency_score(ts, self.config.half_life_days)
            score = 0.65 * ko + 0.25 * rs + 0.10 * float(imp or 0.0)
            scored.append({"id": mid, "text": text, "ts": ts, "score": score})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[: max(1, k)]

    

    def forget(self, memory_id: int) -> bool:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def wipe_user(self, user_id: str) -> int:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM memories WHERE user_id=?", (user_id,))
            self._conn.commit()
            return cur.rowcount

    def export_user(self, user_id: str) -> List[Dict]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT id, user_id, agent_id, namespace, ts, text, source, importance FROM memories WHERE user_id=?",
                (user_id,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def import_user(self, user_id: str, items: Sequence[Dict]) -> int:
        cnt = 0
        with self._lock:
            cur = self._conn.cursor()
            for it in items:
                try:
                    cur.execute(
                        """
                        INSERT INTO memories(user_id, agent_id, namespace, ts, text, source, importance)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            it.get("agent_id"),
                            it.get("namespace", "default"),
                            int(it.get("ts", _now_ts())),
                            it["text"],
                            it.get("source", "import"),
                            float(it.get("importance", 0.5)),
                        ),
                    )
                    cnt += 1
                except sqlite3.IntegrityError:
                    pass
            self._conn.commit()
        return cnt

    # ---------- internals ----------
    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT NOT NULL,
                  agent_id TEXT,
                  namespace TEXT,
                  ts INTEGER NOT NULL,
                  text TEXT NOT NULL,
                  source TEXT,
                  importance REAL,
                  UNIQUE(user_id, namespace, text)
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_user_ns ON memories(user_id, namespace);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(ts);")
            self._conn.commit()

    def _extract_salient_sentences(self, messages: Sequence[Dict[str, str]]) -> List[str]:
        # Focus on short, declarative facts from user + assistant
        raw = []
        for m in messages:
            role = (m.get("role") or "").lower()
            if role in {"user", "assistant"}:
                raw.append(m.get("content", ""))
        text = "\n".join(raw)
        sents = _sentence_split(text)

        keep = []
        for s in sents:
            sl = len(s)
            if sl < self.config.min_sentence_len or sl > self.config.max_sentence_len:
                continue
            # Heuristics: keep preference/bio/todo-style sentences
            if re.search(r"\b(I|I'm|I am|my|prefer|always|from now on|call me|remind|major|college|Jarvis|voice|schedule|birthday|tennis)\b", s, re.I):
                keep.append(s)
                continue
            # otherwise keep if it has at least 8 alphabetic words (likely a complete thought)
            if sum(1 for t in _tokens(s) if t.isalpha()) >= 8:
                keep.append(s)
        # Deduplicate and cap
        keep = _unique(keep)[: self.config.prefer_first_k_sentences]
        return keep


# ---------- integration helper (optional) ----------

def build_memory_system_prefix(hits: List[Dict]) -> str:
    if not hits:
        return "Known user facts: (none)"
    lines = ["Known user facts (keep brief, only use when relevant):"]
    for h in hits:
        lines.append(f"- {h['text']}")
    return "\n".join(lines)


# ---------- CLI demo ----------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Jarvis local memory demo")
    ap.add_argument("action", choices=["add", "search", "export", "wipe"], help="action")
    ap.add_argument("--user", default="roman")
    ap.add_argument("--ns", "--namespace", default="personal")
    ap.add_argument("--query", default="")
    ap.add_argument("--text", default="")
    args = ap.parse_args()

    mem = MemoryAdapter(db_path="jarvis_mem.db")

    if args.action == "add":
        messages = [
            {"role": "user", "content": args.text or "Call me sir and prefer a concise, formal tone."},
            {"role": "assistant", "content": "Understood."},
        ]
        n = mem.add(messages, user_id=args.user, agent_id="jarvis-desktop", namespace=args.ns)
        print(f"Inserted {n} memories.")

    elif args.action == "search":
        hits = mem.search(args.query or "preferences", user_id=args.user, namespace=args.ns, k=5)
        for h in hits:
            print(f"({h['score']:.3f}) {h['text']}")

    elif args.action == "export":
        print(json.dumps(mem.export_user(args.user), indent=2))

    elif args.action == "wipe":
        n = mem.wipe_user(args.user)
        print(f"Deleted {n} rows for user {args.user}.")
