"""Yahoo Finance prices, earnings results, and aligned return comparisons."""

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from urllib.parse import quote

import yfinance as yf
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field
from yfinance.exceptions import YFRateLimitError


def finite_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_tickers(tickers):
    if any(
        not isinstance(ticker, str)
        or not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,19}", ticker.strip().upper())
        for ticker in tickers
    ):
        raise ToolError(
            "Use Yahoo ticker symbols such as NVDA, BRK-B or SPY, not company names or URLs."
        )
    return list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))


def historical_dates(start_date, end_date):
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start.isoformat() != start_date or end.isoformat() != end_date:
            raise ValueError("Noncanonical dates")
    except (TypeError, ValueError):
        raise ToolError("Use valid dates in YYYY-MM-DD format.") from None
    if not 1 <= (end - start).days + 1 <= 1827 or end >= date.today():
        raise ToolError(
            "Dates must be ordered, before today, and span at most 1,827 calendar days."
        )
    return start, end


def fetch_earnings(symbol, start, end):
    frame = yf.Ticker(symbol).get_earnings_dates(limit=100)
    if (
        frame is None
        or frame.empty
        or len(frame) > 100
        or not {"EPS Estimate", "Reported EPS"} <= set(frame.columns)
        or any(not isinstance(t, datetime) or t.tzinfo is None for t in frame.index)
    ):
        raise ValueError("Missing or malformed earnings calendar")
    selected = frame.loc[[start <= t.date() <= end for t in frame.index]].sort_index()
    if selected.index.has_duplicates:
        raise ValueError("Conflicting earnings dates in the requested window")
    reports = []
    for timestamp, row in selected.iterrows():
        estimate = finite_number(row["EPS Estimate"])
        actual = finite_number(row["Reported EPS"])
        surprise = finite_number(row.get("Surprise(%)"))
        outcome = "not_reported" if actual is None else "unknown"
        if actual is not None and estimate is not None:
            # Yahoo rounds displayed EPS; its surprise can reveal a miss even when both look equal.
            difference = surprise if surprise is not None else actual - estimate
            outcome = "beat" if difference > 0 else "miss" if difference < 0 else "met"
        reports.append(
            {
                "earnings_date": timestamp.isoformat(),
                "eps_estimate": estimate,
                "eps_actual": actual,
                "surprise_pct": surprise,
                "outcome": outcome,
            }
        )
    return {
        "ticker": symbol,
        "source_url": f"https://finance.yahoo.com/calendar/earnings?symbol={quote(symbol, safe='')}",
        "available_start": frame.index.min().date().isoformat(),
        "available_end": frame.index.max().date().isoformat(),
        "reports": reports,
    }


def get_earnings(
    tickers: Annotated[
        list[Annotated[str, Field(strict=True, min_length=1, max_length=20)]],
        Field(min_length=1, max_length=15),
    ],
    start_date: Annotated[str, Field(strict=True, description="Inclusive start date, YYYY-MM-DD.")],
    end_date: Annotated[
        str, Field(strict=True, description="Inclusive end date, YYYY-MM-DD; before today.")
    ],
) -> dict[str, Any]:
    """Get historical earnings announcement dates, actual EPS, estimates, and beat/miss results.

    Accepts 1–15 Yahoo ticker symbols and an inclusive historical date range of
    up to 1,827 days. Example: NVDA and AMD from 2022-01-01 to 2022-12-31.
    Returns structured earnings results, not full earnings-release documents.
    Dates are Yahoo announcement timestamps in their supplied timezone, not fiscal
    quarter-end dates. Each ticker fetches at most 100 calendar entries; available
    bounds describe that fetched calendar, not guaranteed complete coverage.
    Missing actual EPS is not_reported; missing consensus is unknown, never zero.
    Beat/miss/met uses Yahoo's surprise percentage when available, otherwise the
    displayed EPS comparison. Cite source_url and preserve any reported gaps.
    Yahoo may round or retrospectively adjust EPS; the calendar does not specify
    currency or GAAP/non-GAAP methodology. It is not frozen point-in-time consensus.
    Use graph tools for connected companies and get_stock_history for subsequent returns.
    """
    if not 1 <= len(tickers) <= 15:
        raise ToolError("Supply between 1 and 15 Yahoo ticker symbols.")
    symbols = normalize_tickers(tickers)
    start, end = historical_dates(start_date, end_date)
    series, errors = [], {}
    for index, symbol in enumerate(symbols):
        try:
            series.append(fetch_earnings(symbol, start, end))
        except YFRateLimitError:
            errors[symbol] = "Yahoo Finance rate limited this request. Try again later."
            errors.update(
                dict.fromkeys(
                    symbols[index + 1 :],
                    "Not requested because Yahoo Finance rate limited this call.",
                )
            )
            break
        except Exception:
            errors[symbol] = "Yahoo Finance returned no usable earnings data or the request failed."
    return {
        "source": "Yahoo Finance via yfinance",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "requested_start": start_date,
        "requested_end": end_date,
        "status": "partial" if errors and series else "unavailable" if errors else "ok",
        "series": series,
        "errors": errors,
        "eps_basis": "Yahoo calendar values may be rounded or retrospectively adjusted; currency "
        "and GAAP/non-GAAP basis are unspecified. Coverage is limited to 100 fetched entries per ticker.",
    }


def fetch_stock_chart(symbol, period, interval):
    try:
        stock = yf.Ticker(symbol)
        frame = stock.history(
            period=period,
            interval=interval,
            auto_adjust=False,
            actions=False,
            prepost=False,
            keepna=True,
            raise_errors=True,
            timeout=10,
        )
        if frame.empty or len(frame) > 2000 or frame.index.has_duplicates:
            raise ValueError("Missing, duplicate, or oversized price series")
        metadata = stock.history_metadata or {}
        points, invalid_rows = [], 0
        for timestamp, row in frame.iterrows():
            close = finite_number(row.get("Close"))
            if close is None or close <= 0 or timestamp.tzinfo is None:
                invalid_rows += 1
                continue
            time = timestamp.astimezone(timezone.utc).isoformat()
            opening, volume = finite_number(row.get("Open")), finite_number(row.get("Volume"))
            points.append(
                {
                    "time": time,
                    "date": timestamp.date().isoformat(),
                    "close": close,
                    "open": opening if opening is not None and opening > 0 else None,
                    "volume": volume if volume is not None and volume >= 0 else None,
                }
            )
        if not points:
            raise ValueError("No valid close prices")
        points.sort(key=lambda point: point["time"])
    except YFRateLimitError:
        raise ToolError("Yahoo Finance rate limited this request. Try again later.") from None
    except Exception:
        raise ToolError(
            "Yahoo Finance returned no usable chart data. Check the symbol or retry later."
        ) from None

    warnings = []
    try:
        info = stock.get_info()
        if not isinstance(info, dict) or not info:
            raise ValueError("Missing market snapshot")
    except Exception:
        info = {}
        warnings.append(
            "Latest valuation metrics are unavailable; the price chart is still available."
        )
    snapshot_fields = {
        "price": "regularMarketPrice",
        "previous_close": "regularMarketPreviousClose",
        "open": "regularMarketOpen",
        "day_low": "regularMarketDayLow",
        "day_high": "regularMarketDayHigh",
        "volume": "regularMarketVolume",
        "market_cap": "marketCap",
        "forward_pe": "forwardPE",
        "trailing_pe": "trailingPE",
        "fifty_two_week_low": "fiftyTwoWeekLow",
        "fifty_two_week_high": "fiftyTwoWeekHigh",
        "eps": "epsTrailingTwelveMonths",
    }
    snapshot = {
        key: finite_number(info.get(field, metadata.get(field)))
        for key, field in snapshot_fields.items()
    }
    market_time = finite_number(info.get("regularMarketTime", metadata.get("regularMarketTime")))
    try:
        snapshot["as_of"] = (
            datetime.fromtimestamp(market_time, timezone.utc).isoformat() if market_time else None
        )
    except (ValueError, OverflowError, OSError):
        snapshot["as_of"] = None
    annual_dividend, price = finite_number(info.get("dividendRate")), snapshot["price"]
    # Yahoo's dividendYield units vary; derive the percentage from its annual cash rate.
    snapshot["dividend_yield_pct"] = (
        finite_number(annual_dividend / price * 100)
        if annual_dividend is not None and annual_dividend >= 0 and price and price > 0
        else None
    )
    snapshot["dividend_yield_basis"] = "Annual dividend rate / latest regular-market price"
    identity = {
        key: info.get(field) or metadata.get(field)
        for key, field in {
            "name": "longName",
            "exchange": "fullExchangeName",
            "currency": "currency",
            "timezone": "exchangeTimezoneName",
        }.items()
    }
    identity["name"] = (
        identity["name"] or info.get("shortName") or metadata.get("shortName") or symbol
    )
    identity["exchange"] = (
        identity["exchange"] or info.get("exchange") or metadata.get("exchangeName")
    )
    identity = {key: value if isinstance(value, str) else None for key, value in identity.items()}
    identity["name"] = identity["name"] or symbol
    return {
        "kind": "stock_chart",
        "status": "ok",
        "ticker": symbol,
        **identity,
        "period": period,
        "interval": interval,
        "source": "Yahoo Finance via yfinance",
        "source_url": f"https://finance.yahoo.com/quote/{quote(symbol, safe='')}/",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "price_basis": "Yahoo Close, split-adjusted but not dividend-adjusted; regular market hours.",
        "points": points,
        "invalid_rows": invalid_rows,
        "snapshot": snapshot,
        "warnings": warnings,
        "interpretation": "Price change uses the first and last plotted close. Latest snapshot metrics are not historical "
        "point-in-time values. Prices may be delayed; this is not a streaming live feed.",
    }


def get_stock_chart(
    ticker: Annotated[str, Field(strict=True, min_length=1, max_length=20)],
    period: Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "5y"] = "5d",
    compare_to: Annotated[
        list[Annotated[str, Field(strict=True, min_length=1, max_length=20)]], Field(max_length=4)
    ]
    | None = None,
) -> dict[str, Any]:
    """Show detailed recent stock prices, or compare up to five stocks at the same resolution.

    Use for current/recent charts and comparisons, defaulting to five trading days.
    For NVIDIA versus AMD over the last month, use ticker="NVDA", compare_to=["AMD"],
    period="1mo". 1d uses 1-minute bars; 5d uses 5-minute bars; 1mo uses 30-minute
    bars; 3mo/6mo/1y use daily bars; 5y uses weekly bars. Regular market hours only,
    without smoothing or invented observations. Close prices are not dividend-adjusted.
    Comparisons require every ticker and at least two shared observations. Intraday
    points align by UTC timestamp; daily/weekly points by local trading date. Only
    shared observations are returned, with no filling or currency conversion.
    Chart changes start at the first returned close. Snapshot metrics are current
    Yahoo values, not historical values at chart dates. Missing metrics remain null.
    Data can be delayed; this is not a live feed. Open WebUI renders the panel;
    provide brief interpretation and source links, not another table.
    Use get_stock_history for explicit historical dates, adjusted returns or event studies.
    """
    intervals = {
        "1d": "1m",
        "5d": "5m",
        "1mo": "30m",
        "3mo": "1d",
        "6mo": "1d",
        "1y": "1d",
        "5y": "1wk",
    }
    if compare_to is not None and (not isinstance(compare_to, list) or len(compare_to) > 4):
        raise ToolError("Supply at most four comparison ticker symbols, in a list.")
    symbols = normalize_tickers([ticker, *(compare_to or [])])
    if period not in intervals:
        raise ToolError("Choose a period: 1d, 5d, 1mo, 3mo, 6mo, 1y or 5y.")
    interval = intervals[period]
    series = [fetch_stock_chart(symbol, period, interval) for symbol in symbols]
    if len(series) == 1:
        return series[0]

    alignment = "time" if interval.endswith("m") else "date"
    shared = set(point[alignment] for point in series[0]["points"])
    for stock in series:
        keys = {point[alignment] for point in stock["points"]}
        if len(keys) != len(stock["points"]):
            raise ToolError(
                "Yahoo Finance returned duplicate comparison observations. Retry later."
            )
        shared.intersection_update(keys)
    if len(shared) < 2:
        raise ToolError(
            "These stocks have fewer than two shared price observations for this period. Choose another period or ticker set."
        )
    for stock in series:
        stock["points"] = [point for point in stock["points"] if point[alignment] in shared]
    return {
        "kind": "stock_comparison",
        "status": "ok",
        "ticker": symbols[0],
        "period": period,
        "interval": interval,
        "series": series,
        "price_basis": series[0]["price_basis"],
        "comparison": {
            "alignment": alignment,
            "observations": len(shared),
            "start": min(shared),
            "end": max(shared),
        },
        "interpretation": "Each stock's percentage change starts at its first shared close. Only common observations are shown; "
        "no filling or currency conversion. Latest snapshot metrics are not historical point-in-time values. "
        "Prices may be delayed; this is not a streaming live feed.",
    }


def fetch_stock_prices(ticker, start, end):
    stock = yf.Ticker(ticker)
    frame = stock.history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,
        actions=False,
        keepna=True,
        raise_errors=True,
        timeout=10,
    )
    if frame.empty:
        raise ValueError("No price observations")
    prices, invalid_rows, seen = [], 0, set()
    for timestamp, value in frame["Adj Close"].items():
        trading_date = timestamp.date()
        value = float(value)
        if not start <= trading_date <= end or not math.isfinite(value) or value <= 0:
            invalid_rows += 1
            continue
        if trading_date in seen:
            raise ValueError("Duplicate trading dates")
        seen.add(trading_date)
        prices.append({"date": trading_date.isoformat(), "adjusted_close": value})
    if not prices:
        raise ValueError("No valid adjusted prices")
    metadata = stock.history_metadata
    return {
        "currency": metadata.get("currency"),
        "timezone": metadata.get("exchangeTimezoneName"),
        "prices": sorted(prices, key=lambda p: p["date"]),
        "invalid_rows": invalid_rows,
    }


def get_stock_history(
    tickers: Annotated[
        list[Annotated[str, Field(strict=True, min_length=1, max_length=20)]],
        Field(min_length=1, max_length=15),
    ],
    start_date: Annotated[str, Field(strict=True, description="Inclusive start date, YYYY-MM-DD.")],
    end_date: Annotated[
        str, Field(strict=True, description="Inclusive end date, YYYY-MM-DD; before today.")
    ],
    include_prices: Annotated[bool, Field(strict=True)] = True,
) -> dict[str, Any]:
    """Fetch Yahoo daily adjusted closing prices and compare one or more stock tickers.

    Displays historical price data in MCP Apps clients. Multiple tickers appear
    together as distinct lines on one return-percentage chart, each starting at
    0% on the first shared trading date, with a price/return table underneath.
    Keep include_prices=True when a chart is requested; False provides summary data only.
    These are daily observations. For detailed current/recent intraday charts or
    comparisons and the latest valuation metrics, use get_stock_chart instead.
    Summarize the result briefly without another table unless the user requests one.

    Accepts Yahoo symbols, e.g. NVDA, AMD, TSM, ASML, and benchmark ETFs such as SPY;
    this tool is not limited to the graph basket. Use graph tools first to discover
    competitors or connected companies, then pass their tickers here.
    Dates are inclusive; only completed historical dates before today are accepted.
    Returns full per-ticker histories plus returns on the common trading dates,
    with a base-100 ending value. No forward filling or currency conversion.
    Adjusted Close accounts for splits and dividends; it is not an executable price.
    Comparison requires every requested ticker and at least two shared dates.
    For an event study, include a pre-event trading close as the baseline; returns
    alone do not establish causality or statistical significance.
    Maximum 15 symbols and 1,827 calendar days. With include_prices=True, dates
    times symbols must not exceed 5,000; use False for longer comparison summaries.
    """
    if not 1 <= len(tickers) <= 15:
        raise ToolError("Supply between 1 and 15 Yahoo ticker symbols.")
    symbols = normalize_tickers(tickers)
    start, end = historical_dates(start_date, end_date)
    days = (end - start).days + 1
    if include_prices and days * len(symbols) > 5000:
        raise ToolError(
            "Price output is limited to 5,000 calendar days times symbols; narrow the range or use include_prices=False."
        )

    series, errors, histories = [], {}, {}
    for index, ticker in enumerate(symbols):
        try:
            data = fetch_stock_prices(ticker, start, end)
        except YFRateLimitError:
            errors[ticker] = "Yahoo Finance rate limited this request. Try again later."
            errors.update(
                dict.fromkeys(
                    symbols[index + 1 :],
                    "Not requested because Yahoo Finance rate limited this call.",
                )
            )
            break
        except Exception:
            errors[ticker] = (
                "Yahoo Finance returned no usable data or the request failed. Check the symbol/date range or retry later."
            )
            continue
        prices = data.pop("prices")
        histories[ticker] = {row["date"]: row["adjusted_close"] for row in prices}
        series.append(
            {
                "ticker": ticker,
                **data,
                "source_url": f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/",
                "first_date": prices[0]["date"],
                "last_date": prices[-1]["date"],
                "observations": len(prices),
                **({"prices": prices} if include_prices else {}),
            }
        )

    comparison = {
        "status": "unavailable",
        "reason": "Every requested ticker needs usable data; no subset comparison was made.",
    }
    if not errors:
        common = sorted(set.intersection(*(set(prices) for prices in histories.values())))
        comparison = {
            "status": "unavailable",
            "reason": "Fewer than two shared trading dates; a return cannot be calculated.",
        }
        if len(common) >= 2:
            returns = []
            for ticker, prices in histories.items():
                first, last = prices[common[0]], prices[common[-1]]
                returns.append(
                    {
                        "ticker": ticker,
                        "start_adjusted_close": first,
                        "end_adjusted_close": last,
                        "return_pct": round((last / first - 1) * 100, 6),
                        "end_index_100": round(last / first * 100, 6),
                    }
                )
            comparison = {
                "status": "ok",
                "start_date": common[0],
                "end_date": common[-1],
                "observations": len(common),
                "returns": returns,
            }
    return {
        "source": "Yahoo Finance via yfinance",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "requested_start": start_date,
        "requested_end": end_date,
        "interval": "1d",
        "price_basis": "Yahoo Adjusted Close (splits and dividends), in each listing's currency.",
        "status": "partial" if errors and series else "unavailable" if errors else "ok",
        "series": series,
        "errors": errors,
        "comparison": comparison,
        "interpretation": "Returns use shared trading dates with no filling. No currency conversion; dates are exchange-local, "
        "so different exchanges may close at different times. Missing dates remain missing. "
        "Historical prices can be revised; this is not a point-in-time dataset. "
        "Price movements alone do not establish causation or statistical significance.",
    }
