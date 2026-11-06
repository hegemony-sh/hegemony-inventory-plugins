#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Seeds contain an object id, a dotted path, then JSON, separated by newlines."""

import copy
import json
import string
import sys

import atheris

with atheris.instrument_imports(include=["hegemony_inventory_sdk"]):
    from hegemony_inventory_sdk.identifiers import validate_object_type_id
    from hegemony_inventory_sdk.mapping import (
        ObjectFieldMapping,
        RelationshipMapping,
        extract_path,
        map_record,
    )


@atheris.instrument_func
def test_one_input(data: bytes) -> None:
    raw = data[:16384].decode("utf-8", errors="replace")
    identifier, _, rest = raw.partition("\n")
    allowed_start = string.ascii_lowercase + string.digits
    valid = (
        1 <= len(identifier) <= 100
        and identifier[0] in allowed_start
        and all(c in allowed_start + "_.-" for c in identifier)
    )
    try:
        result = validate_object_type_id(identifier)
    except ValueError:
        assert not valid
    else:
        assert valid and result == identifier

    fdp = atheris.FuzzedDataProvider(data)
    identity = str(fdp.ConsumeIntInRange(0, 99999))
    name = fdp.ConsumeUnicodeNoSurrogates(64)
    parent = "parent-" + identity
    leaf = {"id": identity, "name": name, "value": [identity, None], "parent": parent}
    record = leaf
    segments = []
    for n in range(fdp.ConsumeIntInRange(1, 8)):
        key = "level" + str(n)
        record = {key: record, "unrelated": False}
        segments.insert(0, key)
    prefix = ".".join(segments) + "."
    before = copy.deepcopy(record)
    assert extract_path(record, prefix + "id") == identity
    assert extract_path(record, prefix + "missing") is None
    mapping = ObjectFieldMapping(
        object_type="device",
        identity=prefix + "id",
        name_field=prefix + "name",
        field_map={"value": prefix + "value"},
        relationships=(RelationshipMapping("parent", "site", prefix + "parent"),),
    )
    mapped = map_record(record, mapping, provider_id="synthetic-provider")
    assert mapped.external_id == identity
    assert mapped.name == (name.strip() or identity)
    assert mapped.attributes == {"value": leaf["value"]}
    assert mapped.relationships[0].target_external_id == parent
    assert mapped.object_type == "device" and mapped.provider_id == "synthetic-provider"
    assert record == before
    path, _, document = rest.partition("\n")
    try:
        record = json.loads(document)
    except (ValueError, RecursionError):
        return
    if isinstance(record, dict):
        extract_path(record, path)


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
