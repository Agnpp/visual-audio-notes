"""Small content-only candidate retrieval for the desktop bundle.

The default path uses no model or ML runtime. Optional vocabulary from a
quote-verified AI ledger is explicitly labelled AI-derived; a lexical overlap
score never confirms a match or proves that two sources make the same claim.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import math
import re

from .core import Chunk, Segment, Unit, normalize


_STOPWORDS = frozenset("a an and are as at be been but by for from had has have he her his i if in into is it its me my of on or our she so than that the their them then there these they this those to was we were what when where which who will with would you your".split())
_WORDS = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+", re.I)


def _terms(text: str) -> Counter:
    result = Counter()
    for word in _WORDS.findall(normalize(text).casefold()):
        if word in _STOPWORDS:
            continue
        # Whole terms retain short code tokens; character features support
        # Chinese without segmentation and small English inflection changes.
        result["word:" + word] += 1
        if word.isascii() and len(word) > 4 and word.endswith("s"):
            result["word:" + word[:-1]] += 1
        bounded = " " + word + " "
        for width in range(2, 6):
            for offset in range(len(bounded) - width + 1):
                token = bounded[offset:offset + width]
                if len(token.strip()) >= 2:
                    result["char:" + token] += 1
    return result


def _aliases(ledger, sources):
    aliases = defaultdict(set)
    if ledger is None:
        return aliases
    if not isinstance(ledger, list):
        raise ValueError("Candidate vocabulary needs an extracted ledger list")
    for claim in ledger:
        if (not isinstance(claim, dict) or claim.get("status") != "quote_verified_draft"
                or claim.get("origin") not in {"audio", "courseware"}):
            raise ValueError("Candidate vocabulary accepts quote-verified source-specific drafts only")
        terms = []
        for field in ("title_draft", "paraphrase_draft"):
            value = claim.get(field)
            if not isinstance(value, str):
                raise ValueError("Malformed candidate vocabulary text")
            if value.strip():
                terms.append(value.strip())
        evidence = claim.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("Candidate vocabulary needs quotation evidence")
        for reference in evidence:
            if not isinstance(reference, dict):
                raise ValueError("Malformed candidate vocabulary evidence")
            source_id, quote = reference.get("source_id"), reference.get("quote")
            if not isinstance(source_id, str) or source_id not in sources:
                raise ValueError("Candidate vocabulary refers to an unknown source")
            origin, original = sources[source_id]
            if (origin != claim["origin"] or not isinstance(quote, str)
                    or not normalize(quote) or normalize(quote) not in normalize(original)):
                raise ValueError("Candidate vocabulary quotation or origin differs from its source")
            aliases[source_id].update(terms)
    return aliases


def rank_candidates(units: list[Unit], chunks: list[Chunk], config: dict | None = None,
                    *, ledger: list[dict] | None = None) -> list[dict]:
    """Return up to three ranked page candidates; never auto-confirm a page.

    Only source text and optional mechanically grounded draft vocabulary enter
    scoring. IDs break score ties deterministically; time fields are validated
    for the review contract but are never features. No source map, page ordinal,
    evaluator answer, embedding service or model call is accepted here.

    ``ledger`` is optional. Supplying it makes the candidate vocabulary
    AI-derived, even though this ranking operation itself makes no API call.
    """
    config = config or {}
    top_k = config.get("top_k", 3)
    floor = config.get("candidate_score_floor", .03)
    maximum = config.get("candidate_max_features", 60000)
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise ValueError("Candidate count must be an integer from one to ten")
    if type(maximum) is not int or not 100 <= maximum <= 200000:
        raise ValueError("Invalid candidate vocabulary limit")
    if type(floor) not in (int, float) or not math.isfinite(floor) or not 0 <= floor <= 1:
        raise ValueError("Invalid candidate score floor")
    if not isinstance(units, list) or not isinstance(chunks, list):
        raise ValueError("Candidate sources must be lists")
    documents, queries = [], []
    for unit in units:
        if not isinstance(unit, Unit):
            raise ValueError("Candidate pages require content-only Unit records")
        documents.append(Unit.from_wire(asdict(unit)))
    for chunk in chunks:
        if not isinstance(chunk, (Chunk, Segment)):
            raise ValueError("Candidate transcript requires timed text records")
        validated = Segment.from_wire(asdict(chunk))
        if not validated.segment_id:
            raise ValueError("Candidate transcript ID cannot be empty")
        queries.append(validated)
    identifiers = [u.unit_id for u in documents] + [c.segment_id for c in queries]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Candidate source IDs must be unique and disjoint")
    documents.sort(key=lambda unit: unit.unit_id)
    sources = {u.unit_id: ("courseware", u.text) for u in documents}
    sources.update({c.segment_id: ("audio", c.text) for c in queries})
    aliases = _aliases(ledger, sources)
    ai_derived = any(aliases.values())

    def text(record, identifier):
        return record.text + "\n" + "\n".join(sorted(aliases.get(identifier, ())))

    counts = [_terms(text(unit, unit.unit_id)) for unit in documents]
    total = Counter()
    frequencies = Counter()
    for count in counts:
        total.update(count)
        frequencies.update(count.keys())
    # Limit vocabulary, not source text. Stable frequency ties keep import order
    # out of the result; sparse postings avoid scanning every term of every page.
    vocabulary = sorted(total, key=lambda token: (-total[token], token))[:maximum]
    idf = {token: math.log((1 + len(documents)) / (1 + frequencies[token])) + 1
           for token in vocabulary}

    def vector(count):
        weighted = {token: (1 + math.log(number)) * idf[token]
                    for token, number in count.items() if token in idf}
        length = math.sqrt(sum(value * value for value in weighted.values()))
        return {token: value / length for token, value in weighted.items()} if length else {}

    postings = defaultdict(list)
    for index, count in enumerate(counts):
        for token, value in vector(count).items():
            postings[token].append((index, value))
    results = []
    for query in queries:
        scores = defaultdict(float)
        for token, value in vector(_terms(text(query, query.segment_id))).items():
            for index, document_value in postings.get(token, ()):
                scores[index] += value * document_value
        candidates = []
        for index, score in sorted(scores.items(), key=lambda pair: (-pair[1], documents[pair[0]].unit_id)):
            score = round(max(0.0, min(1.0, score)), 6)
            if score > 0 and score >= floor:
                candidates.append({"unit_id": documents[index].unit_id, "score": score})
            if len(candidates) == top_k:
                break
        results.append({"segment_id": query.segment_id, "candidates": candidates,
                        "status": "review", "unit_id": None,
                        "method": "lexical_with_ai_derived_terms" if ai_derived else "lexical",
                        "uses_ai_derived_terms": ai_derived})
    return results
