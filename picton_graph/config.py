"""Project paths shared by the local research pipeline and Docker services."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY = ROOT / "ontology"
SCHEMA_PATH = ONTOLOGY / "schema.json"
PROMPT_PATH = ROOT / "prompts" / "extraction.md"
GOLD_PATH = ROOT / "benchmarks" / "gold"
COMPANIES_PATH = ROOT / "docs" / "companies.md"
