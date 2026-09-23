#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "build_meta.py"
JS_TARGET = ROOT / "src" / "build_meta.js"
SKIP_DIRS = {".git", ".venv", ".venv-workers", "node_modules", "python_modules", "backups", "playwright-report", "test-results", "__pycache__", ".pytest_cache", ".ruff_cache"}

def tree_fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path in {TARGET, JS_TARGET} or any(part in SKIP_DIRS for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        digest.update(rel.encode())
        try:
            digest.update(path.read_bytes())
        except Exception:
            continue
    return digest.hexdigest()[:12]

app_meta_text = (ROOT / "src" / "app_meta.py").read_text()
app_version = __import__("re").search(r'APP_VERSION = "([^"]+)"', app_meta_text).group(1)
api_schema = __import__("re").search(r'API_SCHEMA_VERSION = "([^"]+)"', app_meta_text).group(1)
fingerprint = tree_fingerprint()
try:
    git_sha = subprocess.check_output(["git", "rev-parse", "--short=12", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip())
    revision = f"{git_sha}-dirty-{fingerprint}" if dirty else git_sha
except Exception:
    revision = f"tree-{fingerprint}"

ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
safe_revision = revision.replace("/", "_")
build_id = f"{app_version}-{safe_revision}-{ts.replace(':','').replace('-','')}"
TARGET.write_text(
    '"""Generated deployment build metadata."""\n\n'
    f'BUILD_SHA = {revision!r}\n'
    f'BUILD_DEPLOYED_AT = {ts!r}\n'
    f'BUILD_ID = {build_id!r}\n'
)
JS_TARGET.write_text(
    "// Generated deployment build metadata.\n"
    f"export const APP_VERSION = {app_version!r};\n"
    f"export const API_SCHEMA_VERSION = {api_schema!r};\n"
    f"export const BUILD_SHA = {revision!r};\n"
    f"export const BUILD_DEPLOYED_AT = {ts!r};\n"
    f"export const BUILD_ID = {build_id!r};\n"
)
print(f"Stamped build {build_id}")
