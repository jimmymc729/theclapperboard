#!/usr/bin/env python3
"""
Static site builder for The Clapperboard — plain HTML, zero build tooling.

Reads every listicle-post JSON file in content/posts/, renders plain .html
files using nothing but the Python standard library (no npm, no build step
other than "run this script"), and writes the result to docs/.

All internal links and asset references are relative (with explicit
"index.html" filenames rather than clean directory URLs), so the site works
two ways with zero configuration:

  1. Double-click docs/index.html and click around directly in a browser —
     no server, no GitHub, nothing installed.
  2. Push it to GitHub and serve docs/ via GitHub Pages — the same relative
     links resolve just as well over https.

Usage:
    python3 build_site.py

Run this after adding or editing anything in content/posts/, or after
scripts/generate_post.py adds new listicles.
"""

import html
import json
import random
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content" / "posts"
TRAILERS_PATH = ROOT / "content" / "trailers.json"
ENGAGEMENT_PATH = ROOT / "content" / "engagement.json"
ASSETS_DIR = ROOT / "assets"
OUT_DIR = ROOT / "docs"

# Set once in main() from load_trailers() — lets base_page() decide whether
# to show the "Trailers" nav link without threading an extra argument through
# every single page-builder function.
HAS_TRAILERS = False

SITE = {
    "name": "The Clapperboard",
    "tagline": "The stories behind the stories.",
    "description": "Movie facts, personality quizzes, guess-the-movie games, and the latest trailers.",
    "url": "https://theclapperboard.com",
    "flickle_url": "https://flickle.io",
    "flickle_name": "Flickle",
    "flickle_tagline": "The daily movie guessing game.",
}

GA_MEASUREMENT_ID = "G-B3W2EJRMYK"  # Google Analytics 4 property for theclapperboard.com

CATEGORY_EMOJI = {"Actors": "🎭", "Movies": "🎬", "Games": "🎮"}
CATEGORY_SLUGS = {"Actors": "actors", "Movies": "movies", "Games": "games"}

REACTIONS = [("😂", "LOL"), ("😍", "LOVE"), ("😱", "WOW"), ("🧠", "TIL")]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def esc(value) -> str:
    """HTML-escape anything, treating None as empty string."""
    return html.escape(str(value)) if value is not None else ""


def load_posts():
    posts = []
    for f in sorted(CONTENT_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        data.setdefault("slug", f.stem)
        posts.append(data)
    # Sort by generated_at (a real timestamp) when a post has one, falling
    # back to the plain "date" field for older posts written before that
    # field existed. Without this, two posts published the same calendar
    # day — routine, since several get published per run — would only ever
    # tiebreak alphabetically by slug (the order sorted(CONTENT_DIR.glob())
    # happened to collect them in above), not by which was actually written
    # first. ISO timestamp strings and plain "YYYY-MM-DD" strings compare
    # correctly against each other lexicographically, so old and new posts
    # sort sensibly together with no migration needed.
    posts.sort(key=lambda p: p.get("generated_at") or p.get("date", ""), reverse=True)
    return posts


def load_trailers() -> list:
    """Reads content/trailers.json — a plain snapshot written by
    scripts/update_trailers.py, not a hand-authored content file. Missing
    entirely (e.g. the very first build before that script has ever run) is
    treated the same as "no trailers yet", not an error."""
    if not TRAILERS_PATH.exists():
        return []
    try:
        return json.loads(TRAILERS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def load_engagement() -> dict:
    """Reads content/engagement.json — a plain snapshot written by
    scripts/fetch_engagement.py from real GA4 data, not hand-authored.
    Missing or unreadable (no GA4 credentials configured yet, or the very
    first build before that script has ever run) is treated as "no
    engagement data yet", not an error — every post just scores 0, so
    "Trending" quietly behaves the same as "Newest" until real data exists."""
    if not ENGAGEMENT_PATH.exists():
        return {}
    try:
        return json.loads(ENGAGEMENT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def trending_order(posts: list, engagement: dict) -> list:
    """Same posts, reordered by real engagement score instead of publish
    date. Python's sort is stable, and `posts` arrives already newest-first
    (see load_posts()), so anything tied on score — including every post
    with no engagement data yet, which is most brand-new posts — keeps
    falling back to newest-first amongst themselves rather than some
    arbitrary or reversed order."""
    return sorted(posts, key=lambda p: engagement.get(p["slug"], {}).get("score", 0), reverse=True)


def view_toggle(group_id: str, tabs: list) -> str:
    """Wraps 2+ pre-rendered versions of the same content in a client-side
    tab switcher — e.g. Newest/Trending post ordering, or an All / In
    Theaters Now / Coming Soon trailer filter. Every version is fully
    rendered into the page at build time; script.js just toggles which one
    is visible (see the .view-toggle-group handler in script.js), so
    switching tabs is instant with no reload and works even with
    JS-blocking privacy tools (it just shows whichever tab is first in
    `tabs`, and the others silently do nothing rather than erroring).

    `tabs` is a list of (key, label, html) tuples — the FIRST tab in the
    list is the one shown by default."""
    buttons = "\n".join(
        f'      <button type="button" class="view-toggle-btn{" active" if i == 0 else ""}" data-view="{esc(key)}">{label}</button>'
        for i, (key, label, _) in enumerate(tabs)
    )
    panels = "".join(
        f'    <div data-view-panel="{esc(key)}"{"" if i == 0 else " hidden"}>\n{html}    </div>\n'
        for i, (key, label, html) in enumerate(tabs)
    )
    return f"""  <div class="view-toggle-group" data-toggle-group="{esc(group_id)}">
    <div class="view-toggle-tabs">
{buttons}
    </div>
{panels}  </div>
"""


def pretty_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        return iso or ""


def read_minutes(p: dict) -> int:
    text_blob = " ".join([
        p.get("dek", ""),
        *[i.get("text", "") for i in p.get("items", [])],
        *[i.get("reveal_text", "") for i in p.get("items", [])],
        *[i.get("quote", "") for i in p.get("items", [])],
    ])
    if p.get("quiz"):
        quiz = p["quiz"]
        text_blob += " " + quiz.get("intro", "")
        text_blob += " " + " ".join(q.get("question", "") for q in quiz.get("questions", []))
        text_blob += " " + " ".join(r.get("description", "") for r in quiz.get("results", []))
    words = len(re.findall(r"\S+", text_blob))
    return max(1, round(words / 200))


def category_pill(category: str) -> str:
    emoji = CATEGORY_EMOJI.get(category, "🎬")
    return f'<span class="pill">{emoji} {esc(category)}</span>'


def opinion_pill() -> str:
    """Sits next to the regular category pill on posts flagged
    "opinion": true (see generate_post.py) — a grounded stance/ranking
    piece rather than a plain facts listicle. Visually distinct on purpose:
    the site's credibility rests on "this is sourced and factual," so
    anything that's taking a real stance instead should be clearly
    labeled as such rather than blending in."""
    return '<span class="pill pill-opinion">🗣️ Our Take</span>'


def game_type_pill(p) -> str:
    """A second pill, alongside the category pill, specifically for Games
    posts — "Games" alone doesn't distinguish the different interactions
    a Games post might be: a multi-question personality quiz, a single
    reveal-based guess-the-movie trivia card (emoji or quote clue), or a
    zoomed-poster + guess-the-year game. Empty string for every other
    category, or for a Games post that's none of these shapes (shouldn't
    happen, but this degrades gracefully rather than guessing)."""
    if p.get("quiz"):
        return '<span class="pill pill-quiz">🧩 Quiz</span>'
    if p.get("poster_guess"):
        return '<span class="pill pill-poster">🖼️ Poster ID</span>'
    if p.get("year_guess"):
        return '<span class="pill pill-year">🗓️ Guess The Year</span>'
    if p.get("category") == "Games" and p.get("items"):
        first_item = p["items"][0]
        if "emoji" in first_item or "quote" in first_item:
            return '<span class="pill pill-trivia">🎯 Trivia</span>'
    return ""


def theater_status_pill(iso: str, theatrical: bool = True) -> str:
    """Trailers now cover both already-released and still-upcoming movies
    (see scripts/update_trailers.py), so a flat "In theaters {date}" label
    would read oddly for something that opened weeks ago. Renders the
    distinction as a small pill rather than plain caption text — solid for
    anything already out, lighter/outlined for anything still upcoming —
    so the two states read as genuinely different information at a glance,
    not just two different date strings in the same font.

    TMDB's /discover/movie only surfaces one release date per movie, which
    is very often a streaming/digital date rather than a theatrical one —
    so a released movie isn't automatically "in theaters" just because its
    release date has passed. `theatrical` (resolved once in
    update_trailers.py via the real US release_dates list — see
    has_us_theatrical_release()) distinguishes those: a released,
    non-theatrical movie gets an honest "Streaming Now" pill instead of a
    flatly wrong "In Theaters Now" one. Defaults to True so any
    trailers.json written before this field existed still renders exactly
    as before rather than erroring or guessing."""
    try:
        release = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        release = None
    if release is not None and release <= datetime.now().date():
        if theatrical:
            return '<span class="status-pill status-pill-now">🎬 In Theaters Now</span>'
        return '<span class="status-pill status-pill-streaming">📺 Streaming Now</span>'
    return f'<span class="status-pill status-pill-upcoming">🍿 Coming {esc(pretty_date(iso))}</span>'


def is_upcoming(iso: str) -> bool:
    """Same released-vs-upcoming split as theater_status_pill(), returned
    as a plain bool instead of a rendered pill — used to build the All / In
    Theaters Now / Coming Soon filter tabs on the trailers index."""
    try:
        release = datetime.strptime(iso, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return release > datetime.now().date()


def slugify(text: str) -> str:
    """Turns a movie title into a URL-safe slug, e.g. "The Odyssey" ->
    "the-odyssey" — trailers don't come from a JSON file with a filename to
    borrow a slug from (like posts do), so one has to be derived from the
    title itself at build time instead."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "trailer"


# --------------------------------------------------------------------------
# Shared page chrome
#
# `root` is the relative path prefix back to the site root, computed per
# page depth: "" for docs/index.html, "../" for docs/posts/index.html,
# "../../" for docs/posts/<slug>/index.html.
# --------------------------------------------------------------------------

def social_image_size(url: str) -> str:
    """Swaps a TMDB image URL's size tier (e.g. /w780/, /w500/) up to
    /w1280/, but ONLY for the og:image/twitter:image meta tags — never the
    on-page <img> that reuses the same stored URL. X's own guidance wants
    at least 1200x628 for reliable full-quality summary_large_image
    rendering; the w780 tier used for regular on-page images (roughly
    780x439) is below that and was the likely cause of X's card sometimes
    rendering inconsistently even though the tags themselves are correct.
    Bumping every on-page image to match would add real page weight for a
    quality bump that only matters at the moment of sharing, so this stays
    scoped to just the two social tags via a simple URL string swap — no
    extra fetch, no new stored field, just a different size segment on the
    same already-resolved TMDB URL.

    Anything that isn't a recognized TMDB image URL (e.g. a locally hosted
    custom photo, see custom-photos/) is returned unchanged rather than
    risking a broken substitution on a URL that was never in that
    /t/p/w###/ format to begin with."""
    match = re.match(r"^(https://image\.tmdb\.org/t/p/)w\d+(/.+)$", url)
    if not match:
        return url
    return f"{match.group(1)}w1280{match.group(2)}"


def base_page(title: str, description: str, canonical_path: str, body: str, root: str,
              image: str = "", schema: str = "") -> str:
    # Both og:image AND twitter:image are emitted (rather than relying on
    # Twitter/X falling back to og:image, which it usually does but isn't
    # guaranteed) — this is what makes a shared link unfurl into a card with
    # a real image instead of a bare text link, on both Facebook/iMessage
    # (which read og:*) and X (which prefers twitter:* when present).
    social_image_url = social_image_size(image) if image else ""
    social_image = f"""<meta property="og:image" content="{esc(social_image_url)}">
<meta name="twitter:image" content="{esc(social_image_url)}">""" if image else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<!-- Google tag (gtag.js) -->
<script async src="https://www.googletagmanager.com/gtag/js?id={GA_MEASUREMENT_ID}"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());

  gtag('config', '{GA_MEASUREMENT_ID}');
</script>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{SITE['url']}{canonical_path}">
<link rel="icon" type="image/svg+xml" href="{root}assets/favicon.svg">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@700;800;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{root}assets/style.css">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="article">
<meta property="og:url" content="{SITE['url']}{canonical_path}">
{social_image}
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{esc(title)}">
<meta name="twitter:description" content="{esc(description)}">
{schema}
</head>
<body>
  <div class="clapper-stripes" aria-hidden="true"></div>
  <header class="site-header">
    <div class="wrap header-inner">
      <a class="brand" href="{root}index.html">🎬 {esc(SITE['name'])}</a>
      <nav class="header-nav">
        <a href="{root}posts/index.html">All Posts</a>
        <a class="flickle-link" href="{SITE['flickle_url']}" target="_blank" rel="noopener">Play {esc(SITE['flickle_name'])} →</a>
      </nav>
    </div>
    <div class="category-nav-wrap">
      <div class="wrap category-nav">
        <a href="{root}posts/index.html">All Posts</a>
        {"".join(f'<a href="{root}posts/{slug}/index.html">{esc(CATEGORY_EMOJI.get(cat, ""))} {esc(cat)}</a>' for cat, slug in CATEGORY_SLUGS.items())}
        {f'<a href="{root}trailers/index.html">🎥 Trailers</a>' if HAS_TRAILERS else ""}
      </div>
    </div>
  </header>

  <main class="wrap page-main">
{body}
  </main>

  <footer class="site-footer">
    <div class="wrap">
      <p>{esc(SITE['name'])} is an independent fan/entertainment site. Movie stills, posters, and
        photos are used for commentary and reference purposes and are sourced via TMDB. Not
        affiliated with any studio.</p>
      <p><a href="{SITE['flickle_url']}" target="_blank" rel="noopener">{esc(SITE['flickle_name'])}</a> —
        {esc(SITE['flickle_tagline'])}</p>
      <p>This product uses the TMDB API but is not endorsed or certified by TMDB.</p>
    </div>
  </footer>

  <script src="{root}assets/script.js"></script>
</body>
</html>
"""


def flickle_cta(context: str = "") -> str:
    headline = context or f"Play today's {esc(SITE['flickle_name'])}."
    return f"""    <div class="flickle-cta">
      <div class="flickle-cta-text">
        <p class="flickle-cta-eyebrow">Think you know movies?</p>
        <p class="flickle-cta-headline">{headline}</p>
        <p class="flickle-cta-sub">{esc(SITE['flickle_tagline'])}</p>
      </div>
      <a class="flickle-cta-button" href="{SITE['flickle_url']}" target="_blank" rel="noopener">Play {esc(SITE['flickle_name'])} →</a>
    </div>
"""


def post_accent_class(p) -> str:
    """Matches the exact same type-detection logic as game_type_pill()/
    opinion_pill() (quiz flag, Games-with-emoji/quote items, opinion flag)
    — reused here to add a colored top-edge accent on the card itself (see
    .post-card-quiz/-trivia/-opinion in the CSS, the same "colored top bar"
    language the emoji/quote game cards already use), so a Quiz or Trivia
    or Our Take piece reads as visually distinct from a plain listicle at
    a skim, not just on close reading of the pill text under the image."""
    if p.get("quiz"):
        return " post-card-quiz"
    if p.get("poster_guess"):
        return " post-card-poster"
    if p.get("year_guess"):
        return " post-card-year"
    if p.get("category") == "Games" and p.get("items"):
        first_item = p["items"][0]
        if "emoji" in first_item or "quote" in first_item:
            return " post-card-trivia"
    if p.get("opinion"):
        return " post-card-opinion"
    return ""


def post_card(p, root: str, featured: bool = False, compact: bool = False) -> str:
    """`featured` and `compact` reuse the exact same inner markup/classes
    (.post-card-image, .post-card-body, .post-card-title) as a plain card
    — only the outer class list differs — so each variant's actual visual
    behavior lives entirely in CSS (.post-card-featured for the full-width
    desktop breakout, .post-card-compact for the mobile-only horizontal
    row) rather than needing separate rendering logic here."""
    cls = "post-card"
    if featured:
        cls += " post-card-featured"
    if compact:
        cls += " post-card-compact"
    cls += post_accent_class(p)
    return f"""    <a class="{cls}" href="{root}posts/{esc(p['slug'])}/index.html">
      <div class="post-card-image"><img src="{esc(p['cover_image'])}" alt="{esc(p['title'])}" loading="lazy"></div>
      <div class="post-card-body">
        {category_pill(p.get('category', ''))}{game_type_pill(p)}{opinion_pill() if p.get('opinion') else ''}
        <p class="post-card-title">{esc(p['title'])}</p>
      </div>
    </a>
"""


def post_grid_html(posts: list, root: str) -> str:
    """Renders an ordered list of posts as .post-card entries, applying one
    positional rhythm-break that reads differently per viewport to keep a
    long feed from reading as one monotonous run of identical cards.

    Every FEATURED_INTERVAL-th post is "featured"; every other post is
    "compact". The two CSS classes only do anything at opposite ends of the
    viewport range, so the same underlying cycle produces two different
    effects for free:
      - On wide screens, .post-card-featured spans the full grid width
        (CSS that existed fully styled but was never wired up until now)
        and .post-card-compact has no effect there — a normal card.
      - On narrow screens, .post-grid has already collapsed to one column,
        where "full width" looks identical to a normal card, so instead
        .post-card-compact switches to a compact horizontal thumbnail+title
        row there (adapting the same shape already used for the homepage
        hero's small trending items), giving a repeating "1 full-size card,
        then FEATURED_INTERVAL-1 compact rows" pattern down the mobile feed.
        .post-card-featured still renders as an ordinary full-size stacked
        card on mobile (just not compact) — that's what supplies the "1
        full-size" anchor in the cycle.

    FEATURED_INTERVAL is 7 rather than a rounder number like 6, because on
    desktop, with the featured slot always the last of its window, the
    remaining INTERVAL-1 posts need to be a clean multiple of 3 to fill
    complete 3-column rows — 6 isn't (5 regular cards left a dangling short
    row), 7 is (6 regular cards divides into two full rows). An earlier
    version of this also tried preferring whichever post was Quiz/Trivia-
    typed for the featured slot regardless of its position in the window,
    which broke this exact guarantee — visible as a real gap on the live
    homepage — so the position is now unconditional, always the fixed last
    slot, never content-dependent."""
    FEATURED_INTERVAL = 7
    parts = []
    for i, p in enumerate(posts):
        featured = i % FEATURED_INTERVAL == FEATURED_INTERVAL - 1
        compact = not featured
        parts.append(post_card(p, root, featured=featured, compact=compact))
    return "".join(parts)


def share_row(canonical_path: str, title: str, label: str = "Share this", share_text: str = None, row_id: str = None) -> str:
    """Share links for a page, covering the platforms people actually use
    to spread this kind of content (X, Bluesky, Facebook, Reddit, WhatsApp,
    email) plus a one-click Copy Link for anywhere else — Discord,
    Instagram bio, text messages, whatever doesn't have its own
    share-intent URL. `share_text` lets a caller (e.g. a quiz result) put
    custom copy in the tweet/message body while still linking back to the
    same canonical page — falls back to just `title` when not given.

    `row_id` is only needed when the share text can't be known at build
    time — e.g. a score that only exists after someone actually plays a
    round (see the poster-guess/year-guess/trivia game-complete banners).
    Those callers pass generic fallback copy here (so the row works even
    if JS never runs) and script.js finds this row by id once the real
    score is known, then rewrites each link's href in place rather than
    replacing any markup."""
    url_raw = f"{SITE['url']}{canonical_path}"
    url = quote(url_raw, safe="")
    text = quote(share_text or title)
    twitter = f"https://twitter.com/intent/tweet?text={text}&url={url}"
    # Bluesky's compose intent only takes a single "text" param — no
    # separate url field like Twitter's — so the link has to be folded
    # into the same string and the WHOLE thing quoted together, rather
    # than reusing the already-encoded `text`/`url` pieces above.
    bluesky = f"https://bsky.app/intent/compose?text={quote(f'{share_text or title} {url_raw}')}"
    facebook = f"https://www.facebook.com/sharer/sharer.php?u={url}"
    reddit = f"https://www.reddit.com/submit?url={url}&title={text}"
    whatsapp = f"https://api.whatsapp.com/send?text={text}%20{url}"
    email = f"mailto:?subject={quote(title)}&body={text}%20{url}"
    # data-method lets script.js fire a clean "share_click" GA event per
    # button without having to parse aria-label text; the Copy Link button
    # also uses data-url (the plain, non-percent-encoded address) for the
    # actual clipboard write.
    id_attr = f' id="{esc(row_id)}"' if row_id else ""
    return f"""  <div class="share-row"{id_attr}>
    <span class="share-label">{esc(label)}</span>
    <a href="{twitter}" target="_blank" rel="noopener" aria-label="Share on X/Twitter" data-method="twitter">𝕏</a>
    <a href="{bluesky}" target="_blank" rel="noopener" aria-label="Share on Bluesky" data-method="bluesky">🦋</a>
    <a href="{facebook}" target="_blank" rel="noopener" aria-label="Share on Facebook" data-method="facebook">f</a>
    <a href="{reddit}" target="_blank" rel="noopener" aria-label="Share on Reddit" data-method="reddit">r/</a>
    <a href="{whatsapp}" target="_blank" rel="noopener" aria-label="Share on WhatsApp" data-method="whatsapp">💬</a>
    <a href="{email}" aria-label="Share by email" data-method="email">✉</a>
    <button type="button" class="share-copy-btn" data-method="copy" data-url="{esc(url_raw)}" aria-label="Copy link">🔗</button>
  </div>
"""


def reaction_strip(slug: str, prompt: str = "React to this post") -> str:
    buttons = "".join(
        f'<button class="reaction-btn" data-slug="{esc(slug)}" data-reaction="{label}">'
        f'<span class="reaction-emoji">{emoji}</span><span class="reaction-label">{label}</span>'
        f'<span class="reaction-count" data-count="0">0</span></button>'
        for emoji, label in REACTIONS
    )
    return f"""  <div class="reaction-strip">
    <p class="reaction-prompt">{esc(prompt)}</p>
    <div class="reaction-buttons">{buttons}</div>
  </div>
"""


# --------------------------------------------------------------------------
# Page builders
# --------------------------------------------------------------------------

def trending_hero_card(p, root: str, number: int) -> str:
    """The big card in the top-left of the trending grid: image fills the
    whole card, headline + number sit on a dark gradient scrim over the
    bottom of the image — BuzzFeed's classic #1 trending-story treatment."""
    return f"""    <a class="trending-card trending-hero" href="{root}posts/{esc(p['slug'])}/index.html">
      <div class="trending-hero-image">
        <img src="{esc(p['cover_image'])}" alt="{esc(p['title'])}" loading="lazy">
        <div class="trending-hero-scrim">
          <span class="trending-number">{number}</span>
          <p class="trending-hero-title">{esc(p['title'])}</p>
        </div>
      </div>
    </a>
"""


def trending_small_card(p, root: str, number: int) -> str:
    """Cards #2-4 in the trending grid: number badge on the image corner,
    headline sits below the image (not overlaid) — smaller and plainer than
    the hero card, same pattern BuzzFeed uses for its other trending slots."""
    return f"""    <a class="trending-card trending-item" href="{root}posts/{esc(p['slug'])}/index.html">
      <div class="trending-item-image">
        <img src="{esc(p['cover_image'])}" alt="{esc(p['title'])}" loading="lazy">
        <span class="trending-number">{number}</span>
      </div>
      <p class="trending-item-title">{esc(p['title'])}</p>
    </a>
"""


def render_home(posts, trailers: list, engagement: dict) -> str:
    root = ""
    # The top hero+small-card treatment always stays newest-first — it's a
    # fixed, prominent "what just happened" slot, not something a visitor
    # toggles. The plain grid below it (everything else) is what the
    # Newest/Trending tabs actually switch between; both tabs pull from the
    # same "rest" set so nothing appears twice or disappears when switching.
    trending, rest = posts[:4], posts[4:]
    small_cards = "".join(trending_small_card(p, root, i + 2) for i, p in enumerate(trending[1:]))
    trending_html = (
        trending_hero_card(trending[0], root, 1)
        + f'    <div class="trending-items">\n{small_cards}    </div>\n'
    )
    newest_grid = post_grid_html(rest, root)
    trending_grid = post_grid_html(trending_order(rest, engagement), root)
    toggle_html = view_toggle("home", [
        ("newest", "Newest", f'    <div class="post-grid">\n{newest_grid}\n    </div>\n'),
        ("trending", "🔥 Trending", f'    <div class="post-grid">\n{trending_grid}\n    </div>\n'),
    ])
    today = datetime.now().strftime("%m.%d.%y")

    trailers_html = ""
    if trailers:
        cards = "".join(trailer_card(t, root) for t in trailers)
        trailers_html = f"""  <section class="trailer-shelf">
    <h2 class="section-heading">🎥 Latest Trailers</h2>
    <div class="trailer-scroll">
{cards}    </div>
    <a class="see-all-link" href="{root}trailers/index.html">See All Trailers →</a>
  </section>
"""
    # The hero doubles as a movie slate: a ruled "slate info" strip (PROD /
    # SCENE / TAKE / DATE, the same fields printed on a real clapperboard)
    # sits above the headline, with TAKE standing in for the live post count.
    body = f"""  <section class="hero">
    <div class="hero-slate-info">
      <span>PROD <strong>{esc(SITE['name'])}</strong></span>
      <span>SCENE <strong>Home</strong></span>
      <span>TAKE <strong>{len(posts):02d}</strong></span>
      <span>DATE <strong>{today}</strong></span>
    </div>
    <h1>{esc(SITE['tagline'])}</h1>
  </section>

  <div class="trending-grid">
{trending_html}
  </div>
  <a class="see-all-link" href="{root}posts/index.html">See All Posts →</a>

{trailers_html}
{flickle_cta()}
{toggle_html}"""
    return base_page(
        f"{SITE['name']} — {SITE['tagline']}",
        SITE["description"],
        "/",
        body,
        root,
    )


def render_posts_index(posts, engagement: dict, category: str = None) -> str:
    root = "../" if category is None else "../../"
    title = f"{CATEGORY_EMOJI.get(category, '')} {category}".strip() if category else "All Posts"
    canonical = f"/posts/{CATEGORY_SLUGS[category]}/" if category else "/posts/"
    group_id = f"posts-{CATEGORY_SLUGS[category]}" if category else "posts-all"
    newest_grid = post_grid_html(posts, root)
    trending_grid = post_grid_html(trending_order(posts, engagement), root)
    toggle_html = view_toggle(group_id, [
        ("newest", "Newest", f'    <div class="post-grid">\n{newest_grid}\n    </div>\n'),
        ("trending", "🔥 Trending", f'    <div class="post-grid">\n{trending_grid}\n    </div>\n'),
    ])
    body = f"""  <section class="hero">
    <h1>{esc(title)}</h1>
    <p>{len(posts)} post{"s" if len(posts) != 1 else ""}{f" in {esc(category)}" if category else ""}.</p>
  </section>

{toggle_html}"""
    return base_page(
        f"{esc(title)} | {SITE['name']}",
        f"Every {category.lower() if category else 'post'} post on The Clapperboard.",
        canonical,
        body,
        root,
    )


def render_list_item(item, root: str) -> str:
    """A normal numbered listicle item, supporting one or more side-by-side images.

    Layout note: the number + heading + body text sit in a narrower centered
    column (.list-item-text), while the image(s) run the full width of the
    item — that width contrast is what gives the images visual weight and
    keeps the reading column from feeling packed edge-to-edge."""
    images = item.get("images")
    if not images:
        # Back-compat: a single "image"/"image_alt" pair is still accepted.
        images = [{"url": item["image"], "caption": item.get("image_alt", "")}] if item.get("image") else []

    # A single image displays at its own natural size (no cropping, no
    # letterbox box needed since there's nothing to visually balance it
    # against). Two or more images get wrapped in equal-size, letterboxed
    # boxes — see the .img-box comment in style.css for why.
    multi = len(images) > 1

    def render_figure(img):
        if multi:
            body = f'<div class="img-box"><img src="{esc(img["url"])}" alt="{esc(img.get("caption", ""))}" loading="lazy"></div>'
        else:
            body = f'<img src="{esc(img["url"])}" alt="{esc(img.get("caption", ""))}" loading="lazy">'
        caption = f'<figcaption>{esc(img["caption"])}</figcaption>' if img.get("caption") else ""
        return f"<figure>{body}{caption}</figure>"

    figures = "".join(render_figure(img) for img in images)
    images_html = f'<div class="list-item-images">{figures}</div>' if figures else ""
    trailer = youtube_embed(item.get("trailer_key", ""))

    return f"""  <div class="list-item">
    <div class="list-item-text">
      <span class="list-item-number">{item['number']}</span>
      <h2>{esc(item['heading'])}</h2>
    </div>
    {images_html}
{trailer}
    <div class="list-item-text">
      <p>{esc(item['text'])}</p>
    </div>
  </div>
"""


def youtube_embed(key: str) -> str:
    """A responsive, lazy-loaded YouTube embed. Uses the -nocookie domain
    (no tracking cookies set until the viewer actually presses play) and
    loading="lazy" so it doesn't fetch anything while the <details> reveal
    it lives in is still closed."""
    if not key:
        return ""
    return (
        '<div class="video-embed">'
        f'<iframe src="https://www.youtube-nocookie.com/embed/{esc(key)}" '
        'title="YouTube trailer" loading="lazy" frameborder="0" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" '
        'allowfullscreen></iframe></div>'
    )


def trailer_primary_embed(t: dict) -> str:
    """The single newest trailer/teaser scripts/update_trailers.py found
    for this movie — shown flush at the top of the page exactly like
    before. Any others (see trailer_extra_videos_html) are handled
    separately further down the page rather than stacked here, so a
    heavily-marketed movie with several videos on file doesn't turn into a
    wall of embeds before the title and overview even show up."""
    videos = t.get("videos") or []
    return youtube_embed(videos[0]["key"]) if videos else ""


def trailer_extra_videos_html(t: dict) -> str:
    """Big releases regularly rack up a teaser plus one or more full
    trailers over time — rather than picking just one and losing the rest,
    anything beyond the primary trailer at the top of the page (see
    trailer_primary_embed) gets listed here, each labeled with TMDB's own
    name for that video (e.g. "Official Trailer" vs "Teaser Trailer") so
    it's clear which is which. Renders to nothing for the common
    single-trailer case."""
    videos = (t.get("videos") or [])[1:]
    if not videos:
        return ""
    blocks = "".join(
        f'<div class="trailer-video-block"><p class="trailer-video-label">{esc(v.get("name") or v.get("type") or "Trailer")}</p>{youtube_embed(v["key"])}</div>'
        for v in videos
    )
    return f'<div class="trailer-extra-videos"><p class="trailer-extra-heading">More trailers for this movie</p>{blocks}</div>'


def trailer_card(t: dict, root: str) -> str:
    """A compact card for the homepage's horizontal-scrolling trailer shelf.
    Links straight to that movie's own trailer page (each trailer gets one —
    see render_trailer_page) rather than to YouTube directly, so playing it
    still counts as an on-site pageview and keeps the visitor a click away
    from the rest of the site."""
    image = t.get("backdrop") or t.get("poster", "")
    slug = slugify(t["title"])
    return f"""    <a class="trailer-card" href="{root}trailers/{slug}/index.html">
      <div class="trailer-card-image">
        <img src="{esc(image)}" alt="{esc(t['title'])}" loading="lazy">
        <span class="trailer-card-play">▶</span>
      </div>
      <p class="trailer-card-title">{esc(t['title'])}</p>
      <div class="trailer-card-date">{theater_status_pill(t.get('release_date'), t.get('theatrical', True))}</div>
    </a>
"""


def trailer_index_card(t: dict, root: str) -> str:
    """A card on the /trailers/ listing page. Shares the same card "pop"
    hover (lift + shadow + image zoom) as .post-card so it still feels like
    part of the same site, but is deliberately its own look rather than a
    reused post card: a widescreen 16:9 thumbnail (actual video framing,
    not a 4:3 photo crop), a persistent play badge so it reads as "video"
    at a glance even before hovering, and a mono/uppercase date styled
    after the clapperboard-slate look used in the hero/category nav —
    instead of the gold category pill every post card gets — so a page
    full of trailers doesn't look like it could be mistaken for a page of
    articles."""
    image = t.get("backdrop") or t.get("poster", "")
    slug = slugify(t["title"])
    return f"""    <a class="trailer-index-card" href="{root}trailers/{slug}/index.html">
      <div class="trailer-index-image">
        <img src="{esc(image)}" alt="{esc(t['title'])}" loading="lazy">
        <span class="trailer-index-play">▶</span>
      </div>
      <div class="trailer-index-body">
        <div class="trailer-index-date">{theater_status_pill(t.get('release_date'), t.get('theatrical', True))}</div>
        <p class="trailer-index-title">{esc(t['title'])}</p>
      </div>
    </a>
"""


def render_trailers_page(trailers: list) -> str:
    """The /trailers/ index — a browsable grid of cards (reusing
    .post-grid/.post-card, identical to /posts/), each linking out to that
    movie's own dedicated page where the trailer actually plays. Watching a
    trailer is a separate click/pageview from browsing the list, same as
    reading a post is separate from browsing the post grid.

    "In Theaters Now" and "Coming Soon" represent genuinely different
    visitor intent (what can I watch tonight vs. what should I be excited
    about later) rather than just two orderings of the same list, so this
    is a real filter — not a resort like Newest/Trending — with "All"
    (today's full popularity-ranked mix, unchanged) as the default so
    nobody's existing experience of this page changes unless they
    deliberately pick a narrower tab.

    Released movies are split further into theatrical vs. streaming-only
    (see the `theatrical` field resolved in update_trailers.py) — a movie
    that only ever premiered on a streaming service was never something a
    visitor could "go watch tonight" in the theatrical sense the "In
    Theaters Now" tab promises, so it gets its own honest tab instead of
    just diluting that one with off-topic, often lower-profile titles."""
    root = "../"
    released = [t for t in trailers if not is_upcoming(t.get("release_date", ""))]
    upcoming = [t for t in trailers if is_upcoming(t.get("release_date", ""))]
    theaters = [t for t in released if t.get("theatrical", True)]
    streaming = [t for t in released if not t.get("theatrical", True)]

    def grid(items, empty_message):
        if not items:
            return f'    <p class="trailer-empty">{esc(empty_message)}</p>\n'
        cards = "".join(trailer_index_card(t, root) for t in items)
        return f'    <div class="post-grid">\n{cards}    </div>\n'

    toggle_html = view_toggle("trailers", [
        ("all", "All", grid(trailers, "No trailers on file right now — check back soon.")),
        ("now", "🎬 In Theaters Now", grid(theaters, "Nothing currently in theaters on file right now.")),
        ("streaming", "📺 Streaming Now", grid(streaming, "Nothing streaming-only on file right now.")),
        ("soon", "🍿 Coming Soon", grid(upcoming, "Nothing upcoming on file right now — check back soon.")),
    ])

    body = f"""  <section class="hero">
    <h1>🎥 Latest Movie Trailers</h1>
    <p>The newest trailers for the movies everyone's about to be talking about — check back often, this shelf keeps growing.</p>
  </section>

{toggle_html}
{flickle_cta()}
"""
    return base_page(
        f"Latest Movie Trailers | {SITE['name']}",
        "The newest trailers for the most-anticipated upcoming movies, all in one place.",
        "/trailers/",
        body,
        root,
    )


def render_trailer_page(t: dict) -> str:
    """A trailer's own dedicated page — one per movie, same URL depth as a
    regular post page (docs/trailers/<slug>/index.html). Reactions and
    share both reuse the exact same components/JS as regular posts (see
    reaction_strip/share_row), keyed to this specific trailer so reacting
    or sharing here is independent of every other trailer's page."""
    root = "../../"
    slug = slugify(t["title"])
    canonical_path = f"/trailers/{slug}/"
    react_slug = f"trailer-{t['id']}"
    share_text = f"🎬 The trailer for \"{t['title']}\" just dropped — watch it:"
    body = f"""  <nav class="breadcrumb"><a href="{root}trailers/index.html">← All Trailers</a></nav>

  <div class="trailer-page-card">
    {trailer_primary_embed(t)}
    <div class="trailer-page-body">
      <div class="trailer-page-date">{theater_status_pill(t.get('release_date'), t.get('theatrical', True))}</div>
      <h1 class="trailer-page-title">{esc(t['title'])}</h1>
      <p class="trailer-page-overview">{esc(t.get('overview', ''))}</p>
{trailer_extra_videos_html(t)}
{share_row(canonical_path, t['title'], label="Share this trailer", share_text=share_text)}
{reaction_strip(react_slug, prompt="React to this trailer")}
    </div>
  </div>

{flickle_cta("Now go prove it — play today's Flickle.")}
"""
    return base_page(
        f"{t['title']} Trailer | {SITE['name']}",
        (t.get("overview") or f"Watch the trailer for {t['title']}.")[:160],
        canonical_path,
        body,
        root,
        image=t.get("backdrop") or t.get("poster", ""),
    )


def render_emoji_item(item, root: str, slug: str, post_title: str) -> str:
    """A guess-the-movie emoji clue with a native <details> reveal.

    The share row inside the reveal only ever becomes visible once that
    reveal is actually opened (same principle as a quiz result's share
    row only appearing after the quiz is done) — and its copy never
    spoils the answer, just re-poses the same emoji clue as a challenge,
    so whoever it's shared to is enticed to click and try it rather than
    just being told the answer secondhand. Deep-links to this specific
    clue's own anchor on the page rather than just the post's URL."""
    trailer = youtube_embed(item.get("trailer_key", ""))
    anchor_path = f"/posts/{slug}/#item-{item['number']}"
    share_text = f"Can you guess this movie from just the emoji? {item['emoji']} I got it — bet you can't:"
    return f"""  <div class="list-item" id="item-{item['number']}">
    <div class="list-item-text">
      <span class="list-item-number">{item['number']}</span>
    </div>
    <div class="emoji-clue">{item['emoji']}</div>
    <details class="reveal" data-item="{item['number']}">
      <summary>Reveal the answer</summary>
      <div class="reveal-body">
        <img src="{esc(item['reveal_image'])}" alt="{esc(item['reveal_title'])}" loading="lazy">
        <div>
          <p class="reveal-eyebrow">Answer</p>
          <p class="reveal-title">{esc(item['reveal_title'])}</p>
          <p class="reveal-quote">&ldquo;{esc(item['reveal_text'])}&rdquo;</p>
        </div>
      </div>
{share_row(anchor_path, post_title, label="Challenge a friend", share_text=share_text)}
{trailer}
    </details>
  </div>
"""


def render_quote_item(item, root: str, slug: str, post_title: str) -> str:
    """A guess-the-movie-from-its-quote item — same reveal mechanic (and
    same post-reveal share row, see render_emoji_item) as the emoji format,
    but sized/styled for sentence-length text instead of a few large emoji
    characters."""
    trailer = youtube_embed(item.get("trailer_key", ""))
    anchor_path = f"/posts/{slug}/#item-{item['number']}"
    share_text = f'Can you guess this movie from just one quote? "{item["quote"]}" I got it — bet you can\'t:'
    return f"""  <div class="list-item" id="item-{item['number']}">
    <div class="list-item-text">
      <span class="list-item-number">{item['number']}</span>
    </div>
    <blockquote class="quote-clue">&ldquo;{esc(item['quote'])}&rdquo;</blockquote>
    <details class="reveal" data-item="{item['number']}">
      <summary>Reveal the answer</summary>
      <div class="reveal-body">
        <img src="{esc(item['reveal_image'])}" alt="{esc(item['reveal_title'])}" loading="lazy">
        <div>
          <p class="reveal-eyebrow">Answer</p>
          <p class="reveal-title">{esc(item['reveal_title'])}</p>
        </div>
      </div>
{share_row(anchor_path, post_title, label="Challenge a friend", share_text=share_text)}
{trailer}
    </details>
  </div>
"""


def render_poster_guess(pg: dict, slug: str, root: str, post_title: str) -> str:
    """Zoomed-poster guessing game. The poster starts heavily zoomed in —
    a CSS custom property (--pg-zoom) driving `transform: scale()` on the
    plain poster image, not a separately-cropped asset — alongside up to
    four multiple-choice title buttons. A wrong pick (script.js) disables
    that button and zooms the poster out one notch, so the puzzle is
    always eventually solvable (choices only ever shrink) rather than
    able to genuinely stump anyone forever. Score is simply how many
    wrong picks it took.

    Originally this also chained straight into a guess-the-year slider
    per movie, but that got split out into its own standalone format (see
    render_year_guess() below) — bundling both into one round added a
    second interaction after the win that not everyone wants, muddied a
    single clean "solved in N tries" score into two different kinds of
    stats, and made for a wordier social pitch than "guess the movie from
    a zoomed poster" on its own.

    Wrong-answer choices are drawn from the OTHER movies already in this
    same post rather than needing any extra API or LLM call — they're
    already thematically related (same post, usually same era/franchise/
    actor), so this is both free to generate and coherent to play. Below
    2 movies in the post this quietly renders with fewer/no choices
    rather than failing to build."""
    movies = pg["movies"]
    total = len(movies)

    blocks = []
    for i, m in enumerate(movies):
        n = i + 1
        others = [o["title"] for j, o in enumerate(movies) if j != i]
        decoys = random.sample(others, k=min(3, len(others)))
        choices = decoys + [m["title"]]
        random.shuffle(choices)
        choice_parts = []
        for c in choices:
            correct_attr = ' data-correct="true"' if c == m["title"] else ""
            choice_parts.append(f'        <button type="button" class="poster-guess-choice"{correct_attr}>{esc(c)}</button>\n')
        choices_html = "".join(choice_parts)
        blocks.append(f'''  <div class="list-item poster-guess-item" id="pg-item-{n}" data-title="{esc(m["title"])}">
    <div class="list-item-text">
      <span class="list-item-number">{n}</span>
    </div>
    <div class="poster-guess-frame">
      <img src="{esc(m["poster"])}" alt="Mystery movie poster" loading="lazy" class="poster-guess-img" style="--pg-zoom: 4.5">
    </div>
    <p class="poster-guess-status" aria-live="polite"></p>
    <div class="poster-guess-choices">
{choices_html}    </div>
  </div>
''')

    intro = f'<p class="quiz-intro">{esc(pg["intro"])}</p>' if pg.get("intro") else ""

    score_share = share_row(
        f"/posts/{slug}/", post_title,
        label="Share your score",
        share_text=f"I just played The Clapperboard's zoomed-poster movie game 🎬",
        row_id="poster-guess-share",
    )

    return f"""  <div class="poster-guess" data-poster-guess="{esc(slug)}">
    {intro}
{"".join(blocks)}    <div class="game-complete" id="game-complete" hidden>
      <p class="game-complete-text">🎬 You cleared all {total}!</p>
      <p class="game-complete-sub" id="poster-guess-summary"></p>
{score_share}    </div>
  </div>
"""


def render_year_guess(yg: dict, slug: str, root: str, post_title: str) -> str:
    """Guess-the-release-year mini-game. Standalone sibling to
    render_poster_guess() above (see that function's docstring for why
    they were split apart rather than chained into one round) — this
    format shows the FULL poster/still plus the movie's title (there's
    nothing to identify here, the challenge is purely "how well do you
    know movie chronology") and a single year-guess slider, scored by how
    far off the guess lands rather than flat right/wrong — the same
    spirit as GeoGuessr's distance scoring.

    The slider's range is the exact same wide span on every single round
    (1960 through the current year) — deliberately NOT narrowed anywhere
    near any particular movie's real year, which would just leak the
    answer by making the slider itself a hint."""
    movies = yg["movies"]
    total = len(movies)
    current_year = datetime.now().year
    slider_min = 1960
    mid_year = (slider_min + current_year) // 2

    blocks = []
    for i, m in enumerate(movies):
        n = i + 1
        blocks.append(f'''  <div class="list-item year-guess-item" id="yg-item-{n}" data-year="{m["year"]}">
    <div class="list-item-text">
      <span class="list-item-number">{n}</span>
    </div>
    <div class="year-guess-frame">
      <img src="{esc(m["image"])}" alt="{esc(m["title"])}" loading="lazy">
    </div>
    <p class="year-guess-title">{esc(m["title"])}</p>
    <div class="year-guess-controls">
      <input type="range" class="year-guess-slider" min="{slider_min}" max="{current_year}" value="{mid_year}" step="1">
      <output class="year-guess-output">{mid_year}</output>
    </div>
    <button type="button" class="year-guess-lock">Lock in guess</button>
    <p class="year-guess-result" aria-live="polite" hidden></p>
  </div>
''')

    intro = f'<p class="quiz-intro">{esc(yg["intro"])}</p>' if yg.get("intro") else ""

    score_share = share_row(
        f"/posts/{slug}/", post_title,
        label="Share your score",
        share_text=f"I just guessed movie release years on The Clapperboard 🗓️",
        row_id="year-guess-share",
    )

    return f"""  <div class="year-guess" data-year-guess="{esc(slug)}">
    {intro}
{"".join(blocks)}    <div class="game-complete" id="game-complete" hidden>
      <p class="game-complete-text">🗓️ You guessed all {total}!</p>
      <p class="game-complete-sub" id="year-guess-summary"></p>
{score_share}    </div>
  </div>
"""


def render_quiz(quiz: dict, slug: str, root: str, quiz_title: str) -> str:
    """A self-scoring 'which character are you' personality quiz. Entirely
    client-side (see the quiz block in assets/script.js). Only one question
    is ever in the DOM's visible flow at a time — a progress bar tracks
    position, and picking an answer auto-advances to the next question —
    so the page never shows all the answer text for every question at once.
    After the last question, the checked-radio tally reveals the matching
    .quiz-result card. Nothing is sent anywhere — no backend, no accounts.

    Every question is expected to offer exactly one answer per possible
    result (see content schema), so however someone answers, the tally stays
    a fair, evenly-weighted count instead of favoring any one outcome.

    Each result card gets its own share row baked in at build time (all
    possible results are known upfront, so there's no need for any
    JS-side URL building) with share copy naming that specific result.
    Sharing links to that result's own dedicated page (see
    render_quiz_result_page) rather than the quiz post itself, so the
    social-media card that unfurls shows that specific character's photo
    instead of the quiz's generic cover image."""
    total = len(quiz["questions"])
    q_blocks = []
    for qi, q in enumerate(quiz["questions"], start=1):
        answers = "".join(
            f'''        <label class="quiz-answer">
          <input type="radio" name="q{qi}" value="{esc(a["result"])}">
          <span class="quiz-answer-badge">{chr(64 + ai)}</span>
          <span class="quiz-answer-text">{esc(a["text"])}</span>
        </label>
'''
            for ai, a in enumerate(q["answers"], start=1)
        )
        active = " active" if qi == 1 else ""
        q_blocks.append(f'''    <fieldset class="quiz-question{active}" data-q="{qi}">
      <legend>{esc(q["question"])}</legend>
      <div class="quiz-answers">
{answers}      </div>
    </fieldset>
''')

    results_html = "".join(f'''    <div class="quiz-result" data-result="{esc(r["key"])}" hidden>
      <img src="{esc(r["image"])}" alt="{esc(r["name"])}" loading="lazy">
      <div class="quiz-result-text">
        <p class="quiz-result-eyebrow">You Got</p>
        <h3 class="quiz-result-name">{esc(r["name"])}</h3>
        <p class="quiz-result-subtitle">{esc(r["subtitle"])}</p>
        <p class="quiz-result-desc">{esc(r["description"])}</p>
        <div class="quiz-result-actions">
{share_row(f"/posts/{slug}/result/{r['key']}/", quiz_title, label="Share your result", share_text=f'I got {r["name"]} on "{quiz_title}"! Take the quiz:')}          <button type="button" class="quiz-retake-btn">↻ Retake The Quiz</button>
        </div>
      </div>
    </div>
''' for r in quiz["results"])

    intro = f'<p class="quiz-intro">{esc(quiz["intro"])}</p>' if quiz.get("intro") else ""

    return f"""  <div class="quiz" data-quiz="{esc(slug)}">
    {intro}
    <div class="quiz-questions">
      <div class="quiz-progress-track"><div class="quiz-progress-fill" style="width: {100 / total:.1f}%"></div></div>
      <p class="quiz-progress-label">Question <span class="quiz-progress-current">1</span> of {total}</p>
{"".join(q_blocks)}    </div>
    <div class="quiz-results" hidden>
{results_html}    </div>
  </div>
"""


def render_quiz_result_page(slug: str, quiz_title: str, r: dict) -> str:
    """A tiny standalone landing page for one specific quiz result — the
    actual target of that result's "Share your result" button (see
    render_quiz above). Its whole reason to exist is the og:image: set to
    this exact result's own photo rather than the quiz's generic cover
    image, so sharing "I got Peter Parker" actually unfurls into a social
    card showing Peter Parker, not a generic quiz thumbnail — the single
    biggest lever for making a shared quiz result actually look enticing to
    click. Visitors who land here from a shared link see the result plus a
    clear, one-click way to go take the quiz themselves."""
    root = "../../../../"
    canonical_path = f"/posts/{slug}/result/{r['key']}/"
    share_text = f'I got {r["name"]} on "{quiz_title}"! Take the quiz:'
    quiz_href = f"{root}posts/{slug}/index.html"

    body = f"""  <nav class="breadcrumb"><a href="{quiz_href}">← Take &quot;{esc(quiz_title)}&quot;</a></nav>

  <div class="quiz-result quiz-result-standalone">
    <img src="{esc(r['image'])}" alt="{esc(r['name'])}" loading="lazy">
    <div class="quiz-result-text">
      <p class="quiz-result-eyebrow">They Got</p>
      <h1 class="quiz-result-name">{esc(r['name'])}</h1>
      <p class="quiz-result-subtitle">{esc(r['subtitle'])}</p>
      <p class="quiz-result-desc">{esc(r['description'])}</p>
      <div class="quiz-result-actions">
{share_row(canonical_path, quiz_title, label="Share this result", share_text=share_text)}
        <a class="quiz-retake-btn" href="{quiz_href}">Take the Quiz →</a>
      </div>
    </div>
  </div>

{flickle_cta("Now go prove it — play today's Flickle.")}
"""
    return base_page(
        f"I got {r['name']}! | {quiz_title} | {SITE['name']}",
        r.get("description") or r.get("subtitle") or f'Take "{quiz_title}" to find out which result you get.',
        canonical_path,
        body,
        root,
        image=r.get("image", ""),
    )


def post_schema(p) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": p.get("title"),
        "description": p.get("dek", ""),
        "datePublished": p.get("date"),
        "image": p.get("cover_image", ""),
        "author": {"@type": "Organization", "name": SITE["name"]},
    }
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


MIN_RELATED = 6  # "Keep Reading" always shows at least this many rich cards


def pick_related_posts(p, posts_by_slug: dict) -> list:
    """The post's own hand-picked `related` list, topped up with other posts
    (same category first) if it doesn't reach MIN_RELATED — this section is
    one of the main things keeping a reader on the site, so it should never
    render as just one or two sparse cards."""
    chosen_slugs = [
        r["slug"] for r in p.get("related", [])
        if r.get("slug") in posts_by_slug and r["slug"] != p["slug"]
    ]

    if len(chosen_slugs) < MIN_RELATED:
        already = set(chosen_slugs) | {p["slug"]}
        candidates = [s for s in posts_by_slug if s not in already]
        same_category = [s for s in candidates if posts_by_slug[s].get("category") == p.get("category")]
        other = [s for s in candidates if s not in same_category]
        chosen_slugs += (same_category + other)[: MIN_RELATED - len(chosen_slugs)]

    return [posts_by_slug[s] for s in chosen_slugs[:MIN_RELATED]]


def render_post_page(p, posts_by_slug: dict) -> str:
    root = "../../"
    canonical_path = f"/posts/{p['slug']}/"

    trivia_items = [item for item in p.get("items", []) if "emoji" in item or "quote" in item]
    is_quiz = bool(p.get("quiz"))
    is_poster_guess = bool(p.get("poster_guess"))
    is_year_guess = bool(p.get("year_guess"))
    is_trivia = not is_quiz and not is_poster_guess and not is_year_guess and len(trivia_items) > 0
    is_game = is_quiz or is_trivia or is_poster_guess or is_year_guess

    items_html = []
    if p.get("quiz"):
        items_html.append(render_quiz(p["quiz"], p["slug"], root, p["title"]))
    elif p.get("poster_guess"):
        items_html.append(render_poster_guess(p["poster_guess"], p["slug"], root, p["title"]))
    elif p.get("year_guess"):
        items_html.append(render_year_guess(p["year_guess"], p["slug"], root, p["title"]))
    else:
        for item in p.get("items", []):
            if "emoji" in item:
                items_html.append(render_emoji_item(item, root, p["slug"], p["title"]))
            elif "quote" in item:
                items_html.append(render_quote_item(item, root, p["slug"], p["title"]))
            else:
                items_html.append(render_list_item(item, root))

    # A visitor arriving from a social link already knows this is a game —
    # the top CTA (a full "Play Flickle" promo card) used to sit between the
    # cover image and the first clue/question, meaning the actual payoff
    # they clicked for was pushed below an ad-like banner. Skipped here for
    # any game post (quiz or trivia); the bottom CTA ("now go prove it")
    # still runs after the game either way, so nothing is actually lost —
    # it was previously just duplicated in the one spot doing the most harm.
    top_cta = "" if is_game else flickle_cta()

    # Trivia-format games (unlike the quiz, which already has its own
    # progress bar) are a flat list with nothing pulling a visitor from clue
    # 1 toward clue N — no sense that anything is "in progress" or worth
    # finishing. This adds a small running counter (updated client-side in
    # script.js's reveal listener, which already fires once per opened
    # <details>) and a one-time completion message once every clue in the
    # post has been opened. Quiz posts are excluded — they already have an
    # equivalent mechanic — and plain listicles have nothing to count.
    progress_html = ""
    complete_html = ""
    if is_trivia:
        total = len(trivia_items)
        progress_html = f"""  <div class="list-item-text">
    <div class="game-progress" id="game-progress" data-total="{total}">
      <span class="game-progress-count">0</span> / {total} revealed
    </div>
  </div>
"""
        trivia_share = share_row(
            canonical_path, p["title"],
            label="Share your score",
            share_text=f"I just cleared all {total} in The Clapperboard's guessing game 🎯",
            row_id="trivia-complete-share",
        )
        complete_html = f"""  <div class="game-complete" id="game-complete" hidden>
    <p class="game-complete-text">🎉 You cleared all {total} — nice work.</p>
{trivia_share}  </div>
"""

    related_posts = pick_related_posts(p, posts_by_slug)
    related_html = ""
    if related_posts:
        cards = "".join(post_card(rp, root) for rp in related_posts)
        related_html = f"""  <section>
    <h2 class="section-heading">Keep Reading</h2>
    <div class="post-grid related-grid">{cards}</div>
  </section>
"""

    sources_html = ""
    if p.get("sources"):
        items = "".join(
            f'<li><a href="{esc(s["url"])}" target="_blank" rel="noopener">{esc(s["title"])}</a></li>'
            for s in p["sources"]
        )
        sources_html = f"""  <section>
    <h2 class="section-heading section-heading-small">Sources</h2>
    <ul class="sources-list">{items}</ul>
  </section>
"""

    meta_line = f"""    <p class="post-meta">By {esc(SITE['name'])} Staff · {esc(pretty_date(p.get('date')))} · {read_minutes(p)} min read</p>
"""

    body = f"""  <nav class="breadcrumb"><a href="{root}posts/index.html">← All Posts</a></nav>

  <div class="post-header">
    <div class="list-item-text">
      {category_pill(p.get('category', ''))}{game_type_pill(p)}{opinion_pill() if p.get('opinion') else ''}
      <h1>{esc(p['title'])}</h1>
      <p class="post-dek">{esc(p.get('dek', ''))}</p>
{meta_line}
    </div>
    <div class="post-cover"><img src="{esc(p['cover_image'])}" alt="{esc(p['title'])}" loading="lazy"></div>
    <div class="list-item-text">
{share_row(canonical_path, p['title'])}
    </div>
  </div>

{top_cta}

{progress_html}
{"".join(items_html)}

{complete_html}

{flickle_cta("Now go prove it — play today's Flickle.")}

{reaction_strip(p['slug'])}

  <div class="list-item-text post-bottom-share">
{share_row(canonical_path, p['title'], label="Enjoyed this? Share it")}
  </div>

  <div class="list-item-text">
{related_html}
{sources_html}
  </div>
"""
    return base_page(
        f"{p['title']} | {SITE['name']}",
        p.get("dek", ""),
        canonical_path,
        body,
        root,
        image=p.get("cover_image", ""),
        schema=post_schema(p),
    )


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def main():
    global HAS_TRAILERS

    posts = load_posts()
    if not posts:
        print("No posts found in content/posts/ — nothing to build.")
        return

    trailers = load_trailers()
    HAS_TRAILERS = bool(trailers)
    engagement = load_engagement()

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    shutil.copytree(ASSETS_DIR, OUT_DIR / "assets")

    # docs/ is wiped and rebuilt from scratch on every run (including the
    # automated one that runs 3x/day), so GitHub Pages' custom-domain CNAME
    # file has to be re-written here every time too — otherwise the very
    # next automated rebuild would silently delete it and break the domain.
    domain = SITE["url"].replace("https://", "").replace("http://", "")
    (OUT_DIR / "CNAME").write_text(domain + "\n")

    (OUT_DIR / "index.html").write_text(render_home(posts, trailers, engagement))

    if trailers:
        trailers_dir = OUT_DIR / "trailers"
        trailers_dir.mkdir()
        (trailers_dir / "index.html").write_text(render_trailers_page(trailers))
        for t in trailers:
            t_dir = trailers_dir / slugify(t["title"])
            t_dir.mkdir(exist_ok=True)
            (t_dir / "index.html").write_text(render_trailer_page(t))

    posts_dir = OUT_DIR / "posts"
    posts_dir.mkdir()
    (posts_dir / "index.html").write_text(render_posts_index(posts, engagement))

    for category, slug in CATEGORY_SLUGS.items():
        cat_posts = [p for p in posts if p.get("category") == category]
        if not cat_posts:
            continue
        cat_dir = posts_dir / slug
        cat_dir.mkdir(exist_ok=True)
        (cat_dir / "index.html").write_text(render_posts_index(cat_posts, engagement, category=category))

    posts_by_slug = {p["slug"]: p for p in posts}
    for p in posts:
        page_dir = posts_dir / p["slug"]
        page_dir.mkdir(exist_ok=True)
        (page_dir / "index.html").write_text(render_post_page(p, posts_by_slug))

        if p.get("quiz"):
            results_dir = page_dir / "result"
            results_dir.mkdir(exist_ok=True)
            for r in p["quiz"]["results"]:
                r_dir = results_dir / r["key"]
                r_dir.mkdir(exist_ok=True)
                (r_dir / "index.html").write_text(render_quiz_result_page(p["slug"], p["title"], r))

    (OUT_DIR / ".nojekyll").write_text("")

    today = datetime.now().strftime("%Y-%m-%d")
    urls = ["/", "/posts/"] + [f"/posts/{slug}/" for slug in CATEGORY_SLUGS.values()] + [f"/posts/{p['slug']}/" for p in posts]
    if trailers:
        urls.append("/trailers/")
        urls += [f"/trailers/{slugify(t['title'])}/" for t in trailers]
    sitemap_entries = "\n".join(
        f"  <url><loc>{SITE['url']}{u}</loc><lastmod>{today}</lastmod></url>" for u in urls
    )
    (OUT_DIR / "sitemap.xml").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{sitemap_entries}
</urlset>
""")

    (OUT_DIR / "robots.txt").write_text(f"""User-agent: *
Allow: /

Sitemap: {SITE['url']}/sitemap.xml
""")

    print(f"Built {len(posts)} posts + homepage + index into {OUT_DIR}")
    print(f"Double-click {OUT_DIR / 'index.html'} to preview — no server needed.")


if __name__ == "__main__":
    main()
