"""Authoritative state for one human-sized coding work unit."""

from dataclasses import dataclass, field

MAX_WORK_ITEMS = 12


@dataclass
class WorkPlanLedger:
    items: list = field(default_factory=list)
    active_id: str = ""
    decision_progress_count: int = 0

    def active(self, identifier=None):
        target = str(identifier or self.active_id).strip()
        return next(
            (item for item in self.items if str(item.get("id", "")) == target),
            None,
        )

    def update(self, args):
        existing = {str(item.get("id", "")): item for item in self.items}
        order = [str(item.get("id", "")) for item in self.items]
        updated = {identifier: dict(item) for identifier, item in existing.items()}
        changed = False
        for raw in args.get("items", ())[:MAX_WORK_ITEMS]:
            identifier = str(raw.get("id", "")).strip()
            previous = existing.get(identifier, {})
            item = {
                "id": identifier,
                "requirement": str(raw.get("requirement", "")).strip(),
                "hypothesis": str(raw.get("hypothesis", "")).strip(),
                "blocker": str(raw.get("blocker", "")).strip(),
                "candidate_action": str(raw.get("candidate_action", "")).strip(),
                "expected_observation": str(
                    raw.get("expected_observation", "")
                ).strip(),
                "evidence_paths": list(previous.get("evidence_paths", ()))[:12],
                "mutation_paths": list(previous.get("mutation_paths", ()))[:12],
            }
            previous_status = str(previous.get("status", ""))
            if previous_status in {"implemented", "verified"}:
                item["status"] = previous_status
            elif item["blocker"]:
                item["status"] = "needs_evidence"
            elif item["candidate_action"]:
                item["status"] = "actionable"
            else:
                item["status"] = "orienting"
            if item != previous:
                changed = True
            updated[identifier] = item
            if identifier not in order:
                order.append(identifier)
        active_id = str(args.get("active_id", "")).strip()
        if active_id != self.active_id:
            changed = True
        self.items = [updated[identifier] for identifier in order][-MAX_WORK_ITEMS:]
        self.active_id = active_id
        if changed:
            self.decision_progress_count += 1
        return changed

    def blockers(self):
        return {
            ("work_item_blocker", str(item.get("id", "")), str(item["blocker"]))
            for item in self.items
            if item.get("status") in {"needs_evidence", "decision_due"}
            and item.get("blocker")
        }

    def bind_evidence(self, identifier, paths):
        item = self.active(identifier)
        if item is None:
            return False
        known = list(item.get("evidence_paths", ()))
        for path in paths:
            if path and path not in known:
                known.append(path)
        item["evidence_paths"] = known[-12:]
        if item.get("status") not in {"implemented", "verified"}:
            item["status"] = "decision_due"
        return True

    def mark_implemented(self, identifiers, paths):
        selected = [str(item) for item in identifiers]
        if not selected and self.active_id:
            selected = [self.active_id]
        for identifier in selected:
            item = self.active(identifier)
            if item is None:
                continue
            item["status"] = "implemented"
            item["blocker"] = ""
            mutation_paths = list(item.get("mutation_paths", ()))
            for path in paths:
                if path and path not in mutation_paths:
                    mutation_paths.append(path)
            item["mutation_paths"] = mutation_paths[-12:]

    def mark_verified(self, identifiers, passed):
        selected = [str(item) for item in identifiers]
        if not selected:
            selected = [
                str(item.get("id", ""))
                for item in self.items
                if item.get("status") in {"implemented", "repair"}
            ]
        for identifier in selected:
            item = self.active(identifier)
            if item is not None:
                item["status"] = "verified" if passed else "repair"

    def pending(self):
        return [
            item
            for item in self.items
            if item.get("status") not in {"implemented", "verified"}
        ]

    def view(self):
        return {
            "active_id": self.active_id,
            "items": [
                {
                    key: value
                    for key, value in item.items()
                    if value not in ("", [], None)
                }
                for item in self.items[-MAX_WORK_ITEMS:]
            ],
            "decision_progress_count": self.decision_progress_count,
        }

    def to_dict(self):
        return self.view()

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(
            items=[
                dict(item)
                for item in data.get("items", [])[-MAX_WORK_ITEMS:]
                if isinstance(item, dict)
            ],
            active_id=str(data.get("active_id", "")),
            decision_progress_count=int(data.get("decision_progress_count", 0)),
        )
