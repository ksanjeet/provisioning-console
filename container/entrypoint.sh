#!/bin/sh
set -eu

export SAP_WIZARD_STATE_DIR="${SAP_WIZARD_STATE_DIR:-/var/lib/sap-wizard}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

mkdir -p "${SAP_WIZARD_STATE_DIR}/generated/runs"

exec python /opt/sap-wizard/app.py
