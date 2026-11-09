"""Set the dashboard login: writes a password hash and a new session key to .env.

Usage (from the project root):
    python scripts/set_login_password.py

The password is read without echo and only its hash is stored. Restart the app
afterwards; existing browser sessions are logged out because the key changes.
"""

import getpass
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
MIN_PASSWORD_LENGTH = 10


def read_env_lines() -> list:
    if not os.path.exists(ENV_PATH):
        return []
    with open(ENV_PATH, "r", encoding="utf-8-sig") as env_file:
        return env_file.read().splitlines()


def current_value(lines: list, key: str) -> str:
    for line in lines:
        if line.strip().startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main() -> None:
    lines = read_env_lines()
    default_user = current_value(lines, "LOGIN_USERNAME")
    prompt = f"Username [{default_user}]: " if default_user else "Username: "
    username = input(prompt).strip() or default_user
    if not username:
        sys.exit("A username is required.")

    password = getpass.getpass("New password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
    if getpass.getpass("Repeat password: ") != password:
        sys.exit("Passwords do not match; nothing was changed.")

    # Single quotes keep the '$' in the hash literal if the file is ever sourced by a shell.
    updates = {
        "LOGIN_USERNAME": username,
        "LOGIN_PASSWORD_HASH": f"'{generate_password_hash(password)}'",
        "APP_SECRET_KEY": secrets.token_hex(32),
    }
    remove = {"LOGIN_PASSWORD"}  # the old plain-text password

    out, written = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if key in remove:
            continue
        if key in updates:
            out.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in updates.items() if key not in written)

    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as env_file:
        env_file.write("\n".join(out) + "\n")
    if os.name != "nt":
        os.chmod(ENV_PATH, 0o600)
    print(f"Login updated in {ENV_PATH}. Restart the app to use it.")


if __name__ == "__main__":
    main()
