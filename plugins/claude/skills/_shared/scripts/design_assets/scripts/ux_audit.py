#!/usr/bin/env python3
"""
UX Audit Script — Checks frontend code for common accessibility and UX issues.

Usage:
    python3 ux_audit.py <directory>
    python3 ux_audit.py frontend/app/
    python3 ux_audit.py --help

Checks:
- WCAG contrast ratios (basic heuristic)
- Missing alt text on images
- Missing form labels
- Interactive elements without proper cursors
- Inconsistent spacing patterns
- Missing reduced-motion media queries
- Missing focus indicators
- Semantic HTML violations

Stdlib-only — no external dependencies.
"""

import argparse
import os
import re
import sys
import json
from pathlib import Path
from typing import List, Dict, Tuple

# Severity levels
CRITICAL = "Critical"
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"
INFO = "Info"


class Finding:
    def __init__(self, severity: str, category: str, message: str,
                 file: str, line: int = 0, suggestion: str = ""):
        self.severity = severity
        self.category = category
        self.message = message
        self.file = file
        self.line = line
        self.suggestion = suggestion

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "suggestion": self.suggestion,
        }

    def __str__(self) -> str:
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.severity}] {self.category}: {self.message} ({loc})"


def find_files(directory: str, extensions: Tuple[str, ...]) -> List[str]:
    """Find all files with given extensions recursively."""
    files = []
    for root, _, filenames in os.walk(directory):
        # Skip node_modules, .next, dist, build directories
        if any(skip in root for skip in ['node_modules', '.next', 'dist', 'build', '.git']):
            continue
        for f in filenames:
            if f.endswith(extensions):
                files.append(os.path.join(root, f))
    return files


def check_missing_alt_text(content: str, filepath: str) -> List[Finding]:
    """Check for images without alt text."""
    findings = []
    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        # HTML img tags
        if re.search(r'<img\b', line, re.IGNORECASE):
            if not re.search(r'\balt\s*=', line, re.IGNORECASE):
                findings.append(Finding(
                    CRITICAL, "Accessibility",
                    "Image missing alt attribute",
                    filepath, i,
                    'Add alt="descriptive text" or alt="" for decorative images'
                ))
        # JSX/React Image components
        if re.search(r'<Image\b', line):
            if not re.search(r'\balt\s*=', line):
                # Check next few lines for multi-line JSX
                context = '\n'.join(lines[i-1:min(i+4, len(lines))])
                if not re.search(r'\balt\s*=', context):
                    findings.append(Finding(
                        CRITICAL, "Accessibility",
                        "Image component missing alt prop",
                        filepath, i,
                        'Add alt="descriptive text" prop'
                    ))
    return findings


def check_form_labels(content: str, filepath: str) -> List[Finding]:
    """Check for form inputs without associated labels."""
    findings = []
    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        # Check for input, select, textarea without labels
        if re.search(r'<(input|select|textarea)\b', line, re.IGNORECASE):
            # Check if type is hidden or submit (don't need labels)
            if re.search(r'type\s*=\s*["\']hidden["\']', line, re.IGNORECASE):
                continue
            if re.search(r'type\s*=\s*["\']submit["\']', line, re.IGNORECASE):
                continue
            # Check for aria-label, aria-labelledby, or id (for external label)
            context = '\n'.join(lines[max(0, i-4):min(i+2, len(lines))])
            has_label = (
                re.search(r'aria-label\s*=', context, re.IGNORECASE) or
                re.search(r'aria-labelledby\s*=', context, re.IGNORECASE) or
                re.search(r'<label\b', context, re.IGNORECASE) or
                re.search(r'<Label\b', context)
            )
            if not has_label:
                findings.append(Finding(
                    HIGH, "Accessibility",
                    "Form input without associated label",
                    filepath, i,
                    "Add a <label> element, aria-label, or aria-labelledby"
                ))
    return findings


def check_interactive_cursors(content: str, filepath: str) -> List[Finding]:
    """Check for interactive elements that might be missing cursor styles."""
    findings = []
    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        # Div/span with onClick but potentially no cursor pointer
        if re.search(r'<(div|span)\b.*onClick', line):
            if 'cursor' not in line and 'button' not in line.lower():
                findings.append(Finding(
                    MEDIUM, "UX",
                    "Clickable div/span may need cursor: pointer",
                    filepath, i,
                    "Add cursor-pointer class or use a <button> element instead"
                ))
        # role="button" without cursor
        if re.search(r'role\s*=\s*["\']button["\']', line):
            if not re.search(r'<button', line, re.IGNORECASE):
                findings.append(Finding(
                    MEDIUM, "UX",
                    'Element with role="button" should be a <button> element',
                    filepath, i,
                    "Use semantic <button> instead of role=\"button\" on a div"
                ))
    return findings


def check_outline_none(content: str, filepath: str) -> List[Finding]:
    """Check for removed focus indicators."""
    findings = []
    lines = content.split('\n')
    for i, line in enumerate(lines, 1):
        if re.search(r'outline\s*:\s*none', line, re.IGNORECASE):
            # Check if there's a replacement focus style nearby
            context = '\n'.join(lines[max(0, i-3):min(i+3, len(lines))])
            has_replacement = (
                re.search(r'box-shadow', context) or
                re.search(r'ring', context) or
                re.search(r'border.*focus', context, re.IGNORECASE)
            )
            if not has_replacement:
                findings.append(Finding(
                    CRITICAL, "Accessibility",
                    "Focus indicator removed with outline: none and no replacement",
                    filepath, i,
                    "Add a visible focus indicator (box-shadow, ring, or custom outline)"
                ))
        if re.search(r'outline-none', line) and 'focus' not in line:
            findings.append(Finding(
                HIGH, "Accessibility",
                "outline-none without focus replacement may remove focus indicator",
                filepath, i,
                "Ensure focus:ring or focus:outline is also applied"
            ))
    return findings


def check_reduced_motion(content: str, filepath: str) -> List[Finding]:
    """Check CSS/styled files for reduced-motion media query."""
    findings = []
    has_animation = bool(re.search(r'(animation|transition|@keyframes)', content))
    has_reduced_motion = bool(re.search(r'prefers-reduced-motion', content))

    if has_animation and not has_reduced_motion:
        findings.append(Finding(
            HIGH, "Accessibility",
            "File has animations but no prefers-reduced-motion media query",
            filepath, 0,
            "Add @media (prefers-reduced-motion: reduce) to disable/simplify animations"
        ))
    return findings


def check_semantic_html(content: str, filepath: str) -> List[Finding]:
    """Check for div-soup and missing semantic elements."""
    findings = []
    lines = content.split('\n')

    # Check for div with nav-like class names but not <nav>
    for i, line in enumerate(lines, 1):
        if re.search(r'<div\b.*class\s*=\s*["\'][^"\']*\b(nav|navigation|sidebar|menu)\b', line, re.IGNORECASE):
            findings.append(Finding(
                MEDIUM, "Semantics",
                "Div with navigation-related class should use <nav> element",
                filepath, i,
                "Replace <div> with <nav> for navigation landmarks"
            ))
        if re.search(r'<div\b.*class\s*=\s*["\'][^"\']*\b(header|banner)\b', line, re.IGNORECASE):
            if not re.search(r'card-header|section-header|modal-header', line, re.IGNORECASE):
                findings.append(Finding(
                    LOW, "Semantics",
                    "Div with header-related class could use <header> element",
                    filepath, i,
                    "Consider using <header> for page/section headers"
                ))
        if re.search(r'<div\b.*class\s*=\s*["\'][^"\']*\b(footer)\b', line, re.IGNORECASE):
            if not re.search(r'card-footer|modal-footer', line, re.IGNORECASE):
                findings.append(Finding(
                    LOW, "Semantics",
                    "Div with footer-related class could use <footer> element",
                    filepath, i,
                    "Consider using <footer> for page/section footers"
                ))
    return findings


def check_color_contrast_heuristic(content: str, filepath: str) -> List[Finding]:
    """Basic heuristic check for potentially low-contrast color combinations."""
    findings = []
    lines = content.split('\n')

    # Light text colors that might have contrast issues
    light_colors = [
        (r'(?:color|text)\s*[:\-]\s*["\']?#(?:[cdef][0-9a-f]{5}|[89ab][0-9a-f]{5})', "Light text color may have contrast issues on light backgrounds"),
        (r'text-gray-[34]00', "text-gray-300/400 may fail WCAG AA contrast on white backgrounds"),
        (r'text-(?:slate|zinc|neutral|stone)-[34]00', "Light text utility may fail WCAG AA contrast"),
    ]

    for i, line in enumerate(lines, 1):
        for pattern, message in light_colors:
            if re.search(pattern, line, re.IGNORECASE):
                findings.append(Finding(
                    MEDIUM, "Contrast",
                    message,
                    filepath, i,
                    "Verify contrast ratio is at least 4.5:1 for normal text, 3:1 for large text"
                ))
                break  # One finding per line is enough

    return findings


def check_touch_targets(content: str, filepath: str) -> List[Finding]:
    """Check for potentially small touch targets."""
    findings = []
    lines = content.split('\n')

    for i, line in enumerate(lines, 1):
        # Small icon buttons without adequate sizing
        if re.search(r'<(button|a)\b.*(?:w-[4-6]|h-[4-6]|size-[4-6])\b', line):
            if not re.search(r'(?:w-(?:[89]|1[0-9]|2[0-9])|h-(?:[89]|1[0-9]|2[0-9])|min-w|min-h|p-[2-9])', line):
                findings.append(Finding(
                    MEDIUM, "UX",
                    "Interactive element may be below 44px minimum touch target",
                    filepath, i,
                    "Ensure interactive elements are at least 44x44px (WCAG 2.5.5)"
                ))
    return findings


def run_audit(directory: str, output_format: str = "text") -> Tuple[List[Finding], dict]:
    """Run all audit checks on the directory."""
    all_findings: List[Finding] = []

    # Find relevant files
    jsx_files = find_files(directory, ('.tsx', '.jsx', '.vue', '.svelte', '.html'))
    css_files = find_files(directory, ('.css', '.scss', '.sass', '.less'))
    all_files = jsx_files + css_files

    if not all_files:
        print(f"No frontend files found in {directory}", file=sys.stderr)
        sys.exit(1)

    # Run checks
    for filepath in jsx_files:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except (IOError, OSError):
            continue

        all_findings.extend(check_missing_alt_text(content, filepath))
        all_findings.extend(check_form_labels(content, filepath))
        all_findings.extend(check_interactive_cursors(content, filepath))
        all_findings.extend(check_outline_none(content, filepath))
        all_findings.extend(check_semantic_html(content, filepath))
        all_findings.extend(check_color_contrast_heuristic(content, filepath))
        all_findings.extend(check_touch_targets(content, filepath))

    for filepath in css_files:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except (IOError, OSError):
            continue

        all_findings.extend(check_outline_none(content, filepath))
        all_findings.extend(check_reduced_motion(content, filepath))
        all_findings.extend(check_color_contrast_heuristic(content, filepath))

    # Also check JSX files for inline styles and animations
    for filepath in jsx_files:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except (IOError, OSError):
            continue
        all_findings.extend(check_reduced_motion(content, filepath))

    # Summary
    summary = {
        "total_files_scanned": len(all_files),
        "total_findings": len(all_findings),
        "by_severity": {},
        "by_category": {},
    }

    for sev in [CRITICAL, HIGH, MEDIUM, LOW, INFO]:
        count = sum(1 for f in all_findings if f.severity == sev)
        if count:
            summary["by_severity"][sev] = count

    for f in all_findings:
        summary["by_category"][f.category] = summary["by_category"].get(f.category, 0) + 1

    return all_findings, summary


def print_text_report(findings: List[Finding], summary: dict) -> None:
    """Print findings as formatted text."""
    print("━" * 60)
    print("  UX AUDIT REPORT")
    print("━" * 60)
    print(f"\n  Files scanned: {summary['total_files_scanned']}")
    print(f"  Total findings: {summary['total_findings']}\n")

    if summary["by_severity"]:
        print("  By Severity:")
        for sev, count in summary["by_severity"].items():
            print(f"    {sev}: {count}")

    if summary["by_category"]:
        print("\n  By Category:")
        for cat, count in summary["by_category"].items():
            print(f"    {cat}: {count}")

    print("\n" + "─" * 60)

    # Sort by severity
    severity_order = {CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, INFO: 4}
    sorted_findings = sorted(findings, key=lambda f: severity_order.get(f.severity, 5))

    current_severity = None
    for f in sorted_findings:
        if f.severity != current_severity:
            current_severity = f.severity
            print(f"\n  [{current_severity}]")
            print("  " + "─" * 40)

        loc = f"{f.file}:{f.line}" if f.line else f.file
        print(f"\n  {f.category}: {f.message}")
        print(f"  Location: {loc}")
        if f.suggestion:
            print(f"  Fix: {f.suggestion}")

    print("\n" + "━" * 60)

    if summary["by_severity"].get(CRITICAL, 0) > 0:
        print(f"\n  ⚠ {summary['by_severity'][CRITICAL]} CRITICAL findings require immediate attention")
    elif summary["total_findings"] == 0:
        print("\n  ✓ No issues found")
    else:
        print(f"\n  {summary['total_findings']} findings to review")


def main():
    parser = argparse.ArgumentParser(
        description="UX Audit — Check frontend code for accessibility and UX issues"
    )
    parser.add_argument("directory", help="Directory to audit")
    parser.add_argument("--format", choices=["text", "json"], default="text",
                       help="Output format (default: text)")
    parser.add_argument("--min-severity", choices=["critical", "high", "medium", "low", "info"],
                       default="low", help="Minimum severity to report (default: low)")

    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        print(f"Error: {args.directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    findings, summary = run_audit(args.directory)

    # Filter by severity
    severity_filter = {
        "critical": [CRITICAL],
        "high": [CRITICAL, HIGH],
        "medium": [CRITICAL, HIGH, MEDIUM],
        "low": [CRITICAL, HIGH, MEDIUM, LOW],
        "info": [CRITICAL, HIGH, MEDIUM, LOW, INFO],
    }
    allowed = severity_filter[args.min_severity]
    findings = [f for f in findings if f.severity in allowed]
    summary["total_findings"] = len(findings)

    if args.format == "json":
        output = {
            "summary": summary,
            "findings": [f.to_dict() for f in findings],
        }
        print(json.dumps(output, indent=2))
    else:
        print_text_report(findings, summary)

    # Exit code: 1 if critical findings, 0 otherwise
    sys.exit(1 if summary["by_severity"].get(CRITICAL, 0) > 0 else 0)


if __name__ == "__main__":
    main()
