#!/usr/bin/env python3
"""
Listicle generator for The Clapperboard.

For every idea in post_ideas.txt that doesn't already have a JSON file in
content/posts/, this script:

  1. Calls the Claude API (with the web_search tool) to research the topic
     and write the listicle copy — headings, captions, quotes, sources —
     but instructs the model to name TMDB lookups (people and/or movies) for
     each item's images, rather than inventing image URLs itself. Each item
     can request multiple images (e.g. an actor's headshot alongside a still
     from the movie in question), which render side by side.
  2. Looks each of those names up on TMDB and pulls real, working image URLs
     from TMDB's CDN — actor headshots, and movie BACKDROPS (real stills
     from the film) rather than poster art, since posters are key art, not
     an actual frame from the movie.

     TMDB has no metadata describing WHAT a given backdrop shows, so for
     movie stills this also uses Claude's vision: it downloads several
     candidate backdrops for the film and asks Claude to pick whichever one
     actually depicts the fact/scene the item is about, instead of just
     grabbing the first (or most-voted) one and hoping it's relevant. This
     adds one extra (cheap, low-token) API call per movie-still lookup.
  3. For "guess the movie" Games items, also fetches the movie's official
     trailer from TMDB's /videos endpoint (a pointer to a YouTube video,
     not a downloaded file) and embeds it as a payoff shown after the
     viewer reveals the answer.
  4. Assembles the final post JSON (matching the schema build_site.py
     expects) and writes it to content/posts/<slug>.json.

Images are never cropped in the rendered site (see assets/style.css), so
this script doesn't need to worry about aspect ratios — whatever TMDB
returns is shown at its natural size.

This only writes data files. Run build_site.py afterward (or let the GitHub
Actions workflow do it) to turn them into actual HTML pages.

Usage:
    export TMDB_API_KEY=...
    export ANTHROPIC_API_KEY=...
    pip install -r requirements.txt
    python scripts/generate_post.py
    python build_site.py

Note on images: all images come from TMDB's CDN. Per TMDB's terms of use,
any site using their API/images must display attribution (already in this
site's footer) — see https://www.themoviedb.org/documentation/api/terms-of-use
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content" / "posts"
IDEAS_PATH = Path(__file__).resolve().parent / "post_ideas.txt"

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_PERSON_IMG = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_IMG = "https://image.tmdb.org/t/p/w780"
TMDB_BACKDROP_THUMB = "https://image.tmdb.org/t/p/w300"  # small, cheap to send to vision
TMDB_POSTER_IMG = "https://image.tmdb.org/t/p/w500"

MODEL = "claude-sonnet-5"
MAX_VISION_CANDIDATES = 6  # how many backdrop candidates to show Claude per lookup
POSTS_PER_RUN = 2  # cap on how many new posts a single run will generate/spend API budget on

# Subjects that have been observed to get higher engagement on social (e.g. Twitter/X)
# when they're the focus of a post. The self-directed topic generator is biased toward
# working these in — as a facts listicle, a couple/relationship angle, a guessing game,
# or tying into one of their real upcoming projects — without ever forcing it or making
# every post about them. Add/remove names here as performance data changes; no other
# code needs to change.
TRENDING_SUBJECTS = ["Tom Holland", "Zendaya"]


def parse_ideas():
    ideas = []
    for line in IDEAS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            print(f"Skipping malformed idea line: {line}", file=sys.stderr)
            continue
        slug, category, instructions = parts
        ideas.append({"slug": slug, "category": category, "instructions": instructions})
    return ideas


def load_existing_post_titles() -> list:
    """Titles (not just slugs) of everything already published, so the
    self-directed brainstorming step can avoid inventing something that's a
    near-duplicate of an existing post, not just an exact slug collision."""
    titles = []
    for f in CONTENT_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            titles.append(data.get("title", f.stem))
        except (json.JSONDecodeError, OSError):
            continue
    return titles


TOPIC_BRAINSTORM_PROMPT = """You invent fresh, viral BuzzFeed-style movie/celebrity listicle topics \
for a site called The Clapperboard, a companion site to Flickle (a daily movie-guessing game) — \
every post ends with a "play Flickle" call to action, so movie-literate, guessable topics work best.

You'll be given a list of titles already published on the site. Invent {count} BRAND NEW topic \
ideas that are NOT duplicates or close variations of anything in that list (don't just reuse the \
same actors/movies/angle with a different number).

Rotate across these categories:
- Actors: casting stories, actor facts, on-set anecdotes (numbered image+text listicle format)
- Movies: behind-the-scenes facts, trivia, rankings, production stories (numbered image+text format)
- Games: "guess the movie" — pick EITHER an emoji-clue format OR a famous-quote format per idea

Every idea must be the kind of real, well-documented, fact-checkable topic a researcher could \
actually verify with web search — not vague, not unfalsifiable, not about living people's private \
lives beyond publicly reported career/casting history.

Trending subjects: {trending_subjects} have measurably driven higher social engagement for this \
site recently. Bias roughly 1 in every 3-4 ideas toward one of them where it's a natural fit — a \
facts listicle about them, a real/public relationship or career-timeline angle, tying into one of \
their genuinely upcoming or recent projects, or a guessing game built from their filmography. \
Never force it into an idea it doesn't fit, never invent private details, and don't repeat an \
angle already covered in the existing titles below.

Return ONLY a JSON array, no prose, no markdown fences:
[
  {{
    "slug": "kebab-case-unique-slug",
    "category": "Actors" or "Movies" or "Games",
    "instructions": "plain-English description of exactly what the post should cover, specific \
enough to research — e.g. how many items, what kind of facts, which format for Games"
  }},
  ...
]"""


def generate_new_topic_ideas(existing_titles: list, count: int) -> list:
    """Ask Claude to invent `count` brand-new listicle topics, checking
    against what's already published so the self-directed pipeline doesn't
    just repeat itself once post_ideas.txt runs dry."""
    client = anthropic.Anthropic()
    titles_blob = "\n".join(f"- {t}" for t in existing_titles) or "(nothing published yet)"
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=TOPIC_BRAINSTORM_PROMPT.format(
            count=count,
            trending_subjects=", ".join(TRENDING_SUBJECTS) or "(none set)",
        ),
        messages=[{
            "role": "user",
            "content": f"Already published:\n{titles_blob}\n\nInvent {count} new topic ideas now.",
        }],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("No JSON array found in topic brainstorm output")
    ideas = json.loads(text[start:end + 1])
    return [
        {"slug": i["slug"], "category": i["category"], "instructions": i["instructions"]}
        for i in ideas
        if i.get("slug") and i.get("category") and i.get("instructions")
    ]


def tmdb_person_image(name: str, used_images: set = None, max_candidates: int = 6) -> str:
    """Return a photo of this person, preferring one that isn't already used
    elsewhere on the site. A plain /search/person lookup only ever returns
    that person's single "primary" profile photo, so a subject who comes up
    across multiple posts (or multiple items in the same post — e.g. several
    facts with no specific movie tied to them) would otherwise get the exact
    same headshot every single time. Pulling the full /images list and
    picking around whatever's already used avoids that repetition without
    needing scene-specific vision matching the way movie stills do."""
    used_images = used_images if used_images is not None else set()
    resp = requests.get(
        f"{TMDB_BASE}/search/person",
        params={"api_key": TMDB_API_KEY, "query": name},
        timeout=20,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results or not results[0].get("id"):
        return ""

    images_resp = requests.get(
        f"{TMDB_BASE}/person/{results[0]['id']}/images",
        params={"api_key": TMDB_API_KEY},
        timeout=20,
    )
    images_resp.raise_for_status()
    profiles = images_resp.json().get("profiles", [])
    if not profiles:
        # Dedicated images endpoint came back empty — fall back to whatever
        # /search/person itself returned rather than dropping the image.
        profile_path = results[0].get("profile_path")
        return f"{TMDB_PERSON_IMG}{profile_path}" if profile_path else ""

    profiles.sort(key=lambda p: p.get("vote_count", 0), reverse=True)
    candidates = [f"{TMDB_PERSON_IMG}{p['file_path']}" for p in profiles[:max_candidates]]

    for url in candidates:
        if url not in used_images:
            return url
    return candidates[0]  # every candidate already used somewhere — best available fallback


def tmdb_movie_trailer(title: str, year: int = None) -> str:
    """Return a YouTube video key for the movie's official trailer, if TMDB
    has one on file. Falls back to any YouTube trailer, then any YouTube
    video at all, before giving up. Used for the "reveal" payoff on
    guess-the-movie Games posts — not fetched for regular Actors/Movies
    posts, since those don't have a single obvious video to attach."""
    params = {"api_key": TMDB_API_KEY, "query": title}
    if year:
        params["year"] = year
    resp = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=20)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return ""

    videos_resp = requests.get(
        f"{TMDB_BASE}/movie/{results[0]['id']}/videos",
        params={"api_key": TMDB_API_KEY},
        timeout=20,
    )
    videos_resp.raise_for_status()
    videos = videos_resp.json().get("results", [])

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


def tmdb_backdrop_candidates(title: str, year: int = None) -> tuple:
    """Return (poster_path, [candidate backdrop file_paths]) for a movie,
    sorted by TMDB's own vote_average (a rough proxy for "this is one of the
    more notable/iconic stills" rather than an arbitrary upload order)."""
    params = {"api_key": TMDB_API_KEY, "query": title}
    if year:
        params["year"] = year
    resp = requests.get(f"{TMDB_BASE}/search/movie", params=params, timeout=20)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return None, []

    movie = results[0]
    poster_path = movie.get("poster_path")

    images_resp = requests.get(
        f"{TMDB_BASE}/movie/{movie['id']}/images",
        params={"api_key": TMDB_API_KEY},
        timeout=20,
    )
    images_resp.raise_for_status()
    backdrops = images_resp.json().get("backdrops", [])
    # Drop anything that's secretly just the poster art re-uploaded as a backdrop.
    backdrops = [b for b in backdrops if b.get("file_path") and b["file_path"] != poster_path]
    backdrops.sort(key=lambda b: b.get("vote_average", 0), reverse=True)

    if not backdrops and movie.get("backdrop_path"):
        return poster_path, [movie["backdrop_path"]]
    return poster_path, [b["file_path"] for b in backdrops[:MAX_VISION_CANDIDATES]]


def choose_backdrop_with_vision(candidates: list, movie_title: str, context_text: str) -> str:
    """Ask Claude (with real vision, via the API) to look at several actual
    still-frame candidates and pick whichever one genuinely illustrates the
    specific fact/scene being described — rather than blindly taking
    whatever TMDB happens to list first. Falls back to the first candidate
    if the vision call fails or the reply can't be parsed."""
    if len(candidates) <= 1:
        return candidates[0] if candidates else ""

    content = [{
        "type": "text",
        "text": (
            f"Movie: {movie_title}\n"
            f"Fact/scene being illustrated: {context_text}\n\n"
            f"Below are {len(candidates)} candidate stills from this film, numbered 1-{len(candidates)}. "
            "Reply with ONLY the number of the single image that most specifically and accurately "
            "depicts the fact/scene above. If none of them clearly show that specific scene, reply "
            "with the number of the most visually striking/representative still instead. Reply with "
            "just the number, nothing else."
        ),
    }]
    for i, file_path in enumerate(candidates, start=1):
        try:
            img_bytes = requests.get(f"{TMDB_BACKDROP_THUMB}{file_path}", timeout=20).content
        except requests.RequestException:
            continue
        content.append({"type": "text", "text": f"Image {i}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(img_bytes).decode("ascii"),
            },
        })

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": content}],
        )
        reply = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        match = re.search(r"\d+", reply)
        if match:
            idx = int(match.group()) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
    except Exception as e:
        print(f"    vision selection failed, using first candidate: {e}")

    return candidates[0]


def tmdb_movie_image(title: str, year: int = None, context_text: str = None) -> str:
    """Prefer a real backdrop (an actual still from the film) over poster
    art, since posters are promotional key art rather than a movie frame.

    When `context_text` is given (the specific fact this image needs to
    illustrate), this fetches several candidate backdrops and uses Claude's
    vision to pick whichever one actually matches — TMDB has no metadata
    tagging what a backdrop depicts, so this is the only reliable way to do
    better than "grab the first/most-voted one and hope.\""""
    poster_path, candidates = tmdb_backdrop_candidates(title, year)
    if not candidates:
        return f"{TMDB_POSTER_IMG}{poster_path}" if poster_path else ""

    if context_text:
        chosen = choose_backdrop_with_vision(candidates, title, context_text)
    else:
        chosen = candidates[0]
    return f"{TMDB_BACKDROP_IMG}{chosen}"


def resolve_lookup(lookup: dict, context_text: str = None, used_images: set = None) -> str:
    lookup_type = lookup.get("lookup_type")
    lookup_name = lookup.get("lookup_name")
    if not lookup_type or not lookup_name:
        return ""
    if lookup_type == "person":
        return tmdb_person_image(lookup_name, used_images)
    if lookup_type == "movie":
        return tmdb_movie_image(lookup_name, lookup.get("lookup_year"), context_text)
    return ""


SYSTEM_PROMPT = """You are a viral entertainment writer for a BuzzFeed-style movie/celebrity site. \
Use the web_search tool to research the topic you're given, then produce ONLY a JSON object (no \
prose, no markdown fences) with this exact shape:

{
  "title": "punchy clickbait-style headline for the whole post",
  "dek": "one-sentence teaser subheading",
  "items": [
    {
      "number": 1,
      "heading": "punchy headline for this specific item",
      "text": "2-3 sentence caption, factual, your own wording, not copied verbatim from a source",
      "lookups": [
        { "lookup_type": "person", "lookup_name": "exact full actor name", "caption": "short caption for this image" },
        { "lookup_type": "movie", "lookup_name": "exact movie title", "lookup_year": 1999, "caption": "short caption for this image" }
      ]
    },
    ...
  ],
  "sources": [ { "title": "...", "url": "..." }, ... ]
}

Include 1-3 lookups per item, but only when each one is a genuinely distinct subject — e.g. an
actor's photo alongside a still from the movie they were considered for. Do NOT pair two images of
the exact same single subject (like a poster and a still from the same movie with no second
subject) just to fill space; a single strong image is better than two redundant ones.

If the topic is specifically about upcoming/anticipated movies (not yet released, or recently
released), add "include_trailer": true to that item's movie lookup so its official trailer gets
embedded below the images. Don't add this for topics where a trailer isn't the point (e.g. a
casting-history or behind-the-scenes post about an older film).

If the topic is a "guess the movie from emoji" format, use this item shape instead:

{
  "number": 1,
  "emoji": "three emoji that clue the movie without giving it away outright",
  "reveal_title": "Movie Title (Year)",
  "lookups": [
    { "lookup_type": "movie", "lookup_name": "exact movie title", "lookup_year": 1999 }
  ],
  "reveal_text": "the movie's most famous quote"
}

If the topic is a "guess the movie from its quote" format, use this item shape instead — note the
clue here IS the movie's famous quote, so don't also repeat it in reveal_text:

{
  "number": 1,
  "quote": "the exact famous line, without you naming the movie or character",
  "reveal_title": "Movie Title (Year)",
  "lookups": [
    { "lookup_type": "movie", "lookup_name": "exact movie title", "lookup_year": 1999 }
  ]
}

Rules:
- Only include real, verifiable facts and quotes. If you can't verify something, leave it out
  rather than guessing or inventing it.
- lookup_name must be a real, exact, searchable name (full actor name, or exact movie title) —
  this is used to fetch a real photo, so precision matters more than style here.
- Do not invent image URLs yourself; that's handled separately from what you return.
- Output nothing before or after the JSON object."""


def claude_generate(category: str, instructions: str) -> dict:
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Category: {category}\nTopic: {instructions}\n\nResearch it and return the JSON object now.",
        }],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start:end + 1])


def resolve_item_images(item: dict, used_images: set) -> list:
    """Turn an item's `lookups` list into a list of {"url", "caption", "type"}
    dicts, silently dropping any lookup that doesn't resolve to a real image.
    `used_images` is shared across the whole run (and seeded from every
    image already published on the site) so a subject who comes up more
    than once — within this item, this post, or a different post entirely —
    doesn't just get the same photo repeated at every single turn.

    `type` (the original lookup_type, "person" or "movie") rides along so
    `main()` can pick a landscape movie still as the post's cover image
    instead of a portrait actor headshot — it's stripped back out before
    the image dict is written to the post JSON, since build_site.py's
    schema only expects "url" and "caption"."""
    # Use the item's own fact/heading/quote as the context vision uses to
    # judge which candidate still actually matches.
    context_text = (
        item.get("text") or item.get("heading") or item.get("quote") or item.get("reveal_text", "")
    )

    resolved = []
    for lookup in item.get("lookups", []):
        url = resolve_lookup(lookup, context_text, used_images)
        if url:
            resolved.append({
                "url": url,
                "caption": lookup.get("caption", ""),
                "type": lookup.get("lookup_type", ""),
            })
            used_images.add(url)
        else:
            print(f"    no image found for lookup '{lookup.get('lookup_name')}'")
    return resolved


def load_used_images() -> set:
    """Every image URL already published anywhere on the site — cover
    images plus every item/reveal image — so newly generated posts avoid
    repeating a photo that's already showing up elsewhere."""
    used = set()
    for f in CONTENT_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("cover_image"):
            used.add(data["cover_image"])
        for item in data.get("items", []):
            for img in item.get("images", []):
                if img.get("url"):
                    used.add(img["url"])
            if item.get("reveal_image"):
                used.add(item["reveal_image"])
    return used


def main():
    if not TMDB_API_KEY:
        sys.exit("TMDB_API_KEY is not set.")

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    existing_slugs = {p.stem for p in CONTENT_DIR.glob("*.json")}

    # Seeded from every image already published, then added to as this run
    # resolves its own new images — so repeats are caught both against the
    # rest of the site and against earlier posts generated in this same run.
    used_images = load_used_images()

    # 1. Use any ideas already queued in post_ideas.txt first.
    pending = [i for i in parse_ideas() if i["slug"] not in existing_slugs]

    # 2. Top up with self-directed ideas (checked against published titles to
    #    avoid repeats) if the queue doesn't cover this run's target.
    if len(pending) < POSTS_PER_RUN:
        needed = POSTS_PER_RUN - len(pending)
        print(f"post_ideas.txt has {len(pending)} pending — brainstorming {needed} new topic(s)...")
        try:
            existing_titles = load_existing_post_titles()
            new_ideas = generate_new_topic_ideas(existing_titles, needed)
            # Guard against a self-generated slug accidentally colliding with
            # something already published or already queued this run.
            seen_slugs = existing_slugs | {i["slug"] for i in pending}
            new_ideas = [i for i in new_ideas if i["slug"] not in seen_slugs]
            pending.extend(new_ideas)

            if new_ideas:
                with IDEAS_PATH.open("a") as f:
                    f.write(f"\n# --- self-generated {datetime.now(timezone.utc).strftime('%Y-%m-%d')} ---\n")
                    for i in new_ideas:
                        f.write(f"{i['slug']} | {i['category']} | {i['instructions']}\n")
                print(f"  added {len(new_ideas)} self-generated idea(s) to post_ideas.txt")
        except Exception as e:
            print(f"  topic brainstorm failed, continuing with what's queued: {e}", file=sys.stderr)

    processed = 0
    for idea in pending:
        if processed >= POSTS_PER_RUN:
            print(f"Reached POSTS_PER_RUN cap ({POSTS_PER_RUN}) — stopping for this run.")
            break

        slug = idea["slug"]
        if slug in existing_slugs:
            print(f"skip (exists): {slug}")
            continue

        print(f"processing: {slug}")
        try:
            draft = claude_generate(idea["category"], idea["instructions"])

            resolved_items = []
            cover_candidates = []  # every resolved {"url","caption","type"} in this post, in order
            for item in draft.get("items", []):
                images = resolve_item_images(item, used_images)
                if not images:
                    print(f"  no images resolved for item {item.get('number')}, skipping item")
                    continue
                cover_candidates.extend(images)
                plain_images = [{"url": i["url"], "caption": i["caption"]} for i in images]

                clean = {k: v for k, v in item.items() if k != "lookups"}
                if "emoji" in item or "quote" in item:
                    clean["reveal_image"] = plain_images[0]["url"]
                    # Games items reveal a single movie — attach its trailer
                    # as a bonus payoff after the viewer guesses correctly.
                    movie_lookup = next(
                        (l for l in item.get("lookups", []) if l.get("lookup_type") == "movie"), None
                    )
                    if movie_lookup:
                        try:
                            trailer_key = tmdb_movie_trailer(
                                movie_lookup["lookup_name"], movie_lookup.get("lookup_year")
                            )
                            if trailer_key:
                                clean["trailer_key"] = trailer_key
                        except requests.RequestException as e:
                            print(f"    trailer lookup failed for '{movie_lookup['lookup_name']}': {e}")
                else:
                    clean["images"] = plain_images
                    # Regular items can also opt into a trailer embed (e.g.
                    # a post about upcoming/anticipated movies) by flagging
                    # "include_trailer": true on the relevant movie lookup.
                    trailer_lookup = next(
                        (l for l in item.get("lookups", [])
                         if l.get("lookup_type") == "movie" and l.get("include_trailer")),
                        None,
                    )
                    if trailer_lookup:
                        try:
                            trailer_key = tmdb_movie_trailer(
                                trailer_lookup["lookup_name"], trailer_lookup.get("lookup_year")
                            )
                            if trailer_key:
                                clean["trailer_key"] = trailer_key
                        except requests.RequestException as e:
                            print(f"    trailer lookup failed for '{trailer_lookup['lookup_name']}': {e}")
                resolved_items.append(clean)

            if not resolved_items:
                print(f"  FAILED ({slug}): no items resolved to real images, skipping post")
                continue

            # Prefer a movie still (landscape) as the cover — the homepage's
            # trending hero slot is a wide box, and a portrait actor headshot
            # forced into it crops badly and visibly distorts the layout next
            # to it. Only fall back to a person photo if the post has no
            # movie image anywhere in it.
            cover_image = next(
                (c["url"] for c in cover_candidates if c["type"] == "movie"),
                cover_candidates[0]["url"],
            )

            post = {
                "slug": slug,
                "title": draft["title"],
                "dek": draft.get("dek", ""),
                "category": idea["category"],
                "cover_image": cover_image,
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "items": resolved_items,
                "sources": draft.get("sources", []),
                "related": [],
            }

            out_path = CONTENT_DIR / f"{slug}.json"
            out_path.write_text(json.dumps(post, indent=2) + "\n")
            print(f"  wrote {out_path.relative_to(REPO_ROOT)} ({len(resolved_items)} items)")
            existing_slugs.add(slug)
            processed += 1
        except Exception as e:
            print(f"  FAILED ({slug}): {e}", file=sys.stderr)

        time.sleep(1)

    print(f"Done — published {processed} new post(s) this run.")


if __name__ == "__main__":
    main()
