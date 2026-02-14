# Odoo Deployment Platform

Self-hosted Odoo.sh alternative. One repo, one server, multi-version Odoo 18/19 instance management.

## Prerequisites

- Fresh Ubuntu 24.04 server (e.g. Hetzner, DigitalOcean, AWS)
- Root SSH access
- A domain with wildcard DNS pointing to the server (e.g. `*.odoo.example.com`)

## 1. Create SSH Key

**Windows (PowerShell or Git Bash):**

```powershell
ssh-keygen -t ed25519 -C "your-email@example.com"
cat ~/.ssh/id_ed25519.pub
```

**Linux / macOS:**

```bash
ssh-keygen -t ed25519 -C "your-email@example.com"
cat ~/.ssh/id_ed25519.pub
```

Add the public key to your server's `~/.ssh/authorized_keys`:

```bash
ssh root@your-server-ip "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys" < ~/.ssh/id_ed25519.pub
```

## 2. Connect to Server

SSH into the server with port forwarding so you can access the admin panel locally:

```bash
ssh -L 8080:localhost:8080 root@your-server-ip
```

This forwards port 8080 — you'll use `http://localhost:8080` to access the admin panel.

## 3. Install Git

```bash
sudo apt update
sudo apt install git
```

## 4. Clone and Bootstrap

```bash
git clone https://github.com/fuugus/odoo-platform.git /root/odoo-platform
cd /root/odoo-platform
chmod +x bootstrap.sh
./bootstrap.sh
```

The bootstrap script installs Python dependencies and starts the admin panel as a systemd service on port 8080.

## 5. Install Node.js (optional)

Required for Claude Code:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
```

## 6. Install Claude Code (optional)

For AI-assisted server management:

```bash
npm install -g @anthropic-ai/claude-code
cd /root/odoo-platform
claude
```

## 7. Access the Admin Panel

Open `http://localhost:8080` in your browser (via the SSH tunnel from step 2).

The Setup Wizard will guide you through:

1. **System Update** — installs build tools and libraries
2. **PostgreSQL 16** — database server
3. **wkhtmltopdf** — PDF rendering for Odoo reports
4. **Odoo 19 / Odoo 18 Source** — clone Community + Enterprise repos (at least one required)
5. **Nginx** — reverse proxy for subdomains
6. **Mailpit** — email catch-all for dev/staging
7. **DNS Check** — verifies wildcard DNS
8. **SSL Certificates** — Let's Encrypt for HTTPS

After completing DNS and SSL setup, access the panel at `https://admin.your-domain.com`.
