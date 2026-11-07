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


TIMEOUT_JUNIT = """<testsuite>
<testcase classname="tests.t" name="test_hang_v2" time="30.01">
  <failure message="Failed: Timeout (&gt;30.0s) from pytest-timeout.">E   Failed: Timeout</failure>
</testcase>
<testcase classname="tests.t" name="test_hang_v1" time="30.0">
  <failure message="Failed: Timeout &gt;30.0s">...</failure>
</testcase>
<testcase classname="tests.t" name="test_fixture_hang" time="30.0">
  <error message='failed on setup with "Failed: Timeout (&gt;30.0s) from pytest-timeout."'>.</error>
</testcase>
<testcase classname="tests.t" name="test_mentions_timeout" time="0.2">
  <failure message="assert 'Timeout' in output">E   AssertionError</failure>
</testcase>
<testcase classname="tests.t" name="test_ok" time="1.5"/>
</testsuite>"""


def test_per_test_timeouts_are_failures_in_any_phase():
    parsed = parse_junit(TIMEOUT_JUNIT)
    assert parsed.timed_out == [
        "tests.t::test_hang_v2",
        "tests.t::test_hang_v1",
        "tests.t::test_fixture_hang",
    ]
    assert parsed.tests["tests.t::test_fixture_hang"] is TestStatus.FAILED
    assert parsed.tests["tests.t::test_hang_v2"] is TestStatus.FAILED


def test_a_failure_merely_mentioning_timeout_is_not_a_timeout():
    parsed = parse_junit(TIMEOUT_JUNIT)
    assert "tests.t::test_mentions_timeout" not in parsed.timed_out
    assert parsed.tests["tests.t::test_mentions_timeout"] is TestStatus.FAILED


def test_per_test_durations_are_recorded():
    assert parse_junit(TIMEOUT_JUNIT).seconds["tests.t::test_ok"] == 1.5


def test_timeout_message_patterns():
    from repogym.env.results import is_timeout_message

    assert is_timeout_message("Failed: Timeout (>30.0s) from pytest-timeout.")
    assert is_timeout_message("Failed: Timeout >5.0s")
    assert not is_timeout_message("TimeoutError: socket timed out")
    assert not is_timeout_message("assert 'Timeout' in output")


def test_parse_buildx_du_total():
    from repogym.env.docker import parse_buildx_du_total

    text = "ID  RECLAIMABLE  SIZE\nabc true 1.2GB\nShared:\t0B\nPrivate:\t1.2GB\nTotal:\t1.25GB\n"
    assert parse_buildx_du_total(text) == 1_250_000_000
    assert parse_buildx_du_total("") == 0


def test_repogym_image_bytes_counts_shared_layers_once():
    from repogym.env.docker import _repogym_image_bytes

    rows = [
        {"UniqueSize": "100MB", "SharedSize": "300MB"},
        {"UniqueSize": "50MB", "SharedSize": "300MB"},
    ]
    assert _repogym_image_bytes(rows) == 450_000_000


def test_build_uses_the_repogym_builder_and_apt_packages():
    from repogym.config import REPOS
    from repogym.env.docker import build_command

    command = build_command(REPOS["click"], "/ctx")
    assert command[:3] == ["docker", "buildx", "build"]
    assert command[command.index("--builder") + 1] == "repogym-builder"
    assert "--load" in command
    assert "APT_PACKAGES=less" in command
    assert "APT_PACKAGES=" in build_command(REPOS["attrs"], "/ctx")
