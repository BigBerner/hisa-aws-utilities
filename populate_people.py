# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 09:12:24
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-09-29 10:59:10
# 

"""Create Cognito test users and save their generated passwords."""

import csv
import json
import secrets
import string
from pathlib import Path

import boto3
from boto3.dynamodb.types import TypeDeserializer


REGION = "us-east-1"
PROFILE = "ResourceCreator"
USER_POOL_ID = "us-east-1_MxDxfUblP"
TABLE_NAME = "CoveredPerson-dev1"
PEOPLE_CSV_PATH = Path(__file__).with_name("people.csv")
CREDENTIALS_PATH = Path(__file__).with_name("userid_and_password.csv")


def generate_password(length: int = 24) -> str:
    """Generate a password meeting common Cognito password requirements."""
    characters = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    required_characters = (
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*()-_=+"),
    )
    remaining_characters = [
        secrets.choice(characters) for _ in range(length - len(required_characters))
    ]
    password_characters = list(required_characters) + remaining_characters
    secrets.SystemRandom().shuffle(password_characters)
    return "".join(password_characters)


def load_people() -> list[dict[str, str]]:
    """Load people and their Cognito usernames from people.csv."""
    with PEOPLE_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def to_bool(value: str) -> bool:
    """Convert a CSV boolean value to a native Python boolean."""
    return value.strip().lower() == "true"


def row_to_item(row: dict[str, str], deserializer: TypeDeserializer) -> dict:
    """Convert one people.csv row into a CoveredPerson DynamoDB item."""
    typed_name = json.loads(row["Name"])
    return {
        "HisaPersonId": row["HisaPersonId"],
        "BarredFromRacing": to_bool(row["BarredFromRacing"]),
        "BirthDate": row["BirthDate"],
        "DisplayName": row["DisplayName"],
        "IsActive": to_bool(row["IsActive"]),
        "IsCoveredPerson": to_bool(row["IsCoveredPerson"]),
        "IsUnregisteredPerson": to_bool(row["IsUnregisteredPerson"]),
        "Name": {
            attribute_name: deserializer.deserialize(attribute_value)
            for attribute_name, attribute_value in typed_name.items()
        },
        "UserName": row["UserName"],
        "UUID": row["UUID"],
    }


def main() -> None:
    """Create Cognito users and CoveredPerson records from people.csv."""
    people = load_people()
    session = boto3.Session(profile_name=PROFILE, region_name=REGION)
    cognito = session.client("cognito-idp")
    table = session.resource("dynamodb").Table(TABLE_NAME)
    deserializer = TypeDeserializer()

    with CREDENTIALS_PATH.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=("username", "password"))
        writer.writeheader()

        with table.batch_writer() as batch:
            for person in people:
                username = person["UserName"]
                password = generate_password()
                try:
                    cognito.admin_create_user(
                        UserPoolId=USER_POOL_ID,
                        Username=username,
                        TemporaryPassword=password,
                        MessageAction="SUPPRESS",
                    )
                    print(f"Created Cognito user {username}")
                except cognito.exceptions.UsernameExistsException:
                    # Existing passwords are unknown once the CSV is rewritten, so reset them.
                    cognito.admin_set_user_password(
                        UserPoolId=USER_POOL_ID,
                        Username=username,
                        Password=password,
                        Permanent=True,
                    )
                    print(f"Reset password for existing Cognito user {username}")
                writer.writerow({"username": username, "password": password})
                batch.put_item(Item=row_to_item(person, deserializer))
                print(f"Created CoveredPerson record {person['HisaPersonId']}")

    print(f"Saved credentials to {CREDENTIALS_PATH}")


if __name__ == "__main__":
    main()
