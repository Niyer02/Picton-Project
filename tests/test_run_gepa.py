import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import picton_graph.evaluation.optimize as runner
from tests.test_extract_relationships import MAPPING


class GepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = [
            r for split in ("train", "validation", "holdout") for r in runner.load_split(split)
        ]
        cls.by_text = {r["input"]["chunk_text"]: r for r in cls.rows}

    def fake_provider(self, request, key):
        self.assertEqual(key, "fake-key")
        self.assertEqual(request["model"], runner.MODEL)
        if "response_format" in request:
            context = json.loads(request["messages"][1]["content"])
            content = json.dumps(self.by_text[context["chunk_text"]]["expected_output"])
        else:
            content = (
                "```\nExtract only evidenced ontology facts using the provided JSON schema.\n```"
            )
        return {
            "id": "mock",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": 0.001,
            },
            "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        }

    def test_actual_gepa_three_epochs_with_mocked_provider_and_holdout_separation(self):
        with (
            TemporaryDirectory() as tmp,
            patch.object(runner, "api_key", return_value="fake-key"),
            patch.object(runner, "verify_examples", return_value=MAPPING),
            patch.object(runner, "call_openrouter", side_effect=self.fake_provider),
            redirect_stdout(StringIO()),
        ):
            directory = runner.run(epochs=3, output_root=Path(tmp), workers=3)
            manifest = json.loads((directory / "manifest.json").read_text())
            results = json.loads((directory / "results.json").read_text())
            rounds = json.loads((directory / "rounds.json").read_text())
            proposals = json.loads((directory / "proposals.json").read_text())
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["output_format"], runner.OUTPUT_FORMAT)
            self.assertEqual(len(rounds), 12)
            self.assertEqual([p["round"] for p in proposals if p["usable"]], list(range(1, 13)))
            for epoch in (1, 2, 3):
                visited = [i for r in rounds if r["epoch"] == epoch for i in r["training_indices"]]
                self.assertEqual(sorted(visited), list(range(12)))
            self.assertEqual(results["epochs_completed"], 3)
            self.assertEqual(results["baseline"]["holdout"]["mean_score"], 1)
            self.assertEqual(results["optimized"]["holdout"]["mean_score"], 1)
            holdout_text = [r["input"]["chunk_text"] for r in self.rows if r["split"] == "holdout"]
            for path in (directory / "calls").glob("reflection-*.json"):
                record = json.loads(path.read_text())
                text = record["request"]["messages"][0]["content"]
                for passage in holdout_text:
                    self.assertNotIn(passage, text)
                self.assertNotIn("provenance", text)
                self.assertNotIn("source_url", text)
                self.assertNotIn("original_excerpt", text)
            for path in directory.rglob("*.json"):
                self.assertNotIn("fake-key", path.read_text())

    def test_holdout_gate_and_invalid_extraction_zero_score(self):
        with TemporaryDirectory() as tmp:
            client = runner.LoggedClient(Path(tmp) / "calls", "fake-key")
            adapter = runner.ExtractionAdapter(
                client, Path(tmp) / "evaluations", runner.load_ontology(), MAPPING
            )
            held = next(r for r in self.rows if r["split"] == "holdout")
            with patch.object(runner, "call_openrouter") as provider:
                with self.assertRaisesRegex(ValueError, "Holdout is locked"):
                    adapter.evaluate_one(held, "test")
                provider.assert_not_called()
            bad = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
            with patch.object(runner, "call_openrouter", return_value=bad) as provider:
                result = adapter.evaluate_one(self.rows[0], "test")
                self.assertFalse(result["valid"])
                self.assertEqual(result["score"], 0)
                self.assertIn("validation failed", result["feedback"])
                self.assertEqual(adapter.evaluate_one(self.rows[0], "test"), result)
                self.assertEqual(provider.call_count, 1)

    def test_old_gold_format_stops_before_provider_calls_or_artifacts(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(json.dumps({"dataset_version": "1.0-draft"}))
            with (
                patch.object(runner, "GOLD", root),
                patch.object(runner, "api_key", return_value="fake-key"),
                patch.object(runner, "call_openrouter") as provider,
            ):
                with self.assertRaisesRegex(ValueError, "Gold output format"):
                    runner.run(output_root=root / "output")
                provider.assert_not_called()
            self.assertFalse((root / "output").exists())

    def test_swallowed_reflection_failure_marks_run_failed_before_holdout(self):
        seen = []

        def truncated_reflection(request, key):
            if "response_format" not in request:
                return {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
            text = json.loads(request["messages"][1]["content"])["chunk_text"]
            seen.append(self.by_text[text]["split"])
            return self.fake_provider(request, key)

        with (
            TemporaryDirectory() as tmp,
            patch.object(runner, "api_key", return_value="fake-key"),
            patch.object(runner, "verify_examples", return_value=MAPPING),
            patch.object(runner, "call_openrouter", side_effect=truncated_reflection),
            redirect_stdout(StringIO()),
        ):
            with self.assertRaisesRegex(RuntimeError, "reflections failed"):
                runner.run(epochs=1, output_root=Path(tmp))
            directory = next(Path(tmp).iterdir())
            self.assertEqual(
                json.loads((directory / "manifest.json").read_text())["status"], "failed"
            )
            self.assertNotIn("holdout", seen)
            self.assertFalse((directory / "optimized_prompt.md").exists())

    def test_operational_failure_is_not_scored_as_bad_extraction_or_retried(self):
        with TemporaryDirectory() as tmp:
            client = runner.LoggedClient(tmp, "fake-key")
            with patch.object(
                runner, "call_openrouter", side_effect=RuntimeError("HTTP 401")
            ) as provider:
                with self.assertRaises(RuntimeError):
                    client("reflection", {"model": runner.MODEL})
                with self.assertRaisesRegex(RuntimeError, "prior request failed"):
                    client("reflection", {"model": runner.MODEL})
                self.assertEqual(provider.call_count, 1)

    def test_reflection_refuses_validation_and_holdout_examples(self):
        with TemporaryDirectory() as tmp:
            adapter = runner.ExtractionAdapter(None, tmp, runner.load_ontology(), MAPPING)
            for split in ("validation", "holdout"):
                row = next(r for r in self.rows if r["split"] == split)
                batch = runner.EvaluationBatch(outputs=[{}], scores=[0], trajectories=[row])
                with self.assertRaisesRegex(ValueError, "Only training"):
                    adapter.make_reflective_dataset({}, batch, ["system_prompt"])


if __name__ == "__main__":
    unittest.main()
