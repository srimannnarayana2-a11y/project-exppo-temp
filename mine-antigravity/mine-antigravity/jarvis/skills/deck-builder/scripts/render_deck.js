#!/usr/bin/env node
/**
 * jarvis/skills/deck-builder/scripts/render_deck.js
 *
 * World-class PPTX renderer using pptxgenjs.
 * Supports 6 slide layouts + 6 premium themes.
 *
 * Usage:
 *   node render_deck.js <spec.json> <output.pptx>
 *
 * Layouts: title | section | content_image | stat | quote | closing
 * Themes:  midnight | paper | forest | ocean | corporate | neon
 */

import { readFileSync, existsSync, mkdirSync } from 'fs'
import { resolve, dirname } from 'path'

// ─── Bootstrap pptxgenjs ──────────────────────────────────────────────────────
let PptxGenJS
try {
  // Try multiple import paths (bun, node, monorepo)
  const mod = await import('pptxgenjs')
  PptxGenJS = mod.default ?? mod
} catch {
  console.error('[render_deck] ERROR: pptxgenjs not found. Run: bun add pptxgenjs')
  process.exit(1)
}

// ─── CLI Args ─────────────────────────────────────────────────────────────────
const [, , specPath, outputPath] = process.argv
if (!specPath || !outputPath) {
  console.error('Usage: node render_deck.js <spec.json> <output.pptx>')
  process.exit(1)
}

const spec = JSON.parse(readFileSync(resolve(specPath), 'utf8'))
const outPath = resolve(outputPath)
mkdirSync(dirname(outPath), { recursive: true })

// ─── Themes ───────────────────────────────────────────────────────────────────
const THEMES = {
  midnight: {
    bg: '0a0f1e', text: 'e2e8f0', accent: '22d3ee', accent2: '818cf8',
    headingFont: 'Calibri', bodyFont: 'Calibri',
    subtitleColor: '94a3b8', statColor: '22d3ee', quoteColor: '818cf8',
  },
  paper: {
    bg: 'fafaf9', text: '1c1917', accent: 'dc2626', accent2: 'ea580c',
    headingFont: 'Georgia', bodyFont: 'Arial',
    subtitleColor: '78716c', statColor: 'dc2626', quoteColor: 'ea580c',
  },
  forest: {
    bg: '0f2417', text: 'ecfdf5', accent: '34d399', accent2: '6ee7b7',
    headingFont: 'Calibri', bodyFont: 'Calibri',
    subtitleColor: 'a7f3d0', statColor: '34d399', quoteColor: '6ee7b7',
  },
  ocean: {
    bg: '0c1445', text: 'e0f2fe', accent: '38bdf8', accent2: '7dd3fc',
    headingFont: 'Calibri', bodyFont: 'Calibri',
    subtitleColor: 'bae6fd', statColor: '38bdf8', quoteColor: '7dd3fc',
  },
  corporate: {
    bg: 'ffffff', text: '1e293b', accent: '1e40af', accent2: '3b82f6',
    headingFont: 'Arial', bodyFont: 'Arial',
    subtitleColor: '475569', statColor: '1e40af', quoteColor: '3b82f6',
  },
  neon: {
    bg: '0d0d0d', text: 'f0f0f0', accent: 'ff006e', accent2: 'fb5607',
    headingFont: 'Calibri', bodyFont: 'Calibri',
    subtitleColor: 'a0a0a0', statColor: 'ff006e', quoteColor: 'fb5607',
  },
}

const themeName = spec.theme ?? 'midnight'
const T = THEMES[themeName] ?? THEMES.midnight

// ─── Slide Dimensions (Widescreen 16:9) ──────────────────────────────────────
const W = 13.33
const H = 7.5

// ─── pptxgenjs Instance ───────────────────────────────────────────────────────
const pptx = new PptxGenJS()
pptx.layout = 'LAYOUT_WIDE'
pptx.title = spec.title ?? 'Presentation'
pptx.subject = spec.subject ?? ''
pptx.author = spec.author ?? 'JARVIS-NVIDIA'

// ─── Helper: Background ───────────────────────────────────────────────────────
function addBg(slide) {
  slide.background = { color: T.bg }
}

// ─── Helper: Image (optional) ─────────────────────────────────────────────────
function addImage(slide, imgPath, x, y, w, h) {
  if (!imgPath) return
  const resolved = resolve(imgPath)
  if (!existsSync(resolved)) return
  try {
    slide.addImage({ path: resolved, x, y, w, h })
  } catch { /* skip invalid images */ }
}

// ─── Layout: title ───────────────────────────────────────────────────────────
function renderTitle(slide, s) {
  addBg(slide)
  // Accent bar at bottom
  slide.addShape(pptx.ShapeType.rect, { x: 0, y: H - 0.08, w: W, h: 0.08, fill: { color: T.accent } })
  // Background image (optional, left half)
  if (s.image) addImage(slide, s.image, 0, 0, W * 0.5, H)
  // Title
  slide.addText(s.title ?? '', {
    x: s.image ? W * 0.5 + 0.3 : 1.2, y: 2.2, w: s.image ? W * 0.5 - 0.6 : W - 2.4, h: 1.5,
    fontSize: s.image ? 36 : 44, bold: true, color: T.text, fontFace: T.headingFont,
    align: 'left', wrap: true,
  })
  // Subtitle
  if (s.subtitle) {
    slide.addText(s.subtitle, {
      x: s.image ? W * 0.5 + 0.3 : 1.2, y: 3.9, w: s.image ? W * 0.5 - 0.6 : W - 2.4, h: 0.9,
      fontSize: 22, color: T.subtitleColor, fontFace: T.bodyFont, align: 'left', wrap: true,
    })
  }
  // Accent dot
  slide.addShape(pptx.ShapeType.ellipse, { x: s.image ? W * 0.5 + 0.3 : 1.2, y: 3.7, w: 0.12, h: 0.12, fill: { color: T.accent } })
}

// ─── Layout: section ─────────────────────────────────────────────────────────
function renderSection(slide, s) {
  addBg(slide)
  // Bold accent left bar
  slide.addShape(pptx.ShapeType.rect, { x: 0.8, y: H / 2 - 0.65, w: 0.1, h: 1.3, fill: { color: T.accent } })
  slide.addText(s.title ?? '', {
    x: 1.2, y: H / 2 - 0.7, w: W - 1.5, h: 1.4,
    fontSize: 42, bold: true, color: T.text, fontFace: T.headingFont, align: 'left', wrap: true,
  })
  if (s.subtitle) {
    slide.addText(s.subtitle, {
      x: 1.2, y: H / 2 + 0.8, w: W - 1.5, h: 0.7,
      fontSize: 20, color: T.subtitleColor, fontFace: T.bodyFont, align: 'left', wrap: true,
    })
  }
}

// ─── Layout: content_image ────────────────────────────────────────────────────
function renderContentImage(slide, s) {
  addBg(slide)
  const hasImage = !!s.image && existsSync(resolve(s.image))
  const imgSide = s.image_side ?? 'right'
  const textX = (!hasImage || imgSide === 'right') ? 0.5 : W * 0.45 + 0.3
  const textW = hasImage ? W * 0.48 : W - 1.0

  // Title
  slide.addText(s.title ?? '', {
    x: textX, y: 0.5, w: textW, h: 0.85,
    fontSize: 28, bold: true, color: T.text, fontFace: T.headingFont, align: 'left', wrap: true,
  })
  // Accent underline
  slide.addShape(pptx.ShapeType.rect, { x: textX, y: 1.35, w: Math.min(textW * 0.4, 2.5), h: 0.04, fill: { color: T.accent } })

  // Bullets
  const bullets = (s.bullets ?? []).slice(0, 6)
  bullets.forEach((b, i) => {
    slide.addText(b, {
      x: textX, y: 1.6 + i * 0.85, w: textW, h: 0.8,
      fontSize: 18, color: T.text, fontFace: T.bodyFont, align: 'left', wrap: true,
      bullet: { type: 'number', color: T.accent, startAt: i + 1 },
    })
  })

  // Image
  if (hasImage) {
    const imgX = imgSide === 'right' ? W * 0.52 : 0.3
    addImage(slide, s.image, imgX, 0.4, W * 0.45, H - 0.8)
  }

  // Supporting text
  if (s.supporting) {
    slide.addText(s.supporting, {
      x: textX, y: H - 0.8, w: textW, h: 0.5,
      fontSize: 13, color: T.subtitleColor, fontFace: T.bodyFont, align: 'left', italic: true,
    })
  }
}

// ─── Layout: stat ─────────────────────────────────────────────────────────────
function renderStat(slide, s) {
  addBg(slide)
  // Subtle radial accent circle
  slide.addShape(pptx.ShapeType.ellipse, {
    x: W / 2 - 2.5, y: H / 2 - 2.2, w: 5, h: 4.4,
    line: { color: T.accent, width: 2 }, fill: { type: 'none' },
  })
  // Stat value
  const statText = String(s.stat ?? '').slice(0, 8)
  slide.addText(statText, {
    x: 1, y: H / 2 - 1.6, w: W - 2, h: 2.2,
    fontSize: 96, bold: true, color: T.statColor, fontFace: T.headingFont, align: 'center',
  })
  // Label
  slide.addText(s.label ?? '', {
    x: 2, y: H / 2 + 0.7, w: W - 4, h: 0.7,
    fontSize: 26, bold: true, color: T.text, fontFace: T.headingFont, align: 'center', wrap: true,
  })
  // Supporting
  if (s.supporting) {
    slide.addText(s.supporting, {
      x: 2, y: H / 2 + 1.5, w: W - 4, h: 0.6,
      fontSize: 18, color: T.subtitleColor, fontFace: T.bodyFont, align: 'center', wrap: true,
    })
  }
}

// ─── Layout: quote ────────────────────────────────────────────────────────────
function renderQuote(slide, s) {
  addBg(slide)
  // Large decorative quote mark
  slide.addText('\u201C', {
    x: 0.8, y: 0.3, w: 2.5, h: 2.5,
    fontSize: 140, bold: true, color: T.quoteColor, fontFace: T.headingFont, align: 'left',
  })
  // Quote text
  slide.addText(s.quote ?? '', {
    x: 1.5, y: 1.2, w: W - 3, h: 3.6,
    fontSize: 26, italic: true, color: T.text, fontFace: T.headingFont, align: 'center', wrap: true,
  })
  // Attribution
  if (s.attribution) {
    slide.addText(`— ${s.attribution}`, {
      x: 2, y: 5.0, w: W - 4, h: 0.6,
      fontSize: 18, bold: true, color: T.accent, fontFace: T.bodyFont, align: 'center',
    })
  }
  // Bottom accent line
  slide.addShape(pptx.ShapeType.rect, { x: W / 2 - 2, y: H - 0.5, w: 4, h: 0.05, fill: { color: T.quoteColor } })
}

// ─── Layout: closing ─────────────────────────────────────────────────────────
function renderClosing(slide, s) {
  addBg(slide)
  // Gradient feel via two overlapping shapes
  slide.addShape(pptx.ShapeType.rect, { x: 0, y: 0, w: W, h: H * 0.08, fill: { color: T.accent } })
  slide.addShape(pptx.ShapeType.rect, { x: 0, y: H - H * 0.08, w: W, h: H * 0.08, fill: { color: T.accent2 } })
  // Title
  slide.addText(s.title ?? 'Thank You', {
    x: 1, y: 1.4, w: W - 2, h: 1.5,
    fontSize: 52, bold: true, color: T.text, fontFace: T.headingFont, align: 'center', wrap: true,
  })
  // CTA
  if (s.cta) {
    slide.addText(s.cta, {
      x: 2, y: 3.2, w: W - 4, h: 0.7,
      fontSize: 22, color: T.accent, fontFace: T.bodyFont, align: 'center', underline: { color: T.accent },
    })
  }
  // Sub-cta
  if (s.sub_cta) {
    slide.addText(s.sub_cta, {
      x: 2, y: 4.1, w: W - 4, h: 0.6,
      fontSize: 18, color: T.subtitleColor, fontFace: T.bodyFont, align: 'center',
    })
  }
}

// ─── Layout: bullets (simple text slide) ─────────────────────────────────────
function renderBullets(slide, s) {
  addBg(slide)
  slide.addText(s.title ?? '', {
    x: 0.8, y: 0.5, w: W - 1.6, h: 0.9,
    fontSize: 30, bold: true, color: T.text, fontFace: T.headingFont, align: 'left', wrap: true,
  })
  slide.addShape(pptx.ShapeType.rect, { x: 0.8, y: 1.45, w: 3, h: 0.05, fill: { color: T.accent } })
  const bullets = (s.bullets ?? s.body ?? []).slice(0, 8)
  bullets.forEach((b, i) => {
    slide.addText(typeof b === 'string' ? b : String(b), {
      x: 0.8, y: 1.65 + i * 0.72, w: W - 1.6, h: 0.68,
      fontSize: 19, color: T.text, fontFace: T.bodyFont, align: 'left', wrap: true,
      bullet: { color: T.accent },
    })
  })
}

// ─── Layout Dispatch ──────────────────────────────────────────────────────────
const LAYOUTS = {
  title: renderTitle,
  section: renderSection,
  content_image: renderContentImage,
  stat: renderStat,
  quote: renderQuote,
  closing: renderClosing,
  bullets: renderBullets,
}

// ─── Render All Slides ────────────────────────────────────────────────────────
for (const slideSpec of (spec.slides ?? [])) {
  const layout = (slideSpec.layout ?? 'bullets').toLowerCase()
  const renderer = LAYOUTS[layout] ?? renderBullets
  const slide = pptx.addSlide()
  renderer(slide, slideSpec)
}

// Ensure at least one slide
if (!spec.slides || spec.slides.length === 0) {
  const slide = pptx.addSlide()
  renderTitle(slide, { title: 'Untitled Presentation', subtitle: 'Generated by JARVIS-NVIDIA' })
}

// ─── Write Output ─────────────────────────────────────────────────────────────
await pptx.writeFile({ fileName: outPath })
console.log(`[render_deck] ✓ Wrote ${spec.slides?.length ?? 1} slides → ${outPath}`)
console.log(`[render_deck] Theme: ${themeName} | Size: 13.33×7.5 in (16:9)`)
