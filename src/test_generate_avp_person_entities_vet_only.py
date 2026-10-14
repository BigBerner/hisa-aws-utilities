"""Offline coverage for combined person generation and AVP population."""

import csv
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from boto3.dynamodb.types import TypeDeserializer

from src import populate_avp_entities as generator
from src import populate_avp_entities as population


def write_csv(path, fieldnames, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def inputs(tmp_path):
    linked = {"L": [{"M": {"entityId": {"S": "INT001"},
                          "entityType": {"S": "hisa::integrator"}}}]}
    rows = [
        {"pk": "person-1", "sk": "#",
         "attributes": json.dumps({"linked_integrators": linked}),
         "identifier": json.dumps({"entityId": {"S": "person-1"},
                                   "entityType": {"S": "hisa::person"}}),
         "parents": '[{"entityId":"old","entityType":"hisa::group"}]'},
        {"pk": "person-1", "sk": "related",
         "attributes": '{"enabled":{"BOOL":true}}',
         "identifier": '{"entityId":{"S":"related"},"entityType":{"S":"hisa::resource"}}',
         "parents": "[]"},
        {"pk": "vet", "sk": "#", "attributes": "{}",
         "identifier": '{"entityId":{"S":"vet"},"entityType":{"S":"hisa::group"}}',
         "parents": "[]"},
    ]
    people = [
        {"HisaPersonId": "person-1", "BarredFromRacing": "false",
         "IsCoveredPerson": "true", "IsUnregisteredPerson": "false",
         "DisplayName": "Attending veterinarian"},
        {"HisaPersonId": "person-2", "BarredFromRacing": "true",
         "IsCoveredPerson": "false", "IsUnregisteredPerson": "true",
         "DisplayName": "Responsible person"},
    ]
    people_path = tmp_path / "people.csv"
    avp_path = tmp_path / "avp.csv"
    output_path = tmp_path / "output" / "entities.csv"
    write_csv(people_path, sorted(generator.REQUIRED_PEOPLE_COLUMNS), people)
    write_csv(avp_path, generator.AVP_COLUMNS, rows)
    return people_path, avp_path, output_path, rows, linked


def test_generation_preserves_other_entities_and_vet_behavior(inputs):
    people, avp, output, original, linked = inputs
    assert generator.generate_entities(people, avp, output) == 4
    with output.open(encoding="utf-8", newline="") as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert rows[1:3] == original[1:3]
    assert json.loads(rows[0]["attributes"]) == {
        "is_covered_person": {"BOOL": True},
        "is_barred": {"BOOL": False},
        "is_registered": {"BOOL": True},
        "linked_integrators": linked,
    }
    assert json.loads(rows[0]["parents"]) == [
        {"entityId": "vet", "entityType": "hisa::group"}
    ]
    assert rows[3]["pk"] == "person-2"
    assert json.loads(rows[3]["parents"]) == []
    assert json.loads(rows[3]["attributes"]) == {
        "is_covered_person": {"BOOL": False},
        "is_barred": {"BOOL": True},
        "is_registered": {"BOOL": False},
        "linked_integrators": {"L": []},
    }
    with avp.open(encoding="utf-8-sig", newline="") as csv_file:
        assert list(csv.DictReader(csv_file)) == original


@pytest.mark.parametrize("csv_only", [False, True])
def test_main_uploads_complete_generated_csv_unless_csv_only(
    inputs, monkeypatch, csv_only
):
    people, avp, output, _, _ = inputs
    args = ["generator", str(people), str(avp), "-o", str(output)]
    if csv_only:
        args.append("--csv-only")
    monkeypatch.setattr(sys, "argv", args)
    session = MagicMock()
    session_factory = MagicMock(return_value=session)
    monkeypatch.setattr(generator.boto3, "Session", session_factory)
    generator.main()
    assert output.exists()
    if csv_only:
        session_factory.assert_not_called()
        return
    session_factory.assert_called_once_with(
        profile_name=population.PROFILE, region_name=population.REGION
    )
    session.resource.assert_called_once_with("dynamodb")
    session.resource.return_value.Table.assert_called_once_with(population.TABLE_NAME)
    table = session.resource.return_value.Table.return_value
    batch = table.batch_writer.return_value.__enter__.return_value
    actual = [call.kwargs["Item"] for call in batch.put_item.call_args_list]
    with output.open(encoding="utf-8", newline="") as csv_file:
        expected = [
            population.row_to_item(row, TypeDeserializer())
            for row in csv.DictReader(csv_file)
        ]
    assert actual == expected
    assert len(actual) == 4
    assert actual[0]["attributes"]["is_covered_person"] is True
    assert actual[0]["identifier"]["entityType"] == "hisa::person"
    assert actual[0]["parents"] == [{"entityId": "vet", "entityType": "hisa::group"}]


def test_missing_input_does_not_access_aws(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["generator", str(tmp_path / "missing.csv"),
                                    str(tmp_path / "missing-avp.csv")])
    session_factory = MagicMock()
    monkeypatch.setattr(generator.boto3, "Session", session_factory)
    with pytest.raises(FileNotFoundError):
        generator.main()
    session_factory.assert_not_called()


@pytest.mark.parametrize("mode", ["script", "module"])
def test_cli_csv_only_with_repository_defaults(mode, tmp_path):
    root = Path(__file__).resolve().parent.parent
    output = tmp_path / "generated.csv"
    if mode == "script":
        command = [str(root / "src" / "populate_avp_entities.py")]
        cwd = tmp_path
    else:
        command = ["-m", "src.populate_avp_entities"]
        cwd = root
    result = subprocess.run(
        [sys.executable, "-B", *command, "--csv-only", "-o", str(output)],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.exists()
    assert "Wrote" in result.stdout
    assert "Loaded" not in result.stdout


def test_aws_upload_failure_propagates(inputs, monkeypatch):
    people, avp, output, _, _ = inputs
    monkeypatch.setattr(sys, "argv", [
        "generator", str(people), str(avp), "-o", str(output)
    ])
    monkeypatch.setattr(generator.boto3, "Session", MagicMock())
    monkeypatch.setattr(generator, "populate_table",
                        MagicMock(side_effect=RuntimeError("upload failed")))
    with pytest.raises(RuntimeError, match="upload failed"):
        generator.main()
    assert output.exists()
