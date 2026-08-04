"""Fundamental & News Sentiment Engine.

Fetches recent headlines (Google News RSS by default; NewsAPI/Finnhub if API
keys are configured) and scores sentiment with VADER, then validates that the
news tone aligns with the proposed technical direction.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import feedparser
import requests
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from core.settings import NEWSAPI_KEY, NEWS_LOOKBACK_HOURS, REQUEST_TIMEOUT

_analyzer = SentimentIntensityAnalyzer()


@dataclass
class Headline:
    title: str
    link: str
    published: dt.datetime | None
    compound: float


@dataclass
class NewsResult:
    query: str
    headlines: list[Headline] = field(default_factory=list)
    avg_compound: float = 0.0
    label: str = "Neutral"  # Positive / Neutral / Negative


def _within_lookback(published: dt.datetime | None, hours: int) -> bool:
    if published is None:
        return True  # keep items with unknown dates rather than silently dropping all news
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    if published.tzinfo is None:
        published = published.replace(tzinfo=dt.timezone.utc)
    return published >= cutoff


def fetch_google_news_rss(query: str, hours: int = NEWS_LOOKBACK_HOURS, max_items: int = 15) -> list[Headline]:
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
    feed = feedparser.parse(url)
    headlines = []
    for entry in feed.entries[:max_items]:
        published = None
        if getattr(entry, "published", None):
            try:
                published = parsedate_to_datetime(entry.published)
            except Exception:
                published = None
        if not _within_lookback(published, hours):
            continue
        score = _analyzer.polarity_scores(entry.title)["compound"]
        headlines.append(Headline(title=entry.title, link=entry.link, published=published, compound=score))
    return headlines


def fetch_newsapi(query: str, hours: int = NEWS_LOOKBACK_HOURS, max_items: int = 15) -> list[Headline]:
    if not NEWSAPI_KEY:
        return []
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": query, "from": since, "sortBy": "publishedAt", "apiKey": NEWSAPI_KEY, "language": "en"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])[:max_items]
    except Exception:
        return []

    headlines = []
    for a in articles:
        published = None
        if a.get("publishedAt"):
            try:
                published = dt.datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00"))
            except Exception:
                published = None
        score = _analyzer.polarity_scores(a.get("title", ""))["compound"]
        headlines.append(Headline(title=a.get("title", ""), link=a.get("url", ""), published=published, compound=score))
    return headlines


def analyze_sentiment(ticker: str, asset_class: str, hours: int = NEWS_LOOKBACK_HOURS) -> NewsResult:
    query = f"{ticker} stock" if asset_class == "stocks" else f"{ticker.replace('USDT', '')} crypto"

    headlines = fetch_newsapi(query, hours) if NEWSAPI_KEY else []
    if not headlines:
        headlines = fetch_google_news_rss(query, hours)

    if not headlines:
        return NewsResult(query=query, headlines=[], avg_compound=0.0, label="Neutral")

    avg = sum(h.compound for h in headlines) / len(headlines)
    if avg >= 0.05:
        label = "Positive"
    elif avg <= -0.05:
        label = "Negative"
    else:
        label = "Neutral"

    return NewsResult(query=query, headlines=headlines, avg_compound=round(avg, 3), label=label)


def confluence_check(direction: str, news: NewsResult) -> tuple[bool, str]:
    """Bullish TA requires Neutral-to-Positive sentiment; Bearish TA requires
    Neutral-to-Negative sentiment. Reject on conflicting sentiment."""
    if direction == "LONG" and news.label == "Negative":
        return False, f"Rejected: LONG setup conflicts with Negative news sentiment ({news.avg_compound})"
    if direction == "SHORT" and news.label == "Positive":
        return False, f"Rejected: SHORT setup conflicts with Positive news sentiment ({news.avg_compound})"
    return True, f"News sentiment ({news.label}, {news.avg_compound}) aligns with {direction} thesis"
