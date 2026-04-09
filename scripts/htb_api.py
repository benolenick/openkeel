#!/usr/bin/env python3
"""HTB API helper — hardcoded token for quick box management.

Usage:
    python3 htb_api.py list-active       # List active/retired boxes
    python3 htb_api.py spawn <id>        # Spawn a box
    python3 htb_api.py reset <id>        # Reset a box
    python3 htb_api.py stop <id>         # Stop a box
    python3 htb_api.py status            # Show current active instance
    python3 htb_api.py submit <id> <flag> [difficulty]  # Submit flag
    python3 htb_api.py profile <id>      # Get box profile/info
    python3 htb_api.py search <name>     # Search for a box by name
"""

import json
import sys
import urllib.request
import urllib.error

HTB_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiI1IiwianRpIjoiOWYyZDkzMDFhMmJjNjBiNGYzOTBiZWZhZDBlNTVjODBjYzhiNjMzNmJlYjA4YjYyNTE5YzBiNTI2MWE5OTU2M2ExMDViZTRkZDVmOGYwMjkiLCJpYXQiOjE3NzI5ODQ4MjguNjU2ODc2LCJuYmYiOjE3NzI5ODQ4MjguNjU2ODc5LCJleHAiOjE4MDQ1MjA4MjguNjQzODM0LCJzdWIiOiIzMTE2Nzk0Iiwic2NvcGVzIjpbXX0.E3UkdUmxeAlwrUv7msXKTEu3xXNDSIkRWaJ9-lGn5ojcH8EsxWMqgXWMgYv3TAdQVbHluHC244u0rgTQfq26EtSM9R_iWeZLZ-6XBdYKS5Esd7c2rr-cXj19R7z_RxfxcF8PL0j7mcv9XJg_xuv_BI6ealJWxxQh0avdSvnOZM9ZCLjffkOCFAfKXT5os4_Lj-sqEyS4SMiHbKIgpYpcmAcXvMT-IAm_52r415t1nTN5Eh4p0kwoFJewaO2ySjXPk57binAiGWmwntS_21bmyl_hPTmJtw0TzxxGUyNx4H48aLwnGNxZivhRkc6nsqKeSVeUKFGVMpOP6_c4iO3bFKwn2hVyOp0zF985EP4923O66uSHk9kK_LlaTuC-tAS9VRyvHic68waklue2jQKwwY88rEkQdXefSwm73bV0tJVrqKodk3FmttfdUBdx3HPajazrWgXZIchqmiohkr4c-rlIm7TzrXyshJXFaV1LUuVwGKOaWBupKyrBvQIYRgoqkiMqscBdQ_Yz1NxVwq5ECI3xydd7geweDvz8rqn3FUkKI8JGYjN3lF_Aor7mh_mr_ZAwUIzQLg3eoGTJRSvXVMOo6RHpRRZc_GWyAoiG8LyUmaYv4QKdBDPcnCf8FIfAweA6MBDNin2CzHuf4MZx9TGoSs7p-o37evAXn8OeeXg"
HTB_BASE = "https://labs.hackthebox.com/api/v4"
HTB_USER_ID = 3116794


def _request(endpoint, method="GET", body=None):
    """Make an authenticated HTB API request."""
    url = f"{HTB_BASE}/{endpoint.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {HTB_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    }

    data = None
    if body:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"raw": raw}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        return {"error": e.code, "message": body_text}
    except Exception as e:
        return {"error": str(e)}


def list_active():
    """List active season machines."""
    # Try paginated endpoint first, fall back to others
    for endpoint in ["/machine/paginated?per_page=50", "/season/machines", "/machine/list"]:
        result = _request(endpoint)
        if "error" not in result:
            break

    if "error" in result:
        print(f"Error: {result.get('error')} — {result.get('message', '')[:200]}")
        return

    # Handle paginated response
    machines = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(machines, list):
        for m in machines[:40]:
            name = m.get("name", "?")
            mid = m.get("id", "?")
            os_name = m.get("os", "?")
            diff = m.get("difficultyText", m.get("difficulty", "?"))
            ip = m.get("ip", "")
            user_own = m.get("authUserInUserOwns", False)
            root_own = m.get("authUserInRootOwns", False)
            owned = " PWNED" if user_own and root_own else " (user)" if user_own else ""
            print(f"  [{mid}] {name} ({os_name}, {diff}) {ip}{owned}")
    else:
        print(json.dumps(result, indent=2)[:2000])


def spawn(machine_id):
    result = _request("/vm/spawn", method="POST", body={"machine_id": int(machine_id)})
    print(json.dumps(result, indent=2))


def reset(machine_id):
    result = _request("/vm/reset", method="POST", body={"machine_id": int(machine_id)})
    print(json.dumps(result, indent=2))


def stop(machine_id):
    result = _request("/vm/terminate", method="POST", body={"machine_id": int(machine_id)})
    print(json.dumps(result, indent=2))


def status():
    result = _request("/machine/active")
    if isinstance(result, dict) and result.get("info"):
        info = result["info"]
        print(f"Active: {info.get('name', '?')} (ID {info.get('id', '?')})")
        print(f"  IP: {info.get('ip', '?')}")
        print(f"  OS: {info.get('os', '?')}")
        print(f"  Expires: {info.get('expires_at', '?')}")
    else:
        print(json.dumps(result, indent=2)[:1000])


def submit_flag(machine_id, flag, difficulty=20):
    result = _request("/machine/own", method="POST", body={
        "id": int(machine_id),
        "flag": flag,
        "difficulty": int(difficulty),
    })
    print(json.dumps(result, indent=2))


def profile(machine_id):
    result = _request(f"/machine/profile/{machine_id}")
    if isinstance(result, dict) and result.get("info"):
        info = result["info"]
        print(f"Name: {info.get('name', '?')}")
        print(f"ID: {info.get('id', '?')}")
        print(f"OS: {info.get('os', '?')}")
        print(f"Difficulty: {info.get('difficultyText', '?')} ({info.get('difficulty', '?')})")
        print(f"IP: {info.get('ip', '?')}")
        print(f"Points: {info.get('points', '?')}")
        print(f"User Owns: {info.get('user_owns_count', '?')}")
        print(f"Root Owns: {info.get('root_owns_count', '?')}")
        print(f"Stars: {info.get('stars', '?')}")
        user_owned = info.get("authUserInUserOwns", False)
        root_owned = info.get("authUserInRootOwns", False)
        print(f"You own: user={'YES' if user_owned else 'no'} root={'YES' if root_owned else 'no'}")
    else:
        print(json.dumps(result, indent=2)[:2000])


def search(name):
    # Search through active machines
    result = _request("/machine/list")
    machines = result.get("data", result) if isinstance(result, dict) else result
    if isinstance(machines, list):
        matches = [m for m in machines if name.lower() in m.get("name", "").lower()]
        if matches:
            for m in matches:
                print(f"  [{m.get('id')}] {m.get('name')} ({m.get('os')}, {m.get('difficultyText')}) IP: {m.get('ip', 'not spawned')}")
        else:
            print(f"No machines matching '{name}' found in active list.")
    else:
        print(json.dumps(result, indent=2)[:1000])


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]
    if cmd == "list-active":
        list_active()
    elif cmd == "spawn" and len(sys.argv) >= 3:
        spawn(sys.argv[2])
    elif cmd == "reset" and len(sys.argv) >= 3:
        reset(sys.argv[2])
    elif cmd == "stop" and len(sys.argv) >= 3:
        stop(sys.argv[2])
    elif cmd == "status":
        status()
    elif cmd == "submit" and len(sys.argv) >= 4:
        diff = int(sys.argv[4]) if len(sys.argv) >= 5 else 20
        submit_flag(sys.argv[2], sys.argv[3], diff)
    elif cmd == "profile" and len(sys.argv) >= 3:
        profile(sys.argv[2])
    elif cmd == "search" and len(sys.argv) >= 3:
        search(sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
