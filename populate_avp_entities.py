# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 09:02:16
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-09-29 09:32:11
# 

"""Populate the AVP entities DynamoDB table from a CSV export."""

import csv
import json
from pathlib import Path

import boto3
from boto3.dynamodb.types import TypeDeserializer


REGION = "us-east-1"
PROFILE = "ResourceCreator"
TABLE_NAME = "avp-entities-and-resources-dev"
CSV_PATH = Path(__file__).with_name("avp-entities.csv")


def deserialize_map(value: str, deserializer: TypeDeserializer) -> dict:
    """Convert a DynamoDB JSON map into native Python values."""
    typed_map = json.loads(value)
    return {
        attribute_name: deserializer.deserialize(attribute_value)
        for attribute_name, attribute_value in typed_map.items()
    }


def row_to_item(row: dict[str, str], deserializer: TypeDeserializer) -> dict:
    """Convert one CSV row into a DynamoDB item."""
    return {
        "pk": row["pk"],
        "sk": row["sk"],
        "attributes": deserialize_map(row["attributes"], deserializer),
        "identifier": deserialize_map(row["identifier"], deserializer),
        "parents": json.loads(row["parents"]),
    }


def populate_table(table) -> int:
    """Write all CSV rows to DynamoDB and return the item count."""
    deserializer = TypeDeserializer()
    loaded_count = 0

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        rows = csv.DictReader(csv_file)
        with table.batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=row_to_item(row, deserializer))
                loaded_count += 1

    return loaded_count


def main() -> None:
    """Load avp-entities.csv into the AVP entities table."""
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    table = session.resource("dynamodb").Table(TABLE_NAME)
    loaded_count = populate_table(table)
    print(f"Loaded {loaded_count} item(s) into {TABLE_NAME}")


if __name__ == "__main__":
    main()
