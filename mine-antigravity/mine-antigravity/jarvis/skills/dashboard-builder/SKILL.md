---
name: dashboard-builder
description: "Use this skill to generate interactive HTML dashboards with KPI cards, charts (bar/line/pie/doughnut), data tables, and text sections. Trigger this when users ask for: dashboards, analytics views, sales reports with charts, KPI summaries, or any data visualization that should be viewed in a browser. The output is a single self-contained HTML file with Chart.js charts, sticky navigation header, and responsive grid layout. Themes: dark (default), light, corporate."
---

# Dashboard Builder — Interactive HTML Dashboards

Generates a **self-contained single-file HTML dashboard** with Chart.js charts, KPI cards, and tables. No server needed — just open in any browser.

## Workflow

1. **Understand the data** — what metrics, charts, or tables are needed?
2. **Choose a theme** — `dark` (default), `light`, or `corporate`
3. **Write the JSON spec** (see schema below)
4. **Render:**
   ```bash
   node jarvis/skills/dashboard-builder/scripts/build_dashboard.js spec.json output.html
   ```
   Or use the tool directly:
   ```
   BuildDashboard({ spec: { ... }, output_path: "dashboard.html" })
   ```

## Section Types

### `kpi` — Key Performance Indicators
```json
{
  "type": "kpi",
  "items": [
    { "label": "Revenue",    "value": "$4.2M",  "trend": "+18%", "positive": true },
    { "label": "Users",      "value": "12,400", "trend": "+5%" },
    { "label": "Churn Rate", "value": "2.1%",   "trend": "-0.3%", "positive": true }
  ]
}
```

### `chart` — Charts (bar / line / pie / doughnut)
```json
{
  "type": "chart",
  "chart_type": "bar",
  "title": "Monthly Revenue",
  "labels": ["Jan", "Feb", "Mar", "Apr"],
  "datasets": [
    { "label": "Revenue", "data": [1.2, 1.8, 2.1, 2.4], "color": "#22d3ee" },
    { "label": "Expenses", "data": [0.9, 1.1, 1.3, 1.5], "color": "#f87171" }
  ]
}
```

### `table` — Data Tables
```json
{
  "type": "table",
  "title": "Top Customers",
  "headers": ["Customer", "Revenue", "Growth", "Status"],
  "rows": [
    ["Acme Corp",   "$1.2M", "+22%", "Active"],
    ["Globex Inc",  "$0.8M", "+15%", "Active"]
  ]
}
```

### `text` — Text / Insights
```json
{
  "type": "text",
  "title": "Key Insights",
  "body": "Q3 revenue grew 18% driven by enterprise deals closing in the final week..."
}
```

## Full Spec Example
```json
{
  "title": "Q3 Sales Dashboard",
  "subtitle": "September 2024",
  "theme": "dark",
  "sections": [
    {
      "type": "kpi",
      "items": [
        { "label": "Total Revenue", "value": "$4.2M", "trend": "+18%", "positive": true },
        { "label": "New Customers", "value": "234",   "trend": "+31%", "positive": true },
        { "label": "Avg Deal Size", "value": "$18K",  "trend": "+8%" },
        { "label": "Win Rate",      "value": "42%",   "trend": "+5%", "positive": true }
      ]
    },
    {
      "type": "chart",
      "chart_type": "bar",
      "title": "Monthly Revenue vs Target",
      "labels": ["Jul", "Aug", "Sep"],
      "datasets": [
        { "label": "Actual",  "data": [1.1, 1.4, 1.7], "color": "#22d3ee" },
        { "label": "Target",  "data": [1.2, 1.3, 1.6], "color": "#818cf8" }
      ]
    },
    {
      "type": "chart",
      "chart_type": "doughnut",
      "title": "Revenue by Segment",
      "labels": ["Enterprise", "Mid-Market", "SMB"],
      "datasets": [{ "label": "Revenue", "data": [2.1, 1.5, 0.6] }]
    },
    {
      "type": "table",
      "title": "Top Deals Closed",
      "headers": ["Company", "Value", "Rep", "Close Date"],
      "rows": [
        ["Acme Corp",  "$420K", "Sarah J.", "Sep 28"],
        ["Globex Inc", "$310K", "Mike T.",  "Sep 22"]
      ]
    }
  ]
}
```

## Common Mistakes

- **Missing `type` field** — every section must have `"type": "kpi"`, `"chart"`, `"table"`, or `"text"`
- **Wrong chart_type** — use exactly: `bar`, `line`, `pie`, `doughnut`, `polarArea`
- **KPI trend direction** — set `"positive": true` when a decrease is good (e.g., churn, costs)
- **No labels for chart** — labels array length must match data array length in each dataset