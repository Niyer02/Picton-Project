"""Load one completed de-anonymized export into the existing domain graph."""

import argparse
import fcntl
import hashlib
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from picton_graph.config import ROOT, SCHEMA_PATH
from picton_graph.extraction.extractor import RELATIONSHIPS
from picton_graph.files import file_hash, save_json, utc_now
from picton_graph.graph.connection import connect
from picton_graph.graph.validation import validate_records

CANONICAL_LABELS = {"Product", "Service", "Topic", "Sector"}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def provenance(source, record):
    return canonical(
        {"source": source, "evidence": record.get("evidence"), "time_text": record.get("time_text")}
    )


def assertion_id(chunk_id, ordinal, edge):
    identity = {key: edge[key] for key in ("source", "target", "type", "properties")}
    identity.update(chunk_id=chunk_id, ordinal=ordinal, time_text=edge.get("time_text"))
    return hashlib.sha256(canonical(identity).encode()).hexdigest()


def check_fields(record, required, optional=()):
    if (
        not isinstance(record, dict)
        or not set(required) <= record.keys()
        or record.keys() - set(required) - set(optional)
    ):
        raise ValueError("Missing or unknown export fields.")


def preflight(directory):
    """Validate the entire immutable input before opening a database connection."""
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("status") != "complete" or manifest.get("format") != "graph-payload-jsonl-v1":
        raise ValueError("A completed graph-payload-jsonl-v1 export is required.")
    if file_hash(SCHEMA_PATH) != manifest["source_files"]["schema.json"]:
        raise ValueError("Current ontology schema differs from the exported schema.")
    source = directory / "assertions.jsonl"
    if file_hash(source) != manifest["output_sha256"]:
        raise ValueError("Export checksum mismatch.")
    schema = json.loads(SCHEMA_PATH.read_text())
    if any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
        for name in (*schema["classes"], *RELATIONSHIPS)
    ):
        raise ValueError("Unsafe ontology identifier.")
    nodes, relationships, contexts, chunks = {}, [], defaultdict(set), set()
    node_counts, relationship_counts, empty = Counter(), Counter(), 0
    with source.open() as stream:
        for line in stream:
            payload = json.loads(line)
            check_fields(payload, ("provenance", "nodes", "relationships"))
            source_context = payload["provenance"]
            if not isinstance(source_context, dict) or not isinstance(
                source_context.get("chunk_id"), str
            ):
                raise ValueError("Missing chunk provenance.")
            chunk_id = source_context["chunk_id"]
            if chunk_id in chunks:
                raise ValueError("Duplicate chunk in export.")
            chunks.add(chunk_id)
            row_nodes, row_edges = [], []
            if not isinstance(payload["nodes"], list) or not isinstance(
                payload["relationships"], list
            ):
                raise ValueError("Export nodes and relationships must be lists.")
            for node in payload["nodes"]:
                check_fields(node, ("id", "labels", "properties"), ("evidence", "time_text"))
                if (
                    not isinstance(node["labels"], list)
                    or len(node["labels"]) != 1
                    or not isinstance(node["properties"], dict)
                    or node["id"] != node["properties"].get("iri")
                ):
                    raise ValueError("Export nodes require one concrete class and matching id/iri.")
                if node["properties"].keys() & {"provenance_json", "provenance_ref"}:
                    raise ValueError("Input already contains storage metadata.")
                kind = node["labels"][0]
                if kind == "SourceDocument" and (
                    node.get("evidence") is not None or node.get("time_text") is not None
                ):
                    raise ValueError(
                        "SourceDocument provenance must not invent evidence or event time."
                    )
                context = provenance(source_context, node)
                properties = {**node["properties"], "provenance_json": "[" + context + "]"}
                row_nodes.append(
                    {"id": node["id"], "labels": node["labels"], "properties": properties}
                )
                clean = {key: node[key] for key in ("id", "labels", "properties")}
                previous = nodes.setdefault(node["id"], clean)
                if previous != clean:
                    raise ValueError("Repeated node IRI has conflicting domain properties.")
                contexts[node["id"]].add(context)
                node_counts[kind] += 1
            for ordinal, edge in enumerate(payload["relationships"]):
                check_fields(
                    edge,
                    ("source", "target", "type", "properties"),
                    ("source_name", "target_name", "evidence", "time_text"),
                )
                if edge["type"] not in RELATIONSHIPS or not isinstance(edge["properties"], dict):
                    raise ValueError("Expected a canonical relationship with properties.")
                if edge["properties"].keys() & {
                    "assertion_id",
                    "relationship_id",
                    "evidence_count",
                    "provenance_json",
                    "provenance_ref",
                    "evidence",
                }:
                    raise ValueError("Input already contains storage metadata.")
                properties = {
                    **edge["properties"],
                    "assertion_id": assertion_id(chunk_id, ordinal, edge),
                    "provenance_json": "[" + provenance(source_context, edge) + "]",
                }
                row_edges.append(
                    {
                        "source": edge["source"],
                        "target": edge["target"],
                        "type": edge["type"],
                        "properties": properties,
                    }
                )
                relationship_counts[edge["type"]] += 1
            validate_records(schema, row_nodes, row_edges)
            relationships.extend(row_edges)
            empty += (
                len(row_nodes) == 1
                and row_nodes[0]["labels"] == ["SourceDocument"]
                and not row_edges
            )
    for iri, node in nodes.items():
        node["properties"] = {
            **node["properties"],
            "provenance_json": "[" + ",".join(sorted(contexts[iri])) + "]",
        }
    counts = {
        "exported_chunks": len(chunks),
        "unique_node_iris": len(nodes),
        "empty_extractions": empty,
        "node_records_by_type": dict(node_counts),
        "relationship_assertions": len(relationships),
        "relationship_assertions_by_type": dict(relationship_counts),
    }
    if any(manifest.get(key) != value for key, value in counts.items()):
        raise ValueError("Export counts do not match the manifest.")
    if manifest.get("source_counts", {}).get("valid") != len(chunks):
        raise ValueError("Export valid chunk count does not match its source.")
    validate_records(schema, list(nodes.values()), relationships)
    return schema, nodes, relationships, manifest


def canonicalize_nodes(originals):
    nodes, aliases, contexts = {}, {}, defaultdict(set)
    for old_iri, node in originals.items():
        kind, properties = node["labels"][0], dict(node["properties"])
        iri = old_iri
        if kind in CANONICAL_LABELS:
            name = " ".join(unicodedata.normalize("NFKC", properties["name"]).casefold().split())
            if kind == "Product" and name in ("gpu", "gpus"):
                name = "gpu"
            iri = (
                "urn:picton:entity:"
                + hashlib.sha256(canonical({"type": kind, "name": name}).encode()).hexdigest()
            )
        if iri != old_iri:
            aliases[old_iri] = iri
        contexts[iri].update(
            canonical(context) for context in json.loads(properties.pop("provenance_json"))
        )
        properties["iri"] = iri
        if iri in nodes:
            previous = nodes[iri]["properties"]
            if {k: v for k, v in previous.items() if k != "name"} != {
                k: v for k, v in properties.items() if k != "name"
            }:
                raise ValueError("Canonical entity names have incompatible domain properties.")
            previous["name"] = min(previous["name"], properties["name"])
        else:
            nodes[iri] = {"id": iri, "labels": node["labels"], "properties": properties}
    for iri, node in nodes.items():
        node["properties"]["provenance_json"] = "[" + ",".join(sorted(contexts[iri])) + "]"
    return nodes, aliases


def edge_references(edge):
    references = {edge["source"], edge["target"]}
    for name in ("supplied_product", "supplied_service"):
        values = edge["properties"].get(name, [])
        references.update(values if isinstance(values, list) else [values])
    return references


def resolve_edge(edge, aliases):
    return {
        **edge,
        "source": aliases.get(edge["source"], edge["source"]),
        "target": aliases.get(edge["target"], edge["target"]),
    }


def relationship_identity(schema, edge):
    source, target = edge["source"], edge["target"]
    if schema["relationships"][edge["type"]].get("symmetric"):
        source, target = sorted((source, target))
    return {"source": source, "type": edge["type"], "target": target}


def relationship_id(schema, edge):
    return hashlib.sha256(canonical(relationship_identity(schema, edge)).encode()).hexdigest()


def group_assertions(schema, assertions, aliases=None):
    aliases = aliases or {}
    groups = {}
    for edge in assertions:
        resolved_edge = resolve_edge(edge, aliases)
        key = relationship_id(schema, resolved_edge)
        group = groups.setdefault(
            key,
            {
                **relationship_identity(schema, resolved_edge),
                "contexts": [],
                "annotations": defaultdict(set),
            },
        )
        annotations = {
            k: v
            for k, v in edge["properties"].items()
            if k not in ("assertion_id", "provenance_json")
        }
        (context,) = json.loads(edge["properties"]["provenance_json"])
        context["assertion"] = {
            "id": edge["properties"]["assertion_id"],
            "source": edge["source"],
            "target": edge["target"],
            "properties": annotations,
        }
        resolved = {iri: aliases[iri] for iri in sorted(edge_references(edge)) if iri in aliases}
        if resolved:
            context["assertion"]["resolved"] = resolved
        group["contexts"].append(context)
        for name, values in annotations.items():
            values = values if isinstance(values, list) else [values]
            if name in ("supplied_product", "supplied_service"):
                values = [aliases.get(value, value) for value in values]
            group["annotations"][name].update(values)
    relationships = []
    for key, group in sorted(groups.items()):
        contexts = sorted(group.pop("contexts"), key=lambda context: context["assertion"]["id"])
        annotations = {
            name: sorted(values) for name, values in group.pop("annotations").items() if values
        }
        relationships.append(
            {
                **group,
                "properties": {
                    **annotations,
                    "relationship_id": key,
                    "evidence_count": len(contexts),
                    "provenance_json": canonical(contexts),
                },
            }
        )
    return relationships


def require_same_edge(expected, actual):
    if any(expected[key] != actual[key] for key in ("source", "target", "type", "properties")):
        raise ValueError("Existing relationship conflicts with the verified export.")


def consolidate_evidence(edge):
    """Convert verified grouped provenance without changing other edge properties."""
    records = []
    for context in json.loads(edge["properties"]["provenance_json"]):
        source, assertion = context["source"], context["assertion"]
        record = {
            "id": assertion["id"],
            "evidence_text": context["evidence"]["original_quote"],
            "document": source["source_url"],
            "document_type": source["document_type"],
            "document_date": source.get("filing_date"),
        }
        if context["time_text"] is not None:
            record["time_text"] = context["time_text"]
        for name in ("supplied_product", "supplied_service"):
            values = assertion["properties"].get(name, [])
            values = values if isinstance(values, list) else [values]
            if values:
                resolved = assertion.get("resolved", {})
                record[name] = sorted({resolved.get(value, value) for value in values})
        records.append(record)
    properties = {
        key: value
        for key, value in edge["properties"].items()
        if key not in ("evidence_text", "provenance_json", "supported_by")
    }
    return {**edge, "properties": {**properties, "evidence": canonical(records)}}


def evidence_migration_baseline(previous_groups, existing_edges):
    previous = {edge["properties"]["relationship_id"]: edge for edge in previous_groups}
    baseline, migrations = [], []
    for edge in existing_edges:
        old = previous.get(edge["properties"].get("relationship_id"))
        if old is not None and "provenance_json" in edge["properties"]:
            require_same_edge(old, edge)
            updated = consolidate_evidence(edge)
            migrations.append({**updated, "previous_properties": edge["properties"]})
            baseline.append(updated)
        else:
            baseline.append(edge)
    return baseline, migrations


def write_evidence_batch(tx, rows):
    # Compare the full snapshot so a concurrent edit cannot be silently overwritten.
    return write_batch(
        tx,
        """UNWIND $rows AS row
        MATCH (a)-[r]->(b) WHERE elementId(r) = row.storage_id
            AND a.iri = row.source AND b.iri = row.target AND type(r) = row.type
            AND (properties(r) = row.previous_properties OR properties(r) = row.properties)
        SET r.evidence = row.properties.evidence
        REMOVE r.evidence_text, r.provenance_json, r.supported_by
        WITH r, row WHERE properties(r) = row.properties RETURN count(r) AS matched""",
        rows,
    )


def classify_existing_edges(schema, assertions, groups, existing_edges, aliases=None):
    aliases = aliases or {}
    originals = {edge["properties"]["assertion_id"]: edge for edge in assertions}
    desired = {edge["properties"]["relationship_id"]: edge for edge in groups}
    legacy, preserved = defaultdict(list), []
    for edge in existing_edges:
        properties = edge["properties"]
        old = originals.get(properties.get("assertion_id"))
        grouped = desired.get(properties.get("relationship_id"))
        if old is not None:
            require_same_edge(old, edge)
            legacy[relationship_id(schema, resolve_edge(old, aliases))].append(edge)
        elif grouped is not None:
            require_same_edge(grouped, edge)
            preserved.append(edge)
        elif relationship_id(schema, edge) in desired or edge_references(edge) & aliases.keys():
            raise ValueError(
                "Unknown relationship touches a targeted pair or obsolete entity alias."
            )
        else:
            preserved.append(edge)
    return legacy, preserved


def archive_node_provenance(directory, nodes):
    """Archive every canonical node history before changing the graph."""
    archive, compact = {}, {}
    for iri, node in nodes.items():
        properties = dict(node["properties"])
        raw = properties.pop("provenance_json")
        reference = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        archive[reference] = raw
        properties["provenance_ref"] = reference
        compact[iri] = {**node, "properties": properties}
    path = directory / "node_provenance_v2.json"
    if not path.exists():
        save_json(path, archive)
    if json.loads(path.read_text()) != archive:
        raise ValueError(
            "Node provenance archive differs from the verified export; refusing overwrite."
        )
    return compact, archive, {"path": str(path), "sha256": file_hash(path), "entries": len(archive)}


def classify_existing_nodes(originals, aliases, existing_nodes):
    obsolete, preserved = [], []
    for node in existing_nodes:
        if node["id"] not in aliases:
            preserved.append(node)
            continue
        original = originals[node["id"]]
        properties = dict(original["properties"])
        raw = properties.pop("provenance_json")
        properties["provenance_ref"] = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        if node["labels"] != original["labels"] or node["properties"] not in (
            original["properties"],
            properties,
        ):
            raise ValueError("Obsolete entity alias differs from its verified original node.")
        obsolete.append(node)
    return obsolete, preserved


def migrate_legacy_baseline(nodes, existing_nodes, archive):
    baseline = []
    for node in existing_nodes:
        incoming = nodes.get(node["id"], {})
        reference = incoming.get("properties", {}).get("provenance_ref")
        if reference and "provenance_json" in node["properties"]:
            if node["properties"]["provenance_json"] != archive[reference]:
                raise ValueError("Existing provenance_json conflicts with the verified archive.")
            properties = {
                key: value for key, value in node["properties"].items() if key != "provenance_json"
            }
            if properties.get("provenance_ref", reference) != reference:
                raise ValueError("Existing provenance_ref conflicts with the verified archive.")
            baseline.append({**node, "properties": {**properties, "provenance_ref": reference}})
        else:
            baseline.append(node)
    return baseline


def read_graph(tx, schema):
    nodes = [
        dict(row)
        for row in tx.run("""
        MATCH (n) RETURN n.iri AS id, elementId(n) AS storage_id,
                         labels(n) AS labels, properties(n) AS properties
    """)
    ]
    edges = [
        dict(row)
        for row in tx.run("""
        MATCH (a)-[r]->(b) RETURN a.iri AS source, b.iri AS target,
            elementId(r) AS storage_id, type(r) AS type, properties(r) AS properties
    """)
    ]
    validate_records(schema, nodes, edges)
    return nodes, edges


def verify_constraints(tx, schema):
    constraints = list(
        tx.run("SHOW CONSTRAINTS YIELD type, entityType, labelsOrTypes, properties RETURN *")
    )
    covered = {
        row["labelsOrTypes"][0]
        for row in constraints
        if row["type"] == "UNIQUENESS"
        and row["entityType"] == "NODE"
        and len(row["labelsOrTypes"]) == 1
        and row["properties"] == ["iri"]
    }
    if schema["classes"].keys() - covered:
        raise ValueError(
            "Missing required per-label iri uniqueness constraints; run python -m picton_graph.graph.ontology first."
        )


def compatible_node(incoming, existing, *, complete=False):
    if incoming["labels"] != existing["labels"]:
        raise ValueError("Existing graph identity or class conflicts with the export.")
    for key, value in incoming["properties"].items():
        if key in existing["properties"] and existing["properties"][key] != value:
            raise ValueError(f"Existing graph property conflicts with the export: {key}")
        if complete and key not in existing["properties"]:
            raise ValueError(f"Imported graph property missing: {key}")


def compare_graph(nodes, edges, existing_nodes, existing_edges, *, complete=False):
    by_iri = {node["id"]: node for node in existing_nodes}
    by_relationship = {
        edge["properties"]["relationship_id"]: edge
        for edge in existing_edges
        if "relationship_id" in edge["properties"]
    }
    for key, incoming in nodes.items():
        if key in by_iri:
            compatible_node(incoming, by_iri[key], complete=complete)
        elif complete:
            raise ValueError("Imported node is missing.")
    for incoming in edges:
        key = incoming["properties"]["relationship_id"]
        if key in by_relationship:
            require_same_edge(incoming, by_relationship[key])
        elif complete:
            raise ValueError("Imported relationship is missing.")


def write_batch(tx, query, rows):
    result = tx.run(query, rows=rows)
    matched = result.single(strict=True)["matched"]
    if matched != len(rows):
        raise ValueError("Batch endpoint or identity mismatch; transaction must roll back.")
    counters = result.consume().counters
    return counters.nodes_created, counters.relationships_created, counters.relationships_deleted


def write_relationship_batch(tx, read_query, merge_query, rows, legacy):
    current = defaultdict(dict)
    for record in tx.run(read_query, rows=rows):
        if record["storage_id"] is not None:
            current[record["group_id"]][record["storage_id"]] = dict(record)
    old_rows = [
        {**edge, "group_id": row["properties"]["relationship_id"]}
        for row in rows
        for edge in legacy.get(row["properties"]["relationship_id"], [])
    ]
    if old_rows:
        for record in tx.run(
            """UNWIND $rows AS row
            MATCH (a)-[r]->(b) WHERE elementId(r) = row.storage_id
            RETURN row.group_id AS group_id, elementId(r) AS storage_id,
                a.iri AS source, b.iri AS target, type(r) AS type, properties(r) AS properties""",
            rows=old_rows,
        ):
            current[record["group_id"]][record["storage_id"]] = dict(record)
    removable = []
    for row in rows:
        key = row["properties"]["relationship_id"]
        expected_old = {edge["storage_id"]: edge for edge in legacy.get(key, [])}
        seen_old, grouped = set(), 0
        for edge in current[key].values():
            if edge["properties"].get("relationship_id") == key:
                require_same_edge(row, edge)
                grouped += 1
            elif edge["storage_id"] in expected_old:
                require_same_edge(expected_old[edge["storage_id"]], edge)
                seen_old.add(edge["storage_id"])
                removable.append(edge)
            else:
                raise ValueError("Relationship changed after preflight; refusing migration.")
        if grouped > 1 or (expected_old.keys() - seen_old and not grouped):
            raise ValueError("Relationship changed after preflight; refusing migration.")
    created_nodes, created_edges, _ = write_batch(tx, merge_query, rows)
    deleted = 0
    if removable:
        _, _, deleted = write_batch(
            tx,
            """UNWIND $rows AS row
            MATCH (a)-[r]->(b) WHERE elementId(r) = row.storage_id
                AND a.iri = row.source AND b.iri = row.target AND type(r) = row.type
                AND properties(r) = row.properties
            DELETE r RETURN count(*) AS matched""",
            removable,
        )
        if deleted != len(removable):
            raise ValueError(
                "Legacy relationship deletion count mismatch; transaction must roll back."
            )
    return created_nodes, created_edges, deleted


def write_groups(session, schema, nodes, edges, batch_size, report, archive, legacy):
    groups = defaultdict(list)
    for node in nodes.values():
        label = node["labels"][0]
        node = {
            **node,
            "archived_provenance_json": archive[node["properties"]["provenance_ref"]],
        }
        query = f"""UNWIND $rows AS row MERGE (n:{label} {{iri: row.id}})
            WITH n, row
            WHERE (n.provenance_json IS NULL OR n.provenance_json = row.archived_provenance_json)
              AND (n.provenance_ref IS NULL OR n.provenance_ref = row.properties.provenance_ref)
            SET n += row.properties REMOVE n.provenance_json RETURN count(n) AS matched"""
        groups[(None, query)].append(node)
    for edge in edges:
        source_label, target_label = (
            nodes[edge[field]]["labels"][0] for field in ("source", "target")
        )
        endpoints = (
            f"MATCH (a:{source_label} {{iri: row.source}}), (b:{target_label} {{iri: row.target}})"
        )
        arrow = "-" if schema["relationships"][edge["type"]].get("symmetric") else "->"
        read_query = f"""UNWIND $rows AS row {endpoints}
            OPTIONAL MATCH (a)-[r:{edge["type"]}]{arrow}(b)
            RETURN row.properties.relationship_id AS group_id, elementId(r) AS storage_id,
                startNode(r).iri AS source, endNode(r).iri AS target, type(r) AS type, properties(r) AS properties"""
        query = f"""UNWIND $rows AS row {endpoints}
            MERGE (a)-[r:{edge["type"]} {{relationship_id: row.properties.relationship_id}}]->(b)
            ON CREATE SET r += row.properties
            WITH r, row WHERE properties(r) = row.properties RETURN count(r) AS matched"""
        groups[(read_query, query)].append(edge)
    for (read_query, query), rows in groups.items():
        for offset in range(0, len(rows), batch_size):
            batch = rows[offset : offset + batch_size]
            if read_query is None:
                created_nodes, created_edges, deleted_edges = session.execute_write(
                    write_batch, query, batch
                )
            else:
                created_nodes, created_edges, deleted_edges = session.execute_write(
                    write_relationship_batch, read_query, query, batch, legacy
                )
            report["nodes_created"] += created_nodes
            report["relationships_created"] += created_edges
            report["relationships_deleted"] += deleted_edges
            report["committed_batches"] += 1


def delete_obsolete_batch(tx, rows):
    aliases = {row["id"] for row in rows}
    for record in tx.run("""MATCH ()-[r:supplies_to|buys_from]->()
            RETURN r.supplied_product AS supplied_product, r.supplied_service AS supplied_service"""):
        for value in record.values():
            if value is not None and aliases.intersection(
                value if isinstance(value, list) else [value]
            ):
                raise ValueError(
                    "Relationship annotations still reference obsolete entity aliases."
                )
    current = {
        record["storage_id"]: dict(record)
        for record in tx.run(
            """UNWIND $rows AS row
        MATCH (n) WHERE elementId(n) = row.storage_id
        RETURN n.iri AS id, elementId(n) AS storage_id, labels(n) AS labels, properties(n) AS properties""",
            rows=rows,
        )
    }
    remaining = []
    for row in rows:
        if row["storage_id"] in current:
            if any(
                row[key] != current[row["storage_id"]][key]
                for key in ("id", "labels", "properties")
            ):
                raise ValueError("Obsolete entity alias changed after preflight.")
            remaining.append(row)
    if not remaining:
        return 0
    result = tx.run(
        """UNWIND $rows AS row MATCH (n) WHERE elementId(n) = row.storage_id
        AND n.iri = row.id AND labels(n) = row.labels AND properties(n) = row.properties
        AND NOT EXISTS { MATCH (n)--() }
        DELETE n RETURN count(*) AS matched""",
        rows=remaining,
    )
    if result.single(strict=True)["matched"] != len(remaining):
        raise ValueError(
            "Obsolete entity alias is changed or connected; transaction must roll back."
        )
    deleted = result.consume().counters.nodes_deleted
    if deleted != len(remaining):
        raise ValueError("Obsolete node deletion count mismatch; transaction must roll back.")
    return deleted


def import_export(directory, batch_size=500):
    if type(batch_size) is not int or not 1 <= batch_size <= 500:
        raise ValueError("Batch size must be between 1 and 500.")
    directory = Path(directory).resolve()
    lock_path = ROOT / "data/.neo4j-import.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another local Neo4j import is active.") from None
        started = time.monotonic()
        report = {
            "status": "preflight",
            "started_at": utc_now(),
            "export": str(directory),
            "nodes_created": 0,
            "nodes_deleted": 0,
            "relationships_created": 0,
            "relationships_deleted": 0,
            "committed_batches": 0,
            "batch_size": batch_size,
            "relationships_evidence_updated": 0,
            "limits": [
                "One frozen export only; conflicting existing provenance is refused.",
                "Entity names merge only within Product/Service/Topic/Sector; events remain separate occurrences.",
                "Successful batches remain committed if a later batch fails; rerun safely resumes.",
            ],
        }
        try:
            schema, originals, assertions, manifest = preflight(directory)
            nodes, aliases = canonicalize_nodes(originals)
            previous_groups = group_assertions(schema, assertions, aliases)
            validate_records(schema, list(nodes.values()), previous_groups)
            edges = [consolidate_evidence(edge) for edge in previous_groups]
            nodes, archive, report["node_provenance_archive"] = archive_node_provenance(
                directory, nodes
            )
            validate_records(schema, list(nodes.values()), edges)
            report.update(
                expected_nodes=len(nodes),
                expected_relationships=len(edges),
                source_assertions=len(assertions),
                output_sha256=manifest["output_sha256"],
                status="connecting",
            )
            with connect() as driver, driver.session(database="neo4j") as session:
                session.execute_read(verify_constraints, schema)
                existing_nodes, existing_edges = session.execute_read(read_graph, schema)
                obsolete, existing_nodes = classify_existing_nodes(
                    originals, aliases, existing_nodes
                )
                existing_nodes = migrate_legacy_baseline(nodes, existing_nodes, archive)
                existing_edges, migrations = evidence_migration_baseline(
                    previous_groups, existing_edges
                )
                compare_graph(nodes, edges, existing_nodes, existing_edges)
                legacy, preserved_edges = classify_existing_edges(
                    schema, assertions, edges, existing_edges, aliases
                )
                report["status"] = "writing"
                for offset in range(0, len(migrations), batch_size):
                    batch = migrations[offset : offset + batch_size]
                    session.execute_write(write_evidence_batch, batch)
                    report["relationships_evidence_updated"] += len(batch)
                    report["committed_batches"] += 1
                write_groups(session, schema, nodes, edges, batch_size, report, archive, legacy)
                if obsolete:
                    report["status"] = "verifying_before_alias_cleanup"
                    checked_nodes, checked_edges = session.execute_read(read_graph, schema)
                    compare_graph(nodes, edges, checked_nodes, checked_edges, complete=True)
                    if any(edge_references(edge) & aliases.keys() for edge in checked_edges):
                        raise ValueError("Relationships still touch obsolete entity aliases.")
                    report["status"] = "removing_obsolete_aliases"
                    for offset in range(0, len(obsolete), batch_size):
                        report["nodes_deleted"] += session.execute_write(
                            delete_obsolete_batch, obsolete[offset : offset + batch_size]
                        )
                        report["committed_batches"] += 1
                report["status"] = "verifying"
                final_nodes, final_edges = session.execute_read(read_graph, schema)
                compare_graph(nodes, edges, final_nodes, final_edges, complete=True)
                final_by_storage = {n["storage_id"]: n for n in final_nodes}
                for node in existing_nodes:
                    compatible_node(node, final_by_storage[node["storage_id"]], complete=True)
                final_by_storage = {e["storage_id"]: e for e in final_edges}
                for edge in preserved_edges:
                    require_same_edge(edge, final_by_storage[edge["storage_id"]])
                old_iris = {n["id"] for n in existing_nodes}
                old_ids = {e["properties"].get("relationship_id") for e in preserved_edges}
                if len(final_nodes) != len(old_iris | nodes.keys()) or len(final_edges) != len(
                    preserved_edges
                ) + sum(e["properties"]["relationship_id"] not in old_ids for e in edges):
                    raise ValueError("Readback graph counts do not reconcile.")
                expected_ids = {edge["properties"]["assertion_id"] for edge in assertions}
                group_ids = {edge["properties"]["relationship_id"] for edge in edges}
                imported = [
                    edge
                    for edge in final_edges
                    if edge["properties"].get("relationship_id") in group_ids
                ]
                final_ids = [
                    record["id"]
                    for edge in imported
                    for record in json.loads(edge["properties"]["evidence"])
                ]
                evidence_count = sum(edge["properties"]["evidence_count"] for edge in imported)
                if (
                    set(final_ids) != expected_ids
                    or len(final_ids) != len(assertions)
                    or evidence_count != len(assertions)
                ):
                    raise ValueError("Readback lost or duplicated original assertion evidence.")
                report.update(
                    status="complete",
                    readback_nodes=len(final_nodes),
                    readback_relationships=len(final_edges),
                    readback_source_assertions=len(final_ids),
                    readback_evidence_count=evidence_count,
                    nodes_by_label=dict(Counter(n["labels"][0] for n in final_nodes)),
                    relationships_by_type=dict(Counter(e["type"] for e in final_edges)),
                )
        except BaseException as exc:
            report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            report.update(
                finished_at=utc_now(), elapsed_seconds=round(time.monotonic() - started, 3)
            )
            save_json(directory / "neo4j_import_report.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Completed de-anonymized export directory")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    print(json.dumps(import_export(args.directory, args.batch_size), indent=2))


if __name__ == "__main__":
    main()
