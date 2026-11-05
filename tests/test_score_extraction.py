import json
import unittest
from copy import deepcopy

from picton_graph.config import GOLD_PATH
from picton_graph.evaluation.scoring import canonical_facts, normalize_time, score_extraction

GOLD = {
    r["id"]: r
    for split in ("train", "validation", "holdout")
    for r in map(json.loads, (GOLD_PATH / f"{split}.jsonl").read_text().splitlines())
}


def change_entity(result, entity, **changes):
    before = {key: entity[key] for key in ("type", "name")}
    entity.update(changes)
    after = {key: entity[key] for key in ("type", "name")}
    for edge in result["relationships"]:
        for endpoint in ("source", "target"):
            if edge[endpoint] == before:
                edge[endpoint] = dict(after)
        for field, kind in (("supplied_product", "Product"), ("supplied_service", "Service")):
            if before["type"] == kind:
                edge[field] = [
                    after["name"] if name == before["name"] else name for name in edge[field]
                ]


class ScorerTests(unittest.TestCase):
    def test_every_gold_example_scores_one(self):
        for example in GOLD.values():
            with self.subTest(example=example["id"]):
                score = score_extraction(example["expected_output"], example)
                self.assertEqual(score["score"], 1.0)
                self.assertTrue(all(m["f1"] == 1.0 for m in score["metrics"].values()))
                self.assertFalse(any(score["missing"].values()))
                self.assertFalse(any(score["unmatched"].values()))

    def test_order_quote_boundaries_and_symmetric_direction_are_ignored(self):
        example = GOLD["gold_02"]
        prediction = deepcopy(example["expected_output"])
        for entity in prediction["entities"]:
            entity["evidence_text"] = "different already validated quote"
        for edge in prediction["relationships"]:
            if edge["type"] == "competes_with":
                edge["source"], edge["target"] = edge["target"], edge["source"]
        prediction["entities"].reverse()
        prediction["relationships"].reverse()
        self.assertEqual(score_extraction(prediction, example)["score"], 1.0)

    def test_supply_direction_and_bindings_are_separate_scored_facts(self):
        example = GOLD["gold_01"]
        prediction = deepcopy(example["expected_output"])
        edge = next(r for r in prediction["relationships"] if r["type"] == "supplies_to")
        edge["source"], edge["target"] = edge["target"], edge["source"]
        score = score_extraction(prediction, example)
        self.assertLess(score["metrics"]["business_edges"]["f1"], 1)
        self.assertLess(score["metrics"]["supplied_items"]["f1"], 1)

        self.assertEqual(score["metrics"]["entities"]["f1"], 1)
        prediction = deepcopy(example["expected_output"])
        supply = [r for r in prediction["relationships"] if r["type"] == "supplies_to"]
        supply[0]["supplied_product"], supply[1]["supplied_product"] = (
            supply[1]["supplied_product"],
            supply[0]["supplied_product"],
        )
        score = score_extraction(prediction, example)
        self.assertEqual(score["metrics"]["business_edges"]["f1"], 1)
        self.assertLess(score["metrics"]["supplied_items"]["f1"], 1)

    def test_document_topics_do_not_inflate_business_edge_metric(self):
        facts = canonical_facts(GOLD["gold_02"]["expected_output"])
        self.assertEqual(len(facts["business_edges"]), 2)
        self.assertTrue(all(f[0] == "competes_with" for f in facts["business_edges"]))
        self.assertTrue(any(f[0] == "has_topic" for f in facts["documentary_edges"]))

    def test_event_display_name_alias_and_date_format_do_not_change_identity(self):
        example = GOLD["gold_09"]
        prediction = deepcopy(example["expected_output"])
        event = next(e for e in prediction["entities"] if e["type"] == "Event")
        change_entity(
            prediction,
            event,
            name="Different display name",
            event_type="acquisition_agreement_signed",
            time_text="In October 2026",
        )
        self.assertEqual(score_extraction(prediction, example)["score"], 1)
        self.assertEqual(normalize_time("On September 15, 2025"), normalize_time("2025-09-15"))
        self.assertNotEqual(normalize_time("September 2025"), normalize_time("September 15, 2025"))

    def test_event_type_time_and_participants_are_not_ignored(self):
        example = GOLD["gold_10"]
        for change in ("type", "time", "participant"):
            with self.subTest(change=change):
                prediction = deepcopy(example["expected_output"])
                event = next(e for e in prediction["entities"] if e["type"] == "Event")
                if change == "type":
                    event["event_type"] = "equity_sale_completed"
                elif change == "time":
                    event["time_text"] = "December 26, 2025"
                else:
                    prediction["relationships"] = [
                        r
                        for r in prediction["relationships"]
                        if not (
                            r["type"] == "concerns"
                            and r["source"] == {"type": "Event", "name": event["name"]}
                        )
                    ]
                score = score_extraction(prediction, example)
                self.assertLess(score["score"], 1)
                self.assertLess(score["metrics"]["entities"]["f1"], 1)

    def test_edge_time_is_separate_and_missing_qualifier_is_penalized(self):
        example = GOLD["gold_06"]
        prediction = deepcopy(example["expected_output"])
        for edge in prediction["relationships"]:
            edge["time_text"] = None
        score = score_extraction(prediction, example)
        self.assertEqual(score["metrics"]["business_edges"]["f1"], 1)
        self.assertEqual(score["metrics"]["temporal_qualifiers"]["f1"], 0)
        self.assertLess(score["score"], 1)

    def test_empty_gold_and_empty_prediction(self):
        empty = {"entities": [], "relationships": []}
        self.assertEqual(score_extraction(empty, GOLD["gold_04"])["score"], 1)
        self.assertEqual(
            score_extraction(GOLD["gold_15"]["expected_output"], GOLD["gold_04"])["score"], 0
        )
        self.assertEqual(score_extraction(empty, GOLD["gold_01"])["score"], 0)

    def test_entity_types_stay_distinct_and_unmatched_is_not_called_hallucination(self):
        example = GOLD["gold_13"]
        prediction = deepcopy(example["expected_output"])
        entity = next(e for e in prediction["entities"] if e["type"] == "Service")
        change_entity(prediction, entity, type="Product")
        score = score_extraction(prediction, example)
        self.assertLess(score["score"], 1)
        self.assertTrue(score["unmatched"]["entities"])
        self.assertIn("may be supported alternatives", score["feedback"])
        self.assertNotIn("hallucination", score["feedback"])

    def test_aliases_do_not_merge_products_with_services_or_agreements_with_completion(self):
        example = GOLD["gold_20"]
        prediction = deepcopy(example["expected_output"])
        service = next(e for e in prediction["entities"] if e["type"] == "Service")
        change_entity(prediction, service, name="PCS")
        self.assertEqual(score_extraction(prediction, example)["score"], 1)
        change_entity(prediction, service, type="Product")
        self.assertNotEqual(
            canonical_facts(prediction), canonical_facts(example["expected_output"])
        )

    def test_supplied_items_resolve_by_field_type_when_names_are_shared(self):
        result = deepcopy(GOLD["gold_01"]["expected_output"])
        product = next(e for e in result["entities"] if e["type"] == "Product")
        service = next(e for e in result["entities"] if e["type"] == "Service")
        change_entity(result, service, name=product["name"])
        items = canonical_facts(result)["supplied_items"]
        name = next(
            f[2][1]
            for f in items
            if f[1] == "supplied_product" and f[2][1] == "semiconductor wafers"
        )
        self.assertIn(("Product", name), {f[2] for f in items if f[1] == "supplied_product"})
        self.assertIn(("Service", name), {f[2] for f in items if f[1] == "supplied_service"})


if __name__ == "__main__":
    unittest.main()
