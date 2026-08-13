#!/usr/bin/env node
/**
 * jarvis/skills/dashboard-builder/scripts/build_dashboard.js
 *
 * Generates interactive self-contained HTML dashboards with Chart.js.
 * No external dependencies — everything is inlined.
 *
 * Usage:
 *   node build_dashboard.js <spec.json> <output.html>
 *
 * Spec format:
 * {
 *   "title": "Dashboard Title",
 *   "theme": "dark",         // dark | light | corporate
 *   "subtitle": "Q3 2024",
 *   "sections": [
 *     {
 *       "type": "kpi",
 *       "items": [
 *         { "label": "Revenue", "value": "$4.2M", "trend": "+18%", "positive": true },
 *         { "label": "Users", "value": "12,400", "trend": "+5%" }
 *       ]
 *     },
 *     {
 *       "type": "chart",
 *       "chart_type": "bar",   // bar | line | pie | doughnut
 *       "title": "Monthly Revenue",
 *       "labels": ["Jan","Feb","Mar"],
 *       "datasets": [
 *         { "label": "Revenue", "data": [1.2, 1.8, 2.1], "color": "#22d3ee" }
 *       ]
 *     },
 *     {
 *       "type": "table",
 *       "title": "Top Customers",
 *       "headers": ["Name", "Revenue", "Growth"],
 *       "rows": [["Acme Corp", "$1.2M", "+22%"]]
 *     },
 *     {
 *       "type": "text",
 *       "title": "Key Insights",
 *       "body": "Revenue grew 18% driven by enterprise deals..."
 *     }
 *   ]
 * }
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs'
import { resolve, dirname } from 'path'

const [,, specPath, outputPath] = process.argv
if (!specPath || !outputPath) {
  console.error('Usage: node build_dashboard.js <spec.json> <output.html>')
  process.exit(1)
}

const spec = JSON.parse(readFileSync(resolve(specPath), 'utf8'))
const outPath = resolve(outputPath)
mkdirSync(dirname(outPath), { recursive: true })

// ─── Themes ───────────────────────────────────────────────────────────────────
const THEMES = {
  dark: {
    '--bg': '#0a0f1e', '--surface': '#111827', '--border': '#1e2a3a',
    '--text': '#e2e8f0', '--text-muted': '#64748b', '--accent': '#22d3ee',
    '--accent2': '#818cf8', '--positive': '#34d399', '--negative': '#f87171',
    '--card-bg': '#111827',
  },
  light: {
    '--bg': '#f8fafc', '--surface': '#ffffff', '--border': '#e2e8f0',
    '--text': '#1e293b', '--text-muted': '#64748b', '--accent': '#3b82f6',
    '--accent2': '#8b5cf6', '--positive': '#10b981', '--negative': '#ef4444',
    '--card-bg': '#ffffff',
  },
  corporate: {
    '--bg': '#f1f5f9', '--surface': '#ffffff', '--border': '#e2e8f0',
    '--text': '#1e293b', '--text-muted': '#475569', '--accent': '#1e40af',
    '--accent2': '#7c3aed', '--positive': '#059669', '--negative': '#dc2626',
    '--card-bg': '#ffffff',
  },
}

const theme = THEMES[spec.theme ?? 'dark'] ?? THEMES.dark
const cssVars = Object.entries(theme).map(([k, v]) => `${k}: ${v};`).join('\n    ')

let chartIndex = 0

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

// ─── Section Renderers ────────────────────────────────────────────────────────

function renderKpi(s) {
  const items = (s.items ?? []).map(item => {
    const pos = item.positive !== false && item.trend && !item.trend.startsWith('-')
    const trendClass = item.trend ? (pos ? 'positive' : 'negative') : ''
    const trendIcon = item.trend ? (pos ? '▲' : '▼') : ''
    return `
      <div class="kpi-card">
        <div class="kpi-value">${esc(item.value)}</div>
        <div class="kpi-label">${esc(item.label)}</div>
        ${item.trend ? `<div class="kpi-trend ${trendClass}">${trendIcon} ${esc(item.trend)}</div>` : ''}
        ${item.description ? `<div class="kpi-desc">${esc(item.description)}</div>` : ''}
      </div>`
  }).join('')
  return `<div class="kpi-grid">${items}</div>`
}

function renderChart(s) {
  const id = `chart-${chartIndex++}`
  const chartType = s.chart_type ?? 'bar'
  const isPie = ['pie', 'doughnut', 'polarArea'].includes(chartType)

  const datasets = (s.datasets ?? []).map((ds, i) => {
    const defaultColors = ['#22d3ee', '#818cf8', '#34d399', '#fb923c', '#f87171', '#a78bfa']
    const color = ds.color ?? defaultColors[i % defaultColors.length]
    return JSON.stringify({
      label: ds.label ?? '',
      data: ds.data ?? [],
      backgroundColor: isPie
        ? (ds.data ?? []).map((_, di) => ['#22d3ee','#818cf8','#34d399','#fb923c','#f87171','#a78bfa','#fbbf24'][di % 7])
        : `${color}33`,
      borderColor: color,
      borderWidth: 2,
      tension: 0.4,
      fill: s.chart_type === 'line' ? false : undefined,
      pointRadius: s.chart_type === 'line' ? 4 : undefined,
    })
  }).join(',\n')

  return `
    <div class="chart-card">
      ${s.title ? `<h3 class="card-title">${esc(s.title)}</h3>` : ''}
      <div class="chart-container">
        <canvas id="${id}"></canvas>
      </div>
    </div>
    <script>
    (function() {
      const ctx = document.getElementById('${id}').getContext('2d')
      new Chart(ctx, {
        type: '${chartType}',
        data: {
          labels: ${JSON.stringify(s.labels ?? [])},
          datasets: [${datasets}]
        },
        options: {
          responsive: true,
          maintainAspectRatio: true,
          plugins: {
            legend: { labels: { color: getComputedStyle(document.documentElement).getPropertyValue('--text').trim() } },
            tooltip: { mode: 'index', intersect: false }
          },
          scales: ${isPie ? 'undefined' : `{
            x: { grid: { color: getComputedStyle(document.documentElement).getPropertyValue('--border').trim() }, ticks: { color: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() } },
            y: { grid: { color: getComputedStyle(document.documentElement).getPropertyValue('--border').trim() }, ticks: { color: getComputedStyle(document.documentElement).getPropertyValue('--text-muted').trim() } }
          }`}
        }
      })
    })()
    </script>`
}

function renderTable(s) {
  const headers = (s.headers ?? []).map(h => `<th>${esc(h)}</th>`).join('')
  const rows = (s.rows ?? []).map((row, ri) =>
    `<tr class="${ri % 2 === 0 ? 'even' : 'odd'}">${row.map(c => `<td>${esc(String(c))}</td>`).join('')}</tr>`
  ).join('')
  return `
    <div class="table-card">
      ${s.title ? `<h3 class="card-title">${esc(s.title)}</h3>` : ''}
      <div class="table-wrap">
        <table><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table>
      </div>
    </div>`
}

function renderText(s) {
  return `
    <div class="text-card">
      ${s.title ? `<h3 class="card-title">${esc(s.title)}</h3>` : ''}
      <p>${esc(s.body ?? '')}</p>
    </div>`
}

function renderSection(s) {
  switch (s.type) {
    case 'kpi': return renderKpi(s)
    case 'chart': return renderChart(s)
    case 'table': return renderTable(s)
    case 'text': return renderText(s)
    default: return `<!-- Unknown section type: ${esc(s.type)} -->`
  }
}

const sectionsHtml = (spec.sections ?? []).map(renderSection).join('\n')

// ─── HTML Output ──────────────────────────────────────────────────────────────
const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${esc(spec.title ?? 'Dashboard')}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

  :root { ${cssVars} }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; }

  .header {
    background: var(--surface); border-bottom: 1px solid var(--border);
    padding: 20px 32px; display: flex; justify-content: space-between; align-items: center;
    position: sticky; top: 0; z-index: 100; backdrop-filter: blur(12px);
  }

  .header-left h1 { font-size: 22px; font-weight: 700; color: var(--text); }
  .header-left .subtitle { font-size: 13px; color: var(--text-muted); margin-top: 2px; }
  .header-badge { background: var(--accent); color: #000; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }

  .content { padding: 28px 32px; max-width: 1400px; margin: 0 auto; }

  /* KPI Grid */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .kpi-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 20px 24px; transition: transform 0.2s, box-shadow 0.2s; }
  .kpi-card:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.15); }
  .kpi-value { font-size: 32px; font-weight: 700; color: var(--accent); line-height: 1.1; }
  .kpi-label { font-size: 13px; color: var(--text-muted); margin-top: 6px; font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }
  .kpi-trend { font-size: 13px; margin-top: 8px; font-weight: 600; }
  .kpi-trend.positive { color: var(--positive); }
  .kpi-trend.negative { color: var(--negative); }
  .kpi-desc { font-size: 11px; color: var(--text-muted); margin-top: 4px; opacity: 0.7; }

  /* Chart Card */
  .chart-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .chart-container { position: relative; height: 280px; }
  .card-title { font-size: 16px; font-weight: 600; color: var(--text); margin-bottom: 16px; border-left: 3px solid var(--accent); padding-left: 12px; }

  /* Table Card */
  .table-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; overflow: hidden; }
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  thead { background: var(--accent); }
  thead th { color: #000; padding: 10px 16px; text-align: left; font-weight: 600; letter-spacing: 0.03em; }
  tbody td { padding: 10px 16px; border-bottom: 1px solid var(--border); color: var(--text); }
  tr.odd { background: rgba(255,255,255,0.02); }
  tr:hover { background: rgba(34,211,238,0.05); }

  /* Text Card */
  .text-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; }
  .text-card p { color: var(--text); line-height: 1.7; font-size: 14px; }
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <h1>${esc(spec.title ?? 'Dashboard')}</h1>
    ${spec.subtitle ? `<div class="subtitle">${esc(spec.subtitle)}</div>` : ''}
  </div>
  <div class="header-badge">Live</div>
</div>

<div class="content">
  ${sectionsHtml}
</div>

</body>
</html>`

writeFileSync(outPath, html, 'utf8')
console.log(`[build_dashboard] ✓ ${spec.sections?.length ?? 0} sections → ${outPath}`)
console.log(`[build_dashboard] Theme: ${spec.theme ?? 'dark'}`)
