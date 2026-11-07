from repogym.env.docker import _parse_run, parse_size
from repogym.env.results import parse_junit, test_belongs_to_module
from repogym.models import RunStage, TestStatus

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="6">
<testcase classname="tests.test_a" name="test_ok" time="0.1"/>
<testcase classname="tests.test_a.TestC" name="test_bad[x-1]" time="0.1">
  <failure message="assert 1 == 2">def test():
E   assert 1 == 2
tests/test_a.py:3: AssertionError</failure>
</testcase>
<testcase classname="tests.test_a" name="test_setup" time="0"><error message="failed on setup">E   ValueError: boom</error></testcase>
<testcase classname="tests.test_a" name="test_skip" time="0"><skipped type="pytest.skip" message="no"/></testcase>
<testcase classname="tests.test_a" name="test_xf" time="0"><skipped type="pytest.xfail" message="known"/></testcase>
<testcase classname="" name="tests.test_broken" time="0"><error message="collection failure">E   ImportError: nope</error></testcase>
</testsuite></testsuites>
"""


def test_statuses():
    tests = parse_junit(JUNIT).tests
    assert tests == {
        "tests.test_a::test_ok": TestStatus.PASSED,
        "tests.test_a.TestC::test_bad[x-1]": TestStatus.FAILED,
        "tests.test_a::test_setup": TestStatus.ERROR,
        "tests.test_a::test_skip": TestStatus.SKIPPED,
        "tests.test_a::test_xf": TestStatus.XFAILED,
    }


def test_collection_errors_are_separate_from_tests():
    assert parse_junit(JUNIT).collection_errors == ["tests.test_broken"]


def test_first_error_is_the_exception_line():
    assert parse_junit(JUNIT).first_error == "E   assert 1 == 2"


def test_duplicate_ids_keep_the_worst_status():
    xml = (
        '<testsuite><testcase classname="m" name="t"/>'
        '<testcase classname="m" name="t"><error message="failed on teardown"/></testcase>'
        '<testcase classname="m" name="t"/></testsuite>'
    )
    assert parse_junit(xml).tests == {"m::t": TestStatus.ERROR}


def test_empty_xml_means_no_results():
    assert parse_junit("").tests == {}


def test_module_membership_does_not_match_name_prefixes():
    assert test_belongs_to_module("tests.test_a::t", "tests.test_a")
    assert test_belongs_to_module("tests.test_a.TestC::t", "tests.test_a")
    assert not test_belongs_to_module("tests.test_ab::t", "tests.test_a")


def test_parse_run_extracts_results_from_last_stdout_line():
    payload = '{"stage": "completed", "junit_xml": "<testsuite/>", "duration": 1.5, "exit_code": 0}'
    run = _parse_run(0, "stray print\n" + payload + "\n", "")
    assert run.stage is RunStage.COMPLETED
    assert run.duration_seconds == 1.5


def test_parse_run_reports_setup_failure():
    run = _parse_run(0, '{"stage": "install_failed", "detail": "no backend"}', "")
    assert run.stage is RunStage.INSTALL_FAILED and run.detail == "no backend"


def test_parse_run_treats_exit_137_as_oom():
    assert _parse_run(137, "", "").stage is RunStage.OOM


def test_parse_run_garbage_output_is_no_results():
    assert _parse_run(1, "Traceback", "").stage is RunStage.NO_RESULTS


def test_parse_size():
    assert parse_size("4.698GB") == 4_698_000_000
    assert parse_size("0B") == 0
    assert parse_size("323.8MB") == 323_800_000
    assert parse_size("12kB") == 12_000
