"""Translate the OWL ontology into a property-graph schema, without creating data."""

import argparse
import hashlib
import json
import re

from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, URIRef

from picton_graph.config import ONTOLOGY
from picton_graph.graph.connection import connect

KINDS = {
    OWL.Class: "classes",
    OWL.ObjectProperty: "relationships",
    OWL.DatatypeProperty: "data_properties",
    OWL.AnnotationProperty: "annotations",
}
ANNOTATION_REFERENCES = {
    "supported_by": "SourceDocument",
    "supplied_product": "Product",
    "supplied_service": "Service",
}


def parse_ontology(path):
    source = path.read_bytes()
    graph = Graph().parse(data=source, format="xml", publicID=path.resolve().as_uri())
    ontologies = list(graph.subjects(RDF.type, OWL.Ontology))
    if len(ontologies) != 1 or not isinstance(ontologies[0], URIRef):
        raise ValueError("Expected exactly one named ontology.")
    ontology = ontologies[0]
    consumed = {(ontology, RDF.type, OWL.Ontology)}
    schema = {kind: {} for kind in KINDS.values()}

    def value(subject, predicate, required=False):
        values = list(graph.objects(subject, predicate))
        if len(values) > 1 or (required and not values):
            raise ValueError(f"Expected one {predicate} on {subject}.")
        if values:
            consumed.add((subject, predicate, values[0]))
            return values[0]
        return None

    def name(term):
        if not isinstance(term, URIRef):
            raise ValueError(f"Expected a named term: {term}")
        result = str(term).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", result):
            raise ValueError(f"Unsafe Cypher identifier: {result}")
        return result

    def definition(subject):
        result = {"iri": str(subject)}
        for predicate, key in ((RDFS.label, "label"), (RDFS.comment, "description")):
            literal = value(subject, predicate)
            if literal is not None:
                if not isinstance(literal, Literal):
                    raise ValueError(f"Expected a literal {predicate} on {subject}.")
                result[key] = str(literal)
                if literal.language:
                    result[key + "_language"] = literal.language
                if literal.datatype:
                    result[key + "_datatype"] = str(literal.datatype)
        return result

    schema["ontology"] = definition(ontology)
    schema["ontology"].update(
        source_sha256=hashlib.sha256(source).hexdigest(), source_triples=len(graph)
    )
    declared = {}
    for rdf_type, kind in KINDS.items():
        for subject in sorted(graph.subjects(RDF.type, rdf_type)):
            if isinstance(subject, BNode):
                continue
            key = name(subject)
            if subject in declared or key in schema[kind]:
                raise ValueError(f"Ambiguous declaration: {subject}")
            declared[subject] = kind
            schema[kind][key] = definition(subject)
            consumed.add((subject, RDF.type, rdf_type))

    def require_kind(term, kind):
        if declared.get(term) != kind:
            raise ValueError(f"Expected a declared {kind} term: {term}")
        return name(term)

    def class_options(term):
        if isinstance(term, URIRef):
            return [require_kind(term, "classes")]
        if not isinstance(term, BNode) or value(term, RDF.type, True) != OWL.Class:
            raise ValueError(f"Unsupported class expression: {term}")
        head = value(term, OWL.unionOf, True)
        members, visited = set(), set()
        while head != RDF.nil:
            if not isinstance(head, BNode) or head in visited:
                raise ValueError("Invalid or cyclic union list.")
            visited.add(head)
            members.add(require_kind(value(head, RDF.first, True), "classes"))
            head = value(head, RDF.rest, True)
        if not members:
            raise ValueError("Empty union is unsupported.")
        return sorted(members)

    for kind in ("relationships", "data_properties"):
        for key, rule in schema[kind].items():
            subject = URIRef(rule["iri"])
            rule["domain_any_of"] = class_options(value(subject, RDFS.domain, True))
            target = value(subject, RDFS.range, True)
            if kind == "data_properties":
                if target not in (XSD.string, XSD.anyURI, XSD.dateTime):
                    raise ValueError(f"Unsupported datatype: {target}")
                rule["datatype"] = str(target)
            else:
                rule["range_any_of"] = class_options(target)
                inverse = value(subject, OWL.inverseOf)
                if inverse is not None:
                    rule["inverse"] = require_kind(inverse, "relationships")
                for rdf_type, flag in (
                    (OWL.IrreflexiveProperty, "irreflexive"),
                    (OWL.SymmetricProperty, "symmetric"),
                ):
                    characteristic = (subject, RDF.type, rdf_type)
                    if characteristic in graph:
                        rule[flag] = True
                        consumed.add(characteristic)
                if rule.get("symmetric"):
                    if rule.get("inverse", key) != key:
                        raise ValueError(
                            "Symmetric properties with a separate named inverse are unsupported."
                        )
                    rule["inverse"] = key

    for key, rule in schema["relationships"].items():
        if "inverse" in rule:
            inverse = schema["relationships"][rule["inverse"]]
            if inverse.get("inverse", key) != key:
                raise ValueError("Multiple inverses are unsupported.")
            if (
                rule["domain_any_of"] != inverse["range_any_of"]
                or rule["range_any_of"] != inverse["domain_any_of"]
            ):
                raise ValueError("Inverse domains and ranges disagree.")
            inverse["inverse"] = key
            if rule.get("irreflexive") or inverse.get("irreflexive"):
                rule["irreflexive"] = inverse["irreflexive"] = True

    disjoint = set()
    for subject, _, target in graph.triples((None, OWL.disjointWith, None)):
        disjoint.add(
            tuple(sorted((require_kind(subject, "classes"), require_kind(target, "classes"))))
        )
        consumed.add((subject, OWL.disjointWith, target))
    schema["disjoint_classes"] = sorted(map(list, disjoint))

    # These reference types are application policies from the annotation descriptions, not OWL axioms.
    if set(schema["annotations"]) != set(ANNOTATION_REFERENCES) | {"evidence_text"}:
        raise ValueError("Update annotation validation for the changed annotation properties.")
    for key, target in ANNOTATION_REFERENCES.items():
        if target not in schema["classes"]:
            raise ValueError(f"Missing annotation reference class: {target}")
        schema["annotations"][key]["reference_class"] = target
    schema["annotations"]["evidence_text"]["value_type"] = "string"

    # shortcut: map this ontology's constructs; extend the translator before adding new OWL features.
    unsupported = set(graph) - consumed
    if unsupported:
        raise ValueError(
            f"Unmapped OWL statements ({len(unsupported)}): {sorted(map(str, unsupported))[:3]}"
        )
    return schema


def schema_statements(schema):
    statements = []
    for label in schema["classes"]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            raise ValueError(f"Unsafe Cypher identifier: {label}")
        statements.append(
            f"CREATE CONSTRAINT picton_{label}_iri_unique IF NOT EXISTS FOR (n:{label}) REQUIRE n.iri IS UNIQUE"
        )
    statements.append(
        "UNWIND "
        + json.dumps(sorted(schema["relationships"]))
        + " AS name CALL db.createRelationshipType(name)"
    )
    properties = (
        {"iri", "name", "description"}
        | schema["data_properties"].keys()
        | schema["annotations"].keys()
    )
    statements.append(
        "UNWIND " + json.dumps(sorted(properties)) + " AS name CALL db.createProperty(name)"
    )
    return statements


def visualization_query(schema):
    literal = json.dumps(json.dumps(schema))
    return f"""// Virtual schema only: this query does not create stored nodes or relationships.
WITH apoc.convert.fromJsonMap({literal}) AS schema
UNWIND keys(schema.classes) AS label
WITH schema, label, schema.classes[label] AS definition
CALL apoc.create.vNode([label], definition{{.*,
    name: label,
    data_properties: [key IN keys(schema.data_properties)
        WHERE label IN schema.data_properties[key].domain_any_of |
        key + ': ' + last(split(schema.data_properties[key].datatype, '#'))],
    disjoint_with: [pair IN schema.disjoint_classes WHERE label IN pair |
        head([other IN pair WHERE other <> label])]
}}) YIELD node
WITH schema, collect({{name: label, node: node}}) AS nodes
UNWIND keys(schema.relationships) AS kind
WITH schema, nodes, kind, schema.relationships[kind] AS rule
UNWIND rule.domain_any_of AS sourceLabel
UNWIND rule.range_any_of AS targetLabel
WITH schema, kind, rule,
     head([entry IN nodes WHERE entry.name = sourceLabel | entry.node]) AS source,
     head([entry IN nodes WHERE entry.name = targetLabel | entry.node]) AS target
CALL apoc.create.vRelationship(source, kind,
    rule{{.*, evidence_annotations: keys(schema.annotations)}}, target) YIELD rel
RETURN source, rel, target;
"""


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    schema = parse_ontology(ONTOLOGY / "picton.owl")
    statements = schema_statements(schema)
    with connect() as driver, driver.session(database="neo4j") as session:
        for statement in statements:
            session.run(statement).consume()
    (ONTOLOGY / "schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    (ONTOLOGY / "schema.cypher").write_text(";\n\n".join(statements) + ";\n", encoding="utf-8")
    (ONTOLOGY / "visualize.cypher").write_text(visualization_query(schema), encoding="utf-8")
    print(
        f"Applied {len(schema['classes'])} label identity constraints and registered {len(schema['relationships'])} relationship types. No nodes or relationships created."
    )


if __name__ == "__main__":
    main()
