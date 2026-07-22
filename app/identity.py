"""Passenger identity canonicalization and safe merge of split profiles.

Common manifesto noise that splits one person into two uniques:
- ``123.456.789-09`` vs ``CPF 123.456.789-09`` → different identity_keys
- Passport + CPF glued in one cell
- Minor name spelling differences under the same CPF
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

_DOC_NOISE = {
    "nan",
    "none",
    "à confirmar",
    "a confirmar",
    "confirmar",
    "null",
}

# Labels stripped before / while normalizing
_LABEL_RE = re.compile(
    r"\b(?:CPF|RG|CNH|RNE|RGMG|PASSAPORTE|PASSPORT|PSPT|PST|DOC(?:UMENTO)?)\b",
    re.I,
)


def fold_name(s: str) -> str:
    text = unicodedata.normalize("NFKD", s or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_tokens(name: str) -> list[str]:
    stop = {"DE", "DA", "DO", "DAS", "DOS", "E"}
    return [t for t in fold_name(name).split() if t and t not in stop]


def name_similarity(a: str, b: str) -> float:
    ta, tb = set(name_tokens(a)), set(name_tokens(b))
    if not ta or not tb:
        return 0.0
    jacc = len(ta & tb) / len(ta | tb)
    fa, la = name_tokens(a)[0], name_tokens(a)[-1]
    fb, lb = name_tokens(b)[0], name_tokens(b)[-1]
    if fa == fb and la == lb:
        return max(jacc, 0.85)
    # Typo-tolerant last names (Calbucci / Cabulcci)
    if fa == fb and _edit_distance(la, lb) <= 2 and min(len(la), len(lb)) >= 4:
        return max(jacc, 0.75)
    if la == lb and (fa.startswith(fb[:3]) or fb.startswith(fa[:3])):
        return max(jacc, 0.7)
    return jacc


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = cur
    return prev[-1]


def extract_cpf_digits(text: str) -> Optional[str]:
    """Pull a Brazilian CPF (11 digits) from free text when present."""
    raw = (text or "").upper()
    compact = re.sub(r"[^0-9A-Z]", "", raw)
    m = re.search(r"CPF(\d{11})", compact)
    if m:
        return m.group(1)
    digits = re.sub(r"\D", "", raw)
    seqs = re.findall(r"\d{11}", digits)
    if len(seqs) == 1:
        return seqs[0]
    if len(digits) == 11:
        return digits
    return None


def canonical_document(document: Any) -> Optional[str]:
    """Stable document key for identity_key.

    Prefers CPF when detectable so ``CPF 070…`` and ``070…`` collapse.
    """
    if document is None:
        return None
    text = str(document).strip()
    if not text or text.lower() in _DOC_NOISE:
        return None

    cpf = extract_cpf_digits(text)
    if cpf:
        return f"CPF{cpf}"

    compact = re.sub(r"[^0-9A-Za-z]", "", text).upper()
    if not compact:
        return None
    # Strip leading type labels repeatedly (CPF/RG/PSPT…)
    prev = None
    while prev != compact:
        prev = compact
        compact = _LABEL_RE.sub("", compact)
        compact = re.sub(r"[^0-9A-Z]", "", compact)
    return compact if len(compact) >= 4 else None


def identity_key(name: str, document: Any) -> tuple[str, Optional[str]]:
    """Build (identity_key, document_normalized) with canonical docs."""
    doc = canonical_document(document)
    n = fold_name(name) if name else None
    if doc:
        return f"doc:{doc}", doc
    if n:
        return f"name:{n}", None
    import hashlib

    raw = (name or "").encode()
    return f"raw:{hashlib.sha1(raw).hexdigest()[:16]}", None


@dataclass
class IdentityMergeReport:
    groups_found: int = 0
    passengers_merged: int = 0
    boardings_reassigned: int = 0
    boardings_deduped: int = 0
    skipped_unsafe: int = 0
    dry_run: bool = False
    samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "groups_found": self.groups_found,
            "passengers_merged": self.passengers_merged,
            "boardings_reassigned": self.boardings_reassigned,
            "boardings_deduped": self.boardings_deduped,
            "skipped_unsafe": self.skipped_unsafe,
            "dry_run": self.dry_run,
            "samples": self.samples,
        }


def repair_merge_split_identities(
    db: Session,
    *,
    min_name_similarity: float = 0.5,
    dry_run: bool = True,
    sample_limit: int = 40,
) -> IdentityMergeReport:
    """Merge passenger rows that share a canonical CPF/document but split keys."""
    from app.models import Boarding, Passenger

    report = IdentityMergeReport(dry_run=dry_run)
    passengers = list(db.scalars(select(Passenger)).all())

    # Map canonical doc → passenger ids
    buckets: dict[str, list[Passenger]] = {}
    for pax in passengers:
        # Prefer stored document_normalized; also try identity_key body
        candidates: list[str] = []
        if pax.document_normalized:
            candidates.append(pax.document_normalized)
        if pax.identity_key.startswith("doc:"):
            candidates.append(pax.identity_key[4:])
        canon = None
        for c in candidates:
            canon = canonical_document(c)
            if canon:
                break
        if not canon:
            continue
        buckets.setdefault(canon, []).append(pax)

    for canon, group in buckets.items():
        if len(group) < 2:
            continue

        # Name safety check across the group
        names = [g.display_name or "" for g in group]
        sims: list[float] = []
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                sims.append(name_similarity(names[i], names[j]))
        min_sim = min(sims) if sims else 1.0
        if min_sim < min_name_similarity:
            report.skipped_unsafe += 1
            if len(report.samples) < sample_limit:
                report.samples.append(
                    {
                        "action": "skip_unsafe_name_mismatch",
                        "canonical_doc": canon,
                        "min_name_similarity": round(min_sim, 2),
                        "members": [
                            {
                                "id": g.id,
                                "identity_key": g.identity_key,
                                "name": g.display_name,
                                "boardings": g.total_boardings,
                            }
                            for g in group
                        ],
                    }
                )
            continue

        report.groups_found += 1
        # Keep the passenger with most boardings, then lowest id
        keeper = max(group, key=lambda g: (g.total_boardings or 0, -g.id))
        losers = [g for g in group if g.id != keeper.id]

        sample = {
            "action": "merge_identities",
            "canonical_doc": canon,
            "min_name_similarity": round(min_sim, 2),
            "kept_id": keeper.id,
            "kept_key": keeper.identity_key,
            "kept_name": keeper.display_name,
            "merged": [
                {
                    "id": g.id,
                    "identity_key": g.identity_key,
                    "name": g.display_name,
                    "boardings": g.total_boardings,
                }
                for g in losers
            ],
        }
        if len(report.samples) < sample_limit:
            report.samples.append(sample)

        if dry_run:
            report.passengers_merged += len(losers)
            continue

        best_name = keeper.display_name
        for loser in losers:
            if len(loser.display_name or "") > len(best_name or ""):
                best_name = loser.display_name

        from sqlalchemy import delete, update

        target_key = f"doc:{canon}"
        # If another row already owns the canonical key, that row must be deleted
        # before we rename the keeper (unique identity_key).
        for loser in losers:
            boardings = list(
                db.scalars(select(Boarding).where(Boarding.passenger_id == loser.id)).all()
            )
            for b in boardings:
                existing = db.scalar(
                    select(Boarding.id).where(
                        Boarding.flight_id == b.flight_id,
                        Boarding.passenger_id == keeper.id,
                    )
                )
                if existing:
                    db.execute(delete(Boarding).where(Boarding.id == b.id))
                    report.boardings_deduped += 1
                else:
                    db.execute(
                        update(Boarding)
                        .where(Boarding.id == b.id)
                        .values(passenger_id=keeper.id)
                    )
                    report.boardings_reassigned += 1
            db.flush()
            db.execute(delete(Passenger).where(Passenger.id == loser.id))
            report.passengers_merged += 1
            db.flush()

        db.execute(
            update(Passenger)
            .where(Passenger.id == keeper.id)
            .values(
                display_name=best_name or keeper.display_name,
                identity_key=target_key,
                document_normalized=canon,
            )
        )
        db.flush()

        remaining = list(
            db.scalars(select(Boarding).where(Boarding.passenger_id == keeper.id)).all()
        )
        dates = [b.flight_date for b in remaining if b.flight_date]
        db.execute(
            update(Passenger)
            .where(Passenger.id == keeper.id)
            .values(
                total_boardings=len(remaining),
                first_seen=min(dates) if dates else None,
                last_seen=max(dates) if dates else None,
            )
        )
        db.flush()
        db.expire_all()

    if not dry_run:
        db.commit()
    else:
        db.rollback()
    return report
