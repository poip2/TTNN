import hashlib
import hmac
import base64
import urllib.parse
import time
import uuid
import json
import threading
import os
import sounddevice as sd
import websocket
import random
from dotenv import load_dotenv
import queue

load_dotenv()
SECRET_ID  = os.getenv('TC_ID') or ''
SECRET_KEY = os.getenv('TC_KEY') or ''
APPID      = int(os.getenv('TC_APPID') or '0')

SAMPLE_RATE = 16000
CHANNELS    = 1
CHUNK       = 3200

# ========== 鉴权 ==========
def generate_asr_signature(secret_key: str, appid: int, params: dict) -> str:
    sorted_keys  = sorted(params.keys())
    query_string = "&".join(f"{k}={params[k]}" for k in sorted_keys)
    sign_str     = f"asr.cloud.tencent.com/asr/v2/{appid}?{query_string}"
    # print(f"[DEBUG] sign_str: {sign_str}")   # 加这行
    digest       = hmac.new(secret_key.encode(), sign_str.encode(), hashlib.sha1).digest()
    return urllib.parse.quote(base64.b64encode(digest).decode())

def get_ws_url() -> str:
    params = {
        "engine_model_type": "16k_zh",
        "expired":           str(int(time.time()) + 3600),
        "need_vad":          "1",
        "vad_silence_time":  "500",   # 静音 500ms 即触发 slice_type=2
        "nonce": str(random.randint(1, 9999999999)),
        "secretid":          SECRET_ID,
        "timestamp":         str(int(time.time())),
        "voice_format":      "1",
        "voice_id":          str(uuid.uuid4()),
    }
    signature   = generate_asr_signature(SECRET_KEY, APPID, params)
    query_parts = [f"{k}={params[k]}" for k in sorted(params.keys())]
    query_parts.append(f"signature={signature}")
    return f"wss://asr.cloud.tencent.com/asr/v2/{APPID}?{'&'.join(query_parts)}"

# ========== WebSocket 回调 ==========
# ws_ref 由 start_asr 内部管理；on_open 仅供 __main__ 直接运行时使用
def on_open(ws: websocket.WebSocketApp) -> None:
    print("✅ 连接成功，开始说话...")

    def callback(indata, frames, t, status):  # type: ignore
        ws.send(indata.tobytes(), websocket.ABNF.OPCODE_BINARY)

    def record() -> None:
        print("🎤 录音中... 按 Ctrl+C 停止\n")
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype='int16', blocksize=CHUNK, callback=callback,
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
    print("🔌 连接关闭")



def start_asr(target_queue: queue.Queue) -> None:
    """启动 ASR，把最终识别结果放入 target_queue，后台运行。"""
    last_text = [""]
    _timer: list[threading.Timer | None] = [None]
    ws_ref:  list = [None]   # 始终指向当前活跃的 WebSocket

    def _send_final(text: str, ws) -> None:
        """把一句话推入队列，并关闭当前连接（触发重连=新会话）。"""
        last_text[0] = ""
        print(f"\r🎤 {text}\n", flush=True)
        target_queue.put(text)
        try:
            ws.close()          # 关闭后 run() 自动用新 voice_id 重连
        except Exception:
            pass

    def _flush() -> None:
        """客户端 1.5s 静音兜底：服务端没发 slice_type=2 时自己触发。"""
        ws = ws_ref[0]
        text = last_text[0]
        if text and ws:
            _send_final(text, ws)

    def _reset_timer() -> None:
        if _timer[0]:
            _timer[0].cancel()
        _timer[0] = threading.Timer(1.5, _flush)
        _timer[0].start()

    def on_open_asr(ws) -> None:
        ws_ref[0] = ws
        print("✅ 连接成功，开始说话...")

    def on_message(ws, message) -> None:
        result = json.loads(message)
        if result.get("code") != 0:
            print(f"\n❌ ASR: {result.get('message')}")
            return
        asr        = result.get("result", {})
        text       = asr.get("voice_text_str", "")
        is_final   = asr.get("slice_type") == 2

        if text:
            print(f"\r{'🎤' if is_final else '⏳'} {text}",
                  end="\n" if is_final else "", flush=True)
            last_text[0] = text
            if not is_final:
                _reset_timer()

        if is_final:
            if _timer[0]:
                _timer[0].cancel()
                _timer[0] = None
            final = text or last_text[0]
            if final:
                if not text:
                    print(f"\r🎤 {final}\n", flush=True)
                _send_final(final, ws)

    def record() -> None:
        """全局唯一录音线程；音频通过 ws_ref 发往当前连接。"""
        print("🎤 录音中...\n")

        def callback(indata, frames, t, status):  # type: ignore
            ws = ws_ref[0]
            if ws:
                try:
                    ws.send(indata.tobytes(), websocket.ABNF.OPCODE_BINARY)
                except Exception:
                    pass

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype='int16',
            blocksize=CHUNK,
            callback=callback,
        ):
            while True:
                sd.sleep(100)

    def run() -> None:
        while True:
            try:
                websocket.WebSocketApp(
                    get_ws_url(),
                    on_open=on_open_asr,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                ).run_forever()
            except Exception as e:
                print(f"ASR 异常: {e}")
            ws_ref[0] = None
            time.sleep(1)   # 1s 后用新 voice_id 重连

    threading.Thread(target=record, daemon=True).start()
    threading.Thread(target=run,    daemon=True).start()
# ========== 启动 ==========
if __name__ == "__main__":
    assert SECRET_ID and SECRET_KEY and APPID, "❌ .env 未读到，请检查 TC_ID / TC_KEY / TC_APP"

    websocket.WebSocketApp(
        get_ws_url(),
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    ).run_forever()