# Sponsor registers

The CSVs live here. They are the reason this project exists: they turn a
firehose of postings into the subset at employers legally able to hire you.

**Installed 2026-08-07:** UK (121,140 employers after route filtering) and
Netherlands (12,823). Both governments publish dated files whose URLs change on
every update, so they are not auto-downloaded — refresh every month or two. The
UK register in particular churns constantly as licences are granted and revoked.

## United Kingdom — `uk_licensed_sponsors.csv`

<https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers>

Download the current "Worker and Temporary Worker" CSV and save it here under
that filename. Columns are `Organisation Name, Town/City, County, Type & Rating,
Route`; carodi auto-detects the name column.

**Route filtering matters.** The register covers every sponsorship route, and
roughly 20,000 rows are routes that cannot hire a software engineer:

| route | rows | usable? |
|---|---:|---|
| Skilled Worker | 122,764 | ✅ the main route |
| Global Business Mobility (all variants) | 11,507 | ❌ requires you to already work for the group overseas |
| Tier 2 Ministers of Religion | 1,935 | ❌ |
| Creative Worker | 1,582 | ❌ |
| Charity Worker | 1,528 | ❌ |
| International Sportsperson | 1,472 | ❌ |
| Religious Worker | 1,422 | ❌ |
| Scale-up | 92 | ✅ |
| others | ~450 | ❌ |

`sources.yaml` therefore sets `routes: ["Skilled Worker", "Scale-up"]`, which
drops 5,250 employers licensed *only* for routes irrelevant to you. Remove the
key to match on any route.

## Netherlands — `nl_recognised_sponsors.csv`

<https://ind.nl/en/public-register-recognised-sponsors>

Use the "Regular labour and highly skilled migrants" register — every row on it
already qualifies, so there is no route column to filter. IND publishes it as
PDF/XLSX depending on the year; convert to a CSV with a `name` column.

## Verifying

    carodi registers
    carodi registers --probe "Monzo" "Adyen" "Some Random Startup"

Until a register is present, every onsite role in that country is rejected for
lack of a sponsor match — indistinguishable from a working filter that found
nothing. `carodi registers` exits non-zero in that state.

## How a match is graded

Company-name matching across 121k rows is genuinely ambiguous, so confidence is
graded rather than boolean:

| score | rule | example |
|---:|---|---|
| 100 | exact, after normalizing legal suffixes | `GoCardless` → `GoCardless Limited` |
| 95 | identical ignoring spacing | `Starlingbank` → `Starling Bank Limited` |
| 90 | unique word-boundary prefix | `Monzo` → `Monzo Bank Ltd` |
| 60 | ambiguous prefix — several employers share it | `Ramp` → `Ramp Swaps` *and* `Ramp Networks` |

90+ clears the hard gate and earns score; 60 is recorded but counts for nothing,
because a single common word collides with something almost every time. Any
match below 100 shows the matched name in the digest so you can spot a wrong one.

A match only counts **in the country the job is in** — being on the UK register
says nothing useful about a role in Antwerp.

## Adding another country

Any country publishing a similar list works. Add to `sources.yaml`:

    sponsor_registers:
      - country: IE
        path: data/registers/ie_employment_permits.csv

The loader auto-detects common name columns; override with `name_column`.
