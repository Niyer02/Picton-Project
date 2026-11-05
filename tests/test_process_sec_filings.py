import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from picton_graph.ingestion.html import (
    chunk_section,
    process_corpus,
    process_document,
    text_blocks,
)

SOURCE = {
    "ticker": "NVDA",
    "accession": "0001045810-26-000021",
    "filename": "report.htm",
    "form": "10-K",
    "role": "primary",
}


def parse(html, **source):
    return process_document(html.encode(), {**SOURCE, **source})


class HtmlProcessingTests(unittest.TestCase):
    def test_inline_words_whitespace_entities_and_hidden_content(self):
        html = """<html><head><style>.a { color: red; }</style><title>Hidden title</title></head>
        <body><ix:header><ix:hidden>Hidden XBRL</ix:hidden></ix:header>
        <div hidden>Hidden attribute</div><p style="display: none !important">Hidden style</p>
        <p><ix:nonNumeric>NVIDIA CORP</ix:nonNumeric>ORATION has
        <ix:nonFraction format="ixt:fixed-zero">no</ix:nonFraction>t issued debt.</p>
        <p><b>Framewor</b>k &amp; Micron&nbsp;Technology.<br>Next line.</p>
        <script>hidden script</script><!-- hidden comment --></body></html>"""
        out = parse(html)
        text = "\n\n".join(s["text"] for s in out["sections"])
        self.assertEqual(
            text,
            "NVIDIA CORPORATION has not issued debt.\n\nFramework & Micron Technology. Next line.",
        )
        self.assertNotIn("<", text)

    def test_sec_sgml_wrapper_declarations_and_links_in_other_blocks(self):
        out = parse(
            "<DOCUMENT><TYPE>EX-99.1\n<SEQUENCE>2\n<TEXT>"
            '<!DOCTYPE html PUBLIC "x"><html><head><title>Document</title></head><body>'
            '<div>Item 1. Business<table><tr><td><a href="x">Reference</a></td></tr></table></div>'
            '<p><span>1</span><span style="padding-left:4pt">Footnote.</span></p>'
            "</body></html></TEXT></DOCUMENT>"
        )
        self.assertEqual(out["sections"][0]["items"], ["1"])
        self.assertEqual(out["sections"][0]["text"], "Item 1. Business\n\nReference\n\n1 Footnote.")

    def test_document_toc_prevents_splitting_every_styled_fragment(self):
        html = "<table>"
        for page, title in enumerate(["Our business", "Risk factors", "Financial statements"], 1):
            html += f'<tr><td><a href="#p{page}">{page}</a></td><td>{title}</td></tr>'
        html += "</table>"
        for title in ["Our business", "Styled pull quote", "Risk factors", "Financial statements"]:
            html += f"<h2>{title}</h2><p>Supporting text.</p>"
        out = parse(html, form="20-F")
        self.assertEqual(
            [s["title"] for s in out["sections"]],
            ["Front matter", "Our business", "Risk factors", "Financial statements"],
        )
        self.assertIn("Styled pull quote", out["sections"][1]["text"])

    def test_tables_keep_source_rows_merged_columns_and_numeric_signs(self):
        blocks, _ = text_blocks(b"""<table>
        <tr><th rowspan="2">Metric</th><th colspan="2">2025</th></tr>
        <tr><th>Q1</th><th>Q2</th></tr>
        <tr><td><p>Opening</p>balance</td><td>100</td><td>120</td></tr>
        <tr><td>Net income</td><td>(5)</td><td>10%</td></tr></table>""")
        self.assertEqual(blocks[0]["kind"], "table")
        self.assertEqual(
            blocks[0]["text"].splitlines(),
            ["Metric\t2025\t", "\tQ1\tQ2", "Opening balance\t100\t120", "Net income\t(5)\t10%"],
        )

    def test_table_caption_units_and_inline_references_are_preserved(self):
        blocks, _ = text_blocks(
            b"<table><caption>Revenue in USD millions</caption>"
            b"<tr><td>Year</td><td>Revenue</td></tr>"
            b"<tr><td>2025</td><td>123</td></tr></table>"
        )
        self.assertEqual(blocks[0]["text"], "Revenue in USD millions")
        out = parse(
            "<p>Item 9.01 Financial Statements and Exhibits</p>"
            "<p>Item 9.01 of the Original Report is amended as follows.</p>",
            form="8-K",
        )
        self.assertEqual(len(out["sections"]), 1)

    def test_nested_layout_tables_do_not_duplicate_or_lose_inner_text(self):
        blocks, _ = text_blocks(b"""<table><tr><td>Before
        <table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>
        After</td></tr></table>""")
        self.assertEqual([b["text"] for b in blocks], ["Before", "A\tB\n1\t2", "After"])

    def test_image_only_document_is_explicitly_flagged_without_invented_text(self):
        result = parse('<html><body><img src="presentation.jpg"></body></html>', role="exhibit")
        self.assertEqual(result["status"], "no_extractable_text")
        self.assertEqual(result["sections"], [])
        self.assertIn("image_only_document; no_extractable_text", result["warnings"])
        with self.assertRaisesRegex(ValueError, "no visible text"):
            parse("<html><body></body></html>")

    def test_quarterly_parts_and_toc_are_distinct(self):
        out = parse(
            """<table><tr><td><a href="#a">Item 1.</a></td><td>Financial Statements</td></tr>
        <tr><td><a href="#b">Item 2.</a></td><td>Management's Discussion</td></tr></table>
        <div>PART I. FINANCIAL INFORMATION</div><div id="a">Item 1.</div>
        <div>Financial Statements</div><p>Quarterly figures.</p>
        <div id="b">Item 2. Management's Discussion and Analysis</div><p>Revenue rose.</p>
        <div>PART II. OTHER INFORMATION</div><div>Item 1. Legal Proceedings</div>
        <p>No proceedings.</p><div>Item 1A. Risk Factors</div><p>See Item 1 for discussion.</p>""",
            form="10-Q",
        )
        items = [(s["part"], s["items"]) for s in out["sections"] if s["kind"] == "sec_item"]
        self.assertEqual(items, [("I", ["1"]), ("I", ["2"]), ("II", ["1"]), ("II", ["1A"])])
        self.assertEqual(out["sections"][0]["kind"], "front_matter")
        self.assertIn(
            "Quarterly figures.", next(s["text"] for s in out["sections"] if s["items"] == ["1"])
        )

    def test_form_specific_titles_and_combined_current_report_items(self):
        annual = parse(
            "<p>ITEM 1.IDENTITY OF DIRECTORS, SENIOR MANAGEMENT AND ADVISERS</p>"
            "<p>Not applicable.</p><p>Item 2. Approve AGM resolution</p>",
            form="20-F",
        )
        self.assertEqual([s["items"] for s in annual["sections"]], [["1"]])
        current = parse(
            "<div>Items 2.02 and 9.01 Results of Operations and Financial Condition</div>"
            "<p>Results are attached.</p>",
            form="8-K",
        )
        self.assertEqual(current["sections"][0]["items"], ["2.02", "9.01"])
        quarterly = parse(
            "<p>PART I</p><p>ITEM 1. CONDENSED CONSOLIDATED FINANCIAL STATEMENTS</p>"
            "<p>Figures.</p>",
            form="10-Q",
        )
        self.assertEqual(quarterly["sections"][-1]["items"], ["1"])
        annual = parse(
            "<p>Item 6. (Reserved)</p><p>Item 15. Exhibit and Financial Statement Schedules</p>"
        )
        self.assertEqual([s["items"] for s in annual["sections"]], [["6"], ["15"]])

    def test_fallback_preserves_cross_references_and_content_after_signatures(self):
        intel = parse(
            "<h2>Financial Statements</h2><p>Financial content.</p>"
            "<table><tr><td>Item 1</td><td>Financial Statements</td><td>3–30</td></tr>"
            "<tr><td>Item 2</td><td>Management's Discussion</td><td>33–44</td></tr></table>",
            form="10-Q",
        )
        self.assertTrue(any("no_reliable_sec_item_outline" in w for w in intel["warnings"]))
        self.assertFalse(any(s["kind"] == "sec_item" for s in intel["sections"]))
        foreign = parse(
            "<h2>SIGNATURES</h2><p>Signed.</p><h2>Monthly Shareholdings</h2><p>Shares: 123.</p>",
            form="6-K",
        )
        self.assertIn("Shares: 123.", foreign["sections"][-1]["text"])
        exhibit = parse("<h2>Item 1. Business</h2><p>Exhibit content.</p>", role="exhibit")
        self.assertFalse(any(s["kind"] == "sec_item" for s in exhibit["sections"]))

    def test_chunks_keep_caption_units_and_never_truncate(self):
        blocks = [
            {"kind": "text", "text": "A" * 250},
            {"kind": "text", "text": "Statements of income"},
            {"kind": "text", "text": "(USD millions)"},
            {"kind": "table", "text": "Year\tRevenue\n2025\t" + "1" * 350},
        ]
        chunks = chunk_section(blocks, max_chars=300)
        self.assertEqual(
            "\n\n".join(c["text"] for c in chunks), "\n\n".join(b["text"] for b in blocks)
        )
        self.assertIn("(USD millions)", chunks[-1]["text"])
        self.assertTrue(chunks[-1]["contains_table"])

    def test_corpus_preserves_originals_and_reports_failed_runs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source_root, output = root / "sec", root / "processed"
            folder = source_root / SOURCE["ticker"] / SOURCE["accession"]
            (folder / "raw").mkdir(parents=True)
            raw = folder / "raw/report.htm"
            payload = b"<p>Item 1. Business</p><p>NVIDIA makes processors.</p>"
            raw.write_bytes(payload)
            doc = {k: SOURCE[k] for k in ("filename", "role")}
            doc.update(
                raw_path=str(raw.relative_to(source_root)),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            metadata = {
                "company": {"ticker": "NVDA"},
                "accession": SOURCE["accession"],
                "form": "10-K",
                "filing_date": "2026-02-25",
                "report_date": "2026-01-25",
                "accepted_at": "2026-02-25",
                "documents": [doc],
            }
            metadata_path = folder / "metadata.json"
            original_metadata = json.dumps(metadata).encode()
            metadata_path.write_bytes(original_metadata)
            result = process_corpus(source_root, output)
            self.assertTrue(result["complete"])
            export = json.loads((output / result["documents"][0]["output_path"]).read_text())
            self.assertEqual(export["sections"][0]["items"], ["1"])
            self.assertEqual(raw.read_bytes(), payload)
            self.assertEqual(metadata_path.read_bytes(), original_metadata)
            with self.assertRaisesRegex(ValueError, "separate"):
                process_corpus(source_root, source_root / "derived")
            raw.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                process_corpus(source_root, output)
            self.assertFalse(json.loads((output / "manifest.json").read_text())["complete"])
            metadata["documents"][0]["raw_path"] = "../outside.htm"
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "Unexpected original"):
                process_corpus(source_root, output)


if __name__ == "__main__":
    unittest.main()
