#!/usr/bin/env node
/**
 * jarvis/skills/report-builder/scripts/build_docx.js
 *
 * World-class Word document generator using the `docx` npm package.
 * Produces .docx files comparable to premium AI report tools.
 *
 * Usage:
 *   node build_docx.js <spec.json> <output.docx>
 *
 * Spec format:
 * {
 *   "title": "Report Title",
 *   "subtitle": "Optional subtitle",
 *   "author": "Author Name",
 *   "date": "2024-01-15",
 *   "theme": "corporate",  // corporate | dark | minimal
 *   "sections": [
 *     { "heading": "Section Name", "body": "Paragraph text..." },
 *     { "heading": "With Table", "table": { "headers": ["A","B"], "rows": [["x","y"]] } },
 *     { "heading": "Code Block", "code": "const x = 1;" },
 *     { "heading": "Bullets", "bullets": ["Item 1", "Item 2"] },
 *     { "heading": "Stats", "stats": [{ "label": "Revenue", "value": "$4.2M" }] }
 *   ],
 *   "footer": "Confidential — 2024"
 * }
 */

import { readFileSync, writeFileSync, mkdirSync } from 'fs'
import { resolve, dirname } from 'path'

let docx
try {
  docx = await import('docx')
} catch {
  console.error('[build_docx] ERROR: docx not found. Run: bun add docx')
  process.exit(1)
}

const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  TableRow, TableCell, Table, WidthType, BorderStyle, ShadingType,
  PageOrientation, convertInchesToTwip,
} = docx

// ─── CLI Args ─────────────────────────────────────────────────────────────────
const [,, specPath, outputPath] = process.argv
if (!specPath || !outputPath) {
  console.error('Usage: node build_docx.js <spec.json> <output.docx>')
  process.exit(1)
}

const spec = JSON.parse(readFileSync(resolve(specPath), 'utf8'))
const outPath = resolve(outputPath)
mkdirSync(dirname(outPath), { recursive: true })

// ─── Color Themes ─────────────────────────────────────────────────────────────
const THEMES = {
  corporate: { accent: '1e40af', heading: '1e293b', body: '374151', code: '1e40af', codeBg: 'f1f5f9' },
  minimal:   { accent: '000000', heading: '111827', body: '374151', code: '059669', codeBg: 'f0fdf4' },
  dark:      { accent: '22d3ee', heading: '0f172a', body: '1e293b', code: '22d3ee', codeBg: '0f172a' },
}

const T = THEMES[spec.theme ?? 'corporate'] ?? THEMES.corporate

// ─── Builder Helpers ──────────────────────────────────────────────────────────

function makeHeading(text, level = 1) {
  const headingLevels = [
    HeadingLevel.TITLE,
    HeadingLevel.HEADING_1,
    HeadingLevel.HEADING_2,
    HeadingLevel.HEADING_3,
  ]
  return new Paragraph({
    text,
    heading: headingLevels[level] ?? HeadingLevel.HEADING_2,
    spacing: { before: level === 1 ? 360 : 240, after: 120 },
    thematicBreak: level === 1,
  })
}

function makeBody(text) {
  return new Paragraph({
    children: [new TextRun({ text, size: 22, color: T.body })],
    spacing: { after: 200 },
  })
}

function makeBullet(text, level = 0) {
  return new Paragraph({
    children: [new TextRun({ text, size: 22, color: T.body })],
    bullet: { level },
    spacing: { after: 100 },
  })
}

function makeCode(code) {
  return new Paragraph({
    children: [new TextRun({
      text: code, font: 'Courier New', size: 18, color: T.code,
    })],
    shading: { type: ShadingType.SOLID, color: T.codeBg, fill: T.codeBg },
    spacing: { before: 120, after: 120 },
    indent: { left: 360 },
  })
}

function makeTable(headers, rows) {
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map(h =>
      new TableCell({
        children: [new Paragraph({
          children: [new TextRun({ text: h, bold: true, size: 20, color: 'ffffff' })],
        })],
        shading: { type: ShadingType.SOLID, color: T.accent, fill: T.accent },
        margins: { top: 80, bottom: 80, left: 120, right: 120 },
        width: { size: Math.floor(9000 / headers.length), type: WidthType.DXA },
      })
    ),
  })

  const dataRows = rows.map((row, ri) =>
    new TableRow({
      children: row.map(cell =>
        new TableCell({
          children: [new Paragraph({
            children: [new TextRun({ text: String(cell), size: 20, color: T.body })],
          })],
          shading: ri % 2 === 0
            ? undefined
            : { type: ShadingType.SOLID, color: 'f8fafc', fill: 'f8fafc' },
          margins: { top: 60, bottom: 60, left: 120, right: 120 },
          borders: { bottom: { style: BorderStyle.SINGLE, size: 1, color: 'e2e8f0' } },
        })
      ),
    })
  )

  return new Table({
    rows: [headerRow, ...dataRows],
    width: { size: 9000, type: WidthType.DXA },
  })
}

function makeStats(stats) {
  const children = []
  for (const { label, value, description } of stats) {
    children.push(new Paragraph({
      children: [
        new TextRun({ text: value, bold: true, size: 48, color: T.accent }),
        new TextRun({ text: `  ${label}`, size: 24, color: T.heading }),
      ],
      spacing: { before: 120, after: 60 },
    }))
    if (description) {
      children.push(new Paragraph({
        children: [new TextRun({ text: description, size: 18, color: T.body, italics: true })],
        spacing: { after: 160 },
        indent: { left: 180 },
      }))
    }
  }
  return children
}

// ─── Assemble Document ────────────────────────────────────────────────────────
const children = []

// Cover: title
children.push(
  new Paragraph({
    children: [new TextRun({ text: spec.title ?? 'Report', bold: true, size: 56, color: T.accent })],
    heading: HeadingLevel.TITLE,
    alignment: AlignmentType.CENTER,
    spacing: { before: 720, after: 240 },
  })
)

if (spec.subtitle) {
  children.push(new Paragraph({
    children: [new TextRun({ text: spec.subtitle, size: 28, color: T.body, italics: true })],
    alignment: AlignmentType.CENTER,
    spacing: { after: 120 },
  }))
}

const meta = [spec.author, spec.date].filter(Boolean).join('  ·  ')
if (meta) {
  children.push(new Paragraph({
    children: [new TextRun({ text: meta, size: 20, color: T.body })],
    alignment: AlignmentType.CENTER,
    spacing: { after: 720 },
  }))
}

// Sections
for (const section of (spec.sections ?? [])) {
  if (section.heading) children.push(makeHeading(section.heading, 2))
  if (section.subheading) children.push(makeHeading(section.subheading, 3))

  if (section.body) {
    const paras = section.body.split('\n\n')
    paras.forEach(p => children.push(makeBody(p.trim())))
  }

  if (section.bullets) {
    section.bullets.forEach(b => children.push(makeBullet(typeof b === 'string' ? b : String(b))))
  }

  if (section.code) {
    const lines = section.code.split('\n')
    lines.forEach(l => children.push(makeCode(l)))
  }

  if (section.table?.headers && section.table?.rows) {
    children.push(makeTable(section.table.headers, section.table.rows))
    children.push(new Paragraph({ text: '', spacing: { after: 200 } }))
  }

  if (section.stats) {
    children.push(...makeStats(section.stats))
  }
}

// Footer paragraph
if (spec.footer) {
  children.push(new Paragraph({
    children: [new TextRun({ text: spec.footer, size: 18, color: T.body, italics: true })],
    alignment: AlignmentType.CENTER,
    spacing: { before: 720 },
  }))
}

const doc = new Document({
  sections: [{
    properties: {
      page: { margin: { top: convertInchesToTwip(1), bottom: convertInchesToTwip(1), left: convertInchesToTwip(1.2), right: convertInchesToTwip(1.2) } },
    },
    children,
  }],
  styles: {
    default: {
      document: { run: { font: 'Arial', size: 22 } },
      heading1: { run: { font: 'Arial', size: 36, bold: true, color: T.heading } },
      heading2: { run: { font: 'Arial', size: 28, bold: true, color: T.heading } },
      heading3: { run: { font: 'Arial', size: 22, bold: true, color: T.heading } },
    },
  },
})

const buffer = await Packer.toBuffer(doc)
writeFileSync(outPath, buffer)
console.log(`[build_docx] ✓ ${spec.sections?.length ?? 0} sections → ${outPath}`)
