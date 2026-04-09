#!/bin/bash
# Deploy UX researcher pipeline to kagg (192.168.0.224)
#
# Sets up:
#   1. Copies scraper + categorizer to kagg
#   2. Creates systemd timer for daily scraping
#   3. Creates systemd timer for categorization (runs after scraping)
#   4. Sets up dedicated Hyphae instance for UX data (port 8102)
#
# Usage: ./deploy_kagg.sh

set -euo pipefail

KAGG="om@192.168.0.224"
REMOTE_DIR="/home/om/ux_researcher"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying UX Researcher to kagg ==="

# 1. Copy files
echo "[1/4] Copying files..."
ssh "$KAGG" "mkdir -p $REMOTE_DIR"
scp "$SCRIPT_DIR/reddit_scraper.py" "$KAGG:$REMOTE_DIR/"
scp "$SCRIPT_DIR/categorizer.py" "$KAGG:$REMOTE_DIR/"
scp "$SCRIPT_DIR/pipeline.py" "$KAGG:$REMOTE_DIR/"
scp "$SCRIPT_DIR/__init__.py" "$KAGG:$REMOTE_DIR/"

# 2. Create scraping service + timer
echo "[2/4] Creating systemd services..."
ssh "$KAGG" "cat > ~/.config/systemd/user/ux-scraper.service << 'EOF'
[Unit]
Description=LLMOS UX Researcher — Reddit scraper

[Service]
Type=oneshot
WorkingDirectory=/home/om/ux_researcher
ExecStart=/usr/bin/python3 reddit_scraper.py scrape --all --limit 200
ExecStartPost=/usr/bin/python3 reddit_scraper.py comments
Environment=PYTHONUNBUFFERED=1
EOF"

ssh "$KAGG" "cat > ~/.config/systemd/user/ux-scraper.timer << 'EOF'
[Unit]
Description=Run UX scraper every 6 hours

[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF"

# 3. Create categorization service + timer
ssh "$KAGG" "cat > ~/.config/systemd/user/ux-categorizer.service << 'EOF'
[Unit]
Description=LLMOS UX Researcher — LLM categorizer
After=ux-scraper.service

[Service]
Type=oneshot
WorkingDirectory=/home/om/ux_researcher
ExecStart=/usr/bin/python3 categorizer.py run --limit 200
Environment=PYTHONUNBUFFERED=1
EOF"

ssh "$KAGG" "cat > ~/.config/systemd/user/ux-categorizer.timer << 'EOF'
[Unit]
Description=Run UX categorizer every 6 hours (30min after scraper)

[Timer]
OnCalendar=*-*-* 00,06,12,18:30:00
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
EOF"

# 4. Enable timers
echo "[3/4] Enabling timers..."
ssh "$KAGG" "systemctl --user daemon-reload"
ssh "$KAGG" "systemctl --user enable --now ux-scraper.timer"
ssh "$KAGG" "systemctl --user enable --now ux-categorizer.timer"

# 5. Verify
echo "[4/4] Verifying..."
ssh "$KAGG" "systemctl --user list-timers | grep ux-"

echo ""
echo "=== Deployed! ==="
echo "Scraper runs every 6h: 00:00, 06:00, 12:00, 18:00"
echo "Categorizer runs 30min after: 00:30, 06:30, 12:30, 18:30"
echo ""
echo "Manual run:"
echo "  ssh $KAGG 'cd $REMOTE_DIR && python3 reddit_scraper.py scrape'"
echo "  ssh $KAGG 'cd $REMOTE_DIR && python3 categorizer.py run'"
echo ""
echo "Check status:"
echo "  ssh $KAGG 'cd $REMOTE_DIR && python3 reddit_scraper.py stats'"
echo "  ssh $KAGG 'cd $REMOTE_DIR && python3 categorizer.py stats'"
