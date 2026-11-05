import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb
from mcp import Client

from picton_graph.ingestion.storage import SCHEMA
from picton_graph.mcp import documents, server


class DocumentToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "filings.duckdb"
        self.release = "INTC/accession/earnings.htm"
        self.empty = "INTC/accession/image.htm"
        self.texts = [f"Intel earnings paragraph {i}: 100% original text." for i in range(7)]
        with duckdb.connect(str(self.database)) as db:
            db.execute(SCHEMA)
            db.execute("ALTER TABLE chunks ADD COLUMN masked_text VARCHAR")
            self.add_document(db, self.release, "INTC", "2024-10-31", self.texts)
            self.add_document(db, self.empty, "INTC", "2024-10-30", [])
            self.add_document(db, "INTC/annual.htm", "INTC", "2025-01-31", ["Annual earnings."])
            self.add_document(db, "AMD/earnings.htm", "AMD", "2024-10-31", ["AMD earnings."])
        self.addCleanup(patch.stopall)
        patch.object(documents, "DATABASE", self.database).start()

    def add_document(self, db, identifier, ticker, filing_date, texts):
        db.execute(
            "INSERT INTO documents VALUES (?, ?, 'accession', '8-K', 'EX-99.1', 'exhibit', "
            "?, ?, 'source-hash', 'output-hash', 'removed.json', ?, ?)",
            [
                identifier,
                ticker,
                filing_date,
                "https://www.sec.gov/Archives/edgar/data/50863/filing.htm",
                "processed" if texts else "no_extractable_text",
                json.dumps({"warnings": ["images_not_extracted"]}),
            ],
        )
        # Reverse insertion order makes pagination depend on the document's stored order.
        for section_index in reversed(range((len(texts) + 3) // 4)):
            section_id = f"{identifier}#section-{section_index}"
            subset = texts[section_index * 4 : section_index * 4 + 4]
            db.execute(
                "INSERT INTO sections VALUES (?, ?, ?, 'Results', 'heading', NULL, '[]', ?)",
                [section_id, identifier, section_index, "\n\n".join(subset)],
            )
            for chunk_index, text in reversed(list(enumerate(subset))):
                db.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, FALSE, FALSE, 'Company A masked')",
                    [
                        f"{section_id}-chunk-{chunk_index}",
                        identifier,
                        section_id,
                        chunk_index,
                        text,
                        len(text),
                    ],
                )

    async def call(self, name, arguments, error=False):
        async with Client(server.mcp) as client:
            result = await client.call_tool(name, arguments)
        self.assertEqual(result.is_error, error, result.content)
        return result.structured_content

    async def search(self, **changes):
        return await self.call(
            "find_company_documents",
            {"ticker": " intc ", "start_date": "2024-10-31", "end_date": "2024-10-31"} | changes,
        )

    async def test_search_filters_literal_text_and_preserves_coverage(self):
        found = await self.search(query="EARNINGS")
        self.assertEqual(found["ticker"], "INTC")
        self.assertEqual([r["document_id"] for r in found["documents"]], [self.release])
        self.assertEqual(found["documents"][0]["document_type"], "EX-99.1")
        self.assertEqual(found["documents"][0]["filing_date"], "2024-10-31")
        self.assertEqual(
            found["coverage"],
            {"first_filing_date": "2024-10-30", "last_filing_date": "2025-01-31"},
        )
        self.assertFalse(found["has_more"])
        for query in ("EARNINGS%", "' OR 1=1 --", "Company A"):
            with self.subTest(query=query):
                self.assertEqual((await self.search(query=query))["documents"], [])
        self.assertEqual(len((await self.search(query="100%"))["documents"]), 1)
        self.assertEqual(
            (await self.search(start_date="2024-09-01", end_date="2024-09-30"))["documents"], []
        )

    async def test_search_reports_when_more_than_twenty_documents_match(self):
        with duckdb.connect(str(self.database)) as db:
            for i in range(21):
                self.add_document(db, f"INTC/extra-{i:02}.htm", "INTC", "2024-10-31", ["Extra."])
        found = await self.search(query="Extra.")
        self.assertEqual(len(found["documents"]), 20)
        self.assertTrue(found["has_more"])
        self.assertEqual(found, await self.search(query="Extra."))

    async def test_pagination_reads_all_original_chunks_with_citations_without_writes(self):
        before = hashlib.sha256(self.database.read_bytes()).digest()
        first = await self.call("read_company_document", {"document_id": self.release})
        self.assertEqual((first["total_chunks"], first["offset"], first["next_offset"]), (7, 0, 5))
        self.assertEqual(len(first["chunks"]), 5)
        last = await self.call("read_company_document", {"document_id": self.release, "offset": 5})
        self.assertIsNone(last["next_offset"])
        chunks = first["chunks"] + last["chunks"]
        self.assertEqual([c["evidence_text"] for c in chunks], self.texts)
        self.assertEqual(len({c["id"] for c in chunks}), 7)
        self.assertIn("images_not_extracted", first["warnings"])
        for chunk in chunks:
            self.assertEqual(chunk["document"], first["source_url"])
            self.assertEqual(chunk["document_type"], "EX-99.1")
            self.assertEqual(chunk["document_date"], "2024-10-31")
            self.assertTrue(chunk["citation_url"].startswith(first["source_url"] + "#:~:text="))
            self.assertNotIn("Company A", chunk["evidence_text"])
        await self.search()
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), before)

    async def test_unknown_empty_and_out_of_range_documents_are_explicit(self):
        await self.call("read_company_document", {"document_id": "../../etc/passwd"}, error=True)
        await self.call(
            "read_company_document", {"document_id": self.release, "offset": 7}, error=True
        )
        await self.call(
            "find_company_documents",
            {"ticker": "UNKNOWN", "start_date": "2024-01-01", "end_date": "2024-12-31"},
            error=True,
        )
        empty = await self.call("read_company_document", {"document_id": self.empty})
        self.assertEqual(empty["status"], "no_extractable_text")
        self.assertEqual(empty["chunks"], [])
        self.assertEqual(empty["total_chunks"], 0)
        self.assertIsNone(empty["next_offset"])

    async def test_invalid_arguments_fail_before_database_access(self):
        base = {"ticker": "INTC", "start_date": "2024-10-31", "end_date": "2024-10-31"}
        searches = [
            {"ticker": 123},
            {"ticker": "bad/name"},
            {"ticker": ""},
            {"start_date": "20241031"},
            {"start_date": "2024-02-30"},
            {"start_date": "2024-11-01"},
            {"end_date": None},
            {"query": 2},
            {"query": "x" * 161},
        ]
        reads = [{"document_id": ""}, {"document_id": 2}, {"document_id": "x" * 501}]
        reads += [{"document_id": self.release, "offset": value} for value in (-1, True, "1")]
        with patch.object(documents.duckdb, "connect") as connect:
            for change in searches:
                await self.call("find_company_documents", base | change, error=True)
            for args in reads:
                await self.call("read_company_document", args, error=True)
            connect.assert_not_called()

    async def test_missing_database_is_not_created(self):
        missing = Path(self.temp.name) / "missing.duckdb"
        with patch.object(documents, "DATABASE", missing):
            await self.call("read_company_document", {"document_id": self.release}, error=True)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
