"""Choosing the shape of a run before paying to plan it.

`plat draft` is one expensive take -- several dollars and up to fifteen minutes --
that commits to a single decomposition. If the shape is wrong you have paid full
price to find out, and the shape is precisely where a person holds context a model
does not: that repo is mid-refactor, we ship Thursday, leave the overlay alone.

So: cheap reconnaissance, a menu with estimates drawn from what runs here have
actually cost, and the expensive pass spent only on the shape you chose.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

# Used only when this database has no history of its own. Stated as a guess, and
# labelled as one wherever it is shown.
FALLBACK = {"code": 1.20, "review": 0.60, "code_s": 150, "review_s": 135,
            "retry": 1.8}


@dataclass
class Estimate:
    code: float
    review: float
    code_s: float
    review_s: float
    retry: float          # mean attempts per lot; 1.0 would mean one pass always
    n: int                # attempts this is drawn from
    grounded: bool        # False when it is the fallback guess

    def per_lot(self, phases: list[str]) -> tuple[float, float]:
        """Cost and seconds for one lot, retries included.

        A single code pass plus a single review is the number people expect and it
        is the wrong one: most lots here needed more than one pass. Quoting the
        optimistic figure would understate the real cost roughly twofold.
        """
        cost = self.code + (self.review if "review" in phases else 0.0)
        secs = self.code_s + (self.review_s if "review" in phases else 0.0)
        if "quality" in phases:
            cost += self.review
            secs += self.review_s
        return cost * self.retry, secs * self.retry


def estimate(db) -> Estimate:
    row = db.execute(text("""
        SELECT a.phase,
               avg(a.cost_usd) AS cost,
               avg(EXTRACT(EPOCH FROM (a.finished_at - a.started_at))) AS secs,
               count(*) AS n
        FROM attempts a
        JOIN lots l ON l.id = a.lot_id
        JOIN plats p ON p.id = l.plat_id
        WHERE p.kind = 'plat' AND p.origin_id <> 'demo'
          AND a.finished_at IS NOT NULL AND a.provider <> 'supervisor'
        GROUP BY a.phase""")).mappings().all()
    by = {r["phase"]: r for r in row}
    total = sum(r["n"] for r in row)
    if total < 4:
        return Estimate(FALLBACK["code"], FALLBACK["review"], FALLBACK["code_s"],
                        FALLBACK["review_s"], FALLBACK["retry"], total, False)

    retry = db.execute(text("""
        SELECT coalesce(avg(l.attempt), 1) FROM lots l JOIN plats p ON p.id = l.plat_id
        WHERE p.kind = 'plat' AND p.origin_id <> 'demo' AND l.attempt > 0""")).scalar()
    g = lambda ph, k, d: float(by[ph][k]) if ph in by and by[ph][k] else d   # noqa: E731
    return Estimate(
        code=g("code", "cost", FALLBACK["code"]),
        review=g("review", "cost", FALLBACK["review"]),
        code_s=g("code", "secs", FALLBACK["code_s"]),
        review_s=g("review", "secs", FALLBACK["review_s"]),
        retry=max(1.0, float(retry or 1.0)),
        n=total, grounded=True)


@dataclass
class Shape:
    key: str
    label: str
    lots: int
    cost: float
    minutes: float
    tradeoff: str
    constraint: str = ""        # what stage 2 is told about this choice
    extra: dict[str, Any] = field(default_factory=dict)


def propose(recon: dict, est: Estimate, phases: list[str]) -> list[Shape]:
    """The shapes worth choosing between — not every combination of every knob.

    Models, effort and phases are standing preferences that live in roles.yaml.
    What changes per ticket is how the work is divided, and whether it should be
    divided at all yet.
    """
    repos = list(recon.get("repos") or [])
    out: list[Shape] = []
    c1, s1 = est.per_lot(phases)

    if recon.get("code_exists"):
        out.append(Shape(
            "review", "review only", 0, est.review, est.review_s / 60,
            "the change already exists; this reviews it and writes nothing",
            "The work is already written. Produce no lots."))

    if len(repos) > 1:
        n = len(repos)
        out.append(Shape(
            "split", f"split by repo ({n} lots)", n, c1 * n, s1 * n / 60,
            "isolated blast radius and one review each; not faster until lots run "
            "in parallel",
            f"Produce one lot per repo, {n} in total: {', '.join(repos)}."))

    if repos:
        out.append(Shape(
            "single", "single lot", 1, c1, s1 / 60,
            "cheapest; one agent holds the whole change and one failure loses all "
            "of it",
            "Produce exactly ONE lot covering the whole change, even if it spans "
            "more than one directory. Pick the repo where most of the work lands."))

    m = recon.get("measurable") or {}
    if m.get("objective"):
        out.append(Shape(
            "converge", f"converge to {m['objective']}", 1, c1 * 2.5, s1 * 2.5 / 60,
            "iterates until the measurement is met rather than stopping at three "
            "attempts",
            f"Produce ONE converge lot: mode: converge, objective: "
            f"{m['objective']!r}, gate.probe: {m.get('probe', '<a command printing a number>')!r}.",
            {"objective": m["objective"]}))

    out.append(Shape(
        "spike", "spike first", 1, est.code * 0.5, est.code_s * 0.5 / 60,
        "read-only investigation that comes back with a decomposition, for when "
        "the scope is not yet trustworthy",
        "Produce ONE read-only lot that INVESTIGATES and reports what it found. "
        "It must change no code and its acceptance criterion is a written "
        "finding, not a diff."))
    return out


def recommended(recon: dict, shapes: list[Shape]) -> str:
    """What to default the prompt to. An unspecified ticket is a spike, whatever
    else looks attractive."""
    keys = [s.key for s in shapes]
    if not recon.get("specified") and "spike" in keys:
        return "spike"
    for k in ("review", "split", "single", "converge", "spike"):
        if k in keys:
            return k
    return keys[0]
