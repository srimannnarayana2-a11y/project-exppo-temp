#!/usr/bin/env node
/**
 * jarvis/skills/sheet-builder/scripts/build_sheet.js
 *
 * Generates formatted Excel workbooks (.xlsx) using ExcelJS.
 * Falls back to CSV if ExcelJS is not installed.
 *
 * Usage:
 *   node build_sheet.js <spec.json> <output.xlsx>
 *   node build_sheet.js <spec.json> <output.csv>
 *
 * Spec format:
 * {
 *   "title": "Workbook Title",
 *   "sheets": [
 *     {
 *       "name": "Revenue",
 *       "headers": ["Month", "Revenue", "Growth", "Notes"],
 *       "rows": [
 *         ["January", "$1.2M", "12%", "Record month"],
 *         ["February", "$1.8M", "50%", "New enterprise deals"]
 *       ],
 *       "column_widths": [15, 12, 10, 30],  // optional
 *       "freeze_top": true,                   // optional
 *       "style": "blue"                       // blue | green | dark | minimal
 *     }
 *   ],
 *   "formats": ["xlsx"]   // xlsx | csv
 * }
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs'
import { resolve, dirname } from 'path'

const [,, specPath, outputPath] = process.argv
if (!specPath || !outputPath) {
  console.error('Usage: node build_sheet.js <spec.json> <output.xlsx>')
  process.exit(1)
}

const spec = JSON.parse(readFileSync(resolve(specPath), 'utf8'))
const outPath = resolve(outputPath)
mkdirSync(dirname(outPath), { recursive: true })

const isCSV = outPath.toLowerCase().endsWith('.csv')

// ─── CSV Fallback ─────────────────────────────────────────────────────────────

function buildCSV(sheet) {
  const rows = []
  if (sheet.headers?.length) rows.push(sheet.headers.map(h => `"${String(h).replace(/"/g, '""')}"`).join(','))
  for (const row of (sheet.rows ?? [])) {
    rows.push(row.map(c => `"${String(c).replace(/"/g, '""')}"`).join(','))
  }
  return rows.join('\r\n')
}

if (isCSV) {
  const sheet = spec.sheets?.[0] ?? { headers: [], rows: [] }
  writeFileSync(outPath, buildCSV(sheet), 'utf8')
  console.log(`[build_sheet] ✓ CSV → ${outPath} (${(spec.sheets?.[0]?.rows ?? []).length} rows)`)
  process.exit(0)
}

// ─── Excel via ExcelJS ────────────────────────────────────────────────────────

let ExcelJS
try {
  const mod = await import('exceljs')
  ExcelJS = mod.default ?? mod
} catch {
  // Fallback to CSV even for xlsx request if ExcelJS missing
  console.warn('[build_sheet] ⚠ exceljs not found (run: bun add exceljs). Writing CSV fallback.')
  const csvPath = outPath.replace(/\.xlsx$/i, '.csv')
  const sheet = spec.sheets?.[0] ?? { headers: [], rows: [] }
  writeFileSync(csvPath, buildCSV(sheet), 'utf8')
  console.log(`[build_sheet] ✓ CSV fallback → ${csvPath}`)
  process.exit(0)
}

// ─── Theme Colors ─────────────────────────────────────────────────────────────
const HEADER_STYLES = {
  blue:    { fgColor: { argb: 'FF1e40af' }, fontColor: { argb: 'FFFFFFFF' } },
  green:   { fgColor: { argb: 'FF059669' }, fontColor: { argb: 'FFFFFFFF' } },
  dark:    { fgColor: { argb: 'FF111827' }, fontColor: { argb: 'FFe2e8f0' } },
  minimal: { fgColor: { argb: 'FFf1f5f9' }, fontColor: { argb: 'FF1e293b' } },
  purple:  { fgColor: { argb: 'FF6d28d9' }, fontColor: { argb: 'FFFFFFFF' } },
}

const workbook = new ExcelJS.Workbook()
workbook.creator = 'JARVIS-NVIDIA'
workbook.created = new Date()
workbook.title = spec.title ?? 'Workbook'

for (const sheetSpec of (spec.sheets ?? [])) {
  const ws = workbook.addWorksheet(sheetSpec.name ?? 'Sheet')
  const style = HEADER_STYLES[sheetSpec.style ?? 'blue'] ?? HEADER_STYLES.blue

  // Add headers
  if (sheetSpec.headers?.length) {
    const headerRow = ws.addRow(sheetSpec.headers)
    headerRow.eachCell(cell => {
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: style.fgColor }
      cell.font = { bold: true, color: style.fontColor, size: 11, name: 'Calibri' }
      cell.alignment = { vertical: 'middle', horizontal: 'center', wrapText: false }
      cell.border = {
        bottom: { style: 'medium', color: { argb: 'FF000000' } },
      }
    })
    headerRow.height = 22
  }

  // Add data rows
  for (let ri = 0; ri < (sheetSpec.rows ?? []).length; ri++) {
    const row = sheetSpec.rows[ri]
    const ws_row = ws.addRow(row)
    // Alternating row colors
    const isEven = ri % 2 === 0
    ws_row.eachCell(cell => {
      if (isEven) {
        cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFf8fafc' } }
      }
      cell.font = { name: 'Calibri', size: 10 }
      cell.alignment = { vertical: 'middle', wrapText: false }
      cell.border = {
        bottom: { style: 'thin', color: { argb: 'FFe2e8f0' } },
      }
    })
    ws_row.height = 18
  }

  // Set column widths
  const colWidths = sheetSpec.column_widths
  ws.columns.forEach((col, i) => {
    if (colWidths?.[i]) {
      col.width = colWidths[i]
    } else {
      // Auto-width: max of header + data
      const header = sheetSpec.headers?.[i] ?? ''
      const maxData = (sheetSpec.rows ?? []).reduce((max, row) => Math.max(max, String(row[i] ?? '').length), 0)
      col.width = Math.min(Math.max(header.length, maxData, 8) + 2, 40)
    }
  })

  // Freeze top row
  if (sheetSpec.freeze_top !== false && sheetSpec.headers?.length) {
    ws.views = [{ state: 'frozen', ySplit: 1, activeCell: 'A2' }]
  }

  // Auto filter on header row
  if (sheetSpec.headers?.length) {
    ws.autoFilter = {
      from: { row: 1, column: 1 },
      to: { row: 1, column: sheetSpec.headers.length },
    }
  }
}

await workbook.xlsx.writeFile(outPath)

const totalRows = (spec.sheets ?? []).reduce((sum, s) => sum + (s.rows?.length ?? 0), 0)
console.log(`[build_sheet] ✓ ${spec.sheets?.length ?? 0} sheet(s) | ${totalRows} rows → ${outPath}`)
