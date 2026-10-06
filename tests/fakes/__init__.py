"""Yerel model katmanı için test altyapısı (K0, 2026-09-25).

- ``fake_llm_server``: sahte Ollama / OpenAI uyumlu / LM Studio HTTP sunucusu.
- ``fake_llama_cpp``: sahte ``llama_cpp`` modülü (llama-cpp-python yerine).
- ``model_files``: sahte GGUF başlığı ve MLX klasörü üreticileri.
- ``apply_mt_isolation``: tests/conftest.py'deki autouse ``_mt_isolation``
  fixture'ının gövdesi (ayrı fonksiyon: kendi testinde doğrudan sınanır).

Hiçbiri gerçek model yüklemez ve gerçek Ollama (11434) / LM Studio (1234)
portlarına gitmez. Yalnız test altyapısıdır; ürün kodu bu paketi içe aktarmaz.
"""

from __future__ import annotations

import errno
import importlib
import importlib.util
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, FrozenSet, Iterable, Optional

from .fake_llama_cpp import make_fake_llama_module
from .fake_llm_server import FakeLLMServer, FakeModel, FakeReply, FakeRequest
from .model_files import (
    read_gguf_kv,
    write_corrupt_gguf,
    write_fake_gguf,
    write_fake_gguf_split,
    write_fake_mlx_dir,
)

__all__ = [
    "REAL_LOCAL_MODEL_PORTS",
    "FakeLLMServer",
    "FakeModel",
    "FakeReply",
    "FakeRequest",
    "MtIsolation",
    "apply_mt_isolation",
    "closed_local_port",
    "make_fake_llama_module",
    "read_gguf_kv",
    "write_corrupt_gguf",
    "write_fake_gguf",
    "write_fake_gguf_split",
    "write_fake_mlx_dir",
]

# Gerçek yerel model sunucularının varsayılan portları (Ollama, LM Studio).
# Testte bu portlara loopback bağlantısı HER ZAMAN reddedilir.
REAL_LOCAL_MODEL_PORTS: FrozenSet[int] = frozenset({11434, 1234})

# Yalıtımdan önceki gerçek fonksiyon (yamalar üst üste binse de gerçeğe gidilir)
_REAL_CREATE_CONNECTION = socket.create_connection
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"})


def closed_local_port() -> int:
    """Bağlanılıp hemen kapatılan bir loopback portu (bağlantı anında reddedilir).

    Not: macOS'ta bağlı-ama-dinlemeyen soket reddetmez, zaman aşımına düşer;
    bu yüzden soket kapatılır. Başka bir sürecin portu kapma olasılığına karşı
    yalıtım bu portu da ``socket.create_connection`` düzeyinde reddeder.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _is_loopback_name(host: Any) -> bool:
    if not isinstance(host, str):
        return False
    h = host.strip().lower().strip("[]")
    return h in _LOOPBACK_NAMES or h.startswith("127.")


def _make_guarded_create_connection(blocked_ports: Iterable[int]) -> Callable:
    blocked = frozenset(int(p) for p in blocked_ports)

    def guarded_create_connection(address, *args, **kwargs):
        try:
            host, port = address[0], int(address[1])
        except (TypeError, ValueError, IndexError):
            return _REAL_CREATE_CONNECTION(address, *args, **kwargs)
        if port in blocked and _is_loopback_name(host):
            raise ConnectionRefusedError(
                errno.ECONNREFUSED,
                f"test yalıtımı: {host}:{port} gerçek yerel model sunucusu portu, kapalı sayılır")
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    guarded_create_connection.blocked_ports = blocked   # testler görebilsin
    return guarded_create_connection


@dataclass
class MtIsolation:
    """``_mt_isolation`` fixture'ının yamaladığı değerler (testler okuyabilir)."""
    root: Path                      # bu teste özel yalıtım klasörü
    models_dir: Path                # registry.MODELS_DIR (boş)
    user_specs_path: Path           # registry.USER_SPECS_PATH (yok)
    selection_path: Path            # ui.runner.engine_choice_path() (yok)
    closed_url: str                 # http://127.0.0.1:<kapalı port>
    sources: Any                    # contracts.SourcesConfig (kapalı porta)
    blocked_ports: FrozenSet[int]   # socket düzeyinde reddedilen loopback portları
    original_load_sources: Optional[Callable] = None
    original_engine_choice_path: Optional[Callable] = None
    patched: list = field(default_factory=list)   # yamalanan "modül.ad" listesi


def apply_mt_isolation(monkeypatch, root: Path, closed_port: int) -> MtIsolation:
    """Yerel model katmanını testte yalıt (sözleşme: core/mt_engines/contracts.py).

    ``root`` bu teste özel boş bir klasördür (çağıran oluşturur ve siler).
    """
    from core.mt_engines import registry
    from core.mt_engines.contracts import SourcesConfig

    root = Path(root)
    models_dir = root / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    closed_url = f"http://127.0.0.1:{closed_port}"
    sources = SourcesConfig(ollama_url=closed_url, lmstudio_url=closed_url + "/v1",
                            openai_servers=())
    blocked = frozenset(REAL_LOCAL_MODEL_PORTS | {closed_port})
    iso = MtIsolation(root=root, models_dir=models_dir,
                      user_specs_path=root / "mt_engines.json",
                      selection_path=root / "mt_selection.json",
                      closed_url=closed_url, sources=sources, blocked_ports=blocked)

    # (1) Ağ: gerçek Ollama / LM Studio portlarına ASLA gidilmez.
    monkeypatch.setattr(socket, "create_connection", _make_guarded_create_connection(blocked))
    iso.patched.append("socket.create_connection")

    # (2) Model kökü ve kullanıcı motor kayıtları: gerçek models/ ve config/ okunmaz.
    monkeypatch.setattr(registry, "MODELS_DIR", models_dir)
    monkeypatch.setattr(registry, "USER_SPECS_PATH", iso.user_specs_path)
    iso.patched += ["core.mt_engines.registry.MODELS_DIR",
                    "core.mt_engines.registry.USER_SPECS_PATH"]

    # (3) llama-cpp-python kurulu değilmiş gibi (makineden bağımsız sonuç);
    #     fake_llama fixture'ı sahte modülü koyar.
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    iso.patched.append("sys.modules['llama_cpp']")

    # (4) Kaynak yapılandırması (K2 discovery.py yazınca devreye girer).
    if importlib.util.find_spec("core.mt_engines.discovery") is not None:
        discovery = importlib.import_module("core.mt_engines.discovery")
        original = getattr(discovery, "load_sources", None)
        if original is not None:
            iso.original_load_sources = original

            def isolated_load_sources(path=None, *args, **kwargs):
                if path is not None:
                    return original(path, *args, **kwargs)
                return sources

            monkeypatch.setattr(discovery, "load_sources", isolated_load_sources)
            iso.patched.append("core.mt_engines.discovery.load_sources")

    # (5) Arayüzün kalıcı seçim dosyası (K3 ui.runner.engine_choice_path yazınca).
    runner = importlib.import_module("ui.runner")
    iso.original_engine_choice_path = getattr(runner, "engine_choice_path", None)
    selection_path = iso.selection_path
    monkeypatch.setattr(runner, "engine_choice_path", lambda: selection_path, raising=False)
    iso.patched.append("ui.runner.engine_choice_path")
    return iso
