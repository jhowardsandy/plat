"""Write-through. Agents write files because they must; Postgres is the record.

The moment a phase completes its artifacts are ingested, and disk becomes a cache
you are free to delete -- which matters because `/mlg-worktree-down` will delete it.
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from .models import (Verdict, Finding, Attempt,
                     Session as SessionRow, Artifact, Transcript)
from .decisions import record

SCHEMAS = Path(__file__).parent / "schemas"


class ContractError(RuntimeError):
    """The agent did not honour the termination contract. Never inferred around."""


def _validator(phase: str) -> Draft202012Validator:
    name = {"code": "code", "review": "review", "quality": "review"}.get(phase, phase)
    return Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


def load_verdict(out_dir: Path, phase: str) -> dict:
    f = out_dir / f"{phase}.json"
    if not f.exists():
        raise ContractError(f"{f} was never written")
    try:
        data = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        raise ContractError(f"{f} is not valid JSON: {e}") from e
    errs = sorted(_validator(phase).iter_errors(data), key=lambda e: e.path)
    if errs:
        detail = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}"
                           for e in errs[:5])
        raise ContractError(f"{f} failed schema: {detail}")
    return data


def persist(db: DbSession, *, plat, lot, attempt, verdict: dict, result,
            out_dir: Path, parent_decision_id: int | None) -> int | None:
    """Ingest one finished phase. Returns the last decision id, to chain the tree."""
    db.add(Verdict(
        attempt_id=attempt.id, raw=verdict,
        status=verdict.get("status"), verdict=verdict.get("verdict"),
        tests_passed=(verdict.get("tests") or {}).get("passed", 0),
        tests_failed=(verdict.get("tests") or {}).get("failed", 0),
    ))
    if result.session_id:
        db.add(SessionRow(attempt_id=attempt.id, provider=result.provider,
                          provider_session_id=result.session_id))
    for md in sorted(out_dir.glob("*.md")):
        body = md.read_text()
        db.add(Artifact(plat_id=plat.id, attempt_id=attempt.id, kind=attempt.phase,
                        name=md.name, body=body,
                        sha256=hashlib.sha256(body.encode()).hexdigest()))
    if result.stdout:
        db.add(Transcript(attempt_id=attempt.id, body=result.stdout[:4_000_000]))

    # A finding that stopped being raised was fixed. Without this the corpus can
    # only ever say "someone once complained about X", never whether the complaint
    # survived -- which is most of its value as prior art (design doc §12), and
    # what separates a resolved finding from one dismissed on purpose.
    if attempt.phase in ("review", "quality"):
        still = {f["fingerprint"] for f in verdict.get("findings", [])}
        prior = db.scalars(
            select(Finding).join(Attempt, Attempt.id == Finding.attempt_id)
            .where(Attempt.lot_id == lot.id,
                   Attempt.n < attempt.n,
                   Finding.resolved_in_attempt_id.is_(None))).all()
        for f in prior:
            if f.fingerprint not in still:
                f.resolved_in_attempt_id = attempt.id

    for f in verdict.get("findings", []):
        db.add(Finding(
            attempt_id=attempt.id, origin_id=plat.origin_id, anchor=plat.anchor,
            repo=lot.repo, fingerprint=f["fingerprint"], severity=f["severity"],
            file=f["file"], line=f.get("line"), claim=f["claim"],
            required_fix=f.get("required_fix", ""),
        ))

    last = parent_decision_id
    # Self-reported. Weaker evidence than anything the supervisor observed, and
    # rendered differently everywhere it is shown.
    for d in verdict.get("decisions", []):
        if not d.get("alternatives"):
            continue
        last = record(
            db, plat_id=plat.id, lot_id=lot.id, attempt_id=attempt.id,
            parent_id=last, actor="agent",
            actor_detail=f"{result.provider}/{attempt.model}",
            kind="design_choice", decision=d["choice"],
            alternatives=d["alternatives"], rationale=d.get("rationale", ""),
            inputs={"phase": attempt.phase, "attempt": attempt.n},
            reversible=d.get("reversible", True),
        ).id
    if attempt.phase in ("review", "quality"):
        label = verdict["verdict"]
        n = len(verdict.get("findings", []))
        last = record(
            db, plat_id=plat.id, lot_id=lot.id, attempt_id=attempt.id,
            parent_id=last, actor="agent",
            actor_detail=f"{result.provider}/{attempt.model}",
            kind="verdict", decision=f"{label}" + (f" · {n} finding(s)" if n else ""),
            alternatives=["pass"] if label != "pass" else ["changes_requested"],
            rationale=(verdict.get("findings") or [{}])[0].get("claim",
                       verdict.get("blocking_question", "criteria satisfied")),
            inputs={"verdict": label, "findings": n},
        ).id
    return last
