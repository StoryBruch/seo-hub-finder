"""
Striking Distance Finder — Streamlit demo app.

Upload a Google Search Console export (or use the built-in demo data) and get a
ranked list of striking-distance keywords: queries already ranking just below
the top, with real impression volume and the biggest estimated click upside.
Every row comes with a plain-language, data-grounded reason — no API key needed.

Optionally the current meta title of each page is scraped and checked against
its keyword (fuzzy), and — with a free Gemini key — an improved 52–59 character
title is proposed. The tool works fully without any key.
"""
import io
import os

import pandas as pd
import streamlit as st

import striking_distance_finder as sdf

st.set_page_config(page_title="Striking Distance Finder", page_icon="🎯",
                   layout="wide")

SAMPLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "sample_gsc.csv")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def get_api_key() -> str:
    """Read GEMINI_API_KEY from Streamlit secrets, then the environment."""
    try:
        value = st.secrets.get("GEMINI_API_KEY", "")
        if value:
            return str(value).strip()
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY", "").strip()


@st.cache_data(show_spinner=False)
def analyze(file_bytes: bytes, pos_min: float, pos_max: float,
            min_impressions: float, underperformance: float,
            manual_brand_terms: tuple, value_per_click):
    """Parse + analyze. Cached so slider changes don't re-read the file.

    Brand terms are the manually entered ones *plus* the ones auto-detected from
    the page domains. `is_brand` is always computed; the actual list exclusion /
    re-inclusion happens in the UI layer so it stays interactive.
    """
    df = sdf.clean_gsc(sdf.read_gsc_csv(io.BytesIO(file_bytes)))
    detected = sdf.detect_brand_terms(df["page"])
    brand_terms = list(dict.fromkeys([*manual_brand_terms, *detected]))
    candidates, baseline, fallback_used = sdf.find_striking_distance(
        df, pos_min=pos_min, pos_max=pos_max, min_impressions=min_impressions,
        underperformance_threshold=underperformance,
        brand_terms=brand_terms, exclude_brand=False,
        value_per_click=value_per_click)
    return df, candidates, baseline, fallback_used, detected, brand_terms


def display_frame(candidates: pd.DataFrame, value_per_click, meta=None) -> pd.DataFrame:
    disp = pd.DataFrame()
    disp["Keyword"] = candidates["query"].values
    disp["Seite"] = candidates["page"].values
    disp["Position"] = candidates["position"].round(1).values
    disp["Impressionen"] = candidates["impressions"].astype(int).values
    disp["Klicks"] = candidates["clicks"].astype(int).values
    disp["CTR %"] = (candidates["ctr"] * 100).round(2).values
    disp["Ø-CTR Position %"] = (candidates["expected_ctr"] * 100).round(2).values
    disp["Klick-Potenzial/Monat"] = candidates["opportunity_score"].astype(int).values
    if value_per_click and "est_revenue_upside" in candidates:
        disp["Umsatz-Potenzial €"] = candidates["est_revenue_upside"].round(2).values
    disp["Begründung"] = candidates["reasoning"].values

    if meta and meta.get("titles") is not None:
        titles = meta["titles"]
        statuses = meta.get("statuses", {})
        kw_sug = meta.get("kw_suggestions", {})
        current, contained, new = [], [], []
        for page, query in zip(candidates["page"], candidates["query"]):
            title = titles.get(page)
            if title:
                current.append(title)
                contained.append("Ja" if sdf.keyword_in_title(query, title) else "Nein")
            else:
                # No title -> show the reason (placeholder domain, blocked, …).
                current.append(sdf.title_status_label(statuses.get(page))
                               if page in statuses else "")
                contained.append("?")
            sug = kw_sug.get((page, query))
            new.append(sug[0] if sug else "")
        disp["Keyword enthalten"] = contained
        disp["Meta Title aktuell"] = current
        disp["Meta Title neu (Vorschlag)"] = new
    return disp


def parse_fallback_titles(raw: str, pages) -> dict:
    """`URL-Fragment | Meta Title` lines -> {page: title} for matching pages."""
    out = {}
    for line in (raw or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2 and parts[0]:
            frag, text = parts[0], " | ".join(parts[1:]).strip()
            if not text:
                continue
            for page in pages:
                if frag in str(page):
                    out[page] = text
    return out


def run_meta_analysis(visible: pd.DataFrame, top_n: int, fallback_raw: str,
                      api_key: str, brand: str):
    """Scrape titles, check keyword containment, and (with a key) suggest titles.

    Results land in st.session_state["meta"] and drive the new table columns.
    """
    pages = [p for p in visible["page"].unique().tolist()
             if p and p != "(keine URL)"]
    with st.spinner(f"Rufe Meta-Titles von {len(pages)} Seiten ab …"):
        scraped = sdf.fetch_meta_titles(pages)
    titles = {url: title for url, (title, _status) in scraped.items()}
    statuses = {url: status for url, (_title, status) in scraped.items()}
    # Manually pasted titles win over (and repair) failed scrapes.
    for page, text in parse_fallback_titles(fallback_raw, pages).items():
        titles[page] = text
        statuses[page] = "ok"

    n_ok = sum(1 for t in titles.values() if t)
    n_placeholder = sum(1 for s in statuses.values() if s == "placeholder")
    summary = f"{n_ok}/{len(pages)} Titel erfolgreich abgerufen."
    if n_ok < len(pages):
        summary += " Fehlende Titel stehen mit Grund in der Spalte »Meta Title aktuell«."
    if n_placeholder:
        summary += (f" Hinweis: {n_placeholder} Demo-/Platzhalter-URL(s) (.example) "
                    "sind technisch nicht abrufbar — mit echten GSC-Daten klappt der "
                    "Abruf.")

    kw_suggestions, page_suggestions = {}, {}
    if api_key:
        top_rows = visible.head(int(top_n))
        total = max(1, len(top_rows))
        prog = st.progress(0.0, text="Erzeuge Titel-Vorschläge …")
        for i, (page, query) in enumerate(
                zip(top_rows["page"], top_rows["query"]), 1):
            current = titles.get(page) or ""
            title, status = sdf.gemini_meta_title(query, current, api_key, brand=brand)
            kw_suggestions[(page, query)] = (title, status)
            prog.progress(i / total, text=f"Titel-Vorschläge … ({i}/{total})")
        prog.empty()

        # Per-page suggestion that tries to cover several keywords of one URL.
        top_pages = (visible.groupby("page")["opportunity_score"].sum()
                     .sort_values(ascending=False).head(int(top_n)).index.tolist())
        with st.spinner("Erzeuge seitenweise Multi-Keyword-Titel …"):
            for page in top_pages:
                if page == "(keine URL)":
                    continue
                rows = visible[visible["page"] == page].sort_values(
                    "opportunity_score", ascending=False)
                kws = rows["query"].tolist()[:4]
                current = titles.get(page) or ""
                title, status = sdf.gemini_meta_title(kws, current, api_key, brand=brand)
                covered = sum(1 for k in kws if sdf.keyword_in_title(k, title))
                page_suggestions[page] = {"title": title, "covered": covered,
                                          "n": len(kws), "status": status}

    st.session_state["meta"] = {"titles": titles, "statuses": statuses,
                                "kw_suggestions": kw_suggestions,
                                "page_suggestions": page_suggestions,
                                "had_key": bool(api_key), "summary": summary}


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

st.sidebar.title("🎯 Striking Distance Finder")
st.sidebar.caption("Findet Keywords, die knapp vor den Top-Platzierungen stehen "
                   "— mit den wichtigsten Zahlen und einer Begründung je Zeile.")

source = st.sidebar.radio("Datenquelle", ["Demo-Daten", "GSC-CSV hochladen"])

uploaded = None
if source == "GSC-CSV hochladen":
    uploaded = st.sidebar.file_uploader(
        "GSC-Export (CSV)", type=["csv", "tsv", "txt"],
        help="Search Console → Leistung → Suchergebnisse → Dimensionen "
             "»Suchanfragen« und »Seiten« → Exportieren → CSV.")

st.sidebar.subheader("Filter")
pos_min, pos_max = st.sidebar.slider("Positions-Bereich", 1.0, 50.0, (4.0, 20.0),
                                     step=0.5)
min_impressions = st.sidebar.number_input("Mindest-Impressionen/Monat", 0, 5000, 30,
                                          step=10)
underperformance = st.sidebar.slider(
    "Unterperformance-Schwelle", 0.5, 1.0, 0.8, step=0.05,
    help="Ein Keyword gilt als CTR-Unterperformer, wenn seine CTR unter diesem "
         "Anteil deiner Baseline-CTR für die Position liegt.")
brand_input = st.sidebar.text_input(
    "Marken-Begriffe (kommagetrennt)",
    help="Werden aus der CTR-Baseline herausgerechnet — Brand-CTR verzerrt sonst "
         "die Erwartungswerte. Die Marke aus deiner Domain wird automatisch "
         "erkannt und ergänzt.")
exclude_brand = st.sidebar.checkbox("Marken-Keywords auch aus der Liste ausschließen")
use_revenue = st.sidebar.checkbox("Umsatz-Hebel berechnen")
value_per_click = None
if use_revenue:
    value_per_click = st.sidebar.number_input("Wert pro Klick (€)", 0.0, 1000.0,
                                              2.50, step=0.50)

manual_brand_terms = tuple(sdf.parse_brand_terms(brand_input))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

st.title("Striking Distance Keywords")

if source == "GSC-CSV hochladen" and uploaded is None:
    st.info("Lade links deinen GSC-CSV-Export hoch — oder wähle **Demo-Daten**, "
            "um das Tool sofort ohne eigene Datei auszuprobieren.")
    st.stop()

try:
    if source == "Demo-Daten":
        with open(SAMPLE_PATH, "rb") as fh:
            file_bytes = fh.read()
    else:
        file_bytes = uploaded.getvalue()
except OSError as exc:
    st.error(f"Datei konnte nicht gelesen werden: {exc}")
    st.stop()

try:
    df, candidates, baseline, fallback_used, detected, brand_terms = analyze(
        file_bytes, pos_min, pos_max, min_impressions, underperformance,
        manual_brand_terms, value_per_click)
except sdf.GscFormatError as exc:
    st.error(str(exc))
    st.stop()
except Exception as exc:  # keep the demo from ever showing a raw traceback
    st.error("Die Datei konnte nicht analysiert werden. Bitte prüfe, ob es ein "
             f"gültiger GSC-Export ist.\n\nDetails: {exc}")
    st.stop()

if candidates.empty:
    st.warning("Keine Keywords im Striking-Distance-Bereich gefunden. Versuch einen "
               "größeren Positions-Bereich oder eine niedrigere Impressions-Schwelle.")
    st.stop()

# --- Brand review: auto-detected brand keywords, exclusion + re-inclusion ----
brand_queries = sorted(candidates.loc[candidates["is_brand"], "query"].unique().tolist())
detected_str = ", ".join(detected) if detected else "—"
reinclude = []
if exclude_brand:
    if brand_queries:
        st.caption(f"🏷️ **{len(brand_queries)} Marken-Keyword(s)** erkannt (Marke aus "
                   f"Domain: {detected_str}). Sie werden aus der Liste ausgeschlossen "
                   "— unten kannst du einzelne wieder aufnehmen.")
        reinclude = st.multiselect(
            "Fälschlich als Marke erkannt? Diese Keywords wieder aufnehmen:",
            options=brand_queries, default=[],
            help="Standardmäßig gelten alle erkannten Marken-Keywords als Marke. "
                 "Hier ausgewählte Keywords bleiben in der Liste.")
    else:
        st.caption("🏷️ **Keine Marken-Keywords zum Ausschließen gefunden.** Erkannte "
                   f"Marke aus deiner Domain: **{detected_str}** — kommt aber in keiner "
                   "Suchanfrage vor. Trage links unter »Marken-Begriffe« deinen "
                   "Markennamen ein (z. B. genau so, wie Nutzer nach dir suchen), um "
                   "Marken-Suchanfragen auszuschließen.")
elif brand_queries:
    st.caption(f"🏷️ **{len(brand_queries)} Marken-Keyword(s)** erkannt (Marke: "
               f"{detected_str}). Aktiviere links »Marken-Keywords auch aus der Liste "
               "ausschließen«, um sie auszublenden.")

if exclude_brand and brand_terms:
    drop_mask = candidates["is_brand"] & ~candidates["query"].isin(reinclude)
    visible = candidates[~drop_mask].reset_index(drop=True)
else:
    visible = candidates

if visible.empty:
    st.warning("Alle gefundenen Keywords wurden als Marken-Keywords ausgeschlossen. "
               "Nimm oben einzelne wieder auf oder deaktiviere den Marken-Ausschluss.")
    st.stop()

# --- Summary metrics ---------------------------------------------------------
total_upside = int(visible["opportunity_score"].sum())
col1, col2, col3 = st.columns(3)
col1.metric("Zeilen im Export", f"{len(df):,}".replace(",", "."))
col2.metric("Striking-Distance-Keywords", f"{len(visible):,}".replace(",", "."))
if use_revenue and value_per_click and "est_revenue_upside" in visible:
    col3.metric("Umsatz-Potenzial/Monat",
                f"{visible['est_revenue_upside'].sum():,.0f} €".replace(",", "."))
else:
    col3.metric("Klick-Potenzial/Monat", f"{total_upside:,}".replace(",", "."))

fallback_buckets = [b for b, used in fallback_used.items() if used]
if fallback_buckets:
    st.caption("ℹ️ Zu wenig eigene Daten in Positions-Bucket(s) "
               + ", ".join(fallback_buckets)
               + " — dort wird ein Richtwert statt deiner eigenen CTR verwendet.")

# --- Meta-title panel (scrape + keyword check + optional AI suggestions) ------
brand_primary = detected[0] if detected else ""
api_key = get_api_key()
with st.expander("✍️ Meta-Titles prüfen & optimieren", expanded=False):
    st.caption("Ruft den aktuellen `<title>` jeder Seite ab (kostenlos, kein Key), "
               "prüft ob das Keyword enthalten ist (auch bei Singular/Plural, "
               "Füllwörtern oder anderer Reihenfolge) und schlägt — mit Gemini-Key "
               f"— einen neuen Title mit {sdf.TITLE_MIN}–{sdf.TITLE_MAX} Zeichen vor.")
    top_n = st.number_input("Für wie viele Top-Keywords/Seiten Titel-Vorschläge?",
                            1, 50, 10)
    fallback_raw = st.text_area(
        "Optional: Titles manuell einfügen (für Seiten, die den Abruf blockieren). "
        "Eine Zeile pro Seite: `URL-Fragment | Meta Title`", height=90,
        placeholder="/kaffeevollautomat-test | Kaffeevollautomat Test 2026: die 7 besten Modelle")
    if not api_key:
        st.info("Kein Gemini-Key hinterlegt: **Abruf + Keyword-Check funktionieren "
                "trotzdem.** Für Titel-Vorschläge einen kostenlosen Key auf "
                "[aistudio.google.com/apikey](https://aistudio.google.com/apikey) "
                "erstellen und als `GEMINI_API_KEY` (Umgebungsvariable) bzw. "
                "Streamlit-Secret hinterlegen.")
    if st.button("🔎 Meta-Titles abrufen & prüfen"):
        run_meta_analysis(visible, int(top_n), fallback_raw, api_key, brand_primary)

meta = st.session_state.get("meta")
if meta and meta.get("summary"):
    st.caption("📄 " + meta["summary"])
if meta and not meta.get("had_key"):
    st.caption("Titel-Vorschläge sind leer, weil kein Gemini-Key hinterlegt ist — "
               "Keyword-Check und aktueller Title sind trotzdem gefüllt.")

# --- Tabs --------------------------------------------------------------------
tab_list, tab_pages = st.tabs(["📋 Keyword-Liste", "🗂️ Nach Seite gruppiert"])

with tab_list:
    disp = display_frame(visible, value_per_click, meta=meta)
    column_config = {
        "CTR %": st.column_config.NumberColumn(format="%.2f %%"),
        "Ø-CTR Position %": st.column_config.NumberColumn(format="%.2f %%"),
        "Seite": st.column_config.TextColumn(width="medium"),
        "Begründung": st.column_config.TextColumn(width="large"),
    }
    if "Meta Title neu (Vorschlag)" in disp.columns:
        column_config["Meta Title aktuell"] = st.column_config.TextColumn(width="large")
        column_config["Meta Title neu (Vorschlag)"] = st.column_config.TextColumn(width="large")
    st.dataframe(disp, width="stretch", hide_index=True, column_config=column_config)
    st.download_button(
        "⬇️ Liste als CSV herunterladen",
        disp.to_csv(index=False).encode("utf-8-sig"),
        file_name="striking_distance_keywords.csv", mime="text/csv")

with tab_pages:
    st.caption("Mehrere Chancen auf derselben URL — diese Seiten zuerst überarbeiten "
               "hebt gleich mehrere Keywords. Der Titel-Vorschlag versucht, mehrere "
               "Striking-Distance-Keywords einer Seite abzudecken.")
    by_page = sdf.group_by_page(visible)
    show = by_page.rename(columns={
        "page": "Seite", "n_keywords": "Keywords",
        "total_upside": "Klick-Potenzial/Monat", "avg_position": "Ø-Position",
        "top_keywords": "Top-Keywords"})
    page_cfg = {"Top-Keywords": st.column_config.TextColumn(width="large")}
    if meta and meta.get("page_suggestions"):
        ps = meta["page_suggestions"]
        show["Title-Vorschlag (mehrere KW)"] = by_page["page"].map(
            lambda p: ps.get(p, {}).get("title", ""))
        show["Abgedeckte KW"] = by_page["page"].map(
            lambda p: f"{ps[p]['covered']}/{ps[p]['n']}" if p in ps else "")
        page_cfg["Title-Vorschlag (mehrere KW)"] = st.column_config.TextColumn(width="large")
    st.dataframe(show, width="stretch", hide_index=True, column_config=page_cfg)

st.divider()
st.caption("Striking Distance Finder · GSC-basiert · CTR-Baseline aus deinen "
           "eigenen Daten · kostenlos, keine Installation nötig.")
