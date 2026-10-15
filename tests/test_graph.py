import unittest

from pcb_dev.graph import dependency_order, dependents
from pcb_dev.model import Block, Board, Design, ModelError


def design():
    return Design("graph", "0.1.0", Board(20, 20), Block("board", children=[
        Block("mcu", depends_on=["board.clock", "board.power"]),
        Block("clock", depends_on=["board.power"]),
        Block("power"), Block("unrelated"),
        Block("led", depends_on=["board.mcu"]),
    ]))


class GraphTests(unittest.TestCase):
    def test_order_places_dependencies_first_and_is_deterministic(self):
        project = design()
        ordered = dependency_order(project)
        self.assertEqual(ordered, ["board", "board.power", "board.clock", "board.mcu", "board.led", "board.unrelated"])
        project.root.children.reverse()
        self.assertEqual(dependency_order(project), ordered)

    def test_dependents_are_transitive_and_ordered(self):
        self.assertEqual(dependents(design(), "board.power"), ["board.clock", "board.mcu", "board.led"])
        self.assertEqual(dependents(design(), "board.mcu"), ["board.led"])
        self.assertEqual(dependents(design(), "board.unrelated"), [])

    def test_unknown_queries_and_dependencies_are_clear(self):
        with self.assertRaisesRegex(ModelError, "unknown block path 'missing'"):
            dependents(design(), "missing")
        project = design()
        project.root.children[0].depends_on.append("board.missing")
        with self.assertRaisesRegex(ModelError, "board.mcu: unknown dependency 'board.missing'"):
            dependency_order(project)

    def test_cycle_lists_actual_cycle(self):
        project = design()
        project.root.children[2].depends_on = ["board.clock"]
        with self.assertRaisesRegex(ModelError, r"dependency cycle: board.clock -> board.power -> board.clock"):
            dependency_order(project)

    def test_self_cycle(self):
        project = design()
        project.root.depends_on = ["board"]
        with self.assertRaisesRegex(ModelError, r"board -> board"):
            dependency_order(project)

    def test_empty_root_is_valid_and_ancestors_are_not_implicit_dependencies(self):
        project = Design("empty", "0.1.0", Board(1, 1), Block("board"))
        self.assertEqual(dependency_order(project), ["board"])
        project.root.children.append(Block("child"))
        project.root.depends_on.append("board.child")
        self.assertEqual(dependency_order(project), ["board.child", "board"])

    def test_parser_checks_graph(self):
        data = design().to_dict()
        data["root"]["children"][0]["depends_on"] = ["board.nonexistent"]
        with self.assertRaisesRegex(ModelError, "unknown dependency"):
            Design.from_dict(data)
        data = design().to_dict()
        data["root"]["children"][0]["depends_on"] *= 2
        with self.assertRaisesRegex(ModelError, "duplicate dependency"):
            Design.from_dict(data)


if __name__ == "__main__":
    unittest.main()
