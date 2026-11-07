import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("ops_report", Path(__file__).parents[1] / "deploy" / "ops-report.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def test_redact_hides_secrets():
    text = "klucz sk-ant-api03-ABCDEFGHIJKLMNOP token 1//0abcdefghijklmnopqrstuvwxyz sekret GOCSPX-xyz123"
    out = ops.redact(text)
    assert "ABCDEFGHIJKLMNOP" not in out and "abcdefghijklmnopqrstu" not in out and "xyz123" not in out
    assert "sk-ant-api…[ukryte]" in out


def test_recipient_only_from_team():
    team = ("barbara.bil@kancelariabil.pl", "lukasz@brandwise.pl")
    assert ops.recipient({}, team) == "lukasz@brandwise.pl"
    assert ops.recipient({"OPS_REPORT_TO": "obcy@example.com"}, team) is None
    assert ops.recipient({}, ("barbara.bil@kancelariabil.pl",)) is None
