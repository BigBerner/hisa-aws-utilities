# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-10-02 08:09:24
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-10-02 08:23:47
# 

"""Time get-assigned-horses for P703300001 through to its transaction result.

Each attempt calls horse.get_assigned, waits INITIAL_RESULT_DELAY_SECONDS, then
polls transaction_get_result every RESULT_POLL_INTERVAL_SECONDS until the
result is no longer "processing". The measured time runs from sending the
get_assigned call until the get-result call returns the data.
"""

import csv
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from botocore.exceptions import ClientError

from send_request import (
    GET_RESULT_ACTION,
    REQUESTS_CSV_PATH,
    CognitoTokenProvider,
    build_request,
    find_transaction_id,
    is_processing,
    load_integrator_keys,
)

REQUEST_NAME = "get assigned horses P703300001"
PRINCIPAL = "P703300001"
REQUEST_COUNT = 1000
CONCURRENT_WORKERS = 20
INITIAL_RESULT_DELAY_SECONDS = 0.5
RESULT_POLL_INTERVAL_SECONDS = 0.25
MAX_RESULT_POLLS = 120


class TransactionError(Exception):
    """Raised when a transaction cycle cannot be completed."""


def send(request) -> tuple[int, object]:
    """Send a request and return its HTTP status and parsed JSON body."""
    try:
        with urlopen(request, timeout=30) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        raise TransactionError(
            f"HTTP {error.code} {error.read().decode('utf-8', errors='replace')}"
        ) from error
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None


def run_cycle(
    start_row: dict[str, str],
    result_row: dict[str, str],
    integrator_keys: dict[str, str],
    token_provider: CognitoTokenProvider,
) -> tuple[float, int, object]:
    """Run one start + poll cycle; return elapsed seconds, poll count, result."""
    # Build before starting the clock so Cognito sign-in isn't timed.
    start_request = build_request(start_row, integrator_keys, token_provider)

    started_at = perf_counter()
    _, start_body = send(start_request)
    transaction_id = find_transaction_id(start_body)
    if not transaction_id:
        raise TransactionError(f"no transaction ID in response: {start_body}")

    poll_row = {**result_row, "resource": transaction_id}
    sleep(INITIAL_RESULT_DELAY_SECONDS)
    for poll in range(1, MAX_RESULT_POLLS + 1):
        _, result_body = send(build_request(poll_row, integrator_keys, token_provider))
        if not is_processing(result_body):
            return perf_counter() - started_at, poll, result_body
        sleep(RESULT_POLL_INTERVAL_SECONDS)

    raise TransactionError(
        f"transaction {transaction_id} still processing after "
        f"{MAX_RESULT_POLLS} polls"
    )


def run_worker(
    worker_id: int,
    start_row: dict[str, str],
    result_row: dict[str, str],
    integrator_keys: dict[str, str],
    output_lock: Lock,
) -> tuple[int, float, int]:
    token_provider = CognitoTokenProvider()
    total_elapsed = 0.0
    received_count = 0
    failed_count = 0

    for attempt in range(1, REQUEST_COUNT + 1):
        label = f"Worker {worker_id} attempt {attempt}/{REQUEST_COUNT}"
        try:
            elapsed, polls, _ = run_cycle(
                start_row, result_row, integrator_keys, token_provider
            )
        except (KeyError, ValueError) as error:
            with output_lock:
                print(f"Worker {worker_id}: invalid request: {error}", flush=True)
            break
        except ClientError as error:
            with output_lock:
                print(f"Worker {worker_id}: Cognito error: {error}", flush=True)
            break
        except (TransactionError, URLError, TimeoutError) as error:
            failed_count += 1
            reason = getattr(error, "reason", error)
            with output_lock:
                print(f"{label}: failed ({reason})", flush=True)
            continue

        received_count += 1
        total_elapsed += elapsed
        average = total_elapsed / received_count
        with output_lock:
            print(
                f"{label}: result in {elapsed:.3f}s after {polls} poll(s); "
                f"running average {average:.3f}s over {received_count} results",
                flush=True,
            )

    with output_lock:
        print(
            f"Worker {worker_id} complete: {received_count} results, "
            f"{failed_count} failures; average "
            f"{total_elapsed / received_count:.3f}s"
            if received_count
            else f"Worker {worker_id} complete: no results, "
            f"{failed_count} failures",
            flush=True,
        )
    return received_count, total_elapsed, failed_count


def main() -> None:
    with REQUESTS_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))

    matches = [
        row for row in rows if row.get("request_name", "").strip() == REQUEST_NAME
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {REQUEST_NAME!r} row, found {len(matches)}"
        )
    start_row = matches[0]
    if start_row.get("skip", "").strip().lower() in {"yes", "true", "1"}:
        print(f"Skipped {REQUEST_NAME}")
        return

    # Use the bearer get-result row for the same principal as a template.
    result_rows = [
        row
        for row in rows
        if row.get("action", "").strip() == GET_RESULT_ACTION
        and row.get("principal", "").strip() == PRINCIPAL
    ]
    if not result_rows:
        raise ValueError(f"No {GET_RESULT_ACTION!r} row found for {PRINCIPAL}")
    result_row = result_rows[0]

    integrator_keys = load_integrator_keys()
    output_lock = Lock()
    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        futures = [
            executor.submit(
                run_worker,
                worker_id,
                start_row,
                result_row,
                integrator_keys,
                output_lock,
            )
            for worker_id in range(1, CONCURRENT_WORKERS + 1)
        ]
        results = [future.result() for future in futures]

    total_received = sum(result[0] for result in results)
    total_elapsed = sum(result[1] for result in results)
    total_failed = sum(result[2] for result in results)
    if total_received:
        print(
            f"All workers complete: {total_received} results, {total_failed} "
            f"failures; average get_assigned-to-result time "
            f"{total_elapsed / total_received:.3f}s"
        )
    else:
        print(f"All workers complete: no results; {total_failed} failures")


if __name__ == "__main__":
    main()
