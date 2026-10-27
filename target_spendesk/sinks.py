"""Spendesk native reference-data and export acknowledgement streams."""

from target_spendesk.client import SpendeskRecordSink, segment


class AccountsSink(SpendeskRecordSink):
    name = "Accounts"
    endpoint = "/v1/experimental/accounts"
    fields = (
        "name",
        "description",
        "code",
        "creditCode",
        "auxiliaryCode",
        "externalId",
        "purpose",
        "taxRate",
        "currency",
        "isArchived",
    )
    response_key = "items"

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        account_type = record.get("type")
        if account_type not in ("bank", "expense", "tax", "supplier", "employee", "reverseCharge"):
            raise ValueError("Accounts requires a valid Spendesk account type")
        # Account mappings omit archived rows; scan all pages, including archived accounts.
        accounts, offset = [], 0
        while True:
            page = self.api.request(
                "GET",
                self.endpoint,
                params={
                    "type": account_type,
                    "includeArchived": "true",
                    "offset": offset,
                    "fetch": 100,
                },
            )
            accounts.extend(page["data"])
            offset += len(page["data"])
            if offset >= page["totalResults"]:
                break
            if not page["data"]:
                raise ValueError("Spendesk accounts pagination stopped before totalResults")
        matches = [a for a in accounts if a.get("externalId") == record["externalId"]]
        if len(matches) > 1:
            raise ValueError("Ambiguous account externalId")
        matched_id = matches[0]["id"] if matches else None
        if matched_id and record.get("id") and record["id"] != matched_id:
            raise ValueError("Explicit account id conflicts with externalId")
        remote_id = record.get("id") or matched_id
        # Never let an upsert's natural-key match silently adopt another ERP's account.
        natural_key = (
            ("name", "code", "creditCode")
            if account_type == "reverseCharge"
            else ("code", "purpose", "auxiliaryCode", "taxRate")
        )
        for account in accounts:
            if (
                record.get("code")
                and not record.get("isArchived")
                and not account.get("isArchived")
                and all(account.get(k) == record.get(k) for k in natural_key)
            ):
                if account["id"] != remote_id:
                    raise ValueError(
                        "Account natural key already exists; explicitly reconcile its id first"
                    )
        existing = {}
        if remote_id:
            existing = self.api.request("GET", self.endpoint + "/" + segment(remote_id))
            if existing.get("type") != account_type:
                raise ValueError("Account type mismatch")
            if existing.get("companyId") != self.config["company_id"]:
                raise ValueError("Account belongs to another company")
            if existing.get("externalId") not in (None, "", record["externalId"]):
                raise ValueError("Account is already linked to another externalId")
        # PUT replaces omitted optional properties, so merge only writable native fields.
        body = {k: existing[k] for k in self.fields if k in existing}
        # Reads return null purpose, but the live write validator only accepts the enum.
        if body.get("purpose") is None:
            body.pop("purpose", None)
        body.update(context["body"])
        if remote_id:
            body["id"] = remote_id
        if not body.get("name"):
            raise ValueError("New accounts require name")
        context.update(
            body={"accounts": [body]},
            method="PUT",
            params={"type": account_type},
            remote_id=remote_id,
            reference={
                "entityType": account_type + "Account",
                "provider": self.config.get("provider", "rillet"),
                "externalId": record["externalId"],
            },
        )
        return record


class CostCentersSink(SpendeskRecordSink):
    name = "CostCenters"
    endpoint = "/v1/cost-centers"
    entity_type = "costCenter"
    fields = ("name", "owner", "coOwnerIds")
    update_method = "PUT"

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        self.prepare_reference(record, context)
        if context["remote_id"]:
            context["path"] += "/" + segment(context["remote_id"])
        if any(k not in context["body"] for k in self.fields):
            raise ValueError("CostCenters requires name, owner and coOwnerIds (full replacement)")
        return record


class AnalyticalFieldsSink(SpendeskRecordSink):
    name = "AnalyticalFields"
    endpoint = "/v2/analytical-fields"
    entity_type = "analyticalField"
    fields = ("name", "isRequired", "isSplittable")
    response_key = "data"

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        self.prepare_reference(record, context)
        if context["remote_id"]:
            context["path"] += "/" + segment(context["remote_id"])
        elif not context["body"].get("name"):
            raise ValueError("New analytical fields require name")
        return record


class AnalyticalValuesSink(SpendeskRecordSink):
    name = "AnalyticalValues"
    entity_type = "analyticalFieldValue"
    fields = ("value",)
    response_key = "data"

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        field_id = record.get("fieldId")
        if record.get("fieldExternalId"):
            mapped = self.api.reference("analyticalField", record["fieldExternalId"])
            if not mapped or (field_id and mapped != field_id):
                raise ValueError("Analytical parent missing, failed or conflicts with fieldId")
            field_id = mapped
        if not field_id:
            raise ValueError("AnalyticalValues requires fieldId or a mapped fieldExternalId")
        context["path"] = f"/v2/analytical-fields/{segment(field_id)}/values"
        self.prepare_reference(record, context)
        if context["remote_id"]:
            # The nested endpoint also validates that the value belongs to this parent.
            context["path"] += "/" + segment(context["remote_id"])
        if not context["body"].get("value"):
            raise ValueError("AnalyticalValues requires value")
        return record


class SuppliersSink(SpendeskRecordSink):
    name = "Suppliers"
    endpoint = "/v1/suppliers"
    entity_type = "supplier"
    fields = ("name", "primaryEmail", "supplierDetails", "bankInfo")
    response_key = "item"

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        self.prepare_reference(record, context)
        if context["remote_id"]:
            context["path"] += "/" + segment(context["remote_id"])
        else:
            body = context["body"]
            if not body.get("name") or not body.get("supplierDetails", {}).get("legalName"):
                raise ValueError("New suppliers require name and supplierDetails.legalName")
            context["body"] = [body]
        return record


class PayableStatusSink(SpendeskRecordSink):
    name = "PayableStatus"
    endpoint = "/v1/payables/bookkeeping-status"
    fields = ("id", "version", "status", "message")
    collection = "payables"
    result_key = "updatedPayables"

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        if not record.get("id") or not isinstance(record.get("version"), (int, float)):
            raise ValueError("Export write-back requires Spendesk id and source version")
        version = record["version"]
        if isinstance(version, bool) or version < 0 or int(version) != version:
            raise ValueError("Version must be a non-negative integer from the source record")
        if self.name == "PayableStatus":
            if record.get("status") not in ("exportedManually", "failedExport"):
                raise ValueError("Payable status must be exportedManually or failedExport")
            if record["status"] == "exportedManually" and "message" in record:
                raise ValueError("message is only supported for failedExport")
        elif record.get("state") != "exported":
            raise ValueError("Settlement state must be exported")
        if record.get("status") != "failedExport" and record.get("exportConfirmed") is not True:
            raise ValueError("Successful export write-back requires exportConfirmed=true")
        # The demo API accepts stale versions; enforce a fresh read before sending a write.
        if self.name == "PayableStatus":
            current = self.api.request("GET", "/v1/payables/" + segment(record["id"]))
        else:
            rows = self.api.request("GET", "/v1/settlements", params={"ids": record["id"]})["data"]
            matching = [row for row in rows if row.get("id") == record["id"]]
            if len(matching) != 1:
                raise ValueError("Settlement not found or ambiguous")
            current = matching[0]
        if current.get("id") != record["id"] or current.get("version") != version:
            raise ValueError("Spendesk write-back failed: wrongVersion (refresh the source record)")
        if current.get("companyId", self.config["company_id"]) != self.config["company_id"]:
            raise ValueError("Export entity belongs to another company")
        context["body"] = {self.collection: [context["body"]]}
        return record

    def upsert_record(self, record, context):
        result = self.api.request("PUT", self.endpoint, json=context["body"])
        rows = result.get(self.result_key, [])
        if len(rows) != 1 or rows[0].get("id") != record["id"]:
            raise ValueError("Spendesk did not acknowledge this export write-back")
        if rows[0].get("outcome") != "updated":
            raise ValueError(f"Spendesk write-back failed: {rows[0].get('reason', 'unknown')}")
        return record["id"], True, {"is_updated": True}


class SettlementStatusSink(PayableStatusSink):
    name = "SettlementStatus"
    endpoint = "/v1/settlements/state"
    fields = ("id", "version", "state")
    collection = "settlements"
    result_key = "updatedSettlements"


class ExternalReferencesSink(SpendeskRecordSink):
    name = "ExternalReferences"
    endpoint = "/v1/experimental/external-references"
    fields = ("entityType", "entityId", "provider", "externalId", "externalName", "componentId")

    def preprocess_record(self, record, context):
        record = super().preprocess_record(record, context)
        body = context["body"]
        if not body.get("entityType") or not body.get("entityId"):
            raise ValueError("ExternalReferences requires entityType and entityId")
        body.setdefault("provider", self.config.get("provider", "rillet"))
        if body["provider"] != self.config.get("provider", "rillet"):
            raise ValueError("Reference provider must match configured provider")
        exportable = {
            "payable",
            "settlementAllocation",
            "walletLoad",
            "bankFee",
            "documentaryEvidenceAttachment",
            "creditNoteAllocation",
        }
        if body["entityType"] in exportable and record.get("exportConfirmed") is not True:
            raise ValueError(
                "Exportable references also mark exported; require exportConfirmed=true"
            )
        existing = self.api.reference(body["entityType"], record["externalId"])
        if existing and existing != body["entityId"]:
            raise ValueError("External reference already maps to another Spendesk entity")
        return record

    def upsert_record(self, record, context):
        return self.api.map_reference(context["body"]), True, {}
