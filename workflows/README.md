# workflows/

Markdown SOPs that tell the agent what to do. Plain language, like briefing a teammate.

## Each workflow should include

- **Objective** — what success looks like
- **Inputs** — what the agent needs before starting (params, credentials, source files)
- **Tools** — which scripts in `tools/` to call, in what order
- **Outputs** — where the result lands (Google Sheet URL, Slides deck, etc.)
- **Edge cases** — known failure modes and how to handle them
- **Lessons learned** — append discoveries (rate limits, schema quirks) as they come up

## Naming

Use verb_noun: `fetch_daily_sales.md`, `scrape_website.md`, `generate_weekly_report.md`.

## Don't overwrite without asking

Workflows are durable instructions. Refine them; don't trash them.
