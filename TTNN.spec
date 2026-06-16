# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

# 完整收集有动态加载的包
webview_d,  webview_b,  webview_h  = collect_all("webview")
sd_d,       sd_b,       sd_h       = collect_all("sounddevice")
anthr_d,    anthr_b,    anthr_h    = collect_all("anthropic")
httpx_d,    httpx_b,    httpx_h    = collect_all("httpx")

# 平台专属依赖
if sys.platform == "win32":
    # Windows: pywebview 用 WinForms 后端，依赖 pythonnet + .NET
    pnet_d, pnet_b, pnet_h = collect_all("pythonnet")
    clrl_d, clrl_b, clrl_h = collect_all("clr_loader")
    platform_datas    = [*pnet_d, *clrl_d]
    platform_binaries = [*pnet_b, *clrl_b]
    platform_hidden   = [*pnet_h, *clrl_h, "clr", "webview.platforms.winforms"]
elif sys.platform == "darwin":
    # macOS: pywebview 用 Cocoa 后端，依赖 PyObjC 系列
    objc_d,  objc_b,  objc_h  = collect_all("objc")
    ak_d,    ak_b,    ak_h    = collect_all("AppKit")
    wk_d,    wk_b,    wk_h    = collect_all("WebKit")
    fn_d,    fn_b,    fn_h    = collect_all("Foundation")
    platform_datas    = [*objc_d, *ak_d, *wk_d, *fn_d]
    platform_binaries = [*objc_b, *ak_b, *wk_b, *fn_b]
    platform_hidden   = [
        *objc_h, *ak_h, *wk_h, *fn_h,
        "webview.platforms.cocoa",
        "objc", "AppKit", "WebKit", "Foundation", "Quartz",
    ]
else:
    platform_datas    = []
    platform_binaries = []
    platform_hidden   = []

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[*webview_b, *sd_b, *anthr_b, *httpx_b, *platform_binaries],
    datas=[
        ("ui.html", "."),
        *webview_d, *sd_d, *anthr_d, *httpx_d, *platform_datas,
    ],
    hiddenimports=[
        *webview_h, *sd_h, *anthr_h, *httpx_h,
        *platform_hidden,
        *collect_submodules("websocket"),
        *collect_submodules("dotenv"),
        "certifi",
        "charset_normalizer",
    ],
    hookspath=[],
    runtime_hooks=["rthooks/rthook_pywebview.py"],
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
            # 麦克风权限：弹系统授权框必须有这个，NSAppleMusicUsageDescription 是苹果音乐权限，不是音频录制
            "NSMicrophoneUsageDescription": "需要麦克风权限以识别面试者和面试官的语音",
            "LSUIElement": False,
        },
        entitlements_file="entitlements.plist",
    )
