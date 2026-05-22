from pathlib import Path

import pytest

from autofree import cpa_push, runner, storage


@pytest.fixture()
def isolated_data_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    auths_dir = tmp_path / "auths"
    runs_dir = tmp_path / "runs"
    auths_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(storage, "AUTHS_DIR", auths_dir)
    monkeypatch.setattr(storage, "RUNS_DIR", runs_dir)


def test_auto_push_only_pushes_auths_from_current_run(
    monkeypatch: pytest.MonkeyPatch, isolated_data_dirs: None
) -> None:
    record = storage.create_run(rounds=2, per_round=1, params={"auto_push_cpa": True})

    def fake_run_round(
        *, n, run_id, round_index, cancel, mail_provider=None, register_only=False,
        auto_push_cpa=False, kick_mode=runner.KICK_MODE_ROUND_END,
    ):
        return {
            "round": round_index,
            "n": n,
            "registered": 1,
            "oauthed": 1,
            "kicked": 1,
            "errors": [],
            "oauth_emails": [f"user-{round_index}@example.com"],
            "cpa": {"enabled": False, "attempted": 0, "pushed": 0, "skipped": 0, "failed": 0},
        }

    pushed: dict[str, object] = {}

    def fake_push_many(emails: list[str], *, overwrite: bool = False):
        pushed["emails"] = emails
        pushed["overwrite"] = overwrite
        return {
            "pushed": len(emails),
            "skipped": 0,
            "failed": 0,
            "total": len(emails),
            "results": [{"email": email, "ok": True, "skipped": False} for email in emails],
        }

    monkeypatch.setattr(runner, "_run_round", fake_run_round)
    monkeypatch.setattr(cpa_push, "push_many", fake_push_many)

    runner._run_multi_inner(record["id"], rounds=2, per_round=1, mail_provider=None, auto_push_cpa=True)

    rec = storage.get_run(record["id"])
    assert rec is not None
    assert rec["status"] == "done"
    assert pushed["emails"] == ["user-1@example.com", "user-2@example.com"]
    assert pushed["overwrite"] is False
    assert rec["summary"]["cpa"] == {
        "enabled": True,
        "attempted": 2,
        "pushed": 2,
        "skipped": 0,
        "failed": 0,
    }


def test_start_run_rejects_auto_push_without_cpa_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "get_cpa_config", lambda: {"base_url": "", "key": ""})

    with pytest.raises(ValueError, match="CPA 未配置"):
        runner.start_run(1, 1, auto_push_cpa=True)


def test_immediate_kick_mode_skips_final_batch_cpa_push(
    monkeypatch: pytest.MonkeyPatch, isolated_data_dirs: None
) -> None:
    record = storage.create_run(
        rounds=1,
        per_round=1,
        params={"auto_push_cpa": True, "kick_mode": runner.KICK_MODE_AFTER_EACH_AUTH},
    )

    def fake_run_round(
        *, n, run_id, round_index, cancel, mail_provider=None, register_only=False,
        auto_push_cpa=False, kick_mode=runner.KICK_MODE_ROUND_END,
    ):
        assert kick_mode == runner.KICK_MODE_AFTER_EACH_AUTH
        return {
            "round": round_index,
            "n": n,
            "registered": 1,
            "oauthed": 1,
            "kicked": 1,
            "errors": [],
            "oauth_emails": ["user-1@example.com"],
            "cpa": {"enabled": True, "attempted": 1, "pushed": 1, "skipped": 0, "failed": 0},
        }

    def should_not_push_many(emails: list[str], *, overwrite: bool = False):
        raise AssertionError("push_many should not run in immediate kick mode")

    monkeypatch.setattr(runner, "_run_round", fake_run_round)
    monkeypatch.setattr(cpa_push, "push_many", should_not_push_many)

    runner._run_multi_inner(
        record["id"],
        rounds=1,
        per_round=1,
        mail_provider=None,
        auto_push_cpa=True,
        kick_mode=runner.KICK_MODE_AFTER_EACH_AUTH,
    )

    rec = storage.get_run(record["id"])
    assert rec is not None
    assert rec["status"] == "done"
    assert rec["summary"]["cpa"] == {"enabled": True, "attempted": 1, "pushed": 1, "skipped": 0, "failed": 0}


def test_immediate_kick_mode_pushes_each_auth_before_next_oauth(
    monkeypatch: pytest.MonkeyPatch, isolated_data_dirs: None
) -> None:
    record = storage.create_run(
        rounds=1,
        per_round=2,
        params={"auto_push_cpa": True, "kick_mode": runner.KICK_MODE_AFTER_EACH_AUTH},
    )
    events: list[str] = []
    mailboxes = iter([("box-1", "user-1@example.com"), ("box-2", "user-2@example.com")])

    class FakeMail:
        def login(self) -> None:
            return None

        def create_temp_email(self):
            return next(mailboxes)

    class FakeMasterClient:
        account_id = "acct_123"

        def set_auto_provision(self, value: bool) -> None:
            events.append(f"ap:{value}")

        def list_members(self) -> list[dict]:
            return []

        def kick_user_by_email(self, email: str):
            events.append(f"kick:{email}")
            return True, ""

    class FakeFlow:
        oauth_fail_reason = ""

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def set_mail_context(self, mailbox_id):
            self.mailbox_id = mailbox_id

        def start(self) -> None:
            return None

        def run_register(self, email: str, password: str, name: str, birthdate: str) -> None:
            events.append(f"register:{email}")

        def oauth_personal(self, email: str, password: str) -> dict[str, str]:
            events.append(f"oauth:{email}")
            return {"access_token": f"access-{email}", "refresh_token": "refresh", "id_token": "id-token"}

        def close(self) -> None:
            return None

    def fake_save_and_register(email: str, tokens: dict[str, str], *, extra=None) -> Path:
        events.append(f"save:{email}")
        return Path(f"/tmp/{email}.json")

    def fake_push_one(email: str, *, overwrite: bool = False) -> dict[str, object]:
        events.append(f"push:{email}")
        return {"ok": True, "skipped": False, "name": f"{email}.json"}

    monkeypatch.setattr(runner, "_build_flow", lambda: FakeFlow)
    monkeypatch.setattr(runner, "get_mail_client", lambda provider=None: FakeMail())
    monkeypatch.setattr(runner.master, "get_default_client", lambda: FakeMasterClient())
    monkeypatch.setattr(runner, "get_proxy", lambda: "")
    monkeypatch.setattr(runner, "AP_PROPAGATION_DELAY", 0)
    monkeypatch.setattr(runner, "_interruptible_sleep", lambda seconds, cancel: None)
    monkeypatch.setattr(cpa_push, "save_and_register", fake_save_and_register)
    monkeypatch.setattr(cpa_push, "push_one", fake_push_one)

    summary = runner._run_round(
        n=2,
        run_id=record["id"],
        round_index=1,
        cancel=runner.CancelSignal(),
        auto_push_cpa=True,
        kick_mode=runner.KICK_MODE_AFTER_EACH_AUTH,
    )

    assert summary["registered"] == 2
    assert summary["oauthed"] == 2
    assert summary["kicked"] == 2
    assert summary["cpa"] == {"enabled": True, "attempted": 2, "pushed": 2, "skipped": 0, "failed": 0}
    assert events.index("kick:user-1@example.com") < events.index("oauth:user-2@example.com")
    assert events.index("push:user-1@example.com") < events.index("oauth:user-2@example.com")
    assert events == [
        "ap:False",
        "register:user-1@example.com",
        "register:user-2@example.com",
        "ap:True",
        "oauth:user-1@example.com",
        "save:user-1@example.com",
        "kick:user-1@example.com",
        "push:user-1@example.com",
        "oauth:user-2@example.com",
        "save:user-2@example.com",
        "kick:user-2@example.com",
        "push:user-2@example.com",
    ]
