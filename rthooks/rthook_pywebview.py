"""
PyInstaller runtime hook for pywebview + pythonnet on Windows.

Python.Runtime.dll 在 _internal/pythonnet/runtime/ 下，
但 .NET Framework 加载器找不到它，因为该目录不在 PATH 里。
在这里提前注入，确保 clr/pythonnet 能正常初始化。
"""
import os
import sys

if getattr(sys, "frozen", False) and sys.platform == "win32":
    runtime_dir = os.path.join(sys._MEIPASS, "pythonnet", "runtime")  # type: ignore[attr-defined]
    if os.path.isdir(runtime_dir):
        os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")
