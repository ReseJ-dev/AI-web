# Offline Shopify agencies demo

> **DEMO DATA:** Every company, domain, evidence URL, score, and skipped source
> in this directory is synthetic. None is a verified real-world research result.

This directory provides a network-free portfolio presentation for the
`shopify-agencies-netherlands` scenario:

- [`shopify_agencies_fake_results.json`](shopify_agencies_fake_results.json)
  contains 30 fictional results using reserved `.example` domains.
- [`google_sheets/Research Results.csv`](<google_sheets/Research Results.csv>)
  previews the primary Google Sheets tab.
- [`google_sheets/Skipped Sources.csv`](<google_sheets/Skipped Sources.csv>)
  previews the source audit tab.
- [`google_sheets/Run Metadata.csv`](<google_sheets/Run Metadata.csv>) previews
  the run audit tab and identifies the fake providers.

The canonical research brief, ten example queries, requested fields, and
ordered sheet columns are defined in
[`config/demo_shopify_agencies.yaml`](../config/demo_shopify_agencies.yaml).
The CSV previews deliberately show only a few rows; the JSON fixture contains
the complete 30-result synthetic dataset.

These files are presentation fixtures. They must never be imported into the
verified production-record repository or represented as findings about real
companies.
