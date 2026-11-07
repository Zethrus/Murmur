from pathlib import Path
from unittest.mock import Mock
import pytest
from murmur_engine import runtime


def test_environment_is_offline_and_skips_xet() -> None:
    environ: dict[str, str] = {"HF_HUB_OFFLINE": "0"}
    runtime.configure_environment(environ)
    assert environ == {"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "DO_NOT_TRACK": "1", "HF_HUB_DISABLE_XET": "1"}


def test_windows_registers_cuda_directory(tmp_path: Path) -> None:
    add = Mock(return_value="handle")
    environ = {runtime.CUDA_ENV: str(tmp_path), "PATH": r"C:\Windows\system32;C:\Windows"}
    found = runtime.add_cuda_directory(environ, "nt", add)
    assert found == tmp_path
    add.assert_called_once_with(str(tmp_path))
    assert "handle" in runtime._DLL_DIRECTORIES
    # Plain LoadLibrary (CTranslate2's cublas64_12.dll load) ignores add_dll_directory; PATH works.
    assert environ["PATH"] == str(tmp_path) + r";C:\Windows\system32;C:\Windows"


def test_cuda_directory_is_first_on_path_exactly_once(tmp_path: Path) -> None:
    environ = {runtime.CUDA_ENV: str(tmp_path), "PATH": rf"C:\Windows;{tmp_path}\;;C:\Tools"}
    runtime.add_cuda_directory(environ, "nt", Mock())
    runtime.add_cuda_directory(environ, "nt", Mock())
    assert environ["PATH"] == rf"{tmp_path};C:\Windows;C:\Tools"


def test_cuda_directory_on_path_without_existing_path(tmp_path: Path) -> None:
    environ = {runtime.CUDA_ENV: str(tmp_path)}
    runtime.add_cuda_directory(environ, "nt", Mock())
    assert environ["PATH"] == str(tmp_path)


def test_linux_leaves_cuda_to_loader_path(tmp_path: Path) -> None:
    add = Mock()
    environ = {runtime.CUDA_ENV: str(tmp_path), "PATH": "/usr/bin"}
    assert runtime.add_cuda_directory(environ, "posix", add) is None
    add.assert_not_called()
    assert environ["PATH"] == "/usr/bin"


@pytest.mark.parametrize("value", ["", "   ", "relative/cuda", "/nonexistent/murmur-cuda"])
def test_invalid_cuda_directory_is_ignored(value: str) -> None:
    add = Mock()
    environ = {runtime.CUDA_ENV: value, "PATH": r"C:\Windows"}
    assert runtime.add_cuda_directory(environ, "nt", add) is None
    add.assert_not_called()
    assert environ["PATH"] == r"C:\Windows"


def test_unset_cuda_directory_is_ignored() -> None:
    assert runtime.cuda_directory({}) is None


def test_loader_refusal_is_not_fatal_and_path_still_applies(tmp_path: Path) -> None:
    add = Mock(side_effect=OSError("refused"))
    environ = {runtime.CUDA_ENV: str(tmp_path), "PATH": r"C:\Windows"}
    assert runtime.add_cuda_directory(environ, "nt", add) == tmp_path
    assert environ["PATH"] == str(tmp_path) + r";C:\Windows"


def test_av_stub_only_when_pyav_is_absent() -> None:
    modules: dict[str, object] = {}
    assert runtime.install_av_stub(lambda name: object(), modules) is False
    assert modules == {}
    assert runtime.install_av_stub(lambda name: None, modules) is True
    with pytest.raises(AttributeError, match="PyAV is not bundled"):
        modules["av"].audio  # type: ignore[attr-defined]
    # A second call keeps the first registration.
    assert runtime.install_av_stub(lambda name: None, modules) is False
