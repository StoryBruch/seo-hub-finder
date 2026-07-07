# Search Volume Validation Workflow

## Key point

Google Search Console does **not** provide real keyword search volume.

GSC tells you:

- which queries already generated impressions
- which queries already generated clicks
- where the site currently ranks
- which page Google associates with the query

This is excellent for finding ranking proof, but it is not enough for a scalable Programmatic SEO rollout.

Search volume must come from an external keyword tool, for example:

- Google Keyword Planner
- Google Ads Keyword Planner export
- Sistrix
- Ahrefs
- Semrush
- Ubersuggest
- any other CSV with `keyword` + `volume`

**Recommended free option: Google Keyword Planner.** It's the only source in this list that gives real,
absolute monthly search volume at no cost — you just need a free Google Ads account (no active campaign or
spend required). Under Tools → Keyword Planner → "Get search volume and forecasts", paste the keywords from
`keyword_volume_check_queue.csv`, then export the results as CSV. Sistrix/Ahrefs/Semrush/Ubersuggest are paid
tools (or very limited free tiers) and are only listed here as alternatives if you already have access to one.

This queue only ever contains keywords that **already rank in GSC**. It is intentionally *not* checked via
Google Trends, because Trends only returns a relative 0–100 score per request, not an absolute number, so it
can't be compared against an absolute `--min-volume` threshold. For genuinely **new** keyword ideas that are
not in GSC at all, see "New keyword candidates" below — that's a separate, automatic, Trends-based check.

## Two-step workflow

### Step 1: GSC-only run

Run:

```bash
python seo_hub_finder.py sample_gsc.csv --out-dir out_no_volume
```

The tool creates:

```txt
out_no_volume/keyword_volume_check_queue.csv
```

This file contains candidate keywords that should be checked for search volume.

Example:

```csv
keyword,pattern_id,query_skeleton,gsc_impressions,gsc_clicks,gsc_position,reason
jura e8 entkalken,pattern_004,{slot_1} entkalken,2400,120,3.1,Check search volume before recommending rollout
```

### Step 2: Check volume externally

Upload/copy `keyword_volume_check_queue.csv` into a keyword-volume tool.

Export the result as CSV. The file should contain at least:

```txt
keyword
search volume
```

The app supports common column names such as:

```txt
keyword
query
search term
avg. monthly searches
average monthly searches
search volume
volume
suchvolumen
competition
wettbewerb
cpc
```

### Step 3: Re-run with volume CSV

Run:

```bash
python seo_hub_finder.py sample_gsc.csv --volume-csv sample_volume.csv --out-dir out_with_volume
```

Now the tool merges volume data back into the GSC-derived candidates.

Default gate:

```txt
min_volume = 10 monthly searches
```

Only keywords with confirmed search volume above the threshold become final opportunities.

You can change it:

```bash
python seo_hub_finder.py sample_gsc.csv --volume-csv sample_volume.csv --min-volume 50 --out-dir out_with_volume
```

## New keyword candidates (going beyond GSC)

GSC only proves demand for things people already searched *for this site*. To grow a validated hub with
keywords GSC never saw (e.g. a coffee-machine model nobody has searched for yet on this site), an LLM has to
suggest plausible new keywords — no heuristic can invent real brand/model/city names.

### Automatic (recommended): a free Gemini API key

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey), sign in with any Google account,
   click "Create API key". No credit card needed for the free tier.
2. Set it as an environment variable before running the CLI:

   ```bash
   export GEMINI_API_KEY=your-key-here   # PowerShell: $env:GEMINI_API_KEY = "your-key-here"
   python seo_hub_finder.py sample_gsc.csv --volume-csv sample_volume.csv --out-dir out
   ```

   For the Streamlit app, add it under Streamlit Cloud → your app → Settings → Secrets:
   `GEMINI_API_KEY = "your-key-here"`.
3. That's it — every run now automatically asks Gemini for new keyword ideas per validated pattern and
   checks them against Google Trends, with zero extra uploads. The same key also powers the per-hub
   German article templates and the hero images. The default model is `gemini-3.5-flash` (override with
   `--ai-model`), with an automatic fallback chain to `gemini-2.5-flash` / `gemini-2.5-flash-lite` if the
   default is unavailable — check [aistudio.google.com](https://aistudio.google.com) for current free-tier
   model names if all of them ever get renamed.

### Manual fallback (no API key)

Without `GEMINI_API_KEY`, the tool instead writes `out/new_keyword_candidates_prompt.md`. Paste its content
into any free LLM chat (ChatGPT, Gemini, Claude, ...), ask it to return a CSV with columns
`pattern_id,candidate_query`, save that as e.g. `new_keywords.csv`, then re-run with
`--new-keywords-csv new_keywords.csv`. Both paths converge on the same Trends check below.

### The Trends check (applies either way)

The tool checks each candidate against Google Trends, always alongside the pattern's best-performing real
GSC query as an anchor — so instead of an unlabeled 0–100 number, you get "about as searched as a keyword we
know gets real traffic here" (`trends_status = confirmed`). Only `confirmed` candidates are added to
`content_hub_plan.csv`. `no_signal` doesn't mean zero real demand — Trends often can't detect long-tail
terms, especially over a smoothed 12-month window — check those manually via Keyword Planner if you still
want to pursue them. Results for every candidate (confirmed or not) are in
`new_keyword_candidates_checked.csv`. By default at most 25 candidates are checked per run
(`--max-trends-candidates`) to keep runtime bounded on free hosting.

## Output logic

The final report separates:

1. `discovered_programmatic_patterns.csv`
   - Pattern candidates from GSC data.

2. `existing_pages_by_pattern.csv`
   - Query variants already ranking the *same* URL, grouped per pattern — proof the pattern is real,
     not a list of new opportunities.

3. `keyword_volume_check_queue.csv`
   - Keywords that need external search-volume validation.

4. `programmatic_opportunities.csv`
   - Keyword-level results after volume import.

5. `new_keyword_candidates_prompt.md`
   - Paste this into a free LLM to brainstorm new keyword ideas beyond GSC.

6. `new_keyword_candidates_checked.csv`
   - Those ideas (if re-imported), each checked against Google Trends.

7. `content_hub_plan.csv`
   - Only patterns with volume-confirmed opportunities, plus any Trends-confirmed new keywords.

8. `article_templates_and_linking.md`
   - Hub structure, article template and internal linking strategy.

## Why this is important

Without this volume gate, an AI tool could hallucinate attractive-sounding hubs that have no demand.

With the gate:

```txt
GSC proves relevance.
Search volume proves demand.
Hub plan combines both.
```
