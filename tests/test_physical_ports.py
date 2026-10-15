"""Physical mutations expose errors invisible to an abstract interface graph."""

import copy
import unittest

from pcb_dev.checks import check_design
from pcb_dev.model import Component, Connection, Design, PinBinding, Port
from pcb_dev.physical_ports import check_physical_ports


def rail_design():
    """One mapped 3V3 source and one MCU-like load, including ground pins."""
    return Design.from_dict({
        "schema_version": 1, "name": "Physical interfaces", "version": "0.3.0",
        "board": {"width_mm": 50, "height_mm": 30},
        "root": {"id": "board", "children": [
            {"id": "supply", "components": [{"id": "regulator", "reference": "U1", "value": "3V3 regulator", "footprint": "declared", "pins": {"1": "3V3", "2": "GND"}}],
             "ports": [
                 {"id": "out", "kind": "power", "direction": "output", "voltage_min": 3.2, "voltage_max": 3.4, "max_current_ma": 100,
                  "net": "3V3", "pin_bindings": [{"component": "regulator", "pin": "1"}]},
                 {"id": "gnd", "kind": "ground", "direction": "passive", "net": "GND", "pin_bindings": [{"component": "regulator", "pin": "2"}]},
             ]},
            {"id": "load", "components": [{"id": "mcu", "reference": "U2", "value": "3V3 MCU", "footprint": "declared", "pins": {"1": "3V3", "2": "GND"}}],
             "ports": [
                 {"id": "vcc", "kind": "power", "direction": "input", "voltage_min": 3, "voltage_max": 3.6, "current_draw_ma": 40,
                  "net": "3V3", "pin_bindings": [{"component": "mcu", "pin": "1"}]},
                 {"id": "gnd", "kind": "ground", "direction": "passive", "net": "GND", "pin_bindings": [{"component": "mcu", "pin": "2"}]},
             ]},
        ]},
        "connections": [{"source": "board.supply.out", "target": "board.load.vcc", "net": "3V3"},
                        {"source": "board.supply.gnd", "target": "board.load.gnd", "net": "GND"}],
    })


def issues(report):
    return {issue.code for issue in report.issues}


class PhysicalPortsTests(unittest.TestCase):
    def test_realized_source_and_load_have_complete_explicit_coverage(self):
        design = rail_design()
        before = design.fingerprint()
        report = check_design(design, require_physical_ports=True)
        self.assertFalse(report.blocking, report.to_markdown())
        coverage = report.metadata["physical_ports"]
        self.assertEqual(coverage["status"], "complete")
        self.assertTrue(coverage["complete"])
        self.assertEqual((coverage["total_ports"], coverage["realized_ports"], coverage["mapped_pins"]), (4, 4, 4))
        self.assertEqual(design.fingerprint(), before, "checks must never repair or rewrite physical nets")

    def test_mcu_vcc_miswired_to_vbus_is_blocked_despite_valid_3v3_interface(self):
        design = rail_design()
        load = dict(design.walk_blocks())["board.load"]
        load.components[0].pins["1"] = "VBUS"
        # The typed port still declares a compatible 3V3 load. The physical pin
        # mutation, rather than a changed interface rating, is the test oracle.
        report = check_design(design)
        self.assertTrue(report.blocking)
        finding = next(issue for issue in report.issues if issue.code == "physical_ports.pin_net_mismatch")
        self.assertEqual(finding.block_path, "board.load.vcc")
        self.assertEqual(finding.details["pin_net"], "VBUS")
        self.assertEqual(finding.details["port_net"], "3V3")
        self.assertFalse(report.metadata["physical_ports"]["complete"])

    def test_all_targets_must_share_one_net_and_no_component_path_is_guessed(self):
        design = rail_design()
        load = dict(design.walk_blocks())["board.load"]
        load.ports[0].pin_bindings.append(PinBinding("mcu", "2"))
        report = check_physical_ports(design)
        self.assertIn("physical_ports.pin_net_mismatch", issues(report))
        self.assertIn("physical_ports.pin_reused", issues(report))
        self.assertEqual(load.components[0].pins["2"], "GND")

    def test_abstract_legacy_ports_warn_but_strict_mode_blocks_all_exposed_ports(self):
        design = rail_design()
        for _, block in design.walk_blocks():
            for port in block.ports:
                port.net, port.pin_bindings = None, []
                port.required = False
        draft = check_design(design)
        self.assertFalse(draft.blocking, draft.to_markdown())
        self.assertEqual(draft.metadata["physical_ports"]["status"], "unverified")
        self.assertEqual(draft.metadata["physical_ports"]["abstract_ports"], 4)
        strict = check_design(design, require_physical_ports=True)
        self.assertTrue(strict.blocking)
        self.assertEqual(sum(issue.code == "physical_ports.unrealized" for issue in strict.issues), 4)

    def test_net_name_without_pin_target_is_not_physical_realization(self):
        design = rail_design()
        dict(design.walk_blocks())["board.load"].ports[0].pin_bindings = []
        draft = check_design(design)
        self.assertFalse(draft.blocking)
        self.assertEqual(draft.metadata["physical_ports"]["status"], "partial")
        strict = check_design(design, require_physical_ports=True)
        self.assertTrue(strict.blocking)
        self.assertEqual(strict.metadata["physical_ports"]["realized_ports"], 3)

    def test_dnp_target_does_not_prove_realization_even_if_copper_net_is_correct(self):
        design = rail_design()
        dict(design.walk_blocks())["board.load"].components[0].fitted = False
        draft = check_design(design)
        self.assertFalse(draft.blocking)
        self.assertEqual(draft.metadata["physical_ports"]["realized_ports"], 2)
        self.assertFalse(draft.metadata["physical_ports"]["complete"])
        self.assertTrue(check_design(design, require_physical_ports=True).blocking)

    def test_one_fitted_target_does_not_hide_another_unfitted_target(self):
        design = rail_design()
        load = dict(design.walk_blocks())["board.load"]
        load.components.append(Component("bypass", "C1", "100n", "declared", {"1": "3V3", "2": "GND"}, fitted=False))
        load.ports[0].pin_bindings.append(PinBinding("bypass", "1"))
        report = check_physical_ports(design, require_physical_ports=True)
        record = next(item for item in report.metadata["coverage"]["ports"] if item["path"] == "board.load.vcc")
        self.assertFalse(record["realized"])
        self.assertEqual(record["status"], "unfitted")
        self.assertTrue(report.blocking)

    def test_two_physical_outputs_conflict_without_a_logical_connection_edge(self):
        data = rail_design().to_dict()
        data["root"]["children"] = [{
            "id": identifier, "components": [{"id": "driver", "reference": f"U{index}", "value": "Output driver", "footprint": "declared", "pins": {"1": "DATA"}}],
            "ports": [{"id": "out", "kind": "digital", "direction": "output", "required": False, "net": "DATA", "pin_bindings": [{"component": "driver", "pin": "1"}]}],
        } for index, identifier in enumerate(("left", "right"), 1)]
        data["connections"] = []
        design = Design.from_dict(data)
        report = check_design(design, require_physical_ports=True)
        self.assertTrue(report.blocking)
        self.assertIn("connection.output_conflict", issues(report))
        self.assertNotIn("connection.required_unconnected", issues(report))
        self.assertTrue(report.metadata["physical_ports"]["complete"])

    def test_hidden_shared_rail_demands_are_summed_without_connection_edges(self):
        design = rail_design()
        source, first = design.root.children
        first.ports[0].current_draw_ma = 60
        second = copy.deepcopy(first)
        second.id = "second"
        second.components[0].reference = "U3"
        second.ports[0].current_draw_ma = 50
        design.root.children.append(second)
        design.connections = []
        for block in (source, first, second):
            for port in block.ports:
                port.required = False
        report = check_design(design, require_physical_ports=True)
        self.assertIn("power.budget_exceeded", issues(report))
        self.assertEqual(report.metadata["power_budgets"][0]["total_current_ma"], 110)
        second.ports[0].current_draw_ma = 40
        report = check_design(design, require_physical_ports=True)
        self.assertFalse(report.blocking, report.to_markdown())
        self.assertEqual(report.metadata["power_budgets"][0]["total_current_ma"], 100)

    def test_required_logical_edges_remain_required_even_for_realized_global_nets(self):
        design = rail_design()
        design.connections = []
        report = check_design(design, require_physical_ports=True)
        self.assertTrue(report.metadata["physical_ports"]["complete"])
        self.assertIn("connection.required_unconnected", issues(report))
        self.assertTrue(report.blocking)

    def test_physical_source_joins_an_abstract_branch_with_the_same_global_edge_label(self):
        design = rail_design()
        source, first = design.root.children
        first.ports[0].net, first.ports[0].pin_bindings = None, []
        first.ports[0].current_draw_ma = 60
        second = copy.deepcopy(first)
        second.id = "second"
        second.components[0].reference = "U3"
        second.ports[0].current_draw_ma = 50
        design.root.children.append(second)
        for block in (source, first, second):
            for port in block.ports:
                port.required = False
        # No edge names the physical source port. The shared global name still
        # makes these two declared loads part of its physical rail budget.
        design.connections = [Connection("board.load.vcc", "board.second.vcc", "3V3")]
        report = check_design(design)
        self.assertIn("power.budget_exceeded", issues(report))
        self.assertNotIn("power.no_source", issues(report))
        self.assertEqual(len(report.metadata["power_budgets"]), 1)
        self.assertEqual(report.metadata["power_budgets"][0]["total_current_ma"], 110)
        design.root.ports.append(Port("relay", "power", "passive", required=False))
        design.connections.append(Connection("board.second.vcc", "board.relay", "alias"))
        self.assertIn("physical_ports.net_conflict", issues(check_design(design)))

    def test_transitive_alias_through_abstract_relay_cannot_rewrite_physical_nets(self):
        design = rail_design()
        design.root.ports.append(Port("relay", "power", "passive", required=False))
        design.connections[0] = Connection("board.supply.out", "board.relay", "3V3")
        design.connections.append(Connection("board.relay", "board.load.vcc", "VBUS"))
        report = check_design(design)
        self.assertIn("physical_ports.net_conflict", issues(report))
        self.assertTrue(report.blocking)
        self.assertEqual(dict(design.walk_blocks())["board.load"].components[0].pins["1"], "3V3")

    def test_fully_abstract_transitive_aliases_retain_legacy_draft_behavior(self):
        design = rail_design()
        for _, block in design.walk_blocks():
            for port in block.ports:
                port.net, port.pin_bindings = None, []
        design.root.ports.append(Port("relay", "power", "passive", required=False))
        design.connections[0] = Connection("board.supply.out", "board.relay", "3V3")
        design.connections.append(Connection("board.relay", "board.load.vcc", "alias"))
        report = check_design(design)
        self.assertNotIn("physical_ports.net_conflict", issues(report))
        self.assertFalse(report.blocking, report.to_markdown())
        self.assertFalse(report.metadata["physical_ports"]["complete"])

    def test_edge_typo_is_not_fuzzily_normalized_to_the_declared_physical_net(self):
        design = rail_design()
        design.connections[0].net = "3v3"
        report = check_design(design)
        self.assertIn("physical_ports.net_conflict", issues(report))
        self.assertTrue(report.blocking)

    def test_empty_interface_surface_has_no_global_coverage_claim(self):
        design = rail_design()
        for _, block in design.walk_blocks():
            block.ports = []
        design.connections = []
        report = check_physical_ports(design, require_physical_ports=True)
        self.assertFalse(report.blocking)
        self.assertEqual(report.metadata["coverage"]["status"], "not_applicable")
        self.assertFalse(report.metadata["coverage"]["complete"])

    def test_strict_option_rejects_non_boolean_values(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_design(rail_design(), require_physical_ports=value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_physical_ports(rail_design(), require_physical_ports=value)


if __name__ == "__main__":
    unittest.main()
