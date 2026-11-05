# Ontology and graph validation

[Project overview](../README.md)

All shell commands below run from the project root, after the editable install
described in the [setup guide](setup.md). Paths in prose are relative to that root.

## Apply the domain schema

```sh
.venv/bin/python -m picton_graph.graph.ontology
```

The graph has seven domain labels: `Company`, `Product`, `Service`,
`SourceDocument`, `Topic`, `Sector`, and `Event`.
A company is a `Company` node; an event is an `Event` node.
Object properties become direct relationship types, for example:

```text
(:Company)-[:supplies_to]->(:Company)
(:Company)-[:competes_with]->(:Company)
(:Company)-[:offers]->(:Product)
(:Company)-[:provides]->(:Service)
(:Event)-[:concerns]->(:Company)
(:SourceDocument)-[:reports]->(:Event)
```

The schema setup creates no data nodes or relationships. The 10 companies in
`docs/companies.md` were loaded separately into the local database as `Company`
nodes with `iri`, `name`, and `ticker` properties. A fresh database requires that
basket before masking and graph import; there is no automated company seeder.
View them in Browser with `MATCH (c:Company) RETURN c;`. Labels with no instances
and relationship types with no edges may not appear in Browser's data view.

`ontology/picton.owl` is the source of truth. The Python translator produces:

- `ontology/schema.cypher`: seven native IRI uniqueness constraints, plus registration
  of the 17 relationship type names and property keys.
- `ontology/schema.json`: the seven classes, descriptions, domain/range alternatives,
  eight inverse pairs, symmetric competition, 20 disjoint class pairs, data types, and four evidence
  annotation definitions. This is a schema file, not graph data.

`picton_graph/graph/validation.py` contains the application checks for rules Neo4j
Community cannot enforce; it is maintained source code, not a generated file.

Re-running the setup is safe and does not insert, delete, or duplicate data.
New OWL constructs that the translator cannot handle cause an error before
database changes.

## What enforces each rule?

| Rule | Enforcement |
|---|---|
| Unique `iri` within each domain label | Native Neo4j constraint |
| Identifier present and unique across all nodes | Python validation |
| Relationship source and target labels, including OR alternatives | Python validation |
| Disjoint class combinations; Topic/Sector overlap allowed | Python validation |
| No self-supply or self-competition, including the inverse `buys_from` | Python validation |
| Property domains and data types | Python validation |
| Evidence references point to the expected document/product/service | Python validation |
| Inverse meanings | Recorded in the schema; query the opposite direction as needed |
| Symmetric competition | Declared in OWL; query `competes_with` without a direction in Neo4j |

The IRI identifier is a storage convention, not a new OWL key axiom. The native
uniqueness constraints alone do not require an IRI to be present. Evidence
reference types are application policies taken from the annotation descriptions.

**Python checks are not database triggers.** Future ingestion must call
`validate_graph(tx, schema)` inside the same transaction as its writes, before
committing; a validation error then rolls back that transaction. Direct writes
in Browser can bypass these Python rules. Native uniqueness is still enforced.

The validator expects explicit domain labels, rejects unknown labels/types/
properties, accepts optional and multivalued data properties, and requires
native Python/Neo4j datetime values for `occurred_at` and `published_at`.
URLs and evidence references are stored as strings; annotations are properties
on the specific relationship. The checks do not establish that an evidence
quote is true or actually supports a relationship.

These are ingestion checks, not OWL reasoning. In particular, OWL domain/range
axioms can infer types; this validator requires those types to be supplied.
Inverse edges are not automatically duplicated. Keep using Protégé/HermiT for
OWL reasoning. The validator scans the whole graph, which is appropriate for
this small take-home; revisit that approach for large ingestion jobs.

`competes_with` connects two distinct companies competing in at least one
product or service market. It is symmetric and irreflexive; competition does
not propagate through chains. A pair can both compete and have a supply
relationship. Imported edges retain sources and competitive context in `evidence`.
Shared sector/topic membership is not enough evidence.

Store each supported competition assertion once, in either direction. Neo4j
stores directed relationships and does not enforce OWL symmetry. Read competitors
from either endpoint using an undirected pattern, for example:

```cypher
MATCH (:Company {ticker: 'NVDA'})-[r:competes_with]-(competitor:Company)
RETURN DISTINCT competitor.ticker AS ticker, competitor.name AS name,
       r.evidence AS evidence;
```

Inspect the native constraints in Browser:

```cypher
SHOW CONSTRAINTS
YIELD name, labelsOrTypes, properties
RETURN name, labelsOrTypes, properties;
```

## Visualize the full ontology

Paste the contents of `ontology/visualize.cypher` into Neo4j Browser and run it.
It shows seven virtual class nodes and 31 directed schema edges: all 17 object
property names, including inverses, expanded across the allowed union alternatives.
Click nodes for descriptions, data properties, and disjoint classes; click edges
for descriptions, domains/ranges, inverse names, symmetry, and irreflexivity where specified.

The query uses the APOC Core library bundled with our pinned Neo4j Docker image,
enabled by Compose. It creates no stored records. Re-running `picton_graph/graph/ontology.py`
regenerates the visualization query from the OWL source.

The Company-to-Company schema loops mean a company can supply or compete with another company;
they do not permit self-supply or self-competition by an individual company. Multiple union branches
are alternatives, not requirements to belong to all those classes.
`CALL db.schema.visualization()` describes the stored data/index schema and will
not show these declared relationship patterns while the data graph is empty.

Run parser, validation, and live Neo4j checks. Test data is rolled back:

```sh
NEO4J_INTEGRATION=1 .venv/bin/python -m unittest -v tests.test_import_ontology
```
