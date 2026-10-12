# Singapore Rental Listings Aggregator

Pulls rental listings near a target location from several Singapore property sites,
merges them into one schema, geocodes and distance-ranks them, and produces an Excel
workbook plus a written report.

Built to answer one practical question: *what can a group of students rent within 3 km
of NTU, and what is a fair price per room?* The last full run normalised **1,308 listings
from 7 sources**.

*[中文说明](README.zh-CN.md)*

## Pipeline

```
scrapers (one per site) -> raw JSON -> normalise -> geocode + radius filter -> analyse -> Excel / Markdown / PDF
```

| Stage | Module | What it does |
|---|---|---|
| Collect | `src/scrapers/` | One scraper per site, each returning raw records; raw JSON is saved so analysis can be re-run offline (`--from-raw`) |
| Normalise | `src/normalize.py` | Maps every source to one 23-field schema: price, bedrooms, room type, furnishing, amenities, coordinates, source URL |
| Geocode | `src/core/geo.py` | Addresses without coordinates go through the official OneMap API, with an on-disk cache; distance is haversine to the reference point |
| Analyse | `src/analyze.py` | Price bands, per-room price estimates for whole-unit rentals, value scoring within comparable groups |
| Report | `src/report.py` | Multi-sheet Excel workbook and a Markdown report; `md_to_pdf.py` renders the PDF |
| Diff | `clean_diff.py` | Compares two runs to find listings that disappeared (likely rented) and new ones |

## Sources

| Site | Method |
|---|---|
| 99.co | Browser session, then the site's own listing API called from inside the page |
| PropertyGuru | HTTP fetch, listings parsed from the embedded `__NEXT_DATA__` JSON |
| EdgeProp, Ohmyhome | Browser session with in-page fetch |
| Carousell, roomies.sg, rentinsingapore, SRX | HTTP fetch + HTML parsing, best effort |

Listings per source in the last run: 99.co 831, PropertyGuru 312, Carousell 98,
EdgeProp 31, rentinsingapore 14, roomies.sg 12, Ohmyhome 10. A source that fails is
logged and skipped; it never aborts the run.

## Usage

```bash
pip install -r requirements.txt
playwright install chromium

python run.py                           # all sites
python run.py --sites 99co,propertyguru
python run.py --pages 25                # max pages per site
python run.py --from-raw                # skip collection, rebuild analysis from saved raw data
python clean_diff.py old.json new.json  # what changed between two runs
```

Outputs land in `data/`: `listings.csv`, `rent_summary.xlsx`, `REPORT.md`.

## Method notes

- **Per-room price for whole units.** Rent is split by a master : common weight of
  1.3 : 1, which gives 40/30/30 for a three-bedroom unit and 56.5/43.5 for a two-bedroom,
  so a common room never comes out dearer than the master.
- **Value score.** Within each comparable group (listing type x bedrooms), price and
  distance are z-scored and combined 0.6 / 0.4, then mapped to 0-100.
- **Approximate distances are flagged.** When only a district-level address is available
  the record carries `distance_is_approx`, so it is not ranked as if it were exact.
- **Request handling.** Randomised delays, exponential backoff with jitter, and a retry
  list limited to transient status codes (`src/core/client.py`).

## Data

No listing data is included in this repository. Listings carry agent and owner details
and belong to the source sites; the collection was run at a low request rate for personal
housing research only.
