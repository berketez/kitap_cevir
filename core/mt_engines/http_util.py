"""Yerel model sunucuları için küçük HTTP/JSON istemcisi (yalnız stdlib).

Neden ayrı ve bu kadar dar (2026-09-25): Ollama / LM Studio / OpenAI uyumlu
sunucularla konuşan her kod (arka uçlar, keşif, ön kontrol) bu modülden
geçer. Böylece üç güvence tek yerde durur:

1. **Yalnız bu bilgisayar** (üretimde dış API yok): her istek adresi
   ``check_local_url`` ile loopback'e (127.0.0.0/8, ::1, localhost) sınırlanır;
   yönlendirme (3xx) de yalnız loopback'e izlenir. API anahtarı /
   ``Authorization`` başlığı gönderilmez. Yerel Ollama'nın isteği dışarı
   aktardığı bulut modelleri ``ollama_cloud_model`` ile tanınır (keşif, ön
   kontrol ve ``OllamaBackend.load`` aynı hükmü verir).
2. **Proxy yok sayılır**: ``ProxyHandler({})`` — ortamdaki HTTP(S)_PROXY ya
   da sistem proxy'si yerel isteği dışarı taşımaz.
3. **Hata eşlemesi tek biçim**: çağıran yalnız dört istisna görür
   (``HttpUnreachable``, ``HttpTimeout``, ``HttpStatusError`` ve onun alt
   sınıfı ``HttpInvalidJson``) + adres reddinde ``BackendUnavailable``.

``requests``, ``httpx``, ``openai`` paketleri KULLANILMAZ (requirements'ta
yok; üretimde dış API yok). Testler bu kodu gerçek soket üzerinden sahte sunucuyla
(tests/fakes/fake_llm_server.py) çalıştırır.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional, Tuple

from config.defaults import MT_HTTP_ERROR_BODY_MAX_CHARS, MT_HTTP_MAX_RESPONSE_BYTES

from .contracts import MSG_BAD_URL, MSG_REMOTE_REFUSED, BackendUnavailable

__all__ = [
    "HttpUnreachable",
    "HttpTimeout",
    "HttpStatusError",
    "HttpInvalidJson",
    "INVALID_JSON_BODY",
    "check_local_url",
    "is_loopback_host",
    "ollama_cloud_model",
    "ollama_remote_host",
    "OLLAMA_CLOUD_DEFAULT_HOST",
    "url_join",
    "url_root",
    "lmstudio_loaded_config",
    "get_json",
    "post_json",
]

# HttpInvalidJson.body'nin başı (çağıran "JSON değil" durumunu bu önekle de tanıyabilir)
INVALID_JSON_BODY = "geçersiz JSON"
_ALLOWED_SCHEMES = ("http", "https")
_USER_AGENT = "kitapcevir-yerel-model"


# ---------------------------------------------------------------------------
# İstisnalar
# ---------------------------------------------------------------------------
class HttpUnreachable(Exception):
    """Bağlantı kurulamadı ya da koptu (red, sıfırlama, yarım yanıt)."""

    def __init__(self, url: str, reason: str = ""):
        super().__init__(f"{url}: {reason}" if reason else url)
        self.url = url
        self.reason = reason


class HttpTimeout(Exception):
    """Bağlantı ya da yanıt ``timeout`` saniye içinde gelmedi."""

    def __init__(self, url: str, timeout: float):
        super().__init__(f"{url}: {timeout} sn")
        self.url = url
        self.timeout = timeout


class HttpStatusError(Exception):
    """Sunucu 2xx dışı durum döndü (ya da 2xx ama gövde JSON değil)."""

    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body
        self.url = url


class HttpInvalidJson(HttpStatusError):
    """Yanıt başarılı ama gövde geçerli JSON değil (``body`` INVALID_JSON_BODY ile başlar)."""


# ---------------------------------------------------------------------------
# Adres denetimi
# ---------------------------------------------------------------------------
def is_loopback_host(host: Optional[str]) -> bool:
    """'localhost', 127.0.0.0/8, ::1 (ve IPv4-eşlemeli ::ffff:127.x) ise True."""
    if not host:
        return False
    host = host.strip().rstrip(".").lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def check_local_url(url: str) -> str:
    """Adresi doğrula ve normalize et (sonda '/' yok).

    - Biçim bozuksa (şema http/https değil, host yok, geçersiz port, boşluk ya
      da denetim karakteri, kullanıcı:parola, parça '#') ->
      ``BackendUnavailable(MSG_BAD_URL)``.
    - Host loopback değilse (LAN, Tailscale, bulut, 0.0.0.0) ->
      ``BackendUnavailable(MSG_REMOTE_REFUSED)``.
    """
    if not isinstance(url, str):
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="adres metin değil"))
    raw = url.strip()
    if not raw:
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="adres boş"))
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="boşluk ya da denetim karakteri"))
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port   # geçersiz portta ValueError
    except ValueError:
        raise BackendUnavailable(MSG_BAD_URL.format(
            url=url, why="port ya da adres biçimi geçersiz")) from None
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="şema http ya da https olmalı"))
    if not parts.hostname:
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="sunucu adı yok"))
    if parts.username is not None or parts.password is not None:
        raise BackendUnavailable(MSG_BAD_URL.format(
            url=url, why="kullanıcı adı/parola desteklenmez"))
    if parts.fragment:
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="'#' parçası olamaz"))
    if port == 0:
        raise BackendUnavailable(MSG_BAD_URL.format(url=url, why="port 0 olamaz"))
    if not is_loopback_host(parts.hostname):
        raise BackendUnavailable(MSG_REMOTE_REFUSED.format(url=raw))
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


# ---------------------------------------------------------------------------
# Ollama bulut modeli (2026-09-29, F4-01; KARARLAR 2)
# ---------------------------------------------------------------------------
# Ollama 0.12+ bulut modellerini YEREL sunucu (127.0.0.1:11434) üzerinden
# ollama.com'a aktarır: adres denetimi (check_local_url) bunu göremez. Kaynak
# (28-29 Eyl 2026): ollama/ollama api/types.go — ListModelResponse ve
# ShowResponse ``RemoteModel`` (json "remote_model") ve ``RemoteHost``
# (json "remote_host") alanları; ad biçimi ``gpt-oss:120b-cloud`` (ollama.com
# blog, 19 Eyl 2025) ve ``gemma4:cloud`` (docs.ollama.com/cloud).
_OLLAMA_REMOTE_KEYS = ("remote_host", "remote_model")
_OLLAMA_CLOUD_SUFFIXES = ("-cloud", ":cloud")
_OLLAMA_LATEST_TAG = ":latest"
OLLAMA_CLOUD_DEFAULT_HOST = "ollama.com"


def ollama_remote_host(*metadata: Any) -> str:
    """Ollama üst verisindeki (``/api/tags`` öğesi, ``/api/show`` yanıtı) uzak
    adres ya da uzak model adı; yerel modelde "".

    Önce ``remote_host`` (adresin sunucu adı: "https://ollama.com:443" ->
    "ollama.com"), yoksa ``remote_model``.
    """
    for key in _OLLAMA_REMOTE_KEYS:
        for data in metadata:
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                value = value.strip()
                if key == "remote_host":
                    try:
                        host = urllib.parse.urlsplit(value).hostname
                    except ValueError:
                        host = None
                    return host or value
                return value
    return ""


def ollama_cloud_model(name: Any, *metadata: Any) -> bool:
    """Ollama modeli bulut modeli mi (yerel sunucu isteği bu bilgisayarın dışına aktarır)?

    Üst veride ``remote_host`` / ``remote_model`` doluysa ya da ad ``-cloud``
    / ``:cloud`` ile bitiyorsa (``:latest`` yok sayılır) True.
    """
    if any(isinstance(data, dict) and any(
            isinstance(data.get(key), str) and data.get(key).strip()
            for key in _OLLAMA_REMOTE_KEYS) for data in metadata):
        return True
    if not isinstance(name, str):
        return False
    base = name.strip().lower()
    if base.endswith(_OLLAMA_LATEST_TAG):
        base = base[:-len(_OLLAMA_LATEST_TAG)]
    return base.endswith(_OLLAMA_CLOUD_SUFFIXES)


def url_join(base: str, path: str) -> str:
    """``url_join("http://h:1/v1/", "/models")`` -> ``"http://h:1/v1/models"``."""
    return base.rstrip("/") + "/" + path.lstrip("/")


def url_root(url: str) -> str:
    """Şema + host[:port]: ``"http://127.0.0.1:1234/v1"`` -> ``"http://127.0.0.1:1234"``.

    LM Studio'nun yerel REST ucu (``/api/v1/models``) OpenAI taban adresinin
    (``/v1``) değil sunucu kökünün altındadır.
    """
    parts = urllib.parse.urlsplit(url.strip())
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _positive(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def lmstudio_loaded_config(entry: Any) -> Tuple[Optional[int], Optional[int]]:
    """LM Studio REST model öğesinden yüklü örneğin (bağlam, eşzamanlı tahmin) değerleri.

    G2b-1 (2026-10-05; MT-15). REST v1 (``/api/v1/models``):
    ``loaded_instances[0].config.context_length`` ve ``.parallel``
    (https://lmstudio.ai/docs/developer/rest/list); eski REST v0 öğesinde
    yalnız ``loaded_context_length``. Model yüklü değilse ya da alan yoksa
    (None, None). Keşif (bellek hükmü) ve arka uç (bağlam sınırı) AYNI kuralı
    kullanır.
    """
    if not isinstance(entry, dict):
        return None, None
    context = parallel = None
    instances = entry.get("loaded_instances")
    if isinstance(instances, list) and instances and isinstance(instances[0], dict):
        config = instances[0].get("config")
        if isinstance(config, dict):
            context = _positive(config.get("context_length"))
            parallel = _positive(config.get("parallel"))
    if context is None:
        context = _positive(entry.get("loaded_context_length"))
    return context, parallel


# ---------------------------------------------------------------------------
# İstek
# ---------------------------------------------------------------------------
class _LoopbackRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Yönlendirmeyi yalnız loopback hedefe izle (yerel sunucu dışarı taşıyamasın)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_local_url(newurl)   # uzak hedefte BackendUnavailable
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}): ortam/sistem proxy'si YOK SAYILIR.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       _LoopbackRedirectHandler())


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (socket.timeout, TimeoutError))


def _read_limited(resp) -> bytes:
    data = resp.read(MT_HTTP_MAX_RESPONSE_BYTES + 1)
    if len(data) > MT_HTTP_MAX_RESPONSE_BYTES:
        raise HttpStatusError(getattr(resp, "status", 0) or 0,
                              f"yanıt çok büyük (> {MT_HTTP_MAX_RESPONSE_BYTES} bayt)")
    return data


def _request_json(method: str, url: str, body: Optional[dict], timeout: float) -> Any:
    target = check_local_url(url)
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(target, data=data, headers=headers, method=method)
    try:
        with _build_opener().open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            raw = _read_limited(resp)
    except urllib.error.HTTPError as exc:
        try:
            err_raw = exc.read(MT_HTTP_ERROR_BODY_MAX_CHARS * 4) or b""
        except Exception:   # gövde okunamazsa durum kodu yine anlamlı
            err_raw = b""
        finally:
            exc.close()
        text = err_raw.decode("utf-8", "replace")[:MT_HTTP_ERROR_BODY_MAX_CHARS]
        raise HttpStatusError(exc.code, text, target) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, BaseException) and _is_timeout(reason):
            raise HttpTimeout(target, timeout) from None
        raise HttpUnreachable(target, str(reason)) from None
    except BackendUnavailable:
        raise   # uzak hedefe yönlendirme
    except (socket.timeout, TimeoutError):
        raise HttpTimeout(target, timeout) from None
    except (http.client.HTTPException, ConnectionError, OSError) as exc:
        raise HttpUnreachable(target, f"{type(exc).__name__}: {exc}") from None

    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        snippet = raw[:200].decode("utf-8", "replace")
        raise HttpInvalidJson(status, f"{INVALID_JSON_BODY}: {snippet}", target) from None


def get_json(url: str, timeout: float) -> Any:
    """GET + JSON. Boş gövde -> None. Hatalar: modül docstring'i."""
    return _request_json("GET", url, None, timeout)


def post_json(url: str, body: dict, timeout: float) -> Any:
    """POST (JSON gövde, UTF-8) + JSON yanıt. Boş gövde -> None."""
    return _request_json("POST", url, body, timeout)
