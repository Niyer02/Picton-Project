import hashlib
import json
import re
import unittest
from copy import deepcopy
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import duckdb

import picton_graph.extraction.client as client
import picton_graph.extraction.extractor as extraction
from picton_graph.ingestion.masking import ALIASES, TICKERS, company_rules, mask_chunks, mask_text
from picton_graph.ingestion.storage import SCHEMA

COMPANIES = [
    {"ticker": ticker, "iri": f"urn:company:{ticker}", "name": ALIASES[ticker][0]}
    for ticker in TICKERS
]
RULES, MATCHER = company_rules(COMPANIES)
MAPPING = {r["masked_name"]: r for r in RULES}


def sample_chunk(text="Company A supplies memory chips to Company I."):
    return {
        "chunk_id": "NVDA/filing/file.htm#section-0000-chunk-0000",
        "document_id": "NVDA/filing/file.htm",
        "section_id": "section-0000",
        "text": text,
        "masked_text": text,
        "mask_spans": [],
        "masked_title": "Business",
        "issuer": "Company A",
        "role": "primary",
        "form": "10-K",
        "document_type": "10-K",
        "filing_date": "2026-02-25",
        "source_url": "https://www.sec.gov/Archives/NVDA/file.htm",
        "contains_table": False,
        "metadata": {"source": {"report_date": "2026-01-25"}},
    }


def reference(kind, name):
    return {"type": kind, "name": name}


def entity(kind, name, quote):
    return {
        "type": kind,
        "name": name,
        "evidence_text": quote,
        "event_type": None,
        "time_text": None,
    }


def relation(source, kind, target, quote, products=None, services=None):
    return {
        "source": source,
        "type": kind,
        "target": target,
        "evidence_text": quote,
        "time_text": None,
        "supplied_product": products or [],
        "supplied_service": services or [],
    }


def sample_result():
    quote = sample_chunk()["text"]
    return {
        "entities": [
            entity("Company", "Company A", quote),
            entity("Company", "Company I", quote),
            entity("Product", "memory chips", quote),
        ],
        "relationships": [
            relation(
                reference("Company", "Company A"),
                "supplies_to",
                reference("Company", "Company I"),
                quote,
                ["memory chips"],
            )
        ],
    }


def response(result=None, finish="stop"):
    return {
        "id": "mock-response",
        "model": extraction.MODEL,
        "usage": {"total_tokens": 100},
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": json.dumps(
                        result if result is not None else {"entities": [], "relationships": []}
                    )
                },
            }
        ],
    }


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ontology = extraction.load_ontology()
        cls.prompt = extraction.PROMPT.read_text()

    def test_prompt_examples_validate_and_cover_all_nine_relationships(self):
        examples = [
            json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", self.prompt, re.S)
        ]
        self.assertEqual(len(examples), 4)
        types = set()
        for example in examples:
            chunk = sample_chunk(example["input"]["chunk_text"])
            chunk.update(
                issuer=example["input"]["filing_issuer"], role=example["input"]["document_role"]
            )
            extraction.validate_extraction(example["output"], chunk, MAPPING, self.ontology)
            types.update(r["type"] for r in example["output"]["relationships"])
        self.assertEqual(types, set(extraction.RELATIONSHIPS))

    def test_request_uses_only_masked_context_and_canonical_contract(self):
        request = extraction.build_request(sample_chunk(), self.ontology, self.prompt)
        serialized = json.dumps(request)
        for private in (
            "NVDA",
            "NVIDIA",
            "urn:company:",
            "www.sec.gov",
            "file.htm",
            "source_sha256",
        ):
            self.assertNotIn(private, serialized)
        self.assertEqual(request["model"], "openai/gpt-6-luna")
        self.assertTrue(request["provider"]["require_parameters"])
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertNotIn("temperature", request)
        self.assertEqual(len(request["messages"]), 2)
        self.assertEqual(
            json.loads(request["messages"][1]["content"])["chunk_text"], sample_chunk()["text"]
        )

    def test_exact_original_evidence_survives_multiple_masks_unicode_and_suffixes(self):
        raw = "Before. The NVIDIA Corporation supplies Micron Technology with wafers. After."
        masked, spans = mask_text(raw, MATCHER, RULES)
        chunk = sample_chunk(masked)
        chunk.update(text=raw, mask_spans=spans)
        quote = "The Company A Corporation supplies Company I with wafers."
        proof = extraction.evidence(quote, chunk)
        self.assertEqual(
            proof["original_quote"],
            "The NVIDIA Corporation supplies Micron Technology with wafers.",
        )
        self.assertEqual(
            raw[proof["original_start"] : proof["original_end"]], proof["original_quote"]
        )
        for quote in ("Before.", "After.", "Company A", "Company I", masked):
            proof = extraction.evidence(quote, chunk)
            self.assertEqual(masked[proof["masked_start"] : proof["masked_end"]], quote)
            self.assertEqual(mask_text(proof["original_quote"], MATCHER, RULES)[0], quote)
        with self.assertRaisesRegex(ValueError, "cut through"):
            extraction.evidence("Company", chunk)
        with self.assertRaisesRegex(ValueError, "exact"):
            extraction.evidence("Company A supplies Company I.", chunk)

    def test_validator_enforces_scope_types_quotes_and_references(self):
        valid = sample_result()
        checked = extraction.validate_extraction(valid, sample_chunk(), MAPPING, self.ontology)
        self.assertEqual(checked["entities"][0]["resolved_iri"], "urn:company:NVDA")
        self.assertTrue(all("id" not in e for e in checked["entities"]))
        self.assertEqual(valid, sample_result())  # No mutation of the raw model output.
        mutations = [
            lambda r: r.update(unexpected=True),
            lambda r: r["entities"][0].update(name="NVIDIA"),
            lambda r: r["entities"][0].update(type="Supplier"),
            lambda r: r["entities"][0].update(type="SourceDocument"),
            lambda r: r["entities"][0].update(id="document"),
            lambda r: r["relationships"][0].update(source="e1"),
            lambda r: r["entities"].append(deepcopy(r["entities"][0])),
            lambda r: r["entities"][2].update(name="invented product"),
            lambda r: r["entities"][2].update(evidence_text="a fabricated quote"),
            lambda r: r["entities"][0].update(time_text="2025"),
            lambda r: r["relationships"][0].update(type="buys_from"),
            lambda r: r["relationships"][0].update(source=reference("Product", "memory chips")),
            lambda r: r["relationships"][0].update(target=reference("Company", "Company A")),
            lambda r: r["relationships"][0].update(target=reference("Company", "Company J")),
            lambda r: r["relationships"][0].update(target=reference("Topic", "Company I")),
            lambda r: r["relationships"][0].update(supplied_product=["Company I"]),
            lambda r: r["relationships"][0].update(
                supplied_product=["memory chips", "memory chips"]
            ),
            lambda r: r["relationships"][0].update(supplied_service=["memory chips"]),
            lambda r: r["relationships"][0].update(time_text="during 2025"),
            lambda r: r["relationships"][0].update(evidence_text="A invented quotation"),
            lambda r: r["relationships"][0].update(type="competes_with"),
            lambda r: r["relationships"].append(deepcopy(r["relationships"][0])),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                bad = deepcopy(valid)
                mutation(bad)
                with self.assertRaises(ValueError):
                    extraction.validate_extraction(bad, sample_chunk(), MAPPING, self.ontology)

    def test_formatting_match_preserves_original_quotes_and_mask_offsets(self):
        raw = (
            "Before. The NVIDIA Corporation supplies\n\nMicron Technology with memory-chips. After."
        )
        masked, spans = mask_text(raw, MATCHER, RULES)
        chunk = sample_chunk(masked)
        chunk.update(text=raw, mask_spans=spans)
        quote = "  the company a corporation supplies company i with MEMORY CHIPS!\n"
        proof = extraction.evidence(quote, chunk)
        self.assertEqual(proof["match_type"], "formatting_normalized")
        self.assertEqual(
            proof["original_quote"],
            "The NVIDIA Corporation supplies\n\nMicron Technology with memory-chips",
        )
        self.assertEqual(
            raw[proof["original_start"] : proof["original_end"]], proof["original_quote"]
        )
        self.assertEqual(
            mask_text(proof["original_quote"], MATCHER, RULES)[0],
            masked[proof["masked_start"] : proof["masked_end"]],
        )
        with self.assertRaisesRegex(ValueError, "cut through"):
            extraction.evidence("company", chunk)
        with self.assertRaises(ValueError):
            extraction.evidence("company i memory chips AFTER", chunk)

    def test_matching_tolerates_formatting_but_keeps_words_numbers_and_symbols(self):
        accepted = [
            ("privacy governance\n\nIn June 2025", "Privacy governance In June 2025"),
            ("NUMBER of work-related accidents.", "Number of work related accidents"),
            ("  secondary and recycled materials\t", "Secondary and recycled materials"),
            ("STRASSE", "Straße"),
            ("EPS: - $1.50 this year", "eps -$1.50 this year"),
            ("eps -1.5 this year", "EPS –1.5 this year"),
        ]
        rejected = [
            ("mitigate my impact", "mitigate our impact"),
            ("first sentence third sentence", "first sentence. second sentence. third sentence"),
            ("EPS 15 this year", "EPS 1.5 this year"),
            ("EPS 1.5 this year", "EPS -1.5 this year"),
            ("EPS 1.5 this year", "EPS –1.5 this year"),
            ("EPS 1.5 this year", "EPS ‐1.5 this year"),
            ("EPS 5 this year", "EPS .5 this year"),
            ("EPS 5 this year", "EPS -.5 this year"),
            ("margin 5% this year", "margin .5% this year"),
            ("margin 10 this year", "margin 10% this year"),
            ("margin 10 this year", "margin 10٪ this year"),
            ("EPS 1.5 this year", "EPS ( 1.5 ) this year"),
            ("uses C for development", "uses C++ for development"),
            ("uses c for development", "uses C# for development"),
            ("5", "-5"),
            ("1.5", "(1.5)"),
            ("1.5", "1.50"),
            ("C", "C#"),
            ("memorychips", "memory chips"),
            ("... \n", "..."),
        ]
        for quote, source in accepted:
            with self.subTest(quote=quote):
                self.assertIsNotNone(extraction.matching_span(quote, source))
        for quote, source in rejected:
            with self.subTest(quote=quote):
                self.assertIsNone(extraction.matching_span(quote, source))

    def test_normalized_names_supply_annotations_times_and_duplicate_edges(self):
        source = "Company A supplies MEMORY-CHIPS to Company I in June 2025."
        result = sample_result()
        for e in result["entities"]:
            e["evidence_text"] = source
        result["relationships"][0].update(evidence_text=source, time_text="june\n2025")
        result["entities"].append(
            {
                **entity("Event", "Supply announcement", source),
                "event_type": "supply_announcement",
                "time_text": "june 2025",
            }
        )
        checked = extraction.validate_extraction(
            result, sample_chunk(source), MAPPING, self.ontology
        )
        self.assertEqual(checked["entities"][2]["name"], "memory chips")
        duplicate = deepcopy(result["relationships"][0])
        duplicate["evidence_text"] = source.lower().replace("-", " ").rstrip(".")
        result["relationships"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "Duplicate assertion"):
            extraction.validate_extraction(result, sample_chunk(source), MAPPING, self.ontology)

    def test_existing_literal_names_and_parenthesized_years_remain_supported(self):
        quote = "ESG measures2 include climate changes. Company A acquired Company I (2013)."
        result = {
            "entities": [
                entity("Topic", "ESG measures", quote),
                entity("Topic", "climate change", quote),
                {
                    **entity("Event", "Acquisition in 2013", quote),
                    "event_type": "acquisition",
                    "time_text": "2013",
                },
            ],
            "relationships": [],
        }
        extraction.validate_extraction(result, sample_chunk(quote), MAPPING, self.ontology)

    def test_service_annotations_document_edges_and_symmetric_duplicates(self):
        result = sample_result()
        result["entities"][2]["type"] = "Service"
        result["relationships"][0].update(supplied_product=[], supplied_service=["memory chips"])
        extraction.validate_extraction(result, sample_chunk(), MAPPING, self.ontology)
        document, a, i = (
            reference("SourceDocument", "document"),
            reference("Company", "Company A"),
            reference("Company", "Company I"),
        )
        result["relationships"] = [relation(document, "mentions", a, sample_chunk()["text"])]
        extraction.validate_extraction(result, sample_chunk(), MAPPING, self.ontology)
        result["relationships"][0]["target"] = document
        with self.assertRaises(ValueError):
            extraction.validate_extraction(result, sample_chunk(), MAPPING, self.ontology)
        result["relationships"] = [
            relation(a, "competes_with", i, sample_chunk()["text"]),
            relation(i, "competes_with", a, sample_chunk()["text"]),
        ]
        with self.assertRaisesRegex(ValueError, "Duplicate assertion"):
            extraction.validate_extraction(result, sample_chunk(), MAPPING, self.ontology)

    def test_same_name_different_types_and_internal_identifiers_survive_reordering(self):
        quote = "Company A operates in semiconductor manufacturing and discusses semiconductor manufacturing."
        name = "semiconductor manufacturing"
        result = {
            "entities": [
                entity("Company", "Company A", quote),
                entity("Sector", name, quote),
                entity("Topic", name, quote),
            ],
            "relationships": [
                relation(
                    reference("Company", "Company A"),
                    "operates_in",
                    reference("Sector", name),
                    quote,
                ),
                relation(
                    reference("Company", "Company A"), "has_topic", reference("Topic", name), quote
                ),
            ],
        }
        checked = extraction.validate_extraction(
            result, sample_chunk(quote), MAPPING, self.ontology
        )
        iris = {(e["type"], e["name"]): e["resolved_iri"] for e in checked["entities"]}
        self.assertEqual(len(set(iris.values())), 3)
        result["entities"].reverse()
        reordered = extraction.validate_extraction(
            result, sample_chunk(quote), MAPPING, self.ontology
        )
        self.assertEqual(
            iris, {(e["type"], e["name"]): e["resolved_iri"] for e in reordered["entities"]}
        )
        result["relationships"][0]["target"]["type"] = "Topic"
        with self.assertRaisesRegex(ValueError, "Invalid target class"):
            extraction.validate_extraction(result, sample_chunk(quote), MAPPING, self.ontology)

    def test_separate_event_occurrences_use_distinct_descriptive_names(self):
        quote = "Company A announced a shipment in 2025 and another shipment in 2026."
        events = [
            {
                **entity("Event", f"Shipment in {year}", quote),
                "event_type": "shipment_announcement",
                "time_text": year,
            }
            for year in ("2025", "2026")
        ]
        result = {
            "entities": events,
            "relationships": [
                relation(
                    reference("SourceDocument", "document"),
                    "reports",
                    reference("Event", e["name"]),
                    quote,
                )
                for e in events
            ],
        }
        checked = extraction.validate_extraction(
            result, sample_chunk(quote), MAPPING, self.ontology
        )
        self.assertNotEqual(
            checked["entities"][0]["resolved_iri"], checked["entities"][1]["resolved_iri"]
        )
        result["entities"][1]["name"] = result["entities"][0]["name"]
        with self.assertRaisesRegex(ValueError, "Duplicate entity type/name"):
            extraction.validate_extraction(result, sample_chunk(quote), MAPPING, self.ontology)

    def test_supplied_names_resolve_to_internal_product_and_service_nodes(self):
        result = sample_result()
        result["entities"].append(entity("Service", "memory chips", sample_chunk()["text"]))
        result["relationships"][0]["supplied_service"] = ["memory chips"]
        with patch.object(
            extraction, "validate_records", wraps=extraction.validate_records
        ) as validate:
            extraction.validate_extraction(result, sample_chunk(), MAPPING, self.ontology)
        _, nodes, relationships = validate.call_args.args
        by_kind = {n["labels"][0]: n for n in nodes if n["labels"][0] in ("Product", "Service")}
        self.assertNotEqual(by_kind["Product"]["id"], by_kind["Service"]["id"])
        for field, kind in (("supplied_product", "Product"), ("supplied_service", "Service")):
            self.assertEqual(
                relationships[0]["properties"][field], [by_kind[kind]["properties"]["iri"]]
            )

    def test_response_rejects_truncation_refusal_invalid_json_and_errors(self):
        self.assertEqual(
            extraction.decode_response(response()), {"entities": [], "relationships": []}
        )
        failures = [
            {"error": {"message": "failure"}},
            {},
            [],
            response(finish="length"),
            response(finish="content_filter"),
            response(finish="tool_calls"),
        ]
        for field, value in (
            ("refusal", "declined"),
            ("content", "```json\n{}\n```"),
            ("content", None),
            ("tool_calls", [{}]),
        ):
            bad = response()
            bad["choices"][0]["message"][field] = value
            failures.append(bad)
        for bad in failures:
            with self.subTest(response=bad):
                with self.assertRaises(ValueError):
                    extraction.decode_response(bad)

    def test_transport_is_mocked_and_does_not_retry_or_expose_provider_errors(self):
        request = extraction.build_request(sample_chunk(), self.ontology, self.prompt)
        with patch.object(
            client, "urlopen", return_value=BytesIO(json.dumps(response()).encode())
        ) as send:
            self.assertEqual(client.call_openrouter(request, "test-key")["id"], "mock-response")
            sent = send.call_args.args[0]
            self.assertEqual(sent.full_url, client.ENDPOINT)
            self.assertEqual(sent.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(json.loads(sent.data), request)
            self.assertEqual(send.call_count, 1)
        for error in (
            HTTPError(client.ENDPOINT, 429, "sensitive", {}, BytesIO(b"test-key")),
            URLError("test-key"),
            TimeoutError("test-key"),
            OSError("test-key"),
        ):
            with patch.object(client, "urlopen", side_effect=error) as send:
                with self.assertRaises(RuntimeError) as caught:
                    client.call_openrouter(request, "test-key")
                self.assertNotIn("test-key", str(caught.exception))
                self.assertEqual(send.call_count, 1)


class ExtractionStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "filings.duckdb"
        with duckdb.connect(str(self.database)) as db:
            db.execute(SCHEMA)
            for i, role in enumerate(("primary", "primary", "exhibit")):
                doc, section, chunk = f"NVDA/{i}/nvda.htm", f"section-{i}", f"chunk-{i}"
                text = f"NVIDIA supplies Micron Technology with chips. Paragraph {i}."
                db.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        doc,
                        "NVDA",
                        str(i),
                        "10-K",
                        "10-K" if role == "primary" else "EX-99.1",
                        role,
                        date(2026 - i, 2, 25),
                        f"https://example.test/NVDA/{i}",
                        "a" * 64,
                        "b" * 64,
                        "unused",
                        "processed",
                        json.dumps({"source": {"report_date": "2026-01-25"}}),
                    ],
                )
                db.execute(
                    "INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [section, doc, 0, "NVIDIA business", "sec_item", None, "[]", text],
                )
                db.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [chunk, doc, section, 0, text, len(text), False, False],
                )
        mask_chunks(self.database, COMPANIES)

    def test_unlimited_selection_covers_every_issuer_and_document_role(self):
        with duckdb.connect(str(self.database)) as db:
            db.execute("UPDATE documents SET ticker='AMD' WHERE accession='2'")
        chunks, _ = extraction.select_chunks(self.database, None, None, role="all")
        self.assertEqual({c["chunk_id"] for c in chunks}, {"chunk-0", "chunk-1", "chunk-2"})
        self.assertEqual({c["ticker"] for c in chunks}, {"NVDA", "AMD"})
        self.assertEqual({c["role"] for c in chunks}, {"primary", "exhibit"})
        amd, _ = extraction.select_chunks(self.database, "AMD", None, role="all")
        self.assertEqual([c["chunk_id"] for c in amd], ["chunk-2"])
        self.assertEqual(amd[0]["issuer"], "Company C")

    def test_selection_and_stale_masks_fail_before_any_request(self):
        chunks, _ = extraction.select_chunks(self.database, "NVDA", 1, "10-K")
        self.assertEqual([c["chunk_id"] for c in chunks], ["chunk-0"])
        chunks, _ = extraction.select_chunks(self.database, "NVDA", 3, role="exhibit")
        self.assertEqual([c["chunk_id"] for c in chunks], ["chunk-2"])
        chunks, _ = extraction.select_chunks(
            self.database, "NVDA", 3, document_id="NVDA/1/nvda.htm"
        )
        self.assertEqual([c["chunk_id"] for c in chunks], ["chunk-1"])
        with duckdb.connect(str(self.database)) as db:
            db.execute("UPDATE chunks SET masked_text=NULL WHERE chunk_id='chunk-0'")
        with patch.object(extraction, "call_openrouter") as send:
            with self.assertRaisesRegex(ValueError, "stale masking"):
                extraction.run_extraction(self.database, "NVDA", self.root / "output", dry_run=True)
            send.assert_not_called()
        self.assertFalse((self.root / "output").exists())

    def test_preview_and_mocked_run_preserve_database_and_save_provenance(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(extraction, "call_openrouter") as send,
        ):
            preview = extraction.run_extraction(
                self.database, "NVDA", self.root / "output", limit=2, dry_run=True
            )
            send.assert_not_called()
        manifest = json.loads((preview / "manifest.json").read_text())
        self.assertEqual((manifest["status"], manifest["selected_chunks"]), ("preview", 2))
        request = json.loads((preview / "00001.json").read_text())["request"]
        self.assertNotIn("NVIDIA", json.dumps(request))
        with (
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
            patch.object(extraction, "call_openrouter", return_value=response()) as send,
        ):
            output = extraction.run_extraction(self.database, "NVDA", self.root / "output", limit=2)
            self.assertEqual(send.call_count, 2)
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual((manifest["status"], manifest["validated_chunks"]), ("complete", 2))
        record = json.loads((output / "00001.json").read_text())
        self.assertEqual(record["source_sha256"], "a" * 64)
        self.assertEqual(record["response"]["usage"]["total_tokens"], 100)
        self.assertEqual(record["validated"]["entities"], [])
        self.assertNotIn("test-key", (output / "00001.json").read_text())
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_failed_response_is_saved_and_stops_before_next_chunk(self):
        with (
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
            patch.object(
                extraction, "call_openrouter", return_value=response(finish="length")
            ) as send,
        ):
            with self.assertRaisesRegex(RuntimeError, "Extraction stopped"):
                extraction.run_extraction(self.database, "NVDA", self.root / "output", limit=2)
            self.assertEqual(send.call_count, 1)
        output = next((self.root / "output").iterdir())
        manifest = json.loads((output / "manifest.json").read_text())
        record = json.loads((output / "00001.json").read_text())
        self.assertEqual((manifest["status"], manifest["validated_chunks"]), ("failed", 0))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["response"]["choices"][0]["finish_reason"], "length")
        self.assertFalse((output / "00002.json").exists())

    def test_missing_key_and_invalid_selection_do_not_create_runs(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(extraction, "call_openrouter") as send,
        ):
            with self.assertRaisesRegex(ValueError, "OPENROUTER_API_KEY"):
                extraction.run_extraction(self.database, "NVDA", self.root / "output")
            for kwargs in ({"limit": 0}, {"form": "nothing"}, {"document_id": "' OR 1=1 --"}):
                with self.assertRaises(ValueError):
                    extraction.run_extraction(
                        self.database, "NVDA", self.root / "output", dry_run=True, **kwargs
                    )
            send.assert_not_called()
        self.assertFalse((self.root / "output").exists())


if __name__ == "__main__":
    unittest.main()
