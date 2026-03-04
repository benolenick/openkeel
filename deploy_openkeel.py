#!/usr/bin/env python3
"""Deploy OpenKeel to jagg via SSH/SFTP using paramiko."""

import sys
import io
import paramiko

HOST = "192.168.0.224"
USER = "om"
PASSWORD = "aintnosunshinewhenshesgone"

PENTESTING_YAML = r"""# OpenKeel v0.2 — Penetration Testing Profile
# Designed for controlled pentest / CTF sessions against an explicitly scoped target range.
# Default action is DENY — every unrecognised command requires an explicit safe/gated match.
# Adjust scope.allowed_ips to match the actual engagement target range before use.

name: pentesting
description: >
  Penetration testing and CTF profile. Strict default-deny with an explicit allowlist
  for standard offensive tooling. Scope-locks outbound activity to declared target IPs.
  Frequent re-injection ensures rule awareness does not drift during long engagements.
version: "0.2"

# ---------------------------------------------------------------------------
# Command classification tiers
# ---------------------------------------------------------------------------

blocked:
  patterns:
    # Recursive deletion (no legitimate pentest use case on the host)
    - 'rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+/'
    - 'rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+~'
    - 'rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+\$HOME'
    # Disk formatting and partitioning on the host
    - '(?i)\b(mkfs|mke2fs|mkswap)\b'
    - '(?i)\bfdisk\b'
    - '(?i)\bparted\b.*--(script|mkpart|rm)\b'
    - '(?i)\bdd\s+.*of=/dev/(sd|nvme|vd|hd)[a-z]'
    # System power control
    - '(?i)\b(shutdown|poweroff|halt|reboot|init\s+[06])\b'
    # Exfiltration to clearly non-target external services (hard block)
    - '(?i)\b(curl|wget)\s+[^|&;]*(pastebin\.com|transfer\.sh|file\.io|ghostbin)'
    # Killing host security services
    - '(?i)\b(systemctl|service)\s+(stop|disable|mask)\s+(ufw|iptables|firewalld|auditd|fail2ban|apparmor|selinux)\b'
  message: "BLOCKED by pentesting profile: operation is prohibited in the engagement scope."

gated:
  patterns:
    # SSH to hosts outside the declared target range (requires review)
    - '\bssh\s+\S'
    # curl/wget to any host — checked against scope.allowed_ips at runtime
    - '(?i)\b(curl|wget)\s'
    # Reverse-shell patterns (netcat listeners, bash redirects to TCP)
    - '(?i)\bnc\s+(-[a-zA-Z]*l[a-zA-Z]*|-[a-zA-Z]*e[a-zA-Z]*)\b'
    - '(?i)bash\s+-i\s+>&?\s*/dev/tcp/'
    - '(?i)python[23]?\s+.*socket.*connect\b'
    - '(?i)\bsocat\s+.*EXEC:'
    # Metasploit remote sessions (handler setup — not module run)
    - '(?i)msfconsole\s+.*handler\b'
    - '(?i)msfvenom\s+.*-f\s+(exe|elf|macho|raw)\b'
    # Upload/exfil via scp/rsync to unknown hosts
    - '\b(scp|rsync)\s+[^|&;]+@(?!10\.10\.10\.)'
    # Python http.server (could serve payloads)
    - '(?i)python[23]?\s+.*-m\s+http\.server\b'
    # Privilege escalation tools that should be logged
    - '(?i)\b(linpeas|winpeas|linenum|pspy|unix-privesc-check)\b'
  message: "GATED by pentesting profile: operation targets potential out-of-scope host or runs privileged payload."

safe:
  patterns:
    # Network reconnaissance
    - '^\s*nmap\s'
    - '^\s*masscan\s'
    - '^\s*rustscan\s'
    - '^\s*(ping|ping6)\s'
    - '^\s*(traceroute|tracepath|mtr)\s'
    - '^\s*(dig|nslookup|host|whois)\s'
    - '^\s*arping\s'
    # Web enumeration and fuzzing
    - '^\s*(gobuster|ffuf|feroxbuster|wfuzz|dirb|dirbuster)\s'
    - '^\s*(nikto|whatweb|wafw00f|wpscan)\s'
    - '^\s*(curl|wget)\s+[^|&;]*https?://10\.10\.10\.'
    - '^\s*(curl|wget)\s+[^|&;]*https?://10\.10\.'
    - '^\s*(curl|wget)\s+[^|&;]*https?://172\.(1[6-9]|2[0-9]|3[01])\.'
    - '^\s*(curl|wget)\s+[^|&;]*https?://192\.168\.'
    # SQL injection / web attack tooling
    - '^\s*sqlmap\s'
    - '^\s*(xsstrike|dalfox)\s'
    # Password attacks
    - '^\s*(hydra|medusa|ncrack)\s'
    - '^\s*(john|john-the-ripper)\s'
    - '^\s*(hashcat)\s'
    - '^\s*(crunch|cewl|cupp)\s'
    # SMB / AD enumeration
    - '^\s*(smbclient|smbmap|rpcclient|enum4linux|enum4linux-ng)\s'
    - '^\s*(crackmapexec|cme|netexec|nxc)\s'
    - '^\s*(ldapsearch|ldapenum)\s'
    - '^\s*(impacket-|python[23]?\s+.*impacket)\S*'
    - '^\s*bloodhound\b'
    - '^\s*(kerbrute|GetNPUsers|GetUserSPNs|GetTGT)\b'
    # SNMP / network service enumeration
    - '^\s*(snmpwalk|snmpget|onesixtyone)\s'
    - '^\s*(finger|rpcinfo)\s'
    # Exploitation frameworks (module runs, not handler setup)
    - '^\s*msfconsole\s'
    - '^\s*(msfdb|msfupdate)\b'
    # Python exploit scripts
    - '^\s*(python|python3)\s+\S+\.py\b'
    - '^\s*(python|python3)\s+-c\s'
    # Wordlist / file utilities
    - '^\s*(cat|less|head|tail|grep|rg|strings|file|xxd|hexdump|od)\s'
    - '^\s*(ls|ll|la|dir|pwd|cd|find|fd)\b'
    # Credential and hash utilities
    - '^\s*(hash-identifier|hashid|haiti)\s'
    - '^\s*(openssl|gpg)\s'
    - '^\s*(base64|xxd)\s'
    # Steganography
    - '^\s*(steghide|stegsolve|binwalk|foremost|exiftool)\s'
    # Decompilation / reverse engineering
    - '^\s*(ghidra|radare2|r2|objdump|nm|readelf|ltrace|strace)\b'
    - '^\s*(pwndbg|peda|gdb|gef)\b'
    # Environment and process info
    - '^\s*(id|whoami|hostname|uname|env|ps|top|free|df|ip\s+a)\b'
    # VPN and tunnel management (read-only or connect)
    - '^\s*openvpn\s+[^|&;]*\.ovpn\b'
  message: ""

default_action: deny

# ---------------------------------------------------------------------------
# Scope constraints
# ---------------------------------------------------------------------------

scope:
  allowed_ips:
    - "10.10.10.0/24"
    - "10.10.11.0/24"
    - "10.129.0.0/16"     # HTB active target range
    - "10.10.0.0/16"      # HTB VPN range
    - "127.0.0.1"         # localhost (Memoria, Ollama)
    - "192.168.0.0/24"    # local network
  allowed_hostnames:
    - "*.htb"
    - "*.hackthebox.eu"
  allowed_paths:
    - "/home/*/htb/**"
    - "/home/*/pentest/**"
    - "/home/*/ctf/**"
    - "/tmp/**"
    - "/opt/tools/**"
    - "/opt/wordlists/**"
    - "/home/om/htb-autopwn/**"
    - "/home/om/openkeel/**"
  denied_paths:
    - "/etc/shadow"
    - "/etc/gshadow"
    - "/etc/sudoers"
    - "/root/.ssh/**"

# ---------------------------------------------------------------------------
# Activities (timeboxing)
# ---------------------------------------------------------------------------

activities:
  - name: recon
    patterns:
      - '^\s*(nmap|masscan|rustscan)\s'
      - '^\s*(ping|traceroute|dig|whois|arping)\s'
    timebox_minutes: 30
    grace_minutes: 5

  - name: enumeration
    patterns:
      - '^\s*(gobuster|ffuf|feroxbuster|wfuzz|dirb)\s'
      - '^\s*(nikto|whatweb|wpscan|enum4linux|smbmap|smbclient)\s'
      - '^\s*(ldapsearch|crackmapexec|netexec|nxc|rpcclient)\s'
      - '^\s*(snmpwalk|onesixtyone)\s'
    timebox_minutes: 45
    grace_minutes: 5

  - name: exploitation
    patterns:
      - '^\s*(sqlmap|hydra|medusa|john|hashcat)\s'
      - '^\s*msfconsole\s'
      - '^\s*(python|python3)\s+\S+\.py\b'
      - '^\s*(impacket-|crackmapexec)\S*'
    timebox_minutes: 60
    grace_minutes: 10

  - name: post-exploitation
    patterns:
      - '^\s*(linpeas|winpeas|linenum|pspy)\b'
      - '^\s*(bloodhound|kerbrute|GetNPUsers|GetUserSPNs)\b'
    timebox_minutes: 45
    grace_minutes: 5

  - name: reporting
    patterns:
      - '^\s*(cat|less|head|tail|grep)\s'
      - '^\s*(vim|nvim|nano|code|cursor)\s'
    timebox_minutes: 20
    grace_minutes: 5

# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

phases:
  - name: recon
    description: "Host discovery, port scanning, OS fingerprinting."
    timeout_minutes: 30
    auto_advance: false
    gates:
      - type: command_output
        target: "ip route"
        expect: "10\\.10\\."
        message: "VPN tunnel to target network must be active before starting recon."

  - name: enumeration
    description: "Service enumeration, directory brute-force, credential spraying."
    timeout_minutes: 45
    auto_advance: false
    gates: []

  - name: exploitation
    description: "Active exploitation of identified vulnerabilities."
    timeout_minutes: 60
    auto_advance: false
    gates:
      - type: memory_search
        target: "http://127.0.0.1:8000"
        expect: "exploit technique attack vulnerability"
        message: "Query Memoria for known attack paths before blind exploitation."

  - name: post-exploitation
    description: "Privilege escalation, lateral movement, loot collection."
    timeout_minutes: 45
    auto_advance: false
    gates: []

  - name: reporting
    description: "Evidence consolidation and report writing."
    timeout_minutes: 20
    auto_advance: false
    gates: []

# ---------------------------------------------------------------------------
# Re-injection (more frequent during pentest to maintain scope awareness)
# ---------------------------------------------------------------------------

reinjection:
  capsule_every: 15
  full_every: 80
  rules_path: "~/.openkeel/rules/htb-playbook.txt"
  capsule_lines: 25

# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

sandbox:
  enabled: true
  memory_max: "4G"
  cpu_quota: "200%"
  # Empty — proxy shell handles scope
  network_deny: []
  readonly_paths:
    - "/etc"
    - "/boot"
  inaccessible_paths:
    - "/etc/shadow"
    - "/etc/gshadow"
    - "/root"

# ---------------------------------------------------------------------------
# Guardian (Granite safety model)
# ---------------------------------------------------------------------------

guardian:
  enabled: true
  endpoint: "http://localhost:11434/api/generate"
  model: "granite3.3-guardian:8b"
  check_on: "gated"
  timeout: 10
  context: "Authorized HTB/CTF penetration testing engagement on declared target IPs only"

# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------

timers:
  - name: "memoria_health"
    interval_minutes: 60
    command: "curl -s http://localhost:8000/health"
    expect: "ok"
    on_fail: "warn"
    on_fail_command: ""

# ---------------------------------------------------------------------------
# Learning (Memoria integration — cross-session pentesting knowledge)
# ---------------------------------------------------------------------------

learning:
  enabled: true
  endpoint: "http://127.0.0.1:8000"
  timeout: 15
  extract_on:
    - timebox_blocks
    - successful_phases
    - drift_events
    - blocked_commands
    - tool_gaps
  auto_seed: true
  search_top_k: 5

# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

tags:
  - pentest
  - security
"""

HTB_PLAYBOOK = """1. RESEARCH FIRST — query Memoria before manual exploitation
2. 10-min timebox per technique — no findings = pivot
3. Check Memoria for known attack paths before trying blind
4. Use installed tools (sqlmap, impacket, evil-winrm) — don't hand-write scripts
5. Current phase: {PHASE} | Time remaining: {REMAINING}
"""

RUN_WITH_OPENKEEL_SH = """#!/bin/bash
source /home/om/openkeel-venv/bin/activate
openkeel run --profile pentesting --project "$2" -- \\
  python3 /home/om/htb-autopwn/autopwn.py attack "$1" --name "$2"
"""


def run_cmd(ssh, cmd, stop_on_error=True):
    print(f"\n>>> {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode()
    err = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()
    if out:
        print(out.rstrip())
    if err:
        print(f"[stderr] {err.rstrip()}")
    print(f"[exit {exit_code}]")
    if stop_on_error and exit_code != 0:
        print(f"ERROR: Command failed with exit code {exit_code}. Stopping.")
        sys.exit(1)
    return out, err, exit_code


def sftp_write(sftp, remote_path, content):
    print(f"\n[SFTP] Writing {remote_path} ...")
    with sftp.file(remote_path, 'w') as f:
        f.write(content)
    print(f"[SFTP] Done.")


def sftp_read(sftp, remote_path):
    print(f"\n[SFTP] Reading {remote_path} ...")
    with sftp.file(remote_path, 'r') as f:
        content = f.read().decode()
    print(f"[SFTP] Read {len(content)} bytes.")
    return content


def main():
    print("=" * 60)
    print("OpenKeel Deployment to jagg (192.168.0.224)")
    print("=" * 60)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"\nConnecting to {HOST} as {USER} ...")
    client.connect(
        HOST,
        username=USER,
        password=PASSWORD,
        allow_agent=False,
        look_for_keys=False,
    )
    print("Connected.\n")

    sftp = client.open_sftp()

    # ----------------------------------------------------------------
    # Part 2: Install OpenKeel
    # ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 2: Install OpenKeel")
    print("=" * 60)

    # Step 1: Clone or pull
    out, err, code = run_cmd(
        client,
        'test -d /home/om/openkeel/.git && echo REPO_EXISTS || echo REPO_MISSING',
        stop_on_error=False,
    )
    if 'REPO_EXISTS' in out:
        print("Repo exists — pulling latest ...")
        run_cmd(client, 'cd /home/om/openkeel && git pull')
    else:
        print("Repo not found — cloning ...")
        run_cmd(client, 'git clone https://github.com/benolenick/openkeel /home/om/openkeel')

    # Step 2: Create venv if not exists
    out, err, code = run_cmd(
        client,
        'test -d /home/om/openkeel-venv && echo VENV_EXISTS || echo VENV_MISSING',
        stop_on_error=False,
    )
    if 'VENV_EXISTS' in out:
        print("Venv already exists — skipping creation.")
    else:
        run_cmd(client, 'python3 -m venv /home/om/openkeel-venv')

    # Step 3: pip install -e
    run_cmd(client, '/home/om/openkeel-venv/bin/pip install -e /home/om/openkeel')

    # Step 4: Verify
    run_cmd(client, '/home/om/openkeel-venv/bin/openkeel --help')

    # ----------------------------------------------------------------
    # Part 3: Configure on Jagg
    # ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 3: Configure on Jagg")
    print("=" * 60)

    # Step 5: Create dirs
    run_cmd(client, 'mkdir -p ~/.openkeel/profiles ~/.openkeel/rules')

    # Step 6: Write pentesting.yaml via SFTP
    sftp_write(sftp, '/home/om/.openkeel/profiles/pentesting.yaml', PENTESTING_YAML)

    # Step 7: Write htb-playbook.txt via SFTP
    sftp_write(sftp, '/home/om/.openkeel/rules/htb-playbook.txt', HTB_PLAYBOOK)

    # ----------------------------------------------------------------
    # Part 4: Patch Autopwn's CommandRunner
    # ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 4: Patch Autopwn CommandRunner")
    print("=" * 60)

    # Step 8: Read runner.py
    runner_path = '/home/om/htb-autopwn/core/runner.py'
    try:
        runner_content = sftp_read(sftp, runner_path)
    except FileNotFoundError:
        print(f"ERROR: {runner_path} not found on jagg. Skipping patch.")
        runner_content = None

    if runner_content is not None:
        # Show the subprocess.run line(s) for reference
        lines = runner_content.splitlines()
        for i, line in enumerate(lines):
            if 'subprocess.run' in line:
                print(f"  Line {i+1}: {line}")

        # Check if import os is present
        has_import_os = 'import os' in runner_content

        # Find the subprocess.run call — find the line and patch it
        # We need to find the exact call inside the run() method
        # Strategy: find `subprocess.run(cmd, shell=True` and replace with the guarded version
        import re

        # Find the subprocess.run block — may span multiple lines if args are multi-line
        # We'll do a targeted replacement of the subprocess.run call that has shell=True
        # First, find it
        pattern = re.compile(
            r'([ \t]*)(proc\s*=\s*subprocess\.run\(cmd,\s*shell=True[^)]*\))',
            re.DOTALL,
        )
        match = pattern.search(runner_content)

        if not match:
            # Try broader pattern
            pattern2 = re.compile(
                r'([ \t]*)(proc\s*=\s*subprocess\.run\([^)]+shell=True[^)]*\))',
                re.DOTALL,
            )
            match = pattern2.search(runner_content)

        if match:
            indent = match.group(1)
            original_call = match.group(2)
            print(f"\nFound subprocess.run call:\n  {original_call}")

            # Extract the arguments from the original call to preserve timeout and cwd
            # Build the replacement
            replacement = (
                f'{indent}openkeel_exec = os.environ.get("OPENKEEL_EXEC")\n'
                f'{indent}if openkeel_exec:\n'
                f'{indent}    proc = subprocess.run([openkeel_exec, "-c", cmd], capture_output=True, text=True, timeout=timeout, cwd=cwd or str(self.run_dir))\n'
                f'{indent}else:\n'
                f'{indent}    {original_call}'
            )

            new_content = runner_content.replace(match.group(0), replacement, 1)

            # Ensure import os is present
            if not has_import_os:
                # Add after the first import line
                new_content = re.sub(
                    r'^(import\s+\w+)',
                    r'import os\n\1',
                    new_content,
                    count=1,
                    flags=re.MULTILINE,
                )
                print("Added 'import os' to runner.py")

            print("\nWriting patched runner.py ...")
            sftp_write(sftp, runner_path, new_content)
            print("Patch applied successfully.")
        else:
            print("WARNING: Could not find subprocess.run(cmd, shell=True...) pattern.")
            print("Showing all subprocess lines for manual inspection:")
            for i, line in enumerate(lines):
                if 'subprocess' in line:
                    print(f"  Line {i+1}: {line}")

    # ----------------------------------------------------------------
    # Part 5: Launch Script
    # ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("PART 5: Launch Script")
    print("=" * 60)

    # Step 9: Create run_with_openkeel.sh
    sftp_write(sftp, '/home/om/htb-autopwn/run_with_openkeel.sh', RUN_WITH_OPENKEEL_SH)

    # Step 10: Make executable
    run_cmd(client, 'chmod +x /home/om/htb-autopwn/run_with_openkeel.sh')

    # ----------------------------------------------------------------
    # Verification
    # ----------------------------------------------------------------
    print("\n" + "=" * 60)
    print("VERIFICATION")
    print("=" * 60)

    # Step 11: Profile show
    run_cmd(client, '/home/om/openkeel-venv/bin/openkeel profile show pentesting', stop_on_error=False)

    # Step 12: Profile validate
    run_cmd(client, '/home/om/openkeel-venv/bin/openkeel profile validate pentesting', stop_on_error=False)

    sftp.close()
    client.close()
    print("\n" + "=" * 60)
    print("Deployment complete.")
    print("=" * 60)


if __name__ == '__main__':
    main()
