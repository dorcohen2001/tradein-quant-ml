"""Model-agnostic market-data helpers.

Currently just the cache-bucket policy. The rest of each model's
`utils/data_fetch.py` stays where it is: the two data layers have genuinely
diverged (Livne carries Bybit order-book depth and extra intraday helpers that
Biton has no use for), and folding them together would mean rewriting the live
price path that position management depends on.
"""
