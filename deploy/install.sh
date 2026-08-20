#!/usr/bin/env bash
set -e
REPO="$HOME/localSpotify"
mkdir -p ~/.config/systemd/user
cp "$REPO/deploy/music-adder.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now music-adder
loginctl enable-linger "$USER"
if command -v navidrome >/dev/null; then
  sudo install -d /etc/navidrome
  sed "s|/home/USER|$HOME|g" "$REPO/deploy/navidrome.toml.example" | sudo tee /etc/navidrome/navidrome.toml
  sudo install -d /etc/systemd/system/navidrome.service.d
  sudo cp "$REPO/deploy/navidrome-override.conf" /etc/systemd/system/navidrome.service.d/override.conf
  sudo systemctl daemon-reload && sudo systemctl restart navidrome
fi
sudo ufw allow from 192.168.0.0/16 to any port 4533 proto tcp comment "Navidrome LAN" || true
sudo ufw allow from 192.168.0.0/16 to any port 8787 proto tcp comment "Adder LAN" || true
echo "OK: adder http://localhost:8787 | navidrome http://localhost:4533"
