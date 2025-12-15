import argparse
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

# ========= Optional deps presence ========
_HAS_PSUTIL = False
_HAS_GPUTIL = False
_HAS_WINSDK = False

try:
    import psutil
    _HAS_PSUTIL = True
except Exception:
    pass

try:
    import GPUtil  # GPU stats
    _HAS_GPUTIL = True
except Exception:
    pass

try:
    # Windows 10+ MediaSession via WinRT (for Spotify metadata)
    import winrt.windows.media.control as wmc
    import winrt.windows.foundation as wf
    _HAS_WINSDK = True
except Exception:
    pass

# ========= Qt / Web server =========
from PySide6 import QtCore, QtGui, QtWidgets
from flask import Flask, jsonify, request
from waitress import serve

HUD_HOST = os.getenv("JARVIS_HUD_HOST", "127.0.0.1")
HUD_PORT = int(os.getenv("JARVIS_HUD_PORT", "8765"))

# Quiet Qt logging
os.environ.setdefault("QT_LOGGING_RULES", "*=false")

# ========= Event bus from HTTP -> UI thread =========
@dataclass
class HudEvent:
    kind: str
    text: Optional[str] = None
    value: Optional[bool] = None

EVENT_Q: "queue.Queue[HudEvent]" = queue.Queue()
flask_app = Flask(__name__)

@flask_app.get("/health")
def _health():
    return jsonify({"ok": True})

@flask_app.post("/event")
def _event():
    try:
        data = request.get_json(force=True, silent=False) or {}
        t = str(data.get("type", "")).lower()
        if t in ("partial", "final", "assistant"):
            EVENT_Q.put(HudEvent(t, text=str(data.get("text", ""))))
        elif t == "speaking":
            EVENT_Q.put(HudEvent("speaking", value=bool(data.get("value", False))))
        elif t == "mode":
            EVENT_Q.put(HudEvent("mode", text=str(data.get("value", ""))))
        elif t == "shutdown":
            EVENT_Q.put(HudEvent("system", text="__shutdown__"))
        elif t == "system":
            # swallow health pings
            if str(data.get("text", "")) != "ping":
                EVENT_Q.put(HudEvent("system", text=str(data.get("text", ""))))
        else:
            EVENT_Q.put(HudEvent("system", text=f"Unknown event: {data}"))
        return jsonify({"ok": True})
    except Exception as e:
        EVENT_Q.put(HudEvent("system", text=f"/event error: {e}"))
        return jsonify({"ok": False, "error": str(e)}), 400

def run_server():
    try:
        serve(flask_app, host=HUD_HOST, port=HUD_PORT, threads=4)
    except Exception as e:
        EVENT_Q.put(HudEvent("system", text=f"server error: {e}"))

# ========= Helpers =========
def process_running(name: str) -> bool:
    if not _HAS_PSUTIL:
        return False
    name = name.lower()
    try:
        for p in psutil.process_iter(["name"]):
            n = (p.info.get("name") or "").lower()
            if n == name:
                return True
    except Exception:
        pass
    return False

def bytes_per_sec(prev, now, dt):
    if dt <= 0:
        return 0.0
    return max(0.0, (now - prev) / dt)

def format_rate(bps: float) -> str:
    # simple pretty
    kb = 1024.0
    mb = kb * 1024.0
    gb = mb * 1024.0
    if bps >= gb: return f"{bps/gb:.2f} GB/s"
    if bps >= mb: return f"{bps/mb:.2f} MB/s"
    if bps >= kb: return f"{bps/kb:.1f} KB/s"
    return f"{bps:.0f} B/s"

# ========= Visuals =========
class ArcReactor(QtWidgets.QWidget):
    """
    Large blue arc-reactor-like circle: concentric rings and a rotating sweep.
    Animates stronger when speaking=True; gentle idle otherwise.
    """
    def __init__(self):
        super().__init__()
        self._t = 0.0
        self._speaking = False
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)
        self.setMinimumSize(260, 260)
        self._label = "JARVIS"

    def setSpeaking(self, on: bool):
        self._speaking = bool(on)
        self.update()

    def setLabel(self, txt: str):
        self._label = txt or ""
        self.update()

    def sizeHint(self):
        return QtCore.QSize(340, 340)

    def _tick(self):
        self._t += 0.02 if self._speaking else 0.007
        self.update()

    def paintEvent(self, e: QtGui.QPaintEvent):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)

        w, h = self.width(), self.height()
        cx, cy = w/2, h/2
        R = min(w, h) * 0.44

        # background
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0))

        # outer glow
        for i in range(3):
            alpha = 40 - i*10
            pen = QtGui.QPen(QtGui.QColor(0, 229, 255, alpha), 10 + i*8)
            pen.setCapStyle(QtCore.Qt.RoundCap)
            p.setPen(pen); p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, cy), R + i*6, R + i*6)

        # main outer ring
        pen = QtGui.QPen(QtGui.QColor("#00E5FF"), 6)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        p.setPen(pen)
        p.drawEllipse(QtCore.QPointF(cx, cy), R, R)

        # rotating sweep
        sweep = 95 + 45 * (0.5 + 0.5*math.sin(self._t*3.2))
        start = (self._t * 360) % 360
        rect = QtCore.QRectF(cx - R, cy - R, R*2, R*2)
        grad = QtGui.QConicalGradient(QtCore.QPointF(cx, cy), -start)
        grad.setColorAt(0.00, QtGui.QColor("#34C6FF"))
        grad.setColorAt(0.35, QtGui.QColor("#0088CC"))
        grad.setColorAt(1.00, QtGui.QColor("#34C6FF"))
        p.setPen(QtGui.QPen(QtGui.QBrush(grad), 10))
        p.drawArc(rect, int((90 - start) * 16), int(-sweep * 16))

        # inner rings
        innerR = R * 0.72
        p.setPen(QtGui.QPen(QtGui.QColor(12, 80, 96, 200), 3))
        p.drawEllipse(QtCore.QPointF(cx, cy), innerR, innerR)
        innerR2 = innerR * 0.65
        p.setPen(QtGui.QPen(QtGui.QColor(8, 50, 60, 180), 2))
        p.drawEllipse(QtCore.QPointF(cx, cy), innerR2, innerR2)

        # center dot pulse
        amp = 3 + (6 if self._speaking else 1) * (0.5 + 0.5*math.sin(self._t*6.283))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(0, 229, 255, 220))
        p.drawEllipse(QtCore.QPointF(cx, cy), 7 + amp, 7 + amp)

        # label
        font = QtGui.QFont("Orbitron", 14, QtGui.QFont.Medium)
        if not QtGui.QFontInfo(font).exactMatch():
            font = QtGui.QFont("Segoe UI", 14, QtGui.QFont.Medium)
        p.setFont(font)
        pen = QtGui.QPen(QtGui.QColor("#B7F2FF"))
        p.setPen(pen)
        text = self._label or "JARVIS"
        rectT = QtCore.QRectF(cx - 120, cy - 14, 240, 28)
        p.drawText(rectT, QtCore.Qt.AlignCenter, text)

class Pill(QtWidgets.QFrame):
    def __init__(self, title: str):
        super().__init__()
        self.setObjectName("Pill")
        self.setStyleSheet("""
            QFrame#Pill {
                background: rgba(10, 12, 14, 0.92);
                border: 1px solid #0E2F36;
                border-radius: 12px;
            }
            QLabel[role="title"] {
                color: #7DE7FF; font-size: 11px; letter-spacing: 2px;
            }
            QLabel[role="value"] {
                color: #CFEFF5; font-size: 14px;
            }
        """)
        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 8, 12, 10); v.setSpacing(4)
        self.title = QtWidgets.QLabel(title); self.title.setProperty("role", "title")
        self.value = QtWidgets.QLabel("-");    self.value.setProperty("role", "value")
        v.addWidget(self.title); v.addWidget(self.value)

    def set_text(self, s: str):
        self.value.setText(s)

# ========= Widgets (auto-hide if not applicable) =========
class ClockPill(Pill):
    def __init__(self): super().__init__("TIME")
    def refresh(self):
        self.set_text(QtCore.QDateTime.currentDateTime().toString("ddd, MMM d | hh:mm AP"))

class CpuPill(Pill):
    def __init__(self): super().__init__("CPU")
    def refresh(self):
        if not _HAS_PSUTIL:
            self.setVisible(False); return
        try:
            usage = psutil.cpu_percent(interval=None)
            temps = None
            if hasattr(psutil, "sensors_temperatures"):
                t = psutil.sensors_temperatures()
                if t:
                    # pick first sensor with data
                    for arr in t.values():
                        if arr:
                            temps = arr[0].current
                            break
            txt = f"{usage:.0f}%"
            if temps is not None:
                txt += f"  |  {temps:.0f} degC"
            self.set_text(txt)
            self.setVisible(True)
        except Exception:
            self.setVisible(False)

class GpuPill(Pill):
    def __init__(self): super().__init__("GPU")
    def refresh(self):
        if not _HAS_GPUTIL:
            self.setVisible(False); return
        try:
            gpus = GPUtil.getGPUs()
            if not gpus:
                self.setVisible(False); return
            g = gpus[0]
            # load is 0..1
            usage = g.load * 100.0
            temp  = getattr(g, "temperature", None)
            mem   = f"{g.memoryUsed:.0f}/{g.memoryTotal:.0f}MB"
            txt = f"{usage:.0f}%"
            if temp is not None:
                txt += f"  |  {temp:.0f} degC"
            txt += f"  |  {mem}"
            self.set_text(txt); self.setVisible(True)
        except Exception:
            self.setVisible(False)

class NetPill(Pill):
    def __init__(self):
        super().__init__("NETWORK")
        self._prev = None
        self._prev_t = None

    def refresh(self):
        if not _HAS_PSUTIL:
            self.setVisible(False); return
        try:
            now = psutil.net_io_counters()
            t = time.time()
            if self._prev is None:
                self._prev = now; self._prev_t = t
                self.set_text("-"); self.setVisible(True); return
            dt = t - self._prev_t
            up = bytes_per_sec(self._prev.bytes_sent, now.bytes_sent, dt)
            down = bytes_per_sec(self._prev.bytes_recv, now.bytes_recv, dt)
            self._prev, self._prev_t = now, t
            self.set_text(f"Up {format_rate(up)}   |   Down {format_rate(down)}")
            self.setVisible(True)
        except Exception:
            self.setVisible(False)

class SpotifyPill(Pill):
    def __init__(self):
        super().__init__("SPOTIFY")
        self._last = ""
    def refresh(self):
        if not process_running("Spotify.exe"):
            self.setVisible(False); return
        title = "Spotify running"
        if _HAS_WINSDK:
            try:
                sessions = wmc.GlobalSystemMediaTransportControlsSessionManager.request_async().get()
                current = sessions.get_current_session()
                if current:
                    info = current.try_get_media_properties_async().get()
                    artist = info.artist or ""
                    track = info.title or ""
                    if track:
                        title = f"{track} - {artist}" if artist else track
            except Exception:
                pass
        if title != self._last:
            self._last = title
        self.set_text(title)
        self.setVisible(True)

class DiscordPill(Pill):
    def __init__(self):
        super().__init__("DISCORD")
    def refresh(self):
        if process_running("Discord.exe"):
            self.set_text("Active"); self.setVisible(True)
        else:
            self.setVisible(False)

# ========= Screens / Modes =========
class StartupScreen(QtWidgets.QWidget):
    """All black screen with a centered ArcReactor that animates when speaking."""
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background: #000;")
        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        self.reactor = ArcReactor()
        lay.addStretch(1); lay.addWidget(self.reactor, 0, QtCore.Qt.AlignCenter); lay.addStretch(1)
        self.reactor.setLabel("")

    def setSpeaking(self, on: bool):
        self.reactor.setSpeaking(on)

class HomeScreen(QtWidgets.QWidget):
    """Black wallpaper-like screen: just the blue rings + 'JARVIS'."""
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background: #000;")
        lay = QtWidgets.QVBoxLayout(self); lay.setContentsMargins(0,0,0,0); lay.setSpacing(0)
        self.reactor = ArcReactor()
        self.reactor.setLabel("JARVIS")
        lay.addStretch(1); lay.addWidget(self.reactor, 0, QtCore.Qt.AlignCenter); lay.addStretch(1)

    def setSpeaking(self, on: bool):
        self.reactor.setSpeaking(on)

class DashboardScreen(QtWidgets.QWidget):
    """Vertical-friendly dashboard: side pillars and center reactor."""
    def __init__(self):
        super().__init__()
        self.setStyleSheet("background: #000;")
        root = QtWidgets.QGridLayout(self)
        root.setContentsMargins(18,18,18,18); root.setHorizontalSpacing(12); root.setVerticalSpacing(12)

        # Left stack (auto-hide widgets still keep layout tight)
        self.leftBox  = QtWidgets.QVBoxLayout(); self.leftBox.setSpacing(10)
        self.clock    = ClockPill()
        self.cpu      = CpuPill()
        self.gpu      = GpuPill()
        self.net      = NetPill()

        self.leftBox.addWidget(self.clock)
        self.leftBox.addWidget(self.cpu)
        self.leftBox.addWidget(self.gpu)
        self.leftBox.addWidget(self.net)
        self.leftBox.addStretch(1)

        # Center reactor
        self.reactor  = ArcReactor()
        self.reactor.setLabel("JARVIS")

        # Right stack: Spotify / Discord (only when running)
        self.rightBox = QtWidgets.QVBoxLayout(); self.rightBox.setSpacing(10)
        self.spotify  = SpotifyPill()
        self.discord  = DiscordPill()

        self.rightBox.addWidget(self.spotify)
        self.rightBox.addWidget(self.discord)
        self.rightBox.addStretch(1)

        # Assemble 3 columns
        leftW  = QtWidgets.QWidget(); leftW.setLayout(self.leftBox)
        rightW = QtWidgets.QWidget(); rightW.setLayout(self.rightBox)
        root.addWidget(leftW,  0, 0, 1, 1)
        root.addWidget(self.reactor, 0, 1, 1, 1, QtCore.Qt.AlignCenter)
        root.addWidget(rightW, 0, 2, 1, 1)

        # Timers for live data
        self._timer = QtCore.QTimer(self); self._timer.timeout.connect(self.refresh); self._timer.start(1000)

    def setSpeaking(self, on: bool): self.reactor.setSpeaking(on)

    def refresh(self):
        # Left
        self.clock.refresh()
        self.cpu.refresh()
        self.gpu.refresh()
        self.net.refresh()
        # Right
        self.spotify.refresh()
        self.discord.refresh()

# ========= Main Window =========
class JarvisHUD(QtWidgets.QMainWindow):
    def _send_typed(self):
        text = (self._inputEdit.text() or "").strip()
        if not text:
            return
        self._inputEdit.clear()
        # Give a subtle pulse on send
        self._flash_assistant(text)
        url = os.environ.get("JARVIS_INPUT_URL", "http://127.0.0.1:8766/input")
        try:
            requests.post(url, json={"text": text}, timeout=0.6)
        except Exception:
            # Non-fatal: just a quiet note in the center label for a moment
            self._flash_assistant("Delivery failed")

    def __init__(self, target_screen: Optional[int]):
        super().__init__()
        self.setWindowTitle("JARVIS // HUD")
        self.setWindowIcon(QtGui.QIcon())  # minimal
        self.setStyleSheet("""
            QMainWindow { background: #000; }
        """)
        self._target_screen = target_screen
        self._speaking = False

        # Screens
        self.stack = QtWidgets.QStackedWidget()
        self.startup   = StartupScreen()
        self.home      = HomeScreen()
        self.dashboard = DashboardScreen()
        self.stack.addWidget(self.startup)     # index 0
        self.stack.addWidget(self.home)        # index 1
        self.stack.addWidget(self.dashboard)   # index 2
        # --- bottom input bar ---
        self._inputBar = QtWidgets.QFrame()
        self._inputBar.setObjectName("InputBar")
        self._inputBar.setStyleSheet("""
            QFrame#InputBar {
                background: rgba(8,10,12,0.92);
                border-top: 1px solid #0E2F36;
            }
            QLineEdit#JarvisInput {
                background: rgba(8,10,12,0.95);
                border: 1px solid #0E2F36;
                border-radius: 8px;
                padding: 8px 10px;
                color: #CFEFF5;
                selection-background-color: #00E5FF;
                selection-color: #000;
            }
            QPushButton#SendBtn {
                background: #0E2F36;
                color: #CFEFF5;
                border: 1px solid #00E5FF;
                border-radius: 8px;
                padding: 8px 14px;
            }
            QPushButton#SendBtn:hover { background: #00E5FF; color: #000; }""")
        bar_layout = QtWidgets.QHBoxLayout(self._inputBar)
        bar_layout.setContentsMargins(12, 8, 12, 10)
        bar_layout.setSpacing(8)

        self._inputEdit = QtWidgets.QLineEdit()
        self._inputEdit.setObjectName("JarvisInput")
        self._inputEdit.setPlaceholderText("Type to Jarvis...  (Enter to send)")
        self._sendBtn = QtWidgets.QPushButton("Send")
        self._sendBtn.setObjectName("SendBtn")

        bar_layout.addWidget(self._inputEdit, 1)
        bar_layout.addWidget(self._sendBtn, 0)

        # Wire up
        self._sendBtn.clicked.connect(self._send_typed)
        self._inputEdit.returnPressed.connect(self._send_typed)

        # Container (stack + bottom bar)
        container = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(container)
        v.setContentsMargins(0,0,0,0)
        v.setSpacing(0)
        v.addWidget(self.stack, 1)
        v.addWidget(self._inputBar, 0)

        self.setCentralWidget(container)

        # Start in startup mode
        self.set_mode("startup")

        # Poll events from HTTP
        self._evt = QtCore.QTimer(self); self._evt.timeout.connect(self._drain_events); self._evt.start(30)

        # Health check ticker (optional visual in future)
        self._health = QtCore.QTimer(self); self._health.timeout.connect(self._ping); self._health.start(1500)

        # Hotkeys
        QtGui.QShortcut(QtGui.QKeySequence("F11"),   self, activated=self._toggle_fullscreen)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+M"),self, activated=self.showMinimized)
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+R"),self, activated=self._toggle_frameless)
        QtGui.QShortcut(QtGui.QKeySequence("Esc"),   self, activated=self.close)

        # Window flags
        self._frameless = True
        self._apply_flags()

        # Place on chosen screen and go fullscreen
        QtCore.QTimer.singleShot(10, self._place_fullscreen)

    # ---------- window behavior ----------
    def _apply_flags(self):
        flags = QtCore.Qt.Window
        if self._frameless:
            flags |= QtCore.Qt.FramelessWindowHint
        flags |= QtCore.Qt.WindowMinimizeButtonHint
        flags |= QtCore.Qt.WindowSystemMenuHint
        flags |= QtCore.Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _place_fullscreen(self):
        screens = QtWidgets.QApplication.screens()
        idx = self._target_screen if self._target_screen is not None else (len(screens) - 1)
        idx = max(0, min(idx, len(screens)-1))
        geo = screens[idx].geometry()
        self.setGeometry(geo)
        self.showFullScreen()

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self._place_fullscreen()

    def _toggle_frameless(self):
        self._frameless = not self._frameless
        self._apply_flags()

    # ---------- modes ----------
    def set_mode(self, mode: str):
        m = (mode or "").lower()
        if m == "startup":
            self.stack.setCurrentWidget(self.startup)
            self._inputBar.setVisible(False)
        elif m == "home":
            self.stack.setCurrentWidget(self.home)
            self._inputBar.setVisible(True)
        elif m == "dashboard":
            self.stack.setCurrentWidget(self.dashboard)
            self._inputBar.setVisible(True)
        else:
            # default to home
            self.stack.setCurrentWidget(self.home)
            self._inputBar.setVisible(True)

    # ---------- events ----------
    def _drain_events(self):
        updates = 0
        while True:
            try:
                ev: HudEvent = EVENT_Q.get_nowait()
            except queue.Empty:
                break
            updates += 1
            if ev.kind == "speaking":
                self._set_speaking(bool(ev.value))
            elif ev.kind == "mode":
                self.set_mode(ev.text or "home")
            elif ev.kind == "assistant":
                # could show a toast; for now pulse center label briefly
                self._flash_assistant(ev.text or "")
            elif ev.kind == "system":
                if ev.text == "__shutdown__":
                    QtWidgets.QApplication.quit(); return
            # partial/final could be logged/ignored in this minimal UI
        if updates:
            self.repaint()

    def _set_speaking(self, on: bool):
        self._speaking = bool(on)
        self.startup.setSpeaking(on)
        self.home.setSpeaking(on)
        self.dashboard.setSpeaking(on)

    def _flash_assistant(self, text: str):
        # briefly swap center label to the first 24 chars
        label = (text or "JARVIS").strip()
        label = (label[:24] + "...") if len(label) > 24 else label
        for screen in (self.home, self.dashboard):
            screen.reactor.setLabel(label)
        QtCore.QTimer.singleShot(1200, lambda: [self.home.reactor.setLabel("JARVIS"),
                                                self.dashboard.reactor.setLabel("JARVIS")])

    def _ping(self):
        # nothing rendered; kept for future health animation
        pass

# ========= Boot =========
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", type=int, default=None, help="Target screen index (0..N-1). Default: last screen")
    ap.add_argument("--mode", type=str, default="home", choices=["startup","home","dashboard"])
    args = ap.parse_args()

    # Start HTTP server
    t = threading.Thread(target=run_server, daemon=True); t.start()

    app = QtWidgets.QApplication(sys.argv)
    # Font
    font = QtGui.QFont("Orbitron", 10)
    if not QtGui.QFontInfo(font).exactMatch():
        font = QtGui.QFont("Segoe UI", 10)
    app.setFont(font)

    ui = JarvisHUD(target_screen=args.screen)
    ui.set_mode(args.mode)
    ui.show()

    sys.exit(app.exec())

if __name__ == "__main__":
    main()
