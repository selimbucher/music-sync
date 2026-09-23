"""Mail via the local MTA. On the Hetzner box that is the mailserver's postfix;
elsewhere it is whatever ``sendmail`` is on PATH. Failures to notify are
logged, never fatal -- a sync must not die because mail did."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from email.message import EmailMessage

log = logging.getLogger(__name__)


def send(to: str | None, sender: str, subject: str, body: str) -> bool:
    if not to:
        return False
    # The setgid wrapper is what lets a system user hand mail to postfix.
    wrapper = "/run/wrappers/bin/sendmail"
    sendmail = wrapper if os.path.exists(wrapper) else shutil.which("sendmail")
    if not sendmail:
        log.warning("notify: no sendmail on this host")
        return False
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"[music-sync] {subject}"
    msg.set_content(body)
    try:
        subprocess.run([sendmail, "-t", "-oi"], input=msg.as_bytes(), check=True, timeout=30)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)
        return False
