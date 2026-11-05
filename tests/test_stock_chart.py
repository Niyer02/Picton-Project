import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pandas as pd
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from yfinance.exceptions import YFRateLimitError

from picton_graph.mcp import finance, queries, server


def provider(frame=None):
    if frame is None:
        frame = pd.DataFrame(
            {"Open": [100.0, 102.0], "Close": [101.0, 99.0], "Volume": [120, 240]},
            index=pd.to_datetime(["2026-11-04 09:30", "2026-11-04 09:35"]).tz_localize(
                "America/New_York"
            ),
        )
    stock = Mock(
        history_metadata={
            "currency": "USD",
            "exchangeTimezoneName": "America/New_York",
            "shortName": "NVIDIA",
            "exchangeName": "NMS",
            "regularMarketPrice": 98.0,
            "regularMarketTime": 1793889247,
        }
    )
    stock.history.return_value = frame
    stock.get_info.return_value = {
        "longName": "NVIDIA Corporation",
        "fullExchangeName": "NasdaqGS",
        "currency": "USD",
        "exchangeTimezoneName": "America/New_York",
        "regularMarketPrice": 100.0,
        "regularMarketTime": 1793889247,
        "regularMarketOpen": 101.0,
        "regularMarketVolume": 360,
        "regularMarketPreviousClose": 103.0,
        "regularMarketDayLow": 98.0,
        "regularMarketDayHigh": 104.0,
        "marketCap": 123456789000,
        "forwardPE": 15.0,
        "trailingPE": 25.0,
        "fiftyTwoWeekLow": 70.0,
        "fiftyTwoWeekHigh": 110.0,
        "epsTrailingTwelveMonths": 4.0,
        "dividendRate": 0.5,
        "dividendYield": 0.5,
    }
    return stock


class StockChartTests(unittest.TestCase):
    def test_intraday_points_and_snapshot_have_explicit_bases_and_timestamps(self):
        stock = provider()
        with patch.object(finance.yf, "Ticker", return_value=stock) as ticker:
            result = finance.get_stock_chart(" nvda ")
        ticker.assert_called_once_with("NVDA")
        stock.history.assert_called_once_with(
            period="5d",
            interval="5m",
            auto_adjust=False,
            actions=False,
            prepost=False,
            keepna=True,
            raise_errors=True,
            timeout=10,
        )
        self.assertEqual(
            result["points"],
            [
                {
                    "time": "2026-11-04T14:30:00+00:00",
                    "date": "2026-11-04",
                    "close": 101.0,
                    "open": 100.0,
                    "volume": 120.0,
                },
                {
                    "time": "2026-11-04T14:35:00+00:00",
                    "date": "2026-11-04",
                    "close": 99.0,
                    "open": 102.0,
                    "volume": 240.0,
                },
            ],
        )
        self.assertEqual(result["kind"], "stock_chart")
        self.assertEqual(result["name"], "NVIDIA Corporation")
        self.assertEqual(result["interval"], "5m")
        self.assertEqual(
            result["snapshot"]["as_of"],
            datetime.fromtimestamp(1793889247, timezone.utc).isoformat(),
        )
        self.assertEqual(result["snapshot"]["dividend_yield_pct"], 0.5)
        self.assertEqual(result["snapshot"]["price"], 100.0)
        self.assertEqual(result["points"][-1]["close"], 99.0)
        self.assertIn("not dividend-adjusted", result["price_basis"])
        self.assertIn("not historical", result["interpretation"])
        json.dumps(result, allow_nan=False)

    def test_periods_select_real_provider_resolution(self):
        for period, interval in {
            "1d": "1m",
            "5d": "5m",
            "1mo": "30m",
            "3mo": "1d",
            "6mo": "1d",
            "1y": "1d",
            "5y": "1wk",
        }.items():
            stock = provider()
            with (
                self.subTest(period=period),
                patch.object(finance.yf, "Ticker", return_value=stock),
            ):
                result = finance.get_stock_chart("NVDA", period)
            self.assertEqual(stock.history.call_args.kwargs["interval"], interval)
            self.assertEqual(result["interval"], interval)

    def test_snapshot_failure_keeps_chart_and_never_invents_missing_metrics(self):
        for error in (RuntimeError("secret-cookie"), YFRateLimitError()):
            stock = provider()
            stock.get_info.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(finance.yf, "Ticker", return_value=stock),
            ):
                result = finance.get_stock_chart("NVDA")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["points"]), 2)
            self.assertEqual(result["name"], "NVIDIA")
            self.assertEqual(result["snapshot"]["price"], 98.0)
            self.assertIsNone(result["snapshot"]["forward_pe"])
            self.assertIsNone(result["snapshot"]["dividend_yield_pct"])
            self.assertTrue(result["warnings"])
            self.assertNotIn("secret-cookie", json.dumps(result))

    def test_invalid_rows_are_counted_without_filling_and_optional_numbers_are_null(self):
        frame = pd.DataFrame(
            {
                "Close": [10.0, float("nan"), 0.0, 12.0],
                "Open": [float("nan"), 10, 10, -1],
                "Volume": [0, 100, 100, float("inf")],
            },
            index=pd.date_range("2026-11-04 09:30", periods=4, freq="5min", tz="America/New_York"),
        )
        stock = provider(frame)
        stock.get_info.return_value.update(
            forwardPE=float("inf"),
            trailingPE=float("nan"),
            epsTrailingTwelveMonths=-2.0,
            regularMarketTime=float("inf"),
        )
        with patch.object(finance.yf, "Ticker", return_value=stock):
            result = finance.get_stock_chart("NVDA")
        self.assertEqual(result["invalid_rows"], 2)
        self.assertEqual([p["close"] for p in result["points"]], [10.0, 12.0])
        self.assertEqual(result["points"][0]["volume"], 0.0)
        self.assertIsNone(result["points"][1]["volume"])
        self.assertTrue(all(p["open"] is None for p in result["points"]))
        self.assertIsNone(result["snapshot"]["forward_pe"])
        self.assertIsNone(result["snapshot"]["trailing_pe"])
        self.assertIsNone(result["snapshot"]["as_of"])
        self.assertEqual(result["snapshot"]["eps"], -2.0)
        json.dumps(result, allow_nan=False)

    def test_local_date_is_preserved_while_timestamp_is_utc(self):
        frame = pd.DataFrame(
            {"Close": [10.0, 12.0]},
            index=pd.date_range("2026-11-04 00:30", periods=2, freq="5min", tz="Asia/Tokyo"),
        )
        stock = provider(frame)
        with patch.object(finance.yf, "Ticker", return_value=stock):
            result = finance.get_stock_chart("NVDA")
        self.assertEqual(result["points"][0]["date"], "2026-11-04")
        self.assertTrue(result["points"][0]["time"].startswith("2026-11-03T15:30"))

    def test_comparison_keeps_real_intraday_resolution_and_only_shared_utc_points(self):
        first = provider(
            pd.DataFrame(
                {"Close": [100, 101, 102], "Volume": [10, 20, 30]},
                index=pd.date_range(
                    "2026-11-04 09:30", periods=3, freq="5min", tz="America/New_York"
                ),
            )
        )
        second = provider(
            pd.DataFrame(
                {"Close": [200, 198, 197], "Volume": [40, 50, 60]},
                index=pd.date_range("2026-11-04 14:35", periods=3, freq="5min", tz="Europe/London"),
            )
        )
        with patch.object(finance.yf, "Ticker", side_effect=[first, second]) as ticker:
            result = finance.get_stock_chart(" nvda ", compare_to=[" amd ", "NVDA", "AMD"])
        self.assertEqual([call.args[0] for call in ticker.call_args_list], ["NVDA", "AMD"])
        self.assertEqual(result["kind"], "stock_comparison")
        self.assertEqual(result["interval"], "5m")
        self.assertEqual(
            result["comparison"],
            {
                "alignment": "time",
                "observations": 2,
                "start": "2026-11-04T14:35:00+00:00",
                "end": "2026-11-04T14:40:00+00:00",
            },
        )
        self.assertEqual([p["close"] for p in result["series"][0]["points"]], [101.0, 102.0])
        self.assertEqual([p["volume"] for p in result["series"][1]["points"]], [40.0, 50.0])
        for stock, output in zip((first, second), result["series"]):
            self.assertEqual(stock.history.call_args.kwargs["interval"], "5m")
            self.assertEqual(output["snapshot"]["price"], 100.0)
            self.assertEqual(output["kind"], "stock_chart")
        json.dumps(result, allow_nan=False)

    def test_comparison_periods_match_single_resolution_and_daily_dates_align_across_timezones(
        self,
    ):
        for period, interval in {
            "1d": "1m",
            "5d": "5m",
            "1mo": "30m",
            "3mo": "1d",
            "6mo": "1d",
            "1y": "1d",
            "5y": "1wk",
        }.items():
            first = provider(
                pd.DataFrame(
                    {"Close": [100, 102]},
                    index=pd.date_range(
                        "2026-11-03 09:30", periods=2, freq="D", tz="America/New_York"
                    ),
                )
            )
            second = provider(first.history.return_value.copy())
            if interval in ("1d", "1wk"):
                second.history.return_value.index = pd.date_range(
                    "2026-11-03 09:00", periods=2, freq="D", tz="Asia/Tokyo"
                )
            with (
                self.subTest(period=period),
                patch.object(finance.yf, "Ticker", side_effect=[first, second]),
            ):
                result = finance.get_stock_chart("NVDA", period, ["AMD"])
            self.assertEqual(result["interval"], interval)
            for stock in (first, second):
                self.assertEqual(stock.history.call_args.kwargs["interval"], interval)
                self.assertEqual(stock.history.call_args.kwargs["period"], period)
            self.assertEqual(
                result["comparison"]["alignment"], "time" if interval.endswith("m") else "date"
            )
            self.assertEqual(result["comparison"]["observations"], 2)

    def test_comparison_input_validated_before_io_and_duplicate_only_keeps_single_contract(self):
        with patch.object(finance.yf, "Ticker") as ticker:
            for compare_to in ("AMD", {}, ("AMD",), ["AMD"] * 5, [42], [""], ["AMD", "A/B"]):
                with self.subTest(compare_to=compare_to), self.assertRaises(ToolError):
                    finance.get_stock_chart("NVDA", compare_to=compare_to)
            ticker.assert_not_called()
        for compare_to in (None, [], [" nvda ", "NVDA"]):
            with (
                self.subTest(compare_to=compare_to),
                patch.object(finance.yf, "Ticker", return_value=provider()) as ticker,
            ):
                result = finance.get_stock_chart("NVDA", compare_to=compare_to)
            ticker.assert_called_once_with("NVDA")
            self.assertEqual(result["kind"], "stock_chart")
            self.assertNotIn("series", result)
            self.assertEqual(len(result["points"]), 2)

    def test_comparison_requires_every_series_and_two_shared_observations(self):
        for error, message in [
            (RuntimeError("secret-cookie"), "no usable chart"),
            (YFRateLimitError(), "rate limited"),
        ]:
            failed = provider()
            failed.history.side_effect = error
            with (
                self.subTest(error=type(error).__name__),
                patch.object(finance.yf, "Ticker", side_effect=[provider(), failed]),
                self.assertRaisesRegex(ToolError, message) as caught,
            ):
                finance.get_stock_chart("NVDA", compare_to=["AMD"])
            self.assertNotIn("secret-cookie", str(caught.exception))
        for start in ("2026-11-04 09:35", "2026-11-05 09:30"):
            other = provider(
                pd.DataFrame(
                    {"Close": [200, 201]},
                    index=pd.date_range(start, periods=2, freq="5min", tz="America/New_York"),
                )
            )
            with (
                self.subTest(start=start),
                patch.object(finance.yf, "Ticker", side_effect=[provider(), other]),
                self.assertRaisesRegex(ToolError, "fewer than two shared"),
            ):
                finance.get_stock_chart("NVDA", compare_to=["AMD"])
        with (
            patch.object(finance.yf, "Ticker", side_effect=[provider(), provider()]),
            self.assertRaisesRegex(ToolError, "duplicate comparison observations"),
        ):
            finance.get_stock_chart("NVDA", "3mo", ["AMD"])

    def test_invalid_input_fails_before_provider_and_bad_series_are_sanitized(self):
        with patch.object(finance.yf, "Ticker") as ticker:
            for symbol in (None, 42, "", "  ", "NVIDIA Corporation", "https://example.com", "A/B"):
                with self.subTest(symbol=symbol), self.assertRaises(ToolError):
                    finance.get_stock_chart(symbol)
            with self.assertRaises(ToolError):
                finance.get_stock_chart("NVDA", "max")
            ticker.assert_not_called()
        for frame in (
            pd.DataFrame(),
            pd.DataFrame(
                {"Close": [10, 11]}, index=pd.to_datetime(["2026-11-04"] * 2).tz_localize("UTC")
            ),
            pd.DataFrame(
                {"Close": [10, float("nan")]},
                index=pd.to_datetime(["2026-11-04"] * 2).tz_localize("UTC"),
            ),
            pd.DataFrame({"Close": [10]}, index=pd.to_datetime(["2026-11-04"])),
            pd.DataFrame(
                {"Close": [10] * 2001},
                index=pd.date_range("2026-11-04", periods=2001, freq="min", tz="UTC"),
            ),
        ):
            stock = provider(frame)
            with (
                patch.object(finance.yf, "Ticker", return_value=stock),
                self.assertRaisesRegex(ToolError, "no usable chart"),
            ):
                finance.get_stock_chart("NVDA")
            stock.get_info.assert_not_called()
        for error, message in [
            (YFRateLimitError(), "rate limited"),
            (RuntimeError("secret-cookie"), "no usable chart"),
        ]:
            stock = provider()
            stock.history.side_effect = error
            with (
                patch.object(finance.yf, "Ticker", return_value=stock),
                self.assertRaisesRegex(ToolError, message) as caught,
            ):
                finance.get_stock_chart("NVDA")
            self.assertNotIn("secret-cookie", str(caught.exception))


class StockChartProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_schema_validation_metadata_and_structured_result(self):
        with (
            patch.object(finance.yf, "Ticker", return_value=provider()) as ticker,
            patch.object(queries, "connect") as database,
        ):
            async with Client(server.mcp) as client:
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                tool = tools["get_stock_chart"]
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertEqual(tool.meta["ui"]["resourceUri"], server.STOCK_CHART_URI)
                for arguments in (
                    {"ticker": 42},
                    {"ticker": "NVDA", "period": "max"},
                    {"ticker": "NVDA", "compare_to": [42]},
                    {"ticker": "NVDA", "compare_to": ["AMD"] * 5},
                ):
                    self.assertTrue((await client.call_tool("get_stock_chart", arguments)).is_error)
                ticker.assert_not_called()
                result = await client.call_tool("get_stock_chart", {"ticker": "NVDA"})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["kind"], "stock_chart")
                comparison = await client.call_tool(
                    "get_stock_chart", {"ticker": "NVDA", "compare_to": ["AMD"]}
                )
                self.assertFalse(comparison.is_error)
                self.assertEqual(comparison.structured_content["kind"], "stock_comparison")
                database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
