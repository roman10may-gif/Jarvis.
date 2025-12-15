# commands.py
import os, re, random, subprocess, threading, time, webbrowser, ctypes, traceback, sys
from datetime import datetime

# Optional: structured web search helper (if you have utils.web_search)
try:
    from utils import web_search  # must return a short string summary
except Exception:
    web_search = None

DEBUG_COMMANDS = True  # set True to see which handler matched
# --- SAFETY GATE (drop in commands.py) ---

DIRECT_KEYWORD_TRIGGERS = False  # set True only if you want legacy behavior
CONFIRM_TIMEOUT_SEC = 20

_NEGATIONS = r"(don't|do not|shouldn't|not now|if i say|when i say|talking about|the word)"
_IMPERATIVE_VERBS = r"(open|close|quit|shutdown|restart|reboot|mute|unmute|pause|play|stop|kill|end|delete|empty|format|run|launch|start|enable|disable|search|navigate|directions|route|lock|sleep|exit)"



MORNING_SCRIPT = r"E:\Jarvis\morning_routine.py"   # path to your canvas script

def _set_alarm_via_script(phrase: str) -> bool:
    cmd = [sys.executable, MORNING_SCRIPT, "--set", phrase]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        print("[Jarvis] Alarm set:", r.stdout or r.stderr)
        return True
    print("[Jarvis] Alarm failed:", r.stdout or r.stderr)
    return False

# Example matcher in your dispatcher:
def maybe_handle_alarm_command(text: str, ctx) -> bool:
    m = re.search(r"(?i)\\bset (an )?alarm (for|at) (.+)$", text.strip())
    if not m:
        return False
    phrase = m.group(3).strip()
    ok = _set_alarm_via_script(phrase)
    say = ctx.get("say") or (lambda s: None)
    if ok:
        say(f"Alarm set for {phrase}, sir.")
    else:
        say("I couldn't set that alarm, sir.")
    return True

_last_wake_ts = 0
def note_wake_word_heard():
    global _last_wake_ts
    _last_wake_ts = time.time()

def _addressed_recently():
    return (time.time() - _last_wake_ts) < 25  # tweak as you like

def _addressed_in_text(text: str):
    return re.search(r"\b(jarvis|hey jarvis)\b", text, re.I) is not None

def _imperative_form(text: str):
    # "shutdown the pc", "please restart", "could you open discord", etc.
    return re.search(rf"\b({_IMPERATIVE_VERBS})\b", text, re.I) is not None

def _negated(text: str):
    return re.search(rf"\b{_NEGATIONS}\b", text, re.I) is not None

def should_fire_command(text: str, *, dangerous: bool) -> bool:
    t = text.strip().lower()
    if DIRECT_KEYWORD_TRIGGERS:
        return not _negated(t)

    addressed = _addressed_in_text(t) or _addressed_recently()
    if not addressed: 
        return False
    if _negated(t): 
        return False
    if not _imperative_form(t):
        return False
    # require explicit confirmation for dangerous things
    if dangerous:
        return False  # force confirmation path below
    return True

# simple confirmation flow you can call before executing dangerous actions
_pending_confirm = None
def ask_confirmation(prompt: str, on_yes_callable):
    global _pending_confirm
    _pending_confirm = {"deadline": time.time()+CONFIRM_TIMEOUT_SEC, "do": on_yes_callable}
    # say(prompt) using your TTS:
    return f"CONFIRM: {prompt} (say 'yes' or 'no' within {CONFIRM_TIMEOUT_SEC} seconds)"

def handle_yes_no(text: str):
    global _pending_confirm
    if not _pending_confirm: 
        return None
    if time.time() > _pending_confirm["deadline"]:
        _pending_confirm = None
        return "Confirmation timed out."
    low = text.strip().lower()
    if low in ("yes","yeah","yep","confirm","do it","go ahead","proceed"):
        fn = _pending_confirm["do"]; _pending_confirm = None
        return fn()
    if low in ("no","nope","cancel","stop","abort"):
        _pending_confirm = None
        return "Canceled."
    return None

# ===== helpers =====
def _open_url(url, ctx):
    try:
        if callable(ctx.get("open_chrome")):
            ctx["open_chrome"](url); return
    except Exception:
        pass
    webbrowser.open(url)

def _say(ctx, text):
    f = ctx.get("say")
    if callable(f):
        f(text)

def _try_close_hud(ctx) -> bool:
    """
    Try multiple strategies to close the Jarvis HUD gracefully.
    Works with any of these ctx keys if present:
      - hud_close: callable to directly close HUD
      - hud_emit: callable event bus (e.g. hud_emit("quit"))
      - hud_proc: subprocess.Popen instance (terminate/kill)
      - hud_pid: integer PID to kill
      - hud_url: HTTP endpoint for HUD events (POST JSON)
    Also respects env JARVIS_HUD_URL, default http://127.0.0.1:8765/event
    """
    closed = False

    # 1) Direct close hook
    try:
        f = ctx.get("hud_close")
        if callable(f):
            f()
            closed = True
    except Exception:
        traceback.print_exc()

    # 2) Event bus (emit a few likely verbs)
    if not closed:
        try:
            emit = ctx.get("hud_emit")
            if callable(emit):
                for ev in ("shutdown", "quit", "exit", "close", "kill", "turn off"):
                    try:
                        emit(ev)
                        closed = True
                        break
                    except Exception:
                        continue
        except Exception:
            traceback.print_exc()

    # 3) Stored subprocess handle
    if not closed:
        try:
            p = ctx.get("hud_proc")
            if p is not None:
                p.terminate()
                closed = True
        except Exception:
            traceback.print_exc()

    # 4) Stored PID
    if not closed:
        try:
            pid = ctx.get("hud_pid")
            if pid:
                # /T kills child processes; /F forces
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               check=False, capture_output=True)
                closed = True
        except Exception:
            traceback.print_exc()

    # 5) HTTP endpoint fallback
    if not closed:
        try:
            url = (ctx.get("hud_url")
                   or os.environ.get("JARVIS_HUD_URL")
                   or "http://127.0.0.1:8765/event")
            try:
                import requests  # lazy import
                for payload in ({"type": "quit"},
                                {"type": "exit"},
                                {"type": "shutdown"},
                                {"type": "close"}):
                    try:
                        requests.post(url, json=payload, timeout=0.3)
                        closed = True
                        break
                    except Exception:
                        continue
            except Exception:
                # requests not installed or HTTP failed; swallow
                pass
        except Exception:
            traceback.print_exc()

    return closed

# ===== media/system keys (Windows) =====
try:
    user32 = ctypes.windll.user32
except Exception:
    user32 = None

KEYEVENTF_KEYUP = 0x02
def _press_vk(vk):
    if not user32: return
    user32.keybd_event(vk, 0, 0, 0); time.sleep(0.02)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

VK = {
    "VOL_UP":   0xAF, "VOL_DOWN": 0xAE, "MUTE": 0xAD,
    "PLAYPAUSE":0xB3, "NEXT": 0xB0, "PREV": 0xB1, "STOP": 0xB2,
    "ALT": 0x12, "F4": 0x73,
}

# ===== app paths =====
_APP_MAP = {
    "notepad":  "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "cmd": "cmd.exe", "powershell":"powershell.exe",
    "vscode": r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
    "visual studio code": r"%LocalAppData%\Programs\Microsoft VS Code\Code.exe",
    "spotify": r"%APPDATA%\Spotify\Spotify.exe",
    "discord": r"%LocalAppData%\Discord\Update.exe --processStart Discord.exe",
    "chrome": r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    "edge": r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
}

def _expand_env(s): return os.path.expandvars(s)

def _open_app_by_name(name):
    exe = _APP_MAP.get(name.lower())
    if exe:
        exe = _expand_env(exe)
        try:
            parts = exe.split()
            subprocess.Popen(parts if len(parts) > 1 else [exe]); return True
        except Exception:
            pass
    try:
        subprocess.Popen(["cmd","/c","start","", name]); return True
    except Exception:
        return False

# ===== notes & timers =====
def _notes_path(ctx):
    base = ctx.get("base_dir", os.getcwd())
    return os.path.join(base, "notes.txt")

def _append_note(ctx, text):
    p = _notes_path(ctx)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")

# ===== handlers =====
def h_coin(text, ctx):
    _say(ctx, random.choice(["Heads, sir.", "Tails, sir."])); return True

def h_die(text, ctx):
    m = re.search(r"\bd(\d+)\b", text, re.I)
    sides = int(m.group(1)) if m else 6
    sides = 6 if sides < 2 or sides > 1000 else sides
    _say(ctx, f"{random.randint(1, sides)}, on a d{sides}, sir."); return True

def h_choose(text, ctx):
    m = re.search(r"(?:between|from)\s+(.+)", text, re.I)
    if not m: return False
    options = re.split(r"\s*(?:,| or | / )\s*", m.group(1).strip())
    options = [o for o in options if o]
    if len(options) < 2: return False
    _say(ctx, f"{random.choice(options)}, sir."); return True

def h_time(text, ctx):
    now = datetime.now().strftime('%I:%M %p').lstrip('0')
    _say(ctx, f"It is {now}, sir."); return True

def h_date(text, ctx):
    _say(ctx, datetime.now().strftime("Today is %A, %B %d, %Y, sir.")); return True

def h_open_site(text, ctx):
    if re.search(r"\bgmail\b", text, re.I): _open_url("https://mail.google.com", ctx); _say(ctx, "Gmail, sir."); return True
    if re.search(r"\byoutube\b", text, re.I): _open_url("https://youtube.com", ctx); _say(ctx, "YouTube, sir."); return True
    if re.search(r"\breddit\b", text, re.I): _open_url("https://reddit.com", ctx); _say(ctx, "Reddit, sir."); return True
    if re.search(r"\bdrive\b", text, re.I):  _open_url("https://drive.google.com", ctx); _say(ctx, "Drive, sir."); return True
    if m := re.search(r"(?:open|go to)\s+(https?://\S+)", text, re.I):
        _open_url(m.group(1), ctx); _say(ctx, "At once, sir."); return True
    return False

def h_search(text, ctx):
    m = re.search(r"\b(?:search the web for|search|google|look up)\s+(?:for\s+)?(.+)", text, re.I)
    if not m: return False
    q = m.group(1).strip()
    url = "https://www.google.com/search?q=" + q.replace(" ", "+")
    _open_url(url, ctx)
    _say(ctx, f"Searching for {q}, sir."); return True

def h_youtube_search(text, ctx):
    m = re.search(r"\b(?:youtube|yt)\s+(?:for\s+)?(.+)", text, re.I)
    if not m: return False
    q = m.group(1).strip()
    url = "https://www.youtube.com/results?search_query=" + q.replace(" ", "+")
    _open_url(url, ctx)
    _say(ctx, f"YouTube search for {q}, sir."); return True

def h_open_special_folder(text, ctx):
    paths = {
        r"(downloads?)": r"%USERPROFILE%\Downloads",
        r"(documents?)": r"%USERPROFILE%\Documents",
        r"(pictures?|photos?)":  r"%USERPROFILE%\Pictures",
        r"(music)":     r"%USERPROFILE%\Music",
        r"(videos?)":    r"%USERPROFILE%\Videos",
        r"(desktop)":   r"%USERPROFILE%\Desktop",
        r"(jarvis)":    ctx.get("base_dir", os.getcwd()),
    }
    for rx, p in paths.items():
        if re.search(rf"\bopen (?:the )?{rx}(?: folder)?\b", text, re.I):
            p = os.path.expandvars(p)
            try: subprocess.Popen(["explorer", p]); _say(ctx, "Opened, sir."); return True
            except Exception: pass
    return False

def h_open_app(text, ctx):
    m = re.search(r"\b(open|launch|start)\b\s+([a-z0-9 ._-]+)", text, re.I)
    if not (m and m.group(2)): return False
    seg = m.group(2).strip().lower()
    for name in sorted(_APP_MAP.keys(), key=len, reverse=True):
        if name in seg:
            ok = _open_app_by_name(name)
            _say(ctx, "Complete, sir." if ok else f"Apologies, sir. I couldn't open {name}."); return True
    last = seg.split()[-1]
    ok = _open_app_by_name(last)
    _say(ctx, "Complete, sir." if ok else f"Apologies, sir. I couldn't open {last}."); return True

def h_close_window(text, ctx):
    if re.search(r"\b(close|exit|quit)\b.*\b(window|app|application)\b", text, re.I) or re.search(r"\bclose window\b", text, re.I):
        if user32:
            user32.keybd_event(VK["ALT"],0,0,0); _press_vk(VK["F4"]); user32.keybd_event(VK["ALT"],0,KEYEVENTF_KEYUP,0)
        _say(ctx, "Closed, sir."); return True
    return False

def h_media(text, ctx):
    if re.search(r"\b(play|pause|play pause|resume)\b", text, re.I):
        _press_vk(VK["PLAYPAUSE"]); _say(ctx, "Very well, sir."); return True
    if re.search(r"\b(next|skip)\b", text, re.I):
        _press_vk(VK["NEXT"]); _say(ctx, "Next, sir."); return True
    if re.search(r"\b(previous|prev|back)\b", text, re.I):
        _press_vk(VK["PREV"]); _say(ctx, "Previous, sir."); return True
    if re.search(r"\bstop\b", text, re.I):
        _press_vk(VK["STOP"]); _say(ctx, "Stopped, sir."); return True
    return False

def h_volume(text, ctx):
    if re.search(r"\b(mute|silence)\b", text, re.I):
        _press_vk(VK["MUTE"]); _say(ctx, "Muted, sir."); return True
    if re.search(r"\b(unmute)\b", text, re.I):
        _press_vk(VK["MUTE"]); _say(ctx, "Unmuted, sir."); return True
    if re.search(r"\b(volume|turn (it|the )?volume|sound).*(up|increase|higher|louder)\b", text, re.I) or re.search(r"\bvolume up\b", text, re.I):
        for _ in range(5): _press_vk(VK["VOL_UP"])
        _say(ctx, "As you wish, sir."); return True
    if re.search(r"\b(volume|turn (it|the )?volume|sound).*(down|decrease|lower|quieter)\b", text, re.I) or re.search(r"\bvolume down\b", text, re.I):
        for _ in range(5): _press_vk(VK["VOL_DOWN"])
        _say(ctx, "Very good, sir."); return True
    return False

def h_lock(text, ctx):
    if re.search(r"\b(lock (the )?(pc|computer|screen|workstation))\b", text, re.I):
        _say(ctx, "Locking, sir.")
        subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"]); return True
    return False

def h_sleep(text, ctx):
    if re.search(r"\b(sleep now|go to sleep|sleep the (pc|computer))\b", text, re.I):
        _say(ctx, "Good night, sir.")
        try: subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
        except Exception: pass
        return True
    return False

def h_shutdown_restart(text, ctx):
    # Always confirm for power actions
    if re.search(r"\b(shut\s?down|power off)\b", text, re.I):
        msg = ask_confirmation("Shut down this PC now?", lambda: (subprocess.Popen(["shutdown","/s","/t","0"]) or "Shutting down, sir."))
        _say(ctx, msg); return True
    if re.search(r"\b(restart|reboot)\b", text, re.I):
        msg = ask_confirmation("Restart this PC now?", lambda: (subprocess.Popen(["shutdown","/r","/t","0"]) or "Restarting, sir."))
        _say(ctx, msg); return True
    return False

def h_notes(text, ctx):
    # add / take a note
    if m := re.search(r"\b(?:note|take a note|add note)\b\s*(?:that\s*)?(.+)", text, re.I):
        body = m.group(1).strip()
        _append_note(ctx, body); _say(ctx, "Noted, sir."); return True

    # open notes
    if re.search(r"\b(show|open)\s+notes\b", text, re.I):
        try:
            subprocess.Popen(["notepad.exe", _notes_path(ctx)])
            _say(ctx, "Your notes, sir.")
        except Exception:
            _say(ctx, "Apologies, sir. I could not open your notes.")
        return True

    # clear notes (confirm)
    if re.search(r"\b(clear|erase|delete)\s+notes\b", text, re.I):
        def _wipe():
            try:
                open(_notes_path(ctx), "w", encoding="utf-8").close()
            except Exception:
                pass
            return "Cleared, sir."
        msg = ask_confirmation("Clear all notes?", _wipe)
        _say(ctx, msg); return True

    return False

def h_smalltalk(text, ctx):
    if re.search(r"\b(how are you|how's it going)\b", text, re.I): _say(ctx, "Operating within normal parameters, sir."); return True
    if re.search(r"\b(thank you|thanks)\b", text, re.I): _say(ctx, "Quite so, sir."); return True
    if re.search(r"\b(what is your name|who are you)\b", text, re.I): _say(ctx, "Jarvis, at your service, sir."); return True
    if re.search(r"\b(good (morning|afternoon|evening))\b", text, re.I):
        part = re.search(r"(morning|afternoon|evening)", text, re.I).group(1)
        _say(ctx, f"Good {part}, sir."); return True
    return False

def h_maps(text, ctx):
    m = re.search(r"\b(?:navigate|directions|maps|route)\s+(?:to\s+)?(.+)", text, re.I)
    if not m: return False
    dest = m.group(1).strip()
    url = "https://www.google.com/maps/dir/?api=1&destination=" + dest.replace(" ", "+")
    _open_url(url, ctx); _say(ctx, f"Directions to {dest}, sir."); return True

def _perform_exit(ctx):
    try:
        _try_close_hud(ctx)
    except Exception:
        traceback.print_exc()
    # Give TTS a beat before exit
    try: time.sleep(0.25)
    except Exception: pass
    os._exit(0)
    return "Standing down, sir."

def h_exit(text, ctx):
    if re.search(r"\b(exit|quit|close jarvis|shut down jarvis)\b", text, re.I):
        msg = ask_confirmation("Exit Jarvis now?", lambda: _perform_exit(ctx))
        _say(ctx, msg); return True
    return False
# ===== trigger table =====
TRIGGERS = [
    # random/choice
    (re.compile(r"\b(flip (a )?coin|coin toss|heads or tails)\b", re.I), h_coin),
    (re.compile(r"\b(roll (a )?d\d+|roll (a )?die|roll (a )?dice)\b", re.I), h_die),
    (re.compile(r"\b(pick|choose)\s+(between|from)\s+.+", re.I), h_choose),

    # time / date
    (re.compile(r"\b(what'?s the time|tell me the time|the time|time)\b", re.I), h_time),
    (re.compile(r"\b(what'?s the date|what day is it|date)\b", re.I), h_date),

    # web
    (re.compile(r"\b(open|go to)\b.*\b(gmail|youtube|reddit|drive)\b|\bopen https?://", re.I), h_open_site),
    (re.compile(r"\b(search the web for|search|google|look up)\b", re.I), h_search),
    (re.compile(r"\b(youtube|yt)\b", re.I), h_youtube_search),

    # folders / apps
    (re.compile(r"\bopen (?:the )?(downloads?|documents?|pictures?|photos?|music|videos?|desktop|jarvis)(?: folder)?\b", re.I), h_open_special_folder),
    (re.compile(r"\b(open|launch|start)\b", re.I), h_open_app),

    # media / volume / window
    (re.compile(r"\b(play|pause|resume|next|skip|previous|prev|back|stop)\b", re.I), h_media),
    (re.compile(r"\b(volume|mute|unmute|turn (it|the )?volume|louder|quieter)\b", re.I), h_volume),
    (re.compile(r"\b(close|exit|quit).*(window|app|application)|\bclose window\b", re.I), h_close_window),

    # power
    (re.compile(r"\block (the )?(pc|computer|screen|workstation)\b", re.I), h_lock),
    (re.compile(r"\b(sleep now|go to sleep|sleep the (pc|computer))\b", re.I), h_sleep),
    (re.compile(r"\b(shut ?down|power off|restart|reboot)\b", re.I), h_shutdown_restart),

    # notes / timer
    (re.compile(r"\b(note|remember|take a note|add note|open notes|clear notes)\b", re.I), h_notes),

    # smalltalk / maps / exit
    (re.compile(r"\b(how are you|thank you|thanks|what is your name|who are you|good (morning|afternoon|evening))\b", re.I), h_smalltalk),
    (re.compile(r"\b(navigate|directions|maps|route)\b", re.I), h_maps),
    (re.compile(r"\b(exit|quit|close jarvis|shut down jarvis)\b", re.I), h_exit),
]

# ===== gating classes =====
ACTION_HANDLERS = {
    h_open_site, h_search, h_youtube_search, h_open_special_folder, h_open_app,
    h_close_window, h_media, h_volume, h_lock, h_sleep, h_shutdown_restart,
    h_maps, h_exit, h_notes
}
DANGEROUS_HANDLERS = {h_shutdown_restart, h_exit}  # add more if desired

# ===== dispatcher =====
def dispatch(text: str, ctx: dict) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return False

    # 0) process pending confirmations first
    if resp := handle_yes_no(low):
        _say(ctx, resp)
        return True

    # 1) optional structured “search …” helper
    if web_search is not None:
        m = re.match(r"^(?:search the web for|search|google|look up)\s+(?:for\s+)?(.+)$", low, re.I)
        if m:
            query = m.group(1).strip()
            try:
                result = web_search(query)
                if isinstance(result, str) and len(result) <= 280:
                    _say(ctx, result)
                else:
                    _say(ctx, f"Top results for {query}, sir.")
            except Exception as e:
                print("[cmd] web_search error:", e)
                traceback.print_exc()
                _say(ctx, "Apologies, sir. Web search failed.")
            return True

    addressed = _addressed_in_text(low) or _addressed_recently()
    neg = _negated(low)
    imp = _imperative_form(low)

    # 2) normal trigger matching with safety gate
    for rx, handler in TRIGGERS:
        if not rx.search(low):
            continue

        # For action-type handlers, require context; for dangerous, require stronger context.
        if handler in ACTION_HANDLERS:
            if handler in DANGEROUS_HANDLERS:
                # must be addressed, imperative, and not negated; handler will ask for confirmation
                if not (addressed and imp) or neg:
                    if DEBUG_COMMANDS:
                        print(f"[cmd] blocked dangerous {handler.__name__} (addr={addressed}, imp={imp}, neg={neg})")
                    continue
            else:
                # non-dangerous actions still require being addressed, imperative, and not negated
                if not should_fire_command(low, dangerous=False):
                    if DEBUG_COMMANDS:
                        print(f"[cmd] blocked action {handler.__name__} by gate")
                    continue

        if DEBUG_COMMANDS:
            print(f"[cmd] matched {handler.__name__} via {rx.pattern} on: {low}")
        try:
            return bool(handler(low, ctx))
        except Exception as e:
            _say(ctx, "Apologies, sir. That command failed.")
            print("[cmd] handler error:", e)
            traceback.print_exc()
            return True

    return False
