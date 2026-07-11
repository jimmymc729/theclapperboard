#!/usr/bin/env python3
"""
Latest-trailers fetcher for The Clapperboard.

Unlike generate_post.py (which uses Claude to research and write listicle
copy), this script is pure TMDB — no LLM involved, nothing queued or
deduped. Every run just asks "what are the most popular upcoming movies
right now, and does TMDB have a trailer for them?" and overwrites
content/trailers.json with a fresh snapshot of the answer.

That's what makes the homepage's "Latest Trailers" section always current
with zero manual upkeep: this runs before every automated site rebuild (see
.github/workflows/update.yml), so the list simply reflects whatever's
popular/upcoming on TMDB as of the most recent run — no history, no old
trailers lingering after a movie's released and stopped being "upcoming".

Usage:
    export TMDB_API_KEY=...
    python scripts/update_trailers.py
    python build_site.py
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "content" / "trailers.json"

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_POSTER_IMG = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_IMG = "https://image.tmdb.org/t/p/w780"

TARGET_COUNT = 12  # how many trailers to keep on hand
PAGES_TO_SCAN = 3  # discover/movie pages to pull candidates from (20/page)


def discover_upcoming(page: int) -> list:
    """Popularity-ranked movies releasing today or later. Using /discover
    (not /movie/upcoming) so results are ranked by popularity rather than
    just release-date order — the big anticipated releases should surface
    first, not whatever happens to open this exact week."""
    resp = requests.get(
        f"{TMDB_BASE}/discover/movie",
        params={
            "api_key": TMDB_API_KEY,
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "include_video": "false",
            "primary_release_date.gte": date.today().isoformat(),
            "region": "US",
            "page": page,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def official_youtube_trailer(movie_id: int) -> str:
    """Same preference order as generate_post.py's tmdb_movie_trailer:
    official YouTube trailer, then any YouTube trailer, then any YouTube
    video at all. Returns "" if TMDB has nothing usable on file yet (common
    for movies that are far out from release)."""
    resp = requests.get(
        f"{TMDB_BASE}/movie/{movie_id}/videos",
        params={"api_key": TMDB_API_KEY},
        timeout=20,
    )
    resp.raise_for_status()
    videos = resp.json().get("results", [])

    def pick(predicate):
        for v in videos:
            if predicate(v):
                return v.get("key", "")
        return ""

    return (
        pick(lambda v: v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("official"))
        or pick(lambda v: v.get("site") == "YouTube" and v.get("type") == "Trailer")
        or pick(lambda v: v.get("site") == "YouTube")
    )


def main():
    if not TMDB_API_KEY:
        sys.exit("Missing TMDB_API_KEY environment variable.")

    candidates = []
    for page in range(1, PAGES_TO_SCAN + 1):
        candidates.extend(discover_upcoming(page))

    trailers = []
    seen_ids = set()
    for movie in candidates:
        if len(trailers) >= TARGET_COUNT:
            break
        movie_id = movie.get("id")
        if not movie_id or movie_id in seen_ids:
            continue
        seen_ids.add(movie_id)

        if not movie.get("release_date") or not movie.get("poster_path"):
            continue  # not enough to show a usable card

        try:
            key = official_youtube_trailer(movie_id)
        except requests.RequestException as e:
            print(f"  trailer lookup failed for '{movie.get('title')}': {e}")
            continue

        if not key:
            continue  # no trailer on file yet — skip rather than show a dead card

        trailers.append({
            "id": movie_id,
            "title": movie.get("title", ""),
            "release_date": movie["release_date"],
            "poster": f"{TMDB_POSTER_IMG}{movie['poster_path']}",
            "backdrop": f"{TMDB_BACKDROP_IMG}{movie['backdrop_path']}" if movie.get("backdrop_path") else "",
            "overview": movie.get("overview", ""),
            "trailer_key": key,
        })

    trailers.sort(key=lambda t: t["release_date"])

    OUT_PATH.write_text(json.dumps(trailers, indent=2) + "\n")
    print(f"Wrote {len(trailers)} upcoming-movie trailers to {OUT_PATH}")


if __name__ == "__main__":
    main()
