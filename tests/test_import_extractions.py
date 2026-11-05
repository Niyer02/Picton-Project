import fcntl
import json
import re
import unittest
from collections import Counter
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import picton_graph.graph.importer as importer


class Result(list):
    def __init__(self, rows, nodes=0, edges=0, deleted=0, nodes_deleted=0):
        super().__init__(rows)
        self.counters = SimpleNamespace(
            nodes_created=nodes,
            relationships_created=edges,
            relationships_deleted=deleted,
            nodes_deleted=nodes_deleted,
        )

    def single(self, strict=False):
        if strict and len(self) != 1:
            raise ValueError("Not exactly one row")
        return self[0]

    def consume(self):
        return SimpleNamespace(counters=self.counters)


class Database:
    """Small transactional fake: real validation and generated queries run unchanged."""

    def __init__(self, schema):
        self.schema, self.nodes, self.edges, self.calls = schema, {}, {}, []
        self.fail_batch = None
        self.batches = 0
        self.missing_endpoint = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def session(self, **kwargs):
        return self

    def execute_read(self, operation, *args):
        return operation(self, *args)

    def execute_write(self, operation, *args):
        self.batches += 1
        if self.batches == self.fail_batch:
            raise RuntimeError("Simulated interruption")
        snapshot = deepcopy((self.nodes, self.edges))
        try:
            return operation(self, *args)
        except Exception:
            self.nodes, self.edges = snapshot
            raise

    def run(self, query, **parameters):
        self.calls.append((query, deepcopy(parameters)))
        if query.startswith("SHOW CONSTRAINTS"):
            return Result(
                [
                    {
                        "type": "UNIQUENESS",
                        "entityType": "NODE",
                        "labelsOrTypes": [label],
                        "properties": ["iri"],
                    }
                    for label in self.schema["classes"]
                ]
            )
        if "MATCH ()-[r:supplies_to|buys_from]" in query:
            return Result(
                [
                    {
                        field: edge["properties"].get(field)
                        for field in ("supplied_product", "supplied_service")
                    }
                    for edge in self.edges.values()
                    if edge["type"] in ("supplies_to", "buys_from")
                ]
            )
        if "UNWIND" not in query:
            records = self.edges.values() if "MATCH (a)-[r]" in query else self.nodes.values()
            return Result(deepcopy(list(records)))
        rows, created, matched = parameters["rows"], 0, 0
        if "SET r.evidence = row.properties.evidence" in query:
            for row in rows:
                for edge in self.edges.values():
                    if (
                        edge["storage_id"] == row["storage_id"]
                        and all(edge[field] == row[field] for field in ("source", "target", "type"))
                        and edge["properties"] in (row["previous_properties"], row["properties"])
                    ):
                        edge["properties"]["evidence"] = row["properties"]["evidence"]
                        for name in ("evidence_text", "provenance_json", "supported_by"):
                            edge["properties"].pop(name, None)
                        matched += edge["properties"] == row["properties"]
            return Result([{"matched": matched}])
        if "DELETE n" in query:
            for row in rows:
                for iri, node in list(self.nodes.items()):
                    connected = any(
                        iri in (edge["source"], edge["target"]) for edge in self.edges.values()
                    )
                    if (
                        node["storage_id"] == row["storage_id"]
                        and not connected
                        and all(
                            node[field] == row[field] for field in ("id", "labels", "properties")
                        )
                    ):
                        del self.nodes[iri]
                        matched += 1
            return Result([{"matched": matched}], nodes_deleted=matched)
        if "MATCH (n) WHERE elementId(n)" in query:
            ids = {row["storage_id"] for row in rows}
            return Result(
                deepcopy([node for node in self.nodes.values() if node["storage_id"] in ids])
            )
        if "elementId(r) = row.storage_id" in query and "DELETE r" not in query:
            return Result(
                [
                    {**deepcopy(edge), "group_id": row["group_id"]}
                    for row in rows
                    for edge in self.edges.values()
                    if edge["storage_id"] == row["storage_id"]
                ]
            )
        if "MERGE (n:" in query:
            label = re.search(r"MERGE \(n:(\w+)", query)[1]
            for row in rows:
                if row["id"] not in self.nodes:
                    self.nodes[row["id"]] = {
                        "id": row["id"],
                        "labels": [label],
                        "properties": {},
                        "storage_id": "node:" + row["id"],
                    }
                    created += 1
                properties = self.nodes[row["id"]]["properties"]
                if "row.archived_provenance_json" in query and (
                    properties.get("provenance_json") not in (None, row["archived_provenance_json"])
                    or properties.get("provenance_ref")
                    not in (None, row["properties"]["provenance_ref"])
                ):
                    continue
                properties.update(row["properties"])
                if "REMOVE n.provenance_json" in query:
                    self.nodes[row["id"]]["properties"].pop("provenance_json", None)
                matched += 1
            return Result([{"matched": matched}], nodes=created)
        if "DELETE r" in query:
            for row in rows:
                for key, edge in list(self.edges.items()):
                    if edge["storage_id"] == row["storage_id"] and all(
                        edge[field] == row[field]
                        for field in ("source", "target", "type", "properties")
                    ):
                        del self.edges[key]
                        matched += 1
            return Result([{"matched": matched}], deleted=matched)
        source_label, target_label = re.findall(r"[ab]:(\w+) \{iri:", query)
        kind = re.search(r"\[r:(\w+)", query)[1]
        if "OPTIONAL MATCH" in query:
            records = []
            for row in rows:
                if row["source"] not in self.nodes or row["target"] not in self.nodes:
                    continue
                matches = [
                    edge
                    for edge in self.edges.values()
                    if edge["type"] == kind
                    and (
                        (edge["source"], edge["target"]) == (row["source"], row["target"])
                        or (
                            "]-(b)" in query
                            and (edge["source"], edge["target"]) == (row["target"], row["source"])
                        )
                    )
                ]
                records.extend(
                    {**deepcopy(edge), "group_id": row["properties"]["relationship_id"]}
                    for edge in matches
                )
            return Result(records)
        for row in rows:
            if (
                self.missing_endpoint
                or row["source"] not in self.nodes
                or row["target"] not in self.nodes
            ):
                continue
            if (
                source_label not in self.nodes[row["source"]]["labels"]
                or target_label not in self.nodes[row["target"]]["labels"]
            ):
                continue
            key = row["properties"]["relationship_id"]
            if key not in self.edges:
                self.edges[key] = {
                    "source": row["source"],
                    "target": row["target"],
                    "type": kind,
                    "properties": deepcopy(row["properties"]),
                    "storage_id": "edge:" + key,
                }
                created += 1
            if self.edges[key]["properties"] == row["properties"]:
                matched += 1
        return Result([{"matched": matched}], edges=created)


class ImportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "schema.json").write_bytes(importer.SCHEMA_PATH.read_bytes())
        self.schema = json.loads((self.root / "schema.json").read_text())
        self.directory = self.root / "export"
        self.directory.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(importer, "SCHEMA_PATH", self.root / "schema.json").start()
        patch.object(importer, "ROOT", self.root).start()
        self.database = Database(self.schema)
        self.connect = patch.object(importer, "connect", return_value=self.database).start()
        quote = "Supplier supplies chips to Customer."
        evidence = {
            "match_type": "exact",
            "masked_start": 0,
            "masked_end": len(quote),
            "original_start": 0,
            "original_end": len(quote),
            "original_quote": quote,
        }
        nodes = [
            {
                "id": "doc",
                "labels": ["SourceDocument"],
                "properties": {
                    "iri": "doc",
                    "name": "filing",
                    "url": "https://example.com/filing.htm",
                },
            }
        ]
        for iri, kind in (
            ("supplier", "Company"),
            ("customer", "Company"),
            ("chips", "Product"),
            ("fabrication", "Service"),
        ):
            nodes.append(
                {
                    "id": iri,
                    "labels": [kind],
                    "properties": {"iri": iri, "name": iri},
                    "evidence": evidence,
                    "time_text": None,
                }
            )
        edge = {
            "source": "supplier",
            "target": "customer",
            "type": "supplies_to",
            "properties": {
                "supported_by": "doc",
                "evidence_text": quote,
                "supplied_product": ["chips"],
                "supplied_service": ["fabrication"],
            },
            "evidence": evidence,
            "time_text": "June 2025",
        }
        self.rows = [
            {
                "provenance": {
                    "chunk_id": "chunk-1",
                    "document_id": "filing",
                    "source_url": "https://example.com/filing.htm",
                    "document_type": "10-K",
                    "filing_date": "2025-07-01",
                },
                "nodes": nodes,
                "relationships": [edge],
            }
        ]
        self.write_export()

    def write_export(self):
        output = self.directory / "assertions.jsonl"
        output.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        manifest = {
            "status": "complete",
            "format": "graph-payload-jsonl-v1",
            "source_files": {"schema.json": importer.file_hash(self.root / "schema.json")},
            "source_counts": {"valid": len(self.rows)},
            "output_sha256": importer.file_hash(output),
            "exported_chunks": len(self.rows),
            "empty_extractions": 0,
            "unique_node_iris": len({n["id"] for row in self.rows for n in row["nodes"]}),
            "node_records_by_type": dict(
                Counter(n["labels"][0] for row in self.rows for n in row["nodes"])
            ),
            "relationship_assertions": sum(len(row["relationships"]) for row in self.rows),
            "relationship_assertions_by_type": dict(
                Counter(e["type"] for row in self.rows for e in row["relationships"])
            ),
        }
        importer.save_json(self.directory / "manifest.json", manifest)
        return manifest

    def desired_graph(self):
        _, originals, assertions, _ = importer.preflight(self.directory)
        nodes, aliases = importer.canonicalize_nodes(originals)
        edges = importer.group_assertions(self.schema, assertions, aliases)
        return nodes, [importer.consolidate_evidence(edge) for edge in edges], aliases

    def test_aggregated_provenance_and_stable_time_sensitive_assertion_ids(self):
        second = deepcopy(self.rows[0])
        second["provenance"]["chunk_id"] = "chunk-2"
        second["relationships"].append(
            {**deepcopy(second["relationships"][0]), "time_text": "July 2025"}
        )
        self.rows.append(second)
        self.write_export()
        _, nodes, edges, _ = importer.preflight(self.directory)
        self.assertEqual(len(json.loads(nodes["supplier"]["properties"]["provenance_json"])), 2)
        contexts = json.loads(nodes["doc"]["properties"]["provenance_json"])
        self.assertEqual(
            [(c["evidence"], c["time_text"]) for c in contexts], [(None, None), (None, None)]
        )
        self.assertEqual(len({edge["properties"]["assertion_id"] for edge in edges}), 3)
        self.assertEqual(edges, importer.preflight(self.directory)[2])
        edge = self.rows[0]["relationships"][0]
        self.assertNotEqual(
            importer.assertion_id("chunk", 0, edge),
            importer.assertion_id("chunk", 0, {**edge, "time_text": "July 2025"}),
        )
        self.assertEqual(edges[0]["properties"]["supplied_product"], ["chips"])
        self.assertEqual(edges[0]["properties"]["supplied_service"], ["fabrication"])

    def test_import_and_repeat_preserve_sources_extras_and_all_assertions(self):
        self.database.nodes["supplier"] = {
            "id": "supplier",
            "storage_id": "old-supplier",
            "labels": ["Company"],
            "properties": {"iri": "supplier", "name": "supplier", "exchange": "NASDAQ"},
        }
        before = {
            name: (self.directory / name).read_bytes()
            for name in ("assertions.jsonl", "manifest.json")
        }
        first = importer.import_export(self.directory, batch_size=2)
        second = importer.import_export(self.directory, batch_size=2)
        self.assertEqual((first["nodes_created"], first["relationships_created"]), (4, 1))
        self.assertEqual((second["nodes_created"], second["relationships_created"]), (0, 0))
        self.assertEqual((second["readback_nodes"], second["readback_relationships"]), (5, 1))
        self.assertEqual(self.database.nodes["supplier"]["properties"]["exchange"], "NASDAQ")
        for name, content in before.items():
            self.assertEqual((self.directory / name).read_bytes(), content)
        query, params = next((q, p) for q, p in self.database.calls if "MERGE (a)-" in q)
        self.assertIn("MATCH (a:Company {iri: row.source}), (b:Company {iri: row.target})", query)
        self.assertIn("relationship_id: row.properties.relationship_id", query)
        self.assertNotIn("supplies chips", query)
        self.assertEqual(
            params["rows"][0]["properties"]["supplied_product"], [self.desired_graph()[2]["chips"]]
        )

    def test_bad_checksum_and_manifest_counts_never_connect(self):
        for field, value in (
            ("output_sha256", "bad"),
            ("exported_chunks", 99),
            ("unique_node_iris", 99),
            ("relationship_assertions", 99),
            ("status", "running"),
        ):
            with self.subTest(field=field):
                manifest = self.write_export()
                manifest[field] = value
                importer.save_json(self.directory / "manifest.json", manifest)
                with self.assertRaises(ValueError):
                    importer.import_export(self.directory)
                self.connect.assert_not_called()

    def test_unknown_fields_bad_evidence_and_wrong_reference_types_never_connect(self):
        original = deepcopy(self.rows)
        mutations = [
            lambda row: row.update(unexpected=True),
            lambda row: row["nodes"][1]["properties"].update(unknown="oops"),
            lambda row: row["relationships"][0]["properties"].update(unknown="oops"),
            lambda row: row["relationships"][0]["properties"].update(
                supplied_product=["fabrication"]
            ),
            lambda row: row["nodes"][1]["evidence"].update(original_end=999),
            lambda row: row["relationships"][0].update(type="buys_from"),
            lambda row: row["nodes"][1]["properties"].update(provenance_ref="sha256:" + "0" * 64),
        ]
        for mutate in mutations:
            self.rows = deepcopy(original)
            mutate(self.rows[0])
            self.write_export()
            with self.assertRaises(ValueError):
                importer.import_export(self.directory)
            self.connect.assert_not_called()

    def test_repeated_iri_conflict_is_rejected_before_connect(self):
        second = deepcopy(self.rows[0])
        second["provenance"]["chunk_id"] = "chunk-2"
        second["nodes"][1]["properties"]["name"] = "Different company"
        self.rows.append(second)
        self.write_export()
        with self.assertRaisesRegex(ValueError, "conflicting domain"):
            importer.import_export(self.directory)
        self.connect.assert_not_called()

    def test_existing_conflict_is_not_overwritten(self):
        importer.import_export(self.directory)
        self.database.nodes["supplier"]["properties"]["name"] = "Changed existing name"
        before = deepcopy((self.database.nodes, self.database.edges))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            importer.import_export(self.directory)
        self.assertEqual((self.database.nodes, self.database.edges), before)

    def test_conflicting_provenance_is_not_overwritten(self):
        importer.import_export(self.directory)
        node = self.database.nodes[self.desired_graph()[2]["chips"]]
        node["properties"]["provenance_ref"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "provenance_ref"):
            importer.import_export(self.directory)
        self.assertEqual(node["properties"]["provenance_ref"], "sha256:" + "f" * 64)

    def test_missing_endpoint_rolls_back_and_resume_completes(self):
        self.database.missing_endpoint = True
        with self.assertRaisesRegex(ValueError, "roll back"):
            importer.import_export(self.directory, batch_size=1)
        self.assertEqual(len(self.database.edges), 0)
        report = json.loads((self.directory / "neo4j_import_report.json").read_text())
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["nodes_created"], 5)
        self.database.missing_endpoint = False
        resumed = importer.import_export(self.directory, batch_size=1)
        self.assertEqual((resumed["nodes_created"], resumed["relationships_created"]), (0, 1))

    def test_interrupted_batch_resumes_without_duplicates(self):
        self.database.fail_batch = 3
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            importer.import_export(self.directory, batch_size=1)
        self.assertEqual(len(self.database.nodes), 2)
        self.database.fail_batch = None
        report = importer.import_export(self.directory, batch_size=1)
        self.assertEqual((report["nodes_created"], report["relationships_created"]), (3, 1))
        self.assertEqual((len(self.database.nodes), len(self.database.edges)), (5, 1))

    def test_partial_batch_count_mismatch_rolls_back_created_edge(self):
        importer.import_export(self.directory)
        self.database.edges.clear()
        edge = self.desired_graph()[1][0]
        missing = deepcopy(edge)
        missing["target"] = "missing"
        query = next(q for q, _ in self.database.calls if "MERGE (a)-" in q)
        with self.assertRaisesRegex(ValueError, "roll back"):
            self.database.execute_write(importer.write_batch, query, [edge, missing])
        self.assertEqual(self.database.edges, {})

    def test_keyboard_interrupt_records_failed_status(self):
        with patch.object(self.database, "execute_write", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                importer.import_export(self.directory)
        report = json.loads((self.directory / "neo4j_import_report.json").read_text())
        self.assertEqual((report["status"], report["error_type"]), ("failed", "KeyboardInterrupt"))

    def seed_legacy_graph(self):
        _, nodes, edges, _ = importer.preflight(self.directory)
        self.database.nodes = {
            iri: {**deepcopy(node), "storage_id": "node:" + iri} for iri, node in nodes.items()
        }
        self.database.edges = {
            edge["properties"]["assertion_id"]: {**deepcopy(edge), "storage_id": "edge:" + str(i)}
            for i, edge in enumerate(edges)
        }
        return nodes, edges

    def seed_grouped_graph(self):
        _, originals, assertions, _ = importer.preflight(self.directory)
        nodes, aliases = importer.canonicalize_nodes(originals)
        nodes, _, _ = importer.archive_node_provenance(self.directory, nodes)
        groups = importer.group_assertions(self.schema, assertions, aliases)
        self.database.nodes = {
            iri: {**deepcopy(node), "storage_id": "node:" + iri} for iri, node in nodes.items()
        }
        self.database.edges = {
            edge["properties"]["relationship_id"]: {
                **deepcopy(edge),
                "storage_id": "edge:" + str(i),
            }
            for i, edge in enumerate(groups)
        }
        return groups

    def test_evidence_migration_preserves_nodes_edges_and_other_properties(self):
        self.duplicate_gpu_rows()
        self.seed_grouped_graph()
        before_nodes, before_edges = deepcopy((self.database.nodes, self.database.edges))
        report = importer.import_export(self.directory, batch_size=1)
        self.assertEqual(self.database.nodes, before_nodes)
        self.assertEqual(report["relationships_evidence_updated"], 2)
        self.assertEqual(
            [
                report[key]
                for key in (
                    "nodes_created",
                    "nodes_deleted",
                    "relationships_created",
                    "relationships_deleted",
                )
            ],
            [0] * 4,
        )
        for key, old in before_edges.items():
            new = self.database.edges[key]
            self.assertEqual(new, importer.consolidate_evidence(old))
            self.assertEqual(
                {k: v for k, v in new["properties"].items() if k != "evidence"},
                {
                    k: v
                    for k, v in old["properties"].items()
                    if k not in ("evidence_text", "provenance_json", "supported_by")
                },
            )
            records = json.loads(new["properties"]["evidence"])
            self.assertEqual(new["properties"]["evidence_count"], len(records))
            if new["type"] == "supplies_to":
                self.assertTrue(all(record["time_text"] == "June 2025" for record in records))
                self.assertTrue(
                    all(
                        record["supplied_product"] == new["properties"]["supplied_product"]
                        for record in records
                    )
                )
                self.assertTrue(
                    all(
                        record["supplied_service"] == new["properties"]["supplied_service"]
                        for record in records
                    )
                )
        after = deepcopy((self.database.nodes, self.database.edges))
        repeated = importer.import_export(self.directory)
        self.assertEqual(repeated["relationships_evidence_updated"], 0)
        self.assertEqual((self.database.nodes, self.database.edges), after)

    def test_interrupted_evidence_migration_resumes_mixed_formats(self):
        self.duplicate_gpu_rows()
        self.seed_grouped_graph()
        self.database.fail_batch = 2
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            importer.import_export(self.directory, batch_size=1)
        self.assertEqual(
            sum("evidence" in edge["properties"] for edge in self.database.edges.values()), 1
        )
        self.database.fail_batch = None
        report = importer.import_export(self.directory, batch_size=1)
        self.assertEqual(report["relationships_evidence_updated"], 1)
        self.assertEqual(report["readback_source_assertions"], 4)
        self.assertEqual((report["relationships_created"], report["relationships_deleted"]), (0, 0))

    def test_evidence_migration_refuses_changed_provenance_before_writes(self):
        self.seed_grouped_graph()
        edge = next(iter(self.database.edges.values()))
        contexts = json.loads(edge["properties"]["provenance_json"])
        contexts[0]["source"]["source_sha256"] = "unrecognized source revision"
        edge["properties"]["provenance_json"] = importer.canonical(contexts)
        before = deepcopy((self.database.nodes, self.database.edges))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            importer.import_export(self.directory)
        self.assertEqual((self.database.nodes, self.database.edges), before)
        self.assertEqual(self.database.batches, 0)

    def test_evidence_batch_conflict_rolls_back_and_replay_is_safe(self):
        self.duplicate_gpu_rows()
        groups = self.seed_grouped_graph()
        _, rows = importer.evidence_migration_baseline(groups, self.database.edges.values())
        rows = deepcopy(rows)
        conflicting = next(
            edge
            for edge in self.database.edges.values()
            if edge["storage_id"] == rows[1]["storage_id"]
        )
        conflicting["properties"]["evidence_count"] += 1
        before = deepcopy(self.database.edges)
        with self.assertRaisesRegex(ValueError, "roll back"):
            self.database.execute_write(importer.write_evidence_batch, rows)
        self.assertEqual(self.database.edges, before)
        self.seed_grouped_graph()
        self.assertEqual(
            self.database.execute_write(importer.write_evidence_batch, rows), (0, 0, 0)
        )
        after = deepcopy(self.database.edges)
        self.assertEqual(
            self.database.execute_write(importer.write_evidence_batch, rows), (0, 0, 0)
        )
        self.assertEqual(self.database.edges, after)

    def test_exhibit_type_and_unknown_date_are_not_inferred_from_parent_form(self):
        self.rows[0]["provenance"].update(document_type="EX-99.1", form="8-K")
        self.rows[0]["provenance"].pop("filing_date")
        self.write_export()
        importer.import_export(self.directory)
        (record,) = json.loads(next(iter(self.database.edges.values()))["properties"]["evidence"])
        self.assertEqual(record["document_type"], "EX-99.1")
        self.assertIsNone(record["document_date"])

    def test_node_migration_roundtrips_archive_and_keeps_graph_small(self):
        self.rows = [
            {
                **deepcopy(self.rows[0]),
                "provenance": {**self.rows[0]["provenance"], "chunk_id": f"chunk-{i}"},
            }
            for i in range(20)
        ]
        self.write_export()
        self.seed_legacy_graph()
        self.database.nodes["supplier"]["properties"]["exchange"] = "NASDAQ"
        desired_nodes, desired_edges, _ = self.desired_graph()
        old_archive = self.directory / "node_provenance.json"
        old_archive.write_text("old archive is immutable")
        report = importer.import_export(self.directory)
        archive_path = self.directory / "node_provenance_v2.json"
        archive_bytes = archive_path.read_bytes()
        archive = json.loads(archive_bytes)
        self.assertEqual(
            report["node_provenance_archive"]["sha256"], importer.file_hash(archive_path)
        )
        self.assertEqual(report["node_provenance_archive"]["path"], str(archive_path.resolve()))
        for iri, node in self.database.nodes.items():
            properties = node["properties"]
            self.assertNotIn("provenance_json", properties)
            self.assertLess(len(importer.canonical(properties).encode()), 1024)
            reference = properties["provenance_ref"]
            raw = archive[reference]
            self.assertEqual(raw, desired_nodes[iri]["properties"]["provenance_json"])
            self.assertEqual(
                reference, "sha256:" + importer.hashlib.sha256(raw.encode()).hexdigest()
            )
        self.assertEqual(self.database.nodes["supplier"]["properties"]["exchange"], "NASDAQ")
        self.assertEqual(
            [edge["properties"] for edge in self.database.edges.values()],
            [edge["properties"] for edge in desired_edges],
        )
        self.assertEqual(
            (report["nodes_created"], report["nodes_deleted"], report["relationships_deleted"]),
            (2, 2, 20),
        )
        self.assertEqual(report["readback_source_assertions"], 20)
        repeated = importer.import_export(self.directory)
        self.assertEqual(
            (
                repeated["nodes_created"],
                repeated["relationships_created"],
                repeated["nodes_deleted"],
            ),
            (0, 0, 0),
        )
        self.assertEqual(archive_path.read_bytes(), archive_bytes)
        self.assertEqual(old_archive.read_text(), "old archive is immutable")

    def test_corrupt_archive_is_preserved_and_rejected_before_connect(self):
        _, nodes, _, _ = importer.preflight(self.directory)
        importer.archive_node_provenance(self.directory, nodes)
        archive_path = self.directory / "node_provenance_v2.json"
        original = json.loads(archive_path.read_text())
        for content in ("{", "{}", json.dumps({**original, next(iter(original)): "changed"})):
            archive_path.write_text(content)
            with self.assertRaises(ValueError):
                importer.import_export(self.directory)
            self.connect.assert_not_called()
            self.assertEqual(archive_path.read_text(), content)

    def test_archive_write_is_read_back_before_connecting(self):
        save_json = importer.save_json

        def corrupt_write(path, value):
            save_json(path, {} if path.name == "node_provenance_v2.json" else value)

        with patch.object(importer, "save_json", side_effect=corrupt_write):
            with self.assertRaisesRegex(ValueError, "archive differs"):
                importer.import_export(self.directory)
        self.connect.assert_not_called()

    def test_legacy_conflict_and_conflicting_reference_are_never_overwritten(self):
        self.seed_legacy_graph()
        properties = self.database.nodes["supplier"]["properties"]
        contexts = json.loads(properties["provenance_json"])
        contexts[0]["source"]["chunk_id"] = "different"
        properties["provenance_json"] = importer.canonical(contexts)
        before = deepcopy(self.database.nodes)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            importer.import_export(self.directory)
        self.assertEqual(self.database.nodes, before)
        self.assertEqual(self.database.batches, 0)
        self.seed_legacy_graph()
        properties = self.database.nodes["supplier"]["properties"]
        properties.pop("provenance_json")
        properties["provenance_ref"] = "sha256:" + "f" * 64
        before = deepcopy(self.database.nodes)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            importer.import_export(self.directory)
        self.assertEqual(self.database.nodes, before)
        self.assertEqual(self.database.batches, 0)

    def test_partial_legacy_migration_resumes_without_losing_context(self):
        nodes, _ = self.seed_legacy_graph()
        self.database.fail_batch = 2
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            importer.import_export(self.directory, batch_size=1)
        self.assertIn("provenance_ref", self.database.nodes["doc"]["properties"])
        self.assertIn("provenance_json", self.database.nodes["supplier"]["properties"])
        self.database.fail_batch = None
        report = importer.import_export(self.directory, batch_size=1)
        self.assertEqual(
            (
                report["nodes_created"],
                report["relationships_created"],
                report["relationships_deleted"],
            ),
            (2, 1, 1),
        )
        archive = json.loads((self.directory / "node_provenance_v2.json").read_text())
        for iri in ("doc", "supplier", "customer"):
            self.assertEqual(
                archive[self.database.nodes[iri]["properties"]["provenance_ref"]],
                nodes[iri]["properties"]["provenance_json"],
            )

    def test_concurrent_provenance_change_rolls_back_migration_batch(self):
        self.seed_legacy_graph()
        execute_write = self.database.execute_write
        changed = "different provenance after preflight"

        def change_before_write(operation, query, rows, *args):
            if "REMOVE n.provenance_json" in query:
                self.database.nodes[rows[0]["id"]]["properties"]["provenance_json"] = changed
            return execute_write(operation, query, rows, *args)

        with patch.object(self.database, "execute_write", side_effect=change_before_write):
            with self.assertRaisesRegex(ValueError, "roll back"):
                importer.import_export(self.directory)
        self.assertEqual(self.database.nodes["doc"]["properties"]["provenance_json"], changed)
        self.assertNotIn("provenance_ref", self.database.nodes["doc"]["properties"])

    def test_symmetric_pairs_collapse_but_directional_pairs_stay_separate(self):
        supply = self.rows[0]["relationships"][0]
        reverse_supply = {**deepcopy(supply), "source": "customer", "target": "supplier"}
        compete = {**deepcopy(supply), "type": "competes_with"}
        compete["properties"].pop("supplied_product")
        compete["properties"].pop("supplied_service")
        reverse_compete = {
            **deepcopy(compete),
            "source": "customer",
            "target": "supplier",
            "time_text": "July 2025",
        }
        self.rows[0]["relationships"] += [reverse_supply, compete, reverse_compete]
        self.write_export()
        _, _, originals, _ = importer.preflight(self.directory)
        groups = importer.group_assertions(self.schema, originals)
        self.assertEqual(groups, importer.group_assertions(self.schema, list(reversed(originals))))
        self.assertEqual(len(groups), 3)
        (competition,) = [edge for edge in groups if edge["type"] == "competes_with"]
        self.assertEqual((competition["source"], competition["target"]), ("customer", "supplier"))
        self.assertEqual(competition["properties"]["evidence_count"], 2)
        contexts = json.loads(competition["properties"]["provenance_json"])
        self.assertEqual(
            {(c["assertion"]["source"], c["assertion"]["target"]) for c in contexts},
            {("customer", "supplier"), ("supplier", "customer")},
        )
        supplies = [edge for edge in groups if edge["type"] == "supplies_to"]
        self.assertEqual(
            {(edge["source"], edge["target"]) for edge in supplies},
            {("customer", "supplier"), ("supplier", "customer")},
        )
        self.seed_legacy_graph()
        report = importer.import_export(self.directory, batch_size=1)
        self.assertEqual((report["relationships_created"], report["relationships_deleted"]), (3, 4))
        self.assertEqual(
            (report["readback_relationships"], report["readback_source_assertions"]), (3, 4)
        )
        self.assertTrue(
            any("]-(b)" in query for query, _ in self.database.calls if "OPTIONAL MATCH" in query)
        )

    def test_grouped_evidence_preserves_document_time_and_product_bindings(self):
        second = deepcopy(self.rows[0])
        second["provenance"].update(
            chunk_id="chunk-2",
            document_id="filing-2",
            source_url="https://example.com/filing-2.htm",
            filing_date="2025-08-01",
        )
        second["nodes"][0] = {
            "id": "doc-2",
            "labels": ["SourceDocument"],
            "properties": {
                "iri": "doc-2",
                "name": "filing-2",
                "url": "https://example.com/filing-2.htm",
            },
        }
        product = deepcopy(second["nodes"][3])
        product.update(id="new-chips", properties={"iri": "new-chips", "name": "new chips"})
        second["nodes"].append(product)
        edge = second["relationships"][0]
        quote = "Supplier delivers new chips to Customer."
        edge["time_text"] = "July 2025"
        edge["evidence"].update(
            original_quote=quote, original_end=len(quote), masked_end=len(quote)
        )
        edge["properties"].update(
            supported_by="doc-2", evidence_text=quote, supplied_product=["new-chips"]
        )
        self.rows.append(second)
        self.write_export()
        _, _, originals, _ = importer.preflight(self.directory)
        (group,) = importer.group_assertions(self.schema, originals)
        self.assertEqual(group["properties"]["supplied_product"], ["chips", "new-chips"])
        self.assertEqual(group["properties"]["supported_by"], ["doc", "doc-2"])
        contexts = {
            context["source"]["chunk_id"]: context
            for context in json.loads(group["properties"]["provenance_json"])
        }
        for i, chunk_id in enumerate(("chunk-1", "chunk-2")):
            context, original = contexts[chunk_id], self.rows[i]["relationships"][0]
            self.assertEqual(context["time_text"], original["time_text"])
            self.assertEqual(context["evidence"], original["evidence"])
            self.assertEqual(context["assertion"]["properties"], original["properties"])
        self.seed_legacy_graph()
        report = importer.import_export(self.directory)
        self.assertEqual(
            (report["source_assertions"], report["readback_source_assertions"]), (2, 2)
        )
        self.assertEqual((report["relationships_created"], report["relationships_deleted"]), (1, 2))

    def test_legacy_edge_conflict_aborts_before_any_writes(self):
        self.seed_legacy_graph()
        edge = next(iter(self.database.edges.values()))
        edge["properties"]["evidence_text"] = "Conflicting evidence"
        before = deepcopy((self.database.nodes, self.database.edges))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            importer.import_export(self.directory)
        self.assertEqual((self.database.nodes, self.database.edges), before)
        self.assertEqual(self.database.batches, 0)

    def test_unrelated_relationship_is_preserved_exactly(self):
        self.seed_legacy_graph()
        self.database.nodes["unrelated-product"] = {
            "id": "unrelated-product",
            "storage_id": "unrelated-product",
            "labels": ["Product"],
            "properties": {"iri": "unrelated-product"},
        }
        self.database.edges["unrelated"] = {
            "source": "supplier",
            "target": "unrelated-product",
            "type": "offers",
            "properties": {},
            "storage_id": "unrelated",
        }
        original = deepcopy(self.database.edges["unrelated"])
        report = importer.import_export(self.directory)
        self.assertEqual(self.database.edges["unrelated"], original)
        self.assertEqual(
            (report["readback_relationships"], report["relationships_deleted"]), (2, 1)
        )

    def test_mixed_legacy_and_grouped_relationships_resume_atomically(self):
        supply = self.rows[0]["relationships"][0]
        self.rows[0]["relationships"].append(
            {**deepcopy(supply), "source": "customer", "target": "supplier"}
        )
        self.write_export()
        self.seed_legacy_graph()
        self.database.fail_batch = 7  # Five node batches, then one complete relationship migration.
        with self.assertRaisesRegex(RuntimeError, "interruption"):
            importer.import_export(self.directory, batch_size=1)
        self.assertEqual(
            sum("assertion_id" in edge["properties"] for edge in self.database.edges.values()), 1
        )
        self.assertEqual(
            sum("relationship_id" in edge["properties"] for edge in self.database.edges.values()), 1
        )
        self.database.fail_batch = None
        resumed = importer.import_export(self.directory, batch_size=1)
        self.assertEqual(
            (resumed["relationships_created"], resumed["relationships_deleted"]), (1, 1)
        )
        repeated = importer.import_export(self.directory, batch_size=1)
        self.assertEqual(
            (repeated["relationships_created"], repeated["relationships_deleted"]), (0, 0)
        )
        self.assertEqual(repeated["readback_source_assertions"], 2)

    def test_concurrent_legacy_change_before_delete_rolls_back_new_group(self):
        self.seed_legacy_graph()
        run = self.database.run
        changed = "Changed after the migration batch read"

        def alter_before_delete(query, **parameters):
            if "DELETE r" in query:
                next(
                    edge
                    for edge in self.database.edges.values()
                    if "assertion_id" in edge["properties"]
                )["properties"]["evidence_text"] = changed
            return run(query, **parameters)

        with patch.object(self.database, "run", side_effect=alter_before_delete):
            with self.assertRaisesRegex(ValueError, "roll back"):
                importer.import_export(self.directory)
        self.assertEqual(len(self.database.edges), 1)
        edge = next(iter(self.database.edges.values()))
        self.assertIn("assertion_id", edge["properties"])
        self.assertNotEqual(edge["properties"]["evidence_text"], changed)

    def test_unknown_edge_on_targeted_pair_aborts_before_writes(self):
        self.seed_legacy_graph()
        self.database.edges["unknown"] = {
            "source": "supplier",
            "target": "customer",
            "type": "supplies_to",
            "properties": {},
            "storage_id": "unknown",
        }
        before = deepcopy((self.database.nodes, self.database.edges))
        with self.assertRaisesRegex(ValueError, "targeted"):
            importer.import_export(self.directory)
        self.assertEqual((self.database.nodes, self.database.edges), before)
        self.assertEqual(self.database.batches, 0)

    def test_replayed_committed_relationship_batch_is_idempotent(self):
        _, assertions = self.seed_legacy_graph()
        _, groups, aliases = self.desired_graph()
        legacy, _ = importer.classify_existing_edges(
            self.schema, assertions, groups, self.database.edges.values(), aliases
        )
        importer.import_export(self.directory)
        read_query = next(query for query, _ in self.database.calls if "OPTIONAL MATCH" in query)
        merge_query = next(query for query, _ in self.database.calls if "MERGE (a)-" in query)
        counts = self.database.execute_write(
            importer.write_relationship_batch, read_query, merge_query, groups, legacy
        )
        self.assertEqual(counts, (0, 0, 0))
        self.assertEqual(len(self.database.edges), 1)

    def duplicate_gpu_rows(self):
        first = self.rows[0]
        first["nodes"][3]["properties"]["name"] = "GPU"
        first["relationships"].append(
            {
                "source": "supplier",
                "target": "chips",
                "type": "offers",
                "properties": {
                    "supported_by": "doc",
                    "evidence_text": first["relationships"][0]["properties"]["evidence_text"],
                },
                "evidence": deepcopy(first["relationships"][0]["evidence"]),
                "time_text": None,
            }
        )
        second = deepcopy(first)
        second["provenance"]["chunk_id"] = "chunk-2"
        second["nodes"][3].update(id="chips-2", properties={"iri": "chips-2", "name": "  GPUs  "})
        second["relationships"][0]["properties"]["supplied_product"] = ["chips-2"]
        second["relationships"][1]["target"] = "chips-2"
        self.rows.append(second)
        self.write_export()

    def test_canonicalization_is_typed_conservative_and_preserves_occurrences(self):
        _, originals, _, _ = importer.preflight(self.directory)
        prototype = originals["chips"]
        nodes = {}
        for i, (kind, name) in enumerate(
            (
                ("Product", "GPU"),
                ("Product", "  GPUs "),
                ("Product", "ＧＰＵ"),
                ("Service", "GPU"),
                ("Topic", " AI  demand "),
                ("Topic", "ai demand"),
                ("Sector", "AI demand"),
                ("Product", "accelerator"),
                ("Product", "accelerators"),
                ("Event", "same event"),
                ("Event", "same event"),
            )
        ):
            iri = str(i)
            nodes[iri] = {
                **deepcopy(prototype),
                "id": iri,
                "labels": [kind],
                "properties": {**prototype["properties"], "iri": iri, "name": name},
            }
        canonical_nodes, aliases = importer.canonicalize_nodes(nodes)
        self.assertEqual(aliases["0"], aliases["1"])
        self.assertEqual(aliases["0"], aliases["2"])
        self.assertNotEqual(aliases["0"], aliases["3"])
        self.assertEqual(aliases["4"], aliases["5"])
        self.assertNotEqual(aliases["4"], aliases["6"])
        self.assertNotEqual(aliases["7"], aliases["8"])
        self.assertIn("9", canonical_nodes)
        self.assertIn("10", canonical_nodes)
        self.assertEqual(
            importer.canonicalize_nodes(dict(reversed(list(nodes.items())))),
            (canonical_nodes, aliases),
        )
        nodes["1"]["properties"]["description"] = "incompatible"
        with self.assertRaisesRegex(ValueError, "incompatible"):
            importer.canonicalize_nodes(nodes)

    def test_fresh_import_has_one_gpu_offer_and_keeps_original_bindings(self):
        self.duplicate_gpu_rows()
        source_bytes = (self.directory / "assertions.jsonl").read_bytes()
        _, original_nodes, assertions, _ = importer.preflight(self.directory)
        desired, _, aliases = self.desired_graph()
        report = importer.import_export(self.directory)
        offers = [edge for edge in self.database.edges.values() if edge["type"] == "offers"]
        self.assertEqual(len(offers), 1)
        offer = offers[0]
        self.assertEqual(offer["target"], aliases["chips"])
        self.assertEqual(aliases["chips"], aliases["chips-2"])
        self.assertEqual(offer["properties"]["evidence_count"], 2)
        records = json.loads(offer["properties"]["evidence"])
        originals = {edge["properties"]["assertion_id"]: edge for edge in assertions}
        for record in records:
            original = originals[record["id"]]
            self.assertEqual(aliases[original["target"]], offer["target"])
            self.assertEqual(record["evidence_text"], original["properties"]["evidence_text"])
            self.assertEqual(record["document"], self.rows[0]["provenance"]["source_url"])
            self.assertEqual(record["document_type"], "10-K")
            self.assertEqual(record["document_date"], "2025-07-01")
            self.assertEqual(
                set(record), {"id", "evidence_text", "document", "document_type", "document_date"}
            )
        archive = json.loads((self.directory / "node_provenance_v2.json").read_text())
        for old_iri, original in original_nodes.items():
            canonical_iri = aliases.get(old_iri, old_iri)
            stored = json.loads(
                archive[self.database.nodes[canonical_iri]["properties"]["provenance_ref"]]
            )
            self.assertTrue(
                all(
                    context in stored
                    for context in json.loads(original["properties"]["provenance_json"])
                )
            )
        self.assertEqual(report["readback_source_assertions"], 4)
        self.assertEqual((self.directory / "assertions.jsonl").read_bytes(), source_bytes)
        self.assertEqual(set(self.database.nodes), set(desired))

    def test_migration_accepts_inline_and_prior_shared_reference_storage(self):
        for referenced in (False, True):
            with self.subTest(referenced=referenced):
                self.seed_legacy_graph()
                if referenced:
                    for node in self.database.nodes.values():
                        if node["labels"][0] in ("Company", "SourceDocument"):
                            raw = node["properties"].pop("provenance_json")
                            node["properties"]["provenance_ref"] = (
                                "sha256:" + importer.hashlib.sha256(raw.encode()).hexdigest()
                            )
                report = importer.import_export(self.directory)
                self.assertEqual((report["nodes_deleted"], report["relationships_deleted"]), (2, 1))
                self.assertFalse({"chips", "fabrication"} & self.database.nodes.keys())

    def test_alias_with_unknown_edge_or_reference_is_never_deleted(self):
        for properties, target in (({}, "chips"), ({"supplied_product": "chips"}, "supplier")):
            self.seed_legacy_graph()
            self.database.edges["unknown"] = {
                "source": "customer",
                "target": target,
                "type": "offers" if not properties else "supplies_to",
                "properties": properties,
                "storage_id": "unknown",
            }
            before = deepcopy((self.database.nodes, self.database.edges))
            self.database.batches = 0
            with self.assertRaisesRegex(ValueError, "obsolete"):
                importer.import_export(self.directory)
            self.assertEqual((self.database.nodes, self.database.edges), before)
            self.assertEqual(self.database.batches, 0)

    def test_alias_cleanup_interruption_resumes_without_losing_evidence(self):
        self.seed_legacy_graph()
        execute_write = self.database.execute_write
        cleanups = 0

        def interrupt_second_cleanup(operation, *args):
            nonlocal cleanups
            if operation is importer.delete_obsolete_batch:
                cleanups += 1
                if cleanups == 2:
                    raise RuntimeError("Interrupted node cleanup")
            return execute_write(operation, *args)

        with patch.object(self.database, "execute_write", side_effect=interrupt_second_cleanup):
            with self.assertRaisesRegex(RuntimeError, "cleanup"):
                importer.import_export(self.directory, batch_size=1)
        self.assertEqual(len({"chips", "fabrication"} & self.database.nodes.keys()), 1)
        self.assertEqual(len(self.database.edges), 1)
        resumed = importer.import_export(self.directory, batch_size=1)
        self.assertEqual(
            (
                resumed["nodes_created"],
                resumed["nodes_deleted"],
                resumed["relationships_created"],
                resumed["relationships_deleted"],
            ),
            (0, 1, 0, 0),
        )
        self.assertEqual(resumed["readback_source_assertions"], 1)

    def test_changed_or_connected_alias_rolls_back_cleanup(self):
        self.seed_legacy_graph()
        execute_write = self.database.execute_write

        def connect_before_delete(operation, *args):
            if operation is importer.delete_obsolete_batch:
                row = args[0][0]
                self.database.edges["late"] = {
                    "source": "supplier",
                    "target": row["id"],
                    "type": "offers",
                    "properties": {},
                    "storage_id": "late",
                }
            return execute_write(operation, *args)

        with patch.object(self.database, "execute_write", side_effect=connect_before_delete):
            with self.assertRaisesRegex(ValueError, "connected"):
                importer.import_export(self.directory)
        self.assertIn("chips", self.database.nodes)
        self.assertIn("fabrication", self.database.nodes)
        self.assertTrue(all("DETACH" not in query for query, _ in self.database.calls))

    def test_scalar_annotation_reference_blocks_cleanup(self):
        self.seed_legacy_graph()
        row = self.database.nodes["chips"]
        self.database.edges["late"] = {
            "source": "customer",
            "target": "supplier",
            "type": "supplies_to",
            "properties": {"supplied_product": "chips"},
            "storage_id": "late",
        }
        with self.assertRaisesRegex(ValueError, "annotations"):
            self.database.execute_write(importer.delete_obsolete_batch, [row])
        self.assertIn("chips", self.database.nodes)

    def test_lock_blocks_second_import(self):
        lock_path = self.root / "data/.neo4j-import.lock"
        lock_path.parent.mkdir()
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "active"):
                importer.import_export(self.directory)
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
