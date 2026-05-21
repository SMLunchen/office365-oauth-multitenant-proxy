#!/usr/bin/env bash
set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗ ERROR:${NC} $*" >&2; }
step() { echo -e "\n${BOLD}${BLUE}── $* ──────────────────────────────────────${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Office 365 OAuth SMTP Proxy — Setup   ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. Check dependencies ─────────────────────────────────────────────────────
step "Checking dependencies"

if ! command -v docker &>/dev/null; then
    err "Docker not found. Please install Docker Engine first."
    exit 1
fi
ok "Docker $(docker --version | awk '{print $3}' | tr -d ',')"

if docker compose version &>/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose"
else
    err "Docker Compose not found (neither 'docker compose' nor 'docker-compose')."
    exit 1
fi
ok "Docker Compose ($COMPOSE)"

if ! command -v openssl &>/dev/null; then
    err "openssl not found. Please install openssl."
    exit 1
fi
ok "openssl $(openssl version | awk '{print $2}')"

# ── 2. Create .env from example ───────────────────────────────────────────────
step "Environment configuration"

if [ ! -f .env ]; then
    cp .env.example .env
    info "Created .env from .env.example"
else
    ok ".env already exists — keeping existing values"
fi

# ── 3. Generate missing keys ──────────────────────────────────────────────────
_gen_secret_key() {
    openssl rand -hex 32
}

_gen_encryption_key() {
    openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
}

_replace_or_append() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        echo "${key}=${value}" >> .env
    fi
}

SECRET_KEY_VAL=$(grep "^SECRET_KEY=" .env | cut -d= -f2-)
ENCRYPTION_KEY_VAL=$(grep "^ENCRYPTION_KEY=" .env | cut -d= -f2-)

if [ -z "$SECRET_KEY_VAL" ] || echo "$SECRET_KEY_VAL" | grep -q "changeme"; then
    NEW_KEY=$(_gen_secret_key)
    _replace_or_append "SECRET_KEY" "$NEW_KEY"
    ok "SECRET_KEY generated (openssl rand -hex 32)"
else
    ok "SECRET_KEY already set"
fi

if [ -z "$ENCRYPTION_KEY_VAL" ] || echo "$ENCRYPTION_KEY_VAL" | grep -q "changeme"; then
    NEW_KEY=$(_gen_encryption_key)
    _replace_or_append "ENCRYPTION_KEY" "$NEW_KEY"
    ok "ENCRYPTION_KEY generated (Fernet-compatible)"
else
    ok "ENCRYPTION_KEY already set"
fi

# ── 4. Check admin password ───────────────────────────────────────────────────
ADMIN_PW=$(grep "^ADMIN_PASSWORD=" .env | cut -d= -f2-)
if [ -z "$ADMIN_PW" ] || echo "$ADMIN_PW" | grep -q "changeme"; then
    warn "ADMIN_PASSWORD is still the placeholder value."
    echo -e "  Enter a strong admin password (min. 12 chars):"
    read -rsp "  Password: " NEW_PW
    echo
    if [ ${#NEW_PW} -lt 8 ]; then
        warn "Password is very short — consider using a longer one."
    fi
    _replace_or_append "ADMIN_PASSWORD" "$NEW_PW"
    ok "ADMIN_PASSWORD set"
else
    ok "ADMIN_PASSWORD already set"
fi

# ── 5. Build proxy image ──────────────────────────────────────────────────────
step "Building proxy image (smtp-proxy-tenant)"

PROXY_IMAGE=$(grep "^PROXY_IMAGE_NAME=" .env | cut -d= -f2-)
PROXY_IMAGE="${PROXY_IMAGE:-smtp-proxy-tenant}"

if docker image inspect "${PROXY_IMAGE}:latest" &>/dev/null; then
    warn "Image '${PROXY_IMAGE}:latest' already exists."
    read -rp "  Rebuild? [y/N] " REBUILD
    if [[ "$REBUILD" =~ ^[Yy]$ ]]; then
        docker build -t "${PROXY_IMAGE}:latest" ./proxy
        ok "Proxy image rebuilt"
    else
        ok "Using existing image"
    fi
else
    info "Building ${PROXY_IMAGE}:latest ..."
    docker build --no-cache -t "${PROXY_IMAGE}:latest" ./proxy
    ok "Proxy image built"
fi

# ── 6. Build and start admin container ───────────────────────────────────────
step "Starting admin container"

$COMPOSE up -d --build

# Wait for healthy startup
info "Waiting for admin to start..."
for i in $(seq 1 20); do
    STATUS=$(docker inspect --format='{{.State.Status}}' smtp_proxy_admin 2>/dev/null || echo "not_found")
    if [ "$STATUS" = "running" ]; then
        # Give uvicorn a moment to initialize
        sleep 2
        HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:"${ADMIN_PORT:-8080}"/login 2>/dev/null || echo "000")
        if [ "$HTTP_CODE" = "200" ]; then
            ok "Admin is up and responding"
            break
        fi
    elif [ "$STATUS" = "exited" ]; then
        err "Admin container exited unexpectedly."
        echo -e "\nContainer logs:"
        $COMPOSE logs --tail=30 admin
        exit 1
    fi
    sleep 1
done

# ── 7. Create proxy network if missing ───────────────────────────────────────
NETWORK_NAME=$(grep "^PROXY_NETWORK_NAME=" .env | cut -d= -f2-)
NETWORK_NAME="${NETWORK_NAME:-smtp_proxy_net}"
if ! docker network inspect "$NETWORK_NAME" &>/dev/null; then
    docker network create "$NETWORK_NAME"
    ok "Docker network '$NETWORK_NAME' created"
else
    ok "Docker network '$NETWORK_NAME' exists"
fi

# ── 8. Optional: Nginx reverse proxy with SSL ─────────────────────────────────
step "Optional: Nginx HTTPS reverse proxy"

NGINX_DOMAIN=""
echo ""
read -rp "  Deploy Nginx reverse proxy with SSL for the Admin UI? [y/N] " WANT_NGINX
echo ""

if [[ "$WANT_NGINX" =~ ^[Yy]$ ]]; then

    read -rp "  Domain for the Admin UI (e.g. smtp-admin.example.com): " NGINX_DOMAIN
    while [ -z "$NGINX_DOMAIN" ]; do
        err "Domain cannot be empty."
        read -rp "  Domain: " NGINX_DOMAIN
    done

    read -rp "  E-mail for Let's Encrypt notifications: " LE_EMAIL
    while [ -z "$LE_EMAIL" ]; do
        read -rp "  E-mail: " LE_EMAIL
    done

    info "Creating nginx directories..."
    mkdir -p nginx/certs nginx/webroot nginx/letsencrypt

    # ── Generate self-signed cert (Phase 1 — used until LE cert is obtained) ──
    info "Generating temporary self-signed certificate..."
    openssl req -x509 -nodes -newkey rsa:2048 \
        -keyout nginx/certs/privkey.pem \
        -out nginx/certs/fullchain.pem \
        -days 30 \
        -subj "/CN=${NGINX_DOMAIN}/O=SMTP OAuth Proxy (temporary)" \
        -addext "subjectAltName=DNS:${NGINX_DOMAIN}" 2>/dev/null
    ok "Self-signed cert written to nginx/certs/"

    # ── Write nginx.conf ───────────────────────────────────────────────────────
    cat > nginx/nginx.conf <<NGINXEOF
server {
    listen 80;
    server_name ${NGINX_DOMAIN};

    # Certbot webroot challenge — must be served over HTTP
    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    # Redirect everything else to HTTPS
    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    http2  on;
    server_name ${NGINX_DOMAIN};

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header X-XSS-Protection "1; mode=block";

    location / {
        proxy_pass         http://smtp_proxy_admin:8080;
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;
        proxy_read_timeout 86400;
    }
}
NGINXEOF
    ok "nginx.conf written for domain: ${NGINX_DOMAIN}"

    # ── Start Nginx with self-signed cert ──────────────────────────────────────
    info "Starting Nginx..."
    $COMPOSE --profile proxy up -d nginx
    sleep 3

    if ! docker inspect smtp_proxy_nginx &>/dev/null; then
        err "Nginx container failed to start. Check: docker logs smtp_proxy_nginx"
    else
        ok "Nginx running"

        # ── Obtain Let's Encrypt certificate ──────────────────────────────────
        echo ""
        warn "Let's Encrypt requires port 80 to be reachable from the internet."
        warn "Make sure ${NGINX_DOMAIN} points to this server's public IP."
        read -rp "  Proceed with Let's Encrypt certificate request? [Y/n] " DO_LE
        echo ""

        if [[ ! "$DO_LE" =~ ^[Nn]$ ]]; then
            info "Running certbot (webroot challenge)..."
            if docker run --rm \
                -v "$(pwd)/nginx/letsencrypt:/etc/letsencrypt" \
                -v "$(pwd)/nginx/webroot:/var/www/certbot" \
                certbot/certbot certonly \
                    --webroot \
                    --webroot-path=/var/www/certbot \
                    --email "${LE_EMAIL}" \
                    --agree-tos \
                    --no-eff-email \
                    --non-interactive \
                    -d "${NGINX_DOMAIN}"; then

                # Copy LE certs into the nginx certs directory
                LE_LIVE="nginx/letsencrypt/live/${NGINX_DOMAIN}"
                cp "${LE_LIVE}/fullchain.pem" nginx/certs/fullchain.pem
                cp "${LE_LIVE}/privkey.pem"   nginx/certs/privkey.pem
                docker exec smtp_proxy_nginx nginx -s reload
                ok "Let's Encrypt certificate installed and Nginx reloaded"

                # Write renewal script
                cat > renew-certs.sh <<RENEWEOF
#!/usr/bin/env bash
# Renew Let's Encrypt certificate for ${NGINX_DOMAIN}
# Add to cron: 0 3 * * 0 $(pwd)/renew-certs.sh >> /var/log/cert-renewal.log 2>&1
set -euo pipefail
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
cd "\$SCRIPT_DIR"

docker run --rm \\
    -v "\$(pwd)/nginx/letsencrypt:/etc/letsencrypt" \\
    -v "\$(pwd)/nginx/webroot:/var/www/certbot" \\
    certbot/certbot renew --quiet --webroot --webroot-path=/var/www/certbot

LE_LIVE="nginx/letsencrypt/live/${NGINX_DOMAIN}"
if [ -f "\${LE_LIVE}/fullchain.pem" ]; then
    cp "\${LE_LIVE}/fullchain.pem" nginx/certs/fullchain.pem
    cp "\${LE_LIVE}/privkey.pem"   nginx/certs/privkey.pem
    docker exec smtp_proxy_nginx nginx -s reload 2>/dev/null || true
    echo "\$(date): cert renewed and nginx reloaded"
fi
RENEWEOF
                chmod +x renew-certs.sh
                ok "renew-certs.sh written"

            else
                warn "certbot failed — Nginx will continue with the self-signed certificate."
                warn "Once DNS and port 80 are reachable, run manually:"
                warn "  ./renew-certs.sh"
            fi
        else
            info "Skipping Let's Encrypt — using self-signed cert."
            info "Run ./setup.sh again or use renew-certs.sh once DNS is ready."
        fi
    fi
fi

# ── 9. Summary ────────────────────────────────────────────────────────────────
ADMIN_PORT=$(grep "^ADMIN_PORT=" .env | cut -d= -f2-)
ADMIN_PORT="${ADMIN_PORT:-8080}"
ADMIN_USER=$(grep "^ADMIN_USERNAME=" .env | cut -d= -f2-)
ADMIN_USER="${ADMIN_USER:-admin}"

echo -e "\n${BOLD}${GREEN}  ✓ Setup complete!${NC}\n"
if [ -n "$NGINX_DOMAIN" ]; then
    echo -e "  ${BOLD}Admin UI:${NC}  https://${NGINX_DOMAIN}"
else
    echo -e "  ${BOLD}Admin UI:${NC}  http://<server-ip>:${ADMIN_PORT}"
fi
echo -e "  ${BOLD}Username:${NC}  ${ADMIN_USER}"
echo -e "  ${BOLD}Password:${NC}  (as configured in .env)"
echo -e ""
echo -e "  ${YELLOW}Important:${NC} You will be prompted to set up 2FA (Google Authenticator)"
echo -e "  on first login — have your authenticator app ready."
echo -e ""
echo -e "  ${BOLD}Next steps:${NC}"
echo -e "  1. Open the Admin UI and set up 2FA"
echo -e "  2. Create a tenant (Tenants → New Tenant)"
echo -e "  3. Add OAuth configuration for the tenant"
echo -e "  4. Start the tenant container"
echo -e "  5. Configure your printer/scanner with the displayed SMTP settings"
if [ -n "$NGINX_DOMAIN" ]; then
    echo -e ""
    echo -e "  ${BOLD}SSL renewal:${NC}  Add to cron (weekly):"
    echo -e "  ${CYAN}  0 3 * * 0 $(pwd)/renew-certs.sh >> /var/log/cert-renewal.log 2>&1${NC}"
fi
echo -e ""
echo -e "  ${CYAN}Logs:${NC}  docker compose logs -f admin"
echo -e "  ${CYAN}Stop:${NC}  docker compose down"
echo ""
