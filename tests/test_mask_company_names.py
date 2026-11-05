import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

import tests.test_store_processed_duckdb as storage_tests
from picton_graph.ingestion.masking import ALIASES, TICKERS, company_rules, mask_chunks, mask_text
from picton_graph.ingestion.storage import SCHEMA, store_export

COMPANIES = [
    {"ticker": ticker, "iri": f"urn:company:{ticker}", "name": ALIASES[ticker][0]}
    for ticker in TICKERS
]


class MaskingTests(unittest.TestCase):
    def setUp(self):
        self.rules, self.matcher = company_rules(list(reversed(COMPANIES)))

    def mask(self, text):
        return mask_text(text, self.matcher, self.rules)

    def test_main_names_keep_articles_suffixes_and_exact_surrounding_text(self):
        pairs = {
            "The NVIDIA Corporation": "The Company A Corporation",
            "Nvidia Holdings": "Company A Holdings",
            "NVIDIA Company Blog": "Company A Company Blog",
            "Micron Technology, Inc. and Micron's products": "Company I, Inc. and Company I's products",
            "ASML Holding N.V.": "Company D Holding N.V.",
            "Taiwan Semiconductor Manufacturing Company Limited (TSMC)": "Company J Company Limited (Company J)",
            "Applied Materials and AMD buy from Intel.": "Company B and Company C buy from Company F.",
            "Broadcom, KLA–Tencor, Lam Research": "Company E, Company G, Company H",
            "NVIDIA\n\nsupplies\tMicron Technology.": "Company A\n\nsupplies\tCompany I.",
        }
        for original, expected in pairs.items():
            with self.subTest(original=original):
                masked, spans = self.mask(original)
                self.assertEqual(masked, expected)
                self.assert_restores(original, masked, spans)
        self.assertEqual(
            {r["ticker"]: r["masked_name"] for r in self.rules},
            {ticker: f"Company {chr(65 + i)}" for i, ticker in enumerate(TICKERS)},
        )

    def assert_restores(self, original, masked, spans):
        restored = masked
        for span in reversed(spans):
            self.assertEqual(masked[span["masked_start"] : span["masked_end"]], span["replacement"])
            restored = (
                restored[: span["masked_start"]]
                + original[span["start"] : span["end"]]
                + restored[span["masked_end"] :]
            )
        self.assertEqual(restored, original)

    def test_other_companies_words_measurements_and_legal_suffixes_are_unchanged(self):
        text = (
            "Samsung Microsoft Arm Amdocs Unimicron intelligent AMD64 mu "
            "0.25 micron sub-micron sub‑micron 0.13‑micron 0.25\tmicron half micron. micron\tA metric unit of length. "
            "LYB Finance Company B.V. a company I’ve long admired."
        )
        self.assertEqual(self.mask(text), (text, []))
        self.assertEqual(
            self.mask("micron.com Micron MICRON MU")[0],
            "Company I.com Company I Company I Company I",
        )
        with self.assertRaisesRegex(ValueError, "reserved"):
            self.mask("Company A is an unidentified customer.")

    def test_technical_names_and_aliases_keep_unmatched_characters(self):
        examples = {
            "https://nvidianews.nvidia.com/": "https://Company Anews.Company A.com/",
            "NVDA2027Q2.HTM": "Company A2027Q2.HTM",
            "sbeoainvidia-portsrelease.htm": "sbeoaiCompany A-portsrelease.htm",
            "nvda, amat, avgo, intc, klac, lrcx, tsm": "Company A, Company B, Company E, Company F, Company G, Company H, Company J",
            "Advanced\tMicro Devices KLA Tencor": "Company C Company G",
        }
        for original, expected in examples.items():
            masked, spans = self.mask(original)
            self.assertEqual(masked, expected)
            self.assert_restores(original, masked, spans)

    def test_exact_graph_basket_is_required(self):
        for companies in (
            COMPANIES[:-1],
            COMPANIES + [COMPANIES[0]],
            [{**c, "iri": ""} if c["ticker"] == "NVDA" else c for c in COMPANIES],
        ):
            with self.subTest(companies=companies), self.assertRaises(ValueError):
                company_rules(companies)

    def test_database_masks_and_import_invalidation_preserve_originals(self):
        fixture = storage_tests.StorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.documents[0] = fixture.document(
            "first.htm",
            "<h2>NVIDIA Holdings</h2><p>The NVIDIA Corporation buys from Micron.</p>",
            "exhibit",
        )
        fixture.write_exports(fixture.documents)
        store_export(fixture.exports, fixture.database)
        with duckdb.connect(str(fixture.database), read_only=True) as db:
            before = db.execute("SELECT chunk_id,text FROM chunks ORDER BY chunk_id").fetchall()
        counts = mask_chunks(fixture.database, COMPANIES)
        self.assertEqual(counts, mask_chunks(fixture.database, list(reversed(COMPANIES))))
        with duckdb.connect(str(fixture.database), read_only=True) as db:
            self.assertEqual(
                db.execute("SELECT chunk_id,text FROM chunks ORDER BY chunk_id").fetchall(), before
            )
            rows = db.execute("SELECT text,masked_text,mask_spans FROM chunks").fetchall()
            self.assertTrue(
                any("The Company A Corporation buys from Company I." in row[1] for row in rows)
            )
            for original, masked, spans in rows:
                self.assert_restores(original, masked, json.loads(spans))
            self.assertEqual(
                db.execute(
                    "SELECT masked_title FROM sections WHERE title='NVIDIA Holdings'"
                ).fetchone()[0],
                "Company A Holdings",
            )
            self.assertEqual(db.execute("SELECT count(*) FROM company_masks").fetchone()[0], 10)
        fixture.write_exports([fixture.documents[0]])
        store_export(fixture.exports, fixture.database)
        with duckdb.connect(str(fixture.database), read_only=True) as db:
            self.assertEqual(
                db.execute(
                    "SELECT masked_text,mask_spans FROM chunks WHERE document_id=?",
                    [fixture.documents[0]["document_id"]],
                ).fetchone(),
                (None, None),
            )
            self.assertEqual(
                db.execute(
                    "SELECT masked_title,title_mask_spans FROM sections WHERE document_id=?",
                    [fixture.documents[0]["document_id"]],
                ).fetchone(),
                (None, None),
            )
            self.assertEqual(
                db.execute(
                    "SELECT masked_text FROM chunks WHERE document_id=?",
                    [fixture.documents[1]["document_id"]],
                ).fetchone()[0],
                fixture.documents[1]["sections"][0]["chunks"][0]["text"],
            )
        mask_chunks(fixture.database, COMPANIES)

    def test_failed_update_rolls_back_mapping_schema_and_text(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "filings.duckdb"
            with duckdb.connect(str(database)) as db:
                db.execute(
                    SCHEMA.replace(
                        "UNIQUE (section_id, chunk_index)",
                        "masked_text VARCHAR CHECK (masked_text='unchanged'), UNIQUE (section_id, chunk_index)",
                    )
                )
                db.execute("INSERT INTO chunks VALUES ('c','d','s',0,'NVIDIA',6,false,false,NULL)")
            with self.assertRaises(duckdb.ConstraintException):
                mask_chunks(database, COMPANIES)
            with duckdb.connect(str(database), read_only=True) as db:
                self.assertEqual(
                    db.execute("SELECT text,masked_text FROM chunks").fetchone(), ("NVIDIA", None)
                )
                self.assertNotIn(("company_masks",), db.execute("SHOW TABLES").fetchall())
                self.assertNotIn(
                    "mask_spans",
                    [row[1] for row in db.execute("PRAGMA table_info('chunks')").fetchall()],
                )

    def test_conflicting_persisted_labels_are_rejected(self):
        fixture = storage_tests.StorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store_export(fixture.exports, fixture.database)
        mask_chunks(fixture.database, COMPANIES)
        with duckdb.connect(str(fixture.database)) as db:
            db.execute("UPDATE company_masks SET masked_name='Company Z' WHERE ticker='NVDA'")
            before = db.execute(
                "SELECT chunk_id,masked_text FROM chunks ORDER BY chunk_id"
            ).fetchall()
        with self.assertRaisesRegex(ValueError, "conflict"):
            mask_chunks(fixture.database, COMPANIES)
        with duckdb.connect(str(fixture.database), read_only=True) as db:
            self.assertEqual(
                db.execute("SELECT chunk_id,masked_text FROM chunks ORDER BY chunk_id").fetchall(),
                before,
            )
            self.assertEqual(
                db.execute("SELECT masked_name FROM company_masks WHERE ticker='NVDA'").fetchone()[
                    0
                ],
                "Company Z",
            )


if __name__ == "__main__":
    unittest.main()
