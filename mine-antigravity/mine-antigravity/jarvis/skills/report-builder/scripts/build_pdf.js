#!/usr/bin/env node
/**
 * jarvis/skills/report-builder/scripts/build_pdf.js
 *
 * HTML-based PDF report generator.
 * Produces a beautiful self-contained HTML file with print-optimized CSS.
 * The HTML can be opened in any browser and printed to PDF (Ctrl+P → Save as PDF).
 * Optionally invokes puppeteer if installed for programmatic PDF export.
 *
 * Usage:
 *   node build_pdf.js <spec.json> <output.pdf>
 *   (produces output.html if puppeteer not available)
 *
 * Spec format: same as build_docx.js
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs'
import { resolve, dirname, extname } from 'path'

const [,, specPath, outputPath] = process.argv
if (!specPath || !outputPath) {
  console.error('Usage: node build_pdf.js <spec.json> <output.pdf>')
  process.exit(1)
}

const spec = JSON.parse(readFileSync(resolve(specPath), 'utf8'))
const outPath = resolve(outputPath)
mkdirSync(dirname(outPath), { recursive: true })

// ─── Color Themes ─────────────────────────────────────────────────────────────
const THEMES = {
  corporate: {
    accent: '#1e40af', headingColor: '#1e293b', bodyColor: '#374151',
    bg: '#ffffff', codeBg: '#f1f5f9', codeColor: '#1e40af',
    headerBg: '#1e40af', headerText: '#ffffff',
  },
  minimal: {
    accent: '#111827', headingColor: '#111827', bodyColor: '#374151',
    bg: '#ffffff', codeBg: '#f0fdf4', codeColor: '#059669',
    headerBg: '#111827', headerText: '#ffffff',
  },
  dark: {
    accent: '#22d3ee', headingColor: '#e2e8f0', bodyColor: '#94a3b8',
    bg: '#0f172a', codeBg: '#1e293b', codeColor: '#22d3ee',
    headerBg: '#0a0f1e', headerText: '#e2e8f0',
  },
}

const T = THEMES[spec.theme ?? 'corporate'] ?? THEMES.corporate

// ─── HTML Section Renderer ────────────────────────────────────────────────────
function renderSection(s) {
  const parts = []
  if (s.heading) parts.push(`<h2>${esc(s.heading)}</h2>`)
  if (s.subheading) parts.push(`<h3>${esc(s.subheading)}</h3>`)

  if (s.body) {
    s.body.split('\n\n').forEach(p => {
      if (p.trim()) parts.push(`<p>${esc(p.trim())}</p>`)
    })
  }

  if (s.bullets?.length) {
    const items = s.bullets.map(b => `<li>${esc(String(b))}</li>`).join('\n')
    parts.push(`<ul>${items}</ul>`)
  }

  if (s.code) {
    parts.push(`<pre><code>${esc(s.code)}</code></pre>`)
  }

  if (s.table?.headers && s.table?.rows) {
    const headers = s.table.headers.map(h => `<th>${esc(String(h))}</th>`).join('')
    const rows = s.table.rows.map((row, ri) => {
      const cells = row.map(c => `<td>${esc(String(c))}</td>`).join('')
      return `<tr class="${ri % 2 === 0 ? 'even' : 'odd'}">${cells}</tr>`
    }).join('\n')
    parts.push(`<table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table>`)
  }

  if (s.stats?.length) {
    const stats = s.stats.map(st => `
      <div class="stat-item">
        <div class="stat-value">${esc(String(st.value))}</div>
        <div class="stat-label">${esc(String(st.label))}</div>
        ${st.description ? `<div class="stat-desc">${esc(st.description)}</div>` : ''}
      </div>
    `).join('')
    parts.push(`<div class="stats-grid">${stats}</div>`)
  }

  return `<section class="report-section">${parts.join('\n')}</section>`
}

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// ─── Generate HTML ────────────────────────────────────────────────────────────
const sections = (spec.sections ?? []).map(renderSection).join('\n')
const meta = [spec.author, spec.date].filter(Boolean).join(' · ')

const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${esc(spec.title ?? 'Report')}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Inter', system-ui, sans-serif;
    font-size: 14px; line-height: 1.7;
    color: ${T.bodyColor}; background: ${T.bg};
    max-width: 860px; margin: 0 auto; padding: 0;
  }

  .cover {
    background: ${T.headerBg}; color: ${T.headerText};
    padding: 72px 60px 60px;
    border-bottom: 4px solid ${T.accent};
  }

  .cover h1 {
    font-size: 42px; font-weight: 700; line-height: 1.2;
    color: ${T.headerText}; margin-bottom: 16px;
  }

  .cover .subtitle {
    font-size: 20px; opacity: 0.8; margin-bottom: 24px; font-style: italic;
  }

  .cover .meta {
    font-size: 13px; opacity: 0.6; letter-spacing: 0.05em; text-transform: uppercase;
  }

  .content { padding: 48px 60px; }

  .report-section { margin-bottom: 48px; page-break-inside: avoid; }

  h2 {
    font-size: 24px; font-weight: 600; color: ${T.headingColor};
    border-left: 4px solid ${T.accent}; padding-left: 16px;
    margin-bottom: 20px; margin-top: 8px;
  }

  h3 {
    font-size: 18px; font-weight: 600; color: ${T.headingColor};
    margin-bottom: 12px;
  }

  p { margin-bottom: 14px; }

  ul { padding-left: 24px; margin-bottom: 16px; }
  li { margin-bottom: 6px; }
  li::marker { color: ${T.accent}; }

  pre {
    background: ${T.codeBg}; border-left: 3px solid ${T.accent};
    padding: 16px 20px; border-radius: 0 6px 6px 0;
    overflow-x: auto; margin-bottom: 16px;
  }

  code {
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 13px; color: ${T.codeColor};
  }

  table {
    width: 100%; border-collapse: collapse; margin-bottom: 20px;
    font-size: 13px; border-radius: 8px; overflow: hidden;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1);
  }

  thead { background: ${T.accent}; color: white; }
  thead th { padding: 12px 16px; text-align: left; font-weight: 600; letter-spacing: 0.03em; }

  tbody td { padding: 10px 16px; border-bottom: 1px solid #e2e8f0; }
  tr.odd { background: #f8fafc; }
  tr:hover { background: #eff6ff; }

  .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 20px; margin-bottom: 24px; }

  .stat-item {
    padding: 20px; border-radius: 10px;
    background: ${T.codeBg}; border: 1px solid #e2e8f0;
    text-align: center;
  }

  .stat-value { font-size: 36px; font-weight: 700; color: ${T.accent}; line-height: 1.1; }
  .stat-label { font-size: 13px; font-weight: 500; color: ${T.headingColor}; margin-top: 6px; text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-desc { font-size: 12px; color: ${T.bodyColor}; margin-top: 4px; opacity: 0.7; font-style: italic; }

  .footer {
    background: ${T.codeBg}; padding: 20px 60px;
    text-align: center; font-size: 12px; color: ${T.bodyColor};
    border-top: 1px solid #e2e8f0; opacity: 0.7;
  }

  @media print {
    body { max-width: 100%; }
    .cover { padding: 48px 40px 40px; }
    .content { padding: 32px 40px; }
    .report-section { page-break-inside: avoid; }
    pre { page-break-inside: avoid; }
    table { page-break-inside: avoid; }
  }
</style>
</head>
<body>

<div class="cover">
  <h1>${esc(spec.title ?? 'Report')}</h1>
  ${spec.subtitle ? `<div class="subtitle">${esc(spec.subtitle)}</div>` : ''}
  ${meta ? `<div class="meta">${esc(meta)}</div>` : ''}
</div>

<div class="content">
  ${sections}
</div>

${spec.footer ? `<div class="footer">${esc(spec.footer)}</div>` : ''}

</body>
</html>`

// ─── Try Puppeteer PDF Export ─────────────────────────────────────────────────
let wroteHtml = false
const htmlPath = outPath.replace(/\.pdf$/i, '.html')

writeFileSync(htmlPath, html, 'utf8')
wroteHtml = true

let pdfSuccess = false
if (outPath.toLowerCase().endsWith('.pdf')) {
  try {
    const { default: puppeteer } = await import('puppeteer')
    const browser = await puppeteer.launch({ headless: true })
    const page = await browser.newPage()
    await page.goto(`file://${htmlPath}`, { waitUntil: 'networkidle0' })
    await page.pdf({
      path: outPath,
      format: 'A4',
      printBackground: true,
      margin: { top: '0', bottom: '0', left: '0', right: '0' },
    })
    await browser.close()
    pdfSuccess = true
    console.log(`[build_pdf] ✓ PDF → ${outPath}`)
  } catch {
    // Puppeteer not available — HTML is the output
  }
}

if (!pdfSuccess) {
  console.log(`[build_pdf] ✓ HTML report → ${htmlPath}`)
  console.log(`[build_pdf] ℹ To convert to PDF: open ${htmlPath} in Chrome and print → Save as PDF`)
}
