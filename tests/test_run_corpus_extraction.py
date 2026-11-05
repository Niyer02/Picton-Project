import fcntl
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import picton_graph.extraction.corpus as corpus
from tests.test_extract_relationships import MAPPING, response, sample_chunk, sample_result


class CorpusRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text(
            "Extract only explicit facts with the agreed named-reference schema."
        )
        self.database = self.root / "unused.duckdb"
        self.config = {
            "model": corpus.MODEL,
            "output_format": corpus.OUTPUT_FORMAT,
            "workers": 8,
            "max_tokens": 8192,
        }
        self.output = StringIO()
        capture = redirect_stdout(self.output)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        key = patch.object(corpus, "api_key", return_value="private-test-key")
        key.start()
        self.addCleanup(key.stop)

    def chunks(self, count):
        chunks = []
        for index in range(count):
            chunk = sample_chunk(sample_chunk()["text"] + f"\nCase {index}")
            chunk.update(
                chunk_id=f"NVDA/filing/file.htm#section-{index:04d}-chunk-0000",
                section_id=f"section-{index:04d}",
                ticker="NVDA",
                source_sha256="a" * 64,
            )
            chunks.append(chunk)
        return chunks

    def prepare(self, count, workers=8):
        directory = self.root / "prepared"
        directory.mkdir()
        (directory / "results").mkdir()
        config = {**self.config, "workers": workers}
        chunks = self.chunks(count)
        with patch.object(corpus, "select_chunks", return_value=(chunks, MAPPING)):
            manifest, ontology, prompt, mapping = corpus.snapshot(
                directory, self.database, self.prompt, config
            )
        return directory, manifest, chunks, ontology, prompt, mapping

    def checkpoint(
        self, directory, index, chunk, ontology, prompt, status="pending", with_response=False
    ):
        request = corpus.build_request(chunk, ontology, prompt)
        record = corpus.base_record(index, chunk, request)
        record["status"] = status
        if with_response:
            record["response"] = response(sample_result())
        corpus.save_json(corpus.result_path(directory, index), record)
        return record

    def load_manifest(self, directory):
        return json.loads((directory / "manifest.json").read_text())

    def test_thirty_two_thread_bound_every_selected_chunk_once_and_usage(self):
        active = maximum = 0
        calls = []
        lock = threading.Lock()
        barrier = threading.Barrier(32)

        def provider(request, key):
            nonlocal active, maximum
            index = int(
                json.loads(request["messages"][1]["content"])["chunk_text"].rsplit("Case ", 1)[1]
            )
            self.assertEqual(key, "private-test-key")
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls.append(index)
            try:
                if index < 32:
                    barrier.wait(timeout=5)
                time.sleep(0.002)
                result = response(sample_result())
                result["usage"] = {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                    "cost": 0.001,
                }
                return result
            finally:
                with lock:
                    active -= 1

        with (
            patch.object(
                corpus, "select_chunks", return_value=(self.chunks(48), MAPPING)
            ) as select,
            patch.object(corpus, "call_openrouter", side_effect=provider),
        ):
            directory = corpus.run(
                self.database, self.root / "runs", workers=32, prompt_path=self.prompt
            )
        select.assert_called_once_with(self.database, None, None, role="all")
        self.assertEqual(maximum, 32)
        self.assertEqual(sorted(calls), list(range(48)))
        manifest = self.load_manifest(directory)
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(manifest["counts"]["valid"], 48)
        self.assertEqual(manifest["counts"]["not_started"], 0)
        self.assertEqual(manifest["usage"]["total_tokens"], 48 * 15)
        self.assertAlmostEqual(manifest["usage"]["reported_cost_usd"], 0.048)
        for path in (directory / "results").glob("*.json"):
            record = json.loads(path.read_text())
            self.assertIn("validated", record)
            self.assertIn("response", record)
            self.assertIn("source_sha256", record)
            self.assertNotIn("private-test-key", path.read_text())
        self.assertIn("Corpus run directory:", self.output.getvalue())

    def test_mixed_errors_continue_and_preserve_raw_responses(self):
        def provider(request, key):
            index = int(
                json.loads(request["messages"][1]["content"])["chunk_text"].rsplit("Case ", 1)[1]
            )
            if index == 1:
                return response(finish="length")
            if index == 2:
                raise RuntimeError("private-test-key must not leak")
            if index == 3:
                return {"error": {"code": 429, "message": "Rate limited"}}
            if index == 4:
                return response({"entities": [], "relationships": [], "unexpected": True})
            return response(sample_result())

        with (
            patch.object(corpus, "select_chunks", return_value=(self.chunks(6), MAPPING)),
            patch.object(corpus, "call_openrouter", side_effect=provider) as send,
        ):
            directory = corpus.run(self.database, self.root / "runs", prompt_path=self.prompt)
        self.assertEqual(send.call_count, 6)
        manifest = self.load_manifest(directory)
        self.assertEqual(manifest["status"], "complete_with_errors")
        self.assertEqual(
            {s: manifest["counts"][s] for s in corpus.TERMINAL},
            {"valid": 2, "rejected": 2, "operational_error": 2, "uncertain": 0},
        )
        self.assertEqual(manifest["usage"]["requests_without_response"], 1)
        self.assertEqual(manifest["usage"]["requests_without_reported_cost"], 6)
        records = [
            json.loads(p.read_text()) for p in sorted((directory / "results").glob("*.json"))
        ]
        self.assertIn("response", records[1])
        self.assertEqual(records[2]["error"], "RuntimeError")
        self.assertIn("error", records[3]["response"])

    def test_local_revalidation_clears_stale_validation_outcomes(self):
        directory, _, chunks, ontology, prompt, mapping = self.prepare(1)
        record = self.checkpoint(
            directory, 1, chunks[0], ontology, prompt, "rejected", with_response=True
        )
        record["error"] = "old validation error"
        with patch.object(corpus, "call_openrouter") as send:
            corpus.finalize(record, chunks[0], mapping, ontology)
            self.assertEqual(record["status"], "valid")
            self.assertNotIn("error", record)
            record["response"] = response(finish="length")
            corpus.finalize(record, chunks[0], mapping, ontology)
            self.assertEqual(record["status"], "rejected")
            self.assertNotIn("validated", record)
            send.assert_not_called()

    def test_resume_recovers_response_skips_uncertain_and_uses_frozen_source(self):
        directory, _, chunks, ontology, prompt, mapping = self.prepare(3)
        self.checkpoint(
            directory, 1, chunks[0], ontology, prompt, "response_saved", with_response=True
        )
        self.checkpoint(directory, 2, chunks[1], ontology, prompt)
        self.prompt.write_text("The original prompt can change; resume uses its frozen copy.")
        with (
            patch.object(
                corpus, "select_chunks", side_effect=AssertionError("Do not reopen changed source")
            ),
            patch.object(corpus, "call_openrouter", return_value=response(sample_result())) as send,
        ):
            corpus.run(database=self.root / "changed-or-deleted.duckdb", resume=directory)
            self.assertEqual(send.call_count, 1)
            self.assertIn("Case 2", send.call_args.args[0]["messages"][1]["content"])
            corpus.run(resume=directory)
            self.assertEqual(send.call_count, 1)
        manifest = self.load_manifest(directory)
        self.assertEqual(manifest["status"], "complete_with_errors")
        self.assertEqual(manifest["counts"]["valid"], 2)
        self.assertEqual(manifest["counts"]["uncertain"], 1)
        self.assertEqual(manifest["processed"], 3)

    def test_resume_rejects_config_prompt_schema_and_input_changes_before_calls(self):
        directory, _, _, _, _, _ = self.prepare(1)
        alternate_prompt = self.root / "different.md"
        alternate_prompt.write_text("changed")
        with patch.object(corpus, "call_openrouter") as send:
            for kwargs in (
                {"workers": 33},
                {"max_tokens": 1024},
                {"prompt_path": alternate_prompt},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    corpus.run(resume=directory, **kwargs)
            for filename in (
                "inputs.jsonl",
                "schema.json",
                "code/picton_graph/extraction/extractor.py",
            ):
                path = directory / filename
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with (
                    self.subTest(filename=filename),
                    self.assertRaisesRegex(ValueError, "Frozen run file changed"),
                ):
                    corpus.run(resume=directory)
                path.write_bytes(original)
            send.assert_not_called()

    def test_resume_changes_workers_without_resending_or_rewriting_saved_results(self):
        directory, _, chunks, ontology, prompt, mapping = self.prepare(3, workers=16)
        record = self.checkpoint(directory, 1, chunks[0], ontology, prompt, with_response=True)
        corpus.save_json(
            corpus.result_path(directory, 1), corpus.finalize(record, chunks[0], mapping, ontology)
        )
        original = corpus.result_path(directory, 1).read_bytes()
        inputs_hash = corpus.file_hash(directory / "inputs.jsonl")
        with (
            patch.object(
                corpus, "select_chunks", side_effect=AssertionError("Use the frozen corpus")
            ),
            patch.object(corpus, "call_openrouter", return_value=response(sample_result())) as send,
        ):
            corpus.run(resume=directory, workers=32)
            self.assertEqual(send.call_count, 2)
            cases = [
                json.loads(call.args[0]["messages"][1]["content"])["chunk_text"].rsplit("Case ", 1)[
                    1
                ]
                for call in send.call_args_list
            ]
            self.assertCountEqual(cases, ["1", "2"])
            corpus.run(resume=directory, workers=32)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(corpus.result_path(directory, 1).read_bytes(), original)
        self.assertEqual(corpus.file_hash(directory / "inputs.jsonl"), inputs_hash)
        manifest = self.load_manifest(directory)
        self.assertEqual(manifest["config"]["workers"], 32)
        self.assertEqual(len(manifest["worker_changes"]), 1)
        self.assertEqual(manifest["worker_changes"][0]["previous"], 16)
        self.assertEqual(manifest["worker_changes"][0]["current"], 32)
        self.assertEqual(manifest["counts"]["valid"], 3)

    def test_legacy_run_is_rejected_without_changing_its_manifest_or_calling_provider(self):
        directory, manifest, _, _, _, _ = self.prepare(1)
        manifest["run_version"] = 1
        corpus.save_json(directory / "manifest.json", manifest)
        before = (directory / "manifest.json").read_bytes()
        with patch.object(corpus, "call_openrouter") as send:
            with self.assertRaisesRegex(ValueError, "frozen original code"):
                corpus.run(resume=directory)
            send.assert_not_called()
        self.assertEqual((directory / "manifest.json").read_bytes(), before)

    def test_moved_shared_dependencies_are_frozen_and_runtime_changes_block_resume(self):
        directory, manifest, _, _, _, _ = self.prepare(1)
        dependency = "picton_graph/files.py"
        self.assertIn(f"code/{dependency}", manifest["files"])
        original_hash = corpus.file_hash
        with patch.object(
            corpus,
            "file_hash",
            side_effect=lambda path: (
                "changed" if path == corpus.ROOT / dependency else original_hash(path)
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Runtime code changed"):
                corpus.verify_resume(directory, self.config, None)

    def test_mismatched_result_or_noncanonical_filename_blocks_all_dispatch(self):
        directory, _, chunks, ontology, prompt, _ = self.prepare(2)
        record = self.checkpoint(directory, 1, chunks[0], ontology, prompt)
        bad = deepcopy(record)
        bad["request"]["messages"][1]["content"] = "changed source"
        bad["request_sha256"] = corpus.value_hash(bad["request"])
        corpus.save_json(corpus.result_path(directory, 1), bad)
        with patch.object(corpus, "call_openrouter") as send:
            with self.assertRaisesRegex(ValueError, "source/request"):
                corpus.run(resume=directory)
            corpus.save_json(corpus.result_path(directory, 1), record)
            corpus.result_path(directory, 1).rename(directory / "results" / "1.json")
            with self.assertRaisesRegex(ValueError, "unknown input number"):
                corpus.run(resume=directory)
            send.assert_not_called()

    def test_eight_operational_failures_stop_dispatch_and_keep_inflight_results(self):
        with (
            patch.object(corpus, "select_chunks", return_value=(self.chunks(80), MAPPING)),
            patch.object(
                corpus,
                "call_openrouter",
                side_effect=RuntimeError("OpenRouter HTTP 401; no automatic retry was made."),
            ) as send,
        ):
            directory = corpus.run(
                self.database, self.root / "runs", workers=32, prompt_path=self.prompt
            )
        manifest = self.load_manifest(directory)
        self.assertEqual(manifest["status"], "stopped")
        self.assertGreaterEqual(send.call_count, 8)
        self.assertLessEqual(send.call_count, 39)
        self.assertEqual(manifest["counts"]["operational_error"], send.call_count)
        self.assertEqual(manifest["counts"]["not_started"], 80 - send.call_count)
        self.assertEqual(manifest["counts"]["in_flight"], 0)
        self.assertEqual(len(list((directory / "results").glob("*.json"))), send.call_count)

    def test_schema_rejection_resets_operational_streak(self):
        outcomes = (
            [RuntimeError("network")] * 7
            + [response(finish="length")]
            + [RuntimeError("network")] * 7
            + [response()]
        )
        with (
            patch.object(corpus, "select_chunks", return_value=(self.chunks(16), MAPPING)),
            patch.object(corpus, "call_openrouter", side_effect=outcomes) as send,
        ):
            directory = corpus.run(
                self.database, self.root / "runs", workers=1, prompt_path=self.prompt
            )
        manifest = self.load_manifest(directory)
        self.assertEqual(send.call_count, 16)
        self.assertEqual(manifest["status"], "complete_with_errors")
        self.assertEqual(manifest["counts"]["rejected"], 1)
        self.assertEqual(manifest["counts"]["valid"], 1)

    def test_run_lock_and_missing_completed_checkpoint_prevent_duplicate_calls(self):
        directory, _, _, _, _, _ = self.prepare(1)
        with (
            (directory / ".run.lock").open("a+") as lock,
            patch.object(corpus, "call_openrouter") as send,
        ):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "already active"):
                corpus.run(resume=directory)
            send.assert_not_called()
        manifest = self.load_manifest(directory)
        manifest["processed"] = 1
        corpus.save_json(directory / "manifest.json", manifest)
        with patch.object(corpus, "call_openrouter") as send:
            with self.assertRaisesRegex(ValueError, "checkpoints are missing"):
                corpus.run(resume=directory)
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
