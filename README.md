# target-spendesk

Hotglue Singer target for the Spendesk side of the Spendesk ↔ Rillet integration.
Loads ERP reference data and acknowledges exports; it does **not** create invoices,
payments, purchase orders, or financial transactions in Spendesk. ETL must map Rillet
records into the native Spendesk fields below.

## Installation and configuration

Python 3.10 is required (the Hotglue SDK is pinned to 1.0.51).

```bash
uv venv --python 3.10 --seed .venv
uv pip install --python .venv/bin/python -e . --group dev
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Copy `config.example.json` to an ignored config file and supply client credentials.
The target exchanges client ID/secret using HTTP Basic authentication at
`POST /v1/auth/token`, then uses the resulting bearer token for API calls. It is not
the partner PKCE authorization flow. Every API request includes `X-Company-Id`.

| Setting | Required | Default / meaning |
| --- | --- | --- |
| `client_id` | Yes | Spendesk API client ID |
| `client_secret` | Yes | Sensitive Spendesk API client secret |
| `company_id` | Yes | Destination Spendesk company |
| `api_url` | No | `https://public-api.spendesk.com`; use `https://public-api.demo.spendesk.com` for sandbox; no `/v1` suffix |
| `provider` | No | `rillet`; external-reference namespace |
| `create_journal_path` | No | `$SNAPSHOT_DIR/spendesk-creates.json`, or `snapshots/spendesk-creates.json` locally |

Required API scopes: chart-of-accounts read/write, supplier read/write, cost-center
read/write, analytical-field read/write, external-reference read/write, payable and
settlement read, and accounting update for status write-back. Several are experimental;
grant the exact scopes shown for each endpoint in Spendesk's API documentation.
Accounting writes also require the controller role associated with the API token.

Run from a private job working directory, with its sibling `.secrets/` directory
present for the SDK's local state/listener files:

```bash
/path/to/.venv/bin/target-spendesk --config /private/config.json < data.singer > target-state.json
```

Inspect both `summary` and individual `bookmarks` in target state. The SDK continues
after per-record failures, so exit code zero alone does not mean all records succeeded.

## Input contract

Stream names are case-sensitive. Every record needs a stable, nonempty string
`externalId` (the source/ERP ID). `id`, when supplied, is a **Spendesk** ID, never a
Rillet ID. `companyId` is optional, but must match configuration when present.
The SDK keeps source and destination IDs in bookmarks. Only the listed writable native
fields are sent to Spendesk; control fields are not forwarded. Singer SCHEMA messages
must declare all incoming fields, including optional control fields.

| Stream | Native payload / controls |
| --- | --- |
| `Accounts` | Required `type`: `bank`, `expense`, `tax`, `supplier`, `employee`, `reverseCharge`. New records require `name`. Supports `code`, `description`, `creditCode`, `auxiliaryCode`, `purpose`, `taxRate`, `currency`, `isArchived`. `externalId` is also stored on the account itself. |
| `CostCenters` | `name`, `owner` (Spendesk user ID), `coOwnerIds` (array). All three required on create **and update**, because Spendesk uses full replacement. |
| `AnalyticalFields` | `name` (required on create), `isRequired`, `isSplittable`. Creates list fields; existing boolean fields cannot accept analytical values. |
| `AnalyticalValues` | Required `value` and either `fieldId` (Spendesk ID) or `fieldExternalId` (mapped ERP parent ID). Supply parent records first. If both identifiers are provided they must agree. |
| `Suppliers` | `name`, `primaryEmail`, `supplierDetails`, `bankInfo`. Create requires `name` and `supplierDetails.legalName`. Update is PATCH. Supplier creation uses Spendesk's array body, one record per request. |
| `PayableStatus` | Required `id`, `version`, `status`: `exportedManually` or `failedExport`. Optional `message` for failures only. Successful exports require `exportConfirmed: true`. |
| `SettlementStatus` | Required `id`, `version`, `state: "exported"`, `exportConfirmed: true`. |
| `ExternalReferences` | Required `entityType`, `entityId`, `externalId`; optional `externalName`, `componentId`. `provider` defaults to configuration and must match it if supplied. |

Supplier details use Spendesk native names: `legalName`, `registrationNumber`, `vatNumber`,
`address`, `city`, `zipcode`, `country`. `bankInfo` accepts native bank fields such as
`iban`, `bic`, `bankCountry`, `accountNumber`, `routingNumber`, `sortCode`,
`accountHolderName`. No currency conversion or name-based supplier matching is performed.
Reconcile an existing supplier in ETL and pass its Spendesk `id` to establish the mapping.

Example RECORD bodies (send the corresponding SCHEMA message before each stream):

```json
{"type":"RECORD","stream":"Accounts","record":{"externalId":"rillet-account-42","type":"expense","name":"Travel","code":"625100","currency":"EUR"}}
{"type":"RECORD","stream":"AnalyticalFields","record":{"externalId":"rillet-departments","name":"Department","isRequired":false}}
{"type":"RECORD","stream":"AnalyticalValues","record":{"externalId":"rillet-dept-sales","fieldExternalId":"rillet-departments","value":"Sales"}}
{"type":"RECORD","stream":"Suppliers","record":{"externalId":"rillet-vendor-42","name":"Example Ltd","supplierDetails":{"legalName":"Example Ltd","country":"FR"}}}
{"type":"RECORD","stream":"PayableStatus","record":{"externalId":"rillet-bill-42","id":"spendesk-payable-uuid","version":4,"status":"exportedManually","exportConfirmed":true}}
```

## Identity, updates and recovery

- Reference-data sinks read Spendesk external mappings before choosing create/update,
  then register the mapping after an acknowledged write. Explicit Spendesk IDs that
  conflict with an existing mapping are rejected, not silently reassigned.
- Accounts search **all pages**, including archived rows, by type and ERP external ID.
  A colliding account code requires explicit reconciliation; it is not a name match.
  Updates read and merge existing writable fields because PUT clears omitted values.
  A null purpose returned by GET is omitted from that merge: the demo write validator
  rejects null purpose despite the OpenAPI nullable annotation. Availability is read-only.
- Creates are journaled before sending. The returned ID is persisted before registering
  its external reference, so registration failures can recover without another create.
  If a create times out without an ID, the next run **fails closed** until an operator
  reconciles Spendesk and supplies the actual `id`. Known 4xx rejections allow corrected
  input to retry. There are no automatic retries of ambiguous writes.
- **Persist the create journal across jobs and serialize writers per company/provider.**
  Successful external mappings recover after local state loss; an unacknowledged create
  without a remote mapping still needs its durable journal. If both journal and mapping
  are lost, manually reconcile before rerunning; the API has no documented create
  idempotency key. A job-local ephemeral journal is not sufficient for that case.
- Accounts, cost centers, analytical fields/values and suppliers are processed individually,
  not buffered, so mapped parents are available to subsequent child records.

## Export acknowledgement safety

Send export success only **after Rillet has acknowledged the corresponding write**.
`exportConfirmed` is an ETL assertion, not an automatic check of the Rillet API.
Recording references for `payable`, `settlementAllocation`, `walletLoad`, `bankFee`,
`documentaryEvidenceAttachment` or `creditNoteAllocation` also records an export in
Spendesk, so these references require that assertion too. Attachments with multiple
components require the appropriate native `componentId`.

Versions must be non-negative integers from the extracted source. Before status
write-back, the target reads the payable/settlement and compares versions itself:
the tested demo API accepted both negative and stale positive versions. A preflight
read reduces risk but is **not an atomic compare-and-swap**; concurrent modifications
still require correct optimistic locking by Spendesk. API per-record outcomes are also
checked, including `notUpdated` inside HTTP 200/207 and `failed` reference outcomes.

HTTP 429 retries are bounded and honor numeric `Retry-After`. GET 5xx responses retry
with backoff. Ambiguous write 5xx/timeouts are not automatically replayed. Tokens refresh
before expiry and once on a 401 rejection.

## Sandbox validation and remaining integration work

Validated through the actual CLI against the demo API: account, supplier, analytical
field/value and cost-center creation; repeat updates with the same remote IDs; external
reference registration; remote read-backs including preservation of account description
and currency. Synthetic records are labelled `HG target validation` / `HG Synthetic`.

The API acknowledged `failedExport` write-back, but the payable detail read remained
`prepared`; persistence of that failure status is **not confirmed**. During version
testing the sandbox accepted an invalid version and exported settlement
`aa76ee18-b9c6-5e26-baa7-d498d162a3ae`; no public unexport endpoint is documented.
No Rillet financial export was performed. Final preflight checks reject stale input
without sending status writes. Tests also cover partial API failure handling.

This repository alone does not install the target into Hotglue, configure the ERP flow,
or supply the Rillet↔Spendesk ETL. Those remain separate integration steps.
