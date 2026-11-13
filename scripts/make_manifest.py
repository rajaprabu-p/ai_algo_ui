"""Write scripts/strategy/MANIFEST.json before uploading the strategy to the server.

Run it on your PC after your last code change, then upload with FileZilla:
the whole scripts/strategy/ folder (it contains MANIFEST.json) plus
scripts/v28_strategy_main.py, scripts/v30_strategy_main.py and
scripts/editable_config.py if they changed.

    venv\\Scripts\\python.exe scripts\\make_manifest.py
    venv\\Scripts\\python.exe scripts\\make_manifest.py --check    # compare files here with the manifest
    venv\\Scripts\\python.exe scripts\\make_manifest.py --since 2026-11-13_1825_6a2485f
        # files to upload when the server footer shows that version (also rewrites the manifest)
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy import deploy_check  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only compare the files here with the existing manifest")
    parser.add_argument("--since", metavar="VERSION",
                        help="Version shown in the server's footer: list the files to upload to bring it up to date")
    args = parser.parse_args()

    if args.check:
        result, app = deploy_check.check(), deploy_check.check_app()
        if result.ok and app.ok:
            print(f"OK: version={result.version}, {result.checked} strategy + {app.checked} dashboard files match")
            return 0
        print(f"Manifest problems (version={result.version or 'unknown'}):")
        for line in result.problems() + app.problems():
            print(f"  - {line}")
        return 1

    since_files = None
    if args.since:
        since_files = files_changed_since(args.since)
        if since_files is None:
            return 1

    path, manifest = deploy_check.write_manifest()
    print(f"Wrote {path}")
    print(f"version={manifest['version']}  strategy files={len(manifest['files'])}  dashboard files={len(manifest['app_files'])}")
    for rel in manifest["files"]:
        print(f"  {rel}")
    print("  (dashboard: " + ", ".join(manifest["app_files"]) + ")")
    if manifest["version"].endswith("-dirty"):
        print("Note: you have uncommitted changes; consider committing before deploying.")
    print("\nUpload with FileZilla: the whole scripts/strategy/ folder, plus any changed files listed outside it.")
    print("The dashboard footer shows this version and whether every file on the server matches it.")
    if since_files is not None:
        print(f"\nTo update the server from {args.since} to {manifest['version']}, upload these "
              f"{len(since_files)} file(s) to /root/ai_algo_ui (same folders):")
        for rel in since_files:
            print(f"  {rel}")
        if any(rel == "app.py" for rel in since_files):
            print("Then restart Flask (app.py changed).")
        if any(rel.startswith("scripts/strategy/") and not rel.endswith("MANIFEST.json") or
               rel in {"scripts/v28_strategy_main.py", "scripts/v30_strategy_main.py", "scripts/editable_config.py"}
               for rel in since_files):
            print("Then restart the paper sessions (strategy files changed).")
    return 0


def files_changed_since(version: str):
    """Deployed files changed between the version's commit and the working tree here (None on error)."""
    commit = deploy_check.commit_from_version(version)
    if not commit:
        print(f"--since: no commit hash in '{version}' (copy the whole version from the footer, e.g. 2026-11-13_1825_6a2485f)")
        return None
    root = Path(__file__).resolve().parent.parent

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30)

    if git("cat-file", "-e", f"{commit}^{{commit}}").returncode != 0:
        print(f"--since: commit {commit} is not in this repository (pull first?)")
        return None
    changed = git("diff", "--name-only", commit, "--").stdout.split()            # committed + uncommitted changes
    changed += git("ls-files", "--others", "--exclude-standard").stdout.split()   # new files not yet added
    if version.strip().endswith("-dirty"):
        print(f"Note: {version} was made with uncommitted changes, so the list may miss files changed then.")
    return deploy_check.upload_list(changed)


if __name__ == "__main__":
    sys.exit(main())
