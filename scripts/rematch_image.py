#!/usr/bin/env python3
"""
Standalone tool to re-pick a single movie still using Claude's vision-based
matching (the same logic generate_post.py uses for new posts), so you can
fix an individual mismatched image in an already-published post without
regenerating the whole thing.

Usage:
    export TMDB_API_KEY=...
    export ANTHROPIC_API_KEY=...
    pip install -r requirements.txt
    python scripts/rematch_image.py "Movie Title" 1999 "the fact/scene this image needs to show"

Example:
    python scripts/rematch_image.py "The Matrix" 1999 \\
        "Bullet time used about 120 still cameras arranged in an arc, firing in sequence around Neo dodging bullets"

Prints the chosen image URL. Paste it into the relevant item's "images"
array in the post's JSON file (content/posts/<slug>.json), then re-run
python build_site.py to regenerate the HTML.
"""

import sys

from generate_post import tmdb_movie_image  # reuses the exact same vision-matching logic


def main():
    if len(sys.argv) < 4:
        sys.exit(
            'Usage: python scripts/rematch_image.py "Movie Title" YEAR "fact/scene description"'
        )

    title = sys.argv[1]
    year = int(sys.argv[2]) if sys.argv[2].strip() else None
    context_text = sys.argv[3]

    url = tmdb_movie_image(title, year, context_text)
    if not url:
        sys.exit(f"No image found for '{title}' ({year}).")

    print(url)


if __name__ == "__main__":
    main()
