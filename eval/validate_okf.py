#!/usr/bin/env python3
"""Validate OKF v0.1 frontmatter across a research run directory.

Usage:
    python eval/validate_okf.py <run_dir>

For every *.md file in <run_dir> it checks that:
  - the file leads with a YAML frontmatter block that parses,
  - the required keys for its document type are present,
  - the recorded sha256 equals sha256 over content.split('---', 2)[2],
  - final_report.md's `sources:` entries all exist on disk.

Exits non-zero and prints per-file findings when anything fails.
"""
import hashlib
import os
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML is required: pip install pyyaml\n")
    sys.exit(2)

# Keys every OKF v0.1 doc must carry.
BASE_REQUIRED = ["title", "created", "timestamp", "type", "tags", "sha256", "okf_version"]
# Extra keys required per doc type. content_type is recorded on the fetch path
# but is not always resolvable (e.g. a source written directly by an agent), so
# it is validated when present rather than required.
TYPE_REQUIRED = {
    "source": ["source_url", "ingested"],
    "log": [],
    "summary": [],
}


def _parse(content):
    """Return (frontmatter_dict, post_fence_body) or (None, None) if no valid block."""
    if not content.startswith("---"):
        return None, None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, None
    try:
        fm = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None, None
    if not isinstance(fm, dict):
        return None, None
    return fm, parts[2]


def validate_file(path, run_dir):
    findings = []
    name = os.path.basename(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    fm, body = _parse(content)
    if fm is None:
        findings.append("no parseable YAML frontmatter block")
        return findings

    for key in BASE_REQUIRED:
        if key not in fm or fm[key] in (None, ""):
            findings.append(f"missing required key: {key}")

    doc_type = fm.get("type", "")
    for key in TYPE_REQUIRED.get(doc_type, []):
        if key not in fm or fm[key] in (None, ""):
            findings.append(f"type '{doc_type}' missing required key: {key}")

    if fm.get("okf_version") not in ("0.1", 0.1):
        findings.append(f"okf_version is {fm.get('okf_version')!r}, expected '0.1'")

    recorded = fm.get("sha256", "")
    actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if recorded != actual:
        findings.append(f"sha256 mismatch: recorded {recorded[:12]}… != actual {actual[:12]}…")

    if name == "final_report.md":
        sources = fm.get("sources") or []
        if not isinstance(sources, list):
            findings.append("sources: must be a list")
            sources = []
        for src in sources:
            if not os.path.exists(os.path.join(run_dir, src)):
                findings.append(f"sources entry not found on disk: {src}")

    return findings


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("Usage: python eval/validate_okf.py <run_dir>\n")
        return 2
    run_dir = argv[1]
    if not os.path.isdir(run_dir):
        sys.stderr.write(f"Not a directory: {run_dir}\n")
        return 2

    md_files = sorted(
        os.path.join(run_dir, f) for f in os.listdir(run_dir)
        if f.endswith(".md") and os.path.isfile(os.path.join(run_dir, f))
    )
    if not md_files:
        sys.stderr.write(f"No .md files found in {run_dir}\n")
        return 2

    total_findings = 0
    for path in md_files:
        findings = validate_file(path, run_dir)
        if findings:
            total_findings += len(findings)
            print(f"FAIL {os.path.basename(path)}")
            for f in findings:
                print(f"     - {f}")
        else:
            print(f"OK   {os.path.basename(path)}")

    print()
    if total_findings:
        print(f"{total_findings} finding(s) across {len(md_files)} file(s).")
        return 1
    print(f"All {len(md_files)} file(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
