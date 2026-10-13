import json
from pathlib import Path

from interop.cli import main
from interop.demo import run_demo
from interop.operations import healthy, summarize_logs


def test_quickstart_socket_demo():
    report = run_demo(Path(__file__).parents[1] / "samples")
    assert report["destination_resource_count"] == 4
    assert report["fhir_transaction_requests"] == 3
    assert report["health"]["rejections"] == 1
    assert report["health"]["states"] == {"delivered": 2}


def test_logs_count_operations_without_echoing_foreign_content():
    result = summarize_logs(
        [
            '{"event":"delivery","state":"delivered","diagnostic":null}',
            '{"event":"rejected","diagnostic":"profile.PID-3"}',
            '{"event":"delivery","diagnostic":"private patient content"}',
            "invalid",
            "[]",
        ]
    )
    assert result["events"] == {"delivery": 2, "rejected": 1}
    assert result["diagnostics"] == {"profile.PID-3": 1}
    assert result["malformed_lines"] == 2
    assert "private" not in json.dumps(result)


def test_health_exit_contract():
    assert healthy({"states": {"delivered": 5}, "oldest_pending_seconds": 0}, 60)
    assert not healthy({"states": {"exception": 1}, "oldest_pending_seconds": 0}, 60)
    assert not healthy({"states": {"pending": 1}, "oldest_pending_seconds": 61}, 60)


def test_cli_validation_and_failure_exit(monkeypatch, capsys):
    sample = Path(__file__).parents[1] / "samples/oru_r01.hl7"
    monkeypatch.setattr("sys.argv", ["interop-platform", "validate", str(sample)])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["valid_for_project_profile"]
    monkeypatch.setattr("sys.argv", ["interop-platform", "validate", "missing.hl7"])
    assert main() == 2
    assert json.loads(capsys.readouterr().err)["error"] == "configuration-or-input-error"
