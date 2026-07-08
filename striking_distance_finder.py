"""
Striking Distance Finder — GSC striking-distance keyword analysis.

Reads a Google Search Console performance export (Query + Page dimensions) and
surfaces "striking distance" keywords: queries already ranking just below the
top positions, with real impression volume, ranked by estimated click upside.

The reasoning for every keyword is generated *deterministically* from the
numbers — using a CTR baseline calibrated from the site's own data per position
bucket. No API key, no network, no cost. Optionally, the current meta title of
each page can be scraped and checked against its keyword (fuzzy, tolerant of
singular/plural, punctuation and filler words), and an LLM (free Gemini tier)
can propose an improved 52–59 character title when a key is available; the core
analysis works fully without any of that.

CLI usage:
    python striking_distance_finder.py sample_gsc.csv
    python striking_distance_finder.py sample_gsc.csv --pos-min 4 --pos-max 20 \
        --min-impressions 30 --value-per-click 2.50 --out opportunities.csv
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

# --------------------------------------------------------------------------- #
# Column handling
# --------------------------------------------------------------------------- #

# Canonical column name -> set of accepted aliases (normalized: lowercase, no dots).
COLUMN_ALIASES = {
    "query": {"query", "queries", "top queries", "search query", "suchanfrage",
              "suchanfragen", "suchbegriff", "keyword", "keywords"},
    "page": {"page", "pages", "top pages", "landing page", "url", "seite",
             "seiten", "address", "adresse"},
    "clicks": {"clicks", "click", "klicks", "klick"},
    "impressions": {"impressions", "impression", "impr", "impressionen"},
    "ctr": {"ctr", "click through rate", "clickthrough rate", "klickrate"},
    "position": {"position", "pos", "avg position", "average position",
                 "durchschnittliche position", "durchschnittspos",
                 "position avg", "durchschn position"},
}

# Columns we truly cannot work without (page is optional — used only for grouping).
CORE_COLUMNS = ("query", "clicks", "impressions", "position")

_NA_TOKENS = {"", "nan", "na", "n/a", "none", "null", "-", "–", "—"}


class GscFormatError(ValueError):
    """Raised when an uploaded file is not a usable GSC export."""


def _normalize_key(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip().lower().replace(".", "")).strip()


def _match_columns(columns) -> dict:
    """Map the file's column names onto our canonical names."""
    reverse = {}
    for canon, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            reverse[_normalize_key(alias)] = canon
    mapping = {}
    for col in columns:
        canon = reverse.get(_normalize_key(col))
        if canon and canon not in mapping.values():
            mapping[col] = canon
    return mapping


# --------------------------------------------------------------------------- #
# Robust number parsing (handles German "7,3" / "1.234" and English "1,234.5")
# --------------------------------------------------------------------------- #

def parse_number(value) -> float:
    """Parse a numeric cell, tolerant of thousands separators and comma decimals."""
    if value is None:
        return float("nan")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("%", "").replace(" ", "").replace(" ", "")
    if s.lower() in _NA_TOKENS:
        return float("nan")
    negative = s.startswith("-")
    if negative:
        s = s[1:]
    # Strip separators that clearly group 3 digits (thousands): "1.234" / "1,234".
    s = re.sub(r"(?<=\d)[.,](?=\d{3}(?:\D|$))", "", s)
    # Any remaining comma is a decimal comma.
    s = s.replace(",", ".")
    if s.count(".") > 1:  # leftover grouping dots -> keep only the last as decimal
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + "." + tail
    try:
        result = float(s)
    except ValueError:
        return float("nan")
    return -result if negative else result


# --------------------------------------------------------------------------- #
# Reading & cleaning
# --------------------------------------------------------------------------- #

def _load_bytes(source) -> bytes:
    if hasattr(source, "read"):
        try:
            pos = source.tell()
        except Exception:
            pos = None
        data = source.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            if pos is not None:
                source.seek(pos)
        except Exception:
            pass
        return data
    with open(source, "rb") as fh:
        return fh.read()


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_gsc_csv(source) -> pd.DataFrame:
    """Read a GSC CSV/TSV export from a path or file-like object, robustly."""
    text = _decode(_load_bytes(source))
    attempts = (
        {"sep": None, "engine": "python"},
        {"sep": ","},
        {"sep": ";"},
        {"sep": "\t"},
    )
    best = None
    for kwargs in attempts:
        try:
            df = pd.read_csv(io.StringIO(text), dtype=str,
                             keep_default_na=False, **kwargs)
        except Exception:
            continue
        if df.shape[1] < 2:
            continue
        present = set(_match_columns(df.columns).values())
        if set(CORE_COLUMNS).issubset(present):
            return df
        if best is None or len(present) > best[1]:
            best = (df, len(present))
    if best is not None:
        return best[0]
    raise GscFormatError(
        "Die Datei konnte nicht als CSV gelesen werden. Erwartet wird ein "
        "Google-Search-Console-Export (CSV)."
    )


def clean_gsc(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize columns and types; raise GscFormatError on missing core columns."""
    mapping = _match_columns(df.columns)
    renamed = df.rename(columns=mapping)
    present = set(mapping.values())

    missing = [c for c in CORE_COLUMNS if c not in present]
    if missing:
        pretty = {"query": "Suchanfrage (query)", "clicks": "Klicks (clicks)",
                  "impressions": "Impressionen (impressions)",
                  "position": "Position (position)"}
        raise GscFormatError(
            "Der Export enthält nicht alle nötigen Spalten. Es fehlt: "
            + ", ".join(pretty[c] for c in missing)
            + ".\n\nErwartet wird ein GSC-Leistungsbericht mit den Spalten "
            "Suchanfrage, Seite, Klicks, Impressionen und Position. "
            "So exportierst du ihn: Search Console → Leistung → Suchergebnisse "
            "→ oben die Dimensionen »Suchanfragen« und »Seiten« aktivieren → "
            "»Exportieren« → CSV."
        )

    out = pd.DataFrame()
    out["query"] = renamed["query"].astype(str).str.strip()
    out["page"] = (renamed["page"].astype(str).str.strip()
                   if "page" in present else "(keine URL)")
    out["clicks"] = renamed["clicks"].map(parse_number)
    out["impressions"] = renamed["impressions"].map(parse_number)
    out["position"] = renamed["position"].map(parse_number)

    # CTR is recomputed from clicks/impressions (most reliable); the imported CTR
    # column, if present, only fills gaps where impressions are missing.
    out["ctr"] = out["clicks"] / out["impressions"].where(out["impressions"] > 0)
    if "ctr" in present:
        imported = renamed["ctr"].map(parse_number)
        imported = imported.where(imported <= 1, imported / 100.0)  # "2.15" -> 0.0215
        out["ctr"] = out["ctr"].fillna(imported)

    out = out[out["query"].astype(str).str.strip().astype(bool)]
    out["clicks"] = out["clicks"].fillna(0)
    out = out[out["impressions"].fillna(0) > 0]
    out["ctr"] = out["ctr"].fillna(0.0).clip(lower=0.0, upper=1.0)
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# CTR baseline (calibrated from the site's own data, per position bucket)
# --------------------------------------------------------------------------- #

# (low_inclusive, high_exclusive, label) — contiguous ranges that cover the
# fractional average positions GSC reports (e.g. 8.1 belongs to the "6–8" bucket).
BUCKETS = [
    (1, 2, "1"), (2, 3, "2"), (3, 4, "3"), (4, 6, "4–5"),
    (6, 9, "6–8"), (9, 12, "9–11"), (12, 16, "12–15"),
    (16, 21, "16–20"),
]

# Only used when a bucket has too little of the site's own data to trust.
FALLBACK_CTR = {
    "1": 0.28, "2": 0.15, "3": 0.10, "4–5": 0.06, "6–8": 0.03,
    "9–11": 0.02, "12–15": 0.01, "16–20": 0.005,
}

DEFAULT_MIN_SAMPLES = 5


def assign_bucket(position) -> str | None:
    if position is None or pd.isna(position):
        return None
    if position < 1:
        return "1"
    for low, high, label in BUCKETS:
        if low <= position < high:
            return label
    return None  # positions >= 21 are not part of the striking-distance range


def _brand_pattern(terms) -> str:
    escaped = [re.escape(t.strip()) for t in (terms or []) if t and str(t).strip()]
    if not escaped:
        return r"(?!x)x"  # matches nothing
    return r"(?<!\w)(?:" + "|".join(escaped) + r")(?!\w)"


# Multi-part public suffixes we skip past to reach the real brand label.
_MULTI_TLDS = {"co.uk", "com.au", "co.nz", "co.jp", "com.br", "co.za",
               "com.tr", "co.in", "com.mx", "or.at", "co.at"}
# Generic labels that are never a brand even if they land in the brand slot.
_NON_BRAND_LABELS = {"com", "net", "org", "www", "co"}


def detect_brand_terms(pages) -> list:
    """Derive likely brand token(s) from the domains in the page column.

    e.g. https://www.cloudwards.net/foo -> "cloudwards". Returns the tokens
    ordered by how often they appear (most common domain first). Rows without a
    usable URL are ignored, so an export without the Page dimension yields [].
    """
    counts = {}
    for page in ([] if pages is None else pages):
        s = str(page).strip()
        if not s or s == "(keine URL)":
            continue
        try:
            parsed = urllib.parse.urlparse(s if "//" in s else "//" + s)
            host = (parsed.hostname or "").lower()
        except ValueError:
            continue
        if host.startswith("www."):
            host = host[4:]
        labels = [l for l in host.split(".") if l]
        if len(labels) < 2:
            continue
        if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_TLDS:
            brand = labels[-3]
        else:
            brand = labels[-2]
        if brand and brand not in _NON_BRAND_LABELS:
            counts[brand] = counts.get(brand, 0) + 1
    return [b for b, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)]


def calculate_baseline(df: pd.DataFrame, brand_terms=(),
                       min_samples: int = DEFAULT_MIN_SAMPLES):
    """Return (baseline_ctr_by_bucket, fallback_used_by_bucket)."""
    work = df[df["position"].notna()].copy()
    if brand_terms:
        mask = work["query"].str.contains(_brand_pattern(brand_terms),
                                          case=False, regex=True, na=False)
        work = work[~mask]
    work["bucket"] = work["position"].map(assign_bucket)

    baseline, fallback_used = {}, {}
    for _, _, label in BUCKETS:
        grp = work[work["bucket"] == label]
        impressions = grp["impressions"].sum()
        if len(grp) >= min_samples and impressions > 0:
            baseline[label] = float(grp["clicks"].sum() / impressions)
            fallback_used[label] = False
        else:
            baseline[label] = FALLBACK_CTR[label]
            fallback_used[label] = True
    return baseline, fallback_used


def _top3_reference_ctr(baseline: dict) -> float:
    """Conservative 'if it reached the top 3' CTR = position-3 baseline."""
    return baseline.get("3", FALLBACK_CTR["3"])


# --------------------------------------------------------------------------- #
# Core analysis
# --------------------------------------------------------------------------- #

def build_reasoning(row) -> str:
    position = row["position"]
    impressions = int(round(row["impressions"]))
    clicks = int(round(row["clicks"]))
    ctr_pct = row["ctr"] * 100
    expected_pct = row["expected_ctr"] * 100
    upside = int(row["click_upside"])

    impr_str = f"{impressions:,}".replace(",", ".")
    parts = [f"Platz {position:.1f} · {impr_str} Impressionen/28 Tage · "
             f"{clicks} Klicks (CTR {ctr_pct:.1f} %)."]

    if row["is_underperformer"] and row["expected_ctr"] > 0:
        gap = round((1 - row["ctr"] / row["expected_ctr"]) * 100)
        parts.append(f"CTR liegt {gap} % unter deinem Schnitt von "
                     f"{expected_pct:.1f} % für diese Position — "
                     f"Snippet/Title/Suchintention prüfen.")
    else:
        parts.append("CTR ist für die Position normal — der Hebel ist "
                     "ein besseres Ranking.")

    if upside > 0:
        parts.append(f"Auf Top-3 gehoben wären ~{upside} zusätzliche "
                     f"Klicks/28 Tage drin.")
    return " ".join(parts)


def find_striking_distance(df: pd.DataFrame, pos_min: float = 4.0,
                           pos_max: float = 20.0, min_impressions: float = 30,
                           underperformance_threshold: float = 0.8,
                           brand_terms=(), exclude_brand: bool = False,
                           value_per_click: float | None = None,
                           min_samples: int = DEFAULT_MIN_SAMPLES):
    """Return (candidates_df, baseline, fallback_used)."""
    baseline, fallback_used = calculate_baseline(df, brand_terms, min_samples)
    top3_ctr = _top3_reference_ctr(baseline)

    c = df[df["position"].notna()].copy()
    c = c[(c["position"] >= pos_min) & (c["position"] <= pos_max)
          & (c["impressions"] >= min_impressions)]

    if brand_terms:
        c["is_brand"] = c["query"].str.contains(_brand_pattern(brand_terms),
                                                case=False, regex=True, na=False)
    else:
        c["is_brand"] = False
    if exclude_brand and brand_terms:
        c = c[~c["is_brand"]]

    empty_cols = ["query", "page", "position", "impressions", "clicks", "ctr",
                  "bucket", "expected_ctr", "target_ctr", "is_underperformer",
                  "potential_clicks", "click_upside", "opportunity_score",
                  "is_brand", "reasoning"]
    if value_per_click:
        empty_cols.append("est_revenue_upside")
    if c.empty:
        return pd.DataFrame(columns=empty_cols), baseline, fallback_used

    c["bucket"] = c["position"].map(assign_bucket)
    c["expected_ctr"] = c["bucket"].map(baseline).astype(float)
    c["target_ctr"] = c["expected_ctr"].map(lambda e: max(top3_ctr, e))
    c["is_underperformer"] = c["ctr"] < c["expected_ctr"] * underperformance_threshold
    c["potential_clicks"] = (c["impressions"] * c["target_ctr"]).round().astype(int)
    c["click_upside"] = ((c["potential_clicks"] - c["clicks"])
                         .clip(lower=0).round().astype(int))
    c["opportunity_score"] = c["click_upside"]
    if value_per_click:
        c["est_revenue_upside"] = (c["click_upside"] * float(value_per_click)).round(2)

    c["reasoning"] = c.apply(build_reasoning, axis=1)

    sort_col = "est_revenue_upside" if value_per_click else "opportunity_score"
    c = c.sort_values([sort_col, "impressions"], ascending=False).reset_index(drop=True)
    return c, baseline, fallback_used


def group_by_page(candidates: pd.DataFrame) -> pd.DataFrame:
    cols = ["page", "n_keywords", "total_upside", "avg_position", "top_keywords"]
    if candidates.empty:
        return pd.DataFrame(columns=cols)
    grouped = (candidates.groupby("page")
               .agg(n_keywords=("query", "count"),
                    total_upside=("opportunity_score", "sum"),
                    avg_position=("position", "mean"),
                    top_keywords=("query", lambda x: ", ".join(list(x)[:5])))
               .reset_index()
               .sort_values("total_upside", ascending=False))
    grouped["avg_position"] = grouped["avg_position"].round(1)
    return grouped.reset_index(drop=True)


def parse_brand_terms(text) -> list:
    if not text:
        return []
    return [t.strip() for t in re.split(r"[,;\n]", str(text)) if t.strip()]


def promising_mask(candidates: pd.DataFrame, top_frac: float = 0.2):
    """Boolean Series: which candidates are *especially* promising / profitable.

    Built purely from the metrics the analysis already produces (position →
    expected CTR, impressions, clicks → `opportunity_score`; plus the CTR-
    underperformer flag). A row is flagged when it is either:
      • among the top `top_frac` by click upside (the biggest opportunities), or
      • a CTR underperformer whose upside is in the upper quartile — a quick win
        where a better title/snippet alone should capture clicks at the current
        ranking. Kept to the upper quartile so the highlight stays a meaningful
        minority rather than every underperformer.
    Returns an all-False series when nothing has positive upside.
    """
    if candidates is None or candidates.empty:
        return pd.Series([], dtype=bool)
    score = candidates["opportunity_score"].astype(float)
    if score.max() <= 0:
        return pd.Series(False, index=candidates.index)
    top_threshold = score.quantile(1 - top_frac)
    upper_quartile = score.quantile(0.75)
    mask = (score > 0) & (score >= top_threshold)
    if "is_underperformer" in candidates.columns:
        mask = mask | (candidates["is_underperformer"].fillna(False)
                       & (score >= upper_quartile) & (score > 0))
    return mask


# --------------------------------------------------------------------------- #
# Meta-title scraping (free, standard library only) & fuzzy keyword matching
# --------------------------------------------------------------------------- #

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_REQUEST_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}
# Reserved, non-resolving TLDs (RFC 2606/6761) — e.g. the demo's *.example URLs.
# There is no point trying to fetch these; they can never resolve.
_PLACEHOLDER_TLDS = (".example", ".test", ".invalid", ".localhost")


def fetch_meta_title(url, timeout: int = 10):
    """Fetch a page and extract its <title>. Returns (title|None, status).

    Free, no API: a plain HTTP GET with a browser User-Agent + a regex over the
    first bytes of HTML. Never raises — every failure is reported via `status`
    ("ok", "no_url", "placeholder", "no_title", "http_<code>", "error").
    """
    s = str(url).strip()
    if not s or s == "(keine URL)":
        return None, "no_url"
    if "//" not in s:
        s = "https://" + s
    try:
        host = (urllib.parse.urlparse(s).hostname or "").lower()
    except ValueError:
        host = ""
    if host.endswith(_PLACEHOLDER_TLDS):
        return None, "placeholder"
    request = urllib.request.Request(s, headers=_REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(200_000)  # title lives in <head>; cap the read
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        return None, f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None, "error"
    text = raw.decode(charset, errors="replace")
    match = _TITLE_RE.search(text)
    if not match:
        return None, "no_title"
    title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
    return (title, "ok") if title else (None, "no_title")


def fetch_meta_titles(urls, max_workers: int = 8, timeout: int = 10) -> dict:
    """Scrape titles for a list of URLs in parallel. Returns {url: (title, status)}.

    De-duplicates URLs; the same URL is never fetched twice. The per-URL status
    lets the caller explain *why* a title is missing (placeholder domain,
    blocked, timeout, …) instead of a bare "not available".
    """
    unique = [u for u in dict.fromkeys(str(u) for u in (urls or []))]
    results = {}
    if not unique:
        return results
    workers = max(1, min(max_workers, len(unique)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_url = {pool.submit(fetch_meta_title, u, timeout): u
                         for u in unique}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                title, status = future.result()
            except Exception:
                title, status = None, "error"
            results[url] = (title, status)
    return results


def title_status_label(status) -> str:
    """Human-readable German reason for a missing title (empty string if ok)."""
    if not status or status == "ok":
        return ""
    labels = {
        "no_url": "(keine URL)",
        "placeholder": "(Demo-/Platzhalter-URL – nicht abrufbar)",
        "no_title": "(kein <title> auf der Seite gefunden)",
        "error": "(nicht erreichbar – Timeout/DNS/SSL)",
    }
    if status in labels:
        return labels[status]
    if status.startswith("http_"):
        code = status[5:]
        if code in ("401", "403", "429"):
            return f"(Abruf blockiert – HTTP {code})"
        return f"(HTTP-Fehler {code})"
    return "(nicht abrufbar)"


# Umlaut / accent folding so "Kaffeemaschinen" and "kaffeemaschine" compare cleanly.
_FOLD_MAP = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss", "à": "a",
                           "á": "a", "â": "a", "é": "e", "è": "e", "ê": "e",
                           "í": "i", "ì": "i", "ó": "o", "ò": "o", "ú": "u",
                           "ç": "c", "ñ": "n"})

# Filler words that may sit between keyword words in a title (and vice versa).
_GERMAN_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem",
    "einer", "eines", "und", "oder", "im", "in", "am", "an", "auf", "aus", "bei",
    "mit", "nach", "von", "vom", "zu", "zur", "zum", "fur", "als", "auch", "wie",
    "the", "a", "of", "for", "is", "vs", "on", "at", "das", "so", "es",
}


def _fold(text) -> str:
    return str(text).lower().translate(_FOLD_MAP)


def _tokens(text) -> list:
    return [t for t in re.split(r"[^0-9a-z]+", _fold(text)) if t]


def _stem(token: str) -> str:
    """Very light German stemmer: strip a common plural/inflection suffix."""
    for suffix in ("ern", "en", "er", "es", "e", "n", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a or not b:
        return len(a) or len(b)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _token_match(kw: str, title_tok: str) -> bool:
    """One keyword word vs one title word, tolerant of plural/typo variants."""
    if kw == title_tok:
        return True
    ks, ts = _stem(kw), _stem(title_tok)
    if ks == ts:
        return True
    shorter, longer = sorted((kw, title_tok), key=len)
    if len(shorter) >= 4 and longer.startswith(shorter):  # kaffeemaschine⊂kaffeemaschinen
        return True
    if min(len(kw), len(title_tok)) >= 5 and _levenshtein(ks, ts) <= 1:
        return True
    return False


def keyword_in_title(keyword, title) -> bool:
    """Is `keyword` (fuzzily) present in `title`?

    Tolerant by design — order-independent, ignores punctuation, filler words
    and singular/plural differences. Every significant keyword word must have a
    matching word somewhere in the title. Examples that match:
      "iphone test"            ⊂ "iPhone im Test"
      "kaffeemaschine vergleich" ⊂ "Vergleich: die besten Kaffeemaschinen …"
    """
    kw_tokens = _tokens(keyword)
    significant = [t for t in kw_tokens if t not in _GERMAN_STOPWORDS] or kw_tokens
    if not significant:
        return False
    title_tokens = _tokens(title)
    if not title_tokens:
        return False
    return all(any(_token_match(kt, tt) for tt in title_tokens)
               for kt in significant)


# --------------------------------------------------------------------------- #
# Meta-title length enforcement (the hard 52–59 char double-check)
# --------------------------------------------------------------------------- #

TITLE_MIN = 52
TITLE_MAX = 59

# Ordered from long to short so padding overshoots the window as rarely as
# possible; the enforcer stops as soon as it reaches TITLE_MIN.
_TITLE_FILLERS = (" – Test, Vergleich & Empfehlung", " – die besten Modelle im Test",
                  " – Test, Vergleich & Kaufberatung", " – der große Vergleich 2026",
                  " – Ratgeber, Test & Vergleich", " im Test & Vergleich 2026",
                  " – die besten Modelle", " – Test & Vergleich", " im Vergleich 2026",
                  " – Ratgeber 2026", " im Test 2026", " – Vergleich", " im Test", " 2026")


def _trim_words_to(title: str, hi: int) -> str:
    if len(title) <= hi:
        return title
    out = ""
    for word in title.split(" "):
        candidate = (out + " " + word).strip()
        if len(candidate) > hi:
            break
        out = candidate
    return out or title[:hi].rstrip()


def enforce_title_length(title, lo: int = TITLE_MIN, hi: int = TITLE_MAX,
                         filler_terms=None):
    """Guarantee a title length inside [lo, hi]. Returns (title, ok).

    This is the deterministic double-check: LLMs count characters unreliably, so
    every generated title is passed through here. Too long -> trimmed at a word
    boundary; too short -> padded from ranked filler snippets without ever
    exceeding `hi`. `ok` is False only when no assembly fits (e.g. a single word
    already longer than `hi`), which the UI surfaces as "manuell prüfen".
    """
    title = re.sub(r"\s+", " ", str(title)).strip().strip('"').strip("»«").strip()
    if len(title) > hi:
        title = _trim_words_to(title, hi)
    if len(title) < lo:
        fillers = tuple(filler_terms or ()) + _TITLE_FILLERS
        # Prefer a single filler that lands the title inside the window — keeps
        # the result readable instead of chaining several snippets.
        single = next((title + f for f in fillers if lo <= len(title + f) <= hi), None)
        if single is not None:
            title = single
        else:  # no single fit: stack conservatively, never the same snippet twice
            for filler in fillers:
                if len(title) >= lo:
                    break
                if filler not in title and len(title + filler) <= hi:
                    title += filler
    return title, (lo <= len(title) <= hi)


# --------------------------------------------------------------------------- #
# Optional LLM (free Gemini tier) — meta-title suggestions; degrades cleanly
# --------------------------------------------------------------------------- #

DEFAULT_AI_MODEL = "gemini-3.5-flash"
AI_MODEL_FALLBACKS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
_GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/"
                    "models/{model}:generateContent")


def _call_gemini(prompt: str, api_key: str, model: str = DEFAULT_AI_MODEL,
                 timeout: int = 40, temperature: float = 0.4,
                 max_output_tokens: int = 4096, response_schema: dict | None = None,
                 thinking_budget: int | None = None,
                 fallback_models=AI_MODEL_FALLBACKS):
    """Call Gemini generateContent. Returns (text|None, status). Never raises.

    `thinking_budget=0` disables the model's internal "thinking" — essential for
    short tasks like a title, where a thinking model would otherwise spend the
    whole token budget reasoning and return a truncated answer.
    """
    if not api_key:
        return None, "no_api_key"

    generation_config = {"temperature": temperature,
                         "maxOutputTokens": max_output_tokens}
    if response_schema is not None:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = response_schema
    if thinking_budget is not None:
        generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }).encode("utf-8")

    for candidate_model in (model, *fallback_models):
        url = _GEMINI_ENDPOINT.format(model=candidate_model)
        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("x-goog-api-key", api_key)  # key in header, not URL
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            candidates = payload.get("candidates") or []
            if not candidates:
                continue
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            if text.strip():
                return text, "ok"
            continue
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return None, "http_auth"  # bad key — no point trying other models
            continue  # 400/404/429/5xx -> try next model
        except (urllib.error.URLError, ValueError, TimeoutError):
            continue
    return None, "failed"


def gemini_meta_title(keywords, current_title: str = "", api_key: str = "",
                      brand: str = "", model: str = DEFAULT_AI_MODEL):
    """Propose a German meta title of 52–59 characters covering the keyword(s).

    `keywords` is a single keyword string or a list of keywords (the multi-
    keyword form is used for the per-page grouped view, where one title should
    cover as many striking-distance keywords of that URL as possible).
    `current_title` is the scraped title, given to the model as context.

    Returns (title, status). The raw model output is ALWAYS passed through
    `enforce_title_length`, so a returned title is guaranteed to be within the
    window (status "ok") or flagged (status "length_warn") — never silently the
    wrong length. Never raises.
    """
    if isinstance(keywords, str):
        keywords = [keywords]
    keywords = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not keywords:
        return "", "no_keywords"
    if not api_key:
        return "", "no_api_key"

    kw_str = "; ".join(keywords)
    ctx = f"\nAktueller Title (zur Orientierung): {current_title}" if current_title else ""
    coverage = ("Decke MÖGLICHST VIELE der Keywords in einem einzigen Title ab.\n"
                if len(keywords) > 1 else "")
    prompt = (
        "Formuliere genau EINEN deutschen SEO-Meta-Title.\n"
        f"Pflicht-Keyword(s) (dürfen als Singular/Plural erscheinen): {kw_str}.\n"
        f"{coverage}"
        f"Der Title MUSS zwischen {TITLE_MIN} und {TITLE_MAX} Zeichen lang sein "
        "(inklusive Leerzeichen)."
        f"{ctx}\n"
        "Regeln: natürlich und klickstark formulieren, keine Anführungszeichen, "
        "kein Markdown, nur der Title als eine einzige Zeile."
    )

    text, status = _call_gemini(prompt, api_key, model=model, temperature=0.5,
                                max_output_tokens=256, thinking_budget=0)
    if text is None:
        return "", status
    candidate = text.strip().splitlines()[0] if text.strip() else ""
    filler_terms = [f" – {brand}"] if brand else None
    title, ok = enforce_title_length(candidate, filler_terms=filler_terms)
    return title, ("ok" if ok else "length_warn")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

DISPLAY_COLUMNS = ["query", "page", "position", "impressions", "clicks",
                   "ctr", "expected_ctr", "opportunity_score", "reasoning"]


def _build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Find striking-distance keywords in a GSC export.")
    ap.add_argument("csv", help="Path to the GSC CSV export.")
    ap.add_argument("--pos-min", type=float, default=4.0)
    ap.add_argument("--pos-max", type=float, default=20.0)
    ap.add_argument("--min-impressions", type=float, default=30)
    ap.add_argument("--underperformance", type=float, default=0.8,
                    help="Flag when actual CTR < this fraction of baseline CTR.")
    ap.add_argument("--brand-terms", default="",
                    help="Comma-separated brand terms (excluded from baseline).")
    ap.add_argument("--exclude-brand", action="store_true")
    ap.add_argument("--value-per-click", type=float, default=None)
    ap.add_argument("--out", default=None, help="Write full results to this CSV.")
    ap.add_argument("--top", type=int, default=20, help="Rows to print.")
    return ap


def main(argv=None) -> int:
    args = _build_arg_parser().parse_args(argv)
    try:
        df = clean_gsc(read_gsc_csv(args.csv))
    except GscFormatError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2

    candidates, baseline, fallback_used = find_striking_distance(
        df, pos_min=args.pos_min, pos_max=args.pos_max,
        min_impressions=args.min_impressions,
        underperformance_threshold=args.underperformance,
        brand_terms=parse_brand_terms(args.brand_terms),
        exclude_brand=args.exclude_brand, value_per_click=args.value_per_click)

    print(f"Zeilen im Export: {len(df)}")
    print(f"Striking-Distance-Keywords gefunden: {len(candidates)}")
    fb = [b for b, used in fallback_used.items() if used]
    if fb:
        print("Hinweis: zu wenig eigene Daten in Bucket(s) "
              + ", ".join(fb) + " — dort Richtwert-CTR verwendet.")

    if not candidates.empty:
        show = candidates.head(args.top).copy()
        show["ctr"] = (show["ctr"] * 100).round(2).astype(str) + "%"
        show["expected_ctr"] = (show["expected_ctr"] * 100).round(2).astype(str) + "%"
        show["position"] = show["position"].round(1)
        print()
        print(show[DISPLAY_COLUMNS].to_string(index=False))

    if args.out:
        candidates.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\nVollständige Ergebnisse geschrieben: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
