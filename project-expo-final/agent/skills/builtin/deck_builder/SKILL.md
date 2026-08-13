---
name: Deck Builder
description: Build a professional presentation (PPTX) from a topic or research query
triggers: [presentation, pptx, slides, deck, slideshow, powerpoint]
tools_required: [output_renderer, brave_search]
---

## Instructions

You are building a professional presentation. Follow this workflow:

### 1. Research Phase
- Search for key information about the topic
- Gather 5-8 key points that tell a compelling story
- Find supporting data, statistics, or quotes

### 2. Structure Phase
Create this slide structure:
- **Slide 1: Title** — compelling title + subtitle
- **Slide 2: Overview/Agenda** — what will be covered
- **Slides 3-8: Content** — one key point per slide with:
  - Clear heading
  - 3-4 bullet points (concise, not paragraphs)
  - Supporting data or example where relevant
- **Slide 9: Key Takeaways** — 3 main things to remember
- **Slide 10: Q&A / Thank You** — closing slide

### 3. Content Rules
- Each bullet point: MAX 15 words. Presentations are visual, not essays.
- Use concrete numbers over vague claims ("42% increase" not "significant increase")
- One idea per slide — if you need more, split into two slides
- Include a "hook" in the title slide (provocative question or surprising stat)

### 4. Output
Generate the complete presentation content in Markdown format with ## for each slide heading. The renderer will convert this to PPTX.
