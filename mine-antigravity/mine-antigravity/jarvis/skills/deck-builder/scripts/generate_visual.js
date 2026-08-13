#!/usr/bin/env node
/**
 * jarvis/skills/deck-builder/scripts/generate_visual.js
 *
 * Generates themed SVG gradient backgrounds with centered icons.
 * No network required. Works with pptxgenjs via data URI embedding.
 *
 * Usage:
 *   node generate_visual.js <output.svg> <theme> <IconLabel> [width] [height]
 *
 * Output: writes an SVG file suitable for embedding in PPTX slides.
 * The SVG uses theme-matched gradient + a centered text icon.
 *
 * Icon labels: chart | rocket | users | star | lightning | code | 
 *              trophy | target | globe | shield | brain | diamond
 */

import { writeFileSync, mkdirSync } from 'fs'
import { resolve, dirname } from 'path'

const THEMES = {
  midnight: { from: '0a0f1e', to: '1e2a4a', accent: '22d3ee', accent2: '818cf8' },
  paper:    { from: 'fafaf9', to: 'e7e5e4', accent: 'dc2626', accent2: 'ea580c' },
  forest:   { from: '0f2417', to: '1a4228', accent: '34d399', accent2: '6ee7b7' },
  ocean:    { from: '0c1445', to: '1e3a8a', accent: '38bdf8', accent2: '7dd3fc' },
  corporate:{ from: 'f8fafc', to: 'e2e8f0', accent: '1e40af', accent2: '3b82f6' },
  neon:     { from: '0d0d0d', to: '1a0a1f', accent: 'ff006e', accent2: 'fb5607' },
}

// Simple Unicode icon map for SVG text rendering
const ICONS = {
  chart:     '📊', rocket:    '🚀', users:     '👥',
  star:      '⭐', lightning: '⚡', code:      '💻',
  trophy:    '🏆', target:    '🎯', globe:     '🌐',
  shield:    '🛡', brain:     '🧠', diamond:   '💎',
  money:     '💰', growth:    '📈', idea:      '💡',
  handshake: '🤝', fire:      '🔥', lock:      '🔒',
  check:     '✅', arrow:     '➡', gear:      '⚙',
}

const [,, outPath, themeName = 'midnight', iconLabel = 'rocket', width = '1600', height = '1500'] = process.argv

if (!outPath) {
  console.error('Usage: node generate_visual.js <output.svg> <theme> <icon> [width] [height]')
  process.exit(1)
}

const T = THEMES[themeName.toLowerCase()] ?? THEMES.midnight
const icon = ICONS[iconLabel.toLowerCase()] ?? '✦'
const W = parseInt(width)
const H = parseInt(height)
const cx = W / 2
const cy = H / 2
const gradId = `g${Date.now()}`

const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
  <defs>
    <linearGradient id="${gradId}" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#${T.from}"/>
      <stop offset="100%" stop-color="#${T.to}"/>
    </linearGradient>
    <radialGradient id="${gradId}r" cx="50%" cy="50%" r="60%">
      <stop offset="0%" stop-color="#${T.accent}" stop-opacity="0.18"/>
      <stop offset="100%" stop-color="#${T.accent}" stop-opacity="0"/>
    </radialGradient>
  </defs>

  <!-- Background -->
  <rect width="${W}" height="${H}" fill="url(#${gradId})"/>

  <!-- Radial glow -->
  <ellipse cx="${cx}" cy="${cy}" rx="${W * 0.5}" ry="${H * 0.5}" fill="url(#${gradId}r)"/>

  <!-- Decorative rings -->
  <circle cx="${cx}" cy="${cy}" r="${Math.min(W, H) * 0.38}" stroke="#${T.accent}" stroke-width="1.5" fill="none" opacity="0.2"/>
  <circle cx="${cx}" cy="${cy}" r="${Math.min(W, H) * 0.28}" stroke="#${T.accent2}" stroke-width="1" fill="none" opacity="0.15"/>

  <!-- Corner accents -->
  <line x1="0" y1="0" x2="${W * 0.12}" y2="0" stroke="#${T.accent}" stroke-width="4"/>
  <line x1="0" y1="0" x2="0" y2="${H * 0.12}" stroke="#${T.accent}" stroke-width="4"/>
  <line x1="${W}" y1="${H}" x2="${W * 0.88}" y2="${H}" stroke="#${T.accent2}" stroke-width="4"/>
  <line x1="${W}" y1="${H}" x2="${W}" y2="${H * 0.88}" stroke="#${T.accent2}" stroke-width="4"/>

  <!-- Center icon -->
  <text x="${cx}" y="${cy + Math.min(W, H) * 0.085}" text-anchor="middle" font-size="${Math.min(W, H) * 0.22}" dominant-baseline="middle">${icon}</text>

  <!-- Bottom accent bar -->
  <rect x="0" y="${H - 5}" width="${W}" height="5" fill="#${T.accent}"/>
</svg>`

const out = resolve(outPath)
mkdirSync(dirname(out), { recursive: true })
writeFileSync(out, svg, 'utf8')
console.log(`[generate_visual] ✓ ${iconLabel} (${themeName}) → ${out}  [${W}×${H}]`)
