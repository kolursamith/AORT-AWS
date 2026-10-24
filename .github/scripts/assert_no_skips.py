"""Fail CI when any test was skipped (or nothing ran) in a pytest JUnit XML report.

Every AORTS test is mandatory in CI: the research extra (SimPy) and PostgreSQL are installed,
so an ``importorskip`` or a DB skip means the environment is wrong, not that the test is
optional. Usage: python .github/scripts/assert_no_skips.py junit.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # noqa: S405 - parsing our own pytest output


def main(path: str) -> int:
    root = ET.parse(path).getroot()  # noqa: S314 - trusted local file written by pytest
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(s.get("tests", 0)) for s in suites)
    skipped = []
    for case in root.iter("testcase"):
        node = case.find("skipped")
        if node is not None:
            name = f"{case.get('classname')}::{case.get('name')}"
            skipped.append(f"{name}: {node.get('message', '')}")
    print(f"{tests} tests, {len(skipped)} skipped")
    for line in skipped:
        print(f"SKIPPED {line}")
    if tests == 0 or skipped:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml"))
