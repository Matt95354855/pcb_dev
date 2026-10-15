import unittest

from pcb_dev.checks import check_design
from pcb_dev.model import Block, Board, Component, Connection, Constraint, Design, Port, freeze_fingerprint


def power_source(name="supply", capacity=100):
    return Block(name, ports=[Port("out", "power", "output", 3.2, 3.4, capacity)])


def power_load(name="load", draw=40):
    return Block(name, ports=[Port("vcc", "power", "input", 3.0, 3.6, current_draw_ma=draw)])


def design(children, connections=()):
    return Design("test", "0.1.0", Board(40, 30), Block("board", children=list(children)), list(connections))


def codes(report):
    return {issue.code for issue in report.issues}


class ElectricalChecksTests(unittest.TestCase):
    def test_valid_rail_has_exact_hash_and_budget(self):
        board = design([power_source(), power_load()], [Connection("board.supply.out", "board.load.vcc", "3V3")])
        report = check_design(board)
        self.assertFalse(report.blocking, report.to_markdown())
        self.assertTrue(report.metadata["completed"])
        self.assertFalse(report.metadata["native"])
        self.assertEqual(report.metadata["design_hash"], board.fingerprint())
        self.assertEqual(report.metadata["power_budgets"][0]["total_current_ma"], 40)

    def test_shared_transitive_rail_is_summed_once_per_load(self):
        board = design([power_source(capacity=100), power_load("a", 60), power_load("b", 50)], [
            Connection("board.supply.out", "board.a.vcc", "3V3"),
            Connection("board.a.vcc", "board.b.vcc", "rail_alias"),
            Connection("board.supply.out", "board.b.vcc", "3V3"),
        ])
        report = check_design(board)
        self.assertIn("power.budget_exceeded", codes(report))
        self.assertEqual(len(report.metadata["power_budgets"]), 1)
        self.assertEqual(report.metadata["power_budgets"][0]["total_current_ma"], 110)

    def test_identical_global_net_labels_join_otherwise_disjoint_edges(self):
        bridge = Block("bridge", ports=[Port("p", "power", "passive", required=False)])
        board = design([power_source(capacity=100), power_load("a", 60), power_load("b", 50), bridge], [
            Connection("board.supply.out", "board.a.vcc", "3V3"),
            Connection("board.bridge.p", "board.b.vcc", "3V3"),
        ])
        report = check_design(board)
        self.assertIn("power.budget_exceeded", codes(report))
        self.assertEqual(report.metadata["power_budgets"][0]["total_current_ma"], 110)

    def test_voltage_uses_worst_case_containment_not_range_overlap(self):
        source, load = power_source(), power_load()
        source.ports[0].voltage_max = 3.8
        board = design([source, load], [Connection("board.supply.out", "board.load.vcc", "3V3")])
        self.assertIn("voltage.incompatible", codes(check_design(board)))

    def test_missing_power_limits_and_unpowered_loads_block(self):
        source, load = power_source(), power_load()
        source.ports[0].voltage_max = None
        source.ports[0].max_current_ma = None
        load.ports[0].voltage_min = None
        board = design([source, load], [Connection("board.supply.out", "board.load.vcc", "3V3")])
        self.assertTrue({"power.voltage_undeclared", "power.capacity_undeclared", "power.load_voltage_undeclared"}.issubset(codes(check_design(board))))
        board = design([power_load("a"), power_load("b")], [Connection("board.a.vcc", "board.b.vcc", "VCC")])
        self.assertIn("power.no_source", codes(check_design(board)))

    def test_multiple_sources_are_not_summed_as_parallel_capacity(self):
        board = design([power_source("a", 80), power_source("b", 80), power_load(draw=100)], [
            Connection("board.a.out", "board.load.vcc", "VCC"),
            Connection("board.b.out", "board.load.vcc", "VCC"),
        ])
        report = check_design(board)
        self.assertIn("connection.output_conflict", codes(report))
        self.assertEqual(sum(issue.code == "power.budget_exceeded" for issue in report.issues), 2)

    def test_signal_metadata_mismatches_are_all_reported(self):
        a = Block("a", ports=[Port("io", "digital", "output", 0, 5, logic_standard="TTL", protocol="SPI", polarity="positive")])
        b = Block("b", ports=[Port("io", "analog", "input", 0, 3.3, logic_standard="CMOS", protocol="UART", polarity="negative")])
        report = check_design(design([a, b], [Connection("board.a.io", "board.b.io", "DATA")]))
        self.assertTrue({"connection.kind_mismatch", "signal.logic_mismatch", "connection.protocol_mismatch", "connection.polarity_mismatch", "voltage.incompatible"}.issubset(codes(report)))

    def test_required_ports_no_driver_and_self_connections(self):
        a = Block("a", ports=[Port("missing", "digital", "input"), Port("optional", "digital", "input", required=False), Port("in", "digital", "input")])
        b = Block("b", ports=[Port("in", "digital", "input")])
        board = design([a, b], [Connection("board.a.in", "board.b.in", "FLOAT")])
        report = check_design(board)
        self.assertIn("connection.no_driver", codes(report))
        unconnected = [issue.block_path for issue in report.issues if issue.code == "connection.required_unconnected"]
        self.assertEqual(unconnected, ["board.a.missing"])
        board.connections.append(Connection("board.a.optional", "board.a.optional", "SELF"))
        self.assertIn("connection.self_connection", codes(check_design(board)))

    def test_bidirectional_bus_does_not_require_push_pull_output(self):
        a = Block("a", ports=[Port("bus", "bus", "bidirectional", 0, 3.3, protocol="I2C")])
        b = Block("b", ports=[Port("bus", "bus", "bidirectional", 0, 3.3, protocol="i2c")])
        report = check_design(design([a, b], [Connection("board.a.bus", "board.b.bus", "I2C")]))
        self.assertFalse(report.blocking, report.to_markdown())

    def test_differential_impedance_mismatch(self):
        a = Block("a", ports=[Port("pair", "differential", "output", impedance_ohm=90)])
        b = Block("b", ports=[Port("pair", "differential", "input", impedance_ohm=120)])
        self.assertIn("signal.impedance_mismatch", codes(check_design(design([a, b], [Connection("board.a.pair", "board.b.pair", "USB")]))))

    def test_symbolic_polarities_are_not_erased_as_punctuation(self):
        a = Block("a", ports=[Port("pair", "differential", "output", polarity="+")])
        b = Block("b", ports=[Port("pair", "differential", "input", polarity="-")])
        board = design([a, b], [Connection("board.a.pair", "board.b.pair", "PAIR")])
        self.assertIn("connection.polarity_mismatch", codes(check_design(board)))
        b.ports[0].polarity = "positive"
        self.assertNotIn("connection.polarity_mismatch", codes(check_design(board)))

    def test_manufacturer_planning_data_does_not_claim_supplier_validation(self):
        report = check_design(design([]))
        self.assertIn("manufacturer.rules_unverified", codes(report))
        self.assertFalse(report.metadata["manufacturer_rules_verified"])

    def test_missing_endpoint_and_mutated_invalid_current_fail_closed(self):
        board = design([power_source(), power_load()], [Connection("board.supply.out", "board.unknown", "VCC")])
        self.assertIn("connection.missing_endpoint", codes(check_design(board)))
        board.root.children[0].ports[0].max_current_ma = -1
        self.assertIn("power.invalid_current", codes(check_design(board)))
        board.root.children[0].ports[0].max_current_ma = float("nan")
        report = check_design(board)
        self.assertTrue(report.blocking)
        self.assertFalse(report.metadata["completed"])
        self.assertIn("preflight.incomplete", codes(report))

    def test_i2c_collisions_resolve_connected_bus_paths(self):
        a = Block("a", ports=[Port("bus", "bus", "bidirectional")], resources={"i2c": [{"bus": "board.a.bus", "address": "0x48"}]})
        b = Block("b", ports=[Port("bus", "bus", "bidirectional")], resources={"i2c": [{"bus": "board.b.bus", "address": 72}]})
        board = design([a, b], [Connection("board.a.bus", "board.b.bus", "I2C")])
        self.assertIn("resource.i2c_collision", codes(check_design(board)))
        b.resources["i2c"][0]["bus"] = "other_bus"
        self.assertNotIn("resource.i2c_collision", codes(check_design(board)))

    def test_i2c_invalid_reserved_and_boolean_addresses(self):
        for address in (0x02, 0x78, True, "not_hex", 1.5):
            with self.subTest(address=address):
                block = Block("device", resources={"i2c": [{"bus": "I2C", "address": address}]})
                self.assertIn("resource.invalid_i2c", codes(check_design(design([block]))))

    def test_dependency_and_power_sequence_cycles(self):
        a = Block("a", depends_on=["board.b"], resources={"power_sequence": ["board.b"]})
        b = Block("b", depends_on=["board.a"], resources={"power_sequence": ["board.a"]})
        report = check_design(design([a, b]))
        self.assertIn("dependency.cycle", codes(report))
        self.assertIn("power.sequence_cycle", codes(report))

    def test_power_sequence_delays_and_missing_predecessors(self):
        block = Block("device", resources={"power_sequence": {"after": ["board.missing"], "delay_ms": 5, "required_delay_ms": 10}})
        report = check_design(design([block]))
        self.assertIn("power.sequence_missing", codes(report))
        self.assertIn("power.sequence_delay", codes(report))

    def test_constraints_and_invalid_equations_never_succeed_silently(self):
        block = Block("load", parameters={"current": 11, "limit": 10}, constraints=[Constraint("current <= limit", "Current exceeds limit")])
        board = design([block])
        self.assertIn("constraint.failed", codes(check_design(board)))
        block.constraints = [Constraint("__import__('os')", "Never execute")]
        self.assertIn("constraint.invalid", codes(check_design(board)))
        block.equations = {"unsafe": "1 / 0"}
        self.assertIn("expression.invalid", codes(check_design(board)))

    def test_component_derated_limits_and_unfitted_components(self):
        component = Component("cap", "C1", "10u", "C_0805", {"1": "VCC", "2": "GND"},
                              parameters={"voltage_rating_v": 10, "applied_voltage_v": 8, "derating_factor": 0.5})
        board = design([Block("filter", components=[component])])
        self.assertIn("component.rating_exceeded", codes(check_design(board)))
        component.fitted = False
        self.assertNotIn("component.rating_exceeded", codes(check_design(board)))
        component.fitted = True
        component.parameters["derating_factor"] = True
        self.assertIn("component.invalid_derating", codes(check_design(board)))

    def test_component_rating_metadata_does_not_claim_undeclared_measurement(self):
        component = Component("cap", "C1", "10u", "C_0805", {}, parameters={"voltage_rating_v": 10})
        report = check_design(design([Block("filter", components=[component])]))
        self.assertIn("component.incomplete_rating", codes(report))
        self.assertEqual(report.metadata["component_rating_checks"], 0)

    def test_frozen_dependency_inherited_parameter_and_intrinsic_changes(self):
        source = Block("source", parameters={"voltage": 3.3})
        load = Block("load", depends_on=["board.source"])
        board = design([source, load])
        board.root.parameters["ambient"] = 25
        load.frozen = True
        self.assertIn("frozen.unrecorded", codes(check_design(board)))
        load.frozen_hash = freeze_fingerprint(board, "board.load")
        self.assertNotIn("frozen.invalidated", codes(check_design(board)))
        source.parameters["voltage"] = 5
        self.assertIn("frozen.invalidated", codes(check_design(board)))
        load.frozen_hash = freeze_fingerprint(board, "board.load")
        board.root.parameters["ambient"] = 60
        self.assertIn("frozen.invalidated", codes(check_design(board)))
        load.frozen_hash = freeze_fingerprint(board, "board.load")
        load.description = "Changed intrinsic content"
        self.assertIn("frozen.invalidated", codes(check_design(board)))

    def test_unexpected_checker_exception_blocks_with_incomplete_status(self):
        board = design([])
        board.root.resources = None
        report = check_design(board)
        self.assertTrue(report.blocking)
        self.assertFalse(report.metadata["completed"])
        self.assertIn("preflight.incomplete", codes(report))


if __name__ == "__main__":
    unittest.main()
