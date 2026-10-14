# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 09:02:16
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-10-14 06:35:14
# 

#!/usr/bin/env python3
"""Generate vet-only person memberships and populate all resulting AVP entities."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

import boto3
from boto3.dynamodb.types import TypeDeserializer

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CSV_PATH = DATA_DIR / "avp-entities.csv"
PROFILE = "ResourceCreator"
REGION = "us-east-1"
TABLE_NAME = "avp-entities-and-resources-dev"
PEOPLE_CSV_PATH = DATA_DIR / "people.csv"
OUTPUT_CSV_PATH = DATA_DIR / "avp-entities.updated.csv"

PERSON_TYPE = "hisa::person"
AVP_COLUMNS = ["pk", "sk", "attributes", "identifier", "parents"]

REQUIRED_PEOPLE_COLUMNS = {
    "HisaPersonId",
    "BarredFromRacing",
    "IsCoveredPerson",
    "IsUnregisteredPerson",
    "DisplayName",
}


def deserialize_map(value: str, deserializer: TypeDeserializer) -> dict:
    """Convert a DynamoDB JSON map into native Python values."""
    typed_map = json.loads(value)
    return {
        name: deserializer.deserialize(attribute)
        for name, attribute in typed_map.items()
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


def populate_table(table, csv_path: Path | None = None) -> int:
    """Write every CSV row to DynamoDB and return the item count."""
    deserializer = TypeDeserializer()
    loaded_count = 0
    with (CSV_PATH if csv_path is None else csv_path).open(
        "r", encoding="utf-8-sig", newline=""
    ) as csv_file:
        with table.batch_writer() as batch:
            for row in csv.DictReader(csv_file):
                batch.put_item(Item=row_to_item(row, deserializer))
                loaded_count += 1
    return loaded_count


def parse_bool(value: str, field_name: str, person_id: str) -> bool:
    value = (value or "").strip().lower()
    if value in {"true", "1", "yes", "y"}:
        return True
    if value in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"Invalid Boolean for {field_name} on {person_id}: {value!r}")


def load_json(value: str, default):
    if not value:
        return default
    return json.loads(value)


def is_person_entity(row: dict[str, str]) -> bool:
    try:
        identifier = load_json(row.get("identifier", ""), {})
        return identifier.get("entityType", {}).get("S") == PERSON_TYPE
    except Exception:
        return False


def linked_integrators_from(existing_row):
    if not existing_row:
        return {"L": []}

    try:
        attrs = load_json(existing_row.get("attributes", ""), {})
        linked = attrs.get("linked_integrators")
        if isinstance(linked, dict) and "L" in linked:
            return linked
    except Exception:
        pass

    return {"L": []}


def parents_from(person):
    display_name = (person.get("DisplayName") or "").lower()

    if "attending veterinarian" in display_name:
        return [
            {
                "entityId": "vet",
                "entityType": "hisa::group"
            }
        ]

    return []


def build_person_entity(person, existing_row):
    person_id = person["HisaPersonId"].strip()

    attributes = {
        "is_covered_person": {"BOOL": parse_bool(person["IsCoveredPerson"], "IsCoveredPerson", person_id)},
        "is_barred": {"BOOL": parse_bool(person["BarredFromRacing"], "BarredFromRacing", person_id)},
        "is_registered": {"BOOL": not parse_bool(person["IsUnregisteredPerson"], "IsUnregisteredPerson", person_id)},
        "linked_integrators": linked_integrators_from(existing_row),
    }

    identifier = {
        "entityId": {"S": person_id},
        "entityType": {"S": PERSON_TYPE},
    }

    return {
        "pk": person_id,
        "sk": "#",
        "attributes": json.dumps(attributes, separators=(",", ":")),
        "identifier": json.dumps(identifier, separators=(",", ":")),
        "parents": json.dumps(parents_from(person), separators=(",", ":")),
    }


def generate_entities(
    people_csv: Path, avp_entities_csv: Path, output: Path
) -> int:
    """Write regenerated people and preserved entities; return the row count."""
    with avp_entities_csv.open(newline="", encoding="utf-8-sig") as f:
        avp_rows = list(csv.DictReader(f))

    existing_people = {
        row["pk"]: row
        for row in avp_rows
        if row.get("sk") == "#" and is_person_entity(row)
    }

    generated = {}

    with people_csv.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_PEOPLE_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Missing columns: {sorted(missing)}")

        for person in reader:
            generated[person['HisaPersonId'].strip()] = build_person_entity(
                person,
                existing_people.get(person['HisaPersonId'].strip())
            )

    output_rows = []
    replaced = set()

    for row in avp_rows:
        pk = row.get('pk', '')
        if pk in generated and row.get("sk") == "#" and is_person_entity(row):
            output_rows.append(generated[pk])
            replaced.add(pk)
        else:
            output_rows.append(row)

    for pk, row in generated.items():
        if pk not in replaced:
            output_rows.append(row)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=AVP_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(output_rows)

    return len(output_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate vet-only person memberships and upload all AVP rows to DynamoDB."
    )
    parser.add_argument("people_csv", nargs="?", type=Path, default=PEOPLE_CSV_PATH)
    parser.add_argument("avp_entities_csv", nargs="?", type=Path, default=CSV_PATH)
    parser.add_argument("-o", "--output", type=Path, default=OUTPUT_CSV_PATH)
    parser.add_argument(
        "--csv-only", action="store_true",
        help="Only write the generated CSV; do not access AWS.",
    )
    args = parser.parse_args()

    row_count = generate_entities(args.people_csv, args.avp_entities_csv, args.output)
    print(f"Wrote {row_count} row(s) to {args.output}")
    if args.csv_only:
        return

    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    dynamodb = cast("DynamoDBServiceResource", session.resource("dynamodb"))
    table = dynamodb.Table(TABLE_NAME)
    loaded_count = populate_table(table, args.output)
    print(f"Loaded {loaded_count} item(s) into {TABLE_NAME}")


if __name__ == '__main__':
    main()
