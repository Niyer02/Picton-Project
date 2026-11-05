import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from picton_graph.ingestion.html import process_document
from picton_graph.ingestion.storage import store_export


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.exports = self.root / "processed"
        self.database = self.root / "filings.duckdb"
        self.documents = [
            self.document(
                "first.htm",
                "<p>Item 1. Business</p><p>NVIDIA’s revenue: €42.</p>"
                "<p>Item 1A. Risk Factors</p><p>Customer concentration.</p>",
            ),
            self.document(
                "second.htm", "<h2>Earnings release</h2><p>Results and outlook.</p>", "exhibit"
            ),
            self.document("slides.htm", '<img src="slides.png">', "exhibit"),
        ]
        self.write_exports(self.documents)

    def document(self, filename, html, role="primary"):
        source = {
            "ticker": "NVDA",
            "accession": "0001045810-26-000021",
            "filename": filename,
            "form": "10-K",
            "type": "10-K" if role == "primary" else "EX-99.1",
            "role": role,
            "filing_date": "2026-02-25",
            "report_date": "2026-01-25",
            "url": f"https://www.sec.gov/example/{filename}",
            "sha256": hashlib.sha256(html.encode()).hexdigest(),
        }
        return process_document(html.encode(), source)

    def write_exports(self, documents):
        manifest = {"complete": True, "max_chars": 6000, "documents": []}
        for doc in documents:
            relative = doc["document_id"] + ".json"
            path = self.exports / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(doc, ensure_ascii=False).encode()
            path.write_bytes(payload)
            sections = doc["sections"]
            chunks = [c for s in sections for c in s["chunks"]]
            manifest["documents"].append(
                {
                    "document_id": doc["document_id"],
                    "output_path": relative,
                    "output_sha256": hashlib.sha256(payload).hexdigest(),
                    "source_sha256": doc["source"]["sha256"],
                    "status": doc["status"],
                    "sections": len(sections),
                    "chunks": len(chunks),
                    "sec_items": sum(s["kind"] == "sec_item" for s in sections),
                    "over_target": sum(c["over_target"] for c in chunks),
                    "warnings": doc["warnings"],
                }
            )
        (self.exports / "manifest.json").write_text(json.dumps(manifest))
        return manifest

    def snapshot(self):
        with duckdb.connect(str(self.database), read_only=True) as db:
            return {
                table: db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                for table in ("documents", "sections", "chunks")
            }

    def test_round_trip_rerun_and_subset_replacement_preserve_other_data(self):
        expected = {"documents": 3, "sections": 3, "chunks": 3}
        self.assertEqual(store_export(self.exports, self.database), expected)
        first = self.snapshot()
        self.assertEqual(store_export(self.exports, self.database), expected)
        self.assertEqual(self.snapshot(), first)
        with duckdb.connect(str(self.database)) as db:
            for doc in self.documents:
                stored = db.execute(
                    "SELECT metadata FROM documents WHERE document_id = ?", [doc["document_id"]]
                ).fetchone()[0]
                self.assertEqual(
                    json.loads(stored), {k: v for k, v in doc.items() if k != "sections"}
                )
                sections = db.execute(
                    "SELECT section_id, title, kind, part, items, text FROM sections "
                    "WHERE document_id = ? ORDER BY section_index",
                    [doc["document_id"]],
                ).fetchall()
                self.assertEqual(len(sections), len(doc["sections"]))
                for row, section in zip(sections, doc["sections"]):
                    self.assertEqual(
                        row,
                        (
                            section["section_id"],
                            section["title"],
                            section["kind"],
                            section["part"],
                            json.dumps(section["items"]),
                            section["text"],
                        ),
                    )
                    chunks = db.execute(
                        "SELECT chunk_id, text, character_count, contains_table, over_target "
                        "FROM chunks WHERE section_id = ? ORDER BY chunk_index",
                        [row[0]],
                    ).fetchall()
                    self.assertEqual(
                        chunks,
                        [
                            (
                                c["chunk_id"],
                                c["text"],
                                c["character_count"],
                                c["contains_table"],
                                c["over_target"],
                            )
                            for c in section["chunks"]
                        ],
                    )
            db.execute("CREATE TABLE unrelated_notes (text VARCHAR)")
            db.execute("INSERT INTO unrelated_notes VALUES ('Keep this')")
        updated = self.document("first.htm", "<p>Item 1. Business</p><p>Updated business.</p>")
        self.write_exports([updated])
        self.assertEqual(
            store_export(self.exports, self.database), {"documents": 1, "sections": 1, "chunks": 1}
        )
        with duckdb.connect(str(self.database), read_only=True) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM documents").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone()[0], 2)
            self.assertEqual(
                db.execute("SELECT text FROM unrelated_notes").fetchone()[0], "Keep this"
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM chunks WHERE text LIKE '%concentration%'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM documents WHERE status='no_extractable_text'"
                ).fetchone()[0],
                1,
            )

    def test_incomplete_corrupt_escaping_or_mislinked_exports_are_rejected(self):
        store_export(self.exports, self.database)
        original = self.snapshot()
        for problem in (
            "incomplete",
            "empty",
            "duplicate",
            "hash",
            "path",
            "section",
            "chunk",
            "counts",
        ):
            with self.subTest(problem=problem):
                manifest = self.write_exports(self.documents)
                if problem == "incomplete":
                    manifest["complete"] = False
                elif problem == "empty":
                    manifest["documents"] = []
                elif problem == "duplicate":
                    manifest["documents"].append(manifest["documents"][0])
                elif problem == "hash":
                    manifest["documents"][0]["output_sha256"] = "0" * 64
                elif problem == "path":
                    manifest["documents"][0]["output_path"] = "../outside.json"
                elif problem == "counts":
                    manifest["documents"][0]["chunks"] = 99
                else:
                    changed = json.loads(json.dumps(self.documents))
                    if problem == "section":
                        changed[0]["sections"][0]["section_id"] = "another-document#section-0000"
                    else:
                        changed[0]["sections"][0]["chunks"][0]["text"] = "Truncated"
                    manifest = self.write_exports(changed)
                (self.exports / "manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    store_export(self.exports, self.database)
                self.assertEqual(self.snapshot(), original)

    def test_late_database_failure_rolls_back_earlier_replacement(self):
        store_export(self.exports, self.database)
        original = self.snapshot()
        updated = self.document("first.htm", "<p>Item 1. Business</p><p>Must roll back.</p>")
        broken = json.loads(json.dumps(self.documents[1]))
        broken["sections"][0]["title"] = None
        self.write_exports([updated, broken])
        with self.assertRaises(duckdb.ConstraintException):
            store_export(self.exports, self.database)
        self.assertEqual(self.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
