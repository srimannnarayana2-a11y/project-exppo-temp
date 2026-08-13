---
name: report-builder
description: "Use this skill to generate professional Word documents (.docx) or PDF reports from structured content. Trigger this when users ask for: reports, documents, Word files, PDF exports, executive summaries, proposals, analysis documents, or any long-form structured document. Supports sections with body text, tables with styled headers, code blocks, bullet lists, and stat/metric highlights. Three themes: corporate (default), minimal, dark."
---

# Report Builder — Word Documents & PDF Reports

Generates **premium-quality Word documents** (.docx) and **styled HTML reports** (convertible to PDF) comparable to what you'd get from paid document AI tools.

## Workflow

1. **Plan the structure** — title, subtitle, author, sections
2. **Choose a theme** — `corporate` (default), `minimal`, `dark`
3. **Write the JSON spec**
4. **Render:**
   ```bash
   # Word document
   node jarvis/skills/report-builder/scripts/build_docx.js spec.json output.docx

   # HTML/PDF report
   node jarvis/skills/report-builder/scripts/build_pdf.js spec.json output.pdf
   ```
   Or use the tool:
   ```
   BuildReport({ spec: { ... }, formats: ["docx"], output_path: "report.docx" })
   BuildReport({ spec: { ... }, formats: ["pdf", "docx"] })
   ```

## Spec Schema

```json
{
  "title": "Report Title",
  "subtitle": "Optional subtitle",
  "author": "Author Name",
  "date": "2024-09-01",
  "theme": "corporate",
  "footer": "Confidential — Acme Corp 2024",
  "sections": [
    { "heading": "Section Name", "body": "Paragraph text..." },
    { "heading": "With Bullets", "bullets": ["Item 1", "Item 2"] },
    { "heading": "Code Block",   "code": "const x = await fetch('/api/data')" },
    {
      "heading": "Data Table",
      "table": {
        "headers": ["Column A", "Column B"],
        "rows": [["Value 1", "Value 2"]]
      }
    },
    {
      "heading": "Metrics",
      "stats": [
        { "label": "Revenue", "value": "$4.2M", "description": "18% YoY growth" },
        { "label": "Users",   "value": "12,400" }
      ]
    }
  ]
}
```

## Full Example — Executive Report

```json
{
  "title": "Q3 Business Review",
  "subtitle": "Strategic Performance Summary",
  "author": "Strategy Team",
  "date": "September 30, 2024",
  "theme": "corporate",
  "footer": "Confidential — Internal Use Only",
  "sections": [
    {
      "heading": "Executive Summary",
      "body": "Q3 2024 was a record-breaking quarter for Acme Corp. Revenue grew 18% year-over-year, driven by strong enterprise adoption of our core platform and the successful launch of our AI features in August.\n\nThe sales team closed 31% more new logos compared to Q2, with average deal size increasing 8% to $18,000 ARR."
    },
    {
      "heading": "Key Metrics",
      "stats": [
        { "label": "Revenue",      "value": "$4.2M",  "description": "+18% YoY" },
        { "label": "New Logos",    "value": "234",    "description": "+31% QoQ" },
        { "label": "NPS Score",    "value": "72",     "description": "Industry avg: 45" },
        { "label": "Churn Rate",   "value": "1.8%",   "description": "All-time low" }
      ]
    },
    {
      "heading": "Revenue Breakdown by Segment",
      "table": {
        "headers": ["Segment",     "Revenue",    "% of Total", "Growth"],
        "rows": [
          ["Enterprise",   "$2.1M",  "50%", "+24%"],
          ["Mid-Market",   "$1.5M",  "36%", "+15%"],
          ["SMB",          "$0.6M",  "14%", "+8%"],
          ["TOTAL",        "$4.2M",  "100%", "+18%"]
        ]
      }
    },
    {
      "heading": "Strategic Initiatives",
      "bullets": [
        "Launch AI-powered onboarding to reduce time-to-value by 40%",
        "Expand EMEA presence with 2 new regional offices",
        "Complete SOC 2 Type II certification by Q4",
        "Hire 15 enterprise AEs to support pipeline growth"
      ]
    },
    {
      "heading": "Risks & Mitigations",
      "body": "The primary risk for Q4 is macroeconomic headwinds affecting enterprise deal velocity. We are mitigating this by offering flexible payment terms and a new ROI calculator for procurement reviews.",
      "bullets": [
        "Deal elongation risk: mitigate with ROI tools and flexible terms",
        "Talent competition: address via compensation benchmarking refresh",
        "Technical debt: allocate 20% engineering capacity to infrastructure"
      ]
    }
  ]
}
```

## Common Mistakes

- **Forgetting the title** — the `title` field is required for the cover page
- **Long code blocks** — split very long code across multiple `{ "code": "..." }` sections
- **Null rows in table** — ensure every row in `rows` has the same number of cells as `headers`
- **Theme mismatch** — `dark` theme is best for dark-background presentations, not printed docs
