# login.py

import os
import truststore

# Inject Windows' native certificate store into Python's ssl module.
# Required because the corporate Zscaler proxy re-signs TLS traffic with its
# own CA, which is trusted by Windows but not by Python's bundled certifi store.
truststore.inject_into_ssl()

from kiteconnect import KiteConnect

def load_env_file(env_path: str = None, override: bool = False) -> None:
    if env_path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_env = os.path.join(os.path.dirname(script_dir), ".env")
        script_env = os.path.join(script_dir, ".env")
        env_path = project_env if os.path.exists(project_env) else script_env
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            # Handle UTF-8 BOM on first key so API_KEY is loaded correctly.
            key = key.strip().lstrip("\ufeff")
            value = value.strip().strip('"').strip("'")
            if key and (override or key not in os.environ):
                os.environ[key] = value


load_env_file()

def get_kite():
    load_env_file(override=True)
    api_key = os.getenv("API_KEY")
    access_token = os.getenv("ACCESS_TOKEN")
    if not api_key or not access_token:
        raise ValueError("API_KEY or ACCESS_TOKEN is missing in environment/.env")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite