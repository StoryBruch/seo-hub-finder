import os
import tempfile
from pathlib import Path

import streamlit as st

from seo_hub_finder import MAX_TRENDS_CANDIDATES, data_quality_notes, run_pipeline


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
    with st.expander("New-keyword Trends check (advanced)"):
        trends_geo = st.text_input("Google Trends region code", value="DE")
        min_trends_relative = st.slider("Min. candidate/anchor Trends ratio", 0.0, 1.0, 0.1, 0.05)
        max_trends_candidates = st.number_input(
            "Max. candidates checked per run", value=MAX_TRENDS_CANDIDATES, min_value=1, max_value=100
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
            patterns, memberships, queue, opportunities, hub_plan, existing_coverage, new_keywords_checked = run_pipeline(
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
        st.dataframe(hub_plan, width="stretch")

        html_report = (out_dir / "seo_hub_finder_report.html").read_bytes()
        st.download_button("Download HTML report", html_report, file_name="seo_hub_finder_report.html", mime="text/html")

        zip_path = out_dir / "seo_hub_finder_outputs.zip"
        st.download_button(
            "Download all outputs as ZIP",
            zip_path.read_bytes(),
            file_name="seo_hub_finder_outputs.zip",
            mime="application/zip",
        )
