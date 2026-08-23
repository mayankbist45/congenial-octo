#!/usr/bin/env bash
# Configure a TLS-only public API boundary for the Vercel React workspace.
set -euo pipefail

HOST="${DASHBOARD_PUBLIC_HOST:-}"
if [ -z "$HOST" ]; then
  echo "Set DASHBOARD_PUBLIC_HOST (for example 203-0-113-10.sslip.io)." >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo DASHBOARD_PUBLIC_HOST=$HOST $0" >&2
  exit 1
fi

apt-get update -qq
apt-get install -y -qq caddy
cat >/etc/caddy/Caddyfile <<EOF
${HOST} {
    encode zstd gzip

    # Only authenticated, versioned workspace/admin APIs are public. Legacy
    # Jinja JSON routes remain loopback-only during the React migration.
    @api path /api/v1/* /healthz
    handle @api {
        reverse_proxy 127.0.0.1:8000
    }

    respond "Not found" 404
}
EOF

caddy fmt --overwrite /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl restart caddy
echo "HTTPS API proxy is live at https://${HOST}/healthz"
