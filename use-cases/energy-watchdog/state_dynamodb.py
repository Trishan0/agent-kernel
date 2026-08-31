"""DynamoDB backend for the site state store (deployed / AWS path).

One item per site, partition key ``site_id`` (String), no sort key. The whole
{"baselines", "dismissals", "cases"} document is stored as a JSON string attribute so the
tool-layer contract is identical to the local file backend - tool.py only calls read_item /
write_item. Selected by AK_STATE__BACKEND=dynamodb; the table name comes from
AK_STATE__DYNAMODB__TABLE_NAME (injected by Terraform).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

_TABLE_ENV = "AK_STATE__DYNAMODB__TABLE_NAME"


@lru_cache(maxsize=1)
def _table():
    import boto3  # imported lazily: only the deployed path needs it

    name = os.environ.get(_TABLE_ENV)
    if not name:
        raise RuntimeError(f"{_TABLE_ENV} is not set; cannot use the DynamoDB state backend")
    return boto3.resource("dynamodb").Table(name)


def read_item(site_id: str) -> dict[str, Any] | None:
    resp = _table().get_item(Key={"site_id": site_id})
    item = resp.get("Item")
    if not item:
        return None
    return json.loads(item["document"])


def write_item(site_id: str, state: dict[str, Any]) -> None:
    _table().put_item(
        Item={
            "site_id": site_id,
            "document": json.dumps(state, sort_keys=True, default=str),
        }
    )
