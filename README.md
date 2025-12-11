**SOC 2 Extraction App**

A Flask app that ingests a SOC 2 PDF, segments narrative vs. tables, runs multi-phase extraction via AWS Bedrock, merges/normalizes the results, and generates a downloadable landscape DOCX report.

Frontend: a single HTML page at / with upload → parse → extract → download flow.

PDF parsing & sectioning using pdfminer.six, with detection of “SECTION I/II/III…” (roman, numeric, or word) to classify pages as narrative vs. table content.

Multi-phase Bedrock extraction: auditor opinion, vendor controls, exceptions, subservice controls, user-entity controls, and criteria mappings; includes retries/backoff and cursor-based batching for large reports.

Criteria ↔ control merge so each control includes mapped TSC criteria without duplication.

DOCX report builder with sections for general info, criteria mappings, subservice/user-entity controls, vendor controls, and exceptions (landscape layout, narrow margins).

Minimal UI: upload → parse → extract → download link, with status badges and JSON preview.

**Architecture**
/app.py
/prompts.py  # required constants imported by app.py (already provided)

**High-level flow:**
/parse — Reads PDF, extracts text per page, detects section markers, builds two blobs:

table_text (Section III+ → tables/controls)

narrative_text (Sections 0–II → cover, TOC, opinion/management)
Results are cached in-memory under a doc_id.

/extract_cursor/<doc_id> — Orchestrates multiple phases against Bedrock:
Auditor opinion → vendor controls (cursor+batching) → exceptions → subservice controls (cursor+batching) → user-entity controls (cursor+batching) → criteria mappings → merge criteria into controls → build DOCX & return a report_id.

/download/docx/<report_id> — Streams the generated report.

**Internals:**

invoke_bedrock_with_retry wraps Bedrock calls with exponential backoff and optional logging truncation.

In-memory PARSED_CACHE stores full_text, table_text, narrative_text, and pages for the active doc_id.

**Requirements**

Python 3.10+ (recommended)
AWS credentials with permission to call Bedrock Runtime
System deps for pdfminer.six and python-docx (for report generation)

**Python packages**
flask
boto3
botocore
pdfminer.six
python-docx

python-docx is required for DOCX generation.

**Configuration**
All config via environment variables:

**Variable	Default	Notes**
BEDROCK_REGION	
BEDROCK_MODEL_ID	
READ_TIMEOUT	120	Bedrock client read timeout (s).
CONNECT_TIMEOUT	15	Bedrock client connect timeout (s).
MAX_TOTAL_TOKENS	10000	Token cap for Bedrock calls.
TEMPERATURE	0.0	Deterministic output.
REPORT_DIR	
BATCH_SIZE	20	Controls per extraction pass (cursor batching).
MAX_RETRIES	3	Retries for Bedrock calls.
LOG_BEDROCK_FULL	1	If truthy, logs full Bedrock JSON (consider redaction).
LOG_BEDROCK_MAX_CHARS	0	If >0, truncates logged JSON to this many chars.
AWS_ACCESS_KEY, AWS_SECRET

Example .env:

BEDROCK_REGION=
BEDROCK_MODEL_ID=
MAX_TOTAL_TOKENS=10000
TEMPERATURE=0.0
BATCH_SIZE=20
MAX_RETRIES=3
LOG_BEDROCK_FULL=0
REPORT_DIR=
AWS_ACCESS_KEY=...
AWS_SECRET=...

Running Locally
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export FLASK_ENV=development
export PORT=5000
python app.py


Visit: http://localhost:5000/

Endpoints
GET /

Returns a minimal HTML UI to upload a SOC 2 PDF and kick off extraction.

POST /parse

Body: multipart/form-data with pdf file.
Response (JSON):

{
  "ok": true,
  "doc_id": "abcd1234",
  "total_pages": 72,
  "table_pages": 41,
  "narrative_pages": 31,
  "full_chars": 123456,
  "table_chars": 98765
}


On error: {"ok": false, "error": "..."} with 4xx/5xx.

POST /extract_cursor/<doc_id>

Runs the full multi-phase extraction and builds the DOCX.
Response (JSON, abbreviated):

{
  "ok": true,
  "result": {
    "extraction": {
      "auditor_opinion": { "...": "..." },
      "vendor_controls": [
        {
          "control_id": "C-001",
          "criterion": ["CC1.1"],
          "control_title": "",
          "control_description": "",
          "tests_applied": ["", ""],
          "result": "No exceptions noted"
        }
      ],
      "exceptions": [ { "control_objective": "...", "...": "..." } ],
      "subservice_controls": [ ... ],
      "user_entity_controls": [ ... ]
    },
    "criteria_mappings": [
      { "criterion_id": "CC1.1", "description": "...", "mapped_controls": ["C-001","C-002"] }
    ],
    "meta": {
      "vendor_controls_found": 120,
      "exceptions_found": 1,
      "subservice_controls_found": 8,
      "user_entity_controls_found": 10,
      "criteria_found": 15,
      "vendor_passes": 7,
      "subservice_passes": 2,
      "user_entity_passes": 2
    }
  },
  "report_id": "efgh5678",
  "elapsed_sec": 42.13,
  "passes": 7
}


On error: {"ok": false, "error": "Bedrock error (...)"}.

GET /download/docx/<report_id>

Downloads the generated Word document soc2_report_<report_id>.docx.

**CLI Examples**

Parse:

curl -F "pdf=@/path/to/report.pdf" http://localhost:5000/parse


Extract + build report:

curl -X POST http://localhost:5000/extract_cursor/<doc_id>


**Download report:**

curl -L -o soc2_report.docx http://localhost:5000/download/docx/<report_id>

Page Classification Logic

Sections 0, I, II → narrative (cover, TOC, auditor/management).

Sections III+ → table content (system description, controls, testing).

Fallback heuristics (e.g., “no exceptions noted”, CC1.1 patterns, multi-column lines) apply when section detection is ambiguous.

**Report Layout (DOCX)**

General Information (service/product, report type, scope date, auditor, qualified opinion, opinion text)

Criteria Mappings (criterion → description → mapped controls)

Complementary Subservice Organization Controls

Complementary User Entity Controls

Vendor/Service Organization Controls (ID, criteria, title, description, tests, result)

Exceptions (objective, testing, exception, management response)

Formatting: landscape, ~0.5″ margins, compact tables, minimal borders.

**Tuning & Logging**

Batching: BATCH_SIZE controls rows per Bedrock pass.

Retries: MAX_RETRIES with exponential backoff.

Logging: enable LOG_BEDROCK_FULL=1 for full response logs; limit with LOG_BEDROCK_MAX_CHARS. Be mindful of sensitive content.

**Security & Privacy**

PDFs are held in memory during parsing and cached in-process (PARSED_CACHE) keyed by doc_id.

DOCX files are written to REPORT_DIR (or a temp fallback) and served via /download/docx/<rid>.

Bedrock payloads may include document text; consider disabling full logging in production and restricting access to the app.

**Troubleshooting**

Import error: prompts → Ensure prompts.py exists with the required constants (see below).

DOCX generation error → Install python-docx.

Empty/partial extraction → Increase MAX_TOTAL_TOKENS, reduce BATCH_SIZE, and verify your prompts’ {batch_size} / {cursor} placeholders.

Bedrock connectivity → Check AWS creds/region; the app supports explicit AWS_ACCESS_KEY/AWS_SECRET or default Boto3 providers.

Windows report path → Override REPORT_DIR on Linux/macOS; the app will fall back to a temp dir if creation fails.

API Contract (Prompts → Expected JSON)

Model responses are parsed from text into JSON. Prompts must return strict JSON (no code fences). Example shapes:

Auditor opinion

{
  "auditor_opinion": {
    "service_product": "",
    "report_type": "SOC 2 Type 2",
    "scope_date": "01/01/2024 - 12/31/2024",
    "auditors_opinion": "",
    "auditors_name": "",
    "qualified_opinion": false
  }
}


Vendor controls (batched with cursor)

{
  "extraction": {
    "controls": [
      {
        "control_id": "1.1",
        "criterion": ["CC6.1", "CC6.2"],
        "control_title": "",
        "control_description": "",
        "tests_applied": [""],
        "result": "No exceptions noted"
      }
    ]
  },
  "meta": { "has_more": true, "last_control_id": "1.1" }
}

Similar shapes are expected for subservice/user-entity controls, criteria mappings, and exceptions.

**Maintainers**
Primary: @brian.wardell
Logging prefix: soc2 (via logging.getLogger("soc2"))
