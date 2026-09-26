"""The refusal contract: four outcomes, rendered identically in every client.

    ESTABLISHED       Class A/B evidence; safe to put in a signed document
    INTERPRETED       Class C; labelled as interpretation; an engineer must confirm
    CANNOT_DETERMINE  the evidence does not establish it (and says what would)
    DEGRADED          the capability ran in a reduced mode

Every tool result, every final answer and every artefact carries one of these.
DEGRADED exists so that a user never has to infer from the *quality* of an
answer that the system was running without its sandbox's network namespace, or
reading a scan below its calibrated OCR threshold, or reading a raster drawing
that cannot establish connectivity.
"""
from __future__ import annotations

from typing import Any, Iterable

ESTABLISHED = "ESTABLISHED"
INTERPRETED = "INTERPRETED"
CANNOT_DETERMINE = "CANNOT_DETERMINE"
DEGRADED = "DEGRADED"

OUTCOMES = (ESTABLISHED, INTERPRETED, CANNOT_DETERMINE, DEGRADED)

LABELS = {ESTABLISHED: "ESTABLISHED", INTERPRETED: "INTERPRETED",
          CANNOT_DETERMINE: "CANNOT DETERMINE", DEGRADED: "DEGRADED"}

# Evidence class -> outcome. Class D is a refusal, not a weak establishment.
FROM_CLASS = {"A": ESTABLISHED, "B": ESTABLISHED, "C": INTERPRETED,
              "D": CANNOT_DETERMINE}

# How bad each outcome is, for picking the headline of a mixed set. DEGRADED
# outranks the rest: a reduced mode taints everything produced under it.
_SEVERITY = {ESTABLISHED: 0, INTERPRETED: 1, CANNOT_DETERMINE: 2, DEGRADED: 3}


def check(outcome: str) -> str:
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}; the contract has {OUTCOMES}")
    return outcome


def worst(outcomes: Iterable[str]) -> str:
    items = [o for o in outcomes if o in _SEVERITY]
    return max(items, key=_SEVERITY.__getitem__) if items else ESTABLISHED


def summarise(tool_outcomes: list[dict[str, Any]],
              class_counts: dict[str, int] | None = None) -> dict[str, Any]:
    """The outcome block attached to a final answer.

    `headline` is not a verdict on every sentence — the per-value classes are.
    It is what the answer must be *read as*: an answer produced while any
    capability was degraded is read as degraded, whatever it says.
    """
    counts = {o: 0 for o in OUTCOMES}
    degraded: list[str] = []
    for t in tool_outcomes:
        o = t.get("outcome")
        if o in counts:
            counts[o] += 1
        if o == DEGRADED and t.get("reason"):
            r = f"{t.get('tool', 'tool')}: {t['reason']}"
            if r not in degraded:
                degraded.append(r)
    values = {o: 0 for o in OUTCOMES}
    for cls, n in (class_counts or {}).items():
        if cls in FROM_CLASS:
            values[FROM_CLASS[cls]] += n
    if degraded:
        headline = DEGRADED
    elif values[CANNOT_DETERMINE]:
        headline = CANNOT_DETERMINE
    elif values[INTERPRETED]:
        headline = INTERPRETED
    else:
        headline = ESTABLISHED
    return {"headline": headline, "label": LABELS[headline],
            "tool_outcomes": counts, "value_outcomes": values,
            "degraded": degraded}
