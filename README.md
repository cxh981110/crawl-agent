# Web Extraction Agent (LLM + MCP)

This project runs an autonomous extraction agent with generic tools.
You can switch business scenarios by changing `businesses/*.json` and the matching prompt in `prompts/*.md`.

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

- `prompt_file`: optional Markdown prompt filename under `prompts/`; defaults to `<business>.md`
- `record_mode`: `object` or `array`
- `tool_hints`: optional business constants or runtime options; never a tool-routing directive
- `fields_json`: target business fields for the LLM to extract from tool-returned content
- `output_schema`: JSON schema used to validate final LLM output

Business prompts are stored in `prompts/*.md`. Keep prompts focused on business goals, extraction rules, missing-value policy, deduplication, and output constraints. Do not hard-code which MCP tool should be called in a business prompt.

Example:

```bash
python -m agent.llm_cli "https://example.com/detail-page" --business fund_snapshot
```

Announcement-list example:

```bash
python -m agent.llm_cli "https://www.beijingbobwealth.com.cn/xxpl/cpgg/fxgg/index.html" --business bank_announcements
```

## Generic toolchain

- `get_html_content`: fetch and clean a web page, returning cleaned HTML, visible text, and detected tables
- `parse_pdf_content`: download and parse a PDF, returning page text and detected tables
- The LLM chooses the MCP tool and performs the final business extraction from returned content.

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
