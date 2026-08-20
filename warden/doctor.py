"""Preflight: what is configured, what is missing, what you can run right now.

    python -m warden.doctor        (or: make doctor)

Answers the only question that matters when you sit down: can I test this, and
if not, exactly what is stopping me. Checks configuration only — it makes no
network calls and spends nothing.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from warden.config import credential_mode, settings

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
OK, WARN, FAIL = f"{GREEN}✓{RESET}", f"{YELLOW}○{RESET}", f"{RED}✗{RESET}"


def main() -> int:
    s = settings()
    offline_ok = True
    live_ok = True

    print(f"\n{BOLD}Warden preflight{RESET}\n")

    # --- what works with nothing configured -------------------------------
    print(f"{BOLD}Offline{RESET} {DIM}— no cloud, no key, no spend{RESET}")

    py = sys.version_info
    if (py.major, py.minor) >= (3, 11):
        print(f"  {OK} python {py.major}.{py.minor}.{py.micro}")
    else:
        print(f"  {FAIL} python {py.major}.{py.minor} — needs 3.11+ (StrEnum, datetime.UTC)")
        offline_ok = False

    if importlib.util.find_spec("google.adk"):
        import google.adk

        print(f"  {OK} google-adk {getattr(google.adk, '__version__', '?')}")
    else:
        print(f"  {FAIL} google-adk not installed — pip install -r requirements-dev.txt")
        offline_ok = False

    manifests = list(Path(s.manifest_dir).glob("*.yaml")) if Path(s.manifest_dir).is_dir() else []
    if len(manifests) >= 4:
        print(f"  {OK} {len(manifests)} agent manifests in {s.manifest_dir}")
    else:
        print(f"  {FAIL} expected 4 manifests in {s.manifest_dir}, found {len(manifests)}")
        offline_ok = False

    if Path(".env").exists():
        print(f"  {OK} .env present")
    else:
        print(f"  {WARN} no .env — run: cp .env.example .env")

    # --- what live runs additionally need ---------------------------------
    print(f"\n{BOLD}Live models{RESET} {DIM}— for `make demo-live`{RESET}")
    print(f"  {DIM}mode: {credential_mode()}{RESET}")

    if s.use_vertex:
        if shutil.which("gcloud"):
            print(f"  {OK} gcloud on PATH")
        else:
            print(f"  {FAIL} gcloud not found — needed for application-default credentials")
            live_ok = False
        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if adc.exists():
            print(f"  {OK} application-default credentials present")
        else:
            print(f"  {FAIL} no ADC — run: gcloud auth application-default login")
            live_ok = False
        print(f"  {DIM}  note: Vertex also needs billing linked to {s.gcp_project}{RESET}")
    else:
        if s.google_api_key.startswith("AIza"):
            print(f"  {OK} GOOGLE_API_KEY set")
        elif s.google_api_key:
            print(f"  {WARN} GOOGLE_API_KEY set but does not look like an AI Studio key")
        else:
            print(f"  {FAIL} GOOGLE_API_KEY empty — get one at https://aistudio.google.com/apikey")
            print(f"       {DIM}then paste it into .env{RESET}")
            live_ok = False

    # --- what the real estate needs ---------------------------------------
    print(f"\n{BOLD}Real estate{RESET} {DIM}— for ESTATE_ADAPTER=aks{RESET}")
    print(f"  {DIM}adapter: {s.estate_adapter}{RESET}")
    if s.estate_adapter == "fake":
        print(f"  {OK} fake estate — nothing else required")
    else:
        for tool in ("kubectl", "az"):
            mark = OK if shutil.which(tool) else FAIL
            print(f"  {mark} {tool}")
        print(f"  {DIM}  plus aks-apiserver + aks-reader-token in Secret Manager{RESET}")

    # --- verdict ----------------------------------------------------------
    print(f"\n{BOLD}You can run{RESET}")
    if offline_ok:
        for cmd, what in [
            ("make demo", "a full incident, alert to pull request"),
            ("make probe", "the policy matrix, 4 agents x 12 tools"),
            ("make test", "24 tests including the ADK enforcement path"),
            ("make check", "lint + test + the no-cluster-writes assertion"),
        ]:
            print(f"  {GREEN}{cmd:16}{RESET}{DIM}{what}{RESET}")
    else:
        print(f"  {RED}nothing yet — fix the ✗ items above{RESET}")

    if live_ok and offline_ok:
        print(f"  {GREEN}{'make demo-live':16}{RESET}{DIM}the same, against real Gemini (costs tokens){RESET}")
    else:
        print(f"  {DIM}{'make demo-live':16}blocked — see 'Live models' above{RESET}")

    print()
    return 0 if offline_ok else 1


if __name__ == "__main__":
    sys.exit(main())
