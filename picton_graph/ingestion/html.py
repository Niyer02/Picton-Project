"""Turn cached SEC HTML into clean sections and paragraph/table chunks."""

import argparse
import hashlib
import json
import re
import sys
import warnings
from importlib.metadata import version
from pathlib import Path

from bs4 import (
    BeautifulSoup,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    ProcessingInstruction,
    XMLParsedAsHTMLWarning,
)

from picton_graph.config import COMPANIES_PATH, ROOT
from picton_graph.files import save, save_json, utc_now
from picton_graph.ingestion.sec import basket, decode

MAX_CHARS = 6000
BLOCK_TAGS = {
    "p",
    "div",
    "li",
    "blockquote",
    "section",
    "article",
    "header",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "pre",
    "dt",
    "dd",
}
HIDDEN = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important)?\s*(?:;|$)",
    re.I,
)
ITEM = re.compile(
    r"^items?\s*(\d{1,2}(?:\.\d{2}|[a-k])?(?:\s*(?:,|and|&)\s*(?:item\s*)?\d{1,2}(?:\.\d{2}|[a-k])?)*)[.:\s–—-]*(.*)$",
    re.I,
)
PART = re.compile(r"^part\s+(IV|III|II|I)\b[.:\s–—-]*(.*)$", re.I)
TITLES = {
    "10-K": {
        "1": "business",
        "1A": "risk factors",
        "1B": "unresolved staff comments",
        "1C": "cybersecurity",
        "2": "properties",
        "3": "legal proceedings",
        "4": "mine safety|submission of matters",
        "5": "market for",
        "6": r"[\[(]?reserved|selected financial",
        "7": "management",
        "7A": "quantitative and qualitative",
        "8": "financial statements",
        "9": "changes in and disagreements",
        "9A": "controls and procedures",
        "9B": "other information",
        "9C": "disclosure regarding foreign",
        "10": "directors",
        "11": "executive compensation",
        "12": "security ownership",
        "13": "certain relationships",
        "14": "principal account",
        "15": "exhibits?",
        "16": "form 10-k summary",
    },
    "10-Q:I": {
        "1": r"(?:(?:condensed|consolidated)\s+)*financial statements",
        "2": "management",
        "3": "quantitative and qualitative",
        "4": "controls and procedures",
    },
    "10-Q:II": {
        "1": "legal proceedings",
        "1A": "risk factors",
        "2": "unregistered sales|changes in securities",
        "3": "defaults",
        "4": "mine safety|submission of matters",
        "5": "other information",
        "6": "exhibits",
    },
    "20-F": {
        "1": "identity of directors",
        "2": "offer statistics",
        "3": "key information",
        "4": "information on the company",
        "4A": "unresolved staff",
        "5": "operating and financial",
        "6": "directors",
        "7": "major shareholders",
        "8": "financial information",
        "9": "the offer and listing",
        "10": "additional information",
        "11": "quantitative and qualitative",
        "12": "description of securities",
        "12A": "debt securities|description of securities",
        "12B": "warrants and rights|description of securities",
        "12C": "other securities|description of securities",
        "12D": "american depositary|depositary|description of securities",
        "13": "defaults",
        "14": "material modifications",
        "15": "controls and procedures",
        "16": r"\[?reserved",
        "16A": "audit committee",
        "16B": "code of ethics",
        "16C": "principal account",
        "16D": "exemptions",
        "16E": "purchases of equity",
        "16F": "change in registrant",
        "16G": "corporate governance",
        "16H": "mine safety",
        "16I": "disclosure regarding foreign",
        "16J": "insider trading",
        "16K": "cybersecurity",
        "17": "financial statements",
        "18": "financial statements",
        "19": "exhibits",
    },
    "8-K": {
        "1.01": "entry into",
        "1.02": "termination of",
        "1.03": "bankruptcy",
        "1.04": "mine safety",
        "1.05": "material cybersecurity",
        "2.01": "completion of",
        "2.02": "results of operations",
        "2.03": "creation of",
        "2.04": "triggering events",
        "2.05": "costs associated",
        "2.06": "material impairments",
        "3.01": "notice of delisting",
        "3.02": "unregistered sales",
        "3.03": "material modification",
        "4.01": "changes in registrant",
        "4.02": "non.reliance",
        "5.01": "changes in control",
        "5.02": "departure of|appointment of|election of",
        "5.03": "amendments to",
        "5.04": "temporary suspension",
        "5.05": "amendments to",
        "5.06": "change in shell",
        "5.07": "submission of matters",
        "5.08": "shareholder director",
        "6.01": "abs informational",
        "6.02": "change of servicer",
        "6.03": "change in credit",
        "6.04": "failure to make",
        "6.05": "securities act",
        "7.01": "regulation fd|results of operations",
        "8.01": "other events",
        "9.01": "financial statements",
    },
}


def normalize(text):
    return " ".join(text.replace("\u00ad", "").replace("\u200b", "").replace("\ufeff", "").split())


def styled_heading(tag, text):
    if not 4 <= len(text) <= 180 or text.endswith((".", ";", ",")):
        return False
    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return True
    letters = bold = large = 0
    for string in tag.find_all(string=True):
        size = len(normalize(str(string)))
        letters += size
        ancestors = [string.parent, *string.parent.parents]
        for parent in ancestors:
            if parent.name in {"b", "strong"} or re.search(
                r"font-weight\s*:\s*(?:bold|[6-9]00)", parent.get("style", ""), re.I
            ):
                bold += size
                break
            if parent is tag:
                break
        for parent in ancestors:
            match = re.search(r"font-size\s*:\s*([\d.]+)(pt|px)", parent.get("style", ""), re.I)
            if match and float(match[1]) >= (13 if match[2].lower() == "pt" else 18):
                large += size
                break
            if parent is tag:
                break
    return letters > 0 and max(bold, large) / letters >= 0.9


def text_blocks(html):
    html = decode(html)
    if re.match(r"\s*<document>", html, re.I):
        wrapper = re.search(r"<text>(.*)</text>", html, re.I | re.S)
        if not wrapper:
            raise ValueError("SEC document wrapper has no TEXT body.")
        html = wrapper[1]
    # These are HTML filings with an optional XML declaration, not standalone XBRL XML.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(html, "lxml")
    notes = []
    for style in soup.find_all("style"):
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", style.get_text(), re.I):
            notes.append("stylesheet_visibility_not_evaluated")
            break
    if soup.find(style=re.compile(r"position\s*:\s*(?:absolute|var\()", re.I)):
        notes.append("positioned_layout; text_follows_source_order")
    if soup.find("img"):
        notes.append("images_not_extracted")
    for tag in list(soup.find_all(True)):
        if tag.name and (
            tag.name in {"head", "script", "style", "noscript", "ix:header", "ix:hidden"}
            or tag.has_attr("hidden")
            or HIDDEN.search(tag.get("style", ""))
        ):
            tag.decompose()
    for comment in soup.find_all(
        string=lambda s: isinstance(s, (Comment, Declaration, Doctype, ProcessingInstruction))
    ):
        comment.extract()

    blocks, pending = [], []
    pending_linked = False
    owner = soup.body or soup

    def flush():
        nonlocal pending_linked
        text = normalize("".join(pending))
        pending.clear()
        if text:
            blocks.append(
                {
                    "kind": "text",
                    "text": text,
                    "heading": styled_heading(owner, text),
                    "linked": pending_linked,
                }
            )
        pending_linked = False

    def cell_text(cell):
        parts = []

        def visit(node):
            if isinstance(node, NavigableString):
                parts.append(str(node))
                return
            boundary = node.name in BLOCK_TAGS or node.name in {"br", "tr", "td", "th"}
            if boundary:
                parts.append(" ")
            for child in node.children:
                visit(child)
            if boundary:
                parts.append(" ")

        visit(cell)
        return normalize("".join(parts))

    def table_block(table):
        rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
        grid, occupied = [], {}
        for ri, row in enumerate(rows):
            values, col = {}, 0
            for cell in row.find_all(["td", "th"], recursive=False):
                while (ri, col) in occupied:
                    col += 1
                colspan = int(cell.get("colspan", 1))
                rowspan = int(cell.get("rowspan", 1))
                if not 1 <= colspan <= 1000 or not 0 <= rowspan <= 10000:
                    raise ValueError("Invalid or excessive HTML table span.")
                if rowspan == 0:
                    rowspan = len(rows) - ri
                values[col] = cell_text(cell)
                for r in range(ri, min(ri + rowspan, len(rows))):
                    for c in range(col, col + colspan):
                        occupied[r, c] = True
                col += colspan
            width = max((c + 1 for r, c in occupied if r == ri), default=0)
            grid.append([values.get(c, "") for c in range(width)])
        width = max(map(len, grid), default=0)
        grid = [row + [""] * (width - len(row)) for row in grid]
        columns = [c for c in range(width) if any(row[c] for row in grid)]
        grid = [[row[c] for c in columns] for row in grid if any(row)]
        if not grid:
            return
        text = "\n".join("\t".join(row) for row in grid)
        # Layout tables also carry titles and numbered paragraphs; retain their row boundaries.
        if len(grid) == 1 or len(columns) == 1:
            for row in grid:
                blocks.append(
                    {
                        "kind": "text",
                        "text": normalize(" ".join(row)),
                        "heading": styled_heading(table, normalize(" ".join(row))),
                        "linked": bool(table.find("a", href=True)),
                    }
                )
        else:
            blocks.append(
                {
                    "kind": "table",
                    "text": text,
                    "heading": False,
                    "linked": bool(table.find("a", href=True)),
                }
            )

    def walk(node):
        nonlocal owner, pending_linked
        if isinstance(node, NavigableString):
            pending.append(str(node))
            if normalize(str(node)) and node.find_parent("a", href=True):
                pending_linked = True
            return
        if node.name == "table":
            flush()
            if node.find("table"):
                # Nested tables are usually page layouts. Walk their cells in source order.
                for child in node.children:
                    walk(child)
            else:
                caption = node.find("caption", recursive=False)
                if caption:
                    blocks.append(
                        {
                            "kind": "text",
                            "text": cell_text(caption),
                            "heading": False,
                            "linked": False,
                        }
                    )
                table_block(node)
            return
        boundary = (
            node.name in BLOCK_TAGS
            or node.name in {"td", "th", "tr"}
            or re.search(r"(?:^|;)\s*display\s*:\s*block\b", node.get("style", ""), re.I)
        )
        previous = owner
        if boundary:
            flush()
            owner = node
        padding = re.search(
            r"(?:^|;)\s*padding-left\s*:\s*([\d.]+)(?:pt|px|em)", node.get("style", ""), re.I
        )
        if not boundary and padding and float(padding[1]) > 0:
            pending.append(" ")
        if node.name in {"br", "hr"}:
            pending.append(" ")
        else:
            for child in node.children:
                walk(child)
        if boundary:
            flush()
            owner = previous

    walk(soup.body or soup)
    flush()
    if not blocks:
        if "images_not_extracted" in notes:
            return [], notes + ["image_only_document; no_extractable_text"]
        raise ValueError("HTML has no visible text.")

    def compact(text):
        return re.sub(r"\s+", "", normalize(text))

    expected, actual = (
        compact("".join((soup.body or soup).strings)),
        compact("".join(b["text"] for b in blocks)),
    )
    if expected != actual:
        offset = next(
            (i for i, pair in enumerate(zip(expected, actual)) if pair[0] != pair[1]),
            min(len(expected), len(actual)),
        )
        raise ValueError(
            f"Visible source text changed at character {offset}: "
            f"{expected[offset : offset + 60]!r} became {actual[offset : offset + 60]!r}"
        )

    # Designed reports often map SEC Items to overlapping pages. Their own ToC is
    # a safer source of document headings than treating every bold line as one.
    def title_key(text):
        return re.sub(r"\W+", "", text.casefold())

    contents_titles = {}
    for row in soup.find_all("tr"):
        cells = [(c, cell_text(c)) for c in row.find_all(["td", "th"], recursive=False)]
        cells = [(c, text) for c, text in cells if text]
        if len(cells) == 2:
            for page, title in (cells, cells[::-1]):
                if (
                    re.fullmatch(r"\d+(?:\s*[-–—,]\s*\d+)*", page[1])
                    and page[0].find("a", href=re.compile("^#"))
                    and 4 <= len(title[1]) <= 180
                    and re.search(r"[a-z]", title[1], re.I)
                ):
                    contents_titles[title_key(title[1])] = title[1]
    for i, block in enumerate(blocks):
        if not block["heading"] or block["linked"] or block["kind"] != "text":
            continue
        title = ""
        for following in blocks[i : i + 3]:
            if following["kind"] != "text" or following["linked"] or len(following["text"]) > 180:
                break
            title += " " + following["text"]
            if title_key(title) in contents_titles:
                block["toc_title"] = contents_titles[title_key(title)]
                break
    return blocks, notes


def item_heading(text, form, part):
    match = ITEM.match(text)
    if not match or len(text) > 260:
        return None
    items = re.findall(r"\d{1,2}(?:\.\d{2}|[A-K])?", match[1].upper())
    title = match[2].strip()
    forms = [f"10-Q:{p}" for p in ([part] if part else ["I", "II"])] if form == "10-Q" else [form]
    for key in forms:
        choices = TITLES.get(key, {})
        if all(item in choices for item in items) and re.match(
            "(?:" + choices[items[0]] + ")", title, re.I
        ):
            return {
                "items": items,
                "part": key.split(":")[1] if ":" in key else part,
                "title": text,
                "kind": "sec_item",
            }
    return None


def split_sections(blocks, form, role):
    headings, part = {}, None
    notes = []
    if role == "primary" and form in {"10-K", "10-Q", "20-F", "8-K"}:
        for i, block in enumerate(blocks):
            if block["linked"] or block["kind"] == "table":
                continue
            text = block["text"]
            part_match = PART.match(text)
            if part_match and len(text) < 100:
                part = part_match[1].upper()
                headings[i] = {"kind": "part", "title": text, "part": part, "items": []}
            elif text.upper().rstrip(".:") in {"SIGNATURE", "SIGNATURES"}:
                headings[i] = {"kind": "signatures", "title": text, "part": None, "items": []}
            else:
                # Some publishers place the item number and its title in separate blocks.
                if ITEM.match(text) and not ITEM.match(text)[2] and i + 1 < len(blocks):
                    following = blocks[i + 1]
                    if following["kind"] == "text" and not following["linked"]:
                        text += " " + following["text"]
                heading = item_heading(text, form, part)
                if heading:
                    headings[i] = heading
                elif (
                    ITEM.match(text)
                    and block["heading"]
                    and not ITEM.match(text)[2].startswith("(")
                ):
                    notes.append("unrecognized_item_heading: " + text[:180])
    sec_count = sum(h["kind"] == "sec_item" for h in headings.values())
    if not sec_count:
        use_contents = sum("toc_title" in b for b in blocks) >= 3
        headings = {
            i: {
                "kind": "document_heading",
                "title": b.get("toc_title", b["text"]),
                "items": [],
                "part": None,
            }
            for i, b in enumerate(blocks)
            if (
                b.get("toc_title")
                if use_contents
                else b["heading"] and not b["linked"] and b["kind"] == "text"
            )
        }
        if role == "primary" and form in {"10-K", "10-Q", "20-F", "8-K"}:
            notes.append("no_reliable_sec_item_outline; retained_document_headings")
    sections = []
    for i, block in enumerate(blocks):
        if i in headings or not sections:
            heading = headings.get(
                i,
                {
                    "kind": "front_matter" if headings else "document",
                    "title": "Front matter" if headings else "Document",
                    "items": [],
                    "part": None,
                },
            )
            sections.append({**heading, "blocks": []})
        sections[-1]["blocks"].append(block)
    return sections, notes


def chunk_section(blocks, max_chars=MAX_CHARS):
    # Keep tables and paragraphs whole; the target is soft, never a truncation limit.
    groups, current, length = [], [], 0
    for block in blocks:
        text = block["text"]
        if current and length + len(text) + 2 > max_chars:
            # Carry nearby captions/units into the same chunk as their table.
            carry = []
            if block["kind"] == "table":
                while (
                    current
                    and current[-1]["kind"] == "text"
                    and len(current[-1]["text"]) < 240
                    and len(carry) < 3
                ):
                    carry.insert(0, current.pop())
            if current:
                groups.append(current)
            current = carry
            length = sum(len(b["text"]) + 2 for b in current)
        current.append(block)
        length += len(text) + 2
    if current:
        groups.append(current)
    return [
        {
            "text": "\n\n".join(b["text"] for b in group),
            "contains_table": any(b["kind"] == "table" for b in group),
        }
        for group in groups
    ]


def process_document(payload, source):
    blocks, notes = text_blocks(payload)
    sections, section_notes = split_sections(
        blocks, source["form"].removesuffix("/A"), source["role"]
    )
    document_id = f"{source['ticker']}/{source['accession']}/{source['filename']}"
    for index, section in enumerate(sections):
        section["section_id"] = f"{document_id}#section-{index:04d}"
        content = section.pop("blocks")
        section["text"] = "\n\n".join(block["text"] for block in content)
        section["chunks"] = chunk_section(content)
        for n, chunk in enumerate(section["chunks"]):
            chunk["chunk_id"] = f"{section['section_id']}-chunk-{n:04d}"
            chunk["character_count"] = len(chunk["text"])
            chunk["over_target"] = len(chunk["text"]) > MAX_CHARS
        assert "\n\n".join(c["text"] for c in section["chunks"]) == section["text"]
    assert "\n\n".join(s["text"] for s in sections) == "\n\n".join(b["text"] for b in blocks)
    return {
        "document_id": document_id,
        "source": source,
        "status": "processed" if sections else "no_extractable_text",
        "warnings": notes + section_notes,
        "sections": sections,
        "text_block_count": len(blocks),
    }


def process_corpus(
    source_root=ROOT / "data/sec", output_root=ROOT / "data/processed", tickers=None
):
    source_root, output_root = Path(source_root).resolve(), Path(output_root).resolve()
    if (
        output_root == source_root
        or source_root in output_root.parents
        or output_root in source_root.parents
    ):
        raise ValueError("Processing output must be separate from original SEC files.")
    allowed = {c["ticker"] for c in basket(COMPANIES_PATH)}
    if tickers and not set(tickers) <= allowed:
        raise ValueError("Tickers must come from companies.md.")
    paths = [
        p
        for p in sorted(source_root.glob("*/*/metadata.json"))
        if not tickers or p.parent.parent.name in tickers
    ]
    if not paths:
        raise ValueError("No filing metadata found.")
    manifest = {
        "started_at": utc_now(),
        "complete": False,
        "documents": [],
        "beautifulsoup4_version": version("beautifulsoup4"),
        "lxml_version": version("lxml"),
        "max_chars": MAX_CHARS,
    }
    manifest_path = output_root / "manifest.json"
    save_json(manifest_path, manifest)
    try:
        for path in paths:
            filing = json.loads(path.read_text())
            ticker, accession = filing["company"]["ticker"], filing["accession"]
            if (
                ticker not in allowed
                or ticker != path.parent.parent.name
                or accession != path.parent.name
            ):
                raise ValueError(f"Filing metadata does not match its path: {path}")
            for doc in filing["documents"]:
                raw = (source_root / doc["raw_path"]).resolve()
                if (
                    not raw.is_relative_to(source_root)
                    or raw.parent != (path.parent / "raw").resolve()
                    or raw.name != doc["filename"]
                ):
                    raise ValueError(f"Unexpected original file path: {raw}")
                if raw.suffix.lower() not in {".htm", ".html"}:
                    raise ValueError(f"Expected HTML, got {raw}")
                payload = raw.read_bytes()
                if hashlib.sha256(payload).hexdigest() != doc["sha256"]:
                    raise ValueError(f"Original HTML hash mismatch: {raw}")
                source = {
                    **doc,
                    "ticker": ticker,
                    "accession": accession,
                    "form": filing["form"],
                    "filing_date": filing["filing_date"],
                    "report_date": filing["report_date"],
                    "accepted_at": filing["accepted_at"],
                }
                try:
                    result = process_document(payload, source)
                except ValueError as exc:
                    raise ValueError(f"{raw}: {exc}") from exc
                target = output_root / ticker / accession / (raw.name + ".json")
                save_json(target, result)
                text_path = target.with_suffix(".txt")
                text = "\n\n".join(s["text"] for s in result["sections"])
                save(text_path, (text + "\n" if text else "").encode())
                chunks = [c for s in result["sections"] for c in s["chunks"]]
                manifest["documents"].append(
                    {
                        "document_id": result["document_id"],
                        "output_path": str(target.relative_to(output_root)),
                        "status": result["status"],
                        "source_sha256": doc["sha256"],
                        "output_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                        "sections": len(result["sections"]),
                        "chunks": len(chunks),
                        "sec_items": sum(s["kind"] == "sec_item" for s in result["sections"]),
                        "over_target": sum(c["over_target"] for c in chunks),
                        "warnings": result["warnings"],
                    }
                )
            save_json(manifest_path, manifest)
            if len(manifest["documents"]) % 25 < len(filing["documents"]):
                print(f"Processed {len(manifest['documents'])} documents ({ticker})", flush=True)
        manifest["complete"] = True
    except Exception as exc:
        manifest["error"] = str(exc)
        raise
    finally:
        manifest["finished_at"] = utc_now()
        save_json(manifest_path, manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tickers", nargs="+", help="Subset of the basket; default all cached companies."
    )
    args = parser.parse_args()
    try:
        manifest = process_corpus(tickers=args.tickers)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Processing failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "documents": len(manifest["documents"]),
                **{
                    k: sum(d[k] for d in manifest["documents"])
                    for k in ("sections", "chunks", "over_target")
                },
                "documents_with_warnings": sum(bool(d["warnings"]) for d in manifest["documents"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
