# jarvis_see_server.py
from flask import Flask, jsonify, request
from waitress import serve
from jarvis_see import describe_current_view, describe_element_at_cursor, list_top_windows

app = Flask(__name__)

@app.get("/health")
def health():
    return jsonify({"ok": True})

def _dedupe_lines(text: str) -> list[str]:
    seen = set()
    out = []
    for line in (text or "").splitlines():
        ln = line.strip()
        if not ln:
            continue
        key = ln.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(ln)
    return out

def _extractive_summary(text: str, max_sents: int = 3) -> str:
    # very simple: split on punctuation and keep the first few non-empty sentences
    import re
    parts = [p.strip() for p in re.split(r"[.!?]\s+", text or "") if p.strip()]
    return (". ".join(parts[:max_sents]) + ("" if not parts[:max_sents] else "."))[:900]

@app.get("/what_am_i_looking_at")
def what_am_i_looking_at():
    prefer_accessibility = request.args.get("acc", "1") == "1"
    include_ocr = request.args.get("ocr", "1") == "1"
    mode = request.args.get("mode", "summary")  # "summary" or "verbose"
    lang = request.args.get("lang", "eng")

    info = describe_current_view(prefer_accessibility=prefer_accessibility,
                                 include_ocr=include_ocr, ocr_lang=lang)

    if mode == "verbose":
        return jsonify(info)

    # summary mode
    raw = "\n".join([info.get('accessibility_text',''), info.get('ocr_text','')]).strip()
    lines = _dedupe_lines(raw)
    cleaned = "\n".join(lines)[:2400]
    return jsonify({
        "app": info.get("app", {}),
        "summary": _extractive_summary(cleaned, max_sents=3)
    })

@app.get("/what_is_under_mouse")
def what_is_under_mouse():
    include_ocr_fallback = request.args.get("ocr", "1") == "1"
    lang = request.args.get("lang", "eng")
    box = int(request.args.get("box", "280"))
    info = describe_element_at_cursor(include_ocr_fallback=include_ocr_fallback,
                                      ocr_lang=lang,
                                      ocr_box_px=box)
    return jsonify(info)

@app.get("/windows")
def windows():
    limit = int(request.args.get("limit", "12"))
    return jsonify({"windows": list_top_windows(limit=limit)})

if __name__ == "__main__":
    # IMPORTANT: do NOT use 8765 (HUD uses that). Pick another port:
    serve(app, host="127.0.0.1", port=8770)
    # For quick dev:
    # app.run(host="127.0.0.1", port=8770, debug=True)

