import hashlib
import hmac
import base64
import urllib.parse
import time
import uuid
import json
import threading
import os
import numpy as np
import sounddevice as sd
import websocket
import random
from dotenv import load_dotenv
import queue
from typing import Callable

# ========== 事件总线 ==========

_listeners: list[Callable] = []

def subscribe(fn: Callable) -> Callable:
    """注册事件监听器，返回取消订阅函数。"""
    _listeners.append(fn)
    return lambda: _listeners.remove(fn)

def emit(event: dict) -> None:
    for fn in _listeners:
        fn(event)

load_dotenv()
SECRET_ID  = os.getenv('TC_ID') or ''
SECRET_KEY = os.getenv('TC_KEY') or ''
APPID      = int(os.getenv('TC_APPID') or '0')

# ASR 要求 16kHz 单声道 PCM
ASR_RATE = 16000
ASR_CH   = 1
ASR_CHUNK = 3200   # 200ms @ 16kHz

# 立体声混音设备（系统音频回采）
SPEAKER_DEVICE     = 11
SPEAKER_NATIVE_RATE = 48000   # Realtek 不支持直接 16kHz，用 48kHz 再降采样
SPEAKER_CH         = 2        # 立体声
# 48000/16000 = 3，callback 里 blocksize 也 ×3
SPEAKER_CHUNK      = ASR_CHUNK * (SPEAKER_NATIVE_RATE // ASR_RATE)  # 9600

# ========== 鉴权 ==========

def generate_asr_signature(secret_key: str, appid: int, params: dict) -> str:
    sorted_keys  = sorted(params.keys())
    query_string = "&".join(f"{k}={params[k]}" for k in sorted_keys)
    sign_str     = f"asr.cloud.tencent.com/asr/v2/{appid}?{query_string}"
    digest       = hmac.new(secret_key.encode(), sign_str.encode(), hashlib.sha1).digest()
    return urllib.parse.quote(base64.b64encode(digest).decode())

def get_ws_url() -> str:
    params = {
        "engine_model_type": "16k_zh",
        "expired":           str(int(time.time()) + 3600),
        "need_vad":          "1",
        "vad_silence_time":  "500",
        "nonce":             str(random.randint(1, 9999999999)),
        "secretid":          SECRET_ID,
        "timestamp":         str(int(time.time())),
        "voice_format":      "1",
        "voice_id":          str(uuid.uuid4()),
    }
    signature   = generate_asr_signature(SECRET_KEY, APPID, params)
    query_parts = [f"{k}={params[k]}" for k in sorted(params.keys())]
    query_parts.append(f"signature={signature}")
    return f"wss://asr.cloud.tencent.com/asr/v2/{APPID}?{'&'.join(query_parts)}"

# ========== 共用 WebSocket 回调（__main__ 独立运行用）==========

def on_open(ws: websocket.WebSocketApp) -> None:
    print("✅ 连接成功，开始说话...")

    def callback(indata, frames, t, status):  # type: ignore
        ws.send(indata.tobytes(), websocket.ABNF.OPCODE_BINARY)

    def record() -> None:
        print("🎤 录音中... 按 Ctrl+C 停止\n")
        with sd.InputStream(
            samplerate=ASR_RATE, channels=ASR_CH,
            dtype='int16', blocksize=ASR_CHUNK, callback=callback,
        ):
            try:
                while True:
                    sd.sleep(100)
            except KeyboardInterrupt:
                print("\n⏹ 停止")
                ws.send(json.dumps({"type": "end"}))
                ws.close()

    threading.Thread(target=record, daemon=True).start()

def on_message(ws: websocket.WebSocketApp, message: str) -> None:
    result = json.loads(message)
    if result.get("code") != 0:
        print(f"❌ 错误: {result.get('message')}")
        return
    text     = result.get("result", {}).get("voice_text_str", "")
    is_final = result.get("result", {}).get("slice_type") == 2
    if text:
        print(f"\r{'✅' if is_final else '⏳'} {text}",
              end="\n" if is_final else "", flush=True)

def on_error(ws: websocket.WebSocketApp, error: Exception) -> None:
    print(f"❌ WebSocket 错误: {error}")

def on_close(ws: websocket.WebSocketApp, *_) -> None:  # type: ignore
    pass   # 正常重连，不打印

# ========== 通用 ASR 工厂 ==========

def _make_asr_stream(
    target_queue: queue.Queue,
    role: str,                     # "interviewee" 或 "interviewer"
    channel: str,                  # "mic" 或 "speaker"（用于状态事件）
    label: str,                    # 终端打印前缀，如 "🎤" 或 "🔊"
    device,                        # sounddevice 设备索引，None=默认
    capture_rate: int,             # 采集采样率
    capture_ch: int,               # 采集声道数
    capture_blocksize: int,        # 采集每块帧数
    downsample_ratio: int = 1,     # 降采样比（capture_rate / ASR_RATE）
) -> None:
    """
    通用后台 ASR 流：
    - 单一录音线程，通过 ws_ref 把音频发往当前 WebSocket
    - 收到最终识别结果后关闭连接，run() 自动用新 voice_id 重连
    - 1.5s 静音兜底，防止服务端 slice_type=2 迟迟不来
    """
    last_text = [""]
    _timer: list[threading.Timer | None] = [None]
    ws_ref:  list = [None]

    def _send_final(text: str, ws) -> None:
        last_text[0] = ""
        emit({"type": "asr_final", "role": role, "text": text})
        target_queue.put(text)
        try:
            ws.close()
        except Exception:
            pass

    def _flush() -> None:
        ws   = ws_ref[0]
        text = last_text[0]
        if text and ws:
            _send_final(text, ws)

    def _reset_timer() -> None:
        if _timer[0]:
            _timer[0].cancel()
        _timer[0] = threading.Timer(1.5, _flush)
        _timer[0].start()

    def _on_open(ws) -> None:
        ws_ref[0] = ws
        emit({"type": "asr_status", "channel": channel, "status": "connected"})

    def _on_message(ws, message) -> None:
        result = json.loads(message)
        if result.get("code") != 0:
            print(f"\n❌ ASR({label}): {result.get('message')}")
            return
        asr      = result.get("result", {})
        text     = asr.get("voice_text_str", "")
        is_final = asr.get("slice_type") == 2

        if text:
            last_text[0] = text
            if not is_final:
                emit({"type": "asr_update", "role": role, "text": text})
                _reset_timer()

        if is_final:
            if _timer[0]:
                _timer[0].cancel()
                _timer[0] = None
            final = text or last_text[0]
            if final:
                if not text:
                    print(f"\r✅{label} {final}\n", flush=True)
                _send_final(final, ws)

    def _record() -> None:
        def callback(indata, frames, t, status):  # type: ignore
            ws = ws_ref[0]
            if not ws:
                return
            try:
                if downsample_ratio > 1:
                    # 立体声 → 单声道，再 3:1 降采样到 16kHz
                    mono = indata.mean(axis=1) if indata.ndim > 1 else indata.ravel()
                    down = mono.reshape(-1, downsample_ratio).mean(axis=1)
                    pcm  = down.astype(np.int16).tobytes()
                else:
                    pcm = indata.tobytes()
                ws.send(pcm, websocket.ABNF.OPCODE_BINARY)
            except Exception:
                pass

        with sd.InputStream(
            device=device,
            samplerate=capture_rate,
            channels=capture_ch,
            dtype='int16',
            blocksize=capture_blocksize,
            callback=callback,
        ):
            while True:
                sd.sleep(100)

    def _run() -> None:
        while True:
            try:
                websocket.WebSocketApp(
                    get_ws_url(),
                    on_open=_on_open,
                    on_message=_on_message,
                    on_error=on_error,
                    on_close=on_close,
                ).run_forever()
            except Exception as e:
                emit({"type": "asr_status", "channel": channel, "status": "error", "message": str(e)})
            ws_ref[0] = None
            emit({"type": "asr_status", "channel": channel, "status": "disconnected"})
            time.sleep(1)

    threading.Thread(target=_record, daemon=True).start()
    threading.Thread(target=_run,    daemon=True).start()

# ========== 对外接口 ==========

def start_mic_asr(target_queue: queue.Queue) -> None:
    """麦克风 ASR：16kHz 单声道，直接采集。"""
    _make_asr_stream(
        target_queue      = target_queue,
        role              = "interviewee",
        channel           = "mic",
        label             = "🎤",
        device            = None,
        capture_rate      = ASR_RATE,
        capture_ch        = ASR_CH,
        capture_blocksize = ASR_CHUNK,
        downsample_ratio  = 1,
    )

def start_speaker_asr(target_queue: queue.Queue) -> None:
    """系统音频 ASR：48kHz 立体声回采，降采样到 16kHz 再送 ASR。"""
    _make_asr_stream(
        target_queue      = target_queue,
        role              = "interviewer",
        channel           = "speaker",
        label             = "🔊",
        device            = SPEAKER_DEVICE,
        capture_rate      = SPEAKER_NATIVE_RATE,
        capture_ch        = SPEAKER_CH,
        capture_blocksize = SPEAKER_CHUNK,
        downsample_ratio  = SPEAKER_NATIVE_RATE // ASR_RATE,
    )

# 向后兼容：agent.py 里的 start_asr 调用不需要改
def start_asr(target_queue: queue.Queue) -> None:
    start_mic_asr(target_queue)

# ========== 独立测试入口 ==========

if __name__ == "__main__":
    assert SECRET_ID and SECRET_KEY and APPID, "❌ .env 未读到，请检查 TC_ID / TC_KEY / TC_APP"

    websocket.WebSocketApp(
        get_ws_url(),
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    ).run_forever()
