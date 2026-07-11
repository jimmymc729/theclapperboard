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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

try:
    import anthropic
except ImportError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "content" / "posts"
IDEAS_PATH = Path(__file__).resolve().parent / "post_ideas.txt"

# Hand-picked photos live here, one subfolder per person (see
# custom_photos_for below) — e.g. assets/custom-photos/tom-holland/*.jpg.
# Entirely optional and additive: an empty or missing folder just means
# tmdb_person_image() behaves exactly as it did before this existed.
CUSTOM_PHOTOS_DIR = REPO_ROOT / "assets" / "custom-photos"
SITE_URL = "https://theclapperboard.com"  # keep in sync with SITE["url"] in build_site.py

TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_PERSON_IMG = "https://image.tmdb.org/t/p/w500"
TMDB_BACKDROP_IMG = "https://image.tmdb.org/t/p/w780"
TMDB_BACKDROP_THUMB = "https://image.tmdb.org/t/p/w300"  # small, cheap to send to vision
TMDB_POSTER_IMG = "https://image.tmdb.org/t/p/w500"

MODEL = "claude-sonnet-5"
MAX_VISION_CANDIDATES = 6  # how many backdrop candidates to show Claude per lookup
POSTS_PER_RUN = 2  # cap on how many new posts a single run will generate/spend API budget on

# Manually-pinned subjects that have been observed to get higher engagement on social
# (e.g. Twitter/X) when they're the focus of a post. Always included (see
# get_trending_subjects below) regardless of what's live-trending on TMDB, so you can
# still deliberately keep someone in rotation even if their moment isn't spiking this
# exact day/week. Add/remove names here as performance data changes; no other code
# needs to change.
TRENDING_SUBJECTS = ["Tom Holland", "Zendaya"]

TRENDING_PEOPLE_COUNT = 6  # how many live-trending names to pull from TMDB each run


def fetch_trending_people(max_count: int = TRENDING_PEOPLE_COUNT) -> list:
    """Live 'who's spiking in movie/TV attention right now' signal from TMDB
    — pulled fresh every run instead of relying solely on the hardcoded
    TRENDING_SUBJECTS list above needing someone to remember to update it by
    hand. Best-effort: any failure here (network issue, unexpected response
    shape) just means get_trending_subjects() falls back to the manual list
    on its own, exactly like before this existed — never worth failing the
    whole run over."""
    try:
        resp = requests.get(
            f"{TMDB_BASE}/trending/person/day",
            params={"api_key": TMDB_API_KEY},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []
    return [r["name"] for r in results[:max_count] if r.get("name")]


def get_trending_subjects() -> list:
    """Manually-pinned names (TRENDING_SUBJECTS) always come first and are
    always included, topped up with whatever's live-trending on TMDB right
    now, deduped — so a deliberate pin never gets bumped by an
    auto-fetched name, it only ever adds to the list."""
    combined = list(TRENDING_SUBJECTS)
    for name in fetch_trending_people():
        if name not in combined:
            combined.append(name)
    return combined


def parse_ideas():
    """Each line is normally `slug | category | instructions`. An optional
    4th field, the literal word "opinion", flags a grounded opinion/take
    piece (see TOPIC_BRAINSTORM_PROMPT and SYSTEM_PROMPT) — this is a
    structural flag WE control (which pill build_site.py renders, whether
    it counts against the rarity cap), not something left for Claude to
    decide on its own mid-write."""
    ideas = []
    for line in IDEAS_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) not in (3, 4):
            print(f"Skipping malformed idea line: {line}", file=sys.stderr)
            continue
        slug, category, instructions = parts[0], parts[1], parts[2]
        opinion = len(parts) == 4 and parts[3].lower() == "opinion"
        ideas.append({"slug": slug, "category": category, "instructions": instructions, "opinion": opinion})
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
- Quizzes: a "which character are you" or "which X would you be" personality quiz — MUCH rarer \
than the other three, aim for roughly 1 in every 6-8 ideas, and only when you can genuinely name \
EXACTLY 8 real, well-known, sufficiently DIFFERENT results. Two shapes work: (a) one actor whose own \
filmography spans genuinely contrasting, well-known roles (not eight variations on the same type \
of character), or (b) an ensemble of comparably famous interpretations of one thing (different \
actors who've all played the same iconic role across films). If you can't think of a real subject \
with that kind of range, skip the format entirely rather than force a thin quiz — a quiz with weak, \
repetitive results is worse than one fewer idea this run.

Every idea must be the kind of real, well-documented, fact-checkable topic a researcher could \
actually verify with web search — not vague, not unfalsifiable, not about living people's private \
lives beyond publicly reported career/casting history.

Controversy and drama are genuinely fair game — don't shy away from it — but only the public, \
professional kind: casting backlash, a movie bombing and the internet dunking on it, a franchise \
rivalry, an on-set feud that's already been widely reported, an awards-show snub people are still \
mad about, fans revolting over a creative decision. That's the same territory a lot of viral \
entertainment content lives in, and it's fine here.

What's OFF LIMITS is a completely different category: real personal tragedy — a death, a serious \
health crisis, criminal allegations, or genuine personal hardship. Never build an idea (or use a \
trending subject below) around something in that category, even lightly or as a passing detail. \
If a name is trending right now because of something like that rather than a project/career \
moment, skip them entirely for this rotation — don't force a lighthearted angle onto it.

Trending subjects: {trending_subjects} are either manually pinned or currently spiking in movie/TV \
attention on TMDB right now — the ones sourced live from TMDB carry no context on WHY they're \
trending, so use your judgment (a quick mental check on what's actually in the news for them right \
now) before treating one as a green light. Where a subject clearly IS a natural, appropriate fit, \
bias roughly 1 in every 3-4 ideas toward them — a facts listicle, a real/public relationship or \
career-timeline angle, tying into a genuinely upcoming or recent project, or a guessing game built \
from their filmography. Never force it into an idea it doesn't fit, never invent private details, \
and don't repeat an angle already covered in the existing titles below.

Also mix in occasional "this did NOT age well" ideas (nostalgia-driven hindsight content — a \
prediction, review, casting take, or special effect that seemed reasonable/impressive at the time \
but looks wrong, dated, or funny now) roughly 1 in every 5-6 ideas. Must still be real and \
documented — an actual quote/prediction/review plus what actually happened, not a vague vibe.

Opinion/take pieces are also fair game, as an angle within an Actors or Movies idea (not its own \
category) — a ranking, reassessment, or "hotter take" that argues a real position rather than just \
listing facts, e.g. "5 Best Picture Winners People Still Argue Are Overrated" or "The Most \
Overrated Performances Of The Decade." Keep this RARE — roughly 1 in every 6 ideas — and only ever \
grounded in real, citable disagreement: an actual critic's quote, a specific critic/audience score \
gap, a documented reassessment over time. Never a bare assertion dressed up as a take. Critique the \
WORK or the creative choice, never a real person's talent or worth — "this ending didn't land, and \
here's what critics said at the time" is fine, "this actor can't act" is not. When you use this \
angle, say so explicitly in the instructions (so the writer knows to take a real stance and cite \
sources backing it, not just list neutral facts) and set "opinion": true on that idea.

Return ONLY a JSON array, no prose, no markdown fences:
[
  {{
    "slug": "kebab-case-unique-slug",
    "category": "Actors" or "Movies" or "Games" or "Quizzes",
    "instructions": "plain-English description of exactly what the post should cover, specific \
enough to research — e.g. how many items, what kind of facts, which format for Games, or which \
actor/franchise plus roughly how many results for Quizzes — and if this is an opinion/take piece, \
say so explicitly and note what real disagreement/sources it should be grounded in",
    "opinion": true or false
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
            trending_subjects=", ".join(get_trending_subjects()) or "(none set)",
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
        {
            "slug": i["slug"],
            "category": i["category"],
            "instructions": i["instructions"],
            "opinion": bool(i.get("opinion", False)),
        }
        for i in ideas
        if i.get("slug") and i.get("category") and i.get("instructions")
    ]


def slugify(name: str) -> str:
    """"Timothée Chalamet" -> "timothee-chalamet" — strips accents first
    (via NFKD + ascii-encode) so folder names stay plain ASCII regardless
    of how a name is written, then lowercases/hyphenates same as
    build_site.py's own slugify()."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")


def custom_photos_for(name: str) -> list:
    """Absolute URLs for any hand-picked photos dropped into
    assets/custom-photos/<slug>/ for this specific person — e.g. something
    more current than TMDB has on file, or just a better shot. Purely
    additive: these become extra candidates alongside whatever TMDB
    returns in tmdb_person_image(), never a forced replacement, and an
    empty/missing folder is simply zero extra candidates.

    Filenames don't need to be descriptive or tidy — a raw Twitter media ID
    works exactly as well as a hand-typed name — but they DO need to be
    valid inside a URL, so each path segment is percent-encoded (a literal
    space or "#" in a filename would otherwise produce a broken/unreliable
    URL once embedded in the page)."""
    folder = CUSTOM_PHOTOS_DIR / slugify(name)
    if not folder.is_dir():
        return []
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    files = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in exts)
    return [
        f"{SITE_URL}/assets/custom-photos/{quote(folder.name)}/{quote(f.name)}"
        for f in files
    ]


def tmdb_person_image(name: str, used_images: set = None, max_candidates: int = 6) -> str:
    """Return a photo of this person, preferring one that isn't already used
    elsewhere on the site. Hand-picked photos (see custom_photos_for) are
    checked first and folded into the same candidate pool as TMDB's own
    /images list — a plain /search/person lookup only ever returns that
    person's single "primary" profile photo, so a subject who comes up
    across multiple posts (or multiple items in the same post — e.g.
    several facts with no specific movie tied to them) would otherwise get
    the exact same headshot every single time. Picking around whatever's
    already used avoids that repetition without needing scene-specific
    vision matching the way movie stills do."""
    used_images = used_images if used_images is not None else set()
    candidates = list(custom_photos_for(name))

    try:
        resp = requests.get(
            f"{TMDB_BASE}/search/person",
            params={"api_key": TMDB_API_KEY, "query": name},
            timeout=20,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if results and results[0].get("id"):
            images_resp = requests.get(
                f"{TMDB_BASE}/person/{results[0]['id']}/images",
                params={"api_key": TMDB_API_KEY},
                timeout=20,
            )
            images_resp.raise_for_status()
            profiles = images_resp.json().get("profiles", [])
            if profiles:
                profiles.sort(key=lambda p: p.get("vote_count", 0), reverse=True)
                candidates += [f"{TMDB_PERSON_IMG}{p['file_path']}" for p in profiles[:max_candidates]]
            else:
                # Dedicated images endpoint came back empty — fall back to
                # whatever /search/person itself returned rather than
                # dropping TMDB entirely.
                profile_path = results[0].get("profile_path")
                if profile_path:
                    candidates.append(f"{TMDB_PERSON_IMG}{profile_path}")
    except requests.RequestException:
        pass  # TMDB hiccup — fall back to whatever custom photos we already have, if any

    if not candidates:
        return ""
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


def tmdb_movie_image(title: str, year: int = None, context_text: str = None, used_images: set = None) -> str:
    """Prefer a real backdrop (an actual still from the film) over poster
    art, since posters are promotional key art rather than a movie frame.

    When `context_text` is given (the specific fact this image needs to
    illustrate), this fetches several candidate backdrops and uses Claude's
    vision to pick whichever one actually matches — TMDB has no metadata
    tagging what a backdrop depicts, so this is the only reliable way to do
    better than "grab the first/most-voted one and hope."

    `used_images` applies the same site-wide de-dup that tmdb_person_image()
    already had — without it, two unrelated posts that both mention the same
    movie in similar context would have the vision step land on the exact
    same "best" still both times, since the selection is otherwise
    deterministic for a given title+context pair."""
    used_images = used_images if used_images is not None else set()
    poster_path, candidates = tmdb_backdrop_candidates(title, year)
    if not candidates:
        return f"{TMDB_POSTER_IMG}{poster_path}" if poster_path else ""

    # Prefer a candidate not already used elsewhere on the site; only fall
    # back to the full (possibly-repeat) candidate list if every backdrop
    # TMDB has on file for this movie is already spoken for.
    fresh = [c for c in candidates if f"{TMDB_BACKDROP_IMG}{c}" not in used_images]
    pool = fresh or candidates

    if context_text:
        chosen = choose_backdrop_with_vision(pool, title, context_text)
    else:
        chosen = pool[0]
    return f"{TMDB_BACKDROP_IMG}{chosen}"


QUIZ_RESULT_COUNT = 8  # matches every hand-built quiz already on the site
QUIZ_ANSWERS_PER_QUESTION = 4  # how many of the 8 results actually show up per question


def validate_quiz(quiz: dict) -> None:
    """Raises ValueError with a specific message if the quiz's underlying
    data doesn't actually support fair scoring. Claude is asked to supply
    FULL coverage — every question answered for every one of the 8 results
    (see SYSTEM_PROMPT) — which rotate_quiz_answers() then narrows down to
    the 4-per-question a visitor actually sees. That full-coverage
    requirement is what's validated here; if a question is missing an
    answer for some result, or has a duplicate, there'd be no way to
    rotate it into a fair subset later. Cheap to check, and much better
    than publishing a quiz that's subtly unfair or broken."""
    results = quiz.get("results", [])
    questions = quiz.get("questions", [])
    if len(results) != QUIZ_RESULT_COUNT:
        raise ValueError(f"expected exactly {QUIZ_RESULT_COUNT} results, got {len(results)}")

    result_keys = [r.get("key") for r in results]
    if len(set(result_keys)) != len(result_keys):
        raise ValueError("duplicate result keys")
    if not all(result_keys):
        raise ValueError("a result is missing its key")

    expected = set(result_keys)
    for qi, q in enumerate(questions, start=1):
        answer_keys = [a.get("result") for a in q.get("answers", [])]
        if len(answer_keys) != len(expected) or set(answer_keys) != expected:
            raise ValueError(
                f"question {qi} answers don't cover every result exactly once "
                f"(got {answer_keys}, expected exactly {sorted(expected)})"
            )


def rotate_quiz_answers(questions: list, result_keys: list) -> list:
    """Narrows each question's full 8-answer coverage (see validate_quiz)
    down to a rotating QUIZ_ANSWERS_PER_QUESTION-sized subset — the same
    pattern every hand-built quiz on this site already uses: showing all 8
    choices on every single question would be overwhelming, but picking a
    DIFFERENT 4 each time (shifted by one result per question, wrapping
    cyclically) means every result still appears roughly the same number
    of times across the whole quiz, so no result has a structural
    tallying advantage just from showing up more often. This is done here
    in code rather than trusted to the model, since getting an even
    rotation exactly right is fiddly and easy to get subtly wrong."""
    r = len(result_keys)
    rotated = []
    for i, q in enumerate(questions):
        window = [result_keys[(i + j) % r] for j in range(QUIZ_ANSWERS_PER_QUESTION)]
        by_result = {a["result"]: a for a in q["answers"]}
        rotated.append({
            "question": q["question"],
            "answers": [by_result[key] for key in window],
        })
    return rotated


def resolve_lookup(lookup: dict, context_text: str = None, used_images: set = None) -> str:
    lookup_type = lookup.get("lookup_type")
    lookup_name = lookup.get("lookup_name")
    if not lookup_type or not lookup_name:
        return ""
    if lookup_type == "person":
        return tmdb_person_image(lookup_name, used_images)
    if lookup_type == "movie":
        return tmdb_movie_image(lookup_name, lookup.get("lookup_year"), context_text, used_images)
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

Voice: write titles, deks, and item headings like someone reacting in the moment, not narrating a
neutral summary. Bake the reaction into the phrasing itself instead of just describing the fact —
"Wait, THIS Almost Happened?", "...And Honestly, We're Still Not Over It", "This One Detail
Somehow Ruined Everything For Us". Don't be afraid of a little exaggeration or repetition for
comedic rhythm. This is about tone only, not substance — the fact itself still has to be 100% real
and verifiable; you're just not allowed to report it like a neutral encyclopedia entry.

Include 1-3 lookups per item, but only when each one is a genuinely distinct subject — e.g. an
actor's photo alongside a still from the movie they were considered for. Do NOT pair two images of
the exact same single subject (like a poster and a still from the same movie with no second
subject) just to fill space; a single strong image is better than two redundant ones.

If the topic's instructions mark this as a grounded opinion/take piece: same JSON shape as above
(title/dek/items/sources), but each item should take a real stance rather than just report a
neutral fact — and that stance MUST be backed by something citable: a specific critic's quote, a
concrete critic-vs-audience score gap, a documented reassessment over time. Never publish a bare
assertion with nothing behind it; if you can't find real disagreement to cite for an item, drop
that item rather than invent a take for it. Critique the work or the creative choice, never a real
person's talent or worth as a human being — "this ending didn't land, and here's what critics said
at the time" is fine, "this actor can't act" is not. Sources for these MUST include the actual
reviews/citations backing each stance, not just general background reading.

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

If the topic is a personality quiz ("which character are you" / "which X would you be"), return
this shape INSTEAD of the "items" shape above — same top-level "title"/"dek"/"sources", but a
"quiz" object instead of "items":

{
  "title": "...",
  "dek": "...",
  "quiz": {
    "intro": "one short sentence setting up the quiz, e.g. 'No wrong answers — just go with your gut.'",
    "questions": [
      {
        "question": "a scenario/preference question, not a trivia question",
        "answers": [
          { "text": "an answer written as something a person would say/choose", "result": "result_key" },
          ...
        ]
      },
      ...
    ],
    "results": [
      {
        "key": "short-lowercase-key",
        "name": "Character or actor name",
        "subtitle": "Movie Title (Year)",
        "description": "2-3 sentences, written in second person ('You...'), describing this result",
        "lookup": { "lookup_type": "person" or "movie", "lookup_name": "exact name/title", "lookup_year": 1999 }
      },
      ...
    ]
  }
}

Critical structural rule for quizzes: pick EXACTLY 8 results, then write 9-10 questions. EVERY
SINGLE QUESTION's "answers" array must contain EXACTLY 8 answers — one per result, the same 8
result keys every single time (order doesn't matter). No question may repeat a result twice or
omit one. (The site's build step automatically narrows each question down to a rotating subset of
4 of these 8 answers when it publishes the quiz — showing all 8 choices on every question would be
overwhelming — but it can only do that fairly if you've supplied a real, natural-sounding answer
for every result on every single question, not just a favorite 4.) Reuse the exact same short
lowercase "key" strings (e.g. "peter", "drake") consistently between every question's answers and
the results list. Each result's "lookup" should be whichever of person/movie actually represents
that specific result — usually a movie lookup naming that character's specific film, so each result
gets a visually distinct image rather than eight photos of the same actor's face.

Rules:
- Only include real, verifiable facts and quotes. If you can't verify something, leave it out
  rather than guessing or inventing it.
- lookup_name must be a real, exact, searchable name (full actor name, or exact movie title) —
  this is used to fetch a real photo, so precision matters more than style here.
- Do not invent image URLs yourself; that's handled separately from what you return.
- Output nothing before or after the JSON object."""


def claude_generate(category: str, instructions: str) -> dict:
    """Note on max_tokens: quiz posts in particular need a lot of room — 8
    results x 9-10 questions x 8 full-text answers each is a genuinely large
    JSON payload (see the quiz section of SYSTEM_PROMPT) — and a response
    that gets cut off mid-string or mid-object produces exactly the kind of
    confusing JSON parse errors (unterminated string, missing closing brace)
    that used to show up as generic "FAILED" lines with no obvious cause.
    Checking resp.stop_reason lets a truncated response fail with a message
    that actually says so, instead of a downstream JSONDecodeError that
    looks like a one-off formatting fluke."""
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Category: {category}\nTopic: {instructions}\n\nResearch it and return the JSON object now.",
        }],
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
    )
    if resp.stop_reason == "max_tokens":
        raise ValueError(
            "response was cut off by the max_tokens limit before finishing — "
            "the JSON is incomplete, not just malformed"
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
                        suffix = " | opinion" if i.get("opinion") else ""
                        f.write(f"{i['slug']} | {i['category']} | {i['instructions']}{suffix}\n")
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

            if draft.get("quiz"):
                quiz = draft["quiz"]
                try:
                    validate_quiz(quiz)
                except ValueError as e:
                    print(f"  FAILED ({slug}): invalid quiz structure — {e}")
                    continue

                resolved_results = []
                for r in quiz.get("results", []):
                    url = resolve_lookup(r.get("lookup", {}), r.get("description", ""), used_images)
                    if not url:
                        print(f"  no image found for quiz result '{r.get('name')}', aborting quiz")
                        resolved_results = []
                        break
                    used_images.add(url)
                    resolved_results.append({
                        "key": r["key"],
                        "name": r.get("name", ""),
                        "subtitle": r.get("subtitle", ""),
                        "description": r.get("description", ""),
                        "image": url,
                    })

                if not resolved_results:
                    print(f"  FAILED ({slug}): could not resolve every quiz result's image, skipping post")
                    continue

                result_keys = [r["key"] for r in resolved_results]
                rotated_questions = rotate_quiz_answers(quiz.get("questions", []), result_keys)

                post = {
                    "slug": slug,
                    "title": draft["title"],
                    "dek": draft.get("dek", ""),
                    "category": "Games",
                    "cover_image": resolved_results[0]["image"],
                    "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    "quiz": {
                        "intro": quiz.get("intro", ""),
                        "questions": rotated_questions,
                        "results": resolved_results,
                    },
                    "sources": draft.get("sources", []),
                    "related": [],
                }

                out_path = CONTENT_DIR / f"{slug}.json"
                out_path.write_text(json.dumps(post, indent=2) + "\n")
                print(f"  wrote {out_path.relative_to(REPO_ROOT)} (quiz, {len(resolved_results)} results)")
                existing_slugs.add(slug)
                processed += 1
                time.sleep(1)
                continue

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
            if idea.get("opinion"):
                post["opinion"] = True

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
