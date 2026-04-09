#!/bin/bash
# Jagg Firewall Setup — deploy after reboot
# iptables rules for jagg (192.168.0.224)
# No --comment flags (xt_comment not loaded on jagg)
#
# USAGE: ssh om@192.168.0.224, then: sudo bash jagg_firewall.sh

set -e

LAN="192.168.0.0/24"
DOCKER1="172.17.0.0/16"
DOCKER2="172.18.0.0/16"

echo "Flushing existing rules..."
iptables -F INPUT
iptables -F OUTPUT

# Set default policies
iptables -P INPUT DROP
iptables -P OUTPUT ACCEPT
iptables -P FORWARD ACCEPT

# Loopback — allow all
iptables -A INPUT -i lo -j ACCEPT

# Established/related — critical, must be first real rule
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Docker bridge traffic
iptables -A INPUT -s $DOCKER1 -j ACCEPT
iptables -A INPUT -s $DOCKER2 -j ACCEPT

# ICMP (ping) from LAN
iptables -A INPUT -s $LAN -p icmp -j ACCEPT

# SSH (22) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 22 -j ACCEPT

# HTTP (80) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 80 -j ACCEPT

# Chemister server (8300) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 8300 -j ACCEPT

# Shallots web (8844) + webhook (8855) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 8844 -j ACCEPT
iptables -A INPUT -s $LAN -p tcp --dport 8855 -j ACCEPT

# Redroid ADB ports (5555-5564) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 5555:5564 -j ACCEPT

# Python services (18001-18010) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 18001:18010 -j ACCEPT

# Other services from LAN
iptables -A INPUT -s $LAN -p tcp --dport 4000 -j ACCEPT
iptables -A INPUT -s $LAN -p tcp --dport 8004 -j ACCEPT
iptables -A INPUT -s $LAN -p tcp --dport 8005 -j ACCEPT
iptables -A INPUT -s $LAN -p tcp --dport 8888 -j ACCEPT
iptables -A INPUT -s $LAN -p tcp --dport 8100 -j ACCEPT

# Wazuh (1514, 1515) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 1514 -j ACCEPT
iptables -A INPUT -s $LAN -p tcp --dport 1515 -j ACCEPT
iptables -A INPUT -s $LAN -p udp --dport 1514 -j ACCEPT

# Wazuh manager (55000) from LAN
iptables -A INPUT -s $LAN -p tcp --dport 55000 -j ACCEPT

# Ollama ports from LAN
iptables -A INPUT -s $LAN -p tcp --dport 11434:11446 -j ACCEPT

# Save rules
echo "Saving rules..."
if command -v netfilter-persistent &>/dev/null; then
    netfilter-persistent save
elif [ -d /etc/iptables ]; then
    iptables-save > /etc/iptables/rules.v4
else
    iptables-save > /etc/iptables.rules
    echo '#!/bin/bash' > /etc/network/if-pre-up.d/iptables
    echo 'iptables-restore < /etc/iptables.rules' >> /etc/network/if-pre-up.d/iptables
    chmod +x /etc/network/if-pre-up.d/iptables
fi

echo "Firewall configured. Verifying SSH still works..."
iptables -L INPUT -n --line-numbers
echo ""
echo "Done. If you can see this, SSH is working."
