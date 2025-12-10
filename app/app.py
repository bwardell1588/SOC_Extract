import os
import io
import json
import uuid
import time
import hashlib
import logging
import re
from json import JSONDecodeError
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple

from flask import Flask, request, jsonify, send_file, render_template_string

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, EndpointConnectionError, ReadTimeoutError

from pdfminer.high_level import extract_text_to_fp
from pdfminer.layout import LAParams

try:
    from docx import Document
    from docx.shared import Inches, Pt
    from docx.enum.section import WD_ORIENT
except Exception:
    Document = None
    Inches = Pt = WD_ORIENT = None

# -----------------------------
# Logging
# -----------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("soc2")

# -----------------------------
# App + Config
# -----------------------------

app = Flask(__name__)

DEFAULT_REPORT_DIR = r"C:\Users\brian.wardell\AI_Projects\soc_extract\app\generated_reports"


@dataclass
class AppConfig:
    BEDROCK_REGION: str = os.getenv("BEDROCK_REGION", "us-east-1")
    BEDROCK_MODEL_ID: str = os.getenv(
        "BEDROCK_MODEL_ID",
        "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    )
    READ_TIMEOUT: int = int(os.getenv("READ_TIMEOUT", "120"))
    CONNECT_TIMEOUT: int = int(os.getenv("CONNECT_TIMEOUT", "15"))
    MAX_TOTAL_TOKENS: int = int(os.getenv("MAX_TOTAL_TOKENS", "10000"))
    TEMPERATURE: float = float(os.getenv("TEMPERATURE", "0.0"))
    REPORT_DIR: str = os.getenv("REPORT_DIR", DEFAULT_REPORT_DIR)
    BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "20"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    LOG_BEDROCK_FULL: bool = os.getenv("LOG_BEDROCK_FULL", "1") not in ("0", "false", "False", "")
    LOG_BEDROCK_MAX_CHARS: int = int(os.getenv("LOG_BEDROCK_MAX_CHARS", "0"))


app.config.from_object(AppConfig)

# -----------------------------
# Reports dir helpers
# -----------------------------


def _resolve_report_dir() -> str:
    base = (app.config.get("REPORT_DIR") or DEFAULT_REPORT_DIR).strip()
    try:
        os.makedirs(base, exist_ok=True)
    except Exception as e:
        import tempfile
        fallback = os.path.join(tempfile.gettempdir(), "soc2_reports")
        os.makedirs(fallback, exist_ok=True)
        logger.warning("Could not create REPORT_DIR %r (%s). Falling back to %r", base, e, fallback)
        base = fallback
    return base


def _report_path_docx(report_id: str) -> str:
    return os.path.join(_resolve_report_dir(), f"soc2_report_{report_id}.docx")


# Cache: doc_id -> {full_text, table_text, narrative_text}
PARSED_CACHE: Dict[str, Dict[str, Any]] = {}

# -----------------------------
# AWS Credentials
# -----------------------------


def _env_creds_from_custom_vars():
    ak = os.getenv("AWS_ACCESS_KEY")
    sk = os.getenv("AWS_SECRET")
    if ak and sk:
        return {"aws_access_key_id": ak, "aws_secret_access_key": sk}
    return {}


# -----------------------------
# Bedrock client
# -----------------------------


def _new_bedrock_client():
    cfg = Config(
        region_name=app.config["BEDROCK_REGION"],
        retries={"max_attempts": 3, "mode": "standard"},
        read_timeout=app.config["READ_TIMEOUT"],
        connect_timeout=app.config["CONNECT_TIMEOUT"],
    )
    return boto3.client("bedrock-runtime", config=cfg, **_env_creds_from_custom_vars())


# -----------------------------
# JSON parsing helpers
# -----------------------------


def _strip_code_fences(text: str) -> str:
    if not text:
        return text
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_model_json(text: str):
    try:
        return json.loads(text)
    except JSONDecodeError:
        pass
    s = _strip_code_fences(text)
    try:
        return json.loads(s)
    except JSONDecodeError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(s[start:end + 1])
    raise JSONDecodeError("Model text was not valid JSON", doc=s, pos=0)


# -----------------------------
# Bedrock invocation
# -----------------------------


def _maybe_truncate(s: str) -> str:
    max_chars = app.config["LOG_BEDROCK_MAX_CHARS"]
    if max_chars and len(s) > max_chars:
        return s[:max_chars] + f"... [truncated to {max_chars} chars]"
    return s


def invoke_bedrock(system_text: str, instruction_text: str) -> Dict[str, Any]:
    client = _new_bedrock_client()
    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": app.config["MAX_TOTAL_TOKENS"],
        "temperature": app.config["TEMPERATURE"],
        "system": [{
            "type": "text",
            "text": system_text,
            "cache_control": {"type": "ephemeral"}
        }],
        "messages": [{"role": "user", "content": [{"type": "text", "text": instruction_text}]}]
    }

    resp = client.invoke_model(
        modelId=app.config["BEDROCK_MODEL_ID"],
        body=json.dumps(request_body)
    )

    raw_body = resp.get("body")
    if not raw_body:
        raise RuntimeError("Bedrock returned empty body.")

    try:
        body_json = json.loads(raw_body.read())
    except Exception as e:
        raise RuntimeError(f"Could not decode Bedrock response: {e}")

    if app.config["LOG_BEDROCK_FULL"]:
        logger.info("[Bedrock] Response:\n%s", _maybe_truncate(json.dumps(body_json, indent=2)))

    contents = body_json.get("content", []) if isinstance(body_json, dict) else []
    text_blocks = [c.get("text") for c in contents if isinstance(c, dict) and c.get("type") == "text"]
    model_text = "".join(filter(None, text_blocks)).strip()

    if not model_text:
        raise RuntimeError("No text content in Bedrock response")

    return _parse_model_json(model_text)


def invoke_bedrock_with_retry(system_text: str, instruction_text: str) -> Dict[str, Any]:
    last_err = None
    for attempt in range(app.config["MAX_RETRIES"]):
        try:
            return invoke_bedrock(system_text, instruction_text)
        except (ReadTimeoutError, EndpointConnectionError, BotoCoreError, RuntimeError, JSONDecodeError) as e:
            last_err = e
            logger.warning("Bedrock attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Bedrock failed after {app.config['MAX_RETRIES']} attempts: {last_err}")


# -----------------------------
# PDF extraction and segmentation
# -----------------------------


def extract_pdf_text(file_bytes: bytes) -> Tuple[str, List[str]]:
    """Extract text from PDF, return (full_labeled_text, list_of_page_texts)."""
    output = io.StringIO()
    extract_text_to_fp(io.BytesIO(file_bytes), output, laparams=LAParams(), output_type="text")
    raw = output.getvalue()

    # Split on form feeds (page breaks)
    pages = raw.split("\f") if "\f" in raw else [raw]
    pages = [p.strip() for p in pages]

    # Create labeled full text with simplified markers
    labeled = [f"=== PAGE {i} ===\n{p}" for i, p in enumerate(pages, 1)]
    full_text = "\n\n".join(labeled)

    return full_text, pages


def is_table_page(page_text: str) -> bool:
    """
    Aggressively detect pages likely containing tables.
    Uses multiple heuristics to catch controls, exceptions, criteria tables, etc.
    """
    text_lower = page_text.lower()
    
    # Strong indicators - if any present, likely a table page
    strong_indicators = [
        r"no exceptions? noted",
        r"results of testing",
        r"testing performed",
        r"controls? specified",
        r"\bref\b.*\bcontrols?\b",
        r"section iv",
        r"section v",
        r"common criteria",
        r"control activities",
        r"trust services criteria",
    ]
    
    for pattern in strong_indicators:
        if re.search(pattern, text_lower):
            return True
    
    # Structural indicators - multiple short lines with consistent patterns suggest tables
    lines = [ln.strip() for ln in page_text.split("\n") if ln.strip()]
    
    # Check for control reference patterns (e.g., 1.1, 2.3, CC6.1)
    ref_pattern = re.compile(r"^(?:CC)?\d+\.\d+\b")
    ref_lines = sum(1 for ln in lines if ref_pattern.match(ln))
    if ref_lines >= 3:
        return True
    

    
    # Check for repeated testing/inspection language
    test_words = ["inspected", "inquired", "determined", "observed", "reviewed", "verified"]
    test_count = sum(1 for word in test_words if word in text_lower)
    if test_count >= 3:
        return True
    
    return False


def segment_content(pages: List[str]) -> Dict[str, str]:
    """Separate table content from narrative content."""
    
    # First pass: classify each page
    is_table = [is_table_page(page) for page in pages]
    
    # Second pass: include the page before any table page
    for i in range(1, len(is_table)):
        if is_table[i] and not is_table[i-1]:
            is_table[i-1] = True
    
    # Build the text segments
    table_parts = []
    narrative_parts = []

    for i, page in enumerate(pages):
        labeled = f"=== PAGE {i+1} ===\n{page}"
        if is_table[i]:
            table_parts.append(labeled)
        else:
            narrative_parts.append(labeled)

    table_text = "\n\n".join(table_parts)
    narrative_text = "\n\n".join(narrative_parts)

    logger.info("[Segment] %d table pages, %d narrative pages", len(table_parts), len(narrative_parts))

    return {
        "table_text": table_text,
        "narrative_text": narrative_text,
    }


# -----------------------------
# Control merging/deduplication
# -----------------------------


def _control_key(c: Dict[str, Any]) -> str:
    title = (c.get("control_title") or "").strip().lower()
    desc = (c.get("control_description") or "").strip().lower()
    return hashlib.sha1(f"{title}|||{desc}".encode()).hexdigest()


def merge_controls(existing: List[Dict[str, Any]], new_ones: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {_control_key(x) for x in existing}
    result = list(existing)

    for c in new_ones or []:
        key = _control_key(c)
        if key not in seen:
            seen.add(key)
            result.append(c)

    # Assign IDs to controls missing them
    next_id = 1
    for c in result:
        if not (c.get("control_id") or "").strip():
            c["control_id"] = f"C-{next_id:03d}"
            next_id += 1

    return result


def merge_criteria_into_controls(
    controls: List[Dict[str, Any]], 
    criteria_mappings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Merge criteria mappings into controls.
    Creates a reverse lookup: control_id -> list of criteria that reference it.
    """
    # Build reverse mapping: control_id -> [criterion_ids]
    control_to_criteria: Dict[str, List[str]] = {}
    
    for mapping in criteria_mappings:
        criterion_id = mapping.get("criterion_id", "")
        mapped_controls = mapping.get("mapped_controls", [])
        
        for control_ref in mapped_controls:
            # Normalize control reference (strip whitespace)
            control_ref = str(control_ref).strip()
            if control_ref:
                if control_ref not in control_to_criteria:
                    control_to_criteria[control_ref] = []
                if criterion_id not in control_to_criteria[control_ref]:
                    control_to_criteria[control_ref].append(criterion_id)
    
    logger.info("[Merge] Built reverse mapping for %d control references", len(control_to_criteria))
    
    # Apply criteria to each control
    for control in controls:
        control_id = (control.get("control_id") or "").strip()
        
        # Try to find matching criteria
        matched_criteria = control_to_criteria.get(control_id, [])
        
        # Also try without leading zeros or with different formats
        if not matched_criteria:
            # Try numeric variations (e.g., "1.1" vs "1.01")
            for ref in control_to_criteria:
                if _normalize_control_ref(ref) == _normalize_control_ref(control_id):
                    matched_criteria = control_to_criteria[ref]
                    break
        
        # Sort criteria for consistent output
        control["criterion"] = sorted(matched_criteria) if matched_criteria else []
    
    return controls


def _normalize_control_ref(ref: str) -> str:
    """Normalize control reference for matching (e.g., '1.01' -> '1.1')."""
    ref = str(ref).strip()
    # Try to parse as X.Y and normalize
    match = re.match(r'^(\d+)\.(\d+)$', ref)
    if match:
        major, minor = match.groups()
        return f"{int(major)}.{int(minor)}"
    return ref


# -----------------------------
# DOCX report builder
# -----------------------------


def _apply_table_font(table, font_size_pt: int = 8) -> None:
    """Apply consistent font size to all cells in a table."""
    for row in table.rows:
        for cell in row.cells:
            for p in cell.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(font_size_pt)


def build_controls_docx(
    vendor_controls: List[Dict[str, Any]],
    subservice_controls: List[Dict[str, Any]],
    user_entity_controls: List[Dict[str, Any]],
    out_path: str
) -> None:
    if Document is None:
        raise RuntimeError("python-docx required: pip install python-docx")

    logger.info("[Report] Building DOCX: %s (vendor=%d, subservice=%d, user_entity=%d)", 
                out_path, len(vendor_controls or []), len(subservice_controls or []), len(user_entity_controls or []))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    doc = Document()

    # Landscape with 0.5" margins
    section = doc.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.top_margin = section.bottom_margin = Inches(0.5)
    section.left_margin = section.right_margin = Inches(0.5)

    doc.add_heading("SOC 2 Control Extraction", 0)

    # ===========================================
    # Table 1: Vendor/Service Organization Controls
    # ===========================================
    doc.add_heading("Vendor/Service Organization Controls", level=1)

    headers = ["Control ID", "Criterion", "Title", "Description", "Tests Applied", "Result"]
    col_widths = [Inches(1.0), Inches(1.0), Inches(2.0), Inches(2.8), Inches(3.0), Inches(0.5)]

    table = doc.add_table(rows=1, cols=len(headers))
    hdr = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = h
        hdr[i].width = col_widths[i]

    for c in vendor_controls or []:
        row = table.add_row().cells
        row[0].text = c.get("control_id") or ""
        criterion = c.get("criterion") or []
        row[1].text = ", ".join(criterion) if isinstance(criterion, list) else str(criterion)
        row[2].text = c.get("control_title") or ""
        row[3].text = c.get("control_description") or ""
        row[4].text = ", ".join(c.get("tests_applied") or [])
        row[5].text = c.get("result") or ""
        for i, cell in enumerate(row):
            cell.width = col_widths[i]

    if not vendor_controls:
        row = table.add_row().cells
        row[2].text = "No vendor controls extracted"

    _apply_table_font(table)

    # ===========================================
    # Table 2: Complementary Subservice Organization Controls
    # ===========================================
    doc.add_paragraph()  # spacing
    doc.add_heading("Complementary Subservice Organization Controls", level=1)

    sub_headers = ["Name", "Description", "Criteria Covered"]
    sub_col_widths = [Inches(2.0), Inches(5.0), Inches(3.0)]

    sub_table = doc.add_table(rows=1, cols=len(sub_headers))
    sub_hdr = sub_table.rows[0].cells
    for i, h in enumerate(sub_headers):
        sub_hdr[i].text = h
        sub_hdr[i].width = sub_col_widths[i]

    for c in subservice_controls or []:
        row = sub_table.add_row().cells
        row[0].text = c.get("name") or ""
        row[1].text = c.get("description") or ""
        criteria = c.get("criteria_covered") or []
        row[2].text = ", ".join(criteria) if isinstance(criteria, list) else str(criteria)
        for i, cell in enumerate(row):
            cell.width = sub_col_widths[i]

    if not subservice_controls:
        row = sub_table.add_row().cells
        row[0].text = "No subservice organization controls found"

    _apply_table_font(sub_table)

    # ===========================================
    # Table 3: Complementary User Entity Controls
    # ===========================================
    doc.add_paragraph()  # spacing
    doc.add_heading("Complementary User Entity Controls", level=1)

    ue_headers = ["Name", "Description", "Criteria Covered"]
    ue_col_widths = [Inches(2.0), Inches(5.0), Inches(3.0)]

    ue_table = doc.add_table(rows=1, cols=len(ue_headers))
    ue_hdr = ue_table.rows[0].cells
    for i, h in enumerate(ue_headers):
        ue_hdr[i].text = h
        ue_hdr[i].width = ue_col_widths[i]

    for c in user_entity_controls or []:
        row = ue_table.add_row().cells
        row[0].text = c.get("name") or ""
        row[1].text = c.get("description") or ""
        criteria = c.get("criteria_covered") or []
        row[2].text = ", ".join(criteria) if isinstance(criteria, list) else str(criteria)
        for i, cell in enumerate(row):
            cell.width = ue_col_widths[i]

    if not user_entity_controls:
        row = ue_table.add_row().cells
        row[0].text = "No user entity controls found"

    _apply_table_font(ue_table)

    doc.save(out_path)
    logger.info("[Report] Wrote: %s (%d bytes)", out_path, os.path.getsize(out_path))


# -----------------------------
# HTML UI
# -----------------------------

INDEX_HTML = r'''
<!doctype html>
<html>
  <head>
    <title>SOC 2 Control Extractor</title>
    <style>
      body { font-family: system-ui, -apple-system, sans-serif; margin: 40px; }
      .card { border: 1px solid #ddd; border-radius: 10px; padding: 20px; max-width: 900px; }
      .row { margin-top: 12px; }
      .muted { color: #666; }
      button { padding: 10px 16px; border-radius: 8px; border: 1px solid #444; background: #111; color: #fff; cursor: pointer; }
      button:disabled { background: #aaa; cursor: not-allowed; }
      pre { background: #f6f8fa; padding: 12px; border-radius: 6px; overflow: auto; }
      .badges { display: flex; gap: 8px; margin: 10px 0; }
      .badge { padding: 6px 10px; border-radius: 999px; border: 1px solid #aaa; font-size: 12px; }
      .off { background: #f3f4f6; color: #6b7280; }
      .on { background: #e0f2fe; color: #0369a1; }
      .ok { background: #dcfce7; color: #166534; }
      .err { background: #fee2e2; color: #991b1b; }
      a.download { display: inline-block; margin-top: 10px; margin-right: 12px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>SOC 2 Control Extractor</h1>
      <p class="muted">Upload a SOC 2 PDF to extract controls and download a DOCX report.</p>

      <div class="row">
        <form id="uploadForm">
          <input type="file" id="pdf" name="pdf" accept="application/pdf" required />
          <button id="submitBtn" type="submit">Run Extraction</button>
        </form>
      </div>

      <div class="row badges">
        <div id="badgeParsed" class="badge off">Parsed</div>
        <div id="badgeExtracting" class="badge off">Extracting…</div>
        <div id="badgeDone" class="badge off">Done</div>
      </div>

      <div class="row" id="statusText"></div>
      <div class="row" id="result"></div>
    </div>

    <script>
      const form = document.getElementById('uploadForm');
      const fileInput = document.getElementById('pdf');
      const resultDiv = document.getElementById('result');
      const btn = document.getElementById('submitBtn');
      const statusText = document.getElementById('statusText');
      const badges = {
        parsed: document.getElementById('badgeParsed'),
        extracting: document.getElementById('badgeExtracting'),
        done: document.getElementById('badgeDone')
      };

      function setBadge(el, state) {
        el.classList.remove('off', 'on', 'ok', 'err');
        el.classList.add(state);
      }

      function resetUI() {
        resultDiv.innerHTML = '';
        statusText.textContent = '';
        Object.values(badges).forEach(b => setBadge(b, 'off'));
      }

      fileInput.addEventListener('change', resetUI);

      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        resetUI();
        btn.disabled = true;

        try {
          const formData = new FormData();
          formData.append('pdf', fileInput.files[0]);

          const parseResp = await fetch('/parse', { method: 'POST', body: formData });
          const parseData = await parseResp.json();
          if (!parseData.ok) {
            setBadge(badges.parsed, 'err');
            resultDiv.innerHTML = '<p style="color:red;">' + (parseData.error || 'Parse failed') + '</p>';
            return;
          }
          setBadge(badges.parsed, 'ok');

          setBadge(badges.extracting, 'on');
          statusText.textContent = 'Extracting controls...';

          const extractResp = await fetch('/extract_cursor/' + parseData.doc_id, { method: 'POST' });
          const extractData = await extractResp.json();
          if (!extractData.ok) {
            setBadge(badges.extracting, 'err');
            resultDiv.innerHTML = '<p style="color:red;">' + (extractData.error || 'Extraction failed') + '</p>';
            return;
          }

          setBadge(badges.extracting, 'off');
          setBadge(badges.done, 'ok');
          statusText.textContent = '';

          const pre = document.createElement('pre');
          pre.textContent = JSON.stringify(extractData.result, null, 2);
          resultDiv.appendChild(pre);

          if (extractData.report_id) {
            const link = document.createElement('a');
            link.href = '/download/docx/' + extractData.report_id;
            link.textContent = 'Download DOCX Report';
            link.className = 'download';
            resultDiv.appendChild(link);
          }
        } catch (err) {
          setBadge(badges.extracting, 'err');
          resultDiv.innerHTML = '<p style="color:red;">' + (err?.message || err) + '</p>';
        } finally {
          btn.disabled = false;
        }
      });
    </script>
  </body>
</html>
'''


@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


# -----------------------------
# Routes
# -----------------------------

from prompts import (
    SYSTEM_CONTROLS, INSTRUCTION_CONTROLS,
    SYSTEM_CRITERIA, INSTRUCTION_CRITERIA,
    SYSTEM_SUBSERVICE, INSTRUCTION_SUBSERVICE,
    SYSTEM_USER_ENTITY, INSTRUCTION_USER_ENTITY,
)


@app.post("/parse")
def parse_pdf():
    """Parse PDF and segment into table/narrative content."""
    PARSED_CACHE.clear()

    if "pdf" not in request.files:
        return jsonify({"ok": False, "error": "No PDF uploaded"}), 400

    pdf_file = request.files["pdf"]
    raw = pdf_file.read()
    if not raw:
        return jsonify({"ok": False, "error": "Could not read PDF"}), 400

    try:
        full_text, pages = extract_pdf_text(raw)
        segments = segment_content(pages)
    except Exception as e:
        logger.exception("PDF parse error")
        return jsonify({"ok": False, "error": f"PDF parse error: {e}"}), 500

    doc_id = str(uuid.uuid4())[:8]
    PARSED_CACHE[doc_id] = {
        "full_text": full_text,
        "table_text": segments["table_text"],
        "narrative_text": segments["narrative_text"],
    }

    # Log stats
    table_pages = segments["table_text"].count("=== PAGE")
    narrative_pages = segments["narrative_text"].count("=== PAGE")
    logger.info(
        "[Parse] doc_id=%s, total_pages=%d, table_pages=%d, narrative_pages=%d, full_chars=%d, table_chars=%d",
        doc_id, len(pages), table_pages, narrative_pages, len(full_text), len(segments["table_text"])
    )

    return jsonify({
        "ok": True,
        "doc_id": doc_id,
        "total_pages": len(pages),
        "table_pages": table_pages,
        "narrative_pages": narrative_pages,
        "full_chars": len(full_text),
        "table_chars": len(segments["table_text"]),
    })


@app.post("/extract_cursor/<doc_id>")
def extract_controls_cursor(doc_id: str):
    """
    Multi-phase extraction:
    1. Extract vendor/service organization controls (batched, cursor-based)
    2. Extract subservice organization controls (single call)
    3. Extract user entity controls (single call)
    4. Extract criteria mappings (single call)
    5. Merge criteria into vendor controls
    6. Generate DOCX with all control types
    """
    if doc_id not in PARSED_CACHE:
        return jsonify({"ok": False, "error": "Unknown doc_id"}), 404

    cache = PARSED_CACHE[doc_id]

    # Use table_text if available, fallback to full_text
    doc_text = cache["table_text"] if cache["table_text"].strip() else cache["full_text"]
    logger.info("[Extract] Using %s (%d chars)", 
                "table_text" if cache["table_text"].strip() else "full_text (fallback)", 
                len(doc_text))

    t0 = time.time()

    # ===========================================
    # Phase 1: Extract vendor controls (batched)
    # ===========================================
    system_prompt_controls = SYSTEM_CONTROLS.replace("{{DOCUMENT_TEXT}}", doc_text)
    batch_size = app.config["BATCH_SIZE"]

    merged_controls: List[Dict[str, Any]] = []
    cursor = ""
    passes = 0

    while True:
        instruction = INSTRUCTION_CONTROLS.format(batch_size=batch_size, cursor=cursor)
        try:
            result = invoke_bedrock_with_retry(system_prompt_controls, instruction)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Bedrock error (controls): {e}"}), 500

        controls = (result.get("extraction", {}) or {}).get("controls", [])
        merged_controls = merge_controls(merged_controls, controls)

        meta = result.get("meta", {}) or {}
        cursor = meta.get("last_control_id") or meta.get("last_index") or ""
        has_more = bool(meta.get("has_more"))

        passes += 1
        logger.info("[Extract Vendor Controls] Pass %d: got %d controls, has_more=%s", passes, len(controls), has_more)

        if not has_more:
            break
        time.sleep(0.2)

    logger.info("[Extract Vendor Controls] Complete: %d total controls in %d passes", len(merged_controls), passes)

    # ===========================================
    # Phase 2: Extract subservice organization controls (single call)
    # ===========================================
    logger.info("[Extract Subservice] Starting subservice controls extraction...")
    system_prompt_subservice = SYSTEM_SUBSERVICE.replace("{{DOCUMENT_TEXT}}", doc_text)
    
    try:
        subservice_result = invoke_bedrock_with_retry(system_prompt_subservice, INSTRUCTION_SUBSERVICE)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Bedrock error (subservice): {e}"}), 500

    subservice_controls = subservice_result.get("subservice_controls", [])
    logger.info("[Extract Subservice] Found %d subservice organizations", len(subservice_controls))

    # ===========================================
    # Phase 3: Extract user entity controls (single call)
    # ===========================================
    logger.info("[Extract User Entity] Starting user entity controls extraction...")
    system_prompt_user_entity = SYSTEM_USER_ENTITY.replace("{{DOCUMENT_TEXT}}", doc_text)
    
    try:
        user_entity_result = invoke_bedrock_with_retry(system_prompt_user_entity, INSTRUCTION_USER_ENTITY)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Bedrock error (user entity): {e}"}), 500

    user_entity_controls = user_entity_result.get("user_entity_controls", [])
    logger.info("[Extract User Entity] Found %d user entity controls", len(user_entity_controls))

    # ===========================================
    # Phase 4: Extract criteria mappings (single call)
    # ===========================================
    logger.info("[Extract Criteria] Starting criteria extraction...")
    system_prompt_criteria = SYSTEM_CRITERIA.replace("{{DOCUMENT_TEXT}}", doc_text)
    
    try:
        criteria_result = invoke_bedrock_with_retry(system_prompt_criteria, INSTRUCTION_CRITERIA)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Bedrock error (criteria): {e}"}), 500

    criteria_mappings = criteria_result.get("criteria_mappings", [])
    logger.info("[Extract Criteria] Found %d criteria", len(criteria_mappings))

    # ===========================================
    # Phase 5: Merge criteria into vendor controls
    # ===========================================
    final_controls = merge_criteria_into_controls(merged_controls, criteria_mappings)
    logger.info("[Merge] Merged criteria into %d controls", len(final_controls))

    # ===========================================
    # Phase 6: Build DOCX with all control types
    # ===========================================
    rid = str(uuid.uuid4())[:8]
    docx_path = _report_path_docx(rid)
    try:
        build_controls_docx(final_controls, subservice_controls, user_entity_controls, docx_path)
    except Exception as e:
        logger.exception("Failed to build DOCX: %s", e)

    docx_ok = os.path.exists(docx_path)
    elapsed = round(time.time() - t0, 2)

    return jsonify({
        "ok": True,
        "result": {
            "extraction": {
                "vendor_controls": final_controls,
                "subservice_controls": subservice_controls,
                "user_entity_controls": user_entity_controls,
            },
            "criteria_mappings": criteria_mappings,
            "meta": {
                "vendor_controls_found": len(final_controls),
                "subservice_controls_found": len(subservice_controls),
                "user_entity_controls_found": len(user_entity_controls),
                "criteria_found": len(criteria_mappings),
                "passes": passes,
            }
        },
        "report_id": rid if docx_ok else None,
        "elapsed_sec": elapsed,
        "passes": passes,
    })


@app.get("/download/docx/<rid>")
def download_docx(rid: str):
    docx_path = _report_path_docx(rid)
    if not os.path.exists(docx_path):
        return jsonify({"ok": False, "error": "DOCX not found"}), 404
    return send_file(docx_path, as_attachment=True, download_name=f"soc2_report_{rid}.docx")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)