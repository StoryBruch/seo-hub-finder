# Deployment Guide

## Best option for a hiring pitch

Use a Streamlit demo link.

The company should be able to click one link, choose `Try demo data`, and see the full workflow immediately.

## Local test

```bash
git clone <your-repo-url>
cd seo-hub-finder
pip install -r requirements.txt
streamlit run app_streamlit.py
```

## Streamlit Cloud deployment

1. Create a GitHub repo.
2. Add all project files.
3. Go to Streamlit Community Cloud.
4. Create a new app from the GitHub repo.
5. Main file path:

```txt
app_streamlit.py
```

6. Share the app link in your application.

## What to send in the application

Recommended package:

```txt
1. Live demo link
2. Sample HTML report
3. GitHub repo or ZIP
4. 60-90 second Loom video
```

Suggested pitch line:

```txt
I built a small prototype that analyzes GSC exports, automatically detects repeatable Programmatic SEO patterns, validates demand through a search-volume CSV, and turns the result into content hubs, article templates and internal linking plans.
```

## Do they need Claude Code?

No.

Claude Code is only for rebuilding or extending the project. Stakeholders should test via:

- Streamlit demo link, or
- local Python run, or
- sample HTML report.
