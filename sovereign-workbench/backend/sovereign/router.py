"""Task classification and capability/residency-aware model selection.

Two things separate this from a keyword-to-model lookup table.

1. Selection is *capability* matching, not name matching. A task declares the
   capability profile it needs; every model advertises what it offers; the router
   scores fit. Adding a model later changes the outcome without changing code.

2. Selection is a **residency decision**, not a cost decision. On one mid-range
   GPU the scarce resources are VRAM and wall-clock, so the router charges each
   candidate the measured cost of making it resident, and prefers an already-loaded
   model whenever its capability fit is close enough. This is the difference
   between a workbench that thrashes and one that does not -- see docs/scheduling.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import db
from .gateway.registry import ModelCard, registry

# ---------------------------------------------------------------------------
# Task taxonomy. Each entry declares the capability floor a task of this type
# needs, plus the axes that actually matter for ranking.
# ---------------------------------------------------------------------------


@dataclass
class TaskProfile:
    """A task type's capability demand.

    `needs` are hard floors: a model below any of them is not a candidate at any
    speed. This is where domain judgement belongs — at BATCH priority the score
    weights throughput heavily, and without a floor a 3B vision model wins the
    summarisation of confidential inspection reports because it is quick.
    `weights` then rank the models that clear the floors.
    """
    task_type: str
    needs: dict[str, float]              # capability floors
    weights: dict[str, float]            # ranking emphasis
    default_priority: str = "MEDIUM"
    est_context_tokens: int = 4096
    reasoning: str = "medium"
    description: str = ""


TASK_TYPES: dict[str, TaskProfile] = {
    "document_extraction": TaskProfile(
        "document_extraction",
        needs={"text": 0.85, "extraction": 0.7},
        weights={"extraction": 1.0, "structured": 0.7, "speed": 0.5, "long_context": 0.5},
        est_context_tokens=7000, reasoning="low",
        description="Pull structured findings out of a document with page references.",
    ),
    "knowledge_qa": TaskProfile(
        "knowledge_qa",
        needs={"text": 0.85, "extraction": 0.5},
        weights={"reasoning": 0.7, "extraction": 0.8, "speed": 0.7, "long_context": 0.6},
        est_context_tokens=5500, reasoning="low",
        description="Answer from the organisation's own SOPs and manuals.",
    ),
    "engineering_analysis": TaskProfile(
        "engineering_analysis",
        needs={"text": 0.9, "reasoning": 0.75},
        weights={"reasoning": 1.0, "structured": 0.6, "extraction": 0.5},
        default_priority="HIGH", est_context_tokens=6500, reasoning="high",
        description="Compare measured values against limits and judge compliance.",
    ),
    "deliverable_drafting": TaskProfile(
        "deliverable_drafting",
        needs={"text": 0.9, "structured": 0.6},
        weights={"reasoning": 0.8, "structured": 0.9, "long_context": 0.6},
        est_context_tokens=7500, reasoning="medium",
        description="Draft an approval note, report or presentation from evidence.",
    ),
    "coding": TaskProfile(
        "coding",
        needs={"text": 0.85, "coding": 0.7},
        weights={"coding": 1.0, "reasoning": 0.6, "structured": 0.5},
        est_context_tokens=7000, reasoning="medium",
        description="Write, repair or test internal code in the sandbox.",
    ),
    "calculation": TaskProfile(
        "calculation",
        needs={"text": 0.85, "reasoning": 0.6},
        weights={"reasoning": 0.9, "structured": 0.8, "speed": 0.6},
        est_context_tokens=4000, reasoning="medium",
        description="Perform an engineering calculation with a reproducible trace.",
    ),
    "vision_understanding": TaskProfile(
        "vision_understanding",
        needs={"vision": 0.4},
        weights={"vision": 1.0, "extraction": 0.7, "speed": 0.5},
        est_context_tokens=5000, reasoning="low",
        description="Read a scanned page, photograph or handwritten note.",
    ),
    "drawing_analysis": TaskProfile(
        "drawing_analysis",
        # Deliberately no vision floor. Drawing understanding is done by the
        # geometric pipeline inside `analyse_drawing`, which reads the vector
        # geometry directly; the vision model is used inside that tool only as a
        # cross-check on the title block. The agent's job here is to report a
        # structured result faithfully and preserve its confidence labels, which
        # is text work — routing it to a small VLM produced the answer
        # "V-204 is connected to vessel V-204".
        needs={"text": 0.85, "structured": 0.6},
        weights={"structured": 1.0, "extraction": 0.8, "reasoning": 0.7},
        est_context_tokens=6000, reasoning="low",
        description="Interpret an engineering drawing: symbols, tags, connectivity.",
    ),
    "summarisation": TaskProfile(
        "summarisation",
        # A digest of confidential inspection reports is text work. The floor
        # excludes the small vision model, which is fast enough to win on score
        # at BATCH priority but is not a writer.
        needs={"text": 0.85},
        weights={"speed": 1.0, "extraction": 0.6, "long_context": 0.7},
        default_priority="LOW", est_context_tokens=6000, reasoning="low",
        description="Condense documents. Usually batch work, so throughput wins.",
    ),
    "general": TaskProfile(
        "general",
        needs={"text": 0.5},
        weights={"reasoning": 0.6, "speed": 0.8},
        est_context_tokens=4000, reasoning="low",
        description="Open conversation with no specialised capability demand.",
    ),
}

# Deterministic signals, checked in order. Regex beats a classifier here: it is
# auditable, instant, and cannot itself be prompt-injected by an uploaded document.
_RULES: list[tuple[str, str, float]] = [
    (r"\b(p&id|pid|piping and instrument|isometric|general arrangement|"
     r"drawing|schematic|datasheet drawing|loop diagram)\b", "drawing_analysis", 0.92),
    (r"\b(approval note|board note|note for approval|memo|minutes|"
     r"presentation|deck|ppt|slide|report to management|letter)\b",
     "deliverable_drafting", 0.86),
    (r"\b(python|javascript|bash|script|function|unit test|pytest|traceback|"
     r"stack trace|refactor|debug|compile|repository|codebase)\b", "coding", 0.88),
    (r"\b(calculate|compute|margin|derate|corrosion rate|remaining life|"
     r"thickness|mawp|stress|flow rate|percentage|ratio)\b", "calculation", 0.72),
    (r"\b(exceed\w*|within limits?|compliant|non[- ]?compliance|violat\w*|"
     r"permitted|permissible|allowable|acceptance criteri\w*|fit for service|"
     r"safe operating|over[- ]?pressur\w*|remaining life|corrosion rate)\b",
     "engineering_analysis", 0.84),
    (r"\b(handwritten|photograph|photo|scan|scanned|image|picture|legible)\b",
     "vision_understanding", 0.70),
    (r"\b(extract|list the findings|pull out|tabulate|itemise|itemize|"
     r"what are the observations)\b", "document_extraction", 0.78),
    (r"\b(summari[sz]e|summari[sz]ing|summaries|summary|brief|digest|"
     r"condense|overview of)\b", "summarisation", 0.72),
    (r"\b(according to|per the sop|as per|which clause|does the manual|"
     r"procedure says|standard requires)\b", "knowledge_qa", 0.80),
]

_PRIORITY_HINTS: list[tuple[str, str]] = [
    (r"\b(emergency|immediately|urgent|safety critical|shutdown|leak|"
     r"incident|unsafe|critical)\b", "CRITICAL"),
    (r"\b(asap|today|high priority|priority|before end of day|escalat)\b", "HIGH"),
    (r"\b(batch|bulk|overnight|when convenient|low priority|background|"
     r"all \d+ |process \d+ )\b", "BATCH"),
]

# An explicitly chosen workflow is a stronger signal than a regex over the
# prompt. "Extract the values, then check clause 5.2" matches the extraction
# pattern first, but the caller asked for engineering_qa and meant it.
WORKFLOW_TASK_TYPE = {
    "engineering_qa": "engineering_analysis",
    "inspection_to_approval": "deliverable_drafting",
    "drawing_review": "drawing_analysis",
    "coding": "coding",
    "batch_summarize": "summarisation",
    "sovereignty_proof": "general",
}

PRIORITY_ORDER = ["BATCH", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITY_ORDER)}


@dataclass
class Classification:
    task_type: str
    confidence: float
    priority: str
    signals: list[str] = field(default_factory=list)
    modality: str = "text"
    est_context_tokens: int = 4096
    reasoning: str = "medium"

    @property
    def profile(self) -> TaskProfile:
        return TASK_TYPES[self.task_type]


def classify(prompt: str, *, attachments: list[dict[str, Any]] | None = None,
             workflow: str | None = None,
             priority_override: str | None = None) -> Classification:
    """Classify a task. Deterministic, explainable, and injection-resistant."""
    text = (prompt or "").lower()
    attachments = attachments or []
    signals: list[str] = []

    # Attachment modality is the strongest signal and cannot be spoofed by text.
    kinds = {a.get("kind", "") for a in attachments}
    modality = "text"
    if "drawing" in kinds:
        modality = "drawing"
    elif kinds & {"image", "scan", "photo"}:
        modality = "image"

    task_type, confidence = "general", 0.35
    if workflow and workflow in TASK_TYPES:
        task_type, confidence = workflow, 0.99
        signals.append(f"workflow pinned to {workflow}")
    else:
        for pattern, ttype, conf in _RULES:
            m = re.search(pattern, text)
            if m and conf > confidence:
                task_type, confidence = ttype, conf
                signals.append(f"matched {m.group(0)!r} -> {ttype}")

        if modality == "drawing":
            task_type, confidence = "drawing_analysis", max(confidence, 0.95)
            signals.append("attachment classified as engineering drawing")
        elif modality == "image" and task_type in ("general", "summarisation",
                                                   "document_extraction"):
            task_type = "vision_understanding"
            confidence = max(confidence, 0.85)
            signals.append("image attachment requires a vision-capable model")

    prof = TASK_TYPES[task_type]
    priority = prof.default_priority
    for pattern, p in _PRIORITY_HINTS:
        m = re.search(pattern, text)
        if m and PRIORITY_RANK[p] > PRIORITY_RANK[priority]:
            priority = p
            signals.append(f"priority raised to {p} by {m.group(0)!r}")
            break
        if m and PRIORITY_RANK[p] < PRIORITY_RANK[priority] and p == "BATCH":
            priority = p
            signals.append(f"priority lowered to BATCH by {m.group(0)!r}")
            break
    if priority_override:
        priority = priority_override
        signals.append(f"priority set explicitly to {priority_override}")

    # Context estimate: the peak working set, not the corpus size. Documents are
    # reached through retrieval rather than pasted in whole, so an attachment
    # costs a few retrieved passages, not its full page count. Over-estimating
    # here silently pushes work onto weaker models with cheaper KV caches, which
    # is worse than trimming a trajectory that runs long.
    est = prof.est_context_tokens + len(prompt) // 3
    for a in attachments:
        est += min(int(a.get("pages", 1)), 6) * 300
    est = min(est, 30000)

    return Classification(task_type=task_type, confidence=round(confidence, 2),
                          priority=priority, signals=signals, modality=modality,
                          est_context_tokens=est, reasoning=prof.reasoning)


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------

@dataclass
class RoutingDecision:
    model: str
    backend: str
    score: float
    reason: str
    capability_fit: float
    residency_penalty: float
    latency_estimate_s: float
    considered: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    resident_reuse: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "backend": self.backend, "score": round(self.score, 3),
            "reason": self.reason, "capability_fit": round(self.capability_fit, 3),
            "residency_penalty": round(self.residency_penalty, 3),
            "latency_estimate_s": round(self.latency_estimate_s, 1),
            "resident_reuse": self.resident_reuse,
            "considered": self.considered, "rejected": self.rejected,
        }


# How much capability fit we are willing to give up to avoid an eviction.
#
# This is priority-dependent, and that matters more than it looks. Reusing a
# resident model saves real seconds, but on a workstation the resident model may
# be materially weaker at multi-step tool use, and an approval note drafted by a
# model that loses track of which value is a pressure and which is a thickness is
# worth less than nothing. So urgent work barely trades capability for residency,
# while overnight batch work trades it freely.
# These are on the *score* scale, which already prices decode speed and load
# cost, so the band only has to break near-ties — it is not a second, larger
# discount on top of the one the score already gives a resident model. Calibrated
# on the fit scale instead, 0.25 at BATCH let an overnight digest reuse a
# resident deep reasoner that was 0.23 worse by score.
RESIDENCY_TOLERANCE = 0.06          # the MEDIUM-priority default
_TOLERANCE_BY_PRIORITY = {
    "CRITICAL": 0.01, "HIGH": 0.03, "MEDIUM": 0.06, "LOW": 0.10, "BATCH": 0.15,
}

# Penalty weights by priority. The trade-off between answer quality and wall-clock
# is not fixed: an operator waiting on a safety question wants the best model even
# if it is slow, while overnight batch work wants throughput above all. Scaling the
# latency and residency terms by priority encodes that directly.
#                     latency, residency
_PRIORITY_PENALTY = {
    "CRITICAL": (0.10, 0.15),
    "HIGH":     (0.18, 0.25),
    "MEDIUM":   (0.35, 0.45),
    "LOW":      (0.50, 0.60),
    "BATCH":    (0.65, 0.75),
}


def capability_fit(card: ModelCard, prof: TaskProfile) -> tuple[float, str | None]:
    """Score a model against a task profile. Returns (fit, rejection reason)."""
    for axis, floor in prof.needs.items():
        if card.cap(axis) < floor:
            return 0.0, (f"{axis} capability {card.cap(axis):.2f} below required "
                         f"floor {floor:.2f}")
    total_w = sum(prof.weights.values()) or 1.0
    fit = sum(w * card.cap(axis) for axis, w in prof.weights.items()) / total_w
    return fit, None


def select_model(cls: Classification, *, resident: list[str] | None = None,
                 free_vram_mb: float = 0.0, exclude: set[str] | None = None,
                 output_tokens: int = 800,
                 budget_for: Any = None) -> RoutingDecision:
    """Choose a model for a classified task.

    `resident` is the set of models currently holding VRAM. A resident model gets
    a bonus equal to the load time it saves; a non-resident model is charged its
    measured cold-load cost. That single term is what turns model selection into
    a memory-scheduling decision.

    `budget_for` maps a model to the context tokens actually affordable right now,
    given the VRAM its weights occupy. Context capacity has to be part of
    *selection*, not a veto applied afterwards: a task that does not fit the
    preferred model usually fits a smaller one with a cheaper KV cache, and
    rejecting it outright would refuse work the machine can plainly do.
    """
    prof = cls.profile
    resident = resident or []
    exclude = exclude or set()

    considered: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    best: tuple[float, ModelCard, dict[str, Any]] | None = None

    for card in registry.all():
        if card.name in exclude:
            rejected.append({"model": card.name, "reason": "excluded by caller"})
            continue

        fit, why = capability_fit(card, prof)
        if why:
            rejected.append({"model": card.name, "reason": why})
            continue
        if cls.est_context_tokens > card.ctx_max:
            rejected.append({"model": card.name,
                             "reason": f"context estimate {cls.est_context_tokens} "
                                       f"exceeds {card.ctx_max} token window"})
            continue

        live_budget = int(budget_for(card)) if budget_for else card.ctx_max
        if cls.est_context_tokens > live_budget:
            rejected.append({
                "model": card.name,
                "reason": f"context estimate {cls.est_context_tokens} exceeds the "
                          f"{live_budget} tokens affordable after this model's "
                          f"weights ({card.residency_mb:.0f} MB) are resident"})
            continue

        is_resident = card.name in resident
        tps = card.decode_tps or max(6.0, 60.0 * card.cap("speed"))
        gen_s = output_tokens / tps
        load_s = 0.0 if is_resident else card.cold_load_s

        # Normalise both costs onto the same 0-1 scale as capability fit, using a
        # 60 s reference horizon for "a task the operator is waiting on".
        w_lat, w_res = _PRIORITY_PENALTY.get(cls.priority, (0.35, 0.45))
        latency_penalty = min(1.0, gen_s / 60.0) * w_lat
        residency_penalty = min(1.0, load_s / 60.0) * w_res
        if is_resident:
            residency_penalty = 0.0

        score = fit - latency_penalty - residency_penalty
        entry = {
            "model": card.name, "fit": round(fit, 3), "score": round(score, 3),
            "context_budget": live_budget,
            "latency_penalty": round(latency_penalty, 3),
            "residency_penalty": round(residency_penalty, 3),
            "resident": is_resident, "decode_tps": round(tps, 1),
            "est_generation_s": round(gen_s, 1), "load_s": round(load_s, 1),
            "residency_mb": round(card.residency_mb, 0),
        }
        considered.append(entry)
        if best is None or score > best[0]:
            best = (score, card, entry)

    if best is None:
        return RoutingDecision(
            model="", backend="", score=0.0, capability_fit=0.0,
            residency_penalty=0.0, latency_estimate_s=0.0,
            reason=("no enabled local model satisfies the required capability "
                    "floors within its affordable context budget"),
            considered=considered, rejected=rejected)

    score, card, entry = best

    # Anti-thrash override: if a resident model is close enough to the winner,
    # take the resident one and say so.
    #
    # The comparison is on *score*, not raw capability fit. Score already prices
    # decode speed and load cost, and a resident model already carries a zero
    # residency penalty — so if it still loses on score, reusing it is a real
    # loss, not a saving. Comparing fit alone produced exactly that: an overnight
    # summarisation batch reused a resident deep reasoner that decodes 2.8x
    # slower, spending far more time than the 5 s reload it avoided.
    top_fit = entry["fit"]
    top_score = entry["score"]
    if not entry["resident"]:
        tolerance = _TOLERANCE_BY_PRIORITY.get(cls.priority, RESIDENCY_TOLERANCE)
        for alt in sorted((e for e in considered if e["resident"]),
                          key=lambda e: -e["score"]):
            if top_score - alt["score"] <= tolerance:
                alt_card = registry.get(alt["model"])
                if alt_card:
                    reason = (
                        f"{alt['model']} is already resident and scores "
                        f"{alt['score']:.2f} against {card.name}'s "
                        f"{top_score:.2f}, within the {tolerance:.2f} tolerance "
                        f"allowed at {cls.priority} priority; reusing it avoids a "
                        f"{entry['load_s']:.0f}s eviction and reload"
                    )
                    return RoutingDecision(
                        model=alt_card.name, backend=alt_card.backend,
                        score=alt["score"], reason=reason, capability_fit=alt["fit"],
                        residency_penalty=0.0,
                        latency_estimate_s=alt["est_generation_s"],
                        considered=considered, rejected=rejected, resident_reuse=True)
                break

    if entry["resident"]:
        reason = (f"{card.name} best fits {cls.task_type} at {cls.priority} priority "
                  f"(fit {top_fit:.2f}) and is already resident, so it incurs no "
                  f"load cost")
    else:
        reason = (f"{card.name} best fits {cls.task_type} at {cls.priority} priority "
                  f"(fit {top_fit:.2f}); it is not resident, so admission must budget "
                  f"{entry['load_s']:.0f}s of load and "
                  f"{entry['residency_mb']:.0f} MB of VRAM")
    return RoutingDecision(
        model=card.name, backend=card.backend, score=score, reason=reason,
        capability_fit=top_fit, residency_penalty=entry["load_s"],
        latency_estimate_s=entry["est_generation_s"],
        considered=considered, rejected=rejected, resident_reuse=entry["resident"])


def explain(cls: Classification, decision: RoutingDecision) -> dict[str, Any]:
    return {
        "task_type": cls.task_type, "confidence": cls.confidence,
        "priority": cls.priority, "modality": cls.modality,
        "signals": cls.signals,
        "estimated_context_tokens": cls.est_context_tokens,
        "capability_floors": cls.profile.needs,
        "ranking_weights": cls.profile.weights,
        "routing": decision.to_dict(),
    }
