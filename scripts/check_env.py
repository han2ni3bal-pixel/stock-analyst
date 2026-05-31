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
    ("anthropic", "0.40"),
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
    base = os.environ.get("ANTHROPIC_BASE_URL", "")
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    model = os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
    proxy = os.environ.get("STOCK_ANALYST_PROXY", "")

    all_good = True
    if key:
        masked = key[:7] + "***" if len(key) > 10 else "***"
        ok(f"ANTHROPIC_API_KEY / AUTH_TOKEN 已设置 ({masked})")
    else:
        fail("缺少 ANTHROPIC_API_KEY 或 ANTHROPIC_AUTH_TOKEN — 情感分析与综合解读会跳过")
        all_good = False

    if base:
        ok(f"ANTHROPIC_BASE_URL = {base}")
    else:
        warn("ANTHROPIC_BASE_URL 未设置 — 走默认 api.anthropic.com")

    if model:
        ok(f"ANTHROPIC_DEFAULT_OPUS_MODEL = {model}")
    else:
        warn("ANTHROPIC_DEFAULT_OPUS_MODEL 未设置 — 默认 claude-opus-4-7")

    if proxy:
        ok(f"STOCK_ANALYST_PROXY = {proxy}（用于 yfinance 美股数据）")
    else:
        warn("STOCK_ANALYST_PROXY 未设置 — 美股可能无法访问 Yahoo Finance")
    return all_good


def check_anthropic_call() -> bool:
    try:
        import anthropic, httpx
    except ImportError:
        return False
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return False
    try:
        client = anthropic.Anthropic(http_client=httpx.Client(trust_env=False, timeout=30.0))
        model = os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude-opus-4-7")
        r = client.messages.create(
            model=model, max_tokens=16,
            messages=[{"role": "user", "content": "say OK"}],
        )
        text = "".join(b.text for b in r.content if getattr(b, "type", None) == "text")
        ok(f"Anthropic API 连通: {text.strip()[:30]}")
        return True
    except Exception as e:
        fail(f"Anthropic API 调用失败: {e}")
        return False


def main() -> int:
    print("=" * 50)
    print("  stock-analyst 环境自检")
    print("=" * 50)
    results = [
        ("Python 版本", check_python()),
        ("Python 依赖", check_deps()),
        ("Chrome/Chromium", check_chrome()),
        ("环境变量", check_env_vars()),
        ("Anthropic API 连通", check_anthropic_call()),
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
