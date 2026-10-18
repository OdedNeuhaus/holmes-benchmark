import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("HOLMES_API_URL", "HOLMES_API_TOKEN", "HOLMES_MODEL", "HOLMES_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)


def test_env_configuration_and_cli_precedence(monkeypatch):
    from holmes_benchmark.config import parse_config

    monkeypatch.setenv("HOLMES_API_URL", "http://localhost:8080/api/chat")
    monkeypatch.setenv("HOLMES_API_TOKEN", "secret-test-token")
    monkeypatch.setenv("HOLMES_MODEL", "server-model")
    monkeypatch.setenv("HOLMES_TIMEOUT", "not-a-number")
    config = parse_config(["-q", "A", "-q", "B", "-p", "5", "--timeout", "12.5",
                           "--model", "fast", "--api-url", "https://holmes.test/api/chat",
                           "--export-to-excel"])
    assert config.questions == ("A", "B")
    assert config.parallel == 5
    assert config.api_url == "https://holmes.test/api/chat"
    assert config.timeout == 12.5
    assert config.model == "fast"
    assert config.token == "secret-test-token"
    assert "secret-test-token" not in repr(config)
    assert config.export_to_excel


def test_defaults_and_optional_authentication(monkeypatch):
    from holmes_benchmark.config import parse_config

    monkeypatch.setenv("HOLMES_API_URL", "http://localhost:8080/api/chat")
    config = parse_config(["-q", "What happened?"])
    assert (config.parallel, config.timeout, config.model, config.token) == (1, 300, None, None)
    assert not config.export_to_excel


@pytest.mark.parametrize("args", [
    [], ["-q", ""], ["-q", "   "], ["-q", "A", "-q", "B"],
    ["-q", "A", "-p", "0"], ["-q", "A", "-p", "-1"],
    ["-q", "A", "-p", "1.5"], ["-q", "A", "--timeout", "0"],
    ["-q", "A", "--timeout", "-3"], ["-q", "A", "--timeout", "nan"],
    ["-q", "A", "--timeout", "inf"], ["-q", "A", "--model", " "],
])
def test_rejects_invalid_workload(args, monkeypatch):
    from holmes_benchmark.config import parse_config

    monkeypatch.setenv("HOLMES_API_URL", "http://localhost:8080/api/chat")
    with pytest.raises(SystemExit) as exc:
        parse_config(args)
    assert exc.value.code == 2


@pytest.mark.parametrize("url", [
    "", "localhost:8080", "ftp://host/api/chat", "http://", "http://host:bad/api/chat",
    "http://user:password@host/api/chat", "http://host/api/chat#fragment", "http://host/has space",
])
def test_rejects_invalid_endpoint_without_echoing_it(url, capsys):
    from holmes_benchmark.config import parse_config

    with pytest.raises(SystemExit) as exc:
        parse_config(["-q", "A", "--api-url", url])
    assert exc.value.code == 2
    assert "password" not in capsys.readouterr().err


def test_rejects_invalid_auth_before_network(monkeypatch, capsys):
    from holmes_benchmark.config import parse_config

    monkeypatch.setenv("HOLMES_API_URL", "http://host/api/chat")
    monkeypatch.setenv("HOLMES_API_TOKEN", "secret\nheader")
    with pytest.raises(SystemExit) as exc:
        parse_config(["-q", "A"])
    assert exc.value.code == 2
    assert "secret" not in capsys.readouterr().err
