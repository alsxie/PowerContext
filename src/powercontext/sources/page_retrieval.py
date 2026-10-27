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

"""Offline lexical ranking of document pages for financial-report questions.

Every page text starts with its document identifier on the first line. Ranking
combines three generic signals: which document the question names, BM25 over
stemmed unigrams and bigrams with financial-vocabulary query expansion, and the
kind of primary financial statement a question relies on.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

_WORD = re.compile(r"[a-z]+|\d+")
_RAW_WORD = re.compile(r"[A-Za-z][A-Za-z&]*|\d+")
_K1 = 1.2
_B = 0.75
_BIGRAM_WEIGHT = 0.6
_PRIOR_WEIGHT = 0.05

_STOPWORDS = frozenset([
    "a",
    "about",
    "above",
    "according",
    "address",
    "adopting",
    "after",
    "again",
    "against",
    "all",
    "also",
    "am",
    "among",
    "an",
    "and",
    "any",
    "approach",
    "approximation",
    "are",
    "as",
    "ask",
    "asked",
    "assume",
    "assuming",
    "at",
    "based",
    "basing",
    "be",
    "been",
    "being",
    "below",
    "best",
    "between",
    "both",
    "but",
    "by",
    "calculate",
    "calculating",
    "can",
    "clearly",
    "compute",
    "could",
    "data",
    "define",
    "defined",
    "definition",
    "details",
    "did",
    "do",
    "does",
    "doing",
    "drawing",
    "during",
    "each",
    "exact",
    "explain",
    "extract",
    "following",
    "for",
    "formerly",
    "from",
    "fy",
    "give",
    "had",
    "has",
    "have",
    "having",
    "help",
    "here",
    "how",
    "if",
    "in",
    "information",
    "into",
    "is",
    "it",
    "its",
    "judgment",
    "judgments",
    "known",
    "like",
    "line",
    "lines",
    "may",
    "me",
    "metric",
    "more",
    "most",
    "my",
    "need",
    "not",
    "of",
    "off",
    "on",
    "only",
    "or",
    "other",
    "our",
    "out",
    "outlined",
    "over",
    "own",
    "per",
    "perspective",
    "plainly",
    "please",
    "possible",
    "primarily",
    "provide",
    "provided",
    "question",
    "rather",
    "reasonable",
    "referencing",
    "relying",
    "resource",
    "resources",
    "respond",
    "response",
    "round",
    "shown",
    "so",
    "solution",
    "some",
    "standard",
    "state",
    "stated",
    "statements",
    "such",
    "take",
    "taking",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "to",
    "under",
    "up",
    "upon",
    "us",
    "use",
    "used",
    "useful",
    "using",
    "utilize",
    "utilizing",
    "very",
    "want",
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
    "why",
    "will",
    "with",
    "within",
    "would",
    "year",
    "years",
    "you",
    "your",
    "answer",
    "answers",
    "units",
    "unit",
    "decimal",
    "place",
    "places",
    "percent",
    "percents",
    "usd",
    "millions",
    "million",
    "billions",
    "billion",
    "thousands",
    "analyst",
    "analysts",
    "financial",
    "conclusions",
    "agreement",
    "items",
    "item",
    "shown",
    "amount",
    "quantity",
    "relevant",
    "meaningful",
])

# Generic financial vocabulary: a question concept and the wording reports use for it.
_EXPANSIONS: tuple[tuple[str, str], ...] = (
    (
        r"\bcapex\b|capital expenditure|capital spending",
        "capital expenditures capital spending purchases property plant equipment additions",
    ),
    (r"\bd ?& ?a\b|depreciation", "depreciation amortization"),
    (r"ebitda", "operating income operating profit depreciation amortization"),
    (
        r"ebit\b|operating income|operating margin|operating profit",
        "operating income operating profit income from operations",
    ),
    (r"gross margin|gross profit", "gross margin gross profit cost of sales revenue"),
    (r"\bcogs\b|cost of goods", "cost of sales cost of goods sold cost of revenue"),
    (r"revenue|top ?line|\bsales\b", "revenue revenues net sales total"),
    (r"\beps\b", "earnings per share diluted"),
    (r"pp ?& ?e|\bppne\b|fixed asset", "property plant equipment net"),
    (
        r"capital.intensi|capital intensity|asset.(light|heavy)",
        "capital expenditures purchases property plant equipment net total assets depreciation",
    ),
    (r"\bar\b|receivable|\bdso\b", "accounts receivable net"),
    (r"inventor|\bdio\b", "inventories inventory"),
    (r"payable|\bdpo\b", "accounts payable"),
    (r"quick ratio|liquidity", "liquidity cash equivalents marketable securities current assets current liabilities"),
    (r"working capital", "current assets current liabilities working capital"),
    (r"\broa\b|return on assets|total assets", "total assets net income"),
    (r"\bdebt\b|borrow", "debt long term borrowings notes"),
    (r"dividend|payout|retention ratio", "dividends paid cash dividends declared net income"),
    (
        r"cash flow|operating activities|financing activities|investing activities|\bfcf\b",
        "net cash provided by operating investing financing activities",
    ),
    (r"tax rate", "effective tax rate income taxes provision"),
    (r"legal|litigation|lawsuit", "legal proceedings litigation lawsuits claims contingencies"),
    (r"geograph|region", "geographic regions international countries north america europe asia latin america"),
    (r"product|services", "products services segments brands"),
    (
        r"customer",
        "one customer accounted for revenue we derive a significant substantial portion of our revenues from customers",
    ),
    (r"concentration", "concentration significant customer accounted for revenue"),
    (r"\bdr(ove|ive|iven|ivers?)\b|\bwhy\b|\breasons?\b", "primarily due driven increase decrease compared"),
    (r"turnover", "cost of sales inventories revenue average"),
    (r"acqui|m ?& ?a\b|divest", "acquisition acquisitions acquired business combinations divestitures"),
    (r"restructuring", "restructuring impairment charges"),
    (r"segment", "segment segments reportable operating"),
    (r"growth|grow", "growth increase increased compared"),
    (r"guidance|outlook", "guidance outlook expects"),
)

# "Excluding acquisitions/divestitures/currency" asks for organic figures; the
# excluded concept must not pull its own disclosures to the top.
_EXCLUDED_EFFECTS = re.compile(
    r"\b(?:exclud\w*|ignor\w*|net of|without|apart from|other than)\s+(?:the\s+)?"
    r"(?:(?:impact|effect|contribution)s?\s+of\s+)?(?:the\s+)?"
    r"(?:m ?& ?a\b|mergers? and acquisitions?|acquisitions?|divestitures?|currency|foreign exchange|fx\b)"
    r"(?:\s*(?:and|or|,)\s*(?:m ?& ?a\b|acquisitions?|divestitures?|currency|foreign exchange|fx\b))*"
)
_ORGANIC_EXPANSION = "organic sales growth change"

_STATEMENTS: dict[str, re.Pattern[str]] = {
    "income": re.compile(
        r"statements? of (consolidated )?(income|operations|earnings|comprehensive income)|income statements?"
    ),
    "cash": re.compile(r"statements? of (consolidated )?cash flows?"),
    "balance": re.compile(r"balance sheets?|statements? of (consolidated )?financial (position|condition)"),
}

_QUESTION_STATEMENTS: tuple[tuple[str, str], ...] = (
    (r"income statement|statement of income|statement of operations|p ?& ?l|profit and loss", "income"),
    (r"cash flow statement|statement of cash flows?|cash flows? statement", "cash"),
    (r"balance sheet|statement of financial position", "balance"),
    (r"\bcapex\b|capital expenditure|\bd ?& ?a\b|depreciation|ebitda|cash from operations|\bfcf\b|free cash", "cash"),
    (r"operating activities|financing activities|investing activities|dividends paid|cash dividends", "cash"),
    (r"\bcogs\b|revenue|net income|operating income|operating margin|gross margin|net profit|\beps\b", "income"),
    # Capital intensity relates capital spending and fixed assets to revenue.
    (r"capital.intensi|capital intensity|asset.(light|heavy)", "cash"),
    (r"capital.intensi|capital intensity|asset.(light|heavy)", "balance"),
    (r"capital.intensi|capital intensity|asset.(light|heavy)", "income"),
    (r"\bppne\b|pp ?& ?e|total assets|current assets|current liabilities|working capital|quick ratio", "balance"),
    (r"inventor|accounts payable|receivable|\broa\b|largest liability", "balance"),
    (r"turnover|\broa\b|return on assets|\bdpo\b|\bdso\b|\bdio\b|conversion cycle", "income"),
)


_PLURAL_SUFFIXES = (("ies", "y"), ("sses", "ss"), ("es", "e"), ("s", ""))
_VERB_SUFFIXES = (("ality", "al"), ("ing", ""), ("ed", ""))


def _strip_suffix(token: str, suffixes: tuple[tuple[str, str], ...]) -> str:
    for suffix, replacement in suffixes:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            if suffix == "s" and token.endswith(("ss", "us", "is")):
                return token
            return token[: len(token) - len(suffix)] + replacement
    return token


def _stem(token: str) -> str:
    # Light stemming: a plural ending first, then one derivational/verbal ending,
    # so "proceedings", "proceeding" and "proceed" share one term.
    if token.isdigit() or len(token) <= 3:
        return token
    return _strip_suffix(_strip_suffix(token, _PLURAL_SUFFIXES), _VERB_SUFFIXES)


def _terms(text: str) -> list[str]:
    return [_stem(token) for token in _WORD.findall(text.lower())]


_STOP_TERMS = frozenset(_stem(word) for word in _STOPWORDS)


def _features(terms: Sequence[str]) -> Counter[str]:
    features: Counter[str] = Counter(terms)
    features.update(f"{left} {right}" for left, right in itertools.pairwise(terms))
    return features


@dataclass(frozen=True)
class _Page:
    document: str
    features: Counter[str]
    length: int
    statement: frozenset[str]


@dataclass(frozen=True)
class _Index:
    pages: tuple[_Page, ...]
    document_frequency: Counter[str]
    average_length: float


def _statement_kinds(body: str) -> frozenset[str]:
    lines = [line.strip().lower() for line in body.splitlines() if line.strip()][:8]
    head = " ".join(lines)
    tokens = _WORD.findall(body.lower())
    if not tokens:
        return frozenset()
    numeric = sum(token.isdigit() for token in tokens) / len(tokens)
    if numeric < 0.25 or "notes to" in head:
        return frozenset()
    kinds = frozenset(kind for kind, pattern in _STATEMENTS.items() if pattern.search(head))
    # A page heading several statements is an index or table of contents.
    return kinds if len(kinds) == 1 else frozenset()


def _build_index(texts: Sequence[str]) -> _Index:
    pages = []
    frequency: Counter[str] = Counter()
    for text in texts:
        document, _, body = text.partition("\n")
        terms = _terms(body)
        features = _features(terms)
        frequency.update(features.keys())
        pages.append(_Page(document.strip(), features, len(terms), _statement_kinds(body)))
    average = sum(page.length for page in pages) / max(1, len(pages))
    return _Index(tuple(pages), frequency, max(average, 1.0))


_CACHE: dict[str, _Index] = {}


def _index_for(texts: Sequence[str]) -> _Index:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(hashlib.sha256(text.encode("utf-8")).digest())
    key = digest.hexdigest()
    index = _CACHE.get(key)
    if index is None:
        _CACHE.clear()
        index = _CACHE[key] = _build_index(texts)
    return index


def _document_parts(document: str) -> list[str]:
    parts = []
    for part in re.split(r"[^a-z0-9]+", document.lower()):
        parts.extend(piece for piece in re.split(r"(\d{4})(?=q\d)", part) if piece)
    return parts


def _company(parts: Sequence[str]) -> list[str]:
    company = list(parts[:1])
    for part in parts[1:]:
        if any(character.isdigit() for character in part):
            break
        company.append(part)
    return company


def _mentions(part: str, words: set[str], compact: str) -> bool:
    if part.isdigit():
        return part in words or (len(part) == 4 and f"fy{part[2:]}" in compact)
    return part in words or (len(part) >= 5 and part in compact)


def _abbreviates(name: str, part: str) -> bool:
    if len(name) < 3 or name[0] != part[0] or len(name) >= len(part):
        return False
    position = 0
    for character in name:
        position = part.find(character, position)
        if position < 0:
            return False
        position += 1
    return True


def _document_scores(question: str, documents: Sequence[str]) -> dict[str, float]:
    """Score how specifically ``question`` names each document identifier."""
    lowered = question.lower()
    words = set(_WORD.findall(lowered)) | set(re.findall(r"[a-z0-9]+", lowered))
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    # Capitalised words that may abbreviate a company name, such as "AMEX" or "JnJ".
    names = [
        word.lower().replace("&", "")
        for word in _RAW_WORD.findall(question)
        if len(word) >= 3 and word[:1].isupper() and any(character.isupper() for character in word[1:])
    ]
    parts = {document: _document_parts(document) for document in documents}
    spread = Counter(part for values in parts.values() for part in set(values))
    names = [name for name in names if name not in spread]
    total = len(documents)
    scores = {}
    for document, values in parts.items():
        company = _company(values)
        joined = "".join(company)
        abbreviated = any(_abbreviates(name, joined) for name in names)
        score = 0.0
        for part in set(values):
            weight = math.log(1 + total / spread[part])
            if part.isdigit():
                weight *= 0.5
            if _mentions(part, words, compact) or (abbreviated and part in company):
                score += weight
            elif part not in company:
                score -= 0.25 * weight
        scores[document] = max(score, 0.0)
    return scores


_POSSESSIVE = re.compile(r"(?<=\w)['\u2019]s\b")


def _query(question: str) -> tuple[Counter[str], frozenset[str]]:
    lowered = _POSSESSIVE.sub("", question.lower())
    expansions = []
    if _EXCLUDED_EFFECTS.search(lowered):
        lowered = _EXCLUDED_EFFECTS.sub(" ", lowered)
        expansions.append(_ORGANIC_EXPANSION)
    core = [term for term in _terms(lowered) if term not in _STOP_TERMS]
    query: Counter[str] = _features(core)
    expansions.extend(expansion for pattern, expansion in _EXPANSIONS if re.search(pattern, lowered))
    for expansion in expansions:
        for feature, count in _features(_terms(expansion)).items():
            query[feature] += 0.5 * count
    kinds = frozenset(kind for pattern, kind in _QUESTION_STATEMENTS if re.search(pattern, lowered))
    return query, kinds


def _bm25(index: _Index, page: _Page, query: Counter[str]) -> float:
    total = len(index.pages)
    score = 0.0
    norm = _K1 * (1 - _B + _B * page.length / index.average_length)
    for feature, weight in query.items():
        frequency = page.features.get(feature)
        if not frequency:
            continue
        df = index.document_frequency[feature]
        idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
        boost = _BIGRAM_WEIGHT if " " in feature else 1.0
        score += boost * weight * idf * frequency * (_K1 + 1) / (frequency + norm)
    return score


def rank_pages(question: str, texts: Sequence[str], prior: Sequence[float] | None = None) -> list[int]:
    """Return page indexes ordered from most to least relevant to ``question``.

    ``prior`` optionally adds an external per-page relevance in ``[0, 1]``, such as
    the rank of a page in a Memory full-text search.
    """
    if not texts:
        return []
    index = _index_for(texts)
    documents = sorted({page.document for page in index.pages})
    document_scores = _document_scores(question, documents)
    best = max(document_scores.values(), default=0.0)
    query, kinds = _query(question)
    lexical = [_bm25(index, page, query) for page in index.pages]
    top = max(lexical, default=0.0) or 1.0
    scored = []
    for position, page in enumerate(index.pages):
        score = lexical[position] / top
        if prior is not None:
            score += _PRIOR_WEIGHT * prior[position]
        if kinds & page.statement:
            score += 0.6
        if best > 0:
            score += 2.0 * document_scores[page.document] / best
        scored.append((-score, position))
    scored.sort()
    return [position for _, position in scored]


__all__ = ["rank_pages"]
