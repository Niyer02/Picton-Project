"""Import the completed Beautiful Soup exports into a local DuckDB database."""

import argparse
import hashlib
import json
from pathlib import Path

import duckdb

from picton_graph.config import ROOT

DATABASE = ROOT / "data/filings.duckdb"
SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id VARCHAR PRIMARY KEY,
    ticker VARCHAR NOT NULL,
    accession VARCHAR NOT NULL,
    form VARCHAR NOT NULL,
    document_type VARCHAR NOT NULL,
    role VARCHAR NOT NULL CHECK (role IN ('primary', 'exhibit')),
    filing_date DATE NOT NULL,
    source_url VARCHAR NOT NULL,
    source_sha256 VARCHAR NOT NULL,
    output_sha256 VARCHAR NOT NULL,
    export_path VARCHAR NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('processed', 'no_extractable_text')),
    metadata JSON NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
    section_id VARCHAR PRIMARY KEY,
    document_id VARCHAR NOT NULL,
    section_index INTEGER NOT NULL CHECK (section_index >= 0),
    title VARCHAR NOT NULL,
    kind VARCHAR NOT NULL,
    part VARCHAR,
    items JSON NOT NULL,
    text VARCHAR NOT NULL,
    UNIQUE (document_id, section_index)
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id VARCHAR PRIMARY KEY,
    document_id VARCHAR NOT NULL,
    section_id VARCHAR NOT NULL,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    text VARCHAR NOT NULL CHECK (length(text) > 0),
    character_count INTEGER NOT NULL CHECK (character_count = length(text)),
    contains_table BOOLEAN NOT NULL,
    over_target BOOLEAN NOT NULL,
    UNIQUE (section_id, chunk_index)
);
"""


def load_document(root, entry, max_chars):
    path = (root / entry["output_path"]).resolve()
    if not path.is_relative_to(root) or path.suffix != ".json":
        raise ValueError("Export path must be a JSON file inside the processed directory.")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != entry["output_sha256"]:
        raise ValueError(f"Export hash mismatch: {path}")
    document = json.loads(payload)
    source = document["source"]
    document_id = f"{source['ticker']}/{source['accession']}/{source['filename']}"
    if (
        document["document_id"] != document_id
        or entry["document_id"] != document_id
        or path != (root / (document_id + ".json")).resolve()
        or source["sha256"] != entry["source_sha256"]
        or document["status"] != entry["status"]
        or document["warnings"] != entry["warnings"]
    ):
        raise ValueError(f"Document provenance does not match the manifest: {path}")
    sections, chunks = [], []
    for si, section in enumerate(document["sections"]):
        section_id = f"{document_id}#section-{si:04d}"
        if section["section_id"] != section_id:
            raise ValueError(f"Section ID/order does not match its document: {path}")
        if "\n\n".join(c["text"] for c in section["chunks"]) != section["text"]:
            raise ValueError(f"Chunks do not reconstruct their section: {path}")
        sections.append(
            (
                section_id,
                document_id,
                si,
                section["title"],
                section["kind"],
                section["part"],
                json.dumps(section["items"]),
                section["text"],
            )
        )
        for ci, chunk in enumerate(section["chunks"]):
            if (
                chunk["chunk_id"] != f"{section_id}-chunk-{ci:04d}"
                or not chunk["text"]
                or chunk["character_count"] != len(chunk["text"])
                or type(chunk["contains_table"]) is not bool
                or type(chunk["over_target"]) is not bool
                or chunk["over_target"] != (len(chunk["text"]) > max_chars)
            ):
                raise ValueError(f"Invalid chunk content, flags, or ID/order: {path}")
            chunks.append(
                (
                    chunk["chunk_id"],
                    document_id,
                    section_id,
                    ci,
                    chunk["text"],
                    chunk["character_count"],
                    chunk["contains_table"],
                    chunk["over_target"],
                )
            )
    if (
        len(sections) != entry["sections"]
        or len(chunks) != entry["chunks"]
        or sum(s[4] == "sec_item" for s in sections) != entry["sec_items"]
        or sum(c[-1] for c in chunks) != entry["over_target"]
        or (document["status"] == "no_extractable_text") != (not sections and not chunks)
    ):
        raise ValueError(f"Document counts/status do not match the manifest: {path}")
    metadata = {key: value for key, value in document.items() if key != "sections"}
    row = (
        document_id,
        source["ticker"],
        source["accession"],
        source["form"],
        source["type"],
        source["role"],
        source["filing_date"],
        source["url"],
        source["sha256"],
        entry["output_sha256"],
        str(path),
        document["status"],
        json.dumps(metadata, ensure_ascii=False),
    )
    return row, sections, chunks


def store_export(input_root=ROOT / "data/processed", database=DATABASE):
    root, database = Path(input_root).resolve(), Path(database).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    entries = manifest["documents"]
    if manifest.get("complete") is not True or not entries:
        raise ValueError("Import requires a complete, non-empty processing manifest.")
    if len({e["document_id"] for e in entries}) != len(entries):
        raise ValueError("The processing manifest contains duplicate documents.")
    if type(manifest["max_chars"]) is not int or manifest["max_chars"] <= 0:
        raise ValueError("The processing manifest has an invalid chunk target.")
    database.parent.mkdir(parents=True, exist_ok=True)
    counts = {"documents": 0, "sections": 0, "chunks": 0}
    with duckdb.connect(str(database)) as db:
        # Closing the connection rolls back if anything fails before COMMIT.
        db.execute("BEGIN TRANSACTION")
        db.execute(SCHEMA)
        for entry in entries:
            document, sections, chunks = load_document(root, entry, manifest["max_chars"])
            # Replace only this document's rows; retain other documents and tables.
            # Parent links are checked above and written together in this transaction.
            for table in ("chunks", "sections", "documents"):
                db.execute(f"DELETE FROM {table} WHERE document_id = ?", [document[0]])
            db.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", document
            )
            if sections:
                db.executemany(
                    """INSERT INTO sections
                    (section_id, document_id, section_index, title, kind, part, items, text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    sections,
                )
            if chunks:
                db.executemany(
                    """INSERT INTO chunks
                    (chunk_id, document_id, section_id, chunk_index, text, character_count, contains_table, over_target)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    chunks,
                )
            counts["documents"] += 1
            counts["sections"] += len(sections)
            counts["chunks"] += len(chunks)
        db.execute("COMMIT")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=ROOT / "data/processed")
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()
    print(
        json.dumps(
            {"database": str(args.database), **store_export(args.input_root, args.database)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
