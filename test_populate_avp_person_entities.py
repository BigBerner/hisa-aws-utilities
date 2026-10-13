# -*- coding: utf-8 -*-
"""Unit tests for populate_avp_person_entities.py."""

import csv
import json
from pathlib import Path

import pytest
from boto3.dynamodb.types import TypeDeserializer

from populate_avp_person_entities import (
    build_group_entity,
    build_person_entity,
    entity_to_csv_row,
    extract_usergroups_from_results,
    generate_usergroups_template,
    load_people,
    load_user_groups,
    parse_linked_integrators,
    parse_raw_auth_group_value,
    to_bool,
    update_avp_entities_csv,
)


def test_to_bool():
    assert to_bool(True) is True
    assert to_bool(False) is False
    assert to_bool("True") is True
    assert to_bool("true") is True
    assert to_bool("1") is True
    assert to_bool("yes") is True
    assert to_bool("False") is False
    assert to_bool("false") is False
    assert to_bool("0") is False
    assert to_bool("no") is False
    assert to_bool("", default=True) is False
    assert to_bool(None, default=True) is True


def test_parse_raw_auth_group_value():
    # Comma-separated string
    assert parse_raw_auth_group_value("CoveredPerson, ResponsiblePerson") == [
        "CoveredPerson",
        "ResponsiblePerson",
    ]

    # JSON array of strings
    assert parse_raw_auth_group_value('["CoveredPerson", "vet"]') == [
        "CoveredPerson",
        "vet",
    ]

    # DynamoDB JSON export format (as found in results (11).csv)
    ddb_json = '[{"M": {"AuthGroupId": {"S": "CoveredPerson"}}}, {"M": {"AuthGroupId": {"S": "AttendingVet"}}}]'
    assert parse_raw_auth_group_value(ddb_json) == [
        "CoveredPerson",
        "AttendingVet",
    ]

    # Empty inputs
    assert parse_raw_auth_group_value("") == []
    assert parse_raw_auth_group_value(None) == []


def test_parse_linked_integrators():
    # Empty
    assert parse_linked_integrators(None) == []
    assert parse_linked_integrators("") == []

    # Comma-separated string
    result = parse_linked_integrators("INT001, INT002", namespace="hisa")
    assert result == [
        {
            "entityIdentifier": {
                "entityId": "INT001",
                "entityType": "hisa::integrator",
            }
        },
        {
            "entityIdentifier": {
                "entityId": "INT002",
                "entityType": "hisa::integrator",
            }
        },
    ]

    # JSON list
    result_json = parse_linked_integrators('["INT001"]', namespace="hisa")
    assert result_json == [
        {
            "entityIdentifier": {
                "entityId": "INT001",
                "entityType": "hisa::integrator",
            }
        }
    ]


def test_load_user_groups_simple_and_results(tmp_path: Path):
    # Test simple CSV
    simple_csv = tmp_path / "simple_groups.csv"
    simple_csv.write_text(
        "PersonId,AuthGroupId\nP100,CoveredPerson\nP100,vet\nP200,DesignatedOwner\n",
        encoding="utf-8",
    )
    loaded = load_user_groups(simple_csv)
    assert loaded["P100"] == ["CoveredPerson", "vet"]
    assert loaded["P200"] == ["DesignatedOwner"]

    # Test results format CSV with DynamoDB JSON
    results_csv = tmp_path / "results_mock.csv"
    with results_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["PersonId", "ObjectId", "AuthGroupIds"])
        writer.writerow(["P300", "General", '[{"M": {"AuthGroupId": {"S": "CoveredPerson"}}}]'])
        writer.writerow(["P400", "Specific", '[{"M": {"AuthGroupId": {"S": "ResponsiblePerson"}}}]'])

    loaded_res = load_user_groups(results_csv)
    assert loaded_res["P300"] == ["CoveredPerson"]
    assert loaded_res["P400"] == ["ResponsiblePerson"]

    # Test filter by ObjectId
    filtered = load_user_groups(results_csv, object_id_filter="General")
    assert "P300" in filtered
    assert "P400" not in filtered


def test_load_people(tmp_path: Path):
    people_csv = tmp_path / "people_mock.csv"
    people_csv.write_text(
        "HisaPersonId,BarredFromRacing,DisplayName,IsCoveredPerson,IsUnregisteredPerson\n"
        "P1,false,John Doe,true,false\n"
        "P2,true,Jane Smith,false,true\n",
        encoding="utf-8",
    )
    people = load_people(people_csv)
    assert len(people) == 2
    assert people[0]["person_id"] == "P1"
    assert people[0]["is_covered_person"] == "true"
    assert people[0]["is_barred"] == "false"
    assert people[1]["person_id"] == "P2"
    assert people[1]["is_barred"] == "true"


def test_build_person_entity_schema():
    person = {
        "person_id": "P703300001",
        "is_covered_person": "true",
        "is_barred": "false",
        "is_unregistered": "false",
        "is_registered": None,
        "linked_integrators": "INT1",
    }
    groups = ["CoveredPerson", "ResponsiblePerson"]

    entity = build_person_entity(person, groups, namespace="hisa")

    # Check root keys
    assert entity["pk"] == "P703300001"
    assert entity["sk"] == "#"

    # Check identifier
    assert entity["identifier"] == {
        "entityId": "P703300001",
        "entityType": "hisa::person",
    }

    # Check parents (memberOfTypes: ["group"])
    assert entity["parents"] == [
        {"entityId": "CoveredPerson", "entityType": "hisa::group"},
        {"entityId": "ResponsiblePerson", "entityType": "hisa::group"},
    ]

    # Check attributes according to schema
    attrs = entity["attributes"]
    assert attrs["is_covered_person"] is True
    assert attrs["is_barred"] is False
    assert attrs["is_registered"] is True
    assert attrs["linked_integrators"] == [
        {
            "entityIdentifier": {
                "entityId": "INT1",
                "entityType": "hisa::integrator",
            }
        }
    ]


def test_entity_to_csv_row_and_deserialization():
    person = {
        "person_id": "P703300001",
        "is_covered_person": "true",
        "is_barred": "false",
        "is_unregistered": "false",
        "is_registered": None,
        "linked_integrators": None,
    }
    groups = ["CoveredPerson"]
    entity = build_person_entity(person, groups, namespace="hisa")

    row = entity_to_csv_row(entity)
    assert row["pk"] == "P703300001"
    assert row["sk"] == "#"

    # Simulate what populate_avp_entities.py does: deserialize_map
    deserializer = TypeDeserializer()
    loaded_attrs = {
        k: deserializer.deserialize(v) for k, v in json.loads(row["attributes"]).items()
    }
    loaded_ident = {
        k: deserializer.deserialize(v) for k, v in json.loads(row["identifier"]).items()
    }
    loaded_parents = json.loads(row["parents"])

    assert loaded_attrs["is_covered_person"] is True
    assert loaded_attrs["is_barred"] is False
    assert loaded_attrs["is_registered"] is True
    assert loaded_attrs["linked_integrators"] == []
    assert loaded_ident["entityId"] == "P703300001"
    assert loaded_ident["entityType"] == "hisa::person"
    assert loaded_parents == [{"entityId": "CoveredPerson", "entityType": "hisa::group"}]


def test_update_avp_entities_csv_preserves_non_person_rows(tmp_path: Path):
    csv_file = tmp_path / "avp-entities.csv"

    # Create initial CSV with a horse, an integrator, and an old person record
    initial_rows = [
        {
            "pk": "H000000199",
            "sk": "#",
            "attributes": '{"is_active": {"BOOL": true}}',
            "identifier": '{"entityId": {"S": "H000000199"}, "entityType": {"S": "hisa::horse"}}',
            "parents": "[]",
        },
        {
            "pk": "3RD000000001",
            "sk": "#",
            "attributes": '{"name": {"S": "Integrator1"}}',
            "identifier": '{"entityId": {"S": "3RD000000001"}, "entityType": {"S": "hisa::integrator"}}',
            "parents": "[]",
        },
        {
            "pk": "P100",
            "sk": "#",
            "attributes": '{"is_covered_person": {"BOOL": false}}',
            "identifier": '{"entityId": {"S": "P100"}, "entityType": {"S": "hisa::person"}}',
            "parents": "[]",
        },
    ]

    with csv_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pk", "sk", "attributes", "identifier", "parents"])
        writer.writeheader()
        writer.writerows(initial_rows)

    # Build updated P100 and new P200
    p100_updated = build_person_entity(
        {"person_id": "P100", "is_covered_person": True, "is_barred": False},
        ["CoveredPerson"],
    )
    p200_new = build_person_entity(
        {"person_id": "P200", "is_covered_person": False, "is_barred": True},
        ["ResponsiblePerson"],
    )

    updated, appended = update_avp_entities_csv(csv_file, [p100_updated, p200_new])
    assert updated == 1
    assert appended == 1

    # Verify rows in CSV
    with csv_file.open("r", encoding="utf-8", newline="") as f:
        reader = list(csv.DictReader(f))

    assert len(reader) == 4
    pks = [r["pk"] for r in reader]
    assert pks == ["H000000199", "3RD000000001", "P100", "P200"]

    # Verify P100 attributes were updated
    p100_row = next(r for r in reader if r["pk"] == "P100")
    deserializer = TypeDeserializer()
    attrs = {k: deserializer.deserialize(v) for k, v in json.loads(p100_row["attributes"]).items()}
    assert attrs["is_covered_person"] is True
    assert json.loads(p100_row["parents"]) == [{"entityId": "CoveredPerson", "entityType": "hisa::group"}]


def test_extract_usergroups_from_results(tmp_path: Path):
    results_csv = tmp_path / "results_mock.csv"
    with results_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["PersonId", "AuthGroupIds"])
        writer.writerow(["P10", '[{"M": {"AuthGroupId": {"S": "GroupA"}}}]'])

    output_csv = tmp_path / "extracted.csv"
    count = extract_usergroups_from_results(results_csv, output_csv)
    assert count == 1
    content = output_csv.read_text(encoding="utf-8")
    assert "P10,GroupA" in content


def test_generate_usergroups_template(tmp_path: Path):
    people_csv = tmp_path / "people_mock.csv"
    people_csv.write_text("HisaPersonId\nP1\nP2\n", encoding="utf-8")
    output_csv = tmp_path / "template.csv"
    count = generate_usergroups_template(people_csv, output_csv)
    assert count == 2
    content = output_csv.read_text(encoding="utf-8")
    assert "P1,CoveredPerson" in content
    assert "P2,CoveredPerson" in content


def test_real_files_parsing_and_end_to_end(tmp_path: Path):
    root = Path(__file__).parent
    people_file = root / "people.csv"
    results_file = root / "results (11).csv"
    avp_file = root / "avp-entities.csv"

    if not (people_file.exists() and results_file.exists()):
        pytest.skip("Workspace data files not found")

    people = load_people(people_file)
    assert len(people) > 0

    groups = load_user_groups(results_file)
    assert len(groups) > 0

    # Test end-to-end entity generation
    entities = []
    for p in people:
        pid = p["person_id"]
        grps = groups.get(pid, [])
        entity = build_person_entity(p, grps, namespace="hisa")
        entities.append(entity)

    assert len(entities) == len(people)

    # Test updating a copy of avp-entities.csv
    temp_avp = tmp_path / "avp-entities-test.csv"
    if avp_file.exists():
        temp_avp.write_bytes(avp_file.read_bytes())

    updated, appended = update_avp_entities_csv(temp_avp, entities)
    assert updated + appended == len(entities)

    # Verify that existing non-person rows are still present in temp_avp
    with temp_avp.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    pks = {r["pk"] for r in rows}
    assert "H000000199" in pks  # Horse entity preserved
    assert "3RD000000001" in pks  # Integrator entity preserved

