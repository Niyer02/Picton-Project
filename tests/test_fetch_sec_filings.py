import json
import unittest
from datetime import date
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from picton_graph.config import COMPANIES_PATH
from picton_graph.ingestion.sec import (
    SUBMISSIONS_URL,
    TICKERS_URL,
    SecClient,
    basket,
    discover_filings,
    documents,
    fetch_corpus,
    fetch_filing,
    filing_rows,
    save_json,
)

ACCESSION = "0001045810-25-000023"
COMPANY = {"name": "NVIDIA", "ticker": "NVDA", "cik": "0001045810"}
BASE = "https://www.sec.gov/Archives/edgar/data/1045810/000104581025000023/"


def columns(rows):
    keys = (
        "accessionNumber",
        "filingDate",
        "form",
        "primaryDocument",
        "reportDate",
        "acceptanceDateTime",
    )
    return {key: [row.get(key, "") for row in rows] for key in keys}


def filing(accession=ACCESSION, filed="2025-02-26", form="10-K"):
    return {
        "accessionNumber": accession,
        "filingDate": filed,
        "form": form,
        "primaryDocument": "report.htm",
        "reportDate": "2025-01-26",
        "acceptanceDateTime": "2025-02-26T21:48:33Z",
    }


def index_html(extra=""):
    return (
        '<table summary="Document Format Files">'
        '<tr><td>1</td><td>Annual report</td><td><a href="/ix?doc=/Archives/edgar/data/1045810/000104581025000023/report.htm">report.htm</a> iXBRL</td><td>10-K</td><td>10</td></tr>'
        '<tr><td>2</td><td>Press release</td><td><a href="release.htm">release.htm</a></td><td>EX-99.1</td><td>10</td></tr>'
        '<tr><td>3</td><td>Certification</td><td><a href="cert.htm">cert.htm</a></td><td>EX-31.1</td><td>10</td></tr>'
        + extra
        + "</table>"
        '<table summary="Data Files"><tr><td>4</td><td>Data</td><td><a href="facts.xml">facts.xml</a></td><td>EX-101.INS</td></tr></table>'
    )


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value

    def json(self, url, path):
        value = json.loads(self.get(url))
        save_json(path, value)
        return value


class SecFilingTests(unittest.TestCase):
    def test_basket_matches_the_ten_companies(self):
        self.assertEqual(
            {c["ticker"] for c in basket(COMPANIES_PATH)},
            {"ASML", "AMAT", "LRCX", "KLAC", "TSM", "INTC", "MU", "NVDA", "AMD", "AVGO"},
        )

    def test_five_year_discovery_reads_history_filters_and_deduplicates(self):
        oldest = filing("0001045810-21-000100", "2021-11-04", "10-Q")
        amendment = filing("0001045810-25-000024", "2025-02-27", "10-K/A")
        current = {
            "cik": 1045810,
            "filings": {
                "recent": columns(
                    [
                        filing(),
                        amendment,
                        filing("0001045810-26-000001", "2026-11-05"),
                        filing(form="4"),
                    ]
                ),
                "files": [
                    {"name": "old.json", "filingFrom": "2020-01-01", "filingTo": "2025-02-26"},
                    {
                        "name": "irrelevant.json",
                        "filingFrom": "1999-01-01",
                        "filingTo": "2020-12-31",
                    },
                ],
            },
        }
        history = columns([oldest, filing(), filing("0001045810-21-000099", "2021-11-03")])
        client = FakeClient(
            {
                SUBMISSIONS_URL + "CIK0001045810.json": json.dumps(current).encode(),
                SUBMISSIONS_URL + "old.json": json.dumps(history).encode(),
            }
        )
        with TemporaryDirectory() as directory:
            rows = discover_filings(
                client,
                COMPANY,
                Path(directory),
                date(2021, 11, 4),
                date(2026, 11, 4),
                ("10-K", "10-Q"),
            )
        self.assertEqual(
            [row["accessionNumber"] for row in rows],
            [oldest["accessionNumber"], ACCESSION, amendment["accessionNumber"]],
        )
        self.assertEqual(len(client.calls), 2)

    def test_primary_and_narrative_exhibits_include_foreign_reports(self):
        contract = '<tr><td>5</td><td>Contract</td><td><a href="contract.htm">contract.htm</a></td><td>EX-10.1</td></tr>'
        for form in ("10-K", "20-F", "6-K", "8-K"):
            with self.subTest(form=form):
                rows = documents(index_html(contract).replace("10-K", form), BASE, "report.htm")
                self.assertEqual(
                    [row["filename"] for row in rows], ["report.htm", "release.htm", "contract.htm"]
                )
                self.assertEqual(rows[0]["url"], BASE + "report.htm")
                self.assertEqual(rows[0]["role"], "primary")

    def test_misaligned_submission_columns_are_rejected(self):
        for key in ("form", "acceptanceDateTime"):
            malformed = columns([filing()])
            malformed[key] = []
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "inconsistent lengths"):
                list(filing_rows(malformed))

    def test_index_preserves_inline_text_and_ignores_non_document_rows(self):
        html = index_html(
            "<tr><th>Sequence</th><th>Description</th><th>Document</th><th>Type</th></tr>"
            '<tr><td><a href="partial.htm">Partial row</a></td></tr>'
            "<tr><td>5</td><td>No link</td><td>missing.htm</td><td>EX-99.1</td></tr>"
        )
        html = html.replace("Annual report", "Annual <b>report</b> &amp; accounts")
        html = html.replace(" iXBRL</td>", ' <a href="facts.xml">iXBRL</a></td>')
        html = html.replace("EX-101.INS", "EX-99.1")
        rows = documents(html, BASE, "report.htm")
        self.assertEqual([row["filename"] for row in rows], ["report.htm", "release.htm"])
        self.assertEqual(rows[0]["description"], "Annual report & accounts")

    def test_unexpected_document_locations_and_missing_primary_fail(self):
        for html in (
            index_html().replace('href="release.htm"', 'href="https://example.com/release.htm"'),
            index_html().replace('href="release.htm"', 'href="../release.htm"'),
            index_html().replace("report.htm", "different.htm"),
        ):
            with self.subTest(html=html):
                with self.assertRaises(ValueError):
                    documents(html, BASE, "report.htm")

    def test_download_reuses_hash_checked_files_and_retains_provenance(self):
        responses = {
            BASE + ACCESSION + "-index.html": index_html().encode(),
            BASE + "report.htm": b"<p>Annual narrative.</p>",
            BASE + "release.htm": b"<p>Micron supplies NVIDIA.</p>",
        }
        client = FakeClient(responses)
        with TemporaryDirectory() as directory:
            output = Path(directory)
            first = fetch_filing(client, COMPANY, filing(), output)
            self.assertEqual(len(client.calls), 3)
            release = first["documents"][1]
            self.assertEqual(
                (output / release["raw_path"]).read_bytes(), responses[BASE + "release.htm"]
            )
            self.assertFalse((output / "NVDA" / ACCESSION / "text").exists())
            self.assertNotIn("text_path", release)
            self.assertNotIn("text_status", release)
            self.assertEqual(first["accepted_at"], filing()["acceptanceDateTime"])
            second = fetch_filing(client, COMPANY, filing(), output)
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(first, second)
            (output / release["raw_path"]).write_bytes(b"corrupted")
            third = fetch_filing(client, COMPANY, filing(), output)
            self.assertEqual(client.calls[-1], BASE + "release.htm")
            self.assertEqual(len(client.calls), 4)
            self.assertEqual(third["documents"][1]["sha256"], release["sha256"])

    def test_partial_fetch_is_not_reported_as_complete_and_can_resume(self):
        current = {"cik": 1045810, "filings": {"recent": columns([filing()]), "files": []}}
        client = FakeClient(
            {
                TICKERS_URL: json.dumps(
                    {"0": {"ticker": "NVDA", "cik_str": 1045810, "title": "NVIDIA CORP"}}
                ).encode(),
                SUBMISSIONS_URL + "CIK0001045810.json": json.dumps(current).encode(),
                BASE + ACCESSION + "-index.html": index_html().encode(),
                BASE + "report.htm": b"<p>Annual narrative.</p>",
                BASE + "release.htm": OSError("connection lost"),
            }
        )
        with TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(OSError, "connection lost"):
                fetch_corpus(
                    client, [COMPANY], output, date(2021, 11, 4), date(2026, 11, 4), ("10-K",)
                )
            failed = json.loads((output / "manifest.json").read_text())
            self.assertFalse(failed["complete"])
            self.assertEqual(failed["error"], "connection lost")
            client.responses[BASE + "release.htm"] = b"<p>Recovered.</p>"
            result = fetch_corpus(
                client, [COMPANY], output, date(2021, 11, 4), date(2026, 11, 4), ("10-K",)
            )
            self.assertTrue(result["complete"])
            self.assertEqual(len(result["filings"]), 1)
            self.assertEqual(client.calls.count(BASE + "report.htm"), 1)


class SecClientTests(unittest.TestCase):
    def test_identification_is_required_before_requests(self):
        for identity in ("", "anonymous", "Name test@example.com\nInjected: header"):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                SecClient(identity)

    @patch("picton_graph.ingestion.sec.time.sleep")
    @patch("picton_graph.ingestion.sec.urlopen")
    def test_incomplete_responses_retry_without_returning_partial_bytes(self, urlopen, sleep):
        urlopen.side_effect = [IncompleteRead(b"partial"), BytesIO(b"complete")]
        self.assertEqual(SecClient("Offline Test test@example.com").get(TICKERS_URL), b"complete")
        self.assertEqual(urlopen.call_count, 2)

    @patch("picton_graph.ingestion.sec.time.sleep")
    @patch("picton_graph.ingestion.sec.urlopen")
    def test_transient_errors_retry_with_identification_and_timeout(self, urlopen, sleep):
        urlopen.side_effect = [
            HTTPError(TICKERS_URL, 429, "slow down", {"Retry-After": "2"}, None),
            BytesIO(b'{"ok": true}'),
        ]
        client = SecClient("Offline Test test@example.com")
        self.assertEqual(client.get(TICKERS_URL), b'{"ok": true}')
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(
            urlopen.call_args.args[0].get_header("User-agent"), "Offline Test test@example.com"
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)
        self.assertIn(unittest.mock.call(2.0), sleep.call_args_list)

    @patch("picton_graph.ingestion.sec.time.sleep")
    @patch("picton_graph.ingestion.sec.urlopen")
    def test_access_denial_is_not_retried_or_cached(self, urlopen, sleep):
        client = SecClient("Offline Test test@example.com")
        urlopen.side_effect = HTTPError(TICKERS_URL, 403, "denied", {}, None)
        with TemporaryDirectory() as directory:
            target = Path(directory) / "cached.json"
            with self.assertRaises(HTTPError):
                client.json(TICKERS_URL, target)
            self.assertFalse(target.exists())
        self.assertEqual(urlopen.call_count, 1)

    @patch("picton_graph.ingestion.sec.time.sleep")
    @patch(
        "picton_graph.ingestion.sec.urlopen",
        return_value=BytesIO(b"<html>Request Rate Threshold Exceeded</html>"),
    )
    def test_html_access_block_is_rejected(self, urlopen, sleep):
        with self.assertRaisesRegex(ValueError, "access-block"):
            SecClient("Offline Test test@example.com").get(TICKERS_URL)


if __name__ == "__main__":
    unittest.main()
