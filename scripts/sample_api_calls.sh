#!/usr/bin/env bash
# Sample API calls demonstrating all major features
BASE=http://localhost:8000

echo "=== System ==="
curl -s $BASE/health | python3 -m json.tool
curl -s $BASE/metrics

echo "=== Companies ==="
curl -s "$BASE/companies" | python3 -m json.tool
curl -s "$BASE/companies/1" | python3 -m json.tool
curl -s "$BASE/companies/1/versions" | python3 -m json.tool
curl -s "$BASE/companies/1/changelog" | python3 -m json.tool
curl -s "$BASE/companies/1/history" | python3 -m json.tool

echo "=== Point-in-time comparison ==="
curl -s "$BASE/companies/compare?company_ids=1,2&as_of_date=2025-01-01T00:00:00Z" | python3 -m json.tool

echo "=== Snapshots ==="
curl -s "$BASE/snapshots" | python3 -m json.tool
curl -s "$BASE/snapshots/latest" | python3 -m json.tool
curl -s "$BASE/snapshots?currency=CHF" | python3 -m json.tool
curl -s "$BASE/snapshots/1" | python3 -m json.tool
curl -s "$BASE/snapshots/1/provenance" | python3 -m json.tool

echo "=== Uploads ==="
curl -s "$BASE/uploads" | python3 -m json.tool
curl -s "$BASE/uploads/stats" | python3 -m json.tool
curl -s "$BASE/uploads/1/details" | python3 -m json.tool
curl -s "$BASE/uploads/1/schema-audit" | python3 -m json.tool
curl -o /tmp/original.xlsm "$BASE/uploads/1/file"

echo "=== Reprocess ==="
curl -s -X POST "$BASE/uploads/1/reprocess" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Bug fix in extractor v1.1 — recalculate industry weights"}' \
  | python3 -m json.tool

echo "=== Rule Engine ==="
curl -s "$BASE/rules" | python3 -m json.tool

curl -s -X POST "$BASE/rules" \
  -H "Content-Type: application/json" \
  -d '{
    "field_name": "currency",
    "rule_type": "allowed_values",
    "params": ["EUR", "USD", "GBP", "CHF"],
    "severity": "error",
    "description": "Only accepted submission currencies"
  }' | python3 -m json.tool

curl -s -X PATCH "$BASE/rules/1/deactivate" | python3 -m json.tool

echo "=== Catalog ==="
curl -s "$BASE/catalog" | python3 -m json.tool
