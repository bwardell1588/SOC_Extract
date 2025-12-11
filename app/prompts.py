# ===========================================
# SHARED SYSTEM PROMPT - TABLE CONTENT (gets cached)
# ===========================================

SYSTEM_DOCUMENT_TABLES = """You are an information extraction system specialized in SOC 2 reports.

You will be given specific extraction tasks in the user message. Follow those instructions precisely and return STRICT JSON ONLY with NO CODE FENCES.

Document table content with page markers:

{{DOCUMENT_TEXT}}
"""

# ===========================================
# SHARED SYSTEM PROMPT - GENERAL (for auditor opinion, first 25 pages, not cached)
# ===========================================

SYSTEM_DOCUMENT_GENERAL = """You are an information extraction system specialized in SOC 2 reports.

You will be given specific extraction tasks in the user message. Follow those instructions precisely and return STRICT JSON ONLY with NO CODE FENCES.

Document content with page markers:

{{DOCUMENT_TEXT}}
"""

# ===========================================
# VENDOR/SERVICE ORGANIZATION CONTROLS
# ===========================================

INSTRUCTION_CONTROLS = """
TASK: Extract VENDOR/SERVICE ORGANIZATION CONTROLS from test result tables.

These are controls implemented by the service organization (the company being audited).
Each control typically has: a reference ID, a description of what the organization does, testing procedures performed, and test results.

What IS a vendor/service organization control:
- Has a reference number (e.g., 1.1, 2.3, 6.15 - typically numeric like X.Y or X.YY)
- Describes an action or procedure the service organization performs
- Has associated testing (what auditors did to verify)
- Has a result (e.g., "No exceptions noted")

What is NOT a vendor control (do not extract these):
- Mapping tables that show which criteria apply to which controls (these are separate from control tables)
- Complementary Subservice Organization Controls (controls performed by third-party subservice providers)
- Complementary User Entity Controls (controls that customers/users are expected to implement)
- Narrative descriptions, assertions, or overview text
- Exception tables or management responses to exceptions

CRITERIA EXTRACTION:
- If criteria (like CC1.1, CC6.1, A1.1) are shown in the control table as a column or section header, extract them for each control.
- Criteria IDs start with letters like CC, C, A, or P followed by numbers.
- A control may map to multiple criteria - include all of them.
- If no criteria are visible in the control table for a control, use an empty array [].

You will be called multiple times. Each call should return UP TO the requested number of controls (batch size), starting AFTER the provided cursor.

Output STRICT JSON ONLY with EXACTLY this schema:

{{
  "extraction": {{
    "controls": [
      {{
        "control_id": "string (the reference number/ID)",
        "criterion": ["string", "..."],  // criteria IDs if present in table (e.g., ["CC6.1", "CC6.2"]), else []
        "control_title": "string (short title or summary)",
        "control_description": "string (what the control does)",
        "tests_applied": ["string", "..."],
        "result": "string"
      }}
    ]
  }},
  "meta": {{
    "last_control_id": "string (the ID of the LAST control in this batch)",
    "has_more": true or false
  }}
}}

Rules:
- Respect the cursor: start from the control AFTER the given cursor.
- Return at most the requested batch size of controls.
- When ALL controls have been extracted and none remain, set "has_more": false.
- If no additional controls exist after the cursor, return an empty controls list and set "has_more": false.
- Use empty strings for missing scalar fields and [] for missing arrays.

EXTRACTION REQUEST: Extract up to {batch_size} controls, starting after cursor: "{cursor}".
If the cursor is empty, start at the beginning.
"""

# ===========================================
# EXCEPTIONS
# ===========================================

INSTRUCTION_EXCEPTIONS = """
TASK: Extract EXCEPTIONS from this document.

Exceptions are control test failures or deviations that were identified during the audit.

IMPORTANT: Exception information may be spread across MULTIPLE tables or sections:
- The control table may list the exceptions (control ID, testing performed, exception found)
- A SEPARATE table may contain management's responses to those exceptions
- Look for sections titled "Management's Response", "Management Response to Exceptions", "Corrective Action", etc.
- Match management responses to their corresponding exceptions by control ID or exception reference

Locations to search:
- A dedicated exceptions table
- Sections titled "Exceptions", "Control Exceptions", "Test Exceptions", "Deviations"
- Management's response to exceptions section (may be a separate table)
- "Management's Response" or "Management Response" sections, can sometimes appear at the very end of the document
- Exceptions column within a controls table

For each exception, extract:
- The control objective or control ID that the exception relates to
- Description of the testing that was performed
- Description of the exception or deviation found
- Management's response to the exception (search in both the controls/exception table AND any separate management response table/section)

Output STRICT JSON ONLY with EXACTLY this schema:

{{
  "exceptions": [
    {{
      "control_objective": "string (the control ID, control objective, or criteria the exception maps to)",
      "testing_description": "string (description of the testing performed that found the exception)",
      "exception_description": "string (description of the exception, deviation, or issue found)",
      "management_response": "string (management's response or remediation plan - may be in a separate table)"
    }}
  ]
}}

Rules:
- Extract ALL exceptions found in the document.
- IMPORTANT: Search the ENTIRE document for management responses - they may be in a separate section/table from the exceptions.
- Match management responses to exceptions by control ID, exception number, or context.
- If no exceptions are found, return an empty list.
- Use empty strings for any fields not present.
- DO NOT include controls that passed testing (e.g., "No exceptions noted" means no exception to extract).

Extract all exceptions now.
"""

# ===========================================
# COMPLEMENTARY SUBSERVICE ORGANIZATION CONTROLS
# ===========================================

INSTRUCTION_SUBSERVICE = """
TASK: Extract COMPLEMENTARY SUBSERVICE ORGANIZATION CONTROLS from this document.

These are controls that third-party subservice providers (e.g., AWS, Azure, Google Cloud, data centers) are expected to have in place.
They are typically listed in a table or section describing what the service organization relies on subservice providers to do.
Extract EACH ROW from the table - each row typically has an ID (like 1, 2, 3), a description, and criteria covered.
Group the rows by subservice organization name.

Look for sections or tables with titles like:
- "Complementary Subservice Organization Controls"
- "Subservice Organization Controls"
- "Controls at Subservice Organizations"

You will be called multiple times. Each call should return UP TO the requested number of controls (batch size), starting AFTER the provided cursor.

Output STRICT JSON ONLY with EXACTLY this schema:

{{
  "subservice_controls": [
    {{
      "organization_name": "string (name of the subservice organization, e.g., 'Amazon Web Services', 'AWS', 'Microsoft Azure')",
      "control_id": "string (row ID like 1, 2, 3 or other identifier if present, else empty)",
      "description": "string (description of the control/what the subservice org is expected to do)",
      "criteria_covered": ["string", "..."]
    }}
  ],
  "meta": {{
    "last_control_id": "string (the ID of the LAST control in this batch, format as 'OrgName|ControlID')",
    "has_more": true or false
  }}
}}

Rules:
- Respect the cursor: start from the control AFTER the given cursor.
- Return at most the requested batch size of controls.
- When ALL controls have been extracted and none remain, set "has_more": false.
- If no additional controls exist after the cursor, return an empty controls list and set "has_more": false.
- Extract EVERY row from subservice organization control tables.
- If criteria are listed as ranges, expand them (e.g., "CC6.1-CC6.3" becomes ["CC6.1", "CC6.2", "CC6.3"]).
- If no subservice organization controls are found, return an empty list.
- Use empty strings for missing IDs/descriptions and [] for missing criteria.

EXTRACTION REQUEST: Extract up to {batch_size} subservice controls, starting after cursor: "{cursor}".
If the cursor is empty, start at the beginning.
"""

# ===========================================
# COMPLEMENTARY USER ENTITY CONTROLS
# ===========================================

INSTRUCTION_USER_ENTITY = """
TASK: Extract COMPLEMENTARY USER ENTITY CONTROLS (CUECs) from this document.

These are controls that customers/users of the service are expected to implement on their end.
They are typically listed in a table or section describing what the service organization expects its customers to do, generally found in Section III of the report.
Extract EACH ROW from the table - each row typically has an ID (like 1, 2, 3), a description, and criteria covered.

Look for sections or tables with titles like:
- "Complementary User Entity Controls"
- "CUECS"
- Trust Service Criteria
- "User Entity Controls"
- "User Control Considerations"
- "Customer Responsibilities"

You will be called multiple times. Each call should return UP TO the requested number of controls (batch size), starting AFTER the provided cursor.

Output STRICT JSON ONLY with EXACTLY this schema:

{{
  "user_entity_controls": [
    {{
      "category": "string (category name or 'User Entity Controls' if not categorized)",
      "control_id": "string (row ID like 1, 2, 3 or other identifier if present, else empty)",
      "description": "string (description of what the user/customer is expected to do)",
      "criteria_covered": ["string", "..."]
    }}
  ],
  "meta": {{
    "last_description": "string (the FIRST 100 CHARACTERS of the description of the LAST control in this batch)",
    "has_more": true or false
  }}
}}

Rules:
- Respect the cursor: start from the control AFTER the one whose description starts with the given cursor text.
- Return at most the requested batch size of controls.
- When ALL controls have been extracted and none remain, set "has_more": false.
- If no additional controls exist after the cursor, return an empty controls list and set "has_more": false.
- Extract EVERY row from user entity control tables.
- If criteria are listed as ranges, expand them (e.g., "CC6.1-CC6.3" becomes ["CC6.1", "CC6.2", "CC6.3"]).
- If no user entity controls are found, return an empty list.
- CUECs may be included in a Trust Service Criteria or General Criteria table, with a one to many relationship. This table could list the Criteria first, followed by all associated CUECs. In this case, extract all CUECs and list that Mapped Criteria for each one.
- Use empty strings for missing IDs/descriptions and [] for missing criteria.

EXTRACTION REQUEST: Extract up to {batch_size} user entity controls, starting after the control whose description begins with: "{cursor}".
If the cursor is empty, start at the beginning.
"""

# ===========================================
# CRITERIA MAPPINGS (supplements control-level criteria)
# ===========================================

INSTRUCTION_CRITERIA = """
TASK: Extract criteria-to-control mappings from MAPPING TABLES in this document.

This extracts criteria mappings that may NOT be directly visible in the control test tables.
Look for dedicated mapping tables or sections (often in Section V or similar) that show which controls satisfy each criterion.

Criteria are requirements/principles that controls are designed to satisfy (e.g., CC1.1, CC2.1, CC6.1, A1.1, C1.1, P1.1).
Criteria IDs typically start with letters (CC, C, A, P) followed by numbers.

Control references may be listed as ranges using hyphens. You MUST expand these ranges into individual control IDs.
  - Example: "1.1-1.5" must be expanded to ["1.1", "1.2", "1.3", "1.4", "1.5"]
  - Example: "6.1-6.18" must be expanded to ["6.1", "6.2", "6.3", ... "6.18"]
Control references may also be listed with commas. Include each one separately.
  - Example: "1.3, 2.1, 3.4" becomes ["1.3", "2.1", "3.4"]

Output STRICT JSON ONLY with EXACTLY this schema:

{{
  "criteria_mappings": [
    {{
      "criterion_id": "string (e.g., CC1.1, CC6.2, A1.1)",
      "criterion_description": "string (the requirement text if available, else empty)",
      "mapped_controls": ["string", "..."]
    }}
  ]
}}

Rules:
- Extract criteria mappings from dedicated mapping tables/sections.
- Expand ALL control reference ranges into individual control IDs.
- A control can map to multiple criteria - include the control in each relevant criterion's mapped_controls list.
- If no mapping tables are found, return an empty list.
- Use empty strings for missing descriptions and [] for criteria with no mapped controls found.

Extract all criteria mappings from mapping tables now.
"""

# ===========================================
# AUDITOR'S OPINION (from narrative content)
# ===========================================

INSTRUCTION_AUDITOR_OPINION = """
TASK: Extract the AUDITOR'S OPINION and general report information from this document.

Look for:
- The Independent Service Auditor's Report section (usually near the beginning)
- Report type information (SOC 1 or SOC 2, Type I or Type II)
- The scope/period covered by the report
- Whether the opinion is qualified or unqualified
- The name of the auditing firm/auditor who issued the report

Output STRICT JSON ONLY with EXACTLY this schema:

{{
  "auditor_opinion": {{
    "service_product": "string (name of the service or product that submitted this SOC report)",
    "report_type": "string (one of: 'SOC 1 Type 1', 'SOC 1 Type 2', 'SOC 2 Type 1', 'SOC 2 Type 2')",
    "scope_date": "string (date range in format mm/dd/yyyy - mm/dd/yyyy)",
    "auditors_opinion": "string (the full text of the auditor's opinion section)",
    "auditors_name": "string (name of the auditing firm or auditor, e.g., 'Ernst & Young LLP', 'Deloitte', 'KPMG')",
    "qualified_opinion": true or false
  }}
}}

Rules:
- service_product: Extract the name of the company/service being audited.
- report_type: Determine if this is SOC 1 or SOC 2, and Type 1 (point in time) or Type 2 (period of time).
- scope_date: Convert dates to mm/dd/yyyy format. For Type 2, use "mm/dd/yyyy - mm/dd/yyyy" for the period.
- auditors_opinion: Extract the complete opinion text from the Independent Service Auditor's Report.
- auditors_name: Extract the name of the auditing firm or individual auditor who signed/issued the report. Look for signatures, letterhead, or "Independent Service Auditor's Report" attribution.
- qualified_opinion: Set to true if the opinion is "qualified" (contains reservations or exceptions). Set to false if "unqualified" or "unmodified" (clean opinion).

Extract the auditor's opinion information now.
"""
