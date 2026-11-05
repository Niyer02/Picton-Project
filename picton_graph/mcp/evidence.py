"""Validate graph evidence and build direct SEC excerpt citations."""

import json
from datetime import date
from urllib.parse import quote, urlsplit

from mcp.server.mcpserver.exceptions import ToolError


def evidence_records(properties):
    try:
        if (
            not isinstance(properties.get("relationship_id"), str)
            or not properties["relationship_id"].strip()
        ):
            raise ValueError("Missing relationship identity")
        records = json.loads(properties["evidence"])
        if (
            not isinstance(records, list)
            or not records
            or type(properties["evidence_count"]) is not int
            or properties["evidence_count"] != len(records)
        ):
            raise ValueError("Invalid evidence count")
        for record in records:
            if not isinstance(record, dict) or any(
                not isinstance(record.get(key), str) or not record[key].strip()
                for key in ("id", "evidence_text", "document", "document_type")
            ):
                raise ValueError("Invalid evidence fields")
            if record["document_date"] is not None:
                if (
                    date.fromisoformat(record["document_date"]).isoformat()
                    != record["document_date"]
                ):
                    raise ValueError("Document dates must use YYYY-MM-DD")
        return records
    except (KeyError, TypeError, ValueError):
        raise ToolError(
            "An edge has missing or invalid evidence. Repair it with python -m picton_graph.graph.importer before querying."
        ) from None


def citation_url(record):
    document = record["document"]
    try:
        url = urlsplit(document)
        if (
            url.scheme != "https"
            or url.hostname not in ("www.sec.gov", "sec.gov")
            or url.username
            or url.password
            or url.port not in (None, 443)
            or not url.path.startswith("/Archives/edgar/data/")
            or any(character.isspace() or ord(character) < 32 for character in document)
        ):
            raise ValueError("Invalid SEC URL")
    except ValueError:
        raise ToolError(
            "Evidence has an invalid SEC document URL. Repair the stored source URL."
        ) from None
    lines = [
        " ".join(line.split()) for line in record["evidence_text"].splitlines() if line.strip()
    ]
    if not lines:
        raise ToolError("Evidence has no text to highlight.")
    text = " ".join(lines)
    if len(lines) == 1 and (len(text) <= 300 or len(text.split()) <= 24):
        selectors = [text]
    else:
        # Range endpoints stay within source lines; the excerpt may span HTML blocks.
        selectors = [" ".join(lines[0].split()[:12]), " ".join(lines[-1].split()[-12:])]
    directive = ",".join(quote(value, safe="").replace("-", "%2D") for value in selectors)
    anchor = url.fragment.partition(":~:")[0]
    return url._replace(fragment=anchor + ":~:text=" + directive).geturl()


def edge_evidence(properties, evidence_limit):
    records = evidence_records(properties)
    records.sort(key=lambda r: (r["document_date"] or "", r["id"]), reverse=True)
    selected, seen = [], set()
    for record in records:
        key = (record["document"], record["evidence_text"])
        if key in seen:
            continue
        seen.add(key)
        excerpt = dict(record)
        excerpt["citation_url"] = citation_url(record)
        if len(excerpt["evidence_text"]) > 3000:
            excerpt["evidence_text"] = excerpt["evidence_text"][:3000]
            excerpt["evidence_text_truncated"] = True
        selected.append(excerpt)
        if len(selected) == evidence_limit:
            break
    return {
        "relationship_id": properties["relationship_id"],
        "evidence_count": properties["evidence_count"],
        "evidence": selected,
        "evidence_truncated": len(selected) < len(records),
    }
