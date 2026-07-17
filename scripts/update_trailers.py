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

TARGET_COUNT = 150  # how many movies to keep on hand. Bumped up again from 70 —
                   # this is now a genuinely large library rather than a curated
                   # top-N, so the pool sizes/reserved-slot counts below all had
                   # to scale up proportionally too, not just this one number.
PAGES_TO_SCAN = 50  # discover/movie pages to pull candidates from (20/page) —
                   # 1,000 candidates deep, comfortably enough headroom for
                   # MIN_UPCOMING at its new size (see below) to actually find
                   # that many usable upcoming movies rather than running out
                   # of pages before hitting the target.
RECENT_WINDOW_DAYS = 60  # also include movies that opened up to this many days ago
MAX_VIDEOS_PER_MOVIE = 4  # a heavily-marketed movie can have a dozen+ clips on file;
                           # cap it so one movie's page doesn't turn into an endless list
MIN_UPCOMING = 45  # guaranteed minimum still-unreleased movies out of TARGET_COUNT.
                   # A pure popularity.desc pass systematically starves "Coming Soon"
                   # — attention/searches spike once a movie is actually out, so an
                   # upcoming movie rarely out-popularity-ranks something currently
                   # in theaters no matter how anticipated it eventually becomes.
                   # Left alone, this meant the site's "Coming Soon" trailer tab
                   # stayed thin even when plenty of upcoming movies had trailers on
                   # file — they just never survived a straight popularity cut.

# Major studios get their own guaranteed-slot pass (MIN_MAJOR_STUDIO below) —
# same "reserve slots so the popularity algorithm can't starve a whole
# category" trick as MIN_UPCOMING, just applied to studio pedigree instead of
# release timing. Names are resolved to TMDB company IDs at runtime (see
# resolve_company_ids()) rather than hardcoded numeric IDs, which would be
# unverifiable from memory and silently wrong if mistyped. Mostly the classic
# major Hollywood studios, plus a handful of prestige studios (A24, Focus
# Features, Searchlight, Neon) that reliably put out high-quality work even
# when it's not blockbuster-scale — "major studio" here means "trustworthy
# quality signal," not strictly "biggest box office."
MAJOR_STUDIOS = [
    "Walt Disney Pictures",
    "Marvel Studios",
    "Pixar",
    "Warner Bros. Pictures",
    "Universal Pictures",
    "Paramount Pictures",
    "Columbia Pictures",
    "20th Century Studios",
    "Lionsgate",
    "New Line Cinema",
    "Legendary Pictures",
    "DreamWorks Animation",
    "Metro-Goldwyn-Mayer",
    "Amblin Entertainment",
    "A24",
    "Focus Features",
    "Searchlight Pictures",
    "Neon",
]
MIN_MAJOR_STUDIO = 60  # guaranteed minimum major-studio movies out of TARGET_COUNT
MAJOR_STUDIO_PAGES_TO_SCAN = 15  # the studio-filtered candidate pool is much smaller
                                # than the general one, so it needs far fewer pages
                                # to comfortably find MIN_MAJOR_STUDIO usable results


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


def resolve_company_ids(names: list) -> list:
    """Resolves studio names (see MAJOR_STUDIOS) to TMDB company IDs via
    /search/company, run once per script run. Deliberately not a hardcoded
    ID list — those numeric IDs aren't something to eyeball-verify from
    memory, and a wrong one would silently just return zero results rather
    than erroring, which could go unnoticed indefinitely. Best-effort per
    name: if one studio fails to resolve (typo, rename, API hiccup), it's
    logged and skipped rather than failing the whole run over it."""
    ids = []
    for name in names:
        try:
            resp = requests.get(
                f"{TMDB_BASE}/search/company",
                params={"api_key": TMDB_API_KEY, "query": name},
                timeout=20,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.RequestException as e:
            print(f"  company lookup failed for '{name}': {e}")
            continue
        if results:
            ids.append(results[0]["id"])
        else:
            print(f"  no TMDB company match for '{name}' — skipping")
    return ids


def discover_by_companies(company_ids: list, page: int) -> list:
    """Same popularity/recency/region filters as discover_movies() above,
    but restricted to a specific set of studios via TMDB's with_companies
    filter — pipe-separated means OR (matches ANY one of these studios),
    not AND, which is what actually makes this "major studio OR major
    studio OR ..." rather than requiring a movie be made by all of them at
    once. This is the piece that actually guarantees studio coverage (see
    MIN_MAJOR_STUDIO) instead of just hoping a plain popularity sort
    happens to surface enough of them on its own."""
    resp = requests.get(
        f"{TMDB_BASE}/discover/movie",
        params={
            "api_key": TMDB_API_KEY,
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "include_video": "false",
            "primary_release_date.gte": (date.today() - timedelta(days=RECENT_WINDOW_DAYS)).isoformat(),
            "region": "US",
            "with_companies": "|".join(str(c) for c in company_ids),
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


def has_us_theatrical_release(movie_id: int) -> bool:
    """TMDB's /discover/movie only returns ONE release date per movie
    (whichever TMDB considers "primary"), which is very often the streaming/
    digital date for a straight-to-streaming release, not a theatrical one —
    so a purely popularity + release-date-based pick can easily surface
    something that never played in a theater at all, and the site's own
    "In Theaters Now" pill would then just be wrong for it. This checks the
    real US release_dates list and looks for an actual theatrical entry —
    TMDB's `type` field is 2 (limited theatrical) or 3 (wide theatrical);
    4/5/6 are digital/physical/TV, which don't count here regardless of
    how official-looking the trailer is. Defaults to False (treated as
    non-theatrical / "Streaming Now") if the lookup fails or TMDB simply
    has no US release_dates on file — safer to under-claim "in theaters"
    than to keep mislabeling streaming-only titles."""
    try:
        resp = requests.get(
            f"{TMDB_BASE}/movie/{movie_id}/release_dates",
            params={"api_key": TMDB_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return False

    for entry in resp.json().get("results", []):
        if entry.get("iso_3166_1") != "US":
            continue
        return any(rd.get("type") in (2, 3) for rd in entry.get("release_dates", []))
    return False


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

    major_company_ids = resolve_company_ids(MAJOR_STUDIOS)
    major_candidates = []
    if major_company_ids:
        for page in range(1, MAJOR_STUDIO_PAGES_TO_SCAN + 1):
            major_candidates.extend(discover_by_companies(major_company_ids, page))
    else:
        print("  no major-studio company IDs resolved — skipping that pass entirely")

    # Real TMDB popularity score per movie, gathered from whichever list(s)
    # it showed up in — used for the final output ordering below instead of
    # each candidate list's own index, since major_candidates and candidates
    # are two DIFFERENT filtered universes and a movie's position in one
    # doesn't mean the same thing as its position in the other.
    popularity_score = {}
    for m in candidates + major_candidates:
        if m.get("id") is not None:
            popularity_score[m["id"]] = m.get("popularity", 0)

    resolved = {}  # movie_id -> trailer dict, in the order each was resolved
    seen_ids = set()

    def try_resolve(movie: dict):
        """Resolve one /discover result into a trailer record, or None if
        it's not usable (missing basics, or TMDB has no trailer on file
        yet). Marks the id seen either way so a later pass over any
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

        # Only worth checking for movies that have actually opened — an
        # upcoming movie can't have a theatrical release on file yet either
        # way, and build_site.py's pill only reads this field once a movie's
        # release date has passed (upcoming ones always show "Coming
        # {date}" regardless), so skipping the extra API call here for
        # every still-upcoming candidate is free.
        theatrical = True
        if not is_upcoming(movie):
            theatrical = has_us_theatrical_release(movie_id)

        return {
            "id": movie_id,
            "title": movie.get("title", ""),
            "release_date": movie["release_date"],
            "poster": f"{TMDB_POSTER_IMG}{movie['poster_path']}",
            "backdrop": f"{TMDB_BACKDROP_IMG}{movie['backdrop_path']}" if movie.get("backdrop_path") else "",
            "overview": movie.get("overview", ""),
            "theatrical": theatrical,
            "videos": videos,
        }

    # Counters tracked across ALL passes (not just their "own" pass) — a
    # movie the major-studio pass happens to pick up that's also upcoming
    # should count toward MIN_UPCOMING too, otherwise the two reservations
    # would double-count the same slots against TARGET_COUNT.
    major_resolved = 0
    upcoming_resolved = 0

    # Pass 1: guarantee MIN_MAJOR_STUDIO major-studio slots first — this is
    # the actual fix for "more trailers, but keep them high quality": a
    # dedicated studio-filtered candidate pool that doesn't have to compete
    # against the raw popularity chart to get in.
    for movie in major_candidates:
        if major_resolved >= MIN_MAJOR_STUDIO:
            break
        trailer = try_resolve(movie)
        if trailer:
            resolved[trailer["id"]] = trailer
            major_resolved += 1
            if is_upcoming(movie):
                upcoming_resolved += 1

    # Pass 2: top up MIN_UPCOMING upcoming-movie slots from the general pool
    # — see the comment on MIN_UPCOMING above for why a plain popularity
    # pass alone would otherwise crowd these out almost entirely. Only tops
    # up the REMAINING gap, since pass 1 may already have resolved some
    # upcoming major-studio movies that count toward this same total.
    for movie in candidates:
        if upcoming_resolved >= MIN_UPCOMING:
            break
        if not is_upcoming(movie):
            continue
        trailer = try_resolve(movie)
        if trailer:
            resolved[trailer["id"]] = trailer
            upcoming_resolved += 1

    # Pass 3: fill the remaining slots from the full popularity-ranked
    # list (released or upcoming) — same selection as before this fix,
    # just picking up wherever passes 1-2 left off (seen_ids already skips
    # anything already added or already ruled out).
    for movie in candidates:
        if len(resolved) >= TARGET_COUNT:
            break
        trailer = try_resolve(movie)
        if trailer:
            resolved[trailer["id"]] = trailer

    # The passes above are only about WHICH movies make the cut — the actual
    # output order is re-sorted back by real TMDB popularity score, so "All"
    # and the homepage shelf still read biggest/most-talked-about-first
    # regardless of which pass happened to pick up a given movie.
    trailers = sorted(
        resolved.values(),
        key=lambda t: popularity_score.get(t["id"], 0),
        reverse=True,
    )

    print(f"Reserved {major_resolved} major-studio slot(s), "
          f"{upcoming_resolved} upcoming-movie slot(s) total (may overlap with major-studio), "
          f"{len(trailers) - major_resolved} filled beyond the major-studio pass.")
    OUT_PATH.write_text(json.dumps(trailers, indent=2) + "\n")
    print(f"Wrote {len(trailers)} in-theaters/upcoming movie trailers to {OUT_PATH}")


if __name__ == "__main__":
    main()
