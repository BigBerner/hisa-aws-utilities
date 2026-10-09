# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-09-29 11:09:55
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-09-29 19:40:17
# 

"""Send HTTP requests defined in transaction_requests.csv."""

import base64
import csv
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError


REGION = "us-east-1"
PROFILE = "ResourceCreator"
USER_POOL_ID = "us-east-1_MxDxfUblP"
COGNITO_CLIENT_ID = "opovhq5sep0imt5g0kvpakifb"
HISA_APP_API_KEY = "abc123-super-secret"
HISA_SCREEN_ID = "HP-4"
REQUESTS_CSV_PATH = Path(__file__).with_name("transaction_requests.csv")
INTEGRATOR_SECRETS_CSV_PATH = Path(__file__).with_name("integrator_secrets.csv")
PEOPLE_CSV_PATH = Path(__file__).with_name("people.csv")
CREDENTIALS_PATH = Path(__file__).with_name("userid_and_password.csv")
GET_RESULT_ACTION = "transaction_get_result"
RESULT_POLL_INTERVAL_SECONDS = 0.5
MAX_RESULT_POLL_ATTEMPTS = 60
STATUS_KEYS = ("status", "state", "transaction_status", "transactionStatus")
TRANSACTION_ID_KEYS = ("transaction_id", "transactionId", "trans_id", "transId", "id")
TRANS_ID_PLACEHOLDER = re.compile(r"\{\{\s*trans_id_R(\d+)\s*\}\}", re.IGNORECASE)


class CognitoTokenProvider:
    """Obtain and cache Cognito ID tokens for HISA people."""

    def __init__(self) -> None:
        self._client = None
        self._client_id = ""
        self._client_secret = ""
        self._usernames: dict[str, str] = {}
        self._passwords: dict[str, str] = {}
        self._tokens: dict[str, str] = {}

    def _initialize(self) -> None:
        if self._client is not None:
            return

        session = boto3.Session(profile_name=PROFILE, region_name=REGION)
        self._client = session.client("cognito-idp")

        self._client_id = COGNITO_CLIENT_ID
        client_details = self._client.describe_user_pool_client(
            UserPoolId=USER_POOL_ID, ClientId=self._client_id
        )["UserPoolClient"]
        self._client_secret = client_details.get("ClientSecret", "")

        with PEOPLE_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
            self._usernames = {
                row["HisaPersonId"]: row["UserName"] for row in csv.DictReader(csv_file)
            }
        with CREDENTIALS_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
            self._passwords = {
                row["username"]: row["password"] for row in csv.DictReader(csv_file)
            }

    def _secret_hash(self, username: str) -> str:
        digest = hmac.new(
            self._client_secret.encode("utf-8"),
            f"{username}{self._client_id}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _authenticate(self, username: str, password: str) -> dict:
        auth_parameters = {"USERNAME": username, "PASSWORD": password}
        if self._client_secret:
            auth_parameters["SECRET_HASH"] = self._secret_hash(username)
        return self._client.initiate_auth(
            ClientId=self._client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters=auth_parameters,
        )

    def token_for(self, person_id: str) -> str:
        """Return a Cognito ID token for the given HISA person ID."""
        self._initialize()
        if person_id in self._tokens:
            return self._tokens[person_id]

        username = self._usernames.get(person_id)
        password = self._passwords.get(username or "")
        if not username or not password:
            raise ValueError(f"No Cognito credentials found for {person_id}")

        response = self._authenticate(username, password)
        if response.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
            # Users are created with temporary passwords; make the stored one permanent.
            self._client.admin_set_user_password(
                UserPoolId=USER_POOL_ID,
                Username=username,
                Password=password,
                Permanent=True,
            )
            response = self._authenticate(username, password)

        if "AuthenticationResult" not in response:
            raise ValueError(
                f"Cognito returned unexpected challenge {response.get('ChallengeName')!r}"
            )

        token = response["AuthenticationResult"]["IdToken"]
        self._tokens[person_id] = token
        return token


def current_timestamp() -> str:
    """Return the current UTC timestamp in the API's required format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_integrator_keys() -> dict[str, str]:
    """Load generated integrator API keys when the credentials CSV exists."""
    if not INTEGRATOR_SECRETS_CSV_PATH.exists():
        return {}

    with INTEGRATOR_SECRETS_CSV_PATH.open(
        "r", encoding="utf-8-sig", newline=""
    ) as csv_file:
        return {
            row["integrator_id"]: row["key"]
            for row in csv.DictReader(csv_file)
            if row.get("integrator_id") and row.get("key")
        }


def resolve_credential(value: str, environment_name: str) -> str:
    """Resolve a CSV value or its environment-variable placeholder."""
    if value and not (value.startswith("{{") and value.endswith("}}")):
        return value

    return os.environ.get(environment_name, "")


def parse_json(value: str, default):
    """Parse an optional JSON CSV field."""
    return json.loads(value) if value.strip() else default


def find_transaction_id(data) -> str:
    """Return the first transaction ID found in a (possibly nested) JSON response."""
    if isinstance(data, dict):
        for key in TRANSACTION_ID_KEYS:
            value = data.get(key)
            if isinstance(value, (str, int)) and str(value):
                return str(value)
        children = data.values()
    elif isinstance(data, list):
        children = data
    else:
        return ""

    for child in children:
        found = find_transaction_id(child)
        if found:
            return found
    return ""


def is_processing(data) -> bool:
    """Return True if a (possibly nested) JSON response reports a processing status."""
    if isinstance(data, dict):
        for key in STATUS_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip().lower() == "processing":
                return True
        return any(is_processing(child) for child in data.values())
    if isinstance(data, list):
        return any(is_processing(child) for child in data)
    return False


def substitute_transaction_ids(value: str, transaction_ids: dict[int, str]) -> str:
    """Replace {{trans_id_R<n>}} with the transaction ID returned by CSV row n."""

    def replace(match: re.Match) -> str:
        row_number = int(match.group(1))
        if row_number not in transaction_ids:
            raise ValueError(f"No transaction ID available from row R{row_number}")
        return transaction_ids[row_number]

    return TRANS_ID_PLACEHOLDER.sub(replace, value)


def build_request(
    row: dict[str, str],
    integrator_keys: dict[str, str],
    token_provider: CognitoTokenProvider,
) -> Request:
    """Build an HTTP request from one CSV row."""
    auth_mode = row.get("auth_mode", "").strip().lower()
    if not auth_mode:
        auth_mode = "integrator" if "/3rd/" in row["url"] else "bearer"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if auth_mode == "bearer":
        bearer_token = resolve_credential(
            row.get("bearer_token", ""), "BEARER_TOKEN"
        ) or token_provider.token_for(row["principal"])
        app_api_key = (
            resolve_credential(row.get("app_api_key", ""), "APP_API_KEY")
            or HISA_APP_API_KEY
        )
        headers["Authorization"] = f"Bearer {bearer_token}"
        headers["x-app-api-key"] = app_api_key
        headers["screen-id"] = HISA_SCREEN_ID
    elif auth_mode == "integrator":
        integrator_id = row.get("integrator_id", "").strip()
        if not integrator_id and len(integrator_keys) == 1:
            integrator_id = next(iter(integrator_keys))
        api_key = (
            row.get("app_api_key", "").strip()
            or integrator_keys.get(integrator_id, "")
            or os.environ.get("INTEGRATOR_API_KEY", "")
        )
        if not integrator_id or not api_key:
            raise ValueError(
                "Integrator requests require integrator_id and an API key "
                "from the CSV, integrator_secrets.csv, or INTEGRATOR_API_KEY"
            )

        timestamp = current_timestamp()
        signature = hmac.new(
            api_key.encode("utf-8"),
            f"{timestamp}{integrator_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["x-integrator-id"] = integrator_id
        headers["x-timestamp"] = timestamp
        headers["x-signature"] = signature
    else:
        raise ValueError(f"Unsupported auth_mode: {auth_mode!r}")

    body = {
        "principal": row["principal"],
        "action": row["action"],
        "resource": row["resource"],
        "priority": row["priority"],
    }
    if row["action"] != GET_RESULT_ACTION:
        body["payload"] = parse_json(row.get("payload", ""), {})
    context = parse_json(row.get("context", ""), None)
    if context is not None:
        body["context"] = context

    return Request(
        url=row["url"],
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method=row.get("method", "POST").upper(),
    )


def print_request(request: Request | None) -> None:
    """Print the method, URL, headers, and body of a sent request."""
    if request is None:
        return
    print(f"  Request: {request.get_method()} {request.full_url}")
    for name, value in request.header_items():
        print(f"    {name}: {value}")
    if request.data:
        print(f"  Body: {request.data.decode('utf-8')}")


def send_requests() -> None:
    """Send every non-skipped request from the input CSV."""
    integrator_keys = load_integrator_keys()
    token_provider = CognitoTokenProvider()
    # Transaction IDs keyed by 1-based CSV data row number (R1, R2, ...).
    transaction_ids: dict[int, str] = {}
    with REQUESTS_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        for row_number, row in enumerate(csv.DictReader(csv_file), start=1):
            if row.get("skip", "").strip().lower() in {"yes", "true", "1"}:
                print(f"Skipped R{row_number} {row.get('request_name', '<unnamed>')}")
                continue

            request_name = f"R{row_number} {row.get('request_name', '<unnamed>')}"
            request = None
            try:
                row = {
                    key: substitute_transaction_ids(value or "", transaction_ids)
                    for key, value in row.items()
                    if key is not None
                }
                is_get_result = row["action"] == GET_RESULT_ACTION
                for attempt in range(1, MAX_RESULT_POLL_ATTEMPTS + 1):
                    request = build_request(row, integrator_keys, token_provider)
                    with urlopen(request, timeout=30) as response:
                        response_body = response.read().decode("utf-8")
                        print(f"{request_name}: {response.status} {response_body}")
                    try:
                        response_json = json.loads(response_body)
                    except json.JSONDecodeError:
                        response_json = None
                    if not (is_get_result and is_processing(response_json)):
                        break
                    if attempt == MAX_RESULT_POLL_ATTEMPTS:
                        print(
                            f"  still processing after {attempt} attempts; giving up"
                        )
                        break
                    print(
                        f"  processing; retrying in {RESULT_POLL_INTERVAL_SECONDS}s "
                        f"(attempt {attempt + 1}/{MAX_RESULT_POLL_ATTEMPTS})"
                    )
                    time.sleep(RESULT_POLL_INTERVAL_SECONDS)
                transaction_id = (
                    find_transaction_id(response_json) if response_json else ""
                )
                if transaction_id:
                    transaction_ids[row_number] = transaction_id
                    print(f"  trans_id_R{row_number} = {transaction_id}")
            except HTTPError as error:
                error_body = error.read().decode("utf-8", errors="replace")
                print(f"{request_name}: {error.code} {error_body}")
                print_request(request)
            except URLError as error:
                print(f"{request_name}: request failed: {error.reason}")
                print_request(request)
            except (KeyError, ValueError) as error:
                print(f"{request_name}: invalid request: {error}")
            except ClientError as error:
                print(f"{request_name}: Cognito error: {error}")


if __name__ == "__main__":
    send_requests()
