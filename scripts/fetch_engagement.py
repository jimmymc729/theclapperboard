#!/usr/bin/env python3
"""
Engagement snapshot fetcher for The Clapperboard.

Pulls real traffic + engagement numbers straight from Google Analytics (GA4)
via its Data API, and writes content/engagement.json — a simple per-slug
score that build_site.py uses to power the site's "Trending" view. "Newest"
is always just publish-date order and needs no external data at all; this
script is what makes "Trending" mean something real instead of a fake or
manually-guessed ordering.

This is a separate script/step from generate_post.py and update_trailers.py
because it needs its own credential (a GA4 service account key, not the
TMDB/Anthropic keys those use) and is the one piece of the pipeline that
reads data BACK from the live site rather than writing new content to it.

Auth: needs a Google service account JSON key that's been added as a Viewer
on the GA4 property itself (GA4 Admin -> Property Access Management — this
is separate from any Google Cloud IAM role, which doesn't matter here). The
key's raw JSON contents go in the GA4_SERVICE_ACCOUNT_JSON env var as a
string (not a file path — GitHub Actions secrets are just text), parsed
in-memory; nothing ever touches disk.

Usage:
    export GA4_PROPERTY_ID=...
    export GA4_SERVICE_ACCOUNT_JSON='{"type": "service_account", ...}'
    python scripts/fetch_engagement.py
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "content" / "engagement.json"

GA4_PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID")
GA4_SERVICE_ACCOUNT_JSON = os.environ.get("GA4_SERVICE_ACCOUNT_JSON")

DATE_RANGE_DAYS = 30  # "Trending" reflects the last month, not all-time —
                       # otherwise an old post's lifetime pageviews would
                       # permanently bury anything newer no matter how well
                       # it's doing right now.

# How much each signal counts toward a post's trending score. Pageviews are
# the baseline (everyone who reads it counts a little); reactions and shares
# are weighted higher since they're active engagement — a visitor had to
# deliberately do something, not just land on the page. Easy to retune later
# without touching any of the query logic below.
WEIGHT_VIEWS = 1
WEIGHT_REACTIONS = 4
WEIGHT_SHARES = 6


def _client():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account

    info = json.loads(GA4_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(info)
    return BetaAnalyticsDataClient(credentials=credentials)


def _slug_from_path(path: str) -> str:
    """"/posts/some-slug/index.html" or "/posts/some-slug/" -> "some-slug".
    Anything that isn't a /posts/<slug>/... path (the homepage, /trailers/,
    quiz result sub-pages, etc.) returns "" and gets skipped — Newest/
    Trending only ever reorders top-level posts, so that's the only path
    shape worth scoring."""
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "posts":
        return parts[1]
    return ""


def fetch_pageviews(client) -> dict:
    from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="screenPageViews")],
        date_ranges=[DateRange(start_date=f"{DATE_RANGE_DAYS}daysAgo", end_date="today")],
        limit=10000,
    )
    resp = client.run_report(request)
    views = {}
    for row in resp.rows:
        slug = _slug_from_path(row.dimension_values[0].value)
        if not slug:
            continue
        views[slug] = views.get(slug, 0) + int(row.metric_values[0].value)
    return views


def fetch_event_counts(client, event_name: str) -> dict:
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Filter, FilterExpression, Metric, RunReportRequest,
    )

    request = RunReportRequest(
        property=f"properties/{GA4_PROPERTY_ID}",
        dimensions=[Dimension(name="pagePath")],
        metrics=[Metric(name="eventCount")],
        date_ranges=[DateRange(start_date=f"{DATE_RANGE_DAYS}daysAgo", end_date="today")],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="eventName",
                string_filter=Filter.StringFilter(value=event_name),
            )
        ),
        limit=10000,
    )
    resp = client.run_report(request)
    counts = {}
    for row in resp.rows:
        slug = _slug_from_path(row.dimension_values[0].value)
        if not slug:
            continue
        counts[slug] = counts.get(slug, 0) + int(row.metric_values[0].value)
    return counts


def main():
    if not GA4_PROPERTY_ID or not GA4_SERVICE_ACCOUNT_JSON:
        print(
            "GA4 credentials not configured — skipping engagement fetch. "
            "Trending will just fall back to Newest everywhere until "
            "GA4_PROPERTY_ID and GA4_SERVICE_ACCOUNT_JSON are set.",
            file=sys.stderr,
        )
        return

    try:
        client = _client()
        views = fetch_pageviews(client)
        reactions = fetch_event_counts(client, "reaction_click")
        shares = fetch_event_counts(client, "share_click")
    except Exception as e:
        # Best-effort, same philosophy as update_trailers.py: a GA4 hiccup
        # (rate limit, transient auth issue, etc.) should never break the
        # whole site rebuild. Leaves whatever engagement.json already
        # existed untouched rather than wiping it on a bad run.
        print(f"GA4 fetch failed, leaving engagement data as-is: {e}", file=sys.stderr)
        return

    slugs = set(views) | set(reactions) | set(shares)
    scores = {}
    for slug in slugs:
        v, r, s = views.get(slug, 0), reactions.get(slug, 0), shares.get(slug, 0)
        scores[slug] = {
            "views": v,
            "reactions": r,
            "shares": s,
            "score": v * WEIGHT_VIEWS + r * WEIGHT_REACTIONS + s * WEIGHT_SHARES,
        }

    OUT_PATH.write_text(json.dumps(scores, indent=2) + "\n")
    print(f"Wrote engagement data for {len(scores)} posts to {OUT_PATH}")


if __name__ == "__main__":
    main()
