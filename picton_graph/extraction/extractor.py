"""Extract reviewable ontology assertions from masked DuckDB chunks via OpenRouter."""

import argparse
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from uuid import uuid4

import duckdb

from picton_graph.config import PROMPT_PATH, ROOT, SCHEMA_PATH
from picton_graph.extraction.client import call_openrouter
from picton_graph.files import save_json, utc_now
from picton_graph.graph.validation import validate_records
from picton_graph.ingestion.masking import TICKERS, company_rules, mask_text
from picton_graph.ingestion.storage import DATABASE

MODEL = "openai/gpt-6-luna"
PROMPT = PROMPT_PATH
RELATIONSHIPS = (
    "supplies_to",
    "competes_with",
    "offers",
    "provides",
    "operates_in",
    "has_topic",
    "concerns",
    "reports",
    "mentions",
)
ENTITY_TYPES = ("Company", "Product", "Service", "Sector", "Topic", "Event")
OUTPUT_FORMAT = "named-entities-v1"
EXTRACTION_SOURCES = (
    "picton_graph/__init__.py",
    "picton_graph/config.py",
    "picton_graph/files.py",
    "picton_graph/extraction/__init__.py",
    "picton_graph/extraction/client.py",
    "picton_graph/extraction/extractor.py",
    "picton_graph/ingestion/__init__.py",
    "picton_graph/ingestion/masking.py",
    "picton_graph/ingestion/storage.py",
    "picton_graph/graph/__init__.py",
    "picton_graph/graph/connection.py",
    "picton_graph/graph/validation.py",
)


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string"}
NULLABLE_TEXT = {"type": ["string", "null"]}
REFERENCES = {"type": "array", "items": TEXT}
ENDPOINT_REFERENCE = object_schema(
    {
        "type": {"type": "string", "enum": [*ENTITY_TYPES, "SourceDocument"]},
        "name": TEXT,
    }
)
OUTPUT_SCHEMA = object_schema(
    {
        "entities": {
            "type": "array",
            "items": object_schema(
                {
                    "type": {"type": "string", "enum": list(ENTITY_TYPES)},
                    "name": TEXT,
                    "evidence_text": TEXT,
                    "event_type": NULLABLE_TEXT,
                    "time_text": NULLABLE_TEXT,
                }
            ),
        },
        "relationships": {
            "type": "array",
            "items": object_schema(
                {
                    "source": ENDPOINT_REFERENCE,
                    "type": {"type": "string", "enum": list(RELATIONSHIPS)},
                    "target": ENDPOINT_REFERENCE,
                    "evidence_text": TEXT,
                    "time_text": NULLABLE_TEXT,
                    "supplied_product": REFERENCES,
                    "supplied_service": REFERENCES,
                }
            ),
        },
    }
)


def load_ontology():
    ontology = json.loads(SCHEMA_PATH.read_text())
    covered = set(RELATIONSHIPS) | {ontology["relationships"][r]["inverse"] for r in RELATIONSHIPS}
    if set(ontology["classes"]) != {*ENTITY_TYPES, "SourceDocument"} or covered != set(
        ontology["relationships"]
    ):
        raise ValueError("Ontology changed; review the extraction contract before running.")
    return ontology


def select_chunks(database, ticker, limit=1, form=None, role="primary", document_id=None):
    if (
        (ticker is not None and ticker not in TICKERS)
        or (limit is not None and (type(limit) is not int or limit < 1))
        or role not in ("primary", "exhibit", "all")
    ):
        raise ValueError(
            "Select a basket ticker or None, a positive limit or None, and a valid document role."
        )
    if not Path(database).is_file():
        raise ValueError("DuckDB database does not exist.")
    with duckdb.connect(str(database), read_only=True) as db:
        companies = [
            dict(zip(("ticker", "name", "iri", "masked_name"), r))
            for r in db.execute(
                "SELECT ticker, company_name, company_iri, masked_name FROM company_masks"
            ).fetchall()
        ]
        rules, matcher = company_rules(companies)
        mapping = {r["masked_name"]: r for r in rules}
        if {c["ticker"]: c["masked_name"] for c in companies} != {
            r["ticker"]: r["masked_name"] for r in rules
        }:
            raise ValueError("Stored company labels conflict with the agreed mapping.")
        where, parameters = ["TRUE"], []
        for column, value in (
            ("d.ticker", ticker),
            ("d.form", form),
            ("d.role", role if role != "all" else None),
            ("d.document_id", document_id),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                parameters.append(value)
        query = f"""
            SELECT c.chunk_id, c.section_id, c.text, c.masked_text, c.mask_spans,
                   s.title, s.masked_title, s.title_mask_spans, c.contains_table,
                   d.document_id, d.ticker, d.form, d.document_type, d.role,
                   d.filing_date, d.source_url, d.source_sha256, d.metadata
            FROM chunks c JOIN sections s ON c.section_id = s.section_id AND c.document_id = s.document_id
            JOIN documents d ON c.document_id = d.document_id
            WHERE {" AND ".join(where)}
            ORDER BY d.filing_date DESC, d.document_id, s.section_index, c.chunk_index
        """
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        result = db.execute(query, parameters)
        columns = [c[0] for c in result.description]
        chunks = [dict(zip(columns, r)) for r in result.fetchall()]
    if not chunks:
        raise ValueError("No chunks match the selection.")
    for chunk in chunks:
        for original, masked, spans in (
            ("text", "masked_text", "mask_spans"),
            ("title", "masked_title", "title_mask_spans"),
        ):
            expected_text, expected_spans = mask_text(chunk[original], matcher, rules)
            if (
                chunk[masked] != expected_text
                or chunk[spans] is None
                or json.loads(chunk[spans]) != expected_spans
            ):
                raise ValueError(
                    "Missing or stale masking; rerun python -m picton_graph.ingestion.masking before extraction."
                )
            chunk[spans] = expected_spans
        chunk["filing_date"] = chunk["filing_date"].isoformat()
        chunk["metadata"] = json.loads(chunk["metadata"])
        chunk["issuer"] = next(r["masked_name"] for r in rules if r["ticker"] == chunk["ticker"])
    return chunks, mapping


def build_request(chunk, ontology, prompt, max_tokens=8192):
    definitions = {
        "node_types": {k: v["description"] for k, v in ontology["classes"].items()},
        "relationships": {
            r: {
                k: ontology["relationships"][r][k]
                for k in ("description", "domain_any_of", "range_any_of")
            }
            for r in RELATIONSHIPS
        },
    }
    # Source identifiers, URLs, original text and the company lookup never enter messages.
    context = {
        "document_ref": "document",
        "filing_issuer": chunk["issuer"],
        "document_role": chunk["role"],
        "form": chunk["form"],
        "document_type": chunk["document_type"],
        "filing_date": chunk["filing_date"],
        "report_date": chunk["metadata"]["source"].get("report_date"),
        "section_heading": chunk["masked_title"],
        "contains_table": chunk["contains_table"],
        "chunk_text": chunk["masked_text"],
    }
    return {
        "model": MODEL,
        "stream": False,
        "max_tokens": max_tokens,
        "provider": {"require_parameters": True},
        "messages": [
            {
                "role": "system",
                "content": prompt
                + "\n\nAUTHORITATIVE ONTOLOGY\n"
                + json.dumps(definitions, ensure_ascii=False),
            },
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "filing_relationships",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            },
        },
    }


def decode_response(response):
    if not isinstance(response, dict) or response.get("error"):
        raise ValueError("OpenRouter returned an error response.")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("Expected one complete OpenRouter response.")
    choice = choices[0]
    message = choice.get("message")
    if (
        choice.get("finish_reason") != "stop"
        or not isinstance(message, dict)
        or message.get("refusal")
        or message.get("tool_calls")
        or not isinstance(message.get("content"), str)
    ):
        raise ValueError("Incomplete, refused, or non-text extraction; nothing was accepted.")
    try:
        return json.loads(message["content"])
    except json.JSONDecodeError:
        raise ValueError("Extraction was not valid JSON; no automatic repair was made.") from None


def check_shape(value, schema):
    """Validate the small JSON Schema subset used by our output contract, offline."""
    expected = schema["type"]
    if expected == ["string", "null"] and value is None:
        return
    if expected in ("string", ["string", "null"]):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Expected a non-empty string or an explicitly permitted null.")
    elif expected == "array":
        if not isinstance(value, list):
            raise ValueError("Expected an array.")
        for item in value:
            check_shape(item, schema["items"])
    elif expected == "object":
        if not isinstance(value, dict) or set(value) != set(schema["properties"]):
            raise ValueError("Missing or unexpected extraction fields.")
        for key, child in schema["properties"].items():
            check_shape(value[key], child)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Unknown entity or relationship type.")


NUMBER = r"(?:[$€£¥]\s*)?(?:\d+(?:[.,]\d+)*|[.,]\d+)(?:\s*[%‰‱٪％])?"
MATCH_TOKEN = re.compile(
    rf"\(\s*[+\-]?\s*{NUMBER}\s*\)|[+\-]\s*{NUMBER}|{NUMBER}|[^\W_]+[+#]*|[^\w\s]"
)
PROSE_PUNCTUATION = frozenset(".,;:!?\"'“”‘’…-()[]{}")


def matching_tokens(text):
    """Ignore prose formatting but retain numbers, signs, units and symbols."""
    # One-character replacements retain source offsets and protect Unicode minus signs.
    comparable = "".join("-" if unicodedata.category(c) == "Pd" or c == "−" else c for c in text)
    return [
        (re.sub(r"\s+", "", match.group()).casefold(), match.start(), match.end())
        for match in MATCH_TOKEN.finditer(comparable)
        if match.group() not in PROSE_PUNCTUATION
    ]


def matching_span(quote, text):
    wanted = [token for token, _, _ in matching_tokens(quote)]
    if not wanted:
        return None
    tokens = matching_tokens(text)
    start = text.find(quote)
    end = start + len(quote)
    if start >= 0 and not any(a < start < b or a < end < b for _, a, b in tokens):
        return start, start + len(quote)
    for index in range(len(tokens) - len(wanted) + 1):
        if (
            tokens[index][0] == wanted[0]
            and [t[0] for t in tokens[index : index + len(wanted)]] == wanted
        ):
            return tokens[index][1], tokens[index + len(wanted) - 1][2]
    return None


def contains_text(phrase, text):
    # Keep literal name/time containment, including filing footnotes and parenthesized years.
    return phrase in text or matching_span(phrase, text) is not None


def evidence(quote, chunk):
    span = matching_span(quote, chunk["masked_text"])
    if span is None:
        raise ValueError(
            "Evidence must be an exact or formatting-normalized, non-empty quote from the masked chunk."
        )
    start, end = span
    for span in chunk["mask_spans"]:
        if (
            span["masked_start"] < start < span["masked_end"]
            or span["masked_start"] < end < span["masked_end"]
        ):
            raise ValueError("Evidence cannot cut through a masked company label.")

    def original_offset(position):
        return position + sum(
            (s["end"] - s["start"]) - (s["masked_end"] - s["masked_start"])
            for s in chunk["mask_spans"]
            if s["masked_end"] <= position
        )

    original_start, original_end = original_offset(start), original_offset(end)
    return {
        "match_type": "exact"
        if chunk["masked_text"][start:end] == quote
        else "formatting_normalized",
        "masked_start": start,
        "masked_end": end,
        "original_start": original_start,
        "original_end": original_end,
        "original_quote": chunk["text"][original_start:original_end],
    }


def validate_extraction(result, chunk, mapping, ontology):
    check_shape(result, OUTPUT_SCHEMA)
    source_iri = "urn:picton:document:" + hashlib.sha256(chunk["document_id"].encode()).hexdigest()
    prefix = "urn:picton:chunk:" + hashlib.sha256(chunk["chunk_id"].encode()).hexdigest() + ":"
    nodes = [
        {
            "id": source_iri,
            "labels": ["SourceDocument"],
            "properties": {"iri": source_iri, "url": chunk["source_url"]},
        }
    ]
    by_name, checked_entities = {("SourceDocument", "document"): nodes[0]}, []
    for entity in result["entities"]:
        key = (entity["type"], entity["name"])
        if key in by_name:
            raise ValueError(
                "Duplicate entity type/name; distinguish separate event occurrences by name."
            )
        proof = evidence(entity["evidence_text"], chunk)
        # Internal graph identifiers are derived in Python, never invented by the model.
        properties = {
            "iri": prefix + hashlib.sha256(json.dumps(key).encode()).hexdigest(),
            "name": entity["name"],
        }
        if entity["type"] == "Company":
            company = mapping.get(entity["name"])
            if company is None:
                raise ValueError("Only the ten masked basket companies can be Company nodes.")
            properties = {
                "iri": company["company_iri"],
                "name": company["company_name"],
                "ticker": company["ticker"],
            }
        elif entity["type"] != "Event" and not contains_text(
            entity["name"], entity["evidence_text"]
        ):
            raise ValueError(
                "Product, Service, Sector and Topic names must occur in their evidence (formatting differences only)."
            )
        if entity["type"] == "Event":
            if entity["event_type"] is None or entity["time_text"] is None:
                raise ValueError("Events need an event type and an evidenced time expression.")
            if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", entity["event_type"]):
                raise ValueError("Event types must use snake_case.")
            properties["event_type"] = entity["event_type"]
        elif entity["event_type"] is not None or entity["time_text"] is not None:
            raise ValueError("Only Event entities have event_type and time_text.")
        if entity["time_text"] is not None and not contains_text(
            entity["time_text"], entity["evidence_text"]
        ):
            raise ValueError("Event time must occur in its evidence (formatting differences only).")
        node = {"id": properties["iri"], "labels": [entity["type"]], "properties": properties}
        nodes.append(node)
        by_name[key] = node
        checked_entities.append({**entity, "evidence": proof, "resolved_iri": properties["iri"]})
    relationships, checked_relationships, seen = [], [], set()
    for relationship in result["relationships"]:
        endpoint_keys = tuple(
            (relationship[field]["type"], relationship[field]["name"])
            for field in ("source", "target")
        )
        if any(key not in by_name for key in endpoint_keys):
            raise ValueError(
                "Relationship endpoint type/name is missing from the extracted entities."
            )
        proof = evidence(relationship["evidence_text"], chunk)
        if relationship["time_text"] is not None and not contains_text(
            relationship["time_text"], relationship["evidence_text"]
        ):
            raise ValueError(
                "Relationship time must occur in its evidence (formatting differences only)."
            )
        properties = {"supported_by": source_iri, "evidence_text": proof["original_quote"]}
        for field, kind in (("supplied_product", "Product"), ("supplied_service", "Service")):
            refs = relationship[field]
            if len(refs) != len(set(refs)):
                raise ValueError("Duplicate supplied product/service reference.")
            if refs and relationship["type"] != "supplies_to":
                raise ValueError("Only supplies_to can carry supplied product/service references.")
            for ref in refs:
                if (kind, ref) not in by_name:
                    raise ValueError(
                        "Supplied product/service reference has the wrong type or is missing."
                    )
                if not contains_text(ref, relationship["evidence_text"]):
                    raise ValueError(
                        "The supplied product/service must be named in the supply quote."
                    )
            if refs:
                properties[field] = [by_name[(kind, ref)]["properties"]["iri"] for ref in refs]
        endpoints = endpoint_keys
        if relationship["type"] == "competes_with":
            endpoints = tuple(sorted(endpoints))
        quote_key = tuple(token for token, _, _ in matching_tokens(proof["original_quote"]))
        key = (
            endpoints,
            relationship["type"],
            quote_key,
            relationship["time_text"],
            tuple(sorted(relationship["supplied_product"])),
            tuple(sorted(relationship["supplied_service"])),
        )
        if key in seen:
            raise ValueError("Duplicate assertion, including a repeated symmetric edge.")
        seen.add(key)
        relationships.append(
            {
                "source": by_name[endpoint_keys[0]]["id"],
                "target": by_name[endpoint_keys[1]]["id"],
                "type": relationship["type"],
                "properties": properties,
            }
        )
        checked_relationships.append({**relationship, "evidence": proof})
    validate_records(ontology, nodes, relationships)
    # These are review candidates: temporal qualifiers are not Neo4j schema properties.
    return {
        "entities": checked_entities,
        "relationships": checked_relationships,
        "document_iri": source_iri,
    }


def run_extraction(
    database,
    ticker,
    output_root,
    *,
    limit=1,
    form=None,
    role="primary",
    document_id=None,
    max_tokens=8192,
    dry_run=False,
):
    if type(max_tokens) is not int or max_tokens < 1:
        raise ValueError("max_tokens must be positive.")
    ontology, prompt = load_ontology(), PROMPT.read_text()
    chunks, mapping = select_chunks(database, ticker, limit, form, role, document_id)
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not dry_run and not api_key:
        raise ValueError("Set OPENROUTER_API_KEY in the process environment.")
    directory = Path(output_root) / str(uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    manifest = {
        "created_at": utc_now(),
        "model": MODEL,
        "status": "preview" if dry_run else "running",
        "output_format": OUTPUT_FORMAT,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "selected_chunks": len(chunks),
        "validated_chunks": 0,
        "company_mapping": mapping,
    }
    save_json(directory / "manifest.json", manifest)
    for index, chunk in enumerate(chunks, 1):
        request = build_request(chunk, ontology, prompt, max_tokens)
        record = {
            "chunk_id": chunk["chunk_id"],
            "document_id": chunk["document_id"],
            "section_id": chunk["section_id"],
            "source_url": chunk["source_url"],
            "source_sha256": chunk["source_sha256"],
            "filing_date": chunk["filing_date"],
            "original_text_sha256": hashlib.sha256(chunk["text"].encode()).hexdigest(),
            "masked_text_sha256": hashlib.sha256(chunk["masked_text"].encode()).hexdigest(),
            "request": request,
            "status": "preview" if dry_run else "pending",
        }
        path = directory / f"{index:05d}.json"
        save_json(path, record)
        if dry_run:
            continue
        try:
            response = call_openrouter(request, api_key)
            # Keep the provider response before parsing so a rejected result remains auditable.
            record["response"] = response
            save_json(path, record)
            result = decode_response(response)
            record["validated"] = validate_extraction(result, chunk, mapping, ontology)
            record["status"] = "validated"
            manifest["validated_chunks"] += 1
        except Exception as exc:
            record["status"] = "failed"
            record["error"] = (
                str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            )
            manifest["status"] = "failed"
            save_json(path, record)
            save_json(directory / "manifest.json", manifest)
            raise RuntimeError(f"Extraction stopped; inspect {path}") from None
        save_json(path, record)
        save_json(directory / "manifest.json", manifest)
    manifest["status"] = "preview" if dry_run else "complete"
    save_json(directory / "manifest.json", manifest)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--ticker", required=True, choices=TICKERS)
    parser.add_argument("--form", help="Exact parent filing form, such as 10-K or 10-Q")
    parser.add_argument("--role", choices=("primary", "exhibit", "all"), default="primary")
    parser.add_argument("--document-id", help="Exact local document_id, never sent to the model")
    parser.add_argument(
        "--limit", type=int, default=1, help="Maximum chunks, newest filings first (default: 1)"
    )
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--output-root", type=Path, default=ROOT / "data/extractions")
    parser.add_argument(
        "--dry-run", action="store_true", help="Write request previews only; no API calls"
    )
    args = parser.parse_args()
    print(run_extraction(**vars(args)))


if __name__ == "__main__":
    main()
