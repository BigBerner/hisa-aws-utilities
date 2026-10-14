"""Offline tests for transaction tracking and selective AWS cleanup."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import clean_up
import src.populate_avp_entities as populate_avp_entities
import src.populate_integrators as populate_integrators
import src.populate_people as populate_people
import send_assigned_horses_p703300001 as assigned
import send_request
import src.transaction_tracking as tracking


@pytest.mark.parametrize("module, names", [
    (clean_up, ("PEOPLE_CSV_PATH", "INTEGRATORS_CSV_PATH",
                "INTEGRATOR_SECRETS_CSV_PATH", "CREDENTIALS_PATH")),
    (send_request, ("REQUESTS_CSV_PATH", "INTEGRATOR_SECRETS_CSV_PATH",
                    "PEOPLE_CSV_PATH", "CREDENTIALS_PATH")),
    (populate_people, ("PEOPLE_CSV_PATH", "CREDENTIALS_PATH")),
    (populate_integrators, ("INTEGRATORS_CSV_PATH",
                           "SECRETS_CSV_PATH")),
    (populate_avp_entities, ("CSV_PATH",)),
    (tracking, ("TRANSACTIONS_PATH",)),
    (assigned, ("REQUESTS_CSV_PATH",)),
])
def test_all_data_paths_are_repository_relative(module, names, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = Path(__file__).resolve().parent.parent / "data"
    for name in names:
        path = getattr(module, name)
        assert path.is_absolute()
        assert path.parent == data_dir


@pytest.mark.parametrize("relative_path", [
    "clean_up.py",
    "send_request.py",
    "send_assigned_horses_p703300001.py",
    str(Path("src") / "populate_people.py"),
    str(Path("src") / "populate_integrators.py"),
    str(Path("src") / "populate_avp_entities.py"),
    str(Path("src") / "transaction_tracking.py"),
])
def test_scripts_load_from_other_directory_without_aws_calls(relative_path, tmp_path):
    script = Path(__file__).resolve().parent.parent / relative_path
    code = """
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

script = Path(sys.argv[1])
sys.path.insert(0, str(script.parent))
with patch("boto3.Session", side_effect=AssertionError("Unexpected AWS access")):
    namespace = runpy.run_path(str(script), run_name="layout_check")
for name, value in namespace.items():
    if name.endswith(("_CSV_PATH", "CREDENTIALS_PATH", "TRANSACTIONS_PATH", "CSV_PATH")):
        assert value.parent == Path(sys.argv[2]), (name, value)
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(script),
         str(Path(__file__).resolve().parent.parent / "data")],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_moved_csv_inputs_load_from_another_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert clean_up.load_people() == populate_people.load_people()
    assert clean_up.load_integrators() == populate_integrators.load_integrators()
    assert populate_people.load_people()
    assert populate_integrators.load_integrators()
    table = MagicMock()
    assert populate_avp_entities.populate_table(table) > 0
    with send_request.REQUESTS_CSV_PATH.open(encoding="utf-8-sig") as csv_file:
        assert "request_name" in csv_file.readline()


def test_integrator_loaders_use_corrected_filename():
    expected = Path(__file__).resolve().parent.parent / "data" / "integrators.csv"
    assert clean_up.INTEGRATORS_CSV_PATH == expected
    assert populate_integrators.INTEGRATORS_CSV_PATH == expected
    assert clean_up.load_integrators() == populate_integrators.load_integrators()


def test_ledger_creates_data_directory(tmp_path, monkeypatch):
    path = tmp_path / "data" / "sent_transactions.sqlite3"
    monkeypatch.setattr(tracking, "TRANSACTIONS_PATH", path)
    tracking.record_transaction_id("txn-created")
    assert tracking.load_transaction_ids() == {"txn-created"}


def test_integrator_output_creates_data_directory(tmp_path, monkeypatch):
    path = tmp_path / "data" / "integrator_secrets.csv"
    monkeypatch.setattr(populate_integrators, "SECRETS_CSV_PATH", path)
    assert populate_integrators.create_integrator_secrets(MagicMock(), []) == 0
    assert path.read_text(encoding="utf-8").strip() == "integrator_id,key"


def test_people_output_creates_data_directory(tmp_path, monkeypatch):
    path = tmp_path / "data" / "userid_and_password.csv"
    monkeypatch.setattr(populate_people, "CREDENTIALS_PATH", path)
    monkeypatch.setattr(populate_people, "load_people", lambda: [])
    monkeypatch.setattr(populate_people.boto3, "Session", MagicMock())
    populate_people.main()
    assert path.read_text(encoding="utf-8").strip() == "username,password"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "sent_transactions.sqlite3"
    monkeypatch.setattr(tracking, "TRANSACTIONS_PATH", path)
    return path


def test_missing_ledger_does_not_create_file(ledger):
    assert tracking.load_transaction_ids() == set()
    assert not ledger.exists()


def test_ledger_persists_deduplicated_concurrent_ids(ledger):
    ids = [f"txn-{i}" for i in range(40)] * 2
    with ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(tracking.record_transaction_id, ids))
    assert tracking.load_transaction_ids() == set(ids)
    assert ledger.exists()


def test_corrupt_ledger_fails_closed(ledger):
    ledger.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(tracking.sqlite3.DatabaseError):
        tracking.load_transaction_ids()


@pytest.mark.parametrize("value", ["", " ", " txn", "txn "])
def test_invalid_id_is_rejected(ledger, value):
    with pytest.raises(ValueError):
        tracking.record_transaction_id(value)
    assert not ledger.exists()


def test_matching_is_complete_and_literal():
    pattern = tracking.transaction_id_pattern({"txn-123", "id.with.dots"})
    for key in ("txn-123", "transaction#txn-123", "requests/txn-123.json",
                "id.with.dots"):
        assert pattern.search(key)
    for key in ("txn-1234", "other-txn-123", "txn-123_extra", "txn-123-copy",
                "idXwithXdots", "unrelated"):
        assert not pattern.search(key)
    with pytest.raises(ValueError):
        tracking.transaction_id_pattern(set())


def test_dynamo_cleanup_preserves_untracked_and_setup_items_across_pages():
    table = MagicMock()
    table.key_schema = [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    last_key = {"pk": "page", "sk": "#"}
    table.scan.side_effect = [
        {"Items": [
            {"pk": "txn-123", "sk": "#"},
            {"pk": "P703300001", "sk": "#"},
            {"pk": "txn-1234", "sk": "#"},
            {"pk": "unrelated", "sk": "#", "transaction_id": "txn-123"},
        ], "LastEvaluatedKey": last_key},
        {"Items": [{"pk": "transaction", "sk": "result#txn-123"}]},
    ]
    count = clean_up.delete_transaction_items(
        table, tracking.transaction_id_pattern({"txn-123"})
    )
    assert count == 2
    batch = table.batch_writer.return_value.__enter__.return_value
    assert [call.kwargs["Key"] for call in batch.delete_item.call_args_list] == [
        {"pk": "txn-123", "sk": "#"},
        {"pk": "transaction", "sk": "result#txn-123"},
    ]
    assert table.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == last_key
    assert table.scan.call_args_list[0].kwargs["ConsistentRead"] is True


@pytest.mark.parametrize("bucket", clean_up.BUCKET_NAMES)
def test_s3_cleanup_preserves_untracked_objects_across_pages(bucket):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [
            {"Key": "payloads/txn-123.json"},
            {"Key": "payloads/txn-1234.json"},
            {"Key": "setup.json"},
        ]},
        {},
        {"Contents": [{"Key": "txn-123"}]},
    ]
    s3.delete_objects.side_effect = [
        {"Deleted": [{"Key": "payloads/txn-123.json"}]},
        {"Deleted": [{"Key": "txn-123"}]},
    ]
    assert clean_up.delete_transaction_objects(
        s3, bucket, tracking.transaction_id_pattern({"txn-123"})
    ) == 2
    assert [call.kwargs for call in s3.delete_objects.call_args_list] == [
        {"Bucket": bucket, "Delete": {"Objects": [{"Key": "payloads/txn-123.json"}]}},
        {"Bucket": bucket, "Delete": {"Objects": [{"Key": "txn-123"}]}},
    ]


def test_s3_partial_failure_is_reported():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": "txn-123"}]}
    ]
    s3.delete_objects.return_value = {
        "Errors": [{"Key": "txn-123", "Code": "AccessDenied"}]
    }
    with pytest.raises(RuntimeError, match="AccessDenied"):
        clean_up.delete_transaction_objects(
            s3, "bucket", tracking.transaction_id_pattern({"txn-123"})
        )


@pytest.mark.parametrize("code", ["NoSuchBucket", "AccessDenied"])
def test_s3_missing_bucket_is_explicit_and_access_errors_propagate(code, capsys):
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": code, "Message": code}}, "ListObjectsV2"
    )
    pattern = tracking.transaction_id_pattern({"txn-123"})
    if code == "NoSuchBucket":
        assert clean_up.delete_transaction_objects(s3, "bucket", pattern) == 0
        assert "does not exist" in capsys.readouterr().out
    else:
        with pytest.raises(ClientError):
            clean_up.delete_transaction_objects(s3, "bucket", pattern)
    s3.delete_objects.assert_not_called()


def test_assigned_script_records_id_before_poll_failure(ledger, monkeypatch):
    monkeypatch.setattr(assigned, "build_request", MagicMock())
    monkeypatch.setattr(assigned, "sleep", MagicMock())
    send = MagicMock(side_effect=[
        (202, {"transaction_id": "txn-123"}),
        assigned.TransactionError("poll failed"),
    ])
    monkeypatch.setattr(assigned, "send", send)
    with pytest.raises(assigned.TransactionError, match="poll failed"):
        assigned.run_cycle({}, {}, {}, MagicMock())
    assert tracking.load_transaction_ids() == {"txn-123"}


def test_general_script_tracks_starts_but_not_results_or_skips(
    ledger, tmp_path, monkeypatch
):
    csv_path = tmp_path / "requests.csv"
    csv_path.write_text(
        "request_name,action,resource,skip\n"
        "start,horse.get_assigned,horse,\n"
        "result,transaction_get_result,existing-transaction,\n"
        "skipped,horse.get_assigned,horse,yes\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(send_request, "REQUESTS_CSV_PATH", csv_path)
    monkeypatch.setattr(send_request, "load_integrator_keys", lambda: {})
    monkeypatch.setattr(send_request, "build_request", MagicMock())
    responses = []
    for transaction_id in ("txn-created", "existing-transaction"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 202
        response.read.return_value = json.dumps(
            {"transaction_id": transaction_id, "status": "complete"}
        ).encode()
        responses.append(response)
    urlopen = MagicMock(side_effect=responses)
    monkeypatch.setattr(send_request, "urlopen", urlopen)
    send_request.send_requests()
    assert tracking.load_transaction_ids() == {"txn-created"}
    assert urlopen.call_count == 2


@pytest.mark.parametrize("tracked", [False, True])
def test_cleanup_uses_ledger_preserves_setup_cleanup_and_never_purges_sqs(
    ledger, monkeypatch, tracked
):
    if tracked:
        tracking.record_transaction_id("txn-123")
    session = MagicMock()
    monkeypatch.setattr(clean_up.boto3, "Session", MagicMock(return_value=session))
    monkeypatch.setattr(clean_up, "load_people", lambda: [])
    monkeypatch.setattr(clean_up, "load_integrators", lambda: [])
    for name in ("delete_people_records", "delete_integrator_records",
                 "delete_integrator_secrets"):
        monkeypatch.setattr(clean_up, name, MagicMock(return_value=0))
    monkeypatch.setattr(clean_up, "CREDENTIALS_PATH", MagicMock())
    monkeypatch.setattr(clean_up, "INTEGRATOR_SECRETS_CSV_PATH", MagicMock())
    delete_items = MagicMock(return_value=1)
    delete_objects = MagicMock(return_value=1)
    monkeypatch.setattr(clean_up, "delete_transaction_items", delete_items)
    monkeypatch.setattr(clean_up, "delete_transaction_objects", delete_objects)
    clean_up.main()
    if tracked:
        assert delete_items.call_count == len(clean_up.TABLE_NAMES)
        assert [call.args[1] for call in delete_objects.call_args_list] == list(
            clean_up.BUCKET_NAMES
        )
        for call in delete_items.call_args_list + delete_objects.call_args_list:
            assert call.args[-1].search("txn-123")
            assert not call.args[-1].search("txn-unrelated")
        assert tracking.load_transaction_ids() == {"txn-123"}
    else:
        delete_items.assert_not_called()
        delete_objects.assert_not_called()
    clean_up.delete_people_records.assert_called_once()
    clean_up.delete_integrator_records.assert_called_once()
    clean_up.delete_integrator_secrets.assert_called_once()
    assert "sqs" not in [call.args[0] for call in session.client.call_args_list]
