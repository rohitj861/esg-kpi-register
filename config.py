"""Configuration loader for the ESG KPI register.

Secrets are read from the first source that supplies them, in this order:

  1. Real environment variables (best for CI and one-off shells)
  2. %USERPROFILE%\\.config\\company-agent\\.env   <- outside OneDrive, use this
  3. .env next to these scripts                    <- synced by OneDrive, non-secrets only

The project folder lives inside OneDrive, so anything written there is uploaded
to Microsoft's cloud and kept in version history. The service-role key is a
full-access admin credential that bypasses row level security, so it belongs in
location 2, never location 3.

Check what is currently resolved with:

    python config.py
"""

import os
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parent
USER_CONFIG = pathlib.Path.home() / ".config" / "company-agent" / ".env"
LOCAL_CONFIG = APP_DIR / ".env"

# Values that must never be written into the OneDrive-synced .env.
SECRET_KEYS = {"SUPABASE_SERVICE_KEY", "SUPABASE_DB_PASSWORD"}


def _parse_env_file(path):
    """Minimal KEY=VALUE reader. Ignores blanks, comments, and surrounding quotes."""
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load():
    """Merge all configuration sources. Environment variables always win."""
    merged = {}
    merged.update(_parse_env_file(LOCAL_CONFIG))
    merged.update(_parse_env_file(USER_CONFIG))
    for key in list(merged) + [
        "SUPABASE_URL", "SUPABASE_PROJECT_REF", "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_SERVICE_KEY", "SEC_USER_AGENT",
    ]:
        if os.environ.get(key):
            merged[key] = os.environ[key]
    return merged


def require(*keys):
    """Fetch config values or fail with an instruction the user can act on."""
    config = load()
    missing = [k for k in keys if not config.get(k)]
    if missing:
        raise SystemExit(
            "missing configuration: " + ", ".join(missing) + "\n"
            f"Add them to {USER_CONFIG} (create the folder if needed), or set them\n"
            "as environment variables. See .env.example for the expected names."
        )
    return {k: config[k] for k in keys}


def _redact(value):
    if not value:
        return "—"
    return f"{value[:8]}…{value[-4:]} ({len(value)} chars)" if len(value) > 16 else "set"


def main():
    config = load()
    print(f"user config : {USER_CONFIG}  {'found' if USER_CONFIG.is_file() else 'not present'}")
    print(f"local config: {LOCAL_CONFIG}  {'found' if LOCAL_CONFIG.is_file() else 'not present'}")
    print()
    for key in ("SUPABASE_URL", "SUPABASE_PROJECT_REF", "SUPABASE_PUBLISHABLE_KEY",
                "SUPABASE_SERVICE_KEY", "SEC_USER_AGENT"):
        value = config.get(key, "")
        shown = _redact(value) if key in SECRET_KEYS else (value or "—")
        print(f"  {key:<26} {shown}")

    leaked = [k for k in SECRET_KEYS if k in _parse_env_file(LOCAL_CONFIG)]
    if leaked:
        print(
            "\nWARNING: " + ", ".join(leaked) + f" found in {LOCAL_CONFIG}.\n"
            "That file is inside OneDrive and is being synced to the cloud.\n"
            f"Move those values to {USER_CONFIG} and rotate the key."
        )


if __name__ == "__main__":
    main()
