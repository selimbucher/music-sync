"""Mail to the operator.

SMTP to the local MTA first: the service runs with NoNewPrivileges, which
disables the setgid sendmail/postdrop wrapper, so a sandboxed process cannot
hand mail to postfix that way. ``sendmail`` stays as the fallback for hosts
without a listener on localhost. Failures to notify are logged, never fatal.
"""
from __future__ import annotations

import logging
import os
import shutil
import smtplib
import subprocess
from email.message import EmailMessage

log = logging.getLogger(__name__)


def send(to: str | None, sender: str, subject: str, body: str) -> bool:
    if not to:
        return False
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"[music-sync] {subject}"
    msg.set_content(body)

    host = os.environ.get("MUSIC_SYNC_SMTP_HOST", "localhost")
    port = int(os.environ.get("MUSIC_SYNC_SMTP_PORT", "25"))
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("notify via smtp %s:%s failed: %s; trying sendmail", host, port, e)

    wrapper = "/run/wrappers/bin/sendmail"
    sendmail = wrapper if os.path.exists(wrapper) else shutil.which("sendmail")
    if not sendmail:
        log.warning("notify: no sendmail on this host")
        return False
    try:
        subprocess.run([sendmail, "-t", "-oi"], input=msg.as_bytes(), check=True, timeout=30)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)
        return False
