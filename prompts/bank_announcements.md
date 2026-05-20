# Business Goal

Extract announcement records from the target source. Return one announcement per item in the `data` array.

# Source Understanding

The source may be a rendered web page, a list page, or page content returned from an API response. Treat visible text, cleaned HTML, detected tables, links, and `network_json` as possible evidence.

# Extraction Rules

- Extract announcement title, announcement URL, and publish date.
- Keep only announcement-like records. Ignore navigation links, category tabs, footer links, and unrelated product links.
- Resolve relative announcement URLs against the target page URL when possible.
- Normalize dates to `YYYY-MM-DD` when the source provides a recognizable date.
- If the same announcement appears more than once, keep only one record.
- Deduplicate by announcement URL first. If URL is missing or identical, deduplicate by normalized title plus publish date.
- If two duplicates have complementary values, keep the version with more complete fields.

# Missing Values

- Use `null` when a required field is not present in the returned content.
- Include field names in `missing_fields` only when that field is missing for all or most records.

# Output Requirements

Return strict JSON matching the provided schema. Do not add fields outside the schema.
