# Business Goal

Extract all fund sales or distribution institutions from the target page. Return each institution as one item in the `data` array.

# Source Understanding

The target is often a SPA page. The useful evidence may be visible page text, rendered HTML, detected tables, or JSON responses returned in `network_json`. Treat all of those as source content.

# Extraction Rules

* Extract every sales organization, distribution organization, direct-sales channel, or agency listed for the target fund.
* Each organization or channel must be returned as a separate record.
* Use the target fund name and fund code for every record.
* Prefer organization names from a dedicated sales/distribution section or a relevant JSON response such as an agency list.
* Do not mix records from other funds when the page or API response contains multiple products.
* Deduplicate organizations by normalized `org_name`.
* If the same organization appears in both visible text and JSON response, keep one record.
* If the available same-site content exposes only direct-sale channels, return those direct-sale records instead of continuing to search indefinitely.
* Do not repeat the same tool call with the same URL and arguments. Use already returned content to produce the best valid JSON.

# Direct-Sale Identification Rules

* At most one institution may be identified as a direct-sale institution (`is_distributor = false`).
* If the page explicitly identifies an institution as a direct-sale channel, direct-sale institution, fund-company direct sale, or similar wording, mark that institution as:

  * `is_distributor = false`
* All third-party agencies or external sales channels must be marked as:

  * `is_distributor = true`
* If the source does not explicitly identify a direct-sale institution, compare the institution name with the fund-company/site-company name.
* If the institution name substantially matches the fund-company name, treat that institution as the direct-sale institution.
* Minor suffix differences should still be considered a match, including examples such as:

  * `国金基金`
  * `国金基金管理`
  * `国金基金管理有限公司`
* When multiple institutions partially match the company name, keep only the strongest or most explicit match as:

  * `is_distributor = false`
* All remaining institutions should be marked as:

  * `is_distributor = true`

# Contact Information Rules

* Only extract the following contact information types:

  * official website URL
  * landline phone number
  * customer-service phone number
* Do not extract or store:

  * WeChat accounts
  * QR codes
  * mini-programs
  * social-media accounts
  * email addresses
  * QQ accounts
  * online customer-service links
  * app-download links
  * or any other non-phone contact channels

# Field Rules

* `fund_name`: use the full fund/product name when available.
* `fund_code`: use the code from the target URL or page content.
* `org_name`: use the institution or channel name exactly as shown, with surrounding whitespace removed.
* `phone`: use customer-service or contact phone for the organization if explicitly provided; otherwise `null`.
* `WEB`: use official website URL if explicitly provided; otherwise `null`.
* `is_distributor`:

  * `false` for the single identified direct-sale institution
  * `true` for third-party distributors or agencies
  * `null` only when the source provides insufficient evidence

# Missing Values

Use `null` for missing `phone`, `WEB`, or uncertain `is_distributor`.

Include missing field names in `missing_fields` when they are generally unavailable in the source.

# Output Requirements

Return strict JSON matching the provided schema. Do not add fields outside the schema.
