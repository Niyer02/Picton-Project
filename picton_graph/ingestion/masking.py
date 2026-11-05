"""Mask the ten graph companies in DuckDB without changing original text."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import duckdb

from picton_graph.graph.connection import connect
from picton_graph.ingestion.storage import DATABASE

# Preserve the labels from the original NVIDIA masking pass, across every company.
TICKERS = ("NVDA", "AMAT", "AMD", "ASML", "AVGO", "INTC", "KLAC", "LRCX", "MU", "TSM")
ALIASES = {
    "NVDA": ("NVIDIA",),
    "AMAT": ("Applied Materials",),
    "AMD": ("Advanced Micro Devices", "AMD"),
    "ASML": ("ASML",),
    "AVGO": ("Broadcom",),
    "INTC": ("Intel",),
    "KLAC": ("KLA-Tencor", "KLA Tencor", "KLA"),
    "LRCX": ("Lam Research",),
    "MU": ("Micron Technology", "Micron"),
    "TSM": ("Taiwan Semiconductor Manufacturing", "TSMC"),
}
# A legal suffix such as "LYB Finance Company B.V." is not our "Company B" label.
RESERVED = re.compile(r"\bCompany [A-J]\b(?!\.[A-Za-z]\.)")


def graph_companies():
    with connect() as driver:
        records, _, _ = driver.execute_query(
            "MATCH (c:Company) RETURN c.ticker AS ticker, c.iri AS iri, c.name AS name"
        )
        return [dict(record) for record in records]


def company_rules(companies):
    if len(companies) != len(TICKERS) or {c["ticker"] for c in companies} != set(TICKERS):
        raise ValueError("Masking requires exactly the ten agreed graph companies.")
    if any(not c.get("iri") or not c.get("name") for c in companies) or len(
        {c["iri"] for c in companies}
    ) != len(TICKERS):
        raise ValueError("Graph companies must have names and unique identifiers.")
    by_ticker = {c["ticker"]: c for c in companies}
    rules, patterns = [], []
    for index, ticker in enumerate(TICKERS):
        aliases = sorted(set(ALIASES[ticker]), key=len, reverse=True)
        names = "|".join(
            re.escape(alias).replace(r"\ ", r"\s+").replace(r"\-", r"[-‐‑‒–—]") for alias in aliases
        )
        pattern = rf"(?<!\w)(?i:{names})(?!\w)"
        if ticker not in aliases:
            # Ordinary lowercase "mu" is not the Micron stock symbol.
            symbol = re.escape(ticker) if ticker == "MU" else f"(?i:{re.escape(ticker)})"
            pattern += rf"|(?<!\w){symbol}(?!\w)"
        if ticker == "NVDA":
            # Mask only the name inside the technical strings seen in these filings.
            pattern += r"|(?i:nvidia(?=news\b|[\w.-]*\.html?\b))"
            pattern += r"|(?<!\w)(?i:nvda(?=\d[\w.-]*\.html?\b))"
        rules.append(
            {
                "ticker": ticker,
                "company_iri": by_ticker[ticker]["iri"],
                "company_name": by_ticker[ticker]["name"],
                "masked_name": f"Company {chr(65 + index)}",
                "aliases": sorted(set([*aliases, ticker])),
                "pattern": pattern,
            }
        )
        patterns.append(f"(?P<c{index}>{pattern})")
    return rules, re.compile("|".join(patterns))


def mask_text(text, matcher, rules):
    if RESERVED.search(text):
        raise ValueError(
            "Original text contains a reserved Company A–J label; resolve the collision first."
        )
    parts, spans = [], []
    cursor = masked_length = 0
    for match in matcher.finditer(text):
        rule = rules[int(match.lastgroup[1:])]
        # In this corpus lowercase micron also denotes chip dimensions, not a company.
        if (
            rule["ticker"] == "MU"
            and match.group() == "micron"
            and text[match.end() : match.end() + 4].lower() != ".com"
        ):
            before, after = (
                text[max(0, match.start() - 50) : match.start()],
                text[match.end() : match.end() + 100],
            )
            if re.search(r"(?:\d|half|sub)[\t ‐‑‒–—−-]*$", before, re.I) or re.match(
                r"\s+A metric unit", after, re.I
            ):
                continue
        gap = text[cursor : match.start()]
        replacement = rule["masked_name"]
        start = masked_length + len(gap)
        parts.extend((gap, replacement))
        spans.append(
            {
                "start": match.start(),
                "end": match.end(),
                "masked_start": start,
                "masked_end": start + len(replacement),
                "ticker": rule["ticker"],
                "replacement": replacement,
            }
        )
        masked_length = start + len(replacement)
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts), spans


def mask_chunks(database=DATABASE, companies=None):
    rules, matcher = company_rules(graph_companies() if companies is None else companies)
    database = Path(database)
    if not database.is_file():
        raise ValueError("Import the processed exports into DuckDB before masking.")
    counts = Counter()
    with duckdb.connect(str(database)) as db:
        # Closing the connection rolls back if anything fails before COMMIT.
        db.execute("BEGIN TRANSACTION")
        rows = db.execute("SELECT chunk_id, text FROM chunks ORDER BY chunk_id").fetchall()
        if not rows:
            raise ValueError("No stored chunks to mask.")
        if db.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='company_masks'"
        ).fetchone()[0]:
            existing = dict(db.execute("SELECT ticker, masked_name FROM company_masks").fetchall())
            if existing and existing != {r["ticker"]: r["masked_name"] for r in rules}:
                raise ValueError("Existing company labels conflict with the agreed stable mapping.")
        updates = []
        changed = 0
        for chunk_id, text in rows:
            masked, spans = mask_text(text, matcher, rules)
            counts.update(span["ticker"] for span in spans)
            changed += bool(spans)
            updates.append((masked, json.dumps(spans), chunk_id))
        heading_updates = []
        for section_id, title in db.execute(
            "SELECT section_id, title FROM sections ORDER BY section_id"
        ).fetchall():
            masked, spans = mask_text(title, matcher, rules)
            heading_updates.append((masked, json.dumps(spans), section_id))
        db.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS masked_text VARCHAR")
        db.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS mask_spans JSON")
        db.execute("ALTER TABLE sections ADD COLUMN IF NOT EXISTS masked_title VARCHAR")
        db.execute("ALTER TABLE sections ADD COLUMN IF NOT EXISTS title_mask_spans JSON")
        db.execute("""CREATE TABLE IF NOT EXISTS company_masks (
            ticker VARCHAR PRIMARY KEY, company_iri VARCHAR NOT NULL UNIQUE,
            company_name VARCHAR NOT NULL, masked_name VARCHAR NOT NULL UNIQUE,
            aliases JSON NOT NULL, pattern VARCHAR NOT NULL
        )""")
        db.execute("DELETE FROM company_masks")
        db.executemany(
            "INSERT INTO company_masks VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    r["ticker"],
                    r["company_iri"],
                    r["company_name"],
                    r["masked_name"],
                    json.dumps(r["aliases"]),
                    r["pattern"],
                )
                for r in rules
            ],
        )
        db.executemany("UPDATE chunks SET masked_text=?, mask_spans=? WHERE chunk_id=?", updates)
        db.executemany(
            "UPDATE sections SET masked_title=?, title_mask_spans=? WHERE section_id=?",
            heading_updates,
        )
        db.execute("COMMIT")
    return {
        "chunks": len(updates),
        "changed_chunks": changed,
        "sections": len(heading_updates),
        "replacements": sum(counts.values()),
        "replacements_by_ticker": {ticker: counts[ticker] for ticker in TICKERS},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    args = parser.parse_args()
    print(json.dumps(mask_chunks(args.database), indent=2))


if __name__ == "__main__":
    main()
