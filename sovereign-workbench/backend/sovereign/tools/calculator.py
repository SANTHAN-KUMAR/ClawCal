"""Deterministic calculator with a reproducible trace.

Every derived number in a deliverable must be reproducible from its inputs. This
tool evaluates a restricted arithmetic grammar with a hand-written AST walker --
never `eval` -- and records the expression, the bound inputs, the substituted
form and the result. The `calculations` row it writes is what makes a Class B
evidence claim checkable.
"""
from __future__ import annotations

import ast
import math
import operator
import re
import time
from typing import Any

from .. import db
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_FUNCS: dict[str, Any] = {
    "abs": abs, "min": min, "max": max, "round": round,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "pi": math.pi,
    "floor": math.floor, "ceil": math.ceil,
}


class CalcError(ValueError):
    pass


def _eval(node: ast.AST, env: dict[str, float], steps: list[str]) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body, env, steps)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise CalcError(f"only numeric constants are allowed, got {node.value!r}")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in env:
            return float(env[node.id])
        if node.id in _FUNCS and not callable(_FUNCS[node.id]):
            return float(_FUNCS[node.id])
        raise CalcError(f"unknown symbol {node.id!r}; bind it in `inputs` first")
    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise CalcError(f"operator {type(node.op).__name__} is not permitted")
        left, right = _eval(node.left, env, steps), _eval(node.right, env, steps)
        if op in (operator.truediv, operator.floordiv, operator.mod) and right == 0:
            raise CalcError("division by zero")
        val = op(left, right)
        steps.append(f"{left:g} {_symbol(node.op)} {right:g} = {val:g}")
        return float(val)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY.get(type(node.op))
        if op is None:
            raise CalcError("unary operator not permitted")
        return float(op(_eval(node.operand, env, steps)))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise CalcError("only the whitelisted mathematical functions may be called")
        fn = _FUNCS[node.func.id]
        if not callable(fn):
            raise CalcError(f"{node.func.id} is a constant, not a function")
        argv = [_eval(a, env, steps) for a in node.args]
        val = float(fn(*argv))
        steps.append(f"{node.func.id}({', '.join(f'{a:g}' for a in argv)}) = {val:g}")
        return val
    raise CalcError(f"expression element {type(node).__name__} is not permitted")


def _symbol(op: ast.operator) -> str:
    return {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
            ast.Pow: "**", ast.Mod: "%", ast.FloorDiv: "//"}[type(op)]


# A value written as "10.4 mm (IR-2026-0731, page 1)" leads with its number; a
# pure citation like "IR-2026-0731 p.1" does not. Anchoring at the start is what
# separates the two, since a citation is full of digits that are not the value.
_LEADING_NUM = re.compile(r"^[-+]?\d+(?:\.\d+)?")


def coerce_inputs(raw: Any, provenance: dict[str, Any] | None = None
                  ) -> tuple[dict[str, float], dict[str, Any], list[str]]:
    """Separate numeric bindings from provenance that arrived in the wrong field.

    Models routinely put the *source* of a value into `inputs` -- either as a
    citation string or as a nested object -- because the two arguments are
    adjacent and both keyed by variable name. Hard-failing on that produces a
    retry loop in which the model changes everything except the mistake. So
    non-numeric bindings are moved to provenance, a number is recovered from
    them where one is present, and the correction is reported back.
    """
    values: dict[str, float] = {}
    prov: dict[str, Any] = dict(provenance or {})
    notes: list[str] = []

    for key, val in (raw or {}).items():
        name = str(key)
        if isinstance(val, bool):
            notes.append(f"{name!r} was a boolean and was ignored")
            continue
        if isinstance(val, (int, float)):
            values[name] = float(val)
            continue
        if isinstance(val, dict):
            # e.g. {"design_pressure": {"value": 16, "source": "IR p.1"}}
            num = next((v for v in val.values()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)),
                       None)
            src = next((v for v in val.values() if isinstance(v, str)), None)
            if num is not None:
                values[name] = float(num)
            if src:
                prov.setdefault(name, src)
            notes.append(f"{name!r} was an object; its numeric value was used and "
                         f"its text recorded as provenance")
            continue
        if isinstance(val, str):
            text = val.strip()
            try:
                values[name] = float(text)
                continue
            except ValueError:
                pass
            lead = _LEADING_NUM.match(text)
            if lead:
                values[name] = float(lead.group(0))
                prov.setdefault(name, text)
                notes.append(f"{name!r} was the string {text!r}; the leading "
                             f"number {lead.group(0)} was used as the value and "
                             f"the full text recorded as provenance")
            else:
                prov.setdefault(name, text)
                notes.append(f"{name!r} was not a number ({text[:60]!r}); it was "
                             f"recorded as provenance, not as a value")
            continue
        notes.append(f"{name!r} had an unusable type and was ignored")
    return values, prov, notes


def verify_inputs(values: dict[str, float], task_id: str | None
                  ) -> tuple[dict[str, bool], list[str]]:
    """Check every calculator input against what the evidence establishes.

    Without this, the calculator is a laundering machine: a model that invents a
    wall thickness and divides by it gets a result stamped DERIVED, which the
    provenance layer then treats as trustworthy. An input is acceptable only if
    it appears in a retrieved source passage or is the result of an earlier
    verified calculation -- the same standard applied to any other number.
    """
    if not task_id:
        return {k: True for k in values}, []

    established: set[str] = set()
    for row in db.query("SELECT snippet FROM evidence WHERE task_id=?", (task_id,)):
        for m in re.finditer(r"\d+(?:\.\d+)?", row["snippet"] or ""):
            established.add(f"{float(m.group(0)):g}")
    for row in db.query("SELECT result FROM calculations WHERE task_id=? AND ok=1",
                        (task_id,)):
        if row["result"] is not None:
            try:
                established.add(f"{float(row['result']):g}")
            except (TypeError, ValueError):
                pass

    verdicts: dict[str, bool] = {}
    unverified: list[str] = []
    for name, val in values.items():
        ok = f"{val:g}" in established
        verdicts[name] = ok
        if not ok:
            unverified.append(f"{name}={val:g}")
    return verdicts, unverified


def evaluate(expression: str, inputs: dict[str, float] | None = None, *,
             label: str = "", unit: str = "", task_id: str | None = None,
             input_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    inputs, input_provenance, coercion_notes = coerce_inputs(
        inputs, input_provenance)
    steps: list[str] = []
    calc_id = db.new_id("CALC")
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree, inputs, steps)
        # Binary floating point turns 1.2/3 into 0.40000000000000036, which then
        # fails to match the "0.4" a document states and would be classified as
        # an unsupported number. Round to a precision no engineering value needs
        # beyond.
        result = round(result, 9)
        ok, error = True, ""
    except CalcError as exc:
        result, ok, error = None, False, str(exc)
    except SyntaxError as exc:
        result, ok, error = None, False, f"could not parse expression: {exc.msg}"
    except (ValueError, OverflowError, ZeroDivisionError) as exc:
        result, ok, error = None, False, f"{type(exc).__name__}: {exc}"

    substituted = expression
    for k, v in sorted(inputs.items(), key=lambda kv: -len(kv[0])):
        substituted = substituted.replace(k, f"{v:g}")

    # Literal numbers written straight into the expression are inputs too, and
    # are the easiest place to smuggle an invented value.
    literals = {f"literal[{i}]": float(m)
                for i, m in enumerate(re.findall(r"(?<![\w.])\d+(?:\.\d+)?",
                                                 expression))}
    verified, unverified = verify_inputs({**inputs, **literals}, task_id)
    inputs_verified = all(verified.values()) if verified else True

    db.insert("calculations", {
        "id": calc_id, "task_id": task_id, "label": label or expression[:80],
        "expression": expression, "inputs": db.jdump(inputs),
        "input_prov": db.jdump(input_provenance or {}),
        "result": None if result is None else f"{result:g}", "unit": unit,
        "steps": db.jdump({"substituted": substituted, "steps": steps,
                           "inputs_verified": inputs_verified,
                           "unverified_inputs": unverified}),
        "ok": int(ok), "error": error, "created_at": time.time()})

    return {"id": calc_id, "label": label, "expression": expression,
            "inputs": inputs, "substituted": substituted, "steps": steps,
            "result": result, "unit": unit, "ok": ok, "error": error,
            "input_provenance": input_provenance or {},
            "notes": coercion_notes, "inputs_verified": inputs_verified,
            "unverified_inputs": unverified}


class CalculatorTool(Tool):
    name = "calculator"
    risk = Risk.READ_ONLY
    description = (
        "Evaluate an arithmetic expression deterministically and record a "
        "reproducible calculation trace. Use this for EVERY number you derive; "
        "numbers you compute mentally cannot be cited and will be rejected. "
        "Bind named inputs so the trace records where each value came from.")
    parameters = {
        "type": "object",
        "properties": {
            "expression": {"type": "string",
                           "description": "arithmetic only, e.g. "
                                          "'design_pressure - operating_pressure'"},
            "inputs": {"type": "object",
                       "description": "name -> NUMBER only, e.g. "
                                      "{\"design_pressure\": 16, "
                                      "\"operating_pressure\": 12}. Never put "
                                      "a document reference here; that belongs "
                                      "in input_provenance."},
            "label": {"type": "string", "description": "what this calculation is"},
            "unit": {"type": "string", "description": "unit of the result, e.g. 'bar'"},
            "input_provenance": {
                "type": "object",
                "description": "name -> where the value came from, e.g. "
                               "{'design_pressure': 'IR-2026-0731 page 1'}"},
        },
        "required": ["expression"],
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        res = evaluate(
            str(args.get("expression", "")),
            args.get("inputs") or {},
            label=str(args.get("label", "")),
            unit=str(args.get("unit", "")),
            task_id=ctx.task_id,
            input_provenance=args.get("input_provenance") or {},
        )
        if not res["ok"]:
            hint = ("\n\nCorrect form:\n"
                    '{"expression": "design_pressure - operating_pressure", '
                    '"inputs": {"design_pressure": 16, "operating_pressure": 12}, '
                    '"label": "operating margin", "unit": "bar", '
                    '"input_provenance": {"design_pressure": "IR-2026-0731 p.1", '
                    '"operating_pressure": "IR-2026-0731 p.1"}}\n'
                    "`inputs` holds numbers. `input_provenance` holds the "
                    "document and page each number came from.")
            return ToolResult(False, error=res["error"] + hint,
                              meta={"calc_id": res["id"]})
        display = (f"{res['id']}: {res['expression']} = {res['result']:g} "
                   f"{res['unit']}".strip())
        ctx.note("calculation", display,
                 " | ".join(res["steps"]) if res["steps"] else res["substituted"],
                 res)
        content = {
            "calculation_id": res["id"], "result": res["result"],
            "unit": res["unit"], "substituted": res["substituted"],
            "steps": res["steps"]}
        if res["notes"]:
            content["argument_corrections"] = res["notes"]
        if res["unverified_inputs"]:
            content["WARNING"] = (
                f"These inputs do not appear in any passage you have retrieved, "
                f"and are not the result of an earlier calculation: "
                f"{', '.join(res['unverified_inputs'])}. This result will NOT "
                f"count as a derived value while that is true. Retrieve the "
                f"passage that states each of them, check you have the right "
                f"figure, and recompute. Do not proceed on a number you cannot "
                f"cite.")
        return ToolResult(True, content=content, display=display,
                          meta={"calc_id": res["id"]})


def task_calculations(task_id: str) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM calculations WHERE task_id=? ORDER BY created_at",
                    (task_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["inputs"] = db.jload(d["inputs"], {})
        d["steps"] = db.jload(d["steps"], {})
        d["input_prov"] = db.jload(d["input_prov"], {})
        try:
            d["result"] = float(d["result"]) if d["result"] is not None else None
        except (TypeError, ValueError):
            pass
        out.append(d)
    return out
