# Web Extraction Agent (LLM + MCP)

This project runs an autonomous extraction agent with generic tools.
You can switch business scenarios by only changing `businesses/*.json`.

## Run

Create `.env` in project root (or copy from `.env.example`):

```bash
OPENAI_API_KEY=your_api_key
OPENAI_MODEL=qwen3.5-plus
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

Then run:

```bash
python -m agent.llm_cli "https://example.com/detail-page" --business company_profile
```

The model discovers and calls MCP tools automatically.
Agent orchestration is implemented with LangGraph for controllable tool loops and routing.

## Multi-business configs

Business configs are in `businesses/*.json`. Each file defines:

- `prompt`: business-specific extraction intent
- `record_mode`: `object` or `array`
- `source_hint`: `auto` / `table` / `list`
- `tool_hints`: optional tool parameters (selectors, table hints, max items, token budget)
- `fields_json`: schema passed to `extract_fields`
- `output_schema`: JSON schema used to validate final LLM output

Example:

```bash
python -m agent.llm_cli "https://example.com/detail-page" --business fund_snapshot
```

Announcement-list example:

```bash
python -m agent.llm_cli "https://www.beijingbobwealth.com.cn/xxpl/cpgg/fxgg/index.html" --business bank_announcements
```

## Generic toolchain

- Fixed internal preprocessing: fetch HTML + clean noise + build context (not exposed to LLM as separate tools)
- `extract_table_rows`: array extraction for table pages
- `extract_list_rows`: array extraction for list pages (announcements/news/notices)
- `extract_fields`: object extraction pipeline with built-in preprocessing

## Output

The command prints JSON with:

- `data`: extracted field values
- `missing_fields`: fields that could not be found
- `strategy`: extraction strategy returned by the LLM/tool flow
- `evidence`: why each field was extracted

## Install

```bash
pip install -r requirements.txt
```
