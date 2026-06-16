"""
interview.py — 面试辅助主程序

  🔊 面试官说完话  → 自动触发 AI 给出建议（流式打印）
  🎤 面试者说话    → 仅记录到历史，不触发
  🤖 AI 建议       → 基于完整对话上下文生成

按 Ctrl+C 退出。
"""
import os
import sys
import queue
import threading
from typing import Any

from dotenv import load_dotenv
from anthropic import Anthropic

from meeting import start_mic_asr, start_speaker_asr

load_dotenv()

SYSTEM_PROMPT = """你是一位专业的面试辅导助手，实时帮助面试者应对面试官的提问。

对话记录中包含两类输入：
- 【面试官】：面试官刚才说的话（来自电脑音频）
- 【面试者】：面试者刚才说的话（来自麦克风）

每当面试官发言后，请给出：
1. 一句话点出这道题的考察意图
2. 2-3 个回答要点（简洁，面试者可立刻使用）
3. 一个参考开场白（可选，当题目比较难时提供）

风格要求：简洁、实用、直接，优先帮面试者在 30 秒内组织出答案。用中文回复。"""


# ──────────────────────────────────────────────
# 三角色历史管理
# ──────────────────────────────────────────────

class InterviewHistory:
    """维护 interviewer / interviewee / assistant 三角色历史。"""

    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._lock = threading.Lock()

    def add(self, role: str, text: str) -> None:
        with self._lock:
            self._entries.append({"role": role, "text": text})

    def to_llm_messages(self) -> list[dict]:
        """
        转换为 Anthropic API 格式（user / assistant 交替）。

        规则：
          - interviewer / interviewee 消息合并为 user 消息，带角色前缀
          - assistant 消息直接映射
          - 相邻的 user 段自动合并，保证不出现连续 user
        """
        with self._lock:
            entries = list(self._entries)

        messages: list[dict] = []
        pending: list[str] = []

        for entry in entries:
            if entry["role"] == "assistant":
                if pending:
                    messages.append({"role": "user", "content": "\n".join(pending)})
                    pending = []
                messages.append({"role": "assistant", "content": entry["text"]})
            else:
                label = "【面试官】" if entry["role"] == "interviewer" else "【面试者】"
                pending.append(f"{label}: {entry['text']}")

        if pending:
            messages.append({"role": "user", "content": "\n".join(pending)})

        return messages


# ──────────────────────────────────────────────
# LLM 建议（流式）
# ──────────────────────────────────────────────

def stream_suggestion(client: Anthropic, model: str, history: InterviewHistory) -> str:
    messages = history.to_llm_messages()
    if not messages:
        return ""

    print("\n" + "─" * 55)
    print("🤖 AI 建议\n")
    full_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            print(chunk, end="", flush=True)
            full_text += chunk
    print("\n" + "─" * 55 + "\n")
    return full_text


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main() -> None:
    api_key  = os.environ.get("api_key")
    base_url = os.environ.get("base_url")
    model    = os.environ.get("model", "claude-sonnet-4-20250514")

    if not api_key:
        print("❌ 请在 .env 文件中设置 api_key")
        sys.exit(1)

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = Anthropic(**client_kwargs)

    # ── 双路 ASR，统一入 event_queue ──────────────────────
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

    # ── 建议触发控制 ───────────────────────────────────────
    history   = InterviewHistory()
    _running  = threading.Event()
    _retrigger = [False]   # 建议运行中收到新面试官消息时置 True

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
            _retrigger[0] = True   # 等当前建议跑完再重触发
        else:
            threading.Thread(target=_run_suggestion, daemon=True).start()

    # ── 启动提示 ───────────────────────────────────────────
    print("🎙️  面试助手已启动")
    print("   🔊 系统音频 → 识别面试官  │  🎤 麦克风 → 识别面试者")
    print("   面试官说完话后自动生成 AI 建议\n")
    print("   Ctrl+C 退出\n")

    # ── 主循环 ─────────────────────────────────────────────
    try:
        while True:
            try:
                event = event_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            role = event["role"]
            text = event["text"]
            history.add(role, text)

            if role == "interviewer":
                print(f"\n🔊 面试官: {text}")
                trigger()
            else:
                print(f"\n👤 面试者: {text}")

    except KeyboardInterrupt:
        print("\n\n👋 退出面试助手。")
        sys.exit(0)


if __name__ == "__main__":
    main()
