#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fuzz the inventory SDK's object-type ids and record paths.

An object-type id from a plugin or a URL is valid or refused with ValueError,
and a dotted path into a source record (JSON from the inventory system) reads
a value or None, never an exception.
"""

import json
import sys

import atheris

with atheris.instrument_imports():
    from hegemony_inventory_sdk.identifiers import (
        OBJECT_TYPE_ID_MAX_LENGTH,
        validate_object_type_id,
    )
    from hegemony_inventory_sdk.mapping import extract_path


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    object_type = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 128))
    try:
        assert validate_object_type_id(object_type) == object_type
        assert 1 <= len(object_type) <= OBJECT_TYPE_ID_MAX_LENGTH
    except ValueError:
        pass
    path = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 128))
    try:
        record = json.loads(fdp.ConsumeUnicodeNoSurrogates(4096))
    except (ValueError, RecursionError):
        # Not a document, or nested deeper than json.loads itself handles.
        return
    if isinstance(record, dict):
        extract_path(record, path)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
