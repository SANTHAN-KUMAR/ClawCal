"""Connectivity assembly: line runs + symbols -> a scored graph.

This is the part the survey calls a graph problem rather than a captioning
problem, and it is where a vision model would confidently invent edges. The
approach here is purely geometric and refuses when geometry is inconclusive.

Steps:
  1. chain line runs whose endpoints nearly touch (drawings break a single run
     into many primitives at every jog);
  2. attach each chain end to a symbol, at strict then relaxed tolerance;
  3. score the edge from how it was attached and where the geometry came from;
  4. associate tags to symbols and line tags to edges;
  5. emit anything that failed to attach as UNRESOLVED rather than dropping it.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import networkx as nx

from .model import (CONFIRMED, PROBABLE, UNRESOLVED, BBox, Edge, Polyline,
                    Symbol, TextTag)
from .vector import (ENDPOINT_TOLERANCE, ENDPOINT_TOLERANCE_RELAXED,
                     JOIN_TOLERANCE, MIN_LINE_LENGTH)


def _grid_key(p: tuple[float, float], cell: float) -> tuple[int, int]:
    return (int(p[0] // cell), int(p[1] // cell))


def _neighbours(key: tuple[int, int]) -> Iterable[tuple[int, int]]:
    kx, ky = key
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            yield (kx + dx, ky + dy)


def chain_lines(lines: list[Polyline], tol: float = JOIN_TOLERANCE) -> list[Polyline]:
    """Merge line runs whose endpoints coincide into single chains.

    Drawings break one logical pipe run into a separate primitive at every jog,
    so chaining is unavoidable. The naive form compares every remaining run
    against every chain end, which is O(n^2) and becomes the dominant cost on a
    dense sheet -- about 5.5 s at 5000 primitives on this hardware.

    Endpoints are therefore indexed into a uniform grid whose cell size is the
    join tolerance, so a candidate lookup only scans the nine neighbouring cells.
    That makes chaining effectively linear in the number of runs.

    Only runs of the same style are chained: a dashed instrument lead must never
    be welded onto a solid process line, or the graph gains a connection the
    drawing does not assert.
    """
    if not lines:
        return []
    cell = max(tol, 1e-6)

    # endpoint grid -> list of (line index, which_end)
    index: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for i, ln in enumerate(lines):
        index.setdefault(_grid_key(ln.start, cell), []).append((i, 0))
        index.setdefault(_grid_key(ln.end, cell), []).append((i, 1))

    consumed = [False] * len(lines)
    chains: list[Polyline] = []

    def candidates(point: tuple[float, float], dashed: bool) -> list[tuple[int, int]]:
        out = []
        for key in _neighbours(_grid_key(point, cell)):
            for (idx, end) in index.get(key, ()):
                if consumed[idx] or lines[idx].dashed != dashed:
                    continue
                ep = lines[idx].end if end else lines[idx].start
                if math.dist(point, ep) <= tol:
                    out.append((idx, end))
        return out

    for seed_i, seed in enumerate(lines):
        if consumed[seed_i]:
            continue
        consumed[seed_i] = True
        pts = list(seed.points)
        dashed = seed.dashed

        extended = True
        while extended:
            extended = False
            # extend forward from the tail
            for idx, end in candidates(pts[-1], dashed):
                other = lines[idx].points
                seq = list(reversed(other)) if end == 1 else other
                pts.extend(seq[1:])
                consumed[idx] = True
                extended = True
                break
            if extended:
                continue
            # extend backward from the head
            for idx, end in candidates(pts[0], dashed):
                other = lines[idx].points
                seq = other if end == 1 else list(reversed(other))
                pts = seq[:-1] + pts
                consumed[idx] = True
                extended = True
                break

        chains.append(Polyline(id=f"chain-{len(chains):03d}", points=pts,
                               dashed=dashed, width=seed.width))
    return [c for c in chains if c.length >= MIN_LINE_LENGTH]


def _attach(point: tuple[float, float], symbols: list[Symbol],
            exclude: str | None = None,
            scale: float = 1.0) -> tuple[Symbol | None, float, bool]:
    """Find the symbol an endpoint touches. Returns (symbol, distance, strict).

    Tolerances are expressed for a nominal A3 sheet in PDF points and scaled to
    the coordinate system actually in use, because a raster page rendered at
    200 dpi carries roughly 2.8x the coordinate units of the same sheet in
    points. Absolute tolerances silently fail on scanned input.
    """
    # A tagged symbol is known equipment; an untagged blob on a scanned sheet is
    # often just noise. Attaching to the blob would fabricate a connection, so a
    # tagged candidate wins any near-tie.
    best: tuple[float, Symbol] | None = None
    for s in symbols:
        if exclude and s.id == exclude:
            continue
        d = s.bbox.distance_to(*point)
        ranked = d - (4.0 * scale if s.tag else 0.0)
        if best is None or ranked < best[0]:
            best = (ranked, s)
    if best is None:
        return None, math.inf, False
    _ranked, s = best
    d = s.bbox.distance_to(*point)
    if d <= ENDPOINT_TOLERANCE * scale:
        return s, d, True
    if d <= ENDPOINT_TOLERANCE_RELAXED * scale:
        return s, d, False
    return None, d, False


def associate_tags(symbols: list[Symbol], tags: list[TextTag],
                   max_distance: float = 90.0, scale: float = 1.0) -> None:
    """Bind each equipment/instrument tag to its nearest plausible symbol.

    Greedy nearest-first over all (tag, symbol) pairs, with a class compatibility
    check so an instrument tag cannot label a pump. One tag per symbol.
    """
    from .model import SYMBOL_LEGEND

    max_distance = max_distance * scale
    pairs: list[tuple[float, TextTag, Symbol]] = []
    for t in tags:
        if t.tag_type not in ("equipment", "instrument"):
            continue
        prefix = t.text.split("-")[0]
        for s in symbols:
            d = s.bbox.distance_to(t.bbox.cx, t.bbox.cy)
            if d > max_distance:
                continue
            allowed = SYMBOL_LEGEND.get(s.sym_class, {}).get("tag_prefixes", [])
            # Prefix mismatch is a soft signal, not a veto. Shape classification
            # cannot distinguish a manual valve from a relief valve -- both are
            # bowties -- so the tag has to be allowed to win, and the symbol is
            # reclassified afterwards from the prefix.
            penalty = 0.0 if (not allowed or prefix in allowed) else 18.0 * scale
            if t.tag_type == "instrument" and s.sym_class != "instrument":
                penalty += 25.0 * scale
            if t.tag_type == "equipment" and s.sym_class == "instrument":
                penalty += 25.0 * scale
            pairs.append((d + penalty, t, s))

    pairs.sort(key=lambda p: p[0])
    taken_syms: set[str] = set()
    taken_tags: set[str] = set()
    for score, t, s in pairs:
        if t.id in taken_tags or s.id in taken_syms or score > max_distance:
            continue
        s.tag = t.text
        t.symbol_id = s.id
        t.assoc_distance = s.bbox.distance_to(t.bbox.cx, t.bbox.cy)
        taken_tags.add(t.id)
        taken_syms.add(s.id)

    _reclassify_from_tags(symbols)


# A tag prefix identifies equipment far more reliably than its glyph does.
_PREFIX_CLASS = {
    "PSV": "relief_valve", "PRV": "relief_valve", "RV": "relief_valve",
    "HV": "valve", "GV": "valve", "BV": "valve", "CV": "valve", "XV": "valve",
    "P": "pump", "GA": "pump",
    "E": "heat_exchanger", "HX": "heat_exchanger",
    "V": "vessel", "D": "vessel",
    "C": "column",
}


def _reclassify_from_tags(symbols: list[Symbol]) -> None:
    """Let an attached tag correct the shape classifier.

    Geometry establishes that a symbol exists and where it is; the tag prefix
    establishes what it is. Recording the correction keeps the change auditable.
    """
    for s in symbols:
        if not s.tag:
            continue
        prefix = s.tag.split("-")[0].upper()
        implied = _PREFIX_CLASS.get(prefix)
        if implied and implied != s.sym_class:
            s.primitives.append(f"reclassified:{s.sym_class}->{implied}:tag:{prefix}")
            s.sym_class = implied
            s.confidence = min(0.95, s.confidence + 0.08)


def _nearest_line_tag(chain: Polyline, tags: list[TextTag],
                      max_distance: float = 26.0) -> str:
    best: tuple[float, str] | None = None
    for t in tags:
        if t.tag_type != "line":
            continue
        d = min(math.dist((t.bbox.cx, t.bbox.cy), p) for p in chain.points)
        if d <= max_distance and (best is None or d < best[0]):
            best = (d, t.text)
    return best[1] if best else ""


def build_edges(chains: list[Polyline], symbols: list[Symbol],
                tags: list[TextTag], *, source_kind: str,
                scale: float = 1.0) -> list[Edge]:
    edges: list[Edge] = []
    for i, chain in enumerate(chains):
        s_sym, s_dist, s_strict = _attach(chain.start, symbols, scale=scale)
        e_sym, e_dist, e_strict = _attach(chain.end, symbols,
                                          exclude=s_sym.id if s_sym else None,
                                          scale=scale)
        line_type = "instrument_signal" if chain.dashed else "process"
        line_tag = _nearest_line_tag(chain, tags)
        eid = f"edge-{i:03d}"

        if s_sym is None or e_sym is None:
            missing = []
            if s_sym is None:
                missing.append(f"start ({chain.start[0]:.0f},{chain.start[1]:.0f}) "
                               f"nearest symbol {s_dist:.0f} pt away")
            if e_sym is None:
                missing.append(f"end ({chain.end[0]:.0f},{chain.end[1]:.0f}) "
                               f"nearest symbol {e_dist:.0f} pt away")
            edges.append(Edge(
                id=eid, src=s_sym.id if s_sym else "", dst=e_sym.id if e_sym else "",
                line_type=line_type, polyline=chain.points, confidence=0.25,
                status=UNRESOLVED, line_tag=line_tag,
                rationale=(f"a {chain.length:.0f} pt {line_type} line was traced but "
                           f"could not be attached at both ends: "
                           + "; ".join(missing))))
            continue

        if s_sym.id == e_sym.id:
            continue                        # a line looping back on one symbol

        strict = s_strict and e_strict
        if source_kind == "vector" and strict:
            status, conf = CONFIRMED, 0.94
            rationale = (f"exact vector geometry; both endpoints within "
                         f"{ENDPOINT_TOLERANCE * scale:.0f} units of a symbol "
                         f"boundary "
                         f"({s_dist:.1f} pt and {e_dist:.1f} pt)")
        elif source_kind == "vector":
            status, conf = PROBABLE, 0.66
            rationale = (f"vector geometry, but attachment required relaxing the "
                         f"tolerance to {ENDPOINT_TOLERANCE_RELAXED * scale:.0f} "
                         f"units "
                         f"({s_dist:.1f} pt and {e_dist:.1f} pt)")
        elif strict:
            status, conf = PROBABLE, 0.60
            rationale = ("geometry recovered from a raster image, so the line run "
                         "is inferred rather than read; both endpoints attached "
                         "cleanly")
        else:
            status, conf = UNRESOLVED, 0.35
            rationale = (f"raster-inferred line with loose endpoint attachment "
                         f"({s_dist:.1f} pt and {e_dist:.1f} pt); not reliable "
                         f"enough to assert a connection")

        edges.append(Edge(id=eid, src=s_sym.id, dst=e_sym.id, line_type=line_type,
                          polyline=chain.points, confidence=conf, status=status,
                          line_tag=line_tag, rationale=rationale))
    return edges


def to_networkx(symbols: list[Symbol], edges: list[Edge], *,
                include: Iterable[str] = (CONFIRMED,)) -> nx.MultiGraph:
    g = nx.MultiGraph()
    for s in symbols:
        g.add_node(s.id, label=s.label, tag=s.tag, cls=s.sym_class,
                   confidence=s.confidence)
    for e in edges:
        if e.status in include and e.src and e.dst:
            g.add_edge(e.src, e.dst, key=e.id, line_type=e.line_type,
                       line_tag=e.line_tag, status=e.status,
                       confidence=e.confidence)
    return g


def analyse_topology(symbols: list[Symbol], edges: list[Edge]) -> dict[str, Any]:
    """Derived facts an engineer would actually ask for."""
    g = to_networkx(symbols, edges, include=(CONFIRMED, PROBABLE))
    process = to_networkx(
        symbols, [e for e in edges if e.line_type == "process"],
        include=(CONFIRMED,))

    by_id = {s.id: s for s in symbols}
    components = [sorted(by_id[n].label for n in c)
                  for c in nx.connected_components(process) if len(c) > 1]
    isolated = sorted(by_id[n].label for n in process.nodes
                      if process.degree(n) == 0)

    degrees = {by_id[n].label: process.degree(n) for n in process.nodes}
    return {
        "process_components": components,
        "isolated_symbols": isolated,
        "degree": dict(sorted(degrees.items(), key=lambda kv: -kv[1])[:20]),
        "node_count": g.number_of_nodes(),
        "edge_count": g.number_of_edges(),
        "confirmed_process_edges": process.number_of_edges(),
    }


def trace_path(symbols: list[Symbol], edges: list[Edge], src_tag: str,
               dst_tag: str) -> dict[str, Any]:
    """Is there a confirmed process path between two tags?"""
    by_tag = {s.tag: s.id for s in symbols if s.tag}
    if src_tag not in by_tag or dst_tag not in by_tag:
        missing = [t for t in (src_tag, dst_tag) if t not in by_tag]
        return {"found": False, "status": "CANNOT DETERMINE",
                "reason": f"tag(s) {missing} are not present on this drawing"}
    g = to_networkx(symbols, [e for e in edges if e.line_type == "process"],
                    include=(CONFIRMED,))
    labels = {s.id: s.label for s in symbols}
    try:
        path = nx.shortest_path(g, by_tag[src_tag], by_tag[dst_tag])
        return {"found": True, "status": CONFIRMED,
                "path": [labels[n] for n in path]}
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    g2 = to_networkx(symbols, [e for e in edges if e.line_type == "process"],
                     include=(CONFIRMED, PROBABLE))
    try:
        path = nx.shortest_path(g2, by_tag[src_tag], by_tag[dst_tag])
        return {"found": True, "status": PROBABLE,
                "path": [labels[n] for n in path],
                "reason": "path relies on at least one PROBABLE connection"}
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return {"found": False, "status": "CANNOT DETERMINE",
                "reason": "no traceable process path between these tags on this "
                          "drawing; a connection may exist via an off-sheet "
                          "reference that this drawing does not show"}
