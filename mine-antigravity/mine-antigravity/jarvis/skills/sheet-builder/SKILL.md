---
name: sheet-builder
description: "Use this skill to generate formatted Excel workbooks (.xlsx) or CSV files from structured data. Trigger this when users ask for: spreadsheets, Excel files, data exports, financial tables, CSV exports, multi-sheet workbooks, or any tabular data that should be opened in Excel or Google Sheets. Supports multiple sheets, styled headers with color themes, alternating row colors, freeze-top, auto-filter, and auto-column widths. Falls back to clean CSV if ExcelJS is not installed."
---

# Sheet Builder — Excel & CSV Generation

Generates **production-quality Excel workbooks** (.xlsx) with styled headers, alternating rows, freeze-top, and auto-filter. Falls back to clean CSV automatically if ExcelJS isn't available.

## Workflow

1. **Plan the sheets** — how many sheets, what columns and rows?
2. **Choose a theme** — `blue` (default), `green`, `dark`, `minimal`, `purple`
3. **Write the JSON spec** (see schema below)
4. **Render:**
   ```bash
   node jarvis/skills/sheet-builder/scripts/build_sheet.js spec.json output.xlsx
   node jarvis/skills/sheet-builder/scripts/build_sheet.js spec.json output.csv  # CSV mode
   ```
   Or use the tool:
   ```
   BuildSheet({ spec: { ... }, output_path: "data.xlsx" })
   ```

## Spec Schema

```json
{
  "title": "Workbook Title",
  "sheets": [
    {
      "name": "Sheet Name",
      "style": "blue",
      "headers": ["Column A", "Column B", "Column C"],
      "rows": [
        ["Row 1 A", "Row 1 B", "Row 1 C"],
        ["Row 2 A", "Row 2 B", "Row 2 C"]
      ],
      "column_widths": [20, 15, 30],
      "freeze_top": true
    }
  ],
  "formats": ["xlsx"]
}
```

## Header Style Themes

| Theme     | Header Color | Best For |
|-----------|-------------|----------|
| `blue`    | Navy blue   | Business / Financial |
| `green`   | Emerald     | Environmental / Sales |
| `dark`    | Charcoal    | Technical / Engineering |
| `minimal` | Light gray  | Clean / Minimal |
| `purple`  | Violet      | Marketing / Creative |

## Full Spec Example

```json
{
  "title": "Q3 Financial Report",
  "sheets": [
    {
      "name": "Revenue",
      "style": "blue",
      "freeze_top": true,
      "headers": ["Month", "Revenue", "Target", "Delta", "Growth %"],
      "rows": [
        ["January",   "$1,200,000", "$1,100,000", "$100,000",  "9.1%"],
        ["February",  "$1,800,000", "$1,500,000", "$300,000",  "20%"],
        ["March",     "$2,100,000", "$1,800,000", "$300,000",  "16.7%"],
        ["Q3 TOTAL",  "$5,100,000", "$4,400,000", "$700,000",  "15.9%"]
      ],
      "column_widths": [14, 14, 14, 14, 12]
    },
    {
      "name": "Top Customers",
      "style": "green",
      "freeze_top": true,
      "headers": ["Company", "Revenue", "Country", "Segment", "Status"],
      "rows": [
        ["Acme Corp",   "$420,000", "US", "Enterprise", "Active"],
        ["Globex Inc",  "$310,000", "UK", "Mid-Market", "Active"],
        ["Initech LLC", "$180,000", "CA", "SMB",        "At Risk"]
      ]
    }
  ]
}
```

## Common Mistakes

- **Unequal rows** — every row must have the same number of cells as `headers`
- **String numbers** — if you want Excel to treat values as numbers for formulas, omit quotes: `42` not `"42"`
- **Long sheet names** — Excel limits sheet names to 31 characters
- **Single sheet with CSV** — CSV only exports the first sheet
