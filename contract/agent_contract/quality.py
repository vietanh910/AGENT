"""Extract quality decisions from Contract TaskResult outputs and reports."""

from typing import Any


def quality_verdict(value: Any) -> str | None:
    verdicts = []
    if isinstance(value, dict):
        if str(value.get("severity", "")).upper() in {"HIGH", "CRITICAL"}:
            return "FAIL"
        verdict = str(value.get("verdict", "")).upper()
        if verdict in {"PASS", "FAIL"}:
            verdicts.append(verdict)
        for child in value.values():
            if isinstance(child, (dict, list)) and (found := quality_verdict(child)):
                verdicts.append(found)
    elif isinstance(value, list):
        verdicts = [found for child in value if (found := quality_verdict(child))]
    return "FAIL" if "FAIL" in verdicts else ("PASS" if "PASS" in verdicts else None)
