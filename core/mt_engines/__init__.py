"""MT motor katmanı — tak-çıkar çeviri modeli desteği.

Dışa açık API::

    from core.mt_engines import available_engines, create_engine, EngineSpec

Kulvarlar arası sözleşme (veri yapıları, hatalar, Türkçe ileti kataloğu)
``core.mt_engines.contracts``'tadır ve buradan da dışa aktarılır::

    from core.mt_engines import BackendUnavailable, DiscoveryReport, MSG_OLLAMA_DOWN

Arka uçlar (``backends``, ``http_backends``, ``llamacpp_backend``), keşif
(``discovery``) ve HTTP yardımcıları (``http_util``) paket düzeyinde dışa
aktarılmaz; doğrudan alt modülden içe aktarılır (döngüsel içe aktarma ve ağır
bağımlılık riski olmasın).

Yeni model ekleme rehberi: docs/YENI_MODEL_EKLEME.md
"""

from . import contracts
from .base import DEFAULT_LANG_NAMES, EngineSpec, MTEngine
from .chat import ChatTemplateEngine
from .contracts import *  # noqa: F401,F403 — sözleşme (contracts.__all__)
from .hymt import HYMT_SPECIAL_TOKENS, HYMTFormatEngine
from .registry import (
    BUILTIN_SPECS,
    ENGINE_FORMATS,
    MODELS_DIR,
    USER_SPECS_PATH,
    available_engines,
    create_engine,
    load_user_specs,
)

__all__ = [
    "DEFAULT_LANG_NAMES",
    "EngineSpec",
    "MTEngine",
    "ChatTemplateEngine",
    "HYMTFormatEngine",
    "HYMT_SPECIAL_TOKENS",
    "BUILTIN_SPECS",
    "ENGINE_FORMATS",
    "MODELS_DIR",
    "USER_SPECS_PATH",
    "available_engines",
    "create_engine",
    "load_user_specs",
] + list(contracts.__all__)
