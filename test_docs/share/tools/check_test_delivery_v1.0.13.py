#!/usr/bin/env python3
"""Read-only static delivery checks. A zero exit code is not business acceptance."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
from urllib.parse import unquote, urlsplit


VERSION = "v1.0.13"
SEMVER = r"v[0-9]+\.[0-9]+\.[0-9]+"
CASE_ID = re.compile(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\Z")
SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
ROLE_DIR = {"input": "inputs", "actual_output": "outputs"}
TEXT_SUFFIXES = {".md", ".json", ".txt"}
SECRET_RULES = (
    ("ABSOLUTE_DRIVE_PATH", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+")),
    ("UNC_PATH", re.compile(r"\\\\[^\s\\\"']+\\[^\s\\\"']+")),
    ("TOKEN_VALUE", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|(?:sk|ck)_[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("JWT_VALUE", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("BEARER_VALUE", re.compile(r"\bBearer\s+([A-Za-z0-9._~+/-]{24,}={0,2})", re.I)),
    ("CREDENTIAL_ASSIGNMENT", re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)\b[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_+/=-]{24,})", re.I)),
    ("PRIVATE_KEY_VALUE", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{32,}")),
)


class ParameterProblem(Exception):
    """Do not include user argument values in diagnostics."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ParameterProblem("INVALID_ARGUMENTS")


def within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def placeholder(value: str) -> bool:
    lower = value.lower()
    return any(word in lower for word in ("example", "placeholder", "your_", "replace_", "redacted")) or len(set(value)) == 1


def public_label(value: str) -> str:
    """Diagnostic names are untrusted too; never echo a detected value."""
    for _, pattern in SECRET_RULES:
        for match in pattern.finditer(value):
            found = match.group(1) if match.lastindex else match.group(0)
            if not placeholder(found):
                return "<redacted_name>"
    return value


def markdown_destinations(text: str):
    """Read ordinary inline destinations and reference definitions, not code."""
    lines = []
    fence = None
    tick = chr(96)
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in (tick * 3, "~~~"):
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            lines.append("")
        elif fence is None:
            lines.append(re.sub(tick + r"+" + r"[^" + tick + r"]*" + tick + r"+", "", line))
        else:
            lines.append("")
    visible = "\n".join(lines)
    for match in re.finditer(r"(?m)^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|[^\s]+)", visible):
        yield match.group(1).strip("<>")
    for match in re.finditer(r"\]\(", visible):
        index = match.end()
        while index < len(visible) and visible[index].isspace():
            index += 1
        if index >= len(visible):
            continue
        if visible[index] == "<":
            end = visible.find(">", index + 1)
            if end >= 0:
                yield visible[index + 1:end]
            continue
        start, depth = index, 0
        while index < len(visible):
            char = visible[index]
            if char == "\\" and index + 1 < len(visible):
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    break
                depth -= 1
            elif char.isspace() and depth == 0:
                break
            index += 1
        if index > start:
            yield visible[start:index]


class Audit:
    def __init__(self, package: Path, share_root: Path):
        self.package = package
        self.share_root = share_root
        self.issues = []
        self.notes = []
        self.text_cache = {}
        self.json_cache = {}
        self.files = []
        self.case_ids = []
        self.artifacts = []
        self.package_version = None
        self.counts = {"artifacts_checked": 0, "markdown_links_checked": 0, "external_links_not_visited": 0, "text_files_scanned": 0}
        self._issue_keys = set()

    def name(self, path: Path) -> str:
        try:
            return public_label(path.relative_to(self.package).as_posix())
        except ValueError:
            return "<outside_package>"

    def issue(self, rule: str, path: Path) -> None:
        key = (rule, self.name(path))
        if key not in self._issue_keys:
            self._issue_keys.add(key)
            self.issues.append({"rule": rule, "file": key[1]})

    def safe(self, path: Path) -> Path | None:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            self.issue("PATH_MISSING_OR_UNREADABLE", path)
            return None
        if not within(resolved, self.package):
            self.issue("PATH_OUTSIDE_PACKAGE", path)
            return None
        return resolved

    def inventory(self) -> None:
        pending, seen = [self.package], set()
        while pending:
            folder = pending.pop()
            resolved = self.safe(folder)
            if resolved is None or resolved in seen:
                continue
            seen.add(resolved)
            try:
                entries = list(folder.iterdir())
            except OSError:
                self.issue("DIRECTORY_UNREADABLE", folder)
                continue
            for entry in entries:
                target = self.safe(entry)
                if target is None:
                    continue
                if target.is_dir():
                    pending.append(entry)
                elif target.is_file():
                    self.files.append(entry)
                else:
                    self.issue("UNSUPPORTED_FILESYSTEM_ENTRY", entry)

    def text(self, path: Path) -> str | None:
        if path in self.text_cache:
            return self.text_cache[path]
        resolved = self.safe(path)
        if resolved is None:
            return None
        try:
            result = resolved.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            self.issue("TEXT_NOT_READABLE_AS_UTF8", path)
            return None
        self.text_cache[path] = result
        return result

    def data(self, path: Path):
        if path in self.json_cache:
            return self.json_cache[path]
        text = self.text(path)
        if text is None:
            return None
        try:
            result = json.loads(text)
        except (ValueError, RecursionError):
            self.issue("INVALID_JSON", path)
            return None
        self.json_cache[path] = result
        return result

    def required_layout(self) -> None:
        readme = self.package / "README.md"
        resolved = self.safe(readme)
        if resolved is not None and not resolved.is_file():
            self.issue("README_NOT_A_FILE", readme)
        for name in ("cases", "inputs", "expected", "outputs", "reports"):
            path = self.package / name
            resolved = self.safe(path)
            if resolved is not None and not resolved.is_dir():
                self.issue("REQUIRED_DIRECTORY_NOT_A_DIRECTORY", path)

    def manifest(self) -> None:
        matches = [p for p in self.files if re.fullmatch(r"manifest_v.*\.json", p.name)]
        if len(matches) != 1:
            self.issue("MANIFEST_COUNT_NOT_ONE", self.package)
            return
        path = matches[0]
        if path.parent != self.package:
            self.issue("MANIFEST_NOT_AT_PACKAGE_ROOT", path)
        data = self.data(path)
        if not isinstance(data, dict):
            self.issue("MANIFEST_OBJECT_REQUIRED", path)
            return
        version = data.get("package_version")
        if not isinstance(version, str) or not re.fullmatch(SEMVER, version) or path.name != f"manifest_{version}.json":
            self.issue("MANIFEST_VERSION_MISMATCH", path)
        else:
            self.package_version = version
        cases = data.get("cases")
        if not isinstance(cases, list) or not cases:
            self.issue("MANIFEST_CASES_REQUIRED", path)
            cases = []
        seen = set()
        for case in cases:
            if not isinstance(case, str) or CASE_ID.fullmatch(case) is None:
                self.issue("INVALID_CASE_ID", path)
            elif case in seen:
                self.issue("DUPLICATE_CASE_ID", path)
            else:
                seen.add(case)
                self.case_ids.append(case)
        artifacts = data.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            self.issue("MANIFEST_ARTIFACTS_REQUIRED", path)
            return
        seen_paths, counts = set(), {(case, role): 0 for case in self.case_ids for role in ROLE_DIR}
        for item in artifacts:
            if not isinstance(item, dict):
                self.issue("ARTIFACT_OBJECT_REQUIRED", path)
                continue
            case, role, value = item.get("case_id"), item.get("role"), item.get("path")
            if not isinstance(case, str) or case not in self.case_ids or not isinstance(role, str) or role not in ROLE_DIR:
                self.issue("ARTIFACT_CASE_OR_ROLE_INVALID", path)
                continue
            if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute():
                self.issue("ARTIFACT_PATH_INVALID", path)
                continue
            parts = value.split("/")
            if any(part in ("", ".", "..") for part in parts) or len(parts) < 3 or parts[:2] != [ROLE_DIR[role], case]:
                self.issue("ARTIFACT_ROLE_DIRECTORY_MISMATCH", path)
                continue
            artifact_path = self.package.joinpath(*parts)
            resolved = self.safe(artifact_path)
            if resolved is None:
                continue
            canonical = os.path.normcase(str(resolved))
            if canonical in seen_paths:
                self.issue("DUPLICATE_ARTIFACT_PATH", artifact_path)
                continue
            seen_paths.add(canonical)
            if not resolved.is_file():
                self.issue("ARTIFACT_NOT_A_FILE", artifact_path)
                continue
            expected_size, expected_hash = item.get("bytes"), item.get("sha256")
            if type(expected_size) is not int or expected_size < 0 or not isinstance(expected_hash, str) or SHA256.fullmatch(expected_hash) is None:
                self.issue("ARTIFACT_SIZE_OR_HASH_INVALID", artifact_path)
                continue
            if "original_filename" in item and item["original_filename"] != parts[-1]:
                self.issue("ARTIFACT_ORIGINAL_FILENAME_MISMATCH", artifact_path)
            digest, size = hashlib.sha256(), 0
            try:
                with resolved.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(block)
                        digest.update(block)
            except OSError:
                self.issue("ARTIFACT_UNREADABLE", artifact_path)
                continue
            if size != expected_size:
                self.issue("ARTIFACT_BYTE_COUNT_MISMATCH", artifact_path)
            if digest.hexdigest() != expected_hash.lower():
                self.issue("ARTIFACT_SHA256_MISMATCH", artifact_path)
            counts[(case, role)] += 1
            self.artifacts.append({"case_id": case, "role": role, "sha256": expected_hash.lower()})
            self.counts["artifacts_checked"] += 1
        for (case, role), count in counts.items():
            if count == 0:
                self.issue(f"CASE_{role.upper()}_REQUIRED", self.package / "cases" / case)

    def case_documents(self) -> None:
        for folder, prefix in (("cases", "case"), ("expected", "expected")):
            entries = [p for p in self.files if p.parent == self.package / folder and p.suffix.lower() == ".md"]
            for case in self.case_ids:
                matches = [p for p in entries if re.fullmatch(rf"{prefix}_{re.escape(case)}_{SEMVER}\.md", p.name)]
                if len(matches) != 1:
                    self.issue("CASE_DOCUMENT_COUNT_NOT_ONE", self.package / folder / case)
            for path in entries:
                match = re.fullmatch(rf"{prefix}_(.+)_({SEMVER})\.md", path.name)
                if match and match.group(1) not in self.case_ids:
                    self.issue("DOCUMENT_CASE_NOT_IN_MANIFEST", path)

    def reports(self) -> None:
        report_root = self.package / "reports"
        if not any(p.parent == report_root and p.suffix.lower() == ".md" for p in self.files):
            self.issue("MARKDOWN_REPORT_REQUIRED", report_root)
        matches = [p for p in self.files if within(p, report_root) and re.fullmatch(r"file_checks_v.*\.json", p.name)]
        if len(matches) != 1:
            self.issue("FILE_CHECKS_COUNT_NOT_ONE", report_root)
            return
        path = matches[0]
        data = self.data(path)
        if not isinstance(data, (dict, list)):
            self.issue("FILE_CHECKS_CONTAINER_REQUIRED", path)
            return
        if isinstance(data, dict) and "version" in data and path.name != f"file_checks_{data['version']}.json":
            self.issue("FILE_CHECKS_VERSION_MISMATCH", path)
        covered = set()
        def visit(node):
            if isinstance(node, dict):
                if "case_id" in node:
                    case = node["case_id"]
                    if not isinstance(case, str) or case not in self.case_ids:
                        self.issue("REPORT_CASE_NOT_IN_MANIFEST", path)
                    else:
                        covered.add(case)
                        for field, role in (("output_sha256", "actual_output"), ("before_sha256", "input"), ("after_sha256", "actual_output")):
                            if field in node:
                                value = node[field]
                                hashes = {a["sha256"] for a in self.artifacts if a["case_id"] == case and a["role"] == role}
                                if not isinstance(value, str) or value.lower() not in hashes:
                                    self.issue("REPORT_ARTIFACT_HASH_MISMATCH", path)
                for value in node.values():
                    visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)
        visit(data)
        for case in self.case_ids:
            if case not in covered:
                self.issue("REPORT_CASE_RECORD_REQUIRED", self.package / "reports" / case)
        familiar = isinstance(data, dict) and isinstance(data.get("new_document"), dict) and isinstance(data.get("rewrite_document"), dict)
        if not familiar:
            self.notes.append({"rule": "REPORT_SCHEMA_REQUIRES_MANUAL_REVIEW", "file": self.name(path)})

    def markdown_links(self, path: Path, text: str) -> None:
        for destination in markdown_destinations(text):
            destination = html.unescape(destination).strip()
            try:
                parsed = urlsplit(destination)
                decoded = unquote(parsed.path)
                if PureWindowsPath(unquote(destination)).drive:
                    self.issue("MARKDOWN_ABSOLUTE_OR_UNC_LINK", path)
                    continue
                if parsed.scheme:
                    if parsed.scheme.lower() in ("http", "https", "mailto"):
                        self.counts["external_links_not_visited"] += 1
                    else:
                        self.issue("MARKDOWN_UNSUPPORTED_LINK_SCHEME", path)
                    continue
                if "\\" in decoded or decoded.startswith("/") or parsed.netloc:
                    self.issue("MARKDOWN_ABSOLUTE_OR_UNC_LINK", path)
                    continue
                if not decoded:
                    continue
                target = (path.parent / decoded).resolve(strict=False)
                if not within(target, self.share_root):
                    self.issue("MARKDOWN_LINK_OUTSIDE_SHARE_ROOT", path)
                elif not target.exists():
                    self.issue("MARKDOWN_LINK_TARGET_MISSING", path)
                self.counts["markdown_links_checked"] += 1
            except (OSError, ValueError, RuntimeError):
                self.issue("MARKDOWN_LINK_INVALID", path)

    def scan_texts(self) -> None:
        def strings(node):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for key, value in node.items():
                    yield str(key)
                    yield from strings(value)
            elif isinstance(node, list):
                for value in node:
                    yield from strings(value)
        for path in self.files:
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = self.text(path)
            if text is None:
                continue
            self.counts["text_files_scanned"] += 1
            contents = [text]
            if path.suffix.lower() == ".json":
                data = self.data(path)
                if data is not None:
                    contents.extend(strings(data))
            for content in contents:
                for rule, pattern in SECRET_RULES:
                    for match in pattern.finditer(content):
                        value = match.group(1) if match.lastindex else match.group(0)
                        if not placeholder(value):
                            self.issue(rule, path)
                            break
            if path.suffix.lower() == ".md":
                self.markdown_links(path, text)

    def run(self) -> dict:
        self.required_layout()
        self.inventory()
        self.manifest()
        self.case_documents()
        self.reports()
        self.scan_texts()
        return {
            "checker_version": VERSION,
            "scope": "read-only static delivery consistency; no business, visual, font, or runtime acceptance",
            "package": public_label(self.package.name),
            "package_version": self.package_version,
            "static_delivery_passed": not self.issues,
            "business_validation": "not_performed",
            "manual_review_required": True,
            "case_ids": [public_label(case) for case in self.case_ids],
            "counts": self.counts,
            "issues": self.issues,
            "manual_review_notes": self.notes,
        }


def main(argv=None) -> int:
    parser = Parser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path, help="Package directory to inspect.")
    parser.add_argument("--share-root", type=Path, help="Allowed local-link root; defaults to the package argument's parent.")
    try:
        args = parser.parse_args(argv)
        package = args.package.resolve(strict=True)
        share_root = (args.share_root if args.share_root is not None else args.package.parent).resolve(strict=True)
        if not package.is_dir() or not share_root.is_dir() or not within(package, share_root):
            raise ParameterProblem("INVALID_PACKAGE_OR_SHARE_ROOT")
    except (ParameterProblem, OSError, ValueError, RuntimeError):
        print(json.dumps({"checker_version": VERSION, "static_delivery_passed": False, "business_validation": "not_performed", "error": "INVALID_ARGUMENTS_OR_PACKAGE_BOUNDARY"}, ensure_ascii=True))
        return 2
    try:
        result = Audit(package, share_root).run()
    except Exception:
        print(json.dumps({"checker_version": VERSION, "static_delivery_passed": False, "business_validation": "not_performed", "error": "CHECKER_RUNTIME_ERROR_NO_CONTENT_ECHOED"}, ensure_ascii=True))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["static_delivery_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
