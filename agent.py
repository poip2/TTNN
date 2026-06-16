"""
mini-agent — 最小化 Anthropic Agent 实现
ReAct 模式：Reasoning（推理） + Acting（工具调用）循环
"""

import concurrent.futures
import datetime
import json
import os
from queue import Queue, Empty
# import select
import sys
# import termios
import threading
# import tty
from dataclasses import dataclass, field
from typing import Callable,Any

from dotenv import load_dotenv
from anthropic import Anthropic
from anthropic.types import MessageParam, ToolParam
from meeting import start_asr

import msvcrt
import time
# ──────────────────────────────────────────────
# 事件基础设施（全局，不变）
# ──────────────────────────────────────────────

listeners = []


def subscribe(fn):
    listeners.append(fn)
    return lambda: listeners.remove(fn)


def emit(event):
    for fn in listeners:
        fn(event)


# ──────────────────────────────────────────────
# 工具定义
# ──────────────────────────────────────────────

def get_current_time() -> str:
    """获取当前日期和时间"""
    now = datetime.datetime.now()
    weekday_map = {
        0: "星期一", 1: "星期二", 2: "星期三",
        3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日",
    }
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {weekday_map[now.weekday()]}"


def calculate_expression(expression: str) -> str:
    """安全计算数学表达式（仅允许数字、运算符、空格、括号）"""

    # raise Exception("故意报错") 
    allowed = set("0123456789+-*/.()% ")
    if not all(c in allowed for c in expression):
        return "错误：表达式包含不允许的字符，仅支持数字和 + - * / . ( ) %"
    try:
        result = eval(expression, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"计算错误：{e}"


# Anthropic 工具定义（符合 function-calling 规范）
TOOLS: list[ToolParam] = [
    {
        "name": "get_current_time",
        "description": "获取当前日期和时间，包含星期几信息",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "calculate_expression",
        "description": "计算数学表达式，支持 + - * / . ( ) % 运算",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "要计算的数学表达式，例如 '(10 + 5) * 2'",
                },
            },
            "required": ["expression"],
        },
    },
]

# 工具名 → 函数映射
TOOL_MAP = {
    "get_current_time": get_current_time,
    "calculate_expression": calculate_expression,
}

SYSTEM_PROMPT = """你是一个有用的 AI 助手。你可以使用工具来获取实时信息或进行计算。
回复时请用中文。当用户问时间或要求计算时，请调用相应工具。"""


# ──────────────────────────────────────────────
# 事件流信号
# ──────────────────────────────────────────────

@dataclass
class AgentSignal:
    """外部控制信号，用于取消 Agent 循环"""
    _aborted: threading.Event = field(default_factory=threading.Event)

    def abort(self):
        """外部调用，取消 Agent"""
        self._aborted.set()

    @property
    def aborted(self) -> bool:
        return self._aborted.is_set()

    def reset(self):
        self._aborted.clear()


# ──────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────

@dataclass
class AgentContext:
    """Agent 运行上下文，跨轮次共享"""
    messages: list[MessageParam] = field(default_factory=list)
    queue: Queue = field(default_factory=Queue)
    needs_retry: bool = False


@dataclass
class AgentLoopConfig:
    """Agent 循环配置，包含所有钩子"""
    client: Anthropic
    model: str
    tools: list[ToolParam]
    tool_map: dict[str, Callable]
    system_prompt: str = SYSTEM_PROMPT

    # 消息管道
    transform_context: Callable = lambda msgs: msgs
    convert_to_llm: Callable = lambda msgs: msgs

    # 工具钩子
    before_tool_call: Callable = lambda tc: {"blocked": False, "terminate": False}
    after_tool_call: Callable = lambda tc, result: result

    # 循环控制
    should_stop_after_turn: Callable = lambda ctx: False


# ──────────────────────────────────────────────
# Agent 核心循环（纯函数）
# ──────────────────────────────────────────────

def agentLoop(ctx: AgentContext, config: AgentLoopConfig, signal: AgentSignal | None = None) -> str:
    """
    AgentLoop(ctx, config, signal):
      emit({type: "agent_start"})

      LOOP:
        signal.aborted → emit({type: "agent_cancelled"}); break

        emit({type: "turn_start"})

        context  = config.transformContext(ctx.messages)
        llm_msgs = config.convertToLlm(context)

        tool_calls = []
        FOR event in LLM.stream(llm_msgs, config.model, config.tools, signal):
          ...（收集 text / tool_calls）...

        no tool_calls → emit({type: "turn_end"}); emit({type: "agent_end"}); break

        ctx.messages.push(response)

        terminate = False
        FOR tc in tool_calls:
          hook = config.beforeToolCall(tc)
          hook.blocked
            → result = {content: "已拦截"}
            → result = executeTool(tc.name, tc.input, signal)
               result = config.afterToolCall(tc, result)

          ctx.messages.push(tool_result)
          hook.terminate OR result.terminate → terminate = True

        emit({type: "turn_end"})
        terminate                        → emit({type: "agent_terminate"}); break
        config.shouldStopAfterTurn(ctx)  → emit({type: "agent_stop"}); break

      emit({type: "agent_end"})
    """
    if signal is None:
        signal = AgentSignal()

    emit({"type": "agent_start"})

    while True:
        # 退出点 1: 外部取消
        if signal.aborted:
            emit({"type": "agent_cancelled"})
            return ""

        # 新增：把队列里的消息全部塞进 messages
        while not ctx.queue.empty():
            try:
                msg = ctx.queue.get_nowait()
                ctx.messages.append({"role": "user", "content": msg})
                emit({"type": "queued_message", "text": msg})
            except Empty:
                break

        emit({"type": "turn_start"})

        # ── 消息管道 ────────────────────────────────────
        context = config.transform_context(ctx.messages)
        llm_messages = config.convert_to_llm(context)
        # ──────────────────────────────────────────────

        tool_calls = []
        current = {}
        response_content = []
        text_chunks = []

        with config.client.messages.stream(
            model=config.model,
            max_tokens=1024,
            system=config.system_prompt,
            tools=config.tools,
            messages=llm_messages,
        ) as stream:
            for event in stream:
                # 退出点 1b: 流式过程中也检查
                if signal.aborted:
                    emit({"type": "agent_cancelled"})
                    return ""

                # text_delta → emit message_update
                if event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        emit({"type": "message_update", "text": event.delta.text})
                        text_chunks.append(event.delta.text)
                    # json_delta → 拼接碎片
                    elif event.delta.type == "input_json_delta":
                        if current:
                            current["raw_json"] += event.delta.partial_json

                # block_start → 开始收集 tool_use
                elif event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        current = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "raw_json": "",
                        }
                    elif event.content_block.type == "text":
                        text_chunks = []

                # block_stop → 碎片→dict，推入 tool_calls
                elif event.type == "content_block_stop":
                    if current:
                        current["input"] = json.loads(current["raw_json"]) if current["raw_json"] else {} #type: ignore
                        tool_calls.append(current)
                        response_content.append({
                            "type": "tool_use",
                            "id": current["id"],
                            "name": current["name"],
                            "input": current["input"],
                        })
                        current = {}
                    elif text_chunks:
                        response_content.append({"type": "text", "text": "".join(text_chunks)})

        # 流结束，发射 message_end 事件
        text_blocks = [b for b in response_content if b["type"] == "text"]
        full_text = "".join(b["text"] for b in text_blocks) if text_blocks else ""
        emit({"type": "message_end", "message": full_text})

        # 没有 tool calls → 退出循环
        # if not tool_calls:
        #     emit({"type": "turn_end"})
        #     emit({"type": "agent_end"})
        #     return full_text
        # 无论有无工具调用，都要把 assistant 回复存入历史
        ctx.messages.append({
            "role": "assistant",
            "content": response_content,
        })

        if not tool_calls:
            emit({"type": "turn_end"})
            if ctx.queue.empty():
                # 队列真的空了，正常退出
                emit({"type": "agent_end"})
                return full_text
            # 队列有新消息，continue 回顶部让上面的 drain 处理
            continue

        # 定义工具执行函数
        def executeTool(tc: dict, signal: AgentSignal) -> dict:
            """执行工具，返回 {"content": str, "terminate": bool}"""
            # 检查信号
            if signal.aborted:
                return {"content": "已取消", "terminate": False}
            
            if tc["name"] in config.tool_map:
                fn = config.tool_map[tc["name"]]
                try:
                    result = fn(**tc["input"])
                except Exception as e:
                    result = f"工具执行错误：{e}"
                    ctx.needs_retry = True
            else:
                result = f"未知工具：{tc['name']}"
                ctx.needs_retry = True
            
            # 默认 terminate 为 False
            return {"content": result, "terminate": False}

        # 初始化工具结果列表
        tool_results = []
        
        # 串行 preflight
        preflights = []
        for tc in tool_calls:
            hook = config.before_tool_call(tc)
            preflights.append(hook)

        # 并行 execute
        futures = []
        with concurrent.futures.ThreadPoolExecutor() as executor:
            for tc, hook in zip(tool_calls, preflights):
                if hook.get("blocked", False):
                    # 已拦截的情况，创建一个已完成的future
                    future = concurrent.futures.Future()
                    future.set_result({"content": "已拦截", "terminate": False})
                    futures.append(future)
                else:
                    # 提交工具执行
                    futures.append(executor.submit(executeTool, tc, signal))

        # 等待所有完成
        # concurrent.futures.wait(futures)
        results = [f.result() for f in futures]

        # 按原始顺序写回 messages
        terminate_all = True  # 初始为True，与操作
        for tc, result in zip(tool_calls, results):
            result = config.after_tool_call(tc, result)
            
            # 收集 terminate 条件（所有工具都说停才停）
            terminate_all = terminate_all and result.get("terminate", False)
            
            # ctx.messages.push(tool_result)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": result["content"] if isinstance(result["content"], str) else json.dumps(result["content"], ensure_ascii=False),
            })

        # terminate：所有工具都说停才停（不是一个）
        terminate = terminate_all

        ctx.messages.append({
            "role": "user",
            "content": tool_results,
        })

        emit({"type": "turn_end"})

        # 退出点 2: 工具主动说停
        if terminate:
            emit({"type": "agent_terminate"})
            break

        # 退出点 3: 外部条件判断
        if config.should_stop_after_turn(ctx):
            emit({"type": "agent_stop"})
            break

    emit({"type": "agent_end"})
    return ""


# ──────────────────────────────────────────────
# 交互式对话（主线程独占 stdin）
# ──────────────────────────────────────────────

def main():
    load_dotenv()

    api_key = os.environ.get("api_key")
    if not api_key:
        print("❌ 请在 .env 文件中设置 api_key")
        sys.exit(1)

    base_url = os.environ.get("base_url")
    model = os.environ.get("model", "claude-sonnet-4-20250514")

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    # ── 配置（包含所有钩子）────────────────────────────

    def handle_before_tool_call(tc: dict) -> dict:
        emit({"type": "tool_start", "name": tc["name"], "args": tc["input"]})
        return {"blocked": False, "terminate": False}

    def handle_after_tool_call(tc: dict, result: dict) -> dict:
        emit({"type": "tool_end", "name": tc["name"], "result": result["content"]})
        return result

    client = Anthropic(**client_kwargs) 

    config = AgentLoopConfig(
        client=client,
        model=model,
        tools=TOOLS,
        tool_map=TOOL_MAP,
        before_tool_call=handle_before_tool_call,
        after_tool_call=handle_after_tool_call,
        should_stop_after_turn=lambda ctx: False,
    )

    # ── 上下文（跨轮次共享）────────────────────────────

    ctx = AgentContext()
    start_asr(ctx.queue)          # ← 启动 ASR，后台自动喂数据
    print("⏳ 等待语音输入...\n")
    # ── 事件订阅 ──────────────────────────────────────

    def handle_event(event):
        if event["type"] == "message_update":
            print(event["text"], end="", flush=True)
        elif event["type"] == "message_end":
            print()
        elif event["type"] == "tool_start":
            print(f"  → 即将执行: {event['name']}({event['args']})")
        elif event["type"] == "tool_end":
            print(f"  → 工具结果: {event['result']}")
        elif event["type"] == "agent_cancelled":
            print("\n⚠️  Agent 被外部取消")
        elif event["type"] == "agent_terminate":
            print("\n🛑 工具请求终止")
        elif event["type"] == "agent_stop":
            print("\n🛑 should_stop_after_turn 触发")

    unsubscribe = subscribe(handle_event)

    # ── 主循环 ────────────────────────────────────────

    print(f"🤖 mini-agent (model: {model})")
    print("   输入 'quit' 或 'exit' 退出 | ESC 取消当前任务\n")

    signal = AgentSignal()

    while True:
        try:
            # user_input = input("👤 你: ").strip()
            user_input = None
            while user_input is None:
                try:
                    user_input = ctx.queue.get(timeout=1.0)
                except Empty:
                    continue
            print(f"👤 [语音] {user_input}")
        except (EOFError, KeyboardInterrupt):
            print("\n👋 再见！")
            break

        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            print("👋 再见！")
            break

        # 重置信号，准备新一轮
        signal.reset()

        # 追加用户消息到历史（跨轮次保留）
        ctx.messages.append({"role": "user", "content": user_input})
        ctx.needs_retry = False

        # Agent 跑在后台线程，工具出错自动重试
        while True:
            ctx.needs_retry = False
            agent_thread = threading.Thread(
                target=agentLoop,
                args=(ctx, config, signal),
                daemon=True,
            )
            agent_thread.start()

            # # 主线程独占 stdin，切换到 cbreak 模式监听 ESC
            # fd = sys.stdin.fileno()
            # old_settings = termios.tcgetattr(fd)
            # tty.setcbreak(fd)
            # buf = ""
            # try:
            #     while agent_thread.is_alive():
            #         if select.select([sys.stdin], [], [], 0.05)[0]:
            #             ch = sys.stdin.read(1)
            #             if ch == '\x1b':  # ESC 键
            #                 signal.abort()
            #                 print("\n⚠️  ESC 按下，正在取消...")
            #                 break
            #             elif ch == '\n' or ch == '\r':  # 回车
            #                 if buf.strip():
            #                     ctx.queue.put(buf.strip())
            #                     print(f"\n📨 消息已加入队列: {buf.strip()}")
            #                 buf = ""
            #             elif ch == '\x7f' or ch == '\b':  # 退格键
            #                 if buf:
            #                     buf = buf[:-1]
            #                     print('\b \b', end='', flush=True)
            #             else:
            #                 buf += ch
            #                 print(ch, end='', flush=True)
            # finally:
            #     termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            while agent_thread.is_alive():
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch == b'\x1b':  # ESC
                        signal.abort()
                        print("\n⚠️  ESC 按下，正在取消...")
                        break
                time.sleep(0.05)

            agent_thread.join()

            # 检查是否需要重试（工具出错）
            if not ctx.needs_retry:
                break
            # needs_retry=True，不等用户输入，直接重跑
            print("🔄 工具出错，自动重试中...")
            signal.reset()

    unsubscribe()


if __name__ == "__main__":
    main()
