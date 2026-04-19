import importlib

import xobop.router as router


def test_direct_shell_prefixes_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("XOBOP_DIRECT_SHELL_PREFIXES", "echo,uname")
    importlib.reload(router)
    try:
        assert router.classify("echo hi")["name"] == "direct_shell"
        assert router.classify("uname -a")["name"] == "direct_shell"
        assert router.classify("python3 --version")["name"] != "direct_shell"
    finally:
        monkeypatch.delenv("XOBOP_DIRECT_SHELL_PREFIXES", raising=False)
        importlib.reload(router)
