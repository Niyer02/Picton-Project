import hashlib
import json
import os
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from neo4j.exceptions import Neo4jError
from rdflib import OWL, RDF, Graph, URIRef

from picton_graph.config import ONTOLOGY
from picton_graph.graph.connection import connect
from picton_graph.graph.ontology import parse_ontology, schema_statements, visualization_query
from picton_graph.graph.validation import validate_graph, validate_records


def node(identifier, *labels, **properties):
    return {
        "id": identifier,
        "labels": list(labels),
        "properties": {"iri": "urn:test:" + identifier, **properties},
    }


def edge(source, kind, target, **properties):
    return {"source": source, "type": kind, "target": target, "properties": properties}


class OntologyTests(unittest.TestCase):
    def setUp(self):
        self.schema = parse_ontology(ONTOLOGY / "picton.owl")

    def test_domain_schema_and_native_constraints(self):
        self.assertEqual(
            set(self.schema["classes"]),
            {"Company", "Product", "Service", "SourceDocument", "Topic", "Sector", "Event"},
        )
        self.assertEqual(len(self.schema["relationships"]), 17)
        self.assertEqual(len(self.schema["data_properties"]), 6)
        self.assertEqual(len(self.schema["annotations"]), 4)
        self.assertEqual(len(self.schema["disjoint_classes"]), 20)
        self.assertEqual(self.schema["ontology"]["source_triples"], 226)
        statements = schema_statements(self.schema)
        self.assertEqual(
            sum(statement.startswith("CREATE CONSTRAINT") for statement in statements), 7
        )
        self.assertFalse(
            any("OntologyTerm" in statement or "CREATE (" in statement for statement in statements)
        )

    def test_union_and_inverse_rules(self):
        relationships = self.schema["relationships"]
        self.assertEqual(
            set(relationships["has_topic"]["domain_any_of"]),
            {"Company", "Product", "Service", "SourceDocument", "Event"},
        )
        self.assertEqual(
            set(relationships["mentions"]["range_any_of"]),
            {"Company", "Product", "Service", "Sector"},
        )
        for name, rule in relationships.items():
            inverse = relationships[rule["inverse"]]
            self.assertEqual(inverse["inverse"], name)
            self.assertEqual(rule["domain_any_of"], inverse["range_any_of"])
            self.assertEqual(rule["range_any_of"], inverse["domain_any_of"])
        self.assertTrue(relationships["supplies_to"]["irreflexive"])
        self.assertTrue(relationships["buys_from"]["irreflexive"])

    def test_competition_is_symmetric_and_can_coexist_with_supply(self):
        rule = self.schema["relationships"]["competes_with"]
        self.assertEqual(rule["domain_any_of"], ["Company"])
        self.assertEqual(rule["range_any_of"], ["Company"])
        self.assertTrue(rule["symmetric"])
        self.assertTrue(rule["irreflexive"])
        self.assertEqual(rule["inverse"], "competes_with")
        nodes = [node("a", "Company"), node("b", "Company"), node("doc", "SourceDocument")]
        for source, target in (("a", "b"), ("b", "a")):
            validate_records(
                self.schema,
                nodes,
                [
                    edge(
                        source,
                        "competes_with",
                        target,
                        supported_by="urn:test:doc",
                        evidence_text="Synthetic competition evidence.",
                    ),
                    edge("a", "supplies_to", "b"),
                ],
            )

    def test_symmetric_property_with_separate_inverse_is_rejected(self):
        graph = Graph().parse(ONTOLOGY / "picton.owl", format="xml")
        competition = URIRef(self.schema["relationships"]["competes_with"]["iri"])
        supply = URIRef(self.schema["relationships"]["supplies_to"]["iri"])
        graph.add((competition, OWL.inverseOf, supply))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.owl"
            graph.serialize(destination=path, format="xml")
            with self.assertRaisesRegex(ValueError, "separate named inverse"):
                parse_ontology(path)

    def test_unsupported_owl_is_rejected(self):
        graph = Graph().parse(ONTOLOGY / "picton.owl", format="xml")
        subject = URIRef(self.schema["relationships"]["supplies_to"]["iri"])
        graph.add((subject, RDF.type, OWL.TransitiveProperty))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unsupported.owl"
            graph.serialize(destination=path, format="xml")
            with self.assertRaisesRegex(ValueError, "Unmapped OWL"):
                parse_ontology(path)

    def test_all_class_pairs_and_union_alternatives(self):
        classes = sorted(self.schema["classes"])
        for index, first in enumerate(classes):
            for second in classes[index + 1 :]:
                with self.subTest(first=first, second=second):
                    if {first, second} == {"Topic", "Sector"}:
                        validate_records(self.schema, [node("both", first, second)], [])
                    else:
                        with self.assertRaisesRegex(ValueError, "disjoint"):
                            validate_records(self.schema, [node("both", first, second)], [])
        for label in ("Company", "Product", "Service", "SourceDocument", "Event"):
            validate_records(
                self.schema,
                [node("source", label), node("topic", "Topic")],
                [edge("source", "has_topic", "topic")],
            )
        for label in ("Company", "Product", "Service", "Sector"):
            validate_records(
                self.schema,
                [node("doc", "SourceDocument"), node("target", label)],
                [edge("doc", "mentions", "target")],
            )

    def test_direct_relationships_with_evidence_and_optional_multivalued_properties(self):
        nodes = [
            node("supplier", "Company", ticker=["AAA", "AAB"]),
            node("customer", "Company"),
            node("product", "Product"),
            node("service", "Service"),
            node("doc", "SourceDocument", url="https://example.com/filing"),
            node("event", "Event", occurred_at=datetime(2026, 11, 3, tzinfo=timezone.utc)),
        ]
        relationships = [
            edge(
                "supplier",
                "supplies_to",
                "customer",
                supported_by="urn:test:doc",
                supplied_product="urn:test:product",
                supplied_service="urn:test:service",
                evidence_text="Synthetic test evidence.",
            ),
            edge("supplier", "offers", "product"),
            edge("supplier", "provides", "service"),
            edge("doc", "reports", "event"),
            edge("event", "concerns", "customer"),
        ]
        validate_records(self.schema, nodes, relationships)
        validate_records(self.schema, nodes, [edge("customer", "buys_from", "supplier")])

    def test_invalid_facts_are_rejected(self):
        cases = [
            ([node("a", "Company")], [edge("a", "supplies_to", "a")], "itself"),
            ([node("a", "Company")], [edge("a", "buys_from", "a")], "itself"),
            ([node("a", "Company")], [edge("a", "competes_with", "a")], "itself"),
            (
                [node("a", "Company"), node("p", "Product")],
                [edge("a", "competes_with", "p")],
                "target class",
            ),
            (
                [node("a", "Company"), node("p", "Product")],
                [edge("p", "competes_with", "a")],
                "source class",
            ),
            (
                [node("a", "Company"), node("b", "Company"), node("p", "Product")],
                [edge("a", "competes_with", "b", supplied_product="urn:test:p")],
                "supply relationships only",
            ),
            (
                [node("a", "Company"), node("p", "Product")],
                [edge("a", "supplies_to", "p")],
                "target class",
            ),
            (
                [node("a", "Company"), node("b", "Company")],
                [edge("a", "concerns", "b")],
                "source class",
            ),
            ([node("a", "Company", ticker=123)], [], "datatype"),
            ([node("e", "Event", occurred_at="2026-11-03")], [], "datatype"),
            ([node("a", "Company", event_type="earnings_miss")], [], "not defined"),
            ([node("a", "Company", iri="")], [], "identifier"),
            ([node("a", "Company"), node("b", "Product", iri="urn:test:a")], [], "Duplicate"),
            ([node("a", "OntologyTerm")], [], "unknown domain labels"),
            ([node("a", "Company")], [edge("a", "made_up", "a")], "Unknown relationship"),
            (
                [node("a", "Company"), node("b", "Company")],
                [edge("a", "supplies_to", "b", supported_by="urn:test:a")],
                "SourceDocument",
            ),
            (
                [node("a", "Company"), node("b", "Company")],
                [edge("a", "supplies_to", "b", unsupported_field="x")],
                "Unknown relationship property",
            ),
        ]
        for nodes, relationships, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_records(self.schema, nodes, relationships)

    def test_import_metadata_preserves_domain_validation(self):
        context = {
            "source": {"chunk_id": "doc#chunk", "document_id": "doc"},
            "evidence": {
                "match_type": "exact",
                "original_quote": "proof",
                "original_start": 1,
                "original_end": 6,
                "masked_start": 1,
                "masked_end": 6,
            },
            "time_text": "fiscal 2026",
        }
        metadata = {"provenance_json": json.dumps([context]), "assertion_id": "a" * 64}
        nodes = [
            node("a", "Company", provenance_json=metadata["provenance_json"]),
            node("b", "Company"),
            node("doc", "SourceDocument"),
        ]
        relationship = edge(
            "a", "supplies_to", "b", **metadata, supported_by="urn:test:doc", evidence_text="proof"
        )
        validate_records(self.schema, nodes, [relationship])
        with self.assertRaisesRegex(ValueError, "Duplicate assertion_id"):
            validate_records(self.schema, nodes, [relationship, relationship])
        for changes, message in (
            ({"unsupported_field": "x"}, "Unknown relationship property"),
            ({"supported_by": "urn:test:a"}, "SourceDocument"),
            ({"assertion_id": "A" * 64}, "SHA-256"),
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, message):
                validate_records(
                    self.schema, nodes, [edge("a", "supplies_to", "b", **(metadata | changes))]
                )
        for key in metadata:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "together"):
                validate_records(
                    self.schema, nodes, [edge("a", "supplies_to", "b", **{key: metadata[key]})]
                )
        with self.assertRaisesRegex(ValueError, "not defined"):
            validate_records(self.schema, [node("a", "Company", assertion_id="a" * 64)], [])

    def test_invalid_provenance_is_rejected(self):
        context = {
            "source": {"chunk_id": "doc#chunk", "document_id": "doc"},
            "evidence": None,
            "time_text": None,
        }
        validate_records(
            self.schema, [node("doc", "SourceDocument", provenance_json=json.dumps([context]))], []
        )
        evidence = {
            "match_type": "exact",
            "original_quote": "proof",
            "original_start": 1,
            "original_end": 6,
            "masked_start": 1,
            "masked_end": 6,
        }
        bad_values = [
            None,
            [],
            "not json",
            "[]",
            "{}",
            "[null]",
            json.dumps([context | {"extra": 1}]),
            json.dumps([context | {"source": {"document_id": "doc"}}]),
            json.dumps([context | {"time_text": 2026}]),
            json.dumps([context | {"evidence": {}}]),
        ]
        for change in (
            {"original_start": -1},
            {"masked_start": True},
            {"masked_end": 1},
            {"original_end": 7},
            {"original_quote": ""},
            {"match_type": "guessed"},
        ):
            bad_values.append(json.dumps([context | {"evidence": evidence | change}]))
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_records(
                    self.schema, [node("doc", "SourceDocument", provenance_json=value)], []
                )

    def test_node_provenance_references_require_valid_exclusive_metadata(self):
        reference = "sha256:" + "a" * 64
        for label in self.schema["classes"]:
            validate_records(self.schema, [node("shared", label, provenance_ref=reference)], [])
        for value in (None, "", "a" * 64, "sha256:" + "A" * 64):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_records(self.schema, [node("shared", "Company", provenance_ref=value)], [])
        context = {
            "source": {"chunk_id": "c", "document_id": "d"},
            "evidence": None,
            "time_text": None,
        }
        with self.assertRaisesRegex(ValueError, "not both"):
            validate_records(
                self.schema,
                [
                    node(
                        "shared",
                        "Company",
                        provenance_ref=reference,
                        provenance_json=json.dumps([context]),
                    )
                ],
                [],
            )
        with self.assertRaisesRegex(ValueError, "Unknown relationship property"):
            validate_records(
                self.schema,
                [node("a", "Company"), node("b", "Company")],
                [edge("a", "competes_with", "b", provenance_ref=reference)],
            )

    def grouped_fixture(self):
        nodes = [
            node("a", "Company"),
            node("b", "Company"),
            node("doc", "SourceDocument"),
            node("doc2", "SourceDocument"),
            node("p", "Product"),
        ]
        contexts = []
        for index, (source, target, doc) in enumerate((("a", "b", "doc"), ("b", "a", "doc2"))):
            quote = f"proof {index}"
            contexts.append(
                {
                    "source": {"chunk_id": doc + "#chunk", "document_id": doc},
                    "evidence": {
                        "match_type": "exact",
                        "original_quote": quote,
                        "original_start": 0,
                        "original_end": len(quote),
                        "masked_start": 0,
                        "masked_end": len(quote),
                    },
                    "time_text": str(2025 + index),
                    "assertion": {
                        "id": str(index) * 64,
                        "source": "urn:test:" + source,
                        "target": "urn:test:" + target,
                        "properties": {"supported_by": "urn:test:" + doc, "evidence_text": quote},
                    },
                }
            )
        identity = {"source": "urn:test:a", "type": "competes_with", "target": "urn:test:b"}
        identifier = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        relationship = edge(
            "a",
            "competes_with",
            "b",
            relationship_id=identifier,
            evidence_count=2,
            evidence_text=["proof 0", "proof 1"],
            supported_by=["urn:test:doc", "urn:test:doc2"],
            provenance_json=json.dumps(contexts),
        )
        return nodes, relationship, contexts

    def test_grouped_evidence_uses_iris_even_when_storage_ids_differ(self):
        nodes, relationship, contexts = self.grouped_fixture()
        validate_records(self.schema, nodes, [relationship])
        self.assertEqual([c["time_text"] for c in contexts], ["2025", "2026"])
        self.assertNotEqual(nodes[0]["id"], contexts[0]["assertion"]["source"])

    def test_grouped_evidence_cannot_lose_or_misbind_sources(self):
        nodes, relationship, contexts = self.grouped_fixture()
        changes = [
            ({"evidence_count": 1}, "evidence_count"),
            ({"evidence_count": True}, "evidence_count"),
            ({"relationship_id": "f" * 64}, "canonical endpoints"),
            ({"assertion_id": "f" * 64}, "Grouped relationships"),
            ({"evidence_text": ["proof 0"]}, "unions"),
            ({"supported_by": ["urn:test:doc"]}, "unions"),
            ({"unsupported_field": "x"}, "Unknown relationship property"),
        ]
        for change, message in changes:
            changed = deepcopy(relationship)
            changed["properties"].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                validate_records(self.schema, nodes, [changed])
        for field, value, message in (
            ("id", "0" * 64, "Duplicate assertion_id"),
            ("source", "urn:test:p", "endpoints"),
            ("properties", {"supported_by": "urn:test:p"}, "SourceDocument"),
            ("properties", {"evidence_text": "wrong"}, "original quote"),
        ):
            changed = deepcopy(relationship)
            changed_contexts = deepcopy(contexts)
            changed_contexts[1]["assertion"][field] = value
            changed["properties"]["provenance_json"] = json.dumps(changed_contexts)
            with (
                self.subTest(field=field, value=value),
                self.assertRaisesRegex(ValueError, message),
            ):
                validate_records(self.schema, nodes, [changed])

    def test_directed_group_cannot_contain_reverse_assertion(self):
        nodes, relationship, _ = self.grouped_fixture()
        relationship["type"] = "supplies_to"
        identity = {"source": "urn:test:a", "type": "supplies_to", "target": "urn:test:b"}
        relationship["properties"]["relationship_id"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "endpoints"):
            validate_records(self.schema, nodes, [relationship])

    def test_grouped_aliases_preserve_original_ids_and_validate_resolved_types(self):
        nodes, relationship, contexts = self.grouped_fixture()
        relationship["type"] = "offers"
        relationship["target"] = "p"
        contexts = [contexts[0]]
        contexts[0]["assertion"]["target"] = "urn:old:gpu"
        contexts[0]["assertion"]["resolved"] = {"urn:old:gpu": "urn:test:p"}
        identity = {"source": "urn:test:a", "type": "offers", "target": "urn:test:p"}
        properties = relationship["properties"]
        properties.update(
            relationship_id=hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            evidence_count=1,
            evidence_text=["proof 0"],
            supported_by=["urn:test:doc"],
            provenance_json=json.dumps(contexts),
        )
        validate_records(self.schema, nodes, [relationship])
        for aliases in (
            {"urn:old:gpu": "urn:test:b"},
            {"unrelated": "urn:test:p"},
            {"urn:old:gpu": None},
        ):
            changed = deepcopy(relationship)
            changed_contexts = deepcopy(contexts)
            changed_contexts[0]["assertion"]["resolved"] = aliases
            changed["properties"]["provenance_json"] = json.dumps(changed_contexts)
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                validate_records(self.schema, nodes, [changed])

    def test_grouped_supply_items_use_canonical_references_without_losing_originals(self):
        nodes, relationship, contexts = self.grouped_fixture()
        relationship["type"] = "supplies_to"
        contexts = [contexts[0]]
        contexts[0]["assertion"]["properties"]["supplied_product"] = ["urn:old:gpu"]
        contexts[0]["assertion"]["resolved"] = {"urn:old:gpu": "urn:test:p"}
        identity = {"source": "urn:test:a", "type": "supplies_to", "target": "urn:test:b"}
        properties = relationship["properties"]
        properties.update(
            relationship_id=hashlib.sha256(
                json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            evidence_count=1,
            evidence_text=["proof 0"],
            supported_by=["urn:test:doc"],
            supplied_product=["urn:test:p"],
            provenance_json=json.dumps(contexts),
        )
        validate_records(self.schema, nodes, [relationship])
        properties["supplied_product"] = ["urn:old:gpu"]
        with self.assertRaisesRegex(ValueError, "Product node"):
            validate_records(self.schema, nodes, [relationship])

    def compact_fixture(self):
        nodes, relationship, contexts = self.grouped_fixture()
        for document in nodes[2:4]:
            document["properties"]["url"] = "https://example.com/" + document["id"]
        records = [
            {
                "id": context["assertion"]["id"],
                "evidence_text": context["evidence"]["original_quote"],
                "document": "https://example.com/" + context["source"]["document_id"],
                "document_type": "10-K",
                "document_date": "2025-02-28",
                "time_text": context["time_text"],
            }
            for context in contexts
        ]
        for key in ("provenance_json", "evidence_text", "supported_by"):
            relationship["properties"].pop(key)
        relationship["properties"]["evidence"] = json.dumps(records)
        return nodes, relationship, records

    def test_compact_evidence_preserves_document_dates_and_optional_qualifiers(self):
        nodes, relationship, records = self.compact_fixture()
        records[1].update(document_date=None, document_type="EX-99.1")
        records[1].pop("time_text")
        relationship["properties"]["evidence"] = json.dumps(records)
        validate_records(self.schema, nodes, [relationship])
        relationship["type"] = "supplies_to"
        identity = {"source": "urn:test:a", "type": "supplies_to", "target": "urn:test:b"}
        relationship["properties"]["relationship_id"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        nodes.append(node("s", "Service"))
        records[0]["supplied_product"] = ["urn:test:p"]
        records[1]["supplied_service"] = ["urn:test:s"]
        relationship["properties"].update(
            evidence=json.dumps(records),
            supplied_product=["urn:test:p"],
            supplied_service=["urn:test:s"],
        )
        validate_records(self.schema, nodes, [relationship])
        relationship["properties"].pop("supplied_product")
        with self.assertRaisesRegex(ValueError, "unions"):
            validate_records(self.schema, nodes, [relationship])
        records[0]["supplied_product"] = ["urn:test:s"]
        relationship["properties"]["evidence"] = json.dumps(records)
        with self.assertRaisesRegex(ValueError, "Product node"):
            validate_records(self.schema, nodes, [relationship])

    def test_compact_evidence_rejects_malformed_or_unlinked_records(self):
        nodes, relationship, records = self.compact_fixture()
        changes = [
            {"id": "A" * 64},
            {"id": records[1]["id"]},
            {"evidence_text": " "},
            {"evidence_text": 123},
            {"document": "https://example.com/missing"},
            {"document": None},
            {"document_type": ""},
            {"document_type": 10},
            {"document_date": "2025-02-29"},
            {"document_date": "20250228"},
            {"document_date": "2025-02-28T00:00:00Z"},
            {"document_date": 20250228},
            {"document_date": ""},
            {"document_date": "2025-13-01"},
            {"time_text": None},
            {"time_text": ""},
            {"masked_start": 0},
            {"supplied_product": []},
            {"supplied_product": "urn:test:p"},
            {"supplied_product": ["urn:test:p"]},
        ]
        invalid_records = [[records[0] | change, records[1]] for change in changes]
        invalid_records.extend(
            [
                [{k: v for k, v in records[0].items() if k != key}, records[1]]
                for key in ("id", "evidence_text", "document", "document_type", "document_date")
            ]
        )
        invalid_records.extend([[], {}, [None], [records[0], records[0]]])
        for value in invalid_records:
            with self.subTest(value=value), self.assertRaises(ValueError):
                changed = deepcopy(relationship)
                changed["properties"]["evidence"] = json.dumps(value)
                validate_records(self.schema, nodes, [changed])
        for value in (None, [], "not JSON"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                changed = deepcopy(relationship)
                changed["properties"]["evidence"] = value
                validate_records(self.schema, nodes, [changed])

    def test_compact_evidence_rejects_legacy_fields_bad_counts_and_bad_identity(self):
        nodes, relationship, _ = self.compact_fixture()
        for change in (
            {"provenance_json": "[]"},
            {"evidence_text": []},
            {"supported_by": []},
            {"assertion_id": "a" * 64},
            {"evidence_count": True},
            {"evidence_count": 1},
            {"relationship_id": "f" * 64},
            {"unknown": "value"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                changed = deepcopy(relationship)
                changed["properties"].update(change)
                validate_records(self.schema, nodes, [changed])
        for key in ("relationship_id", "evidence_count"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Grouped relationships"):
                changed = deepcopy(relationship)
                changed["properties"].pop(key)
                validate_records(self.schema, nodes, [changed])
        relationship["source"], relationship["target"] = (
            relationship["target"],
            relationship["source"],
        )
        with self.assertRaisesRegex(ValueError, "canonical endpoint order"):
            validate_records(self.schema, nodes, [relationship])

    def test_compact_and_legacy_evidence_can_coexist_without_duplicate_assertion_ids(self):
        nodes, compact, records = self.compact_fixture()
        _, _, contexts = self.grouped_fixture()
        legacy = edge(
            "a",
            "supplies_to",
            "b",
            assertion_id="f" * 64,
            evidence_text="proof 0",
            supported_by="urn:test:doc",
            provenance_json=json.dumps(
                [{k: v for k, v in contexts[0].items() if k != "assertion"}]
            ),
        )
        for relationships in ([compact, legacy], [legacy, compact]):
            validate_records(self.schema, nodes, relationships)
        legacy["properties"]["assertion_id"] = records[0]["id"]
        for relationships in ([compact, legacy], [legacy, compact]):
            with self.assertRaisesRegex(ValueError, "Duplicate assertion_id"):
                validate_records(self.schema, nodes, relationships)


@unittest.skipUnless(
    os.environ.get("NEO4J_INTEGRATION") == "1", "Set NEO4J_INTEGRATION=1 for live database checks."
)
class Neo4jSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = parse_ontology(ONTOLOGY / "picton.owl")
        self.driver = connect()
        self.session = self.driver.session(database="neo4j")
        self.addCleanup(self.driver.close)
        self.addCleanup(self.session.close)

    def test_full_ontology_visualization_is_virtual(self):
        before_nodes = self.session.run("MATCH (n) RETURN count(n)").single()[0]
        before_edges = self.session.run("MATCH ()-[r]->() RETURN count(r)").single()[0]
        records = list(self.session.run(visualization_query(self.schema)))
        self.assertEqual(len(records), 31)
        nodes = {n.element_id: n for row in records for n in (row["source"], row["target"])}
        self.assertEqual(len(nodes), 7)
        self.assertEqual(
            {tuple(n.labels) for n in nodes.values()}, {(name,) for name in self.schema["classes"]}
        )
        expected_edges = {
            (source, kind, target)
            for kind, rule in self.schema["relationships"].items()
            for source in rule["domain_any_of"]
            for target in rule["range_any_of"]
        }
        self.assertEqual(
            {(r["source"]["name"], r["rel"].type, r["target"]["name"]) for r in records},
            expected_edges,
        )
        self.assertTrue(
            all("data_properties" in n and "disjoint_with" in n for n in nodes.values())
        )
        competition = next(row["rel"] for row in records if row["rel"].type == "competes_with")
        self.assertTrue(competition["symmetric"])
        self.assertTrue(competition["irreflexive"])
        self.assertEqual(self.session.run("MATCH (n) RETURN count(n)").single()[0], before_nodes)
        self.assertEqual(
            self.session.run("MATCH ()-[r]->() RETURN count(r)").single()[0], before_edges
        )

    def test_constraints_are_idempotent_and_create_no_data(self):
        before = self.session.run("MATCH (n) RETURN count(n)").single()[0]
        for statement in schema_statements(self.schema):
            self.session.run(statement).consume()
        constraints = list(
            self.session.run("""
            SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
            WHERE name STARTS WITH 'picton_' RETURN type, labelsOrTypes, properties
        """)
        )
        self.assertEqual(
            {tuple(r["labelsOrTypes"]) for r in constraints},
            {(label,) for label in self.schema["classes"]},
        )
        self.assertEqual(len(constraints), 7)
        self.assertTrue(
            all(r["type"] == "UNIQUENESS" and r["properties"] == ["iri"] for r in constraints)
        )
        self.assertEqual(self.session.run("MATCH (n) RETURN count(n)").single()[0], before)

    def test_database_rejects_duplicate_company_identifier(self):
        iri = "urn:test:duplicate:" + uuid4().hex
        with self.assertRaises(Neo4jError) as caught:
            with self.session.begin_transaction() as tx:
                tx.run("CREATE (:Company {iri: $iri})", iri=iri).consume()
                tx.run("CREATE (:Company {iri: $iri})", iri=iri).consume()
        self.assertIn("ConstraintValidationFailed", caught.exception.code)
        self.assertEqual(
            self.session.run("MATCH (n {iri: $iri}) RETURN count(n)", iri=iri).single()[0], 0
        )

    def test_valid_direct_data_and_invalid_write_rollback(self):
        prefix = "urn:test:" + uuid4().hex
        with self.session.begin_transaction() as tx:
            tx.run(
                """
                CREATE (a:Company {iri: $prefix + ':a'}),
                       (b:Company {iri: $prefix + ':b'}),
                       (e:Event {iri: $prefix + ':event', occurred_at: datetime('2026-11-03T12:00:00Z')}),
                       (a)-[:supplies_to]->(b), (e)-[:concerns]->(b)
            """,
                prefix=prefix,
            ).consume()
            validate_graph(tx, self.schema)
            self.assertEqual(
                tx.run(
                    "MATCH (a:Company {iri: $iri})-[:supplies_to]->(:Company) RETURN count(a)",
                    iri=prefix + ":a",
                ).single()[0],
                1,
            )
            tx.rollback()
        with self.assertRaisesRegex(ValueError, "itself"):
            with self.session.begin_transaction() as tx:
                tx.run("CREATE (a:Company {iri: $iri})-[:supplies_to]->(a)", iri=prefix).consume()
                validate_graph(tx, self.schema)
        self.assertEqual(
            self.session.run(
                "MATCH (n) WHERE n.iri STARTS WITH $prefix RETURN count(n)", prefix=prefix
            ).single()[0],
            0,
        )

    def test_competition_is_retrieved_from_either_endpoint_and_self_link_rolls_back(self):
        prefix = "urn:test:competition:" + uuid4().hex
        with self.session.begin_transaction() as tx:
            tx.run(
                """
                CREATE (a:Company {iri: $prefix + ':a'}), (b:Company {iri: $prefix + ':b'}),
                       (a)-[:competes_with]->(b)
            """,
                prefix=prefix,
            ).consume()
            validate_graph(tx, self.schema)
            for source, target in ((":a", ":b"), (":b", ":a")):
                result = tx.run(
                    """
                    MATCH (:Company {iri: $iri})-[:competes_with]-(competitor:Company)
                    RETURN competitor.iri AS iri
                """,
                    iri=prefix + source,
                ).value()
                self.assertEqual(result, [prefix + target])
            tx.rollback()
        with self.assertRaisesRegex(ValueError, "competes_with cannot link a node to itself"):
            with self.session.begin_transaction() as tx:
                tx.run("CREATE (a:Company {iri: $iri})-[:competes_with]->(a)", iri=prefix).consume()
                validate_graph(tx, self.schema)
        self.assertEqual(
            self.session.run(
                "MATCH (n) WHERE n.iri STARTS WITH $prefix RETURN count(n)", prefix=prefix
            ).single()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
