---
name: chart-format
description: "Use when: an answer is easier as a chart (token/AIU spend split, sprint burndown, counts per day/ticket, trends, comparisons) — emit a `chart` fenced block so the dop-agent Web UI renders a chart (Slack shows plain code, so add a one-line text summary)."
---
# `chart` fence

````
```chart
type: donut | pie | bar | hbar | stacked | line | burndown | spark
title: optional
unit: optional suffix (SP, AIU, turns)
center: text in donut hole        # donut only
x: a, b, c                        # x labels (required for bar/stacked/line/burndown)
ideal: 34 -> 0                    # burndown only
cards: Total=194k, Watcher=71k    # stat cards above the chart
table: yes                        # data table under the chart
data:
  - Label: v1[, v2, ...]
```
````

- donut/pie/hbar: one row per slice/bar, first value used. bar/stacked/line/burndown/spark: one row per **series**, one value per `x`. `x:` is required except for donut/pie/hbar/spark.
- Not raw Chart.js config (`{"data":{"labels":..,"datasets":..}}`) — it fails to render. Use `x:` + `data:` rows, or the JSON shape `{"type":..,"x":"a,b","data":[{"label":"S","values":[1,2]}]}` (YAML-ish form preferred).
- Values are numbers (`null` = gap). Max 500 points. `table: yes` is ignored for spark.
- An invalid fence is shown as code with a note, so keep it valid. Blocks render only after the turn completes.
- Examples: donut `data: - Watcher: 71` / `- Interactive: 123` with `center: 36% watcher`; burndown `x: D1, D2, D3`, `ideal: 10 -> 0`, `- Remaining: 10, 7, 4`.
- Implementation: `dop-agent/webui/chart.js` (+ vendored `webui/vendor/chart.umd.min.js`); test `ui_tests/test_chart_blocks.py`.
