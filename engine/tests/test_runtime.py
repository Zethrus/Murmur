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
    found = runtime.add_cuda_directory({runtime.CUDA_ENV: str(tmp_path)}, "nt", add)
    assert found == tmp_path
    add.assert_called_once_with(str(tmp_path))
    assert "handle" in runtime._DLL_DIRECTORIES


def test_linux_leaves_cuda_to_loader_path(tmp_path: Path) -> None:
    add = Mock()
    assert runtime.add_cuda_directory({runtime.CUDA_ENV: str(tmp_path)}, "posix", add) is None
    add.assert_not_called()


@pytest.mark.parametrize("value", ["", "   ", "relative/cuda", "/nonexistent/murmur-cuda"])
def test_invalid_cuda_directory_is_ignored(value: str) -> None:
    add = Mock()
    assert runtime.add_cuda_directory({runtime.CUDA_ENV: value}, "nt", add) is None
    add.assert_not_called()


def test_unset_cuda_directory_is_ignored() -> None:
    assert runtime.cuda_directory({}) is None


def test_loader_refusal_is_not_fatal(tmp_path: Path) -> None:
    add = Mock(side_effect=OSError("refused"))
    assert runtime.add_cuda_directory({runtime.CUDA_ENV: str(tmp_path)}, "nt", add) is None


def test_av_stub_only_when_pyav_is_absent() -> None:
    modules: dict[str, object] = {}
    assert runtime.install_av_stub(lambda name: object(), modules) is False
    assert modules == {}
    assert runtime.install_av_stub(lambda name: None, modules) is True
    with pytest.raises(AttributeError, match="PyAV is not bundled"):
        modules["av"].audio  # type: ignore[attr-defined]
    # A second call keeps the first registration.
    assert runtime.install_av_stub(lambda name: None, modules) is False
