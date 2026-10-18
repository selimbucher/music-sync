from music_sync import notify


class FakeSMTP:
    sent = []
    fail = False

    def __init__(self, host, port, timeout):
        if FakeSMTP.fail:
            raise ConnectionRefusedError("no listener")
        self.where = (host, port)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send_message(self, msg):
        FakeSMTP.sent.append((self.where, msg["To"], msg["Subject"]))


def test_mail_goes_over_smtp_to_localhost(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    assert notify.send("me@example.org", "music-sync@example.org", "needs review", "body")
    assert FakeSMTP.sent == [(("localhost", 25), "me@example.org", "[music-sync] needs review")]


def test_falls_back_to_sendmail_and_never_raises(monkeypatch):
    FakeSMTP.fail = True
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.os.path, "exists", lambda p: False)
    monkeypatch.setattr(notify.shutil, "which", lambda n: None)
    assert notify.send("me@example.org", "x@example.org", "s", "b") is False
    assert notify.send(None, "x@example.org", "s", "b") is False


def test_dry_run_never_mails(monkeypatch, tmp_path):
    from music_sync import cli
    from music_sync.config import Config
    from music_sync.models import APPLE, SPOTIFY
    from music_sync.state import State
    from tests.fakes import Fake, world
    w = world(("ISRC-A", "Alpha", "Artist One", 200000, "am-a", "sp-a"))
    apple, spotify = Fake(APPLE, w), Fake(SPOTIFY, w)
    apple.playlists_["am-pl-1"] = {"name": "Only Apple", "items": ["am-a"]}     # produces a review note
    cfg = Config(state_path=tmp_path / "s.db", sync_albums=False); cfg.apple_user_token = "x"
    cfg.notify_email = "me@example.org"
    sent = []
    monkeypatch.setattr(cli, "Config", lambda: cfg)
    monkeypatch.setattr(cli, "_providers", lambda c, s: (apple, spotify))
    monkeypatch.setattr(cli.notify, "send", lambda *a, **k: sent.append(a) or True)
    assert cli.main(["seed", "--dry-run"]) == 2
    assert sent == []
