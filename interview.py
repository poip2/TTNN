"""
interview.py — 面试辅助主程序

  🔊 面试官说完话  → 自动触发 AI 给出建议（流式打印）
  🎤 面试者说话    → 仅记录到历史，不触发
  🤖 AI 建议       → 基于完整对话上下文生成

按 Ctrl+C 退出。
"""
import os
import re
import sys
import queue
import threading
from typing import Any

from dotenv import load_dotenv
from anthropic import Anthropic

from meeting import start_mic_asr, start_speaker_asr, subscribe, emit

load_dotenv()

# ──────────────────────────────────────────────
# 简历 & 公司信息加载
# ──────────────────────────────────────────────

def _context_dir() -> str:
    """
    返回用户数据目录（resume.md / company.md 所在位置）。
    macOS .app 打包后 sys.executable 在 .app/Contents/MacOS/ 内，
    需要找到 .app bundle 的父目录。
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "darwin":
        path = sys.executable
        while path != os.path.dirname(path):
            path = os.path.dirname(path)
            if path.endswith(".app"):
                return os.path.dirname(path)
    return os.path.dirname(sys.executable)

def _load_context_file(filename: str) -> str:
    path = os.path.join(_context_dir(), filename)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        content = f.read().strip()
    # 文件存在但只有模板标题行（空白简历），视为未填写
    filled_lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
    return content if filled_lines else ""

_RESUME  = _load_context_file("resume.md")
_COMPANY = _load_context_file("company.md")

# ──────────────────────────────────────────────
# 填充词过滤
# ──────────────────────────────────────────────

_FILLER_RE = re.compile(
    r'^[\s，。！？、]*('
    r'嗯+|哦+|啊+|哈+|呃+|额+'
    r'|对[对的]?|好[的了哦]?|行[的了]?'
    r'|是[的哦]?|嗯[嗯哦]?'
    r'|好好|对对|嗯嗯'
    r'|明白[了]?|收到|了解[了]?'
    r'|继续|没问题|可以[的]?'
    r')[\s，。！？、]*$'
)

def _is_filler(text: str) -> bool:
    t = text.strip()
    if _FILLER_RE.match(t):
        return True
    # 极短且不含问句，也跳过
    if len(t) <= 4 and '？' not in t and '?' not in t:
        return True
    return False


def _build_system_prompt() -> str:
    base = """你是一位专业的面试辅导助手，实时帮助面试者应对面试官的提问。

对话记录中包含两类输入：
- 【面试官】：面试官刚才说的话（来自电脑音频）
- 【面试者】：面试者刚才说的话（来自麦克风）

每当面试官发言后，请给出：
1. 一句话点出这道题的考察意图
2. 2-3 个回答要点（简洁，面试者可立刻使用）
3. 一个参考开场白（可选，当题目比较难时提供）

风格要求：简洁、实用、直接，优先帮面试者快速组织出答案。用中文回复。"""

    if _RESUME:
        base += f"\n\n===候选人简历===\n{_RESUME}"
    if _COMPANY:
        base += f"\n\n===目标公司与岗位===\n{_COMPANY}"
    if _RESUME or _COMPANY:
        base += "\n\n请结合以上背景信息，给出更有针对性的建议。"
    return base

SYSTEM_PROMPT = _build_system_prompt()


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

    emit({"type": "suggestion_start"})
    full_text = ""
    with client.messages.stream(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            emit({"type": "suggestion_chunk", "text": chunk})
            full_text += chunk
    emit({"type": "suggestion_end", "text": full_text})
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

    # 预热 TLS 连接，避免第一条建议有冷启动延迟
    def _warmup() -> None:
        try:
            client.messages.create(
                model=model, max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        except Exception:
            pass
    threading.Thread(target=_warmup, daemon=True).start()

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

    # ── 终端默认 handler（前端接入时在此基础上再 subscribe）──
    def _print_event(event: dict) -> None:
        t = event["type"]
        if t in ("asr_final", "asr_update") and _is_filler(event.get("text", "")):
            return
        if t == "asr_update":
            icon = "🔊" if event["role"] == "interviewer" else "🎤"
            print(f"\r{icon} {event['text']}...", end="", flush=True)
        elif t == "asr_final":
            icon = "🔊" if event["role"] == "interviewer" else "🎤"
            label = "面试官" if event["role"] == "interviewer" else "面试者"
            print(f"\r{icon} {label}: {event['text']}")
        elif t == "asr_status":
            icon = "🎤" if event["channel"] == "mic" else "🔊"
            print(f"{icon} [{event['channel']}] {event['status']}")
        elif t == "suggestion_start":
            print("\n" + "─" * 55)
            print("🤖 AI 建议\n")
        elif t == "suggestion_chunk":
            print(event["text"], end="", flush=True)
        elif t == "suggestion_end":
            print("\n" + "─" * 55 + "\n")

    subscribe(_print_event)

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

            if _is_filler(text):
                continue

            history.add(role, text)

            if role == "interviewer":
                trigger()
            # asr_final 事件由 meeting.emit 触发，_print_event 已处理打印

    except KeyboardInterrupt:
        print("\n\n👋 退出面试助手。")
        sys.exit(0)


if __name__ == "__main__":
    main()
