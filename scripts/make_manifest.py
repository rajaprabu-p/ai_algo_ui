"""Write scripts/strategy/MANIFEST.json before uploading the strategy to the server.

Run it on your PC after your last code change, then upload with FileZilla:
the whole scripts/strategy/ folder (it contains MANIFEST.json) plus
scripts/v28_strategy_main.py, scripts/v30_strategy_main.py and
scripts/editable_config.py if they changed.

    venv\\Scripts\\python.exe scripts\\make_manifest.py
    venv\\Scripts\\python.exe scripts\\make_manifest.py --check    # compare files here with the manifest
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategy import deploy_check  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only compare the files here with the existing manifest")
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
