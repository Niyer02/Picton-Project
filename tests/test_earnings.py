import json
import unittest
from datetime import date
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import pandas as pd
from mcp import Client
from yfinance.exceptions import YFRateLimitError

from picton_graph.mcp import finance, queries, server


def earnings(rows=((1.0, 1.1, 10.0),)):
    return pd.DataFrame(
        rows,
        columns=["EPS Estimate", "Reported EPS", "Surprise(%)"],
        index=pd.date_range("2025-01-01 20:00", periods=len(rows), tz="America/New_York"),
    )


def provider(frame):
    return Mock(get_earnings_dates=Mock(return_value=frame))


class EarningsTests(unittest.TestCase):
    def test_eps_outcomes_preserve_surprise_sign_zero_losses_and_missing_values(self):
        stock = provider(
            earnings(
                [
                    (-1.0, -0.8, float("nan")),
                    (0, 0, None),
                    (1, 1, -0.5),
                    (1, 1, 0.5),
                    (1, float("nan"), 10),
                    (None, 0, None),
                    (1, 0.9, None),
                    (float("inf"), 1, float("inf")),
                ]
            )
        )
        with patch.object(finance.yf, "Ticker", return_value=stock):
            result = finance.get_earnings(["NVDA"], "2025-01-01", "2025-01-10")
        stock.get_earnings_dates.assert_called_once_with(limit=100)
        reports = result["series"][0]["reports"]
        self.assertEqual(
            [r["outcome"] for r in reports],
            [
                "beat",
                "met",
                "miss",
                "beat",
                "not_reported",
                "unknown",
                "miss",
                "unknown",
            ],
        )
        self.assertEqual(reports[0]["eps_actual"], -0.8)
        self.assertEqual(reports[1]["eps_actual"], 0)
        self.assertIsNone(reports[0]["surprise_pct"])
        self.assertEqual(reports[2]["surprise_pct"], -0.5)
        self.assertIsNone(reports[4]["eps_actual"])
        self.assertIsNone(reports[-1]["eps_estimate"])
        self.assertIsNone(reports[-1]["surprise_pct"])
        self.assertIn("Yahoo", result["source"])
        self.assertTrue(result["eps_basis"])
        json.dumps(result, allow_nan=False)

    def test_local_dates_are_inclusive_sorted_and_coverage_is_not_the_requested_range(self):
        frame = earnings([(1, 1.1, 10)] * 4).iloc[::-1]
        stock = provider(frame.drop(columns="Surprise(%)"))
        with patch.object(finance.yf, "Ticker", return_value=stock) as ticker:
            result = finance.get_earnings([" nvda ", "NVDA"], "2025-01-02", "2025-01-03")
        ticker.assert_called_once_with("NVDA")
        series = result["series"][0]
        self.assertEqual(
            (series["available_start"], series["available_end"]),
            (
                "2025-01-01",
                "2025-01-04",
            ),
        )
        self.assertEqual(
            [r["earnings_date"] for r in series["reports"]],
            [
                "2025-01-02T20:00:00-05:00",
                "2025-01-03T20:00:00-05:00",
            ],
        )
        self.assertTrue(all(r["surprise_pct"] is None for r in series["reports"]))
        self.assertTrue(all(r["outcome"] == "beat" for r in series["reports"]))
        self.assertEqual(urlsplit(series["source_url"]).hostname, "finance.yahoo.com")
        with patch.object(finance.yf, "Ticker", return_value=stock):
            empty = finance.get_earnings(["NVDA"], "2024-01-01", "2024-01-10")
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["series"][0]["reports"], [])

    def test_conflicting_duplicates_outside_requested_dates_do_not_block_reports(self):
        frame = earnings([(1, 1.1, 10), (1, 0.8, -20), (1, 0.9, -10)])
        frame.index = pd.to_datetime(
            ["2006-01-01 20:00", "2006-01-01 20:00", "2025-01-02 20:00"]
        ).tz_localize("America/New_York")
        with patch.object(finance.yf, "Ticker", return_value=provider(frame)):
            result = finance.get_earnings(["NVDA"], "2025-01-01", "2025-01-10")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["errors"], {})
        series = result["series"][0]
        self.assertEqual(series["available_start"], "2006-01-01")
        (report,) = series["reports"]
        self.assertEqual(report["earnings_date"], "2025-01-02T20:00:00-05:00")
        self.assertEqual(report["outcome"], "miss")

    def test_unusable_provider_frames_are_explicit_ticker_errors(self):
        frame = earnings()
        frames = [
            None,
            pd.DataFrame(),
            frame.drop(columns="Reported EPS"),
            pd.concat([frame, frame]),
            frame.tz_localize(None),
            earnings([(1, 1.1, 10)] * 101),
        ]
        for frame in frames:
            with (
                self.subTest(frame=frame),
                patch.object(finance.yf, "Ticker", return_value=provider(frame)),
            ):
                result = finance.get_earnings(["NVDA"], "2025-01-01", "2025-01-10")
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["series"], [])
            self.assertEqual(set(result["errors"]), {"NVDA"})

    def test_partial_failures_are_sanitized_and_rate_limits_stop_later_requests(self):
        for error, requested in [(RuntimeError("secret-cookie"), 3), (YFRateLimitError(), 2)]:
            good, bad = provider(earnings()), provider(None)
            bad.get_earnings_dates.side_effect = error
            with (
                self.subTest(error=error),
                patch.object(finance.yf, "Ticker", side_effect=[good, bad, good]) as ticker,
            ):
                result = finance.get_earnings(["NVDA", "AMD", "INTC"], "2025-01-01", "2025-01-10")
            self.assertEqual(ticker.call_count, requested)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(set(result["errors"]), {"AMD", "INTC"} if requested == 2 else {"AMD"})
            self.assertEqual(len(result["series"]), 1 if requested == 2 else 2)
            self.assertNotIn("secret-cookie", json.dumps(result))


class EarningsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_validation_precedes_provider_and_graph_access(self):
        base = {"tickers": ["NVDA"], "start_date": "2025-01-01", "end_date": "2025-01-10"}
        invalid = [
            {"tickers": value}
            for value in (
                [],
                ["NVDA"] * 16,
                "NVDA",
                [123],
                [True],
                None,
                ["bad/name"],
            )
        ]
        invalid += [
            {"start_date": 20250101},
            {"end_date": None},
            {"start_date": "20250101"},
            {"start_date": "2025-02-30"},
            {"start_date": "2025-01-11"},
            {"start_date": "2010-01-01"},
            {"end_date": date.today().isoformat()},
        ]
        with (
            patch.object(finance.yf, "Ticker") as ticker,
            patch.object(queries, "connect") as graph,
        ):
            async with Client(server.mcp) as client:
                for change in invalid:
                    with self.subTest(change=change):
                        result = await client.call_tool("get_earnings", base | change)
                        self.assertTrue(result.is_error)
            ticker.assert_not_called()
            graph.assert_not_called()

    async def test_returns_structured_earnings_without_finance_chart_or_graph_access(self):
        with (
            patch.object(finance.yf, "Ticker", return_value=provider(earnings())),
            patch.object(queries, "connect") as graph,
        ):
            async with Client(server.mcp) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                result = await client.call_tool(
                    "get_earnings",
                    {
                        "tickers": ["NVDA"],
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-10",
                    },
                )
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(
                result.structured_content["series"][0]["reports"][0]["outcome"], "beat"
            )
            self.assertNotIn("ui", tools["get_earnings"].meta or {})
            graph.assert_not_called()


if __name__ == "__main__":
    unittest.main()
