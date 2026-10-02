#!/usr/bin/env python3
"""Что из службы видно снаружи и чем она защищена — проверка только на чтение.

Ничего не меняет: смотрит, кто слушает порты, что Tailscale отдаёт наружу,
пускает ли Navidrome со стандартным паролем, закрыты ли файлы с ключами и что
отвечает /health без ключа. Что пользователь осознанно оставил как есть
(ACCEPTED), печатается как «принято», а не как провал.

    .venv/bin/python scripts/security_check.py          # таблица; выход 1 — есть провал

Файрвол (ufw) без sudo не прочесть: его строка — «проверить руками» с командой.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SERVICE_PORT = 8787
NAVIDROME_PORT = 4533

# Риски, которые пользователь принял сам: ключ — имя проверки, значение — когда и почему.
ACCEPTED = {
    "navidrome_default_password": "2026-09-18 и 2026-10-02 (вопрос Q1): оставить admin/admin",
}

OK, FAIL, ACCEPT, MANUAL = "ok", "ПРОВАЛ", "принято", "руками"


@dataclass
class Finding:
    check: str
    state: str
    detail: str


def listening(ss_output: str, port: int) -> list[str]:
    """Адреса, на которых слушает порт, из вывода `ss -ltnH`."""
    found = []
    for line in ss_output.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[3].rsplit(":", 1)[-1] == str(port):
            found.append(parts[3].rsplit(":", 1)[0])
    return found


def is_loopback_only(addresses: list[str]) -> bool:
    return bool(addresses) and all(a in ("127.0.0.1", "[::1]") for a in addresses)


def check_ports(ss_output: str) -> list[Finding]:
    out = []
    for name, port in (("service", SERVICE_PORT), ("navidrome", NAVIDROME_PORT)):
        addresses = listening(ss_output, port)
        if not addresses:
            out.append(Finding(f"{name}_port", FAIL, f":{port} никто не слушает"))
        elif is_loopback_only(addresses):
            out.append(Finding(f"{name}_port", OK, f":{port} только на этой машине"))
        else:
            out.append(
                Finding(
                    f"{name}_port",
                    MANUAL,
                    f":{port} слушает {', '.join(addresses)} — снаружи его держит только ufw: "
                    "sudo ufw status verbose (ждём доступ лишь из LAN_SUBNET)",
                )
            )
    return out


def check_tailscale(serve: str, funnel: str) -> Finding:
    text = serve + "\n" + funnel
    public = [line for line in text.splitlines() if "Funnel on" in line or "(Funnel" in line]
    if public:
        return Finding("tailscale", FAIL, "Funnel открывает в интернет: " + "; ".join(public))
    if not text.strip():
        # Пусто и без Tailscale, и при упавшем демоне, и без прав: не «ok».
        return Finding("tailscale", MANUAL, "tailscale ничего не сказал: tailscale serve status")
    if "tailnet only" in text:
        return Finding("tailscale", OK, "только внутри tailnet, Funnel выключен")
    return Finding("tailscale", MANUAL, "вывод tailscale не распознан: tailscale serve status")


def check_secret_files(paths: list[Path]) -> list[Finding]:
    out = []
    for path in paths:
        if not path.exists():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        state = OK if mode & 0o077 == 0 else FAIL
        out.append(Finding(f"perm:{path.name}", state, f"{path} — {oct(mode)}"))
    return out


def subsonic_ping(base: str, user: str, password: str) -> bool | None:
    """Пускает ли Navidrome с этими логином и паролем (None — не ответил)."""
    salt = "lsSecCheck"
    token = hashlib.md5((password + salt).encode()).hexdigest()  # noqa: S324 — так велит Subsonic API
    url = f"{base}/rest/ping.view?u={user}&t={token}&s={salt}&v=1.16.1&c=security_check&f=json"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 — свой адрес
            body = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return body.get("subsonic-response", {}).get("status") == "ok"


def check_navidrome_password(base: str) -> Finding:
    name = "navidrome_default_password"
    opened = subsonic_ping(base, "admin", "admin")
    if opened is None:
        return Finding(name, MANUAL, f"Navidrome ({base}) не ответил")
    if not opened:
        return Finding(name, OK, "admin/admin не пускает")
    if name in ACCEPTED:
        return Finding(name, ACCEPT, "admin/admin пускает — " + ACCEPTED[name])
    return Finding(name, FAIL, "admin/admin пускает в Navidrome")


def check_health_without_token(base: str) -> Finding:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:  # noqa: S310
            body = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Finding("health_anonymous", MANUAL, f"служба не ответила: {exc}")
    extra = sorted(set(body) - {"status"})
    if extra:
        return Finding("health_anonymous", FAIL, f"без ключа отдаёт лишнее: {extra}")
    return Finding("health_anonymous", OK, "без ключа — только status")


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def collect(service: str, navidrome: str) -> list[Finding]:
    findings = check_ports(run(["ss", "-ltnH"]))
    findings.append(
        check_tailscale(
            run(["tailscale", "serve", "status"]), run(["tailscale", "funnel", "status"])
        )
    )
    findings.append(check_navidrome_password(navidrome))
    findings.append(check_health_without_token(service))
    backups = Path(os.environ.get("BACKUP_DIR", Path.home() / "local-spotify-backups"))
    secrets = [PROJECT / "adder" / ".env", *sorted(backups.glob("env_*"))]
    findings += check_secret_files(secrets)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--service", default=f"http://127.0.0.1:{SERVICE_PORT}")
    parser.add_argument("--navidrome", default=f"http://127.0.0.1:{NAVIDROME_PORT}")
    args = parser.parse_args(argv)
    findings = collect(args.service.rstrip("/"), args.navidrome.rstrip("/"))
    for f in findings:
        print(f"{f.state:<8} {f.check:<28} {f.detail}")
    return 1 if any(f.state == FAIL for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
