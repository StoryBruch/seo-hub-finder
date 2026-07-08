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

TABLE_HEIGHT = 460  # fester Rahmen -> vertikaler Scrollbalken ist immer aktiv

# Schutzgrenzen, damit die App bei großen Datensätzen / dem geteilten Gemini-
# Free-Tier stabil bleibt.
MAX_KEYWORDS = 2000        # so viele Striking-Distance-Keywords werden angezeigt
MAX_META_PER_RUN = 12      # so viele Meta-Titles pro »Optimieren«-Klick (RPM-Limit)

# Scrollbalken der Tabellen dauerhaft anzeigen (vertikal + horizontal). Das Grid
# (glide-data-grid) scrollt über `.dvn-scroller`; overflow:scroll erzwingt die
# Balken auch dann, wenn Inhalt gerade passt.
st.markdown(
    """
    <style>
    .dvn-scroller { overflow: scroll !important; }
    .dvn-scroller::-webkit-scrollbar { width: 14px; height: 14px; -webkit-appearance: none; }
    .dvn-scroller::-webkit-scrollbar-thumb {
        background: rgba(128,128,128,.65); border-radius: 7px;
        border: 3px solid transparent; background-clip: content-box; }
    .dvn-scroller::-webkit-scrollbar-track { background: rgba(128,128,128,.18); }
    /* CSV-Download-Button blau mit weißem Text/Icon */
    [data-testid="stDownloadButton"] button {
        background-color: #1f6feb !important; border-color: #1f6feb !important; }
    [data-testid="stDownloadButton"] button,
    [data-testid="stDownloadButton"] button * { color: #ffffff !important; }
    /* Datenquelle-Auswahl prominenter */
    [data-testid="stSidebar"] div[role="radiogroup"] label p { font-size: 1.05rem; font-weight: 600; }
    /* START-Button groß & auffällig */
    [data-testid="stSidebar"] .stButton button {
        padding: 0.7rem 1rem; font-size: 1.25rem; font-weight: 800; }
    </style>
    """,
    unsafe_allow_html=True,
)


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


SELECT_COL = "Meta-Title optimieren"
META_COLS = ["Keyword enthalten", "Meta Title aktuell", "Meta Title neu (Vorschlag)"]


def display_frame(candidates: pd.DataFrame, show_cpc, meta=None):
    """Build the keyword table for st.data_editor. Returns (df, gray_columns).

    Column order: [checkbox] Keyword | Seite | <3 Meta-Spalten nach Optimierung>
    | Zahlen … . The 3 meta columns are only added once results exist and are
    returned as `gray_columns` so the caller can shade them. `show_cpc` adds an
    (empty) CPC column — the value needs a paid API and stays blank.
    """
    disp = pd.DataFrame()
    disp[SELECT_COL] = [False] * len(candidates)
    disp["Keyword"] = candidates["query"].values
    disp["Seite"] = candidates["page"].values

    gray_cols = []
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
        gray_cols = list(META_COLS)

    disp["Position"] = candidates["position"].round(1).values
    disp["Impressionen"] = candidates["impressions"].astype(int).values
    disp["Klicks"] = candidates["clicks"].astype(int).values
    disp["CTR %"] = (candidates["ctr"] * 100).round(2).values
    disp["Ø-CTR Position %"] = (candidates["expected_ctr"] * 100).round(2).values
    disp["Klick-Potenzial/28 Tage"] = candidates["opportunity_score"].astype(int).values
    if show_cpc:
        disp["CPC"] = ["" for _ in range(len(candidates))]  # braucht kostenpfl. API
    disp["Begründung"] = candidates["reasoning"].values
    return disp, gray_cols


def page_frame(by_page: pd.DataFrame, meta=None):
    """Build the grouped-by-page table for st.data_editor. Returns (df, gray)."""
    show = pd.DataFrame()
    show[SELECT_COL] = [False] * len(by_page)
    show["Seite"] = by_page["page"].values

    gray_cols = []
    if meta and meta.get("page_suggestions"):
        ps = meta["page_suggestions"]
        show["Title-Vorschlag (mehrere KW)"] = [ps.get(p, {}).get("title", "")
                                                for p in by_page["page"]]
        show["Abgedeckte KW"] = [f"{ps[p]['covered']}/{ps[p]['n']}" if p in ps else ""
                                 for p in by_page["page"]]
        gray_cols = ["Title-Vorschlag (mehrere KW)", "Abgedeckte KW"]

    show["Keywords"] = by_page["n_keywords"].values
    show["Klick-Potenzial/28 Tage"] = by_page["total_upside"].values
    show["Ø-Position"] = by_page["avg_position"].values
    show["Top-Keywords"] = by_page["top_keywords"].values
    return show, gray_cols


def selected_indices(editor_key: str) -> list:
    """Row positions the user checked in a data_editor (from its widget state)."""
    state = st.session_state.get(editor_key)
    if not isinstance(state, dict):
        return []
    edited = state.get("edited_rows", {})
    return sorted(int(i) for i, ch in edited.items() if ch.get(SELECT_COL))


GRAY_BG = "background-color: #eceef3"
GREEN_BG = "background-color: #dff2d8"  # sanftes Grün für vielversprechende Zeilen


def _style_frame(frame: pd.DataFrame, gray_cols, promising):
    """Styles df: gray meta columns; soft-green rows flagged as promising.

    Green wins over gray on a promising row. Only non-editable columns are
    styled (Streamlit ignores styles on the editable checkbox column anyway).
    """
    styles = pd.DataFrame("", index=frame.index, columns=frame.columns)
    for col in gray_cols:
        styles[col] = GRAY_BG
    if promising is not None and len(promising):
        prom = pd.Series(list(promising), index=frame.index).fillna(False)
        for col in frame.columns:
            if col != SELECT_COL:
                styles.loc[prom, col] = GREEN_BG
    return styles


def render_selectable_table(frame: pd.DataFrame, gray_cols, key, extra_cfg=None,
                            promising=None):
    """Render a data_editor whose only editable column is the select checkbox."""
    cfg = {
        SELECT_COL: st.column_config.CheckboxColumn(
            SELECT_COL, default=False,
            help="Auswählen → oben auf »Meta-Titles der Auswahl prüfen & "
                 "optimieren« klicken."),
        "Seite": st.column_config.TextColumn(width="medium"),
    }
    if extra_cfg:
        cfg.update(extra_cfg)
    for col in gray_cols:  # give the meta columns room to read
        cfg.setdefault(col, st.column_config.TextColumn(width="large"))
    disabled = [c for c in frame.columns if c != SELECT_COL]
    has_green = promising is not None and bool(pd.Series(list(promising)).any()) \
        if promising is not None else False
    data = frame
    if gray_cols or has_green:  # styling applies to disabled columns (Streamlit rule)
        styles = _style_frame(frame, gray_cols, promising)
        data = frame.style.apply(lambda _df: styles, axis=None)
    return st.data_editor(data, key=key, hide_index=True, width="stretch",
                          height=TABLE_HEIGHT, column_config=cfg, disabled=disabled)


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


def run_meta_analysis(selected_kw: pd.DataFrame, selected_pages: list,
                      all_rows: pd.DataFrame, fallback_raw: str, api_key: str,
                      brand: str):
    """Scrape + check + (with a key) optimize titles for the SELECTED rows/pages.

    Results accumulate in st.session_state["meta"] across clicks, so earlier
    optimizations stay filled while you add more selections.
    """
    prev = st.session_state.get("meta") or {}
    titles = dict(prev.get("titles") or {})
    statuses = dict(prev.get("statuses") or {})
    kw_suggestions = dict(prev.get("kw_suggestions") or {})
    page_suggestions = dict(prev.get("page_suggestions") or {})

    pages = [p for p in dict.fromkeys(list(selected_kw["page"]) + list(selected_pages))
             if p and p != "(keine URL)"]
    if pages:
        with st.spinner(f"Rufe Meta-Titles von {len(pages)} Seite(n) ab …"):
            scraped = sdf.fetch_meta_titles(pages)
        for url, (title, status) in scraped.items():
            titles[url], statuses[url] = title, status
    # Manually pasted titles win over (and repair) failed scrapes.
    for page, text in parse_fallback_titles(fallback_raw, pages).items():
        titles[page], statuses[page] = text, "ok"

    n_ok = sum(1 for p in pages if titles.get(p))
    n_placeholder = sum(1 for p in pages if statuses.get(p) == "placeholder")
    if pages:
        summary = f"{n_ok}/{len(pages)} Titel der Auswahl abgerufen."
        if n_ok < len(pages):
            summary += " Fehlende stehen mit Grund in »Meta Title aktuell«."
        if n_placeholder:
            summary += (f" {n_placeholder} Demo-/Platzhalter-URL(s) (.example) sind "
                        "nicht abrufbar — mit echten GSC-Daten klappt der Abruf.")
    else:
        summary = "Keine Seiten in der Auswahl."

    if api_key and len(selected_kw):
        total = len(selected_kw)
        prog = st.progress(0.0, text="Erzeuge Titel-Vorschläge …")
        for i, (page, query) in enumerate(
                zip(selected_kw["page"], selected_kw["query"]), 1):
            current = titles.get(page) or ""
            title, _status = sdf.gemini_meta_title(query, current, api_key, brand=brand)
            kw_suggestions[(page, query)] = (title, _status)
            prog.progress(i / total, text=f"Titel-Vorschläge … ({i}/{total})")
        prog.empty()

    if api_key and selected_pages:
        with st.spinner("Erzeuge seitenweise Multi-Keyword-Titel …"):
            for page in selected_pages:
                if not page or page == "(keine URL)":
                    continue
                rows = all_rows[all_rows["page"] == page].sort_values(
                    "opportunity_score", ascending=False)
                kws = rows["query"].tolist()[:4]
                if not kws:
                    continue
                current = titles.get(page) or ""
                title, status = sdf.gemini_meta_title(kws, current, api_key, brand=brand)
                covered = sum(1 for k in kws if sdf.keyword_in_title(k, title))
                page_suggestions[page] = {"title": title, "covered": covered,
                                          "n": len(kws), "status": status}

    st.session_state["meta"] = {"titles": titles, "statuses": statuses,
                                "kw_suggestions": kw_suggestions,
                                "page_suggestions": page_suggestions,
                                "had_key": bool(api_key), "summary": summary}


def spread_promising_order(df, prom):
    """Zeilen-Reihenfolge für die Anzeige: nach Seite gruppiert (Keywords einer
    URL bleiben zusammen), aber die URLs mit einem grünen Keyword werden
    gleichmäßig zwischen die übrigen verteilt — so stehen nie mehrere grüne
    Zeilen dicht beieinander. Gibt eine Liste der df-Indizes zurück."""
    green_groups, plain_groups = [], []
    for _page, sub in df.groupby("page", sort=True):
        idx = list(sub.sort_values("query").index)
        (green_groups if bool(prom.loc[idx].any()) else plain_groups).append(idx)
    if not green_groups:
        return [i for g in plain_groups for i in g]
    ratio = len(plain_groups) / len(green_groups)
    order, pi = [], 0
    for gi in range(len(green_groups)):
        take = round((gi + 1) * ratio) - round(gi * ratio)  # ~gleichmäßig verteilen
        for g in plain_groups[pi:pi + take]:
            order += g
        pi += take
        order += green_groups[gi]
    for g in plain_groups[pi:]:
        order += g
    return order


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #

OPT_DEMO = "Demo-Daten"
OPT_UPLOAD = "Eigene GSC-CSV hochladen"

st.sidebar.title("Datensatz & Filter")

st.sidebar.markdown("### 📁 Datenquelle")
source = st.sidebar.radio("Datenquelle", [OPT_DEMO, OPT_UPLOAD],
                          label_visibility="collapsed")

# Beim Wechsel der Datenquelle wieder auf den leeren Startzustand zurück.
if st.session_state.get("_last_source") != source:
    st.session_state["_last_source"] = source
    st.session_state["started"] = False

uploaded = None
if source == OPT_UPLOAD:
    uploaded = st.sidebar.file_uploader(
        "GSC-Export (CSV)", type=["csv", "tsv", "txt"],
        help="Search Console → Leistung → Suchergebnisse → Dimensionen "
             "»Suchanfragen« und »Seiten« → Exportieren → CSV.")

if st.sidebar.button("🚀 START", type="primary", width="stretch"):
    st.session_state["started"] = True

st.sidebar.subheader("Filter")
pos_min, pos_max = st.sidebar.slider("Positions-Bereich", 1.0, 50.0, (4.0, 20.0),
                                     step=0.5)
min_impressions = st.sidebar.number_input("Mindest-Impressionen/28 Tage", 0, 5000, 30,
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
if use_revenue:
    st.sidebar.caption("ℹ️ Berechnung nur mit kostenpflichtiger API möglich.")
value_per_click = None  # kein Umsatz-Wert -> die CPC-Spalte bleibt leer

manual_brand_terms = tuple(sdf.parse_brand_terms(brand_input))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

@st.dialog("So funktioniert's – Erklärung & Dokumentation", width="large")
def show_help():
    st.markdown(
        """
        ### In einem Satz
        Das Tool zeigt dir Suchbegriffe, bei denen du bei Google **knapp vor Seite 1
        / den Top-Plätzen** stehst — also dort, wo sich Arbeit am meisten lohnt.

        ### So legst du los
        1. Links **Demo-Daten** wählen *oder* deine **eigene GSC-CSV hochladen**.
        2. Auf **🚀 START** drücken – erst dann wird ausgewertet und die Tabelle erscheint.

        **Muss ich vorher die Filter setzen?** Nein – egal ob **vor oder nach** dem Start.
        Nach dem Start aktualisiert sich die Tabelle bei **jeder Filter-Änderung
        automatisch**; du musst *nicht* erneut auf START drücken. Nur wenn du die
        **Datenquelle wechselst** (Demo ↔ eigene CSV), fängst du wieder mit START an.

        ### TL;DR – die wichtigsten Hebel
        - **Grün hinterlegte Zeilen = die vielversprechendsten Keywords.** Über die
          Spalte **»Klick-Potenzial/28 Tage«** sortieren (Klick auf den Spaltenkopf),
          um sie oben zu sehen.
        - **Meta-Titles prüfen & optimieren:** Häkchen in der Spalte
          **»Meta-Title optimieren«** setzen → unten links auf den roten Button
          klicken. Dann erscheinen (grau) nach der URL: *Keyword enthalten*,
          *Meta Title aktuell*, *Meta Title neu (Vorschlag)*.
        - **Nach Seite gruppiert** (2. Tab): URLs mit mehreren Chancen; der
          Titel-Vorschlag deckt dort mehrere Keywords einer Seite ab.
        - **⬇️ Liste als CSV** (unten rechts) exportiert die Tabelle.

        ---
        ### Woher kommen die Daten? (Google Search Console)
        Das Tool braucht deinen **GSC-Leistungsbericht** als CSV:
        1. In der [Search Console](https://search.google.com/search-console) →
           **Leistung → Suchergebnisse**.
        2. Oben die Dimensionen **»Suchanfragen«** *und* **»Seiten«** aktivieren.
        3. Rechts oben **Exportieren → CSV** (bzw. Google Tabellen).
        4. Diese Datei links unter **»Eigene GSC-CSV hochladen«** einlesen — oder
           erstmal **»Demo-Daten«** wählen und alles ausprobieren.

        ---
        ### Die Filter links – einfach erklärt (mit Beispiel)

        **Positions-Bereich (Standard 4–20)**
        Google notiert für jedes Keyword deine **Durchschnittsposition** (1 = ganz
        oben). Der Regler legt fest, welcher Bereich dich interessiert.
        *Warum 4–20?* Platz **1–3** hast du praktisch schon gewonnen (wird ignoriert),
        Platz **über 20** ist meist zu weit weg. Dazwischen liegt die „Schlagdistanz".
        *Beispiel:* Keyword auf **Platz 6** = Seite 1 unten → mit etwas Arbeit realistisch
        auf Top-3 zu heben.

        **Mindest-Impressionen/28 Tage (Standard 30)**
        „Impressionen" = wie oft dein Eintrag in der Suche **angezeigt** wurde. Dieser
        Filter wirft Keywords raus, die kaum jemand sucht (kein Potenzial).
        *Beispiel:* Wert **30** = nur Keywords mit **≥ 30** Einblendungen/28 Tage.
        Höher stellen = nur „dicke Fische".

        **Unterperformance-Schwelle (Standard 0,8)**
        Das Tool rechnet aus **deinen eigenen Daten** aus, welche Klickrate (CTR) für
        eine Position **normal** ist. Die Schwelle sagt: **ab wann ist deine CTR
        auffällig zu niedrig?**
        *Beispiel:* Für Platz 6 sind bei dir z. B. ~3 % CTR normal. Bei Schwelle **0,8**
        giltst du als **Unterperformer**, wenn du unter **80 % davon** liegst (< 2,4 %).
        Hast du nur 1,2 %, ist das ein klarer Fall → oft reicht schon ein **besserer
        Titel/Snippet**, um Klicks zu holen (ganz ohne besseres Ranking).
        Niedrigere Schwelle = strenger (nur krasse Fälle), höhere = lockerer.

        **Marken-Begriffe / »… aus der Liste ausschließen«**
        Deine **Marke** (z. B. dein Domain-/Firmenname) wird automatisch aus der Domain
        erkannt. Marken-Suchen (Leute, die dich eh kennen) verzerren die Auswertung –
        sie werden aus der CTR-Berechnung herausgenommen und lassen sich per Häkchen
        auch aus der Liste ausblenden (einzelne kann man wieder aufnehmen). Eigene
        Begriffe (weitere Marken, Tippfehler-Varianten) kannst du im Feld ergänzen.

        ---
        ### Die wichtigsten Spalten
        - **Position** – dein durchschnittliches Ranking (eine Nachkommastelle).
        - **Impressionen / Klicks / CTR %** – Anzeigen, Klicks, Klickrate.
        - **Ø-CTR Position %** – die *normale* CTR für diese Position (aus deinen Daten).
          Liegt deine CTR klar darunter → Hebel = besserer Title/Snippet.
        - **Klick-Potenzial/28 Tage** – geschätzte **zusätzliche Klicks**, wenn das Keyword
          auf Top-3 steigt. **Danach sortieren = größte Chancen zuerst.**
        - **Begründung** – ein Satz, warum das Keyword eine Chance ist.

        ### Meta-Title-Optimierung
        Der aktuelle Seitentitel wird **kostenlos** abgerufen und geprüft, ob dein
        Keyword darin vorkommt (auch bei Singular/Plural, Füllwörtern oder anderer
        Reihenfolge). Mit hinterlegtem (kostenlosem) **Gemini-Key** kommt zusätzlich ein
        neuer Titel-Vorschlag mit garantiert **52–59 Zeichen**. Ohne Key laufen Abruf +
        Keyword-Check trotzdem.

        > Tipp: nach **Klick-Potenzial** sortieren → grüne Zeilen ansehen → die
        > spannendsten anhaken → Meta-Titles optimieren.
        """
    )


col_help, _spacer, col_cv = st.columns([2, 2, 1.4])
with col_help:
    if st.button("❓ So funktioniert's – Erklärung & Dokumentation",
                 width="stretch", type="primary"):
        show_help()
with col_cv:
    st.markdown(
        '<div style="text-align:right;">'
        '<a href="app/static/lebenslauf.html" target="_blank" rel="noopener" '
        'style="display:inline-block;background:#6f3d2d;color:#ffffff;'
        'text-decoration:none;padding:0.55rem 1.15rem;border-radius:0.5rem;'
        'font-weight:700;white-space:nowrap;">📄 Mein Lebenslauf</a></div>',
        unsafe_allow_html=True)

st.title("Striking Distance Finder")
st.caption("Findet Keywords, die knapp vor den Top-Platzierungen stehen — mit den "
           "wichtigsten Zahlen und einer Begründung je Zeile.")

_max_kw = f"{MAX_KEYWORDS:,}".replace(",", ".")
st.info(
    f"- Bitte maximal Datensätze mit **{_max_kw}** Keywords hochladen "
    "(oder **Demo-Daten** verwenden).\n"
    f"- Maximal **{MAX_META_PER_RUN}** Meta-Titles auf einmal optimieren lassen.\n"
    "- Bitte nur GSC-Exporte mit Zeitraum **letzte 28 Tage** hochladen "
    "(darauf sind die Filter abgestimmt).\n"
    "- **Loslegen:** Demo-Daten oder eigene GSC-CSV wählen und auf **🚀 START** "
    "drücken. Die **Filter** kannst du vor oder nach dem Start ändern — die Tabelle "
    "aktualisiert sich automatisch (nur beim Wechsel der Datenquelle erneut START)."
)

if not st.session_state.get("started"):
    st.info("👈 Wähle links deine **Datenquelle** (Demo-Daten oder eigener "
            "GSC-Export) und klick dann auf **🚀 START**.")
    st.stop()

if source == OPT_UPLOAD and uploaded is None:
    st.info("Lade links deinen GSC-CSV-Export hoch und klick anschließend erneut "
            "auf **🚀 START** — oder wähle **Demo-Daten**, um es ohne eigene Datei "
            "auszuprobieren.")
    st.stop()

try:
    if source == OPT_DEMO:
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

if exclude_brand and brand_terms:
    drop_mask = candidates["is_brand"] & ~candidates["query"].isin(reinclude)
    visible = candidates[~drop_mask].reset_index(drop=True)
else:
    visible = candidates

if visible.empty:
    st.warning("Alle gefundenen Keywords wurden als Marken-Keywords ausgeschlossen. "
               "Nimm oben einzelne wieder auf oder deaktiviere den Marken-Ausschluss.")
    st.stop()

# Schutzgrenze: bei sehr großen Exporten nur die Top-Keywords nach Klick-Potenzial
# anzeigen, damit die Tabelle flott und die App stabil bleibt.
if len(visible) > MAX_KEYWORDS:
    visible = visible.nlargest(MAX_KEYWORDS, "opportunity_score")
    st.warning(f"Sehr großer Datensatz — es werden die **{MAX_KEYWORDS:,}** Keywords "
               "mit dem höchsten Klick-Potenzial angezeigt.".replace(",", "."))

# Standard-Reihenfolge: nach Seite gruppiert, aber grüne (vielversprechende)
# Zeilen gleichmäßig verteilt, damit kein grüner Block entsteht. Per Spaltenkopf
# (z. B. Klick-Potenzial) lässt sich jederzeit umsortieren.
visible = visible.reset_index(drop=True)
_prom = sdf.promising_mask(visible)
visible = visible.loc[spread_promising_order(visible, _prom)].reset_index(drop=True)

# --- Summary metrics ---------------------------------------------------------
total_upside = int(visible["opportunity_score"].sum())
col1, col2, col3 = st.columns(3)
col1.metric("Zeilen im Export", f"{len(df):,}".replace(",", "."))
col2.metric("Striking-Distance-Keywords", f"{len(visible):,}".replace(",", "."))
col3.metric("Klick-Potenzial/28 Tage", f"{total_upside:,}".replace(",", "."))

brand_primary = detected[0] if detected else ""
api_key = get_api_key()
by_page = sdf.group_by_page(visible)

# Especially promising / profitable rows (green highlight).
kw_promising = sdf.promising_mask(visible)
if not by_page.empty and by_page["total_upside"].max() > 0:
    page_thr = by_page["total_upside"].quantile(0.85)  # Top 15 %
    page_promising = (by_page["total_upside"] >= page_thr) & (by_page["total_upside"] > 0)
else:
    page_promising = pd.Series(False, index=by_page.index)

meta = st.session_state.get("meta")
n_promising = int(kw_promising.sum())
if n_promising:
    st.caption(f"🟢 **{n_promising} besonders vielversprechende Keyword(s)** sanft "
               "grün hervorgehoben — die **Top 15 %** nach Klick-Potenzial. Nach "
               "»Klick-Potenzial/28 Tage« sortieren zeigt sie oben.")
if meta and meta.get("summary"):
    st.caption("📄 " + meta["summary"])
if st.session_state.get("meta_notice"):
    st.warning(st.session_state["meta_notice"])


def handle_meta_click():
    """Read the checked rows/pages, run the analysis, then refresh the tables."""
    kw_idx = selected_indices("kw_editor")
    pg_idx = selected_indices("page_editor")
    if not kw_idx and not pg_idx:
        st.warning("Bitte zuerst in einer Tabelle Zeilen über die Spalte "
                   "»Meta-Title optimieren« anhaken.")
        return
    # Obergrenze pro Durchgang: Keywords zuerst, dann Seiten auffüllen, damit das
    # geteilte Gemini-Minutenlimit nicht überläuft und nichts leer zurückkommt.
    total = len(kw_idx) + len(pg_idx)
    notice = ""
    if total > MAX_META_PER_RUN:
        kw_idx = kw_idx[:MAX_META_PER_RUN]
        pg_idx = pg_idx[:max(0, MAX_META_PER_RUN - len(kw_idx))]
        notice = (f"Max. {MAX_META_PER_RUN} Meta-Titles pro Durchgang (du hattest "
                  f"{total} gewählt). Die ersten {len(kw_idx) + len(pg_idx)} wurden "
                  "verarbeitet — den Rest einfach in einem zweiten Durchgang anhaken.")
    st.session_state["meta_notice"] = notice  # überlebt das st.rerun() unten
    selected_kw = visible.iloc[kw_idx] if kw_idx else visible.iloc[0:0]
    selected_pages = [by_page.iloc[i]["page"] for i in pg_idx if i < len(by_page)]
    run_meta_analysis(selected_kw, selected_pages, visible, "", api_key,
                      brand_primary)
    st.rerun()  # re-render tables with the fresh results


# --- Tabs --------------------------------------------------------------------
tab_list, tab_pages = st.tabs(["📋 Keyword-Liste", "🗂️ Nach Seite gruppiert"])

with tab_list:
    disp, gray = display_frame(visible, use_revenue, meta=meta)
    extra = {
        "Position": st.column_config.NumberColumn(format="%.1f"),
        "CTR %": st.column_config.NumberColumn(format="%.2f %%"),
        "Ø-CTR Position %": st.column_config.NumberColumn(format="%.2f %%"),
        "Begründung": st.column_config.TextColumn(width="large"),
        # Die 3 Meta-Spalten schmaler halten (voller Text per Hover / Klick sichtbar).
        "Keyword enthalten": st.column_config.TextColumn(width="small"),
        "Meta Title aktuell": st.column_config.TextColumn(width="medium"),
        "Meta Title neu (Vorschlag)": st.column_config.TextColumn(width="medium"),
    }
    if "CPC" in disp.columns:
        extra["CPC"] = st.column_config.TextColumn(
            "CPC", help="Berechnung nur mit kostenpflichtiger API möglich.")
    render_selectable_table(disp, gray, key="kw_editor", extra_cfg=extra,
                            promising=kw_promising.tolist())

    left, right = st.columns([3, 1])
    with left:
        meta_clicked = st.button(
            "🔎 Meta-Titles der Auswahl prüfen & optimieren", type="primary")
        note = ("Zeilen oben über »Meta-Title optimieren« anhaken, dann klicken — "
                f"Titel werden abgerufen, geprüft und (mit Gemini-Key) auf "
                f"{sdf.TITLE_MIN}–{sdf.TITLE_MAX} Zeichen optimiert. Max. "
                f"{MAX_META_PER_RUN} pro Durchgang.")
        if not api_key:
            note += (" Ohne Gemini-Key laufen Abruf + Keyword-Check trotzdem; für "
                     "Vorschläge einen kostenlosen Key als `GEMINI_API_KEY` "
                     "hinterlegen.")
        st.caption(note)
    with right:
        st.download_button(
            "Liste als CSV", icon=":material/download:", width="stretch",
            data=disp.drop(columns=[SELECT_COL]).to_csv(index=False).encode("utf-8-sig"),
            file_name="striking_distance_keywords.csv", mime="text/csv")

    if meta_clicked:
        handle_meta_click()

with tab_pages:
    st.caption("Mehrere Chancen auf derselben URL — diese Seiten zuerst überarbeiten "
               "hebt gleich mehrere Keywords. Der Titel-Vorschlag versucht, mehrere "
               "Striking-Distance-Keywords einer Seite abzudecken.")
    page_disp, page_gray = page_frame(by_page, meta=meta)
    render_selectable_table(
        page_disp, page_gray, key="page_editor",
        extra_cfg={"Top-Keywords": st.column_config.TextColumn(width="large")},
        promising=page_promising.tolist())

st.divider()
st.caption("Striking Distance Finder · GSC-basiert · CTR-Baseline aus deinen "
           "eigenen Daten · kostenlos, keine Installation nötig.")
