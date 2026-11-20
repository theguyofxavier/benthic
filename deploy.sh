#!/usr/bin/env bash
# Installs or updates Benthic Online on a Raspberry Pi.
# Run with:  curl -fsSL https://raw.githubusercontent.com/theguyofxavier/benthic/main/deploy.sh | bash
set -e
APP="$HOME/benthic-online"
REPO="https://github.com/theguyofxavier/benthic.git"

echo "== Getting the latest Benthic =="
if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only; else git clone "$REPO" "$APP"; fi

if [ ! -f "$APP/.env" ]; then
  echo "== Looking for your Groq key from the old judge server =="
  OLD=$(systemctl show -p ExecStart benthic-judge 2>/dev/null | grep -oE '/[^ ;]+\.py' | head -1 || true)
  OLDDIR=$( [ -n "$OLD" ] && dirname "$OLD" || echo "$HOME/benthic-judge")
  KEY=$( (cat "$OLDDIR"/.env "$OLDDIR"/*.env "$OLDDIR"/*.py 2>/dev/null; sudo cat /etc/systemd/system/benthic-judge.service 2>/dev/null) | grep -oE 'gsk_[A-Za-z0-9]{20,}' | head -1 || true)
  MODEL=$( (cat "$OLDDIR"/.env "$OLDDIR"/*.py 2>/dev/null) | grep -oE '(GROQ_MODEL=|MODEL *= *["'"'"'])[A-Za-z0-9._/-]+' | head -1 | sed -E 's/.*[=\"'"'"' ]//' || true)
  if [ -z "$KEY" ]; then
    read -rsp "Couldn't find it. Paste your Groq API key (it won't show): " KEY < /dev/tty; echo
  else
    echo "Found it."
  fi
  printf 'GROQ_API_KEY=%s\nGROQ_MODEL=%s\nPORT=8787\n' "$KEY" "${MODEL:-llama-3.3-70b-versatile}" > "$APP/.env"
  chmod 600 "$APP/.env"
fi

echo "== Swapping the old judge server for Benthic Online =="
sudo systemctl disable --now benthic-judge 2>/dev/null || true

sudo tee /etc/systemd/system/benthic-online.service >/dev/null <<EOF
[Unit]
Description=Benthic Online (game, accounts, chat, AI judge)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 $APP/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

# check GitHub for updates every 10 minutes; the server restarts itself when one lands
sudo tee /etc/systemd/system/benthic-update.service >/dev/null <<EOF
[Unit]
Description=Pull Benthic updates

[Service]
Type=oneshot
User=$USER
ExecStart=/usr/bin/git -C $APP pull --ff-only -q
EOF
sudo tee /etc/systemd/system/benthic-update.timer >/dev/null <<EOF
[Unit]
Description=Check for Benthic updates every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now benthic-online benthic-update.timer
sudo systemctl restart benthic-online
sleep 3
echo "== Checking =="
curl -s localhost:8787/api/health; echo
sudo tailscale funnel --bg 8787 >/dev/null 2>&1 || true
sudo tailscale funnel status | grep https || true
echo
echo "Done! Benthic Online is at the https address above."
