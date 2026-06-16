# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

# 完整收集有动态加载的包
webview_d, webview_b, webview_h = collect_all("webview")
sd_d,      sd_b,      sd_h      = collect_all("sounddevice")
anthr_d,   anthr_b,   anthr_h   = collect_all("anthropic")
httpx_d,   httpx_b,   httpx_h   = collect_all("httpx")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[*webview_b, *sd_b, *anthr_b, *httpx_b],
    datas=[
        ("ui.html", "."),           # HTML 界面
        *webview_d, *sd_d, *anthr_d, *httpx_d,
    ],
    hiddenimports=[
        *webview_h, *sd_h, *anthr_h, *httpx_h,
        *collect_submodules("websocket"),
        *collect_submodules("dotenv"),
        "clr",                      # pywebview Windows 后端 (pythonnet)
        "certifi",
        "charset_normalizer",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TTNN",
    debug=False,
    strip=False,
    upx=False,          # UPX 在 macOS 上可能破坏签名
    console=False,      # 无终端窗口
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TTNN",
)

# macOS 专属：打包为 .app bundle
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="TTNN.app",
        icon=None,
        bundle_identifier="com.ttnn.interview",
        info_plist={
            "CFBundleDisplayName": "TTNN面试",
            "CFBundleShortVersionString": "1.0.0",
            "NSMicrophoneUsageDescription": "需要麦克风权限以识别面试者语音",
            "NSAppleMusicUsageDescription": "需要音频权限以识别面试官语音",
            "LSUIElement": False,
        },
    )
