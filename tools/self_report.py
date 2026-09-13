#!/usr/bin/env python3
"""Generate SELF-REPORT.md: PASS / FAIL / NO TEST for every acceptance criterion.

PRD Section 14 requires a PASS/FAIL report per acceptance criterion. Writing one
by hand would be a claim; this measures it. Tests are named after the criterion
they prove (``test_us_t06_ac2_net_cap_scales_dominant_side``), so the mapping is
read out of the suite itself, and a criterion with no test named for it is
reported as NO TEST rather than quietly omitted.

Its limit is worth stating plainly, because it is easy to over-read: this tool
checks that a *test named for* a criterion passes. It cannot check that the test
proves the criterion — a test can carry the right name and assert something
weaker, or assert whatever the code happens to produce. Only reading the test
settles that, which is what ``docs/COVERAGE.md`` does. Where the two disagree,
the audit is right.

    python tools/self_report.py [--out SELF-REPORT.md] [--junit report.xml]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parent.parent
TEST_RE = re.compile(r"us_t(\d{1,2})_ac(\d{1,2})", re.IGNORECASE)

#: The PRD's user stories, their titles and how many acceptance criteria each
#: has (Section 8). Hard-coded because it is the specification: a criterion that
#: nobody wrote a test for must still appear in the report.
STORIES: dict[str, tuple[str, int]] = {
    "T01": ("Shared-layer integration", 4),
    "T02": ("Universe selection and history", 4),
    "T03": ("Daily bars and funding data", 4),
    "T04": ("Trend signal engine", 5),
    "T05": ("Volatility and covariance estimators", 3),
    "T06": ("Sizing, scaling, caps", 4),
    "T07": ("Funding overlay", 2),
    "T08": ("Drawdown governor", 4),
    "T09": ("Target computation and hysteresis", 4),
    "T10": ("Rebalance executor", 6),
    "T11": ("Drift monitor, status watch, funding-interval watch", 4),
    "T12": ("Exposure and margin supervisor", 5),
    "T13": ("Kill rules and safe mode", 4),
    "T14": ("Per-symbol and long/short attribution", 4),
    "T15": ("TREND metrics on top of the shared set", 3),
    "T16": ("Point-in-time backtester and live tracking error", 6),
    "T17": ("Reports and alerts", 4),
    "T18": ("Dashboard pages", 8),
    "T19": ("Deployment beside CARRY and cost guard", 4),
}


@dataclass
class Outcome:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.failed:
            return "FAIL"
        if self.passed:
            return "PASS"
        if self.skipped:
            return "SKIP"
        return "NO TEST"

    @property
    def count(self) -> int:
        return len(self.passed) + len(self.failed) + len(self.skipped)


def run_pytest(junit: Path, selection: list[str] | None = None) -> int:
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=no", f"--junit-xml={junit}"]
    cmd += selection or ["tests"]
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def parse_junit(path: Path) -> dict[str, Outcome]:
    """Group every test by the ``us_tNN_acM`` key in its name."""
    by_key: dict[str, Outcome] = defaultdict(Outcome)
    root = ElementTree.parse(path).getroot()
    for case in root.iter("testcase"):
        name = case.get("name", "")
        classname = case.get("classname", "")
        full = f"{classname}::{name}"
        match = TEST_RE.search(name)
        if match is None:
            continue
        key = f"T{int(match.group(1)):02d}.{int(match.group(2))}"
        outcome = by_key[key]
        if case.find("failure") is not None or case.find("error") is not None:
            outcome.failed.append(full)
        elif case.find("skipped") is not None:
            outcome.skipped.append(full)
        else:
            outcome.passed.append(full)
    return by_key


def totals(path: Path) -> dict[str, int]:
    root = ElementTree.parse(path).getroot()
    suite = root if root.tag == "testsuite" else next(iter(root), root)
    return {
        "tests": int(suite.get("tests", 0)),
        "failures": int(suite.get("failures", 0)),
        "errors": int(suite.get("errors", 0)),
        "skipped": int(suite.get("skipped", 0)),
        "time": float(suite.get("time", 0.0)),
    }


def render(by_key: dict[str, Outcome], suite: dict[str, int]) -> str:
    rows: list[str] = []
    tally: dict[str, int] = defaultdict(int)
    for story, (title, n_criteria) in STORIES.items():
        rows.append(f"\n### US-{story} — {title}\n")
        rows.append("| AC | Verdict | Tests | Failing |")
        rows.append("|---|---|---|---|")
        for i in range(1, n_criteria + 1):
            key = f"{story}.{i}"
            outcome = by_key.get(key, Outcome())
            tally[outcome.verdict] += 1
            failing = ", ".join(Path(f).name for f in outcome.failed[:3]) or ""
            rows.append(f"| AC {i} | **{outcome.verdict}** | {outcome.count} | {failing} |")

    graded = sum(tally.values())
    header = [
        "# TREND — self-verification report",
        "",
        "Generated by `python tools/self_report.py`.",
        "",
        "> **What this report does and does not tell you.** Each row answers one narrow",
        "> question: *is there a passing test whose name claims this criterion?* It is",
        "> measured by running the suite rather than asserted, and a criterion with no test",
        "> named for it is reported as **NO TEST** rather than quietly omitted. But a name is",
        "> not a proof: a test can be named for a criterion and assert something weaker than",
        "> the criterion, or assert whatever the code happens to produce. **PASS here means",
        '> "a test named for it passes", not "the criterion is met".**',
        ">",
        "> `docs/COVERAGE.md` is the audit that actually opens the tests, and it disagrees",
        "> with this table in a number of places. Where they differ, believe `COVERAGE.md`.",
        "",
        "## Suite",
        "",
        f"- **{suite['tests']}** tests, **{suite['failures']}** failures, "
        f"**{suite['errors']}** errors, **{suite['skipped']}** skipped",
        f"- wall clock **{suite['time']:.0f}s**",
        "- every test is offline and deterministic (FakeGateway + FakeClock + in-memory SQLite)",
        "",
        "## Acceptance criteria",
        "",
        "| Verdict | Criteria |",
        "|---|---|",
        f"| PASS | {tally.get('PASS', 0)} / {graded} |",
        f"| FAIL | {tally.get('FAIL', 0)} / {graded} |",
        f"| SKIP | {tally.get('SKIP', 0)} / {graded} |",
        f"| NO TEST | {tally.get('NO TEST', 0)} / {graded} |",
    ]
    return "\n".join(header + rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="SELF-REPORT.md")
    parser.add_argument("--junit", default="build/report.xml")
    parser.add_argument("--no-run", action="store_true", help="reuse an existing junit file")
    parser.add_argument("selection", nargs="*", help="pytest paths (default: tests)")
    args = parser.parse_args(argv)

    junit = ROOT / args.junit
    junit.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_run:
        run_pytest(junit, args.selection or None)
    if not junit.exists():
        print(f"no junit report at {junit}", file=sys.stderr)
        return 2

    by_key = parse_junit(junit)
    report = render(by_key, totals(junit))
    (ROOT / args.out).write_text(report, encoding="utf-8")
    print(f"wrote {args.out}")

    failures = sum(1 for o in by_key.values() if o.verdict == "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
