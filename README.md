# SOC_Extract
Extract core info from SOC II Reports
# SOC 2 Control Extractor

A Flask-based application that extracts control information from SOC 2 PDF reports using AWS Bedrock (Claude) and generates structured DOCX reports.

## Features

- **PDF Parsing**: Extracts text from SOC 2 PDFs using pdfminer with intelligent table/narrative segmentation
- **Multi-Phase Extraction**: Extracts three types of controls:
  - Vendor/Service Organization Controls (with tests and results)
  - Complementary Subservice Organization Controls
  - Complementary User Entity Controls
- **Criteria Mapping**: Extracts and merges criteria mappings from both control tables and dedicated mapping sections
- **Prompt Caching**: Optimized for AWS Bedrock prompt caching to reduce costs on multi-call extractions
- **DOCX Report Generation**: Produces formatted Word documents with separate tables for each control type

## Architecture

┌─────────────────────────────────────────────────────────────────────┐
│                         PDF Upload                                   │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Parse & Segment                                   │
│  • Extract text with pdfminer                                       │
│  • Identify table pages vs narrative pages                          │
│  • Cache: full_text, table_text, narrative_text                     │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│              Multi-Phase LLM Extraction (Shared Cache)              │
│                                                                      │
│  Phase 1: Vendor Controls (batched, cursor-based)                   │
│  Phase 2: Subservice Organization Controls (single call)            │
│  Phase 3: User Entity Controls (single call)                        │
│  Phase 4: Criteria Mappings (single call)                           │
│  Phase 5: Merge criteria into vendor controls                       │
└─────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    DOCX Report Generation                           │
│  • Vendor Controls table (ID, Criteria, Title, Desc, Tests, Result) │
│  • Subservice Controls table (Org, ID, Description, Criteria)       │
│  • User Entity Controls table (Category, ID, Description, Criteria) │
└─────────────────────────────────────────────────────────────────────┘
```

**Installation**

**Prerequisites**
Python 3.8+
AWS credentials configured with Bedrock access

**Environment Variables**

| Variable | Default | Description |
|----------|---------|-------------|
| `BEDROCK_REGION` | `us-east-1` | AWS region for Bedrock |
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-haiku-4-5-20251001-v1:0` | Claude model ID |
| `READ_TIMEOUT` | `120` | Bedrock read timeout (seconds) |
| `CONNECT_TIMEOUT` | `15` | Bedrock connect timeout (seconds) |
| `MAX_TOTAL_TOKENS` | `10000` | Max tokens for LLM response |
| `TEMPERATURE` | `0.0` | LLM temperature |
| `BATCH_SIZE` | `20` | Controls per extraction batch |
| `MAX_RETRIES` | `3` | Bedrock retry attempts |
| `REPORT_DIR` | `./generated_reports` | Directory for DOCX output |
| `LOG_BEDROCK_FULL` | `1` | Log full Bedrock responses |
| `LOG_BEDROCK_MAX_CHARS` | `0` | Truncate logs (0 = no limit) |
| `AWS_ACCESS_KEY` | - | Optional AWS access key |
| `AWS_SECRET` | - | Optional AWS secret key |

**Usage:**

**Start the Server**

```bash
python app.py
```

The server starts on `http://localhost:5000` by default.

**Web Interface**

1. Open `http://localhost:5000` in your browser
2. Upload a SOC 2 PDF file
3. Click "Run Extraction"
4. Download the generated DOCX report

**Data Schemas:**

**Vendor Control**
```json
{
  "control_id": "1.1",
  "criterion": ["CC1.1", "CC1.2"],
  "control_title": "Board Oversight",
  "control_description": "The board of directors meets quarterly...",
  "tests_applied": ["Inspected board meeting minutes", "Inquired of management"],
  "result": "No exceptions noted"
}
```

**Subservice Organization Control**
```json
{
  "name": "Amazon Web Services",
  "controls": [
    {
      "control_id": "1",
      "description": "AWS provides physical security controls...",
      "criteria_covered": ["CC6.1", "CC6.2"]
    }
  ]
}
```

**User Entity Control**
```json
{
  "name": "User Entity Controls",
  "controls": [
    {
      "control_id": "1",
      "description": "Users are responsible for maintaining strong passwords...",
      "criteria_covered": ["CC6.1"]
    }
  ]
}
```

**Criteria Mapping**
```json
{
  "criterion_id": "CC6.1",
  "criterion_description": "The entity implements logical access security...",
  "mapped_controls": ["6.1", "6.2", "6.3", "6.4", "6.5"]
}
```

**Table Detection**

The app intelligently segments PDF pages into table content vs narrative content to reduce LLM context size. Table pages are identified by:

**Positive Indicators:**
- Control reference patterns (e.g., `1.1`, `6.15`) combined with result phrases
- Table column headers ("Controls Specified", "Testing Performed", "Results of Testing")
- Criteria references (`CC1.1`, `CC6.2`)
- Dense testing language ("inspected", "inquired", "determined", "no exceptions noted")
- "(continued)" markers

**Negative Indicators:**
- Long prose paragraphs (>500 chars)
- Narrative section headers ("Overview of Operations", "Management's Assertion")

Pages directly before detected table pages are automatically included to capture table headers.

## Prompt Caching

The app is optimized for AWS Bedrock's prompt caching feature:

- A single system prompt containing the document text is created once
- All extraction phases (vendor controls, subservice, user entity, criteria) reuse this cached prompt
- Only the first call creates the cache; subsequent calls read from cache at reduced cost

**Expected token usage:**
```
Call 1: cache_creation_input_tokens: ~26000, cache_read_input_tokens: 0
Call 2+: cache_creation_input_tokens: 0, cache_read_input_tokens: ~26000
```

**File Structure**

```
├── app.py              # Main Flask application
├── prompts.py          # LLM prompt templates
└── README.md           # This file
```

**Troubleshooting**

Extraction loops indefinitely
The LLM may not be setting `has_more: false` correctly. Check:
- Prompt clarity about when to stop
- Whether the document structure matches expected patterns
- Bedrock logs for the actual responses

Missing criteria
Criteria can come from two sources:
1. Directly from control tables (column or header)
2. From dedicated mapping tables (Section V)

The app merges both without duplicates. Check logs for:
[Merge] X controls had criteria from extraction, Y had criteria added from mapping


PDF parsing issues
Some PDFs may not extract cleanly with pdfminer. Try:
- Ensuring the PDF is text-based (not scanned images)
- Checking the test_parser.py output for extraction quality
