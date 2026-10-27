"""Spendesk Hotglue Singer target."""

from functools import cached_property

from hotglue_singer_sdk import typing as th
from hotglue_singer_sdk.target_sdk.target import TargetHotglue

from target_spendesk.client import SpendeskClient
from target_spendesk.sinks import (
    AccountsSink,
    AnalyticalFieldsSink,
    AnalyticalValuesSink,
    CostCentersSink,
    ExternalReferencesSink,
    PayableStatusSink,
    SettlementStatusSink,
    SuppliersSink,
)


class TargetSpendesk(TargetHotglue):
    name = "target-spendesk"
    MAX_PARALLELISM = 1
    SINK_TYPES = [
        AccountsSink,
        CostCentersSink,
        AnalyticalFieldsSink,
        AnalyticalValuesSink,
        SuppliersSink,
        PayableStatusSink,
        SettlementStatusSink,
        ExternalReferencesSink,
    ]
    config_jsonschema = th.PropertiesList(
        th.Property("api_url", th.StringType, default="https://public-api.spendesk.com"),
        th.Property("company_id", th.StringType, required=True),
        th.Property("client_id", th.StringType, required=True),
        th.Property(
            "client_secret", th.StringType, required=True, description="Sensitive credential"
        ),
        th.Property("provider", th.StringType, default="rillet"),
    ).to_dict()

    @cached_property
    def api(self):
        return SpendeskClient(self.config)


if __name__ == "__main__":
    TargetSpendesk.cli()
