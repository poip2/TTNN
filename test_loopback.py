"""
测试系统音频回采（立体声混音 / Stereo Mix）
运行后在电脑上播放任意音频，观察电平是否有变化。
"""
import sys
import numpy as np
import sounddevice as sd

SPEAKER_DEVICE = 11   # 立体声混音 (Realtek HD Audio Stereo input)
SAMPLE_RATE    = 16000
CHUNK          = 3200  # 200ms

def main():
    info = sd.query_devices(SPEAKER_DEVICE)
    print(f"设备: {info['name']}")
    print(f"最大输入声道: {info['max_input_channels']}")
    print(f"原生采样率:   {info['default_samplerate']} Hz")
    print(f"目标采样率:   {SAMPLE_RATE} Hz\n")
    print("开始监听系统音频，请在电脑上播放视频/音乐/腾讯会议...")
    print("按 Ctrl+C 停止\n")

    def callback(indata, frames, t, status):
        if status:
            print(f"\n[status] {status}", flush=True)
        # 立体声 → 单声道
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata.ravel()
        rms  = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
        bars = int(rms / 80)
        print(f"\r电平: {'█' * min(bars, 50):50s} {rms:6.0f}", end="", flush=True)

    try:
        with sd.InputStream(
            device=SPEAKER_DEVICE,
            samplerate=SAMPLE_RATE,
            channels=1,          # sounddevice 自动降为单声道
            dtype="int16",
            blocksize=CHUNK,
            callback=callback,
        ):
            sd.sleep(30_000)     # 监听 30 秒
    except sd.PortAudioError as e:
        print(f"\n\nPortAudio 错误: {e}")
        print("\n尝试使用设备原生采样率...")
        native_sr = int(info["default_samplerate"])
        with sd.InputStream(
            device=SPEAKER_DEVICE,
            samplerate=native_sr,
            channels=1,
            dtype="int16",
            blocksize=int(native_sr * 0.2),   # 200ms
            callback=callback,
        ):
            print(f"已切换到 {native_sr} Hz（Tencent ASR 将在内部重采样）")
            sd.sleep(30_000)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n停止。")
        sys.exit(0)
