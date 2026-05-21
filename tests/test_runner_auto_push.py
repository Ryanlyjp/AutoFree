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

    def fake_run_round(*, n, run_id, round_index, cancel, mail_provider=None, register_only=False):
        return {
            "round": round_index,
            "n": n,
            "registered": 1,
            "oauthed": 1,
            "kicked": 1,
            "errors": [],
            "oauth_emails": [f"user-{round_index}@example.com"],
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
