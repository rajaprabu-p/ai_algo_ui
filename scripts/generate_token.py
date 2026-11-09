# generate_token.py
# Use this script to generate a fresh access token

import os
import ssl
import certifi
from kiteconnect import KiteConnect
import webbrowser

import truststore
truststore.inject_into_ssl()

# Fix SSL certificate verification on Windows
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
ssl._create_default_https_context = ssl.create_default_context

def load_env_file(env_path: str = None, override: bool = True) -> None:
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

def generate_access_token():
    """
    Generate a fresh access token for Zerodha KiteConnect API
    """
    kite = KiteConnect(api_key=os.getenv("API_KEY"))
    
    # Step 1: Get login URL
    login_url = kite.login_url()
    print("Opening login URL in browser...")
    print(f"Login URL: {login_url}")
    webbrowser.open(login_url)
    
    # Step 2: After login, you'll be redirected with a request token
    request_token = input("\nEnter the request_token from the redirect URL: ")
    
    # Step 3: Generate access token using request token
    try:
        data = kite.generate_session(request_token, api_secret=os.getenv("API_SECRET"))
        access_token = data['access_token']
        
        print(f"\n✓ Access Token Generated Successfully!")
        print(f"Access Token: {access_token}")

        # Auto-save to .env
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_env = os.path.join(os.path.dirname(script_dir), ".env")
        script_env = os.path.join(script_dir, ".env")
        env_path = project_env if os.path.exists(project_env) else script_env
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            updated = False
            for i, line in enumerate(lines):
                if line.strip().startswith("ACCESS_TOKEN"):
                    lines[i] = f"ACCESS_TOKEN={access_token}\n"
                    updated = True
                    break
            if not updated:
                lines.append(f"ACCESS_TOKEN={access_token}\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print(f"\n✓ ACCESS_TOKEN saved to .env automatically.")
        else:
            print(f"\nUpdate .env file with: ACCESS_TOKEN={access_token}")

        return access_token
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    generate_access_token()
