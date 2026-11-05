# Local setup and storage

[Project overview](../README.md)

Run commands from the repository root. The supplied ZIP runs with Docker alone;
follow the [quick start](../README.md#quick-start). The Python installation below
is needed only for pipeline development and local tests.

## Python environment

Use Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[research,dev]'
```

The base package (`-e .`) installs the MCP runtime. The `research` extra adds
RDFLib, Beautiful Soup, lxml, DuckDB, and GEPA for ontology tooling, the pipeline,
and evaluation tests;
`dev` adds Ruff for linting and formatting. Dependency versions are pinned in
`requirements/` and read by `pyproject.toml`. Installing only
`requirements/pipeline.txt` does not install the base runtime.

## Local configuration

For a fresh checkout, create the ignored `.env` file described in the
[quick start](../README.md#quick-start). Only `OPENROUTER_API_KEY` is required
for the Docker demo. `NEO4J_AUTH` defaults to `neo4j/picton-local-demo`, and
Open WebUI generates its secret in the persistent data volume. New SEC
collection also requires a descriptive `SEC_USER_AGENT` with a real contact email.

Restrict credentials with `chmod 600 .env`. Optional `NEO4J_AUTH` and
`WEBUI_SECRET_KEY` overrides are supported; preserve existing values when
upgrading. `NEO4J_AUTH` initializes credentials only for a new database; editing
the file does not change an existing database's password. Never include `.env`
in a distribution ZIP.

Use `docker compose config --quiet` to validate configuration. Do not share
expanded `docker compose config` output, which includes credentials.

## Start and connect

From this project directory:

```sh
docker compose up -d --build --wait
docker compose ps
```

- Research chat: [Open WebUI](http://localhost:3080)
- Graph browser: [Neo4j Browser](http://localhost:7474/browser/)
- MCP endpoint: `http://localhost:3081/mcp`
- Python connection URI: `bolt://localhost:7687`
- Database: `neo4j`
- Username: `neo4j`
- Password: `picton-local-demo`, or the value after `neo4j/` in your override.

All published ports are bound to localhost. Neo4j has a 2 GiB memory limit for
this small dataset. Open WebUI has no login screen; this is not a public or
multi-user deployment. See the [interface guide](interface.md) for its persistent
settings, native citation filter, and chart behavior.

On first startup, `graph-seed` restores `deploy/seed/neo4j.dump` before Neo4j
starts. It contains the ten companies, extracted graph, edge evidence and
uniqueness constraints. An existing graph is retained. Open WebUI installs the
Murph model, bear avatar and citation filter only when missing. The document
tools read the bundled `data/filings.duckdb`; no extraction is rerun.

The graph snapshot and DuckDB file are required in the ZIP but excluded from
Git and Docker image build contexts. The graph dump contains only the `neo4j`
research database, not the `system` authentication database. UI users, chats,
API keys and host Docker volumes are not bundled.

If the initial graph restore is interrupted, startup stops instead of treating
a partially restored database as complete. For a disposable first installation
with no saved work, `docker compose down -v` removes its incomplete volumes so
you can retry. This also erases any chats and graph changes in that installation;
do not use it on an installation whose data you need to keep.

## Stop and persistence

```sh
docker compose stop
docker compose up -d --wait
docker compose logs --tail=50 neo4j
```

Graph data and database authentication are stored in the named volume
`picton-interview_neo4j_data`, mounted at `/data` inside the container.
The volume lives in Docker Desktop's Linux VM, outside this Git repository.
Chats and UI settings use `picton-interview_open_webui_data`. Local `data/` retains
only `filings.duckdb` and `gepa/`. The database contains the document metadata,
sections, clean and masked chunks, and company masks. Raw HTML, processed exports,
corpus extraction requests/responses and checkpoints, setup/debug artifacts, and
external node-provenance archives were deliberately removed during cleanup.
GEPA artifacts were retained unchanged. The existing Neo4j graph and its edge
evidence remain in the Docker volume; old corpus runs can no longer be resumed.
Database source/export paths remain historical provenance, not available files.
The ingestion and extraction commands can create new artifacts when run again.

`docker compose down` removes the container and network but keeps the volume.
Starting again reuses the existing database. **Do not use
`docker compose down -v` or delete the volume unless you intend to erase the graph.**
Deleting/resetting Docker Desktop's storage can also erase it; persistence is
not a backup.

## Development checks

```sh
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Model and market-data requests are mocked in the normal tests. Live Neo4j checks
are opt-in and documented in the [ontology](ontology.md) and
[interface](interface.md) guides. Do not run paid extraction or GEPA workflows
merely to test an installation.
