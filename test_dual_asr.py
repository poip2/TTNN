"""
双路 ASR 测试：麦克风 + 系统音频同时识别
- 🎤 来自麦克风（你说话）
- 🔊 来自系统音频（腾讯会议/视频里的声音）
按 Ctrl+C 退出。
"""
import queue
import time
import sys
from meeting import start_mic_asr, start_speaker_asr

def main():
    q: queue.Queue = queue.Queue()

    print("启动麦克风 ASR...")
    start_mic_asr(q)

    print("启动系统音频 ASR（立体声混音）...")
    start_speaker_asr(q)

    print("\n等待识别结果（🎤=麦克风  🔊=系统音频）...\n")

    try:
        while True:
            try:
                text = q.get(timeout=1.0)
                # 队列里的消息目前只是纯文字，角色由打印前缀区分
                # 后续 agent 集成时会换成 {"role": ..., "text": ...} 结构
                print(f"[队列收到] {text}")
            except queue.Empty:
                continue
    except KeyboardInterrupt:
        print("\n退出。")
        sys.exit(0)

if __name__ == "__main__":
    main()
