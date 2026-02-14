"""
tools/scrape_news.py

Scrapes current AI/ML news articles from NewsAPI and RSS feeds.
Returns a deduplicated, sorted list of article dicts for use by generate_newsletter.py.

Usage:
  from tools.scrape_news import scrape_news
  articles = scrape_news(newsapi_key=os.environ["NEWSAPI_KEY"])

Standalone test:
  python tools/scrape_news.py
"""

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests

# Common words that carry no topic signal — excluded from topic fingerprints
STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "has", "have", "had",
    "be", "been", "being", "this", "that", "these", "those", "it", "its",
    "for", "in", "on", "at", "to", "from", "with", "by", "of", "and",
    "or", "but", "not", "vs", "after", "before", "as", "about", "into",
    "over", "under", "out", "will", "can", "could", "would", "should",
    "may", "might", "do", "does", "did", "get", "gets", "got",
    "make", "makes", "made", "says", "said", "reports", "report",
    "new", "now", "just", "more", "than", "so", "up", "here", "there",
    "also", "even", "only", "very", "much", "many", "most", "all",
    "how", "why", "what", "when", "where", "who", "which",
    "our", "your", "their", "one", "two", "three",
    "year", "years", "week", "weeks", "day", "days", "time",
    "way", "use", "used", "using", "company", "companies",
})

RSS_FEEDS = [
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.technologyreview.com/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.wired.com/feed/rss",
]

# NewsAPI query — restricted to AI-focused publications via domains param
NEWSAPI_QUERY = (
    '"artificial intelligence" OR "machine learning" OR "generative AI" OR '
    '"large language model" OR "LLM" OR "AI startup" OR "AI funding"'
)

# Only pull from reputable AI/tech news sources
NEWSAPI_DOMAINS = (
    "techcrunch.com,wired.com,theverge.com,venturebeat.com,"
    "technologyreview.com,arstechnica.com,zdnet.com,reuters.com,"
    "bloomberg.com,wsj.com,ft.com,theinformation.com"
)

GOSSIP_KEYWORDS = [
    "openai", "google", "meta", "anthropic", "microsoft", "apple",
    "deepmind", "lawsuit", "regulation", "partnership", "ceo", "valuation",
    "agi", "chatgpt", "gemini", "gpt", "regulation", "banned", "fired",
    "acquisition", "billion", "controversy", "warning", "safety", "senate",
    "congress", "ftc", "european", "china", "nvidia",
]

BUILDERS_KEYWORDS = [
    "startup", "funding", "raises", "raised", "series a", "series b", "series c",
    "seed", "launch", "launched", "tool", "platform", "open-source", "open source",
    "api", "release", "released", "product", "app", "model", "framework",
    "developer", "founder", "venture", "vc", "investment", "backed",
]


@dataclass
class Article:
    title: str
    url: str
    source: str
    published_at: str          # ISO 8601
    description: str
    category_hint: str         # "gossip" | "builders" | "unknown"


def _strip_utm(url: str) -> str:
    """Remove UTM tracking parameters from URL for deduplication."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        clean_params = {k: v for k, v in params.items() if not k.startswith("utm_")}
        clean_query = urlencode(clean_params, doseq=True)
        return urlunparse(parsed._replace(query=clean_query))
    except Exception:
        return url


def _parse_date(date_str: Optional[str]) -> str:
    """Parse various date formats to ISO 8601. Falls back to now."""
    if not date_str:
        return datetime.now(timezone.utc).isoformat()
    try:
        from dateutil import parser as dateutil_parser
        return dateutil_parser.parse(date_str).isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def assign_category_hint(article: Article) -> Article:
    """
    Keyword heuristic to pre-label articles before Claude sees them.
    Claude makes the final categorization decision.
    """
    text = (article.title + " " + article.description).lower()
    gossip_score = sum(1 for kw in GOSSIP_KEYWORDS if kw in text)
    builders_score = sum(1 for kw in BUILDERS_KEYWORDS if kw in text)

    if gossip_score > builders_score:
        article.category_hint = "gossip"
    elif builders_score > gossip_score:
        article.category_hint = "builders"
    else:
        article.category_hint = "unknown"

    return article


def fetch_newsapi_articles(
    api_key: str,
    query: str = NEWSAPI_QUERY,
    domains: str = NEWSAPI_DOMAINS,
    page_size: int = 30,
    lookback_days: int = 6,
) -> list[Article]:
    """
    Calls NewsAPI /v2/everything endpoint restricted to AI-focused domains.
    Returns up to page_size articles sorted by publishedAt descending.
    """
    from_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime(
        "%Y-%m-%d"
    )
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "domains": domains,
        "from": from_date,
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "language": "en",
        "apiKey": api_key,
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 429:
            print("WARNING: NewsAPI rate limit hit. Falling back to RSS-only.", flush=True)
            return []
        elif resp.status_code == 401:
            raise ValueError("NewsAPI key is invalid. Check NEWSAPI_KEY.") from e
        else:
            print(f"WARNING: NewsAPI returned {resp.status_code}. Falling back to RSS.", flush=True)
            return []
    except requests.exceptions.RequestException as e:
        print(f"WARNING: NewsAPI request failed: {e}. Falling back to RSS.", flush=True)
        return []

    data = resp.json()
    articles = []
    for item in data.get("articles", []):
        title = item.get("title", "")
        url_val = item.get("url", "")
        description = item.get("description") or item.get("content") or title

        # Skip removed articles
        if not title or title == "[Removed]" or not url_val:
            continue

        article = Article(
            title=title,
            url=url_val,
            source=item.get("source", {}).get("name", "Unknown"),
            published_at=_parse_date(item.get("publishedAt")),
            description=description[:500],
            category_hint="unknown",
        )
        articles.append(assign_category_hint(article))

    return articles


def fetch_rss_articles(
    feed_urls: list[str] = RSS_FEEDS,
    max_per_feed: int = 8,
    lookback_days: int = 6,
) -> list[Article]:
    """
    Fetches and parses RSS feeds. Uses requests for timeout control,
    then passes content to feedparser.
    Only returns articles published within the last lookback_days days.
    """
    try:
        import feedparser
    except ImportError:
        print("WARNING: feedparser not installed. Skipping RSS feeds.")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    articles = []
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}

    for feed_url in feed_urls:
        try:
            resp = requests.get(feed_url, timeout=10, headers=headers)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as e:
            print(f"WARNING: RSS feed {feed_url} failed: {e}. Skipping.", flush=True)
            continue

        count = 0
        for entry in feed.entries:
            if count >= max_per_feed:
                break

            title = entry.get("title", "").strip()
            url_val = entry.get("link", "")
            description = (
                entry.get("summary", "")
                or entry.get("description", "")
                or title
            )
            # Strip HTML tags from description
            description = re.sub(r"<[^>]+>", "", description).strip()

            published_raw = (
                entry.get("published", "")
                or entry.get("updated", "")
                or ""
            )

            if not title or not url_val:
                continue

            published_at = _parse_date(published_raw)

            # Skip articles older than the lookback window
            try:
                from dateutil import parser as dateutil_parser
                pub_dt = dateutil_parser.parse(published_at)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    continue
            except Exception:
                pass  # If date parsing fails, include the article anyway

            source = feed.feed.get("title", urlparse(feed_url).netloc)

            article = Article(
                title=title,
                url=url_val,
                source=source,
                published_at=published_at,
                description=description[:500],
                category_hint="unknown",
            )
            articles.append(assign_category_hint(article))
            count += 1

    return articles


def _keyword_set(title: str) -> frozenset:
    """
    Extracts significant keywords from an article title for topic comparison.
    Returns only lowercase alpha words of length >= 4 that are not stopwords.
    """
    words = re.findall(r'\b[a-z][a-z]{2,}\b', title.lower())
    return frozenset(w for w in words if w not in STOPWORDS)


def _same_topic(title1: str, title2: str, threshold: float = 0.45) -> bool:
    """
    Returns True if two article titles appear to cover the same news event.
    Uses overlap coefficient: shared keywords / size of smaller keyword set.
    Requires at least 2 keywords in each title to avoid spurious matches.
    """
    kw1 = _keyword_set(title1)
    kw2 = _keyword_set(title2)
    min_size = min(len(kw1), len(kw2))
    if min_size < 2:
        return False
    overlap = len(kw1 & kw2)
    return (overlap / min_size) >= threshold


def _deduplicate(articles: list[Article]) -> list[Article]:
    """
    Removes duplicate articles using three checks (in order):
    1. Exact URL match (after stripping UTM params)
    2. Title string similarity >= 0.85 (difflib) — catches near-identical rewrites
    3. Topic keyword overlap >= 45% — catches different-source articles about the same event

    Articles are processed in order; the first (highest-priority) article is kept.
    NewsAPI articles are prepended before RSS, so they win ties.
    """
    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    unique: list[Article] = []

    for article in articles:
        clean_url = _strip_utm(article.url)
        if clean_url in seen_urls:
            continue

        is_dup_title = any(
            SequenceMatcher(None, article.title.lower(), seen.lower()).ratio() >= 0.85
            for seen in seen_titles
        )
        if is_dup_title:
            continue

        is_dup_topic = any(
            _same_topic(article.title, seen)
            for seen in seen_titles
        )
        if is_dup_topic:
            continue

        seen_urls.add(clean_url)
        seen_titles.append(article.title)
        unique.append(article)

    return unique


def scrape_news(
    newsapi_key: str,
    target_article_count: int = 30,
    lookback_days: int = 6,
) -> list[dict]:
    """
    Main entry point. Fetches from NewsAPI + RSS feeds, deduplicates,
    sorts by date descending, returns as list of JSON-serializable dicts.

    lookback_days: how far back to look for articles (default 6 days).
    target_article_count: number of articles to return after dedup (default 30).

    Raises ValueError if fewer than 6 articles are found (not enough for newsletter).
    """
    print(f"Fetching articles from the past {lookback_days} days...", flush=True)
    print("Fetching articles from NewsAPI...", flush=True)
    newsapi_articles = fetch_newsapi_articles(
        api_key=newsapi_key,
        page_size=target_article_count,
        lookback_days=lookback_days,
    )
    print(f"  NewsAPI: {len(newsapi_articles)} articles", flush=True)

    print("Fetching articles from RSS feeds...", flush=True)
    rss_articles = fetch_rss_articles(RSS_FEEDS, max_per_feed=8, lookback_days=lookback_days)
    print(f"  RSS feeds: {len(rss_articles)} articles", flush=True)

    # Combine: NewsAPI first (generally higher quality), then RSS
    combined = newsapi_articles + rss_articles

    # Deduplicate
    unique = _deduplicate(combined)
    print(f"  After deduplication: {len(unique)} unique articles", flush=True)

    # Sort by published_at descending
    unique.sort(key=lambda a: a.published_at, reverse=True)

    # Trim to target count
    result = unique[:target_article_count]

    if len(result) < 6:
        raise ValueError(
            f"Insufficient articles scraped: {len(result)}. "
            "Check NewsAPI key and RSS feed connectivity."
        )

    # Convert to plain dicts for JSON serializability
    return [asdict(a) for a in result]


if __name__ == "__main__":
    # Standalone test
    from dotenv import load_dotenv
    load_dotenv()

    api_key = os.environ.get("NEWSAPI_KEY", "")
    if not api_key:
        print("ERROR: NEWSAPI_KEY not set in .env")
        sys.exit(1)

    articles = scrape_news(newsapi_key=api_key)
    print(f"\nScraped {len(articles)} articles:")
    for i, a in enumerate(articles, 1):
        print(f"\n{i}. [{a['category_hint'].upper()}] {a['title']}")
        print(f"   Source: {a['source']} | {a['published_at'][:10]}")
        print(f"   URL: {a['url']}")
