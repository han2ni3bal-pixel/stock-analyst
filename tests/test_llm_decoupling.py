from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from llm_client import get_llm_status, parse_json_text
from report_pdf import build_markdown, render_pdf
from sentiment_llm import analyze_news_with_llm, analyze_news_locally


class LLMDecouplingTests(unittest.TestCase):
    def test_disabled_provider_is_optional(self):
        with patch.dict(os.environ, {"STOCK_ANALYST_LLM_PROVIDER": "disabled"}, clear=True):
            status = get_llm_status()
        self.assertFalse(status.available)
        self.assertEqual(status.provider, "disabled")

    def test_openai_compatible_provider_can_be_selected(self):
        env = {
            "STOCK_ANALYST_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "test-model",
        }
        with patch.dict(os.environ, env, clear=True):
            status = get_llm_status()
        self.assertTrue(status.available)
        self.assertEqual(status.provider, "openai")
        self.assertEqual(status.model, "test-model")

    def test_sentiment_falls_back_locally_without_credentials(self):
        news = pd.DataFrame([{"标题": "公司业绩预增并获重大合同", "内容": "净利润同比增长"}])
        with patch.dict(os.environ, {"STOCK_ANALYST_LLM_PROVIDER": "disabled"}, clear=True):
            result = analyze_news_with_llm(news, "测试股票", "2026-06-18")
        self.assertTrue(result["available"])
        self.assertNotIn("error", result)
        self.assertEqual(result["_provider"], "local-rules")
        self.assertGreater(result["score"], 0)
        self.assertTrue(result["events"])

    def test_local_sentiment_deduplicates_and_handles_negation(self):
        news = pd.DataFrame([
            {"新闻标题": "公司澄清不存在股东减持", "新闻内容": "目前没有减持计划", "发布时间": "2026-06-18"},
            {"新闻标题": "公司澄清：不存在股东减持", "新闻内容": "未发生减持", "发布时间": "2026-06-18"},
        ])
        result = analyze_news_locally(news, "测试股票", "2026-06-18")
        self.assertEqual(result["_event_count"], 1)
        self.assertEqual(result["events"][0]["sources_count"], 2)
        self.assertGreaterEqual(result["score"], 0)

    def test_json_parser_accepts_fenced_output(self):
        self.assertEqual(parse_json_text("```json\n{\"ok\": true}\n```"), {"ok": True})

    def test_pdf_markdown_sanitizes_legacy_auth_errors(self):
        legacy = "Could not resolve authentication method. Expected one of api_key"
        report = {
            "sentiment": {"error": legacy},
            "verdict": {
                "score_total": 0,
                "signals": [{"name": "LLM情感", "score": 0, "detail": legacy}],
            },
        }
        markdown = build_markdown(report)
        self.assertNotIn("Could not resolve authentication method", markdown)
        self.assertIn("未配置 LLM 凭证", markdown)

    def test_pdf_uses_absolute_file_uri(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            output = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--print-to-pdf="))
            Path(output).write_bytes(b"%PDF-1.4\n%%EOF\n")

        with tempfile.TemporaryDirectory() as tmp:
            with patch("report_pdf._find_chrome", return_value="/fake/chrome"), \
                 patch("report_pdf.subprocess.run", side_effect=fake_run), \
                 patch("report_pdf.build_markdown", return_value="# test"):
                path = render_pdf({"code": "000001", "target_date": "20260618"}, tmp)

        self.assertTrue(captured["command"][-1].startswith("file:///"))
        self.assertTrue(Path(path).is_absolute())


if __name__ == "__main__":
    unittest.main()
