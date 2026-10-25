#!/usr/bin/env python3
"""CI helper for the Android workflow.

Per-API mode (emulator job):
    android_test_summary.py --api 34 --results <androidTest-results dir> --out result.json
  Parses the JUnit XML written by connectedDebugAndroidTest, prints a GitHub
  ::notice (or ::error) annotation and writes a small JSON summary.

Aggregate mode (publish job):
    android_test_summary.py --aggregate <artifacts dir> --emulator-job <result> \
        --sha256 SHA256SUMS.txt --commit <sha> --out notes.md
  Collects every emulator-result-*.json into release notes.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import xml.etree.ElementTree as ET  # noqa: S405 - parsing our own CI output


def summarize(api: str, results_dir: str) -> dict:
    total = failures = errors = skipped = 0
    failed: list[str] = []
    device = ""
    for path in glob.glob(os.path.join(results_dir, "**", "*.xml"), recursive=True):
        try:
            root = ET.parse(path).getroot()  # noqa: S314
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for suite in suites:
            for prop in suite.iter("property"):
                if prop.get("name") == "device":
                    device = prop.get("value", device)
            for case in suite.iter("testcase"):
                total += 1
                name = f"{case.get('classname', '').rsplit('.', 1)[-1]}.{case.get('name')}"
                if case.find("failure") is not None:
                    failures += 1
                    failed.append(name)
                elif case.find("error") is not None:
                    errors += 1
                    failed.append(name)
                elif case.find("skipped") is not None:
                    skipped += 1
    passed = total - failures - errors - skipped
    status = "pass" if total > 0 and not failed else ("no-results" if total == 0 else "fail")
    return {
        "api": api,
        "device": device,
        "total": total,
        "passed": passed,
        "failed": failures + errors,
        "skipped": skipped,
        "failed_tests": failed[:50],
        "status": status,
    }


def per_api(args: argparse.Namespace) -> int:
    result = summarize(args.api, args.results)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    line = (
        f"API {result['api']}: {result['status']} - {result['passed']}/{result['total']} passed, "
        f"{result['failed']} failed, {result['skipped']} skipped"
    )
    kind = "notice" if result["status"] == "pass" else "error"
    print(f"::{kind} title=Emulator API {result['api']}::{line}")
    for name in result["failed_tests"][:20]:
        print(f"::error title=API {result['api']} failed test::{name}")
    return 0


def aggregate(args: argparse.Namespace) -> int:
    rows = []
    for path in sorted(glob.glob(os.path.join(args.aggregate, "**", "emulator-result-*.json"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            rows.append(json.load(fh))
    rows.sort(key=lambda r: int(r.get("api", 0)))
    lines = [
        "Sideload preview of HeyBuddy for Android. Not on Google Play.",
        "",
        "**Signing:** the release APK is signed with a throwaway key generated inside this CI run. "
        "It is not a Play Store key, so each preview can have a different certificate: uninstall the "
        "previous preview before installing a new one if Android refuses the update.",
        "",
        f"Commit: `{args.commit}`",
        "",
        "## Emulator results (connectedDebugAndroidTest)",
        "",
        "| API | Result | Passed | Failed | Skipped |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['api']} | {r['status']} | {r['passed']}/{r['total']} | {r['failed']} | {r['skipped']} |")
    if not rows:
        lines.append("| - | no emulator results were produced | | | |")
    if args.emulator_job != "success":
        lines += ["", f"Emulator matrix job result: **{args.emulator_job}** (at least one API level did not pass; see table)."]
    lines += [
        "",
        "Verified only in CI (unit tests, lint, emulator instrumentation). Not verified on a real phone, "
        "with live AI/voice providers, or for battery and latency.",
        "",
        "## SHA-256",
        "",
        "```",
        open(args.sha256, encoding="utf-8").read().strip(),
        "```",
    ]
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api")
    parser.add_argument("--results")
    parser.add_argument("--aggregate")
    parser.add_argument("--emulator-job", default="unknown")
    parser.add_argument("--sha256")
    parser.add_argument("--commit", default="")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    return aggregate(args) if args.aggregate else per_api(args)


if __name__ == "__main__":
    raise SystemExit(main())
