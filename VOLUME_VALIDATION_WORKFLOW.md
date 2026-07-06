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
Google Trends is intentionally not used as a substitute: it only returns a relative interest score (0–100),
not an absolute search volume, so it can't be compared against the `--min-volume` threshold.

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

## Output logic

The final report separates:

1. `discovered_programmatic_patterns.csv`
   - Pattern candidates from GSC data.

2. `keyword_volume_check_queue.csv`
   - Keywords that need external search-volume validation.

3. `programmatic_opportunities.csv`
   - Keyword-level results after volume import.

4. `content_hub_plan.csv`
   - Only patterns with volume-confirmed opportunities.

5. `article_templates_and_linking.md`
   - Hub structure, article template and internal linking strategy.

## Why this is important

Without this volume gate, an AI tool could hallucinate attractive-sounding hubs that have no demand.

With the gate:

```txt
GSC proves relevance.
Search volume proves demand.
Hub plan combines both.
```
