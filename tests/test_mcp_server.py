import hashlib
import json
import os
import sys
import unittest
from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError
from neo4j.exceptions import ServiceUnavailable
from starlette.testclient import TestClient

import picton_graph.graph.connection as connection
from picton_graph.graph.connection import connect
from picton_graph.mcp import evidence, queries, server


def company(ticker, name=None):
    return {"iri": "urn:test:" + ticker, "ticker": ticker, "name": name or ticker}


def proof(identifier="a", quote="Source quotation.", document_date="2025-01-01", document="filing"):
    return {
        "id": identifier * 64,
        "evidence_text": quote,
        "document": "https://www.sec.gov/Archives/edgar/data/1045810/000104581025000023/"
        + document,
        "document_type": "10-K",
        "document_date": document_date,
    }


def properties(records=None, identifier="r"):
    records = records if records is not None else [proof()]
    return {
        "relationship_id": identifier * 64,
        "evidence_count": len(records),
        "evidence": json.dumps(records),
    }


class EvidenceTests(unittest.TestCase):
    def test_newest_distinct_excerpts_preserve_dates_and_qualifiers(self):
        newest = proof("b", document_date="2026-01-01")
        newest.update(time_text="fiscal 2025", supplied_product=["urn:test:gpu"])
        records = [
            proof("a", document_date=None, document="unknown-date"),
            proof("c", document_date="2025-01-01"),
            newest,
            proof("d", quote="A second quotation.", document="second"),
        ]
        edge = properties(records)
        before = deepcopy(edge)
        result = evidence.edge_evidence(edge, 2)
        self.assertEqual(
            [{k: v for k, v in item.items() if k != "citation_url"} for item in result["evidence"]],
            [newest, records[3]],
        )
        self.assertEqual(result["evidence_count"], 4)
        self.assertTrue(result["evidence_truncated"])
        self.assertEqual(edge, before)
        self.assertIsNone(evidence.edge_evidence(edge, 5)["evidence"][-1]["document_date"])

    def test_long_quote_is_a_marked_exact_prefix(self):
        original = proof(quote="A sentence. " * 400 + "The final source words are retained.")
        result = evidence.edge_evidence(properties([original]), 2)
        (excerpt,) = result["evidence"]
        self.assertEqual(excerpt["evidence_text"], original["evidence_text"][:3000])
        self.assertTrue(excerpt["evidence_text_truncated"])
        self.assertFalse(result["evidence_truncated"])
        self.assertEqual(
            original["evidence_text"], "A sentence. " * 400 + "The final source words are retained."
        )
        selector = unquote(urlsplit(excerpt["citation_url"]).fragment)
        self.assertTrue(selector.endswith("The final source words are retained."))
        self.assertNotIn("The final source words", excerpt["evidence_text"])

    def test_citation_urls_point_directly_to_documents_and_keep_evidence_identity(self):
        edge = properties([proof("a"), proof("b", quote="Another excerpt.")], identifier="e")
        before = deepcopy(edge)
        with patch.dict(os.environ, {"EVIDENCE_BASE_URL": "https://obsolete.example.test/"}):
            result = evidence.edge_evidence(edge, 5)
        for item in result["evidence"]:
            self.assertEqual(
                item["citation_url"],
                item["document"] + "#:~:text=" + item["evidence_text"].replace(" ", "%20"),
            )
        self.assertEqual({item["id"] for item in result["evidence"]}, {"a" * 64, "b" * 64})
        self.assertEqual(edge, before)

    def test_text_selector_preserves_punctuation_and_encodes_reserved_characters(self):
        record = proof(quote="\n  NVIDIA’s\tAI-optimized GPUs, CPUs & software (100%).\u00a0\n")
        self.assertEqual(
            evidence.citation_url(record),
            record["document"]
            + "#:~:text=NVIDIA%E2%80%99s%20AI%2Doptimized%20GPUs%2C%20CPUs%20%26%20software%20%28100%25%29.",
        )

    def test_multiblock_excerpt_uses_start_and_end_ranges(self):
        record = proof(quote="Our named supplier is TSMC.\n\nIt manufactures semiconductor wafers.")
        self.assertEqual(
            unquote(urlsplit(evidence.citation_url(record)).fragment),
            ":~:text=Our named supplier is TSMC.,It manufactures semiconductor wafers.",
        )
        record["evidence_text"] = (
            "one two three four five six seven eight nine ten eleven twelve thirteen\n"
            "fourteen fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix"
        )
        self.assertEqual(
            unquote(urlsplit(evidence.citation_url(record)).fragment),
            ":~:text=one two three four five six seven eight nine ten eleven twelve,"
            "fifteen sixteen seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentyfour twentyfive twentysix",
        )

    def test_element_anchor_is_preserved_and_existing_text_directive_replaced(self):
        for host in ("sec.gov", "www.sec.gov:443"):
            record = proof(quote="Source quotation.")
            record["document"] = (
                f"https://{host}/Archives/edgar/data/1045810/filing.htm?output=1#business:~:text=old"
            )
            self.assertEqual(
                evidence.citation_url(record),
                f"https://{host}/Archives/edgar/data/1045810/filing.htm?output=1#business:~:text=Source%20quotation.",
            )

    def test_unsafe_or_non_sec_source_urls_are_rejected(self):
        for document in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "http://www.sec.gov/Archives/edgar/data/1/filing.htm",
            "https://www.sec.gov.evil.test/Archives/edgar/data/1/filing.htm",
            "https://user:secret@www.sec.gov/Archives/edgar/data/1/filing.htm",
            "https://www.sec.gov:444/Archives/edgar/data/1/filing.htm",
            "https://www.sec.gov/news",
            "/Archives/edgar/data/1/filing.htm",
            "https://www.sec.gov/Archives/edgar/data/1/\nfiling.htm",
            "https://[invalid/Archives/edgar/data/1/filing.htm",
        ):
            with self.subTest(document=document), self.assertRaises(ToolError):
                evidence.edge_evidence(properties([{**proof(), "document": document}]), 1)

    def test_corrupt_evidence_is_an_actionable_error(self):
        invalid = [
            properties([]),
            {**properties(), "evidence": "{"},
            {**properties(), "evidence_count": True},
            {**properties(), "evidence_count": 2},
            {**properties(), "evidence": "{}"},
            {**properties(), "evidence": None},
            properties([None]),
            properties([proof(document_date="2025-02-30")]),
            properties([proof(document_date="20250101")]),
            properties([proof(document_date="2025-W01-1")]),
            properties([proof(document_date=12)]),
            properties([proof(quote=" ")]),
        ]
        for key in ("id", "evidence_text", "document", "document_type", "document_date"):
            invalid.append(properties([{k: v for k, v in proof().items() if k != key}]))
        for edge in invalid:
            with self.subTest(edge=edge), self.assertRaisesRegex(ToolError, "invalid evidence"):
                evidence.edge_evidence(edge, 2)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tx = Mock()
        self.read = patch.object(queries, "read_transaction").start()
        self.addCleanup(patch.stopall)
        self.read.return_value.__enter__.return_value = self.tx

    async def request(self, name, arguments):
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments)

    async def call(self, name, arguments):
        result = await self.request(name, arguments)
        self.assertFalse(result.is_error, result.content)
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    async def test_nine_read_only_tools_with_bounded_schemas(self):
        async with Client(server.mcp) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        expected = {
            "find_company_documents": {"ticker", "start_date", "end_date", "query"},
            "read_company_document": {"document_id", "offset"},
            "get_company_basket": set(),
            "get_competitors": {"company", "evidence_limit"},
            "find_product_suppliers": {"product", "evidence_limit"},
            "trace_supply_chain": {"company", "direction", "max_hops", "limit", "evidence_limit"},
            "get_stock_history": {"tickers", "start_date", "end_date", "include_prices"},
            "get_stock_chart": {"ticker", "period", "compare_to"},
            "get_earnings": {"tickers", "start_date", "end_date"},
        }
        self.assertEqual(set(tools), set(expected))
        for name, arguments in expected.items():
            tool = tools[name]
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertEqual(
                tool.annotations.open_world_hint,
                name in ("get_stock_history", "get_stock_chart", "get_earnings"),
            )
            self.assertEqual(set(tool.input_schema["properties"]), arguments)
            self.assertIsNotNone(tool.output_schema)
            self.assertGreater(len(tool.description), 50)
            if name in ("find_company_documents", "read_company_document"):
                self.assertNotIn("ui", tool.meta or {})
            if "evidence_limit" in arguments:
                evidence = tool.input_schema["properties"]["evidence_limit"]
                self.assertEqual((evidence["minimum"], evidence["maximum"]), (1, 5))
        hops = tools["trace_supply_chain"].input_schema["properties"]["max_hops"]
        self.assertEqual((hops["minimum"], hops["maximum"]), (1, 2))
        for name in ("get_stock_history", "get_earnings"):
            tickers = tools[name].input_schema["properties"]["tickers"]
            self.assertEqual((tickers["minItems"], tickers["maxItems"]), (1, 15))
        self.assertEqual(
            tools["get_stock_chart"].input_schema["properties"]["period"]["enum"],
            ["1d", "5d", "1mo", "3mo", "6mo", "1y", "5y"],
        )
        comparison = tools["get_stock_chart"].input_schema["properties"]["compare_to"]
        self.assertEqual(
            next(option for option in comparison["anyOf"] if option["type"] == "array")["maxItems"],
            4,
        )
        self.tx.run.assert_not_called()

    async def test_company_basket_returns_only_names_and_tickers(self):
        companies = [
            {"name": "Advanced Micro Devices", "ticker": "AMD"},
            {"name": "NVIDIA", "ticker": "NVDA"},
        ]
        self.tx.run.return_value = companies
        self.assertEqual(await self.call("get_company_basket", {}), {"companies": companies})
        self.tx.run.assert_called_once()

    async def test_invalid_arguments_fail_before_any_query(self):
        calls = [("get_competitors", {"company": value}) for value in ("", "   ", 5, "x" * 161)]
        calls += [
            ("get_competitors", {"company": "NVDA", "evidence_limit": value})
            for value in (0, 6, True, "2")
        ]
        calls += [
            ("trace_supply_chain", {"company": "NVDA", key: value})
            for key, value in (
                ("max_hops", 0),
                ("max_hops", 3),
                ("max_hops", True),
                ("direction", "both"),
                ("limit", 0),
                ("limit", 51),
            )
        ]
        calls.append(("find_product_suppliers", {"product": "   "}))
        for name, arguments in calls:
            with self.subTest(name=name, arguments=arguments):
                result = await self.request(name, arguments)
                self.assertTrue(result.is_error)
                self.tx.run.assert_not_called()

    async def test_competitor_lookup_prefers_exact_ticker_and_reads_both_directions(self):
        focus = company("AMD", "Advanced Micro Devices")
        competitor = {"ticker": "NVDA", "name": "NVIDIA"}
        self.tx.run.side_effect = [
            [{"company": focus}, {"company": company("FAKE", "AMD supplier")}],
            [{"company": competitor, "relationship": properties()}],
        ]
        result = await self.call("get_competitors", {"company": " aMd "})
        self.assertEqual(result["company"]["ticker"], "AMD")
        self.assertEqual(result["competitors"][0]["company"], competitor)
        (excerpt,) = result["competitors"][0]["evidence"]
        self.assertEqual({k: v for k, v in excerpt.items() if k != "citation_url"}, proof())
        self.assertEqual(
            excerpt["citation_url"], proof()["document"] + "#:~:text=Source%20quotation."
        )
        self.assertEqual(self.tx.run.call_args_list[0].kwargs, {"name": "amd"})
        self.assertIn("-[r:competes_with]-", self.tx.run.call_args.args[0])
        self.assertFalse(result["truncated"])

    async def test_unknown_and_ambiguous_companies_are_not_guessed(self):
        self.tx.run.side_effect = [[], [{"ticker": "AMD"}, {"ticker": "NVDA"}]]
        result = await self.request("get_competitors", {"company": "missing"})
        self.assertTrue(result.is_error)
        self.assertIn("Available tickers: AMD, NVDA", result.content[0].text)
        self.tx.run.side_effect = [
            [{"company": company("AAA", "Advanced A")}, {"company": company("BBB", "Advanced B")}]
        ]
        result = await self.request("get_competitors", {"company": "Advanced"})
        self.assertTrue(result.is_error)
        self.assertIn("Ambiguous company", result.content[0].text)

    async def test_company_and_product_search_text_is_parameterized(self):
        text = "x'}) MATCH (n) DETACH DELETE n //"
        self.tx.run.side_effect = [[], []]
        self.assertTrue((await self.request("get_competitors", {"company": text})).is_error)
        self.tx.run.side_effect = [[], []]
        result = await self.call("find_product_suppliers", {"product": text})
        self.assertEqual(result["match"], "none")
        for call in self.tx.run.call_args_list:
            self.assertNotIn(text, call.args[0])
            self.assertNotIn("DELETE", call.args[0])
            if "name" in call.kwargs:
                self.assertEqual(call.kwargs["name"], text.lower())

    async def test_product_alias_and_no_match_suggestions_are_explicit(self):
        self.tx.run.side_effect = [
            [
                {
                    "company": {"name": "NVIDIA", "ticker": "NVDA"},
                    "product": {"name": "GPU", "iri": "urn:test:gpu"},
                    "relationship": properties(),
                }
            ]
        ]
        result = await self.call("find_product_suppliers", {"product": " GPUs "})
        self.assertEqual(result["match"], "exact")
        self.assertEqual(result["suppliers"][0]["product"]["name"], "GPU")
        self.assertEqual(self.tx.run.call_args.kwargs["name"], "gpu")
        self.tx.run.side_effect = [[], [{"name": "AI Accelerators"}]]
        result = await self.call("find_product_suppliers", {"product": "AI"})
        self.assertEqual(result["match"], "none")
        self.assertEqual(result["suppliers"], [])
        self.assertEqual(result["suggestions"], ["AI Accelerators"])

    async def test_upstream_paths_keep_supplier_direction_and_deduplicate_edges(self):
        companies = [
            {"name": name, "ticker": ticker}
            for ticker, name in (("NVDA", "NVIDIA"), ("TSM", "TSMC"), ("ASML", "ASML"))
        ]
        first = {"supplier": "TSM", "customer": "NVDA", "properties": properties(identifier="a")}
        second = {"supplier": "ASML", "customer": "TSM", "properties": properties(identifier="b")}
        rows = [
            {"companies": companies[:2], "links": [first]},
            {"companies": companies, "links": [first, second]},
        ]
        self.tx.run.side_effect = [[{"company": company("NVDA", "NVIDIA")}], rows]
        result = await self.call("trace_supply_chain", {"company": "NVDA", "direction": "upstream"})
        self.assertEqual([p["connection"] for p in result["paths"]], ["direct", "indirect"])
        self.assertEqual(result["paths"][1]["companies"], companies)
        self.assertEqual(len(result["relationships"]), 2)
        self.assertEqual(
            [(r["supplier"], r["customer"]) for r in result["relationships"]],
            [("TSM", "NVDA"), ("ASML", "TSM")],
        )
        self.assertIn("<-[:supplies_to*1..2]-", self.tx.run.call_args.args[0])
        self.assertEqual(self.tx.run.call_args.kwargs["max_hops"], 2)
        self.tx.run.side_effect = [[{"company": company("NVDA", "NVIDIA")}], rows]
        capped = await self.call(
            "trace_supply_chain", {"company": "NVDA", "direction": "upstream", "limit": 1}
        )
        self.assertTrue(capped["truncated"])
        self.assertEqual(len(capped["paths"]), 1)
        self.assertEqual(len(capped["relationships"]), 1)
        self.assertEqual(self.tx.run.call_args.kwargs["limit"], 2)


class EvidenceRedirectTests(unittest.TestCase):
    def setUp(self):
        self.tx = Mock()
        self.read = patch.object(queries, "read_transaction").start()
        self.addCleanup(patch.stopall)
        self.read.return_value.__enter__.return_value = self.tx
        self.client = TestClient(
            server.mcp.streamable_http_app(stateless_http=True), follow_redirects=False
        )
        self.addCleanup(self.client.close)
        self.relationship_id = "e" * 64
        self.evidence_id = "a" * 64
        self.path = f"/evidence/{self.relationship_id}/{self.evidence_id}"

    def row(self, records=None):
        return {"properties": properties(records, identifier="e")}

    def test_old_link_selects_exact_evidence_and_redirects_to_its_source(self):
        selected = proof(
            "a",
            quote="\n  NVIDIA’s chips → AMD & partners.\n\n"
            + "Exact source words & punctuation. " * 150
            + "\u00a0end  \n",
            document="nvda-20250126.htm",
        )
        self.tx.run.return_value = [self.row([proof("b", quote="A different excerpt."), selected])]
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], evidence.citation_url(selected))
        self.assertIn("NVIDIA’s chips → AMD & partners.", unquote(response.headers["location"]))
        self.assertTrue(unquote(response.headers["location"]).endswith("end"))
        self.assertNotIn("A different excerpt.", response.text)
        self.assertEqual(response.text, "")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(self.tx.run.call_args.kwargs, {"relationship_id": self.relationship_id})
        query = self.tx.run.call_args.args[0]
        self.assertIn("$relationship_id", query)
        self.assertNotIn(self.relationship_id, query)
        for mutation in ("CREATE ", "MERGE ", "DELETE ", "SET "):
            self.assertNotIn(mutation, query.upper())

    def test_quote_markup_is_encoded_in_the_link_and_never_rendered(self):
        attack = '<script>alert("quoted text")</script> & <img src=x onerror=alert(1)>'
        self.tx.run.return_value = [self.row([proof(quote=attack)])]
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 307)
        self.assertIn("%3Cscript%3E", response.headers["location"])
        self.assertNotIn("<script>", response.headers["location"])
        self.assertEqual(response.text, "")

    def test_unsafe_source_links_are_unavailable_without_redirect_or_secret_leaks(self):
        for document in (
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "https://user:secret@www.sec.gov/Archives/edgar/data/1/filing.htm",
        ):
            row = self.row([{**proof(), "document": document}])
            self.tx.run.return_value = [row]
            with self.subTest(document=document):
                response = self.client.get(self.path)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("location", response.headers)
                self.assertNotIn("secret", response.text)
                self.assertNotIn("<script>", response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                policy = response.headers["content-security-policy"]
                self.assertIn("default-src 'none'", policy)
                self.assertIn("frame-ancestors 'none'", policy)

    def test_unknown_relationship_or_wrong_evidence_id_is_not_found(self):
        for rows in ([], [self.row([proof("b")])]):
            self.tx.run.return_value = rows
            with self.subTest(rows=rows):
                response = self.client.get(self.path)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn("Source quotation.", response.text)

    def test_malformed_identifiers_are_rejected_before_database_access(self):
        for relationship_id, evidence_id in (
            ("invalid", self.evidence_id),
            (self.relationship_id, "a" * 63),
            ("E" * 64, self.evidence_id),
            (self.relationship_id, "g" * 64),
        ):
            with self.subTest(relationship_id=relationship_id, evidence_id=evidence_id):
                response = self.client.get(f"/evidence/{relationship_id}/{evidence_id}")
                self.assertEqual(response.status_code, 404)
        self.read.assert_not_called()

    def test_corrupt_or_ambiguous_evidence_fails_without_showing_an_excerpt(self):
        corrupt = self.row()
        corrupt["properties"]["evidence"] = "{"
        for rows in ([corrupt], [self.row(), self.row()], [self.row([proof(), proof()])]):
            self.tx.run.return_value = rows
            with self.subTest(rows=rows):
                response = self.client.get(self.path)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("Source quotation.", response.text)

    def test_database_failure_is_a_sanitized_unavailable_response(self):
        self.read.return_value.__enter__.side_effect = ToolError("secret-password in driver error")
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-password", response.text)


class ConnectionTests(unittest.TestCase):
    def test_database_failure_is_sanitized(self):
        with patch.object(queries, "connect", side_effect=ServiceUnavailable("secret-password")):
            with self.assertRaisesRegex(ToolError, "Cannot read Neo4j") as caught:
                with queries.read_transaction():
                    self.fail("Unavailable database yielded a transaction")
        self.assertNotIn("secret-password", str(caught.exception))

    def test_environment_connection_takes_precedence_with_or_without_env_file(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(connection, "ROOT", Path(directory)),
            patch.dict(
                os.environ,
                {
                    "NEO4J_AUTH": "container-user/container/password",
                    "NEO4J_URI": "bolt://neo4j:7687",
                },
            ),
            patch.object(connection.GraphDatabase, "driver") as driver,
        ):
            for has_file in (False, True):
                with self.subTest(has_file=has_file):
                    if has_file:
                        (Path(directory) / ".env").write_text(
                            "NEO4J_AUTH=file-user/file-password\n"
                        )
                    self.assertIs(connect(), driver.return_value)
                    driver.assert_called_with(
                        "bolt://neo4j:7687", auth=("container-user", "container/password")
                    )

    def test_legacy_file_auth_and_local_uri_remain_the_fallback(self):
        with (
            TemporaryDirectory() as directory,
            patch.object(connection, "ROOT", Path(directory)),
            patch.dict(os.environ),
            patch.object(connection.GraphDatabase, "driver") as driver,
        ):
            os.environ.pop("NEO4J_AUTH", None)
            os.environ.pop("NEO4J_URI", None)
            (Path(directory) / ".env").write_text(
                "OTHER=value\nNEO4J_AUTH=file-user/file/password\n"
            )
            self.assertIs(connect(), driver.return_value)
            driver.assert_called_once_with(
                "bolt://localhost:7687", auth=("file-user", "file/password")
            )


class TransportTests(unittest.TestCase):
    def test_default_transport_remains_stdio(self):
        with (
            patch.object(sys, "argv", ["picton_graph.mcp.server"]),
            patch.object(server.mcp, "run") as run,
        ):
            server.main()
        run.assert_called_once_with(transport="stdio")

    def test_http_transport_passes_host_port_and_stateless_setting(self):
        arguments = [
            "picton_graph.mcp.server",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
        ]
        with patch.object(sys, "argv", arguments), patch.object(server.mcp, "run") as run:
            server.main()
        run.assert_called_once_with(
            transport="streamable-http", host="0.0.0.0", port=8765, stateless_http=True
        )

    def test_unsupported_transport_is_rejected_before_server_start(self):
        with (
            patch.object(sys, "argv", ["picton_graph.mcp.server", "--transport", "invalid"]),
            patch.object(server.mcp, "run") as run,
            redirect_stderr(StringIO()),
        ):
            with self.assertRaises(SystemExit) as caught:
                server.main()
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()


@unittest.skipUnless(
    os.environ.get("NEO4J_INTEGRATION") == "1", "Set NEO4J_INTEGRATION=1 for live MCP checks."
)
class LiveMCPTests(unittest.IsolatedAsyncioTestCase):
    def graph_digest(self):
        digest = hashlib.sha256()
        with (
            connect() as driver,
            driver.session(database="neo4j", default_access_mode="READ") as session,
        ):
            for query in (
                "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties ORDER BY id",
                "MATCH (a)-[r]->(b) RETURN elementId(r) AS id, elementId(a) AS source, elementId(b) AS target, type(r) AS type, properties(r) AS properties ORDER BY id",
            ):
                for row in session.run(query):
                    digest.update(
                        json.dumps(dict(row), sort_keys=True, ensure_ascii=False).encode()
                    )
        return digest.hexdigest()

    async def test_real_stdio_tools_return_dated_paths_without_graph_changes(self):
        before = self.graph_digest()
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "picton_graph.mcp.server"]
        )
        async with Client(parameters, read_timeout_seconds=30) as client:
            self.assertEqual(
                {t.name for t in (await client.list_tools()).tools},
                {
                    "find_company_documents",
                    "read_company_document",
                    "get_company_basket",
                    "get_competitors",
                    "find_product_suppliers",
                    "trace_supply_chain",
                    "get_stock_history",
                    "get_stock_chart",
                    "get_earnings",
                },
            )

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                self.assertFalse(result.is_error, result.content)
                return result.structured_content

            competitors = await call("get_competitors", {"company": "NVDA"})
            self.assertIn("AMD", {r["company"]["ticker"] for r in competitors["competitors"]})
            suppliers = await call("find_product_suppliers", {"product": "GPU"})
            self.assertEqual(
                {r["company"]["ticker"] for r in suppliers["suppliers"]}, {"AMD", "INTC", "NVDA"}
            )
            downstream = await call("trace_supply_chain", {"company": "ASML"})
            self.assertIn(
                ["ASML", "TSM", "NVDA"],
                [[c["ticker"] for c in p["companies"]] for p in downstream["paths"]],
            )
            upstream = await call(
                "trace_supply_chain", {"company": "NVDA", "direction": "upstream"}
            )
            self.assertIn(
                ["NVDA", "TSM", "ASML"],
                [[c["ticker"] for c in p["companies"]] for p in upstream["paths"]],
            )
            for result in (downstream, upstream):
                indexed = {r["relationship_id"]: r for r in result["relationships"]}
                for path in result["paths"]:
                    self.assertEqual(path["hops"], len(path["companies"]) - 1)
                    self.assertEqual(path["hops"], len(path["relationship_ids"]))
                    for identifier in path["relationship_ids"]:
                        self.assertIn(identifier, indexed)
                for edge in indexed.values():
                    for evidence in edge["evidence"]:
                        self.assertTrue(evidence["document"].startswith("https://www.sec.gov/"))
                        self.assertIsNotNone(evidence["document_date"])
            direct = await call("trace_supply_chain", {"company": "ASML", "max_hops": 1})
            self.assertTrue(all(path["hops"] == 1 for path in direct["paths"]))
            capped = await call("trace_supply_chain", {"company": "ASML", "limit": 1})
            self.assertEqual(len(capped["paths"]), 1)
            self.assertTrue(capped["truncated"])
        self.assertEqual(self.graph_digest(), before)


if __name__ == "__main__":
    unittest.main()
