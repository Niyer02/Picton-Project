import fcntl
import json
import re
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import picton_graph.extraction.corpus as corpus
import picton_graph.extraction.export as export
import picton_graph.extraction.extractor as extraction
from picton_graph.ingestion.masking import mask_text
from tests.test_extract_relationships import (
    MAPPING,
    MATCHER,
    RULES,
    entity,
    reference,
    relation,
    response,
    sample_chunk,
    sample_result,
)


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ontology = extraction.load_ontology()
        self.counter = 0

    def chunk(self, raw="NVIDIA supplies memory chips to Micron Technology."):
        masked, spans = mask_text(raw, MATCHER, RULES)
        chunk = sample_chunk(masked)
        chunk.update(text=raw, mask_spans=spans, ticker="NVDA", source_sha256="a" * 64)
        return chunk

    def prepare(self, outcomes=None):
        self.counter += 1
        source = self.root / f"source-{self.counter}"
        source.mkdir()
        (source / "results").mkdir()
        (source / ".run.lock").touch()
        prompt_path = self.root / "prompt.md"
        prompt_path.write_text("Extract explicit facts only.")
        outcomes = outcomes or [response(sample_result())]
        chunks = []
        for index in range(len(outcomes)):
            chunk = self.chunk()
            chunk.update(
                chunk_id=f"NVDA/file.htm#section-{index:04d}", section_id=f"section-{index:04d}"
            )
            chunks.append(chunk)
        config = {
            "model": extraction.MODEL,
            "output_format": extraction.OUTPUT_FORMAT,
            "workers": 32,
            "max_tokens": 8192,
        }
        with patch.object(corpus, "select_chunks", return_value=(chunks, MAPPING)):
            manifest, ontology, prompt, mapping = corpus.snapshot(
                source, self.root / "unused.duckdb", prompt_path, config
            )
        counts = {status: 0 for status in (*corpus.TERMINAL, "not_started", "in_flight")}
        for index, (chunk, outcome) in enumerate(zip(chunks, outcomes), 1):
            request = extraction.build_request(chunk, ontology, prompt)
            record = corpus.base_record(index, chunk, request)
            record["response"] = outcome
            corpus.finalize(record, chunk, mapping, ontology)
            counts[record["status"]] += 1
            corpus.save_json(corpus.result_path(source, index), record)
        manifest.update(
            status="complete_with_errors" if counts["rejected"] else "complete",
            processed=len(chunks),
            counts=counts,
        )
        corpus.save_json(source / "manifest.json", manifest)
        return source

    def test_label_restoration_preserves_suffixes_and_legal_company_names(self):
        text = (
            "The Company A Corporation, company\n c’s chips, COMPANY-D-imec and Company I Holdings."
        )
        self.assertEqual(
            export.restore_names(text, MAPPING),
            "The NVIDIA Corporation, Advanced Micro Devices’s chips, ASML-imec and Micron Technology Holdings.",
        )
        unchanged = "LYB Finance Company B.V.; Company ABC; Customer A; the company; Company K"
        self.assertEqual(export.restore_names(unchanged, MAPPING), unchanged)
        self.assertIsNone(export.restore_names(None, MAPPING))

    def test_payload_uses_original_quotes_and_preserves_event_time_and_typed_links(self):
        raw = "The NVIDIA Corporation supplies memory chips to Micron in June 2025."
        chunk = self.chunk(raw)
        quote = chunk["masked_text"]
        result = sample_result()
        for e in result["entities"]:
            e["evidence_text"] = quote
        result["entities"].extend(
            [
                entity("Service", "memory chips", quote),
                {
                    **entity("Event", "Company A’s supply announcement", quote),
                    "event_type": "supply_announcement",
                    "time_text": "June 2025",
                },
            ]
        )
        result["relationships"][0].update(
            evidence_text=quote, time_text="June 2025", supplied_service=["memory chips"]
        )
        result["relationships"].append(
            relation(
                reference("SourceDocument", "document"),
                "reports",
                reference("Event", "Company A’s supply announcement"),
                quote,
            )
        )
        checked = extraction.validate_extraction(result, chunk, MAPPING, self.ontology)
        before = deepcopy(checked)
        record = corpus.base_record(
            1, chunk, extraction.build_request(chunk, self.ontology, "prompt")
        )
        payload = export.graph_payload(record, chunk, checked, MAPPING, self.ontology)
        nodes = {node["id"]: node for node in payload["nodes"]}
        edge = payload["relationships"][0]
        self.assertEqual(
            (edge["source_name"], edge["target_name"]), ("NVIDIA", "Micron Technology")
        )
        self.assertEqual(edge["properties"]["evidence_text"], raw)
        self.assertNotIn("Company A", edge["properties"]["evidence_text"])
        self.assertEqual(edge["time_text"], "June 2025")
        self.assertNotIn("time_text", edge["properties"])
        product, service = (
            edge["properties"]["supplied_product"][0],
            edge["properties"]["supplied_service"][0],
        )
        self.assertNotEqual(product, service)
        self.assertEqual(nodes[product]["labels"], ["Product"])
        self.assertEqual(nodes[service]["labels"], ["Service"])
        event = next(node for node in nodes.values() if node["labels"] == ["Event"])
        self.assertEqual(event["properties"]["name"], "NVIDIA’s supply announcement")
        self.assertEqual(event["time_text"], "June 2025")
        self.assertNotIn("occurred_at", event["properties"])
        self.assertEqual(checked, before)

    def test_all_prompt_edge_types_export_as_valid_graph_payloads(self):
        examples = [
            json.loads(block)
            for block in re.findall(r"```json\n(.*?)\n```", extraction.PROMPT.read_text(), re.S)
        ]
        kinds = set()
        for example in examples:
            raw = export.restore_names(example["input"]["chunk_text"], MAPPING)
            chunk = self.chunk(raw)
            chunk.update(
                issuer=example["input"]["filing_issuer"], role=example["input"]["document_role"]
            )
            checked = extraction.validate_extraction(
                example["output"], chunk, MAPPING, self.ontology
            )
            record = corpus.base_record(
                1, chunk, extraction.build_request(chunk, self.ontology, "prompt")
            )
            payload = export.graph_payload(record, chunk, checked, MAPPING, self.ontology)
            kinds.update(r["type"] for r in payload["relationships"])
        self.assertEqual(kinds, set(extraction.RELATIONSHIPS))

    def test_export_excludes_rejected_keeps_empty_and_never_calls_services_or_changes_source(self):
        source = self.prepare([response(sample_result()), response(finish="length"), response()])
        hashes = {
            p.relative_to(source): corpus.file_hash(p) for p in source.rglob("*") if p.is_file()
        }
        with (
            patch.object(corpus, "call_openrouter") as corpus_api,
            patch.object(extraction, "call_openrouter") as api,
            patch("picton_graph.graph.connection.connect") as graph,
            patch("duckdb.connect") as database,
            redirect_stdout(StringIO()),
        ):
            output = export.export_run(source)
        for mock in (corpus_api, api, graph, database):
            mock.assert_not_called()
        self.assertTrue(
            all(corpus.file_hash(source / name) == digest for name, digest in hashes.items())
        )
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["exported_chunks"], 2)
        self.assertEqual(manifest["omitted_results"]["rejected"], 1)
        self.assertEqual(manifest["empty_extractions"], 1)
        rows = [json.loads(line) for line in (output / "assertions.jsonl").read_text().splitlines()]
        self.assertEqual([r["provenance"]["index"] for r in rows], [1, 3])
        self.assertEqual(rows[1]["nodes"][0]["labels"], ["SourceDocument"])
        self.assertEqual(rows[1]["relationships"], [])
        self.assertEqual(manifest["output_sha256"], corpus.file_hash(output / "assertions.jsonl"))
        with self.assertRaises(FileExistsError):
            export.export_run(source)
        self.assertEqual(manifest["output_sha256"], corpus.file_hash(output / "assertions.jsonl"))

    def test_corrupt_missing_extra_mapping_and_unfinished_results_fail(self):
        for failure in (
            "missing",
            "extra",
            "request",
            "validated",
            "mapping",
            "counts",
            "pending",
            "running",
        ):
            with self.subTest(failure=failure):
                source = self.prepare()
                path = corpus.result_path(source, 1)
                record = json.loads(path.read_text())
                manifest = json.loads((source / "manifest.json").read_text())
                if failure == "missing":
                    path.unlink()
                elif failure == "extra":
                    (source / "results" / "1.json").write_text(path.read_text())
                elif failure == "mapping":
                    (source / "company_mapping.json").write_text("{}")
                elif failure in ("counts", "running"):
                    if failure == "counts":
                        manifest["counts"]["valid"] = 2
                    else:
                        manifest["status"] = "running"
                    corpus.save_json(source / "manifest.json", manifest)
                else:
                    if failure == "request":
                        record["request"]["messages"][1]["content"] = "changed"
                    elif failure == "validated":
                        record["validated"]["entities"][0]["resolved_iri"] = "invented"
                    else:
                        record["status"] = "pending"
                    corpus.save_json(path, record)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    export.export_run(source)
                self.assertFalse((source / "deanonymized" / "assertions.jsonl").exists())

    def test_active_run_lock_prevents_export(self):
        source = self.prepare()
        with (source / ".run.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "active"):
                export.export_run(source)
        self.assertFalse((source / "deanonymized").exists())

    def test_equivalent_frozen_alias_order_is_accepted_but_swapped_identities_are_not(self):
        for swapped in (False, True):
            with self.subTest(swapped=swapped):
                source = self.prepare()
                mapping = json.loads((source / "company_mapping.json").read_text())
                if swapped:
                    mapping["Company A"], mapping["Company I"] = (
                        mapping["Company I"],
                        mapping["Company A"],
                    )
                else:
                    rule = mapping["Company G"]
                    dashed, spaced = r"KLA[-‐‑‒–—]Tencor", r"KLA\s+Tencor"
                    original = rule["pattern"]
                    rule["pattern"] = (
                        original.replace(dashed, "SWAP")
                        .replace(spaced, dashed)
                        .replace("SWAP", spaced)
                    )
                    rule["aliases"].reverse()
                    self.assertNotEqual(original, rule["pattern"])
                    for alias in ("KLA", "KLA-Tencor", "KLA Tencor", "KLAC"):
                        self.assertEqual(
                            bool(re.search(original, alias)),
                            bool(re.search(rule["pattern"], alias)),
                        )
                corpus.save_json(source / "company_mapping.json", mapping)
                manifest = json.loads((source / "manifest.json").read_text())
                manifest["files"]["company_mapping.json"] = corpus.file_hash(
                    source / "company_mapping.json"
                )
                corpus.save_json(source / "manifest.json", manifest)
                if swapped:
                    with self.assertRaisesRegex(ValueError, "agreed ten-company labels"):
                        export.export_run(source)
                else:
                    output = export.export_run(source)
                    self.assertEqual(
                        json.loads((output / "manifest.json").read_text())["status"], "complete"
                    )


if __name__ == "__main__":
    unittest.main()
