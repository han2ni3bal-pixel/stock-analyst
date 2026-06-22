"""自检脚本：验证 stock-analyst 运行所需的全部依赖。

用法：
    python scripts/check_env.py

退出码：0=全部通过，1=有缺失。
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys


GREEN, YELLOW, RED, RESET = "\033[92m", "\033[93m", "\033[91m", "\033[0m"


def ok(msg: str) -> None:    print(f"{GREEN}✓{RESET} {msg}")
def warn(msg: str) -> None:  print(f"{YELLOW}!{RESET} {msg}")
def fail(msg: str) -> None:  print(f"{RED}✗{RESET} {msg}")


def check_python() -> bool:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    fail(f"需要 Python 3.10+，当前 {v.major}.{v.minor}.{v.micro}")
    return False


REQUIRED = [
    ("akshare",   "1.18"),
    ("pandas",    "2.0"),
    ("httpx",     "0.27"),
    ("markdown",  "3.5"),
    ("yfinance",  "0.2"),
    ("requests",  "2.31"),
]


def check_deps() -> bool:
    all_good = True
    for pkg, _min in REQUIRED:
        try:
            mod = importlib.import_module(pkg)
            ok(f"{pkg} {getattr(mod, '__version__', '?')}")
        except ImportError:
            fail(f"{pkg} 未安装  →  pip install -r requirements.txt")
            all_good = False
    return all_good


def check_chrome() -> bool:
    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from report_pdf import _find_chrome
    except ImportError as e:
        fail(f"无法导入 report_pdf: {e}")
        return False
    chrome = _find_chrome()
    if chrome:
        ok(f"Chrome/Chromium: {chrome}")
        return True
    warn("Chrome/Chromium 未找到 — PDF 会降级为 HTML 输出")
    warn("  macOS:  brew install --cask google-chrome  或  https://www.google.com/chrome")
    warn("  Linux:  apt install chromium-browser  /  snap install chromium")
    warn("  自定义: export CHROME_BIN=/path/to/chrome")
    return True  # 不强制


def check_env_vars() -> bool:
    proxy = os.environ.get("STOCK_ANALYST_PROXY", "")
    provider = os.environ.get("STOCK_ANALYST_LLM_PROVIDER", "auto")

    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from llm_client import get_llm_status
    status = get_llm_status()
    if status.available:
        ok(f"可选 LLM: {status.provider}/{status.model}（配置={provider}）")
    else:
        warn(f"可选 LLM 未启用：{status.reason}；核心分析与 PDF 不受影响")

    if proxy:
        ok(f"STOCK_ANALYST_PROXY = {proxy}（用于 yfinance 美股数据）")
    else:
        warn("STOCK_ANALYST_PROXY 未设置 — 美股可能无法访问 Yahoo Finance")
    return True


def check_llm_call() -> bool:
    HERE = os.path.dirname(os.path.abspath(__file__))
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from llm_client import generate_text, get_llm_status
    status = get_llm_status()
    if not status.available:
        warn("可选 LLM 连通性跳过")
        return True
    try:
        result = generate_text("say OK", max_tokens=16, timeout_seconds=30, retries=1)
        ok(f"LLM API 连通: {result.text.strip()[:30]}")
        return True
    except Exception as e:
        warn(f"可选 LLM API 调用失败（核心分析仍可运行）: {e}")
        return True


def main() -> int:
    print("=" * 50)
    print("  stock-analyst 环境自检")
    print("=" * 50)
    results = [
        ("Python 版本", check_python()),
        ("Python 依赖", check_deps()),
        ("Chrome/Chromium", check_chrome()),
        ("环境变量", check_env_vars()),
        ("可选 LLM API 连通", check_llm_call()),
    ]
    print()
    print("=" * 50)
    print("  汇总")
    print("=" * 50)
    for name, passed in results:
        (ok if passed else fail)(name)

    failed_critical = not all(passed for name, passed in results
                              if name in ("Python 版本", "Python 依赖"))
    if failed_critical:
        print(f"\n{RED}有关键依赖缺失，无法运行。{RESET}")
        return 1
    print(f"\n{GREEN}基本可以运行。{RESET}（warning 项不影响主功能）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
