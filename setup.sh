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

# ── 8. Summary ────────────────────────────────────────────────────────────────
ADMIN_PORT=$(grep "^ADMIN_PORT=" .env | cut -d= -f2-)
ADMIN_PORT="${ADMIN_PORT:-8080}"
ADMIN_USER=$(grep "^ADMIN_USERNAME=" .env | cut -d= -f2-)
ADMIN_USER="${ADMIN_USER:-admin}"

echo -e "\n${BOLD}${GREEN}  ✓ Setup complete!${NC}\n"
echo -e "  ${BOLD}Admin UI:${NC}  http://<server-ip>:${ADMIN_PORT}"
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
echo -e ""
echo -e "  ${CYAN}Logs:${NC}  docker compose logs -f admin"
echo -e "  ${CYAN}Stop:${NC}  docker compose down"
echo ""
