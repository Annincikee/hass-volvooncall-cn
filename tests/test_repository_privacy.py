"""Prevent packet captures and production identifiers from reaching GitHub."""

from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
}
CAPTURE_SUFFIXES = {".har", ".pcap", ".pcapng"}
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_PATTERNS = {
    "Chinese mobile number": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "production-like VIN": re.compile(
        r"(?<![A-Z0-9])"
        r"(?=[A-HJ-NPR-Z0-9]{0,16}[A-HJ-NPR-Z])"
        r"[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])"
    ),
    "long captured identifier": re.compile(
        r"(?<![\d.])\d{18,}(?!\d)"
    ),
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}


def _repository_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def test_repository_contains_no_packet_captures():
    captures = [
        str(path.relative_to(REPO_ROOT))
        for path in _repository_files()
        if path.suffix.lower() in CAPTURE_SUFFIXES
    ]

    assert captures == []


def test_repository_contains_no_production_identifiers():
    findings = []
    for path in _repository_files():
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{path.relative_to(REPO_ROOT)}: {label}")

    assert findings == []
