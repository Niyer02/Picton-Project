"""Read-only Neo4j queries for competitors, shared products, and supply paths."""

from contextlib import contextmanager
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from neo4j import READ_ACCESS
from neo4j.exceptions import DriverError, Neo4jError
from pydantic import Field

from picton_graph.graph.connection import connect
from picton_graph.mcp.evidence import edge_evidence, evidence_records

SCOPE = (
    "Results are extracted historical disclosures within the ten-company basket, not a complete "
    "or verified-current market map. No match does not prove no relationship exists. "
    "Document dates are filing/publication dates, not relationship start or end dates. "
    "Evidence counts are extraction counts, not confidence scores."
)
SearchText = Annotated[str, Field(strict=True, min_length=1, max_length=160)]
EvidenceLimit = Annotated[
    int,
    Field(strict=True, ge=1, le=5, description="Maximum evidence excerpts per edge, newest first."),
]


@contextmanager
def read_transaction():
    try:
        with (
            connect() as driver,
            driver.session(database="neo4j", default_access_mode=READ_ACCESS) as session,
            session.begin_transaction(timeout=10) as tx,
        ):
            yield tx
    except (Neo4jError, DriverError, OSError, StopIteration):
        raise ToolError(
            "Cannot read Neo4j. Check that Docker is running and the project .env has NEO4J_AUTH."
        ) from None


def search_text(value):
    value = value.strip()
    if not value:
        raise ToolError("Enter a non-empty company or product name.")
    return value


def resolve_company(tx, company):
    name = search_text(company).lower()
    matches = [
        dict(row["company"])
        for row in tx.run(
            """
        MATCH (c:Company)
        WHERE toLower(c.ticker) = $name OR toLower(c.name) CONTAINS $name
        RETURN c{.iri, .name, .ticker} AS company ORDER BY c.ticker
    """,
            name=name,
        )
    ]
    exact = [c for c in matches if name in (c["ticker"].lower(), c["name"].lower())]
    matches = exact or matches
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ToolError(
            "Ambiguous company; use a ticker: " + ", ".join(c["ticker"] for c in matches)
        )
    available = [
        row["ticker"]
        for row in tx.run("MATCH (c:Company) RETURN c.ticker AS ticker ORDER BY ticker")
    ]
    raise ToolError("Company not found in this graph. Available tickers: " + ", ".join(available))


def load_evidence(relationship_id, evidence_id):
    with read_transaction() as tx:
        rows = list(
            tx.run(
                """
            MATCH ()-[r]->() WHERE r.relationship_id = $relationship_id
            RETURN properties(r) AS properties LIMIT 2
        """,
                relationship_id=relationship_id,
            )
        )
    if not rows:
        return None
    if len(rows) != 1:
        raise ToolError("Ambiguous relationship identity.")
    matches = [e for e in evidence_records(rows[0]["properties"]) if e["id"] == evidence_id]
    if not matches:
        return None
    if len(matches) != 1:
        raise ToolError("Ambiguous evidence identity.")
    return matches[0]


def get_company_basket() -> dict[str, Any]:
    """List the company names and tickers in the graph's ten-company basket. No arguments needed."""
    with read_transaction() as tx:
        companies = [
            dict(row)
            for row in tx.run(
                "MATCH (c:Company) RETURN c.name AS name, c.ticker AS ticker ORDER BY ticker"
            )
        ]
    return {"companies": companies}


def get_competitors(company: SearchText, evidence_limit: EvidenceLimit = 2) -> dict[str, Any]:
    """Find explicitly disclosed competitors by company ticker or name, e.g. NVDA or NVIDIA.

    Reads competes_with in both directions. Cite evidence citation_url links to exact excerpts.
    Returns competitors with dated source evidence;
    shared products alone do not imply a disclosed competitive relationship.
    """
    with read_transaction() as tx:
        focus = resolve_company(tx, company)
        rows = list(
            tx.run(
                """
            MATCH (:Company {iri: $iri})-[r:competes_with]-(c:Company)
            RETURN c{.name, .ticker} AS company, properties(r) AS relationship
            ORDER BY c.ticker LIMIT 21
        """,
                iri=focus["iri"],
            )
        )
    return {
        "company": {k: focus[k] for k in ("name", "ticker")},
        "competitors": [
            {"company": row["company"], **edge_evidence(row["relationship"], evidence_limit)}
            for row in rows[:20]
        ],
        "truncated": len(rows) > 20,
        "scope": SCOPE,
    }


def find_product_suppliers(
    product: SearchText, evidence_limit: EvidenceLimit = 2
) -> dict[str, Any]:
    """Find basket companies offering the same named Product, e.g. GPU or CPUs.

    Matches offers edges by exact product name, ignoring case and outer whitespace;
    cite evidence citation_url links to exact supporting excerpts.
    GPUs also matches canonical GPU. If no exact match exists, returns name suggestions
    to retry, not inferred substitutes. Offering a product does not imply supplying a
    particular customer, interchangeable products, or a competes_with relationship.
    """
    query = search_text(product)
    name = "gpu" if query.lower() == "gpus" else query.lower()
    with read_transaction() as tx:
        rows = list(
            tx.run(
                """
            MATCH (c:Company)-[r:offers]->(p:Product)
            WHERE toLower(trim(p.name)) = $name
            RETURN c{.name, .ticker} AS company, p{.iri, .name} AS product,
                   properties(r) AS relationship ORDER BY p.name, c.ticker LIMIT 21
        """,
                name=name,
            )
        )
        suggestions = []
        if not rows:
            suggestions = [
                row["name"]
                for row in tx.run(
                    """
                MATCH (:Company)-[:offers]->(p:Product)
                WHERE toLower(p.name) CONTAINS $name
                RETURN DISTINCT p.name AS name ORDER BY name LIMIT 10
            """,
                    name=name,
                )
            ]
    return {
        "product_query": query,
        "match": "exact" if rows else "none",
        "suppliers": [
            {
                "company": row["company"],
                "product": row["product"],
                **edge_evidence(row["relationship"], evidence_limit),
            }
            for row in rows[:20]
        ],
        "suggestions": suggestions,
        "truncated": len(rows) > 20,
        "scope": SCOPE,
    }


def trace_supply_chain(
    company: SearchText,
    direction: Literal["downstream", "upstream"] = "downstream",
    max_hops: Annotated[int, Field(strict=True, ge=1, le=2)] = 2,
    limit: Annotated[
        int, Field(strict=True, ge=1, le=50, description="Maximum paths, shortest first.")
    ] = 20,
    evidence_limit: EvidenceLimit = 2,
) -> dict[str, Any]:
    """Trace direct and two-step supply connections with evidence for every edge.

    Use downstream for customers potentially exposed to a supplier (e.g. ASML),
    or upstream for a company's suppliers and their suppliers (e.g. NVIDIA).
    Paths start at the queried company; each returned relationship separately retains
    citation_url links to its exact supporting evidence excerpts and
    its actual supplier-to-customer direction. Paths reference the deduplicated
    relationships array by relationship_id. These historical connections do not
    establish simultaneous activity, disruption transmission, or a stock-price effect.
    """
    patterns = {
        "downstream": "(root)-[:supplies_to*1..2]->(other:Company)",
        "upstream": "(root)<-[:supplies_to*1..2]-(other:Company)",
    }
    with read_transaction() as tx:
        focus = resolve_company(tx, company)
        rows = list(
            tx.run(
                f"""
            MATCH (root:Company {{iri: $iri}})
            MATCH p = {patterns[direction]}
            WHERE length(p) <= $max_hops
              AND all(n IN nodes(p) WHERE n:Company AND single(m IN nodes(p) WHERE m = n))
            RETURN [n IN nodes(p) | n{{.name, .ticker}}] AS companies,
                   [r IN relationships(p) | {{properties: properties(r),
                       supplier: startNode(r).ticker, customer: endNode(r).ticker}}] AS links
            ORDER BY length(p), [n IN nodes(p) | n.ticker] LIMIT $limit
        """,
                iri=focus["iri"],
                max_hops=max_hops,
                limit=limit + 1,
            )
        )
    paths, relationships = [], {}
    for row in rows[:limit]:
        identifiers = []
        for link in row["links"]:
            identifier = link["properties"]["relationship_id"]
            identifiers.append(identifier)
            if identifier not in relationships:
                relationships[identifier] = {
                    "type": "supplies_to",
                    "supplier": link["supplier"],
                    "customer": link["customer"],
                    **edge_evidence(link["properties"], evidence_limit),
                }
        paths.append(
            {
                "companies": row["companies"],
                "hops": len(identifiers),
                "connection": "direct" if len(identifiers) == 1 else "indirect",
                "relationship_ids": identifiers,
            }
        )
    return {
        "company": {k: focus[k] for k in ("name", "ticker")},
        "direction": direction,
        "max_hops": max_hops,
        "paths": paths,
        "relationships": list(relationships.values()),
        "truncated": len(rows) > limit,
        "scope": SCOPE,
        "interpretation": "Potential exposure routes, not proof of disruption transmission or expected stock returns.",
    }
