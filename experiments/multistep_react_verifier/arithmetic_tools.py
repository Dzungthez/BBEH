from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

WORD_TO_INT = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

ATOM_OPS = [
    "[]",
    "][",
    "<>",
    "><",
    "+",
    "-",
    "*",
    "!",
    "#",
    "&",
    ":",
    ";",
    "@",
    "~",
]
ATOM_OPS_SORTED = sorted(ATOM_OPS, key=len, reverse=True)
SYMBOL_CHARS = set("!#&:;@~+-*<>[]")


@dataclass
class OperatorDef:
    symbol: str
    true_expr: str
    condition: str
    false_expr: str


@dataclass
class ArithmeticProblem:
    raw_question: str
    operator_defs: dict[str, OperatorDef]
    expr_a: str
    expr_b: str
    expr_c: str
    final_expr: str


@dataclass
class Token:
    typ: str
    val: str


class ParseError(Exception):
    pass


class MultiStepArithmeticEngine:
    def __init__(self, problem: ArithmeticProblem):
        self.problem = problem

    @classmethod
    def from_question(cls, question: str) -> "MultiStepArithmeticEngine":
        return cls(parse_problem(question))

    def compute_named_values(self) -> dict[str, int]:
        values: dict[str, int] = {}
        values["A"] = self.evaluate_expression(self.problem.expr_a, values)
        values["B"] = self.evaluate_expression(self.problem.expr_b, values)
        values["C"] = self.evaluate_expression(self.problem.expr_c, values)
        values["FINAL"] = self.evaluate_expression(self.problem.final_expr, values)
        return values

    def get_problem_summary(self) -> dict[str, Any]:
        values = self.compute_named_values()
        return {
            "operators": {
                k: {
                    "condition": v.condition,
                    "true_expr": v.true_expr,
                    "false_expr": v.false_expr,
                }
                for k, v in sorted(self.problem.operator_defs.items())
            },
            "expr_a": self.problem.expr_a,
            "expr_b": self.problem.expr_b,
            "expr_c": self.problem.expr_c,
            "final_expr": self.problem.final_expr,
            "values": values,
        }

    def evaluate_expression(self, expr: str, env: dict[str, int] | None = None) -> int:
        env = dict(env or {})
        normalized = normalize_expr(expr)
        tokens = tokenize(normalized)
        parser = ExpressionParser(tokens)
        node = parser.parse_expression(0)
        parser.expect("EOF")
        return self._eval_node(node, env)

    def check_equation(self, equation: str, env: dict[str, int] | None = None) -> dict[str, Any]:
        env = dict(env or {})
        if "=" not in equation:
            raise ValueError(f"Equation must contain '=': {equation}")
        left, right = equation.split("=", 1)
        left_val = self.evaluate_expression(left.strip(), env)
        right_val = self.evaluate_expression(right.strip(), env)
        return {
            "equation": equation,
            "left_value": left_val,
            "right_value": right_val,
            "is_equal": left_val == right_val,
        }

    def _eval_node(self, node: Any, env: dict[str, int]) -> int:
        kind = node[0]
        if kind == "num":
            return int(node[1])
        if kind == "id":
            name = node[1]
            if name in env:
                return int(env[name])
            lname = name.lower()
            if lname in env:
                return int(env[lname])
            if lname in WORD_TO_INT:
                return WORD_TO_INT[lname]
            raise ParseError(f"Unknown identifier: {name}")
        if kind == "call":
            fname = node[1].lower()
            args = [self._eval_node(a, env) for a in node[2]]
            if fname == "min":
                return min(args)
            if fname == "max":
                return max(args)
            if fname == "gcd":
                if len(args) != 2:
                    raise ParseError("gcd expects 2 arguments")
                return math.gcd(args[0], args[1])
            if fname == "abs":
                if len(args) != 1:
                    raise ParseError("abs expects 1 argument")
                return abs(args[0])
            raise ParseError(f"Unsupported function call: {node[1]}")
        if kind == "unary":
            op = node[1]
            val = self._eval_node(node[2], env)
            if op == "+":
                return val
            if op == "-":
                return -val
            raise ParseError(f"Unsupported unary operator: {op}")
        if kind == "bin":
            op_chunk = node[1]
            left = self._eval_node(node[2], env)
            right = self._eval_node(node[3], env)
            return self._apply_operator_chunk(op_chunk, left, right)
        raise ParseError(f"Unknown AST node kind: {kind}")

    @lru_cache(maxsize=None)
    def _split_operator_chunk(self, chunk: str) -> tuple[str, ...]:
        out: list[str] = []
        s = chunk
        while s:
            matched = None
            for op in ATOM_OPS_SORTED:
                if s.startswith(op):
                    matched = op
                    break
            if matched is None:
                raise ParseError(f"Unknown operator chunk: {chunk}")
            out.append(matched)
            s = s[len(matched):]
        return tuple(out)

    def _apply_operator_chunk(self, chunk: str, left: int, right: int) -> int:
        ops = self._split_operator_chunk(chunk)
        result = left
        for op in ops:
            result = self._apply_atomic_operator(op, result, right)
        return result

    def _apply_atomic_operator(self, op: str, a: int, b: int) -> int:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b

        if op not in self.problem.operator_defs:
            raise ParseError(f"Undefined custom operator: {op}")

        d = self.problem.operator_defs[op]
        local_env = {"a": a, "b": b}
        cond = self._evaluate_condition(d.condition, local_env)
        chosen = d.true_expr if cond else d.false_expr
        return self.evaluate_expression(chosen, local_env)

    def _evaluate_condition(self, cond: str, env: dict[str, int]) -> bool:
        c = normalize_expr(cond).strip().lower()
        if c == "either a or b is prime":
            return is_prime(env["a"]) or is_prime(env["b"])

        cmp_match = re.match(r"^(.+)\s(==|>=|<=|>|<)\s(.+)$", c)
        if cmp_match:
            left = cmp_match.group(1).strip()
            cmp_op = cmp_match.group(2)
            right = cmp_match.group(3).strip()
            left_val = self.evaluate_expression(left, env)
            right_val = self.evaluate_expression(right, env)
            if cmp_op == "==":
                return left_val == right_val
            if cmp_op == ">=":
                return left_val >= right_val
            if cmp_op == "<=":
                return left_val <= right_val
            if cmp_op == ">":
                return left_val > right_val
            if cmp_op == "<":
                return left_val < right_val

        raise ParseError(f"Unsupported condition format: {cond}")


class ExpressionParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def current(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, typ: str, val: str | None = None) -> Token:
        tok = self.current()
        if tok.typ != typ:
            raise ParseError(f"Expected token type {typ}, got {tok.typ} ({tok.val})")
        if val is not None and tok.val != val:
            raise ParseError(f"Expected token {val}, got {tok.val}")
        return self.advance()

    def parse_expression(self, min_prec: int) -> Any:
        node = self.parse_unary()
        while True:
            tok = self.current()
            if tok.typ != "OP":
                break
            prec = precedence(tok.val)
            if prec < min_prec:
                break
            op = tok.val
            self.advance()
            rhs = self.parse_expression(prec + 1)
            node = ("bin", op, node, rhs)
        return node

    def parse_unary(self) -> Any:
        tok = self.current()
        if tok.typ == "OP" and tok.val in {"+", "-"}:
            op = tok.val
            self.advance()
            return ("unary", op, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> Any:
        tok = self.current()
        if tok.typ == "NUM":
            self.advance()
            return ("num", int(tok.val))
        if tok.typ == "ID":
            self.advance()
            name = tok.val
            if self.current().typ == "LPAREN":
                self.advance()
                args: list[Any] = []
                if self.current().typ != "RPAREN":
                    while True:
                        args.append(self.parse_expression(0))
                        if self.current().typ == "COMMA":
                            self.advance()
                            continue
                        break
                self.expect("RPAREN")
                return ("call", name, args)
            return ("id", name)
        if tok.typ == "LPAREN":
            self.advance()
            node = self.parse_expression(0)
            self.expect("RPAREN")
            return node
        raise ParseError(f"Unexpected token: {tok.typ} ({tok.val})")


def precedence(op_chunk: str) -> int:
    if op_chunk == "*":
        return 20
    if op_chunk in {"+", "-"}:
        return 10
    return 15


def tokenize(expr: str) -> list[Token]:
    out: list[Token] = []
    i = 0
    n = len(expr)

    while i < n:
        ch = expr[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit():
            j = i + 1
            while j < n and expr[j].isdigit():
                j += 1
            out.append(Token("NUM", expr[i:j]))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < n and (expr[j].isalnum() or expr[j] == "_"):
                j += 1
            out.append(Token("ID", expr[i:j]))
            i = j
            continue
        if ch == "(":
            out.append(Token("LPAREN", ch))
            i += 1
            continue
        if ch == ")":
            out.append(Token("RPAREN", ch))
            i += 1
            continue
        if ch == ",":
            out.append(Token("COMMA", ch))
            i += 1
            continue
        if ch in SYMBOL_CHARS:
            j = i + 1
            while j < n and expr[j] in SYMBOL_CHARS:
                j += 1
            out.append(Token("OP", expr[i:j]))
            i = j
            continue
        raise ParseError(f"Cannot tokenize character: {ch!r} in {expr!r}")

    out.append(Token("EOF", ""))
    return out


def normalize_expr(text: str) -> str:
    s = text.strip()
    s = s.replace("$", "")
    s = s.replace("−", "-")
    s = s.replace("\u2212", "-")
    s = re.sub(r",\s*where\s+gcd\s+stands\s+for\s+greatest\s+common\s+divisor\.?", "", s, flags=re.IGNORECASE)
    s = replace_abs_bars(s)
    s = s.strip()
    if s.endswith("."):
        s = s[:-1].strip()
    return s


def replace_abs_bars(text: str) -> str:
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = re.sub(r"\|([^|]+)\|", r"abs(\1)", cur)
    return cur


def is_prime(x: int) -> bool:
    if x <= 1:
        return False
    if x <= 3:
        return True
    if x % 2 == 0 or x % 3 == 0:
        return False
    i = 5
    while i * i <= x:
        if x % i == 0 or x % (i + 2) == 0:
            return False
        i += 6
    return True


def parse_problem(question: str) -> ArithmeticProblem:
    op_defs: dict[str, OperatorDef] = {}
    expr_a = expr_b = expr_c = final_expr = None

    for raw_line in question.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m_op = re.match(r"^\$a\s+(.+?)\s+b\$\s+equals\s+(.+)$", line)
        if m_op:
            symbol = m_op.group(1).strip()
            rhs = m_op.group(2).strip()
            true_expr, condition, false_expr = parse_operator_rhs(rhs)
            op_defs[symbol] = OperatorDef(
                symbol=symbol,
                true_expr=true_expr,
                condition=condition,
                false_expr=false_expr,
            )
            continue

        m_let = re.match(r"^Let\s+([ABC])\s*=\s*(.+)$", line)
        if m_let:
            name = m_let.group(1)
            expr = normalize_expr(m_let.group(2))
            if name == "A":
                expr_a = expr
            elif name == "B":
                expr_b = expr
            elif name == "C":
                expr_c = expr
            continue

        if line.startswith("Compute "):
            chunk = line[len("Compute ") :]
            final_expr = normalize_expr(chunk.split(".", 1)[0])

    if not op_defs:
        raise ParseError("Could not find operator definitions")
    if expr_a is None or expr_b is None or expr_c is None:
        raise ParseError("Could not find all of Let A/B/C expressions")
    if final_expr is None:
        raise ParseError("Could not find final expression after 'Compute'")

    return ArithmeticProblem(
        raw_question=question,
        operator_defs=op_defs,
        expr_a=expr_a,
        expr_b=expr_b,
        expr_c=expr_c,
        final_expr=final_expr,
    )


def parse_operator_rhs(rhs: str) -> tuple[str, str, str]:
    s = normalize_expr(rhs)
    if " if " not in s:
        raise ParseError(f"Operator RHS missing 'if': {rhs}")

    true_expr, after_if = s.split(" if ", 1)
    true_expr = normalize_expr(true_expr)

    if "; otherwise" in after_if:
        condition, after = after_if.split("; otherwise", 1)
    elif " otherwise" in after_if and " and " in after_if:
        condition, after = after_if.rsplit(" and ", 1)
    else:
        raise ParseError(f"Unsupported operator RHS format: {rhs}")

    false_expr = after.strip()
    false_expr = re.sub(r"^[,\s]+", "", false_expr)
    false_expr = re.sub(r"^otherwise\s*,?\s*", "", false_expr, flags=re.IGNORECASE)
    false_expr = re.sub(r"^it\s+equals\s+", "", false_expr, flags=re.IGNORECASE)
    false_expr = re.sub(r"\s*otherwise\s*$", "", false_expr, flags=re.IGNORECASE)

    return normalize_expr(true_expr), normalize_expr(condition), normalize_expr(false_expr)


def validate_engine_on_multistep_dataset(task_path: str | Path) -> dict[str, Any]:
    task_path = Path(task_path)
    data = json.loads(task_path.read_text())
    examples = data["examples"]

    mismatches = []
    for idx, ex in enumerate(examples):
        engine = MultiStepArithmeticEngine.from_question(ex["input"])
        values = engine.compute_named_values()
        pred = values["FINAL"]
        target = int(ex["target"])
        if pred != target:
            mismatches.append({"index": idx, "pred": pred, "target": target})

    return {
        "total": len(examples),
        "correct": len(examples) - len(mismatches),
        "accuracy": (len(examples) - len(mismatches)) / len(examples),
        "mismatches": mismatches,
    }
