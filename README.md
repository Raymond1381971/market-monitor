# Market monitor

A cross-asset dashboard that rebuilds itself every weekday evening: equities, style factors, sectors, fixed income, Treasury curve, credit spreads, currencies, commodities and alternatives, volatility and financial conditions, the US economy, and your own portfolio against a benchmark. Every view switches between 1D, 1W, 1M, 3M, YTD, 1Y, 3Y and 5Y, and any instrument opens its own chart with its change over all eight horizons.

GitHub Actions fetches the data at 22:30 UTC Monday to Friday (6:30pm Eastern in summer, 5:30pm in winter), builds a single HTML page and publishes it to GitHub Pages. It costs nothing to run.

## Files

| File | Purpose |
|---|---|
| `config.yaml` | Every instrument, section and the benchmark. Edit this to add or remove series. |
| `build.py` | Fetches data, values the portfolio, writes `site/index.html`. |
| `template.html` | The page: layout, charts and interactions (ECharts). |
| `holdings.example.csv` | Format for your holdings. |
| `.github/workflows/build.yml` | The daily schedule, password step and Pages deployment. |

## Setup (about 15 minutes)

1. **Create the repository.** New repository on GitHub, then upload these files (keep the `.github` folder). A public repository works fine because your holdings live in an encrypted secret, never in the code. A private repository needs a paid GitHub plan to use Pages, and the published page is public either way unless you add the password in step 3.
2. **Get a FRED API key.** Free at fredaccount.stlouisfed.org → API Keys. Without it the rates, credit, conditions and economy sections are left out.
3. **Add secrets.** Repository → Settings → Secrets and variables → Actions → New repository secret:
   - `FRED_API_KEY`: the key from step 2.
   - `HOLDINGS_CSV`: paste the full contents of your holdings file (format below).
   - `DASHBOARD_PASSWORD`: recommended once holdings are in. The page is encrypted in the browser with StatiCrypt and asks for this password; "remember me" keeps you signed in for 30 days on each device.
4. **Turn on Pages.** Settings → Pages → Build and deployment → Source: **GitHub Actions**.
5. **Run it once.** Actions → Build dashboard → Run workflow. When it finishes (2–4 minutes) the page address appears on the run summary, typically `https://<your-username>.github.io/<repo-name>/`.

After that it runs on its own. Use **Run workflow** any time you want an immediate refresh, for example after updating holdings.

## Holdings

```csv
ticker,quantity,value,asset_class,name
VTI,1200,,US equity,Vanguard Total Stock Market
CASH,,85000,Cash,Money market and checking
,,250000,Private and illiquid,Deferred compensation (unpriced)
```

- Priced holdings: `ticker` (Yahoo symbol) and `quantity`. The same ticker across several accounts can appear on several rows; they are combined.
- Cash: ticker `CASH` with the dollar amount in `value`.
- Anything without a market price (deferred cash, private funds, real estate equity): leave `ticker` blank and put the dollar amount in `value`. It counts toward allocation but not toward performance.
- `asset_class` drives the allocation bar; use whatever categories you like.
- To update, edit the `HOLDINGS_CSV` secret and run the workflow.

**How performance is measured.** The page applies today's holdings to past prices, with no trades or cash flows. It answers "how has what I own now behaved", which is a holdings-based view, not your actual time-weighted return. Portfolio history starts on the first date every priced holding has data, so a recently listed holding shortens it.

## Privacy

- By default the page shows only percentages and weights. Set `show_dollar_values: true` in `config.yaml` to add total value and dollar change; do that only with `DASHBOARD_PASSWORD` set.
- Actions logs are public in a public repository. `build.py` never prints holdings, quantities or values; it prints only counts and names of series that failed.
- A GitHub Pages address is reachable by anyone who has it. StatiCrypt encrypts the page contents, so without the password the page shows nothing but a password box.

## Customizing

Everything visible comes from `config.yaml`. To add a series, copy a line in the right section and change `id`, `name`, `symbol` and `kind`; commit and the workflow rebuilds the page. The comments at the top of the file explain every field. Yahoo symbols are the ones in finance.yahoo.com URLs; FRED ids are the codes on fred.stlouisfed.org series pages.

The benchmark is a daily-rebalanced blend set under `benchmark:` (60% ACWI / 40% AGG to start).

Preview locally without any network or keys:

```bash
pip install -r requirements.txt
python build.py --demo --out site      # synthetic data, clearly bannered
open site/index.html
```

With a key and holdings, `FRED_API_KEY=... python build.py --out site` builds the real page from `holdings.csv` (git-ignored).

## Data notes

- **Missing data is left out, never estimated.** If a series returns nothing, it disappears from that day's page and is listed in the footer. If fewer than half the market series come back (Yahoo occasionally throttles cloud servers), the build stops and yesterday's page stays up; the workflow run shows as failed so you can rerun it.
- **Yahoo Finance** is unofficial and free. ETF returns are US-dollar total returns (dividend-adjusted). Commodity futures are front-month continuous contracts, so their returns include roll effects.
- **FRED** Treasury yields and ICE BofA credit spreads usually post one business day after the market close. FRED currently limits the ICE BofA spread series to about three years of history, so those show "–" at 5Y.
- Economic series show the latest release and the change from the prior one, with trend lines over three years (five at 5Y).

## Troubleshooting

- **Scheduled runs stopped.** GitHub pauses schedules after 60 days without repository activity. The workflow makes a small heartbeat commit every 40 days to prevent this; if it was paused anyway, re-enable it under Actions.
- **Run failed at "Fetch data".** Usually Yahoo throttling. Rerun the workflow; the previous page stays live in the meantime.
- **A series shows "–".** It has less history than the selected horizon.
