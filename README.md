# SEO Hub Finder — AI Pattern Discovery + Search Volume Gate

A small, pitch-ready prototype that analyzes Google Search Console exports and finds Programmatic SEO opportunities.

The core idea:

```txt
GSC export → automatic pattern discovery → keyword volume queue → volume CSV import → content hub plan
```

## What makes it different

The user does **not** have to manually define every pattern like:

- `beste X für Y`
- `[marke] reinigen`
- `[marke] entkalken`
- `X rezept`

Those are only examples. The tool tries to infer repeatable query structures from GSC data itself.

Example:

```txt
delonghi magnifica entkalken
jura e8 entkalken
saeco xelsis entkalken
```

becomes:

```txt
{slot_1} entkalken
```

Then the tool checks whether this pattern has enough GSC ranking proof, slot diversity and, after the second step, confirmed search volume.

## Project files

```txt
seo_hub_finder.py                # CLI + core logic
app_streamlit.py                 # browser demo app
sample_gsc.csv                   # sample GSC export
sample_volume.csv                # sample search-volume export
requirements.txt                 # dependencies
README.md                        # this file
VOLUME_VALIDATION_WORKFLOW.md    # exact volume validation logic
DEPLOYMENT_GUIDE.md              # how to share as live demo
CLAUDE_CODE_MASTER_PROMPT.md     # prompt to paste into Claude Code
```

Generated output files:

```txt
out/discovered_programmatic_patterns.csv
out/pattern_keyword_memberships.csv
out/existing_pages_by_pattern.csv
out/keyword_volume_check_queue.csv
out/programmatic_opportunities.csv
out/new_keyword_candidates_prompt.md
out/new_keyword_candidates_checked.csv
out/content_hub_plan.csv
out/article_plan_per_keyword.csv
out/article_templates_and_linking.md
out/hero_images/<hub>.jpg
out/ai_pattern_review_prompt.md
out/seo_hub_finder_report.html
out/seo_hub_finder_outputs.zip
```

Every volume-confirmed hub also gets:

- an intent-specific **German article template** (H1/meta/intro templates with literal `{slot_n}`
  placeholders, H2/H3 outline, FAQ questions, schema.org types, word-count target, E-E-A-T checklist) —
  AI-refined via the free Gemini tier when `GEMINI_API_KEY` is set, otherwise from built-in static
  intent profiles;
- a **photorealistic 16:9 hero image** (free Gemini image model when a key is set; Pollinations.ai as
  keyless fallback — anonymous fallback images may carry a small watermark; disable with
  `--no-hero-images`). The ready-to-paste image prompt is always written to `content_hub_plan.csv`;
- a spoke-level editorial plan (`article_plan_per_keyword.csv`): one row per article with the
  slot-filled H1/meta/URL, including Trends-confirmed AI keyword suggestions as new-article rows.

See `VOLUME_VALIDATION_WORKFLOW.md` for the full search-volume and new-keyword-candidate workflow.

## Tests

```bash
python -m unittest test_seo_hub_finder -v
```

(dev-only; all network calls are mocked)

## Quick local test

```bash
pip install -r requirements.txt
python seo_hub_finder.py sample_gsc.csv --out-dir out_no_volume
```

This creates a keyword queue for search-volume validation.

## Full local test with volume validation

```bash
python seo_hub_finder.py sample_gsc.csv --volume-csv sample_volume.csv --out-dir out_with_volume
```

Now the tool creates final opportunities and a content hub plan.

## Streamlit demo

```bash
streamlit run app_streamlit.py
```

In the app, choose:

```txt
Try demo data
```

This lets a company test the complete flow without preparing any files.

## Pitch-friendly usage

For a hiring/application pitch, the easiest setup is:

1. Deploy `app_streamlit.py` as a Streamlit app.
2. Include a button/mode: `Try demo data`.
3. Link the generated sample HTML report.
4. Include the GitHub repo or ZIP as proof.
5. Add a 60-90 second Loom walkthrough.

The company should not need Claude Code to test the tool. Claude Code is only useful to rebuild or extend the prototype.
