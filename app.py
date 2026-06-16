"""
app.py — 面试助手 pywebview 入口

用法：
    python app.py
    uv run app.py
"""
import json
import os
import queue
import threading
from typing import Any

import webview
from dotenv import load_dotenv
from anthropic import Anthropic

from meeting import start_mic_asr, start_speaker_asr, subscribe, emit
from interview import InterviewHistory, stream_suggestion, _is_filler

load_dotenv()

_window: "webview.Window | None" = None


def _push(event: dict) -> None:
    if _window is None:
        return
    if event.get("type") in ("asr_final", "asr_update") and _is_filler(event.get("text", "")):
        return
    try:
        _window.evaluate_js(f"handleEvent({json.dumps(event, ensure_ascii=False)})")
    except Exception:
        pass


def _backend() -> None:
    api_key  = os.environ.get("api_key")
    base_url = os.environ.get("base_url")
    model    = os.environ.get("model", "claude-sonnet-4-20250514")

    if not api_key:
        emit({"type": "asr_status", "channel": "mic",     "status": "error"})
        emit({"type": "asr_status", "channel": "speaker", "status": "error"})
        return

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = Anthropic(**kwargs)

    event_queue: queue.Queue[dict] = queue.Queue()

    def _forward(raw_q: queue.Queue, role: str) -> None:
        while True:
            text = raw_q.get()
            event_queue.put({"role": role, "text": text})

    mic_q: queue.Queue = queue.Queue()
    spk_q: queue.Queue = queue.Queue()
    start_mic_asr(mic_q)
    start_speaker_asr(spk_q)
    threading.Thread(target=_forward, args=(mic_q, "interviewee"), daemon=True).start()
    threading.Thread(target=_forward, args=(spk_q, "interviewer"),  daemon=True).start()

    history    = InterviewHistory()
    _running   = threading.Event()
    _retrigger = [False]

    def _run_suggestion() -> None:
        _running.set()
        try:
            text = stream_suggestion(client, model, history)
            if text:
                history.add("assistant", text)
        finally:
            _running.clear()
            if _retrigger[0]:
                _retrigger[0] = False
                threading.Thread(target=_run_suggestion, daemon=True).start()

    def trigger() -> None:
        if _running.is_set():
            _retrigger[0] = True
        else:
            threading.Thread(target=_run_suggestion, daemon=True).start()

    while True:
        try:
            event = event_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if _is_filler(event["text"]):
            continue

        history.add(event["role"], event["text"])
        if event["role"] == "interviewer":
            trigger()


def _on_started(window: "webview.Window") -> None:
    global _window
    _window = window
    subscribe(_push)
    threading.Thread(target=_backend, daemon=True).start()


if __name__ == "__main__":
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
    with open(html_path, encoding="utf-8") as f:
        html = f.read()

    win = webview.create_window(
        title="TTNN面试",
        html=html,
        width=1400,
        height=900,
        resizable=True,
        background_color="#0d1117",
    )
    webview.start(_on_started, win)
