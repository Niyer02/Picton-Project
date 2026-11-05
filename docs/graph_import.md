# Graph import and provenance

[Project overview](../README.md)

All shell commands below run from the project root, after the editable install
described in the [setup guide](setup.md). Paths in prose are relative to that root.

## Load the extracted graph into Neo4j

The existing graph is already loaded. Its local corpus export and external
node-provenance archives were removed during cleanup; edge evidence remains in
Neo4j. To import a new run, generate a fresh export, then use its directory with
the local Neo4j container running:

```sh
.venv/bin/python -m picton_graph.graph.importer \
  'data/corpus_extractions/<run_id>/deanonymized'
```

The importer checks the complete export's checksum, schema, counts and graph
rules before opening a database connection. It preserves the existing Company
nodes. Repeated Product, Service, Topic and Sector mentions are consolidated by
class and normalized name (Unicode normalization, case and whitespace). Product
`GPU` and `GPUs` share one identity; arbitrary synonyms and fuzzy matches are not
merged. Company, SourceDocument and Event identities stay unchanged, preserving
distinct event occurrences. Relationships are grouped by canonical source node,
relationship type and target node, with one deterministic `relationship_id` per
connection. `competes_with` uses an unordered company pair because competition
is symmetric; all other relationship directions are preserved. Query competitors
with an undirected pattern, such as `(company)-[:competes_with]-(competitor)`.
Rerunning the same export resumes committed work without creating duplicates.
Writes use batches of at most 500 records; a failed batch rolls back, while
earlier successful batches remain committed. The local import lock prevents two
instances of this script from writing concurrently.

The seven ontology classes and their domain properties stay the same.
The immutable export retains source/chunk identifiers, hashes, original quotes,
offsets and literal `time_text`; imported edges use the compact evidence format below.
All nodes hold a compact `provenance_ref`: their complete histories are stored
in `node_provenance_v2.json` beside the export, keyed by the SHA-256 digest of
each exact JSON string. Keep
this archive with the project when moving or backing up the database. The
import report records its path and checksum. For example, Python can resolve a
node's reference with `json.loads(archive[provenance_ref])`, where `archive` is
the loaded `node_provenance_v2.json` mapping. The current graph's references cannot
be resolved locally because its archives were removed. MCP graph queries and
edge citations do not depend on these archives.

The archive is verified before database writes. Existing inline shared-node
histories are replaced with references only when they exactly match the saved
archive. This avoids returning megabytes of repeated company history for every
path: the initial 147-row competition query repeated roughly 577 MB and exhausted
Neo4j's network-buffer memory. Indexes do not solve that result-size problem.

Relationships store `evidence` as a JSON-encoded list (Neo4j cannot store a list
of objects directly as a property). Each record has:

```json
{
  "id": "<original stable assertion SHA-256>",
  "evidence_text": "<original unmasked supporting excerpt>",
  "document": "<source document URL>",
  "document_type": "10-K",
  "document_date": "2025-07-01"
}
```

`document` matches a `SourceDocument.url`. `document_type` describes the actual
document, including exhibit types such as `EX-99.1`, rather than its parent form.
`document_date` is the SEC filing date in `YYYY-MM-DD` format, or `null` when
unavailable; it is not a fiscal period end or inferred event date. A future news
importer should use the publication date. Python callers use
`json.loads(relationship["evidence"])` to retrieve the list.

When present, each record also retains literal `time_text` and canonical
`supplied_product` / `supplied_service` IRI lists, preserving which quote supports
each period and supplied item. Masking offsets, masked names and extraction
bookkeeping are excluded from the edge metadata. Original evidence IDs and
records are retained, including repeated quotes extracted from different chunks;
`evidence_count` continues to count these original assertions. The outer typed
`supplied_product` / `supplied_service` lists and `relationship_id` are unchanged.

The importer replaces only the edge-level `evidence_text`, `provenance_json` and
`supported_by` properties with `evidence`. Existing grouped edges are updated in
place after exact comparison with the verified export; endpoints, storage IDs
and all remaining properties are preserved. Each batch checks the original full
properties before writing, rolls back on a conflict, and safely resumes alongside
already converted edges. The report's `relationships_evidence_updated` records
the number of converted edges; a rerun reports zero.

The Python validator checks evidence fields, dates, source-document references,
counts, typed supplied-item unions and domain rules. The importer also checks
every record against the immutable export and verifies archive contents.
Literal time qualifiers are not converted into invented event timestamps.
When migrating the earlier assertion-per-edge graph, each transaction verifies
the old records, creates the grouped edges, then deletes only those verified
old relationships. Any conflict rolls back that batch. Obsolete duplicate entity
nodes are removed only after their relationships have been migrated and verified;
unknown data attached to them blocks migration rather than being deleted.

`neo4j_import_report.json` in the export directory records creation counts,
readback counts and any failure. Completion requires a full graph validation
and property-by-property readback. Existing unrelated properties are preserved;
conflicting properties are refused before writing. This MVP supports this one
frozen export and its reruns. Combining different exports requires an explicit
policy for merging provenance. Entity names with different wording can still
refer to the same real-world concept; resolving those requires a separate alias
policy. Event nodes retain their original occurrence identities.

The importer uses the completed export directly. Historical extraction runtime
snapshots remain unchanged; their resume checks intentionally reject a changed
current runtime, including later validator changes. Source HTML, DuckDB, saved
LLM responses and the exported JSONL are not modified by the import.

Start with the company connections in Neo4j Browser:

```cypher
MATCH p = (:Company)-[:supplies_to|competes_with]->(:Company)
RETURN p
LIMIT 300;
```

```sh
.venv/bin/python -m unittest -v tests.test_import_extractions tests.test_import_ontology
```
