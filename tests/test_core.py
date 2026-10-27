"""Exercise the Singer interface and its observable destination/state contract."""

import copy
import io
import json
import re
from pathlib import Path

import pytest
import requests

from target_spendesk.target import TargetSpendesk

BASE = "https://public-api.demo.spendesk.com"
REFS = BASE + "/v1/experimental/external-references"
CONFIG = {
    "api_url": BASE,
    "company_id": "company-one",
    "client_id": "test-client",
    "client_secret": "test-secret",
}


@pytest.fixture
def run(tmp_path, monkeypatch, requests_mock):
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    # The SDK reads its snapshot location at import time.
    monkeypatch.setattr(
        "hotglue_singer_sdk.target_sdk.target.SNAPSHOT_DIR", str(tmp_path / "snapshots")
    )
    requests_mock.post(BASE + "/v1/auth/token", json={"access_token": "token", "expires_in": 3600})
    requests_mock.get(REFS, json={"data": []})
    versions = {}

    def payable_detail(request, context):
        remote_id = request.path.rsplit("/", 1)[-1]
        return {"id": remote_id, "version": versions[remote_id]}

    def settlement_detail(request, context):
        remote_id = request.qs["ids"][0]
        return {"data": [{"id": remote_id, "version": versions[remote_id]}]}

    requests_mock.get(re.compile(BASE + r"/v1/payables/[^/?]+$"), json=payable_detail)
    requests_mock.get(BASE + "/v1/settlements", json=settlement_detail)

    def execute(records, state=None):
        versions.update(
            {
                r["id"]: r["version"]
                for s, r in records
                if s in ("PayableStatus", "SettlementStatus") and "version" in r
            }
        )
        target = TargetSpendesk(config=copy.deepcopy(CONFIG))
        target.incremental_target_state_path = str(secrets / "incremental.json")
        if state:
            (secrets / "incremental.json").write_text(json.dumps(state))
        else:
            (secrets / "incremental.json").unlink(missing_ok=True)
        lines, schemas = [], set()
        for stream, record in records:
            if stream not in schemas:
                schemas.add(stream)
                lines.append(
                    {
                        "type": "SCHEMA",
                        "stream": stream,
                        "key_properties": ["externalId"],
                        "schema": {
                            "type": "object",
                            "properties": {
                                key: {}
                                for source_stream, source in records
                                if source_stream == stream
                                for key in source
                            },
                        },
                    }
                )
            lines.append({"type": "RECORD", "stream": stream, "record": record})
        try:
            target.listen(io.StringIO("\n".join(json.dumps(line) for line in lines)))
            return copy.deepcopy(target._latest_state)
        finally:
            target._shutdown_requested.set()
            target.signal_listener_thread.join(timeout=1)
            target.api.session.close()

    return execute


def reference_response(request, context):
    return {"data": [{**r, "outcome": "mapped"} for r in request.json()["references"]]}


def bookmark(state, stream, external_id):
    return next(b for b in state["bookmarks"][stream] if b.get("externalId") == external_id)


def test_supplier_create_repeat_without_local_state_and_update(run, requests_mock):
    mappings, suppliers = {}, {}

    def lookup(request, context):
        external_id = request.qs["externalids"][0]
        return {"data": [mappings[external_id]] if external_id in mappings else []}

    def create(request, context):
        payload = request.json()
        assert isinstance(payload, list) and len(payload) == 1
        assert "externalId" not in payload[0]
        assert request.headers["X-Company-Id"] == "company-one"
        remote_id = f"supplier-{len(suppliers) + 1}"
        suppliers[remote_id] = payload[0]
        return {"item": {"id": remote_id, **payload[0]}}

    def update(request, context):
        suppliers["supplier-1"].update(request.json())
        return {"item": {"id": "supplier-1", **suppliers["supplier-1"]}}

    def mapped(request, context):
        mapping = request.json()["references"][0]
        mappings[mapping["externalId"]] = mapping
        return reference_response(request, context)

    requests_mock.get(REFS, json=lookup)
    requests_mock.post(REFS, json=mapped)
    requests_mock.post(BASE + "/v1/suppliers", json=create)
    requests_mock.patch(BASE + "/v1/suppliers/supplier-1", json=update)
    record = {
        "externalId": "erp-vendor-7",
        "name": "Original",
        "supplierDetails": {"legalName": "Legal Ltd"},
    }
    first = run([("Suppliers", record)])
    assert bookmark(first, "Suppliers", "erp-vendor-7")["id"] == "supplier-1"
    assert first["summary"]["Suppliers"]["success"] == 1
    Path("../snapshots/spendesk-creates.json").unlink()
    second = run([("Suppliers", {**record, "name": "Changed"})])
    assert bookmark(second, "Suppliers", "erp-vendor-7")["success"] is True
    assert suppliers == {
        "supplier-1": {"name": "Changed", "supplierDetails": {"legalName": "Legal Ltd"}}
    }
    # Reusing successful state must also retain the original source/destination mapping.
    repeated = run([("Suppliers", {**record, "name": "Changed"})], state=second)
    assert bookmark(repeated, "Suppliers", "erp-vendor-7")["id"] == "supplier-1"
    assert len(suppliers) == 1


def test_ambiguous_create_is_not_replayed_and_explicit_id_recovers(run, requests_mock):
    requests_mock.post(BASE + "/v1/suppliers", exc=requests.ReadTimeout)
    record = {
        "externalId": "ambiguous",
        "name": "Unknown",
        "supplierDetails": {"legalName": "Unknown Ltd"},
    }
    first = run([("Suppliers", record)])
    assert bookmark(first, "Suppliers", "ambiguous")["success"] is False
    second = run([("Suppliers", record)])
    assert "outcome unknown" in bookmark(second, "Suppliers", "ambiguous")["error"]
    requests_mock.patch(BASE + "/v1/suppliers/recovered", json={"item": {"id": "recovered"}})
    requests_mock.post(REFS, json=reference_response)
    recovered = run([("Suppliers", {**record, "id": "recovered"})])
    assert bookmark(recovered, "Suppliers", "ambiguous")["id"] == "recovered"
    assert bookmark(recovered, "Suppliers", "ambiguous")["success"] is True


def test_create_survives_failed_reference_registration(run, requests_mock):
    requests_mock.post(BASE + "/v1/suppliers", json={"item": {"id": "created"}})
    requests_mock.post(
        REFS,
        json={
            "data": [
                {
                    "entityType": "supplier",
                    "entityId": "created",
                    "externalId": "recovery",
                    "outcome": "failed",
                    "reason": "temporary failure",
                }
            ]
        },
    )
    record = {
        "externalId": "recovery",
        "name": "Vendor",
        "supplierDetails": {"legalName": "Vendor Ltd"},
    }
    first = run([("Suppliers", record)])
    assert bookmark(first, "Suppliers", "recovery")["success"] is False
    requests_mock.patch(BASE + "/v1/suppliers/created", json={"item": {"id": "created"}})
    requests_mock.post(REFS, json=reference_response)
    second = run([("Suppliers", record)])
    assert bookmark(second, "Suppliers", "recovery")["id"] == "created"
    assert bookmark(second, "Suppliers", "recovery")["success"] is True


@pytest.mark.parametrize("purpose", [None, "company"])
def test_accounts_find_archived_later_page_and_preserve_omissions(run, requests_mock, purpose):
    existing = {
        "id": "account-older",
        "externalId": "erp-tax",
        "type": "tax",
        "companyId": "company-one",
        "name": "Old",
        "code": "TAX20",
        "taxRate": 20,
        "description": "Keep me",
        "currency": "EUR",
        "isArchived": True,
        "auxiliaryCode": "AUX",
        "purpose": purpose,
        "isAvailable": False,
    }

    def accounts(request, context):
        assert request.qs["includearchived"] == ["true"]
        if request.qs["offset"] == ["0"]:
            return {"data": [{"id": "other", "externalId": "other"}], "totalResults": 2}
        return {"data": [existing], "totalResults": 2}

    def put(request, context):
        body = request.json()["accounts"][0]
        assert body == {
            k: v
            for k, v in {**existing, "name": "New"}.items()
            if k not in ("type", "companyId", "isAvailable") and not (k == "purpose" and v is None)
        }
        return {"items": [{**existing, **body}]}

    requests_mock.get(BASE + "/v1/experimental/accounts", json=accounts)
    requests_mock.get(BASE + "/v1/experimental/accounts/account-older", json=existing)
    requests_mock.put(BASE + "/v1/experimental/accounts", json=put)
    requests_mock.post(REFS, json=reference_response)
    state = run([("Accounts", {"externalId": "erp-tax", "type": "tax", "name": "New"})])
    assert bookmark(state, "Accounts", "erp-tax")["id"] == "account-older"
    assert state["summary"]["Accounts"]["updated"] == 1


@pytest.mark.parametrize(
    "stream,endpoint,result_key,status",
    [
        (
            "PayableStatus",
            "/v1/payables/bookkeeping-status",
            "updatedPayables",
            {"status": "failedExport"},
        ),
        (
            "SettlementStatus",
            "/v1/settlements/state",
            "updatedSettlements",
            {"state": "exported", "exportConfirmed": True},
        ),
    ],
)
def test_partial_success_is_failure_and_next_record_still_runs(
    run,
    requests_mock,
    stream,
    endpoint,
    result_key,
    status,
):
    requests_mock.put(
        BASE + endpoint,
        [
            {
                "status_code": 207,
                "json": {
                    result_key: [{"id": "stale", "outcome": "notUpdated", "reason": "wrongVersion"}]
                },
            },
            {"json": {result_key: [{"id": "good", "outcome": "updated"}]}},
        ],
    )
    state = run(
        [
            (stream, {"externalId": "erp-stale", "id": "stale", "version": 1, **status}),
            (stream, {"externalId": "erp-good", "id": "good", "version": 3, **status}),
        ]
    )
    assert "wrongVersion" in bookmark(state, stream, "erp-stale")["error"]
    assert bookmark(state, stream, "erp-good")["success"] is True
    assert state["summary"][stream]["fail"] == 1
    assert state["summary"][stream]["updated"] == 1


def test_parent_child_success_and_failed_parent_never_guesses_id(run, requests_mock):
    mappings = {}

    def lookup(request, context):
        key = request.qs["externalids"][0]
        return {"data": [mappings[key]] if key in mappings else []}

    def mapped(request, context):
        mapping = request.json()["references"][0]
        mappings[mapping["externalId"]] = mapping
        return reference_response(request, context)

    requests_mock.get(REFS, json=lookup)
    requests_mock.post(REFS, json=mapped)
    requests_mock.post(
        BASE + "/v2/analytical-fields",
        [
            {"json": {"data": {"id": "field-good"}}},
            {"status_code": 400, "json": {"error": "Invalid field"}},
        ],
    )
    requests_mock.post(
        BASE + "/v2/analytical-fields/field-good/values", json={"data": {"id": "value-good"}}
    )
    state = run(
        [
            ("AnalyticalFields", {"externalId": "parent-good", "name": "Departments"}),
            (
                "AnalyticalValues",
                {"externalId": "child-good", "fieldExternalId": "parent-good", "value": "Sales"},
            ),
            ("AnalyticalFields", {"externalId": "parent-bad", "name": "Bad"}),
            (
                "AnalyticalValues",
                {
                    "externalId": "child-bad",
                    "fieldExternalId": "parent-bad",
                    "value": "Never create",
                },
            ),
        ]
    )
    assert bookmark(state, "AnalyticalFields", "parent-good")["id"] == "field-good"
    assert bookmark(state, "AnalyticalValues", "child-good")["id"] == "value-good"
    assert bookmark(state, "AnalyticalValues", "child-bad")["success"] is False


def test_cost_center_empty_update_response_and_external_reference_conflict(run, requests_mock):
    requests_mock.put(BASE + "/v1/cost-centers/cc-1", status_code=204)
    requests_mock.post(REFS, json=reference_response)
    state = run(
        [
            (
                "CostCenters",
                {
                    "externalId": "dept-1",
                    "id": "cc-1",
                    "name": "R&D",
                    "owner": "user-1",
                    "coOwnerIds": [],
                },
            )
        ]
    )
    assert bookmark(state, "CostCenters", "dept-1")["id"] == "cc-1"
    requests_mock.get(
        REFS,
        json={"data": [{"entityType": "supplier", "entityId": "other", "externalId": "vendor-1"}]},
    )
    state = run(
        [
            (
                "ExternalReferences",
                {"externalId": "vendor-1", "entityType": "supplier", "entityId": "wrong"},
            )
        ]
    )
    assert bookmark(state, "ExternalReferences", "vendor-1")["success"] is False


def test_429_retry_and_export_confirmation_company_guards(run, requests_mock, monkeypatch):
    monkeypatch.setattr("target_spendesk.client.time.sleep", lambda _: None)
    requests_mock.put(
        BASE + "/v1/settlements/state",
        [
            {"status_code": 429, "headers": {"Retry-After": "1"}},
            {"json": {"updatedSettlements": [{"id": "s1", "outcome": "updated"}]}},
        ],
    )
    state = run(
        [
            (
                "SettlementStatus",
                {
                    "externalId": "ok",
                    "id": "s1",
                    "version": 1,
                    "state": "exported",
                    "exportConfirmed": True,
                },
            ),
            (
                "SettlementStatus",
                {"externalId": "unsafe", "id": "s2", "version": 1, "state": "exported"},
            ),
            (
                "ExternalReferences",
                {"externalId": "unsafe-ref", "entityId": "p1", "entityType": "payable"},
            ),
            (
                "Suppliers",
                {"externalId": "wrong-company", "companyId": "company-two", "name": "No"},
            ),
        ]
    )
    assert bookmark(state, "SettlementStatus", "ok")["success"] is True
    assert bookmark(state, "SettlementStatus", "unsafe")["success"] is False
    assert bookmark(state, "ExternalReferences", "unsafe-ref")["success"] is False
    assert bookmark(state, "Suppliers", "wrong-company")["success"] is False


def test_writeback_preflight_rejects_stale_and_invalid_versions(run, requests_mock):
    requests_mock.get(BASE + "/v1/payables/p-stale", json={"id": "p-stale", "version": 4})
    state = run(
        [
            (
                "PayableStatus",
                {"externalId": "stale", "id": "p-stale", "version": 3, "status": "failedExport"},
            ),
            (
                "PayableStatus",
                {
                    "externalId": "negative",
                    "id": "p-negative",
                    "version": -1,
                    "status": "failedExport",
                },
            ),
            (
                "PayableStatus",
                {
                    "externalId": "boolean",
                    "id": "p-bool",
                    "version": True,
                    "status": "failedExport",
                },
            ),
        ]
    )
    assert "wrongVersion" in bookmark(state, "PayableStatus", "stale")["error"]
    for external_id in ("negative", "boolean"):
        assert "non-negative integer" in bookmark(state, "PayableStatus", external_id)["error"]


def test_account_natural_key_conflict_does_not_block_a_different_purpose(run, requests_mock):
    requests_mock.get(
        BASE + "/v1/experimental/accounts",
        json={
            "data": [
                {
                    "id": "owned",
                    "externalId": "other-erp",
                    "code": "625",
                    "purpose": "company",
                    "auxiliaryCode": None,
                    "taxRate": None,
                    "isArchived": False,
                }
            ],
            "totalResults": 1,
        },
    )

    def put(request, context):
        body = request.json()["accounts"][0]
        assert body["purpose"] == "spendesk"
        return {"items": [{"id": "new-account", **body}]}

    requests_mock.put(BASE + "/v1/experimental/accounts", json=put)
    requests_mock.post(REFS, json=reference_response)
    state = run(
        [
            (
                "Accounts",
                {
                    "externalId": "conflict",
                    "type": "expense",
                    "code": "625",
                    "name": "Conflict",
                    "purpose": "company",
                },
            ),
            (
                "Accounts",
                {
                    "externalId": "different-purpose",
                    "type": "expense",
                    "code": "625",
                    "name": "Spendesk fees",
                    "purpose": "spendesk",
                },
            ),
        ]
    )
    assert "natural key already exists" in bookmark(state, "Accounts", "conflict")["error"]
    assert bookmark(state, "Accounts", "different-purpose")["id"] == "new-account"
