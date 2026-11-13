"""Check results and the desktop report (desktop-report.json / desktop-report.md). Pure Python.

Every checklist id gets one row. A check returns pass, fail (with a reason) or skipped (with a
reason); a run stopped by real user input marks the remaining checks "aborted". With --repeat N
each id keeps every attempt and the row shows the aggregate (any fail -> fail, "flaky" noted).
"""
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import time

PASS, FAIL, SKIPPED, ABORTED = "pass", "fail", "skipped", "aborted"
STATUSES = (PASS, FAIL, SKIPPED, ABORTED)
SCHEMA = 1


@dataclass
class Result:
    id: str
    status: str
    reason: str = ""
    title: str = ""
    group: str = ""
    best_effort: bool = False
    artifacts: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    duration_s: float = 0.0
    attempt: int = 1
    source: str = "auto"

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"Unknown status {self.status!r}")
        if self.status in (FAIL, SKIPPED, ABORTED) and not self.reason:
            raise ValueError(f"{self.id}: a {self.status} result needs a reason")


def aggregate(attempts):
    """One row from the attempts of one id (in attempt order)."""
    if not attempts:
        raise ValueError("No attempts")
    first = attempts[0]
    statuses = [a.status for a in attempts]
    counted = [s for s in statuses if s in (PASS, FAIL)]
    if FAIL in statuses:
        status = FAIL
        failed = next(a for a in attempts if a.status == FAIL)
        reason = failed.reason
        if PASS in statuses:
            reason = f"flaky: failed {statuses.count(FAIL)} of {len(counted)} attempts; first failure: {reason}"
    elif ABORTED in statuses:
        # An aborted attempt means the run did not finish: never hide it behind an earlier pass.
        aborted = next(a for a in attempts if a.status == ABORTED)
        status, reason = ABORTED, aborted.reason
        if PASS in statuses:
            reason = f"{reason} (passed {statuses.count(PASS)} earlier attempt(s))"
    elif PASS in statuses:
        status, reason = PASS, ""
        if len(statuses) > statuses.count(PASS):
            others = [a for a in attempts if a.status != PASS]
            reason = f"passed {statuses.count(PASS)} of {len(statuses)} attempts; {others[0].status}: {others[0].reason}"
    else:
        status, reason = statuses[-1], attempts[-1].reason
    artifacts = []
    for attempt in attempts:
        artifacts += [a for a in attempt.artifacts if a not in artifacts]
    notes = []
    for attempt in attempts:
        notes += [n for n in attempt.notes if n not in notes]
    return Result(id=first.id, status=status, reason=reason, title=first.title, group=first.group,
                  best_effort=first.best_effort, artifacts=artifacts, notes=notes,
                  duration_s=round(sum(a.duration_s for a in attempts), 1), attempt=len(attempts),
                  source=first.source)


def rows(results):
    """Aggregate per id, keeping the first-seen order of ids."""
    by_id = {}
    for result in results:
        by_id.setdefault(result.id, []).append(result)
    return [aggregate(attempts) for attempts in by_id.values()]


def summary(rows_):
    counts = {status: 0 for status in STATUSES}
    for row in rows_:
        counts[row.status] += 1
    return counts


def _cell(text):
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def markdown(meta, results):
    table = rows(results)
    counts = summary(table)
    lines = [f"# Windows desktop checks: {meta.get('sha', 'unknown')[:12]}", ""]
    for key in ("sha", "build_run", "started", "finished", "repeat", "windows", "murmur", "aborted"):
        if meta.get(key) not in (None, ""):
            lines.append(f"- {key.replace('_', ' ').capitalize()}: {_cell(meta[key])}")
    lines += ["", " · ".join(f"{counts[s]} {s}" for s in STATUSES), "",
              "| Id | Status | Reason | Evidence |", "| --- | --- | --- | --- |"]
    for row in table:
        status = row.status.upper() if row.status == FAIL else row.status
        tags = []
        if row.best_effort:
            tags.append("best effort")
        if row.source != "auto":
            tags.append(row.source)
        if row.attempt > 1:
            tags.append(f"{row.attempt} attempts")
        label = row.id + (f" ({', '.join(tags)})" if tags else "")
        reason = "; ".join([row.reason] + row.notes if row.reason else row.notes)
        evidence = ", ".join(f"`{Path(a).name}`" for a in row.artifacts)
        lines.append(f"| {label} | {status} | {_cell(reason)} | {evidence} |")
    return "\n".join(lines) + "\n"


def to_json(meta, results):
    return {"schema": SCHEMA, "meta": meta, "rows": [asdict(r) for r in rows(results)],
            "attempts": [asdict(r) for r in results]}


def write(out, meta, results):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "desktop-report.json").write_text(json.dumps(to_json(meta, results), indent=2, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
    (out / "desktop-report.md").write_text(markdown(meta, results), encoding="utf-8")
    return out / "desktop-report.md"


def load(path):
    """(meta, attempts) from a desktop-report.json."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA:
        raise ValueError(f"Unsupported report schema {data.get('schema')!r}")
    return data["meta"], [Result(**attempt) for attempt in data["attempts"]]


def merge(attempts, extra):
    """Add results from another source (manual session); an id from `extra` replaces earlier rows
    of the same id only when they were skipped/aborted, otherwise both are kept as attempts."""
    replace = {r.id for r in extra}
    kept = [r for r in attempts if not (r.id in replace and r.status in (SKIPPED, ABORTED))]
    return kept + list(extra)


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
