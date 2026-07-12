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

TARGET_COUNT = 36  # how many movies to keep on hand
PAGES_TO_SCAN = 15  # discover/movie pages to pull candidates from (20/page) — wider
                   # than TARGET_COUNT alone would need, since MIN_UPCOMING below
                   # specifically needs a deep enough candidate pool to find that
                   # many upcoming movies, which rank lower in raw popularity than
                   # already-released ones (see MIN_UPCOMING comment) and so need
                   # more of the ranked list scanned to turn up at all.
RECENT_WINDOW_DAYS = 60  # also include movies that opened up to this many days ago
MAX_VIDEOS_PER_MOVIE = 4  # a heavily-marketed movie can have a dozen+ clips on file;
                           # cap it so one movie's page doesn't turn into an endless list
MIN_UPCOMING = 16  # guaranteed minimum still-unreleased movies out of TARGET_COUNT.
                   # A pure popularity.desc pass systematically starves "Coming Soon"
                   # — attention/searches spike once a movie is actually out, so an
                   # upcoming movie rarely out-popularity-ranks something currently
                   # in theaters no matter how anticipated it eventually becomes.
                   # Left alone, this meant the site's "Coming Soon" trailer tab
                   # stayed thin even when plenty of upcoming movies had trailers on
                   # file — they just never survived a straight popularity cut.


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
    trailer"). Sorting is NOT simply "newest first": once a movie's real
    trailer is out, TMDB often accumulates a pile of smaller promotional
    clips tagged "Teaser" (character spots, "in cinemas" bumpers, etc.)
    that get logged even more recently than the actual flagship trailer —
    a pure recency sort let those crowd the real trailer out past
    MAX_VIDEOS_PER_MOVIE entirely. So this sorts on two passes instead:
    first by published_at (newest first, as the tiebreaker), then — since
    Python's sort is stable — by official flag and type (official "Trailer"
    entries always float to the front, ahead of teasers/promo clips
    regardless of which was logged more recently). Returns [] if TMDB has
    nothing usable on file yet (common for movies far out from release)."""
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
    candidates.sort(key=lambda v: (0 if v.get("official") else 1, 0 if v.get("type") == "Trailer" else 1))

    return [
        {"key": v["key"], "name": v.get("name") or v["type"], "type": v["type"]}
        for v in candidates[:MAX_VIDEOS_PER_MOVIE]
    ]


def is_upcoming(movie: dict) -> bool:
    """A raw TMDB /discover result is "upcoming" if its release date is
    still in the future — same released-vs-upcoming split build_site.py's
    is_upcoming()/theater_status_pill() apply to the resolved trailer
    records, just working off the raw movie dict before it's been turned
    into one."""
    release = movie.get("release_date")
    if not release:
        return False
    try:
        return date.fromisoformat(release) > date.today()
    except ValueError:
        return False


def main():
    if not TMDB_API_KEY:
        sys.exit("Missing TMDB_API_KEY environment variable.")

    candidates = []
    for page in range(1, PAGES_TO_SCAN + 1):
        candidates.extend(discover_movies(page))

    resolved = {}  # movie_id -> trailer dict, in the order each was resolved
    seen_ids = set()

    def try_resolve(movie: dict):
        """Resolve one /discover result into a trailer record, or None if
        it's not usable (missing basics, or TMDB has no trailer on file
        yet). Marks the id seen either way so a second pass over the same
        candidate list never re-attempts (or double-counts) it."""
        movie_id = movie.get("id")
        if not movie_id or movie_id in seen_ids:
            return None
        seen_ids.add(movie_id)

        if not movie.get("release_date") or not movie.get("poster_path"):
            return None  # not enough to show a usable card

        try:
            videos = movie_trailers(movie_id)
        except requests.RequestException as e:
            print(f"  trailer lookup failed for '{movie.get('title')}': {e}")
            return None

        if not videos:
            return None  # no trailer on file yet — skip rather than show a dead card

        return {
            "id": movie_id,
            "title": movie.get("title", ""),
            "release_date": movie["release_date"],
            "poster": f"{TMDB_POSTER_IMG}{movie['poster_path']}",
            "backdrop": f"{TMDB_BACKDROP_IMG}{movie['backdrop_path']}" if movie.get("backdrop_path") else "",
            "overview": movie.get("overview", ""),
            "videos": videos,
        }

    # Pass 1: guarantee MIN_UPCOMING upcoming-movie slots first — see the
    # comment on MIN_UPCOMING above for why a plain popularity pass alone
    # would otherwise crowd these out almost entirely.
    for movie in candidates:
        if len(resolved) >= MIN_UPCOMING:
            break
        if not is_upcoming(movie):
            continue
        trailer = try_resolve(movie)
        if trailer:
            resolved[trailer["id"]] = trailer

    upcoming_reserved = len(resolved)

    # Pass 2: fill the remaining slots from the full popularity-ranked
    # list (released or upcoming) — same selection as before this fix,
    # just picking up wherever pass 1 left off (seen_ids already skips
    # anything pass 1 added or already ruled out).
    for movie in candidates:
        if len(resolved) >= TARGET_COUNT:
            break
        trailer = try_resolve(movie)
        if trailer:
            resolved[trailer["id"]] = trailer

    # The two passes above are only about WHICH movies make the cut — the
    # actual output order is re-sorted back to TMDB's own popularity.desc
    # ranking, so "All" and the homepage shelf still read biggest/
    # most-talked-about-first exactly like before, regardless of which
    # pass happened to pick up a given movie.
    popularity_rank = {m.get("id"): i for i, m in enumerate(candidates)}
    trailers = sorted(
        resolved.values(),
        key=lambda t: popularity_rank.get(t["id"], len(candidates)),
    )

    print(f"Reserved {upcoming_reserved} upcoming-movie slot(s) for Coming Soon, "
          f"{len(trailers) - upcoming_reserved} filled by overall popularity.")
    OUT_PATH.write_text(json.dumps(trailers, indent=2) + "\n")
    print(f"Wrote {len(trailers)} in-theaters/upcoming movie trailers to {OUT_PATH}")


if __name__ == "__main__":
    main()
