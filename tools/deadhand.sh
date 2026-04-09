#!/bin/bash
# DEAD HAND — automatic internet recovery system
#
# Monitors internet connectivity. When it goes down:
# 1. Switches wlp2s0 to Bell497 (192.168.2.x — direct ISP, bypasses pfSense)
# 2. SSHes into torr (192.168.2.171) via Claude CLI
# 3. Diagnoses and attempts to fix pfSense/Pi-hole
#
# Run: bash deadhand.sh
# Stop: Ctrl+C
#
# Network layout:
#   Bell4975 (192.168.0.x) → pfSense (VM on torr) → internet  ← THIS BREAKS
#   Bell497  (192.168.2.x) → direct ISP → internet             ← FALLBACK

set -euo pipefail

CHECK_INTERVAL=30  # seconds between checks
FAIL_THRESHOLD=3   # consecutive failures before triggering
PING_TARGET="8.8.8.8"
PING_TIMEOUT=3

# Network config
BACKUP_IFACE="wlp2s0"
BACKUP_SSID="BELL497"
BACKUP_PASSWORD="benitoclawrezliberatorofmexicat"  # same password, different SSID
TORR_IP="192.168.2.171"
TORR_USER="om"
TORR_PASS="aintnosunshinewhenshesgone"

# State
fail_count=0
triggered=false

log() {
    echo "[$(date '+%H:%M:%S')] $1"
}

check_internet() {
    ping -c 1 -W $PING_TIMEOUT $PING_TARGET > /dev/null 2>&1
}

switch_to_bell497() {
    log "Switching $BACKUP_IFACE to $BACKUP_SSID..."

    # Create temp wpa config for Bell497
    WPA_CONF=$(mktemp /tmp/wpa_bell497_XXXX.conf)
    cat > "$WPA_CONF" << WPAEOF
ctrl_interface=/run/wpa_supplicant_deadhand

network={
  ssid="$BACKUP_SSID"
  key_mgmt=WPA-PSK WPA-PSK-SHA256 SAE
  ieee80211w=1
  psk="$BACKUP_PASSWORD"
}
WPAEOF

    # Kill existing wpa_supplicant on backup interface
    sudo pkill -f "wpa_supplicant.*$BACKUP_IFACE" 2>/dev/null || true
    sleep 2

    # Connect to Bell497
    sudo wpa_supplicant -B -i "$BACKUP_IFACE" -c "$WPA_CONF" -D nl80211,wext 2>/dev/null
    sleep 3

    # Get DHCP
    sudo dhclient -r "$BACKUP_IFACE" 2>/dev/null || true
    sudo dhclient "$BACKUP_IFACE" 2>/dev/null
    sleep 3

    # Verify
    IP=$(ip addr show "$BACKUP_IFACE" | grep "inet 192.168.2" | awk '{print $2}')
    if [ -n "$IP" ]; then
        log "Connected to $BACKUP_SSID — got $IP"
        return 0
    else
        log "Failed to get IP on $BACKUP_SSID"
        return 1
    fi
}

diagnose_and_fix() {
    log "===== DEAD HAND TRIGGERED ====="
    log "Internet is DOWN. Switching to backup network..."

    if ! switch_to_bell497; then
        log "FAILED to connect to Bell497. Manual intervention required."
        return 1
    fi

    # Verify we can reach torr
    if ! ping -c 1 -W 3 "$TORR_IP" > /dev/null 2>&1; then
        log "Cannot reach torr at $TORR_IP even on Bell497. Hardware issue?"
        return 1
    fi

    log "Reached torr. Launching diagnostic agent..."

    # Build the diagnostic prompt with full network context
    DIAG_PROMPT=$(cat << 'DIAGEOF'
You are a network diagnostic agent. The user's internet just went down.

NETWORK LAYOUT:
- This machine connects via WiFi to Bell4975 (192.168.0.x subnet)
- Bell4975 traffic goes through pfSense (a VirtualBox VM running on "torr")
- pfSense is the gateway at 192.168.0.1
- Pi-hole DNS runs on torr at 192.168.0.99
- torr itself is at 192.168.0.99 (Bell4975 side) and 192.168.2.171 (Bell497 side)
- Bell497 (192.168.2.x) is the direct ISP connection that bypasses pfSense
- We are currently connected to torr via Bell497 (the backup path)

COMMON FAILURE MODES:
1. pfSense VM crashed or froze → VBoxManage shows it not running
2. pfSense WAN interface lost DHCP → no internet even though VM is running
3. Pi-hole crashed → DNS resolution fails but ping by IP works
4. pfSense firewall rule got corrupted → blocks all traffic

YOU ARE SSHed INTO TORR. Diagnose the problem:
1. Check if pfSense VM is running: VBoxManage list runningvms
2. If not running, start it: VBoxManage startvm pfSense --type headless
3. Check Pi-hole: systemctl status pihole-FTL
4. If Pi-hole is down: sudo systemctl restart pihole-FTL
5. Check if pfSense WAN has an IP: curl -s http://192.168.0.1 (should show pfSense login)
6. Check DNS resolution: dig google.com @192.168.0.99

Fix whatever is broken. Report what you found and what you fixed.
DIAGEOF
)

    # Use Claude CLI via the Bell497 connection (internet works on this subnet)
    # SSH into torr and let Claude diagnose
    log "Running Claude diagnostic via SSH to torr..."

    sshpass -p "$TORR_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "$TORR_USER@$TORR_IP" << 'SSHEOF' 2>&1 | tee /tmp/deadhand_diag.log

    echo "=== TORR DIAGNOSTICS ==="
    echo "Date: $(date)"
    echo ""

    echo "--- pfSense VM Status ---"
    VBoxManage list runningvms 2>/dev/null || echo "VBoxManage not found or no VMs"
    echo ""

    echo "--- Pi-hole Status ---"
    systemctl status pihole-FTL 2>/dev/null | head -5 || echo "pihole-FTL not found"
    echo ""

    echo "--- DNS Test ---"
    dig +short google.com @192.168.0.99 2>/dev/null || echo "DNS failed"
    dig +short google.com @8.8.8.8 2>/dev/null || echo "External DNS also failed"
    echo ""

    echo "--- pfSense Web Check ---"
    curl -sk --connect-timeout 5 https://192.168.0.1 2>/dev/null | head -5 || echo "pfSense web unreachable"
    echo ""

    echo "--- Network Interfaces ---"
    ip addr show | grep "inet " | grep -v 127.0.0
    echo ""

    echo "--- Internet from torr ---"
    ping -c 2 -W 3 8.8.8.8 2>/dev/null || echo "No internet from torr either"
    echo ""

    echo "=== END DIAGNOSTICS ==="
SSHEOF

    DIAG_RESULT=$(cat /tmp/deadhand_diag.log)

    # Now feed the diagnostics to Claude for analysis and fix
    log "Analyzing diagnostics with Claude..."

    echo "$DIAG_PROMPT

--- DIAGNOSTIC OUTPUT FROM TORR ---
$DIAG_RESULT
---

Based on the diagnostic output above, what's wrong and what should be fixed?
If the pfSense VM is down, I need the exact command to restart it.
If Pi-hole is down, I need the restart command.
Give me the exact SSH commands to run on torr to fix this." | \
    claude -p --model sonnet --output-format text 2>&1 | tee /tmp/deadhand_analysis.log

    log "Diagnosis complete. See /tmp/deadhand_analysis.log"
    log "Diagnostic data: /tmp/deadhand_diag.log"

    # Try to auto-fix common issues
    log "Attempting auto-fix..."

    sshpass -p "$TORR_PASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "$TORR_USER@$TORR_IP" << 'FIXEOF' 2>&1 | tee -a /tmp/deadhand_diag.log

    # Auto-fix: restart pfSense if not running
    if ! VBoxManage list runningvms 2>/dev/null | grep -qi pfsense; then
        echo "FIXING: pfSense VM not running — starting it"
        VBoxManage startvm pfSense --type headless 2>&1 || echo "Failed to start pfSense VM"
        sleep 30
    else
        echo "pfSense VM is running"
    fi

    # Auto-fix: restart Pi-hole if not running
    if ! systemctl is-active pihole-FTL > /dev/null 2>&1; then
        echo "FIXING: Pi-hole not running — restarting"
        sudo systemctl restart pihole-FTL 2>&1
        sleep 5
    else
        echo "Pi-hole is running"
    fi

    echo "Auto-fix complete"
FIXEOF

    log "Auto-fix attempted. Waiting 30s for services to come up..."
    sleep 30

    # Switch back to Bell4975 and test
    log "Switching back to Bell4975..."
    sudo pkill -f "wpa_supplicant.*deadhand" 2>/dev/null || true
    sleep 2
    # Re-enable the original wpa_supplicant for wlp2s0
    sudo wpa_supplicant -B -i "$BACKUP_IFACE" -c /run/netplan/wpa-wlp2s0.conf -D nl80211,wext 2>/dev/null || true
    sleep 5

    if check_internet; then
        log "===== INTERNET RESTORED ====="
        return 0
    else
        log "Internet still down after fix attempt. Check /tmp/deadhand_analysis.log"
        return 1
    fi
}

# --- Main loop ---
log "Dead Hand active. Checking internet every ${CHECK_INTERVAL}s."
log "Fail threshold: ${FAIL_THRESHOLD} consecutive failures."
log "Backup network: $BACKUP_SSID via $BACKUP_IFACE"
log "Torr: $TORR_USER@$TORR_IP"

while true; do
    if check_internet; then
        if [ "$fail_count" -gt 0 ]; then
            log "Internet recovered (was failing: $fail_count)"
        fi
        fail_count=0
        triggered=false
    else
        fail_count=$((fail_count + 1))
        log "Internet check FAILED ($fail_count/$FAIL_THRESHOLD)"

        if [ "$fail_count" -ge "$FAIL_THRESHOLD" ] && [ "$triggered" = false ]; then
            triggered=true
            diagnose_and_fix || log "Diagnosis/fix failed. Will retry next threshold."
            fail_count=0
        fi
    fi

    sleep $CHECK_INTERVAL
done
