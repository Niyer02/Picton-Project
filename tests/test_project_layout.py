"""Guard the resource paths and command entry points changed by the package move."""

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from picton_graph.config import COMPANIES_PATH, GOLD_PATH, ONTOLOGY, PROMPT_PATH, ROOT, SCHEMA_PATH
from picton_graph.extraction.extractor import load_ontology
from picton_graph.graph.ontology import parse_ontology
from picton_graph.mcp.server import stock_chart


class ProjectLayoutTests(unittest.TestCase):
    def test_moved_resources_resolve_to_the_same_project(self):
        for path in (
            COMPANIES_PATH,
            GOLD_PATH / "manifest.json",
            ONTOLOGY / "picton.owl",
            PROMPT_PATH,
            SCHEMA_PATH,
        ):
            self.assertTrue(path.is_file(), path)
            self.assertTrue(path.is_relative_to(ROOT), path)
        self.assertEqual(
            set(load_ontology()["classes"]), set(parse_ontology(ONTOLOGY / "picton.owl")["classes"])
        )
        self.assertIn("<script>", stock_chart())

    def test_module_help_is_safe_from_another_working_directory(self):
        modules = (
            "ingestion.sec",
            "ingestion.html",
            "ingestion.storage",
            "ingestion.masking",
            "extraction.extractor",
            "extraction.corpus",
            "extraction.export",
            "evaluation.optimize",
            "graph.ontology",
            "graph.importer",
            "mcp.server",
        )
        with TemporaryDirectory() as directory:
            for module in modules:
                with self.subTest(module=module):
                    result = subprocess.run(
                        [sys.executable, "-m", "picton_graph." + module, "--help"],
                        cwd=directory,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout)
                    self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
