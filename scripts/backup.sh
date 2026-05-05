#!/bin/bash
# Backup critical state files — local + S3
# Run daily via orchestrator or cron

DIR="$(cd "$(dirname "$0")/.." && pwd)"
DATE=$(date +%Y-%m-%d)
BACKUP_DIR="$DIR/backups/$DATE"
VENV_PYTHON="$DIR/venv/bin/python"

mkdir -p "$BACKUP_DIR"

# Critical files — these ARE the learning
cp -f "$DIR/state/trade_history.json" "$BACKUP_DIR/" 2>/dev/null
cp -f "$DIR/state/tuning_log.json" "$BACKUP_DIR/" 2>/dev/null
cp -f "$DIR/state/equity_curve.json" "$BACKUP_DIR/" 2>/dev/null
cp -f "$DIR/state/performance.json" "$BACKUP_DIR/" 2>/dev/null
cp -f "$DIR/state/api_usage.json" "$BACKUP_DIR/" 2>/dev/null
cp -f "$DIR/state/bot_log.json" "$BACKUP_DIR/" 2>/dev/null
cp -f "$DIR/config/strategy.json" "$BACKUP_DIR/" 2>/dev/null
cp -rf "$DIR/config/strategy_history" "$BACKUP_DIR/" 2>/dev/null

# Keep last 90 days of local backups
find "$DIR/backups" -maxdepth 1 -type d -mtime +90 -exec rm -rf {} \;

echo "Local backup saved: $BACKUP_DIR"

# --- S3 Upload (if configured) ---
# Reads S3_BACKUP_BUCKET from .env
S3_BUCKET=$(grep -s '^S3_BACKUP_BUCKET=' "$DIR/.env" | cut -d'=' -f2)

if [ -n "$S3_BUCKET" ]; then
    # Create a tarball for efficient upload
    TARBALL="/tmp/trading-bot-backup-$DATE.tar.gz"
    tar -czf "$TARBALL" -C "$DIR/backups" "$DATE" 2>/dev/null

    "$VENV_PYTHON" -c "
import boto3, os
s3 = boto3.client('s3')
bucket = '$S3_BUCKET'
key = 'trading-bot-backups/$DATE.tar.gz'
s3.upload_file('$TARBALL', bucket, key)
print(f'S3 backup uploaded: s3://{bucket}/{key}')
" 2>&1

    rm -f "$TARBALL"
else
    echo "S3 backup skipped (S3_BACKUP_BUCKET not set in .env)"
fi
