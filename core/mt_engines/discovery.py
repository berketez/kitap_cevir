"""Yerel çeviri modeli keşfi ve ön kontrol (TASARIM D; KARARLAR 1-2).

Ne tarar (yalnız bu bilgisayar):

- **Dosyalar:** YALNIZ projenin ``models/`` klasörü (``registry.MODELS_DIR``,
  çağrı anında okunur): ``config.json`` içeren MLX klasörleri ve ``*.gguf``
  dosyaları, derinlik ≤ ``DISCOVERY_MAX_DEPTH``. Başka yerdeki bir model
  ``models/`` altına sembolik bağla konur; bağ izlenir, gerçek yol ile
  tekilleştirilir (``hymt-7b-mlx`` -> ``hymt-7b-mlx.nosync`` tek kayıt).
  ``~/.lmstudio/models`` ve Hugging Face önbelleği TARANMAZ;
  ``config/mt_sources.json`` model klasörü ekleyemez.
- **Sunucular:** Ollama (``/api/tags`` + ``/api/show`` + ``/api/ps``),
  LM Studio (``/v1/models`` + ``/api/v1/models`` ya da ``/api/v0/models``)
  ve ``mt_sources.json``'daki OpenAI uyumlu sunucular (``/models``). Adresler
  yalnız loopback (``http_util.check_local_url``). Sunucu kapalıysa o
  sunucunun modeli LİSTELENMEZ (sahte liste yok); yalnız durum satırı döner.
  Durum satırı bağlantı reddini ("kapalı … başlatın"), zaman aşımını ve HTTP
  hatasını ("açık ama model listesini vermedi") ayırır (2026-09-29, F5-10).
  Ollama bulut modelleri (``remote_host`` / ``remote_model``, ad ``-cloud`` /
  ``:cloud``) yerel sunucudan listelense de kullanılamaz: istek bu
  bilgisayarın dışına gider (F4-01; KARARLAR 2).

Hiçbir model YÜKLENMEZ: GGUF'ta yalnız başlık (üst veri) okunur, MLX'te
``config.json`` / tokenizer dosyaları, sunucuda liste uçları. ``check_engine``
tek bir kimliği aynı kurallarla denetler (CLI ön kontrolü ve arayüzün
"Çevir" öncesi son kontrolü).

Ekranda gösterilen her sayı ve ayrıntı (boyut, niceleme, mimari, "N model")
gerçek dosya sistemi ya da sunucu yanıtından gelir; bilinmiyorsa boş kalır.

Donanım kapısı (KARARLAR 17, 17a; ``core.hardware``): her model için
tahmini gereken bellek hesaplanır (ağırlık = keşfin ölçtüğü boyut, KV şekli
modelin kendi üst verisinden: MLX ``config.json``, GGUF başlığı, Ollama
``/api/show`` ``model_info``). Toplu iş 1 ile bile sığmayan model "bu Mac'e
sığmaz" nedeniyle kullanılamaz; bu Mac referans 4 bit 7B modeli
çalıştıramıyorsa (genel kapı) hiçbir model kullanılamaz. Tahmin yapılabilen
modelin kaydı ``MemoryAssessedModel``'dir (JSON'da ek ``memory`` alanı);
``check_engine`` aynı hükmü verir.

MT-12 (2026-09-30): LM Studio ve OpenAI uyumlu sunucu KV şeklini bildirmez
(tahmin ağırlıkla orantılı yedek). Bu modellerde "sığmaz" ENGEL değil
gerekçeli uyarıdır (kayıt kullanılabilir kalır, ``memory.verdict`` "uyarı",
``check_engine`` ``ok`` + ``warning``); sunucunun şu an yüklü tuttuğu modele
"sığmaz" hükmü verilmez (``memory.verdict`` "yüklü"). Kural
``hardware.server_verdict``'te; MLX / GGUF / Ollama hükmü değişmez.
``~/.lmstudio`` bu yüzden de TARANMAZ (keşif kapsamı kararı).

Komut satırı::

    python -m core.mt_engines.discovery           # Türkçe tablo
    python -m core.mt_engines.discovery --json    # makine okunur (TASARIM D.4)
"""

from __future__ import annotations

import argparse
import ast
import functools
import importlib.util
import json
import logging
import os
import re
import shutil
import struct
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from config.defaults import (
    DISCOVERY_HTTP_TIMEOUT_S,
    DISCOVERY_MAX_DEPTH,
    DISCOVERY_MAX_ENTRIES,
    DISCOVERY_MAX_SHOW_REQUESTS,
    ENGINE_CHECK_HTTP_TIMEOUT_S,
    GGUF_MAX_KV_COUNT,
    GGUF_MAX_STRING_BYTES,
    LMSTUDIO_DEFAULT_URL,
    MT_CONTEXT_TOKENS,
    OLLAMA_DEFAULT_URL,
)
from config.paths import ayar_koku

from core import hardware

from . import http_util, registry
from .thinking import (
    ThinkingProfile,
    lmstudio_profile,
    ollama_profile,
    qwen3_soft_switch,
    template_profile,
    template_text,
)
from .base import EngineSpec
from .contracts import (
    BACKEND_LABELS,
    BACKEND_LLAMACPP,
    BACKEND_MLX,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    FLAVOR_LMSTUDIO,
    FORMAT_CHAT,
    FORMAT_HYMT,
    GGUF_SUFFIX,
    MSG_BAD_ID,
    MSG_BUILTIN_MISSING,
    MSG_EMBEDDING_MODEL,
    MSG_GGUF_INVALID,
    MSG_GGUF_MISSING,
    MSG_LLAMA_MISSING,
    MSG_LMSTUDIO_DOWN,
    MSG_LMSTUDIO_NOT_LOADED,
    MSG_MLX_ARCH_UNSUPPORTED,
    MSG_MLX_INCOMPLETE,
    MSG_MLX_MISSING,
    MSG_MODEL_NOT_ON_SERVER,
    MSG_NOT_OPENAI,
    MSG_OLLAMA_DOWN,
    MSG_OLLAMA_INSTALLED_DOWN,
    MSG_OPENAI_DOWN,
    MSG_OLLAMA_CLOUD_MODEL,
    MSG_SERVER_NO_LIST,
    MSG_SOURCE_COUNT_DIRS,
    MSG_SOURCE_COUNT_DIRS_ENGINES,
    MSG_SOURCE_COUNT_FILES,
    MSG_SOURCE_COUNT_FILES_ENGINES,
    MSG_SOURCE_COUNT_MODELS,
    MSG_SOURCE_COUNT_TRUNCATED,
    MT_SOURCES_FILENAME,
    PREFIX_GGUF,
    PREFIX_LMSTUDIO,
    PREFIX_MLX,
    PREFIX_OLLAMA,
    PREFIX_OPENAI,
    SOURCE_BUILTIN,
    SOURCE_GGUF,
    SOURCE_LMSTUDIO,
    SOURCE_MLX,
    SOURCE_OLLAMA,
    SOURCE_OPENAI,
    SOURCE_STATUS_LABELS,
    SOURCE_USER,
    SPEC_SOURCE_BUILTIN,
    SPEC_SOURCE_USER,
    BackendUnavailable,
    DiscoveredModel,
    DiscoveryReport,
    EngineCheck,
    OpenAIServer,
    SourcesConfig,
    SourceStatus,
    THINKING_ALWAYS,
    THINKING_NONE,
    THINKING_UNKNOWN,
    build_engine_id,
    guess_format,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SOURCES_PATH",
    "GgufMeta",
    "MemoryAssessedModel",
    "MlxInfo",
    "WarnedEngineCheck",
    "allowed_roots",
    "check_engine",
    "discover_all",
    "engine_label",
    "hardware_check",
    "inspect_mlx_dir",
    "llama_cpp_available",
    "load_sources",
    "local_memory_fit",
    "main",
    "mlx_arch_supported",
    "read_gguf_meta",
]

# config/mt_sources.json (isteğe bağlı; yoksa varsayılan adresler). Veri
# kökünün config/'undadır (TASARIM F1; paketli kipte <kök>/veri/config).
SOURCES_PATH = ayar_koku(registry.PROJECT_ROOT) / MT_SOURCES_FILENAME

# MLX klasörü: tokenizer olarak bunlardan biri yeter (mlx_lm.load gereksinimi)
MLX_TOKENIZER_FILES = ("tokenizer.json", "tokenizer.model", "tokenizer_config.json")
MLX_CONFIG_FILE = "config.json"
MLX_WEIGHTS_GLOB = "*.safetensors"
# Sohbet şablonunda düşünme belirtisi (düşünen model)
_THINK_MARKERS = ("<think>", "enable_thinking")
_CHAT_TEMPLATE_FILES = ("chat_template.jinja", "chat_template.json")
_NOSYNC_SUFFIX = ".nosync"
_OLLAMA_APP = Path("/Applications/Ollama.app")
_OLLAMA_LATEST = ":latest"

# gguf-split parça adı: <kök>-00001-of-00003.gguf
_GGUF_SPLIT_RE = re.compile(r"^(?P<stem>.+)-(?P<no>\d{5})-of-(?P<count>\d{5})\.gguf$",
                            re.IGNORECASE)
# Dosya adındaki niceleme etiketi (Q4_K_M, Q8_0, IQ4_XS, MXFP4, F16, BF16 ...)
_QUANT_RE = re.compile(r"(?:^|(?<=[-._ ]))(I?Q[1-8](?:_[0-9A-Z]{1,3})*|MXFP4|BF16|F16|F32|"
                       r"TQ[12]_0)(?=$|[-._ ])", re.IGNORECASE)

_FETCH_DOWN = "down"          # bağlantı kurulamadı (red) / adres reddi
_FETCH_TIMEOUT = "timeout"    # sunucu süre içinde yanıt vermedi (açık ama meşgul olabilir)
_FETCH_INVALID = "invalid"    # sunucu yanıt verdi ama beklenen JSON değil
# JSON ayar ve model dosyaları: BOM'lu UTF-8 de okunur (F4-14)
_JSON_ENCODING = "utf-8-sig"


# ---------------------------------------------------------------------------
# config/mt_sources.json
# ---------------------------------------------------------------------------
def _server_url(raw: Any, *, key: str, openai: bool) -> Optional[str]:
    """Adres alanını doğrula; geçersizse uyarı + None."""
    if not isinstance(raw, str) or not raw.strip():
        logger.warning("mt_sources.json: '%s' adresi metin olmalı, varsayılan kullanılıyor", key)
        return None
    try:
        return registry.openai_base_url(raw) if openai else http_util.check_local_url(raw)
    except BackendUnavailable as exc:
        logger.warning("mt_sources.json: '%s' adresi reddedildi: %s", key, exc)
        return None


def _parse_server_entry(value: Any, key: str, *, openai: bool,
                        default_url: str) -> Tuple[str, bool]:
    if not isinstance(value, dict):
        logger.warning("mt_sources.json: '%s' sözlük olmalı, varsayılanlar kullanılıyor", key)
        return default_url, True
    url, enabled = default_url, True
    for sub, sub_value in value.items():
        if isinstance(sub, str) and sub.startswith("_"):
            continue
        if sub == "url":
            url = _server_url(sub_value, key=key, openai=openai) or default_url
        elif sub == "enabled":
            if isinstance(sub_value, bool):
                enabled = sub_value
            else:
                logger.warning("mt_sources.json: '%s.enabled' true/false olmalı, yok sayıldı",
                               key)
        else:
            logger.warning("mt_sources.json: '%s' içinde bilinmeyen alan '%s', yok sayıldı",
                           key, sub)
    return url, enabled


def _parse_openai_servers(value: Any) -> Tuple[OpenAIServer, ...]:
    if not isinstance(value, list):
        logger.warning("mt_sources.json: 'openai_servers' liste olmalı, yok sayıldı")
        return ()
    servers: List[OpenAIServer] = []
    seen: Set[str] = set()
    for index, item in enumerate(value):
        key = f"openai_servers[{index}]"
        if not isinstance(item, dict):
            logger.warning("mt_sources.json: '%s' sözlük olmalı, atlandı", key)
            continue
        url = _server_url(item.get("url"), key=key, openai=True)
        if url is None or url in seen:
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            name = _host_port(url)
        for sub in item:
            if sub not in ("url", "name") and not str(sub).startswith("_"):
                logger.warning("mt_sources.json: '%s' içinde bilinmeyen alan '%s', yok sayıldı",
                               key, sub)
        seen.add(url)
        servers.append(OpenAIServer(name=name.strip(), url=url))
    return tuple(servers)


def load_sources(path: Optional[Path] = None) -> SourcesConfig:
    """``config/mt_sources.json``'u oku (TASARIM C.5; KARARLAR 1-2). Fail-open.

    Dosya yoksa varsayılan adresler. Bozuk dosya / geçersiz ya da uzak adres
    -> uyarı + o alan için varsayılan. ``model_dirs`` desteklenmez (tarama
    kökü yalnız ``models/``). Çağıranlar bunu ``discovery.load_sources(...)``
    biçiminde, modül özniteliği olarak çağırır (test yalıtımı yamalar).
    """
    path = Path(path) if path is not None else SOURCES_PATH
    if not path.exists():
        return SourcesConfig()
    try:
        # utf-8-sig: editörler dosyayı BOM'lu kaydedebiliyor (F4-14)
        data = json.loads(path.read_text(encoding=_JSON_ENCODING))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("mt_sources.json okunamadı (%s) — varsayılan adresler kullanılıyor", exc)
        return SourcesConfig()
    if not isinstance(data, dict):
        logger.warning("mt_sources.json kök öğesi sözlük olmalı — varsayılan adresler "
                       "kullanılıyor")
        return SourcesConfig()

    ollama_url, ollama_enabled = OLLAMA_DEFAULT_URL, True
    lmstudio_url, lmstudio_enabled = LMSTUDIO_DEFAULT_URL, True
    servers: Tuple[OpenAIServer, ...] = ()
    for key, value in data.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key == "ollama":
            ollama_url, ollama_enabled = _parse_server_entry(
                value, key, openai=False, default_url=OLLAMA_DEFAULT_URL)
        elif key == "lmstudio":
            lmstudio_url, lmstudio_enabled = _parse_server_entry(
                value, key, openai=True, default_url=LMSTUDIO_DEFAULT_URL)
        elif key == "openai_servers":
            servers = _parse_openai_servers(value)
        elif key == "model_dirs":
            logger.warning(
                "mt_sources.json: 'model_dirs' desteklenmiyor, yok sayıldı — yalnız projenin "
                "models/ klasörü taranır; başka yerdeki bir modeli models/ altına sembolik "
                "bağla koyun")
        else:
            logger.warning("mt_sources.json: bilinmeyen alan '%s', yok sayıldı", key)
    return SourcesConfig(ollama_url=ollama_url, ollama_enabled=ollama_enabled,
                         lmstudio_url=lmstudio_url, lmstudio_enabled=lmstudio_enabled,
                         openai_servers=servers)


def allowed_roots(sources: Optional[SourcesConfig] = None) -> List[Path]:
    """İzinli model kökleri: YALNIZ ``models/`` (sözcüksel yol + gerçek yolu).

    ``sources`` yalnız imza uyumu için alınır: KARARLAR 1 gereği
    ``mt_sources.json`` kök ekleyemez. ``registry`` mutlak yol iznini buradan
    alır (tek kaynak).
    """
    root = Path(os.path.abspath(str(registry.MODELS_DIR)))
    real = Path(os.path.realpath(str(root)))
    return [root] if real == root else [root, real]


# ---------------------------------------------------------------------------
# Küçük yardımcılar
# ---------------------------------------------------------------------------
def _host_port(url: str) -> str:
    from urllib.parse import urlsplit
    return urlsplit(url).netloc or url


def llama_cpp_available() -> bool:
    """llama-cpp-python bulunuyor mu (İÇE AKTARMADAN; ``find_spec``)."""
    try:
        return importlib.util.find_spec("llama_cpp") is not None
    except (ImportError, ValueError):
        return sys.modules.get("llama_cpp") is not None


def _ollama_installed() -> bool:
    return bool(shutil.which("ollama")) or _OLLAMA_APP.exists()


def _ollama_down_message(url: str) -> str:
    template = MSG_OLLAMA_INSTALLED_DOWN if _ollama_installed() else MSG_OLLAMA_DOWN
    return template.format(url=url)


def _server_kind(spec: EngineSpec) -> str:
    if spec.backend == BACKEND_OLLAMA:
        return SOURCE_OLLAMA
    return SOURCE_LMSTUDIO if spec.flavor == FLAVOR_LMSTUDIO else SOURCE_OPENAI


def _openai_server_name(url: str, sources: Optional[SourcesConfig]) -> str:
    for server in (sources.openai_servers if sources else ()):
        if server.url == url:
            return f"{server.name} ({_host_port(url)})"
    return f"{BACKEND_LABELS[BACKEND_OPENAI]} ({_host_port(url)})"


def _strip_nosync(name: str) -> str:
    return name[:-len(_NOSYNC_SUFFIX)] if name.endswith(_NOSYNC_SUFFIX) else name


def _base_label(spec: EngineSpec) -> str:
    """Kimliğin sade adı: kayıtlarda açıklamanın ilk parçası, önekli kimlikte model/dosya."""
    if spec.source in (SPEC_SOURCE_BUILTIN, SPEC_SOURCE_USER):
        return (spec.description or spec.name).split(" — ")[0].strip() or spec.name
    if spec.backend == BACKEND_MLX:
        return _strip_nosync(Path(spec.path).name if spec.path else spec.local_dir)
    if spec.backend == BACKEND_LLAMACPP:
        return Path(spec.path).name
    return spec.model


def engine_label(spec: EngineSpec) -> str:
    """Kimliğin ekrandaki / günlükteki adı: "qwen3:8b (Ollama)", "HY-MT1.5 7B (MLX)"."""
    base = _base_label(spec)
    if spec.backend == BACKEND_OPENAI and spec.flavor == FLAVOR_LMSTUDIO:
        kind = SOURCE_STATUS_LABELS[SOURCE_LMSTUDIO]
    elif spec.backend == BACKEND_OPENAI:
        kind = f"{BACKEND_LABELS[BACKEND_OPENAI]}, {_host_port(spec.base_url)}"
    else:
        kind = BACKEND_LABELS.get(spec.backend, spec.backend)
    return f"{base} ({kind})"


def _fetch_detail(url: str, timeout: float,
                  body: Optional[dict] = None) -> Tuple[Any, Optional[str], str]:
    """(json, None, "") ya da (None, _FETCH_DOWN | _FETCH_TIMEOUT | _FETCH_INVALID, ayrıntı).

    Ağ hatası fırlatmaz. Zaman aşımı ayrı sınıftır (F5-10): açık ama yavaş
    sunucu "kapalı" gösterilmez. Ayrıntı Türkçe, kısa ("1,5 sn içinde yanıt
    yok", "HTTP 500").
    """
    try:
        if body is None:
            return http_util.get_json(url, timeout), None, ""
        return http_util.post_json(url, body, timeout), None, ""
    except http_util.HttpTimeout:
        return None, _FETCH_TIMEOUT, f"{_seconds_text(timeout)} sn içinde yanıt yok"
    except (http_util.HttpUnreachable, BackendUnavailable):
        return None, _FETCH_DOWN, ""
    except http_util.HttpInvalidJson:
        return None, _FETCH_INVALID, "yanıt JSON değil"
    except http_util.HttpStatusError as exc:
        return None, _FETCH_INVALID, f"HTTP {exc.status}"


def _fetch(url: str, timeout: float, body: Optional[dict] = None) -> Tuple[Any, Optional[str]]:
    """(json, None) ya da (None, _FETCH_DOWN | _FETCH_TIMEOUT | _FETCH_INVALID). Ağ hatası fırlatmaz."""
    data, err, _detail = _fetch_detail(url, timeout, body)
    return data, err


def _ollama_show(base_url: str, name: str, timeout: float) -> Tuple[Any, Optional[str]]:
    """Ollama ``/api/show`` (yalnız üst veri; model yüklenmez).

    2026-09-30 (MT-11): kısa yanıtta katman başına diziler (gemma4
    ``head_count_kv``, ``sliding_window_pattern``) null gelir; o zaman bir kez
    ``verbose=true`` ile istenir ve yalnız ``model_info``'su alınır. Ayrıntılı
    yanıt sözlük dizileri yüzünden büyük (gemma4:31b ~12 MB), bu yüzden
    yalnız gerektiğinde; tokenizer anahtarları atılır.
    """
    url = http_util.url_join(base_url, "/api/show")
    show, err = _fetch(url, timeout, {"model": name})
    info = show.get("model_info") if isinstance(show, dict) else None
    if not hardware.gguf_needs_per_layer(info):
        return show, err
    full, _ = _fetch(url, timeout, {"model": name, "verbose": True})
    full_info = full.get("model_info") if isinstance(full, dict) else None
    if isinstance(full_info, dict):
        show = dict(show)
        show["model_info"] = {k: v for k, v in full_info.items()
                              if not str(k).startswith("tokenizer.")}
    return show, err


def _seconds_text(seconds: float) -> str:
    """1.5 -> "1,5"; 2.0 -> "2" (Türkçe ondalık)."""
    return f"{float(seconds):g}".replace(".", ",")


def _no_list_message(server: str, url: str, err: Optional[str], detail: str,
                     down: str) -> Tuple[bool, str]:
    """Liste alınamadı: (erişilebilir mi, Türkçe ileti) (F5-10).

    Bağlantı reddi -> ``down`` (sunucuyu başlatma önerisiyle); zaman aşımı ya
    da HTTP hatası / beklenmeyen biçim -> ``MSG_SERVER_NO_LIST`` (sunucu açık,
    "başlatın" denmez). Zaman aşımında ``reachable`` False (bu sürede
    erişilemedi), yanıt veren sunucuda True.
    """
    if err == _FETCH_DOWN:
        return False, down
    why = detail or "yanıt beklenen biçimde değil"
    return err != _FETCH_TIMEOUT, MSG_SERVER_NO_LIST.format(server=server, url=url, why=why)


def _openai_ids(data: Any) -> Optional[List[str]]:
    """OpenAI ``/models`` yanıtındaki kimlikler; biçim uymazsa None."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return None
    return [item["id"] for item in data["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]]


def _ollama_names(data: Any) -> Optional[List[dict]]:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    out = []
    for item in data["models"]:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        if isinstance(name, str) and name:
            out.append(item)
    return out


def _has_ollama_tag(name: str) -> bool:
    """Adın son bileşeninde etiket (``:8b``) var mı (``hf.co/org/repo`` yolu hariç)."""
    return ":" in name.rsplit("/", 1)[-1]


def _ollama_match(wanted: str, names: Sequence[str]) -> Optional[str]:
    """Ollama ad eşleşmesi: ``qwen3`` ≡ ``qwen3:latest`` (iki yönde).

    Yalnız etiketsiz ad ile ``:latest`` eşdeğerdir; ``gemma3:4b:latest`` gibi
    iki etiketli bir ad ``gemma3:4b``'ye eşlenmez (Ollama onu tanımaz).
    """
    if wanted in names:
        return wanted
    if not _has_ollama_tag(wanted) and wanted + _OLLAMA_LATEST in names:
        return wanted + _OLLAMA_LATEST
    if wanted.endswith(_OLLAMA_LATEST):
        base = wanted[:-len(_OLLAMA_LATEST)]
        if base and not _has_ollama_tag(base) and base in names:
            return base
    return None


def _join_detail(*parts: Any) -> str:
    return " · ".join(str(p).strip() for p in parts if isinstance(p, (str, int, float))
                      and str(p).strip())


_HYMT_THINKING = ThinkingProfile(THINKING_NONE, basis="HY-MT biçimi düşünmez")
_OPENAI_THINKING = ThinkingProfile(THINKING_UNKNOWN,
                                   basis="OpenAI uyumlu sunucu düşünme bilgisi vermez")


def _thinking_fields(profile: Optional[ThinkingProfile], fmt: str = "") -> Dict[str, Any]:
    """``DiscoveredModel`` düşünme alanları (G2b-1). HY-MT biçimi her zaman "yok";
    profil yoksa (incelenemedi) alanlar None kalır."""
    if fmt == FORMAT_HYMT:
        profile = _HYMT_THINKING
    if profile is None:
        return {}
    return {"thinking": profile.cls, "thinking_off": profile.off_path}


def _template_has_think(template: Any) -> bool:
    if isinstance(template, str):
        return any(marker in template for marker in _THINK_MARKERS)
    if isinstance(template, list):   # [{"name": ..., "template": ...}]
        return any(_template_has_think(item.get("template"))
                   for item in template if isinstance(item, dict))
    return False


# ---------------------------------------------------------------------------
# MLX
# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _mlx_lm_layout() -> Optional[Tuple[str, Tuple[Tuple[str, str], ...]]]:
    """(mlx_lm paket dizini, MODEL_REMAPPING) — içe aktarmadan (ast ile)."""
    try:
        spec = importlib.util.find_spec("mlx_lm")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    pkg = Path(list(spec.submodule_search_locations)[0])
    remap: Dict[str, str] = {}
    try:
        tree = ast.parse((pkg / "utils.py").read_text(encoding="utf-8"))
        for node in tree.body:
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            if any(isinstance(t, ast.Name) and t.id == "MODEL_REMAPPING" for t in targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, dict):
                    remap = {str(k): str(v) for k, v in value.items()}
                break
    except (OSError, SyntaxError, ValueError):
        pass
    return str(pkg), tuple(sorted(remap.items()))


def mlx_arch_supported(model_type: str) -> Optional[bool]:
    """Kurulu mlx-lm bu mimariyi tanıyor mu? mlx-lm yoksa None (bilinmiyor).

    mlx_lm.utils._get_classes ile aynı kural: ``MODEL_REMAPPING`` +
    ``mlx_lm/models/<tip>.py`` varlığı. İçe aktarma YOK (ms düzeyinde).
    """
    layout = _mlx_lm_layout()
    if layout is None:
        return None
    if not model_type or not re.match(r"^[A-Za-z0-9_]+$", model_type):
        return False
    pkg, remap = layout
    target = dict(remap).get(model_type, model_type)
    models = Path(pkg) / "models"
    return (models / f"{target}.py").is_file() or (models / target / "__init__.py").is_file()


@dataclass(frozen=True)
class MlxInfo:
    """Bir MLX klasörünün salt okunur incelemesi."""
    usable: bool
    reason: str = ""
    model_type: str = ""
    detail: str = ""
    size_bytes: Optional[int] = None
    reasoning: Optional[bool] = None
    kv: Optional[hardware.KvShape] = None   # config.json'dan KV şekli (donanım kapısı)
    thinking: Optional[ThinkingProfile] = None   # sohbet şablonundan (G2b-1)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding=_JSON_ENCODING))


def _mlx_reasoning(path: Path) -> Optional[bool]:
    try:
        tcfg = _read_json(path / "tokenizer_config.json")
        if isinstance(tcfg, dict) and _template_has_think(tcfg.get("chat_template")):
            return True
    except (OSError, ValueError):
        pass
    for name in _CHAT_TEMPLATE_FILES:
        try:
            text = (path / name).read_text(encoding="utf-8")
        except OSError:
            continue
        if any(marker in text for marker in _THINK_MARKERS):
            return True
    return None


def _mlx_template(path: Path) -> Optional[str]:
    """MLX klasörünün sohbet şablonu metni (tokenizer_config.json ``chat_template``,
    yoksa chat_template.jinja / chat_template.json); bulunamazsa None (G2b-1)."""
    try:
        tcfg = _read_json(path / "tokenizer_config.json")
        text = template_text(tcfg.get("chat_template")) if isinstance(tcfg, dict) else None
        if text:
            return text
    except (OSError, ValueError):
        pass
    for name in _CHAT_TEMPLATE_FILES:
        try:
            raw = (path / name).read_text(encoding=_JSON_ENCODING)
        except OSError:
            continue
        if name.endswith(".json"):
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            text = template_text(data.get("chat_template")) if isinstance(data, dict) else None
            if text:
                return text
        if raw.strip():
            return raw
    return None


def _mlx_thinking(path: Path) -> ThinkingProfile:
    """MLX klasörünün düşünme sınıfı: çevirmen ``enable_thinking=False``'u
    ``apply_chat_template``'e geçirir (şablon anahtarı varsa "kapatilabilir")."""
    return template_profile(_mlx_template(path), backend=BACKEND_MLX,
                            soft_switch=qwen3_soft_switch([_strip_nosync(path.name)]))


def inspect_mlx_dir(path: Path) -> MlxInfo:
    """MLX klasörünü yüklemeden denetle: config.json, *.safetensors, tokenizer, mimari."""
    path = Path(path)
    missing: List[str] = []
    config: Any = None
    if not (path / MLX_CONFIG_FILE).is_file():
        missing.append(MLX_CONFIG_FILE)
    else:
        try:
            config = _read_json(path / MLX_CONFIG_FILE)
        except (OSError, ValueError):
            missing.append(f"okunabilir {MLX_CONFIG_FILE}")
    weights = sorted(path.glob(MLX_WEIGHTS_GLOB))
    if not weights:
        missing.append(MLX_WEIGHTS_GLOB)
    if not any((path / name).is_file() for name in MLX_TOKENIZER_FILES):
        missing.append("tokenizer")
    size: Optional[int] = None
    if weights:
        try:
            size = sum(w.stat().st_size for w in weights)
        except OSError:
            size = None
    model_type = ""
    detail = ""
    kv = hardware.kv_shape_from_hf_config(config)
    if config is not None and not isinstance(config, dict):
        # JSON ama nesne değil (liste, sayı): mimari okunamaz — "mlx-lm bu
        # mimariyi tanımıyor ()" yanıltıcıydı (F4-14).
        missing.append(f"okunabilir {MLX_CONFIG_FILE}")
    if isinstance(config, dict):
        model_type = str(config.get("model_type") or "")
        quant = config.get("quantization") or config.get("quantization_config")
        quant_note = ""
        if isinstance(quant, dict):
            bits = quant.get("bits")
            quant_note = f"{bits} bit nicemlenmiş" if isinstance(bits, int) else "nicemlenmiş"
        detail = _join_detail(f"mimari: {model_type}" if model_type else "", quant_note)
        if not model_type:
            missing.append(f"{MLX_CONFIG_FILE} model_type")
    reasoning = _mlx_reasoning(path)
    thinking = _mlx_thinking(path)
    if missing:
        return MlxInfo(False, MSG_MLX_INCOMPLETE.format(missing=", ".join(missing), path=path),
                       model_type, detail, size, reasoning, kv, thinking)
    if mlx_arch_supported(model_type) is False:
        return MlxInfo(False, MSG_MLX_ARCH_UNSUPPORTED.format(arch=model_type),
                       model_type, detail, size, reasoning, kv, thinking)
    return MlxInfo(True, "", model_type, detail, size, reasoning, kv, thinking)


def _mlx_dir_for(spec: EngineSpec, root: Path) -> Optional[Path]:
    """MLX spec'inin klasörü: ``path`` (doğrulanmış mutlak) ya da ``models/<local_dir>``."""
    if spec.path:
        p = Path(spec.path)
        return p if p.is_dir() else None
    engine = registry.ENGINE_FORMATS[spec.format](spec)
    return engine.resolve_local_model_dir(root)


def _mlx_missing(spec: EngineSpec, root: Path) -> Tuple[str, str]:
    """(neden, ipucu) — MLX klasörü bulunamadı."""
    engine = registry.ENGINE_FORMATS[spec.format](spec)
    expected = Path(spec.path) if spec.path else root / (spec.local_dir or f"{spec.name}-mlx")
    hint = engine.convert_hint(root) if (spec.model_id and not spec.path) else ""
    if spec.source == SPEC_SOURCE_BUILTIN:
        return MSG_BUILTIN_MISSING.format(path=expected), engine.convert_hint(root)
    return MSG_MLX_MISSING.format(path=expected), hint


# ---------------------------------------------------------------------------
# GGUF
# ---------------------------------------------------------------------------
_GGUF_MAGIC = b"GGUF"
_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?",
                 10: "<Q", 11: "<q", 12: "<d"}
_GGUF_STRING, _GGUF_ARRAY = 8, 9
_GGUF_SUPPORTED_VERSIONS = (2, 3)
_GGUF_KEYS = {"general.architecture", "general.type", "general.name", "general.size_label",
              "tokenizer.chat_template", "split.count", "split.no"}
# Donanım kapısı: KV şekli için mimari önekli sayısal anahtarlar (dizi değerler
# okunmaz; o zaman şekil yedek orana düşer).
_GGUF_KV_SUFFIXES = (".block_count", ".attention.head_count", ".attention.head_count_kv",
                     ".attention.key_length", ".attention.value_length", ".embedding_length",
                     ".context_length", ".attention.kv_lora_rank")
_GGUF_MAX_ARRAY_DEPTH = 2


class _GgufError(ValueError):
    """Başlık bozuk (Türkçe neden)."""


@dataclass(frozen=True)
class GgufMeta:
    """GGUF başlığından okunan üst veri (tensör verisi okunmaz)."""
    path: str
    size_bytes: int
    version: int = 0
    architecture: str = ""
    type: str = ""
    name: str = ""
    size_label: str = ""
    has_chat_template: bool = False
    reasoning: Optional[bool] = None
    split_count: int = 0
    split_no: int = 0
    error: str = ""               # boş değilse geçerli GGUF değil (Türkçe neden)
    kv: Optional[hardware.KvShape] = None   # KV şekli (donanım kapısı); yoksa None
    context_length: int = 0       # <mimari>.context_length; yoksa 0
    thinking: Optional[ThinkingProfile] = None   # sohbet şablonundan (G2b-1); hatalıysa None

    @property
    def is_projector(self) -> bool:
        """Görsel projektör (mmproj / clip): model değil, listelenmez."""
        return self.type == "mmproj" or self.architecture == "clip"


class _GgufReader:
    def __init__(self, stream, size: int):
        self.f = stream
        self.size = size

    def read(self, n: int) -> bytes:
        data = self.f.read(n)
        if len(data) != n:
            raise _GgufError("dosya yarıda kesik")
        return data

    def skip(self, n: int) -> None:
        if n < 0 or self.f.tell() + n > self.size:
            raise _GgufError("dosya yarıda kesik")
        self.f.seek(n, 1)

    def remaining(self) -> int:
        return self.size - self.f.tell()

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def string(self, keep: bool) -> Optional[str]:
        n = self.u64()
        if n > GGUF_MAX_STRING_BYTES:
            raise _GgufError(f"dize çok uzun ({n} bayt)")
        if not keep:
            self.skip(n)
            return None
        return self.read(n).decode("utf-8", "replace")

    def value(self, vtype: int, keep: bool, depth: int = 0) -> Any:
        if vtype in _GGUF_SCALARS:
            fmt = _GGUF_SCALARS[vtype]
            width = struct.calcsize(fmt)
            if not keep:
                self.skip(width)
                return None
            return struct.unpack(fmt, self.read(width))[0]
        if vtype == _GGUF_STRING:
            return self.string(keep)
        if vtype == _GGUF_ARRAY:
            if depth >= _GGUF_MAX_ARRAY_DEPTH:
                raise _GgufError("iç içe dizi çok derin")
            etype = self.u32()
            count = self.u64()
            if etype in _GGUF_SCALARS:
                total = count * struct.calcsize(_GGUF_SCALARS[etype])
                if total > self.remaining():
                    raise _GgufError("dizi dosyadan büyük")
                self.skip(total)
            elif etype == _GGUF_STRING:
                if count * 8 > self.remaining():
                    raise _GgufError("dizi dosyadan büyük")
                for _ in range(count):
                    self.string(False)
            elif etype == _GGUF_ARRAY:
                if count * 12 > self.remaining():
                    raise _GgufError("dizi dosyadan büyük")
                for _ in range(count):
                    self.value(_GGUF_ARRAY, False, depth + 1)
            else:
                raise _GgufError(f"bilinmeyen değer türü {etype}")
            return None
        raise _GgufError(f"bilinmeyen değer türü {vtype}")


@functools.lru_cache(maxsize=256)
def _read_gguf_cached(path: str, size: int, mtime_ns: int) -> GgufMeta:
    values: Dict[str, Any] = {}
    version = 0
    try:
        with open(path, "rb") as stream:
            reader = _GgufReader(stream, size)
            if size < 24 or reader.read(4) != _GGUF_MAGIC:
                raise _GgufError("GGUF sihirli sayısı yok")
            version = reader.u32()
            if version not in _GGUF_SUPPORTED_VERSIONS:
                raise _GgufError(f"desteklenmeyen GGUF sürümü {version}")
            reader.u64()                      # tensör sayısı (kullanılmaz)
            kv_count = reader.u64()
            if kv_count > GGUF_MAX_KV_COUNT:
                raise _GgufError(f"üst veri sayısı çok büyük ({kv_count})")
            for _ in range(kv_count):
                key = reader.string(True) or ""
                vtype = reader.u32()
                values[key] = reader.value(
                    vtype, keep=key in _GGUF_KEYS or key.endswith(_GGUF_KV_SUFFIXES))
    except _GgufError as exc:
        return GgufMeta(path=path, size_bytes=size, version=version, error=str(exc))
    except (OSError, struct.error) as exc:
        return GgufMeta(path=path, size_bytes=size, version=version,
                        error=f"dosya okunamadı ({exc})")

    def text(key: str) -> str:
        value = values.get(key)
        return value.strip() if isinstance(value, str) else ""

    def number(key: str) -> int:
        value = values.get(key)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    template = values.get("tokenizer.chat_template")
    has_template = isinstance(template, str) and bool(template.strip())
    arch = text("general.architecture")
    return GgufMeta(
        path=path, size_bytes=size, version=version,
        architecture=arch, type=text("general.type"),
        name=text("general.name"), size_label=text("general.size_label"),
        has_chat_template=has_template,
        reasoning=True if has_template and _template_has_think(template) else None,
        split_count=number("split.count"), split_no=number("split.no"),
        kv=hardware.kv_shape_from_gguf_keys(values),
        context_length=number(f"{arch}.context_length") if arch else 0,
        # llama-cpp-python şablona enable_thinking geçiremez; yalnız Qwen3 karma "/no_think"
        thinking=template_profile(template if has_template else None, backend=BACKEND_LLAMACPP,
                                  soft_switch=qwen3_soft_switch(
                                      [os.path.basename(path), text("general.name")])),
    )


def read_gguf_meta(path: Path) -> GgufMeta:
    """GGUF başlığını oku; (yol, boyut, mtime) ile önbellekli. Hata -> ``meta.error``.

    Korumalar: sihirli sayı, sürüm 2/3, üst veri sayısı ≤ ``GGUF_MAX_KV_COUNT``,
    dize ≤ ``GGUF_MAX_STRING_BYTES``, dizi dosya boyutunu aşamaz.
    """
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        return GgufMeta(path=str(path), size_bytes=0, error=f"dosya okunamadı ({exc})")
    return _read_gguf_cached(str(path), st.st_size, st.st_mtime_ns)


def _quant_label(stem: str) -> str:
    matches = _QUANT_RE.findall(stem or "")
    return matches[-1].upper() if matches else ""


def _gguf_display(path: Path) -> Tuple[str, str]:
    """(etiket kökü, niceleme) — parça son eki ve .gguf atılmış ad."""
    match = _GGUF_SPLIT_RE.match(path.name)
    stem = match.group("stem") if match else path.name[:-len(GGUF_SUFFIX)]
    return stem, _quant_label(stem)


def _gguf_split_parts(path: Path) -> Tuple[int, List[Path]]:
    """(beklenen parça sayısı, bulunan parçalar) — tek dosyada (1, [path])."""
    match = _GGUF_SPLIT_RE.match(path.name)
    if not match:
        return 1, [path]
    count = int(match.group("count"))
    stem = match.group("stem")
    parts = []
    for no in range(1, count + 1):
        candidate = path.with_name(f"{stem}-{no:05d}-of-{count:05d}{GGUF_SUFFIX}")
        if candidate.is_file():
            parts.append(candidate)
    return count, parts


def _gguf_size(path: Path) -> Optional[int]:
    """GGUF'un (parçalıysa bulunan parçaların) toplam boyutu; dosya yoksa None."""
    if not path.is_file():
        return None
    _, parts = _gguf_split_parts(path)
    try:
        return sum(p.stat().st_size for p in parts) if parts else None
    except OSError:
        return None


def _gguf_problem(path: Path, llama_ok: bool,
                  meta: Optional[GgufMeta] = None) -> Tuple[Optional[str], Optional[GgufMeta]]:
    """(neden ya da None, üst veri). Sıra: dosya -> başlık -> parçalar -> llama-cpp-python."""
    if not path.is_file():
        return MSG_GGUF_MISSING.format(path=path), None
    meta = meta or read_gguf_meta(path)
    if meta.error:
        return MSG_GGUF_INVALID.format(why=meta.error, path=path), meta
    if meta.is_projector:
        return MSG_GGUF_INVALID.format(why="görsel projektör (mmproj) dosyası, model değil",
                                       path=path), meta
    match = _GGUF_SPLIT_RE.match(path.name)
    if match and int(match.group("no")) != 1:
        return MSG_GGUF_INVALID.format(why="parçalı dosyanın ilk parçası değil", path=path), meta
    count, parts = _gguf_split_parts(path)
    if len(parts) != count:
        return MSG_GGUF_INVALID.format(
            why=f"parçalı dosyanın {count} parçasından {len(parts)} tanesi var", path=path), meta
    if not llama_ok:
        return MSG_LLAMA_MISSING, meta
    return None, meta


# ---------------------------------------------------------------------------
# Donanım kapısı (KARARLAR 17, 17a): model başına bellek hükmü
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MemoryAssessedModel(DiscoveredModel):
    """Bellek tahmini yapılabilen keşif kaydı (``DiscoveredModel`` + ``memory``).

    Tahmin yalnız ağırlık boyutu bilinen modelde yapılır; boyut bilinmiyorsa
    (ör. genel OpenAI uyumlu sunucu, kurulu olmayan model) kayıt düz
    ``DiscoveredModel`` kalır (JSON'da ``memory`` alanı yoktur).
    """
    memory: Optional[hardware.MemoryFit] = None


@dataclass(frozen=True)
class WarnedEngineCheck(EngineCheck):
    """``check_engine``'in uyarılı olumlu sonucu (``ok`` True + ``warning``).

    MT-12 (2026-09-30): LM Studio / OpenAI uyumlu sunucu modelinin kaba bellek
    tahmini "sığmayabilir" diyorsa denetim geçer ama gerekçe kaybolmaz
    (``hardware.fit_warning``). Uyarı yoksa düz ``EngineCheck`` döner.
    """
    warning: str = ""


@dataclass(frozen=True)
class _MemInput:
    """Tahminin girdileri: ölçülen ağırlık, KV şekli (yoksa None), kip, bağlam.

    ``server_managed``: yüklemeyi ve bellek denetimini sunucu yapar (LM Studio,
    OpenAI uyumlu sunucu); hüküm ``hardware.server_verdict``'ten geçer.
    ``loaded``: sunucu modeli şu an yüklü tutuyor mu (LM Studio REST
    ``loaded_instances`` / ``state``); bilinmiyorsa None.
    ``think_tokens`` (G2b-2): düşünerek çeviride istek başı düşünme payı (toplu
    kipte KV'ye eklenir; ``hardware.assess``); düşünmeyen modelde 0.
    """
    weights: Optional[int]
    kv: Optional[hardware.KvShape] = None
    mode: str = hardware.MODE_BATCHED
    context_tokens: Optional[int] = None
    server_managed: bool = False
    loaded: Optional[bool] = None
    think_tokens: int = 0


def _positive_size(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _mlx_mem(info: "MlxInfo", spec: Optional[EngineSpec] = None, fmt: str = "") -> _MemInput:
    """MLX klasörünün bellek girdileri (süreç içi, toplu kip).

    G2b-2 (KARARLAR 22): sohbet şablonu düşünen ama kapatma anahtarı olmayan
    model ("kapatilamaz") düşünerek çevirir; istek başı düşünme payının tavanı
    (motorun ``options.think_budget``'ı ya da ``MT_THINK_ALLOWANCE_CAP_TOKENS``)
    KV hesabına girer. HY-MT biçimi ve kapatılabilen model: pay yok (0).
    """
    think = 0
    profile = info.thinking
    if fmt != FORMAT_HYMT and profile is not None and profile.cls == THINKING_ALWAYS:
        from .backends import resolve_think_cap
        think = resolve_think_cap(spec)
    return _MemInput(info.size_bytes, info.kv, hardware.MODE_BATCHED, think_tokens=think)


def _mem_fit(mem: Optional[_MemInput],
             hw: Optional[hardware.HardwareInfo] = None) -> Optional[hardware.MemoryFit]:
    if mem is None or _positive_size(mem.weights) is None:
        return None
    fit = hardware.assess(mem.weights, mem.kv, mode=mem.mode,
                          context_tokens=mem.context_tokens, hw=hw,
                          think_tokens=mem.think_tokens)
    if mem.server_managed:
        fit = hardware.server_verdict(fit, loaded=mem.loaded)
    return fit


def _server_mem(size: Optional[int], loaded: Optional[bool],
                spec: Optional[EngineSpec] = None, *,
                loaded_context: Optional[int] = None) -> _MemInput:
    """LM Studio / OpenAI uyumlu sunucu modelinin bellek girdileri (KV şekli
    bilinmiyor; MT-12: hüküm sunucu kuralıyla, ``hardware.server_verdict``).
    MT-15 (G2b-1): sunucunun modeli YÜKLEDİĞİ bağlam biliniyorsa o kullanılır."""
    return _MemInput(size, None, hardware.MODE_CONTEXT, _server_context(spec, loaded_context),
                     server_managed=True, loaded=loaded)


def _finish(model: DiscoveredModel, mem: Optional[_MemInput]) -> DiscoveredModel:
    """Kayda donanım hükmünü uygula (yalnız kullanılabilir kaydı kapatır).

    Genel kapı kapalıysa neden onun iletisidir; değilse model toplu iş 1 ile
    sığmıyorsa "bu Mac'e sığmaz". Zaten kullanılamayan kaydın nedeni korunur.
    MT-12: sunucu modelinde hüküm uyarıysa (``hardware.blocks`` False) kayıt
    kullanılabilir kalır; uyarı ``memory``'dedir (seçici etiketi, tablo).
    """
    gate = hardware.general_gate()
    fit = _mem_fit(mem, gate.hardware)
    usable, reason = model.usable, model.reason
    if usable and not gate.allowed:
        usable, reason = False, gate.reason
    elif usable and hardware.blocks(fit):
        usable, reason = False, hardware.too_big_message(fit)
    values = {f.name: getattr(model, f.name) for f in fields(DiscoveredModel)}
    values.update(usable=usable, reason=reason)
    if fit is None:
        return DiscoveredModel(**values)
    return MemoryAssessedModel(**values, memory=fit)


def _gguf_context(spec: Optional[EngineSpec] = None) -> int:
    """llama.cpp arka ucunun bağlamı: options.n_ctx ya da MT_CONTEXT_TOKENS
    (llamacpp_backend ile aynı kural; KV bu uzunlukta baştan ayrılır)."""
    options = spec.options if spec is not None and isinstance(spec.options, dict) else {}
    return _positive_size(options.get("n_ctx")) or int(MT_CONTEXT_TOKENS)


def _ollama_mem(item: Optional[dict], show: Any,
                spec: Optional[EngineSpec] = None) -> _MemInput:
    """Ollama modeli: ağırlık /api/tags ``size``, KV /api/show ``model_info``."""
    size = _positive_size(item.get("size")) if isinstance(item, dict) else None
    info = show.get("model_info") if isinstance(show, dict) else None
    info = info if isinstance(info, dict) else {}
    arch = info.get("general.architecture")
    model_context = info.get(f"{arch}.context_length") if isinstance(arch, str) else None
    options = spec.options if spec is not None and isinstance(spec.options, dict) else {}
    return _MemInput(size, hardware.kv_shape_from_gguf_keys(info, hardware.KV_SOURCE_OLLAMA),
                     hardware.MODE_CONTEXT,
                     hardware.ollama_context_tokens(options.get("num_ctx"), model_context))


def _server_context(spec: Optional[EngineSpec] = None,
                    loaded_context: Optional[int] = None) -> int:
    """LM Studio / OpenAI uyumlu sunucunun bağlamı (KV tahmini için).

    MT-15 (G2b-1, 2026-10-05): LM Studio modeli istenenden farklı bağlamla
    yükleyebilir (gemma-4-26b: ``--context-length 8192`` istendi, 116 736
    yüklendi). Yüklü bağlam (REST ``loaded_instances[0].config.context_length``)
    biliniyorsa O kullanılır; yoksa bildirilen options.num_ctx ya da
    MT_CONTEXT_TOKENS varsayılır (eski kural)."""
    loaded = _positive_size(loaded_context)
    if loaded is not None:
        return loaded
    options = spec.options if spec is not None and isinstance(spec.options, dict) else {}
    return _positive_size(options.get("num_ctx")) or int(MT_CONTEXT_TOKENS)


# ---------------------------------------------------------------------------
# models/ taraması
# ---------------------------------------------------------------------------
@dataclass
class _Walk:
    mlx_dirs: List[Path] = field(default_factory=list)
    gguf_files: List[Path] = field(default_factory=list)
    truncated: bool = False


def _walk_models(root: Path) -> _Walk:
    """models/ altında MLX klasörleri ve GGUF dosyaları (derinlik ≤ DISCOVERY_MAX_DEPTH).

    Sembolik bağlar izlenir (KARARLAR 1), aynı gerçek yol bir kez gezilir;
    gizli girdiler ('.' ile başlayan) atlanır; en çok DISCOVERY_MAX_ENTRIES
    girdi. Ad sırası kararlı: ``x`` ile ``x.nosync`` aynı klasörse ``x`` kalır.
    """
    walk = _Walk()
    if not root.is_dir():
        return walk
    visited: Set[str] = set()
    entries = 0
    queue: deque = deque([(root, 0)])
    while queue:
        directory, depth = queue.popleft()
        real = os.path.realpath(str(directory))
        if real in visited:
            continue
        visited.add(real)
        try:
            with os.scandir(directory) as it:
                items = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        names = {e.name for e in items}
        if depth > 0 and MLX_CONFIG_FILE in names:
            has_weights = any(n.endswith(".safetensors") for n in names)
            has_gguf = any(n.lower().endswith(GGUF_SUFFIX) for n in names)
            if has_weights or not has_gguf:
                walk.mlx_dirs.append(directory)
                continue
        for entry in items:
            entries += 1
            if entries > DISCOVERY_MAX_ENTRIES:
                walk.truncated = True
                logger.warning("Model taraması %d girdide kesildi (%s)",
                               DISCOVERY_MAX_ENTRIES, root)
                return walk
            if entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=True)
                is_file = not is_dir and entry.is_file(follow_symlinks=True)
            except OSError:
                continue
            if is_dir and depth + 1 <= DISCOVERY_MAX_DEPTH:
                queue.append((Path(entry.path), depth + 1))
            elif is_file and entry.name.lower().endswith(GGUF_SUFFIX) \
                    and depth + 1 <= DISCOVERY_MAX_DEPTH:
                walk.gguf_files.append(Path(entry.path))
    return walk


def _file_engine_id(prefix: str, path: Path, root: Path) -> str:
    """models/ doğrudan çocuğu tek adla, derindeki mutlak (sözcüksel) yolla."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 1 and registry._is_single_segment(rel.parts[0]):
        return build_engine_id(prefix, rel.parts[0])
    return build_engine_id(prefix, str(path))


def _realpath(path: Path) -> str:
    return os.path.realpath(str(path))


def _scan_files(root: Path, excluded: Dict[str, str], llama_ok: bool
                ) -> Tuple[List[DiscoveredModel], List[DiscoveredModel], SourceStatus, SourceStatus]:
    """``excluded``: kayıtlı motorların gerçek yolu -> motor adı (ayrıca listelenmez)."""
    walk = _walk_models(root)

    mlx_models: List[DiscoveredModel] = []
    engine_dirs: Dict[str, str] = {}      # sayıma giren kayıtlı motor klasörleri (F4-08)
    for directory in walk.mlx_dirs:
        real = _realpath(directory)
        if real in excluded:
            engine_dirs.setdefault(real, excluded[real])
            continue   # kayıtlı motorun klasörü ayrıca listelenmez (kendi kaydıyla görünür)
        info = inspect_mlx_dir(directory)
        label = _strip_nosync(directory.name)
        mlx_models.append(_finish(DiscoveredModel(
            engine_id=_file_engine_id(PREFIX_MLX, directory, root), label=label,
            source=SOURCE_MLX, backend=BACKEND_MLX, format_guess=guess_format(label),
            usable=info.usable, reason=info.reason, size_bytes=info.size_bytes,
            detail=info.detail, location=str(directory), reasoning=info.reasoning,
            **_thinking_fields(info.thinking, guess_format(label))),
            _mlx_mem(info, fmt=guess_format(label))))

    gguf_models: List[DiscoveredModel] = []
    engine_files: Dict[str, str] = {}     # sayıma giren kayıtlı motor dosyaları
    seen_real: Set[str] = set()
    for path in walk.gguf_files:
        match = _GGUF_SPLIT_RE.match(path.name)
        if match and int(match.group("no")) != 1:
            continue   # parçalı dosyada yalnız ilk parça listelenir
        if path.name.lower().startswith("mmproj"):
            continue   # görsel projektör, model değil
        real = _realpath(path)
        if real in excluded:
            engine_files.setdefault(real, excluded[real])
            continue
        if real in seen_real:
            continue
        seen_real.add(real)
        meta = read_gguf_meta(path)
        if not meta.error and meta.is_projector:
            continue
        reason, _ = _gguf_problem(path, llama_ok, meta)
        _, parts = _gguf_split_parts(path)
        size: Optional[int]
        try:
            size = sum(p.stat().st_size for p in parts) if parts else None
        except OSError:
            size = None
        stem, quant = _gguf_display(path)
        gguf_models.append(_finish(DiscoveredModel(
            engine_id=_file_engine_id(PREFIX_GGUF, path, root), label=stem,
            source=SOURCE_GGUF, backend=BACKEND_LLAMACPP, format_guess=guess_format(path.name),
            usable=reason is None, reason=reason or "", size_bytes=size,
            detail=_join_detail(meta.size_label, quant), location=str(path),
            reasoning=meta.reasoning, **_thinking_fields(meta.thinking, guess_format(path.name))),
            _MemInput(size, meta.kv, hardware.MODE_CONTEXT, _gguf_context())))

    # F4-08: sayım models/'daki TÜM MLX klasörleridir; yerleşik / kayıtlı
    # motorun klasörü "mlx:" kaydı olarak ayrıca listelenmez ama sayılır
    # ("1 klasör (hymt-7b dahil)"; eskiden "0 klasör" diyordu).
    mlx_count = len(mlx_models) + len(engine_dirs)
    if engine_dirs:
        names = ", ".join(sorted(set(engine_dirs.values())))
        mlx_message = MSG_SOURCE_COUNT_DIRS_ENGINES.format(n=mlx_count, engines=names)
    else:
        mlx_message = MSG_SOURCE_COUNT_DIRS.format(n=mlx_count)
    gguf_count = len(gguf_models) + len(engine_files)
    if engine_files:
        gguf_message = MSG_SOURCE_COUNT_FILES_ENGINES.format(
            n=gguf_count, engines=", ".join(sorted(set(engine_files.values()))))
    else:
        gguf_message = MSG_SOURCE_COUNT_FILES.format(n=gguf_count)
    if gguf_models and not llama_ok:
        gguf_message = f"{gguf_message} · {MSG_LLAMA_MISSING}"
    if walk.truncated:
        # F4-10: kesilen tarama sessiz geçmez (yalnız günlükte kalıyordu).
        note = MSG_SOURCE_COUNT_TRUNCATED.format(limit=DISCOVERY_MAX_ENTRIES)
        mlx_message = f"{mlx_message} · {note}"
        gguf_message = f"{gguf_message} · {note}"
    mlx_status = SourceStatus(
        source=SOURCE_MLX, label=SOURCE_STATUS_LABELS[SOURCE_MLX], reachable=None,
        message=mlx_message, location=str(root))
    gguf_status = SourceStatus(
        source=SOURCE_GGUF, label=SOURCE_STATUS_LABELS[SOURCE_GGUF], reachable=None,
        message=gguf_message, location=str(root))
    return mlx_models, gguf_models, mlx_status, gguf_status


# ---------------------------------------------------------------------------
# Sunucu yoklamaları
# ---------------------------------------------------------------------------
@dataclass
class _Probe:
    status: SourceStatus
    ok: bool                                   # liste alındı mı
    models: List[DiscoveredModel] = field(default_factory=list)
    index: Dict[str, DiscoveredModel] = field(default_factory=dict)   # model adı -> kayıt
    # model adı -> (tags öğesi, /api/show yanıtı): kayıtlı motorun kendi
    # seçenekleriyle (num_ctx) bellek hükmünü yeniden kurmak için
    raw: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)


def _model_id_problem(engine_id: str, name: str) -> str:
    problem = registry.model_name_problem(name)
    return MSG_BAD_ID.format(engine_id=engine_id, why=problem) if problem else ""


def _ollama_cloud_reason(name: str, item: Any, show: Any) -> str:
    """Bulut modeliyse Türkçe neden (F4-01; KARARLAR 2), yerelse ""."""
    if not http_util.ollama_cloud_model(name, item, show):
        return ""
    host = http_util.ollama_remote_host(item, show) or http_util.OLLAMA_CLOUD_DEFAULT_HOST
    return MSG_OLLAMA_CLOUD_MODEL.format(model=name, host=host)


def _probe_ollama(url: str, timeout: float) -> _Probe:
    label = SOURCE_STATUS_LABELS[SOURCE_OLLAMA]
    data, err, detail = _fetch_detail(http_util.url_join(url, "/api/tags"), timeout)
    items = _ollama_names(data)
    if items is None:
        reachable, message = _no_list_message(label, url, err, detail,
                                              _ollama_down_message(url))
        return _Probe(SourceStatus(SOURCE_OLLAMA, label, reachable, message, url), ok=False)
    names = [str(item.get("name") or item.get("model")) for item in items]
    enrich = names[:DISCOVERY_MAX_SHOW_REQUESTS]
    with ThreadPoolExecutor(max_workers=min(8, len(enrich) + 1)) as pool:
        ps_future = pool.submit(_fetch, http_util.url_join(url, "/api/ps"), timeout)
        show_futures = {name: pool.submit(_ollama_show, url, name, timeout) for name in enrich}
        ps_data, _ = ps_future.result()
        shows = {name: fut.result()[0] for name, fut in show_futures.items()}
    loaded_names = _ollama_names(ps_data)
    loaded_set = None if loaded_names is None else {
        str(item.get("name") or item.get("model")) for item in loaded_names}

    probe = _Probe(SourceStatus(SOURCE_OLLAMA, label, True, "", url), ok=True)
    for item, name in zip(items, names):
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        show = shows.get(name)
        caps = show.get("capabilities") if isinstance(show, dict) else None
        caps = [str(c) for c in caps] if isinstance(caps, list) else None
        engine_id = build_engine_id(PREFIX_OLLAMA, name)
        reason = _model_id_problem(engine_id, name)
        cloud = _ollama_cloud_reason(name, item, show)
        if not reason and cloud:
            reason = cloud
        if not reason and caps is not None and "completion" not in caps:
            reason = MSG_EMBEDDING_MODEL
        size = item.get("size")
        model = _finish(DiscoveredModel(
            engine_id=engine_id, label=name, source=SOURCE_OLLAMA, backend=BACKEND_OLLAMA,
            format_guess=FORMAT_CHAT, usable=not reason, reason=reason,
            size_bytes=size if isinstance(size, int) and not isinstance(size, bool) else None,
            detail=_join_detail(details.get("parameter_size"), details.get("quantization_level")),
            location=url,
            reasoning=None if caps is None else ("thinking" in caps),
            loaded=None if loaded_set is None else (name in loaded_set),
            **_thinking_fields(ollama_profile(show) if isinstance(show, dict) else None)),
            # Bulut modelinin "ağırlığı" bu Mac'te değil: bellek hükmü verilmez.
            None if cloud else _ollama_mem(item, show))
        probe.models.append(model)
        probe.index[name] = model
        probe.raw[name] = (item, show)
    probe.status = SourceStatus(SOURCE_OLLAMA, label, True,
                                MSG_SOURCE_COUNT_MODELS.format(n=len(probe.models)), url)
    return probe


def _lmstudio_rest(root: str, timeout: float) -> Dict[str, dict]:
    """LM Studio yerel REST üst verisi: /api/v1/models, yoksa /api/v0/models; yoksa {}."""
    data, _ = _fetch(http_util.url_join(root, "/api/v1/models"), timeout)
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        out: Dict[str, dict] = {}
        for item in data["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            quant = item.get("quantization")
            quant_name = quant.get("name") if isinstance(quant, dict) else quant
            caps = item.get("capabilities")
            loaded = item.get("loaded_instances")
            fmt = item.get("format")
            out[item["key"]] = {
                "embedding": item.get("type") == "embedding",
                "detail": _join_detail(item.get("params_string"), quant_name,
                                       fmt.upper() if isinstance(fmt, str) else ""),
                "size": item.get("size_bytes"),
                "reasoning": (None if not isinstance(caps, dict) else ("reasoning" in caps)),
                "loaded": (bool(loaded) if isinstance(loaded, list) else None),
                # G2b-1: düşünme sınıfı (allowed_options) + MT-15 yüklü bağlam
                "thinking": lmstudio_profile(caps),
                "loaded_context": http_util.lmstudio_loaded_config(item)[0],
            }
        return out
    data, _ = _fetch(http_util.url_join(root, "/api/v0/models"), timeout)
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        out = {}
        for item in data["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            state = item.get("state")
            out[item["id"]] = {
                "embedding": item.get("type") in ("embedding", "embeddings"),
                "detail": _join_detail(item.get("quantization"), item.get("compatibility_type")),
                "size": None,
                "reasoning": None,
                "loaded": (state == "loaded") if state in ("loaded", "not-loaded") else None,
                "thinking": lmstudio_profile(None),     # REST v0 düşünme bildirmez
                "loaded_context": http_util.lmstudio_loaded_config(item)[0],
            }
        return out
    return {}


def _probe_lmstudio(base: str, timeout: float) -> _Probe:
    label = SOURCE_STATUS_LABELS[SOURCE_LMSTUDIO]
    root = http_util.url_root(base)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list_future = pool.submit(_fetch_detail, http_util.url_join(base, "/models"), timeout)
        rest_future = pool.submit(_lmstudio_rest, root, timeout)
        data, err, detail = list_future.result()
        rest = rest_future.result()
    ids = _openai_ids(data)
    if ids is None:
        reachable, message = _no_list_message(label, base, err, detail,
                                              MSG_LMSTUDIO_DOWN.format(url=base))
        return _Probe(SourceStatus(SOURCE_LMSTUDIO, label, reachable, message, base), ok=False)
    probe = _Probe(SourceStatus(SOURCE_LMSTUDIO, label, True, "", base), ok=True)
    listed = list(dict.fromkeys(ids))
    # REST'te olup /v1/models'ta olmayan (indirilmiş ama yüklü değil, JIT kapalı)
    not_listed = [key for key in rest if key not in set(listed)]
    for model_id in listed + not_listed:
        info = rest.get(model_id, {})
        engine_id = build_engine_id(PREFIX_LMSTUDIO, model_id)
        reason = _model_id_problem(engine_id, model_id)
        if not reason and model_id not in listed:
            reason = MSG_LMSTUDIO_NOT_LOADED.format(model=model_id)
        if not reason and info.get("embedding"):
            reason = MSG_EMBEDDING_MODEL
        size = info.get("size")
        size = size if isinstance(size, int) and not isinstance(size, bool) else None
        model = _finish(DiscoveredModel(
            engine_id=engine_id, label=model_id, source=SOURCE_LMSTUDIO, backend=BACKEND_OPENAI,
            format_guess=FORMAT_CHAT, usable=not reason, reason=reason,
            size_bytes=size,
            detail=info.get("detail", ""), location=base,
            reasoning=info.get("reasoning"), loaded=info.get("loaded"),
            loaded_context=info.get("loaded_context"),
            **_thinking_fields(info.get("thinking"))),
            _server_mem(size, info.get("loaded"), loaded_context=info.get("loaded_context")))
        probe.models.append(model)
        probe.index[model_id] = model
        probe.raw[model_id] = (info, None)
    probe.status = SourceStatus(SOURCE_LMSTUDIO, label, True,
                                MSG_SOURCE_COUNT_MODELS.format(n=len(probe.models)), base)
    return probe


def _probe_openai(base: str, timeout: float, sources: Optional[SourcesConfig]) -> _Probe:
    label = _openai_server_name(base, sources)
    data, err = _fetch(http_util.url_join(base, "/models"), timeout)
    ids = _openai_ids(data)
    if ids is None:
        # Zaman aşımı da "yanıt vermiyor" (MSG_OPENAI_DOWN başlatma komutu önermez)
        silent = err in (_FETCH_DOWN, _FETCH_TIMEOUT)
        message = (MSG_OPENAI_DOWN if silent else MSG_NOT_OPENAI).format(url=base)
        return _Probe(SourceStatus(SOURCE_OPENAI, label, not silent, message, base),
                      ok=False)
    probe = _Probe(SourceStatus(SOURCE_OPENAI, label, True, "", base), ok=True)
    for model_id in dict.fromkeys(ids):
        engine_id = build_engine_id(PREFIX_OPENAI, model_id, base)
        reason = _model_id_problem(engine_id, model_id)
        model = _finish(DiscoveredModel(
            engine_id=engine_id, label=model_id, source=SOURCE_OPENAI, backend=BACKEND_OPENAI,
            format_guess=FORMAT_CHAT, usable=not reason, reason=reason, location=base,
            **_thinking_fields(_OPENAI_THINKING)), None)
        probe.models.append(model)
        probe.index[model_id] = model
    probe.status = SourceStatus(SOURCE_OPENAI, label, True,
                                MSG_SOURCE_COUNT_MODELS.format(n=len(probe.models)), base)
    return probe


def _run_probe(kind: str, url: str, timeout: float, sources: Optional[SourcesConfig]) -> _Probe:
    if kind == SOURCE_OLLAMA:
        return _probe_ollama(url, timeout)
    if kind == SOURCE_LMSTUDIO:
        return _probe_lmstudio(url, timeout)
    return _probe_openai(url, timeout, sources)


def _server_miss(kind: str, spec: EngineSpec, sources: Optional[SourcesConfig]) -> str:
    if kind == SOURCE_LMSTUDIO:
        return MSG_LMSTUDIO_NOT_LOADED.format(model=spec.model)
    server = (SOURCE_STATUS_LABELS[SOURCE_OLLAMA] if kind == SOURCE_OLLAMA
              else _openai_server_name(spec.base_url, sources))
    return MSG_MODEL_NOT_ON_SERVER.format(model=spec.model, server=server)


def _probe_lookup(kind: str, probe: _Probe, model: str) -> Optional[DiscoveredModel]:
    if kind == SOURCE_OLLAMA:
        name = _ollama_match(model, list(probe.index))
        return probe.index.get(name) if name else None
    return probe.index.get(model)


# ---------------------------------------------------------------------------
# Keşif
# ---------------------------------------------------------------------------
def _registered_specs() -> List[EngineSpec]:
    return list(registry.available_engines().values())


def _registered_realpaths(specs: Sequence[EngineSpec], root: Path) -> Dict[str, str]:
    """Kayıtlı (yerleşik / mt_engines.json) motorların gerçek yolu -> motor adı."""
    out: Dict[str, str] = {}
    for spec in specs:
        if spec.backend == BACKEND_MLX:
            directory = _mlx_dir_for(spec, root)
            if directory is not None:
                out.setdefault(_realpath(directory), spec.name)
        elif spec.backend == BACKEND_LLAMACPP and spec.path:
            out.setdefault(_realpath(Path(spec.path)), spec.name)
    return out


def _registered_model(spec: EngineSpec, root: Path, llama_ok: bool,
                      probes: Dict[Tuple[str, str], _Probe],
                      sources: SourcesConfig) -> DiscoveredModel:
    source = SOURCE_BUILTIN if spec.source == SPEC_SOURCE_BUILTIN else SOURCE_USER
    common = dict(engine_id=spec.name, label=_base_label(spec), source=source, backend=spec.backend,
                  format_guess=spec.format)
    if spec.backend == BACKEND_MLX:
        directory = _mlx_dir_for(spec, root)
        if directory is None:
            reason, hint = _mlx_missing(spec, root)
            if hint:
                reason = f"{reason} İpucu: {hint}"
            expected = Path(spec.path) if spec.path else root / spec.local_dir
            return _finish(DiscoveredModel(usable=False, reason=reason, location=str(expected),
                                           **common), None)
        info = inspect_mlx_dir(directory)
        return _finish(DiscoveredModel(usable=info.usable, reason=info.reason,
                                       size_bytes=info.size_bytes, detail=info.detail,
                                       location=str(directory), reasoning=info.reasoning,
                                       **_thinking_fields(info.thinking, spec.format),
                                       **common),
                       _mlx_mem(info, spec, spec.format))
    if spec.backend == BACKEND_LLAMACPP:
        path = Path(spec.path)
        reason, meta = _gguf_problem(path, llama_ok)
        size = _gguf_size(path)
        _, quant = _gguf_display(path)
        return _finish(DiscoveredModel(usable=reason is None, reason=reason or "", size_bytes=size,
                                       detail=_join_detail(meta.size_label if meta else "", quant),
                                       location=str(path),
                                       reasoning=meta.reasoning if meta else None,
                                       **_thinking_fields(meta.thinking if meta else None,
                                                          spec.format),
                                       **common),
                       _MemInput(size, meta.kv if meta else None, hardware.MODE_CONTEXT,
                                 _gguf_context(spec)))
    kind = _server_kind(spec)
    probe = probes.get((kind, spec.base_url))
    if probe is None or not probe.ok:
        message = probe.status.message if probe is not None else ""
        return _finish(DiscoveredModel(usable=False, reason=message, location=spec.base_url,
                                       **common), None)
    match = _probe_lookup(kind, probe, spec.model)
    if match is None:
        return _finish(DiscoveredModel(usable=False, reason=_server_miss(kind, spec, sources),
                                       location=spec.base_url, **common), None)
    # Bellek hükmü kayıtlı motorun KENDİ seçenekleriyle (num_ctx) yeniden kurulur.
    raw_item, raw_show = probe.raw.get(match.label, (None, None))
    if kind == SOURCE_OLLAMA and _ollama_cloud_reason(match.label, raw_item, raw_show):
        mem: Optional[_MemInput] = None     # bulut modeli: bellek hükmü yok (F4-01)
    elif kind == SOURCE_OLLAMA:
        mem = _ollama_mem(raw_item, raw_show, spec)
    elif match.size_bytes:
        mem = _server_mem(match.size_bytes, match.loaded, spec,
                          loaded_context=match.loaded_context)
    else:
        mem = None
    # Sunucu listesindeki kaydın donanım hükmü (varsayılan bağlamla) geri alınır;
    # _finish onu kaydın kendi seçenekleriyle yeniden verir.
    usable, reason = match.usable, match.reason
    gate = hardware.general_gate()
    match_fit = getattr(match, "memory", None)
    by_gate = not gate.allowed and reason == gate.reason
    by_fit = match_fit is not None and reason == hardware.too_big_message(match_fit)
    if not usable and reason and (by_gate or by_fit):
        usable, reason = True, ""
    return _finish(DiscoveredModel(usable=usable, reason=reason,
                                   size_bytes=match.size_bytes, detail=match.detail,
                                   location=spec.base_url, reasoning=match.reasoning,
                                   loaded=match.loaded, loaded_context=match.loaded_context,
                                   thinking=match.thinking, thinking_off=match.thinking_off,
                                   **common), mem)


def discover_all(*, http_timeout: float = DISCOVERY_HTTP_TIMEOUT_S,
                 sources_path: Optional[Path] = None,
                 models_dir: Optional[Path] = None) -> DiscoveryReport:
    """Tüm kaynakları tara (model YÜKLEMEZ). Kaynaklar paralel taranır.

    Sıra: yerleşik, kayıtlı (mt_engines.json), MLX, GGUF, Ollama, LM Studio,
    OpenAI uyumlu sunucular. Sunucu kapalıysa o sunucunun modeli yoktur;
    yalnız ``sources``'ta durum satırı. Yerleşik motor her zaman listededir
    (kullanılamıyorsa nedeniyle).
    """
    sources = load_sources(sources_path) if sources_path is not None else load_sources()
    root = Path(os.path.abspath(str(models_dir if models_dir is not None
                                    else registry.MODELS_DIR)))
    hardware.hardware_info()   # ölçüm bir kez, iş parçacıklarından önce (önbellek)
    specs = _registered_specs()
    excluded = _registered_realpaths(specs, root)
    llama_ok = llama_cpp_available()

    listed: List[Tuple[str, str]] = []
    if sources.ollama_enabled:
        listed.append((SOURCE_OLLAMA, sources.ollama_url))
    if sources.lmstudio_enabled:
        listed.append((SOURCE_LMSTUDIO, sources.lmstudio_url))
    for server in sources.openai_servers:
        listed.append((SOURCE_OPENAI, server.url))
    plan = list(dict.fromkeys(listed))
    for spec in specs:
        if spec.backend in (BACKEND_OLLAMA, BACKEND_OPENAI) and spec.base_url:
            key = (_server_kind(spec), spec.base_url)
            if key not in plan:
                plan.append(key)

    with ThreadPoolExecutor(max_workers=len(plan) + 1) as pool:
        files_future = pool.submit(_scan_files, root, excluded, llama_ok)
        probe_futures = {key: pool.submit(_run_probe, key[0], key[1], http_timeout, sources)
                         for key in plan}
        mlx_models, gguf_models, mlx_status, gguf_status = files_future.result()
        probes = {key: fut.result() for key, fut in probe_futures.items()}

    models: List[DiscoveredModel] = [
        _registered_model(spec, root, llama_ok, probes, sources) for spec in specs]
    models += mlx_models + gguf_models
    statuses: List[SourceStatus] = [mlx_status, gguf_status]
    for key in dict.fromkeys(listed):
        models += probes[key].models
        statuses.append(probes[key].status)
    return DiscoveryReport(models=tuple(models), sources=tuple(statuses), scanned_at=time.time())


# ---------------------------------------------------------------------------
# Ön kontrol (yükleme yok)
# ---------------------------------------------------------------------------
def _spec_problem(spec: EngineSpec, http_timeout: float
                  ) -> Tuple[Optional[str], str, Optional[_MemInput]]:
    """(neden ya da None, ipucu, bellek tahmini girdileri ya da None).

    Bellek girdileri yalnız bu denetimin ZATEN yaptığı okumalardan gelir
    (klasör, GGUF başlığı, /api/tags, /api/show, LM Studio REST); donanım
    kapısı için ek istek gönderilmez.
    """
    root = Path(os.path.abspath(str(registry.MODELS_DIR)))
    if spec.backend == BACKEND_MLX:
        directory = _mlx_dir_for(spec, root)
        if directory is None:
            reason, hint = _mlx_missing(spec, root)
            return reason, hint, None
        info = inspect_mlx_dir(directory)
        if not info.usable:
            return info.reason, "", None
        return None, "", _mlx_mem(info, spec, spec.format)
    if spec.backend == BACKEND_LLAMACPP:
        path = Path(spec.path)
        reason, meta = _gguf_problem(path, llama_cpp_available())
        if reason:
            return reason, "", None
        return None, "", _MemInput(_gguf_size(path), meta.kv if meta else None,
                                   hardware.MODE_CONTEXT, _gguf_context(spec))
    started = time.monotonic()

    def remaining() -> float:
        # Ek üst veri isteği ön kontrolün toplam süre bütçesini aşmasın.
        return http_timeout - (time.monotonic() - started)

    if spec.backend == BACKEND_OLLAMA:
        data, err, detail = _fetch_detail(http_util.url_join(spec.base_url, "/api/tags"),
                                          http_timeout)
        items = _ollama_names(data)
        if items is None:
            _reachable, message = _no_list_message(
                SOURCE_STATUS_LABELS[SOURCE_OLLAMA], spec.base_url, err, detail,
                _ollama_down_message(spec.base_url))
            return message, "", None
        names = [str(item.get("name") or item.get("model")) for item in items]
        name = _ollama_match(spec.model, names)
        if name is None:
            return _server_miss(SOURCE_OLLAMA, spec, None), "", None
        item = items[names.index(name)]
        # Keşifle aynı hüküm (F4-01): bulut modeli bu bilgisayarda çalışmaz.
        cloud = _ollama_cloud_reason(name, item, None)
        if cloud:
            return cloud, "", None
        # Keşifle aynı hüküm (Vk 2026-09-28): gömme modeli metin üretemez.
        # /api/show yalnız üst veri okur (model yüklenmez); yanıt alınamazsa
        # engel sayılmaz (yükleme yine denetler).
        if remaining() <= 0:
            return None, "", _ollama_mem(item, None, spec)
        show, _ = _ollama_show(spec.base_url, name, remaining())
        cloud = _ollama_cloud_reason(name, item, show)
        if cloud:
            return cloud, "", None
        caps = show.get("capabilities") if isinstance(show, dict) else None
        if isinstance(caps, list) and "completion" not in [str(c) for c in caps]:
            return MSG_EMBEDDING_MODEL, "", None
        return None, "", _ollama_mem(item, show, spec)
    kind = _server_kind(spec)
    data, err, detail = _fetch_detail(http_util.url_join(spec.base_url, "/models"),
                                      http_timeout)
    ids = _openai_ids(data)
    if ids is None:
        if kind == SOURCE_LMSTUDIO:
            _reachable, message = _no_list_message(
                SOURCE_STATUS_LABELS[SOURCE_LMSTUDIO], spec.base_url, err, detail,
                MSG_LMSTUDIO_DOWN.format(url=spec.base_url))
            return message, "", None
        silent = err in (_FETCH_DOWN, _FETCH_TIMEOUT)
        template = MSG_OPENAI_DOWN if silent else MSG_NOT_OPENAI
        return template.format(url=spec.base_url), "", None
    if spec.model not in ids:
        sources = load_sources() if kind == SOURCE_OPENAI else None
        return _server_miss(kind, spec, sources), "", None
    if kind == SOURCE_LMSTUDIO and remaining() > 0:
        # Keşifle aynı hüküm: LM Studio yerel REST'i gömme modelini bildirir.
        rest = _lmstudio_rest(http_util.url_root(spec.base_url), remaining())
        info = rest.get(spec.model, {})
        if info.get("embedding"):
            return MSG_EMBEDDING_MODEL, "", None
        size = _positive_size(info.get("size"))
        if size:
            return None, "", _server_mem(size, info.get("loaded"), spec,
                                         loaded_context=info.get("loaded_context"))
    return None, "", None


def check_engine(engine_id: str, *,
                 http_timeout: float = ENGINE_CHECK_HTTP_TIMEOUT_S) -> EngineCheck:
    """Kimliği modeli YÜKLEMEDEN denetle (TASARIM D.3).

    Kimlik geçersizse / motor bilinmiyorsa / klasör-dosya yok ya da eksikse /
    mimari desteklenmiyorsa / llama-cpp-python yoksa / sunucu kapalıysa /
    model sunucuda yoksa ``ok=False`` ve Türkçe ``reason`` (+ varsa ``hint``).
    """
    try:
        spec = registry.resolve_spec(engine_id)
    except ValueError as exc:
        return EngineCheck(engine_id=str(engine_id), ok=False, label=str(engine_id),
                           reason=str(exc))
    label = engine_label(spec)
    # Donanım kapısı (KARARLAR 17): bu Mac referans 7B'yi çalıştıramıyorsa hiçbir
    # model denenmez (ağa da gidilmez).
    gate = hardware.general_gate()
    if not gate.allowed:
        return EngineCheck(engine_id=engine_id, ok=False, label=label, reason=gate.reason)
    reason, hint, mem = _spec_problem(spec, http_timeout)
    if reason:
        return EngineCheck(engine_id=engine_id, ok=False, label=label, reason=reason, hint=hint)
    fit = _mem_fit(mem, gate.hardware)
    if hardware.blocks(fit):
        return EngineCheck(engine_id=engine_id, ok=False, label=label,
                           reason=hardware.too_big_message(fit))
    # MT-12: sunucu modelinin kaba tahmini "sığmayabilir" diyorsa engel değil uyarı.
    warning = hardware.fit_warning(fit)
    if warning:
        return WarnedEngineCheck(engine_id=engine_id, ok=True, label=label, warning=warning)
    return EngineCheck(engine_id=engine_id, ok=True, label=label)


def _local_mem(spec: EngineSpec) -> Optional[_MemInput]:
    """Süreç içi motorun (MLX, GGUF) bellek girdileri, AĞSIZ; dosya yoksa None."""
    root = Path(os.path.abspath(str(registry.MODELS_DIR)))
    if spec.backend == BACKEND_MLX:
        directory = _mlx_dir_for(spec, root)
        if directory is None:
            return None
        info = inspect_mlx_dir(directory)
        return _mlx_mem(info, spec, spec.format)
    if spec.backend == BACKEND_LLAMACPP and spec.path:
        path = Path(spec.path)
        if not path.is_file():
            return None
        meta = read_gguf_meta(path)
        if meta.error:
            return None
        return _MemInput(_gguf_size(path), meta.kv, hardware.MODE_CONTEXT, _gguf_context(spec))
    return None


def local_memory_fit(engine_id: str) -> Optional[hardware.MemoryFit]:
    """Süreç içi motorun (MLX, GGUF) bellek hükmü, ağsız; hesaplanamazsa None.

    translate_book toplu iş boyutunu buradan alır (KARARLAR 17a: dar bellekte
    toplu iş küçülür). Sunucu motorlarında None (bellek sunucunundur, istekler
    sıralıdır).
    """
    try:
        spec = registry.resolve_spec(engine_id)
    except ValueError:
        return None
    return _mem_fit(_local_mem(spec))


def hardware_check(engine_id: str, *, hint: str = "") -> EngineCheck:
    """Çıkarımdan ÖNCE, ağsız donanım denetimi (her motor, varsayılan dahil).

    Genel kapı kapalıysa ``ok=False`` + genel ileti (+ ``hint``); süreç içi
    motorun dosyası varsa ve toplu iş 1 ile bile sığmıyorsa "bu Mac'e sığmaz".
    Model yoksa / kimlik geçersizse burada hüküm verilmez (``check_engine``
    ve ön kontrol söyler); sunucu motorlarının boyutu ``check_engine``'de.
    """
    try:
        spec = registry.resolve_spec(engine_id)
    except ValueError:
        return EngineCheck(engine_id=str(engine_id), ok=True, label=str(engine_id))
    label = engine_label(spec)
    gate = hardware.general_gate()
    if not gate.allowed:
        return EngineCheck(engine_id=engine_id, ok=False, label=label, reason=gate.reason,
                           hint=hint)
    fit = _mem_fit(_local_mem(spec), gate.hardware)
    if hardware.blocks(fit):
        return EngineCheck(engine_id=engine_id, ok=False, label=label,
                           reason=hardware.too_big_message(fit))
    return EngineCheck(engine_id=engine_id, ok=True, label=label)


# ---------------------------------------------------------------------------
# Komut satırı
# ---------------------------------------------------------------------------
# G2b-1 alanları değer taşımıyorsa (None) JSON'a yazılmaz: TASARIM D.4 biçimi aynen.
_OPTIONAL_JSON_FIELDS = ("thinking", "thinking_off", "loaded_context")


def _model_json(model: DiscoveredModel) -> Dict[str, Any]:
    data = asdict(model)
    for key in _OPTIONAL_JSON_FIELDS:
        if data.get(key) is None:
            data.pop(key, None)
    return data


def report_to_json(report: DiscoveryReport) -> Dict[str, Any]:
    """TASARIM D.4 biçimi (``scanned_at`` yerel saatle ISO)."""
    return {
        "scanned_at": datetime.fromtimestamp(report.scanned_at).isoformat(timespec="seconds"),
        "models": [_model_json(m) for m in report.models],
        "sources": [asdict(s) for s in report.sources],
    }


def _format_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return ""
    for unit, size in (("GB", 10 ** 9), ("MB", 10 ** 6), ("KB", 10 ** 3)):
        if num_bytes >= size:
            return f"{num_bytes / size:.1f}".replace(".", ",") + f" {unit}"
    return f"{num_bytes} B"


_SOURCE_TABLE_LABELS = {
    SOURCE_BUILTIN: "yerleşik", SOURCE_USER: "kayıtlı", SOURCE_MLX: "MLX", SOURCE_GGUF: "GGUF",
    SOURCE_OLLAMA: "Ollama", SOURCE_LMSTUDIO: "LM Studio", SOURCE_OPENAI: "OpenAI uyumlu",
}


def _memory_line(fit: hardware.MemoryFit) -> str:
    """Tablodaki bellek satırı (yalnız gerçek ölçüm + kaynaklı varsayımdan)."""
    if fit.verdict == hardware.VERDICT_LOADED:
        # MT-12: sunucu modeli yüklü tutuyor; tahmin gösterilir, hüküm verilmez.
        return (f"bellek: tahmini ~{hardware.format_gb(fit.minimum_bytes, round_up=True)} GB"
                f" · {hardware.MSG_HW_LOADED_NOTE}")
    if fit.fits is None:
        return (f"bellek: gerekli ~{hardware.format_gb(fit.minimum_bytes, round_up=True)} GB"
                " (bu Mac ölçülemedi, hüküm yok)")
    if not fit.fits:
        return "bellek: " + hardware.too_big_label(fit)
    # F4-13: sığan modelde gereken, eldekinin gösteriminden büyük görünmez.
    text = (f"bellek: gerekli ~{hardware.format_fit_gb(fit.required_bytes, fit.available_bytes)}"
            " GB")
    if fit.mode == hardware.MODE_BATCHED:
        text += f" (toplu iş {fit.batch_size})"
    return text + (f" · kullanılabilir {hardware.format_gb(fit.available_bytes, round_up=False)}"
                   " GB · sığar")


def format_report(report: DiscoveryReport) -> str:
    """Türkçe düz metin tablo (``--json`` olmadan)."""
    when = datetime.fromtimestamp(report.scanned_at).strftime("%H:%M:%S")
    gate = hardware.general_gate()
    lines = [f"Yerel model taraması ({when})", "",
             f"Donanım: {hardware.hardware_caption(gate.hardware)}"]
    if gate.warning:
        lines.append(f"  {gate.warning}")
    if not gate.allowed:
        lines.append(f"  {gate.reason}")
    lines += ["", "Kaynaklar:"]
    for status in report.sources:
        lines.append(f"  {status.label}: {status.message}")
    lines += ["", "Modeller:"]
    if not report.models:
        lines.append("  (yok)")
    for model in report.models:
        extras = _join_detail(model.detail, _format_size(model.size_bytes))
        head = f"  [{_SOURCE_TABLE_LABELS.get(model.source, model.source)}] {model.engine_id}"
        if model.label and model.label != model.engine_id:
            head += f" — {model.label}"
        if extras:
            head += f" · {extras}"
        lines.append(head)
        lines.append(f"      {'hazır' if model.usable else 'kullanılamaz: ' + model.reason}")
        fit = getattr(model, "memory", None)
        if isinstance(fit, hardware.MemoryFit):
            lines.append(f"      {_memory_line(fit)}")
    lines += ["", "Kullanım: python translate_book.py kitap.pdf --mt-engine <kimlik>"]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m core.mt_engines.discovery",
        description="Bu bilgisayardaki yerel çeviri modellerini tarar: models/ klasöründeki "
                    "MLX klasörleri ve GGUF dosyaları, açık Ollama / LM Studio sunucuları. "
                    "Hiçbir model yüklenmez.")
    parser.add_argument("--json", action="store_true", help="sonucu JSON olarak yaz")
    parser.add_argument("--sources", metavar="DOSYA", default=None,
                        help=f"config/{MT_SOURCES_FILENAME} yerine okunacak sunucu adresleri "
                             "dosyası")
    args = parser.parse_args(argv)
    report = discover_all(sources_path=Path(args.sources) if args.sources else None)
    if args.json:
        sys.stdout.write(json.dumps(report_to_json(report), ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(format_report(report) + "\n")
    return 0


if __name__ == "__main__":
    # Kanonik modül üzerinden koş (python -m ile __main__ ayrı bir kopya olur;
    # registry'nin içe aktardığı discovery ile aynı nesne kullanılsın).
    from core.mt_engines import discovery as _discovery
    sys.exit(_discovery.main())
