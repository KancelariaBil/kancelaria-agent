import yaml

from kancelaria_agent.__main__ import main


def write_cfg(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "guardrails": {"allowed_recipients": ["@example.com"], "allowed_clients": ["x"]},
    }))
    return str(tmp_path / "config.yaml")


def test_once_with_demo_creates_pending_proposals(tmp_path, capsys):
    cfg = write_cfg(tmp_path)
    assert main(["--config", cfg, "once", "--demo-mail"]) == 0
    assert "dry_run=True" in capsys.readouterr().out
    main(["--config", cfg, "proposals"])
    out = capsys.readouterr().out
    assert "create_notion_task" in out and "send_mail" not in out   # demo od obcego: bez odpowiedzi


def test_approve_refuses_without_tty(tmp_path, capsys, monkeypatch):
    cfg = write_cfg(tmp_path)
    main(["--config", cfg, "once", "--demo-mail"])
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["--config", cfg, "approve", "1"]) == 2
    assert "terminal" in capsys.readouterr().err
    main(["--config", cfg, "status"])
    assert "oczekujące: 1" in capsys.readouterr().out
