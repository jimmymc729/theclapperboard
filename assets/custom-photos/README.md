# Custom photos

Drop hand-picked photos of specific people here, and `generate_post.py`
will offer them as options the next time that person's name comes up as
an image lookup in a post — alongside whatever TMDB has on file, never
instead of it. Nothing here is forced: if a post needs a photo of someone
with an empty (or missing) folder, everything works exactly as it did
before this existed, purely from TMDB.

## How to add one

1. Find a real photo of the person (a folder for each of the people you
   mentioned already exists below, ready to go).
2. Drop the image file directly into that person's folder. Any of
   `.jpg`, `.jpeg`, `.png`, or `.webp` works.
3. That's it — no code changes needed. The very next time a post needs a
   photo of that person, this folder gets checked first.

## Adding someone new

Create a new folder here named exactly like the person's name, lowercase,
with hyphens instead of spaces (accents get stripped automatically) —
e.g. "Zoë Kravitz" → `zoe-kravitz/`. Drop photos in the same way.

## A couple of things worth knowing

- These photos get used across the whole site (any post, any item) once
  they're in a folder — same as every other image on the site, they're
  not tied to one specific post.
- The site already avoids repeating the exact same photo across posts —
  that logic applies to these the same as any TMDB photo.
- Whatever gets added here is still representing The Clapperboard, so it's
  worth keeping half an eye on tone/fit, the same judgment call you'd make
  for any other image on the site.
