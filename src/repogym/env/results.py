"""Parse pytest JUnit XML into per-test results."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from repogym.models import TestStatus

# When one id is reported twice (pytest can do this for a teardown error), the worst one wins.
_SEVERITY = {
    TestStatus.ERROR: 4,
    TestStatus.FAILED: 3,
    TestStatus.XFAILED: 2,
    TestStatus.SKIPPED: 1,
    TestStatus.PASSED: 0,
}
_MESSAGE_LIMIT = 300
# pytest-timeout fails the test with e.g. "Failed: Timeout (>30.0s) from pytest-timeout." (2.x)
# or "Failed: Timeout >30.0s" (1.x), during the call or inside a fixture.
_TIMEOUT_MESSAGE = re.compile(r"Timeout \(?>\s*[\d.]+s\)?(?: from pytest-timeout)?")


@dataclass
class ParsedResults:
    tests: dict[str, TestStatus] = field(default_factory=dict)
    # Dotted module names that failed to import, e.g. "tests.test_core".
    collection_errors: list[str] = field(default_factory=list)
    first_error: str = ""
    seconds: dict[str, float] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)


def is_timeout_message(message: str) -> bool:
    return bool(_TIMEOUT_MESSAGE.search(message))


def _status_of(case: ET.Element) -> tuple[TestStatus, str]:
    error = case.find("error")
    if error is not None:
        return TestStatus.ERROR, error.get("message", "")
    failure = case.find("failure")
    if failure is not None:
        return TestStatus.FAILED, failure.get("message", "")
    skipped = case.find("skipped")
    if skipped is not None:
        kind = TestStatus.XFAILED if skipped.get("type") == "pytest.xfail" else TestStatus.SKIPPED
        return kind, ""
    return TestStatus.PASSED, ""


def _is_collection_error(case: ET.Element) -> bool:
    error = case.find("error")
    if error is None:
        return False
    return (error.get("message") or "").startswith("collection failure")


def _signature(case: ET.Element, message: str) -> str:
    """Prefer the last line of the traceback (the exception) over a generic message."""
    node = case.find("error")
    if node is None:
        node = case.find("failure")
    body = (node.text or "") if node is not None else ""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("E ")]
    text = errors[-1] if errors else (lines[-1] if lines else message)
    return text[:_MESSAGE_LIMIT]


def parse_junit(xml_text: str) -> ParsedResults:
    """Parse JUnit XML. Test ids are ``dotted.module.Class::test_name``."""
    results = ParsedResults()
    if not xml_text.strip():
        return results
    root = ET.fromstring(xml_text)
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        status, message = _status_of(case)
        if status in (TestStatus.ERROR, TestStatus.FAILED) and not results.first_error:
            results.first_error = _signature(case, message)
        if not classname and _is_collection_error(case):
            results.collection_errors.append(name)
            continue
        test_id = f"{classname}::{name}"
        if status in (TestStatus.ERROR, TestStatus.FAILED) and is_timeout_message(message):
            # A test that hangs is a failing test, wherever in its lifecycle it hung.
            status = TestStatus.FAILED
            if test_id not in results.timed_out:
                results.timed_out.append(test_id)
        try:
            seconds = float(case.get("time", "0") or 0)
        except ValueError:
            seconds = 0.0
        results.seconds[test_id] = results.seconds.get(test_id, 0.0) + seconds
        previous = results.tests.get(test_id)
        if previous is None or _SEVERITY[status] > _SEVERITY[previous]:
            results.tests[test_id] = status
    return results


def test_belongs_to_module(test_id: str, module: str) -> bool:
    """True if ``test_id`` comes from the dotted ``module`` (including classes inside it)."""
    classname = test_id.split("::", 1)[0]
    return classname == module or classname.startswith(module + ".")


test_belongs_to_module.__test__ = False  # type: ignore[attr-defined]
