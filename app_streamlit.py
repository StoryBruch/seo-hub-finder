import os
import tempfile
from pathlib import Path

import streamlit as st

from seo_hub_finder import (
    MAX_HERO_IMAGES,
    MAX_TRENDS_CANDIDATES,
    TEMPLATE_COLUMNS,
    TEMPLATE_STATUS_MESSAGES,
    data_quality_notes,
    hub_template_markdown,
    run_pipeline,
)


def get_gemini_api_key():
    try:
        return st.secrets["GEMINI_API_KEY"]
    except (KeyError, FileNotFoundError):
        return os.environ.get("GEMINI_API_KEY")


GEMINI_API_KEY = get_gemini_api_key()

st.set_page_config(page_title="SEO Hub Finder", layout="wide")
st.title("SEO Hub Finder — Programmatic SEO Discovery")
st.caption("GSC export → automatic pattern discovery → search-volume gate → content hub plan")

st.markdown(
    """
This demo finds repeatable programmatic SEO opportunities from Google Search Console data.
It does **not** require you to manually predefine patterns like `X Rezept` or `Marke entkalken`.
Instead, it discovers repeatable query structures from the uploaded queries and validates them with search-volume data.
"""
)

with st.sidebar:
    st.header("Settings")
    top_position = st.number_input("Ranking proof: top position", value=10.0, min_value=1.0, max_value=100.0)
    expanded_position = st.number_input("Expanded position gate", value=20.0, min_value=1.0, max_value=100.0)
    min_gsc_impressions = st.number_input("Minimum GSC impressions", value=20.0, min_value=0.0)
    min_pattern_queries = st.number_input("Minimum queries per pattern", value=3, min_value=2)
    min_distinct_slot_values = st.number_input("Minimum distinct slot values", value=3, min_value=2)
    min_template_confidence = st.slider("Minimum template confidence", 0.0, 1.0, 0.45, 0.05)
    min_volume = st.number_input("Minimum monthly search volume", value=10.0, min_value=0.0)
    use_ai_templates = st.checkbox(
        "AI-Artikel-Templates (Gemini)", value=True,
        help="Erstellt pro Hub ein intent-spezifisches Artikel-Template. Ohne API-Key wird "
             "automatisch ein statisches, intent-basiertes Template verwendet.",
    )
    hero_enabled = st.checkbox(
        "Hero-Image pro Hub generieren", value=True,
        help="Nutzt das kostenlose Gemini-Bildmodell, wenn GEMINI_API_KEY gesetzt ist; sonst "
             "Pollinations.ai als Fallback (ohne Key, Bilder können ein kleines Wasserzeichen tragen). "
             "Verlängert den Lauf um bis zu ein paar Minuten.",
    )
    with st.expander("New-keyword Trends check (advanced)"):
        trends_geo = st.text_input("Google Trends region code", value="DE")
        min_trends_relative = st.slider("Min. candidate/anchor Trends ratio", 0.0, 1.0, 0.1, 0.05)
        max_trends_candidates = st.number_input(
            "Max. candidates checked per run", value=MAX_TRENDS_CANDIDATES, min_value=1, max_value=100
        )
    with st.expander("Hero images (advanced)"):
        max_hero_images = st.number_input(
            "Max. Hero-Images pro Lauf", value=MAX_HERO_IMAGES, min_value=1, max_value=20
        )

mode = st.radio(
    "Choose input mode",
    ["Try demo data", "Upload GSC CSV", "Upload GSC CSV + volume CSV"],
    horizontal=True,
)

PROJECT_DIR = Path(__file__).parent

gsc_file = None
volume_file = None

if mode == "Try demo data":
    st.info("Demo mode uses included sample GSC + sample volume data, so the full workflow is visible immediately.")
    gsc_path_fixed = PROJECT_DIR / "sample_gsc.csv"
    volume_path_fixed = PROJECT_DIR / "sample_volume.csv"
elif mode == "Upload GSC CSV":
    st.info("GSC-only mode discovers patterns and creates `keyword_volume_check_queue.csv`. Final hubs need volume validation.")
    gsc_file = st.file_uploader("Upload Google Search Console CSV", type=["csv"])
    gsc_path_fixed = None
    volume_path_fixed = None
else:
    st.info("Upload GSC data and a keyword-volume export from Google Keyword Planner or another keyword tool.")
    gsc_file = st.file_uploader("Upload Google Search Console CSV", type=["csv"])
    volume_file = st.file_uploader("Upload search-volume CSV", type=["csv"])
    gsc_path_fixed = None
    volume_path_fixed = None

new_keywords_file = None
if GEMINI_API_KEY:
    st.caption(
        "New keyword ideas beyond GSC are suggested automatically (Gemini) and checked against "
        "Google Trends on every run — no extra steps needed."
    )
    with st.expander("Also add your own keyword ideas manually (optional)"):
        new_keywords_file = st.file_uploader(
            "Upload additional candidates CSV (pattern_id, candidate_query)", type=["csv"], key="new_keywords_upload"
        )
else:
    with st.expander("Optional: found new keyword ideas? Check them here"):
        st.write(
            "No `GEMINI_API_KEY` configured, so this step isn't automatic here. Copy the generated "
            "prompt below into any free LLM chat (ChatGPT, Gemini, Claude, ...), then upload its CSV "
            "reply and run again. Candidates get checked against Google Trends before they're added "
            "to a hub — nothing gets added on invented demand."
        )
        if st.session_state.get("new_keyword_prompt"):
            st.code(st.session_state["new_keyword_prompt"], language="markdown")
        else:
            st.caption("Run the tool once first to generate this prompt from your validated patterns.")
        new_keywords_file = st.file_uploader(
            "Upload new keyword candidates CSV (pattern_id, candidate_query)", type=["csv"], key="new_keywords_upload"
        )

run_clicked = st.button("Run SEO Hub Finder", type="primary")

if run_clicked:
    if mode != "Try demo data" and not gsc_file:
        st.error("Please upload a GSC CSV first.")
        st.stop()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        if mode == "Try demo data":
            gsc_path = gsc_path_fixed
            volume_path = volume_path_fixed
        else:
            gsc_path = tmp_path / "gsc.csv"
            gsc_path.write_bytes(gsc_file.getvalue())
            volume_path = None
            if mode == "Upload GSC CSV + volume CSV" and volume_file:
                volume_path = tmp_path / "volume.csv"
                volume_path.write_bytes(volume_file.getvalue())

        new_keywords_path = None
        if new_keywords_file:
            new_keywords_path = tmp_path / "new_keywords.csv"
            new_keywords_path.write_bytes(new_keywords_file.getvalue())

        out_dir = tmp_path / "out"
        out_dir.mkdir(exist_ok=True)

        try:
            with st.spinner("Analyzing data and generating outputs (AI templates and hero images can take a few minutes)..."):
                (patterns, memberships, queue, opportunities, hub_plan, existing_coverage,
                 new_keywords_checked, article_plan, ai_template_status) = run_pipeline(
                    gsc_csv=gsc_path,
                    volume_csv=volume_path,
                    new_keywords_csv=new_keywords_path,
                    out_dir=out_dir,
                    top_position=top_position,
                    expanded_position=expanded_position,
                    min_gsc_impressions=min_gsc_impressions,
                    min_pattern_queries=int(min_pattern_queries),
                    min_distinct_slot_values=int(min_distinct_slot_values),
                    min_template_confidence=min_template_confidence,
                    min_volume=min_volume,
                    trends_geo=trends_geo,
                    min_trends_relative=min_trends_relative,
                    max_trends_candidates=int(max_trends_candidates),
                    ai_api_key=GEMINI_API_KEY,
                    ai_article_templates=use_ai_templates,
                    hero_images_enabled=hero_enabled,
                    max_hero_images=int(max_hero_images),
                )
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        except Exception as exc:
            st.error("Something went wrong while processing this file. Double-check it's a valid CSV export.")
            with st.expander("Technical details"):
                st.exception(exc)
            st.stop()

        st.session_state["new_keyword_prompt"] = (out_dir / "new_keyword_candidates_prompt.md").read_text(encoding="utf-8")

        for note in data_quality_notes(patterns):
            st.warning(note)
        if not new_keywords_checked.empty:
            truncated = new_keywords_checked.attrs.get("truncated_candidates", 0)
            if truncated:
                st.warning(f"{truncated} candidate keyword(s) skipped (over the max-candidates cap in the sidebar).")

        confirmed = 0 if opportunities.empty else int((opportunities["final_status"] == "confirmed_opportunity").sum())
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Pattern candidates", len(patterns))
        c2.metric("Existing pages", len(existing_coverage))
        c3.metric("Volume queue", len(queue))
        c4.metric("Confirmed keywords", confirmed)
        c5.metric("Content hubs", len(hub_plan))

        st.subheader("1. Discovered programmatic pattern candidates")
        st.dataframe(patterns, width="stretch")

        st.subheader("2. Existing pages already covering a pattern")
        st.write("Query variants that already rank the same URL — not new opportunities, just proof the pattern is real.")
        st.dataframe(existing_coverage, width="stretch")

        st.subheader("3. Keyword volume check queue")
        st.write("Export this queue, check it in a keyword-volume tool, then re-import the volume CSV.")
        st.dataframe(queue, width="stretch")
        st.download_button(
            "Download keyword_volume_check_queue.csv",
            queue.to_csv(index=False).encode("utf-8"),
            file_name="keyword_volume_check_queue.csv",
            mime="text/csv",
        )

        st.subheader("4. Volume-validated opportunities")
        if volume_path:
            st.dataframe(opportunities, width="stretch")
        else:
            st.warning("No volume CSV uploaded. These candidates are not final recommendations yet.")
            st.dataframe(opportunities, width="stretch")

        st.subheader("5. New keyword candidates (Google Trends check)")
        if new_keywords_checked.empty:
            if GEMINI_API_KEY:
                st.caption("Gemini didn't return any candidates for this run's patterns.")
            else:
                st.caption("No candidates yet — see the panel above to add some.")
        else:
            st.write(
                "'confirmed' means Trends shows real relative interest vs. a GSC-proven keyword from the "
                "same hub. 'no_signal' isn't necessarily zero demand — Trends often can't see long-tail "
                "terms; verify manually via Keyword Planner if you still want to pursue those."
            )
            st.dataframe(new_keywords_checked, width="stretch")

        st.subheader("6. Content hub plan")
        if hub_plan.empty:
            st.warning("No final hub yet. Upload a volume CSV or lower the minimum volume threshold.")
        hub_plan_display = hub_plan.drop(
            columns=[c for c in TEMPLATE_COLUMNS + ["article_title_template", "recommended_article_structure",
                                                     "internal_linking_strategy", "hero_image_prompt"]
                     if c in hub_plan.columns and c != "intent"],
            errors="ignore",
        )
        st.dataframe(hub_plan_display, width="stretch")
        if not hub_plan.empty and "duplicate_of" in hub_plan.columns:
            duplicates = hub_plan[hub_plan["duplicate_of"].astype(str) != ""]
            if not duplicates.empty:
                st.warning(
                    f"{len(duplicates)} Hub(s) überschneiden sich fast vollständig mit einem stärkeren Hub "
                    "(Spalte duplicate_of) — nur den kanonischen Hub umsetzen."
                )

        st.subheader("7. Artikel-Templates pro Hub")
        st.caption(TEMPLATE_STATUS_MESSAGES.get(ai_template_status, ai_template_status))
        if hub_plan.empty:
            st.caption("Noch keine bestätigten Hubs — Templates erscheinen nach der Volumen-Validierung.")
        else:
            for _, row in hub_plan.iterrows():
                with st.expander(f"{row['hub_label']} — {row.get('h1_template', '')}"):
                    st.markdown(hub_template_markdown(row))
        st.download_button(
            "Download article_templates_and_linking.md",
            (out_dir / "article_templates_and_linking.md").read_bytes(),
            file_name="article_templates_and_linking.md",
            mime="text/markdown",
        )

        st.subheader("8. Artikel-Plan pro Keyword")
        st.write("Eine Zeile = ein Artikel, mit fertig ausgefüllter H1/Meta aus dem Hub-Template.")
        st.dataframe(article_plan, width="stretch")
        if not article_plan.empty:
            st.download_button(
                "Download article_plan_per_keyword.csv",
                article_plan.to_csv(index=False).encode("utf-8"),
                file_name="article_plan_per_keyword.csv",
                mime="text/csv",
            )

        st.subheader("9. Hub Hero-Images")
        hero_dir = out_dir / "hero_images"
        has_images = (
            not hub_plan.empty and "hero_image_file" in hub_plan.columns
            and (hub_plan["hero_image_file"].astype(str) != "").any()
        )
        if has_images:
            shown = hub_plan[hub_plan["hero_image_file"].astype(str) != ""]
            columns = st.columns(3)
            for i, (_, row) in enumerate(shown.iterrows()):
                image_path = hero_dir / row["hero_image_file"]
                if image_path.exists():
                    columns[i % 3].image(
                        image_path.read_bytes(),
                        caption=f"{row['hub_label']} ({row['hero_image_provider']})",
                        width="stretch",
                    )
            missing = hub_plan[
                (hub_plan["hero_image_file"].astype(str) == "")
                & hub_plan["hero_image_status"].astype(str).str.startswith(("failed", "skipped_time", "skipped_cap"))
            ]
            if not missing.empty:
                st.caption(
                    f"Kein Bild für: {', '.join(missing['hub_label'])} — die fertigen Bild-Prompts "
                    "stehen in content_hub_plan.csv (hero_image_prompt)."
                )
        elif hero_enabled:
            st.caption(
                "Keine Hero-Images in diesem Lauf (keine bestätigten Hubs, Provider nicht erreichbar oder "
                "Rate-Limit). Fertige Bild-Prompts stehen in content_hub_plan.csv (hero_image_prompt)."
            )
        else:
            st.caption("Hero-Image-Generierung ist in der Sidebar deaktiviert.")

        html_report = (out_dir / "seo_hub_finder_report.html").read_bytes()
        st.download_button("Download HTML report", html_report, file_name="seo_hub_finder_report.html", mime="text/html")

        zip_path = out_dir / "seo_hub_finder_outputs.zip"
        st.download_button(
            "Download all outputs as ZIP",
            zip_path.read_bytes(),
            file_name="seo_hub_finder_outputs.zip",
            mime="application/zip",
        )
