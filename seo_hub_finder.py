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
import html
import math
import re
import unicodedata
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
    text = str(value).strip().lower().replace("\xa0", " ")
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
    text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^0-9.]", "", text)
    if not text:
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
    return df[
        (df["position"] <= top_position)
        | (df["clicks"] > 0)
        | ((df["impressions"] >= min_gsc_impressions) & (df["position"] <= expanded_position))
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


def infer_hub_label(skeleton: str) -> str:
    anchors = [p for p in skeleton.split() if not p.startswith("{slot_")]
    meaningful = [a for a in anchors if a not in CONNECTORS and a not in WEAK_ANCHORS]
    if meaningful:
        return f"{meaningful[-1].title()} Hub"
    if anchors:
        return f"{anchors[-1].title()} Pattern Hub"
    return "Programmatic SEO Hub"


def pattern_url_template(skeleton: str) -> str:
    value = strip_accents(normalize_text(skeleton))
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9{}_-]", "", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return f"/{value}/"


def article_structure_for(skeleton: str) -> str:
    return " → ".join([
        "Kurzantwort / Intent Match",
        "Problem oder Use Case",
        "konkrete Anleitung oder Empfehlung",
        "Varianten / Entscheidungskriterien",
        "häufige Fehler",
        "FAQ",
        "interne Links + CTA",
    ])


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
        top10_count = sum(1 for item in items if item["position"] <= top_position) if has_position else 0
        total_clicks = sum(item["clicks"] for item in items)
        total_impressions = sum(item["impressions"] for item in items)
        avg_position = sum(item["position"] for item in items) / max(1, query_count)
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
        hub_label = infer_hub_label(skeleton)
        candidate = PatternCandidate(
            pattern_id=pattern_id,
            query_skeleton=skeleton,
            query_count=query_count,
            slot_diversity=slot_diversity,
            top10_count=top10_count,
            total_clicks=round(total_clicks, 2),
            total_impressions=round(total_impressions, 2),
            avg_position=round(avg_position, 2),
            confidence=confidence,
            is_programmatic_opportunity=(reject_reason == ""),
            reject_reason=reject_reason,
            sample_queries="; ".join(item["query"] for item in items[:8]),
            sample_urls="; ".join(sorted({item["page"] for item in items if item["page"]})[:5]),
            template_label=template_label,
            hub_label=hub_label,
            hub_article_title=f"{hub_label}: Übersicht, Varianten & Empfehlungen",
            hub_slug=f"/{slugify(hub_label)}/",
            url_template=pattern_url_template(skeleton),
            article_title_template=f"{template_label}: Anleitung, Empfehlungen & häufige Fragen",
            recommended_article_structure=article_structure_for(skeleton),
            internal_linking_strategy=(
                "Hub verlinkt auf alle validierten Spoke-Artikel. Jeder Spoke verlinkt zurück zum Hub, "
                "auf 2-4 verwandte Spokes und auf relevante Produkt-/Conversion-Seiten."
            ),
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


def build_hub_plan(opportunities: pd.DataFrame, patterns: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "pattern_id", "hub_label", "hub_article_title", "hub_slug", "query_skeleton", "url_template",
        "validated_keywords", "total_search_volume", "article_count", "article_title_template",
        "recommended_article_structure", "internal_linking_strategy", "risks",
    ]
    if opportunities.empty or patterns.empty:
        return pd.DataFrame(columns=columns)
    confirmed = opportunities[opportunities["final_status"] == "confirmed_opportunity"].copy()
    if confirmed.empty:
        return pd.DataFrame(columns=columns)
    pattern_lookup = patterns.set_index("pattern_id").to_dict("index")
    rows = []
    for pattern_id, sub in confirmed.groupby("pattern_id"):
        p = pattern_lookup.get(pattern_id, {})
        rows.append({
            "pattern_id": pattern_id,
            "hub_label": p.get("hub_label", "Programmatic Hub"),
            "hub_article_title": p.get("hub_article_title", "Programmatic Hub"),
            "hub_slug": p.get("hub_slug", f"/{pattern_id}/"),
            "query_skeleton": p.get("query_skeleton", ""),
            "url_template": p.get("url_template", ""),
            "validated_keywords": "; ".join(sub.sort_values("search_volume", ascending=False)["query"].head(20).tolist()),
            "total_search_volume": int(sub["search_volume"].sum()),
            "article_count": int(sub["query"].nunique()),
            "article_title_template": p.get("article_title_template", ""),
            "recommended_article_structure": p.get("recommended_article_structure", ""),
            "internal_linking_strategy": p.get("internal_linking_strategy", ""),
            "risks": p.get("risks", ""),
        })
    return pd.DataFrame(rows).sort_values(["total_search_volume", "article_count"], ascending=[False, False])


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


def write_article_templates_md(hub_plan: pd.DataFrame, patterns: pd.DataFrame, out_path: Path) -> None:
    lines = ["# Article Templates & Internal Linking Strategy", ""]
    if hub_plan.empty:
        lines += [
            "No volume-confirmed content hub yet.",
            "Run the tool with a volume CSV after checking `keyword_volume_check_queue.csv`.",
        ]
    else:
        for _, row in hub_plan.iterrows():
            lines += [
                f"## {row['hub_label']}",
                f"**Hub URL:** `{row['hub_slug']}`",
                f"**Pattern:** `{row['query_skeleton']}`",
                f"**URL template:** `{row['url_template']}`",
                f"**Article title template:** {row['article_title_template']}",
                f"**Validated article candidates:** {row['validated_keywords']}",
                f"**Total search volume:** {row['total_search_volume']}",
                "",
                "### Recommended article structure",
                row["recommended_article_structure"],
                "",
                "### Internal linking strategy",
                row["internal_linking_strategy"],
                "",
                "### Risks / human review",
                row["risks"],
                "",
            ]
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


def write_html_report(patterns: pd.DataFrame, queue: pd.DataFrame, opportunities: pd.DataFrame, hub_plan: pd.DataFrame, out_path: Path) -> None:
    confirmed_count = 0 if opportunities.empty else int((opportunities["final_status"] == "confirmed_opportunity").sum())
    notes = data_quality_notes(patterns)
    notes_html = (
        '<section class="card notes"><h2>Data quality notes</h2><ul>'
        + "".join(f"<li>{html.escape(note)}</li>" for note in notes)
        + "</ul></section>"
        if notes
        else ""
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
<section class="card"><h2>2. Keyword Volume Check Queue</h2><p>Export this list, check search volume externally, then re-import the volume CSV.</p>{df_to_html_table(queue)}</section>
<section class="card"><h2>3. Volume-Validated Opportunities</h2>{df_to_html_table(opportunities)}</section>
<section class="card"><h2>4. Content Hub Plan</h2>{df_to_html_table(hub_plan)}</section>
</main></body></html>"""
    out_path.write_text(html_doc, encoding="utf-8")


def write_outputs(patterns: pd.DataFrame, memberships: pd.DataFrame, queue: pd.DataFrame, opportunities: pd.DataFrame, hub_plan: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear leftovers from a previous run into the same out-dir so the zip only
    # ever contains the current run's files.
    for file in out_dir.iterdir():
        if file.is_file():
            file.unlink()
    patterns.to_csv(out_dir / "discovered_programmatic_patterns.csv", index=False)
    memberships.to_csv(out_dir / "pattern_keyword_memberships.csv", index=False)
    queue.to_csv(out_dir / "keyword_volume_check_queue.csv", index=False)
    opportunities.to_csv(out_dir / "programmatic_opportunities.csv", index=False)
    hub_plan.to_csv(out_dir / "content_hub_plan.csv", index=False)
    write_ai_prompt(patterns, out_dir / "ai_pattern_review_prompt.md")
    write_article_templates_md(hub_plan, patterns, out_dir / "article_templates_and_linking.md")
    write_html_report(patterns, queue, opportunities, hub_plan, out_dir / "seo_hub_finder_report.html")
    with zipfile.ZipFile(out_dir / "seo_hub_finder_outputs.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        for file in out_dir.iterdir():
            if file.name != "seo_hub_finder_outputs.zip" and file.is_file():
                zf.write(file, arcname=file.name)


def run_pipeline(
    gsc_csv: Path,
    volume_csv: Optional[Path] = None,
    out_dir: Path = Path("out"),
    top_position: float = 10,
    expanded_position: float = 20,
    min_gsc_impressions: float = 20,
    min_pattern_queries: int = 3,
    min_distinct_slot_values: int = 3,
    min_template_confidence: float = 0.45,
    min_volume: float = 10,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
    queue = build_volume_queue(memberships)
    volume = normalize_volume(volume_csv) if volume_csv else pd.DataFrame(columns=["query", "search_volume", "competition", "cpc"])
    opportunities = merge_volume(memberships, volume, min_volume)
    hub_plan = build_hub_plan(opportunities, patterns)
    write_outputs(patterns, memberships, queue, opportunities, hub_plan, out_dir)
    return patterns, memberships, queue, opportunities, hub_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Find programmatic SEO opportunities from GSC data.")
    parser.add_argument("gsc_csv", help="Google Search Console CSV export")
    parser.add_argument("--volume-csv", default=None, help="Optional Keyword Planner / search-volume CSV")
    parser.add_argument("--out-dir", default="out", help="Output directory")
    parser.add_argument("--top-position", type=float, default=10)
    parser.add_argument("--expanded-position", type=float, default=20)
    parser.add_argument("--min-gsc-impressions", type=float, default=20)
    parser.add_argument("--min-pattern-queries", type=int, default=3)
    parser.add_argument("--min-distinct-slot-values", type=int, default=3)
    parser.add_argument("--min-template-confidence", type=float, default=0.45)
    parser.add_argument("--min-volume", type=float, default=10)
    args = parser.parse_args()

    gsc_path = Path(args.gsc_csv)
    if not gsc_path.exists():
        print(f"Error: GSC CSV not found: {gsc_path}")
        raise SystemExit(1)

    try:
        patterns, _, queue, opportunities, hub_plan = run_pipeline(
            gsc_csv=gsc_path,
            volume_csv=Path(args.volume_csv) if args.volume_csv else None,
            out_dir=Path(args.out_dir),
            top_position=args.top_position,
            expanded_position=args.expanded_position,
            min_gsc_impressions=args.min_gsc_impressions,
            min_pattern_queries=args.min_pattern_queries,
            min_distinct_slot_values=args.min_distinct_slot_values,
            min_template_confidence=args.min_template_confidence,
            min_volume=args.min_volume,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

    print("\nSEO Hub Finder finished.")
    print(f"Discovered patterns: {len(patterns)}")
    print(f"Volume-check keywords: {len(queue)}")
    confirmed = 0 if opportunities.empty else int((opportunities["final_status"] == "confirmed_opportunity").sum())
    print(f"Confirmed keyword opportunities: {confirmed}")
    print(f"Content hubs: {len(hub_plan)}")
    for note in data_quality_notes(patterns):
        print(f"Note: {note}")
    print(f"Outputs written to: {Path(args.out_dir).resolve()}")


if __name__ == "__main__":
    main()
