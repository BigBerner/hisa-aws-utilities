# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 09:50:37
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-09-29 10:07:23
# 

"""Populate integrator records and their Secrets Manager values from CSV."""

import csv
import json
import secrets
from pathlib import Path

import boto3


REGION = "us-east-1"
PROFILE = "ResourceCreator"
TABLE_NAME = "third-party-integrators-dev"
INTEGRATORS_CSV_PATH = Path(__file__).with_name("integrators.csv")
LEGACY_CSV_PATH = Path(__file__).with_name("integratos.csv")
SECRETS_CSV_PATH = Path(__file__).with_name("integrator_secrets.csv")


def load_integrators() -> list[dict[str, str]]:
    """Load integrator records from the requested CSV filename."""
    csv_path = (
        INTEGRATORS_CSV_PATH
        if INTEGRATORS_CSV_PATH.exists()
        else LEGACY_CSV_PATH
    )
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Neither {INTEGRATORS_CSV_PATH.name} nor {LEGACY_CSV_PATH.name} was found"
        )

    with csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def populate_table(table, integrators: list[dict[str, str]]) -> int:
    """Write integrator records to DynamoDB and return the item count."""
    loaded_count = 0

    with table.batch_writer() as batch:
        for integrator in integrators:
            batch.put_item(
                Item={
                    "pk": integrator["pk"],
                    "sk": integrator["sk"],
                    "value": integrator["value"],
                }
            )
            loaded_count += 1

    return loaded_count


def create_integrator_secrets(
    secretsmanager, integrators: list[dict[str, str]]
) -> int:
    """Create or refresh one Secrets Manager secret for each integrator."""
    records_by_integrator: dict[str, list[dict[str, str]]] = {}
    for record in integrators:
        records_by_integrator.setdefault(record["pk"], []).append(record)

    created_count = 0
    with SECRETS_CSV_PATH.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=("integrator_id", "key"))
        writer.writeheader()

        for integrator_id, records in records_by_integrator.items():
            key_record = next(
                (record for record in records if record["sk"] == "key"),
                None,
            )
            if key_record is None:
                raise ValueError(f"No key record found for integrator {integrator_id}")

            secret_name = key_record["value"]
            secret_value = str(secrets.randbelow(10**32))
            secret_string = json.dumps({"key": secret_value})

            try:
                secretsmanager.create_secret(
                    Name=secret_name,
                    SecretString=secret_string,
                )
            except secretsmanager.exceptions.ResourceExistsException:
                secretsmanager.put_secret_value(
                    SecretId=secret_name,
                    SecretString=secret_string,
                )
            except secretsmanager.exceptions.InvalidRequestException:
                secretsmanager.restore_secret(SecretId=secret_name)
                secretsmanager.put_secret_value(
                    SecretId=secret_name,
                    SecretString=secret_string,
                )

            writer.writerow(
                {"integrator_id": integrator_id, "key": secret_value}
            )
            created_count += 1
            print(f"Created secret {secret_name} for {integrator_id}")

    return created_count


def main() -> None:
    """Load integrator records into DynamoDB."""
    integrators = load_integrators()
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    table = session.resource("dynamodb").Table(TABLE_NAME)
    secretsmanager = session.client("secretsmanager")
    loaded_count = populate_table(table, integrators)
    print(f"Loaded {loaded_count} record(s) into {TABLE_NAME}")
    secret_count = create_integrator_secrets(secretsmanager, integrators)
    print(f"Saved {secret_count} integrator secret(s) to {SECRETS_CSV_PATH.name}")


if __name__ == "__main__":
    main()
