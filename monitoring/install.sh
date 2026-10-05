#!/bin/sh
set -eu
cd "$(dirname "$0")"
command -v python3 >/dev/null
command -v sadf >/dev/null
test -f config.json || { echo 'Copy config.example.json to config.json and verify targets first.' >&2; exit 1; }
# Targets use already-configured SSH host verification and existing sysstat timers.
sudo install -d -m 0755 /opt/resource-reports
sudo install -m 0644 monitor.py snapshot.py bootstrap.py schema.sql /opt/resource-reports/
sudo install -d -m 0755 /etc/resource-reports
sudo install -m 0644 config.json /etc/resource-reports/config.json
sudo install -d -o mizu -g mizu -m 0700 /var/lib/resource-reports
python3 /opt/resource-reports/bootstrap.py
sudo install -m 0644 resource-reports-*.service resource-reports-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
# Import up to seven completed days from existing sysstat logs; disk history starts now.
python3 /opt/resource-reports/monitor.py collect --history-days 9
python3 /opt/resource-reports/monitor.py report
sudo systemctl enable --now resource-reports-collect.timer resource-reports-daily.timer
sudo systemctl start resource-reports-collect.service
