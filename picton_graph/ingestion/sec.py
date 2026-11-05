"""Fetch the basket's SEC filings and narrative exhibits; no LLM or graph writes."""

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from picton_graph.config import COMPANIES_PATH, ROOT
from picton_graph.files import save, save_json, utc_now

FORMS = ("10-K", "10-Q", "20-F", "6-K", "8-K")
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/"


class SecClient:
    def __init__(self, user_agent):
        if not re.search(r"\S+@\S+\.\S+", user_agent) or any(c in user_agent for c in "\r\n"):
            raise ValueError("Set SEC_USER_AGENT to a name and real contact email.")
        self.user_agent = user_agent
        self.next_request = 0.0
        # Python.org's macOS builds can lack a default CA file; keep TLS verification on.
        self.context = ssl.create_default_context()
        if (
            sys.platform == "darwin"
            and not ssl.get_default_verify_paths().cafile
            and not os.getenv("SSL_CERT_FILE")
        ):
            self.context.load_verify_locations("/etc/ssl/cert.pem")

    def get(self, url):
        request = Request(
            url, headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"}
        )
        for attempt in range(4):
            time.sleep(max(0, self.next_request - time.monotonic()))
            self.next_request = time.monotonic() + 0.25
            try:
                with urlopen(request, timeout=30, context=self.context) as response:
                    payload = response.read()
                if not payload:
                    raise ValueError(f"Empty SEC response: {url}")
                if any(
                    marker in payload[:16000].lower()
                    for marker in (
                        b"your request originates from an undeclared automated tool",
                        b"request rate threshold exceeded",
                    )
                ):
                    raise ValueError(f"SEC returned an access-block page: {url}")
                return payload
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise
                retry_after = exc.headers.get("Retry-After")
                delay = 2**attempt
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        delay = max(
                            delay,
                            (
                                parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                            ).total_seconds(),
                        )
                if delay > 60:
                    raise RuntimeError(f"SEC requested a {delay:.0f}s pause; rerun later.") from exc
                time.sleep(delay)
            except (URLError, TimeoutError, ConnectionError, HTTPException) as exc:
                if attempt == 3 or isinstance(
                    getattr(exc, "reason", None), ssl.SSLCertVerificationError
                ):
                    raise
                time.sleep(2**attempt)

    def json(self, url, path):
        payload = self.get(url)
        value = json.loads(payload)
        save(path, payload)
        return value


def basket(path):
    lines = path.read_text().strip().splitlines()
    if lines[:2] != ["| Company | Ticker |", "|---|---|"]:
        raise ValueError("Expected the Company/Ticker table in companies.md.")
    companies = []
    for line in lines[2:]:
        name, ticker = (value.strip() for value in line.strip("|").split("|"))
        if not name or not re.fullmatch(r"[A-Z0-9.-]+", ticker):
            raise ValueError(f"Invalid company row: {line}")
        companies.append({"name": name, "ticker": ticker})
    if not companies or len({c["ticker"] for c in companies}) != len(companies):
        raise ValueError("The company basket is empty or has duplicate tickers.")
    return companies


def filename(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in (".", ".."):
        raise ValueError(f"Unsafe SEC filename: {value!r}")
    return value


def filing_rows(columns):
    required = ("accessionNumber", "filingDate", "form", "primaryDocument")
    optional = ("reportDate", "acceptanceDateTime", "primaryDocDescription")
    count = len(columns["accessionNumber"])
    if any(
        len(columns[key]) != count
        for key in (*required, *(key for key in optional if key in columns))
    ):
        raise ValueError("SEC submission columns have inconsistent lengths.")
    for index in range(count):
        row = {key: columns[key][index] for key in required}
        for key in optional:
            row[key] = columns[key][index] if key in columns else ""
        yield row


def discover_filings(client, company, output, start, end, forms):
    cik = str(company["cik"]).zfill(10)
    cache = output / "submissions"
    current = client.json(SUBMISSIONS_URL + f"CIK{cik}.json", cache / f"CIK{cik}.json")
    if int(current["cik"]) != int(cik):
        raise ValueError("SEC submission CIK does not match the selected company.")
    groups = [current["filings"]["recent"]]
    for history in current["filings"]["files"]:
        if (
            history.get("filingTo", "9999-12-31") < start.isoformat()
            or history.get("filingFrom", "0001-01-01") > end.isoformat()
        ):
            continue
        name = filename(history["name"])
        groups.append(client.json(SUBMISSIONS_URL + name, cache / name))
    selected = {}
    for columns in groups:
        for row in filing_rows(columns):
            if (
                row["form"].removesuffix("/A") in forms
                and start <= date.fromisoformat(row["filingDate"]) <= end
            ):
                accession = row["accessionNumber"]
                if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
                    raise ValueError(f"Invalid accession number: {accession}")
                filename(row["primaryDocument"])
                selected.setdefault(accession, row)
    return sorted(selected.values(), key=lambda row: (row["filingDate"], row["accessionNumber"]))


def documents(index, base_url, primary):
    """Read filing documents from the SEC index, excluding the XBRL data table."""
    soup = BeautifulSoup(index, "html.parser")
    result = []
    for row in soup.select('table[summary="Document Format Files"] tr'):
        cells = row.find_all("td", recursive=False)
        link = row.select_one("td a[href]")
        if len(cells) < 4 or not link or not link["href"]:
            continue
        description, kind = (" ".join(cells[i].get_text().split()) for i in (1, 3))
        href = link["href"]
        parsed = urlsplit(href)
        if parsed.path in ("/ix", "/ixviewer/doc/action"):
            href = parse_qs(parsed.query)["doc"][0]
        url = urljoin(base_url, href)
        name = urlsplit(url).path.rsplit("/", 1)[-1]
        is_primary = name == primary
        if not is_primary and not re.fullmatch(r"EX-(?:10|99)(?:[.\-].*)?", kind):
            continue
        filename(name)
        if url != base_url + quote(name):
            raise ValueError(f"Unexpected SEC document location: {url}")
        result.append(
            {
                "filename": name,
                "type": kind,
                "role": "primary" if is_primary else "exhibit",
                "description": description,
                "url": url,
            }
        )
    if sum(doc["role"] == "primary" for doc in result) != 1:
        raise ValueError("Filing index must contain exactly one matching primary document.")
    return result


def decode(payload):
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("cp1252")


def fetch_filing(client, company, filing, output):
    accession = filing["accessionNumber"]
    folder = output / company["ticker"] / accession
    metadata_path = folder / "metadata.json"
    previous = (
        json.loads(metadata_path.read_text()) if metadata_path.exists() else {"documents": []}
    )
    old_documents = {doc["filename"]: doc for doc in previous["documents"]}
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(company['cik'])}/{accession.replace('-', '')}/"
    index_url = base_url + accession + "-index.html"
    index_path = folder / "filing-index.html"
    index = index_path.read_bytes() if index_path.exists() else client.get(index_url)
    selected = documents(decode(index), base_url, filing["primaryDocument"])
    save(index_path, index)
    metadata = {
        "company": company,
        "accession": accession,
        "form": filing["form"],
        "filing_date": filing["filingDate"],
        "report_date": filing["reportDate"],
        "accepted_at": filing["acceptanceDateTime"],
        "index_url": index_url,
        "documents": [],
    }
    for doc in selected:
        raw_path = folder / "raw" / doc["filename"]
        old = old_documents.get(doc["filename"], {})
        payload = raw_path.read_bytes() if raw_path.exists() else None
        cached = (
            payload is not None
            and hashlib.sha256(payload).hexdigest() == old.get("sha256")
            and doc["url"] == old.get("url")
        )
        if not cached:
            payload = client.get(doc["url"])
        save(raw_path, payload)
        record = {
            **doc,
            "raw_path": str(raw_path.relative_to(output)),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "retrieved_at": old["retrieved_at"] if cached else utc_now(),
        }
        metadata["documents"].append(record)
        save_json(metadata_path, metadata)
    return metadata


def fetch_corpus(client, companies, output, start, end, forms):
    manifest = {
        "started_at": utc_now(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "forms": list(forms),
        "includes_amendments": True,
        "companies": companies,
        "complete": False,
        "filings": [],
    }
    manifest_path = output / "manifest.json"
    save_json(manifest_path, manifest)
    try:
        lookup = client.json(TICKERS_URL, output / "company_tickers.json")
        for company in companies:
            matches = [entry for entry in lookup.values() if entry["ticker"] == company["ticker"]]
            if len(matches) != 1:
                raise ValueError(f"Expected one SEC ticker mapping for {company['ticker']}.")
            company = {
                **company,
                "cik": str(matches[0]["cik_str"]).zfill(10),
                "sec_name": matches[0]["title"],
            }
            filings = discover_filings(client, company, output, start, end, forms)
            if not filings:
                raise ValueError(
                    f"No matching filings found for {company['ticker']}; check the requested window/forms."
                )
            print(f"{company['ticker']}: {len(filings)} filings", flush=True)
            for position, filing in enumerate(filings, 1):
                result = fetch_filing(client, company, filing, output)
                manifest["filings"].append(result)
                save_json(manifest_path, manifest)
                print(
                    f"  {position}/{len(filings)} {filing['filingDate']} {filing['form']} ({len(result['documents'])} documents)",
                    flush=True,
                )
        manifest["complete"] = True
    except (OSError, ValueError, KeyError, RuntimeError, HTTPException) as exc:
        manifest["error"] = str(exc)
        raise
    finally:
        manifest["finished_at"] = utc_now()
        save_json(manifest_path, manifest)
    return manifest


def main():
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    try:
        start = today.replace(year=today.year - 5)
    except ValueError:
        start = today.replace(year=today.year - 5, day=28)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=start,
        help="Inclusive filing date, YYYY-MM-DD; default five years ago.",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=today,
        help="Inclusive filing date, YYYY-MM-DD; default today in Toronto.",
    )
    parser.add_argument(
        "--tickers", nargs="+", help="Subset of docs/companies.md; defaults to all ten."
    )
    parser.add_argument(
        "--forms", nargs="+", choices=FORMS, default=FORMS, help="Amendments are always included."
    )
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "sec")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start must be on or before --end")
    try:
        companies = basket(COMPANIES_PATH)
        if args.tickers:
            if set(args.tickers) - {company["ticker"] for company in companies}:
                raise ValueError("--tickers must come from docs/companies.md.")
            companies = [company for company in companies if company["ticker"] in args.tickers]
        user_agent = os.getenv("SEC_USER_AGENT", "")
        if not user_agent and (ROOT / ".env").exists():
            user_agent = next(
                (
                    line.split("=", 1)[1].strip()
                    for line in (ROOT / ".env").read_text().splitlines()
                    if line.startswith("SEC_USER_AGENT=")
                ),
                "",
            )
        manifest = fetch_corpus(
            SecClient(user_agent), companies, args.output, args.start, args.end, args.forms
        )
    except (OSError, ValueError, KeyError, RuntimeError, HTTPException) as exc:
        print(f"SEC fetch failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Saved {len(manifest['filings'])} filings to {args.output} as original documents and download metadata."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
