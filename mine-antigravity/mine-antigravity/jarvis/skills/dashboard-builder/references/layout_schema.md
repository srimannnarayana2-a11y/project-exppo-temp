# Layout schema reference

Deck spec shape:
```json
{ "theme": "midnight | paper | forest", "slides": [ { "layout": "...", ...fields } ] }
```

All images are **local file paths** (relative to the working directory, or absolute). The renderer does not fetch anything itself — resolve/generate the image file first (see SKILL.md "Sourcing images"), then pass its path.

## title
Opening slide. `image` is optional — half-bleed on the right if present.
| field | required | notes |
|---|---|---|
| title | yes | main headline |
| kicker | no | small eyebrow label above title, auto-uppercased |
| subtitle | no | one line under the title |
| image | no | right-half bleed image |

## section
Divider / agenda slide between sections of the deck.
| field | required | notes |
|---|---|---|
| heading | yes | |
| index | no | e.g. "01" — shown large above the heading |
| subheading | no | short supporting line |

## content_image
Standard content slide: title + bullets on one side, image full-bleed on the other.
| field | required | notes |
|---|---|---|
| title | yes | |
| bullets | yes | array of strings, 2-5 recommended |
| image | yes | fills the opposite half of the slide |
| image_side | no | `"left"` or `"right"` (default `"right"`) |

## stat
Big-number callout for a single metric.
| field | required | notes |
|---|---|---|
| stat | yes | e.g. `"70%"`, `"$4M"`, `"10x"` — keep under ~6 characters, it renders at 96pt |
| label | yes | one line describing the stat |
| supporting | no | small supporting sentence |

## quote
Testimonial or pull-quote slide.
| field | required | notes |
|---|---|---|
| quote | yes | do not include quotation marks, they're added automatically |
| attribution | no | e.g. `"Jane Doe, CEO"` |

## closing
Final slide — thank you / CTA.
| field | required | notes |
|---|---|---|
| heading | yes | |
| subtext | no | |
| cta | no | renders as a filled button (e.g. "Get in touch") |

## Themes

Defined in `scripts/themes.json`: `midnight` (dark navy/teal), `paper` (white/red), `forest` (dark green/gold). Pick whichever palette best fits the deck's subject — don't default to `midnight` every time. Add a new theme by adding a key to that file with the same fields (bg, bgAlt, text, textMuted, accent, cardBg, fontTitle, fontBody) — no code changes needed.