# Data files

All CSV inputs, generated credential CSVs, and the SQLite transaction ledger live
in the repository's `data` folder. Scripts resolve that folder relative to their
own location, regardless of the working directory. Generated credentials and the
ledger (including SQLite sidecar files) are ignored by Git.
Both integrator population and cleanup read `data/integrators.csv`.

# Running scripts

The send and cleanup entry points are in the repository root; population scripts
and the shared transaction-tracking module are in `src`. All use the same
repository-level `data` folder, not `src/data`.

To run all population scripts sequentially:

```powershell
.\populate_all.ps1
```

The runner uses `.venv\Scripts\python.exe` and discovers all `populate_*.py`
files directly in `src` and runs them in alphabetical order. It works from any
working directory when invoked by its full path and stops immediately if a
script fails. These scripts write to AWS and generate/reset test credentials;
the runner does not send transaction requests or run cleanup.

Preview the scripts without executing them or accessing AWS:

```powershell
.\populate_all.ps1 -WhatIf
```

From the repository root, use the configured Python environment:

```powershell
& '.\.venv\Scripts\python.exe' '.\send_request.py'
& '.\.venv\Scripts\python.exe' '.\send_assigned_horses_p703300001.py'
& '.\.venv\Scripts\python.exe' '.\src\populate_people.py'
& '.\.venv\Scripts\python.exe' '.\src\populate_integrators.py'
& '.\.venv\Scripts\python.exe' '.\src\populate_avp_entities.py'
& '.\.venv\Scripts\python.exe' '.\clean_up.py'
```

These commands perform live AWS/API operations. Run only the scripts you need;
cleanup deletes the tracked transactions and CSV-defined test setup.
Population scripts also support module execution from the repository root, for
example `python -m src.populate_people`. To run from another directory, use
absolute paths to the interpreter and script.

Run the offline tests from the repository root:

```powershell
& '.\.venv\Scripts\python.exe' -B -m pytest '.\src\test_transaction_cleanup.py' -q
```

# Generating and populating vet-only person entities

`src/populate_avp_entities.py` retains its person generation:
Boolean attributes are rebuilt from `data/people.csv`, existing linked integrators
are retained, and only people whose display name contains "attending veterinarian"
receive the `vet` group parent. Other entities and records are preserved.

**This script uploads to DynamoDB by default**, with the CSV conversion and
batch upload functions included in the script itself. It uploads every row in
the generated CSV, not
just people.

```powershell
& '.\.venv\Scripts\python.exe' '.\src\populate_avp_entities.py'
```

By default it reads `data/people.csv` and `data/avp-entities.csv`, writes
`data/avp-entities.updated.csv`, then populates `avp-entities-and-resources-dev`.
The original input CSV is unchanged. Existing positional input arguments and
`-o`/`--output` overrides remain supported; explicit relative paths are resolved
from the working directory. Module execution with
`python -m src.populate_avp_entities` is also supported.

To generate the CSV without accessing AWS:

```powershell
& '.\.venv\Scripts\python.exe' '.\src\populate_avp_entities.py' --csv-only
```

`populate_all.ps1` runs this generator once along with the other population scripts.

Install development dependencies into the project environment to run tests and
provide Pylance with official DynamoDB resource types:

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r '.\requirements-dev.txt'
```

# Transaction cleanup

Both send scripts save transaction IDs returned by transaction-start requests to
`data/sent_transactions.sqlite3`. Result-only requests do not add
IDs. Each ID is committed before result polling, including cycles whose polling
later fails. Concurrent workers/processes share the SQLite ledger.

Run `clean_up.py` to remove only records whose DynamoDB primary-key values or S3
object names contain a saved, complete transaction ID. Cleanup covers
`enterprise-transaction-queue-dev`, `avp-entities-and-resources-dev`,
`transaction-payloads-dev`, and `transaction-responses-dev`. IDs may be exact
keys or delimited by characters such as `/`, `#`, or `.`; letters, digits,
underscores, and hyphens are treated as part of an ID, preventing partial matches.
Records with IDs stored only in non-key attributes are not deleted.

If the ledger is absent or empty, transaction cleanup does nothing. Transactions
sent before this tracking was added are not tracked automatically. Keep the
ledger: cleanup retains IDs for retries and late-arriving transaction results.
Wait for send scripts and backend processing to finish before cleanup.
Only current S3 objects are removed, not historical versions.

Cleanup no longer purges SQS. It still performs the existing CSV-defined
CoveredPerson, integrator, secret, and Cognito user cleanup and removes generated
credential CSVs. Nontransaction AVP setup entities are preserved.
AWS access/partial deletion errors stop cleanup; a nonexistent bucket is reported
and skipped.
