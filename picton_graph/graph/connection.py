"""Neo4j connection settings for local commands and the container service."""

import os

from neo4j import GraphDatabase

from picton_graph.config import ROOT


def connect():
    auth = os.environ.get("NEO4J_AUTH") or next(
        line.removeprefix("NEO4J_AUTH=").strip()
        for line in (ROOT / ".env").read_text().splitlines()
        if line.startswith("NEO4J_AUTH=")
    )
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=tuple(auth.split("/", 1)),
    )
