"""Restore company names and export validated, provenance-preserving graph payloads."""

import argparse
import fcntl
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from picton_graph.extraction.corpus import TERMINAL, base_record, inputs, result_path, verify_resume
from picton_graph.extraction.extractor import build_request, decode_response, validate_extraction
from picton_graph.files import file_hash, save_json, utc_now
from picton_graph.graph.validation import validate_records
from picton_graph.ingestion.masking import company_rules

COMPANY_LABEL = re.compile(r"(?<!\w)company[\s‐‑‒–—-]+([A-J])(?!\w)(?!\.[A-Za-z]\.)", re.I)


def restore_names(text, mapping):
    if text is None:
        return None
    return COMPANY_LABEL.sub(lambda m: mapping[f"Company {m[1].upper()}"]["company_name"], text)


def graph_payload(record, chunk, checked, mapping, ontology):
    document_iri = checked["document_iri"]
    document = {
        "id": document_iri,
        "labels": ["SourceDocument"],
        "properties": {
            "iri": document_iri,
            "name": chunk["document_id"],
            "url": chunk["source_url"],
        },
    }
    nodes, by_name = [document], {("SourceDocument", "document"): document}
    for entity in checked["entities"]:
        kind, name, iri = entity["type"], entity["name"], entity["resolved_iri"]
        properties = {"iri": iri, "name": restore_names(name, mapping)}
        if kind == "Company":
            company = mapping[name]
            if iri != company["company_iri"]:
                raise ValueError("Company identifier differs from the frozen mapping.")
            properties.update(name=company["company_name"], ticker=company["ticker"])
        if kind == "Event":
            properties["event_type"] = entity["event_type"]
        node = {
            "id": iri,
            "labels": [kind],
            "properties": properties,
            "evidence": entity["evidence"],
            "time_text": restore_names(entity["time_text"], mapping),
        }
        nodes.append(node)
        by_name[(kind, name)] = node
    relationships = []
    for assertion in checked["relationships"]:
        source, target = (
            by_name[(assertion[field]["type"], assertion[field]["name"])]
            for field in ("source", "target")
        )
        properties = {
            "supported_by": document_iri,
            "evidence_text": assertion["evidence"]["original_quote"],
        }
        for field, kind in (("supplied_product", "Product"), ("supplied_service", "Service")):
            if assertion[field]:
                properties[field] = [by_name[(kind, name)]["id"] for name in assertion[field]]
        relationships.append(
            {
                "source": source["id"],
                "target": target["id"],
                "type": assertion["type"],
                "source_name": source["properties"]["name"],
                "target_name": target["properties"]["name"],
                "properties": properties,
                "evidence": assertion["evidence"],
                "time_text": restore_names(assertion["time_text"], mapping),
            }
        )
    validate_records(ontology, nodes, relationships)
    provenance = {
        key: record[key]
        for key in (
            "index",
            "chunk_id",
            "document_id",
            "section_id",
            "source_url",
            "source_sha256",
            "filing_date",
            "input_sha256",
            "request_sha256",
        )
    }
    provenance.update(
        ticker=chunk["ticker"],
        form=chunk["form"],
        document_type=chunk["document_type"],
        role=chunk["role"],
    )
    return {"provenance": provenance, "nodes": nodes, "relationships": relationships}


def export_run(source, output=None):
    source = Path(source).resolve()
    output = Path(output).resolve() if output is not None else source / "deanonymized"
    with (source / ".run.lock").open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("The source run is active; export only a completed run.") from None
        source_hash = file_hash(source / "manifest.json")
        original = json.loads((source / "manifest.json").read_text())
        if (
            original.get("status") not in ("complete", "complete_with_errors")
            or type(original.get("total_chunks")) is not int
            or original["total_chunks"] < 1
            or original.get("processed") != original["total_chunks"]
        ):
            raise ValueError("Export requires a completed corpus run.")
        manifest, ontology, prompt, mapping = verify_resume(source, original["config"], None)
        rules, _ = company_rules(
            [
                {
                    "ticker": value["ticker"],
                    "name": value["company_name"],
                    "iri": value["company_iri"],
                }
                for value in mapping.values()
            ]
        )
        # Equal-length regex alternatives can change order without changing company identity.
        identity_fields = ("masked_name", "ticker", "company_name", "company_iri")
        if set(mapping) != {rule["masked_name"] for rule in rules} or any(
            mapping[rule["masked_name"]].get(field) != rule[field]
            for rule in rules
            for field in identity_fields
        ):
            raise ValueError("Frozen company mapping does not match the agreed ten-company labels.")
        expected_files = {f"{index:06d}.json" for index in range(1, manifest["total_chunks"] + 1)}
        if {p.name for p in (source / "results").iterdir()} != expected_files:
            raise ValueError("Source result files are missing or unexpected.")
        output.mkdir(parents=True, exist_ok=False)
        report = {
            "status": "exporting",
            "created_at": utc_now(),
            "source_run": str(source),
            "source_manifest_sha256": source_hash,
            "source_files": manifest["files"],
            "exporter_sha256": file_hash(Path(__file__)),
            "format": "graph-payload-jsonl-v1",
            "limits": [
                "Only valid extraction results are exported; validation is not a semantic accuracy score.",
                "Company and SourceDocument records repeat across chunks; merge nodes by iri on import.",
                "Non-company identities remain chunk-specific; relationships retain each supported assertion.",
                "Evidence and time_text outside properties are provenance, not additional Neo4j properties.",
                "Only canonical edges are exported; no inverse edges or Neo4j writes are performed.",
            ],
        }
        save_json(output / "manifest.json", report)
        counts, node_types, edge_types = Counter(), Counter(), Counter()
        node_iris, chunk_ids, documents = set(), set(), set()
        result_digest = hashlib.sha256()
        rows = empty = 0
        temporary = output / "assertions.jsonl.partial"
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                for index, chunk in inputs(source):
                    if chunk["chunk_id"] in chunk_ids:
                        raise ValueError("Duplicate source chunk identifier.")
                    chunk_ids.add(chunk["chunk_id"])
                    documents.add(chunk["document_id"])
                    path = result_path(source, index)
                    content = path.read_bytes()
                    result_digest.update(
                        path.name.encode()
                        + b":"
                        + hashlib.sha256(content).hexdigest().encode()
                        + b"\n"
                    )
                    record = json.loads(content)
                    request = build_request(
                        chunk, ontology, prompt, manifest["config"]["max_tokens"]
                    )
                    expected = base_record(index, chunk, request)
                    if any(record.get(key) != value for key, value in expected.items()):
                        raise ValueError(
                            f"Source result/request differs from frozen input: {path.name}"
                        )
                    status = record.get("status")
                    if status not in TERMINAL or (
                        status in ("valid", "rejected") and "response" not in record
                    ):
                        raise ValueError(f"Incomplete or malformed result: {path.name}")
                    counts[status] += 1
                    if status != "valid":
                        continue
                    checked = validate_extraction(
                        decode_response(record["response"]), chunk, mapping, ontology
                    )
                    if checked != record.get("validated"):
                        raise ValueError(
                            f"Saved validation differs from the raw response: {path.name}"
                        )
                    payload = graph_payload(record, chunk, checked, mapping, ontology)
                    payload["provenance"]["source_result_sha256"] = hashlib.sha256(
                        content
                    ).hexdigest()
                    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    rows += 1
                    empty += not checked["entities"] and not checked["relationships"]
                    node_types.update(node["labels"][0] for node in payload["nodes"])
                    node_iris.update(node["id"] for node in payload["nodes"])
                    edge_types.update(edge["type"] for edge in payload["relationships"])
                    if index % 1000 == 0:
                        print(
                            f"Checked {index}/{manifest['total_chunks']} chunks; exported {rows}.",
                            flush=True,
                        )
            actual_counts = {
                status: counts[status] for status in (*TERMINAL, "not_started", "in_flight")
            }
            if (
                actual_counts != manifest["counts"]
                or len(chunk_ids) != manifest["total_chunks"]
                or len(documents) != manifest["total_documents"]
            ):
                raise ValueError("Source counts do not reconcile with its completed manifest.")
            if file_hash(source / "manifest.json") != source_hash:
                raise ValueError("Source manifest changed during export.")
            for name, digest in manifest["files"].items():
                if file_hash(source / name) != digest:
                    raise ValueError(f"Frozen source changed during export: {name}")
            temporary.replace(output / "assertions.jsonl")
            report.update(
                status="complete",
                finished_at=utc_now(),
                source_counts=actual_counts,
                exported_chunks=rows,
                empty_extractions=empty,
                omitted_results={
                    status: counts[status] for status in TERMINAL if status != "valid"
                },
                node_records_by_type=dict(node_types),
                unique_node_iris=len(node_iris),
                relationship_assertions_by_type=dict(edge_types),
                relationship_assertions=sum(edge_types.values()),
                source_results_sha256=result_digest.hexdigest(),
                output_sha256=file_hash(output / "assertions.jsonl"),
            )
            save_json(output / "manifest.json", report)
        except BaseException as exc:
            report.update(status="failed", finished_at=utc_now(), error_type=type(exc).__name__)
            save_json(output / "manifest.json", report)
            raise
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Completed corpus run directory")
    parser.add_argument(
        "--output", type=Path, help="New output directory; defaults to SOURCE/deanonymized"
    )
    args = parser.parse_args()
    print(export_run(args.source, args.output))


if __name__ == "__main__":
    main()
