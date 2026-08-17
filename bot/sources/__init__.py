"""Per-exchange data fetchers.

Each module exposes a `fetch(http, series, spec, today, **wiring) -> FetchResult`. Fetchers do
I/O and normalisation only; all percentage arithmetic lives in bot.compute so that every
instrument is treated identically.
"""
