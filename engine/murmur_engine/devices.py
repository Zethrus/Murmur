"""Hardware detection without creating a model or contacting a service."""
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Hardware:
    cuda_devices: int
    cpu: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect() -> Hardware:
    import ctranslate2
    try:
        return Hardware(cuda_devices=ctranslate2.get_cuda_device_count())
    except (RuntimeError, OSError):
        return Hardware(cuda_devices=0)
