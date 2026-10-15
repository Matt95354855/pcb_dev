import math
import unittest
from unittest.mock import patch

from pcb_dev.parity import ParityError, parse_sexpr
from pcb_dev.sexpr import Quoted, SExprError, dumps, parse_quoted


def lexical_shape(value):
    if type(value) is list:
        return [lexical_shape(item) for item in value]
    return ("quoted" if isinstance(value, Quoted) else "bare", str(value))


class SExprTests(unittest.TestCase):
    def test_quoted_numeric_enum_and_empty_string_atoms_remain_distinct(self):
        tree = parse_quoted('(property "1" 1 "yes" yes "")')
        self.assertIs(type(tree[0]), str)
        self.assertIs(type(tree[1]), Quoted)
        self.assertIs(type(tree[2]), str)
        self.assertIs(type(tree[3]), Quoted)
        self.assertIs(type(tree[4]), str)
        self.assertIs(type(tree[5]), Quoted)
        self.assertEqual(lexical_shape(parse_quoted(dumps(tree))), lexical_shape(tree))

    def test_actual_kicad_definition_roundtrips_bare_fields_and_quoted_names(self):
        source = '''(symbol "Device:R" (in_bom yes) (on_board yes)
            (property "Reference" "R" (at 0 0 90) (effects (font (size 1.27 1.27))))
            (symbol "R_1_1" (pin passive line (at 0 3.81 270) (length 1.27)
              (name "~" (effects (font (size 1.27 1.27)))) (number "1"))))'''
        tree = parse_quoted(source)
        serialized = dumps(tree)
        self.assertIn("(pin passive line", serialized)
        self.assertIn('(number "1")', serialized)
        self.assertIn("(in_bom yes)", serialized)
        self.assertEqual(lexical_shape(parse_quoted(serialized)), lexical_shape(tree))
        self.assertEqual(serialized, dumps(parse_quoted(serialized)))

    def test_quoted_parentheses_comments_unicode_and_supported_escapes(self):
        value = 'électrique (pad); "quote" \\ path\nnext\rreturn\ttab'
        serialized = dumps(["data", Quoted(value)])
        self.assertEqual(parse_quoted(serialized)[1], value)
        self.assertIs(type(parse_quoted(serialized)[1]), Quoted)
        for escape in ("\\n", "\\r", "\\t", "\\\\", '\\"'):
            self.assertIn(escape, serialized)

    def test_comments_and_whitespace_are_canonicalized_without_changing_atoms(self):
        tree = parse_quoted('; leading\n (a bare; inline\n "quoted;data" (b 1e-06 -0.000)) ; trailing')
        self.assertEqual(tree[1:3], ["bare", "quoted;data"])
        self.assertEqual(tree[3][1:], ["1e-06", "-0.000"])
        self.assertEqual(lexical_shape(parse_quoted(dumps(tree))), lexical_shape(tree))

    def test_default_parity_parser_remains_plain_strings(self):
        ordinary = parse_sexpr('(a "quoted" (b "1"))')
        self.assertIs(type(ordinary[1]), str)
        self.assertIs(type(ordinary[2][1]), str)
        quoted = parse_sexpr('(a "quoted" (b "1"))', preserve_quotes=True)
        self.assertIs(type(quoted[1]), Quoted)
        self.assertIs(type(quoted[2][1]), Quoted)
        with self.assertRaises(ParityError):
            parse_sexpr('(a "unterminated)', preserve_quotes=True)

    def test_empty_balanced_expression_and_programmatic_numbers(self):
        self.assertEqual(parse_quoted("()"), [])
        self.assertEqual(dumps([]), "()")
        self.assertEqual(parse_quoted(dumps(["at", 1, -0.5])), ["at", "1", "-0.5"])

    def test_malformed_ambiguous_and_unsafe_syntax_is_rejected(self):
        for text in ("(a", "(a))", "(a)(b)", "bare", '"quoted"', '(a "bad\\x")',
                     '(a "unterminated)', '(a "truncated\\', '(a bare"quote")',
                     '(a "one""two")', '(a "one"two)', '(a bare\\path)', '(a \x00)', '(a "\x00")'):
            with self.subTest(text=repr(text)), self.assertRaises(SExprError):
                parse_quoted(text)

    def test_invalid_unicode_and_input_types_are_rejected(self):
        for value in (None, b"(a)", ["a"], '(a "\ud800")'):
            with self.subTest(value=repr(value)), self.assertRaises(SExprError):
                parse_quoted(value)
        with self.assertRaises(SExprError):
            dumps(["a", Quoted("\ud800")])

    def test_unquoted_strings_cannot_inject_new_syntax(self):
        for value in ("", "a b", "a\nb", '(pad "1")', 'a"b', "a;b", "a\\b"):
            with self.subTest(value=value), self.assertRaises(SExprError):
                dumps(["data", value])
            self.assertEqual(parse_quoted(dumps(["data", Quoted(value)]))[1], value)

    def test_objects_booleans_and_nonfinite_numbers_are_not_serialized(self):
        for value in (True, False, None, math.nan, math.inf, -math.inf, {}, (1, 2), object()):
            with self.subTest(value=repr(value)), self.assertRaises(SExprError):
                dumps(["data", value])
        with self.assertRaises(SExprError):
            dumps(("a", "b"))

    def test_byte_token_depth_and_indent_limits_are_symmetric(self):
        with patch("pcb_dev.sexpr.MAX_BYTES", 5):
            with self.assertRaises(SExprError):
                parse_quoted('(a "long")')
            with self.assertRaises(SExprError):
                dumps(["a", Quoted("long")])
        with patch("pcb_dev.sexpr.MAX_TOKENS", 2):
            with self.assertRaises(SExprError):
                parse_quoted("(a)")
            with self.assertRaises(SExprError):
                dumps(["a"])
        with patch("pcb_dev.sexpr.MAX_TOKENS", 3):
            self.assertEqual(parse_quoted(dumps(["a"])), ["a"])
        with self.assertRaises(SExprError):
            parse_quoted("(" * 129 + ")" * 129)
        tree = ["leaf"]
        for _ in range(128):
            tree = ["node", tree]
        with self.assertRaises(SExprError):
            dumps(tree)
        for indent in (-1, 9, True, 1.0):
            with self.subTest(indent=indent), self.assertRaises(SExprError):
                dumps(["a"], indent=indent)

    def test_cycles_are_rejected_but_shared_acyclic_lists_are_supported(self):
        cyclic = ["root"]
        cyclic.append(cyclic)
        with self.assertRaisesRegex(SExprError, "cyclic"):
            dumps(cyclic)
        leaf = ["at", "1", "2"]
        self.assertEqual(parse_quoted(dumps(["root", leaf, leaf])), ["root", leaf, leaf])


if __name__ == "__main__":
    unittest.main()
