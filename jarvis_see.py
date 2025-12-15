# jarvis_see.py
import os
import io
import psutil
import uiautomation as uia
from PIL import Image
from typing import List, Dict, Optional, Tuple

import pytesseract
import ctypes
import win32gui
import win32process
import win32con
import win32ui
import mss

# Make process DPI-aware
try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

# --- If Tesseract isn't on PATH, set it here ---
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# ===================== Win32 helpers =====================

def get_foreground_hwnd() -> int:
    return win32gui.GetForegroundWindow()

def get_window_rect(hwnd: int) -> Optional[Tuple[int,int,int,int]]:
    try:
        return win32gui.GetWindowRect(hwnd)  # (L,T,R,B)
    except Exception:
        return None

def is_window_visible_and_normal(hwnd: int) -> bool:
    if not win32gui.IsWindowVisible(hwnd):
        return False
    placement = win32gui.GetWindowPlacement(hwnd)
    return placement[1] != win32con.SW_SHOWMINIMIZED

def get_window_title(hwnd: int) -> str:
    try:
        return win32gui.GetWindowText(hwnd).strip()
    except Exception:
        return ""

def get_hwnd_process_info(hwnd: int) -> Dict[str, str]:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        p = psutil.Process(pid)
        return {
            "pid": str(pid),
            "exe": (p.name() or ""),
            "path": (p.exe() or "")
        }
    except Exception:
        return {"pid": "", "exe": "", "path": ""}

def list_top_windows(limit: int = 15) -> List[Dict[str, str]]:
    results = []
    def _enum_handler(hwnd, extra):
        if is_window_visible_and_normal(hwnd):
            title = get_window_title(hwnd)
            if title:
                info = get_hwnd_process_info(hwnd)
                results.append({
                    "hwnd": str(hwnd),
                    "title": title,
                    "exe": info["exe"],
                    "pid": info["pid"]
                })
    win32gui.EnumWindows(_enum_handler, None)
    fg = get_foreground_hwnd()
    results.sort(key=lambda w: 0 if int(w["hwnd"]) == fg else 1)
    return results[:limit]

# ===================== Screenshot + OCR =====================

def screenshot_rect(left: int, top: int, width: int, height: int) -> Optional[Image.Image]:
    if width <= 0 or height <= 0:
        return None
    with mss.mss() as sct:
        monitor = {"left": left, "top": top, "width": width, "height": height}
        try:
            raw = sct.grab(monitor)
            return Image.frombytes("RGB", raw.size, raw.rgb)
        except Exception:
            return None

def screenshot_window(hwnd: int) -> Optional[Image.Image]:
    rect = get_window_rect(hwnd)
    if not rect:
        return None
    l, t, r, b = rect
    return screenshot_rect(l, t, r - l, b - t)

def ocr_image(img: Image.Image, lang: str = "eng") -> str:
    gray = img.convert("L")
    try:
        text = pytesseract.image_to_string(gray, lang=lang, config="--psm 6")
        return text.strip()
    except Exception as e:
        return f"[OCR error] {e}"

# ===================== Accessibility (UIA) =====================

def ui_automation_text(hwnd: int, max_nodes: int = 200, max_depth: int = 4) -> str:
    try:
        elem = uia.ControlFromHandle(hwnd)
    except Exception:
        return ""

    lines, queue, count = [], [(elem, 0)], 0
    while queue and count < max_nodes:
        node, depth = queue.pop(0)
        try:
            name = (node.Name or "").strip()
            value = (node.Value or "").strip()
            if name or value:
                indent = "  " * depth
                if name and value and name != value:
                    lines.append(f"{indent}{name} — {value}")
                else:
                    lines.append(f"{indent}{name or value}")
        except Exception:
            pass

        if depth < max_depth:
            try:
                for c in node.GetChildren():
                    queue.append((c, depth + 1))
            except Exception:
                pass
        count += 1

    return "\n".join([ln for ln in lines if ln.strip()])

# ===================== Cursor element inspection =====================

def get_cursor_pos() -> Tuple[int, int]:
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

def element_from_point(x: int, y: int):
    """Return a uiautomation element at screen point (x, y), or None."""
    try:
        return uia.ControlFromPoint(x, y)
    except Exception:
        return None

def rect_to_tuple(rect) -> Optional[Tuple[int, int, int, int]]:
    """Convert uiautomation.Rect to (L, T, R, B) ints."""
    if not rect:
        return None
    try:
        L, T = int(rect.left), int(rect.top)
        R, B = int(rect.right), int(rect.bottom)
        return (L, T, R, B)
    except Exception:
        return None

def summarize_uia_element(elem) -> Dict[str, str]:
    """Collect common properties for 'what is this control?'"""
    if not elem:
        return {}
    try:
        ctrl_type = getattr(elem, "ControlTypeName", "") or ""
        name = (elem.Name or "").strip()
        value = (elem.Value or "").strip()
        access_key = (elem.AccessKey or "").strip()
        help_text = (elem.HelpText or "").strip()
        aid = (elem.AutomationId or "").strip()
        class_name = (elem.ClassName or "").strip()
        pid = str(elem.ProcessId) if getattr(elem, "ProcessId", None) is not None else ""
        rect = rect_to_tuple(elem.BoundingRectangle)
        return {
            "control_type": ctrl_type,
            "name": name,
            "value": value,
            "help_text": help_text,
            "access_key": access_key,
            "automation_id": aid,
            "class_name": class_name,
            "process_id": pid,
            "bounds": str(rect) if rect else ""
        }
    except Exception:
        return {}

def ocr_around_point(x: int, y: int, box_px: int = 280, lang: str = "eng") -> str:
    """Small OCR box centered at cursor for fallback text."""
    half = box_px // 2
    left, top = max(0, x - half), max(0, y - half)
    img = screenshot_rect(left, top, box_px, box_px)
    if not img:
        return ""
    return ocr_image(img, lang=lang)

# ===================== High-level APIs =====================

def describe_current_view(prefer_accessibility: bool = True,
                          include_ocr: bool = True,
                          ocr_lang: str = "eng") -> Dict[str, object]:
    hwnd = get_foreground_hwnd()
    title = get_window_title(hwnd)
    proc = get_hwnd_process_info(hwnd)

    app = {
        "title": title or "(untitled window)",
        "exe": proc.get("exe", ""),
        "pid": proc.get("pid", ""),
        "path": proc.get("path", "")
    }

    acc_text = ui_automation_text(hwnd).strip() if prefer_accessibility else ""
    ocr_text = ""
    if include_ocr:
        img = screenshot_window(hwnd)
        if img:
            ocr_text = ocr_image(img, lang=ocr_lang)

    windows = list_top_windows(limit=12)

    summary_lines = []
    summary_lines.append(f"You’re focused on: {app['exe'] or 'Unknown App'} — “{app['title']}”")
    if acc_text:
        acc_preview = "\n".join(acc_text.splitlines()[:20])
        summary_lines.append("Accessible text (preview):")
        summary_lines.append(acc_preview)
    elif ocr_text:
        ocr_preview = "\n".join(ocr_text.splitlines()[:20])
        summary_lines.append("On-screen text via OCR (preview):")
        summary_lines.append(ocr_preview)
    else:
        summary_lines.append("No readable text found (try enabling OCR/Accessibility or check permissions).")

    return {
        "app": app,
        "other_windows": windows,
        "accessibility_text": acc_text,
        "ocr_text": ocr_text,
        "summary": "\n".join(summary_lines)
    }

def describe_element_at_cursor(include_ocr_fallback: bool = True,
                               ocr_lang: str = "eng",
                               ocr_box_px: int = 280) -> Dict[str, object]:
    """
    Primary API for: 'Jarvis, what does this button do?'
    Returns UIA metadata for the control under the mouse, and optional OCR of a small area.
    """
    x, y = get_cursor_pos()
    elem = element_from_point(x, y)
    meta = summarize_uia_element(elem)
    # Try to infer a short description
    label = meta.get("name") or meta.get("value")
    ctrl_type = meta.get("control_type") or ""
    help_text = meta.get("help_text") or ""
    access_key = meta.get("access_key") or ""
    auto_id = meta.get("automation_id") or ""
    class_name = meta.get("class_name") or ""

    # Fallback OCR near cursor
    ocr_text = ""
    if include_ocr_fallback and not label:
        ocr_text = ocr_around_point(x, y, box_px=ocr_box_px, lang=ocr_lang)

    # Human-ish summary
    lines = []
    if ctrl_type:
        lines.append(f"Control: {ctrl_type}")
    if label:
        lines.append(f"Label/Text: {label}")
    if help_text:
        lines.append(f"Help: {help_text}")
    if access_key:
        lines.append(f"Access Key: {access_key}")
    if auto_id:
        lines.append(f"AutomationId: {auto_id}")
    if class_name:
        lines.append(f"Class: {class_name}")
    if not label and ocr_text:
        first = " ".join(ocr_text.split())[:160]
        if first:
            lines.append(f"OCR nearby: {first}")

    if not lines:
        lines.append("I couldn’t identify a specific control here. It may be a custom-drawn canvas or protected UI.")

    return {
        "screen_point": {"x": x, "y": y},
        "uia": meta,
        "ocr_nearby": ocr_text,
        "summary": "\n".join(lines)
    }

# Manual test
if __name__ == "__main__":
    print(describe_element_at_cursor()["summary"])
