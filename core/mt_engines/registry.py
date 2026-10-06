"""Motor kayıt defteri — kimlik -> EngineSpec + format sınıfı çözümü.

Üç kaynak birleşir:

1. ``BUILTIN_SPECS``: kodla gelen motorlar (şimdilik hymt-7b).
2. ``config/mt_engines.json``: kullanıcının eklediği adlı motorlar. Dosya
   yoksa sorun değil; bozuk/eksik girdiler pipeline'ı ASLA düşürmez — uyarı
   loglanır ve girdi atlanır. Builtin bir ad kullanıcı dosyasıyla EZİLEMEZ
   (yanlışlıkla varsayılan motoru bozmayı önler). Anahtarı ``_`` ile
   başlayan girdiler yorumdur, sessizce atlanır.
3. Önekli kimlikler (2026-09-25, TASARIM C): kayıt gerektirmeden
   ``mlx:<klasör>``, ``gguf:<dosya>``, ``ollama:<model>``,
   ``lmstudio:<model>``, ``openai:<adres>|<model>``. Önek arka ucu belirtir;
   adlar ':' içeremediği için çakışma olmaz. Bu spec'lerin ``source``'u
   ``"adhoc"``, ``name``'i kimliğin kendisidir.

Yerellik ve yol kuralları (KARARLAR 1-2): dosya kökü YALNIZ ``MODELS_DIR``'dir
(``models/``; başka yerdeki model oraya sembolik bağla konur). Sunucu
adresleri yalnız bu bilgisayar (loopback); ``lmstudio:``/``ollama:``
adresleri ``config/mt_sources.json``'dan (``discovery.load_sources``) gelir.

``MODELS_DIR`` / ``USER_SPECS_PATH`` çağrı anında modül özniteliği olarak
okunur (test yalıtımı bunları yamalar; bkz. contracts.py docstring'i).

Kullanım::

    from core.mt_engines import available_engines, create_engine
    engine = create_engine("hymt-7b")
    engine = create_engine("ollama:qwen3:8b")
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from config.defaults import MT_HTTP_MAX_PARALLEL_LIMIT
# Model kökü ve kullanıcı motor kayıtları veri kökünden türer (TASARIM F1):
# KITAPCEVIR_VERI_KOKU varsa oradan, yoksa proje kökünden (eski yollar).
from config.paths import ayar_koku, model_koku

from .base import EngineSpec, MTEngine
from .chat import ChatTemplateEngine
from .contracts import (
    BACKEND_LABELS,
    BACKEND_LLAMACPP,
    BACKEND_MLX,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    BACKENDS,
    ENGINE_NAME_RE,
    ENGINE_OPTION_KEYS,
    FLAVOR_GENERIC,
    FLAVOR_LMSTUDIO,
    FORMAT_CHAT,
    GGUF_SUFFIX,
    ID_PREFIX_SEP,
    ID_PREFIXES,
    MODEL_NAME_MAX_LEN,
    MSG_BAD_ID,
    MSG_PATH_NOT_ALLOWED,
    MSG_UNKNOWN_ENGINE,
    OPENAI_ID_SEP,
    PREFIX_GGUF,
    PREFIX_LMSTUDIO,
    PREFIX_MLX,
    PREFIX_OLLAMA,
    PREFIX_OPENAI,
    SPEC_SOURCE_ADHOC,
    SPEC_SOURCE_BUILTIN,
    SPEC_SOURCE_USER,
    SOURCE_BUILTIN,
    SOURCE_GGUF,
    SOURCE_LMSTUDIO,
    SOURCE_OPENAI,
    SOURCE_USER,
    BackendUnavailable,
    guess_format,
)
from .hymt import HYMTFormatEngine

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# İçe aktarma anında veri kökünden çözülür (yönetici değişkeni süreç
# başlamadan kurar); testler bu adları yamalar.
USER_SPECS_PATH = ayar_koku(PROJECT_ROOT) / "mt_engines.json"
MODELS_DIR = model_koku(PROJECT_ROOT)

# format adı -> strateji sınıfı. Yeni bir prompt BİÇİMİ gerekirse (nadir)
# buraya küçük bir sınıf eklenir; yeni bir MODEL için gerekmez (spec yeter).
ENGINE_FORMATS: Dict[str, Type[MTEngine]] = {
    "hymt": HYMTFormatEngine,
    "chat": ChatTemplateEngine,
}

BUILTIN_SPECS: Dict[str, EngineSpec] = {
    "hymt-7b": EngineSpec(
        name="hymt-7b",
        format="hymt",
        model_id="tencent/HY-MT1.5-7B",
        local_dir="hymt-7b-mlx",
        description="HY-MT1.5 7B — WMT25 şampiyonu (varsayılan)",
        source="builtin",
    ),
}

# --mt-engine verilmediğinde kullanılan motor — TEK kaynak: translate_book.py
# (CLI varsayılanı, run_pipeline/_preflight_check imzaları, run_batch/main
# yedekleri) ve ui/runner.local_model_dir buradan okur; arayüz translate_book'u
# --mt-engine vermeden başlattığı için arayüzün motoru da budur.
# Değiştirmeden önce core/translator.py, latex_pipeline/translator.py ve
# retranslator.py'deki motor varsayılanlarının da bu sabite bağlandığını
# doğrula (denetim 24 Eyl 2026, Mercek 3 #4: 24 Eyl itibarıyla literal).
DEFAULT_ENGINE = "hymt-7b"

# local_dir yalnızca models/ altında TEK dizin adı olabilir (path traversal
# guard'ı: kullanıcı json'ı "../../.ssh" gibi bir yol veremez). Önekli
# kimliklerde ve 'path' alanında "tek segment" denetimi de budur.
_LOCAL_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# config/mt_engines.json girdisinde tanınan alanlar (diğerleri uyarı + yok sayılır)
_USER_SPEC_KEYS = frozenset({
    "format", "model_id", "local_dir", "description", "languages", "strip_tokens",
    "backend", "model", "base_url", "path", "flavor", "options", "extra_body",
    "think", "chat_template_kwargs", "system_prompt", "raw",
})
# options değer türleri (contracts.ENGINE_OPTION_KEYS ile aynı küme)
_INT_OPTIONS = frozenset({"num_ctx", "n_ctx", "max_parallel", "n_gpu_layers", "seed"})
_NUMBER_OPTIONS = frozenset({"temperature", "repeat_penalty", "request_timeout_s"})
_STR_OR_INT_OPTIONS = frozenset({"keep_alive"})
_POSITIVE_OPTIONS = frozenset({"num_ctx", "n_ctx", "max_parallel", "request_timeout_s"})
_NOSYNC_SUFFIX = ".nosync"


# ---------------------------------------------------------------------------
# Küçük doğrulayıcılar (keşif ve ön kontrol de kullanır)
# ---------------------------------------------------------------------------
def _is_single_segment(value: str) -> bool:
    """models/ altında tek ad mı ('.'/'..' hariç; Reviewer R5)."""
    return bool(_LOCAL_DIR_RE.match(value or "")) and value not in (".", "..")


def _has_control_chars(value: str) -> bool:
    return any(unicodedata.category(ch).startswith("C") for ch in value)


def model_name_problem(model: str) -> Optional[str]:
    """Sunucu model adı kimlikte kullanılabilir mi? Sorun yoksa None, varsa Türkçe neden.

    Kural (TASARIM C.1): 1-200 görünür karakter; boşluk ve denetim/biçim
    karakteri yok. İlk ':'den sonrası AYNEN model adıdır (``qwen3:8b``).
    """
    if not isinstance(model, str) or not model:
        return "model adı boş"
    if len(model) > MODEL_NAME_MAX_LEN:
        return f"model adı {MODEL_NAME_MAX_LEN} karakterden uzun"
    if any(ch.isspace() for ch in model) or _has_control_chars(model):
        return "model adı boşluk ya da denetim karakteri içeremez"
    return None


def _bad_id(engine_id: str, why: str) -> ValueError:
    return ValueError(MSG_BAD_ID.format(engine_id=engine_id, why=why))


def _is_strictly_inside(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    return str(rel) not in ("", ".")


def _allowed_roots() -> List[Path]:
    # Tek kaynak discovery.allowed_roots (fonksiyon içi içe aktarma: discovery
    # bu modülü içe aktarır; döngü olmasın).
    from . import discovery
    return discovery.allowed_roots()


def _models_dir_display() -> str:
    return str(MODELS_DIR)


def resolve_allowed_path(raw: str, *, engine_id: str) -> Path:
    """Mutlak model yolunu izinli köke göre doğrula; normalize (sözcüksel) yolu döner.

    İzin: yol ``models/`` altında (sözcüksel; ``models/`` içindeki sembolik
    bağ bu yüzden izinlidir — KARARLAR 1) ya da gerçek yolu (realpath)
    ``models/``'un gerçek yolunun altında. ``..`` bileşeni hiç kabul edilmez
    (sözcüksel denetimi atlatamasın). Aksi ``ValueError`` (Türkçe ileti).
    """
    if _has_control_chars(raw):
        raise _bad_id(engine_id, "yol denetim karakteri içeremez")
    path = Path(raw)
    if not path.is_absolute():
        raise _bad_id(engine_id, "yol mutlak olmalı ya da models/ altında tek ad olmalı")
    if ".." in path.parts:
        raise _bad_id(engine_id, "yol '..' içeremez")
    lexical = Path(os.path.normpath(str(path)))
    roots = _allowed_roots()
    if any(_is_strictly_inside(lexical, root) for root in roots):
        return lexical
    real = Path(os.path.realpath(str(lexical)))
    if any(_is_strictly_inside(real, root) for root in roots):
        return lexical
    raise ValueError(MSG_PATH_NOT_ALLOWED.format(path=raw, roots=_models_dir_display()))


def _local_url(url: str) -> str:
    """check_local_url; adres reddedilirse ValueError (Türkçe ileti aynen)."""
    from .http_util import check_local_url
    try:
        return check_local_url(url)
    except BackendUnavailable as exc:
        raise ValueError(str(exc)) from None


def openai_base_url(url: str) -> str:
    """OpenAI uyumlu taban adres: loopback denetimi + yol boşsa ``/v1`` eklenir.

    ``BackendUnavailable`` (MSG_BAD_URL / MSG_REMOTE_REFUSED) fırlatabilir.
    """
    from urllib.parse import urlsplit, urlunsplit

    from .http_util import check_local_url
    checked = check_local_url(url)
    parts = urlsplit(checked)
    if not parts.path:
        return urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, ""))
    return checked


def _sources():
    from . import discovery
    return discovery.load_sources()   # modül özniteliği: yalıtım yamalayabilir


# ---------------------------------------------------------------------------
# Önekli kimlik ayrıştırma (TASARIM C.1)
# ---------------------------------------------------------------------------
def _parse_mlx(engine_id: str, value: str) -> EngineSpec:
    if _is_single_segment(value):
        if value.lower().endswith(GGUF_SUFFIX):
            raise _bad_id(engine_id, "MLX kimliği klasör ister; .gguf dosyası için gguf: öneki")
        folder = value
        label = folder[:-len(_NOSYNC_SUFFIX)] if folder.endswith(_NOSYNC_SUFFIX) else folder
        return EngineSpec(
            name=engine_id, format=guess_format(folder), local_dir=folder,
            description=f"{label} · {BACKEND_LABELS[BACKEND_MLX]}",
            source=SPEC_SOURCE_ADHOC, backend=BACKEND_MLX,
        )
    if value.startswith("/"):
        path = resolve_allowed_path(value, engine_id=engine_id)
        label = path.name[:-len(_NOSYNC_SUFFIX)] if path.name.endswith(_NOSYNC_SUFFIX) \
            else path.name
        return EngineSpec(
            name=engine_id, format=guess_format(path.name), path=str(path),
            description=f"{label} · {BACKEND_LABELS[BACKEND_MLX]}",
            source=SPEC_SOURCE_ADHOC, backend=BACKEND_MLX,
        )
    raise _bad_id(engine_id, "models/ altında tek klasör adı ya da models/ içinde mutlak yol olmalı")


def _gguf_path(engine_id: str, value: str) -> Path:
    if _is_single_segment(value):
        path = Path(MODELS_DIR) / value
    elif value.startswith("/"):
        path = resolve_allowed_path(value, engine_id=engine_id)
    else:
        raise _bad_id(engine_id, "models/ altında tek dosya adı ya da models/ içinde mutlak yol olmalı")
    if not path.name.lower().endswith(GGUF_SUFFIX):
        raise _bad_id(engine_id, "dosya .gguf ile bitmeli")
    return path


def _parse_gguf(engine_id: str, value: str) -> EngineSpec:
    path = _gguf_path(engine_id, value)
    return EngineSpec(
        name=engine_id, format=guess_format(path.name), path=str(path),
        description=f"{path.name} · {BACKEND_LABELS[BACKEND_LLAMACPP]}",
        source=SPEC_SOURCE_ADHOC, backend=BACKEND_LLAMACPP,
    )


def _checked_model(engine_id: str, model: str) -> str:
    problem = model_name_problem(model)
    if problem:
        raise _bad_id(engine_id, problem)
    return model


def _parse_ollama(engine_id: str, value: str) -> EngineSpec:
    model = _checked_model(engine_id, value)
    return EngineSpec(
        name=engine_id, format=FORMAT_CHAT, model=model, base_url=_sources().ollama_url,
        description=f"{model} · {BACKEND_LABELS[BACKEND_OLLAMA]}",
        source=SPEC_SOURCE_ADHOC, backend=BACKEND_OLLAMA,
    )


def _parse_lmstudio(engine_id: str, value: str) -> EngineSpec:
    model = _checked_model(engine_id, value)
    return EngineSpec(
        name=engine_id, format=FORMAT_CHAT, model=model, base_url=_sources().lmstudio_url,
        flavor=FLAVOR_LMSTUDIO, description=f"{model} · LM Studio",
        source=SPEC_SOURCE_ADHOC, backend=BACKEND_OPENAI,
    )


def _parse_openai(engine_id: str, value: str) -> EngineSpec:
    address, sep, model = value.partition(OPENAI_ID_SEP)
    if not sep:
        raise _bad_id(engine_id, f"biçim openai:<adres>{OPENAI_ID_SEP}<model> olmalı "
                                 f"('{OPENAI_ID_SEP}' ayracı yok)")
    if not address:
        raise _bad_id(engine_id, "sunucu adresi boş")
    try:
        base = openai_base_url(address)
    except BackendUnavailable as exc:
        raise ValueError(str(exc)) from None
    model = _checked_model(engine_id, model)
    return EngineSpec(
        name=engine_id, format=FORMAT_CHAT, model=model, base_url=base,
        flavor=FLAVOR_GENERIC,
        description=f"{model} · {BACKEND_LABELS[BACKEND_OPENAI]}",
        source=SPEC_SOURCE_ADHOC, backend=BACKEND_OPENAI,
    )


_PREFIX_PARSERS = {
    PREFIX_MLX: _parse_mlx,
    PREFIX_GGUF: _parse_gguf,
    PREFIX_OLLAMA: _parse_ollama,
    PREFIX_LMSTUDIO: _parse_lmstudio,
    PREFIX_OPENAI: _parse_openai,
}


def parse_engine_id(engine_id: str) -> EngineSpec:
    """Önekli kimliği spec'e çevir (kayıt aranmaz). Geçersizse ``ValueError``."""
    prefix, _, value = engine_id.partition(ID_PREFIX_SEP)
    if prefix not in ID_PREFIXES:
        raise ValueError(_unknown_message(engine_id, None))
    if not value:
        raise _bad_id(engine_id, f"'{prefix}{ID_PREFIX_SEP}' sonrası boş")
    return _PREFIX_PARSERS[prefix](engine_id, value)


# ---------------------------------------------------------------------------
# config/mt_engines.json
# ---------------------------------------------------------------------------
def _parse_options(name: str, raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("mt_engines.json: '%s' options sözlük değil, yok sayıldı", name)
        return {}
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in ENGINE_OPTION_KEYS:
            logger.warning("mt_engines.json: '%s' bilinmeyen seçenek '%s' (geçerli: %s), "
                           "yok sayıldı", name, key, ", ".join(sorted(ENGINE_OPTION_KEYS)))
            continue
        is_int = isinstance(value, int) and not isinstance(value, bool)
        is_number = is_int or (isinstance(value, float) and not isinstance(value, bool))
        if key in _INT_OPTIONS and not is_int:
            ok = False
        elif key in _NUMBER_OPTIONS and not is_number:
            ok = False
        elif key in _STR_OR_INT_OPTIONS and not (is_int or (isinstance(value, str) and value)):
            ok = False
        else:
            ok = True
        if ok and key in _POSITIVE_OPTIONS and value <= 0:
            ok = False
        if not ok:
            logger.warning("mt_engines.json: '%s' seçeneği '%s' geçersiz değer %r, yok sayıldı",
                           name, key, value)
            continue
        if key == "max_parallel" and value > MT_HTTP_MAX_PARALLEL_LIMIT:
            logger.warning("mt_engines.json: '%s' max_parallel %d > %d, %d ile sınırlandı",
                           name, value, MT_HTTP_MAX_PARALLEL_LIMIT, MT_HTTP_MAX_PARALLEL_LIMIT)
            value = MT_HTTP_MAX_PARALLEL_LIMIT
        out[key] = value
    return out


def _parse_user_path(name: str, raw_path: str, backend: str) -> Optional[str]:
    """'path' alanı: models/ altında tek ad ya da izinli kökte mutlak yol."""
    if _is_single_segment(raw_path):
        path = Path(MODELS_DIR) / raw_path
    else:
        try:
            path = resolve_allowed_path(raw_path, engine_id=name)
        except ValueError as exc:
            logger.warning("mt_engines.json: '%s' path geçersiz (%s), atlandı", name, exc)
            return None
    if backend == BACKEND_LLAMACPP and not path.name.lower().endswith(GGUF_SUFFIX):
        logger.warning("mt_engines.json: '%s' llamacpp path .gguf dosyası olmalı, atlandı", name)
        return None
    return str(path)


def _parse_user_spec(name: str, raw: object) -> Optional[EngineSpec]:
    """Tek kullanıcı girdisini doğrula; geçersizse None (uyarı loglanır)."""
    if isinstance(name, str) and name.startswith("_"):
        return None   # yorum girdisi ("_aciklama"): sessizce atlanır
    if not isinstance(raw, dict):
        logger.warning("mt_engines.json: '%s' girdisi sözlük değil, atlandı", name)
        return None
    if not isinstance(name, str) or not name.strip():
        logger.warning("mt_engines.json: boş motor adı, atlandı")
        return None
    name = name.strip()
    if ID_PREFIX_SEP in name:
        logger.warning("mt_engines.json: '%s' adı ':' içeremez (önekli kimliklerle "
                       "çakışır), atlandı", name)
        return None
    if not ENGINE_NAME_RE.match(name):
        logger.warning("mt_engines.json: '%s' geçersiz ad (harf, rakam, '.', '_', '-'; "
                       "en çok 64 karakter), atlandı", name)
        return None
    if name in BUILTIN_SPECS:
        logger.warning(
            "mt_engines.json: '%s' builtin motoru ezemez, atlandı", name)
        return None

    for key in raw:
        if isinstance(key, str) and key.startswith("_"):
            continue
        if key not in _USER_SPEC_KEYS:
            logger.warning("mt_engines.json: '%s' bilinmeyen alan '%s', yok sayıldı", name, key)

    backend = str(raw.get("backend", BACKEND_MLX)).strip() or BACKEND_MLX
    if backend not in BACKENDS:
        logger.warning("mt_engines.json: '%s' bilinmeyen backend '%s' (geçerli: %s), atlandı",
                       name, backend, ", ".join(BACKENDS))
        return None

    fmt = str(raw.get("format", "chat")).strip()
    if fmt not in ENGINE_FORMATS:
        logger.warning(
            "mt_engines.json: '%s' bilinmeyen format '%s' (geçerli: %s), atlandı",
            name, fmt, ", ".join(sorted(ENGINE_FORMATS)))
        return None

    local_dir = str(raw.get("local_dir", "")).strip()
    # Reviewer R5: '.'/'..' tek segment olarak regex'i geçiyordu
    # (models/.. = proje kökü) — açıkça reddedilir.
    if local_dir and not _is_single_segment(local_dir):
        logger.warning(
            "mt_engines.json: '%s' local_dir '%s' geçersiz (models/ altında "
            "tek dizin adı olmalı), atlandı", name, local_dir)
        return None

    path = ""
    raw_path = str(raw.get("path", "") or "").strip()
    if raw_path:
        if backend not in (BACKEND_MLX, BACKEND_LLAMACPP):
            logger.warning("mt_engines.json: '%s' path yalnız mlx/llamacpp için, yok sayıldı",
                           name)
        else:
            checked = _parse_user_path(name, raw_path, backend)
            if checked is None:
                return None
            path = checked
    if backend == BACKEND_MLX and not (local_dir or path):
        logger.warning("mt_engines.json: '%s' mlx motoru local_dir ya da path ister, atlandı",
                       name)
        return None
    if backend == BACKEND_LLAMACPP and not path:
        logger.warning("mt_engines.json: '%s' llamacpp motoru path (.gguf) ister, atlandı", name)
        return None

    model = ""
    base_url = ""
    flavor = str(raw.get("flavor", "") or "").strip()
    if flavor not in (FLAVOR_GENERIC, FLAVOR_LMSTUDIO):
        logger.warning("mt_engines.json: '%s' bilinmeyen flavor '%s', atlandı", name, flavor)
        return None
    if flavor and backend != BACKEND_OPENAI:
        logger.warning("mt_engines.json: '%s' flavor yalnız openai için, yok sayıldı", name)
        flavor = ""
    if backend in (BACKEND_OLLAMA, BACKEND_OPENAI):
        model = str(raw.get("model", "") or "")
        problem = model_name_problem(model)
        if problem:
            logger.warning("mt_engines.json: '%s' model alanı geçersiz (%s), atlandı",
                           name, problem)
            return None
        raw_url = str(raw.get("base_url", "") or "").strip()
        try:
            if backend == BACKEND_OLLAMA:
                base_url = _local_url(raw_url) if raw_url \
                    else _sources().ollama_url
            elif raw_url:
                base_url = openai_base_url(raw_url)
            elif flavor == FLAVOR_LMSTUDIO:
                base_url = _sources().lmstudio_url
            else:
                logger.warning("mt_engines.json: '%s' openai motoru base_url ister, atlandı",
                               name)
                return None
        except (ValueError, BackendUnavailable) as exc:
            logger.warning("mt_engines.json: '%s' base_url reddedildi (%s), atlandı", name, exc)
            return None
    elif raw.get("model") or raw.get("base_url"):
        logger.warning("mt_engines.json: '%s' model/base_url yalnız ollama/openai için, "
                       "yok sayıldı", name)

    languages = raw.get("languages", {})
    if not isinstance(languages, dict):
        languages = {}
    strip_tokens = raw.get("strip_tokens", [])
    if not isinstance(strip_tokens, list):
        strip_tokens = []

    extra_body = raw.get("extra_body", {})
    if not isinstance(extra_body, dict):
        logger.warning("mt_engines.json: '%s' extra_body sözlük değil, yok sayıldı", name)
        extra_body = {}
    if extra_body and backend != BACKEND_OPENAI:
        logger.warning("mt_engines.json: '%s' extra_body yalnız openai için, yok sayıldı", name)
        extra_body = {}

    think: Optional[bool] = False
    if "think" in raw:
        if raw["think"] is None or isinstance(raw["think"], bool):
            think = raw["think"]
        else:
            logger.warning("mt_engines.json: '%s' think true/false/null olmalı, yok sayıldı",
                           name)

    spec_kwargs: Dict[str, Any] = {}
    if "chat_template_kwargs" in raw:
        if isinstance(raw["chat_template_kwargs"], dict):
            spec_kwargs["chat_template_kwargs"] = dict(raw["chat_template_kwargs"])
        else:
            logger.warning("mt_engines.json: '%s' chat_template_kwargs sözlük değil, "
                           "yok sayıldı", name)

    flags: Dict[str, bool] = {}
    for key in ("system_prompt", "raw"):
        if key in raw:
            if isinstance(raw[key], bool):
                flags[key] = raw[key]
            else:
                logger.warning("mt_engines.json: '%s' %s true/false olmalı, yok sayıldı",
                               name, key)

    return EngineSpec(
        name=name,
        format=fmt,
        model_id=str(raw.get("model_id", "")).strip(),
        local_dir=local_dir,
        description=str(raw.get("description", "")).strip() or name,
        languages={str(k): str(v) for k, v in languages.items()},
        strip_tokens=[str(t) for t in strip_tokens if t],
        source=SPEC_SOURCE_USER,
        backend=backend,
        model=model,
        base_url=base_url,
        path=path,
        flavor=flavor,
        options=_parse_options(name, raw.get("options")),
        extra_body=dict(extra_body),
        think=think,
        system_prompt=flags.get("system_prompt", False),
        raw=flags.get("raw", False),
        **spec_kwargs,
    )


def load_user_specs(path: Optional[Path] = None) -> Dict[str, EngineSpec]:
    """config/mt_engines.json'daki kullanıcı motorlarını oku (fail-open)."""
    path = path or USER_SPECS_PATH
    if not path.exists():
        return {}
    try:
        # utf-8-sig (2026-09-29, F4-14): BOM'lu kaydedilen dosyada bütün
        # kullanıcı motorları sessizce kayboluyordu. UTF-8 olmayan dosya da
        # fail-open (uyarı + kayıt yok), istisna yukarı çıkmaz.
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("mt_engines.json okunamadı (%s) — kullanıcı motorları yok", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("mt_engines.json kök öğesi sözlük olmalı — atlandı")
        return {}

    specs: Dict[str, EngineSpec] = {}
    for name, raw in data.items():
        spec = _parse_user_spec(name, raw)
        if spec is not None:
            specs[spec.name] = spec
    return specs


def available_engines(user_specs_path: Optional[Path] = None) -> Dict[str, EngineSpec]:
    """Adlı motorlar: builtin + geçerli kullanıcı girdileri (önekli kimlikler DEĞİL)."""
    merged: Dict[str, EngineSpec] = dict(BUILTIN_SPECS)
    merged.update(load_user_specs(user_specs_path))
    return merged


def _unknown_message(engine_id: str, user_specs_path: Optional[Path]) -> str:
    names = ", ".join(sorted(available_engines(user_specs_path)))
    return MSG_UNKNOWN_ENGINE.format(engine_id=engine_id, names=names)


# ---------------------------------------------------------------------------
# Çözüm
# ---------------------------------------------------------------------------
def resolve_spec(engine_id: str, user_specs_path: Optional[Path] = None) -> EngineSpec:
    """Kimliği spec'e çöz: builtin -> kullanıcı kaydı -> önekli kimlik.

    Hata: ``ValueError`` (Türkçe; MSG_UNKNOWN_ENGINE / MSG_BAD_ID /
    MSG_BAD_URL / MSG_REMOTE_REFUSED / MSG_PATH_NOT_ALLOWED). Ağ ya da dosya
    varlığı denetlenmez (o ``discovery.check_engine``'in işi).
    """
    if not isinstance(engine_id, str) or not engine_id.strip():
        raise _bad_id(str(engine_id), "kimlik boş")
    builtin = BUILTIN_SPECS.get(engine_id)
    if builtin is not None:
        return builtin
    if ID_PREFIX_SEP not in engine_id:
        # Adlar ':' içeremez: kullanıcı kaydı önekli kimlikle çakışamaz.
        user = load_user_specs(user_specs_path).get(engine_id)
        if user is not None:
            return user
        raise ValueError(_unknown_message(engine_id, user_specs_path))
    prefix = engine_id.partition(ID_PREFIX_SEP)[0]
    if prefix not in ID_PREFIXES:
        raise ValueError(_unknown_message(engine_id, user_specs_path))
    return parse_engine_id(engine_id)


def create_engine(name: str,
                  user_specs_path: Optional[Path] = None) -> MTEngine:
    """Kimlikten motor örneği üret. Bilinmeyen/geçersiz kimlik -> ValueError (Türkçe)."""
    spec = resolve_spec(name, user_specs_path)
    engine_cls = ENGINE_FORMATS[spec.format]
    return engine_cls(spec)


def engine_source(spec: EngineSpec) -> str:
    """Spec'in keşif kaynağı (contracts.SOURCE_*): builtin/user ya da önekin kaynağı."""
    if spec.source == SPEC_SOURCE_BUILTIN:
        return SOURCE_BUILTIN
    if spec.source == SPEC_SOURCE_USER:
        return SOURCE_USER
    if spec.backend == BACKEND_OPENAI:
        return SOURCE_LMSTUDIO if spec.flavor == FLAVOR_LMSTUDIO else SOURCE_OPENAI
    if spec.backend == BACKEND_LLAMACPP:
        return SOURCE_GGUF
    return spec.backend   # SOURCE_MLX == BACKEND_MLX, SOURCE_OLLAMA == BACKEND_OLLAMA
