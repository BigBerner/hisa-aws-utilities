# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 08:47:29
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-09-29 10:37:26
# 

"""Delete all objects from the configured AWS resources."""

import csv
import os
from pathlib import Path

import boto3


REGION = "us-east-1"
PROFILE = "ResourceCreator"
TABLE_NAMES = (
    "enterprise-transaction-queue-dev",
    "avp-entities-and-resources-dev",
)
QUEUE_URL = "https://sqs.us-east-1.amazonaws.com/285463978628/authenticated-requests-dev.fifo"
BUCKET_NAME = "transaction-payloads-dev"
USER_POOL_ID = "us-east-1_MxDxfUblP"
PEOPLE_CSV_PATH = Path(__file__).with_name("people.csv")
USER_POOL_USERNAME_FIELDS = ("user_ir", "UserName")
USER_TABLE_NAME = "CoveredPerson-dev1"
INTEGRATORS_TABLE_NAME = "third-party-integrators-dev"
INTEGRATORS_CSV_PATH = Path(__file__).with_name("integratos.csv")
INTEGRATOR_SECRETS_CSV_PATH = Path(__file__).with_name("integrator_secrets.csv")
CREDENTIALS_PATH = Path(__file__).with_name("userid_and_password.csv")


def delete_all_items(table) -> int:
    """Delete all items from a DynamoDB table and return the item count."""
    deleted_count = 0
    scan_kwargs = {}

    while True:
        response = table.scan(**scan_kwargs)
        items = response.get("Items", [])

        with table.batch_writer() as batch:
            for item in items:
                key_names = [key["AttributeName"] for key in table.key_schema]
                batch.delete_item(Key={key: item[key] for key in key_names})
                deleted_count += 1

        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

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
    """Load integrator table keys from integratos.csv."""
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
    """Delete all records from both DynamoDB tables."""
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    people = load_people()
    integrators = load_integrators()
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    dynamodb = session.resource("dynamodb")
    sqs = session.client("sqs")
    s3 = session.resource("s3")
    cognito = session.client("cognito-idp")
    secretsmanager = session.client("secretsmanager")
    covered_person_table = dynamodb.Table(USER_TABLE_NAME)
    integrators_table = dynamodb.Table(INTEGRATORS_TABLE_NAME)

    for table_name in TABLE_NAMES:
        table = dynamodb.Table(table_name)
        deleted_count = delete_all_items(table)
        print(f"Deleted {deleted_count} item(s) from {table_name}")

    sqs.purge_queue(QueueUrl=QUEUE_URL)
    print(f"Purged messages from {QUEUE_URL}")

    s3.Bucket(BUCKET_NAME).objects.delete()
    print(f"Deleted objects from s3://{BUCKET_NAME}")

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
