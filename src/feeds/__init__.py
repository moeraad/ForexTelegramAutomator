"""Feed workers: one module per external data source.

Each module exposes a single async `fetch_<source>()` function that
returns a dict of `{feature_name: value}` ready to be passed to
`feature_store.put_many`. The bot wires each into its own async loop
with a cadence appropriate to the source (COT is weekly, GDELT polled
every 15min, ETF every hour). Loops never raise — they catch broadly
and log, so a remote outage on one feed never takes down the others.
"""
