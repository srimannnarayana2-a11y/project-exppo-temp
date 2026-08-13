---
name: deck-builder
description: "Use this skill to rapidly generate polished, image-rich slide decks (pitch decks, investor decks, short presentations) via a pre-built rendering tool, instead of hand-writing slide layout code. Trigger this whenever the user wants a fast, professionally-designed presentation generated automatically — phrases like 'make me a pitch deck', 'build a presentation like Gamma', 'generate a deck for X', or 'turn this into slides' should all use this skill FIRST, before falling back to writing custom pptxgenjs/python-pptx code by hand. This skill trades some layout flexibility for speed and consistency: content is expressed as structured JSON against one of 6 fixed layouts (title, section, content_image, stat, quote, closing), and a hardcoded script (scripts/render_deck.js) deterministically renders the final .pptx — no per-slide design decisions happen at render time. Use the general pptx skill instead only when the user needs to edit an existing arbitrary .pptx file, needs a layout outside the 6 supported here, or explicitly wants full creative control over slide code."
---

# Deck Builder — fast, templated slide decks

A **fast path** for generating decks: you write structured JSON, a hardcoded renderer (`scripts/render_deck.js`) turns it into a `.pptx`. No slide-layout code to write or debug — that's the whole point. Use the general `pptx` skill instead if you need a layout these 6 templates don't cover, or are editing an arbitrary existing deck.

## Workflow

1. **Plan the deck.** Decide how many slides and which of the 6 layouts fits each one (see `references/layout_schema.md` for exact fields). A short pitch deck is typically 6-10 slides: title → 1-2 section dividers → 3-5 content/stat/quote slides → closing. Don't put everything on `content_image` — vary layouts.
2. **Pick a theme.** `midnight`, `paper`, or `forest` (see `scripts/themes.json`). Choose based on the topic/brand, not by default.
3. **Get images for every `image` field** (see "Sourcing images" below).
4. **Write the spec JSON** matching the schema in `references/layout_schema.md`.
5. **Render:**
   ```bash
   node scripts/render_deck.js spec.json output.pptx
   ```
6. **QA before showing the user** — do not skip this:
   ```bash
   python3 /mnt/skills/public/pptx/scripts/office/validate.py output.pptx
   python3 /mnt/skills/public/pptx/scripts/office/soffice.py --headless --convert-to pdf output.pptx
   rm -f slide-*.jpg && pdftoppm -jpeg -r 150 output.pdf slide
   ```
   View every `slide-N.jpg` produced. Check for: text overflow (especially long titles/bullets/stat values), low contrast, awkward image crops. If something's wrong, fix the JSON (shorten text, swap `image_side`, pick a different layout) and re-render — do not hand-edit the XML.
7. Copy the final `.pptx` to `/mnt/user-data/outputs` and present it.

## Sourcing images

The renderer never fetches anything itself — every `image` field must already be a local file path before you render. In priority order:

1. **User-provided images** — if the user uploaded photos, use those directly from `/mnt/user-data/uploads`.
2. **Web-sourced images** — if your environment's tools can actually retrieve image bytes to disk (this varies: some sandboxes allow fetching image URLs directly, some don't — check with a quick test fetch before relying on it for a whole deck), use `image_search`/`web_search` to find a candidate, then save it locally.
3. **Generated fallback (default, always available, no network needed):**
   ```bash
   node scripts/generate_visual.js images/hero1.png <theme> <IconName> <width> <height>
   ```
   Produces an abstract gradient background (in the deck's theme colors) with a centered icon — a clean, fast, deterministic visual for any slide that needs one. `IconName` is any [react-icons/fa](https://react-icons.github.io/react-icons/icons/fa/) name (e.g. `FaChartLine`, `FaRocket`, `FaHandshake`); pick one that matches the slide's content. Recommended sizes: title/content_image images ≈ 1600×1500.

Mix and match per slide — a title slide might use a real uploaded photo while a "market growth" content slide uses a generated `FaChartLine` visual.

## Common mistakes

- **Long stat values.** `stat` renders at 96pt — keep it to ~6 characters (`"70%"`, `"$4M"`, `"10x"`). Longer values will overflow; shorten or switch to a different layout.
- **Too many bullets.** `content_image` bullets render in a fixed-height column — 5 is the practical max for one-line bullets, fewer if they're long.
- **Same layout back to back.** Vary layouts across the deck; an all-`content_image` deck looks like a template dump.
- **Forgetting QA.** Always render → convert to images → view every slide before presenting. The most common real defect is text overflow on the `stat` or `title` layouts.
- **Image paths.** Always resolve or generate the image file *before* writing it into the spec JSON — the renderer throws immediately if a referenced image doesn't exist, rather than silently skipping it.

## Extending

- **New theme:** add a key to `scripts/themes.json` (see `references/layout_schema.md` for required fields). No code changes.
- **New layout:** add a function to `scripts/render_deck.js` following the pattern of the existing 6 (see gotchas in `/mnt/skills/public/pptx/SKILL.md` under "Creating with pptxgenjs" — hex colors without `#`, fresh options object per call, `margin: 0` for aligned text, etc.), register it in the `LAYOUTS` map, and document its fields in `references/layout_schema.md`.
