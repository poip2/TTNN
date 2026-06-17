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

import sys

import webview
from dotenv import load_dotenv
from anthropic import Anthropic

from meeting import start_mic_asr, start_speaker_asr, subscribe, emit
from interview import InterviewHistory, stream_suggestion, _is_filler


def _resource(name: str) -> str:
    """打包后从 _MEIPASS 读资源，开发时从脚本目录读。"""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def _user_data_dir() -> str:
    """
    返回用户数据目录（.env / resume.md 等文件所在位置）。
    - macOS .app：sys.executable 在 .app/Contents/MacOS/ 里，
      需要走到 .app 的父目录
    - Windows / Linux：直接是 exe 所在目录
    - 开发模式：项目根目录
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "darwin":
        # 向上遍历，找到 .app bundle 的父目录
        path = sys.executable
        while path != os.path.dirname(path):
            path = os.path.dirname(path)
            if path.endswith(".app"):
                return os.path.dirname(path)
    return os.path.dirname(sys.executable)


def _load_env() -> None:
    """打包后从用户数据目录找 .env，开发时从当前目录找。"""
    if getattr(sys, "frozen", False):
        load_dotenv(os.path.join(_user_data_dir(), ".env"))
    else:
        load_dotenv()


_load_env()

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
    with open(_resource("ui.html"), encoding="utf-8") as f:
        html = f.read()

    win = webview.create_window(
        title="TTNN面试",
        html=html,
        width=1400,
        height=900,
        resizable=True,
        background_color="#fdf6e3",
    )
    webview.start(_on_started, win)
