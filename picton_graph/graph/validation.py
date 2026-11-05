"""Application checks for domain rules that Neo4j Community cannot enforce."""

import hashlib
import json
import re
from datetime import date, datetime

from neo4j.time import DateTime


def json_records(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a JSON string.")
    try:
        records = json.loads(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be valid JSON.") from exc
    if not isinstance(records, list) or not records:
        raise ValueError(f"{field} must contain a non-empty list.")
    return records


def register_assertion(identifier, assertion_ids, field):
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest.")
    if identifier in assertion_ids:
        raise ValueError("Duplicate assertion_id.")
    assertion_ids.add(identifier)


def validate_provenance(value, *, with_assertions=False):
    contexts = json_records(value, "provenance_json")
    fields = {"source", "evidence", "time_text"} | ({"assertion"} if with_assertions else set())
    for context in contexts:
        if not isinstance(context, dict) or set(context) != fields:
            raise ValueError("Invalid provenance context fields.")
        source = context["source"]
        if not isinstance(source, dict) or any(
            not isinstance(source.get(key), str) or not source[key].strip()
            for key in ("chunk_id", "document_id")
        ):
            raise ValueError("Provenance needs source chunk_id and document_id.")
        time_text = context["time_text"]
        if time_text is not None and (not isinstance(time_text, str) or not time_text.strip()):
            raise ValueError("Provenance time_text must be null or a non-empty string.")
        evidence = context["evidence"]
        if evidence is None:
            continue
        if not isinstance(evidence, dict) or evidence.get("match_type") not in (
            "exact",
            "formatting_normalized",
        ):
            raise ValueError("Invalid provenance evidence match_type.")
        quote = evidence.get("original_quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("Provenance evidence needs an original_quote.")
        for prefix in ("original", "masked"):
            start, end = evidence.get(prefix + "_start"), evidence.get(prefix + "_end")
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError("Invalid provenance evidence offsets.")
        if evidence["original_end"] - evidence["original_start"] != len(quote):
            raise ValueError("Provenance original quote length disagrees with its offsets.")
    return contexts


def validate_annotations(schema, kind, properties, by_iri):
    for key, value in properties.items():
        annotation = schema["annotations"].get(key)
        if annotation is None:
            raise ValueError(f"Unknown relationship property: {key}")
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, str):
                raise ValueError(f"Annotation {key} must contain strings.")
            if "reference_class" in annotation:
                referenced = by_iri.get(item)
                if referenced is None or annotation["reference_class"] not in referenced["labels"]:
                    raise ValueError(
                        f"Annotation {key} must reference a {annotation['reference_class']} node."
                    )
        if key in ("supplied_product", "supplied_service") and kind not in (
            "supplies_to",
            "buys_from",
        ):
            raise ValueError(f"{key} applies to supply relationships only.")


def validate_compact_evidence(schema, kind, properties, by_iri, assertion_ids, document_urls):
    if properties.keys() & {"provenance_json", "evidence_text", "supported_by"}:
        raise ValueError("Compact evidence cannot coexist with legacy evidence properties.")
    records = json_records(properties["evidence"], "evidence")
    if type(properties["evidence_count"]) is not int or properties["evidence_count"] != len(
        records
    ):
        raise ValueError("evidence_count must equal the number of preserved assertions.")
    required = {"id", "evidence_text", "document", "document_type", "document_date"}
    qualifiers = {"supplied_product", "supplied_service"}
    unions = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or not required <= record.keys()
            or record.keys() - required - qualifiers - {"time_text"}
        ):
            raise ValueError("Invalid compact evidence fields.")
        register_assertion(record["id"], assertion_ids, "Evidence id")
        for key in ("evidence_text", "document", "document_type", "time_text"):
            if key in record and (not isinstance(record[key], str) or not record[key].strip()):
                raise ValueError(f"Evidence {key} must be a non-empty string.")
        if record["document"] not in document_urls:
            raise ValueError("Evidence document must reference an existing SourceDocument URL.")
        document_date = record["document_date"]
        if document_date is not None:
            if not isinstance(document_date, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", document_date
            ):
                raise ValueError("Evidence document_date must be an ISO date or null.")
            try:
                date.fromisoformat(document_date)
            except ValueError as exc:
                raise ValueError("Evidence document_date must be a valid calendar date.") from exc
        annotations = {key: record[key] for key in qualifiers if key in record}
        for key, values in annotations.items():
            if not isinstance(values, list) or not values:
                raise ValueError(f"Evidence {key} must be a non-empty list.")
        validate_annotations(schema, kind, annotations, by_iri)
        for key, values in annotations.items():
            unions.setdefault(key, set()).update(values)
    annotations = {
        key: value
        for key, value in properties.items()
        if key not in ("relationship_id", "evidence_count", "evidence")
    }
    validate_annotations(schema, kind, annotations, by_iri)
    if annotations != {key: sorted(values) for key, values in unions.items()}:
        raise ValueError("Grouped annotations must equal the unions of the evidence records.")


def validate_group(schema, kind, source, target, properties, by_iri, assertion_ids, document_urls):
    evidence_field = "evidence" if "evidence" in properties else "provenance_json"
    metadata = {"relationship_id", "evidence_count", evidence_field}
    if not metadata <= properties.keys() or "assertion_id" in properties:
        raise ValueError(
            f"Grouped relationships need relationship_id, evidence_count and {evidence_field} only."
        )
    source_iri, target_iri = source["properties"]["iri"], target["properties"]["iri"]
    symmetric = schema["relationships"][kind].get("symmetric", False)
    if symmetric and source_iri > target_iri:
        raise ValueError("Symmetric grouped relationships need canonical endpoint order.")
    identity = {"source": source_iri, "type": kind, "target": target_iri}
    expected_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    if properties["relationship_id"] != expected_id:
        raise ValueError("relationship_id disagrees with its canonical endpoints and type.")
    if evidence_field == "evidence":
        validate_compact_evidence(schema, kind, properties, by_iri, assertion_ids, document_urls)
        return
    contexts = validate_provenance(properties["provenance_json"], with_assertions=True)
    if type(properties["evidence_count"]) is not int or properties["evidence_count"] != len(
        contexts
    ):
        raise ValueError("evidence_count must equal the number of preserved assertions.")
    unions = {}
    for context in contexts:
        assertion = context["assertion"]
        required = {"id", "source", "target", "properties"}
        if (
            not isinstance(assertion, dict)
            or not required <= assertion.keys()
            or assertion.keys() - required - {"resolved"}
        ):
            raise ValueError("Invalid grouped assertion fields.")
        register_assertion(assertion["id"], assertion_ids, "Grouped assertion id")
        original = assertion["properties"]
        if not isinstance(original, dict):
            raise ValueError("Grouped assertion properties must be an object.")
        resolved = assertion.get("resolved", {})
        if not isinstance(resolved, dict) or not all(
            isinstance(key, str) and isinstance(value, str) and key and value
            for key, value in resolved.items()
        ):
            raise ValueError("Assertion resolved aliases must map original IRIs to canonical IRIs.")
        for resolved_iri in resolved.values():
            canonical_node = by_iri.get(resolved_iri)
            if canonical_node is None or not set(canonical_node["labels"]) <= {
                "Product",
                "Service",
                "Sector",
                "Topic",
            }:
                raise ValueError(
                    "Resolved aliases must reference canonical Product, Service, Sector or Topic nodes."
                )
        referenced = [assertion["source"], assertion["target"]]
        mapped = dict(original)
        for key, value in original.items():
            if "reference_class" in schema["annotations"].get(key, {}):
                values = value if isinstance(value, list) else [value]
                if not all(isinstance(item, str) for item in values):
                    raise ValueError(f"Annotation {key} must contain strings.")
                referenced.extend(values)
                converted = [resolved.get(item, item) for item in values]
                mapped[key] = converted if isinstance(value, list) else converted[0]
        if not all(isinstance(value, str) for value in referenced) or resolved.keys() - set(
            referenced
        ):
            raise ValueError("Assertion resolved aliases contain unrelated original IRIs.")
        pair = (
            resolved.get(assertion["source"], assertion["source"]),
            resolved.get(assertion["target"], assertion["target"]),
        )
        allowed_pairs = {(source_iri, target_iri)}
        if symmetric:
            allowed_pairs.add((target_iri, source_iri))
        if not all(isinstance(value, str) for value in pair) or pair not in allowed_pairs:
            raise ValueError("Grouped assertion endpoints disagree with the relationship.")
        validate_annotations(schema, kind, mapped, by_iri)
        evidence = context["evidence"]
        if evidence is None or original.get("evidence_text") != evidence["original_quote"]:
            raise ValueError("Grouped assertion evidence_text must match its original quote.")
        for key, value in mapped.items():
            unions.setdefault(key, set()).update(value if isinstance(value, list) else [value])
    annotations = {key: value for key, value in properties.items() if key not in metadata}
    validate_annotations(schema, kind, annotations, by_iri)
    if annotations != {key: sorted(values) for key, values in unions.items()}:
        raise ValueError("Grouped annotations must equal the unions of the original assertions.")


def validate_records(schema, nodes, relationships):
    by_id, by_iri = {}, {}
    for node in nodes:
        labels, properties = set(node["labels"]), node["properties"]
        if not labels or labels - schema["classes"].keys():
            raise ValueError("Node has missing or unknown domain labels.")
        if any(set(pair) <= labels for pair in schema["disjoint_classes"]):
            raise ValueError("Node combines disjoint classes.")
        iri = properties.get("iri")
        if not isinstance(iri, str) or not iri.strip():
            raise ValueError("Each node needs a non-empty string iri as its storage identifier.")
        if iri in by_iri or node["id"] in by_id:
            raise ValueError("Duplicate node identifier.")
        by_id[node["id"]], by_iri[iri] = node, node
        for key, value in properties.items():
            if key == "provenance_ref":
                if "provenance_json" in properties:
                    raise ValueError("Node provenance must be inline or referenced, not both.")
                if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                    raise ValueError("provenance_ref must contain a lowercase SHA-256 digest.")
                continue
            if key == "provenance_json":
                validate_provenance(value)
                continue
            if key in ("iri", "name", "description"):
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a string.")
                continue
            rule = schema["data_properties"].get(key)
            if rule is None or not labels.intersection(rule["domain_any_of"]):
                raise ValueError(f"Property {key} is not defined for these node labels.")
            expected = (datetime, DateTime) if rule["datatype"].endswith("#dateTime") else (str,)
            values = value if isinstance(value, list) else [value]
            if not all(isinstance(item, expected) for item in values):
                raise ValueError(f"Property {key} has the wrong datatype.")

    document_urls = set()
    for node in by_iri.values():
        if "SourceDocument" in node["labels"] and "url" in node["properties"]:
            urls = node["properties"]["url"]
            document_urls.update(urls if isinstance(urls, list) else [urls])
    assertion_ids, relationship_ids = set(), set()
    for relationship in relationships:
        kind = relationship["type"]
        rule = schema["relationships"].get(kind)
        if rule is None:
            raise ValueError(f"Unknown relationship type: {kind}")
        source, target = by_id.get(relationship["source"]), by_id.get(relationship["target"])
        if source is None or target is None:
            raise ValueError("Relationship endpoint is missing.")
        if not set(source["labels"]).intersection(rule["domain_any_of"]):
            raise ValueError(f"Invalid source class for {kind}.")
        if not set(target["labels"]).intersection(rule["range_any_of"]):
            raise ValueError(f"Invalid target class for {kind}.")
        if rule.get("irreflexive") and relationship["source"] == relationship["target"]:
            raise ValueError(f"{kind} cannot link a node to itself.")
        properties = relationship["properties"]
        if properties.keys() & {"relationship_id", "evidence_count", "evidence"}:
            validate_group(
                schema, kind, source, target, properties, by_iri, assertion_ids, document_urls
            )
            identifier = properties["relationship_id"]
            if identifier in relationship_ids:
                raise ValueError("Duplicate relationship_id.")
            relationship_ids.add(identifier)
            continue
        if ("assertion_id" in properties) != ("provenance_json" in properties):
            raise ValueError(
                "Relationship assertion_id and provenance_json must be supplied together."
            )
        if "assertion_id" in properties:
            validate_provenance(properties["provenance_json"])
            register_assertion(properties["assertion_id"], assertion_ids, "assertion_id")
        validate_annotations(
            schema,
            kind,
            {
                key: value
                for key, value in properties.items()
                if key not in ("assertion_id", "provenance_json")
            },
            by_iri,
        )


def validate_graph(tx, schema):
    # shortcut: scan the small take-home graph; use affected-subgraph validation for large ingestion jobs.
    nodes = list(
        tx.run(
            "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS properties"
        )
    )
    relationships = list(
        tx.run("""
        MATCH (a)-[r]->(b)
        RETURN elementId(a) AS source, elementId(b) AS target,
               type(r) AS type, properties(r) AS properties
    """)
    )
    validate_records(schema, nodes, relationships)
