# SEC ingestion and masking

[Project overview](../README.md)

All shell commands below run from the project root, after the editable install
described in the [setup guide](setup.md). Paths in prose are relative to that root.

## Fetch SEC source documents

`picton_graph/ingestion/sec.py` uses the public SEC APIs, Python's standard library
for downloads, and BeautifulSoup for filing index tables.
It reads the ten-company basket from `docs/companies.md`, resolves tickers to CIKs,
and follows the submissions API's older-history files as needed. The default
window is the last five calendar years through today in America/Toronto,
inclusive, filtered by **filing date**, not fiscal period. Amendments remain
separate filings with their own accession numbers and dates.

Set `SEC_USER_AGENT` in the shell or the ignored `.env` file to a descriptive
name and real contact email, as required by the
[SEC's automated-access guidance](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data):

```dotenv
SEC_USER_AGENT=Your Research Project your-contact@your-domain.com
```

The contact is configured locally. Run the agreed five-year window:

```sh
.venv/bin/python -m picton_graph.ingestion.sec --start 2021-11-04 --end 2026-11-04
```

Omit the date arguments for a rolling five-year window. For a smaller run:

```sh
.venv/bin/python -m picton_graph.ingestion.sec --tickers NVDA --forms 10-K 10-Q --start 2024-01-01 --end 2024-12-31
```

The default forms are `10-K`, `10-Q`, `20-F`, `6-K`, and `8-K`, including their
`/A` amendments. Each filing includes its primary document plus attached
`EX-10` contracts and `EX-99` announcements/reports. This captures substantive
attachments behind 6-K and 8-K cover pages. Other exhibit types, linked documents
incorporated by reference, images, and XBRL data files are outside this collector's
scope. The optional `--output` argument changes the default `data/sec` directory.

The output is local and excluded from Git:

```text
data/sec/
  company_tickers.json
  submissions/                    # SEC company/history JSON snapshots
  manifest.json                   # Requested scope, completion, and document metadata
  NVDA/<accession>/
    filing-index.html
    metadata.json
    raw/<original-filename>       # Original downloaded bytes
```

Metadata preserves the company, CIK, accession, form, filing date, report date,
SEC acceptance timestamp, source URLs, retrieval timestamps, and SHA-256 hashes.
Documents are saved byte-for-byte, without text extraction, preprocessing,
chunking, or masking. Non-HTML attachments are also retained in their original
format; there is no PDF parsing or OCR. This step only collects source documents.

Requests are sequential, limited to four per second per process, with bounded
retries for throttling and transient server/network failures. HTTP 403 and SEC
access-block pages stop the run. TLS certificates are verified; on macOS, the
system CA bundle is used if Python has no default CA file. Run one collector
process at a time.

Reruns refresh discovery metadata but reuse original documents whose saved
hashes match; missing or changed local files are downloaded again. Successful
downloads survive an interrupted run. The collector exits nonzero on failure
and leaves `manifest.json` marked incomplete. This manifest describes the latest
requested run; older document directories are retained. Cached SEC documents
are not automatically checked for subsequent SEC corrections; remove a filing's
local directory to download that filing again when a correction is known.

The agreed news window is two years via Massive's standard news API. News
collection and LLM/Neo4j ingestion are separate subsequent steps.

Run the offline collection and ontology tests:

```sh
.venv/bin/python -m unittest -v tests.test_fetch_sec_filings tests.test_import_ontology
```

The database integration tests are skipped unless `NEO4J_INTEGRATION=1` is set.

The November 4, 2026 collection completed for all ten companies: **940 filings
and 1,666 primary/exhibit documents** within the requested five-year window.
Every selected accession was present and all saved original-file hashes matched.
All 1,666 documents are HTML. The originals occupy approximately 782 MiB.

## Process HTML into sections and clean text

Install the pinned dependencies, then process all cached filings and exhibits:

```sh
.venv/bin/python -m pip install -e '.[research]'
.venv/bin/python -m picton_graph.ingestion.html
```

Use `--tickers NVDA` for just NVIDIA. This is an offline Beautiful Soup 4 + lxml
pass; it does not call an LLM, mask names, or write to Neo4j.

Outputs are separate from the originals:

```text
data/processed/
  manifest.json
  NVDA/<accession>/<original-filename>.json
  NVDA/<accession>/<original-filename>.txt
```

Each JSON document keeps its source URL, accession, filing/report dates, original
SHA-256 hash, and document role. Its `sections` contain a unique `section_id`,
`title`, `kind`, `part`, `items`, complete clean `text`, and ordered `chunks`.
Chunks have unique IDs, clean text, character counts, table indicators, and
an `over_target` flag. The text file contains the complete cleaned document.
Image-only documents have `status: no_extractable_text`, no sections/chunks,
and an empty text file; their source metadata and warning remain in the manifest.

The parser removes HTML markup, comments, scripts, styles, SEC SGML envelopes,
and explicitly hidden content, including hidden inline XBRL. Visible inline
wrappers preserve the original text adjacency, so `CORP` + `ORATION` stays
`CORPORATION`. Paragraph boundaries remain separated by blank lines. Tables use
tab-separated cells and newline-separated rows in source order; merged-cell
positions are expanded without guessing headers or promoting data rows.
Nested layout tables are walked in source order.

Primary 10-K, 10-Q, 20-F, and 8-K documents use form-specific Item titles and
Part boundaries. Linked table-of-contents entries do not create SEC sections.
Some companies use overlapping cross-reference indexes instead of a sequential
SEC outline. Those documents retain their own headings, preferably matched to
their contents tables, and receive a `no_reliable_sec_item_outline` warning.
6-K reports and exhibits use their own document headings. The parser never
cuts off content at signatures or follows incorporation-by-reference links.

Chunks stay within sections and group complete paragraphs and tables around a
soft 6,000-character target. Nearby short captions and units stay with a table
when a new chunk starts. Long individual paragraphs/tables remain intact and
are flagged rather than truncated. These are character counts, not token counts.

Every source file must match its saved hash before processing. Each document
also checks that all visible non-whitespace characters survive in order after
intentional invisible-character cleanup, and that its chunks reconstruct its
sections exactly. Output files are replaced atomically; the manifest remains
incomplete if a document fails. A subset rerun's manifest describes only that
run; files from other runs are retained, so use the manifest as the inventory.

Limitations: this is DOM/source-order extraction, not browser rendering.
Stylesheet visibility, complex positioned layouts, and image content are
flagged; images are not OCR'd. Text-retention checks do not prove semantic
section boundaries, financial-table interpretation, or visual reading order.
Older designed annual reports may have fewer identifiable headings, with all
remaining text still retained and chunked.

Run the offline checks:

```sh
.venv/bin/python -m unittest -v tests.test_process_sec_filings tests.test_fetch_sec_filings tests.test_import_ontology
```

## Store processed text in DuckDB

```sh
.venv/bin/python -m pip install -e '.[research]'
.venv/bin/python -m picton_graph.ingestion.storage
```

This imports the completed `data/processed/manifest.json` into
`data/filings.duckdb`, a persistent local file using DuckDB 2.0.0. No database
server is needed. The HTML and JSON/text exports are unchanged.

| Table | Contents |
|---|---|
| `documents` | Filing/exhibit identity, company, form, date, source URL, hashes, status, and metadata |
| `sections` | Document link, section order, title, kind, Part/Items, and complete section text |
| `chunks` | Document and section links, chunk order, clean text, character count, and table/size flags |

`documents.metadata` preserves every original top-level document field except
the separately stored `sections`. It includes all source metadata and warnings.
The two image-only exhibits have document rows with `no_extractable_text` status
and no section/chunk rows. Use `documents.role` to select `primary` or `exhibit`.

The importer verifies export hashes, provenance, counts, IDs, text reconstruction,
and chunk flags. The entire import is one transaction; any failure rolls it back.
Reruns replace only documents listed in the current manifest and their children;
other documents and tables remain untouched. Primary keys and order constraints
prevent duplicates. Parent links are validated by the importer and maintained
within its transaction; there are no database foreign-key constraints.

Example read-only query:

```python
import duckdb

with duckdb.connect("data/filings.duckdb", read_only=True) as db:
    rows = db.execute("""
        SELECT d.ticker, d.form, d.filing_date, s.title, c.text, d.source_url
        FROM chunks c
        JOIN sections s ON c.section_id = s.section_id
        JOIN documents d ON c.document_id = d.document_id
        WHERE d.ticker = 'NVDA' AND d.role = 'primary'
          AND d.form = '10-K' AND s.kind = 'sec_item'
        ORDER BY d.filing_date DESC, d.document_id, s.section_index, c.chunk_index
        LIMIT 5
    """).fetchall()
```

Run the storage checks:

```sh
.venv/bin/python -m unittest -v tests.test_store_processed_duckdb
```

## Mask the ten graph companies

With local Neo4j running, populate the masked copies in DuckDB:

```sh
.venv/bin/python -m picton_graph.ingestion.masking
```

The script reads the ten `Company` nodes and checks the agreed basket. Labels
are fixed across all companies, filings, exhibits, and reruns:

| Company | Label |
|---|---|
| NVIDIA / NVDA | Company A |
| Applied Materials / AMAT | Company B |
| Advanced Micro Devices / AMD | Company C |
| ASML | Company D |
| Broadcom / AVGO | Company E |
| Intel / INTC | Company F |
| KLA / KLAC | Company G |
| Lam Research / LRCX | Company H |
| Micron Technology / MU | Company I |
| TSMC / TSM | Company J |

Only the main names, listed aliases, and ticker symbols are replaced. Articles,
legal suffixes, possessives, punctuation, and other qualifiers remain unchanged:
`The NVIDIA Corporation` becomes `The Company A Corporation`, and
`NVIDIA Holdings` becomes `Company A Holdings`. Name matching ignores case and
allows whitespace/dash variants; the bare ticker `MU` is case-sensitive.
Lowercase `micron` used for dimensions such as `0.25 micron`, `sub-micron`, or
the unit's glossary definition is retained; `micron.com` is masked.

`chunks.masked_text` and `chunks.mask_spans` store the masked chunk and its
replacements. `sections.masked_title` and `sections.title_mask_spans` provide
masked heading context. Each span records the ticker, replacement, and
zero-based Python character ranges `[start, end)` in original and masked text.
`company_masks` stores the fixed mapping, graph identifiers, aliases, and regexes.
Original text, section titles, identifiers, source metadata, HTML, and exports
are retained unchanged. All chunks receive masked text, even when no replacement
is needed. The entire masking pass is transactional.

Rerunning the importer clears the masks for replaced documents, so rerun masking
after importing new or regenerated chunks. Other documents' masks are retained.
Existing label conflicts or literal reserved labels in original text stop the
masking run to avoid ambiguous identities. Legal suffixes such as `Company B.V.`
are not treated as reserved labels.

For later LLM prompts, use the masked text and masked heading with an opaque
reference kept in Python. Do not include the mapping, real ticker, source URL,
original text, or company-bearing document/chunk IDs in that prompt. Other
companies, product brands, facts, and pronouns remain visible: this is masking
the agreed names, not complete anonymization or a guarantee against identification.
Technical strings preserve their unmatched characters, so masked URLs and
filenames may no longer be valid links.

Inspect the paired text locally:

```sql
SELECT c.chunk_id, s.masked_title, c.text, c.masked_text, c.mask_spans
FROM chunks c
JOIN sections s ON c.section_id = s.section_id
WHERE c.masked_text IS DISTINCT FROM c.text
ORDER BY c.chunk_id
LIMIT 10;
```

```sh
.venv/bin/python -m unittest -v tests.test_mask_company_names tests.test_store_processed_duckdb
```
