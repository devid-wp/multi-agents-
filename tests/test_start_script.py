from pathlib import Path


def test_start_script_is_local_and_uses_venv() -> None:
    script = (Path(__file__).parents[1] / "start.ps1").read_text(encoding="utf-8")
    assert '"--host", "127.0.0.1"' in script
    assert "0.0.0.0" not in script
    assert '".venv\\Scripts\\python.exe"' in script
    assert "-m pip install" in script
    assert "Python 3.11+" in script
    assert "ollama pull $requiredModel" in script
    assert "-WindowStyle Hidden" in script


def test_start_script_has_no_interactive_setup_prompt() -> None:
    script = (Path(__file__).parents[1] / "start.ps1").read_text(encoding="utf-8")
    assert "Read-Host" not in script
