# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Email:  steve.keech@hisaus.org
#

"""Populate AVP entities table with person records based on people.csv and usergroups.

This script processes person information from people.csv and user group assignments
from a usergroup file (or an export like results (11).csv) and builds Amazon
Verified Permissions (AVP) person entities adhering to the Cedar schema:

    "person": {
        "memberOfTypes": [
            "group"
        ],
        "shape": {
            "type": "Record",
            "attributes": {
                "is_covered_person": {
                    "required": true,
                    "type": "Boolean"
                },
                "is_barred": {
                    "required": true,
                    "type": "Boolean"
                },
                "is_registered": {
                    "required": true,
                    "type": "Boolean"
                },
                "linked_integrators": {
                    "required": true,
                    "type": "Set",
                    "element": {
                        "type": "Entity",
                        "name": "integrator"
                    }
                }
            }
        }
    }

Entities are written to:
1. The local avp-entities.csv file (preserving existing non-person entities like horses/integrators).
2. The DynamoDB AVP entities table (avp-entities-and-resources-dev).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from pathlib import Path
from typing import Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

# Default configuration matching existing hisa-aws-utilities scripts
REGION = "us-east-1"
PROFILE = "ResourceCreator"
TABLE_NAME = "avp-entities-and-resources-dev"
NAMESPACE = "hisa"

DEFAULT_PEOPLE_CSV = Path(__file__).with_name("people.csv")
DEFAULT_USERGROUPS_CSV = Path(__file__).with_name("user_groups.csv")
DEFAULT_RESULTS_CSV = Path(__file__).with_name("results (11).csv")
DEFAULT_AVP_CSV = Path(__file__).with_name("avp-entities.csv")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("populate_avp_person_entities")


def to_bool(value: Any, default: bool = False) -> bool:
    """Convert an arbitrary value to a boolean safely."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "t"):
        return True
    if text in ("false", "0", "no", "n", "f", ""):
        return False
    return default


def parse_raw_auth_group_value(raw: Any) -> list[str]:
    """Extract a list of group ID strings from raw group field values.

    Handles:
    - String literals or comma-separated strings ('CoveredPerson, ResponsiblePerson')
    - Plain JSON lists (['CoveredPerson', 'vet'])
    - DynamoDB JSON lists ([{"M": {"AuthGroupId": {"S": "CoveredPerson"}}}])
    - Dictionaries with 'AuthGroupId' keys
    """
    if not raw:
        return []
    if isinstance(raw, list):
        groups: list[str] = []
        for item in raw:
            groups.extend(parse_raw_auth_group_value(item))
        return groups
    if isinstance(raw, dict):
        if "M" in raw and "AuthGroupId" in raw["M"]:
            gid = raw["M"]["AuthGroupId"].get("S")
            return [gid] if gid else []
        if "AuthGroupId" in raw:
            val = raw["AuthGroupId"]
            if isinstance(val, dict) and "S" in val:
                return [val["S"]]
            return [str(val)]
        return []
    if isinstance(raw, str):
        raw_str = raw.strip()
        if not raw_str:
            return []
        if (raw_str.startswith("[") and raw_str.endswith("]")) or (
            raw_str.startswith("{") and raw_str.endswith("}")
        ):
            try:
                parsed = json.loads(raw_str)
                return parse_raw_auth_group_value(parsed)
            except json.JSONDecodeError:
                pass
        # Fall back to comma-separated split
        return [g.strip() for g in raw_str.split(",") if g.strip()]
    return [str(raw)]


def load_user_groups(
    file_path: Path, object_id_filter: str | None = None
) -> dict[str, list[str]]:
    """Load person ID to group name assignments from a CSV file.

    Supports:
    - results (11).csv format: PersonId, ObjectId, AuthGroupIds, ...
    - Simple CSV format: PersonId, AuthGroupId
    - Any CSV with person/group identifying column names.
    """
    if not file_path.exists():
        logger.warning(f"Usergroup file not found: {file_path}")
        return {}

    user_groups: dict[str, set[str]] = {}

    with file_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            # Locate person identifier column
            person_id = (
                row.get("PersonId")
                or row.get("HisaPersonId")
                or row.get("pk")
                or row.get("entityId")
                or row.get("UserName")
                or row.get("username")
            )
            if not person_id:
                continue
            person_id = person_id.strip()

            # Apply ObjectId filter if requested (e.g. 'General')
            if object_id_filter is not None and "ObjectId" in row:
                row_obj_id = (row.get("ObjectId") or "").strip()
                if row_obj_id and row_obj_id.lower() != object_id_filter.lower():
                    continue

            # Extract groups from various possible column names
            extracted_groups: set[str] = set()

            for col in (
                "AuthGroupIds",
                "AuthGroupId",
                "Groups",
                "Group",
                "GroupName",
                "GroupId",
                "usergroups",
                "usergroup",
                "UserGroup",
            ):
                if col in row and row[col]:
                    for g in parse_raw_auth_group_value(row[col]):
                        if g:
                            extracted_groups.add(g)

            if person_id not in user_groups:
                user_groups[person_id] = set()
            user_groups[person_id].update(extracted_groups)

    logger.info(
        f"Loaded group mappings for {len(user_groups)} person(s) from {file_path.name}"
    )
    return {pid: sorted(groups) for pid, groups in user_groups.items()}


def parse_linked_integrators(raw: Any, namespace: str = NAMESPACE) -> list[dict[str, Any]]:
    """Parse integrator IDs into AVP entity references."""
    if not raw:
        return []

    integrator_ids: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                integrator_ids.append(item.strip())
            elif isinstance(item, dict) and "entityIdentifier" in item:
                return raw
    elif isinstance(raw, str):
        text = raw.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                return parse_linked_integrators(parsed, namespace=namespace)
            except json.JSONDecodeError:
                pass
        integrator_ids = [item.strip() for item in text.split(",") if item.strip()]

    return [
        {
            "entityIdentifier": {
                "entityId": integ_id,
                "entityType": f"{namespace}::integrator",
            }
        }
        for integ_id in integrator_ids
        if integ_id
    ]


def load_people(file_path: Path) -> list[dict[str, Any]]:
    """Load person records from people.csv."""
    if not file_path.exists():
        raise FileNotFoundError(f"People file not found: {file_path}")

    people: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            person_id = (
                row.get("HisaPersonId")
                or row.get("PersonId")
                or row.get("pk")
                or row.get("entityId")
            )
            if not person_id:
                continue
            person_id = person_id.strip()

            people.append(
                {
                    "person_id": person_id,
                    "is_covered_person": row.get("IsCoveredPerson")
                    or row.get("is_covered_person"),
                    "is_barred": row.get("BarredFromRacing")
                    or row.get("is_barred"),
                    "is_unregistered": row.get("IsUnregisteredPerson"),
                    "is_registered": row.get("is_registered"),
                    "linked_integrators": row.get("linked_integrators")
                    or row.get("integrator_ids")
                    or row.get("integrator_id"),
                    "raw_row": row,
                }
            )

    logger.info(f"Loaded {len(people)} person record(s) from {file_path.name}")
    return people


def build_person_entity(
    person: dict[str, Any],
    groups: list[str],
    namespace: str = NAMESPACE,
) -> dict[str, Any]:
    """Construct an AVP person entity dictionary adhering to the AVP schema.

    Schema:
        identifier: { entityId: <id>, entityType: <namespace>::person }
        parents: [ { entityId: <group_id>, entityType: <namespace>::group }, ... ]
        attributes:
            is_covered_person: Boolean
            is_barred: Boolean
            is_registered: Boolean
            linked_integrators: Set<Entity<integrator>>
    """
    person_id = person["person_id"]

    # Determine is_covered_person
    if person.get("is_covered_person") is not None:
        is_covered = to_bool(person["is_covered_person"], default=True)
    elif "CoveredPerson" in groups:
        is_covered = True
    elif "NonCoveredPerson" in groups:
        is_covered = False
    else:
        is_covered = True

    # Determine is_barred
    is_barred = to_bool(person.get("is_barred"), default=False)

    # Determine is_registered
    if person.get("is_registered") is not None:
        is_registered = to_bool(person["is_registered"], default=True)
    elif person.get("is_unregistered") is not None:
        is_registered = not to_bool(person["is_unregistered"], default=False)
    else:
        is_registered = True

    # Parse linked_integrators
    linked_integrators = parse_linked_integrators(
        person.get("linked_integrators"), namespace=namespace
    )

    # Format parents (memberOfTypes: ["group"])
    parents = [
        {"entityId": gid, "entityType": f"{namespace}::group"}
        for gid in sorted(set(groups))
        if gid
    ]

    return {
        "pk": person_id,
        "sk": "#",
        "identifier": {
            "entityId": person_id,
            "entityType": f"{namespace}::person",
        },
        "attributes": {
            "is_covered_person": is_covered,
            "is_barred": is_barred,
            "is_registered": is_registered,
            "linked_integrators": linked_integrators,
        },
        "parents": parents,
    }


def build_group_entity(
    group_id: str, namespace: str = NAMESPACE
) -> dict[str, Any]:
    """Construct an AVP group entity dictionary."""
    return {
        "pk": group_id,
        "sk": "#",
        "identifier": {
            "entityId": group_id,
            "entityType": f"{namespace}::group",
        },
        "attributes": {},
        "parents": [],
    }


def entity_to_csv_row(
    entity: dict[str, Any], serializer: TypeSerializer | None = None
) -> dict[str, str]:
    """Convert a native Python entity item into an avp-entities.csv formatted row.

    Produces DynamoDB JSON strings matching deserialize_map in populate_avp_entities.py.
    """
    if serializer is None:
        serializer = TypeSerializer()

    serialized_attrs = {
        k: serializer.serialize(v) for k, v in entity["attributes"].items()
    }
    serialized_identifier = {
        k: serializer.serialize(v) for k, v in entity["identifier"].items()
    }

    return {
        "pk": entity["pk"],
        "sk": entity["sk"],
        "attributes": json.dumps(serialized_attrs),
        "identifier": json.dumps(serialized_identifier),
        "parents": json.dumps(entity["parents"]),
    }


def update_avp_entities_csv(
    csv_path: Path,
    new_entities: list[dict[str, Any]],
) -> tuple[int, int]:
    """Update or append entities in avp-entities.csv, preserving all non-person entities.

    Returns:
        tuple of (updated_count, appended_count)
    """
    serializer = TypeSerializer()
    existing_rows: list[dict[str, str]] = []
    fieldnames = ["pk", "sk", "attributes", "identifier", "parents"]

    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            existing_rows = list(reader)

    new_entities_by_pk = {e["pk"]: entity_to_csv_row(e, serializer) for e in new_entities}
    updated_pks: set[str] = set()
    final_rows: list[dict[str, str]] = []

    for row in existing_rows:
        pk = row.get("pk")
        if pk in new_entities_by_pk:
            final_rows.append(new_entities_by_pk[pk])
            updated_pks.add(pk)
        else:
            final_rows.append(row)

    appended_count = 0
    for pk, new_row in new_entities_by_pk.items():
        if pk not in updated_pks:
            final_rows.append(new_row)
            appended_count += 1

    updated_count = len(updated_pks)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(final_rows)

    logger.info(
        f"Updated {updated_count} and appended {appended_count} record(s) in {csv_path.name}"
    )
    return updated_count, appended_count


def populate_dynamodb_table(
    table: Any,
    entities: list[dict[str, Any]],
) -> int:
    """Batch write entities directly to the DynamoDB table."""
    written_count = 0
    with table.batch_writer() as batch:
        for entity in entities:
            batch.put_item(Item=entity)
            written_count += 1
    logger.info(f"Successfully wrote {written_count} item(s) to DynamoDB table {table.name}")
    return written_count


def extract_usergroups_from_results(
    results_path: Path, output_path: Path
) -> int:
    """Extract a clean usergroups CSV mapping from a DynamoDB export file like results (11).csv."""
    groups_by_person = load_user_groups(results_path)
    count = 0
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["PersonId", "AuthGroupId"])
        for pid, groups in sorted(groups_by_person.items()):
            for g in groups:
                writer.writerow([pid, g])
                count += 1
    logger.info(f"Extracted {count} usergroup mapping(s) to {output_path.name}")
    return count


def generate_usergroups_template(
    people_path: Path, output_path: Path
) -> int:
    """Generate a starter usergroup file based on people.csv."""
    people = load_people(people_path)
    count = 0
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["PersonId", "AuthGroupId", "Notes"])
        for p in people:
            pid = p["person_id"]
            # Default every person to CoveredPerson
            writer.writerow([pid, "CoveredPerson", "Default Covered Person group"])
            count += 1
    logger.info(f"Generated usergroup template with {count} row(s) to {output_path.name}")
    return count


def main() -> None:
    """CLI entrypoint for populating AVP entities."""
    parser = argparse.ArgumentParser(
        description="Populate AVP entities table with person records from people.csv and usergroups."
    )
    parser.add_argument(
        "--people-csv",
        type=Path,
        default=DEFAULT_PEOPLE_CSV,
        help="Path to people.csv (default: %(default)s)",
    )
    parser.add_argument(
        "--usergroups-csv",
        type=Path,
        default=None,
        help="Path to usergroups file (default: user_groups.csv or results (11).csv)",
    )
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=DEFAULT_RESULTS_CSV,
        help="Path to results (11).csv export (default: %(default)s)",
    )
    parser.add_argument(
        "--avp-csv",
        type=Path,
        default=DEFAULT_AVP_CSV,
        help="Path to avp-entities.csv file to update (default: %(default)s)",
    )
    parser.add_argument(
        "--table-name",
        type=str,
        default=TABLE_NAME,
        help="DynamoDB table name (default: %(default)s)",
    )
    parser.add_argument(
        "--region",
        type=str,
        default=REGION,
        help="AWS region (default: %(default)s)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=PROFILE,
        help="AWS profile (default: %(default)s)",
    )
    parser.add_argument(
        "--namespace",
        type=str,
        default=NAMESPACE,
        help="AVP namespace (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-dynamo",
        "--csv-only",
        action="store_true",
        dest="csv_only",
        help="Only update CSV, do not write to DynamoDB table",
    )
    parser.add_argument(
        "--include-groups",
        action="store_true",
        help="Also create/add hisa::group entity records for assigned groups",
    )
    parser.add_argument(
        "--extract-from-results",
        type=Path,
        metavar="OUTPUT_CSV",
        help="Extract a clean usergroups CSV from results (11).csv and exit",
    )
    parser.add_argument(
        "--generate-usergroups-template",
        type=Path,
        metavar="OUTPUT_CSV",
        help="Generate a starter usergroups template from people.csv and exit",
    )

    args = parser.parse_args()

    # Handle utility actions
    if args.extract_from_results:
        extract_usergroups_from_results(args.results_csv, args.extract_from_results)
        return

    if args.generate_usergroups_template:
        generate_usergroups_template(args.people_csv, args.generate_usergroups_template)
        return

    # Determine usergroups file to load
    ug_path = args.usergroups_csv
    if ug_path is None:
        if DEFAULT_USERGROUPS_CSV.exists():
            ug_path = DEFAULT_USERGROUPS_CSV
        elif args.results_csv.exists():
            ug_path = args.results_csv
        else:
            logger.warning(
                "Neither user_groups.csv nor results (11).csv was found; proceeding without group assignments."
            )

    # 1. Load user group assignments
    groups_by_person: dict[str, list[str]] = {}
    if ug_path and ug_path.exists():
        groups_by_person = load_user_groups(ug_path)
    # If using user_groups.csv but results (11).csv also exists, merge any additional groups from results (11).csv
    if args.results_csv.exists() and ug_path != args.results_csv:
        results_groups = load_user_groups(args.results_csv)
        for pid, grps in results_groups.items():
            if pid in groups_by_person:
                merged = set(groups_by_person[pid]) | set(grps)
                groups_by_person[pid] = sorted(merged)
            else:
                groups_by_person[pid] = grps

    # 2. Load people from people.csv
    people = load_people(args.people_csv)

    # 3. Build AVP entities
    person_entities: list[dict[str, Any]] = []
    referenced_groups: set[str] = set()

    for person in people:
        pid = person["person_id"]
        groups = groups_by_person.get(pid, [])
        referenced_groups.update(groups)
        entity = build_person_entity(person, groups, namespace=args.namespace)
        person_entities.append(entity)

    all_entities = list(person_entities)

    # If --include-groups is set, add group entities
    if args.include_groups:
        for gid in sorted(referenced_groups):
            all_entities.append(build_group_entity(gid, namespace=args.namespace))
        logger.info(f"Added {len(referenced_groups)} group entity/entities to payload")

    # 4. Update local avp-entities.csv
    update_avp_entities_csv(args.avp_csv, all_entities)

    # 5. Populate DynamoDB if not skipped
    if not args.csv_only:
        try:
            session = boto3.Session(profile_name=args.profile, region_name=args.region)
            dynamodb = session.resource("dynamodb")
            table = dynamodb.Table(args.table_name)
            populate_dynamodb_table(table, all_entities)
        except (BotoCoreError, ClientError) as error:
            logger.error(f"Failed to write to DynamoDB table {args.table_name}: {error}")
            logger.info("Local avp-entities.csv was updated successfully.")
    else:
        logger.info("Skipped DynamoDB write (--skip-dynamo / --csv-only enabled)")


if __name__ == "__main__":
    main()
