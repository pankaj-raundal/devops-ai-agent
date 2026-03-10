"""Tests for the test runner."""

from src.reviewer.test_runner import TestResult, TestSummary


def test_summary_all_passed():
    s = TestSummary(results=[
        TestResult("phpunit", True, "OK", 0),
        TestResult("phpcs", True, "OK", 0),
    ])
    assert s.all_passed is True


def test_summary_with_failure():
    s = TestSummary(results=[
        TestResult("phpunit", True, "OK", 0),
        TestResult("phpcs", False, "Error on line 5", 1),
    ])
    assert s.all_passed is False
    text = s.summary_text()
    assert "[FAIL] phpcs" in text
    assert "Error on line 5" in text
