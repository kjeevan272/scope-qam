"""
OpenLineage integration.

Emits standardised lineage events for every pipeline run and snapshot load.
OpenLineage spec: https://openlineage.io/spec

Event structure:
  - Job: "scope_qam.pipeline" (namespace: tenant slug)
  - Run: UUID per pipeline run (stored in pipeline_runs.openlineage_run_id)
  - Inputs: the source .xlsm file (dataset with schema facet)
  - Outputs: snapshots table (dataset with schema + column-level lineage facets)

Transport: configurable via OPENLINEAGE_URL env var.
  - Set to an Marquez / Atlan / Datahub endpoint for real integration.
  - Defaults to structured log output (always available, zero deps).

Column-level lineage format:
  Each output field lists its input fields with exact source coordinates.
  This satisfies "cell-level provenance" in a standards-compliant way.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

OPENLINEAGE_URL = os.getenv("OPENLINEAGE_URL", "")
NAMESPACE = os.getenv("OPENLINEAGE_NAMESPACE", "scope_qam")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id() -> str:
    return str(uuid.uuid4())


def _file_dataset(file_path: str, file_hash: str) -> dict:
    return {
        "namespace": "file",
        "name": file_path,
        "facets": {
            "dataSource": {
                "_producer": "https://github.com/kjeevan272/scope-qam",
                "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/DatasourceDatasetFacet.json",
                "name": "xlsm-upload",
                "uri": f"file://{file_path}",
            },
            "fileHash": {
                "_producer": "https://github.com/kjeevan272/scope-qam",
                "_schemaURL": "https://openlineage.io/spec/1-0-0/OpenLineage.json",
                "sha256": file_hash,
            },
        },
    }


def _snapshot_dataset(
    tenant_slug: str,
    provenance: list[dict],  # [{field_name, source_sheet, source_row, source_col}]
) -> dict:
    """Output dataset with column-level lineage facet."""
    column_lineage = {}
    for p in provenance:
        field = p.get("field_name", "")
        column_lineage[field] = {
            "inputFields": [
                {
                    "namespace": "file",
                    "name": f"MASTER!{p.get('source_col', '')}{p.get('source_row', '')}",
                    "field": p.get("field_name", ""),
                }
            ],
            "transformationType": "DIRECT",
            "transformationDescription": f"Extracted from MASTER sheet row {p.get('source_row')}",
        }

    return {
        "namespace": NAMESPACE,
        "name": f"{tenant_slug}.snapshots",
        "facets": {
            "columnLineage": {
                "_producer": "https://github.com/kjeevan272/scope-qam",
                "_schemaURL": "https://openlineage.io/spec/facets/1-1-0/ColumnLineageDatasetFacet.json",
                "fields": column_lineage,
            },
        },
    }


def emit_start(run_id: str, file_path: str, file_hash: str, tenant_slug: str = "default") -> dict:
    """Emit a START event when a file ingestion begins."""
    event = {
        "eventType": "START",
        "eventTime": _utcnow_iso(),
        "run": {
            "runId": run_id,
            "facets": {
                "parent": {
                    "_producer": "https://github.com/kjeevan272/scope-qam",
                    "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/ParentRunFacet.json",
                    "run": {"runId": run_id},
                    "job": {"namespace": NAMESPACE, "name": "scope_qam.pipeline"},
                }
            },
        },
        "job": {
            "namespace": NAMESPACE,
            "name": "scope_qam.ingest_file",
            "facets": {
                "jobType": {
                    "_producer": "https://github.com/kjeevan272/scope-qam",
                    "_schemaURL": "https://openlineage.io/spec/facets/2-0-2/JobTypeJobFacet.json",
                    "processingType": "BATCH",
                    "integration": "PYTHON",
                    "jobType": "TASK",
                }
            },
        },
        "inputs": [_file_dataset(file_path, file_hash)],
        "outputs": [],
        "producer": "https://github.com/kjeevan272/scope-qam",
        "schemaURL": "https://openlineage.io/spec/1-0-2/OpenLineage.json",
    }
    _dispatch(event)
    return event


def emit_complete(
    run_id: str,
    file_path: str,
    file_hash: str,
    provenance: list[dict],
    quality_score: float,
    tenant_slug: str = "default",
) -> dict:
    """Emit a COMPLETE event after successful ingestion."""
    event = {
        "eventType": "COMPLETE",
        "eventTime": _utcnow_iso(),
        "run": {"runId": run_id, "facets": {}},
        "job": {"namespace": NAMESPACE, "name": "scope_qam.ingest_file"},
        "inputs": [_file_dataset(file_path, file_hash)],
        "outputs": [_snapshot_dataset(tenant_slug, provenance)],
        "facets": {
            "dataQualityMetrics": {
                "_producer": "https://github.com/kjeevan272/scope-qam",
                "qualityScore": quality_score,
            }
        },
        "producer": "https://github.com/kjeevan272/scope-qam",
        "schemaURL": "https://openlineage.io/spec/1-0-2/OpenLineage.json",
    }
    _dispatch(event)
    return event


def emit_fail(run_id: str, file_path: str, error: str) -> dict:
    event = {
        "eventType": "FAIL",
        "eventTime": _utcnow_iso(),
        "run": {
            "runId": run_id,
            "facets": {
                "errorMessage": {
                    "_producer": "https://github.com/kjeevan272/scope-qam",
                    "_schemaURL": "https://openlineage.io/spec/facets/1-0-0/ErrorMessageRunFacet.json",
                    "message": error,
                    "programmingLanguage": "python",
                }
            },
        },
        "job": {"namespace": NAMESPACE, "name": "scope_qam.ingest_file"},
        "inputs": [{"namespace": "file", "name": file_path}],
        "outputs": [],
        "producer": "https://github.com/kjeevan272/scope-qam",
        "schemaURL": "https://openlineage.io/spec/1-0-2/OpenLineage.json",
    }
    _dispatch(event)
    return event


def _dispatch(event: dict):
    """Send event to configured transport."""
    if OPENLINEAGE_URL:
        try:
            httpx.post(
                f"{OPENLINEAGE_URL}/api/v1/lineage",
                json=event,
                timeout=5.0,
                headers={"Content-Type": "application/json"},
            )
        except Exception as exc:
            log.warning("OpenLineage dispatch failed: %s", exc)
    else:
        # Structured log — always available, zero config required
        log.info("OPENLINEAGE_EVENT %s", json.dumps(event, default=str))
