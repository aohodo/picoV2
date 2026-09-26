"""Ground user-mentioned repository paths before model execution."""

from pathlib import Path


class RepositoryGroundingRuntime:
    """Resolve request path mentions against the active Shadow workspace."""

    def _ground_referenced_paths(self, interaction):
        mentioned = list(interaction.get("referenced_paths", ()))
        graph_paths = list(self.agent.last_repository_evidence.get("paths", ()))
        existing, missing, unresolved = self._classify_path_mentions(
            mentioned, graph_paths
        )

        interaction["requested_existing_paths"] = existing
        interaction["requested_missing_paths"] = missing
        interaction["unresolved_path_mentions"] = unresolved
        if existing:
            seed = dict(self.agent.last_repository_evidence)
            seed["paths"] = list(dict.fromkeys([*existing, *graph_paths]))
            seed["confidence"] = "high"
            seed["requested_existing_paths"] = existing
            seed["requested_missing_paths"] = missing
            self.agent.last_repository_evidence = seed

        if mentioned:
            self._append_path_evidence(interaction, existing, missing, unresolved)

    def _classify_path_mentions(self, mentioned, graph_paths):
        existing = []
        missing = []
        unresolved = []
        root = Path(self.agent.root)
        for raw_path in mentioned:
            path = str(raw_path).replace("\\", "/").removeprefix("./")
            if (root / path).is_file():
                self._append_unique(existing, path)
            elif "/" in path:
                self._append_unique(missing, path)
            else:
                matches = [
                    candidate
                    for candidate in graph_paths
                    if Path(candidate).name.casefold() == path.casefold()
                ]
                if matches:
                    for candidate in matches:
                        self._append_unique(existing, candidate)
                else:
                    self._append_unique(unresolved, path)
        return existing, missing, unresolved

    @staticmethod
    def _append_unique(items, value):
        if value not in items:
            items.append(value)

    @staticmethod
    def _append_path_evidence(interaction, existing, missing, unresolved):
        lines = ["Explicit path evidence from the current request:"]
        if existing:
            lines.append("  existing targets: " + ", ".join(existing))
        if missing:
            lines.append(
                "  named paths not currently present (possible requested outputs): "
                + ", ".join(missing)
            )
        if unresolved:
            lines.append("  unresolved filename mentions: " + ", ".join(unresolved))
        rendered = str(interaction.get("repository_evidence", "")).strip()
        interaction["repository_evidence"] = "\n".join(
            [part for part in (rendered, *lines) if part]
        )
