# Senior Data Engineer Assignment - Corporate Credit Rating Data Pipeline

## Overview
Build a production-ready data pipeline that extracts corporate metadata from the MASTER sheet of Excel files, models it in a dimensional warehouse with temporal tracking, and exposes it through a RESTful API. The entire solution must be containerized using Docker Compose.

## Scenario
You work for a credit rating analytics firm (similar to S&P, Moody's, Fitch). Analysts upload Excel-based rating assessments for corporate entities. Each Excel file contains multiple sheets, but **you only need to extract the MASTER sheet**, which contains:
- Company metadata (entity name, sector, country, currency)
- Rating methodology information
- Industry risk scores and weights
- Accounting principles and business year-end data

The MASTER sheet has a non-standard structure (key-value pairs with "Unnamed" column headers) that requires custom parsing.

Your task is to build a data platform that enables:
- Historical tracking of all rating submissions (requirement #1)
- Point-in-time company comparisons (requirement #2)
- Time-series analysis of individual companies (requirement #3)
- Version control for multiple uploads per company/discussion (requirement #4)
- Data classification for countries, company names, currencies (requirement #5)
- Time-series data availability (requirement #6)
- Data validation (requirement #7)
- BI tool integration (requirement #8)

## Task Breakdown

### 1. Data Extraction & Ingestion
**Challenges:**
- Extract data from .xlsm files (Excel with macros) - **MASTER sheet only**
- Handle non-standard headers (many "Unnamed: X" columns requiring custom parsing)
- MASTER sheet has key-value pair structure (40 rows × 13 columns)
- Column headers are "Unnamed: 0-12", actual labels in column 1, values in column 2
- Preserve file-level metadata (upload timestamp, source filename, version info)

**Requirements:**
- Create extraction module that handles MASTER sheet from each file
- Implement custom parsing for key-value pair structure (not standard table format)
- Extract company metadata:
  - Rated entity name
  - Corporate sector classification
  - Rating methodologies applied
  - Industry risk scores and weights
  - Currency, country, accounting principles
  - Business year-end month
- Generate data quality reports per file (missing fields, invalid values)
- Track data lineage (source file → extracted data → database table)

**Business Context:**
- Files: corporates_A_1.xlsm, corporates_A_2.xlsm, corporates_B_1.xlsm, corporates_B_2.xlsm
- A_1 vs A_2: Same company, different versions (industry risk A → BBB)
- B_1 vs B_2: Same company, different versions (weight changes)
- Only MASTER sheet needs to be extracted per file

### 2. Data Modeling & Warehouse Design
**Challenges:**
- Design dimensional model for corporate metadata tracking
- Implement temporal tracking (point-in-time + time-series)
- Handle version control for multiple uploads per company/discussion
- Model multi-currency data (EUR, CHF)
- Handle slowly changing dimensions for company metadata


### 3. Data Pipeline Orchestration
**Challenges:**
- Implement incremental loading (only process new/changed files)
- Handle pipeline failures gracefully (transaction management)
- Ensure idempotency (re-run same file → no duplicates)
- Track pipeline execution state
- Validate extracted data

**Requirements:**
- Create ETL pipeline with clear stages: Extract → Validate → Transform → Load
- Implement **validation rules** (requirement #7)
- Add retry logic with exponential backoff for transient failures
- Log pipeline execution metrics (files processed, rows extracted, errors, duration)
- Maintain pipeline state (last successful run, processed files list)
- Generate data quality report per run (validation failures, warnings, summary stats)

**Validation Framework:**
- Check required fields are present
- Validate data types (numeric, text, date)
- Validate numeric ranges (weights sum to 1.0, scores in valid range)
- Flag missing or suspicious values
- Report on data quality metrics (completeness, validity rates)

### 4. API Development with FastAPI
**Challenges:**
- Design RESTful endpoints for complex analytical queries
- Support point-in-time queries (requirement #2)
- Support time-series queries (requirement #6)
- Handle version navigation (requirement #4)
- Implement BI-friendly data access (requirement #8)

**Requirements:**
- **Company Endpoints:**
  - GET /companies - List all companies with current metadata
  - GET /companies/{company_id} - Get company details (latest version)
  - GET /companies/{company_id}/versions - Get all versions for a company (requirement #4)
  - GET /companies/{company_id}/history - Get time-series data for analysis (requirement #3)
  - GET /companies/compare - Compare multiple companies at specific point in time (requirement #2)
    - Query params: company_ids, as_of_date

- **Snapshot Endpoints:**
  - GET /snapshots - List all company snapshots with filters
    - Query params: company_id, from_date, to_date, sector, country, currency
  - GET /snapshots/{snapshot_id} - Get specific snapshot details
  - GET /snapshots/latest - Get latest snapshot for each company

- **Upload Audit Endpoints:**
  - GET /uploads - List all file uploads with metadata (requirement #1)
  - GET /uploads/{upload_id}/details - Get specific upload details
  - GET /uploads/{upload_id}/file - Download original file (requirement #1)
  - GET /uploads/stats - Upload statistics and metrics

- **Technical:**
  - Pydantic models for request/response validation
  - OpenAPI/Swagger documentation
  - Proper HTTP status codes and error messages

### 5. Containerization & Infrastructure
**Challenges:**
- Multi-container orchestration with proper dependencies
- Data persistence across container restarts
- Environment configuration management
- Health checks and startup order

**Requirements:**
- **Docker Compose** with services:
  ```yaml
  services:
    postgres:
      - PostgreSQL 15+ for data warehouse
      - Initialize with schema on first run
      - Volume for data persistence
      - Health check endpoint

    api:
      - FastAPI application
      - Depends on postgres health
      - Volume mount for data files
      - Environment variables for config
      - Exposed on port 8000
  ```

- **One-command startup:**
  ```bash
  docker-compose up -d
  ```

## Requirements & Evaluation Criteria

### 1. Data Engineering
- Robust Excel extraction with non-standard header handling
- Efficient data management
- Comprehensive data quality checks and reporting
- Data lineage tracking from source to warehouse
- Proper error handling and logging

### 2. Data Modeling & System Design
- Well-designed dimensional model (star schema)
- Version control strategy for multiple uploads
- Appropriate indexing and partitioning
- Meets all 8 business requirements from Requirements sheet

### 3. Pipeline Orchestration
- Validation framework
- State management (tracking processed files)
- Comprehensive error handling and retry logic
- Data quality reporting
- Monitoring and logging

### 4. API Design & Implementation
- Clean RESTful API design
- Point-in-time and time-series query support
- Complex filtering and aggregation
- BI-friendly data access
- Proper validation and error handling
- Complete OpenAPI documentation

### 5. Infrastructure & Containerization
- Working Docker Compose with all services
- Proper service orchestration and dependencies
- Data persistence configuration
- Health checks implemented
- Environment variable management
- One-command startup

### 6. Code Quality (Qualitative)
- Clean architecture (separation of concerns)
- Type hints throughout
- Unit and integration tests
- Logging and monitoring
- Code documentation

## Deliverables

1. **Source Code Repository**

2. **Docker Compose Setup**

3. **Sample Outputs**
   - At least 10 API call examples with responses
   - Data quality report example
   - Pipeline execution log example

4. **Tests**

7. **AI Usage Disclosure** (REQUIRED)
   - Create AI_USAGE.md documenting:
     - Which AI tools used (ChatGPT, Claude, Copilot, etc.)
     - Which components received AI assistance
     - Chat logs/screenshots (can redact personal info)

## Tech Stack

**Required:**
- Python 3.10+
- FastAPI (web framework)
- PostgreSQL (data warehouse)
- Docker & Docker Compose
- SQLAlchemy (ORM) or raw SQL


## Non-Goals

You do NOT need to:
- Build a frontend UI (focus on API only)
- Implement authentication/authorization
- Deploy to cloud (AWS/Azure/GCP)
- Implement real-time streaming
- Handle production monitoring at scale
- Support Excel file uploads via API (files provided in data/ directory)


## Task Timeline

Complete within **5-7 days**.
