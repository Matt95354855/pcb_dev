from copy import deepcopy
from pathlib import Path
import unittest

from pcb_dev.checks import check_design
from pcb_dev.decoupling import check_decoupling
from pcb_dev.model import Design, freeze_fingerprint, load_design
from pcb_dev.packages import create_package, instantiate_block
from pcb_dev.transactions import TransactionError, apply_patch


FIXTURE = Path(__file__).resolve().parents[1] / "examples/decoupling"


class DecouplingIntentTests(unittest.TestCase):
    def setUp(self):
        self.design = load_design(FIXTURE / "design.json")
        self.load = self.design.root.children[1]
        self.cap = self.load.children[0].components[0]
        self.rule = self.load.resources["decoupling"][0]

    def check(self):
        return check_decoupling(self.design)

    def test_declared_fixture_passes_without_claiming_layout_or_source_verification(self):
        report = self.check()
        self.assertFalse(report.blocking)
        self.assertEqual(report.metadata["validated_rules"], 1)
        self.assertEqual(report.metadata["rule_count"], 1)
        self.assertFalse(report.metadata["source_verified"])
        self.assertFalse(report.metadata["placement_verified"])
        self.assertFalse(report.metadata["manufacturing_qualified"])
        self.assertEqual(report.metadata["declared_power_pins"], [{"component": "board.load.j2", "pin": "2"}])
        preflight = check_design(self.design)
        self.assertFalse(preflight.blocking)
        self.assertEqual(preflight.metadata["decoupling"], report.metadata)

    def test_undeclared_legacy_design_is_explicitly_out_of_scope(self):
        self.load.resources.clear()
        report = self.check()
        self.assertFalse(report.blocking)
        self.assertEqual(report.metadata["coverage"], "not-declared")
        self.assertEqual(report.metadata["rule_count"], 0)
        self.assertFalse(report.metadata["placement_verified"])

    def test_capacitance_and_derated_voltage_boundaries_are_inclusive(self):
        self.rule.update(capacitance_min_f=1e-7, capacitance_max_f=1e-7)
        self.cap.parameters["voltage_rating_v"] = self.rule["rail_max_voltage_v"] / self.rule["voltage_derating_factor"]
        self.assertFalse(self.check().blocking)
        self.cap.parameters["voltage_rating_v"] *= 0.99
        self.assertIn("decoupling.voltage_rating", [issue.code for issue in self.check().issues])
        self.cap.parameters["capacitance_f"] *= 0.5
        report = check_design(self.design)
        self.assertTrue(report.blocking)
        self.assertIn("decoupling.capacitance", [issue.code for issue in report.issues])

    def test_rules_reject_unknown_fields_duplicate_ids_and_non_arrays(self):
        baseline = deepcopy(self.load.resources)
        for replacement in ([{**self.rule, "ignore": True}], [self.rule, self.rule], {}, None):
            self.load.resources = {"decoupling": deepcopy(replacement)}
            with self.subTest(replacement=replacement):
                self.assertTrue(self.check().blocking)
        self.load.resources = baseline

    def test_invalid_numeric_thresholds_and_physical_facts_are_not_coerced(self):
        for key in ("capacitance_min_f", "capacitance_max_f", "rail_max_voltage_v", "voltage_derating_factor", "max_distance_mm"):
            for value in (True, "100n", None, 0, -1, float("inf")):
                original = self.rule[key]
                self.rule[key] = value
                with self.subTest(key=key, value=value):
                    self.assertTrue(self.check().blocking)
                self.rule[key] = original
        for key in ("capacitance_f", "voltage_rating_v"):
            for value in (True, "16V", None, 0, -1):
                original = self.cap.parameters[key]
                self.cap.parameters[key] = value
                with self.subTest(key=key, value=value):
                    self.assertTrue(self.check().blocking)
                self.cap.parameters[key] = original
        self.rule["voltage_derating_factor"] = 1.1
        self.assertTrue(self.check().blocking)

    def test_missing_or_mismatched_pins_capacitors_and_return_nets_block(self):
        replacements = [
            ("power_pin", {"component": "../outside", "pin": "2"}),
            ("power_pin", {"component": "missing", "pin": "2"}),
            ("power_pin", {"component": "j2", "pin": "99"}),
            ("capacitor", {"component": "missing", "power_pin": "1", "return_pin": "2"}),
            ("capacitor", {"component": "decaps.c1", "power_pin": "1", "return_pin": "1"}),
            ("return_net", "VDD"), ("return_net", "AGND"), ("power_net", "OTHER"),
        ]
        for key, value in replacements:
            original = self.rule[key]
            self.rule[key] = value
            with self.subTest(key=key, value=value):
                self.assertTrue(self.check().blocking)
            self.rule[key] = original
        self.cap.pins["3"] = "GND"
        self.assertTrue(self.check().blocking)

    def test_dnp_target_or_capacitor_never_satisfies_a_rule(self):
        for component in (self.cap, self.load.components[0]):
            component.fitted = False
            self.assertTrue(self.check().blocking)
            component.fitted = True

    def test_shared_capacitor_cannot_cover_two_separate_power_pins(self):
        self.load.components[0].pins["3"] = "VDD"
        extra = deepcopy(self.rule)
        extra.update(id="other_pin", power_pin={"component": "j2", "pin": "3"})
        self.load.resources["decoupling"].append(extra)
        self.assertTrue(self.check().blocking)

    def test_rail_limit_cannot_hide_already_declared_higher_voltage(self):
        self.rule["rail_max_voltage_v"] = 3.0
        self.assertIn("decoupling.rail_limit", [issue.code for issue in self.check().issues])

    def test_power_target_cannot_contradict_existing_port_kind(self):
        port = next(port for port in self.load.ports if port.id == "supply")
        port.kind = "analog"
        self.assertTrue(self.check().blocking)

    def test_source_shape_is_strict_but_source_claim_is_never_authenticated(self):
        original = deepcopy(self.rule["source"])
        for value in (None, "datasheet", {**original, "verified": True}, {**original, "kind": "assumed-by-llm"}):
            self.rule["source"] = value
            self.assertTrue(self.check().blocking)
        self.rule["source"] = {**original, "kind": "datasheet", "reference": "A declared title, not downloaded"}
        report = self.check()
        self.assertFalse(report.blocking)
        self.assertFalse(report.metadata["sources"][0]["verified"])

    def test_packaged_descendant_targets_and_shared_return_nets_remain_correct(self):
        package = create_package(self.design, "board.load", name="decoupled-load")
        instance, connections = instantiate_block(package, instance_id="channel", reference_prefix="CH1_", net_prefix="CH1_", shared_nets=["GND"])
        independent = Design("instance", "0.1.0", self.design.board, instance, connections)
        rule = instance.resources["decoupling"][0]
        self.assertEqual(rule["power_net"], "CH1_VDD")
        self.assertEqual(rule["return_net"], "GND")
        self.assertEqual(rule["capacitor"]["component"], "decaps.c1")
        self.assertEqual(instance.children[0].components[0].reference, "CH1_C1")
        self.assertFalse(check_decoupling(independent).blocking)

    def test_dnp_package_variant_remains_a_decoupling_failure(self):
        self.load.variants["without-cap"] = {"dnp": ["decaps.c1"]}
        package = create_package(self.design, "board.load", name="decoupled-load")
        instance, connections = instantiate_block(package, instance_id="channel", reference_prefix="CH1_", net_prefix="CH1_", variant="without-cap")
        self.assertTrue(check_decoupling(Design("variant", "0.1.0", self.design.board, instance, connections)).blocking)

    def test_freeze_records_protect_rule_and_descendant_capacitor(self):
        self.load.frozen = True
        self.load.frozen_hash = freeze_fingerprint(self.design, "board.load")
        with self.assertRaises(TransactionError):
            apply_patch(self.design, [{"op": "update_component", "component": "board.load.decaps.c1", "changes": {"value": "different"}}])
        with self.assertRaises(TransactionError):
            apply_patch(self.design, [{"op": "update_block", "block": "board.load", "changes": {"resources": {}}}])
