# Deck Builder — Layout Schema Reference

**Full spec shape:**
```json
{
  "theme": "midnight | paper | forest | ocean | corporate | neon",
  "title": "optional deck title (for metadata)",
  "author": "optional",
  "slides": [
    { "layout": "title | section | content_image | stat | quote | closing | bullets", ...fields }
  ]
}
```

> All `image` fields must be **local file paths** (relative to cwd, or absolute).  
> The renderer does NOT fetch URLs. Use `generate_visual.js` to create images locally.

---

## Layout: `title`
Opening/cover slide.

| Field | Required | Notes |
|---|---|---|
| `title` | ✅ | Main headline — keep under 8 words |
| `subtitle` | ❌ | One supporting line below title |
| `image` | ❌ | Optional right-half bleed image path |

```json
{ "layout": "title", "title": "Nova AI", "subtitle": "Automating the future of work", "image": null }
```

---

## Layout: `section`
Divider slide between sections of the deck.

| Field | Required | Notes |
|---|---|---|
| `title` | ✅ | Section heading |
| `subtitle` | ❌ | Short supporting line |

```json
{ "layout": "section", "title": "The Problem", "subtitle": "What we're solving" }
```

---

## Layout: `content_image`
Title + bullet points on one side, image on the other. Most versatile layout.

| Field | Required | Notes |
|---|---|---|
| `title` | ✅ | Slide title |
| `bullets` | ✅ | Array of 2–6 strings |
| `image` | ❌ | Image path; if null, text spans full width |
| `image_side` | ❌ | `"left"` or `"right"` (default: `"right"`) |
| `supporting` | ❌ | Small italic footnote at bottom |

```json
{
  "layout": "content_image",
  "title": "Why We Win",
  "bullets": ["3× faster onboarding", "40% cost reduction", "99.9% uptime SLA"],
  "image": null
}
```

---

## Layout: `stat`
Big-number callout. Best for a single striking metric.

| Field | Required | Notes |
|---|---|---|
| `stat` | ✅ | The number/value — keep under 8 chars (renders at 96pt) |
| `label` | ✅ | One line describing the stat |
| `supporting` | ❌ | Small supporting context sentence |

```json
{ "layout": "stat", "stat": "$4M", "label": "ARR in Year 1", "supporting": "Achieved without external funding" }
{ "layout": "stat", "stat": "40%", "label": "Cost Reduction", "supporting": "vs. industry average" }
{ "layout": "stat", "stat": "10×", "label": "Faster Deployment" }
```

⚠ **Keep `stat` under 8 characters** — it renders at 96pt and will overflow if too long.

---

## Layout: `quote`
Testimonial or pull-quote slide.

| Field | Required | Notes |
|---|---|---|
| `quote` | ✅ | The quote text — DO NOT include `"` marks, they're added automatically |
| `attribution` | ❌ | `"Name, Title, Company"` |

```json
{ "layout": "quote", "quote": "JARVIS cut our deployment time from days to hours.", "attribution": "Sarah Chen, CTO at Acme Corp" }
```

---

## Layout: `closing`
Final slide — thank you / CTA.

| Field | Required | Notes |
|---|---|---|
| `title` | ✅ | Closing headline (e.g. `"Let's Build Together"`) |
| `cta` | ❌ | Call-to-action text (e.g. email, URL, tagline) |
| `sub_cta` | ❌ | Secondary line below CTA |

```json
{ "layout": "closing", "title": "Thank You", "cta": "hello@novaai.com", "sub_cta": "novaai.com · @nova_ai" }
```

---

## Layout: `bullets`
Simple text slide with title and bullets. No image.

| Field | Required | Notes |
|---|---|---|
| `title` | ✅ | Slide title |
| `bullets` | ✅ | Array of 2–8 strings |

```json
{ "layout": "bullets", "title": "Our Roadmap", "bullets": ["Q1: Beta launch", "Q2: Enterprise tier", "Q3: EMEA expansion"] }
```

---

## Themes

| Theme | Feel | Best For |
|---|---|---|
| `midnight` | Dark navy + cyan | Tech, SaaS, AI, startups |
| `paper` | White + red | Editorial, consulting, research |
| `forest` | Dark green + emerald | Sustainability, health, nature |
| `ocean` | Deep blue + sky | Finance, trust, enterprise |
| `corporate` | White + navy | Formal, conservative, enterprise |
| `neon` | Black + pink/orange | Bold, startup, attention-grabbing |

---

## Full Pitch Deck Example (7 slides)

```json
{
  "theme": "midnight",
  "slides": [
    { "layout": "title",         "title": "Nova AI", "subtitle": "AI-powered deployment automation", "image": null },
    { "layout": "section",       "title": "The Problem" },
    { "layout": "content_image", "title": "Deployments Are Broken", "bullets": ["Teams spend 40% of time on manual deploys", "Average incident takes 4 hours to resolve", "No visibility across environments"], "image": null },
    { "layout": "stat",          "stat": "40%", "label": "Engineering time wasted", "supporting": "on manual deployment tasks" },
    { "layout": "section",       "title": "Our Solution" },
    { "layout": "content_image", "title": "Nova AI Automates Everything", "bullets": ["One-click deploys across any cloud", "AI-powered rollback on anomaly detection", "Real-time observability dashboard"], "image": null },
    { "layout": "stat",          "stat": "10×", "label": "Faster deployments", "supporting": "avg across 200 enterprise customers" },
    { "layout": "quote",         "quote": "Nova AI cut our deploy time from 4 hours to 8 minutes.", "attribution": "David Kim, VP Engineering at Stripe" },
    { "layout": "closing",       "title": "Let's Build Together", "cta": "hello@novaai.com", "sub_cta": "novaai.com" }
  ]
}
```

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| `stat` is too long (`"$4,000,000"`) | Shorten: `"$4M"` |
| `bullets` array has 7+ items | Max 6 per slide; split into multiple slides |
| `image` path doesn't exist | Set to `null` OR run `generate_visual.js` first |
| Same layout used 5+ times in a row | Vary layouts for visual interest |
| JSON parse error | Validate with `JSON.parse()` before passing to BuildDeck |
