"""Data layer for the quarterly-earnings backtest.

Three independent fetch legs plus the caches they write:

- ``edgar`` — US quarterly fundamentals with point-in-time filing dates,
  from SEC EDGAR XBRL company facts.
- ``prices`` — deep daily price history for US and Indian tickers via Yahoo
  Finance, cached per ticker with a fetch manifest.
- ``screener_quarters`` — Indian quarterly results parsed out of Screener.in
  company pages, with results-announcement dates where they are exposed.

Everything lands under ``data/backtest/`` so the backtest engine can run
entirely offline against these caches.
"""
