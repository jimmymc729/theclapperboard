#!/usr/bin/env python3
"""
Latest-trailers fetcher for The Clapperboard.

Unlike generate_post.py (which uses Claude to research and write listicle
copy), this script is pure TMDB — no LLM involved, nothing queued or
deduped. Every run just asks "what are the most popular movies currently in
theaters or coming soon, and does TMDB have a trailer for them?" and
overwrites content/trailers.json with a fresh snapshot of the answer.

That's what makes the homepage's "Latest Trailers" section always current
with zero manual upkeep: this runs before every automated site rebuild (see
.github/workflows/update.yml), so the list simply reflects whatever's
popular in theaters/upcoming on TMDB as of the most recent run — no
history, no old trailers lingering indefinitely once a movie's release
window has fully passed (see RECENT_WINDOW_DAYS below).

Usage:
    export TMDB_API_KEY=...
    python scripts/update_trailers.py
    python build_site.py
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "content" / "trailers.json"

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_POSTER_IMG = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_IMG = "https://image.tmdb.org/t/p/w780"

TARGET_COUNT = 20  # how many movies to keep on hand
PAGES_TO_SCAN = 5  # discover/movie pages to pull candidates from (20/page)
RECENT_WINDOW_DAYS = 60  # also include movies that opened up to this many days ago
MAX_VIDEOS_PER_MOVIE = 4  # a heavily-marketed movie can have a dozen+ clips on file;
                           # cap it so one movie's page doesn't turn into an endless list


def discover_movies(page: int) -> list:
    """Popularity-ranked movies that opened recently (still likely playing
    in theaters) or haven't opened yet. Using /discover (not /movie/upcoming
    or /movie/now_playing separately) so one popularity-sorted list covers
    both cases at once — the big movies people are actually talking about
    right now should surface first, whether that's because they just came
    out or because they're the most-anticipated thing still to come."""
    resp = requests.get(
        f"{TMDB_BASE}/discover/movie",
        params={
            "api_key": TMDB_API_KEY,
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "include_video": "false",
            "primary_release_date.gte": (date.today() - timedelta(days=RECENT_WINDOW_DAYS)).isoformat(),
            "region": "US",
            "page": page,
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def movie_trailers(movie_id: int) -> list:
    """Big releases regularly get several trailers over time (a teaser,
    then one or more full trailers, sometimes a "final trailer" right
    before release) — rather than picking just one and losing the rest,
    this returns a small, ordered list so a movie's own page can show all
    of them.

    Filtered to YouTube Trailers/Teasers only (skips things like
    behind-the-scenes clips or TV spots, which aren't really "the
    trailer"), newest-first by TMDB's published_at so a recently-released
    final trailer surfaces above an old teaser, then capped at
    MAX_VIDEOS_PER_MOVIE. Returns [] if TMDB has nothing usable on file yet
    (common for movies that are far out from release)."""
    resp = requests.get(
        f"{TMDB_BASE}/movie/{movie_id}/videos",
        params={"api_key": TMDB_API_KEY},
        timeout=20,
    )
    resp.raise_for_status()
    videos = resp.json().get("results", [])

    candidates = [
        v for v in videos
        if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser") and v.get("key")
    ]
    candidates.sort(key=lambda v: v.get("published_at") or "", reverse=True)

    return [
        {"key": v["key"], "name": v.get("name") or v["type"], "type": v["type"]}
        for v in candidates[:MAX_VIDEOS_PER_MOVIE]
    ]


def main():
    if not TMDB_API_KEY:
        sys.exit("Missing TMDB_API_KEY environment variable.")

    candidates = []
    for page in range(1, PAGES_TO_SCAN + 1):
        candidates.extend(discover_movies(page))

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
            videos = movie_trailers(movie_id)
        except requests.RequestException as e:
            print(f"  trailer lookup failed for '{movie.get('title')}': {e}")
            continue

        if not videos:
            continue  # no trailer on file yet — skip rather than show a dead card

        trailers.append({
            "id": movie_id,
            "title": movie.get("title", ""),
            "release_date": movie["release_date"],
            "poster": f"{TMDB_POSTER_IMG}{movie['poster_path']}",
            "backdrop": f"{TMDB_BACKDROP_IMG}{movie['backdrop_path']}" if movie.get("backdrop_path") else "",
            "overview": movie.get("overview", ""),
            "videos": videos,
        })

    trailers.sort(key=lambda t: t["release_date"])

    OUT_PATH.write_text(json.dumps(trailers, indent=2) + "\n")
    print(f"Wrote {len(trailers)} in-theaters/upcoming movie trailers to {OUT_PATH}")


if __name__ == "__main__":
    main()
