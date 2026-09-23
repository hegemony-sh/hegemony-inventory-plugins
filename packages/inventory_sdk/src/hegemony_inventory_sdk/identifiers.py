# SPDX-FileCopyrightText: 2025-2026 Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stable identifiers shared by plugin declarations and platform routes."""

from __future__ import annotations

import re

OBJECT_TYPE_ID_MAX_LENGTH = 100
OBJECT_TYPE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


def validate_object_type_id(value: str, *, field_name: str = "object type id") -> str:
    """Return a valid object-type id or raise a useful contract error."""
    if not isinstance(value, str) or not OBJECT_TYPE_ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must be 1-{OBJECT_TYPE_ID_MAX_LENGTH} lowercase letters, "
            "digits, dots, underscores, or hyphens, beginning with a letter or digit"
        )
    return value


__all__ = ["OBJECT_TYPE_ID_MAX_LENGTH", "OBJECT_TYPE_ID_PATTERN", "validate_object_type_id"]
