import json
import shutil
import subprocess
import unittest
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from yfinance.exceptions import YFRateLimitError

from picton_graph.mcp import finance, queries, server


def history(points, currency="USD", timezone="America/New_York"):
    return {
        "currency": currency,
        "timezone": timezone,
        "invalid_rows": 0,
        "prices": [{"date": day, "adjusted_close": price} for day, price in points],
    }


class StockHistoryTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("node"), "Node is needed to check the inline chart calculations"
    )
    def test_chart_aligns_dates_and_rebases_the_visible_window(self):
        script = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const html = require('node:fs').readFileSync(process.argv[1], 'utf8');
const source = html.split('<script>')[1].split('</script>')[0];
new vm.Script(source);
const context = vm.createContext({});
vm.runInContext(source.split('const ids =')[0], context);
vm.runInContext(`
  const prices = values => values.map(([date,adjusted_close]) => ({date,adjusted_close}));
  const rows = [
    {ticker:'NVDA',prices:prices([['2025-01-01',50],['2025-01-02',100],['2025-01-03',110],['2025-01-06',120]])},
    {ticker:'AMD',prices:prices([['2025-01-02',200],['2025-01-03',180],['2025-01-06',190],['2025-01-07',210]])}
  ];
  globalThis.full = commonWindow(rows);
  globalThis.last = commonWindow(rows,2);
  globalThis.invalid = commonWindow([{prices:prices([['2025-01-02',0],['2025-01-03',10]])}]);
  globalThis.duplicate = commonWindow([{prices:prices([['2025-01-02',10],['2025-01-02',11]])}]);
  const response = {status:'ok',series:rows,errors:{},comparison:{status:'ok'}};
  globalThis.comparisonChart = chartData(response);
  globalThis.singleChart = chartData({...response,series:[rows[0]]});
  globalThis.partialChart = chartData({...response,status:'partial',errors:{AMD:'Unavailable'}});
`, context);
assert.equal(JSON.stringify(context.full.dates), JSON.stringify(['2025-01-02','2025-01-03','2025-01-06']));
assert.ok(Math.abs(context.full.rows[0].change - 20) < 1e-8);
assert.ok(Math.abs(context.full.rows[1].change + 5) < 1e-8);
assert.equal(context.last.dates[0], '2025-01-03');
assert.ok(Math.abs(context.last.rows[0].change - (120/110-1)*100) < 1e-8);
assert.ok(Math.abs(context.last.rows[1].change - (190/180-1)*100) < 1e-8);
assert.equal(context.invalid, null);
assert.equal(context.duplicate, null);
assert.equal(context.comparisonChart.comparison, true);
assert.equal(JSON.stringify(context.comparisonChart.lines.map(line=>line.ticker)), JSON.stringify(['NVDA','AMD']));
for (const line of context.comparisonChart.lines) {
  assert.equal(line.values.length, 3);
  assert.equal(line.values[0], 0);
}
assert.ok(Math.abs(context.comparisonChart.lines[0].values[2]-20)<1e-8);
assert.ok(Math.abs(context.comparisonChart.lines[1].values[2]+5)<1e-8);
assert.equal(context.comparisonChart.points[0].date, '2025-01-02');
assert.equal(context.singleChart.comparison, false);
assert.equal(context.singleChart.points[0].close, 50);
assert.equal(context.partialChart, null);
assert.equal(vm.runInContext("dateLabel('2025-01-02')", context), 'Jan 2, 2025');
assert.equal(vm.runInContext("signClass(-0.001)", context), 'neutral');
assert.equal(vm.runInContext("signed(-2.4)", context), '−2.40%');
vm.runInContext(`
  const intraday = {kind:'stock_chart',status:'ok',ticker:'NVDA',interval:'5m',points:[
    {time:'2026-11-04T09:30:00-05:00',date:'2026-11-04',close:200,open:201,volume:1200},
    {time:'2026-11-04T09:35:00-05:00',date:'2026-11-04',close:199,open:200,volume:1500},
    {time:'2026-11-04T09:40:00-05:00',date:'2026-11-04',close:202,open:199,volume:1100}
  ]};
  globalThis.validIntraday = chartData(intraday);
  globalThis.sameObservations = validIntraday.points === intraday.points;
  globalThis.onePoint = chartData({...intraday,points:[intraday.points[0]]});
  globalThis.duplicateTime = chartData({...intraday,points:[intraday.points[0],intraday.points[0]]});
  globalThis.unordered = chartData({...intraday,points:[intraday.points[1],intraday.points[0]]});
  globalThis.badPrice = chartData({...intraday,points:[{...intraday.points[0],close:NaN}]});
  globalThis.empty = chartData({...intraday,points:[]});
  const peer = {...intraday,ticker:'AMD',points:[
    {time:'2026-11-04T14:35:00Z',date:'2026-11-04',close:400},
    {time:'2026-11-04T14:40:00Z',date:'2026-11-04',close:380},
    {time:'2026-11-04T14:45:00Z',date:'2026-11-04',close:385}
  ]};
  const recent = {kind:'stock_comparison',status:'ok',ticker:'NVDA',period:'5d',interval:'5m',series:[intraday,peer]};
  globalThis.recentComparison = chartData(recent);
  globalThis.noSharedBars = chartData({...recent,series:[intraday,{...peer,points:[peer.points[2]]}]});
  globalThis.repeatedTicker = chartData({...recent,series:[intraday,intraday]});
  globalThis.mixedIntervals = chartData({...recent,series:[intraday,{...peer,interval:'1d'}]});
`, context);
assert.equal(context.validIntraday.points.length, 3);
assert.equal(context.sameObservations, true); // Rendering must keep actual bars, without invented observations.
assert.equal(context.validIntraday.points[1].volume, 1500);
assert.equal(context.onePoint.points.length, 1);
assert.equal(context.duplicateTime, null);
assert.equal(context.unordered, null);
assert.equal(context.badPrice, null);
assert.equal(context.empty, null);
assert.equal(context.recentComparison.lines.length, 2);
assert.equal(context.recentComparison.points.length, 2);
assert.equal(context.recentComparison.points[0].time, '2026-11-04T09:35:00-05:00');
assert.equal(context.recentComparison.shared.rows[0].first, 199);
assert.equal(context.recentComparison.shared.rows[1].first, 400);
assert.equal(context.recentComparison.lines[0].values[0], 0);
assert.equal(context.recentComparison.lines[1].values[0], 0);
assert.ok(Math.abs(context.recentComparison.lines[0].values[1]-(202/199-1)*100)<1e-8);
assert.ok(Math.abs(context.recentComparison.lines[1].values[1]+5)<1e-8);
assert.equal(context.noSharedBars, null);
assert.equal(context.repeatedTicker, null);
assert.equal(context.mixedIntervals, null);
"""
        subprocess.run(
            [
                shutil.which("node"),
                "-e",
                script,
                str(Path(server.__file__).parent / "assets" / "stock_chart.html"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_comparison_uses_shared_dates_without_filling_or_mismatched_baselines(self):
        first = history(
            [
                ("2025-01-02", 50.0),
                ("2025-01-03", 100.0),
                ("2025-01-06", 110.0),
                ("2025-01-07", 120.0),
            ]
        )
        second = history([("2025-01-03", 200.0), ("2025-01-07", 180.0), ("2025-01-08", 210.0)])
        with patch.object(
            finance, "fetch_stock_prices", side_effect=deepcopy([first, second])
        ) as fetch:
            result = finance.get_stock_history(
                [" nvda ", "AMD", "NVDA"], "2025-01-01", "2025-01-10"
            )
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["NVDA", "AMD"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["errors"], {})
        comparison = result["comparison"]
        self.assertEqual(comparison["status"], "ok")
        self.assertEqual(
            (comparison["start_date"], comparison["end_date"], comparison["observations"]),
            ("2025-01-03", "2025-01-07", 2),
        )
        returns = {row["ticker"]: row for row in comparison["returns"]}
        self.assertAlmostEqual(returns["NVDA"]["return_pct"], 20.0)
        self.assertAlmostEqual(returns["AMD"]["return_pct"], -10.0)
        self.assertAlmostEqual(returns["NVDA"]["end_index_100"], 120.0)
        self.assertAlmostEqual(returns["AMD"]["end_index_100"], 90.0)
        self.assertEqual(returns["NVDA"]["start_adjusted_close"], 100.0)
        self.assertEqual(returns["AMD"]["end_adjusted_close"], 180.0)
        self.assertEqual(result["series"][0]["prices"], first["prices"])
        self.assertEqual(result["series"][1]["prices"], second["prices"])
        self.assertEqual(result["series"][0]["observations"], 4)
        self.assertEqual(result["series"][0]["first_date"], "2025-01-02")
        self.assertEqual(result["series"][0]["last_date"], "2025-01-07")
        self.assertTrue(
            result["series"][0]["source_url"].startswith("https://finance.yahoo.com/quote/")
        )
        self.assertEqual(result["source"], "Yahoo Finance via yfinance")
        json.dumps(result, allow_nan=False)

    def test_single_ticker_summary_omits_prices_but_preserves_calculated_return(self):
        prices = history([("2021-01-04", 100.0), ("2025-01-02", 150.0)])
        with patch.object(finance, "fetch_stock_prices", return_value=prices):
            result = finance.get_stock_history(
                ["NVDA"], "2021-01-01", "2025-01-03", include_prices=False
            )
        self.assertNotIn("prices", result["series"][0])
        self.assertEqual(result["series"][0]["observations"], 2)
        self.assertAlmostEqual(result["comparison"]["returns"][0]["return_pct"], 50.0)

    def test_invalid_dates_symbols_and_output_budgets_fail_before_fetch(self):
        calls = [
            (["NVDA"], "20250101", "2025-01-10", True),
            (["NVDA"], "2025-W01-1", "2025-01-10", True),
            (["NVDA"], "2025-02-30", "2025-03-10", True),
            (["NVDA"], "2025-02-01", "2025-01-01", True),
            (["NVDA"], "2025-01-01", date.today().isoformat(), False),
            (["NVDA"], "2025-01-01", (date.today() + timedelta(days=1)).isoformat(), False),
            (["NVDA"], "2010-01-01", "2025-01-01", False),
            ([f"T{i}" for i in range(15)], "2024-01-01", "2025-01-01", True),
        ]
        calls.extend(
            ([symbol], "2025-01-01", "2025-01-10", True)
            for symbol in [
                None,
                5,
                True,
                "",
                "   ",
                "NVDA AMD",
                "https://example.com",
                "A/B",
                "A" * 21,
            ]
        )
        with patch.object(finance, "fetch_stock_prices") as fetch:
            for tickers, start, end, include in calls:
                with (
                    self.subTest(tickers=tickers, start=start, end=end),
                    self.assertRaises(ToolError),
                ):
                    finance.get_stock_history(tickers, start, end, include_prices=include)
            fetch.assert_not_called()

    def test_partial_failure_keeps_good_history_without_comparing_a_silent_subset(self):
        available = history([("2025-01-02", 10.0), ("2025-01-03", 11.0)])
        with patch.object(
            finance, "fetch_stock_prices", side_effect=[available, RuntimeError("secret-cookie")]
        ):
            result = finance.get_stock_history(["NVDA", "MISSING"], "2025-01-01", "2025-01-10")
        self.assertEqual(result["status"], "partial")
        self.assertEqual([row["ticker"] for row in result["series"]], ["NVDA"])
        self.assertEqual(set(result["errors"]), {"MISSING"})
        self.assertEqual(result["comparison"]["status"], "unavailable")
        self.assertNotIn("returns", result["comparison"])
        self.assertNotIn("secret-cookie", json.dumps(result))

    def test_rate_limit_stops_dispatch_and_reports_remaining_tickers(self):
        with patch.object(finance, "fetch_stock_prices", side_effect=YFRateLimitError()) as fetch:
            result = finance.get_stock_history(["NVDA", "AMD", "INTC"], "2025-01-01", "2025-01-10")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(set(result["errors"]), {"NVDA", "AMD", "INTC"})
        self.assertEqual(result["comparison"]["status"], "unavailable")
        self.assertEqual(result["series"], [])

    def test_insufficient_overlap_never_reports_a_zero_or_incomparable_return(self):
        for second in (history([("2025-01-03", 20.0)]), history([("2025-01-07", 20.0)])):
            with (
                self.subTest(second=second),
                patch.object(
                    finance,
                    "fetch_stock_prices",
                    side_effect=[history([("2025-01-02", 10.0), ("2025-01-03", 11.0)]), second],
                ),
            ):
                result = finance.get_stock_history(["NVDA", "AMD"], "2025-01-01", "2025-01-10")
            self.assertEqual(result["comparison"]["status"], "unavailable")
            self.assertNotIn("returns", result["comparison"])

    def test_provider_uses_adjusted_close_and_inclusive_requested_end(self):
        frame = pd.DataFrame(
            {"Close": [1000.0, 100.0], "Adj Close": [90.0, 99.0]},
            index=pd.to_datetime(["2025-01-02", "2025-01-03"]).tz_localize("America/New_York"),
        )
        provider = Mock(
            history_metadata={"currency": "USD", "exchangeTimezoneName": "America/New_York"}
        )
        provider.history.return_value = frame
        with patch.object(finance.yf, "Ticker", return_value=provider):
            result = finance.fetch_stock_prices("NVDA", date(2025, 1, 1), date(2025, 1, 3))
        options = provider.history.call_args.kwargs
        self.assertEqual(str(options["start"]), "2025-01-01")
        self.assertEqual(str(options["end"]), "2025-01-04")
        self.assertEqual(options["interval"], "1d")
        self.assertIs(options["auto_adjust"], False)
        self.assertIs(options["actions"], False)
        self.assertIs(options["keepna"], True)
        self.assertIs(options["raise_errors"], True)
        self.assertEqual(options["timeout"], 10)
        self.assertEqual(
            result["prices"],
            [
                {"date": "2025-01-02", "adjusted_close": 90.0},
                {"date": "2025-01-03", "adjusted_close": 99.0},
            ],
        )
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(result["timezone"], "America/New_York")

    def test_invalid_provider_rows_are_counted_and_json_has_no_nonfinite_prices(self):
        values = [10.0, float("nan"), float("inf"), -float("inf"), 0.0, -1.0, 12.0]
        frame = pd.DataFrame({"Adj Close": values}, index=pd.date_range("2025-01-02", periods=7))
        provider = Mock(
            history_metadata={"currency": "USD", "exchangeTimezoneName": "America/New_York"}
        )
        provider.history.return_value = frame
        with patch.object(finance.yf, "Ticker", return_value=provider):
            result = finance.get_stock_history(["NVDA"], "2025-01-01", "2025-01-10")
        self.assertEqual(result["series"][0]["invalid_rows"], 5)
        self.assertEqual(result["series"][0]["observations"], 2)
        self.assertAlmostEqual(result["comparison"]["returns"][0]["return_pct"], 20.0)
        json.dumps(result, allow_nan=False)

    def test_empty_missing_adjustment_and_conflicting_dates_are_explicit_failures(self):
        frames = [
            pd.DataFrame(),
            pd.DataFrame({"Close": [10.0, 11.0]}, index=pd.date_range("2025-01-02", periods=2)),
            pd.DataFrame({"Adj Close": [10.0, 11.0]}, index=pd.to_datetime(["2025-01-02"] * 2)),
        ]
        for frame in frames:
            provider = Mock(history_metadata={"currency": "USD"})
            provider.history.return_value = frame
            with (
                self.subTest(frame=frame),
                patch.object(finance.yf, "Ticker", return_value=provider),
            ):
                result = finance.get_stock_history(["NVDA"], "2025-01-01", "2025-01-10")
            self.assertEqual(result["status"], "unavailable")
            self.assertIn("NVDA", result["errors"])
            self.assertEqual(result["comparison"]["status"], "unavailable")


class StockProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stock_tool_serves_its_inline_finance_app_resource(self):
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertEqual(
                tools["get_stock_history"].meta["ui"]["resourceUri"], server.STOCK_CHART_URI
            )
            resource = await client.read_resource(server.STOCK_CHART_URI)
        (content,) = resource.contents
        self.assertEqual(content.mime_type, "text/html;profile=mcp-app")
        self.assertEqual(content.text, server.stock_chart())
        self.assertLess(len(content.text.encode("utf-8")), 5 * 1024 * 1024)
        self.assertEqual(content.meta["ui"]["csp"]["connectDomains"], [])

    async def test_mcp_rejects_invalid_types_and_list_bounds_before_network(self):
        base = {"tickers": ["NVDA"], "start_date": "2025-01-01", "end_date": "2025-01-10"}
        invalid = [{"tickers": value} for value in [[], ["NVDA"] * 16, "NVDA", [123], [True], None]]
        invalid += [{"include_prices": value} for value in ["false", 1, None]]
        invalid += [{"start_date": 20250101}, {"end_date": None}]
        with patch.object(finance, "fetch_stock_prices") as fetch:
            async with Client(server.mcp) as client:
                for change in invalid:
                    with self.subTest(change=change):
                        result = await client.call_tool("get_stock_history", base | change)
                        self.assertTrue(result.is_error)
            fetch.assert_not_called()

    async def test_mcp_returns_structured_comparison_without_database_access(self):
        prices = history([("2025-01-02", 100.0), ("2025-01-03", 110.0)])
        with (
            patch.object(finance, "fetch_stock_prices", return_value=prices),
            patch.object(queries, "connect") as database,
        ):
            async with Client(server.mcp) as client:
                result = await client.call_tool(
                    "get_stock_history",
                    {"tickers": ["NVDA"], "start_date": "2025-01-01", "end_date": "2025-01-10"},
                )
            self.assertFalse(result.is_error, result.content)
            self.assertEqual(result.structured_content["comparison"]["status"], "ok")
            database.assert_not_called()


if __name__ == "__main__":
    unittest.main()
