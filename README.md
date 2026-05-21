# Office 365 OAuth Multi-Tenant SMTP Proxy

> **Author:** Gerrit Haas &lt;github@schwarzes-seelenreich.de&gt;  
> **License:** MIT — see [LICENSE](LICENSE)

A self-hosted, multi-tenant SMTP proxy that bridges legacy devices (printers, scanners, copiers) to Microsoft 365, working around Microsoft's deprecation of Basic Auth / SMTP AUTH. Each customer gets their own isolated Docker container with TLS, brute-force protection, and full mail logging.

---

## Table of Contents (English)

- [Problem & Solution](#problem--solution)
- [Architecture](#architecture)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Azure App Registration](#azure-app-registration)
- [Configuration Reference](#configuration-reference)
- [Usage: Adding a Tenant](#usage-adding-a-tenant)
- [Security](#security)
- [Troubleshooting](#troubleshooting)

---

## Problem & Solution

Microsoft disabled SMTP AUTH (Basic Authentication) for Exchange Online / Office 365 in October 2022. Many printers, scanners, and multifunction devices can only send emails via plain SMTP with username and password — they cannot speak OAuth 2.0.

**This proxy solves the problem:**

```
Printer/Scanner
  │  SMTP + STARTTLS
  │  username / password (configured once on the device)
  ▼
Tenant SMTP Proxy Container  (per customer, on your Docker host)
  │  OAuth 2.0 (MSAL)
  │  Client Credentials → Microsoft Graph API
  │  — or —
  │  Delegated (Refresh Token) → SMTP XOAUTH2
  ▼
smtp.office365.com / graph.microsoft.com
  ▼
Microsoft 365 Mail Delivery
```

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Docker Host                       │
│                                                      │
│  ┌──────────────────────────────┐                   │
│  │   Admin Container            │                   │
│  │   FastAPI Web UI  :8080      │                   │
│  │   ┌─────────────────────┐   │                   │
│  │   │ Tenant Management   │   │                   │
│  │   │ OAuth Configuration │   │◄─── Admin Browser │
│  │   │ Mail Log Viewer     │   │                   │
│  │   │ Brute-Force Reset   │   │                   │
│  │   │ 2FA (TOTP)          │   │                   │
│  │   └──────────┬──────────┘   │                   │
│  └──────────────│──────────────┘                   │
│                 │ Docker API + shared volume         │
│  ┌──────────────▼──────────────┐                   │
│  │   Tenant A Container        │                   │
│  │   Postfix  :10025 (SMTP)    │◄── Printer/Scanner│
│  │   Dovecot  (SASL backend)   │                   │
│  │   Self-signed TLS cert       │                   │
│  │   Postfix rate limiting      │                   │
│  │   oauth_deliver.py (MDA)    │                   │
│  └──────────────┬──────────────┘                   │
│                 │                                   │
│  ┌──────────────▼──────────────┐                   │
│  │   Tenant B Container        │                   │
│  │   (same structure)          │◄── Printer/Scanner│
│  └──────────────────────────────┘                   │
└─────────────────────────────────────────────────────┘
```

**Two Docker images, two roles:**

| Image | Role |
|---|---|
| `smtp-proxy-admin` | Single management container; handles all tenants |
| `smtp-proxy-tenant` | One per customer; Postfix + Dovecot + Python MDA |

**Delivery flow inside a tenant container:**
```
Postfix (SMTP server, STARTTLS, SASL auth via Dovecot)
  → Postfix pipe transport
  → oauth_deliver.py (reads /etc/oauth_proxy_config.json)
  → MSAL token acquisition
  → smtp.office365.com (XOAUTH2) or graph.microsoft.com (sendMail)
```

---

## Features

- **Multi-tenant** — unlimited customers, each fully isolated in their own container
- **SMTP with STARTTLS or direct TLS** — self-signed certificate auto-generated on first start (10-year validity)
- **OAuth 2.0 flows:**
  - *Client Credentials* — App registration with `Mail.Send` permission, sent via Microsoft Graph API
  - *Delegated* — Device Code Flow to capture a refresh token; relays via SMTP XOAUTH2
- **Brute-force protection** (resettable):
  - Admin login: persisted in SQLite, reset via web UI
  - SMTP login: Postfix `smtpd_client_auth_rate_limit` — configurable attempts/window per tenant
- **TOTP 2FA** — mandatory on first admin login; compatible with Google Authenticator, Microsoft Authenticator, Authy
- **Mail logging** — every delivery attempt logged with status, sender, recipients, error details, and OAuth error flag
- **Docker-based deployment** — admin starts/stops/restarts tenant containers via Docker API
- **Encrypted secrets** — all OAuth secrets and SMTP passwords encrypted at rest with Fernet (AES-128-CBC)
- **Port mode** — each tenant gets a port from a configurable base port range
- **Subdomain mode** — Traefik-compatible labels for `tenant.smtp.example.com:587` routing

---

## Prerequisites

- Docker Engine 24+ and Docker Compose v2
- Docker socket accessible to the admin container (`/var/run/docker.sock`)
- The `smtp-proxy-tenant` image must be built and available locally before creating any tenant
- Network access to `login.microsoftonline.com`, `smtp.office365.com`, `graph.microsoft.com`
- A Microsoft 365 (Exchange Online) organization with admin access to Azure AD

---

## Installation

### Automated setup (recommended)

```bash
git clone git@github.com:SMLunchen/office365-oauth-multitenant-proxy.git
cd office365-oauth-multitenant-proxy
chmod +x setup.sh
./setup.sh
```

The setup script handles everything automatically:
- Generates `SECRET_KEY` and `ENCRYPTION_KEY` via `openssl` (no Python required)
- Prompts for the admin password if not yet set
- Builds the proxy image (`smtp-proxy-tenant:latest`)
- Builds and starts the admin container
- Waits for the service to become available
- Prints the URL and next steps

Re-running `setup.sh` is safe — existing `.env` values and already-built images are not overwritten without confirmation.

---

### Manual setup (step by step)

#### 1. Clone the repository

```bash
git clone git@github.com:SMLunchen/office365-oauth-multitenant-proxy.git
cd office365-oauth-multitenant-proxy
```

#### 2. Create the `.env` file

```bash
cp .env.example .env
```

Generate the required keys — **no Python needed**, `openssl` is sufficient:

```bash
# Patch both keys directly into .env:
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')|" .env
```

Then set the admin password in `.env`:
```dotenv
ADMIN_PASSWORD=YourStrongPasswordHere
```

<details>
<summary>Alternative key generation with Python</summary>

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"           # SECRET_KEY
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```
</details>

#### 3. Build the proxy image

This image must be built once on the Docker host before any tenant container can be started:

```bash
docker build -t smtp-proxy-tenant:latest ./proxy
```

#### 4. Build and start the admin container

```bash
docker compose up -d --build
```

#### 5. Open the admin UI

```
http://your-server-ip:8080
```

Log in with the credentials from your `.env`. You will be prompted to set up TOTP 2FA immediately — scan the QR code with Google Authenticator before you can proceed.

---

## Azure App Registration

You need one App Registration per customer tenant (in their Azure AD).

### Client Credentials Flow (recommended — simpler)

1. Go to **Azure Portal → Azure Active Directory → App registrations → New registration**
2. Name: e.g. `SMTP Relay Proxy`
3. Supported account types: *Accounts in this organizational directory only*
4. After creation, note the **Application (client) ID** and **Directory (tenant) ID**
5. Go to **Certificates & secrets → New client secret** — note the value immediately
6. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**
7. Add: `Mail.Send`
8. Click **Grant admin consent**

> The sender email address must be a valid mailbox in the customer's Microsoft 365 tenant.

### Delegated Flow (sends as a specific user)

1. Create an App Registration as above, but add:
   - **API permissions → Delegated → `SMTP.Send`** (under Outlook / Exchange)
   - Enable **Allow public client flows** (Authentication → Advanced settings)
2. In the admin UI, use the **Device Code Flow** button — the user visits a Microsoft URL, logs in once, and the refresh token is stored securely

---

## Configuration Reference

### `.env` variables

| Variable | Default | Description |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Admin login username |
| `ADMIN_PASSWORD` | *(required)* | Admin login password |
| `ADMIN_PORT` | `8080` | Host port for the admin UI |
| `SECRET_KEY` | *(required)* | Session signing key (hex string) |
| `ENCRYPTION_KEY` | *(required)* | Fernet key for secret encryption |
| `PROXY_NETWORK_NAME` | `smtp_proxy_net` | Docker network name |
| `PROXY_IMAGE_NAME` | `smtp-proxy-tenant` | Image name for tenant containers |
| `PORT_MODE_ENABLED` | `true` | Assign unique host ports to tenants |
| `PORT_MODE_BASE_PORT` | `10025` | First port to assign |
| `SUBDOMAIN_MODE_ENABLED` | `false` | Add Traefik labels for subdomain routing |
| `SUBDOMAIN_BASE_DOMAIN` | | e.g. `smtp.example.com` for subdomain mode |
| `ADMIN_BF_MAX_ATTEMPTS` | `5` | Failed logins before admin lockout |
| `ADMIN_BF_LOCKOUT_MINUTES` | `15` | Admin lockout duration |
| `SMTP_BF_MAX_ATTEMPTS` | `5` | Failed SMTP logins before lockout |
| `SMTP_BF_LOCKOUT_MINUTES` | `30` | SMTP lockout duration |

### Printer / Scanner configuration

Configure the device to use SMTP with these settings:

| Setting | Value |
|---|---|
| SMTP Server | IP address of your Docker host |
| Port | The port assigned to the tenant (e.g. `10025`) |
| Connection Security | STARTTLS |
| Authentication | Username + Password |
| Username | The SMTP username configured in the tenant |
| Password | The SMTP password configured in the tenant |

> The self-signed certificate will cause a warning on devices that validate TLS certificates. Import the certificate from the container (`/data/certs/smtp.crt`) into the device's trusted certificate store, or disable certificate validation on the device.

---

## Usage: Adding a Tenant

1. **Navigate to Tenants → New Tenant**
2. Fill in:
   - Name (internal identifier, e.g. `customer-abc`)
   - SMTP username (what the printer will use, e.g. `scan@customer-abc.local`)
   - SMTP password (choose a strong password)
   - Port (auto-suggested from the base port range)
3. **Save** the tenant
4. **Add OAuth configuration** (in the tenant detail view):
   - For Client Credentials: enter Azure Tenant ID, Client ID, Client Secret, Sender Email
   - For Delegated: click "Device Code Flow", have the user authenticate, then enter the client secret and sender email
5. **Start the container** using the Start button
6. **Configure the printer/scanner** with the displayed SMTP credentials

---

## Security

### Encryption at rest

All sensitive values (OAuth secrets, refresh tokens, SMTP passwords) are encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256) before being stored in SQLite. Two keys are used:

- **Global key** (`ENCRYPTION_KEY` in `.env`): encrypts data in the admin database
- **Per-tenant key** (generated on tenant creation): encrypts the config file written to the shared volume

### Session security

- HTTP-only, SameSite=Strict cookie
- Signed with HMAC using `SECRET_KEY`
- 8-hour expiry

### TLS certificates

Self-signed certificates are generated on container start if none exist. They are valid for 10 years and include the configured hostname as SAN. For production, replace with a certificate from your CA and mount it into the container at `/data/certs/smtp.crt` and `/data/certs/smtp.key`.

### Brute-force protection

| Target | Mechanism | Reset |
|---|---|---|
| Admin login | SQLite counter, survives restarts | Admin UI → Security page |
| SMTP login | Postfix `smtpd_client_auth_rate_limit` | Container restart or Postfix reload |

### Mail queue persistence

The Postfix mail queue is stored in `/data/spool/` which is mounted on a named Docker volume (`smtp_proxy_tenant_<id>_data`). Mail waiting for delivery survives container restarts and even container recreation. Only a `docker volume rm` would permanently delete queued mail.

### Firewall recommendations

- Expose only the tenant SMTP ports to your internal network (not to the internet)
- Put the admin UI behind a VPN or reverse proxy with HTTPS

---

## Troubleshooting

### Printer cannot connect / TLS error

The self-signed certificate is not trusted by the device. Either:
- Export `/data/certs/smtp.crt` from the container and import into the device's trust store
- Disable TLS certificate validation on the device (check device manual)
- Use a certificate from an internal CA (mount at startup)

```bash
# Export the certificate from a running container:
docker exec smtp_proxy_tenant_1 cat /data/certs/smtp.crt > tenant1.crt
```

### OAuth token error in mail log

Check the Azure App Registration:
- For Client Credentials: verify `Mail.Send` application permission has admin consent
- For Delegated: the refresh token may have expired — use Device Code Flow again to re-authenticate
- Check that the sender email exists as a mailbox in the tenant

### Container fails to start

```bash
# Check container logs (shows Postfix/Dovecot startup errors):
docker logs smtp_proxy_tenant_<id>

# Check admin logs:
docker compose logs admin
```

Common causes: the tenant config file is missing or malformed, the port is already in use, or the proxy image has not been built.

### Mail is stuck in queue / not delivered

```bash
# Check queue content:
docker exec smtp_proxy_tenant_1 mailq

# Force immediate delivery attempt:
docker exec smtp_proxy_tenant_1 postfix flush

# Watch live delivery attempts:
docker logs -f smtp_proxy_tenant_1
```

Common causes: no OAuth config set for the tenant, expired refresh token (delegated flow), missing `Mail.Send` permission (client credentials), sender address not found in the Microsoft 365 tenant.

### Admin UI inaccessible

```bash
docker compose ps
docker compose logs admin
```

---

---

---

# Office 365 OAuth Multi-Tenant SMTP Proxy

> **Autor:** Gerrit Haas &lt;github@schwarzes-seelenreich.de&gt;  
> **Lizenz:** MIT — siehe [LICENSE](LICENSE)

Ein selbst gehosteter, mandantenfähiger SMTP-Proxy, der ältere Geräte (Drucker, Scanner, Kopierer) mit Microsoft 365 verbindet. Er umgeht die Abschaltung von Basic Auth / SMTP AUTH durch Microsoft. Jeder Kunde erhält einen eigenen isolierten Docker-Container mit TLS, Brute-Force-Schutz und vollständiger Mail-Protokollierung.

---

## Inhaltsverzeichnis (Deutsch)

- [Problem & Lösung](#problem--lösung)
- [Architektur](#architektur-1)
- [Funktionen](#funktionen)
- [Voraussetzungen](#voraussetzungen)
- [Installation](#installation-1)
- [Azure App-Registrierung](#azure-app-registrierung)
- [Konfigurationsreferenz](#konfigurationsreferenz)
- [Bedienung: Tenant anlegen](#bedienung-tenant-anlegen)
- [Sicherheit](#sicherheit-1)
- [Fehlerbehebung](#fehlerbehebung)

---

## Problem & Lösung

Microsoft hat SMTP AUTH (Basic Authentication) für Exchange Online / Office 365 im Oktober 2022 deaktiviert. Viele Drucker, Scanner und Multifunktionsgeräte können E-Mails nur über einfaches SMTP mit Benutzername und Passwort versenden — OAuth 2.0 wird von diesen Geräten nicht unterstützt.

**Dieser Proxy löst das Problem:**

```
Drucker/Scanner
  │  SMTP + STARTTLS
  │  Benutzername / Passwort (einmal am Gerät konfiguriert)
  ▼
Tenant-SMTP-Proxy-Container  (pro Kunde, auf eurem Docker-Host)
  │  OAuth 2.0 (MSAL)
  │  Client Credentials → Microsoft Graph API
  │  — oder —
  │  Delegiert (Refresh-Token) → SMTP XOAUTH2
  ▼
smtp.office365.com / graph.microsoft.com
  ▼
Microsoft 365 E-Mail-Zustellung
```

---

## Architektur

Zwei Docker-Images, zwei Rollen:

| Image | Rolle |
|---|---|
| `smtp-proxy-admin` | Einzelner Verwaltungscontainer; steuert alle Tenants |
| `smtp-proxy-tenant` | Einer pro Kunde; führt den SMTP-Server aus |

Der Admin-Container kommuniziert über die Docker-API mit dem Docker-Host und startet/stoppt Tenant-Container. Die Konfiguration wird über eine gemeinsam genutzte Docker-Volume übergeben.

---

## Funktionen

- **Mandantenfähig** — unbegrenzt viele Kunden, jeder vollständig in seinem eigenen Container isoliert
- **SMTP mit STARTTLS oder direktem TLS** — selbstsigniertes Zertifikat wird beim ersten Start automatisch erzeugt (10 Jahre Gültigkeit)
- **OAuth 2.0-Flows:**
  - *Client Credentials* — App-Registrierung mit `Mail.Send`-Berechtigung, Versand über Microsoft Graph API
  - *Delegiert* — Geräte-Code-Flow zum einmaligen Einloggen, danach SMTP XOAUTH2
- **Brute-Force-Schutz** (zurücksetzbar):
  - Admin-Login: persistent in SQLite, Reset über Web-UI
  - SMTP-Login: In-Memory pro Container, Reset über Admin-API
- **TOTP 2FA** — beim ersten Admin-Login verpflichtend; kompatibel mit Google Authenticator, Microsoft Authenticator, Authy
- **Mail-Protokollierung** — jeder Zustellversuch wird mit Status, Absender, Empfänger, Fehlerdetails und OAuth-Fehler-Flag protokolliert
- **Docker-basiertes Deployment** — Admin startet/stoppt/neustarte Tenant-Container über Docker-API
- **Verschlüsselte Secrets** — alle OAuth-Secrets und SMTP-Passwörter werden mit Fernet (AES-128-CBC) verschlüsselt gespeichert
- **Port-Modus** — jeder Tenant erhält einen Port aus einem konfigurierbaren Bereich
- **Subdomain-Modus** — Traefik-kompatible Labels für `tenant.smtp.example.com:587`-Routing

---

## Voraussetzungen

- Docker Engine 24+ und Docker Compose v2
- Docker-Socket muss dem Admin-Container zugänglich sein (`/var/run/docker.sock`)
- Das `smtp-proxy-tenant`-Image muss lokal gebaut und verfügbar sein, bevor ein Tenant-Container erstellt werden kann
- Netzwerkzugang zu `login.microsoftonline.com`, `smtp.office365.com`, `graph.microsoft.com`
- Eine Microsoft 365 (Exchange Online) Organisation mit Administrator-Zugriff auf Azure AD

---

## Installation

### 1. Repository klonen

```bash
git clone git@github.com:SMLunchen/office365-oauth-multitenant-proxy.git
cd office365-oauth-multitenant-proxy
```

### 2. `.env`-Datei erstellen

```bash
cp .env.example .env
```

`.env` öffnen und die erforderlichen Werte eintragen:

**Mit Python:**
```bash
# SECRET_KEY generieren:
python3 -c "import secrets; print(secrets.token_hex(32))"

# ENCRYPTION_KEY generieren:
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Ohne Python (nur OpenSSL — funktioniert auf jedem Linux-Server):**
```bash
# SECRET_KEY generieren:
openssl rand -hex 32

# ENCRYPTION_KEY generieren:
openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n'
```

**One-Liner: beide Keys direkt in `.env` schreiben (kein Python nötig):**
```bash
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(openssl rand -hex 32)|" .env
sed -i "s|^ENCRYPTION_KEY=.*|ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')|" .env
```

Mindestanforderungen in `.env`:
```dotenv
ADMIN_USERNAME=admin
ADMIN_PASSWORD=StarkesPasswortHier
SECRET_KEY=<oben generiert>
ENCRYPTION_KEY=<oben generiert>
PORT_MODE_ENABLED=true
PORT_MODE_BASE_PORT=10025
```

### 3. Tenant-Proxy-Image bauen

Dieses Image muss einmalig gebaut werden, bevor Tenant-Container erstellt werden können:

```bash
docker build -t smtp-proxy-tenant:latest ./proxy
```

### 4. Admin-Container bauen und starten

```bash
docker compose up -d --build
```

### 5. Admin-Oberfläche öffnen

```
http://server-ip:8080
```

Mit den Zugangsdaten aus `.env` einloggen. Beim ersten Login wird sofort die Einrichtung von TOTP 2FA verlangt — QR-Code mit Google Authenticator scannen, bevor die Oberfläche zugänglich wird.

---

## Azure App-Registrierung

Pro Kunden-Tenant (in dessen Azure AD) wird eine App-Registrierung benötigt.

### Client Credentials Flow (empfohlen — einfacher)

1. **Azure Portal → Azure Active Directory → App-Registrierungen → Neue Registrierung**
2. Name: z.B. `SMTP Relay Proxy`
3. Unterstützte Kontotypen: *Nur Konten in diesem Organisationsverzeichnis*
4. Nach der Erstellung **Anwendungs-ID (Client-ID)** und **Verzeichnis-ID (Mandanten-ID)** notieren
5. **Zertifikate & Geheimnisse → Neuer geheimer Clientschlüssel** — Wert sofort kopieren
6. **API-Berechtigungen → Berechtigung hinzufügen → Microsoft Graph → Anwendungsberechtigungen**
7. `Mail.Send` hinzufügen
8. **Administratorzustimmung erteilen** klicken

> Die Absender-E-Mail-Adresse muss ein gültiges Postfach im Microsoft 365-Tenant des Kunden sein.

### Delegierter Flow (sendet als bestimmter Nutzer)

1. App-Registrierung wie oben, aber zusätzlich:
   - **API-Berechtigungen → Delegiert → `SMTP.Send`** (unter Outlook / Exchange)
   - **Öffentliche Client-Flows erlauben** aktivieren (Authentifizierung → Erweiterte Einstellungen)
2. Im Admin-UI die Schaltfläche **Geräte-Code-Anmeldung** verwenden — der Nutzer meldet sich einmalig über eine Microsoft-URL an, das Refresh-Token wird sicher gespeichert

---

## Konfigurationsreferenz

### `.env`-Variablen

| Variable | Standard | Beschreibung |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Admin-Benutzername |
| `ADMIN_PASSWORD` | *(erforderlich)* | Admin-Passwort |
| `ADMIN_PORT` | `8080` | Host-Port für die Admin-Oberfläche |
| `SECRET_KEY` | *(erforderlich)* | Session-Signierschlüssel |
| `ENCRYPTION_KEY` | *(erforderlich)* | Fernet-Schlüssel für Secrets-Verschlüsselung |
| `PROXY_NETWORK_NAME` | `smtp_proxy_net` | Docker-Netzwerkname |
| `PROXY_IMAGE_NAME` | `smtp-proxy-tenant` | Image-Name für Tenant-Container |
| `PORT_MODE_ENABLED` | `true` | Eindeutige Host-Ports für Tenants vergeben |
| `PORT_MODE_BASE_PORT` | `10025` | Erster zu vergebender Port |
| `SUBDOMAIN_MODE_ENABLED` | `false` | Traefik-Labels für Subdomain-Routing |
| `SUBDOMAIN_BASE_DOMAIN` | | z.B. `smtp.example.com` für Subdomain-Modus |
| `ADMIN_BF_MAX_ATTEMPTS` | `5` | Fehlversuche bis Admin-Sperrung |
| `ADMIN_BF_LOCKOUT_MINUTES` | `15` | Admin-Sperrdauer in Minuten |
| `SMTP_BF_MAX_ATTEMPTS` | `5` | Fehlversuche bis SMTP-Sperrung |
| `SMTP_BF_LOCKOUT_MINUTES` | `30` | SMTP-Sperrdauer in Minuten |

### Drucker-/Scanner-Konfiguration

Gerät mit folgenden SMTP-Einstellungen konfigurieren:

| Einstellung | Wert |
|---|---|
| SMTP-Server | IP-Adresse des Docker-Hosts |
| Port | Der dem Tenant zugewiesene Port (z.B. `10025`) |
| Verbindungssicherheit | STARTTLS |
| Authentifizierung | Benutzername + Passwort |
| Benutzername | Der im Tenant konfigurierte SMTP-Benutzername |
| Passwort | Das im Tenant konfigurierte SMTP-Passwort |

> Das selbstsignierte Zertifikat löst auf Geräten, die TLS-Zertifikate validieren, eine Warnung aus. Das Zertifikat aus dem Container (`/data/certs/smtp.crt`) in den Zertifikatsspeicher des Geräts importieren oder die Zertifikatsvalidierung am Gerät deaktivieren.

```bash
# Zertifikat aus laufendem Container exportieren:
docker exec smtp_proxy_tenant_1 cat /data/certs/smtp.crt > tenant1.crt
```

---

## Bedienung: Tenant anlegen

1. **Tenants → Neuer Tenant** aufrufen
2. Ausfüllen:
   - Name (interne Bezeichnung, z.B. `kunde-abc`)
   - SMTP-Benutzername (was der Drucker verwendet, z.B. `scan@kunde-abc.lokal`)
   - SMTP-Passwort (sicheres Passwort wählen)
   - Port (wird automatisch aus dem Portbereich vorgeschlagen)
3. **Speichern**
4. **OAuth-Konfiguration hinzufügen** (in der Tenant-Detailansicht):
   - Client Credentials: Azure Tenant-ID, Client-ID, Client-Secret, Absender-E-Mail eingeben
   - Delegiert: „Geräte-Code-Anmeldung" klicken, Nutzer authentifiziert sich einmalig, dann Client-Secret und Absender-E-Mail eingeben
5. **Container starten** über die Start-Schaltfläche
6. **Drucker/Scanner konfigurieren** mit den angezeigten SMTP-Zugangsdaten

---

## Sicherheit

### Verschlüsselung im Ruhezustand

Alle sensiblen Werte (OAuth-Secrets, Refresh-Token, SMTP-Passwörter) werden vor der Speicherung mit **Fernet** (AES-128-CBC + HMAC-SHA256) verschlüsselt. Zwei Schlüssel werden verwendet:

- **Globaler Schlüssel** (`ENCRYPTION_KEY` in `.env`): verschlüsselt Daten in der Admin-Datenbank
- **Mandantenspezifischer Schlüssel** (bei Tenant-Erstellung generiert): verschlüsselt die in das gemeinsame Volume geschriebene Konfigurationsdatei

### Session-Sicherheit

- HTTP-only, SameSite=Strict Cookie
- HMAC-signiert mit `SECRET_KEY`
- 8 Stunden Ablaufzeit

### TLS-Zertifikate

Selbstsignierte Zertifikate werden beim Container-Start erzeugt, falls keine vorhanden sind. Sie sind 10 Jahre gültig und enthalten den konfigurierten Hostnamen als SAN. Für den Produktionseinsatz mit eigenem CA-Zertifikat: Zertifikat als `/data/certs/smtp.crt` und `/data/certs/smtp.key` in den Container mounten.

### Brute-Force-Schutz

| Ziel | Mechanismus | Reset |
|---|---|---|
| Admin-Login | SQLite-Zähler, überlebt Neustarts | Admin-UI → Seite „Sicherheit" |
| SMTP-Login | Postfix `smtpd_client_auth_rate_limit` | Container-Neustart oder Postfix reload |

### Mail-Queue-Persistenz

Die Postfix-Mail-Queue liegt unter `/data/spool/` und ist auf einem benannten Docker-Volume (`smtp_proxy_tenant_<id>_data`) gespeichert. Mails in der Queue überleben Container-Neustarts und auch das Entfernen und Neuerstellen des Containers. Nur ein `docker volume rm` löscht die Queue dauerhaft.

### Firewall-Empfehlungen

- Nur die Tenant-SMTP-Ports ins interne Netzwerk freigeben (nicht ins Internet)
- Admin-UI hinter VPN oder Reverse-Proxy mit HTTPS betreiben

---

## Fehlerbehebung

### Drucker kann sich nicht verbinden / TLS-Fehler

Das selbstsignierte Zertifikat wird vom Gerät nicht als vertrauenswürdig eingestuft. Lösungen:
- `/data/certs/smtp.crt` aus dem Container exportieren und in den Zertifikatsspeicher des Geräts importieren
- TLS-Zertifikatsvalidierung am Gerät deaktivieren (Gerätehandbuch beachten)
- Zertifikat einer internen CA verwenden (beim Container-Start mounten)

### OAuth-Token-Fehler im Mail-Log

Azure-App-Registrierung prüfen:
- Client Credentials: `Mail.Send`-Anwendungsberechtigung hat Admin-Zustimmung?
- Delegiert: Refresh-Token evtl. abgelaufen → erneut über Geräte-Code-Flow authentifizieren
- Absender-E-Mail als Postfach im Tenant vorhanden?

### Container startet nicht

```bash
# Container-Logs prüfen (zeigt Postfix/Dovecot-Startfehler):
docker logs smtp_proxy_tenant_<id>

# Admin-Logs prüfen:
docker compose logs admin
```

Häufige Ursachen: Konfigurationsdatei fehlt oder ist fehlerhaft, Port bereits belegt, Proxy-Image nicht gebaut.

### Mails kommen nicht an / bleiben in der Queue

```bash
# Queue-Inhalt prüfen:
docker exec smtp_proxy_tenant_1 mailq

# Sofortige Zustellung erzwingen:
docker exec smtp_proxy_tenant_1 postfix flush

# Live-Log der Zustellversuche:
docker logs -f smtp_proxy_tenant_1
```

Häufige Ursachen: keine OAuth-Konfiguration für den Tenant, abgelaufenes Refresh-Token (delegierter Flow), fehlende `Mail.Send`-Berechtigung (Client Credentials), Absender-E-Mail nicht als Postfach im Microsoft 365-Tenant vorhanden.

### Admin-Oberfläche nicht erreichbar

```bash
docker compose ps
docker compose logs admin
```

---

## Mitwirkende / Contributing

Beiträge sind willkommen. Bitte öffne ein Issue oder einen Pull Request.  
Contributions are welcome. Please open an issue or pull request.

---

*Copyright (c) 2025 Gerrit Haas — MIT License*
