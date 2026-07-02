# ZenFlow — Phase 1 Deployment Runbook
# ══════════════════════════════════════════════════════════════

## What Phase 1 delivers
- SECRET_KEY enforced from .env (startup fails if missing or < 64 chars)
- Rate limiting on every endpoint via slowapi (5/min login, 3/min register, etc.)
- CORS locked to explicit ALLOWED_ORIGINS — no wildcard in production
- Access token (24h) + Refresh token (30d) with rotation
- Security response headers on every response (HSTS, CSP, X-Frame-Options, etc.)
- Structured JSON request logging (every req/res with latency + IP)
- Input sanitisation — HTML tags and control chars stripped from all string fields
- Timing-safe password comparison (prevents timing attacks)
- Password strength validation (uppercase + lowercase + digit required)
- Email normalisation + disposable domain blocklist
- JWT audience + issuer claims (prevents token reuse across services)
- Pagination on all list endpoints (page + page_size params)
- Past-date booking validation
- Nginx reverse proxy with TLS, rate limiting, connection limits
- systemd service with sandboxing and auto-restart


## Server requirements
- Ubuntu 22.04 LTS (or Debian 12)
- 1 vCPU, 1 GB RAM minimum (2 GB recommended)
- 20 GB SSD
- Open ports: 80, 443


## Step 1 — Server setup
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git

# Create dedicated user
sudo useradd -r -s /bin/false zenflow
sudo mkdir -p /opt/zenflow /var/log/zenflow
sudo chown zenflow:zenflow /opt/zenflow /var/log/zenflow
```


## Step 2 — Deploy the API
```bash
# Copy files to server
scp -r zenflow-api/ user@your-server:/opt/zenflow/

# On the server
cd /opt/zenflow
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```


## Step 3 — Configure environment
```bash
cp .env.example .env
nano .env
```

Fill in:
```
ZF_SECRET=<output of: python3 -c "import secrets; print(secrets.token_hex(64))">
ALLOWED_ORIGINS=https://zenflow.sg,https://www.zenflow.sg
DB_PATH=/opt/zenflow/zenflow.db
ENV=production
ACCESS_TOKEN_TTL_HOURS=24
REFRESH_TOKEN_TTL_DAYS=30
```

Lock the .env file:
```bash
chmod 600 /opt/zenflow/.env
sudo chown zenflow:zenflow /opt/zenflow/.env
```


## Step 4 — Test the API locally
```bash
source venv/bin/activate
uvicorn main:app --port 8000 --host 127.0.0.1
curl http://localhost:8000/health
```


## Step 5 — Install systemd service
```bash
sudo cp zenflow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable zenflow
sudo systemctl start zenflow
sudo systemctl status zenflow   # should show "active (running)"
```


## Step 6 — Configure Nginx + TLS
```bash
# Copy nginx config
sudo cp nginx.conf /etc/nginx/sites-available/zenflow
sudo ln -s /etc/nginx/sites-available/zenflow /etc/nginx/sites-enabled/
sudo nginx -t   # must say "test is successful"

# Get TLS certificate (replace with your domain)
sudo certbot --nginx -d zenflow.sg -d www.zenflow.sg

# Reload nginx
sudo systemctl reload nginx
```


## Step 7 — Verify production
```bash
# Health check
curl https://zenflow.sg/api/health

# Confirm security headers are present
curl -I https://zenflow.sg/api/health | grep -E "Strict|Content-Security|X-Frame"

# Confirm rate limiting works (run 6 times quickly, 6th should 429)
for i in {1..6}; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST https://zenflow.sg/api/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"test@test.com","password":"wrong"}'
done
```


## Step 8 — Set up log rotation
```bash
sudo tee /etc/logrotate.d/zenflow << 'EOF'
/var/log/zenflow/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    sharedscripts
    postrotate
        systemctl reload zenflow
    endscript
}
EOF
```


## Monitoring commands (daily ops)
```bash
# Live logs
sudo journalctl -u zenflow -f

# Nginx access log
sudo tail -f /var/log/nginx/zenflow_access.log

# Check for 4xx/5xx errors in last hour
sudo grep " [45][0-9][0-9] " /var/log/nginx/zenflow_access.log | tail -50

# DB size
du -sh /opt/zenflow/zenflow.db

# Service status
sudo systemctl status zenflow nginx
```


## Update deployment (zero-downtime)
```bash
# Copy new files
scp main.py user@server:/opt/zenflow/

# Reload (graceful — drains active requests)
sudo systemctl reload zenflow
# OR for full restart:
sudo systemctl restart zenflow
```


## Rollback
```bash
# Keep previous version as main.py.bak before updating
sudo systemctl stop zenflow
sudo cp /opt/zenflow/main.py.bak /opt/zenflow/main.py
sudo systemctl start zenflow
```


## Security checklist before go-live
- [ ] .env has ZF_SECRET with 64+ random hex chars
- [ ] ENV=production in .env
- [ ] ALLOWED_ORIGINS set to your actual domain(s) only
- [ ] TLS certificate installed and auto-renewing (certbot renew --dry-run)
- [ ] /docs and /redoc return 404 (disabled in production)
- [ ] Rate limiting returns 429 after threshold
- [ ] Response headers include Strict-Transport-Security
- [ ] zenflow user has no shell access (ls -la /etc/passwd | grep zenflow)
- [ ] .env is chmod 600
- [ ] DB file is readable only by zenflow user
