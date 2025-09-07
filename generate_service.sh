#!/bin/bash

# generate_service.sh - Generate systemd service file with current user and paths
# Usage: ./generate_service.sh [output_file]

# The WorkingDirectory should contain the virtual environment and the script to run.

set -e

# Get current user and working directory
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)
SERVICE_FILE="${1:-wbor-failsafe-notifier.service}"

echo "Generating systemd service file..."
echo "User: $CURRENT_USER"
echo "Project directory: $CURRENT_DIR"
echo "Output file: $SERVICE_FILE"

# Generate the service file content
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Failsafe Gadget Notifier Service
Wants=network-online.target
After=network-online.target nss-lookup.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR
EnvironmentFile=$CURRENT_DIR/.env
Environment=BLINKA_FT232H=1
Environment=PYTHONUNBUFFERED=1
ExecStartPre=/bin/sh -c 'if [ -n "\$RABBITMQ_HOST" ]; then until getent hosts "\$RABBITMQ_HOST"; do echo "Waiting for DNS for \$RABBITMQ_HOST..."; sleep 2; done; fi'
ExecStart=$CURRENT_DIR/.venv/bin/python $CURRENT_DIR/failsafe.py
Restart=always
RestartSec=300s
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
EOF

echo "Successfully generated $SERVICE_FILE"

# Make the script executable if it's being generated in the current directory
if [[ "$SERVICE_FILE" == "wbor-failsafe-notifier.service" ]]; then
    echo "Service file ready for installation with 'make service-install'"
fi