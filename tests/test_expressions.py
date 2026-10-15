import math
import unittest

from pcb_dev.expressions import (
    ExpressionError, evaluate_constraint, evaluate_constraints, evaluate_expression,
    referenced_names, resolve_parameters,
)
from pcb_dev.model import Block, Board, Constraint, Design


class ExpressionTests(unittest.TestCase):
    def test_electrical_equations_and_functions(self):
        self.assertAlmostEqual(evaluate_expression("vin * r2 / (r1 + r2)", {"vin": 5, "r1": 1000, "r2": 2000}), 10 / 3)
        self.assertAlmostEqual(evaluate_expression("1 / (2 * pi * resistance * capacitance)",
                                                  {"resistance": 1000, "capacitance": 1e-6}), 159.154943, places=5)
        self.assertEqual(evaluate_expression("max(1, abs(-5), sqrt(16)) + min(1, 3)", {}), 6)
        self.assertEqual(evaluate_expression("round(1 / 3, 2)", {}), .33)
        self.assertEqual(evaluate_expression("2 ** 3 + 7 // 2 + 7 % 2 - -1", {}), 13)
        self.assertEqual(evaluate_expression("pi", {"pi": 3}), 3)
        self.assertAlmostEqual(evaluate_expression("e", {}), math.e)

    def test_constraint_compound_comparisons_and_short_circuit(self):
        self.assertTrue(evaluate_constraint("2.9 <= voltage <= 3.6 and current < 100", {"voltage": 3.3, "current": 50}))
        self.assertFalse(evaluate_constraint("voltage == 5 or current != 50", {"voltage": 3.3, "current": 50}))
        self.assertTrue(evaluate_constraint("not (voltage > 4)", {"voltage": 3.3}))
        self.assertTrue(evaluate_constraint("current == 0 or 1 / current > 1", {"current": 0}))
        self.assertFalse(evaluate_constraint("current != 0 and 1 / current > 1", {"current": 0}))

    def test_boolean_values_are_not_numbers_and_numeric_truthiness_is_forbidden(self):
        for expression, values in (("value + 1", {"value": True}), ("True + 1", {}), ("False", {})):
            with self.subTest(expression=expression), self.assertRaises(ExpressionError):
                evaluate_expression(expression, values)
        for expression in ("1", "1 and 2", "not 1", "True", "1 == True", "(1 < 2) + 1"):
            with self.subTest(expression=expression), self.assertRaises(ExpressionError):
                evaluate_constraint(expression, {})

    def test_unsafe_syntax_is_rejected_without_execution(self):
        expressions = [
            "__import__('os').system('touch /tmp/pcb-dev-unsafe')", "x.__class__", "x[0]",
            "[x for x in (1, 2)]", "(lambda: 1)()", "open('file')", "sqrt.__call__(1)",
            "(x := 1)", "{1: 2}", "[1, 2]", "'1'", "1 if 1 else 2", "1 << 2",
            "sqrt(x=4)", "min(*[1, 2])", "f'{x}'", "__class__", "1 in (1,2)",
        ]
        for expression in expressions:
            with self.subTest(expression=expression), self.assertRaises(ExpressionError):
                evaluate_expression(expression, {"x": 1})
        with self.assertRaises(ExpressionError):
            evaluate_constraint("1 < 2 or __import__('os')", {})

    def test_invalid_arithmetic_unknown_names_and_overflow(self):
        for expression, values in (("missing + 1", {}), ("1 / 0", {}), ("sqrt(-1)", {}),
                                   ("1e309", {}), ("1e308 * 100", {}), ("10 ** 1000000000", {}),
                                   ("(-1) ** 0.5", {}), ("round(1.2, 1000000000)", {}),
                                   ("min()", {}), ("sqrt(1, 2)", {}), ("round(1, 2.0)", {}),
                                   ("x + 1", {"x": float("nan")}), ("x + 1", {"x": "3"}),
                                   ("x + 1", {"x": 10 ** 1000}), ("1 +", {}), ("", {})):
            with self.subTest(expression=expression), self.assertRaises(ExpressionError):
                evaluate_expression(expression, values)

    def test_expression_resource_bounds(self):
        for expression in ("1+" * 3000 + "1", "1+" * 200 + "1", "-" * 40 + "1"):
            with self.subTest(length=len(expression)), self.assertRaises(ExpressionError):
                evaluate_expression(expression, {})

    def test_referenced_names_excludes_call_targets(self):
        self.assertEqual(referenced_names("sqrt(a) + min(b, c) * pi"), {"a", "b", "c", "pi"})
        self.assertEqual(referenced_names("a < b and b <= c", constraint=True), {"a", "b", "c"})

    def test_parameter_inheritance_overrides_and_forward_equations(self):
        root = Block("board", parameters={"vin": 5, "load_ma": 20, "note": "metadata", "nested": [1]},
                     equations={"power_mw": "vin * load_ma"}, children=[
            Block("child", parameters={"load_ma": 40}, equations={"result": "double + 1", "double": "load_ma * 2"},
                  children=[Block("grandchild", equations={"total": "result + power_mw"})]),
            Block("sibling"),
        ])
        project = Design("parameters", "0.1.0", Board(20, 20), root)
        values = resolve_parameters(project)
        self.assertEqual(values["board"]["power_mw"], 100)
        self.assertEqual(values["board.child"]["result"], 81)
        self.assertEqual(values["board.child.grandchild"]["total"], 181)
        self.assertEqual(values["board.sibling"]["load_ma"], 20)
        values["board.child"]["nested"].append(2)
        self.assertEqual(values["board"]["nested"], [1])
        self.assertEqual(root.parameters["nested"], [1])
        self.assertNotIn("power_mw", root.parameters)

    def test_equations_override_supplied_defaults(self):
        project = Design("test", "0.1.0", Board(1, 1),
                         Block("board", parameters={"x": 10, "y": 100}, equations={"y": "x * 2"}))
        self.assertEqual(resolve_parameters(project)["board"]["y"], 20)

    def test_equation_cycles_are_clear(self):
        project = Design("test", "0.1.0", Board(1, 1),
                         Block("board", equations={"a": "b + 1", "b": "a + 1", "victim": "a"}))
        with self.assertRaisesRegex(ExpressionError, r"board: equation dependency cycle: a -> b -> a"):
            resolve_parameters(project)
        project.root.equations = {"a": "a + 1"}
        with self.assertRaisesRegex(ExpressionError, r"a -> a"):
            resolve_parameters(project)

    def test_errors_identify_equation_and_block(self):
        project = Design("test", "0.1.0", Board(1, 1),
                         Block("board", children=[Block("child", equations={"power": "voltage * 2"})]))
        with self.assertRaisesRegex(ExpressionError, r"board.child.equations.power: unknown parameter 'voltage'"):
            resolve_parameters(project)

    def test_constraints_use_resolved_values(self):
        project = Design("test", "0.1.0", Board(1, 1), Block(
            "board", parameters={"v": 3.3}, equations={"mv": "v * 1000"}, constraints=[
                Constraint("3000 < mv < 3600", "supply in range"),
                Constraint("v > 5", "intentional failure"),
            ]))
        self.assertEqual(evaluate_constraints(project), {"board": [True, False]})
        project.root.constraints.append(Constraint("unknown < 1", "bad"))
        with self.assertRaisesRegex(ExpressionError, r"board.constraints\[2\]"):
            evaluate_constraints(project)


if __name__ == "__main__":
    unittest.main()
