from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


VERIFIER_INFRA_EXCEPTION_TYPES = frozenset(
    {
        "VerifierInfrastructureError",
        "VerifierTimeoutError",
        "RewardFileNotFoundError",
        "RewardFileEmptyError",
        "VerifierOutputParseError",
        "DownloadVerifierDirError",
        "AddTestsDirError",
    }
)


class VerifierEvidenceError(RuntimeError):
    pass


def validate_binary_reward(value: Any) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) not in {0.0, 1.0}
    ):
        raise VerifierEvidenceError(
            "verifier reward must be the finite number 0 or 1"
        )
    return float(value)


def _validate_test_prerequisites(path: Path, tests: list[dict[str, Any]]) -> None:
    """Reject confirmed verifier-owned setup failures, not missing deliverables.

    A completed pytest assertion can still be checking an empty output left by
    failed setup. Match both the affected assertion/fixture and its causal log
    evidence; network warnings or missing files alone are not sufficient.
    These are deliberately narrow rules for verified failure modes, not an
    exhaustive infrastructure classifier.
    """
    traces = [
        str(test.get("trace") or "")
        for test in tests
        if test.get("status") == "failed"
    ]
    ocaml_output_missing = any(
        "/app/ocaml/tests.txt" in trace and "assert '40 tests passed' in ''" in trace
        for trace in traces
    )
    c4_fixture_failed = any(
        "def generate_test_data(" in trace
        and "load_dataset(" in trace
        and "Couldn't find cache for allenai/c4" in trace
        for trace in traces
    )
    if not (ocaml_output_missing or c4_fixture_failed):
        return

    logs = []
    for name in ("test-stdout.txt", "test-stderr.txt"):
        try:
            logs.append(
                (path.parent / name).read_text(encoding="utf-8", errors="replace")
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise VerifierEvidenceError(
                f"could not read verifier evidence: {name}"
            ) from exc
    output = "\n".join(logs)
    if (
        ocaml_output_missing
        and "fatal: unable to access 'https://github.com/sadiqj/ocaml/':" in output
        and "GnuTLS recv error" in output
        and "cp: cannot stat 'ocaml-original/testsuite': No such file or directory" in output
    ):
        raise VerifierEvidenceError(
            "verifier_prerequisite_failure: OCaml testsuite download failed (TLS); "
            "the verifier then checked empty tests.txt without running the suite"
        )
    if (
        c4_fixture_failed
        and "Captured stderr setup" in output
        and "Network is unreachable" in output
        and "https://huggingface.co/datasets/allenai/c4/" in output
    ):
        raise VerifierEvidenceError(
            "verifier_prerequisite_failure: C4 test-data fixture could not access "
            "Hugging Face and had no matching dataset cache"
        )


def validate_ctrf_report(path: Path) -> dict[str, Any]:
    """Require evidence that pytest actually collected and ran tests."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise VerifierEvidenceError("verifier did not produce ctrf.json") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise VerifierEvidenceError("verifier produced unreadable ctrf.json") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    summary = results.get("summary") if isinstance(results, dict) else None
    tests = results.get("tests") if isinstance(results, dict) else None
    test_count = summary.get("tests") if isinstance(summary, dict) else None
    if (
        type(test_count) is not int
        or test_count <= 0
        or not isinstance(tests, list)
        or len(tests) != test_count
    ):
        raise VerifierEvidenceError(
            "ctrf.json does not prove that any tests were executed"
        )
    if any(not isinstance(test, dict) for test in tests):
        raise VerifierEvidenceError("ctrf.json contains an invalid test record")
    if not any(test.get("status") in {"passed", "failed"} for test in tests):
        raise VerifierEvidenceError(
            "ctrf.json does not prove that any test completed"
        )
    _validate_test_prerequisites(path, tests)
    return {"test_count": test_count}
