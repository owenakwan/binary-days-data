# binary-days-data

Data feed for the Binary Days website. A GitHub Action rebuilds it every 30 minutes and
publishes the JSON files to the **`data`** branch, which the site reads directly:

| File | What it is | Source |
|---|---|---|
| `catalysts.json` | Biotech catalysts (PDUFA dates, readouts) plus dates found in company news | Curated list, Google News, FDA calendar via Finnhub |
| `earnings.json` | US earnings reports for the next 5 weeks, with each company's usual report time | Finnhub |
| `insiders.json` | Open-market insider buys and sells (Form 4, codes P and S), last 14 days | SEC EDGAR |

Scripts: `build_site_data.py` (catalysts + earnings) and `build_insiders.py` (insider trades).

Repository secrets used by the workflow: `FINNHUB_KEY`, `SEC_CONTACT` (contact email the SEC asks
for in automated requests), `PRIVATE_REPO_TOKEN` (read-only access to the curated catalyst list).

Not investment advice. Data can be late or wrong; check the original filing or company release.
