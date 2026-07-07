#!/usr/bin/env python3
"""
SEO Hub Finder — AI Pattern Discovery + Search Volume Gate

A local prototype that turns Google Search Console exports into programmatic SEO
opportunity candidates, creates a search-volume validation queue, and merges a
Keyword Planner / keyword tool export back into final content-hub recommendations.

Works without paid APIs. Optional LLM/AI review can be added later; this version
exports an AI review prompt so an LLM can evaluate the discovered patterns.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import io
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Column normalization
# ---------------------------------------------------------------------------

GSC_ALIASES: Dict[str, List[str]] = {
    "query": [
        "query", "keyword", "keywords", "top queries", "suchanfrage", "suchanfragen",
        "häufigste suchanfragen", "haeufigste suchanfragen", "search query", "search term",
    ],
    "page": [
        "page", "pages", "url", "landing page", "current url", "top pages",
        "die häufigsten seiten", "die haeufigsten seiten", "seite", "seiten",
    ],
    "clicks": ["clicks", "klicks", "traffic", "current organic traffic", "organic traffic"],
    "impressions": ["impressions", "impressionen", "impr.", "gsc impressions"],
    "position": [
        "position", "average position", "avg position", "current position",
        "durchschnittliche position", "avg. position",
    ],
}

VOLUME_ALIASES: Dict[str, List[str]] = {
    "query": ["keyword", "query", "suchanfrage", "search term", "keywords", "keyword ideas"],
    "search_volume": [
        "search volume", "volume", "avg. monthly searches", "avg monthly searches",
        "average monthly searches", "suchvolumen", "durchschn. suchanfragen pro monat",
        "monthly searches", "avg monthly", "avg. monthly",
    ],
    "competition": ["competition", "wettbewerb", "competition indexed value"],
    "cpc": ["cpc", "top of page bid", "gebot", "cost per click"],
}

CONNECTORS = {
    "für", "fuer", "zu", "zur", "zum", "mit", "ohne", "gegen", "bei", "in", "im", "am",
    "auf", "nach", "von", "vor", "unter", "über", "ueber", "als", "statt", "for", "with",
    "without", "vs", "versus", "alternative", "alternativen", "or", "oder", "and", "und",
}

WEAK_ANCHORS = {
    "der", "die", "das", "den", "dem", "ein", "eine", "einer", "einen", "einem", "was", "wie",
    "wo", "wann", "warum", "ist", "sind", "sein", "haben", "test", "vergleich", "review",
    "erfahrung", "erfahrungen", "kaufen", "preis", "kosten", "deutsch", "german", "online",
    "beste", "bester", "bestes", "best",  # can still appear through connector/frequent skeletons
}

@dataclass
class PatternCandidate:
    pattern_id: str
    query_skeleton: str
    query_count: int
    slot_diversity: int
    distinct_url_count: int
    top10_count: int
    total_clicks: float
    total_impressions: float
    avg_position: float
    confidence: float
    is_programmatic_opportunity: bool
    reject_reason: str
    sample_queries: str
    sample_urls: str
    template_label: str
    intent: str
    hub_label: str
    hub_article_title: str
    hub_slug: str
    url_template: str
    article_title_template: str
    recommended_article_structure: str
    internal_linking_strategy: str
    risks: str


def strip_accents(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", str(value)) if not unicodedata.combining(c))


def normalize_text(value: str) -> str:
    value = str(value).strip().lower()
    value = value.replace("–", "-").replace("—", "-").replace("ß", "ss")
    value = re.sub(r"\s+", " ", value)
    return value


def slugify(value: str) -> str:
    value = strip_accents(normalize_text(value))
    value = value.replace("{slot_1}", "slot-1").replace("{slot_2}", "slot-2").replace("{slot_3}", "slot-3")
    value = re.sub(r"[^a-z0-9{}]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "programmatic-hub"


def parse_number(value) -> float:
    """Parse messy CSV numbers and conservative Keyword Planner ranges."""
    if pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace("\xa0", " ").replace(" ", "")
    if not text or text in {"-", "--", "nan", "none"}:
        return 0.0
    # Keyword Planner often exports ranges like "100 - 1K". Use lower bound.
    if "-" in text and re.search(r"\d", text):
        text = text.split("-")[0].strip()
    multiplier = 1
    if text.endswith("k"):
        multiplier = 1000
        text = text[:-1]
    elif text.endswith("m"):
        multiplier = 1_000_000
        text = text[:-1]
    text = re.sub(r"[^0-9.,]", "", text)
    # A separator only acts as thousands-grouping when every following group has
    # exactly 3 digits ("1.000", "12,345,678"); otherwise it's a decimal mark
    # ("1.5k" must stay 1.5, not become 15).
    for sep in (".", ","):
        if sep in text:
            head, *groups = text.split(sep)
            if groups and all(len(g) == 3 and g.isdigit() for g in groups) and head.isdigit():
                text = text.replace(sep, "")
    text = text.replace(",", ".")
    if text.count(".") > 1:
        text = text.replace(".", "", text.count(".") - 1)
    if not text or text == ".":
        return 0.0
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def read_csv_safely(path: Path) -> pd.DataFrame:
    last_error: Optional[Exception] = None
    for sep in [",", ";", "\t"]:
        for encoding in ["utf-8-sig", "utf-8", "latin1"]:
            try:
                df = pd.read_csv(path, sep=sep, encoding=encoding)
                if len(df.columns) > 1:
                    return df
            except Exception as exc:
                last_error = exc
    if last_error:
        raise last_error
    raise ValueError(f"Could not read CSV: {path}")


def normalize_columns(df: pd.DataFrame, aliases: Dict[str, List[str]]) -> pd.DataFrame:
    lower_to_original = {str(c).lower().strip(): c for c in df.columns}
    rename = {}
    for canonical, options in aliases.items():
        for option in options:
            if option.lower() in lower_to_original:
                rename[lower_to_original[option.lower()]] = canonical
                break
    return df.rename(columns=rename)


MAX_GSC_ROWS = 20_000


def normalize_gsc(path: Path) -> pd.DataFrame:
    df = normalize_columns(read_csv_safely(path), GSC_ALIASES)
    if "query" not in df.columns:
        raise ValueError("GSC export needs a query/search term column.")
    df = df.dropna(subset=["query"])
    has_position = "position" in df.columns
    if "page" not in df.columns:
        df["page"] = ""
    for col in ["clicks", "impressions", "position"]:
        if col not in df.columns:
            df[col] = 0
        df[col] = df[col].apply(parse_number)
    df["query"] = df["query"].astype(str).apply(normalize_text)
    df["page"] = df["page"].astype(str).str.strip()
    result = df[df["query"].str.len() > 1].copy()
    truncated_rows = 0
    if len(result) > MAX_GSC_ROWS:
        # Very large exports (e.g. bulk API pulls) can make skeleton discovery slow on
        # a free hosting tier. Keep the rows with the strongest ranking signal.
        truncated_rows = len(result) - MAX_GSC_ROWS
        result = result.sort_values("impressions", ascending=False).head(MAX_GSC_ROWS)
    result.attrs["has_position"] = has_position
    result.attrs["truncated_rows"] = truncated_rows
    return result


def normalize_volume(path: Optional[Path]) -> pd.DataFrame:
    if not path:
        return pd.DataFrame(columns=["query", "search_volume", "competition", "cpc"])
    df = normalize_columns(read_csv_safely(path), VOLUME_ALIASES)
    if "query" not in df.columns:
        raise ValueError("Volume export needs a keyword/query column.")
    if "search_volume" not in df.columns:
        raise ValueError("Volume export needs a search-volume column.")
    df = df.dropna(subset=["query"])
    if "competition" not in df.columns:
        df["competition"] = ""
    if "cpc" not in df.columns:
        df["cpc"] = ""
    df["query"] = df["query"].astype(str).apply(normalize_text)
    df["search_volume"] = df["search_volume"].apply(parse_number)
    return df[["query", "search_volume", "competition", "cpc"]].drop_duplicates("query")

# ---------------------------------------------------------------------------
# Pattern discovery
# ---------------------------------------------------------------------------

def tokenize(query: str) -> List[str]:
    query = normalize_text(query)
    query = re.sub(r"[^a-zA-ZäöüÄÖÜ0-9\s-]", " ", query)
    return [t for t in query.split() if t]


def collapse_slots(parts: Sequence[str]) -> str:
    collapsed: List[str] = []
    prev_slot = False
    for part in parts:
        if part == "{}":
            if not prev_slot:
                collapsed.append(part)
            prev_slot = True
        else:
            collapsed.append(part)
            prev_slot = False
    out: List[str] = []
    slot_n = 1
    for part in collapsed:
        if part == "{}":
            out.append(f"{{slot_{slot_n}}}")
            slot_n += 1
        else:
            out.append(part)
    return " ".join(out)


def extract_slots(tokens: List[str], skeleton: str) -> Tuple[str, ...]:
    skeleton_parts = skeleton.split()
    slots: List[str] = []
    token_i = 0
    current: List[str] = []
    for part in skeleton_parts:
        if part.startswith("{slot_"):
            if token_i < len(tokens):
                current.append(tokens[token_i])
                token_i += 1
        else:
            while token_i < len(tokens) and tokens[token_i] != part:
                current.append(tokens[token_i])
                token_i += 1
            if current:
                slots.append(" ".join(current))
                current = []
            if token_i < len(tokens) and tokens[token_i] == part:
                token_i += 1
    if current:
        slots.append(" ".join(current))
    return tuple(s for s in slots if s)


def skeleton_from_frequent_tokens(tokens: List[str], token_freq: Counter, min_anchor_freq: int) -> Optional[Tuple[str, Tuple[str, ...]]]:
    parts: List[str] = []
    for token in tokens:
        keep = (token_freq[token] >= min_anchor_freq and token not in WEAK_ANCHORS and len(token) > 2) or token in CONNECTORS
        parts.append(token if keep else "{}")
    skeleton = collapse_slots(parts)
    if "{slot_" not in skeleton:
        return None
    anchors = [p for p in skeleton.split() if not p.startswith("{slot_")]
    meaningful = [a for a in anchors if a not in CONNECTORS and a not in WEAK_ANCHORS]
    if not meaningful and len(anchors) < 2:
        return None
    return skeleton, extract_slots(tokens, skeleton)


def generate_query_skeletons(query: str, token_freq: Counter, min_anchor_freq: int) -> List[Tuple[str, Tuple[str, ...]]]:
    """Infer candidate templates from repeated language structures.

    This intentionally avoids a hardcoded SEO pattern list. It uses repeated anchors,
    prefixes, suffixes and connector positions that occur inside the uploaded GSC data.
    """
    tokens = tokenize(query)
    if len(tokens) < 2:
        return []
    results: List[Tuple[str, Tuple[str, ...]]] = []

    freq = skeleton_from_frequent_tokens(tokens, token_freq, min_anchor_freq)
    if freq:
        results.append(freq)

    # Repeated suffix anchor, e.g. "{slot_1} entkalken" or "{slot_1} rezept".
    last = tokens[-1]
    if token_freq[last] >= min_anchor_freq and last not in WEAK_ANCHORS and len(last) > 2:
        results.append((f"{{slot_1}} {last}", (" ".join(tokens[:-1]),)))

    # Repeated prefix anchor, e.g. "alternative {slot_1}".
    first = tokens[0]
    if token_freq[first] >= min_anchor_freq and first not in WEAK_ANCHORS and len(first) > 2:
        results.append((f"{first} {{slot_1}}", (" ".join(tokens[1:]),)))

    # Connector-based patterns, e.g. "{slot_1} für {slot_2}".
    for i, tok in enumerate(tokens):
        if tok not in CONNECTORS or i == 0 or i == len(tokens) - 1:
            continue
        left = tokens[:i]
        right = tokens[i + 1:]
        if not left or not right:
            continue
        if len(left) >= 2 and token_freq[left[0]] >= min_anchor_freq and left[0] not in WEAK_ANCHORS:
            skeleton = f"{left[0]} {{slot_1}} {tok} {{slot_2}}"
            slots = (" ".join(left[1:]), " ".join(right))
        else:
            skeleton = f"{{slot_1}} {tok} {{slot_2}}"
            slots = (" ".join(left), " ".join(right))
        results.append((skeleton, slots))

    # Middle anchor, e.g. "{slot_1} vs {slot_2}" or "{slot_1} ohne {slot_2}".
    for i, tok in enumerate(tokens[1:-1], start=1):
        if token_freq[tok] >= min_anchor_freq and tok not in WEAK_ANCHORS and len(tok) > 2:
            results.append((f"{{slot_1}} {tok} {{slot_2}}", (" ".join(tokens[:i]), " ".join(tokens[i + 1:]))))

    seen = set()
    deduped: List[Tuple[str, Tuple[str, ...]]] = []
    for skeleton, slots in results:
        skeleton = normalize_text(skeleton)
        if skeleton in seen or "{slot_" not in skeleton:
            continue
        seen.add(skeleton)
        deduped.append((skeleton, slots))
    return deduped


def filter_ranking_proof(
    df: pd.DataFrame, top_position: float, min_gsc_impressions: float, expanded_position: float, has_position: bool = True
) -> pd.DataFrame:
    if not has_position:
        # No real position column: a missing position defaults to 0, which would
        # otherwise make every row look like it ranks #1. Fall back to clicks/impressions only.
        return df[(df["clicks"] > 0) | (df["impressions"] >= min_gsc_impressions)].copy()
    # Blank position cells parse to 0.0, but GSC positions start at 1 — treat 0 as missing
    # so those rows can't fake a #1 ranking.
    has_pos = df["position"] > 0
    return df[
        (has_pos & (df["position"] <= top_position))
        | (df["clicks"] > 0)
        | ((df["impressions"] >= min_gsc_impressions) & has_pos & (df["position"] <= expanded_position))
    ].copy()


def score_confidence(query_count: int, slot_diversity: int, top10_count: int, clicks: float, impressions: float, avg_position: float, skeleton: str) -> float:
    count_score = min(1.0, math.log1p(query_count) / math.log1p(12))
    diversity_score = min(1.0, slot_diversity / max(3, query_count))
    top10_score = min(1.0, top10_count / max(1, query_count))
    traffic_score = min(1.0, math.log1p(clicks + impressions / 50) / math.log1p(500))
    position_score = max(0.0, min(1.0, (20 - avg_position) / 20)) if avg_position else 0.0
    anchor_count = len([p for p in skeleton.split() if not p.startswith("{slot_")])
    too_broad_penalty = 0.75 if anchor_count < 1 else 1.0
    too_complex_penalty = 0.85 if skeleton.count("{slot_") > 2 else 1.0
    return round((
        count_score * 0.25 + diversity_score * 0.25 + top10_score * 0.18 +
        traffic_score * 0.17 + position_score * 0.15
    ) * too_broad_penalty * too_complex_penalty, 3)


def human_label(skeleton: str) -> str:
    label = skeleton.replace("{slot_1}", "X").replace("{slot_2}", "Y").replace("{slot_3}", "Z")
    return label[:1].upper() + label[1:]


def infer_hub_label(skeleton: str, member_queries: Optional[Sequence[str]] = None) -> str:
    anchors = [p for p in skeleton.split() if not p.startswith("{slot_")]
    meaningful = [a for a in anchors if a not in CONNECTORS and a not in WEAK_ANCHORS]
    if meaningful:
        return f"{meaningful[-1].title()} Hub"
    # Connector-only skeletons ("{slot_1} für {slot_2}") say nothing about the topic;
    # name the hub after the most common meaningful word in its member queries instead
    # of producing labels like "Für Pattern Hub".
    if member_queries:
        freq = Counter(
            t for q in member_queries for t in tokenize(q)
            if t not in CONNECTORS and t not in WEAK_ANCHORS and len(t) > 2
        )
        if freq:
            return f"{freq.most_common(1)[0][0].title()} Hub"
    if anchors:
        return f"{anchors[-1].title()} Pattern Hub"
    return "Programmatic SEO Hub"


def hub_public_topic(hub_label: str) -> str:
    """Strip internal jargon ("... Hub") from a hub label for public-facing titles."""
    topic = re.sub(r"\b(pattern\s+)?hub\b", "", str(hub_label), flags=re.IGNORECASE).strip(" -:")
    return topic or "Programmatic SEO"


def pattern_url_template(skeleton: str) -> str:
    value = strip_accents(normalize_text(skeleton))
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9{}_-]", "", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return f"/{value}/"


# ---------------------------------------------------------------------------
# Article templates: intent classification + static German template profiles
#
# Every hub gets a full, intent-specific article template. The static profiles
# below always work offline; when a GEMINI_API_KEY is configured, an AI layer
# (see generate_ai_article_templates) refines them per hub. {keyword} in the
# profiles is replaced by the query skeleton, so real {slot_n} placeholders
# survive into the final template (mail-merge ready). Template strings are
# NEVER passed through normalize_text — it would lowercase German nouns.
# ---------------------------------------------------------------------------

HUB_INTENTS = ("how_to", "recipe", "commercial_comparison", "alternatives", "informational")

INTENT_ANCHOR_RULES: List[Tuple[str, frozenset]] = [  # checked in order, first hit wins
    ("recipe", frozenset({"rezept", "rezepte", "zubereitung", "zubereiten"})),
    ("how_to", frozenset({
        "entkalken", "entkalkung", "reinigen", "reinigung", "anleitung", "einstellen",
        "reparieren", "entlueften", "entlüften", "resetten", "zuruecksetzen", "zurücksetzen",
        "kalibrieren", "wechseln", "einbauen", "anschliessen", "anschließen", "mahlgrad",
    })),
    ("alternatives", frozenset({"alternative", "alternativen", "statt", "ersatz"})),
    ("commercial_comparison", frozenset({
        "beste", "bester", "bestes", "best", "vergleich", "test", "vs", "versus",
        "kaufen", "empfehlung", "testsieger",
    })),
]


def classify_hub_intent(skeleton: str, sample_queries: str = "") -> str:
    anchors = [p for p in skeleton.split() if not p.startswith("{slot_")]
    for intent, vocab in INTENT_ANCHOR_RULES:
        if any(t in vocab for t in anchors):
            return intent
    # Weaker signal: the skeleton anchors are inconclusive (e.g. connector-only
    # "{slot_1} für {slot_2}"), but the member queries may carry intent words.
    sample_tokens = set(tokenize(sample_queries)) if sample_queries else set()
    for intent, vocab in INTENT_ANCHOR_RULES:
        if sample_tokens & vocab:
            return intent
    # German verb infinitives ("entlüften", "programmieren") signal a how-to.
    for t in anchors:
        if t not in CONNECTORS and t not in WEAK_ANCHORS and len(t) > 4 and (t.endswith("en") or t.endswith("ieren")):
            return "how_to"
    if skeleton.count("{slot_") >= 2 and any(t in {"für", "fuer", "zu", "zum", "zur"} for t in anchors):
        return "commercial_comparison"
    return "informational"


TEMPLATE_PROFILES: Dict[str, dict] = {
    "how_to": {
        "h1": "{keyword}: Schritt-für-Schritt-Anleitung",
        "meta_title": "{keyword} – so geht's richtig | Anleitung",
        "meta_description": "{keyword} leicht gemacht: Schritt-für-Schritt-Anleitung, passende Mittel, Intervalle und typische Fehler im Überblick.",
        "intro": "So funktioniert {keyword}: Diese Anleitung führt dich Schritt für Schritt durch den kompletten Ablauf, zeigt dir die passenden Mittel und die häufigsten Fehler, die du dabei vermeiden solltest.",
        "outline": [
            {"h2": "Kurzantwort: {keyword} in 30 Sekunden", "h3": []},
            {"h2": "Warum das wichtig ist", "h3": []},
            {"h2": "Was du brauchst", "h3": ["Empfohlene Mittel", "Hausmittel-Alternativen"]},
            {"h2": "Schritt-für-Schritt-Anleitung", "h3": ["Vorbereitung", "Durchführung", "Nachbereitung"]},
            {"h2": "Wie oft ist es nötig?", "h3": []},
            {"h2": "Häufige Fehler und Probleme", "h3": []},
            {"h2": "FAQ", "h3": []},
        ],
        "faq": [
            "Wie oft sollte man {keyword} durchführen?",
            "Welche Mittel eignen sich für {keyword}?",
            "Was passiert, wenn man {keyword} vernachlässigt?",
            "Geht {keyword} auch mit Hausmitteln?",
        ],
        "internal_linking": "Der Artikel verlinkt zurück zum Hub, per Exact-Match-Anker auf 2–4 Geschwister-Artikel mit gleichem {slot_1} und auf passende Produkt- oder Ratgeberseiten.",
        "schema_org": ["HowTo", "FAQPage", "BreadcrumbList"],
        "word_count": (700, 1100),
        "eeat": [
            "Autor mit Praxiserfahrung nennen",
            "Eigene Fotos der einzelnen Schritte",
            "Zuletzt-aktualisiert-Datum anzeigen",
            "Sicherheits- und Garantiehinweis ergänzen",
        ],
    },
    "recipe": {
        "h1": "{keyword}: Zutaten, Mengen & Zubereitung",
        "meta_title": "{keyword} – Original-Rezept mit Mengenangaben",
        "meta_description": "{keyword} einfach selbst machen: alle Zutaten, genaue Mengenangaben und die Zubereitung Schritt für Schritt erklärt.",
        "intro": "Mit diesem Rezept gelingt {keyword} auf Anhieb: Hier findest du alle Zutaten mit genauen Mengen, das passende Equipment und die Zubereitung Schritt für Schritt – inklusive Varianten und typischer Fehler.",
        "outline": [
            {"h2": "Zutaten für {keyword}", "h3": []},
            {"h2": "Das richtige Equipment", "h3": []},
            {"h2": "Zubereitung Schritt für Schritt", "h3": []},
            {"h2": "Varianten und Abwandlungen", "h3": []},
            {"h2": "Tipps und typische Fehler", "h3": []},
            {"h2": "Nährwerte", "h3": []},
            {"h2": "FAQ", "h3": []},
        ],
        "faq": [
            "Welche Zutaten braucht man für {keyword}?",
            "Wie lange dauert die Zubereitung von {keyword}?",
            "Wie viele Kalorien hat {keyword}?",
            "Welche Varianten von {keyword} gibt es?",
        ],
        "internal_linking": "Der Artikel verlinkt zurück zum Rezept-Hub, auf 2–4 verwandte Rezepte (Exact-Match-Anker) und auf passende Equipment- oder Zutaten-Guides.",
        "schema_org": ["Recipe", "FAQPage", "BreadcrumbList"],
        "word_count": (500, 900),
        "eeat": [
            "Autor mit Koch-/Barista-Erfahrung nennen",
            "Eigene Fotos der Zubereitungsschritte",
            "Getestete Mengenangaben und Ergiebigkeit angeben",
        ],
    },
    "commercial_comparison": {
        "h1": "{keyword}: Test, Vergleich & Empfehlungen",
        "meta_title": "{keyword} im Test & Vergleich | Kaufberatung",
        "meta_description": "{keyword} im Vergleich: Top-Empfehlungen, Vergleichstabelle, Kaufkriterien und Preise – kompakt und unabhängig bewertet.",
        "intro": "Du suchst {keyword}? Hier findest du unsere Top-Empfehlung direkt am Anfang, eine übersichtliche Vergleichstabelle und alle Kaufkriterien, damit du die richtige Wahl triffst.",
        "outline": [
            {"h2": "Unsere Top-Empfehlung", "h3": []},
            {"h2": "Vergleichstabelle", "h3": []},
            {"h2": "Die besten Optionen im Detail", "h3": ["Testsieger", "Preis-Leistungs-Sieger", "Premium-Wahl"]},
            {"h2": "Kaufkriterien: Darauf kommt es an", "h3": []},
            {"h2": "Für wen eignet sich was?", "h3": []},
            {"h2": "FAQ", "h3": []},
        ],
        "faq": [
            "Was ist die beste Wahl bei {keyword}?",
            "Worauf sollte man bei {keyword} achten?",
            "Was kostet eine gute Option bei {keyword}?",
            "Welche Alternative lohnt sich bei kleinem Budget?",
        ],
        "internal_linking": "Der Artikel verlinkt zurück zum Hub, auf die Einzeltests der vorgestellten Produkte und auf verwandte Vergleiche mit gleichem {slot_1} (Exact-Match-Anker).",
        "schema_org": ["ItemList", "Product", "FAQPage", "BreadcrumbList"],
        "word_count": (1500, 2500),
        "eeat": [
            "Testmethodik transparent beschreiben",
            "Hands-on-Fotos/eigene Testeindrücke zeigen",
            "Zuletzt-geprüft-Datum anzeigen",
            "Affiliate-Disclosure klar platzieren",
        ],
    },
    "alternatives": {
        "h1": "{keyword}: Die besten Alternativen im Überblick",
        "meta_title": "{keyword} – Top-Alternativen im Vergleich",
        "meta_description": "{keyword}: Diese Alternativen überzeugen im Vergleich – mit Kompatibilität, Preisen und klaren Vor- und Nachteilen.",
        "intro": "Auf der Suche nach {keyword}? Hier findest du die beste Alternative direkt am Anfang, einen kompakten Vergleich aller Optionen und klare Hinweise zu Kompatibilität und Preis.",
        "outline": [
            {"h2": "Kurzantwort: Die beste Alternative", "h3": []},
            {"h2": "Alternativen im Vergleich", "h3": []},
            {"h2": "Die Alternativen im Detail", "h3": []},
            {"h2": "Kompatibilität und Eignung", "h3": []},
            {"h2": "Preisvergleich", "h3": []},
            {"h2": "Vor- und Nachteile", "h3": []},
            {"h2": "FAQ", "h3": []},
        ],
        "faq": [
            "Was ist die beste Option bei {keyword}?",
            "Sind Alternativen genauso gut wie das Original?",
            "Worauf muss man bei der Kompatibilität achten?",
            "Wie viel lässt sich mit einer Alternative sparen?",
        ],
        "internal_linking": "Der Artikel verlinkt zurück zum Hub, auf verwandte Alternativen-Artikel mit gleichem {slot_1} und auf die Pflege-/How-to-Artikel der betroffenen Produkte.",
        "schema_org": ["ItemList", "FAQPage", "BreadcrumbList"],
        "word_count": (900, 1400),
        "eeat": [
            "Eigene Erfahrung mit Original und Alternative nennen",
            "Kompatibilität konkret pro Modell angeben",
            "Zuletzt-geprüft-Datum anzeigen",
        ],
    },
    "informational": {
        "h1": "{keyword}: Alles Wichtige im Überblick",
        "meta_title": "{keyword} – verständlich erklärt",
        "meta_description": "{keyword} verständlich erklärt: Kurzantwort, konkrete Empfehlungen, Varianten und häufige Fehler im Überblick.",
        "intro": "Hier bekommst du die Kurzantwort zu {keyword} direkt am Anfang – gefolgt von konkreten Empfehlungen, den wichtigsten Varianten und den häufigsten Fehlern.",
        "outline": [
            {"h2": "Kurzantwort", "h3": []},
            {"h2": "Problem oder Use Case", "h3": []},
            {"h2": "Konkrete Anleitung oder Empfehlung", "h3": []},
            {"h2": "Varianten und Entscheidungskriterien", "h3": []},
            {"h2": "Häufige Fehler", "h3": []},
            {"h2": "FAQ", "h3": []},
        ],
        "faq": [
            "Was bedeutet {keyword} genau?",
            "Worauf sollte man bei {keyword} achten?",
            "Welche häufigen Fehler gibt es bei {keyword}?",
        ],
        "internal_linking": "Der Artikel verlinkt zurück zum Hub, auf 2–4 thematisch verwandte Artikel und auf relevante Produkt- oder Conversion-Seiten.",
        "schema_org": ["Article", "FAQPage", "BreadcrumbList"],
        "word_count": (800, 1200),
        "eeat": [
            "Autor mit Themenexpertise nennen",
            "Quellen/eigene Daten verlinken",
            "Zuletzt-aktualisiert-Datum anzeigen",
        ],
    },
}


def static_article_template(skeleton: str, hub_label: str, intent: str) -> dict:
    """Fill the intent profile with the concrete skeleton. {slot_n} placeholders stay literal."""
    profile = TEMPLATE_PROFILES.get(intent, TEMPLATE_PROFILES["informational"])
    keyword = skeleton.strip()

    def fill(text: str) -> str:
        filled = text.replace("{keyword}", keyword)
        return filled[:1].upper() + filled[1:] if filled else filled

    return {
        "intent": intent,
        "h1_template": fill(profile["h1"]),
        "meta_title_template": fill(profile["meta_title"]),
        "meta_description_template": fill(profile["meta_description"]),
        "intro_template": fill(profile["intro"]),
        "outline": [
            {"h2": fill(item["h2"]), "h3": [fill(h3) for h3 in item["h3"]]}
            for item in profile["outline"]
        ],
        "faq": [fill(q) for q in profile["faq"]],
        "internal_linking": profile["internal_linking"],
        "schema_org": list(profile["schema_org"]),
        "word_count": tuple(profile["word_count"]),
        "eeat_checklist": list(profile["eeat"]),
    }


def article_structure_for(skeleton: str, intent: str = "informational") -> str:
    profile = TEMPLATE_PROFILES.get(intent, TEMPLATE_PROFILES["informational"])
    return " → ".join(item["h2"].replace("{keyword}", skeleton.strip()) for item in profile["outline"])


def discover_patterns(
    gsc: pd.DataFrame,
    top_position: float = 10,
    min_gsc_impressions: float = 20,
    expanded_position: float = 20,
    min_pattern_queries: int = 3,
    min_distinct_slot_values: int = 3,
    min_template_confidence: float = 0.45,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    has_position = gsc.attrs.get("has_position", True)
    proof = filter_ranking_proof(gsc, top_position, min_gsc_impressions, expanded_position, has_position)
    token_freq = Counter(t for q in proof["query"].tolist() for t in tokenize(q))
    min_anchor_freq = max(2, min(5, int(max(1, len(proof)) * 0.03)))

    grouped: Dict[str, List[dict]] = defaultdict(list)
    for _, row in proof.iterrows():
        for skeleton, slots in generate_query_skeletons(row["query"], token_freq, min_anchor_freq):
            grouped[skeleton].append({
                "query": row["query"],
                "page": row.get("page", ""),
                "clicks": float(row.get("clicks", 0)),
                "impressions": float(row.get("impressions", 0)),
                "position": float(row.get("position", 0)),
                "slots": slots,
            })

    pattern_rows: List[dict] = []
    membership_rows: List[dict] = []
    for idx, (skeleton, items) in enumerate(sorted(grouped.items()), start=1):
        # dedupe same query per skeleton
        deduped = {item["query"]: item for item in items}
        items = list(deduped.values())
        slot_values = {normalize_text(slot) for item in items for slot in item["slots"] if normalize_text(slot)}
        query_count = len(items)
        slot_diversity = len(slot_values)
        distinct_url_count = len({item["page"] for item in items if item["page"]})
        # Position 0 = blank cell in the export, not a real ranking; exclude from position stats.
        positions = [item["position"] for item in items if item["position"] > 0]
        top10_count = sum(1 for p in positions if p <= top_position) if has_position else 0
        total_clicks = sum(item["clicks"] for item in items)
        total_impressions = sum(item["impressions"] for item in items)
        avg_position = sum(positions) / len(positions) if positions else 0.0
        confidence = score_confidence(query_count, slot_diversity, top10_count, total_clicks, total_impressions, avg_position, skeleton)

        reject_reason = ""
        if query_count < min_pattern_queries:
            reject_reason = f"Too few matching GSC queries ({query_count} < {min_pattern_queries})."
        elif slot_diversity < min_distinct_slot_values:
            reject_reason = f"Too little slot diversity ({slot_diversity} < {min_distinct_slot_values})."
        elif confidence < min_template_confidence:
            reject_reason = f"Template confidence below threshold ({confidence} < {min_template_confidence})."

        pattern_id = f"pattern_{idx:03d}"
        template_label = human_label(skeleton)
        sample_queries = "; ".join(item["query"] for item in items[:8])
        hub_label = infer_hub_label(skeleton, [item["query"] for item in items])
        intent = classify_hub_intent(skeleton, sample_queries)
        static_template = static_article_template(skeleton, hub_label, intent)
        candidate = PatternCandidate(
            pattern_id=pattern_id,
            query_skeleton=skeleton,
            query_count=query_count,
            slot_diversity=slot_diversity,
            distinct_url_count=distinct_url_count,
            top10_count=top10_count,
            total_clicks=round(total_clicks, 2),
            total_impressions=round(total_impressions, 2),
            avg_position=round(avg_position, 2),
            confidence=confidence,
            is_programmatic_opportunity=(reject_reason == ""),
            reject_reason=reject_reason,
            sample_queries=sample_queries,
            sample_urls="; ".join(sorted({item["page"] for item in items if item["page"]})[:5]),
            template_label=template_label,
            intent=intent,
            hub_label=hub_label,
            hub_article_title=f"{hub_public_topic(hub_label)}: Übersicht, Varianten & Empfehlungen",
            hub_slug=f"/{slugify(hub_public_topic(hub_label))}/",
            url_template=pattern_url_template(skeleton),
            article_title_template=static_template["h1_template"],
            recommended_article_structure=article_structure_for(skeleton, intent),
            internal_linking_strategy=static_template["internal_linking"],
            risks="Human/AI review required: thin content, cannibalization, duplicated intent, missing unique slot information.",
        )
        pattern_rows.append(asdict(candidate))
        for item in items:
            membership_rows.append({
                "pattern_id": pattern_id,
                "query_skeleton": skeleton,
                "query": item["query"],
                "current_url": item["page"],
                "clicks": item["clicks"],
                "impressions": item["impressions"],
                "position": item["position"],
                "slot_values": " | ".join(item["slots"]),
                "pattern_accepted_before_volume": reject_reason == "",
                "pattern_reject_reason": reject_reason,
            })

    patterns = pd.DataFrame(pattern_rows)
    memberships = pd.DataFrame(membership_rows)
    if not patterns.empty:
        patterns = patterns.sort_values(["is_programmatic_opportunity", "confidence", "total_impressions"], ascending=[False, False, False])
    patterns.attrs["position_data_missing"] = not has_position
    patterns.attrs["truncated_rows"] = gsc.attrs.get("truncated_rows", 0)
    return patterns, memberships

def build_existing_coverage(patterns: pd.DataFrame, memberships: pd.DataFrame) -> pd.DataFrame:
    """Group already-ranking queries by the URL that already ranks for them.

    Query variants like "delonghi entkalken" and "delonghi entkalkung" often already
    rank the same existing page. Without this, they'd look like separate opportunities
    even though the site already covers that need with one article.
    """
    columns = [
        "pattern_id", "hub_label", "current_url", "query_variants", "variant_count",
        "total_clicks", "total_impressions",
    ]
    if memberships.empty or patterns.empty:
        return pd.DataFrame(columns=columns)
    accepted_ids = set(patterns.loc[patterns["is_programmatic_opportunity"], "pattern_id"])
    hub_labels = patterns.set_index("pattern_id")["hub_label"].to_dict()
    scoped = memberships[memberships["pattern_id"].isin(accepted_ids) & (memberships["current_url"].str.len() > 0)]
    if scoped.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for (pattern_id, url), group in scoped.groupby(["pattern_id", "current_url"]):
        rows.append({
            "pattern_id": pattern_id,
            "hub_label": hub_labels.get(pattern_id, ""),
            "current_url": url,
            "query_variants": "; ".join(sorted(group["query"].unique())),
            "variant_count": int(group["query"].nunique()),
            "total_clicks": round(float(group["clicks"].sum()), 2),
            "total_impressions": round(float(group["impressions"].sum()), 2),
        })
    return pd.DataFrame(rows, columns=columns).sort_values(["pattern_id", "total_clicks"], ascending=[True, False])

# ---------------------------------------------------------------------------
# Volume validation + outputs
# ---------------------------------------------------------------------------

def build_volume_queue(memberships: pd.DataFrame) -> pd.DataFrame:
    if memberships.empty:
        return pd.DataFrame(columns=["keyword", "pattern_id", "query_skeleton", "gsc_impressions", "gsc_clicks", "gsc_position", "reason"])
    queue = memberships[memberships["pattern_accepted_before_volume"]].copy()
    if queue.empty:
        queue = memberships.copy()
    queue = queue.rename(columns={"query": "keyword", "impressions": "gsc_impressions", "clicks": "gsc_clicks", "position": "gsc_position"})
    queue["reason"] = "Check search volume before recommending rollout. GSC shows ranking proof; volume validates demand."
    return queue[["keyword", "pattern_id", "query_skeleton", "gsc_impressions", "gsc_clicks", "gsc_position", "current_url", "reason"]].drop_duplicates("keyword")


def merge_volume(memberships: pd.DataFrame, volume: pd.DataFrame, min_volume: float) -> pd.DataFrame:
    if memberships.empty:
        return pd.DataFrame()
    merged = memberships.merge(volume, how="left", on="query")
    merged["search_volume"] = merged["search_volume"].fillna(0)
    merged["volume_status"] = merged["search_volume"].apply(lambda x: "volume_confirmed" if x >= min_volume else "rejected_low_or_missing_volume")
    merged["final_status"] = merged.apply(
        lambda r: "confirmed_opportunity" if r["pattern_accepted_before_volume"] and r["search_volume"] >= min_volume else "not_recommended_yet",
        axis=1,
    )
    return merged.sort_values(["final_status", "search_volume", "impressions"], ascending=[True, False, False])


def build_hub_plan(
    opportunities: pd.DataFrame, patterns: pd.DataFrame, new_keywords_checked: Optional[pd.DataFrame] = None
) -> pd.DataFrame:
    columns = [
        "pattern_id", "hub_label", "hub_article_title", "hub_slug", "query_skeleton", "intent",
        "url_template", "validated_keywords", "total_search_volume", "article_count",
        "ai_suggested_keywords", "ai_suggested_count", "article_title_template",
        "recommended_article_structure", "internal_linking_strategy", "duplicate_of", "risks",
    ]
    if opportunities.empty or patterns.empty:
        return pd.DataFrame(columns=columns)
    confirmed = opportunities[opportunities["final_status"] == "confirmed_opportunity"].copy()
    if confirmed.empty:
        return pd.DataFrame(columns=columns)

    ai_confirmed_by_pattern: Dict[str, List[str]] = {}
    if new_keywords_checked is not None and not new_keywords_checked.empty:
        confirmed_ai = new_keywords_checked[new_keywords_checked["trends_status"] == "confirmed"]
        for pattern_id, sub in confirmed_ai.groupby("pattern_id"):
            ai_confirmed_by_pattern[pattern_id] = sorted(sub["candidate_query"].unique().tolist())

    pattern_lookup = patterns.set_index("pattern_id").to_dict("index")
    rows = []
    keyword_sets: Dict[str, set] = {}
    for pattern_id, sub in confirmed.groupby("pattern_id"):
        p = pattern_lookup.get(pattern_id, {})
        ai_keywords = ai_confirmed_by_pattern.get(pattern_id, [])
        keyword_sets[pattern_id] = set(sub["query"].unique())
        rows.append({
            "pattern_id": pattern_id,
            "hub_label": p.get("hub_label", "Programmatic Hub"),
            "hub_article_title": p.get("hub_article_title", "Programmatic Hub"),
            "hub_slug": p.get("hub_slug", f"/{pattern_id}/"),
            "query_skeleton": p.get("query_skeleton", ""),
            "intent": p.get("intent", "informational"),
            "url_template": p.get("url_template", ""),
            "validated_keywords": "; ".join(sub.sort_values("search_volume", ascending=False)["query"].head(20).tolist()),
            "total_search_volume": int(sub["search_volume"].sum()),
            "article_count": int(sub["query"].nunique()),
            "ai_suggested_keywords": "; ".join(ai_keywords),
            "ai_suggested_count": len(ai_keywords),
            "article_title_template": p.get("article_title_template", ""),
            "recommended_article_structure": p.get("recommended_article_structure", ""),
            "internal_linking_strategy": p.get("internal_linking_strategy", ""),
            "duplicate_of": "",
            "risks": "",
            "_confidence": float(p.get("confidence", 0.0)),
        })

    # Cannibalization guard: different skeletons can claim (nearly) the same keyword set —
    # the plan would then recommend building the same articles several times. Mark every
    # such hub as a duplicate of the strongest one instead of silently listing all of them.
    ranked = sorted(rows, key=lambda r: (r["_confidence"], r["total_search_volume"]), reverse=True)
    for i, row in enumerate(ranked):
        if row["duplicate_of"]:
            continue
        for other in ranked[i + 1:]:
            if other["duplicate_of"]:
                continue
            a, b = keyword_sets[row["pattern_id"]], keyword_sets[other["pattern_id"]]
            union = a | b
            overlap = len(a & b) / len(union) if union else 0.0
            if overlap >= 0.8:
                other["duplicate_of"] = row["pattern_id"]
                other["risks"] = (
                    f"Überschneidet sich zu {round(overlap * 100)}% mit '{row['hub_label']}' "
                    f"({row['pattern_id']}) — nur einen kanonischen Hub umsetzen, sonst Kannibalisierung. "
                )

    for row in rows:
        if row["article_count"] >= 10:
            row["risks"] += (
                "Hohe Artikelzahl: Thin-Content-Risiko — nur Artikel mit eigenem Informationswert "
                "pro Slot-Wert veröffentlichen. "
            )
        row["risks"] += "Menschliche Prüfung vor Rollout empfohlen (Intent-Duplikate, fehlende Slot-Infos)."
        row["risks"] = row["risks"].strip()
        row.pop("_confidence", None)

    return pd.DataFrame(rows, columns=columns).sort_values(
        ["duplicate_of", "total_search_volume", "article_count"], ascending=[True, False, False]
    )

# ---------------------------------------------------------------------------
# New-keyword candidates: AI brainstorm + free Trends relevance check
#
# GSC only proves demand for queries that already rank. To grow a validated hub
# beyond what's already in GSC (e.g. a coffee-machine model that was never
# searched on this site yet), an LLM has to suggest plausible new keywords —
# no heuristic can invent real brand/model/city names. If a free Gemini API
# key is configured (GEMINI_API_KEY), this happens automatically in the
# background on every run. Without a key, the tool falls back to writing a
# prompt file you can paste into any free LLM chat and re-import as a CSV.
# Either way, every candidate is checked against Google Trends before it's
# added to a hub — nothing gets added on invented demand.
# ---------------------------------------------------------------------------

NEW_KEYWORD_ALIASES: Dict[str, List[str]] = {
    "pattern_id": ["pattern_id", "pattern", "hub_id"],
    "candidate_query": ["candidate_query", "keyword", "query", "candidate"],
}

MAX_TRENDS_CANDIDATES = 25
TRENDS_TIME_BUDGET_S = 120.0
# gemini-2.0-flash was shut down on 2026-06-01; 2.5-family models retire Oct 2026
# and only serve as transient fallbacks. Keep all model ids in this one place.
DEFAULT_AI_MODEL = "gemini-3.5-flash"
AI_MODEL_FALLBACKS: Tuple[str, ...] = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
GEMINI_TIMEOUT_S = 45
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _call_gemini(
    prompt: str,
    api_key: str,
    model: str = DEFAULT_AI_MODEL,
    timeout: int = GEMINI_TIMEOUT_S,
    temperature: float = 0.4,
    max_output_tokens: int = 8192,
    response_schema: Optional[dict] = None,
    fallback_models: Sequence[str] = AI_MODEL_FALLBACKS,
) -> Tuple[Optional[str], str]:
    """Call the Gemini generateContent endpoint with automatic model fallback.

    Returns (response_text or None, status). status is "ok:<model>" on success, else one of
    "http_auth", "http_429", "network_error", "bad_response". Never raises — every AI step
    in this tool is additive and the pipeline must finish without it.
    """
    generation_config: Dict[str, object] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if response_schema is not None:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = response_schema
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }).encode("utf-8")

    models = [model] + [m for m in fallback_models if m != model]
    status = "network_error"
    for candidate_model in models:
        request = urllib.request.Request(
            GEMINI_API_URL_TEMPLATE.format(model=candidate_model),
            data=body,
            # Key goes in a header, not the URL, so it can't leak into logs.
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return None, "http_auth"  # bad key won't heal on another model
            if exc.code == 429:
                status = "http_429"
                time.sleep(2)
            else:
                # Includes 400: a fallback model may reject request features
                # (e.g. responseSchema) that the primary accepts — keep trying.
                status = "network_error"
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            # ValueError covers malformed JSON/encoding in a 200 response.
            status = "network_error"
            continue
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            status = "bad_response"
            continue
        return text, f"ok:{candidate_model}"
    return None, status


def generate_ai_keyword_candidates(
    patterns: pd.DataFrame,
    api_key: Optional[str] = None,
    model: str = DEFAULT_AI_MODEL,
    max_candidates_per_pattern: int = 8,
) -> pd.DataFrame:
    """Ask a free-tier LLM (Google Gemini) to suggest new keywords for each validated pattern.

    Returns an empty DataFrame — not an error — whenever no key is configured or the call
    fails for any reason (bad key, rate limit, model renamed, network issue). This step is
    additive; the rest of the pipeline must keep working without it.
    """
    columns = ["pattern_id", "candidate_query"]
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key or patterns.empty:
        return pd.DataFrame(columns=columns)
    accepted = patterns[patterns["is_programmatic_opportunity"]]
    if accepted.empty:
        return pd.DataFrame(columns=columns)

    prompt_lines = [
        "You are an SEO strategist. Each pattern below is a content hub already validated by real "
        "Google Search Console ranking data, with example queries that already rank. Suggest "
        "additional REAL, plausible search queries that fit the same structure but are not already "
        "in the example list. Only suggest things that plausibly exist and get searched; do not "
        "invent fake brands, products or places.",
        "",
        "Respond with ONLY a JSON array, no other text, in exactly this form:",
        '[{"pattern_id": "pattern_001", "candidates": ["...", "..."]}]',
        f"Suggest up to {max_candidates_per_pattern} candidates per pattern.",
        "",
    ]
    for _, row in accepted.iterrows():
        prompt_lines.append(
            f"pattern_id={row['pattern_id']} skeleton=`{row['query_skeleton']}` "
            f"examples: {row['sample_queries']}"
        )
    prompt = "\n".join(prompt_lines)

    text, _ = _call_gemini(prompt, api_key, model=model, timeout=30, temperature=0.7)
    if text is None:
        return pd.DataFrame(columns=columns)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return pd.DataFrame(columns=columns)
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return pd.DataFrame(columns=columns)

    rows = []
    for entry in parsed if isinstance(parsed, list) else []:
        if not isinstance(entry, dict):
            continue
        pattern_id = str(entry.get("pattern_id", "")).strip()
        if not pattern_id:
            continue
        candidates = entry.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates[:max_candidates_per_pattern]:
            candidate_query = normalize_text(candidate)
            if len(candidate_query) > 1:
                rows.append({"pattern_id": pattern_id, "candidate_query": candidate_query})
    return pd.DataFrame(rows, columns=columns).drop_duplicates()


# ---------------------------------------------------------------------------
# Per-hub article templates: AI layer on top of the static intent profiles
# ---------------------------------------------------------------------------

MAX_TEMPLATE_HUBS = 15

TEMPLATE_COLUMNS = [
    "intent", "template_source", "h1_template", "meta_title_template",
    "meta_description_template", "intro_template", "article_outline_json",
    "faq_questions", "schema_org_suggestion", "word_count_target", "eeat_checklist",
    "hero_image_scene",
]

TEMPLATE_STATUS_MESSAGES = {
    "gemini_ok": "Artikel-Templates von Gemini generiert.",
    "gemini_partial": "Artikel-Templates teilweise von Gemini generiert; Rest: statisches Intent-Fallback.",
    "no_api_key": "Kein GEMINI_API_KEY — statische, intent-basierte Artikel-Templates verwendet.",
    "disabled": "AI-Templates deaktiviert — statische, intent-basierte Templates verwendet.",
    "gemini_failed": "Gemini-Aufruf fehlgeschlagen — statische Fallback-Templates verwendet.",
    "parse_error": "Gemini-Antwort unlesbar — statische Fallback-Templates verwendet.",
    "no_hubs": "Keine bestätigten Hubs — keine Templates generiert.",
}

ARTICLE_TEMPLATE_RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "pattern_id": {"type": "STRING"},
            "intent": {"type": "STRING", "enum": list(HUB_INTENTS)},
            "h1_template": {"type": "STRING"},
            "meta_title_template": {"type": "STRING"},
            "meta_description_template": {"type": "STRING"},
            "intro_template": {"type": "STRING"},
            "outline": {"type": "ARRAY", "items": {
                "type": "OBJECT",
                "properties": {
                    "h2": {"type": "STRING"},
                    "h3": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["h2"],
            }},
            "faq": {"type": "ARRAY", "items": {"type": "STRING"}},
            "internal_linking": {"type": "STRING"},
            "schema_org": {"type": "ARRAY", "items": {"type": "STRING"}},
            "word_count_min": {"type": "INTEGER"},
            "word_count_max": {"type": "INTEGER"},
            "eeat_checklist": {"type": "ARRAY", "items": {"type": "STRING"}},
            "hero_image_scene": {"type": "STRING"},
        },
        "required": ["pattern_id", "h1_template", "meta_title_template",
                     "meta_description_template", "intro_template", "outline", "faq"],
    },
}


def _clean_template_string(value, max_len: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_len]


def _sanitize_ai_template(entry: dict, skeleton: str) -> Optional[dict]:
    """Validate one AI template entry; None means the hub keeps its static template."""
    if not isinstance(entry, dict) or not str(entry.get("pattern_id", "")).strip():
        return None
    core = {}
    for field in ("h1_template", "meta_title_template", "meta_description_template", "intro_template"):
        core[field] = _clean_template_string(entry.get(field))
        if not core[field]:
            return None
    # Mail-merge guarantee: the H1 must keep every slot placeholder of the pattern.
    for slot in re.findall(r"\{slot_\d+\}", skeleton):
        if slot not in core["h1_template"]:
            return None

    raw_outline = entry.get("outline") or []
    outline = []
    for item in raw_outline[:12]:
        if isinstance(item, str):
            h2, h3 = _clean_template_string(item), []
        elif isinstance(item, dict):
            h2 = _clean_template_string(item.get("h2"))
            h3 = [_clean_template_string(x) for x in (item.get("h3") or [])[:4] if _clean_template_string(x)]
        else:
            continue
        if h2:
            outline.append({"h2": h2, "h3": h3})
    if len(outline) < 3:
        return None

    faq = [_clean_template_string(q) for q in (entry.get("faq") or [])[:8] if _clean_template_string(q)]
    intent = entry.get("intent") if entry.get("intent") in HUB_INTENTS else None
    schema_org = [_clean_template_string(s, 60) for s in (entry.get("schema_org") or [])[:5] if _clean_template_string(s, 60)]
    eeat = [_clean_template_string(e) for e in (entry.get("eeat_checklist") or [])[:8] if _clean_template_string(e)]
    try:
        wc_min, wc_max = int(entry.get("word_count_min", 0)), int(entry.get("word_count_max", 0))
        word_count = (wc_min, wc_max) if 10 <= wc_min < wc_max <= 20000 else None
    except (TypeError, ValueError):
        word_count = None

    return {
        **core,
        "outline": outline,
        "faq": faq if len(faq) >= 3 else None,
        "internal_linking": _clean_template_string(entry.get("internal_linking")) or None,
        "intent": intent,
        "schema_org": schema_org or None,
        "word_count": word_count,
        "eeat_checklist": eeat or None,
        "hero_image_scene": _clean_template_string(entry.get("hero_image_scene"), 400) or None,
    }


def generate_ai_article_templates(
    hub_plan: pd.DataFrame,
    api_key: str,
    model: str = DEFAULT_AI_MODEL,
    timeout: int = GEMINI_TIMEOUT_S,
    max_hubs: int = MAX_TEMPLATE_HUBS,
) -> Tuple[Dict[str, dict], str]:
    """One batched Gemini call for all confirmed hubs. Never raises."""
    work = hub_plan.head(max_hubs)
    prompt_lines = [
        "Du bist ein deutschsprachiger SEO-Content-Stratege. Für jeden Content-Hub unten erstellst du "
        "EIN Artikel-Template für die Spoke-Artikel des Hubs. Die Hubs stammen aus echten "
        "Google-Search-Console-Daten und sind per Suchvolumen validiert.",
        "",
        "HARTE REGELN:",
        "1. Alle Texte auf Deutsch, korrekte Groß-/Kleinschreibung (Substantive groß).",
        "2. Platzhalter wie {slot_1} und {slot_2} EXAKT unverändert übernehmen (geschweifte Klammern, "
        "Unterstrich, Ziffer). Sie werden später automatisch durch echte Begriffe ersetzt (Mail-Merge). "
        "Die H1 MUSS alle Platzhalter des Patterns enthalten.",
        "3. meta_title_template: maximal 60 Zeichen (Platzhalter als ~15 Zeichen einrechnen). "
        "meta_description_template: maximal 155 Zeichen, mit konkretem Nutzenversprechen.",
        "4. intro_template: 40-60 Wörter, beantwortet die Suchintention im ersten Satz "
        "(Featured-Snippet-tauglich).",
        "5. outline: 5-9 H2-Überschriften mit 0-4 H3 je H2, exakt auf die Suchintention zugeschnitten: "
        "how_to -> Schritt-für-Schritt-Anleitung, benötigte Mittel, Intervalle, Fehlerbehebung; "
        "recipe -> Zutaten, Equipment, Zubereitungsschritte, Varianten, Nährwerte; "
        "commercial_comparison -> Top-Empfehlung zuerst, Vergleichstabelle, Einzelvorstellungen, "
        "Kaufkriterien; alternatives -> Alternativen-Tabelle, Kompatibilität, Preisvergleich, "
        "Vor-/Nachteile.",
        "6. faq: 4-6 realistische Suchfragen (W-Fragen) mit Platzhaltern, passend zum Pattern.",
        "7. internal_linking: 1-3 Sätze mit konkreten Ankertext-Mustern (Hub<->Spoke, "
        "Geschwister-Spokes mit gleichem {slot_1}, thematisch verwandte Hubs).",
        "8. schema_org: passende schema.org-Typen (z. B. HowTo, Recipe, ItemList, FAQPage, BreadcrumbList).",
        "9. word_count_min/word_count_max: realistischer Zielbereich in Wörtern für diese Intention.",
        "10. eeat_checklist: 3-5 konkrete E-E-A-T-Punkte (Autor, eigene Fotos/Tests, Aktualitätsdatum, "
        "Disclosure).",
        "11. Kein Wort 'Hub' und keine Anführungszeichen in öffentlichen Texten (H1, Meta, Intro).",
        "12. hero_image_scene: Beschreibe auf ENGLISCH in 1-2 Sätzen eine rein visuelle, fotorealistische "
        "Szene, die das Hub-Thema repräsentiert — konkrete Objekte, Umgebung und Licht. KEINE Markennamen, "
        "KEINE Wörter/Buchstaben, die im Bild erscheinen könnten, keine Personen-Nahaufnahmen.",
        "13. Antworte NUR mit dem JSON-Array, ohne Erklärtext, ohne Markdown-Codeblock.",
        "",
        "HUBS:",
    ]
    for _, row in work.iterrows():
        prompt_lines += [
            f"pattern_id={row['pattern_id']}",
            f"pattern=`{row['query_skeleton']}`",
            f"hub_name={hub_public_topic(row['hub_label'])}",
            f"intent_vermutung={row.get('intent', 'informational')}",
            f"gesamtsuchvolumen={row.get('total_search_volume', 0)}",
            f"validierte_keywords={row.get('validated_keywords', '')}",
            "",
        ]
    # ~450 output tokens per hub x up to 15 hubs sits too close to the 8192 default —
    # a truncated JSON array would discard ALL AI templates as a parse error.
    text, call_status = _call_gemini(
        "\n".join(prompt_lines), api_key, model=model, timeout=timeout,
        temperature=0.4, max_output_tokens=16384, response_schema=ARTICLE_TEMPLATE_RESPONSE_SCHEMA,
    )
    if text is None:
        return {}, "gemini_failed"
    try:
        parsed = json.loads(text)
    except ValueError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return {}, "parse_error"
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return {}, "parse_error"
    if not isinstance(parsed, list):
        return {}, "parse_error"

    skeletons = dict(zip(work["pattern_id"], work["query_skeleton"]))
    templates: Dict[str, dict] = {}
    for entry in parsed:
        pattern_id = str(entry.get("pattern_id", "")).strip() if isinstance(entry, dict) else ""
        if pattern_id not in skeletons:
            continue
        sanitized = _sanitize_ai_template(entry, skeletons[pattern_id])
        if sanitized:
            templates[pattern_id] = sanitized
    if not templates:
        return {}, "parse_error"
    return templates, ("gemini_ok" if len(templates) == len(work) else "gemini_partial")


def enrich_hub_plan_with_article_templates(
    hub_plan: pd.DataFrame,
    api_key: Optional[str] = None,
    model: str = DEFAULT_AI_MODEL,
    use_ai: bool = True,
    timeout: int = GEMINI_TIMEOUT_S,
) -> Tuple[pd.DataFrame, str]:
    """Fill the per-hub template columns: AI values where available, static profile otherwise."""
    hub_plan = hub_plan.copy()
    if hub_plan.empty:
        for col in TEMPLATE_COLUMNS:
            if col not in hub_plan.columns:
                hub_plan[col] = pd.Series(dtype=object)
        return hub_plan, "no_hubs"

    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not use_ai:
        ai_map, status = {}, "disabled"
    elif not api_key:
        ai_map, status = {}, "no_api_key"
    else:
        ai_map, status = generate_ai_article_templates(hub_plan, api_key, model=model, timeout=timeout)

    filled_rows = []
    for _, row in hub_plan.iterrows():
        skeleton = row["query_skeleton"]
        static = static_article_template(skeleton, row["hub_label"], row.get("intent", "informational"))
        ai = ai_map.get(row["pattern_id"]) or {}

        def merged(field: str, static_value):
            value = ai.get(field)
            return value if value not in (None, "", []) else static_value

        outline = merged("outline", static["outline"])
        faq = merged("faq", static["faq"])
        word_count = merged("word_count", static["word_count"])
        values = dict(row)
        values.update({
            "intent": merged("intent", static["intent"]),
            "template_source": "gemini" if ai else "static",
            "h1_template": merged("h1_template", static["h1_template"]),
            "meta_title_template": merged("meta_title_template", static["meta_title_template"]),
            "meta_description_template": merged("meta_description_template", static["meta_description_template"]),
            "intro_template": merged("intro_template", static["intro_template"]),
            "article_outline_json": json.dumps(outline, ensure_ascii=False),
            "faq_questions": " | ".join(faq),
            "schema_org_suggestion": ", ".join(merged("schema_org", static["schema_org"])),
            "word_count_target": f"{word_count[0]}–{word_count[1]} Wörter",
            "eeat_checklist": " | ".join(merged("eeat_checklist", static["eeat_checklist"])),
            "hero_image_scene": merged("hero_image_scene", ""),
        })
        values["article_title_template"] = values["h1_template"]
        values["recommended_article_structure"] = " → ".join(item["h2"] for item in outline)
        values["internal_linking_strategy"] = merged("internal_linking", row.get("internal_linking_strategy", ""))
        filled_rows.append(values)

    return pd.DataFrame(filled_rows), status


# ---------------------------------------------------------------------------
# Per-hub hero images (photorealistic, 16:9)
#
# Primary: the free Gemini image model via the same generateContent endpoint.
# Fallback: Pollinations.ai (no key needed; anonymous output may carry a small
# watermark — the provider is recorded per hub so the user knows). Both failing
# is fine: the ready-to-paste image prompt always lands in content_hub_plan.csv.
# ---------------------------------------------------------------------------

GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"  # only Gemini image model with a free tier (~500/day)
POLLINATIONS_URL_TEMPLATE = "https://image.pollinations.ai/prompt/{prompt}"
MAX_HERO_IMAGES = 10
HERO_IMAGE_WIDTH, HERO_IMAGE_HEIGHT = 1280, 720  # 16:9, og:image-safe
HERO_IMAGE_JPEG_QUALITY = 82
HERO_EMBED_MAX_BYTES = 400_000  # per-image cap for base64 embedding in the HTML report
GEMINI_IMAGE_MIN_INTERVAL = 6.5  # seconds between Gemini image calls (~10 RPM free tier)
POLLINATIONS_MIN_INTERVAL = 15.0  # anonymous Pollinations limit ~1 request / 15s
HERO_TIME_BUDGET_S = 240.0
HERO_IMAGE_STYLE_SUFFIX = (
    "Photorealistic editorial photography, wide 16:9 hero image, soft natural daylight, "
    "clean minimal composition, subject centered with generous margins, shallow depth of field, "
    "high detail, muted warm tones, evergreen scene without seasonal props or visible brand logos. "
    "Strictly no text, no letters, no numbers, no logos, no watermarks, no captions."
)


@dataclass
class HeroImageResult:
    pattern_id: str
    hub_label: str
    hero_image_prompt: str
    hero_image_file: str = ""
    hero_image_provider: str = ""  # "gemini" | "pollinations" | ""
    hero_image_status: str = ""    # "ok" | "skipped_disabled" | "skipped_cap" |
                                   # "skipped_time_budget" | "failed_all_providers"
    image_bytes: Optional[bytes] = None
    mime_type: str = ""


def build_hero_image_prompt(
    hub_label: str, query_skeleton: str, validated_keywords: str, scene: str = ""
) -> str:
    # An AI-written visual scene (from the template batch call) beats a keyword-based
    # prompt: image models tend to paint quoted keywords as garbled text into the image.
    if scene:
        return f"{scene} {HERO_IMAGE_STYLE_SUFFIX}"
    topic = hub_public_topic(hub_label)
    return (
        f"Website hero image about the German topic {topic} (search pattern: {query_skeleton}). "
        f"Show one clear real-world scene that visually represents this topic, told purely through "
        f"objects and setting. {HERO_IMAGE_STYLE_SUFFIX}"
    )


def _generate_hero_image_gemini(
    prompt: str, api_key: str, model: str = GEMINI_IMAGE_MODEL, timeout: int = 60
) -> Tuple[Optional[bytes], str, str]:
    """Returns (raw_bytes, mime_type, error_code)."""
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "16:9"},
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        GEMINI_API_URL_TEMPLATE.format(model=model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return None, "", "rate_limited"
        if exc.code in (400, 401, 403, 404):
            return None, "", "auth_or_model_error"
        return None, "", "server_error"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None, "", "network_error"
    try:
        parts = payload["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return None, "", "blocked_or_empty"
    inline = next((p["inlineData"] for p in parts if isinstance(p, dict) and p.get("inlineData")), None)
    if not inline or not inline.get("data"):
        return None, "", "blocked_or_empty"
    try:
        raw = base64.b64decode(inline["data"])
    except (ValueError, TypeError):
        return None, "", "blocked_or_empty"
    return raw, inline.get("mimeType", "image/png"), ""


def _generate_hero_image_pollinations(
    prompt: str, seed: int, width: int = HERO_IMAGE_WIDTH, height: int = HERO_IMAGE_HEIGHT, timeout: int = 90
) -> Tuple[Optional[bytes], str, str]:
    url = (
        POLLINATIONS_URL_TEMPLATE.format(prompt=urllib.parse.quote(prompt, safe=""))
        + f"?width={width}&height={height}&model=flux&nologo=true&seed={seed}&safe=true"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "seo-hub-finder/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        return None, "", ("rate_limited" if exc.code == 429 else "server_error")
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, "", "network_error"
    # Pollinations sometimes returns an HTML error page with status 200 — trust
    # only real image magic bytes of a plausible size.
    if len(raw) < 10_000:
        return None, "", "invalid_image"
    if raw[:2] == b"\xff\xd8":
        return raw, "image/jpeg", ""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw, "image/png", ""
    return None, "", "invalid_image"


def _postprocess_hero_image(raw: bytes, mime: str) -> Tuple[bytes, str, str]:
    """Normalize to exactly 1280x720 JPEG via Pillow; passthrough when Pillow is missing."""
    try:
        from PIL import Image
    except ImportError:
        return raw, ("png" if mime == "image/png" else "jpg"), mime
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        scale = max(HERO_IMAGE_WIDTH / img.width, HERO_IMAGE_HEIGHT / img.height)
        img = img.resize(
            (max(HERO_IMAGE_WIDTH, round(img.width * scale)), max(HERO_IMAGE_HEIGHT, round(img.height * scale))),
            Image.LANCZOS,
        )
        left = (img.width - HERO_IMAGE_WIDTH) // 2
        top = (img.height - HERO_IMAGE_HEIGHT) // 2
        img = img.crop((left, top, left + HERO_IMAGE_WIDTH, top + HERO_IMAGE_HEIGHT))
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=HERO_IMAGE_JPEG_QUALITY, optimize=True)
        return buffer.getvalue(), "jpg", "image/jpeg"
    except Exception:
        return raw, ("png" if mime == "image/png" else "jpg"), mime


def generate_hero_images(
    hub_plan: pd.DataFrame,
    api_key: Optional[str] = None,
    enabled: bool = True,
    max_images: int = MAX_HERO_IMAGES,
    time_budget_seconds: float = HERO_TIME_BUDGET_S,
    gemini_min_interval: float = GEMINI_IMAGE_MIN_INTERVAL,
    pollinations_min_interval: float = POLLINATIONS_MIN_INTERVAL,
) -> List[HeroImageResult]:
    """One hero image per confirmed hub, biggest hubs first. Never raises."""
    if hub_plan.empty:
        return []
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    results: List[HeroImageResult] = []
    used_names: set = set()
    generated = 0
    start = time.monotonic()
    last_gemini = last_pollinations = 0.0
    gemini_disabled = api_key is None
    pollinations_disabled = False

    def _pace(last_ts: float, interval: float) -> float:
        wait = interval - (time.monotonic() - last_ts)
        if last_ts and wait > 0:
            time.sleep(wait)
        return time.monotonic()

    for _, row in hub_plan.iterrows():
        prompt = build_hero_image_prompt(
            row.get("hub_label", ""), row.get("query_skeleton", ""),
            row.get("validated_keywords", ""), scene=str(row.get("hero_image_scene", "") or ""),
        )
        result = HeroImageResult(
            pattern_id=row["pattern_id"], hub_label=row.get("hub_label", ""), hero_image_prompt=prompt
        )
        results.append(result)
        if not enabled:
            result.hero_image_status = "skipped_disabled"
            continue
        if row.get("duplicate_of", ""):
            result.hero_image_status = "skipped_duplicate_hub"
            continue
        if generated >= max_images:
            result.hero_image_status = "skipped_cap"
            continue
        if time.monotonic() - start > time_budget_seconds:
            result.hero_image_status = "skipped_time_budget"
            continue

        raw: Optional[bytes] = None
        mime = provider = ""
        try:
            if not gemini_disabled:
                last_gemini = _pace(last_gemini, gemini_min_interval)
                raw, mime, error = _generate_hero_image_gemini(prompt, api_key)
                if raw:
                    provider = "gemini"
                elif error in ("rate_limited", "auth_or_model_error"):
                    gemini_disabled = True  # won't heal within this run
            if raw is None and not pollinations_disabled:
                seed = int(hashlib.sha1(row["pattern_id"].encode("utf-8")).hexdigest()[:8], 16) % 1_000_000
                last_pollinations = _pace(last_pollinations, pollinations_min_interval)
                raw, mime, error = _generate_hero_image_pollinations(prompt, seed)
                if raw:
                    provider = "pollinations"
                elif error == "rate_limited":
                    pollinations_disabled = True
        except Exception:
            raw = None

        if raw is None:
            result.hero_image_status = "failed_all_providers"
            continue
        processed, ext, mime = _postprocess_hero_image(raw, mime)
        base = slugify(hub_public_topic(result.hub_label))
        if base in used_names:
            base = f"{base}-{slugify(result.pattern_id)}"
        used_names.add(base)
        result.hero_image_file = f"{base}.{ext}"
        result.hero_image_provider = provider
        result.hero_image_status = "ok"
        result.image_bytes = processed
        result.mime_type = mime
        generated += 1
    return results


def attach_hero_image_metadata(hub_plan: pd.DataFrame, results: List[HeroImageResult]) -> pd.DataFrame:
    meta_columns = ["pattern_id", "hero_image_file", "hero_image_prompt", "hero_image_provider", "hero_image_status"]
    if hub_plan.empty:
        hub_plan = hub_plan.copy()
        for col in meta_columns[1:]:
            if col not in hub_plan.columns:
                hub_plan[col] = pd.Series(dtype=object)
        return hub_plan
    meta = pd.DataFrame(
        [{c: getattr(r, c) for c in meta_columns} for r in results], columns=meta_columns
    )
    merged = hub_plan.merge(meta, how="left", on="pattern_id")
    for col in meta_columns[1:]:
        merged[col] = merged[col].fillna("")
    return merged


def write_new_keyword_prompt(patterns: pd.DataFrame, out_path: Path) -> None:
    lines = [
        "# New Keyword Candidate Prompt",
        "",
        "You are an SEO strategist. Each pattern below is a content hub already validated by real "
        "Google Search Console ranking data, with example queries that already rank. Suggest "
        "additional REAL, plausible search queries that fit the same structure but are not already "
        "in the example list — e.g. other real product models, brands, or city names, whichever fits "
        "the pattern. Only suggest things that plausibly exist and get searched; do not invent fake "
        "brands, products or places.",
        "",
        "Return your answer as a CSV with exactly two columns: `pattern_id,candidate_query`. "
        "Suggest up to 8 new candidates per pattern.",
        "",
    ]
    accepted = patterns[patterns["is_programmatic_opportunity"]] if not patterns.empty else patterns
    if accepted.empty:
        lines.append("No validated patterns yet — run the tool on GSC data first.")
    else:
        for _, row in accepted.iterrows():
            lines += [
                f"## {row['pattern_id']} — {row['hub_label']}",
                f"Pattern: `{row['query_skeleton']}`",
                f"Existing example queries already ranking: {row['sample_queries']}",
                "",
            ]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def normalize_new_keywords(path: Optional[Path]) -> pd.DataFrame:
    columns = ["pattern_id", "candidate_query"]
    if not path:
        return pd.DataFrame(columns=columns)
    df = normalize_columns(read_csv_safely(path), NEW_KEYWORD_ALIASES)
    if "pattern_id" not in df.columns:
        raise ValueError("New-keyword candidates CSV needs a pattern_id column.")
    if "candidate_query" not in df.columns:
        raise ValueError("New-keyword candidates CSV needs a candidate_query/keyword column.")
    df = df.dropna(subset=["pattern_id", "candidate_query"])
    df["pattern_id"] = df["pattern_id"].astype(str).str.strip()
    df["candidate_query"] = df["candidate_query"].astype(str).apply(normalize_text)
    df = df[df["candidate_query"].str.len() > 1]
    return df[columns].drop_duplicates()


def _trends_confirms(anchor_score: float, cand_score: float, min_relative: float) -> bool:
    if cand_score <= 0:
        return False
    if anchor_score <= 0:
        return True
    return (cand_score / anchor_score) >= min_relative


def check_new_keyword_relevance(
    candidates: pd.DataFrame,
    memberships: pd.DataFrame,
    geo: str = "DE",
    timeframe: str = "today 12-m",
    min_relative_to_anchor: float = 0.1,
    max_candidates: int = MAX_TRENDS_CANDIDATES,
    time_budget_seconds: float = TRENDS_TIME_BUDGET_S,
) -> pd.DataFrame:
    """Check candidate queries against Google Trends, relative to a real GSC-proven anchor keyword.

    Google Trends only returns a 0-100 relative score per request batch, not an absolute
    volume — so each candidate is checked alongside the pattern's best-performing real GSC
    query. That turns an unlabeled Trends number into "about as searched as a keyword we
    know gets real traffic here", which is comparable across different candidates/patterns.
    """
    columns = [
        "pattern_id", "candidate_query", "anchor_query", "trends_score_candidate",
        "trends_score_anchor", "trends_status",
    ]
    if candidates.empty:
        result = pd.DataFrame(columns=columns)
        result.attrs["truncated_candidates"] = 0
        return result

    anchors: Dict[str, str] = {}
    if not memberships.empty:
        best = memberships.sort_values("clicks", ascending=False).groupby("pattern_id").first()
        anchors = best["query"].to_dict()

    # Distribute the cap fairly across patterns instead of letting whichever
    # pattern happens to come first consume the entire budget.
    per_pattern = max(1, max_candidates // max(1, candidates["pattern_id"].nunique()))
    work = candidates.groupby("pattern_id", sort=False).head(per_pattern).head(max_candidates).copy()
    truncated = len(candidates) - len(work)

    try:
        from pytrends.request import TrendReq
    except ImportError:
        result = work.copy()
        result["anchor_query"] = result["pattern_id"].map(anchors).fillna("")
        result["trends_score_candidate"] = 0.0
        result["trends_score_anchor"] = 0.0
        result["trends_status"] = "trends_unavailable"
        result = result[columns]
        result.attrs["truncated_candidates"] = truncated
        return result

    try:
        # Some pytrends versions fetch a Google cookie in the constructor —
        # a network failure here must not crash the whole pipeline.
        pytrends = TrendReq(hl="de-DE", tz=60, timeout=(5, 10))
    except Exception:
        result = work.copy()
        result["anchor_query"] = result["pattern_id"].map(anchors).fillna("")
        result["trends_score_candidate"] = 0.0
        result["trends_score_anchor"] = 0.0
        result["trends_status"] = "trends_unavailable"
        result = result[columns]
        result.attrs["truncated_candidates"] = truncated
        return result

    deadline = time.monotonic() + time_budget_seconds
    rows = []
    for _, row in work.iterrows():
        pattern_id = row["pattern_id"]
        candidate = row["candidate_query"]
        anchor = anchors.get(pattern_id, "")
        if not anchor:
            rows.append({
                "pattern_id": pattern_id, "candidate_query": candidate, "anchor_query": "",
                "trends_score_candidate": 0.0, "trends_score_anchor": 0.0, "trends_status": "no_anchor_available",
            })
            continue
        if candidate == anchor:
            # A duplicate keyword pair makes pytrends raise; the anchor already ranks anyway.
            rows.append({
                "pattern_id": pattern_id, "candidate_query": candidate, "anchor_query": anchor,
                "trends_score_candidate": 0.0, "trends_score_anchor": 0.0, "trends_status": "already_ranking_anchor",
            })
            continue
        if time.monotonic() > deadline:
            rows.append({
                "pattern_id": pattern_id, "candidate_query": candidate, "anchor_query": anchor,
                "trends_score_candidate": 0.0, "trends_score_anchor": 0.0, "trends_status": "skipped_time_budget",
            })
            continue

        anchor_score = cand_score = 0.0
        status = "check_failed"
        for attempt in range(2):
            try:
                pytrends.build_payload([anchor, candidate], timeframe=timeframe, geo=geo)
                trend_df = pytrends.interest_over_time()
                # Trends silently drops zero-interest terms from the result frame —
                # a missing column means "no measurable interest", not a failed check.
                if not trend_df.empty:
                    anchor_score = float(trend_df[anchor].mean()) if anchor in trend_df.columns else 0.0
                    cand_score = float(trend_df[candidate].mean()) if candidate in trend_df.columns else 0.0
                status = "confirmed" if _trends_confirms(anchor_score, cand_score, min_relative_to_anchor) else "no_signal"
                break
            except Exception:
                status = "check_failed"
                if time.monotonic() > deadline:
                    break
                time.sleep(3 * (attempt + 1))
        rows.append({
            "pattern_id": pattern_id, "candidate_query": candidate, "anchor_query": anchor,
            "trends_score_candidate": round(cand_score, 2), "trends_score_anchor": round(anchor_score, 2),
            "trends_status": status,
        })
        time.sleep(1)

    result = pd.DataFrame(rows, columns=columns)
    result.attrs["truncated_candidates"] = truncated
    return result


def write_ai_prompt(patterns: pd.DataFrame, out_path: Path) -> None:
    intro = """# AI Pattern Review Prompt

You are an SEO strategist reviewing programmatic SEO opportunities discovered from Google Search Console data.

Important rules:
- Do not invent patterns that are not present in the table.
- Treat GSC ranking evidence as proof of topical relevance, not proof of search demand.
- Recommend a hub only when the pattern has sufficient slot diversity and volume validation.
- Flag cannibalization, thin-content risk and duplicated intent.

Return JSON with: pattern_id, decision, hub_name, template_summary, risk_notes, internal_linking_notes.

## Pattern candidates
"""
    table = patterns.to_markdown(index=False) if not patterns.empty else "No pattern candidates found."
    out_path.write_text(intro + "\n" + table + "\n", encoding="utf-8")


def _fill_slots(template: str, values: Sequence[str]) -> str:
    filled = str(template or "")
    for i, value in enumerate(values, start=1):
        if value:
            filled = filled.replace(f"{{slot_{i}}}", value)
    # A slot value at sentence start comes in lowercase ("jura e8 ...") — capitalize.
    return filled[:1].upper() + filled[1:] if filled else filled


def build_article_plan(opportunities: pd.DataFrame, hub_plan: pd.DataFrame) -> pd.DataFrame:
    """One row per confirmed keyword = one article, with templates already slot-filled.

    This is the editorial-calendar view: while content_hub_plan.csv is one row per hub,
    a content team needs each article's ready-to-use H1/meta/URL next to its volume.
    """
    columns = [
        "pattern_id", "hub_label", "keyword", "search_volume", "article_h1",
        "article_meta_title", "article_meta_description", "article_url", "status", "existing_url",
    ]
    if opportunities.empty or hub_plan.empty:
        return pd.DataFrame(columns=columns)
    confirmed = opportunities[opportunities["final_status"] == "confirmed_opportunity"]
    if confirmed.empty:
        return pd.DataFrame(columns=columns)
    hub_lookup = hub_plan.set_index("pattern_id").to_dict("index")
    rows = []
    for _, r in confirmed.iterrows():
        hub = hub_lookup.get(r["pattern_id"])
        if not hub:
            continue
        slot_values = [v.strip() for v in str(r.get("slot_values", "")).split("|") if v.strip()]
        slug_values = [slugify(v) for v in slot_values]
        existing_url = str(r.get("current_url", "") or "")
        rows.append({
            "pattern_id": r["pattern_id"],
            "hub_label": hub.get("hub_label", ""),
            "keyword": r["query"],
            "search_volume": int(r.get("search_volume", 0)),
            "article_h1": _fill_slots(hub.get("h1_template", ""), slot_values),
            "article_meta_title": _fill_slots(hub.get("meta_title_template", ""), slot_values),
            "article_meta_description": _fill_slots(hub.get("meta_description_template", ""), slot_values),
            "article_url": _fill_slots(hub.get("url_template", ""), slug_values),
            "status": "already_covered_by_existing_page" if existing_url else "new_article",
            "existing_url": existing_url,
        })
    # Trends-confirmed AI keywords are the genuinely NEW article opportunities —
    # include them with their own status so the editorial calendar is complete.
    seen = {(r["pattern_id"], r["keyword"]) for r in rows}
    for _, hub in hub_plan.iterrows():
        skeleton = str(hub.get("query_skeleton", ""))
        for keyword in str(hub.get("ai_suggested_keywords", "") or "").split(";"):
            keyword = keyword.strip()
            if not keyword or (hub["pattern_id"], keyword) in seen:
                continue
            slot_values = list(extract_slots(tokenize(keyword), skeleton))
            slug_values = [slugify(v) for v in slot_values]
            rows.append({
                "pattern_id": hub["pattern_id"],
                "hub_label": hub.get("hub_label", ""),
                "keyword": keyword,
                "search_volume": 0,
                "article_h1": _fill_slots(hub.get("h1_template", ""), slot_values),
                "article_meta_title": _fill_slots(hub.get("meta_title_template", ""), slot_values),
                "article_meta_description": _fill_slots(hub.get("meta_description_template", ""), slot_values),
                "article_url": _fill_slots(hub.get("url_template", ""), slug_values),
                "status": "new_article_ai_suggested",
                "existing_url": "",
            })
    plan = pd.DataFrame(rows, columns=columns).drop_duplicates(["pattern_id", "keyword"])
    return plan.sort_values(["pattern_id", "search_volume"], ascending=[True, False])


def hub_template_markdown(row) -> str:
    """Per-hub template block, shared by the markdown export and the Streamlit UI."""
    get = row.get if hasattr(row, "get") else lambda k, d="": getattr(row, k, d)
    try:
        outline = json.loads(get("article_outline_json", "") or "[]")
    except (ValueError, TypeError):
        outline = []
    lines = [
        f"## {get('hub_label', '')}",
        f"*Intent: {get('intent', '')} · Template-Quelle: {get('template_source', 'static')}*",
        "",
        f"**Hub-URL:** `{get('hub_slug', '')}` · **Pattern:** `{get('query_skeleton', '')}` · "
        f"**URL-Template:** `{get('url_template', '')}`",
        "",
        f"**H1:** {get('h1_template', '')}",
        f"**Meta-Titel:** {get('meta_title_template', '')}",
        f"**Meta-Description:** {get('meta_description_template', '')}",
        f"**Einleitung (Vorlage):** {get('intro_template', '')}",
        "",
        "### Gliederung",
    ]
    if outline:
        for item in outline:
            lines.append(f"- {item.get('h2', '')}")
            for h3 in item.get("h3", []):
                lines.append(f"  - {h3}")
    else:
        lines.append(str(get("recommended_article_structure", "")))
    faq = [q for q in str(get("faq_questions", "")).split(" | ") if q]
    if faq:
        lines += ["", "### FAQ"]
        lines += [f"- {q}" for q in faq]
    lines += [
        "",
        f"**Schema.org:** {get('schema_org_suggestion', '')} · **Wortanzahl:** {get('word_count_target', '')}",
    ]
    eeat = [e for e in str(get("eeat_checklist", "")).split(" | ") if e]
    if eeat:
        lines += ["", "### E-E-A-T-Checkliste"]
        lines += [f"- {e}" for e in eeat]
    lines += [
        "",
        "### Interne Verlinkung",
        str(get("internal_linking_strategy", "")),
        "",
        f"### Validierte Artikel-Keywords ({get('article_count', 0)} Artikel, "
        f"{get('total_search_volume', 0)} Suchvolumen gesamt)",
        str(get("validated_keywords", "")),
        "",
        f"**AI-Keywords (Trends-bestätigt):** {get('ai_suggested_keywords', '') or '(keine)'}",
        "",
        "### Risiken / Human Review",
        str(get("risks", "")),
    ]
    return "\n".join(lines)


def write_article_templates_md(hub_plan: pd.DataFrame, patterns: pd.DataFrame, out_path: Path) -> None:
    lines = ["# Artikel-Templates & interne Verlinkung", ""]
    if hub_plan.empty:
        lines += [
            "Noch kein volumen-bestätigter Content-Hub.",
            "Tool mit Volumen-CSV erneut ausführen, nachdem `keyword_volume_check_queue.csv` geprüft wurde.",
        ]
    else:
        for _, row in hub_plan.iterrows():
            lines += [hub_template_markdown(row), "", "---", ""]
    out_path.write_text("\n".join(lines), encoding="utf-8")


def df_to_html_table(df: pd.DataFrame, max_rows: int = 50) -> str:
    if df.empty:
        return "<p><em>No rows.</em></p>"
    return df.head(max_rows).to_html(index=False, escape=True, classes="data-table")


def data_quality_notes(patterns: pd.DataFrame) -> List[str]:
    notes: List[str] = []
    if patterns.attrs.get("position_data_missing"):
        notes.append(
            "No Position column found in the GSC export. Ranking-proof filtering fell back to "
            "clicks/impressions only, and confidence scores do not include a position signal — "
            "treat confidence as less reliable for this run."
        )
    truncated = patterns.attrs.get("truncated_rows", 0)
    if truncated:
        notes.append(
            f"The GSC export had more than {MAX_GSC_ROWS:,} rows; the {truncated:,} lowest-impression "
            "rows were dropped before pattern discovery to keep the run fast."
        )
    return notes


def render_article_templates_html(hub_plan: pd.DataFrame, ai_template_status: str = "") -> str:
    if hub_plan.empty or "h1_template" not in hub_plan.columns:
        return "<p><em>No confirmed hubs with templates yet.</em></p>"
    status_line = TEMPLATE_STATUS_MESSAGES.get(ai_template_status, "")
    blocks = [f"<p><em>{html.escape(status_line)}</em></p>"] if status_line else []
    for _, row in hub_plan.iterrows():
        try:
            outline = json.loads(row.get("article_outline_json", "") or "[]")
        except (ValueError, TypeError):
            outline = []
        outline_html = "".join(
            f"<li>{html.escape(item.get('h2', ''))}"
            + ("<ul>" + "".join(f"<li>{html.escape(h3)}</li>" for h3 in item.get("h3", [])) + "</ul>" if item.get("h3") else "")
            + "</li>"
            for item in outline
        )
        faq_html = "".join(
            f"<li>{html.escape(q)}</li>" for q in str(row.get("faq_questions", "")).split(" | ") if q
        )
        blocks.append(f"""<article class="hub-template">
<h3>{html.escape(str(row.get('hub_label', '')))} <span class="badge">{html.escape(str(row.get('intent', '')))} · {html.escape(str(row.get('template_source', 'static')))}</span></h3>
<p><strong>H1:</strong> {html.escape(str(row.get('h1_template', '')))}<br>
<strong>Meta-Titel:</strong> {html.escape(str(row.get('meta_title_template', '')))}<br>
<strong>Meta-Description:</strong> {html.escape(str(row.get('meta_description_template', '')))}<br>
<strong>Einleitung:</strong> {html.escape(str(row.get('intro_template', '')))}</p>
<p><strong>Gliederung:</strong></p><ol>{outline_html}</ol>
<p><strong>FAQ:</strong></p><ul>{faq_html}</ul>
<p><strong>Schema.org:</strong> {html.escape(str(row.get('schema_org_suggestion', '')))} ·
<strong>Wortanzahl:</strong> {html.escape(str(row.get('word_count_target', '')))}</p>
<p><strong>Interne Verlinkung:</strong> {html.escape(str(row.get('internal_linking_strategy', '')))}</p>
</article>""")
    return "\n".join(blocks)


def hero_images_html_section(hero_images: Optional[List[HeroImageResult]]) -> str:
    if not hero_images:
        return "<p><em>No hero images generated for this run.</em></p>"
    figures = []
    for result in hero_images:
        caption = html.escape(f"{result.hub_label} — hero_images/{result.hero_image_file} ({result.hero_image_provider})")
        if result.image_bytes and len(result.image_bytes) <= HERO_EMBED_MAX_BYTES:
            b64 = base64.b64encode(result.image_bytes).decode("ascii")
            figures.append(
                f'<figure><img src="data:{result.mime_type};base64,{b64}" alt="{html.escape(result.hub_label)}" '
                f'loading="lazy" style="width:100%;border-radius:12px"><figcaption>{caption}</figcaption></figure>'
            )
        elif result.image_bytes:
            figures.append(
                f"<p><strong>{html.escape(result.hub_label)}:</strong> saved as "
                f"<code>hero_images/{html.escape(result.hero_image_file)}</code> (too large to embed inline).</p>"
            )
        else:
            figures.append(
                f"<p><strong>{html.escape(result.hub_label)}:</strong> {html.escape(result.hero_image_status)} — "
                f"prompt: <code>{html.escape(result.hero_image_prompt)}</code></p>"
            )
    return '<div class="hero-grid">' + "".join(figures) + "</div>"


def write_html_report(
    patterns: pd.DataFrame,
    queue: pd.DataFrame,
    opportunities: pd.DataFrame,
    hub_plan: pd.DataFrame,
    out_path: Path,
    existing_coverage: Optional[pd.DataFrame] = None,
    new_keywords_checked: Optional[pd.DataFrame] = None,
    article_plan: Optional[pd.DataFrame] = None,
    hero_images: Optional[List[HeroImageResult]] = None,
    ai_template_status: str = "",
) -> None:
    confirmed_count = 0 if opportunities.empty else int((opportunities["final_status"] == "confirmed_opportunity").sum())
    notes = data_quality_notes(patterns)
    notes_html = (
        '<section class="card notes"><h2>Data quality notes</h2><ul>'
        + "".join(f"<li>{html.escape(note)}</li>" for note in notes)
        + "</ul></section>"
        if notes
        else ""
    )
    # The hub-plan table stays readable: long template texts get their own section below.
    hub_plan_table = hub_plan.drop(
        columns=[c for c in TEMPLATE_COLUMNS + ["article_title_template", "recommended_article_structure",
                                                 "internal_linking_strategy", "hero_image_prompt"]
                 if c in hub_plan.columns and c != "intent"],
        errors="ignore",
    )
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SEO Hub Finder Report</title>
<style>
body {{ font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #faf7f3; color: #1f2933; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 36px 20px 60px; }}
.card {{ background: white; border: 1px solid #eadfd5; border-radius: 16px; padding: 22px; margin: 18px 0; box-shadow: 0 8px 24px rgba(31, 41, 51, .06); overflow-x: auto; }}
.card.notes {{ background: #fff6e5; border-color: #f0d9a6; }}
h1 {{ font-size: 34px; margin-bottom: 6px; }}
h2 {{ margin-top: 8px; }}
.badges {{ display:flex; flex-wrap:wrap; gap: 10px; margin: 22px 0; }}
.badge {{ background: #efecea; border-radius: 999px; padding: 10px 14px; font-weight: 700; }}
.data-table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
.data-table th, .data-table td {{ border: 1px solid #e5e0da; padding: 8px; text-align: left; vertical-align: top; }}
.data-table th {{ background: #f4eee8; }}
code {{ background:#f4eee8; padding:2px 5px; border-radius:5px; }}
.hub-template {{ border-top: 1px solid #eadfd5; padding: 14px 0; }}
.hub-template .badge {{ font-size: 12px; font-weight: 600; }}
.hero-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }}
.hero-grid figcaption {{ font-size: 12px; color: #6b7280; margin-top: 6px; }}
</style>
</head>
<body><main>
<h1>SEO Hub Finder Report</h1>
<p>GSC-based programmatic SEO pattern discovery with search-volume validation gate.</p>
<div class="badges">
  <div class="badge">Patterns: {len(patterns)}</div>
  <div class="badge">Volume queue keywords: {len(queue)}</div>
  <div class="badge">Confirmed keyword opportunities: {confirmed_count}</div>
  <div class="badge">Hubs: {len(hub_plan)}</div>
</div>
{notes_html}
<section class="card"><h2>1. Discovered Pattern Candidates</h2>{df_to_html_table(patterns)}</section>
<section class="card"><h2>2. Existing Pages Already Covering a Pattern</h2><p>Query variants that already rank the same URL — not new opportunities, just proof the pattern is real.</p>{df_to_html_table(existing_coverage if existing_coverage is not None else pd.DataFrame())}</section>
<section class="card"><h2>3. Keyword Volume Check Queue</h2><p>Export this list, check search volume externally, then re-import the volume CSV.</p>{df_to_html_table(queue)}</section>
<section class="card"><h2>4. Volume-Validated Opportunities</h2>{df_to_html_table(opportunities)}</section>
<section class="card"><h2>5. New Keyword Candidates (Google Trends Check)</h2><p>AI-suggested candidates not yet in GSC, checked against Google Trends relative to a real GSC-proven keyword from the same hub. "no_signal" means Trends couldn't detect interest — often true for long-tail terms even when real demand exists — verify manually if you still want to pursue those.</p>{df_to_html_table(new_keywords_checked if new_keywords_checked is not None else pd.DataFrame())}</section>
<section class="card"><h2>6. Content Hub Plan</h2>{df_to_html_table(hub_plan_table)}</section>
<section class="card"><h2>7. Artikel-Templates pro Hub</h2>{render_article_templates_html(hub_plan, ai_template_status)}</section>
<section class="card"><h2>8. Artikel-Plan pro Keyword</h2><p>Eine Zeile = ein Artikel, mit fertig ausgefüllter H1/Meta aus dem Hub-Template.</p>{df_to_html_table(article_plan if article_plan is not None else pd.DataFrame(), max_rows=100)}</section>
<section class="card"><h2>9. Hub Hero Images</h2>{hero_images_html_section(hero_images)}</section>
</main></body></html>"""
    out_path.write_text(html_doc, encoding="utf-8")


OUTPUT_FILENAMES = (
    "discovered_programmatic_patterns.csv",
    "pattern_keyword_memberships.csv",
    "existing_pages_by_pattern.csv",
    "keyword_volume_check_queue.csv",
    "programmatic_opportunities.csv",
    "new_keyword_candidates_checked.csv",
    "content_hub_plan.csv",
    "article_plan_per_keyword.csv",
    "ai_pattern_review_prompt.md",
    "new_keyword_candidates_prompt.md",
    "article_templates_and_linking.md",
    "seo_hub_finder_report.html",
    "seo_hub_finder_outputs.zip",
)


def write_outputs(
    patterns: pd.DataFrame,
    memberships: pd.DataFrame,
    queue: pd.DataFrame,
    opportunities: pd.DataFrame,
    hub_plan: pd.DataFrame,
    out_dir: Path,
    existing_coverage: Optional[pd.DataFrame] = None,
    new_keywords_checked: Optional[pd.DataFrame] = None,
    article_plan: Optional[pd.DataFrame] = None,
    hero_images: Optional[List[HeroImageResult]] = None,
    ai_template_status: str = "",
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear this tool's own leftovers from a previous run so the zip only ever
    # contains the current run's files. Never touch anything else: the user may
    # pass a populated directory (or ".") as --out-dir.
    for name in OUTPUT_FILENAMES:
        target = out_dir / name
        if target.is_file():
            target.unlink()
    hero_dir = out_dir / "hero_images"
    if hero_dir.is_dir():
        for file in hero_dir.iterdir():
            if file.is_file():
                file.unlink()
    existing_coverage = existing_coverage if existing_coverage is not None else pd.DataFrame()
    new_keywords_checked = new_keywords_checked if new_keywords_checked is not None else pd.DataFrame()
    article_plan = article_plan if article_plan is not None else pd.DataFrame()
    patterns.to_csv(out_dir / "discovered_programmatic_patterns.csv", index=False)
    memberships.to_csv(out_dir / "pattern_keyword_memberships.csv", index=False)
    existing_coverage.to_csv(out_dir / "existing_pages_by_pattern.csv", index=False)
    queue.to_csv(out_dir / "keyword_volume_check_queue.csv", index=False)
    opportunities.to_csv(out_dir / "programmatic_opportunities.csv", index=False)
    new_keywords_checked.to_csv(out_dir / "new_keyword_candidates_checked.csv", index=False)
    hub_plan.to_csv(out_dir / "content_hub_plan.csv", index=False)
    article_plan.to_csv(out_dir / "article_plan_per_keyword.csv", index=False)
    saved_images = [r for r in (hero_images or []) if r.image_bytes and r.hero_image_file]
    if saved_images:
        hero_dir.mkdir(exist_ok=True)
        for result in saved_images:
            (hero_dir / result.hero_image_file).write_bytes(result.image_bytes)
    write_ai_prompt(patterns, out_dir / "ai_pattern_review_prompt.md")
    write_new_keyword_prompt(patterns, out_dir / "new_keyword_candidates_prompt.md")
    write_article_templates_md(hub_plan, patterns, out_dir / "article_templates_and_linking.md")
    write_html_report(
        patterns, queue, opportunities, hub_plan, out_dir / "seo_hub_finder_report.html",
        existing_coverage=existing_coverage, new_keywords_checked=new_keywords_checked,
        article_plan=article_plan, hero_images=hero_images, ai_template_status=ai_template_status,
    )
    with zipfile.ZipFile(out_dir / "seo_hub_finder_outputs.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for name in OUTPUT_FILENAMES:
            file = out_dir / name
            if name != "seo_hub_finder_outputs.zip" and file.is_file():
                zf.write(file, arcname=name)
        if hero_dir.is_dir():
            for file in sorted(hero_dir.iterdir()):
                if file.is_file():
                    zf.write(file, arcname=f"hero_images/{file.name}")


def run_pipeline(
    gsc_csv: Path,
    volume_csv: Optional[Path] = None,
    new_keywords_csv: Optional[Path] = None,
    out_dir: Path = Path("out"),
    top_position: float = 10,
    expanded_position: float = 20,
    min_gsc_impressions: float = 20,
    min_pattern_queries: int = 3,
    min_distinct_slot_values: int = 3,
    min_template_confidence: float = 0.45,
    min_volume: float = 10,
    trends_geo: str = "DE",
    min_trends_relative: float = 0.1,
    max_trends_candidates: int = MAX_TRENDS_CANDIDATES,
    ai_api_key: Optional[str] = None,
    ai_model: str = DEFAULT_AI_MODEL,
    ai_candidates_per_pattern: int = 8,
    ai_article_templates: bool = True,
    hero_images_enabled: bool = True,
    max_hero_images: int = MAX_HERO_IMAGES,
    hero_time_budget: float = HERO_TIME_BUDGET_S,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, str]:
    gsc = normalize_gsc(gsc_csv)
    patterns, memberships = discover_patterns(
        gsc=gsc,
        top_position=top_position,
        min_gsc_impressions=min_gsc_impressions,
        expanded_position=expanded_position,
        min_pattern_queries=min_pattern_queries,
        min_distinct_slot_values=min_distinct_slot_values,
        min_template_confidence=min_template_confidence,
    )
    existing_coverage = build_existing_coverage(patterns, memberships)
    queue = build_volume_queue(memberships)
    volume = normalize_volume(volume_csv) if volume_csv else pd.DataFrame(columns=["query", "search_volume", "competition", "cpc"])
    opportunities = merge_volume(memberships, volume, min_volume)
    ai_candidates = generate_ai_keyword_candidates(
        patterns, api_key=ai_api_key, model=ai_model, max_candidates_per_pattern=ai_candidates_per_pattern,
    )
    manual_candidates = normalize_new_keywords(new_keywords_csv)
    new_keywords = pd.concat([ai_candidates, manual_candidates], ignore_index=True).drop_duplicates()
    new_keywords_checked = check_new_keyword_relevance(
        new_keywords, memberships, geo=trends_geo, min_relative_to_anchor=min_trends_relative,
        max_candidates=max_trends_candidates,
    )
    hub_plan = build_hub_plan(opportunities, patterns, new_keywords_checked)
    hub_plan, ai_template_status = enrich_hub_plan_with_article_templates(
        hub_plan, api_key=ai_api_key, model=ai_model, use_ai=ai_article_templates,
    )
    article_plan = build_article_plan(opportunities, hub_plan)
    hero_results = generate_hero_images(
        hub_plan, api_key=ai_api_key, enabled=hero_images_enabled,
        max_images=max_hero_images, time_budget_seconds=hero_time_budget,
    )
    hub_plan = attach_hero_image_metadata(hub_plan, hero_results)
    write_outputs(
        patterns, memberships, queue, opportunities, hub_plan, out_dir,
        existing_coverage, new_keywords_checked, article_plan, hero_results, ai_template_status,
    )
    return (patterns, memberships, queue, opportunities, hub_plan, existing_coverage,
            new_keywords_checked, article_plan, ai_template_status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Find programmatic SEO opportunities from GSC data.")
    parser.add_argument("gsc_csv", help="Google Search Console CSV export")
    parser.add_argument("--volume-csv", default=None, help="Optional Keyword Planner / search-volume CSV")
    parser.add_argument(
        "--new-keywords-csv", default=None,
        help="Optional CSV of AI-suggested candidate keywords (pattern_id,candidate_query) to check against Google Trends",
    )
    parser.add_argument("--out-dir", default="out", help="Output directory")
    parser.add_argument("--top-position", type=float, default=10)
    parser.add_argument("--expanded-position", type=float, default=20)
    parser.add_argument("--min-gsc-impressions", type=float, default=20)
    parser.add_argument("--min-pattern-queries", type=int, default=3)
    parser.add_argument("--min-distinct-slot-values", type=int, default=3)
    parser.add_argument("--min-template-confidence", type=float, default=0.45)
    parser.add_argument("--min-volume", type=float, default=10)
    parser.add_argument("--trends-geo", default="DE", help="Google Trends region code for the new-keyword check")
    parser.add_argument("--min-trends-relative", type=float, default=0.1, help="Min candidate/anchor Trends ratio to confirm a new keyword")
    parser.add_argument("--max-trends-candidates", type=int, default=MAX_TRENDS_CANDIDATES, help="Cap on candidates checked against Trends per run")
    parser.add_argument(
        "--ai-api-key", default=None,
        help="Free Gemini API key for automatic new-keyword suggestions (defaults to the GEMINI_API_KEY env var)",
    )
    parser.add_argument("--ai-model", default=DEFAULT_AI_MODEL, help="Gemini model name for keyword suggestions and article templates")
    parser.add_argument("--ai-candidates-per-pattern", type=int, default=8, help="Max AI-suggested candidates per pattern")
    parser.add_argument(
        "--no-ai-templates", dest="ai_article_templates", action="store_false",
        help="Skip the Gemini article-template call; use static intent-based templates",
    )
    parser.add_argument(
        "--no-hero-images", dest="hero_images_enabled", action="store_false",
        help="Skip per-hub hero image generation (Gemini free tier / Pollinations fallback)",
    )
    parser.add_argument("--max-hero-images", type=int, default=MAX_HERO_IMAGES, help="Cap on hero images generated per run")
    parser.add_argument("--hero-time-budget", type=float, default=HERO_TIME_BUDGET_S, help="Max seconds to spend on hero image generation")
    args = parser.parse_args()

    gsc_path = Path(args.gsc_csv)
    if not gsc_path.exists():
        print(f"Error: GSC CSV not found: {gsc_path}")
        raise SystemExit(1)

    try:
        (patterns, _, queue, opportunities, hub_plan, existing_coverage,
         new_keywords_checked, article_plan, ai_template_status) = run_pipeline(
            gsc_csv=gsc_path,
            volume_csv=Path(args.volume_csv) if args.volume_csv else None,
            new_keywords_csv=Path(args.new_keywords_csv) if args.new_keywords_csv else None,
            out_dir=Path(args.out_dir),
            top_position=args.top_position,
            expanded_position=args.expanded_position,
            min_gsc_impressions=args.min_gsc_impressions,
            min_pattern_queries=args.min_pattern_queries,
            min_distinct_slot_values=args.min_distinct_slot_values,
            min_template_confidence=args.min_template_confidence,
            min_volume=args.min_volume,
            trends_geo=args.trends_geo,
            min_trends_relative=args.min_trends_relative,
            max_trends_candidates=args.max_trends_candidates,
            ai_api_key=args.ai_api_key,
            ai_model=args.ai_model,
            ai_candidates_per_pattern=args.ai_candidates_per_pattern,
            ai_article_templates=args.ai_article_templates,
            hero_images_enabled=args.hero_images_enabled,
            max_hero_images=args.max_hero_images,
            hero_time_budget=args.hero_time_budget,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    print("\nSEO Hub Finder finished.")
    print(f"Discovered patterns: {len(patterns)}")
    print(f"Existing pages covering a pattern: {len(existing_coverage)}")
    print(f"Volume-check keywords: {len(queue)}")
    confirmed = 0 if opportunities.empty else int((opportunities["final_status"] == "confirmed_opportunity").sum())
    print(f"Confirmed keyword opportunities: {confirmed}")
    if not new_keywords_checked.empty:
        ai_confirmed = int((new_keywords_checked["trends_status"] == "confirmed").sum())
        print(f"New AI-suggested keywords confirmed via Trends: {ai_confirmed}/{len(new_keywords_checked)}")
        truncated = new_keywords_checked.attrs.get("truncated_candidates", 0)
        if truncated:
            print(f"Note: {truncated} candidate keyword(s) skipped (over the --max-trends-candidates cap).")
    elif not (args.ai_api_key or os.environ.get("GEMINI_API_KEY")):
        print("Note: no GEMINI_API_KEY set — automatic new-keyword suggestions were skipped. "
              "See new_keyword_candidates_prompt.md for the manual fallback.")
    print(f"Content hubs: {len(hub_plan)}")
    print(f"Article plan rows: {len(article_plan)}")
    print(f"Artikel-Templates: {TEMPLATE_STATUS_MESSAGES.get(ai_template_status, ai_template_status)}")
    if "hero_image_status" in hub_plan.columns and len(hub_plan):
        ok = int((hub_plan["hero_image_status"] == "ok").sum())
        print(f"Hero images: {ok}/{len(hub_plan)} generated (see hero_images/ and content_hub_plan.csv)")
    for note in data_quality_notes(patterns):
        print(f"Note: {note}")
    print(f"Outputs written to: {Path(args.out_dir).resolve()}")


if __name__ == "__main__":
    main()
