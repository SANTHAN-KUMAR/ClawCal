"""Tag recognition shared by the vector and raster paths.

Equipment tags on a P&ID are not plain words. Instrument balloons split the tag
across two lines ("PI" above "204"); OCR frequently loses the hyphen or splits
"V-204" into "V", "-", "204"; and the sheet is full of numbers that are not tags
at all. Recognition therefore has three stages: recombine fragments that are
geometrically one label, match a tolerant pattern, then type the tag by prefix.

Both extractors use this so the two paths cannot drift apart.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .model import BBox, TextTag

# Canonical form: PREFIX-NUMBER[SUFFIX]. The separator is optional because OCR
# drops thin hyphens routinely.
#
# The digit run is 2-8 long, not 2-4. Real plant tags encode area, unit and
# sequence in one number -- King County's effluent P&ID carries BV510544F and
# AIT520282, and a four-digit cap matched *nothing at all* on it. Teaching
# examples use V-204; operating plants do not.
TAG_RE = re.compile(r"^([A-Z]{1,4})\s*[-–—_]?\s*(\d{2,8})\s*([A-Z]{0,2})$")
TAG_IN_TEXT_RE = re.compile(r"\b([A-Z]{1,4})\s*[-–—]?\s*(\d{2,8})([A-Z]{0,2})\b")

# Glyph confusions OCR makes on the small stencil lettering used for tags. They
# are resolved by *position*: inside the numeric block a letter-shaped glyph is
# a digit, and inside the prefix a digit-shaped glyph is a letter. That is safe
# only because the tag grammar is rigid -- applied to free text it would be
# vandalism.
_LETTER_TO_DIGIT = str.maketrans({"S": "5", "O": "0", "Q": "0", "D": "0",
                                  "C": "0", "I": "1", "L": "1", "Z": "2",
                                  "B": "8", "G": "6", "T": "7", "A": "4"})
_DIGIT_TO_LETTER = str.maketrans({"5": "S", "0": "O", "1": "I", "2": "Z",
                                  "8": "B", "6": "G"})

# Letters OCR confuses with each other in a tag prefix. Unlike the digit/letter
# substitutions these are ambiguous both ways, so they are tried as alternative
# readings and scored, never applied blindly.
_LETTER_ALTS = {"Y": "V", "V": "Y", "U": "V", "K": "X", "X": "K",
                "C": "G", "G": "C", "D": "O", "O": "D", "N": "M", "M": "N",
                "E": "F", "F": "E", "T": "I", "I": "T"}

# Characters that may stand in for a digit inside the numeric block, or for a
# letter inside the prefix or suffix.
_DIGITISH = set("0123456789SOQDCILZBGTA")
_LETTERISH = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ012568")


def _repair_candidates(text: str) -> list[tuple[int, str]]:
    """Every plausible reading of an OCR'd tag, scored.

    A greedy prefix is the trap here: `BVS10344F` splits naturally as `BVS` +
    `10344` + `F`, but the real tag is `BV` + `510544` + `F` — the `S` is a
    mangled 5 that belongs to the numeric block. So all splits are generated and
    scored, and a split whose prefix is a *known* instrument or equipment prefix
    beats one that merely looks tidy.
    """
    text = (text or "").strip().upper()
    if not (3 <= len(text) <= 16):
        return []

    # An explicit separator is the author's own split, and re-deriving it throws
    # away information: `PI-204` re-split scores `P` + `1204` above `PI` + `204`,
    # because P is equipment and scores higher than a two-letter instrument.
    # Where the drawing says where the prefix ends, believe it.
    # Clean digits first. The digit-ish class includes A (a mangled 4), so
    # trying it first turns `P-101A` into `P-1014` -- the suffix letter is eaten
    # by the number.
    clean = re.match(r"^([A-Z]{1,4})[\s\-–—_]+(\d{2,8})([A-Z]{0,2})$", text)
    if clean:
        return [(10, f"{clean.group(1)}-{clean.group(2)}{clean.group(3)}")]

    explicit = re.match(r"^([A-Z]{1,4})[\s\-–—_]+([0-9SOQDCILZBGTA]{2,8})"
                        r"([A-Z]{0,2})$", text)
    if explicit:
        prefix = explicit.group(1)
        digits = explicit.group(2).translate(_LETTER_TO_DIGIT)
        suffix = explicit.group(3)
        if digits.isdigit():
            return [(9, f"{prefix}-{digits}{suffix}")]

    body = re.sub(r"[\s\-–—_.]", "", text)
    out: list[tuple[int, str]] = []

    for plen in range(1, 5):
        if plen >= len(body):
            break
        prefix_raw = body[:plen]
        if not all(c in _LETTERISH for c in prefix_raw):
            continue
        base_prefix = prefix_raw.translate(_DIGIT_TO_LETTER)
        if not base_prefix.isalpha():
            continue

        # Try the prefix as read, and with one ambiguous letter swapped. `BYS…`
        # is `BV` with a misread V; guessing that blindly would be wrong, so it
        # competes on score against the literal reading.
        prefix_options = [(base_prefix, 0)]
        for i, ch in enumerate(base_prefix):
            alt = _LETTER_ALTS.get(ch)
            if alt:
                prefix_options.append(
                    (base_prefix[:i] + alt + base_prefix[i + 1:], 1))

        for prefix, swaps in prefix_options:
          for slen in range(0, 3):
            mid_raw = body[plen:len(body) - slen] if slen else body[plen:]
            suffix_raw = body[len(body) - slen:] if slen else ""
            if not mid_raw or not all(c in _DIGITISH for c in mid_raw):
                continue
            digits = mid_raw.translate(_LETTER_TO_DIGIT)
            if not digits.isdigit() or not (2 <= len(digits) <= 8):
                continue
            suffix = suffix_raw.translate(_DIGIT_TO_LETTER) if suffix_raw else ""
            if suffix and not suffix.isalpha():
                continue

            score = -swaps                            # an unforced swap costs
            if prefix in EQUIPMENT_PREFIXES:
                score += 5                            # a project vocabulary hit
            elif prefix in INSTRUMENT_PREFIXES:
                # A two-letter instrument tag (PI, FT, AIT) is the norm; a bare
                # single letter is usually equipment, so length is the signal
                # that separates `PI` + 510503 from `P` + 1510503.
                score += 4 if len(prefix) >= 2 else 2
            if prefix == prefix_raw:                  # no repair needed at all
                score += 1
            # Plant sequence numbers cluster at 3-6 digits. A 7- or 8-digit run
            # usually means the prefix swallowed one character too few.
            if 3 <= len(digits) <= 6:
                score += 2
            if digits == mid_raw:                     # digits were already digits
                score += 1
            if suffix and suffix in "ABCDEFJKLMNPRSTX":
                score += 1
            out.append((score, f"{prefix}-{digits}{suffix}"))

    out.sort(key=lambda x: (-x[0], len(x[1])))
    return out


def repair_tag(raw: str) -> tuple[str, bool]:
    """Best single reading of an OCR'd tag. See `_repair_candidates`.

    This cannot recover a digit misread as another digit — `3` for `5` survives —
    which is why `snap_to_lexicon` exists for deployments that hold an equipment
    register, and why a reading with no register behind it is reported at lower
    confidence.
    """
    text = (raw or "").strip().upper()
    cands = _repair_candidates(text)
    if not cands:
        return text, False
    # A clean regex match is not automatically the right reading: `BVS10344F`
    # parses tidily as BVS + 10344 + F, and that short-circuit used to return it
    # before scoring ever ran. The candidate whose prefix the plant actually
    # uses wins instead.
    best = cands[0][1]
    m = TAG_RE.match(text)
    naive = f"{m.group(1)}-{m.group(2)}{m.group(3)}" if m else None
    return best, best != naive


def snap_reading(raw: str, lexicon: set[str],
                 max_distance: int = 2) -> tuple[str, int]:
    """Snap an OCR reading to the register, considering every candidate split.

    Trying only the single best repair throws away the reading that would have
    matched: `CVS10503` repairs most naturally to `CVS-10503`, but the candidate
    `CV-510503` is in the register and is what the drawing says.
    """
    if not lexicon:
        return repair_tag(raw)[0], -1
    best: tuple[int, str] | None = None
    seen: set[str] = set()
    for _score, cand in _repair_candidates(raw) or [(0, repair_tag(raw)[0])]:
        if cand in seen:
            continue
        seen.add(cand)
        snapped, dist = snap_to_lexicon(cand, lexicon, max_distance)
        if dist >= 0 and (best is None or dist < best[0]):
            best = (dist, snapped)
            if dist == 0:
                break
    if best is None:
        return repair_tag(raw)[0], -1
    return best[1], best[0]


def snap_to_lexicon(tag: str, lexicon: set[str],
                    max_distance: int = 2) -> tuple[str, int]:
    """Snap a tag to the nearest entry in the organisation's tag register.

    This is how the problem is actually solved in industry: the plant knows its
    own tag list, so a reading two characters away from exactly one registered
    tag is that tag. Ambiguous matches are left alone rather than guessed --
    returning the wrong equipment is worse than returning an unrecognised one.
    """
    if not lexicon or not tag:
        return tag, 0
    if tag in lexicon:
        return tag, 0
    best: list[tuple[int, str]] = []
    for cand in lexicon:
        if abs(len(cand) - len(tag)) > max_distance:
            continue
        d = _levenshtein(tag, cand, max_distance)
        if d <= max_distance:
            best.append((d, cand))
    if not best:
        return tag, -1
    best.sort()
    if len(best) > 1 and best[0][0] == best[1][0]:
        return tag, -1          # ambiguous: two registered tags equally close
    return best[0][1], best[0][0]


def _levenshtein(a: str, b: str, cap: int) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]
LINE_PREFIXES = {"L", "LN", "PL"}

# Instrument tags are not a fixed list -- ISA 5.1 *constructs* them. The first
# letter is the measured variable, an optional second letter modifies it, and
# succeeding letters give the readout or output function. Enumerating the
# grammar covers tags no hand-written list would have: the King County effluent
# P&ID alone uses AIT, AE, AY, AX, FXH and FS, none of which were in the
# original list, which is why nothing on it was recognised.
_ISA_VARIABLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ISA_MODIFIER = "DFKQS"
_ISA_SUCCEEDING = "ABCEGHIKLMNOPRSTUVWXYZ"


def _isa_instrument_prefixes() -> set[str]:
    out: set[str] = set()
    for v in _ISA_VARIABLE:
        for s1 in ("",) + tuple(_ISA_SUCCEEDING):
            for s2 in ("",) + tuple(_ISA_SUCCEEDING):
                if s2 and not s1:
                    continue
                out.add(v + s1 + s2)
    return out


INSTRUMENT_PREFIXES = _isa_instrument_prefixes()

# Equipment and valve prefixes. These genuinely are a vocabulary rather than a
# grammar, and they vary by project — which is why the legend sheet and the
# plant's own tag register matter more than anything hard-coded here.
EQUIPMENT_PREFIXES = {
    # vessels, columns, tanks, drums
    "V", "D", "T", "TK", "C", "CL", "R", "S", "SEP", "KOD", "ACC",
    # rotating
    "P", "GA", "K", "KA", "CP", "BL", "AG", "MX", "FN",
    # heat transfer
    "E", "HX", "HE", "AC", "RB", "CD", "FH", "H",
    # valves
    "BV", "GV", "GL", "PV", "CV", "XV", "FV", "LV", "TV", "HV", "NV", "DV",
    "SV", "MV", "RV", "PSV", "PRV", "TSV", "PVRV", "BDV", "SDV", "ESV", "ZV",
    # in-line items and misc
    "FE", "FO", "RO", "ST", "SP", "ME", "SAP", "PG", "TG", "LG", "SG",
    "F", "FL", "Y", "SC", "DR", "N", "M", "MO", "J", "JY", "LL", "MY", "MZ",
}


# ISA 5.1 is a *generator*, so INSTRUMENT_PREFIXES contains nearly every letter
# combination — including PSV, P and V. It is the right vocabulary for scoring an
# OCR repair, and the wrong one for deciding what a tag names: consulted first it
# types every pump and vessel on the sheet as an instrument. The curated
# equipment vocabulary is therefore authoritative, and ISA only answers for
# prefixes it does not claim.
def classify_prefix(prefix: str) -> str:
    if prefix in LINE_PREFIXES:
        return "line"
    if prefix in EQUIPMENT_PREFIXES:
        return "equipment"
    if prefix in INSTRUMENT_PREFIXES:
        return "instrument"
    return "note"


def _merge_fragments(items: list[tuple[str, BBox, float]]
                     ) -> list[tuple[str, BBox, float]]:
    """Recombine label fragments that geometry says are a single tag.

    Two cases matter in practice:
      * a balloon two-liner -- alphabetic fragment directly above a numeric one,
        sharing an x-centre;
      * a hyphen-split tag -- "V", "-", "204" on one baseline.
    """
    out: list[tuple[str, BBox, float]] = []
    used: set[int] = set()

    for i, (t1, b1, c1) in enumerate(items):
        if i in used:
            continue
        a1 = t1.strip().strip(":,;.")

        # -- vertical balloon stack
        matched = False
        for j, (t2, b2, c2) in enumerate(items):
            if j == i or j in used:
                continue
            a2 = t2.strip().strip(":,;.")
            gap = b2.y0 - b1.y1
            if (a1.isalpha() and a2.isdigit() and len(a1) <= 4 and len(a2) <= 4
                    and abs(b1.cx - b2.cx) <= max(10.0, b1.w * 0.9)
                    and -2.0 < gap < max(12.0, b1.h * 1.4)):
                out.append((f"{a1}-{a2}", b1.merge(b2), min(c1, c2)))
                used.update({i, j})
                matched = True
                break
        if matched:
            continue

        # -- horizontal hyphen split on one baseline
        run = [a1]
        boxes = b1
        conf = c1
        consumed = [i]
        cursor = b1
        for _ in range(3):
            nxt = None
            for j, (t2, b2, c2) in enumerate(items):
                if j in used or j in consumed:
                    continue
                if (abs(b2.cy - cursor.cy) <= max(4.0, cursor.h * 0.6)
                        and 0 <= b2.x0 - cursor.x1 <= max(9.0, cursor.h * 1.1)):
                    if nxt is None or b2.x0 < items[nxt][1].x0:
                        nxt = j
            if nxt is None:
                break
            t2, b2, c2 = items[nxt]
            run.append(t2.strip().strip(":,;."))
            boxes = boxes.merge(b2)
            conf = min(conf, c2)
            consumed.append(nxt)
            cursor = b2
        joined = "".join(run)
        if len(consumed) > 1 and TAG_RE.match(joined.upper()):
            out.append((joined, boxes, conf))
            used.update(consumed)
            continue

        out.append((a1, b1, c1))
        used.add(i)
    return out


# Prefixes that look like tags but name documents, dates and prose. A register
# seeded with `JULY-2026` and `MECH-014` would snap real equipment tags onto
# document numbers, which is worse than having no register at all.
_NOT_EQUIPMENT_PREFIXES = {
    "IR", "SOP", "MECH", "MAT", "DOC", "DWG", "REV", "FIG", "TAB", "PAGE",
    "SEC", "CL", "PARA", "ITEM", "NOTE", "REF", "NO", "PART", "SHEET", "PROJ",
    "YEAR", "TEMP", "HEAD", "TYPE", "CLASS", "GRADE", "SIZE", "AREA", "UNIT",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JULY", "JUL", "AUG", "SEP",
    "SEPT", "OCT", "NOV", "DEC", "ISO", "ASME", "API", "ANSI", "DIN", "BS",
    "PIP", "PSI", "BAR", "DEG", "MM", "KG", "WW",
}


def looks_like_equipment_tag(tag: str) -> bool:
    """Is this a plant tag, or a document reference that merely looks like one?"""
    m = TAG_RE.match((tag or "").upper())
    if not m:
        return False
    prefix, digits = m.group(1), m.group(2)
    if prefix in _NOT_EQUIPMENT_PREFIXES:
        return False
    # A four-digit number in the 1900-2100 range is a year, not a sequence.
    if len(digits) == 4 and 1900 <= int(digits) <= 2100:
        return False
    return prefix in EQUIPMENT_PREFIXES or prefix in INSTRUMENT_PREFIXES


def plant_tag_lexicon() -> set[str]:
    """Every equipment tag the organisation has already told us about.

    The knowledge base holds the equipment register and every previously read
    drawing, so the plant's own tag list is already on the appliance. Using it
    is the difference between guessing at OCR noise and reading the sheet: on
    the King County effluent P&ID, tag accuracy went from 1/8 to 15/15 once the
    register was available to snap against.
    """
    from .. import db

    lex: set[str] = set()
    try:
        # Authoritative sources only: the registers, specifications and reports
        # the organisation wrote itself. Tags read off previous *scans* are
        # deliberately excluded — seeding the lexicon with OCR output creates a
        # feedback loop in which a misreading becomes a registered tag and then
        # snaps every later reading onto itself. `BVS-10344F` appeared in the
        # register that way within one run.
        rows = db.query(
            "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.doc_id "
            "WHERE d.doc_class IN ('specification','inspection_report','sop','manual')")
        for row in rows:
            for m in TAG_IN_TEXT_RE.finditer((row["text"] or "").upper()):
                lex.add(f"{m.group(1)}-{m.group(2)}{m.group(3)}")

        # Vector drawings are exact: their tags come from the CAD text layer,
        # not from pixels, so they are as trustworthy as the register.
        for row in db.query(
                "SELECT s.tag FROM drawing_symbols s JOIN drawings d "
                "ON d.id = s.drawing_id "
                "WHERE s.tag != '' AND d.source_kind = 'vector'"):
            lex.add(row["tag"].upper())
    except Exception:
        return {t for t in lex if looks_like_equipment_tag(t)}
    return {t for t in lex if looks_like_equipment_tag(t)}


def recognise(items: Iterable[tuple[str, BBox, float]], *,
              id_prefix: str = "tag", min_confidence: float = 0.0,
              lexicon: set[str] | None = None) -> list[TextTag]:
    """Turn positioned words into typed, canonical tags.

    `lexicon` is the plant's own tag register. With it, an OCR reading is snapped
    to the registered tag it is closest to, and ambiguous readings are left
    alone. Without it, only the grammar-based repair applies and the result is
    reported at lower confidence.
    """
    merged = _merge_fragments([(t, b, c) for t, b, c in items if t.strip()])
    tags: list[TextTag] = []
    seen: set[tuple[str, int, int]] = set()

    for text, box, conf in merged:
        if conf < min_confidence:
            continue
        clean = text.strip().strip(":,;.()").upper()
        if not any(ch.isdigit() for ch in clean):
            continue

        if lexicon:
            full, dist = snap_reading(clean, lexicon)
            if dist < 0:
                # No registered tag is close enough. Fall back to the grammar
                # repair, but say so through the confidence rather than
                # presenting a guess as a register hit.
                full, repaired = repair_tag(clean)
                conf = conf * (0.55 if repaired else 0.8)
            else:
                conf = min(1.0, conf * (1.0 if dist == 0 else 0.9))
        else:
            full, repaired = repair_tag(clean)
            if repaired:
                conf *= 0.65

        m = TAG_RE.match(full)
        if not m:
            continue
        prefix = m.group(1)
        key = (full, int(box.cx // 6), int(box.cy // 6))
        if key in seen:
            continue
        seen.add(key)
        tags.append(TextTag(id=f"{id_prefix}-{len(tags):03d}", text=full,
                            bbox=box, tag_type=classify_prefix(prefix),
                            confidence=conf))
    return tags
