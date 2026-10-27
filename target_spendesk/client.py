"""Company-scoped HTTP transport and native Spendesk record loading."""

from __future__ import annotations

import random
import time
from urllib.parse import quote, urlparse

import requests
from hotglue_singer_sdk.target_sdk.client import HotglueSink


def segment(value):
    return quote(str(value), safe="")


class SpendeskClient:
    def __init__(self, config):
        self.config = config
        self.base_url = config.get("api_url", "https://public-api.spendesk.com").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
            raise ValueError("api_url must be an HTTPS origin without a path")
        self.session = requests.Session()
        self.token = None
        self.expires_at = 0

    def request(self, method, path, **kwargs):
        for attempt in range(5):
            if time.monotonic() >= self.expires_at:
                response = self.session.post(
                    self.base_url + "/v1/auth/token",
                    auth=(self.config["client_id"], self.config["client_secret"]),
                    json={"grant_type": "client_credentials"},
                    timeout=60,
                )
                response.raise_for_status()
                token = response.json()
                self.token = token["access_token"]
                self.expires_at = time.monotonic() + max(1, token["expires_in"] - 30)
            response = self.session.request(
                method,
                self.base_url + path,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "X-Company-Id": self.config["company_id"],
                },
                timeout=60,
                **kwargs,
            )
            if response.status_code == 401 and attempt == 0:
                self.expires_at = 0
                continue
            retry = response.status_code == 429 or (
                method == "GET" and response.status_code in (500, 502, 503, 504)
            )
            if retry and attempt < 4:
                delay = response.headers.get("Retry-After")
                time.sleep(
                    float(delay) if delay and delay.isdigit() else 2**attempt + random.random()
                )
                continue
            response.raise_for_status()
            return response.json() if response.content else {}
        raise RuntimeError("Spendesk retry limit reached")

    def reference(self, entity_type, external_id):
        params = {"entityType": entity_type, "externalIds": external_id}
        if not entity_type.endswith("Account"):
            params["provider"] = self.config.get("provider", "rillet")
        rows = self.request("GET", "/v1/experimental/external-references", params=params)["data"]
        ids = {
            r["entityId"]
            for r in rows
            if r["externalId"] == external_id and r["entityType"] == entity_type
        }
        if len(ids) > 1:
            raise ValueError("Ambiguous Spendesk external reference")
        return next(iter(ids), None)

    def map_reference(self, payload):
        result = self.request(
            "POST", "/v1/experimental/external-references", json={"references": [payload]}
        )
        rows = result.get("data", [])
        if len(rows) != 1 or any(
            rows[0].get(k) != payload[k] for k in ("entityType", "entityId", "externalId")
        ):
            raise ValueError("Spendesk did not acknowledge the requested external reference")
        if rows[0].get("outcome") not in ("mapped", "recorded", "exported"):
            raise ValueError(f"Spendesk reference failed: {rows[0].get('reason', 'unknown')}")
        return payload["entityId"]


class SpendeskRecordSink(HotglueSink):
    """Native reference-data upserts; source IDs stay in SDK state, not API bodies."""

    unified_schema = None
    endpoint = ""
    entity_type = ""
    fields = ()
    response_key = None
    update_method = "PATCH"
    allows_externalid = [
        "Accounts",
        "CostCenters",
        "AnalyticalFields",
        "AnalyticalValues",
        "Suppliers",
        "PayableStatus",
        "SettlementStatus",
        "ExternalReferences",
    ]

    @property
    def base_url(self):
        return self.api.base_url

    @property
    def api(self):
        return self._target.api

    def preprocess_record(self, record, context):
        if not isinstance(record.get("externalId"), str) or not record["externalId"]:
            raise ValueError("Every record requires a nonempty string externalId")
        if record.get("companyId", self.config["company_id"]) != self.config["company_id"]:
            raise ValueError("Record companyId does not match configured company")
        context["body"] = {k: record[k] for k in self.fields if k in record}
        context["path"] = self.endpoint
        context["params"] = None
        return record

    def prepare_reference(self, record, context, entity_type=None):
        entity_type = entity_type or self.entity_type
        remote_id = self.api.reference(entity_type, record["externalId"])
        explicit_id = record.get("id")
        if remote_id and explicit_id and remote_id != explicit_id:
            raise ValueError("Explicit id conflicts with Spendesk external reference")
        remote_id = remote_id or explicit_id
        context.update(
            remote_id=remote_id,
            reference={
                "entityType": entity_type,
                "externalId": record["externalId"],
                "provider": self.config.get("provider", "rillet"),
            },
        )

    def upsert_record(self, record, context):
        remote_id = context["remote_id"]
        result = self.api.request(
            context.get("method", self.update_method if remote_id else "POST"),
            context["path"],
            json=context["body"],
            params=context["params"],
        )
        item = result.get(self.response_key, {}) if self.response_key else result
        if isinstance(item, list):
            if len(item) != 1:
                raise ValueError("Expected exactly one acknowledged Spendesk record")
            item = item[0]
        returned_id = item.get("id") if self.response_key else item.get("id") or remote_id
        if not returned_id or (remote_id and returned_id != remote_id):
            raise ValueError("Missing or conflicting Spendesk response id")
        metadata = {"is_updated": bool(remote_id)}
        try:
            self.api.map_reference({**context["reference"], "entityId": returned_id})
        except Exception as error:
            # The native write is confirmed. Preserve its ID for SDK snapshots even
            # when the separate reference registration needs operator attention.
            metadata["reference_registration_error"] = str(error)
            self.logger.warning(
                "Spendesk record %s saved, but external-reference registration failed: %s",
                returned_id,
                error,
            )
        return returned_id, True, metadata
