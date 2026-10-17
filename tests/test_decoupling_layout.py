"""Actual pinned pad evidence, with isolated synthetic geometry mutations."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pcb_dev.backends import generate_bound
from pcb_dev.bindings import load_bindings
from pcb_dev.decoupling import check_layout
from pcb_dev.model import load_design
from pcb_dev.sexpr import dumps, parse_quoted


FIXTURE = Path(__file__).resolve().parents[1] / "examples/decoupling"


def children(node, tag):
    return [item for item in node[1:] if isinstance(item, list) and item and item[0] == tag]


class DecouplingLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fixture = self.root / "fixture"
        shutil.copytree(FIXTURE, self.fixture)
        self.design = load_design(self.fixture / "design.json")
        self.index = 0

    def build(self):
        self.bound = load_bindings(self.design, self.fixture / "bindings.json")
        self.paths = generate_bound(self.bound, self.root / f"build-{self.index}")
        self.index += 1
        self.board = self.paths["board"]
        self.tree = parse_quoted(self.board.read_text(encoding="utf-8"))

    def footprint(self, reference):
        return next(node for node in children(self.tree, "footprint")
                    if any(len(item) >= 3 and item[1:3] == ["Reference", reference] for item in children(node, "property")))

    def check(self):
        self.board.write_text(dumps(self.tree), encoding="utf-8", newline="\n")
        return check_layout(self.design, self.board, self.bound)

    def mutate_source(self, function):
        source = self.fixture / "libraries/PinHeader_1x02_P2.54mm_Vertical.kicad_mod"
        tree = parse_quoted(source.read_text(encoding="utf-8"))
        function(tree)
        source.write_text(dumps(tree), encoding="utf-8", newline="\n")
        registry_path = self.fixture / "registry.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        next(record for record in registry["libraries"] if record["name"] == "footprint-header")["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        registry_path.write_text(json.dumps(registry), encoding="utf-8", newline="\n")

    def test_actual_90_and_270_degree_pads_prove_one_mm_distance(self):
        self.build()
        result = self.check()
        self.assertFalse(result.blocking, result.to_dict())
        self.assertTrue(result.metadata["completed"])
        self.assertTrue(result.metadata["distance_verified"])
        self.assertFalse(result.metadata["native"])
        self.assertFalse(result.metadata["source_verified"])
        measured = result.metadata["measurements"][0]
        self.assertAlmostEqual(measured["distance_mm"], 1.0)
        self.assertAlmostEqual(measured["limiting_target_centre_mm"][0], 32.54)
        self.assertAlmostEqual(measured["limiting_target_centre_mm"][1], 20)
        self.assertAlmostEqual(measured["nearest_capacitor_centre_mm"][1], 21)
        self.assertEqual(result.metadata["board_sha256"], hashlib.sha256(self.board.read_bytes()).hexdigest())
        self.assertEqual(result.metadata["bindings_sha256"], self.bound.provenance["bindings_sha256"])

    def test_exact_distance_limit_is_inclusive(self):
        self.design.root.children[1].resources["decoupling"][0]["max_distance_mm"] = 1.0
        self.build()
        self.assertFalse(self.check().blocking)

    def test_measures_actual_board_pose_even_if_model_pose_did_not_move(self):
        self.build()
        children(self.footprint("C1"), "at")[0][1] = "47.54"
        report = self.check()
        self.assertTrue(report.blocking)
        self.assertTrue(report.metadata["completed"])
        self.assertTrue(report.metadata["distance_verified"])
        self.assertIn("decoupling.distance", [issue.code for issue in report.issues])
        self.assertGreater(report.metadata["measurements"][0]["distance_mm"], 15)

    def test_near_component_centres_cannot_hide_far_power_pads(self):
        capacitor = self.design.root.children[1].children[0].components[0]
        capacitor.position.x_mm, capacitor.position.y_mm, capacitor.position.rotation = 30.3, 20, 0
        self.build()
        result = self.check()
        self.assertTrue(result.blocking)
        self.assertGreater(result.metadata["measurements"][0]["distance_mm"], 3)

    def test_modified_local_pad_geometry_cannot_fake_proximity(self):
        for tag in ("at", "size", "roundrect_rratio"):
            self.build()
            pad = children(self.footprint("C1"), "pad")[0]
            node = children(pad, tag)[0]
            node[1] = "0.01" if tag == "roundrect_rratio" else "10"
            report = self.check()
            with self.subTest(tag=tag):
                self.assertTrue(report.blocking)
                self.assertFalse(report.metadata["completed"])
                self.assertFalse(report.metadata["distance_verified"])

    def test_all_pads_of_a_repeated_power_pin_must_be_close(self):
        def repeated(tree):
            pad = deepcopy(next(pad for pad in children(tree, "pad") if pad[1] == "2"))
            children(pad, "at")[0][1:3] = ["9", "9"]
            tree.append(pad)
        self.mutate_source(repeated)
        self.build()
        report = self.check()
        self.assertTrue(report.blocking)
        self.assertTrue(report.metadata["distance_verified"])
        measured = report.metadata["measurements"][0]
        self.assertEqual(len(measured["target_pad_centres_mm"]), 2)
        self.assertGreater(measured["distance_mm"], 10)

    def test_numbered_mechanical_pad_is_not_an_electrical_target(self):
        def mechanical(tree):
            pad = deepcopy(next(pad for pad in children(tree, "pad") if pad[1] == "2"))
            pad[2:4] = ["np_thru_hole", "circle"]
            children(pad, "at")[0][1:3] = ["9", "9"]
            tree.append(pad)
        self.mutate_source(mechanical)
        self.build()
        result = self.check()
        self.assertFalse(result.blocking, result.to_dict())
        self.assertEqual(len(result.metadata["measurements"][0]["target_pad_centres_mm"]), 1)
        footprint = self.footprint("J2")
        electrical = next(pad for pad in children(footprint, "pad") if pad[1] == "2" and pad[2] != "np_thru_hole")
        footprint.remove(electrical)
        self.assertTrue(self.check().blocking)

    def test_wrong_net_missing_pad_and_dnp_board_capacitor_block(self):
        self.build()
        capacitor = self.footprint("C1")
        capacitor.append(["attr", "smd", "dnp"])
        self.assertTrue(self.check().blocking)
        self.build()
        pad = children(self.footprint("C1"), "pad")[0]
        children(pad, "net")[0][2] = "OTHER"
        self.assertTrue(self.check().blocking)
        self.build()
        cap = self.footprint("C1")
        cap.remove(children(cap, "pad")[0])
        self.assertTrue(self.check().blocking)

    def test_missing_binding_generic_preview_and_stale_binding_are_not_evidence(self):
        self.build()
        self.assertTrue(check_layout(self.design, self.board).blocking)
        changed = load_design(self.fixture / "design.json")
        changed.root.children[1].components[0].value = "changed"
        self.assertTrue(check_layout(changed, self.board, self.bound).blocking)
        from pcb_dev.backends import generate
        generic = generate(self.design.to_dict(), self.root / "generic")
        self.assertTrue(check_layout(self.design, generic["board"], self.bound).blocking)

    def test_bottom_missing_placement_and_nonfinite_pose_fail_closed(self):
        self.build()
        children(self.footprint("C1"), "layer")[0][1] = "B.Cu"
        self.assertTrue(self.check().blocking)
        self.build()
        children(self.footprint("C1"), "at")[0][1] = "nan"
        self.assertTrue(self.check().blocking)
        self.design.root.children[1].children[0].components[0].position = None
        self.build()
        self.assertTrue(self.check().blocking)

    def test_captured_inventory_mutation_is_rejected_against_source_hashes(self):
        self.build()
        self.bound.components["board.load.decaps.c1"].footprint_inventory.pads[0].x_mm += 1
        self.assertTrue(self.check().blocking)

    def test_file_change_during_measurement_invalidates_the_whole_report(self):
        from pcb_dev.decoupling import _board_evidence
        self.build()
        def changed(*args):
            result = _board_evidence(*args)
            self.board.write_text("(kicad_pcb)", encoding="utf-8")
            return result
        with patch("pcb_dev.decoupling._board_evidence", side_effect=changed):
            report = check_layout(self.design, self.board, self.bound)
        self.assertTrue(report.blocking)
        self.assertFalse(report.metadata["distance_verified"])
        self.assertFalse(report.metadata["completed"])

    def test_no_rule_has_no_global_decoupling_or_geometry_claim(self):
        self.design.root.children[1].resources.clear()
        result = check_layout(self.design, self.root / "missing", None)
        self.assertFalse(result.blocking)
        self.assertEqual(result.metadata["coverage"], "not-declared")
        self.assertFalse(result.metadata["distance_verified"])

    def test_extreme_finite_positions_fail_without_poisoning_json_evidence(self):
        self.build()
        children(self.footprint("J2"), "at")[0][1] = "1e308"
        children(self.footprint("C1"), "at")[0][1] = "-1e308"
        result = self.check()
        self.assertTrue(result.blocking)
        self.assertFalse(result.metadata["distance_verified"])
        self.assertNotIn("Infinity", result.to_json())

    def test_distance_comparison_budget_is_enforced(self):
        self.build()
        with patch("pcb_dev.decoupling.MAX_DISTANCE_COMPARISONS", 0):
            result = self.check()
        self.assertTrue(result.blocking)
        self.assertFalse(result.metadata["completed"])
