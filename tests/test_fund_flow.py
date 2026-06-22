from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from fund_flow import summarize_fund_flow
from analyze import Aggregator, Asset, signal_fund_flow


class FundFlowTests(unittest.TestCase):
    @staticmethod
    def _frame(main_net: float) -> pd.DataFrame:
        return pd.DataFrame([{
            "date": "2026-06-18", "main_net": main_net, "main_pct": 1.2,
            "large_net": 4.0, "xlarge_net": 5.0, "close": 11.0, "pct_change": 2.0,
        }])

    def test_eastmoney_uses_latest_snapshot_not_after_target(self):
        frame = pd.DataFrame([
            {"date": "2026-06-17", "main_net": 1.0, "main_pct": 0.1,
             "large_net": 2.0, "xlarge_net": 3.0, "close": 10.0, "pct_change": 1.0},
            {"date": "2026-06-18", "main_net": 20_000_000.0, "main_pct": 1.2,
             "large_net": 4.0, "xlarge_net": 5.0, "close": 11.0, "pct_change": 2.0},
            {"date": "2026-06-22", "main_net": -30_000_000.0, "main_pct": -1.5,
             "large_net": -4.0, "xlarge_net": -5.0, "close": 10.5, "pct_change": -1.0},
        ])
        result = summarize_fund_flow(frame, "eastmoney push2his", "2026-06-19")
        self.assertEqual(result["snapshot_date"], "2026-06-18")
        self.assertEqual(result["main_net"], 20_000_000.0)
        self.assertFalse(result["is_target_date"])

    def test_signal_scores_large_inflow(self):
        asset = Asset("300476", "胜宏科技", "sz", "20260618")
        aggregator = Aggregator()
        with patch("analyze.fetch_with_fallback", return_value=(self._frame(150_000_000), "eastmoney push2his")):
            result = signal_fund_flow(asset, aggregator)
        self.assertTrue(result["available"])
        self.assertEqual(aggregator.signals[-1].score, 1.0)

    def test_signal_marks_missing_data_without_calling_it_neutral(self):
        asset = Asset("300476", "胜宏科技", "sz", "20260618")
        aggregator = Aggregator()
        with patch("analyze.fetch_with_fallback", return_value=(None, None)):
            result = signal_fund_flow(asset, aggregator)
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "all_sources_unavailable")
        self.assertIn("无可用", aggregator.signals[-1].detail)


if __name__ == "__main__":
    unittest.main()
