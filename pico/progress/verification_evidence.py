"""Structured evidence that changed tests were executed by a verification run."""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

_MAVEN_EXECUTABLES = {"mvn", "mvn.cmd", "mvnw", "mvnw.cmd"}
_MAVEN_PRESENTATION_ARGUMENTS = {
    "-b",
    "--batch-mode",
    "-ntp",
    "--no-transfer-progress",
    "-q",
    "--quiet",
}


def _normalized(value):
    return str(value or "").replace("\\", "/").lstrip("./")


def verification_identity(argv):
    """Return the delivery meaning of one verification invocation.

    A retry may change transport/presentation details without changing what is
    being verified.  In particular, Maven's batch, quiet, transfer-progress,
    and local-repository options do not alter the selected lifecycle goals,
    modules, profiles, or tests.  Keeping those execution details in failure
    identity made a successful retry unable to resolve an earlier timeout.

    Unknown runners remain byte-for-byte conservative: only the Maven family
    receives semantic normalization, and test/profile/skip/module arguments
    are deliberately retained.
    """
    values = [str(item) for item in argv]
    if not values:
        return []
    executable = _normalized(values[0]).rsplit("/", 1)[-1].casefold()
    if executable not in _MAVEN_EXECUTABLES:
        return values

    semantic = ["maven"]
    index = 1
    while index < len(values):
        item = values[index]
        folded = item.casefold()
        if folded in _MAVEN_PRESENTATION_ARGUMENTS:
            index += 1
            continue
        if folded.startswith("-dmaven.repo.local="):
            index += 1
            continue
        if folded == "-dmaven.repo.local" and index + 1 < len(values):
            index += 2
            continue
        semantic.append(item)
        index += 1
    return semantic


def _is_pytest(argv):
    names = [_normalized(item).rsplit("/", 1)[-1].casefold() for item in argv]
    return bool(names) and (
        names[0] in {"pytest", "pytest.exe"}
        or (len(names) >= 3 and names[1:3] == ["-m", "pytest"])
    )


def _expects_xml_reports(argv):
    if _is_pytest(argv):
        return True
    executable = _normalized(argv[0]).rsplit("/", 1)[-1].casefold() if argv else ""
    return executable in {
        "mvn", "mvn.cmd", "mvnw", "mvnw.cmd",
        "gradle", "gradle.bat", "gradlew", "gradlew.bat",
    }


class VerificationProbe:
    def __init__(self, root, argv, changed_test_paths=()):
        self.root = Path(root)
        self.argv = [str(item) for item in argv]
        self.changed_test_paths = sorted({_normalized(path) for path in changed_test_paths})
        self.report_path = self.root / ".pico" / "runtime" / "verification" / "pytest.xml"
        self.before = self._report_snapshot()

    def process_argv(self):
        argv = list(self.argv)
        if _is_pytest(argv) and not any(
            item == "--junitxml" or item.startswith("--junitxml=") for item in argv
        ):
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.unlink(missing_ok=True)
            argv.append(f"--junitxml={self.report_path}")
        return argv

    def _report_files(self):
        patterns = (
            "**/surefire-reports/TEST-*.xml",
            "**/failsafe-reports/TEST-*.xml",
            "**/test-results/**/*.xml",
            ".pico/runtime/verification/*.xml",
        )
        return {
            path.resolve()
            for pattern in patterns
            for path in self.root.glob(pattern)
            if path.is_file()
        }

    def _report_snapshot(self):
        result = {}
        for path in self._report_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            result[path] = (stat.st_mtime_ns, stat.st_size)
        return result

    @staticmethod
    def _report_identities(path):
        identities = set()
        test_count = 0
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            return identities, test_count
        for case in root.iter():
            if case.tag.rsplit("}", 1)[-1] != "testcase":
                continue
            test_count += 1
            for field in ("classname", "file"):
                value = _normalized(case.attrib.get(field, ""))
                if value:
                    identities.add(value.casefold())
        return identities, test_count

    def _explicitly_selected(self, path):
        normalized = _normalized(path).casefold()
        stem = Path(path).stem.casefold()
        for item in self.argv:
            candidate = _normalized(item).casefold().split("::", 1)[0]
            if candidate == normalized or candidate.endswith("/" + normalized):
                return True
            if item.casefold().startswith("-dtest="):
                selected = re.split(r"[,#+]", item.split("=", 1)[1].casefold())
                if stem in selected:
                    return True
        return False

    @staticmethod
    def _identity_matches(path, identities):
        stem = Path(path).stem.casefold()
        return any(stem in re.split(r"[./\\$]", identity) for identity in identities)

    def finish(self):
        current = self._report_snapshot()
        reports = [
            path for path, signature in current.items()
            if self.before.get(path) != signature
        ]
        identities = set()
        test_count = 0
        for report in reports:
            report_ids, count = self._report_identities(report)
            identities.update(report_ids)
            test_count += count
        verified = []
        for path in self.changed_test_paths:
            if (
                test_count > 0 and self._identity_matches(path, identities)
            ) or (
                not _expects_xml_reports(self.argv) and self._explicitly_selected(path)
            ):
                verified.append(path)
        missing = sorted(set(self.changed_test_paths) - set(verified))
        return {
            "changed_test_paths": self.changed_test_paths,
            "verified_test_paths": verified,
            "missing_test_paths": missing,
            "test_count": test_count,
            "report_paths": [
                path.relative_to(self.root).as_posix()
                for path in reports
                if self.root in path.parents
            ],
        }


class VerificationObservation(str):
    def __new__(cls, value, evidence):
        instance = super().__new__(cls, value)
        instance.verification_evidence = evidence
        return instance
