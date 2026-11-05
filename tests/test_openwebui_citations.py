import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import picton_graph.integrations.openwebui_citations as citations


def evidence(text="An exact excerpt.", fragment="An%20exact%20excerpt."):
    document = "https://www.sec.gov/Archives/edgar/data/1/filing.htm"
    return {
        "id": "a" * 64,
        "evidence_text": text,
        "document": document,
        "citation_url": document + "#:~:text=" + fragment,
        "document_type": "10-K",
        "document_date": "2025-02-26",
    }


def tool_messages(records, call_id="call-1", name="picton_get_competitors"):
    return [
        {
            "role": "assistant",
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(
                {"competitors": [{"evidence": records}], "scope": "Historical disclosures."}
            ),
        },
    ]


class CitationFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sources = []
        self.request = SimpleNamespace(state=SimpleNamespace())
        self.metadata = {"chat_id": "chat", "message_id": "message"}
        self.filter = citations.Filter()
        self.saved = patch.object(
            citations, "saved_sources", AsyncMock(side_effect=lambda _: self.sources)
        ).start()
        self.addCleanup(patch.stopall)

        async def emit(event):
            self.sources.append(event["data"])

        self.emit = AsyncMock(side_effect=emit)

    async def run_filter(self, body):
        return await self.filter.request(body, self.request, self.metadata, self.emit)

    def body(self, records):
        return {
            "messages": [
                {"role": "system", "content": "Keep financial claims grounded."},
                {"role": "user", "content": "Who competes with NVIDIA?"},
                *tool_messages(records),
            ]
        }

    async def test_registers_quote_and_exact_url_before_returning_marker(self):
        proof = evidence()
        body = self.body([proof])
        before = deepcopy(body)
        result = await self.run_filter(body)
        record = json.loads(result["messages"][-1]["content"])["competitors"][0]["evidence"][0]
        self.assertEqual(record["citation"], "[1]")
        self.assertNotIn("document", record)
        self.assertNotIn("citation_url", record)
        self.assertEqual(record["evidence_text"], proof["evidence_text"])
        self.assertEqual(self.sources[0]["document"], [proof["evidence_text"]])
        self.assertEqual(self.sources[0]["metadata"][0]["url"], proof["citation_url"])
        self.assertTrue(self.sources[0]["metadata"][0]["external"])
        self.assertIn("2025-02-26", self.sources[0]["metadata"][0]["name"])
        self.assertIn(citations.INSTRUCTIONS, result["messages"][0]["content"])
        self.assertEqual(body, before)

    async def test_document_pages_register_native_excerpt_citations(self):
        proof = evidence("Reported EPS and guidance.", "Reported%20EPS%20and%20guidance.")
        messages = tool_messages([proof], name="picton_read_company_document")
        messages[-1]["content"] = json.dumps({"chunks": [proof], "next_offset": 5})
        body = {"messages": [{"role": "user", "content": "Read Intel's release."}, *messages]}
        before = deepcopy(body)
        result = await self.run_filter(body)
        page = json.loads(result["messages"][-1]["content"])
        self.assertEqual(page["chunks"][0]["citation"], "[1]")
        self.assertEqual(page["chunks"][0]["evidence_text"], proof["evidence_text"])
        self.assertEqual(page["next_offset"], 5)
        self.assertEqual(self.sources[0]["metadata"][0]["url"], proof["citation_url"])
        self.assertEqual(body, before)

    async def test_deduplicates_across_edges_and_rounds_but_keeps_different_excerpts(self):
        a, b = evidence(), evidence("Another excerpt.", "Another%20excerpt.")
        body = self.body([a, a])
        await self.run_filter(body)
        body["messages"] += tool_messages([a, b], "call-2", "picton_trace_supply_chain")
        result = await self.run_filter(body)
        records = json.loads(result["messages"][-1]["content"])["competitors"][0]["evidence"]
        self.assertEqual([e["citation"] for e in records], ["[1]", "[2]"])
        self.assertEqual(self.emit.await_count, 2)
        await self.run_filter(body)
        self.assertEqual(self.emit.await_count, 2)

    async def test_uses_existing_source_order_after_request_restart(self):
        self.sources.append(
            {
                "source": {"id": "upload"},
                "document": ["Uploaded document"],
                "metadata": [{"source": "upload"}],
            }
        )
        await self.run_filter(self.body([evidence()]))
        self.request = SimpleNamespace(state=SimpleNamespace())
        result = await self.run_filter(self.body([evidence()]))
        self.assertIn('"citation": "[2]"', result["messages"][-1]["content"])
        self.assertEqual(self.emit.await_count, 1)

    async def test_historical_results_are_sanitized_without_emitting_or_reusing_numbers(self):
        body = self.body([evidence()])
        body["messages"].append({"role": "user", "content": "What about Intel?"})
        result = await self.run_filter(body)
        historic = json.loads(result["messages"][-2]["content"])["competitors"][0]["evidence"][0]
        self.assertNotIn("citation", historic)
        self.assertNotIn("citation_url", historic)
        self.assertNotIn("document", historic)
        self.assertIn("refresh", historic["citation_status"])
        self.emit.assert_not_awaited()
        self.saved.assert_not_awaited()

    async def test_message_state_is_isolated(self):
        await self.run_filter(self.body([evidence()]))
        self.sources.clear()
        self.metadata = {"chat_id": "chat", "message_id": "new-message"}
        result = await self.run_filter(self.body([evidence("New", "New")]))
        self.assertIn('"citation": "[1]"', result["messages"][-1]["content"])

    async def test_text_blocks_are_supported_without_changing_non_text_blocks(self):
        body = self.body([evidence()])
        body["messages"][-1]["content"] = [
            {"type": "text", "text": body["messages"][-1]["content"]},
            {"type": "image_url", "image_url": {"url": "image"}},
        ]
        result = await self.run_filter(body)
        self.assertIn('"citation": "[1]"', result["messages"][-1]["content"][0]["text"])
        self.assertEqual(result["messages"][-1]["content"][1], body["messages"][-1]["content"][1])

    async def test_unrelated_tools_and_user_text_are_untouched(self):
        body = {
            "messages": [
                {"role": "user", "content": json.dumps(evidence())},
                *tool_messages([evidence()], name="another_server_get_competitors"),
            ]
        }
        self.assertIs(await self.run_filter(body), body)
        self.emit.assert_not_awaited()

    async def test_finance_tool_keeps_its_links_in_a_mixed_response(self):
        body = self.body([evidence()])
        body["messages"] += tool_messages([evidence()], "finance-call", "picton_get_stock_history")
        original = deepcopy(body["messages"][-1])
        result = await self.run_filter(body)
        self.assertEqual(result["messages"][-1], original)
        self.assertEqual(self.emit.await_count, 1)

    async def test_tool_errors_and_empty_results_remain_intact(self):
        body = self.body([])
        body["messages"][-1]["content"] = "Cannot read Neo4j."
        result = await self.run_filter(body)
        self.assertEqual(result["messages"][-1]["content"], "Cannot read Neo4j.")
        self.emit.assert_not_awaited()
        result = await self.run_filter(self.body([]))
        self.assertEqual(
            json.loads(result["messages"][-1]["content"])["competitors"][0]["evidence"], []
        )

    async def test_rejects_unsafe_urls_before_emitting_any_sources(self):
        for url in (
            "javascript:alert(1)",
            "https://evil.test/Archives/edgar/data/x#:~:text=x",
            "https://www.sec.gov@evil.test/Archives/edgar/data/x#:~:text=x",
            "https://user:secret@www.sec.gov/Archives/edgar/data/x#:~:text=x",
            "https://www.sec.gov:444/Archives/edgar/data/x#:~:text=x",
            "https://www.sec.gov/Archives/edgar/data/x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.run_filter(self.body([evidence(), {**evidence(), "citation_url": url}]))
        self.emit.assert_not_awaited()

    async def test_failed_source_delivery_does_not_return_unbacked_markers(self):
        self.emit.side_effect = RuntimeError("Cannot save source")
        with self.assertRaisesRegex(RuntimeError, "Cannot save source"):
            await self.run_filter(self.body([evidence()]))
        self.assertFalse(hasattr(self.request.state, "picton_citations"))

    async def test_without_emitter_keeps_standard_mcp_citation_urls(self):
        body = self.body([evidence()])
        self.assertIs(await self.filter.request(body, self.request, self.metadata), body)


if __name__ == "__main__":
    unittest.main()
