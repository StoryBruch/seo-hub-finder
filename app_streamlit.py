"""
Striking Distance Finder — Streamlit demo app.

Upload a Google Search Console export (or use the built-in demo data) and get a
ranked list of striking-distance keywords: queries already ranking just below
the top, with real impression volume and the biggest estimated click upside.
Every row comes with a plain-language, data-grounded reason — no API key needed.

An optional AI deep-dive (free Gemini tier) can enrich the top rows when a
GEMINI_API_KEY is configured; the tool works fully without it.
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
            brand_terms: tuple, exclude_brand: bool,
            value_per_click):
    """Parse + analyze. Cached so slider changes don't re-read the file."""
    df = sdf.clean_gsc(sdf.read_gsc_csv(io.BytesIO(file_bytes)))
    candidates, baseline, fallback_used = sdf.find_striking_distance(
        df, pos_min=pos_min, pos_max=pos_max, min_impressions=min_impressions,
        underperformance_threshold=underperformance,
        brand_terms=list(brand_terms), exclude_brand=exclude_brand,
        value_per_click=value_per_click)
    by_page = sdf.group_by_page(candidates)
    return df, candidates, by_page, baseline, fallback_used


def display_frame(candidates: pd.DataFrame, value_per_click) -> pd.DataFrame:
    disp = pd.DataFrame()
    disp["Keyword"] = candidates["query"]
    disp["Seite"] = candidates["page"]
    disp["Position"] = candidates["position"].round(1)
    disp["Impressionen"] = candidates["impressions"].astype(int)
    disp["Klicks"] = candidates["clicks"].astype(int)
    disp["CTR %"] = (candidates["ctr"] * 100).round(2)
    disp["Ø-CTR Position %"] = (candidates["expected_ctr"] * 100).round(2)
    disp["Klick-Potenzial/Monat"] = candidates["opportunity_score"].astype(int)
    if value_per_click and "est_revenue_upside" in candidates:
        disp["Umsatz-Potenzial €"] = candidates["est_revenue_upside"].round(2)
    disp["Begründung"] = candidates["reasoning"]
    return disp


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
         "die Erwartungswerte.")
exclude_brand = st.sidebar.checkbox("Marken-Keywords auch aus der Liste ausschließen")
use_revenue = st.sidebar.checkbox("Umsatz-Hebel berechnen")
value_per_click = None
if use_revenue:
    value_per_click = st.sidebar.number_input("Wert pro Klick (€)", 0.0, 1000.0,
                                              2.50, step=0.50)

brand_terms = tuple(sdf.parse_brand_terms(brand_input))


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
    df, candidates, by_page, baseline, fallback_used = analyze(
        file_bytes, pos_min, pos_max, min_impressions, underperformance,
        brand_terms, exclude_brand, value_per_click)
except sdf.GscFormatError as exc:
    st.error(str(exc))
    st.stop()
except Exception as exc:  # keep the demo from ever showing a raw traceback
    st.error("Die Datei konnte nicht analysiert werden. Bitte prüfe, ob es ein "
             f"gültiger GSC-Export ist.\n\nDetails: {exc}")
    st.stop()

# Summary metrics
total_upside = int(candidates["opportunity_score"].sum()) if not candidates.empty else 0
col1, col2, col3 = st.columns(3)
col1.metric("Zeilen im Export", f"{len(df):,}".replace(",", "."))
col2.metric("Striking-Distance-Keywords", f"{len(candidates):,}".replace(",", "."))
if use_revenue and value_per_click and not candidates.empty:
    col3.metric("Umsatz-Potenzial/Monat",
                f"{candidates['est_revenue_upside'].sum():,.0f} €".replace(",", "."))
else:
    col3.metric("Klick-Potenzial/Monat", f"{total_upside:,}".replace(",", "."))

fallback_buckets = [b for b, used in fallback_used.items() if used]
if fallback_buckets:
    st.caption("ℹ️ Zu wenig eigene Daten in Positions-Bucket(s) "
               + ", ".join(fallback_buckets)
               + " — dort wird ein Richtwert statt deiner eigenen CTR verwendet.")

if candidates.empty:
    st.warning("Keine Keywords im Striking-Distance-Bereich gefunden. Versuch einen "
               "größeren Positions-Bereich oder eine niedrigere Impressions-Schwelle.")
    st.stop()

tab_list, tab_pages, tab_ai = st.tabs(
    ["📋 Keyword-Liste", "🗂️ Nach Seite gruppiert", "🤖 KI-Deep-Dive (optional)"])

with tab_list:
    disp = display_frame(candidates, value_per_click)
    st.dataframe(
        disp, width="stretch", hide_index=True,
        column_config={
            "CTR %": st.column_config.NumberColumn(format="%.2f %%"),
            "Ø-CTR Position %": st.column_config.NumberColumn(format="%.2f %%"),
            "Seite": st.column_config.TextColumn(width="medium"),
            "Begründung": st.column_config.TextColumn(width="large"),
        })
    st.download_button(
        "⬇️ Liste als CSV herunterladen",
        disp.to_csv(index=False).encode("utf-8-sig"),
        file_name="striking_distance_keywords.csv", mime="text/csv")

with tab_pages:
    st.caption("Mehrere Chancen auf derselben URL — diese Seiten zuerst überarbeiten "
               "hebt gleich mehrere Keywords.")
    show = by_page.rename(columns={
        "page": "Seite", "n_keywords": "Keywords",
        "total_upside": "Klick-Potenzial/Monat", "avg_position": "Ø-Position",
        "top_keywords": "Top-Keywords"})
    st.dataframe(show, width="stretch", hide_index=True,
                 column_config={"Top-Keywords": st.column_config.TextColumn(width="large")})

with tab_ai:
    api_key = get_api_key()
    st.caption("Die Begründung in der Liste ist bereits vollständig und "
               "kostenlos. Der KI-Deep-Dive ergänzt für die Top-Keywords eine "
               "tiefere Diagnose + konkreten nächsten Schritt.")
    if not api_key:
        st.info(
            "**Kein API-Key hinterlegt — der Deep-Dive ist optional.**\n\n"
            "So aktivierst du ihn kostenlos:\n"
            "1. Auf [aistudio.google.com/apikey](https://aistudio.google.com/apikey) "
            "mit einem Google-Konto anmelden → **Create API key** (keine Kreditkarte).\n"
            "2. In Streamlit Cloud: App → **Settings → Secrets** → Zeile einfügen: "
            "`GEMINI_API_KEY = \"dein-key\"`\n"
            "3. Lokal: Umgebungsvariable `GEMINI_API_KEY` setzen.")
    else:
        top_n = st.number_input("Wie viele Top-Keywords analysieren?", 1, 25, 10)
        context_raw = st.text_area(
            "Optional: Title & Meta der Seiten einfügen — dann liefert die KI "
            "konkrete Title-Rewrites statt Hypothesen. Eine Zeile pro Seite: "
            "`URL-Fragment | Titel | Meta-Description`", height=100,
            placeholder="/kaffeevollautomat-test | Kaffeevollautomat Test 2026 | "
                        "Die 7 besten Modelle im Vergleich …")

        if st.button("🤖 Ausgewählte Keywords mit KI analysieren"):
            top_rows = candidates.head(int(top_n)).reset_index(drop=True)
            page_context = {}
            for line in context_raw.splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2 and parts[0]:
                    frag, text = parts[0], " | ".join(parts[1:])
                    for page in top_rows["page"]:
                        if frag in str(page):
                            page_context[page] = text
            rows = top_rows.to_dict("records")
            with st.spinner("KI analysiert die Top-Keywords …"):
                results, status = sdf.gemini_deep_dive(rows, api_key,
                                                       page_context=page_context)
            st.session_state["deep_dive"] = {"results": results, "status": status,
                                             "rows": rows}

        state = st.session_state.get("deep_dive")
        if state:
            status = state["status"]
            if status == "ok":
                for i, r in enumerate(state["rows"]):
                    res = state["results"].get(i)
                    with st.expander(f"{r['query']}  ·  Platz {float(r['position']):.1f}  "
                                     f"·  +{int(r['click_upside'])} Klicks"):
                        if res:
                            st.markdown(f"**Diagnose:** {res['diagnosis']}")
                            st.markdown(f"**Nächster Schritt:** {res['action']}")
                        else:
                            st.markdown(str(r.get("reasoning", "")))
            elif status == "http_auth":
                st.error("Der API-Key wurde abgelehnt (401/403). Bitte in den "
                         "Secrets prüfen.")
            else:
                st.warning("Die KI-Analyse ist gerade nicht verfügbar "
                           f"(Status: {status}). Die deterministische Begründung "
                           "in der Liste bleibt davon unberührt.")

st.divider()
st.caption("Striking Distance Finder · GSC-basiert · CTR-Baseline aus deinen "
           "eigenen Daten · kostenlos, keine Installation nötig.")
