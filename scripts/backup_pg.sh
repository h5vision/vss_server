#!/usr/bin/env bash
# vss DB 야간 백업 — /var/backups/vss/vss-YYYYmmdd.sql.gz, 7일 보관. setup_ec2.sh 가 cron 에 등록합니다.
set -euo pipefail
DIR=/var/backups/vss
mkdir -p "$DIR"
sudo -u postgres pg_dump vss | gzip > "$DIR/vss-$(date +%Y%m%d-%H%M).sql.gz"
find "$DIR" -name 'vss-*.sql.gz' -mtime +7 -delete
