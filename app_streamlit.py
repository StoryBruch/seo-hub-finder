import tempfile
from pathlib import Path
import zipfile

import pandas as pd
import streamlit as st

from seo_hub_finder import (
    build_hub_plan,
    build_volume_queue,
    data_quality_notes,
    discover_patterns,
    merge_volume,
    normalize_gsc,
    normalize_volume,
    write_ai_prompt,
    write_article_templates_md,
    write_html_report,
)

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

        out_dir = tmp_path / "out"
        out_dir.mkdir(exist_ok=True)

        try:
            gsc = normalize_gsc(gsc_path)
            volume = normalize_volume(volume_path) if volume_path else pd.DataFrame(columns=["query", "search_volume", "competition", "cpc"])
            patterns, memberships = discover_patterns(
                gsc=gsc,
                top_position=top_position,
                min_gsc_impressions=min_gsc_impressions,
                expanded_position=expanded_position,
                min_pattern_queries=int(min_pattern_queries),
                min_distinct_slot_values=int(min_distinct_slot_values),
                min_template_confidence=min_template_confidence,
            )
            queue = build_volume_queue(memberships)
            opportunities = merge_volume(memberships, volume, min_volume)
            hub_plan = build_hub_plan(opportunities, patterns)
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        except Exception as exc:
            st.error("Something went wrong while processing this file. Double-check it's a valid CSV export.")
            with st.expander("Technical details"):
                st.exception(exc)
            st.stop()

        for note in data_quality_notes(patterns):
            st.warning(note)

        confirmed = 0 if opportunities.empty else int((opportunities["final_status"] == "confirmed_opportunity").sum())
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Pattern candidates", len(patterns))
        c2.metric("Volume queue", len(queue))
        c3.metric("Confirmed keywords", confirmed)
        c4.metric("Content hubs", len(hub_plan))

        st.subheader("1. Discovered programmatic pattern candidates")
        st.dataframe(patterns, width="stretch")

        st.subheader("2. Keyword volume check queue")
        st.write("Export this queue, check it in a keyword-volume tool, then re-import the volume CSV.")
        st.dataframe(queue, width="stretch")
        st.download_button(
            "Download keyword_volume_check_queue.csv",
            queue.to_csv(index=False).encode("utf-8"),
            file_name="keyword_volume_check_queue.csv",
            mime="text/csv",
        )

        st.subheader("3. Volume-validated opportunities")
        if volume_path:
            st.dataframe(opportunities, width="stretch")
        else:
            st.warning("No volume CSV uploaded. These candidates are not final recommendations yet.")
            st.dataframe(opportunities, width="stretch")

        st.subheader("4. Content hub plan")
        if hub_plan.empty:
            st.warning("No final hub yet. Upload a volume CSV or lower the minimum volume threshold.")
        st.dataframe(hub_plan, width="stretch")

        patterns.to_csv(out_dir / "discovered_programmatic_patterns.csv", index=False)
        memberships.to_csv(out_dir / "pattern_keyword_memberships.csv", index=False)
        queue.to_csv(out_dir / "keyword_volume_check_queue.csv", index=False)
        opportunities.to_csv(out_dir / "programmatic_opportunities.csv", index=False)
        hub_plan.to_csv(out_dir / "content_hub_plan.csv", index=False)
        write_ai_prompt(patterns, out_dir / "ai_pattern_review_prompt.md")
        write_article_templates_md(hub_plan, patterns, out_dir / "article_templates_and_linking.md")
        write_html_report(patterns, queue, opportunities, hub_plan, out_dir / "seo_hub_finder_report.html")

        html_report = (out_dir / "seo_hub_finder_report.html").read_bytes()
        st.download_button("Download HTML report", html_report, file_name="seo_hub_finder_report.html", mime="text/html")

        zip_path = tmp_path / "seo_hub_finder_outputs.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in out_dir.iterdir():
                zf.write(file, arcname=file.name)
        st.download_button(
            "Download all outputs as ZIP",
            zip_path.read_bytes(),
            file_name="seo_hub_finder_outputs.zip",
            mime="application/zip",
        )
