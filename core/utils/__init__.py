"""Shared utilities: yfinance disk cache, Telegram transport, market-session
gating, and the chart-render bridge. Previously duplicated byte-for-byte in
both model codebases; a fix here now reaches both models at once.
"""
