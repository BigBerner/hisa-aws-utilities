# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 08:47:29
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-10-14 06:12:59
# 

"""Delete tracked transactions and the existing CSV-defined test setup."""

import csv
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, cast

import boto3
from botocore.exceptions import ClientError

from src.transaction_tracking import load_transaction_ids, transaction_id_pattern

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import DynamoDBServiceResource


REGION = "us-east-1"
PROFILE = "ResourceCreator"
TABLE_NAMES = (
    "enterprise-transaction-queue-dev",
    "avp-entities-and-resources-dev",
)
BUCKET_NAMES = ("transaction-payloads-dev", "transaction-responses-dev")
USER_POOL_ID = "us-east-1_MxDxfUblP"
DATA_DIR = Path(__file__).resolve().parent / "data"
PEOPLE_CSV_PATH = DATA_DIR / "people.csv"
USER_POOL_USERNAME_FIELDS = ("user_ir", "UserName")
USER_TABLE_NAME = "CoveredPerson-dev1"
INTEGRATORS_TABLE_NAME = "third-party-integrators-dev"
INTEGRATORS_CSV_PATH = DATA_DIR / "integrators.csv"
INTEGRATOR_SECRETS_CSV_PATH = DATA_DIR / "integrator_secrets.csv"
CREDENTIALS_PATH = DATA_DIR / "userid_and_password.csv"


def delete_transaction_items(table, pattern: re.Pattern[str]) -> int:
    """Delete only records with a tracked, complete transaction ID in a key."""
    deleted_count = 0
    key_names = [key["AttributeName"] for key in table.key_schema]
    attribute_names = {f"#k{i}": name for i, name in enumerate(key_names)}
    scan_kwargs = {
        "ProjectionExpression": ", ".join(attribute_names),
        "ExpressionAttributeNames": attribute_names,
        "ConsistentRead": True,
    }

    while True:
        response = table.scan(**scan_kwargs)
        items = response.get("Items", [])

        with table.batch_writer() as batch:
            for item in items:
                if any(
                    isinstance(item[key], str) and pattern.search(item[key])
                    for key in key_names
                ):
                    batch.delete_item(Key={key: item[key] for key in key_names})
                    deleted_count += 1

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

    return deleted_count


def delete_transaction_objects(s3, bucket_name: str, pattern: re.Pattern[str]) -> int:
    """Delete matching current S3 objects, checking partial batch failures."""
    deleted_count = 0
    paginator = s3.get_paginator("list_objects_v2")
    try:
        for page in paginator.paginate(Bucket=bucket_name):
            objects = [
                {"Key": obj["Key"]}
                for obj in page.get("Contents", [])
                if pattern.search(obj["Key"])
            ]
            if not objects:
                continue
            response = s3.delete_objects(
                Bucket=bucket_name, Delete={"Objects": objects}
            )
            if response.get("Errors"):
                raise RuntimeError(
                    f"S3 deletion failed in {bucket_name}: {response['Errors']}"
                )
            deleted_count += len(response.get("Deleted", []))
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchBucket":
            raise
        print(f"Bucket {bucket_name} does not exist; no objects deleted there")
    return deleted_count


def load_people() -> list[dict[str, str]]:
    """Load person IDs and Cognito usernames from people.csv."""
    with PEOPLE_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def delete_people_records(table, people: list[dict[str, str]]) -> int:
    """Delete matching CoveredPerson records and return the deletion count."""
    deleted_count = 0

    with table.batch_writer() as batch:
        for person in people:
            person_id = person["HisaPersonId"]
            response = table.get_item(Key={"HisaPersonId": person_id})
            if "Item" not in response:
                print(f"CoveredPerson record {person_id} was already absent")
                continue

            batch.delete_item(Key={"HisaPersonId": person_id})
            deleted_count += 1
            print(f"Deleted CoveredPerson record {person_id}")

    return deleted_count


def load_integrators() -> list[dict[str, str]]:
    """Load integrator table keys from integrators.csv."""
    with INTEGRATORS_CSV_PATH.open(
        "r", encoding="utf-8-sig", newline=""
    ) as csv_file:
        return list(csv.DictReader(csv_file))


def delete_integrator_records(table, integrators: list[dict[str, str]]) -> int:
    """Delete the integrator records listed in the CSV file."""
    deleted_count = 0

    with table.batch_writer() as batch:
        for integrator in integrators:
            batch.delete_item(Key={"pk": integrator["pk"], "sk": integrator["sk"]})
            deleted_count += 1

    return deleted_count


def delete_integrator_secrets(secretsmanager, integrators: list[dict[str, str]]) -> int:
    """Delete Secrets Manager secrets named by integrator key records."""
    deleted_count = 0

    for integrator in integrators:
        if integrator["sk"] != "key":
            continue

        secret_name = integrator["value"]
        try:
            secretsmanager.delete_secret(
                SecretId=secret_name,
                ForceDeleteWithoutRecovery=True,
            )
            deleted_count += 1
            print(f"Deleted secret {secret_name}")
        except secretsmanager.exceptions.ResourceNotFoundException:
            print(f"Secret {secret_name} was already absent")

    return deleted_count


def main() -> None:
    """Clean tracked transactions without purging shared tables/buckets/SQS."""
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    transaction_ids = load_transaction_ids()
    pattern = transaction_id_pattern(transaction_ids) if transaction_ids else None
    people = load_people()
    integrators = load_integrators()
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    dynamodb = cast("DynamoDBServiceResource", session.resource("dynamodb"))
    s3 = session.client("s3")
    cognito = session.client("cognito-idp")
    secretsmanager = session.client("secretsmanager")
    covered_person_table = dynamodb.Table(USER_TABLE_NAME)
    integrators_table = dynamodb.Table(INTEGRATORS_TABLE_NAME)

    if pattern is None:
        print("No saved transaction IDs; skipping transaction table/bucket cleanup")
    else:
        for table_name in TABLE_NAMES:
            table = dynamodb.Table(table_name)
            deleted_count = delete_transaction_items(table, pattern)
            print(f"Deleted {deleted_count} transaction item(s) from {table_name}")

        for bucket_name in BUCKET_NAMES:
            deleted_count = delete_transaction_objects(s3, bucket_name, pattern)
            print(f"Deleted {deleted_count} transaction object(s) from s3://{bucket_name}")

    deleted_people_count = delete_people_records(covered_person_table, people)
    print(f"Deleted {deleted_people_count} record(s) from {USER_TABLE_NAME}")

    deleted_integrator_count = delete_integrator_records(
        integrators_table, integrators
    )
    print(
        f"Deleted {deleted_integrator_count} record(s) from "
        f"{INTEGRATORS_TABLE_NAME}"
    )

    deleted_secret_count = delete_integrator_secrets(secretsmanager, integrators)
    print(f"Deleted {deleted_secret_count} integrator secret(s)")

    for person in people:
        username = next(
            (
                person.get(field)
                for field in USER_POOL_USERNAME_FIELDS
                if person.get(field)
            ),
            None,
        )
        if not username:
            print(f"No Cognito username found for {person['HisaPersonId']}")
            continue

        try:
            cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
            print(f"Deleted Cognito user {username} from {USER_POOL_ID}")
        except cognito.exceptions.UserNotFoundException:
            print(f"Cognito user {username} was already absent from {USER_POOL_ID}")

    CREDENTIALS_PATH.unlink(missing_ok=True)
    print(f"Deleted {CREDENTIALS_PATH.name}")
    INTEGRATOR_SECRETS_CSV_PATH.unlink(missing_ok=True)
    print(f"Deleted {INTEGRATOR_SECRETS_CSV_PATH.name}")


if __name__ == "__main__":
    main()
