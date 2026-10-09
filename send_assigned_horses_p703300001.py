# -*- coding: utf-8 -*-
# @Author: Steve Keech
# @Date:   2026-10-02 08:09:24
# @Email:  steve.keech@hisaus.org
# @Last Modified by:   Steve Keech
# @Last Modified time: 2026-10-02 08:23:47
# 

"""Send only the get-assigned-horses request for person P703300001."""

import csv
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from botocore.exceptions import ClientError

from send_request import (
    REQUESTS_CSV_PATH,
    CognitoTokenProvider,
    build_request,
    load_integrator_keys,
)

REQUEST_NAME = "get assigned horses P703300001"
REQUEST_COUNT = 1000
CONCURRENT_WORKERS = 20


def run_worker(
    worker_id: int,
    row: dict[str, str],
    integrator_keys: dict[str, str],
    output_lock: Lock,
) -> tuple[int, float, int]:
    token_provider = CognitoTokenProvider()
    total_elapsed = 0.0
    received_count = 0
    failed_count = 0

    for attempt in range(1, REQUEST_COUNT + 1):
        try:
            request = build_request(row, integrator_keys, token_provider)
        except (KeyError, ValueError) as error:
            with output_lock:
                print(f"Worker {worker_id}: invalid request: {error}", flush=True)
            break
        except ClientError as error:
            with output_lock:
                print(f"Worker {worker_id}: Cognito error: {error}", flush=True)
            break

        started_at = perf_counter()
        try:
            with urlopen(request, timeout=30) as response:
                response.read()
                status = response.status
        except HTTPError as error:
            error.read()
            status = error.code
        except (URLError, TimeoutError) as error:
            elapsed = perf_counter() - started_at
            average = total_elapsed / received_count if received_count else 0.0
            failed_count += 1
            reason = getattr(error, "reason", error)
            with output_lock:
                print(
                    f"Worker {worker_id} attempt {attempt}/{REQUEST_COUNT}: "
                    f"failed after {elapsed:.3f}s ({reason}); running average "
                    f"{average:.3f}s over {received_count} responses",
                    flush=True,
                )
            continue

        elapsed = perf_counter() - started_at
        received_count += 1
        total_elapsed += elapsed
        average = total_elapsed / received_count
        with output_lock:
            print(
                f"Worker {worker_id} attempt {attempt}/{REQUEST_COUNT}: "
                f"HTTP {status}, {elapsed:.3f}s; running average "
                f"{average:.3f}s over {received_count} responses",
                flush=True,
            )

    with output_lock:
        print(
            f"Worker {worker_id} complete: {received_count} responses, "
            f"{failed_count} network failures; average "
            f"{total_elapsed / received_count:.3f}s"
            if received_count
            else f"Worker {worker_id} complete: no responses, "
            f"{failed_count} network failures",
            flush=True,
        )
    return received_count, total_elapsed, failed_count


def main() -> None:
    with REQUESTS_CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
        matches = [
            row
            for row in csv.DictReader(csv_file)
            if row.get("request_name", "").strip() == REQUEST_NAME
        ]

    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one {REQUEST_NAME!r} row, found {len(matches)}"
        )

    row = matches[0]
    if row.get("skip", "").strip().lower() in {"yes", "true", "1"}:
        print(f"Skipped {REQUEST_NAME}")
        return

    integrator_keys = load_integrator_keys()
    output_lock = Lock()
    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        futures = [
            executor.submit(run_worker, worker_id, row, integrator_keys, output_lock)
            for worker_id in range(1, CONCURRENT_WORKERS + 1)
        ]
        results = [future.result() for future in futures]

    total_received = sum(result[0] for result in results)
    total_elapsed = sum(result[1] for result in results)
    total_failed = sum(result[2] for result in results)
    if total_received:
        print(
            f"All workers complete: {total_received} responses, {total_failed} "
            f"network failures; aggregate average round-trip time "
            f"{total_elapsed / total_received:.3f}s"
        )
    else:
        print(f"All workers complete: no responses; {total_failed} network failures")


if __name__ == "__main__":
    main()
