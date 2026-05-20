# Business Goal

Extract company profile information from the target page.

# Source Understanding

Use page content returned by the available content tools. If the page includes API responses in `network_json`, treat those response bodies as page content. Otherwise use visible text, cleaned HTML, links, and detected tables.

# Extraction Rules

- Extract the company name, contact phone, email, address, and products.
- Prefer values that describe the target company itself, not advertisers, partners, navigation menus, or unrelated recommended content.
- If multiple phone numbers are present, choose the primary customer-service or contact number.
- If multiple addresses are present, choose the registered address or main office address when labels make that clear.
- Extract products as a list of product names when a product list is present.
- Do not infer products from generic marketing text unless the content clearly names products or services.

# Missing Values

- Use `null` for unavailable scalar fields.
- Use `null` for `products` if no product names can be identified.
- Include missing field names in `missing_fields`.

# Output Requirements

Return strict JSON matching the provided schema. Do not add fields outside the schema.
