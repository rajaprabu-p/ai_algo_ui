"""Deployment manifest: catch missed or stale files after a FileZilla upload.

`python scripts/make_manifest.py` (run on your PC before uploading) writes
scripts/strategy/MANIFEST.json with a fingerprint of every file the strategy
needs. At startup the engine compares the files on disk with it:

  * all files match            -> one "[DEPLOY CHECK] ... OK" line
  * missing / changed / extra  -> each problem is listed;
                                  LIVE_ORDER refuses to start (exit code 4),
                                  PAPERTRADE_LIVE and BACKTEST warn and continue
  * SKIP_DEPLOY_CHECK=1        -> emergency override: LIVE_ORDER starts anyway, with a warning

Fingerprints ignore line endings, because FileZilla uploads .py files in ASCII
mode and converts Windows (CRLF) line endings to Linux (LF) ones.

The manifest also fingerprints the dashboard files ("app_files": app.py,
templates/, the top-level scripts). The engine ignores them; the dashboard
footer shows the version and whether those files match (check_app).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "MANIFEST.json"
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = PACKAGE_DIR / MANIFEST_NAME
# Files outside the strategy/ package that the engine needs (relative to scripts/).
EXTRA_FILES = ("v28_strategy_main.py", "v30_strategy_main.py", "editable_config.py")
PROJECT_DIR = SCRIPTS_DIR.parent
BLOCKING_MODES = {"LIVE_ORDER"}
EXIT_CODE_BLOCKED = 4


def fingerprint(path: Path) -> str:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def tracked_files(scripts_dir: Path = SCRIPTS_DIR) -> list[str]:
    """Every file the manifest covers, as POSIX paths relative to scripts/."""
    package = scripts_dir / "strategy"
    files = [
        p.relative_to(scripts_dir).as_posix()
        for p in package.rglob("*.py")
        if "__pycache__" not in p.parts
    ]
    files += [name for name in EXTRA_FILES if (scripts_dir / name).exists()]
    return sorted(set(files))


def app_tracked_files(project_dir: Path = PROJECT_DIR) -> list[str]:
    """Dashboard files (POSIX paths relative to the project): app.py, templates, top-level scripts.

    Strategy files are in the "files" section already; tests and data are left out.
    """
    files = ["app.py"] if (project_dir / "app.py").exists() else []
    templates = project_dir / "templates"
    if templates.is_dir():
        files += [p.relative_to(project_dir).as_posix() for p in templates.glob("*.html")]
    scripts = project_dir / "scripts"
    if scripts.is_dir():
        files += [p.relative_to(project_dir).as_posix() for p in scripts.glob("*.py") if p.name not in EXTRA_FILES]
        if (scripts / "market_holidays.txt").exists():
            files.append("scripts/market_holidays.txt")
    return sorted(set(files))


def _git_version(scripts_dir: Path) -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=scripts_dir, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", "."], cwd=scripts_dir, capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        return f"{sha}{'-dirty' if dirty else ''}"
    except Exception:
        return "nogit"


def build_manifest(scripts_dir: Path = SCRIPTS_DIR, version: str | None = None) -> dict:
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return {
        "version": version or f"{stamp}_{_git_version(scripts_dir)}",
        "created": datetime.now().isoformat(timespec="seconds"),
        "files": {rel: fingerprint(scripts_dir / rel) for rel in tracked_files(scripts_dir)},
        "app_files": {rel: fingerprint(scripts_dir.parent / rel) for rel in app_tracked_files(scripts_dir.parent)},
    }


def write_manifest(scripts_dir: Path = SCRIPTS_DIR, version: str | None = None) -> tuple[Path, dict]:
    manifest = build_manifest(scripts_dir, version)
    path = scripts_dir / "strategy" / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return path, manifest


@dataclass
class CheckResult:
    version: str = ""
    checked: int = 0
    missing: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not (self.error or self.missing or self.changed or self.extra)

    def problems(self) -> list[str]:
        lines = [self.error] if self.error else []
        lines += [f"missing on server: {p}" for p in self.missing]
        lines += [f"differs from manifest (old or partial upload?): {p}" for p in self.changed]
        lines += [f"not in manifest (leftover file? delete it, or rerun make_manifest.py): {p}" for p in self.extra]
        return lines


def check(scripts_dir: Path = SCRIPTS_DIR) -> CheckResult:
    path = scripts_dir / "strategy" / MANIFEST_NAME
    if not path.exists():
        return CheckResult(error=f"no {MANIFEST_NAME} in {path.parent} (run scripts/make_manifest.py before uploading)")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        expected = dict(manifest["files"])
    except Exception as exc:
        return CheckResult(error=f"cannot read {path}: {exc}")

    result = CheckResult(version=str(manifest.get("version", "?")))
    for rel, digest in sorted(expected.items()):
        target = scripts_dir / rel
        if not target.exists():
            result.missing.append(rel)
        elif fingerprint(target) != digest:
            result.changed.append(rel)
        result.checked += 1
    result.extra = [rel for rel in tracked_files(scripts_dir) if rel not in expected]
    return result


def check_app(scripts_dir: Path = SCRIPTS_DIR) -> CheckResult:
    """Compare the dashboard files with the manifest's "app_files" (the engine does not use this)."""
    path = scripts_dir / "strategy" / MANIFEST_NAME
    project_dir = scripts_dir.parent
    if not path.exists():
        return CheckResult(error=f"no {MANIFEST_NAME} (run scripts/make_manifest.py)")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult(error=f"cannot read {path}: {exc}")
    result = CheckResult(version=str(manifest.get("version", "?")))
    if "app_files" not in manifest:
        result.error = "manifest has no dashboard files (made by an older make_manifest.py; run it again)"
        return result
    expected = dict(manifest["app_files"])
    for rel, digest in sorted(expected.items()):
        target = project_dir / rel
        if not target.exists():
            result.missing.append(rel)
        elif fingerprint(target) != digest:
            result.changed.append(rel)
        result.checked += 1
    result.extra = [rel for rel in app_tracked_files(project_dir) if rel not in expected]
    return result


def verify_or_exit(run_mode: str, scripts_dir: Path = SCRIPTS_DIR, environ=None) -> CheckResult:
    """Print the check result; raise SystemExit(4) when LIVE_ORDER must not start."""
    environ = os.environ if environ is None else environ
    result = check(scripts_dir)
    mode = str(run_mode or "").upper()
    if result.ok:
        print(f"[DEPLOY CHECK] version={result.version}, {result.checked} files OK")
        return result

    print(f"[DEPLOY CHECK] PROBLEM version={result.version or 'unknown'}:")
    for line in result.problems():
        print(f"[DEPLOY CHECK]   - {line}")
    if mode in BLOCKING_MODES:
        if str(environ.get("SKIP_DEPLOY_CHECK", "")).strip().lower() in {"1", "true", "yes"}:
            print("[DEPLOY CHECK] SKIP_DEPLOY_CHECK is set: starting LIVE_ORDER anyway. Remove it once files are fixed.")
            return result
        print(
            f"[DEPLOY CHECK] {mode} blocked. Upload the whole scripts/strategy/ folder and the launchers again "
            "(after running make_manifest.py), or set SKIP_DEPLOY_CHECK=1 in .env to override."
        )
        raise SystemExit(EXIT_CODE_BLOCKED)
    print(f"[DEPLOY CHECK] {mode or 'this mode'} continues (only LIVE_ORDER is blocked).")
    return result
