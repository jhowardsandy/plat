from __future__ import annotations
from pathlib import Path

PACKS = Path(__file__).parent / "prompts"


def _t(name: str) -> str:
    return (PACKS / name).read_text()


CODE_SHAPE = """{
  "lot": "<the lot key you were given>",
  "phase": "code",
  "attempt": 1,
  "status": "complete | partial | blocked",
  "summary": "one paragraph, plain language",
  "changed_files": ["path/one.py"],
  "commits": ["<sha>"],
  "tests": {"cmd": "<the command you ran>", "ran": true, "passed": 0, "failed": 0},
  "acceptance_criteria": [
    {"id": "AC1", "met": true, "evidence": "tests/test_x.py::test_y"}
  ],
  "contracts_touched": ["kafka:some.topic"],
  "decisions": [
    {"choice": "...", "alternatives": ["..."], "rationale": "...", "reversible": true}
  ],
  "open_questions": [],
  "needs_human": false
}"""

REVIEW_SHAPE = """{
  "verdict": "pass | changes_requested | blocked",
  "blocking_question": "only when verdict is blocked",
  "findings": [
    {
      "id": "f1",
      "fingerprint": "<file>:<rule>:<symbol>",
      "severity": "high | medium | low",
      "file": "path/one.py",
      "line": 88,
      "claim": "what is wrong",
      "required_fix": "what must change"
    }
  ],
  "criteria_check": [{"id": "AC1", "met": true, "note": "optional"}]
}"""


def termination(out_dir: Path, phase: str) -> str:
    shape = CODE_SHAPE if phase == "code" else REVIEW_SHAPE
    return (_t("termination_contract.md")
            .replace("{out_dir}", str(out_dir))
            .replace("{phase}", phase)
            .replace("{shape}", shape))


def criteria_block(criteria) -> str:
    if not criteria:
        return "_none declared -- this lot cannot be gated on criteria_"
    out = []
    for c in criteria:
        v = (f"\n  verify: `{c.verify_cmd}`" if c.verify_cmd
             else "\n  _no verify command -- this one cannot gate; judge it on evidence_")
        out.append(f"- **{c.ac_id}** {c.statement}{v}")
    return "\n".join(out)


def feedback_block(findings, attempt: int) -> str:
    if not findings:
        return ""
    lines = [f"## Review feedback to address (attempt {attempt})", ""]
    for f in findings:
        loc = f.file + (f":{f.line}" if f.line else "")
        lines += [f"### [{f.severity}] {loc}", f"`{f.fingerprint}`", "",
                  f.claim, "", f"**Required fix:** {f.required_fix}", ""]
    lines.append("Address every one. If you believe a finding is wrong, do not "
                 "silently ignore it -- say so in `open_questions` and explain why.")
    return "\n".join(lines)


def prior_art_block(rows=None) -> str:
    """v2: nearest prior findings from the corpus. Empty until pgvector lands."""
    return ""


def build_code(lot, plat, wt, branch, base, plat_map, lot_desc, criteria,
               findings, test_cmd, out_dir) -> str:
    return (_t("code.md")
            .replace("{anchor}", plat.anchor).replace("{lot_key}", lot.key)
            .replace("{repo}", lot.repo).replace("{worktree}", str(wt))
            .replace("{branch}", branch).replace("{base}", base)
            .replace("{plat_map}", plat_map or "_not supplied_")
            .replace("{lot_desc}", lot_desc or "_not supplied_")
            .replace("{criteria}", criteria_block(criteria))
            .replace("{feedback}", feedback_block(findings, lot.attempt))
            .replace("{test_cmd}", test_cmd)
            .replace("{termination}", termination(out_dir, "code")))


def build_review(lot, plat, wt, base, lot_desc, criteria, gate, out_dir) -> str:
    g = (f"- command: `{gate['cmd']}`\n"
         f"- exit code: {gate['exit_code']}\n"
         f"- passed: {gate['tests_passed']}, failed: {gate['tests_failed']}\n"
         f"- files changed: {gate['diff_files']}")
    return (_t("review.md")
            .replace("{anchor}", plat.anchor).replace("{lot_key}", lot.key)
            .replace("{worktree}", str(wt)).replace("{base}", base)
            .replace("{lot_desc}", lot_desc or "_not supplied_")
            .replace("{criteria}", criteria_block(criteria))
            .replace("{gate}", g)
            .replace("{prior_art}", prior_art_block())
            .replace("{termination}", termination(out_dir, "review")))
