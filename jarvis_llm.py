import os, json, time, subprocess, webbrowser, difflib, hashlib, collections, queue, re
from datetime import datetime
from urllib.parse import quote_plus
from pathlib import Path
import random
import shutil
import requests
from flask import Flask, request, jsonify  # NEW
import threading

import re
from ddgs import DDGS
from jarvis_see import describe_current_view, describe_element_at_cursor
# ---- streaming ASR (Whisper) ----
from jarvis_listen_whisper_stream import WhisperStreamer, start_asr_governor
# --- Local memory (SQLite) ---
from memory_adapter import MemoryAdapter, build_memory_system_prefix
DEBUG = True
def dprint(*a):
    if DEBUG:
        print("[DBG]", *a)

# ---- commands (your command handlers live here) ----
from commands import dispatch

SEE_USE_HTTP = False  # set True if you want to call the separate see server
SEE_BASE = "http://127.0.0.1:8770"  # use a port that does NOT collide with the HUD (8765)

def cmd_what_am_i_looking_at_summary(brain=None) -> str:
    try:
        if SEE_USE_HTTP:
            r = requests.get(
                f"{SEE_BASE}/what_am_i_looking_at",
                params={"acc": "1", "ocr": "1", "mode": "summary", "lang": "eng"},
                timeout=5
            )
            j = r.json()
            app_title = ((j.get("app") or {}).get("title")
                         or (j.get("app") or {}).get("name")
                         or "this app")
            summary = j.get("summary") or "No readable text found."
            return f"{app_title}: {summary}"
        else:
            info = describe_current_view(prefer_accessibility=True, include_ocr=True, ocr_lang="eng")
            raw = "\n".join([info.get("accessibility_text",""), info.get("ocr_text","")]).strip()
            if not raw:
                return "I couldn't read anything meaningful on the screen, sir."
            if brain is not None:
                # let the LLM compress the raw text
                return brain.ask("Summarize concisely for a blind user:\n" + raw[:2000])
            # lightweight local summary
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            return " ".join(lines[:3])[:500]
    except Exception as e:
        return f"Apologies, sir. Vision module failed: {e}"

def cmd_what_am_i_looking_at() -> str:
    try:
        if SEE_USE_HTTP:
            r = requests.get(
                f"{SEE_BASE}/what_am_i_looking_at",
                params={"acc": "1", "ocr": "1", "mode": "verbose", "lang": "eng"},
                timeout=8
            )
            j = r.json()
            acc = (j.get("accessibility_text") or "").strip()
            ocr = (j.get("ocr_text") or "").strip()
            text = (acc + "\n" + ocr).strip()
            return text[:1500] if text else "No readable text found, sir."
        else:
            info = describe_current_view(prefer_accessibility=True, include_ocr=True, ocr_lang="eng")
            acc = (info.get("accessibility_text") or "").strip()
            ocr = (info.get("ocr_text") or "").strip()
            text = (acc + "\n" + ocr).strip()
            return text[:1500] if text else "No readable text found, sir."
    except Exception as e:
        return f"Apologies, sir. Vision module failed: {e}"

def cmd_whats_under_mouse() -> str:
    try:
        if SEE_USE_HTTP:
            r = requests.get(
                f"{SEE_BASE}/what_is_under_mouse",
                params={"ocr": "1", "lang": "eng", "box": "300"},
                timeout=5
            )
            j = r.json()
        else:
            j = describe_element_at_cursor(include_ocr_fallback=True, ocr_lang="eng", ocr_box_px=300)

        # Prefer the human-ready summary your jarvis_see returns
        summary = (j.get("summary") or "").strip()
        if summary:
            return summary[:500]

        # If you want to compose something from UIA fields:
        uia = j.get("uia") or {}
        bits = []
        if uia.get("control_type"): bits.append(f"Control: {uia['control_type']}")
        if uia.get("name"):         bits.append(f"Label/Text: {uia['name']}")
        if uia.get("value"):        bits.append(f"Value: {uia['value']}")
        if uia.get("help_text"):    bits.append(f"Help: {uia['help_text']}")
        if uia.get("access_key"):   bits.append(f"Access Key: {uia['access_key']}")
        if uia.get("automation_id"):bits.append(f"AutomationId: {uia['automation_id']}")
        if uia.get("class_name"):   bits.append(f"Class: {uia['class_name']}")
        if not bits and j.get("ocr_nearby"):
            bits.append("OCR nearby: " + " ".join(j["ocr_nearby"].split())[:160])

        out = ", ".join(bits)
        return out[:500] if out else "Nothing recognizable under the cursor, sir."
    except Exception as e:
        return f"Apologies, sir. Vision module failed: {e}"
       
# ===================== PATHS / ENV =====================
BASE_DIR = Path(__file__).resolve().parent
ROOT = str(BASE_DIR)
# ===================== MEMORY (Local) =====================
ENABLE_MEMORY = os.getenv("JARVIS_ENABLE_MEMORY", "1") == "1"
MEM = MemoryAdapter(db_path=os.path.join(ROOT, "jarvis_mem.db")) if ENABLE_MEMORY else None
MEM_USER = "roman"
MEM_NAMESPACE = "personal"
# TTS assets (Piper)
PIPER_EXE_ENV   = os.getenv("PIPER_EXE")
PIPER_VOICE_ENV = os.getenv("PIPER_VOICE")
TTS_OUT_DIR     = BASE_DIR / "tts"  # keep .wav files out of the root

# --- Ollama config ---
OLLAMA_HOST  = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT  = os.environ.get("OLLAMA_PORT", "11434")
OLLAMA_URL   = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat"  # CHANGED: Use /api/chat for tool support
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")  # CHANGED: smollm2 may not support tools; use llama3 or tool-compatible model

# --- HUD endpoint ---
HUD_URL = "http://127.0.0.1:8765/event"

# ===================== RUNTIME CONFIG =====================
# “Address me by name” behavior (hotword + fuzzy)
REQUIRE_NAME  = True
NAME_ALIASES  = ("jarvis", "jervis", "jarvish", "jarvz")
EDGE_FILLERS  = {"has", "the", "uh", "um", "uhh", "huh", "yeah", "okay", "ok"}

# LLM control
FORCE_NO_LLM = False
MEMORY_TURNS = 10

# Apps
SPOTIFY_EXE = r"%APPDATA%\Spotify\Spotify.exe"
CHROME_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
]

# Jarvis persona for LLM
JARVIS_SYSTEM = (
    "You are Jarvis: a composed British valet–style personal assistant. "
    "Always address the user as 'sir' or 'Mr. May'. "
    "Be crisp, precise, and courteous. Prefer short sentences with understated confidence. "
    "Avoid slang. Offer succinct confirmations like 'Very well, sir.' or 'At once, sir.' when appropriate. "
    "Use the web_search tool for queries requiring current or post-2024 information."
    "Use the web_search tool only for current events or facts that require the internet. "
    "Never use web_search for mathematics, calculus, unit conversions, or symbolic work—do them yourself."
)

# NEW: Web search tool definition for Ollama
tools = [
    {
        'type': 'function',
        'function': {
            'name': 'web_search',
            'description': 'Search the web using DuckDuckGo to retrieve current and accurate information when built-in knowledge is insufficient or outdated.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'A well-formed search query to retrieve relevant information.',
                    },
                },
                'required': ['query'],
            },
        },
    }
]

# NEW: Web search function using DuckDuckGo
USER_AGENT = os.getenv("JARVIS_UA", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SEARCH_MIN_GAP = float(os.getenv("JARVIS_SEARCH_MIN_GAP", "2.0"))  # min seconds between calls
_last_search_ts = 0.0

def _ddg_text(query, backend="lite", max_results=5):
    # backend ∈ {"lite","html","api"}. "lite" is least rate-limited.
    with DDGS() as ddgs:
        return list(ddgs.text(
            query,
            region="wt-wt",
            safesearch="moderate",
            max_results=max_results,
            backend=backend,      # <- important
        ))

def web_search(query, max_results=5):
    global _last_search_ts

    # simple throttle to avoid 202 Ratelimit
    now = time.time()
    gap = _SEARCH_MIN_GAP - (now - _last_search_ts)
    if gap > 0:
        time.sleep(gap)
    _last_search_ts = time.time()

    # 1) try DDG "lite" with retries
    for attempt in range(3):
        try:
            results = _ddg_text(query, backend="lite", max_results=max_results)
            if results:
                return "\n\n".join(
                    f"Title: {r.get('title','')}\nSnippet: {r.get('body','')}\nLink: {r.get('href','')}"
                    for r in results
                )
            # no results → try another backend
            break
        except Exception as e:
            msg = str(e)
            if ("Ratelimit" in msg) or ("202" in msg) or ("429" in msg):
                time.sleep(1.0 + attempt * 1.25 + random.random())  # backoff + jitter
                continue
            # other error → bail to fallback
            break

    # 2) fallback: DDG "html" backend (slightly different scraper)
    try:
        results = _ddg_text(query, backend="html", max_results=max_results)
        if results:
            return "\n\n".join(
                f"Title: {r.get('title','')}\nSnippet: {r.get('body','')}\nLink: {r.get('href','')}"
                for r in results
            )
    except Exception:
        pass
    try:
        open_chrome("https://duckduckgo.com/?q=" + quote_plus(query))
    except Exception:
        pass
    return "Search engine is rate-limiting at the moment, sir. I’ve opened results in your browser."

# ===================== HUD EMIT =====================
def hud_emit(kind: str, **payload):
    """Send event to HUD; ignore failures (HUD may not be running)."""
    try:
        requests.post(HUD_URL, json={"type": kind, **payload}, timeout=0.4)
    except Exception as e:
        print(f"[Error] Failed to launch HUD: {e}")

def hud_assistant(text: str):
    try:
        requests.post("http://127.0.0.1:8765/event",
                      json={"type":"assistant","text":text}, timeout=0.25)
    except Exception:
        pass


# ===================== TTS (Piper) =====================


try:
    import winsound
    _HAS_WINSOUND = hasattr(winsound, "PlaySound") and hasattr(winsound, "SND_FILENAME")
except Exception:
    winsound = None
    _HAS_WINSOUND = False

def _resolve_piper_exe() -> str:
    candidates = [
        PIPER_EXE_ENV,
        BASE_DIR / "Piper" / "piper.exe",
        BASE_DIR / ".venv" / "Scripts" / "piper.exe",
        shutil.which("piper"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    raise FileNotFoundError(
        "Piper executable not found. Set PIPER_EXE or place Piper\\piper.exe next to this script."
    )

def _resolve_voice_model() -> Path:
    if PIPER_VOICE_ENV and Path(PIPER_VOICE_ENV).exists():
        return Path(PIPER_VOICE_ENV)
    for folder in [BASE_DIR / "Piper" / "voices", BASE_DIR / "voices"]:
        if folder.exists():
            onnx = next(folder.glob("*.onnx"), None)
            if onnx:
                return onnx
    raise FileNotFoundError("No Piper voice (.onnx) found. Put one in Piper\\voices or voices\\, or set PIPER_VOICE.")

def _check_voice_files(model_path: Path) -> None:
    if not model_path.exists():
        raise FileNotFoundError(f"Voice model not found: {model_path}")
    partner = Path(str(model_path) + ".json")
    if not partner.exists():
        raise FileNotFoundError(f"Missing voice JSON: {partner}. Download the matching .onnx.json to the same folder.")

def speak(text: str, async_play: bool = True, out_dir: Path | None = None) -> Path:
    if not text:
        return Path()
    exe = _resolve_piper_exe()
    model = _resolve_voice_model()
    _check_voice_files(model)

    out_dir = out_dir or TTS_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_wav = out_dir / f"tts_{int(time.time()*1000)}.wav"

    cmd = [exe, "-m", str(model), "-f", str(out_wav)]
    proc = subprocess.run(
        cmd,
        input=text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False
    )
    if proc.returncode != 0 or not out_wav.exists():
        raise RuntimeError(f"Piper failed ({proc.returncode}). {proc.stderr.decode('utf-8','ignore')}")

    if _HAS_WINSOUND:
        flags = winsound.SND_FILENAME | (winsound.SND_ASYNC if async_play else 0)
        winsound.PlaySound(str(out_wav), flags)
    else:
        if async_play:
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command",
                 f"(New-Object Media.SoundPlayer '{str(out_wav)}').Play()"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        else:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(New-Object Media.SoundPlayer '{str(out_wav)}').PlaySync()"],
                check=False
            )
    return out_wav

def stop_speaking():
    if _HAS_WINSOUND:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            pass

def _clean_for_tts(s: str) -> str:
    if not s: return s
    # remove LaTeX/math blocks
    s = re.sub(r'\$\$.*?\$\$|\$.*?\$|\\\[.*?\\\]|\\\(.*?\\\)', ' ', s, flags=re.S)
    # strip code/markup noise
    s = re.sub(r'[`*_^~\\]|[#>{}\[\]|]', ' ', s)
    # collapse duplicate punctuation and whitespace
    s = re.sub(r'\s{2,}', ' ', s).strip()
    return s

def say(listener, text: str, hud_text: str | None = None):
    if not text: return
    text = _clean_for_tts(text)
    # What to display on the HUD for this utterance
    display = (hud_text if hud_text is not None else (text or ""))

    # Trim extremely long output for the HUD only
    if len(display) > 500:
        display = display[:500] + " …"

    # NEW: show what Jarvis is about to say (distinct assistant line)
    try:
        hud_emit("assistant", text=display)   # <-- requires jarvis_hud.py to handle "assistant"
    except Exception:
        pass

    # Existing speaking indicator
    hud_emit("speaking", value=True)

    try:
        if listener is not None and hasattr(listener, "pause"):
            listener.pause()
            if hasattr(listener, "drain"):
                listener.drain()
    except Exception:
        pass

    try:
        # Block so we know exactly when speech ends
        speak(text, async_play=False)
    finally:
        try:
            if listener is not None and hasattr(listener, "drain"):
                listener.drain()
            if listener is not None and hasattr(listener, "resume"):
                listener.resume()
            if listener is not None and hasattr(listener, "guard_for"):
                listener.guard_for(0.4)  # small safety window to avoid self-hear
        except Exception:
            pass
        hud_emit("speaking", value=False)


# ===================== Small helpers =====================

typed_input_q = queue.Queue()

def _run_typed_input_server():
    app = Flask("jarvis_input")

    @app.post("/input")
    def _input():
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        if text:
            typed_input_q.put(text)
            return jsonify(ok=True)
        return jsonify(ok=False), 400

    # Port 8766 by default; match HUD env var JARVIS_INPUT_URL
    app.run("127.0.0.1", 8766, debug=False, use_reloader=False)

def open_chrome(url="https://www.google.com"):
    for p in CHROME_PATHS:
        p = os.path.expandvars(p)
        if os.path.exists(p):
            try:
                subprocess.Popen([p, url]); return True
            except Exception:
                pass
    try:
        subprocess.Popen(["cmd","/c","start","chrome", url]); return True
    except Exception:
        return False

def google_search_page(q: str):
    return open_chrome("https://www.google.com/search?q=" + quote_plus(q))

def contains_name(text: str) -> bool:
    low = (text or "").lower()
    if any(alias in low for alias in NAME_ALIASES):
        return True
    words = re.findall(r"[a-z']+", low)
    near = difflib.get_close_matches("jarvis", words, n=1, cutoff=0.78)
    return bool(near)

def strip_name(text: str) -> str:
    low = (text or "").lower()
    for a in NAME_ALIASES:
        # remove the name and any immediate trailing punctuation
        low = re.sub(rf"\b{re.escape(a)}\b[,\s:;\.-]*", " ", low)
    # collapse extra punctuation/spaces at edges
    low = re.sub(r"^[\s,;:\.-]+|[\s,;:\.-]+$", "", low)
    return " ".join(low.split())

def clean_transcript(t: str) -> str:
    t = " ".join((t or "").strip().lower().split())
    if not t:
        return t
    toks = t.split()
    if toks and toks[0] in EDGE_FILLERS:
        keep = (toks[0] == "the" and len(toks) > 1 and toks[1] in {"time", "weather", "news"})
        if not keep:
            toks = toks[1:]
    if toks and toks[-1] in EDGE_FILLERS:
        toks = toks[:-1]
    return " ".join(toks)


def _should_search(text: str) -> bool:
    low = (text or "").lower()
    triggers = [
        "latest", "today", "news", "who won", "score", "last year",
        "price", "stock", "weather", "update", "find for me", "search for",
        "release", "when", "where", "schedule", "this year", "recent"
    ]
    return any(t in low for t in triggers)


# ===================== LLM (Ollama) =====================
class LlamaBrain:
    def __init__(self, url, model, system_prompt, memory_turns=6,
                 memory=None, mem_user="roman", mem_namespace="personal"):
        self.url = url
        self.model = model
        self.system = system_prompt
        self.mem = collections.deque(maxlen=memory_turns * 2)
        self.use_chat_api = True
        self.memory = memory
        self.mem_user = mem_user
        self.mem_namespace = mem_namespace
        self.test_api()

    def test_api(self):
        try:
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": "test"}],
                "stream": False
            }
            r = requests.post(self.url, json=payload, timeout=15)
            if r.status_code != 200:
                self.url = self.url.replace("/api/chat", "/api/generate")
                self.use_chat_api = False
        except Exception:
            self.url = self.url.replace("/api/chat", "/api/generate")
            self.use_chat_api = False

    def _is_keepalive(self, t: str) -> bool:
        s = (t or "").strip().lower()
        if not s:
            return False
        if len(s) <= 6 and s in {"ok", "okay", "yes", "yo", "ya", "hi", "hey", "sup", "yo?", "yes?"}:
            return True
        return bool(re.search(r"^(are you there|you there|hello|hi|hey)$", s, re.I))

    def _should_consult_memory(self, t: str) -> bool:
        s = (t or "").lower()
        if len(s) < 10:
            return False
        if self._is_keepalive(s):
            return False
        triggers = (
            "remember", "from now on", "call me", "my ", "i am", "i'm",
            "favorite", "prefer", "birthday", "address", "email", "phone",
            "roommate", "girlfriend", "class", "course", "schedule", "project",
            "name is", "i like"
        )
        return any(tk in s for tk in triggers)

    def ask(self, user_text, web_context=None):
        dprint("ASK begin:", user_text[:120])
        hud_emit("thinking", value=True)

        # ---- Fast path: keep-alive / ping ----
        if self._is_keepalive(user_text):
            answer = "At your service, sir."
            self.mem.append(f"Sir: {user_text}")
            self.mem.append(f"Jarvis: {answer}")
            hud_emit("thinking", value=False)
            return answer

        # ================= CHAT PATH (tools) =================
        if self.use_chat_api:
            MAX_TOOL_ROUNDS = 2
            rounds = 0

            # Memory prefix (only if useful)
            mem_prefix = ""
            use_memory = self.memory is not None and self._should_consult_memory(user_text)
            if use_memory:
                try:
                    hits = self.memory.search(
                        user_text, user_id=self.mem_user,
                        namespace=self.mem_namespace, k=5
                    )
                    hits = [h for h in hits if h.get("score", 0) >= 0.20]
                    if hits:
                        mem_prefix = build_memory_system_prefix(hits)
                    dprint(f"memory hits: {len(hits)}")
                except Exception as e:
                    dprint("memory.search failed:", e)
                    mem_prefix = ""

            # Build message history
            sys_content = self.system + (("\n" + mem_prefix) if mem_prefix else "")
            messages = [{"role": "system", "content": sys_content}]
            if web_context:
                messages.append({"role": "system", "content": f"Web context:\n{web_context}"})
            if self.mem:
                for turn in self.mem:
                    role = "user" if turn.startswith("Sir:") else "assistant"
                    content = turn.replace("Sir: ", "").replace("Jarvis: ", "")
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": user_text})

            while True:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "tools": tools,
                    "stream": False,
                    "options": {"temperature": 0.4, "num_ctx": 8192, "top_p": 0.9}
                }

                try:
                    dprint("POST chat:", self.url)
                    r = requests.post(self.url, json=payload, timeout=30)
                    r.raise_for_status()
                    data = r.json()
                except requests.Timeout:
                    dprint("LLM timeout (chat); falling back to generate.")
                    break  # -> fallback below
                except Exception as e:
                    dprint("LLM error (chat):", e)
                    break  # -> fallback below

                msg = data.get("message", {}) or {}
                tool_calls = msg.get("tool_calls") or []

                # Tool loop
                if tool_calls and rounds < MAX_TOOL_ROUNDS:
                    rounds += 1
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": tool_calls
                    })

                    for call in tool_calls:
                        fn = (call.get("function") or {})
                        name = fn.get("name")
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}
                        if not isinstance(args, dict):
                            args = {}

                        if name == "web_search":
                            query = args.get("query") or user_text
                            try:
                                result = web_search(query)
                            except Exception as e:
                                result = f"Error during web search: {e}"

                            tool_msg = {"role": "tool", "name": "web_search", "content": result}
                            if "id" in call:
                                tool_msg["tool_call_id"] = call["id"]
                            messages.append(tool_msg)

                    messages.append({
                        "role": "system",
                        "content": "Use the tool results above and answer concisely now. "
                                   "Do not reply with 'searching' or 'query pending'."
                    })
                    continue  # ask again with tool outputs

                # Finalize answer
                answer = (msg.get("content") or "").strip()

                if not answer and tool_calls:
                    messages.append({"role": "system", "content": "Answer directly in 1–2 sentences."})
                    payload["tools"] = []
                    try:
                        r2 = requests.post(self.url, json=payload, timeout=20)
                        answer = (r2.json().get("message", {}).get("content") or "").strip()
                    except Exception:
                        answer = ""

                if not answer:
                    answer = "Understood, sir."

                # Persist and return
                self.mem.append(f"Sir: {user_text}")
                self.mem.append(f"Jarvis: {answer}")
                if self.memory is not None:
                    try:
                        inserted = self.memory.add(
                            [{"role": "user", "content": user_text},
                             {"role": "assistant", "content": answer}],
                            user_id=self.mem_user,
                            agent_id="jarvis-desktop",
                            namespace=self.mem_namespace,
                            source="jarvis",
                            importance=0.6
                        )
                        dprint(f"memory.add inserted={inserted}")
                    except Exception as e:
                        dprint("memory.add failed:", e)

                hud_emit("thinking", value=False)
                dprint("ASK done (chat).")
                return answer

        # =============== GENERATE FALLBACK (no tools) ===============
        # Rebuild memory prefix just for the fallback (rare path)
        mem_prefix = ""
        use_memory = self.memory is not None and self._should_consult_memory(user_text)
        if use_memory:
            try:
                hits = self.memory.search(
                    user_text, user_id=self.mem_user,
                    namespace=self.mem_namespace, k=5
                )
                hits = [h for h in hits if h.get("score", 0) >= 0.20]
                if hits:
                    mem_prefix = build_memory_system_prefix(hits)
                dprint(f"memory hits (gen): {len(hits)}")
            except Exception as e:
                dprint("memory.search (gen) failed:", e)

        blocks = []
        if mem_prefix:
            blocks.append(f"[MEMORY]\n{mem_prefix}\n[/MEMORY]")
        if web_context:
            blocks.append(f"[WEB RESULTS]\n{web_context}\n[/WEB RESULTS]")
        if self.mem:
            blocks.append("[RECENT]\n" + "\n".join(map(str, self.mem)) + "\n[/RECENT]")
        joined = ("\n\n".join(blocks) + "\n\n") if blocks else ""

        prompt = (
            f"{self.system}\n"
            f"{joined}"
            "Answer briefly and directly.\n"
            f"User: {user_text}\n"
        )

        gen_url = self.url if self.url.endswith("/api/generate") else self.url.replace("/api/chat", "/api/generate")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.7, "num_ctx": 4096, "top_p": 0.95}
        }

        try:
            dprint("POST generate:", gen_url)
            r = requests.post(gen_url, json=payload, timeout=30)
            r.raise_for_status()
            answer = (r.json().get("response") or "").strip() or "Understood, sir."
        except requests.Timeout:
            dprint("LLM timeout (generate).")
            answer = "Apologies, sir. My model timed out."
        except Exception as e:
            dprint("LLM error (generate):", e)
            answer = "Apologies, sir. My language model is unavailable."
        finally:
            hud_emit("thinking", value=False)

        # Persist and return (fallback)
        self.mem.append(f"Sir: {user_text}")
        self.mem.append(f"Jarvis: {answer}")
        if self.memory is not None:
            try:
                inserted = self.memory.add(
                    [{"role": "user", "content": user_text},
                     {"role": "assistant", "content": answer}],
                    user_id=self.mem_user,
                    agent_id="jarvis-desktop",
                    namespace=self.mem_namespace,
                    source="jarvis",
                    importance=0.6
                )
                dprint(f"memory.add (gen) inserted={inserted}")
            except Exception as e:
                dprint("memory.add (gen) failed:", e)

        dprint("ASK done (gen).")
        return answer



# ===================== Main (Streaming ASR) =====================
# Start typed-input HTTP inlet
threading.Thread(target=_run_typed_input_server, daemon=True).start()

def _launch_hud(screen_index: int | None = 1):
    """
    Start the HUD on a specific screen, fullscreen, no console.
    Wait for health. If not healthy, fall back to console launch + log.
    """
    import sys
    from pathlib import Path

    hud_path = os.path.join(ROOT, "JarvisHUD", "jarvis_hud.py")
    if not os.path.exists(hud_path):
        print("[Error] HUD script not found:", hud_path)
        return

    # Use SAME venv interpreter
    py = Path(sys.executable)
    pythonw = py.with_name("pythonw.exe")
    exe = str(pythonw if pythonw.exists() else py)

    # Environment (typed input URL for the HUD)
    env = os.environ.copy()
    env["JARVIS_INPUT_URL"] = "http://127.0.0.1:8766/input"

    args = [exe, hud_path]
    if screen_index is not None:
        args += ["--screen", str(screen_index)]

    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    # 1) Try hidden (pythonw) first
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
            env=env,
            shell=False
        )
    except Exception as e:
        print(f"[Error] Failed to spawn HUD hidden: {e}")

    # 2) Wait for health (ping /event)
    def _hud_up(timeout=0.4):
        try:
            import requests
            requests.post("http://127.0.0.1:8765/event",
                          json={"type":"system","text":"ping"},
                          timeout=timeout)
            return True
        except Exception:
            return False

    ok = False
    for _ in range(15):  # ~4.5s
        if _hud_up():
            ok = True
            break
        time.sleep(0.3)

    if ok:
        return

    # 3) Fallback: console launch + log to file so you can see the exception
    try:
        log_dir = os.path.join(ROOT, "logs"); os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"hud_{int(time.time())}.log")
        with open(log_file, "w", encoding="utf-8", newline="") as lf:
            # Use python.exe (console) so errors are visible + captured
            exe2 = str(py)  # not pythonw
            proc = subprocess.Popen(
                [exe2, hud_path, "--screen", str(screen_index or 1)],
                stdout=lf, stderr=lf, env=env, shell=False
            )
        print(f"[Error] HUD didn’t come up; started console HUD. See log: {log_file}")
    except Exception as e:
        print(f"[Error] Fallback HUD launch failed: {e}")


def main():
    # Launch HUD on your second monitor (index 1). Change to 0/2 as needed.
    _launch_hud(screen_index=1)

    say(None, "Boot protocol active.", hud_text="Boot protocol active.")

    start_asr_governor()

    WS = WhisperStreamer(
        partial_window_sec=4.0,
        partial_hop_sec=0.6,
        final_silence_sec=0.5,
        vad_aggr=3,
        enable_vad=True,
        partial_beam_size=2,
        final_beam_size=5,
        language="en"
    )

    say(WS, "Systems online. Awaiting your command, sir.", hud_text="Systems online. Awaiting your command, sir.")
    hud_emit("final", text="Systems online. Awaiting your command, sir.")
    print("[Jarvis] Streaming mode active. Say: 'Jarvis ...'")

    brain = None
    if not FORCE_NO_LLM:
        brain = LlamaBrain(
            OLLAMA_URL, OLLAMA_MODEL, JARVIS_SYSTEM, MEMORY_TURNS,
            memory=MEM, mem_user=MEM_USER, mem_namespace=MEM_NAMESPACE
        )
        dprint("MEM enabled for brain:", bool(MEM))
        try:
            dprint("MEM db path:", getattr(MEM, "config").db_path)
        except Exception:
            pass
    else:
        print("[Jarvis] LLM offline: will only handle built-in intents.")

    ctx = {
        "say": lambda text: say(WS, text),
        "open_chrome": open_chrome,
        "google_search_page": google_search_page,
        "now": lambda: datetime.now(),
        "base_dir": ROOT,
    }

    def on_partial(text: str):
        hud_emit("partial", text=text)
        if REQUIRE_NAME:
            if not contains_name(clean_transcript(text)):
                return
            cmd = strip_name(clean_transcript(text))
        else:
            cmd = clean_transcript(text)

    def on_final(text: str):
        cleaned = clean_transcript(text)
        if not cleaned:
            return
        print(f"[Heard] {cleaned}")
        hud_emit("final", text=cleaned)

        if REQUIRE_NAME and not contains_name(cleaned):
            return

        cmd = strip_name(cleaned) if REQUIRE_NAME else cleaned
        if not cmd:
            cmd = "yes?"

        low = cmd.lower().replace("’", "'")  # normalize curly quotes

        # --- SCREEN / VISION INTENTS ---
        see_summary_re = re.compile(
            r"(what\s+am\s+i\s+looking\s+at|what'?s\s+on\s+my\s+screen|what'?s\s+this\s+window|what\s+is\s+on\s+my\s+screen)",
            re.I
        )
        see_verbose_re = re.compile(
            r"(give\s+me\s+details\s+on\s+this\s+screen|read\s+everything\s+on\s+my\s+screen)",
            re.I
        )
        under_mouse_re = re.compile(
            r"(what'?s\s+under\s+my\s+mouse|what\s+am\s+i\s+hovering|what\s+does\s+this\s+button\s+do)",
            re.I
        )

        if see_summary_re.search(low):
            say(WS, cmd_what_am_i_looking_at_summary(brain)); return
        if see_verbose_re.search(low):
            say(WS, cmd_what_am_i_looking_at()); return
        if under_mouse_re.search(low):
            say(WS, cmd_whats_under_mouse()); return

        handled = False
        try:
            handled = dispatch(cmd, ctx)
        except Exception as e:
            print(f"[warn] dispatch error: {e}")
            handled = False

        if handled:
            try:
                from jarvis_listen_whisper_stream import ASR
                ASR.nudge()
            except Exception:
                pass
            return

        web_ctx = None
        if any(k in low for k in ("latest", "today", "news", "stock", "price", "weather", "score", "who won", "update")):
            try:
                web_ctx = web_search(cmd)
            except Exception:
                web_ctx = None

        if brain is not None:
            answer = brain.ask(cmd, web_context=web_ctx)
            say(WS, answer)
        else:
            say(WS, "Apologies, sir. My language model is offline.")

    WS.start(on_partial=on_partial, on_final=on_final)

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\nExiting…")
    finally:
        try: WS.stop()
        except Exception: pass

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting…")
    except Exception as e:
        print(f"[FATAL] Unhandled error: {e}")