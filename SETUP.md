# Setup guide — The Clapperboard

This site is plain HTML/CSS/JS in a BuzzFeed-style listicle format. There is no Node/npm build
step — a Python script (`build_site.py`, standard library only) turns the JSON files in
`content/posts/` into real `.html` files in `docs/`, and GitHub Pages serves `docs/` directly.

## 1. Build once locally and check it

```
cd clapperboard
python3 build_site.py
```
Then just double-click `docs/index.html` — it opens directly in your browser, no server needed.

## 2. Push to GitHub

```
git init
git add .
git commit -m "Initial site"
git branch -M main
git remote add origin https://github.com/<your-username>/theclapperboard.git
git push -u origin main
```

## 3. Enable GitHub Pages

**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: main / docs**

Your site will be live at `https://<your-username>.github.io/theclapperboard/` within a minute
or two — no Actions run required for this part.

## 4. Get API keys (only needed to generate more posts)

- **TMDB** (actor headshots/movie posters — this is the image source for everything):
  free account at https://www.themoviedb.org → Settings → API → request a v3 API key.
  Usually approved instantly for personal/non-commercial use.
- **Anthropic** (researches and writes the listicle copy via web search): create a key at
  https://platform.claude.com (Console → API Keys). Web search is $10/1,000 searches plus token
  costs — each post uses up to ~15 searches, so a batch of a few posts runs a few dollars.

Add both as **Settings → Secrets and variables → Actions → New repository secret**:
`TMDB_API_KEY` and `ANTHROPIC_API_KEY`.

## 5. It runs itself — here's how

`content/posts/` already has 10 fully-researched posts across all three formats (actors who turned
down/lost roles, behind-the-scenes facts, budget overruns, movie props at auction, and
guess-the-movie games in both emoji-clue and famous-quote flavors) so you can judge real content
quality before turning on automation.

The workflow (**.github/workflows/update.yml**) runs on its own 3x a day (06:00, 13:00, 20:00 UTC)
with no action needed from you once the two secrets are set:

1. It processes any ideas still queued in `scripts/post_ideas.txt` first.
2. Once that queue runs dry, it has Claude invent brand-new topics itself — checking against every
   title already published so it doesn't just repeat itself — and appends them to
   `post_ideas.txt` as an audit trail (tagged with the date they were self-generated).
3. Each run is capped at `POSTS_PER_RUN` new posts (set to 2 in `generate_post.py`) regardless of
   how many ideas are available, so a single run can't blow through API budget unexpectedly. At
   3 runs/day and up to 2 posts/run, that's roughly 3-6 new posts/day.
4. It rebuilds `docs/` with `build_site.py` and commits+pushes `content/`, `docs/`, and
   `post_ideas.txt` — that push is what actually publishes the update, since Pages serves straight
   from the committed `docs/` folder.

You can also trigger it manually anytime from the **Actions** tab: **Add posts and rebuild site →
Run workflow**. Or run it locally:
```
export TMDB_API_KEY=...
export ANTHROPIC_API_KEY=...
pip install -r requirements.txt
python scripts/generate_post.py
python build_site.py
git add content/posts/ docs/ scripts/post_ideas.txt
git commit -m "Add new posts"
git push
```

You can still manually queue specific ideas any time by adding a line to `scripts/post_ideas.txt`
(these are always processed before the self-directed ones):
```
your-post-slug | Category | plain-English description of what the post should cover
```
`Category` should be `Actors`, `Movies`, or `Games`. For Games, say in the instructions whether you
want the emoji-clue format or the famous-quote format — otherwise Claude picks.

### Cost

Each new post costs roughly **$0.15-0.20** in Claude API usage (web search + a few small vision
calls for image matching). TMDB and GitHub Actions are free regardless of volume. At 3-6 posts/day
that's very roughly **$15-35/month** — check the Anthropic Console after a few days of real runs to
see actual usage rather than trusting this estimate blindly. To change the volume, adjust
`POSTS_PER_RUN` in `generate_post.py` and/or the `cron` schedule in `.github/workflows/update.yml`.

## 6. Point your Cloudflare domain (theclapperboard.com) at it

In Cloudflare DNS, add either:
- `CNAME` `www` → `<your-username>.github.io`
- Or for the apex domain, four `A` records to GitHub Pages' IPs:
  `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`
  (double-check GitHub's current Pages docs — this list rarely changes but does occasionally)

Then **Settings → Pages → Custom domain**: enter `theclapperboard.com`, save, and check
"Enforce HTTPS" once DNS propagates (minutes to about an hour).

Set the Cloudflare DNS records to "DNS only" (grey cloud) until the certificate issues, then
switch to proxied (orange cloud) afterward if you want Cloudflare's CDN in front of it.

## 7. Customize

- Site name, tagline, Flickle link/copy: the `SITE` dict at the top of `build_site.py`
- Add specific posts: append to `scripts/post_ideas.txt` (processed before self-directed ideas)
- Post page layout/sections: `render_post_page()`, `render_list_item()`, `render_emoji_item()`,
  `render_quote_item()` in `build_site.py`
- Colors/fonts: CSS variables at the top of `assets/style.css` (`--accent`, `--gold`, etc.)
- How many new posts per run / how often: `POSTS_PER_RUN` in `generate_post.py` and the `cron`
  line in `.github/workflows/update.yml`
- What topics the self-directed brainstorming favors: `TOPIC_BRAINSTORM_PROMPT` in
  `generate_post.py`

## SEO extras already built in

Every build generates `docs/sitemap.xml` and `docs/robots.txt`, and each post carries
`schema.org/Article` structured data plus Open Graph/Twitter card tags so links shared on social
render with a title, description, and image. None of this needs configuration — it's regenerated
automatically by `build_site.py`.

## How images get matched to facts

TMDB doesn't tag its backdrop stills with what scene they show, so there's no way to just query
"the shot of the bullet-time scene." To do better than grabbing an arbitrary still, `generate_post.py`
downloads several candidate backdrops for the movie and asks Claude — using real vision, not text
matching — to pick whichever one actually depicts the fact/scene in question, falling back to the
most-voted candidate if none clearly match. This adds one small extra API call per movie-still
lookup (a handful of cents at most), and only applies to movie stills — actor headshots don't need
it since there's only ever one relevant photo per person, not a "which scene" question.

## A note on images and content quality

Every image on the site — actor headshots, movie posters — comes from TMDB's CDN with
attribution in the footer, not paid stock/paparazzi photography. That means the site leans on
official promotional imagery rather than candid celebrity photos; if you want tabloid-style
candid photos later, that requires a licensed stock photo API (e.g. Getty Images), which isn't
wired up here.

Facts, quotes, and casting stories are AI-researched from web search results and cited under
"Sources" on each post — spot-check a sample after each batch, especially anything stated as fact.
The generator is told to skip an item entirely rather than fabricate a fact or an image it can't
verify/find, but it's not infallible.

## Content strategy notes (from our planning discussion)

The idea behind this site: build shareable, viral movie/celebrity content that funnels search and
social traffic into Flickle, rather than relying on Flickle's daily page alone to attract new
players. Every post ends with a Flickle CTA, and the "guess the movie" format directly mirrors
Flickle's own mechanic, which is intentional — it's the clearest bridge between "read a fun post"
and "go play the game." Suggested next moves:
- Link relevant social posts (trailer news, casting stories, anniversaries) back to a matching
  post here instead of only to Flickle.
- Keep adding "guess the movie" style posts (frame stills, quotes, emoji) — this format is the
  closest thing to free advertising for Flickle since it's literally the same game mechanic.
- Expand `post_ideas.txt` over time — the pipeline (TMDB + Claude + a plain Python build) scales
  to a much larger post volume without further engineering changes, it just costs more in API
  usage per batch.
