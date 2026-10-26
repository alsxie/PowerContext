# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# 作者：FDE WorkMate  # noqa: RUF003

"""Select literal, query-relevant evidence spans from candidate texts under a byte budget.

The selector is pure and deterministic: it never rewrites text, it only returns half-open
character intervals into the original documents. Callers describe the output cost model
(fixed envelope bytes, per-item overhead, per-character content cost) so the selection can
be packed exactly against a UTF-8 budget that includes citations and formatting.

Scoring is lexical: sentences are weighted by the query terms they share, with inverse
sentence frequency computed over the whole candidate set. Weak, domain-neutral priors are
added on top: a document prior (best sentence, query coverage and retrieval rank) so the
strongest documents contribute contiguous context, a position prior because findings tend
to follow background, and a cue prior for sentences that report measurements or results.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise

__all__ = ["EvidenceDocument", "EvidenceSpan", "select_evidence", "split_segments"]

_MAX_SEGMENT_CHARS = 480
_DOCUMENT_WEIGHT = 1.0
_COVERAGE_SHARE = 0.3
_POSITION_WEIGHT = 0.8
_POSITION_KNEE = 0.5
_CUE_WEIGHT = 0.3
_STEM_PREFIX = 7
_RANK_WEIGHT = 0.1
_RESULT_CUES = re.compile(
    r"\d|%|\b(?:significant\w*|increas\w*|decreas\w*|reduc\w*|associat\w*|higher|lower|greater|improv\w*|"
    r"found|show\w*|demonstrat\w*|reveal\w*|indicat\w*|suggest\w*|conclu\w*|results?)\b",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?![a-z])|(?<=[\u3002\uff01\uff1f\uff1b])\s*|\n\s*")
_WORD = re.compile(r"[^\W_]+")
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff"
_SCRIPT_RUN = re.compile(f"(?P<cjk>[{_CJK}]+)|(?P<other>[^{_CJK}]+)")
_ASCII_PART = re.compile(r"[a-z]+|\d+")
_STOPWORDS = frozenset([
    "a",
    "about",
    "above",
    "after",
    "again",
    "against",
    "all",
    "also",
    "am",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "below",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "down",
    "during",
    "each",
    "either",
    "few",
    "for",
    "from",
    "further",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "hers",
    "herself",
    "him",
    "himself",
    "his",
    "how",
    "however",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "itself",
    "just",
    "may",
    "me",
    "might",
    "more",
    "most",
    "must",
    "my",
    "myself",
    "no",
    "nor",
    "not",
    "now",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "ours",
    "ourselves",
    "out",
    "over",
    "own",
    "per",
    "same",
    "she",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "themselves",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "thus",
    "to",
    "too",
    "under",
    "until",
    "up",
    "upon",
    "us",
    "very",
    "via",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "whether",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "within",
    "without",
    "would",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
])


@dataclass(frozen=True)
class EvidenceDocument:
    """One candidate text and the output bytes one item citing it costs besides its content."""

    text: str
    item_overhead: int


@dataclass(frozen=True)
class EvidenceSpan:
    """A half-open character interval selected from one candidate document."""

    document: int
    start: int
    end: int


@dataclass
class _Segment:
    document: int
    index: int
    start: int
    end: int
    terms: Counter[str]
    cues: float = 0.0
    score: float = 0.0


@dataclass
class _Window:
    document: int
    first: int
    last: int
    start: int
    end: int
    priority: float


def split_segments(text: str) -> tuple[tuple[int, int], ...]:
    """Split text into sentence-like ``(start, end)`` spans without leading/trailing spaces."""

    spans: list[tuple[int, int]] = []
    position = 0
    for match in _SENTENCE_BOUNDARY.finditer(text):
        if match.end() == match.start() and match.start() == position:
            continue
        _append_trimmed(text, position, match.start(), spans)
        position = match.end()
    _append_trimmed(text, position, len(text), spans)
    return tuple(spans)


def _append_trimmed(text: str, start: int, end: int, spans: list[tuple[int, int]]) -> None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    while end - start > _MAX_SEGMENT_CHARS:
        cut = text.rfind(" ", start + _MAX_SEGMENT_CHARS // 2, start + _MAX_SEGMENT_CHARS)
        cut = start + _MAX_SEGMENT_CHARS if cut <= start else cut
        spans.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if start < end:
        spans.append((start, end))


def _stem(word: str) -> str:
    if word.isdigit() or len(word) <= 3:
        return word
    for suffix, replacement in (
        ("ational", "ate"),
        ("ization", "ize"),
        ("ations", "ate"),
        ("ation", "ate"),
        ("iveness", "ive"),
        ("fulness", "ful"),
        ("ousness", "ous"),
        ("ities", "ity"),
        ("ments", "ment"),
        ("ies", "y"),
        ("ing", ""),
        ("ed", ""),
        ("es", ""),
        ("ly", ""),
        ("s", ""),
    ):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "s" and word.endswith(("ss", "us", "is")):
                return word
            word = word[: len(word) - len(suffix)] + replacement
            break
    if word.endswith("e") and len(word) > 4:
        word = word[:-1]
    return word[:_STEM_PREFIX]


def _terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", text).casefold()
    terms: list[str] = []
    for match in _WORD.finditer(normalized):
        for part in _SCRIPT_RUN.finditer(match.group()):
            run = part.group()
            if part.lastgroup == "cjk":
                # CJK has no spaces between words: use characters and adjacent pairs.
                terms.extend(run)
                terms.extend(left + right for left, right in pairwise(run))
                continue
            words = _ASCII_PART.findall(run) if run.isascii() else [run]
            terms.extend(_stem(word) for word in words if word not in _STOPWORDS)
    return terms


def select_evidence(
    query: str,
    documents: Sequence[EvidenceDocument],
    *,
    budget: int,
    content_cost: Callable[[str], int],
    separator_cost: int = 1,
) -> tuple[EvidenceSpan, ...]:
    """Choose literal spans maximizing query evidence within ``budget`` output bytes.

    ``budget`` excludes the fixed envelope. Each selected span costs its document's
    ``item_overhead`` plus ``content_cost`` of its exact text; items after the first also pay
    ``separator_cost``. Documents are expected in retrieval-rank order.
    """

    segments = _segments(documents)
    if not segments or budget <= 0:
        return ()
    _score(query, segments, len(documents))
    windows = _Packer(documents, segments, content_cost, separator_cost).pack(budget)
    windows.sort(key=lambda window: (-window.priority, window.document, window.start))
    return tuple(EvidenceSpan(document=window.document, start=window.start, end=window.end) for window in windows)


def _segments(documents: Sequence[EvidenceDocument]) -> list[_Segment]:
    segments: list[_Segment] = []
    for number, document in enumerate(documents):
        for index, (start, end) in enumerate(split_segments(document.text)):
            segments.append(
                _Segment(
                    document=number,
                    index=index,
                    start=start,
                    end=end,
                    terms=Counter(_terms(document.text[start:end])),
                    cues=min(1.0, len(_RESULT_CUES.findall(document.text, start, end)) / 3),
                )
            )
    return segments


def _score(query: str, segments: list[_Segment], document_count: int) -> None:
    query_terms = set(_terms(query))
    if not query_terms:
        return
    frequency: Counter[str] = Counter()
    for segment in segments:
        frequency.update(query_terms & segment.terms.keys())
    total = len(segments)
    weights = {term: math.log(1.0 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5)) for term in query_terms}
    average = sum(sum(segment.terms.values()) for segment in segments) / total or 1.0
    for segment in segments:
        length = sum(segment.terms.values())
        norm = 1.2 * (0.25 + 0.75 * length / average)
        segment.score = sum(
            weights[term] * segment.terms[term] * 2.2 / (segment.terms[term] + norm)
            for term in query_terms
            if term in segment.terms
        )
    best = [0.0] * document_count
    count = [0] * document_count
    present: list[set[str]] = [set() for _ in range(document_count)]
    for segment in segments:
        best[segment.document] = max(best[segment.document], segment.score)
        count[segment.document] += 1
        present[segment.document].update(query_terms & segment.terms.keys())
    top = max(best) or 1.0
    mass = sum(weights.values()) or 1.0
    relevance = [
        (1.0 - _COVERAGE_SHARE) * best[number] / top
        + _COVERAGE_SHARE * sum(weights[term] for term in present[number]) / mass
        + _RANK_WEIGHT / (1 + number)
        for number in range(document_count)
    ]
    leader = max(relevance) or 1.0
    for segment in segments:
        # Strong documents lend weight to all their sentences, so packing keeps context there;
        # findings tend to follow background, and sentences reporting results carry evidence.
        relative = segment.index / max(1, count[segment.document] - 1)
        segment.score = (
            segment.score / top
            + _DOCUMENT_WEIGHT * relevance[segment.document] / leader
            + _POSITION_WEIGHT * min(1.0, relative / _POSITION_KNEE)
            + _CUE_WEIGHT * segment.cues
        )


class _Packer:
    """Grow contiguous windows greedily by sentence score while the exact cost fits."""

    def __init__(
        self,
        documents: Sequence[EvidenceDocument],
        segments: list[_Segment],
        content_cost: Callable[[str], int],
        separator_cost: int,
    ) -> None:
        self.documents = documents
        self.segments = segments
        self.content_cost = content_cost
        self.separator_cost = separator_cost
        self.lines: dict[int, list[_Segment]] = {}
        for segment in segments:
            self.lines.setdefault(segment.document, []).append(segment)
        self.cache: dict[tuple[int, int, int], int] = {}

    def text_cost(self, document: int, start: int, end: int) -> int:
        key = (document, start, end)
        if key not in self.cache:
            self.cache[key] = self.content_cost(self.documents[document].text[start:end])
        return self.cache[key]

    def total(self, windows: Sequence[_Window]) -> int:
        if not windows:
            return 0
        return self.separator_cost * (len(windows) - 1) + sum(
            self.documents[window.document].item_overhead + self.text_cost(window.document, window.start, window.end)
            for window in windows
        )

    def options(self, windows: list[_Window], segment: _Segment) -> list[list[_Window]]:
        """Open a new item for ``segment``, or stretch a same-document window to reach it."""

        line = self.lines[segment.document]
        single = _Window(segment.document, segment.index, segment.index, segment.start, segment.end, segment.score)
        options = [[*windows, single]]
        for position, window in enumerate(windows):
            if window.document != segment.document:
                continue
            first, last = min(window.first, segment.index), max(window.last, segment.index)
            priority = max(window.priority, segment.score)
            grown = _Window(segment.document, first, last, line[first].start, line[last].end, priority)
            options.append([*windows[:position], grown, *windows[position + 1 :]])
        return [_merged(option, self.lines) for option in options]

    def pack(self, budget: int) -> list[_Window]:
        windows: list[_Window] = []
        for segment in sorted(self.segments, key=lambda segment: (-segment.score, segment.document, segment.index)):
            if _covered(windows, segment.document, segment.index):
                continue
            fitting = [option for option in self.options(windows, segment) if self.total(option) <= budget]
            if fitting:
                windows = min(fitting, key=self.total)
        if windows:
            _fill_partial(self.documents, self.lines, windows, budget - self.total(windows), self.text_cost)
            return windows
        # Not even one whole sentence fits: keep the best sentence's longest fitting prefix.
        top = max(self.segments, key=lambda segment: (segment.score, -segment.document, -segment.index))
        seed = _Window(top.document, top.index, top.index, top.start, top.start, top.score)
        allowance = budget - self.documents[top.document].item_overhead
        _extend_end(self.documents, seed, top.end, allowance, self.text_cost)
        return [seed] if seed.end > seed.start else []


def _merged(windows: list[_Window], lines: dict[int, list[_Segment]]) -> list[_Window]:
    """Join same-document windows that touch, since one item is cheaper than two."""

    result: list[_Window] = []
    for window in windows:
        for other in result:
            if other.document == window.document and other.first <= window.last + 1 and window.first <= other.last + 1:
                other.first, other.last = min(other.first, window.first), max(other.last, window.last)
                line = lines[window.document]
                other.start, other.end = line[other.first].start, line[other.last].end
                other.priority = max(other.priority, window.priority)
                break
        else:
            result.append(
                _Window(window.document, window.first, window.last, window.start, window.end, window.priority)
            )
    return result


def _fill_partial(
    documents: Sequence[EvidenceDocument],
    lines: dict[int, list[_Segment]],
    windows: list[_Window],
    remaining: int,
    text_cost: Callable[[int, int, int], int],
) -> None:
    """Spend leftover bytes extending one window into its best neighbouring sentence."""

    best: tuple[float, _Window, int] | None = None
    for window in windows:
        line = lines[window.document]
        for index in (window.first - 1, window.last + 1):
            if 0 <= index < len(line) and not _covered(windows, window.document, index):
                score = line[index].score
                if best is None or score > best[0]:
                    best = (score, window, index)
    if best is None or remaining <= 0:
        return
    _score_value, window, index = best
    text = documents[window.document].text
    segment = lines[window.document][index]
    current = text_cost(window.document, window.start, window.end)
    if index > window.last:
        _extend_end(documents, window, segment.end, current + remaining, text_cost)
    else:
        low, high, found = segment.start, window.start - 1, window.start
        while low <= high:
            middle = (low + high) // 2
            if text_cost(window.document, middle, window.end) - current <= remaining:
                found, high = middle, middle - 1
            else:
                low = middle + 1
        window.start = min(window.start, _word_start(text, found, window.start))


def _extend_end(
    documents: Sequence[EvidenceDocument],
    window: _Window,
    limit: int,
    allowance: int,
    text_cost: Callable[[int, int, int], int],
) -> None:
    """Move ``window.end`` towards ``limit`` while its text costs at most ``allowance``."""

    low, high, found = window.end + 1, limit, window.end
    while low <= high:
        middle = (low + high) // 2
        if text_cost(window.document, window.start, middle) <= allowance:
            found, low = middle, middle + 1
        else:
            high = middle - 1
    window.end = max(window.end, _word_end(documents[window.document].text, window.end, found))


def _covered(windows: Sequence[_Window], document: int, index: int) -> bool:
    return any(window.document == document and window.first <= index <= window.last for window in windows)


def _word_end(text: str, floor: int, end: int) -> int:
    """Retreat ``end`` to a word boundary so a partial sentence never splits a word."""

    cut = end
    if end < len(text) and _wordish(text[end]) and _wordish(text[end - 1]):
        while cut > floor and _wordish(text[cut - 1]):
            cut -= 1
    while cut > floor and text[cut - 1].isspace():
        cut -= 1
    return cut


def _word_start(text: str, start: int, ceiling: int) -> int:
    """Advance ``start`` to a word boundary so a partial sentence never splits a word."""

    cut = start
    if cut > 0 and _wordish(text[cut - 1]) and _wordish(text[cut]):
        while cut < ceiling and _wordish(text[cut]):
            cut += 1
    while cut < ceiling and text[cut].isspace():
        cut += 1
    return cut


def _wordish(character: str) -> bool:
    return character.isascii() and character.isalnum()
