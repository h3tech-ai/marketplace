#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
synaptory Design Assets Core - BM25 search engine for UI/UX style guides
"""

import csv
import re
from pathlib import Path
from math import log
from collections import defaultdict

# ============ CONFIGURATION ============
DATA_DIR = Path(__file__).parent / "../data"
MAX_RESULTS = 3

CSV_CONFIG = {
    "style": {
        "file": "styles.csv",
        "search_cols": ["Style Category", "Keywords", "Best For", "Type"],
        "output_cols": ["Style Category", "Type", "Keywords", "Primary Colors", "Effects & Animation", "Best For", "Performance", "Accessibility", "Framework Compatibility", "Complexity"]
    },
    "color": {
        "file": "colors.csv",
        "search_cols": ["Product Type", "Keywords", "Notes"],
        "output_cols": ["Product Type", "Keywords", "Primary (Hex)", "Secondary (Hex)", "CTA (Hex)", "Background (Hex)", "Text (Hex)", "Border (Hex)", "Notes"]
    },
    "chart": {
        "file": "charts.csv",
        "search_cols": ["Data Type", "Keywords", "Best Chart Type", "Accessibility Notes"],
        "output_cols": ["Data Type", "Keywords", "Best Chart Type", "Secondary Options", "Color Guidance", "Accessibility Notes", "Library Recommendation", "Interactive Level"]
    },
    "product": {
        "file": "products.csv",
        "search_cols": ["Product Type", "Keywords", "Primary Style Recommendation", "Key Considerations"],
        "output_cols": ["Product Type", "Keywords", "Primary Style Recommendation", "Secondary Styles", "Landing Page Pattern", "Dashboard Style (if applicable)", "Color Palette Focus"]
    },
    "ux": {
        "file": "ux-guidelines.csv",
        "search_cols": ["Category", "Issue", "Description", "Platform"],
        "output_cols": ["Category", "Issue", "Platform", "Description", "Do", "Don't", "Code Example Good", "Code Example Bad", "Severity"]
    },
    "typography": {
        "file": "typography.csv",
        "search_cols": ["Font Pairing Name", "Category", "Mood/Style Keywords", "Best For", "Heading Font", "Body Font"],
        "output_cols": ["Font Pairing Name", "Category", "Heading Font", "Body Font", "Mood/Style Keywords", "Best For", "Google Fonts URL", "CSS Import", "Tailwind Config", "Notes"]
    }
}

#: Stack-specific corpora. Empty: this distribution ships none (see
#: UNSHIPPED_STACKS). The machinery is kept because it is the extension point --
#: adding a stack is dropping `data/stacks/<name>.csv` and one line here, and
#: the guard in plugin-claude/tests/lib/test_design_assets_corpora.py fails if
#: those two ever disagree.
STACK_CONFIG = {}

# Common columns for all stacks
_STACK_COLS = {
    "search_cols": ["Category", "Guideline", "Description", "Do", "Don't"],
    "output_cols": ["Category", "Guideline", "Description", "Do", "Don't", "Code Good", "Code Bad", "Severity", "Docs URL"]
}

AVAILABLE_STACKS = list(STACK_CONFIG.keys())

#: Names this tool advertised for two releases and could never answer (#372).
#:
#: `core.py` came from an upstream project with a 23-corpus dataset. The v1.0.0
#: fork took the tooling and 7 of the CSVs; the other 16 were never committed
#: here, in any branch. Because a missing corpus produced an error string rather
#: than a crash, 17 of the 23 advertised options simply printed the absolute
#: path of a file that had never existed.
#:
#: They are recorded rather than deleted so a caller who still passes one gets
#: told what happened. Re-adding a name means committing the corpus first.
UNSHIPPED_DOMAINS = {
    "prompt": "prompts.csv",
    "landing": "landing.csv",
    "icons": "icons.csv",
    "react": "react-performance.csv",
    "web": "web-interface.csv",
}

UNSHIPPED_STACKS = (
    "html-tailwind", "react", "nextjs", "vue", "nuxtjs", "nuxt-ui",
    "svelte", "swiftui", "react-native", "flutter", "shadcn",
    "jetpack-compose",
)


#: Keyword table backing detect_domain(). Every key must name a domain in
#: CSV_CONFIG, checked once below rather than per call.
#:
#: Routing a free-text query into a corpus we do not have is the failure users
#: actually hit: before #372 a bare `search.py "landing page hero cta"` -- no
#: flags, nothing named -- auto-detected `landing` and answered with the
#: absolute path of a file that had never existed. Queries that used to land on
#: a removed domain now fall through to detect_domain()'s `style` default, which
#: answers from a corpus that exists and reports which one it used.
_DOMAIN_KEYWORDS = {
    "color": ["color", "palette", "hex", "#", "rgb"],
    "chart": ["chart", "graph", "visualization", "trend", "bar", "pie", "scatter", "heatmap", "funnel"],
    "product": ["saas", "ecommerce", "e-commerce", "fintech", "healthcare", "gaming", "portfolio", "crypto", "dashboard"],
    "style": ["style", "design", "ui", "minimalism", "glassmorphism", "neumorphism", "brutalism", "dark mode", "flat", "aurora"],
    "ux": ["ux", "usability", "accessibility", "wcag", "touch", "scroll", "animation", "keyboard", "navigation", "mobile"],
    "typography": ["font", "typography", "heading", "serif", "sans"],
}

if set(_DOMAIN_KEYWORDS) - set(CSV_CONFIG):  # pragma: no cover - import-time invariant
    raise RuntimeError(
        "detect_domain would route to unserved domains: "
        f"{sorted(set(_DOMAIN_KEYWORDS) - set(CSV_CONFIG))}"
    )


def _unshipped_error(name, kind, available):
    """One phrasing for 'we know that name and cannot serve it'."""
    offer = ", ".join(sorted(available)) if available else "none in this distribution"
    return (
        f"The {kind} '{name}' is not shipped in this distribution -- its corpus "
        f"was never part of the Synaptory dataset (issue #372). "
        f"Available {kind}s: {offer}."
    )


# ============ BM25 IMPLEMENTATION ============
class BM25:
    """BM25 ranking algorithm for text search"""

    def __init__(self, k1=1.5, b=0.75):
        self.k1 = k1
        self.b = b
        self.corpus = []
        self.doc_lengths = []
        self.avgdl = 0
        self.idf = {}
        self.doc_freqs = defaultdict(int)
        self.N = 0

    def tokenize(self, text):
        """Lowercase, split, remove punctuation, filter short words"""
        text = re.sub(r'[^\w\s]', ' ', str(text).lower())
        return [w for w in text.split() if len(w) > 2]

    def fit(self, documents):
        """Build BM25 index from documents"""
        self.corpus = [self.tokenize(doc) for doc in documents]
        self.N = len(self.corpus)
        if self.N == 0:
            return
        self.doc_lengths = [len(doc) for doc in self.corpus]
        self.avgdl = sum(self.doc_lengths) / self.N

        for doc in self.corpus:
            seen = set()
            for word in doc:
                if word not in seen:
                    self.doc_freqs[word] += 1
                    seen.add(word)

        for word, freq in self.doc_freqs.items():
            self.idf[word] = log((self.N - freq + 0.5) / (freq + 0.5) + 1)

    def score(self, query):
        """Score all documents against query"""
        query_tokens = self.tokenize(query)
        scores = []

        for idx, doc in enumerate(self.corpus):
            score = 0
            doc_len = self.doc_lengths[idx]
            term_freqs = defaultdict(int)
            for word in doc:
                term_freqs[word] += 1

            for token in query_tokens:
                if token in self.idf:
                    tf = term_freqs[token]
                    idf = self.idf[token]
                    numerator = tf * (self.k1 + 1)
                    denominator = tf + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl)
                    score += idf * numerator / denominator

            scores.append((idx, score))

        return sorted(scores, key=lambda x: x[1], reverse=True)


# ============ SEARCH FUNCTIONS ============
def _load_csv(filepath):
    """Load CSV and return list of dicts"""
    with open(filepath, 'r', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def _search_csv(filepath, search_cols, output_cols, query, max_results):
    """Core search function using BM25"""
    if not filepath.exists():
        return []

    data = _load_csv(filepath)

    # Build documents from search columns
    documents = [" ".join(str(row.get(col, "")) for col in search_cols) for row in data]

    # BM25 search
    bm25 = BM25()
    bm25.fit(documents)
    ranked = bm25.score(query)

    # Get top results with score > 0
    results = []
    for idx, score in ranked[:max_results]:
        if score > 0:
            row = data[idx]
            results.append({col: row.get(col, "") for col in output_cols if col in row})

    return results


def detect_domain(query):
    """Auto-detect the most relevant domain from query"""
    query_lower = query.lower()

    scores = {domain: sum(1 for kw in keywords if kw in query_lower) for domain, keywords in _DOMAIN_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "style"


def search(query, domain=None, max_results=MAX_RESULTS):
    """Main search function with auto-domain detection"""
    if domain is None:
        domain = detect_domain(query)

    # No fallback to `style`. It used to read styles.csv while reporting the
    # caller's domain, so an unserved domain produced a plausible wrong answer
    # instead of an error -- which is what trimming CSV_CONFIG would otherwise
    # have turned every removed domain into.
    if domain not in CSV_CONFIG:
        kind = "domain"
        if domain in UNSHIPPED_DOMAINS:
            return {"error": _unshipped_error(domain, kind, CSV_CONFIG), "domain": domain}
        available = ", ".join(sorted(CSV_CONFIG))
        return {
            "error": f"Unknown {kind} '{domain}'. Available {kind}s: {available}.",
            "domain": domain,
        }

    config = CSV_CONFIG[domain]
    filepath = DATA_DIR / config["file"]

    if not filepath.exists():
        return {"error": f"File not found: {filepath}", "domain": domain}

    results = _search_csv(filepath, config["search_cols"], config["output_cols"], query, max_results)

    return {
        "domain": domain,
        "query": query,
        "file": config["file"],
        "count": len(results),
        "results": results
    }


def search_stack(query, stack, max_results=MAX_RESULTS):
    """Search stack-specific guidelines"""
    if stack not in STACK_CONFIG:
        if stack in UNSHIPPED_STACKS:
            return {"error": _unshipped_error(stack, "stack", AVAILABLE_STACKS), "stack": stack}
        available = ", ".join(AVAILABLE_STACKS) or "none in this distribution"
        return {"error": f"Unknown stack: {stack}. Available: {available}.", "stack": stack}

    filepath = DATA_DIR / STACK_CONFIG[stack]["file"]

    if not filepath.exists():
        return {"error": f"Stack file not found: {filepath}", "stack": stack}

    results = _search_csv(filepath, _STACK_COLS["search_cols"], _STACK_COLS["output_cols"], query, max_results)

    return {
        "domain": "stack",
        "stack": stack,
        "query": query,
        "file": STACK_CONFIG[stack]["file"],
        "count": len(results),
        "results": results
    }
