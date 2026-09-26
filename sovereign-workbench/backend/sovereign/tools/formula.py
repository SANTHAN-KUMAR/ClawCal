"""A small, safe evaluator for spreadsheet formulas.

openpyxl reads and writes formulas but never computes them, so without this the
agent could write `=B4-B5` into a workbook and have no way to know what it
evaluates to. LibreOffice would compute everything, but it is a 600 MB
dependency an air-gapped appliance may not carry.

This evaluates the subset engineering registers actually use: arithmetic,
comparison, concatenation, cell and range references across sheets, and the
common functions below. Anything outside that subset is *not guessed*: the
cell evaluates to `Unsupported`, which the tool reports as CANNOT DETERMINE.
No `eval`, no Python builtins: the formula is tokenised and parsed here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Callable


class ExcelError(str):
    """An Excel error value (#DIV/0!, #REF!, ...). Propagates through arithmetic."""


DIV0, REF, NAME, VALUE, NA = (ExcelError(e) for e in
                              ("#DIV/0!", "#REF!", "#NAME?", "#VALUE!", "#N/A"))


@dataclass
class Unsupported:
    reason: str

    def __str__(self) -> str:
        return f"UNSUPPORTED({self.reason})"


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"]|"")*")
  | (?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z_][A-Za-z0-9_.]*)!)?\$?[A-Za-z]{1,3}\$?\d+
            (?::\$?[A-Za-z]{1,3}\$?\d+)?(?![A-Za-z0-9_(]))
  | (?P<num>\d+(?:\.\d*)?(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?)
  | (?P<func>[A-Za-z_][A-Za-z0-9_.]*(?=\())
  | (?P<bool>TRUE|FALSE)\b
  | (?P<op><>|<=|>=|[-+*/^&=<>%(),:])
""", re.X | re.I)

_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")


def col_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def split_ref(ref: str, default_sheet: str) -> tuple[str, str]:
    if "!" in ref:
        sheet, cell = ref.rsplit("!", 1)
        if sheet.startswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
        return sheet, cell
    return default_sheet, ref


def tokenize(src: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise SyntaxError(f"cannot read formula at {src[pos:pos + 12]!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        out.append((kind, m.group(kind)))
    return out


# ------------------------------------------------------------------- parser

_BINARY = {"=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1, "&": 2,
           "+": 3, "-": 3, "*": 4, "/": 4, "^": 5}


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.t, self.i = tokens, 0

    def peek(self) -> tuple[str, str] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise SyntaxError("formula ended unexpectedly")
        self.i += 1
        return tok

    def expect(self, value: str) -> None:
        tok = self.take()
        if tok[1] != value:
            raise SyntaxError(f"expected {value!r}, found {tok[1]!r}")

    def parse(self) -> Any:
        node = self.expr(0)
        if self.peek() is not None:
            raise SyntaxError(f"unexpected {self.peek()[1]!r}")
        return node

    def expr(self, min_prec: int) -> Any:
        left = self.unary()
        while True:
            tok = self.peek()
            if not tok or tok[0] != "op" or tok[1] not in _BINARY:
                return left
            prec = _BINARY[tok[1]]
            if prec < min_prec:
                return left
            self.take()
            # ^ is right-associative; everything else is left-associative.
            right = self.expr(prec if tok[1] == "^" else prec + 1)
            left = ("bin", tok[1], left, right)

    def unary(self) -> Any:
        tok = self.peek()
        if tok and tok[1] in ("-", "+"):
            self.take()
            return ("neg", self.unary()) if tok[1] == "-" else self.unary()
        node = self.atom()
        while self.peek() and self.peek()[1] == "%":
            self.take()
            node = ("bin", "/", node, ("num", 100.0))
        return node

    def atom(self) -> Any:
        kind, val = self.take()
        if kind == "num":
            return ("num", float(val))
        if kind == "str":
            return ("str", val[1:-1].replace('""', '"'))
        if kind == "bool":
            return ("bool", val.upper() == "TRUE")
        if kind == "ref":
            return ("ref", val)
        if kind == "func":
            self.expect("(")
            args = []
            if self.peek() and self.peek()[1] != ")":
                args.append(self.expr(0))
                while self.peek() and self.peek()[1] == ",":
                    self.take()
                    args.append(self.expr(0))
            self.expect(")")
            return ("call", val.upper(), args)
        if val == "(":
            node = self.expr(0)
            self.expect(")")
            return node
        raise SyntaxError(f"unexpected {val!r}")


# ---------------------------------------------------------------- evaluation

def _num(v: Any) -> Any:
    if isinstance(v, (ExcelError, Unsupported)):
        return v
    if isinstance(v, bool):
        return float(v)
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return VALUE


def _flat(values: list[Any]) -> list[Any]:
    out = []
    for v in values:
        out.extend(v if isinstance(v, list) else [v])
    return out


def _nums(args: list[Any]) -> list[Any]:
    """Numbers in the arguments, as Excel's SUM sees them (text in ranges ignored)."""
    out = []
    for v in _flat(args):
        if isinstance(v, (ExcelError, Unsupported)):
            return [v]
        if isinstance(v, bool):
            out.append(float(v))
        elif isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _agg(fn: Callable[[list[float]], Any], empty: Any = 0.0):
    def run(args: list[Any]) -> Any:
        xs = _nums(args)
        if xs and isinstance(xs[0], (ExcelError, Unsupported)):
            return xs[0]
        return fn(xs) if xs else empty
    return run


def _round(mode: str):
    def run(args: list[Any]) -> Any:
        x = _num(args[0]) if args else VALUE
        d = _num(args[1]) if len(args) > 1 else 0.0
        if isinstance(x, (ExcelError, Unsupported)):
            return x
        if isinstance(d, (ExcelError, Unsupported)):
            return d
        f = 10 ** int(d)
        if mode == "round":      # Excel rounds half away from zero
            return math.copysign(math.floor(abs(x) * f + 0.5), x) / f
        if mode == "up":
            return math.copysign(math.ceil(abs(x) * f), x) / f
        return math.copysign(math.floor(abs(x) * f), x) / f
    return run


def _unary(fn: Callable[[float], float], domain: Callable[[float], bool] = lambda x: True):
    def run(args: list[Any]) -> Any:
        if len(args) != 1:
            return VALUE
        x = _num(args[0])
        if isinstance(x, (ExcelError, Unsupported)):
            return x
        return fn(x) if domain(x) else ExcelError("#NUM!")
    return run


def _if(args: list[Any]) -> Any:
    if not args:
        return VALUE
    c = args[0]
    if isinstance(c, (ExcelError, Unsupported)):
        return c
    truth = bool(_num(c)) if not isinstance(c, str) else bool(c)
    if truth:
        return args[1] if len(args) > 1 else True
    return args[2] if len(args) > 2 else False


def _iferror(args: list[Any]) -> Any:
    if len(args) != 2:
        return VALUE
    return args[1] if isinstance(args[0], ExcelError) else args[0]


FUNCTIONS: dict[str, Callable[[list[Any]], Any]] = {
    "SUM": _agg(sum),
    "AVERAGE": _agg(lambda xs: sum(xs) / len(xs), DIV0),
    "MIN": _agg(min), "MAX": _agg(max),
    "COUNT": lambda a: float(len([v for v in _flat(a)
                                  if isinstance(v, (int, float)) and not isinstance(v, bool)])),
    "COUNTA": lambda a: float(len([v for v in _flat(a) if v not in (None, "")])),
    "ROUND": _round("round"), "ROUNDUP": _round("up"), "ROUNDDOWN": _round("down"),
    "ABS": _unary(abs), "INT": _unary(lambda x: float(math.floor(x))),
    "SQRT": _unary(math.sqrt, lambda x: x >= 0),
    "LN": _unary(math.log, lambda x: x > 0),
    "LOG10": _unary(math.log10, lambda x: x > 0),
    "EXP": _unary(math.exp),
    "PI": lambda a: math.pi,
    "POWER": lambda a: _binop("^", a[0], a[1]) if len(a) == 2 else VALUE,
    "MOD": lambda a: (_num(a[0]) % _num(a[1]) if len(a) == 2 and _num(a[1])
                      else DIV0),
    "IF": _if, "IFERROR": _iferror,
    "AND": lambda a: all(bool(_num(v)) for v in _flat(a)),
    "OR": lambda a: any(bool(_num(v)) for v in _flat(a)),
    "NOT": lambda a: not bool(_num(a[0])) if a else VALUE,
    "CONCATENATE": lambda a: "".join(_text(v) for v in _flat(a)),
    "CONCAT": lambda a: "".join(_text(v) for v in _flat(a)),
    "LEN": lambda a: float(len(_text(a[0]))) if a else VALUE,
    "UPPER": lambda a: _text(a[0]).upper() if a else VALUE,
    "LOWER": lambda a: _text(a[0]).lower() if a else VALUE,
}


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _binop(op: str, a: Any, b: Any) -> Any:
    for v in (a, b):
        if isinstance(v, (ExcelError, Unsupported)):
            return v
    if op == "&":
        return _text(a) + _text(b)
    if op in ("=", "<>", "<", ">", "<=", ">="):
        if isinstance(a, str) or isinstance(b, str):
            x, y = _text(a).lower(), _text(b).lower()
        else:
            x, y = _num(a), _num(b)
        return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y,
                "<=": x <= y, ">=": x >= y}[op]
    x, y = _num(a), _num(b)
    for v in (x, y):
        if isinstance(v, (ExcelError, Unsupported)):
            return v
    if op == "+":
        return x + y
    if op == "-":
        return x - y
    if op == "*":
        return x * y
    if op == "/":
        return DIV0 if y == 0 else x / y
    if op == "^":
        try:
            return float(x ** y)
        except (OverflowError, ValueError, ZeroDivisionError):
            return ExcelError("#NUM!")
    return VALUE


class Workbook:
    """Evaluates formulas against a grid: {sheet: {"A1": raw value or "=..."}}."""

    MAX_DEPTH = 200

    def __init__(self, grid: dict[str, dict[str, Any]]) -> None:
        self.grid = grid
        self.cache: dict[tuple[str, str], Any] = {}
        self._active: set[tuple[str, str]] = set()

    def value(self, sheet: str, cell: str) -> Any:
        cell = cell.replace("$", "").upper()
        key = (sheet, cell)
        if key in self.cache:
            return self.cache[key]
        if sheet not in self.grid:
            return REF
        raw = self.grid[sheet].get(cell)
        if isinstance(raw, str) and raw.startswith("="):
            if key in self._active:
                return Unsupported(f"circular reference at {sheet}!{cell}")
            if len(self._active) > self.MAX_DEPTH:
                return Unsupported("dependency chain too deep")
            self._active.add(key)
            try:
                result = self.evaluate(raw, sheet)
            finally:
                self._active.discard(key)
        else:
            result = raw
        self.cache[key] = result
        return result

    def evaluate(self, formula: str, sheet: str) -> Any:
        src = formula[1:] if formula.startswith("=") else formula
        try:
            ast = _Parser(tokenize(src)).parse()
        except SyntaxError as exc:
            return Unsupported(f"cannot parse: {exc}")
        return self._eval(ast, sheet)

    def _range(self, ref: str, sheet: str) -> list[Any]:
        sh, span = split_ref(ref, sheet)
        a, b = span.split(":")
        ma, mb = _CELL.match(a), _CELL.match(b)
        if not ma or not mb:
            return [REF]
        c0, c1 = sorted((col_index(ma.group(1)), col_index(mb.group(1))))
        r0, r1 = sorted((int(ma.group(2)), int(mb.group(2))))
        if (c1 - c0 + 1) * (r1 - r0 + 1) > 200_000:
            return [Unsupported("range too large to evaluate")]
        return [self.value(sh, f"{col_letters(c)}{r}")
                for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]

    def _eval(self, node: Any, sheet: str) -> Any:
        kind = node[0]
        if kind in ("num", "str", "bool"):
            return node[1]
        if kind == "ref":
            if ":" in node[1]:
                return self._range(node[1], sheet)
            sh, cell = split_ref(node[1], sheet)
            return self.value(sh, cell)
        if kind == "neg":
            v = _num(self._eval(node[1], sheet))
            return v if isinstance(v, (ExcelError, Unsupported)) else -v
        if kind == "bin":
            return _binop(node[1], self._eval(node[2], sheet), self._eval(node[3], sheet))
        if kind == "call":
            fn = FUNCTIONS.get(node[1])
            if fn is None:
                return Unsupported(f"function {node[1]} is not supported by the "
                                   f"appliance's formula evaluator")
            args = [self._eval(a, sheet) for a in node[2]]
            for a in _flat(args):
                if isinstance(a, Unsupported):
                    return a
            try:
                return fn(args)
            except (TypeError, ValueError, IndexError, ZeroDivisionError):
                return VALUE
        return Unsupported(f"unknown node {kind}")


def references(formula: str) -> list[str]:
    """Cell and range references a formula reads, for evidence bookkeeping."""
    try:
        return [v for k, v in tokenize(formula.lstrip("=")) if k == "ref"]
    except SyntaxError:
        return []
