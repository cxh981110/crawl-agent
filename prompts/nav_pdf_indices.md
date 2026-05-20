# Business Goal

Extract product NAV or yield index records from the target URL.

# Source Understanding

The target may be a PDF document or a rendered web page. Use the available content tools according to the actual source shape and interpret returned text, tables, and page/API content.

# Business Constants

Use `tool_hints.bank_name` as `bankName` and `tool_hints.platform_code` as `platformCode` when those values are provided. These are business constants, not tool-routing directives.

# Extraction Rules

- Extract all product index records shown in the source.
- Each product/date/class row must be returned as one item in `indices`.
- For NAV-style products, extract `productCode`, `navDate`, `nav`, and `accNav` when available.
- For cash-management or yield-style products, extract `productCode`, `navDate`, `tenThousandRevenue`, and `sevenDayAnnualizedYield` when available.
- Do not merge different product classes unless the source clearly presents them as the same record.
- Deduplicate exact repeated rows using product code, nav date, and the available numeric index fields.
- Preserve numeric values as strings exactly enough to avoid rounding changes.

# Date Rules

Normalize dates to `YYYY-MM-DD` when possible. Use the date attached to the index row, not merely the announcement publish date, unless the source clearly states the index date is the announcement date.

# Missing Values

Use missing optional numeric fields only when the source does not provide that index type. Include `indices` in `missing_fields` if no valid index rows can be extracted.

# Output Requirements

Return strict JSON matching the provided schema. Do not add fields outside the schema.
