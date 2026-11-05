"""Offline integrity checks for the manually curated draft examples."""

import hashlib
import json
import unittest
from collections import Counter, defaultdict

import duckdb

from picton_graph.config import GOLD_PATH, SCHEMA_PATH
from picton_graph.extraction.extractor import (
    ENTITY_TYPES,
    OUTPUT_FORMAT,
    RELATIONSHIPS,
    build_request,
    load_ontology,
    validate_extraction,
)
from picton_graph.ingestion.masking import TICKERS, company_rules, mask_text
from picton_graph.ingestion.storage import DATABASE

GOLD = GOLD_PATH


class GoldSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((GOLD / "manifest.json").read_text())
        cls.rows = [
            json.loads(line)
            for split in ("train", "validation", "holdout")
            for line in (GOLD / f"{split}.jsonl").read_text().splitlines()
        ]
        cls.ontology = load_ontology()

    def test_counts_coverage_splits_and_hashes(self):
        self.assertEqual(self.manifest["output_format"], OUTPUT_FORMAT)
        self.assertEqual(len(self.rows), 20)
        self.assertEqual(len({r["id"] for r in self.rows}), 20)
        self.assertEqual(len({r["input"]["chunk_text"] for r in self.rows}), 20)
        self.assertEqual(
            Counter(r["split"] for r in self.rows), {"train": 12, "validation": 4, "holdout": 4}
        )
        self.assertEqual({r["provenance"]["ticker"] for r in self.rows}, set(TICKERS))
        for field in ("ticker", "document_id", "chunk_id"):
            groups = defaultdict(set)
            for row in self.rows:
                groups[row["provenance"][field]].add(row["split"])
            self.assertTrue(all(len(splits) == 1 for splits in groups.values()))
        train = [r for r in self.rows if r["split"] == "train"]
        self.assertEqual(
            {e["type"] for r in train for e in r["expected_output"]["entities"]}, set(ENTITY_TYPES)
        )
        counts = Counter(
            e["type"] for r in self.rows for e in r["expected_output"]["relationships"]
        )
        self.assertEqual(dict(counts), self.manifest["relationship_counts"])
        self.assertEqual(
            {e["type"] for r in train for e in r["expected_output"]["relationships"]},
            set(RELATIONSHIPS),
        )
        self.assertEqual(
            sorted(r["id"] for r in self.rows if not r["expected_output"]["entities"]),
            self.manifest["empty_examples"],
        )
        self.assertEqual(
            hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(), self.manifest["ontology_sha256"]
        )
        for filename, expected in self.manifest["files"].items():
            self.assertEqual(hashlib.sha256((GOLD / filename).read_bytes()).hexdigest(), expected)
            split = filename.removesuffix(".jsonl")
            self.assertTrue(
                all(
                    json.loads(line)["split"] == split
                    for line in (GOLD / filename).read_text().splitlines()
                )
            )
        for row in self.rows:
            self.assertEqual(row["annotation"]["human_review"], "pending")
            self.assertTrue(row["annotation"]["must_not_extract"])
            self.assertTrue(row["annotation"]["adjudication_notes"])

    def test_targets_and_excerpts_match_read_only_source(self):
        if not DATABASE.is_file():
            self.skipTest("Source DuckDB is unavailable; source provenance could not be verified.")
        before = hashlib.sha256(DATABASE.read_bytes()).hexdigest()
        with duckdb.connect(str(DATABASE), read_only=True) as db:
            companies = [
                dict(zip(("ticker", "name", "iri"), r))
                for r in db.execute(
                    "SELECT ticker, company_name, company_iri FROM company_masks"
                ).fetchall()
            ]
            rules, matcher = company_rules(companies)
            mapping = {r["masked_name"]: r for r in rules}
            for row in self.rows:
                with self.subTest(example=row["id"]):
                    p, context = row["provenance"], row["input"]
                    result = db.execute(
                        """SELECT c.*, s.masked_title, d.ticker, d.form, d.role,
                        d.document_type, d.filing_date, d.source_url, d.source_sha256, d.metadata
                        FROM chunks c JOIN sections s USING(section_id)
                        JOIN documents d ON c.document_id=d.document_id WHERE c.chunk_id=?""",
                        [p["chunk_id"]],
                    )
                    full = dict(zip([c[0] for c in result.description], result.fetchone()))
                    full["metadata"] = json.loads(full["metadata"])
                    full["filing_date"] = full["filing_date"].isoformat()
                    full["issuer"] = next(
                        r["masked_name"] for r in rules if r["ticker"] == full["ticker"]
                    )
                    self.assertEqual(
                        hashlib.sha256(full["text"].encode()).hexdigest(),
                        p["parent_original_chunk_sha256"],
                    )
                    self.assertEqual(
                        hashlib.sha256(full["masked_text"].encode()).hexdigest(),
                        p["parent_masked_chunk_sha256"],
                    )
                    self.assertEqual(
                        full["masked_text"][p["masked_excerpt_start"] : p["masked_excerpt_end"]],
                        context["chunk_text"],
                    )
                    self.assertEqual(
                        full["text"][p["original_excerpt_start"] : p["original_excerpt_end"]],
                        p["original_excerpt"],
                    )
                    for key in (
                        "document_id",
                        "section_id",
                        "source_url",
                        "source_sha256",
                        "ticker",
                    ):
                        self.assertEqual(full[key], p[key])
                    self.assertEqual(full["contains_table"], p["parent_contains_table"])
                    self.assertEqual(context["contains_table"], "\t" in context["chunk_text"])
                    self.assertEqual(
                        mask_text(p["original_excerpt"], matcher, rules),
                        (context["chunk_text"], p["excerpt_mask_spans"]),
                    )
                    local = {
                        **full,
                        "text": p["original_excerpt"],
                        "masked_text": context["chunk_text"],
                        "contains_table": context["contains_table"],
                        "mask_spans": p["excerpt_mask_spans"],
                    }
                    self.assertEqual(
                        json.loads(
                            build_request(local, self.ontology, "")["messages"][1]["content"]
                        ),
                        context,
                    )
                    checked = validate_extraction(
                        row["expected_output"], local, mapping, self.ontology
                    )
                    for item in checked["entities"] + checked["relationships"]:
                        proof = item["evidence"]
                        self.assertEqual(
                            p["original_excerpt"][proof["original_start"] : proof["original_end"]],
                            proof["original_quote"],
                        )
                        self.assertEqual(
                            context["chunk_text"][proof["masked_start"] : proof["masked_end"]],
                            item["evidence_text"],
                        )
        self.assertEqual(hashlib.sha256(DATABASE.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
