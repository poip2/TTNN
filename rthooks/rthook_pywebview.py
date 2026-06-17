"""
PyInstaller runtime hook：
- Windows：把 pythonnet/runtime 注入 PATH，让 .NET 找到 Python.Runtime.dll
- macOS：把 certifi 证书路径注入 SSL_CERT_FILE，修复 SSL 证书验证失败
"""
import os
import sys

if getattr(sys, "frozen", False):
    if sys.platform == "win32":
        # Windows: .NET 需要从 PATH 找到 Python.Runtime.dll
        runtime_dir = os.path.join(sys._MEIPASS, "pythonnet", "runtime")  # type: ignore[attr-defined]
        if os.path.isdir(runtime_dir):
            os.environ["PATH"] = runtime_dir + os.pathsep + os.environ.get("PATH", "")

    elif sys.platform == "darwin":
        # macOS: Python 打包后不使用系统证书，手动指向 certifi 证书包
        try:
            import certifi
            cert_path = certifi.where()
            os.environ["SSL_CERT_FILE"]      = cert_path
            os.environ["REQUESTS_CA_BUNDLE"] = cert_path
        except ImportError:
            pass
