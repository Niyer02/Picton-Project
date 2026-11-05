"""Frozen, deterministic agreement metric for the small extraction gold set.

Call validate_extraction before this scorer. This compares canonical facts, not
semantic truth: an unmatched fact can be a valid alternative needing review.
"""

import json
import re
import unicodedata
from datetime import datetime

METRIC_VERSION = "canonical-facts-v2-named-references"
WEIGHTS = {
    "business_edges": 0.50,
    "entities": 0.20,
    "supplied_items": 0.15,
    "temporal_qualifiers": 0.10,
    "documentary_edges": 0.05,
}
LIMITATION = (
    "Canonical agreement with draft labels, not semantic accuracy. Unmatched facts "
    "may be supported alternatives; review them before claiming extraction errors."
)

# Frozen before inference; do not tune aliases using held-out model responses.
EVENT_ALIASES = {
    "arbitration_agreement_signed": "arbitration_agreement",
    "binding_arbitration_agreement": "arbitration_agreement",
    "joint_venture_termination": "joint_venture_ended",
    "joint_venture_ended": "joint_venture_ended",
    "joint_venture_dissolution": "joint_venture_ended",
    "arbitration_final_order": "arbitration_concluded",
    "arbitration_conclusion": "arbitration_concluded",
    "arbitration_resolution": "arbitration_concluded",
    "debt_issuance": "debt_issuance_completed",
    "notes_issuance": "debt_issuance_completed",
    "note_issuance": "debt_issuance_completed",
    "debt_offering_completed": "debt_issuance_completed",
    "acquisition_agreement_signed": "acquisition_agreement",
    "acquisition_agreement_entered": "acquisition_agreement",
    "definitive_acquisition_agreement": "acquisition_agreement",
    "share_sale_agreement": "equity_sale_agreement",
    "stock_sale_agreement": "equity_sale_agreement",
    "equity_purchase_agreement": "equity_sale_agreement",
    "share_issuance_completed": "equity_sale_completed",
    "share_sale_completed": "equity_sale_completed",
    "stock_sale_completed": "equity_sale_completed",
    "equity_issuance_completed": "equity_sale_completed",
    "acquisition_completion": "acquisition_completed",
    "merger_completed": "acquisition_completed",
    "product_shipment": "product_shipments",
    "equipment_shipment": "product_shipments",
    "system_shipments": "product_shipments",
}
NAME_ALIASES = {
    "Topic": {"artificial intelligence": "ai", "ai ecosystem": "ai"},
    "Service": {
        "production of all wafers": "wafer production",
        "production of wafers": "wafer production",
        "produce our semiconductor wafers": "semiconductor wafer production",
        "produce semiconductor wafers": "semiconductor wafer production",
        "fabricate the silicon wafers": "wafer production",
        "post contract customer support pcs": "post contract customer support",
        "pcs": "post contract customer support",
    },
}


def normalize_name(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def normalize_time(value):
    if value is None:
        return None
    text = re.sub(r"^(?:on|in|during)\s+", "", value.strip(), flags=re.I)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.strftime("%Y-%m" if "%d" not in fmt else "%Y-%m-%d")
        except ValueError:
            pass
    return normalize_name(text)


def _reference(entity):
    return entity["type"], entity["name"]


def canonical_facts(result):
    """Resolve typed names and separate topology from supplied-item/time details."""
    facts = {category: set() for category in WEIGHTS}
    keys = {("SourceDocument", "document"): ("SourceDocument", "document")}
    for entity in result["entities"]:
        if entity["type"] != "Event":
            name = normalize_name(entity["name"])
            name = NAME_ALIASES.get(entity["type"], {}).get(name, name)
            keys[_reference(entity)] = (entity["type"], name)
    for entity in result["entities"]:
        if entity["type"] != "Event":
            continue
        participants = sorted(
            {
                keys[_reference(r["target"])]
                for r in result["relationships"]
                if r["type"] == "concerns" and _reference(r["source"]) == _reference(entity)
            }
        )
        kind = EVENT_ALIASES.get(entity["event_type"], entity["event_type"])
        time = normalize_time(entity["time_text"])
        key = ("Event", kind, time, tuple(participants))
        keys[_reference(entity)] = key
        facts["temporal_qualifiers"].add(("event_time", key, time))
    for entity in result["entities"]:
        facts["entities"].add(keys[_reference(entity)])
    for relationship in result["relationships"]:
        source, target = (
            keys[_reference(relationship["source"])],
            keys[_reference(relationship["target"])],
        )
        kind = relationship["type"]
        if kind == "competes_with":
            source, target = sorted((source, target))
        edge = (kind, source, target)
        category = "documentary_edges" if source[0] == "SourceDocument" else "business_edges"
        facts[category].add(edge)
        for field, entity_type in (
            ("supplied_product", "Product"),
            ("supplied_service", "Service"),
        ):
            for name in relationship[field]:
                facts["supplied_items"].add((edge, field, keys[(entity_type, name)]))
        if relationship["time_text"] is not None:
            facts["temporal_qualifiers"].add(
                ("edge_time", edge, normalize_time(relationship["time_text"]))
            )
    return facts


def fact_metrics(predicted, gold):
    matched = len(predicted & gold)
    precision = matched / len(predicted) if predicted else float(not gold)
    recall = matched / len(gold) if gold else float(not predicted)
    f1 = 2 * matched / (len(predicted) + len(gold)) if predicted or gold else 1.0
    return {
        "matched": matched,
        "predicted": len(predicted),
        "gold": len(gold),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def score_extraction(prediction, example):
    """Score a structurally validated prediction against one frozen gold example.

    The weighted mean includes categories present on either side only. Thus
    absence of many unused relationship types cannot inflate a weak extraction.
    Empty gold requires truly empty output for a score of one.
    """
    predicted = canonical_facts(prediction)
    gold = canonical_facts(example["expected_output"])
    metrics = {category: fact_metrics(predicted[category], gold[category]) for category in WEIGHTS}
    active = [category for category in WEIGHTS if predicted[category] or gold[category]]
    score = (
        sum(WEIGHTS[c] * metrics[c]["f1"] for c in active) / sum(WEIGHTS[c] for c in active)
        if active
        else 1.0
    )
    missing, unmatched = {}, {}
    for category in WEIGHTS:
        missing[category] = sorted(
            json.dumps(fact, ensure_ascii=False) for fact in gold[category] - predicted[category]
        )
        unmatched[category] = sorted(
            json.dumps(fact, ensure_ascii=False) for fact in predicted[category] - gold[category]
        )
    feedback = [f"Canonical agreement score: {score:.4f}. {LIMITATION}"]
    for category in WEIGHTS:
        if missing[category]:
            feedback.append(f"Missing canonical {category}: " + "; ".join(missing[category]))
        if unmatched[category]:
            feedback.append(f"Unmatched predicted {category}: " + "; ".join(unmatched[category]))
    if example.get("annotation"):
        feedback.append("Gold adjudication: " + example["annotation"]["adjudication_notes"])
        feedback.append(
            "Forbidden inferences: " + "; ".join(example["annotation"]["must_not_extract"])
        )
    return {
        "score": score,
        "metrics": metrics,
        "missing": missing,
        "unmatched": unmatched,
        "feedback": "\n".join(feedback),
        "metric_version": METRIC_VERSION,
        "limitation": LIMITATION,
    }
