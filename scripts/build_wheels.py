# SPDX-FileCopyrightText: 2025-2026 Rexonix s.r.o.
# SPDX-FileContributor: Jakub Trávník <jakub.travnik@gmail.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Build a wheel and an sdist for every package into the repository-level dist directory.

The name is historical: the release publishes both, the wheels and the sdists.
"""

from __future__ import annotations

import subprocess

from package_paths import PACKAGE_DIRS, ROOT


def main() -> None:
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    for pattern in ("*.whl", "*.tar.gz"):
        for old in dist.glob(pattern):
            old.unlink()

    for package_dir in PACKAGE_DIRS:
        subprocess.run(
            ["uv", "build", "--wheel", "--sdist", "--out-dir", str(dist), str(package_dir)],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
