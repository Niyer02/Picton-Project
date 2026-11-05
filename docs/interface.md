# MCP tools and Open WebUI

[Project overview](../README.md)

All shell commands below run from the project root, after the editable install
described in the [setup guide](setup.md). Paths in prose are relative to that root.

## MCP: query the company graph and historical stock prices

`picton_graph/mcp/server.py` exposes nine read-only tools using the
[official Python MCP SDK](https://py.sdk.modelcontextprotocol.io/). It uses the
existing Neo4j Docker database and project `.env`; no LLM key is needed to run
the server. Install the pinned runtime with
`.venv/bin/python -m pip install -e .`.

| Tool | Example arguments | Result |
| --- | --- | --- |
| `get_company_basket` | `{}` | All ten company names and tickers, sorted by ticker. |
| `find_company_documents` | `{"ticker":"INTC","start_date":"2024-10-31","end_date":"2024-10-31","query":"earnings"}` | Cached filings and exhibits with their document IDs, types, dates and SEC URLs. |
| `read_company_document` | `{"document_id":"INTC/0000050863-24-000147/q324_earningsrelease.htm"}` | Original extracted document text, five chunks per page, with exact-excerpt citations. |
| `get_competitors` | `{"company":"NVDA"}` | Explicit `competes_with` relationships, read in either direction, with evidence. |
| `find_product_suppliers` | `{"product":"GPU"}` | Companies with `offers` edges to the same named Product. |
| `trace_supply_chain` | `{"company":"ASML","direction":"downstream","max_hops":2}` | Direct and indirect supply paths, with evidence for every edge. |
| `get_stock_history` | `{"tickers":["NVDA","AMD","SPY"],"start_date":"2024-06-03","end_date":"2024-06-14"}` | Yahoo adjusted daily closing prices and returns over shared trading dates. |
| `get_stock_chart` | `{"ticker":"NVDA","period":"5d"}` | A recent price chart with real intraday observations, volume and the latest market metrics. |
| `get_earnings` | `{"tickers":["NVDA","INTC"],"start_date":"2022-01-01","end_date":"2022-12-31"}` | Earnings announcement dates, EPS estimates, reported EPS, surprise percentages and beat/miss outcomes. |

Company lookup accepts a ticker or name, ignoring case and outer whitespace.
An exact match wins; a unique partial name also works. Unknown and ambiguous
companies produce a useful tool error rather than choosing a company silently.
Product lookup uses exact names, ignoring case and outer whitespace, with the
existing `GPUs` to `GPU` alias. An unmatched product returns up to ten name
suggestions to retry; those suggestions are not substitutes or confirmed peers.

Supply tracing accepts `upstream` or `downstream`, one or two hops, and `limit`
from 1 to 50 (default 20 paths, shortest first). Paths start at the queried company.
Their `relationship_ids` reference the response's `relationships` array, so common
edges and their evidence appear only once. Each edge retains its actual
`supplier` and `customer`, including when traversing upstream. Cycles are excluded.
Competitor and product results are capped at 20 rows, above the ten-company
basket size. The top-level `truncated` flag indicates omitted rows or paths.

All tools return structured JSON. For graph tools, `evidence_limit` is 1–5 (default 2) per edge:
newest document dates first, unknown dates last, with repeated document/quote
pairs omitted from the response only. `evidence_count` retains the stored
assertion count; `evidence_truncated` reports omitted evidence. A quote longer
than 3,000 characters is returned as an exact prefix with
`evidence_text_truncated: true`; its `citation_url` is built from the full quote
before truncation and points into the original SEC document.
Dates, source URLs and optional time/supplied-item qualifiers are retained.
Stored graph data is never changed by these tools.

The graph tools issue fixed, parameterized queries in read-mode transactions with a
10-second transaction timeout. There is no arbitrary-Cypher tool. Read mode and
MCP annotations describe the tools' behavior; they do not replace database
permissions. This local MVP reuses the existing connection credentials.

Run it through a stdio-capable MCP client; the client starts and stops the process.
For a client that accepts an `mcpServers` configuration, use the following and
adjust the absolute paths if the project moves:

```json
{
  "mcpServers": {
    "picton-company-graph": {
      "command": "/Users/niyer/Documents/ChatGPT/Picton Interview/.venv/bin/python",
      "args": ["-m", "picton_graph.mcp.server"]
    }
  }
}
```

Starting `.venv/bin/python -m picton_graph.mcp.server` directly uses stdio and waits for MCP
input on stdin. For an HTTP client, use `--transport streamable-http --host
127.0.0.1 --port 8000`; the endpoint is `/mcp`. Credentials stay in the existing
project `.env`, not in the client configuration. Containers can supply
`NEO4J_URI` and `NEO4J_AUTH` through environment variables; those take precedence
over the local defaults. The process works from any working directory.
Startup examples follow the SDK's
[stdio documentation](https://py.sdk.modelcontextprotocol.io/run/).

```sh
.venv/bin/python -m unittest -v tests.test_mcp_server
NEO4J_INTEGRATION=1 .venv/bin/python -m unittest -v tests.test_mcp_server
```

The second command tests the actual stdio server against the local database.
No LLM calls are part of these tests. Running the live tests writes tool results
to `data/mcp_demo/tools.json`. Earlier tool results and the separate OpenRouter
demonstration's request/tool/response trace and answer were removed during
cleanup; they are no longer available under `data/mcp_demo/`.

Interpret results as extracted historical disclosures within this basket.
Missing edges do not prove that a relationship does not exist. Shared product
names do not establish interchangeability or explicit competition. Supply paths
identify candidate exposure routes, not proof that the links were active
simultaneously, that a disruption would propagate, or that stock returns follow.
Document dates are filing/publication dates, not relationship validity intervals;
these tools do not implement a point-in-time backtest. Evidence excerpts are
untrusted source text, and the server instructions tell the LLM not to follow
instructions embedded in them.

### Direct SEC excerpt citations

Every graph evidence record returned by MCP includes a `citation_url` pointing
directly to its SEC filing or exhibit, with a browser text fragment:
`https://www.sec.gov/Archives/edgar/data/.../filing.htm#:~:text=encoded%20excerpt`.
Supporting browsers scroll to and highlight the matching text in the SEC
document; there is no custom evidence page.

Open WebUI uses `picton_graph/integrations/openwebui_citations.py`, a request filter that registers those
exact URLs and excerpts as native sources **before** the answer streams. The
model receives short `[1]` citation markers instead of SEC URLs. Citation numbers
are scoped to one answer, deduplicated by excerpt URL, and preserved across tool
rounds. The native citation preview links to the original highlighted filing;
sources persist with the chat. The standard MCP response remains unchanged for
other clients. Historical tool results are sanitized without reusing an earlier
answer's numbers; the model must retrieve fresh evidence for new citations.

The Docker image automatically installs and enables the `picton_citations`
filter and attaches it to the Murph model on first startup. Existing model and
filter records are preserved. For a separate non-Docker Open WebUI installation,
create a Filter Function with that ID under Admin Panel → Functions, paste
`picton_graph/integrations/openwebui_citations.py`, enable it and attach it to
the chat model's Filters. The model prompt in
`compose.yaml` uses the evidence's `citation` marker when present, with the
original `citation_url` as a fallback for clients without the filter. Persistent
model defaults must be updated in Admin settings when upgrading an existing
installation.

`deploy/Dockerfile.webui` adds one guarded compatibility patch to the pinned v0.13.0
image: the native preview's Content link preserves an existing text fragment
instead of appending a second one. The build checks the original asset hash and
fails if the upstream asset changes; review/remove this patch when upgrading.
Hard-refresh an already-open WebUI tab after rebuilding to refresh cached assets.

```sh
.venv/bin/python -m unittest -v tests.test_openwebui_citations
```

Short, single-line quotes use an exact text selector. Longer or multiline quotes
use the first and last source phrases as a range, allowing the highlight to span
HTML blocks without putting the entire quote in the URL. Whitespace is normalized
for matching; punctuation and names are preserved and URL-encoded, including
hyphens. Existing document anchors are retained. No graph evidence or downloaded
HTML is changed, and generating a citation requires no SEC request.

Highlighting is best effort: unsupported browsers, repeated phrases, or text
that differs in the rendered filing can prevent a match or select a different
occurrence. In that case the source document still opens. See the
[text-fragment specification](https://wicg.github.io/scroll-to-text-fragment/).

New citations work without the local HTTP service. Old saved chats keep their
`http://localhost:3081/evidence/<relationship_id>/<evidence_id>#excerpt` links;
that route now returns a redirect to the same SEC text-fragment URL. This legacy
redirect requires the MCP service and its read-only graph lookup. Missing evidence
returns 404; corrupt or unavailable evidence returns 503. Only HTTPS SEC archive
URLs are accepted. `EVIDENCE_BASE_URL` is no longer used. Yahoo price citations
continue to identify the market-data source.

### Yahoo Finance history and comparisons

`get_stock_history` uses pinned [yfinance](https://github.com/ranaroussi/yfinance)
without a Yahoo API key. It accepts Yahoo ticker symbols, including companies
outside the basket and benchmarks such as `SPY`. The model can first discover
competitors or customers using the graph, then pass those tickers to this tool.

`start_date` and `end_date` are **inclusive**, in `YYYY-MM-DD` format; the end
must be before today. The provider's exclusive end is translated internally.
Results include each ticker's daily **Adjusted Close**, currency, exchange
timezone, actual first/last dates, observation count, Yahoo history URL and a
UTC retrieval timestamp. Adjusted prices account for splits and dividends and
are not historical executable quotes. No API key or graph write is involved.

The comparison uses the intersection of trading dates across **every requested
ticker**, with at least two shared observations. Return is
`100 * (last_adjusted_close / first_adjusted_close - 1)`; `end_index_100`
normalizes each starting value to 100. Returned comparison dates are the actual
shared endpoints, which may differ from the requested calendar dates. Missing
prices are never filled. If a ticker fails, its error is explicit and available
histories are still returned, but a subset comparison is not silently produced.
Invalid price rows are counted and excluded; a rate limit stops remaining
downloads in that call instead of retrying repeatedly.

For this MVP, requests allow 1–15 symbols and at most 1,827 calendar days.
Daily output additionally allows at most 5,000 calendar days times unique
symbols. Set `include_prices: false` for longer comparison summaries or use a
narrower window for daily observations. These limits keep tool responses small.
The tool does not persist price history; fetching again can return revised data.

Comparisons remain in each listing's currency, without FX conversion. Different
exchanges can close at different times even on the same calendar date. For an
event study, include the pre-event trading close as the baseline and account for
release timing. Returns alone do not establish causality or significance.
Yahoo data and availability are not guaranteed; yfinance is an unofficial
research tool, and its project documents Yahoo data-use restrictions.

```sh
.venv/bin/python -m unittest -v tests.test_stock_history tests.test_stock_chart tests.test_mcp_server
docker compose up -d --build --no-deps graph-mcp
```

### Earnings results

`get_earnings` reads Yahoo's earnings calendar through yfinance, with the same
1–15 ticker and historical date bounds as `get_stock_history`. It fetches up to
100 entries per ticker, filters by inclusive announcement dates in the provider's
timezone, and returns source links and available calendar bounds. These bounds
do not guarantee complete coverage. The tool returns structured EPS results;
it does not download full earnings-release documents or display a price chart.

Missing actual EPS is `not_reported`; missing consensus is `unknown`. Zero and
negative EPS remain valid. Outcomes use Yahoo's surprise percentage when present,
otherwise the displayed actual-versus-estimate comparison. Yahoo may round EPS
or adjust historical values; the calendar does not identify currency or GAAP
methodology and is not frozen point-in-time consensus. Failed tickers are explicit,
successful results are retained, and rate limits stop further requests.

### Source documents

The document tools read `data/filings.duckdb`, mounted read-only into the MCP
container. `find_company_documents` searches a ticker and inclusive filing-date
range; its optional query is a literal phrase in the original section text.
It returns at most 20 matches and an explicit `has_more` flag. The coverage dates
describe the cached archive, not a guarantee that every filing was downloaded.

`read_company_document` returns five original, unmasked chunks per call, in source
order. Follow `next_offset` until null for the entire extracted document. Each
chunk carries an SEC excerpt citation, rendered through the same native citation
filter as graph evidence. Tables remain extracted text; images and original HTML
are unavailable, and extraction warnings are returned. These tools do not fetch
new filings from the web. Filing dates may differ from announcement dates.

For example, Intel's October 31, 2024 earnings release is the `EX-99.1` document
`INTC/0000050863-24-000147/q324_earningsrelease.htm`, separate from its primary
8-K. Search for `earnings` on that filing date, then read the returned document ID.

## Open WebUI interface

Open **http://localhost:3080**. The Compose setup runs Open WebUI v0.13.0-slim,
the graph MCP server and the existing Neo4j database. GPT-6-luna is selected
through OpenRouter, with the nine Picton graph, document and market-data tools enabled by default.
Native tool calling lets the model choose and execute the relevant tool, then
answer with exact-excerpt citation links and document dates for graph evidence.

Stock results render an inline **finance panel** using Open WebUI's native MCP
Apps support. The company header, price and period change sit above a large
line chart with volume, followed by a compact market-metrics grid. Gains are
green and losses red. Every chart vertex is a real observation: there is no
smoothing, interpolation of missing prices or artificial detail.

`get_stock_chart` defaults to the recent five-day view. One-day views use
one-minute bars, five-day views use five-minute bars, one-month views use 30-minute bars, three-month,
six-month and one-year views use daily bars, and five-year views use weekly bars.
The current bar may still be forming. The header uses the last chart observation,
and its change is measured from the first displayed observation. The chart uses
Yahoo Close with dividend adjustment disabled, while `get_stock_history` retains
its adjusted daily-close contract for historical comparisons and event studies.

P/E, market cap, open, day range, dividend yield, 52-week range, EPS and volume
are the latest available snapshot, with its timestamp shown separately. They
are not historical valuations for the selected chart period. Missing metrics
remain unavailable. Yahoo can delay or revise observations; this is not a
streaming market-data terminal.

The self-contained `picton_graph/mcp/assets/stock_chart.html` resource uses SVG
and plain JavaScript, without a chart dependency or separate frontend build.
The local Open WebUI image has only the small citation compatibility patch
described above.
Changing the recent-period dropdown requests fresh data through the MCP host;
Open WebUI displays its standard tool-call confirmation. Cancellation or a
provider failure preserves the existing chart. Explicit historical requests
remain daily-resolution and do not invent intraday volume or current metrics.
Recent multi-company comparisons use `get_stock_chart` with the first `ticker`
and the others in `compare_to`, for example `{"ticker":"NVDA","compare_to":["AMD"],"period":"1mo"}`.
They use the same interval as the single-company view: one-minute bars for
one day, five-minute bars for five days, and 30-minute bars for one month.
All companies appear on one chart, rebased to 0% at their first shared
observation. Matching legend colours identify each line, and hovering shows
all returns at the same time. Intraday bars align by UTC timestamp; daily and
weekly bars align by trading date. Every requested company must have usable
data, with at least two common observations. Recent comparisons support up to
five companies and retain every company when the period selector changes.
Explicit historical comparisons still use daily adjusted closes.
The table below retains each company's starting price, ending price and return;
there is no company dropdown hiding the other series.
Clients without MCP Apps still receive structured JSON.

Try these prompts:

- Who are NVIDIA's disclosed competitors? Cite the filings.
- Which companies in our graph offer GPUs?
- Trace ASML's downstream supply connections within two steps. Separate direct
  customers from indirect connections and show the evidence.
- Find NVIDIA's competitors, then compare their stock returns with NVIDIA and
  SPY from 2024-06-03 through 2024-06-14 using Yahoo adjusted prices.
- Chart NVDA, AMD and SPY from 2024-06-03 through 2024-06-14 and show their returns.
- Show NVIDIA's stock chart for the past five days.

See the [setup guide](setup.md) for startup, credentials, local hosting, and storage.

Open WebUI settings are seeded from
Compose on first start and subsequently persist, so later model/tool changes
should be made under Settings → Admin rather than assuming an environment
change will override a saved setting.

The graph service is reached internally at `http://graph-mcp:8000/mcp` using
Open WebUI's native Streamable HTTP MCP connection. Its HTTP service is also
bound to localhost:3081 for MCP and legacy citation redirects, using only the Neo4j credentials.
Only Open WebUI receives the OpenRouter
key. `deploy/Dockerfile.mcp` installs the shared pinned `requirements/mcp.txt`; the build
context excludes filings, graph data and `.env`. Local stdio clients continue to
use `python -m picton_graph.mcp.server` after the editable package install.

The finance app is registered on `get_stock_chart` and `get_stock_history` through
`_meta.ui.resourceUri = ui://picton/stock-chart.html` and served as
`text/html;profile=mcp-app`. Open WebUI passes the tool result into a sandboxed
iframe. The resource allows no external connections or assets; source links use
the host's link-opening interface. Existing text-only answers keep their saved
format; new stock-history tool calls display the panel.

References: [Open WebUI installation](https://docs.openwebui.com/getting-started/quick-start/),
[native MCP](https://docs.openwebui.com/features/extensibility/mcp/),
[configuration persistence](https://docs.openwebui.com/reference/env-configuration/).
