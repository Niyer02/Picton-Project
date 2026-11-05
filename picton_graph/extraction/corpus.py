"""Extract the full masked corpus with bounded concurrency and durable checkpoints."""

import argparse
import fcntl
import json
import math
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4

from picton_graph.config import ROOT, SCHEMA_PATH
from picton_graph.extraction.client import api_key, call_openrouter
from picton_graph.extraction.extractor import (
    EXTRACTION_SOURCES,
    MODEL,
    OUTPUT_FORMAT,
    build_request,
    decode_response,
    load_ontology,
    select_chunks,
    validate_extraction,
)
from picton_graph.files import file_hash, save, save_json, utc_now, value_hash
from picton_graph.ingestion.storage import DATABASE

RUN_VERSION = 2
TERMINAL = ("valid", "rejected", "operational_error", "uncertain")
CODE_FILES = (*EXTRACTION_SOURCES, "picton_graph/extraction/corpus.py")
FROZEN_FILES = (
    "prompt.md",
    "schema.json",
    "company_mapping.json",
    "inputs.jsonl",
    *(f"code/{filename}" for filename in CODE_FILES),
)


def inputs(directory):
    with (directory / "inputs.jsonl").open() as stream:
        for index, line in enumerate(stream, 1):
            yield index, json.loads(line)


def result_path(directory, index):
    return directory / "results" / f"{index:06d}.json"


def base_record(index, chunk, request):
    return {
        "index": index,
        "chunk_id": chunk["chunk_id"],
        "document_id": chunk["document_id"],
        "section_id": chunk["section_id"],
        "source_url": chunk["source_url"],
        "source_sha256": chunk["source_sha256"],
        "filing_date": chunk["filing_date"],
        "input_sha256": value_hash(chunk),
        "request_sha256": value_hash(request),
        "request": request,
    }


def finalize(record, chunk, mapping, ontology):
    record.pop("error", None)
    record.pop("validated", None)
    response = record["response"]
    if isinstance(response, dict) and response.get("error"):
        record.update(
            status="operational_error",
            error="OpenRouter returned an error response; inspect response.",
        )
    else:
        try:
            prediction = decode_response(response)
            record["validated"] = validate_extraction(prediction, chunk, mapping, ontology)
            record["status"] = "valid"
        except ValueError as exc:
            record.update(status="rejected", error=str(exc))
    record["finished_at"] = utc_now()
    return record


def extract_one(path, record, chunk, mapping, ontology, key):
    record.update(status="pending", started_at=utc_now())
    # shortcut: atomic rename covers process restart; add fsync if host/power-loss guarantees are required.
    # Persist intent before the request so a process crash cannot cause a silent retry.
    save_json(path, record)
    try:
        response = call_openrouter(record["request"], key)
    except Exception as exc:
        detail = str(exc)
        safe = (
            detail
            if re.fullmatch(r"OpenRouter HTTP \d{3}; no automatic retry was made\.", detail)
            or detail == "OpenRouter connection failed; no automatic retry was made."
            else type(exc).__name__
        )
        record.update(status="operational_error", error=safe, finished_at=utc_now())
    else:
        record.update(status="response_saved", response=response)
        save_json(path, record)
        finalize(record, chunk, mapping, ontology)
    save_json(path, record)
    return record


def add_usage(usage, record):
    if "response" not in record:
        usage["requests_without_response"] += 1
        usage["requests_without_reported_cost"] += 1
        return
    usage["responses"] += 1
    response = record["response"]
    details = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(details, dict):
        details = {}
    if not details:
        usage["responses_without_usage"] += 1
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = details.get(field)
        if type(value) is int and value >= 0:
            usage[field] += value
        else:
            usage[f"responses_without_{field}"] += 1
    cost = details.get("cost")
    if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
        usage["reported_cost_usd"] += cost
    else:
        usage["requests_without_reported_cost"] += 1


def snapshot(directory, database, prompt_path, config):
    prompt_path = Path(prompt_path)
    prompt = prompt_path.read_text()
    if not prompt.strip():
        raise ValueError("Extraction prompt is empty.")
    ontology = load_ontology()
    chunks, mapping = select_chunks(database, None, None, role="all")
    if not chunks or len({c["chunk_id"] for c in chunks}) != len(chunks):
        raise ValueError("Full-corpus selection must contain unique, non-empty chunks.")
    save(directory / "prompt.md", prompt.encode())
    save(directory / "schema.json", SCHEMA_PATH.read_bytes())
    save_json(directory / "company_mapping.json", mapping)
    for filename in CODE_FILES:
        save(directory / "code" / filename, (ROOT / filename).read_bytes())
    temporary = directory / "inputs.jsonl.tmp"
    with temporary.open("w") as stream:
        for chunk in chunks:
            stream.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    temporary.replace(directory / "inputs.jsonl")
    manifest = {
        "run_version": RUN_VERSION,
        "status": "prepared",
        "created_at": utc_now(),
        "config": config,
        "source_database": str(Path(database).resolve()),
        "prompt_source": str(prompt_path.resolve()),
        "total_chunks": len(chunks),
        "total_documents": len({c["document_id"] for c in chunks}),
        "files": {filename: file_hash(directory / filename) for filename in FROZEN_FILES},
    }
    save_json(directory / "manifest.json", manifest)
    return manifest, ontology, prompt, mapping


def verify_resume(directory, config, prompt_path):
    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest.get("run_version") != RUN_VERSION:
        raise ValueError(
            "Run format differs from this package version; use the run's frozen original code to resume or export it."
        )
    # Concurrency changes scheduling, not the frozen requests or their outputs.
    expected_config = {**config, "workers": manifest.get("config", {}).get("workers")}
    if manifest.get("config") != expected_config:
        raise ValueError("Resume configuration/model/output format differs from the frozen run.")
    if set(manifest.get("files", {})) != set(FROZEN_FILES):
        raise ValueError("Frozen run file manifest is incomplete or unexpected.")
    for filename, expected in manifest["files"].items():
        if file_hash(directory / filename) != expected:
            raise ValueError(f"Frozen run file changed: {filename}")
    for filename in CODE_FILES:
        if file_hash(ROOT / filename) != manifest["files"][f"code/{filename}"]:
            raise ValueError(f"Runtime code changed since this run: {filename}")
    if file_hash(SCHEMA_PATH) != manifest["files"]["schema.json"]:
        raise ValueError("Ontology changed since this run.")
    if prompt_path is not None and file_hash(prompt_path) != manifest["files"]["prompt.md"]:
        raise ValueError("Explicit resume prompt differs from the frozen prompt.")
    return (
        manifest,
        load_ontology(),
        (directory / "prompt.md").read_text(),
        json.loads((directory / "company_mapping.json").read_text()),
    )


def inspect_results(directory, manifest, ontology, prompt, mapping):
    """Check every result binding and recover responses before any new paid call."""
    counts = Counter({s: 0 for s in (*TERMINAL, "not_started", "in_flight")})
    usage, todo = Counter(), set()
    input_count = 0
    for index, chunk in inputs(directory):
        input_count = index
        path = result_path(directory, index)
        if not path.exists():
            counts["not_started"] += 1
            todo.add(index)
            continue
        record = json.loads(path.read_text())
        request = build_request(chunk, ontology, prompt, manifest["config"]["max_tokens"])
        expected = base_record(index, chunk, request)
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError(
                f"Saved result source/request does not match frozen input: {path.name}"
            )
        if record.get("status") in ("pending", "response_saved"):
            if "response" in record:
                finalize(record, chunk, mapping, ontology)
            else:
                record.update(
                    status="uncertain",
                    finished_at=utc_now(),
                    error="Prior request intent has no saved response; not resent to avoid duplicate billing.",
                )
            save_json(path, record)
        if record.get("status") not in TERMINAL:
            raise ValueError(f"Unknown checkpoint status: {path.name}")
        if record["status"] in ("valid", "rejected") and "response" not in record:
            raise ValueError(f"Completed model result has no response: {path.name}")
        counts[record["status"]] += 1
        add_usage(usage, record)
    if input_count != manifest["total_chunks"]:
        raise ValueError("Frozen input count does not match the manifest.")
    if any(
        not p.stem.isdigit()
        or p.name != f"{int(p.stem):06d}.json"
        or not 1 <= int(p.stem) <= input_count
        for p in (directory / "results").glob("*.json")
    ):
        raise ValueError("Result directory contains an unknown input number.")
    if manifest.get("processed", 0) > sum(counts[s] for s in TERMINAL):
        raise ValueError(
            "Previously processed checkpoints are missing; refusing to resend unknown requests."
        )
    return counts, usage, todo


def write_progress(directory, manifest, counts, usage, *, emit=False):
    manifest.update(
        counts=dict(counts),
        processed=sum(counts[s] for s in TERMINAL),
        usage=dict(usage),
        updated_at=utc_now(),
    )
    save_json(directory / "manifest.json", manifest)
    if emit:
        print(
            json.dumps(
                {
                    "run": str(directory),
                    "status": manifest["status"],
                    "processed": manifest["processed"],
                    "total": manifest["total_chunks"],
                    "counts": dict(counts),
                    "usage": dict(usage),
                }
            ),
            flush=True,
        )


def run(
    database=DATABASE,
    output_root=ROOT / "data/corpus_extractions",
    workers=8,
    prompt_path=None,
    resume=None,
    max_tokens=8192,
):
    if (
        type(workers) is not int
        or not 1 <= workers <= 32
        or type(max_tokens) is not int
        or max_tokens < 1
    ):
        raise ValueError("Use 1–32 workers and a positive completion token budget.")
    if resume is None and prompt_path is None:
        raise ValueError("A fresh run requires an explicit --prompt path.")
    config = {
        "model": MODEL,
        "output_format": OUTPUT_FORMAT,
        "workers": workers,
        "max_tokens": max_tokens,
    }
    directory = (
        Path(resume).resolve() if resume is not None else Path(output_root).resolve() / str(uuid4())
    )
    if resume is not None:
        if not directory.is_dir():
            raise ValueError("Resume directory does not exist.")
    else:
        directory.mkdir(parents=True, exist_ok=False)
    print(f"Corpus run directory: {directory}", flush=True)
    with (directory / ".run.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("This run is already active in another process.") from None
        if resume is None:
            save_json(
                directory / "manifest.json",
                {"status": "preparing", "config": config, "created_at": utc_now()},
            )
            try:
                manifest, ontology, prompt, mapping = snapshot(
                    directory, database, prompt_path, config
                )
            except Exception as exc:
                save_json(
                    directory / "manifest.json",
                    {
                        "status": "failed",
                        "phase": "preflight",
                        "error_type": type(exc).__name__,
                        "config": config,
                        "finished_at": utc_now(),
                    },
                )
                raise
        else:
            manifest, ontology, prompt, mapping = verify_resume(directory, config, prompt_path)
        (directory / "results").mkdir(exist_ok=True)
        counts, usage, todo = inspect_results(directory, manifest, ontology, prompt, mapping)
        if manifest["config"]["workers"] != workers:
            manifest.setdefault("worker_changes", []).append(
                {"at": utc_now(), "previous": manifest["config"]["workers"], "current": workers}
            )
        manifest["config"] = config
        manifest.update(status="running", resumed_at=utc_now() if resume else None)
        manifest.pop("stop_reason", None)
        manifest.pop("error_type", None)
        write_progress(directory, manifest, counts, usage, emit=True)
        pending, completed = set(), Queue()
        consecutive_errors = 0
        stopped = False
        last_emit = time.monotonic()
        since_emit = 0
        rows = ((i, c) for i, c in inputs(directory) if i in todo)
        try:
            key = api_key() if todo else None
            with ThreadPoolExecutor(max_workers=workers) as pool:

                def dispatch():
                    while not stopped and len(pending) < workers:
                        try:
                            index, chunk = next(rows)
                        except StopIteration:
                            break
                        request = build_request(chunk, ontology, prompt, max_tokens)
                        future = pool.submit(
                            extract_one,
                            result_path(directory, index),
                            base_record(index, chunk, request),
                            chunk,
                            mapping,
                            ontology,
                            key,
                        )
                        pending.add(future)
                        counts["not_started"] -= 1
                        counts["in_flight"] += 1
                        future.add_done_callback(completed.put)

                dispatch()
                while pending:
                    try:
                        future = completed.get(timeout=1)
                    except Empty:
                        if time.monotonic() - last_emit >= 20:
                            write_progress(directory, manifest, counts, usage, emit=True)
                            last_emit = time.monotonic()
                        continue
                    except KeyboardInterrupt:
                        stopped = True
                        manifest["stop_reason"] = (
                            "interrupted; in-flight requests were allowed to finish"
                        )
                        continue
                    pending.remove(future)
                    record = future.result()
                    counts["in_flight"] -= 1
                    counts[record["status"]] += 1
                    add_usage(usage, record)
                    consecutive_errors = (
                        consecutive_errors + 1 if record["status"] == "operational_error" else 0
                    )
                    if consecutive_errors >= 8:
                        stopped = True
                        manifest["stop_reason"] = (
                            "eight consecutive operational failures; no new requests dispatched"
                        )
                    dispatch()
                    since_emit += 1
                    emit = since_emit >= 25 or time.monotonic() - last_emit >= 20
                    write_progress(directory, manifest, counts, usage, emit=emit)
                    if emit:
                        since_emit, last_emit = 0, time.monotonic()
            manifest["status"] = (
                "stopped"
                if stopped and counts["not_started"]
                else "complete_with_errors"
                if any(counts[s] for s in TERMINAL if s != "valid")
                else "complete"
            )
            manifest["finished_at"] = utc_now()
            write_progress(directory, manifest, counts, usage, emit=True)
        except BaseException as exc:
            # Threads have drained on context exit; reconstruct durable state after an interruption/error.
            counts, usage, _ = inspect_results(directory, manifest, ontology, prompt, mapping)
            manifest.update(
                status="stopped" if isinstance(exc, KeyboardInterrupt) else "failed",
                error_type=type(exc).__name__,
                finished_at=utc_now(),
            )
            write_progress(directory, manifest, counts, usage, emit=True)
            raise
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DATABASE)
    parser.add_argument("--output-root", type=Path, default=ROOT / "data/corpus_extractions")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--prompt", dest="prompt_path", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--max-tokens", type=int, default=8192)
    args = parser.parse_args()
    if args.resume is None and args.prompt_path is None:
        parser.error("a fresh run requires --prompt")
    run(**vars(args))


if __name__ == "__main__":
    main()
