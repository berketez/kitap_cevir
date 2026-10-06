"""HTTP arka uçları: Ollama (yerel API) ve OpenAI uyumlu yerel sunucular (TASARIM B.5-B.6).

- ``OllamaBackend``: ``/api/chat`` (sohbet) ya da ``/api/generate`` +
  ``raw`` (HY-MT özel-token biçimi, deneysel; yalnız mt_engines.json
  kaydında ``"format": "hymt", "raw": true`` ile). Ollama'nın OpenAI uyumlu
  ``/v1`` ucu KULLANILMAZ: ``num_ctx`` ayarlatmaz, uzun segment sessizce
  kırpılır. ``num_ctx`` ``load()``'da belirlenir ve koşu boyunca SABİT kalır
  (farklı ``num_ctx`` Ollama'da modeli yeniden yükletir).
- ``OpenAIBackend``: ``{base}/chat/completions`` (sohbet) ya da
  ``{base}/completions`` (ham, deneysel). LM Studio (``flavor="lmstudio"``),
  llama.cpp server, Jan, vLLM.

Ortak kurallar:

- Yalnız bu bilgisayar: adres ``http_util.check_local_url``'den geçer
  (kurucuda); istekler proxy'siz, ``Authorization`` başlığı YOK.
- ``stream: false``; greedy (``temperature 0``, ``seed 0``); tekrar cezası
  1,0 (``options`` ile değişir). İstisna (2026-09-28, KARARLAR 16): düşünen ya
  da düşündüğü ölçülen modelde yanıt AKIŞ hâlinde okunur (Ollama NDJSON,
  OpenAI uyumlu SSE); düşünme bütçeyi aşınca bağlantı hemen kapatılır, akışın
  toplam süresi ``request_timeout`` ile sınırlıdır (sunucu düşünürken sessiz
  kalsa da). Düşünmeyen modelin istek gövdesi değişmez.
- Düşünme her yanıtta ölçülür: Ollama ``message.thinking`` / ``thinking``,
  OpenAI uyumlu ``reasoning_content`` / ``reasoning``, metin içi
  ``<think>`` ve harmony kanalları (``backends.ThinkingMeter``).
- Düşünme varsayılan kapalı: Ollama'da model "thinking" yeteneği bildiriyorsa
  ``think: false``; OpenAI uyumlularda ``chat_template_kwargs``
  (``{"enable_thinking": false}``). Sunucu bu isteğe bağlı alanlardan birini
  400 ile reddederse istek bir kez alansız yinelenir ve alan o nesne için bir
  daha gönderilmez (yapışkan; WARNING).
- G2b-1 (2026-10-05, RAPOR MT-14): LM Studio'da düşünme YALNIZ yerel REST
  ``POST /api/v1/chat`` ``reasoning: "off"`` ile resmî olarak kapatılır (OpenAI
  uyumlu uçta belgeli alan yok). Model REST ``capabilities.reasoning
  .allowed_options``'ta "off" bildiriyorsa ve kayıt ``think: false`` ise
  üretim bu uçtan yapılır (``store: false``); uç yoksa / ayar reddedilirse
  OpenAI uyumlu uca dönülür ve ``thinking_monitor`` modeli bu koşu için
  "kapatilamaz" sayar. Düşünme sınıfı ve kapatma doğrulaması:
  ``core/mt_engines/thinking.py``.
- G2b-2 (2026-10-05, KARARLAR 22): düşünerek çeviride her deneme
  ``GenerationRequest.think_budget`` (parçanın düşünme payı) ve
  ``time_limit_s`` (parçanın süre sınırı) ile gelir; akış ölçeri bu sınırla keser,
  istek token sınırı pay + çeviri sınırıdır. Pay dolup cevap gelmeyen parçanın
  TEK yeniden denemesi, LM Studio modeli REST ``allowed_options``'ta "low"
  bildiriyorsa yerel REST ``reasoning: "low"`` ile yapılır (uç ilk kullanımda
  bir kez yoklanır; yoksa aynı istekle).
- Hata tablosu (TASARIM B.6): bağlantı reddi ve 5xx -> ``DEFAULT_RETRY_COUNT``
  kez yeniden deneme (``DEFAULT_RETRY_BASE_DELAY_S·2^k``). Tükenince
  (2026-09-29, F4-05): bağlantı reddi ve sunucu düzeyinde geçici durum (502,
  503, 504, 408, 425, 429) -> bağlantı sayacı + ``BackendUnavailable``
  (devre açıldıysa ``MSG_SERVER_LOST``, açılmadıysa
  ``MSG_SERVER_RETRY_FAILED``); sunucu yanıt verdi ve parçaya özgü 5xx (500
  …) -> yalnız o öğenin hatası (``MSG_SERVER_ERROR``), ayrı sayaçla art arda
  ``MT_HTTP_SERVER_ERROR_MAX_ITEMS`` parçada devre açılır
  (``MSG_SERVER_FAILING``). Gövdesinde bellek hatası olan 5xx ve 404 (model
  yok) -> devre hemen açılır; bağlam aşımı, okuma zaman aşımı ve boş içerik
  -> yalnız o öğenin hatası.
- Bağlam (F4-03): üretim sınırı bağlamda kalan yere kısılır
  (``backends.context_budget``); kısılan sınıra dayanan çıktı öğe hatasıdır
  (kesik çeviri kabul edilmez).
- Ollama bulut modeli (F4-01; KARARLAR 2): ``load()`` reddeder
  (``MSG_OLLAMA_CLOUD_MODEL``); ``keep_alive`` sayı ya da süre metni olarak
  AYNEN gider (F4-06).
"""

from __future__ import annotations

import copy
import http.client
import json
import logging
import re
import socket
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple

from config.defaults import (
    LMSTUDIO_DEFAULT_URL,
    MT_CONTEXT_TOKENS,
    MT_HTTP_ERROR_BODY_MAX_CHARS,
    MT_HTTP_MAX_RESPONSE_BYTES,
    OLLAMA_DEFAULT_URL,
    OLLAMA_KEEP_ALIVE,
)

from . import backends as _b
from . import http_util
from .contracts import (
    BACKEND_LABELS,
    BACKEND_OLLAMA,
    BACKEND_OPENAI,
    FLAVOR_LMSTUDIO,
    FORMAT_HYMT,
    MSG_BAD_ID,
    MSG_CONTEXT_EXCEEDED,
    MSG_EMBEDDING_MODEL,
    MSG_EMPTY_OUTPUT,
    MSG_LMSTUDIO_DOWN,
    MSG_LMSTUDIO_NOT_LOADED,
    MSG_MODEL_NOT_ON_SERVER,
    MSG_NOT_OPENAI,
    MSG_OLLAMA_DOWN,
    MSG_OPENAI_DOWN,
    MSG_OUT_OF_MEMORY_TR,
    MSG_OLLAMA_CLOUD_MODEL,
    MSG_SERVER_FAILING,
    MSG_SERVER_LOST,
    MSG_SERVER_RETRY_FAILED,
    MSG_THINK_BUDGET,
    MSG_TIMEOUT,
    SOURCE_LMSTUDIO,
    SOURCE_STATUS_LABELS,
    THINKING_NONE,
    THINKING_OFF_LMSTUDIO_REST,
    THINKING_UNKNOWN,
    BackendUnavailable,
    GenerationRequest,
    GenerationResult,
)
from .thinking import (
    LMSTUDIO_REASONING_LOW,
    LMSTUDIO_REASONING_OFF,
    ThinkingProfile,
    lmstudio_profile,
    lmstudio_reasoning_options,
    ollama_profile,
)

logger = logging.getLogger(__name__)

__all__ = ["OllamaBackend", "OpenAIBackend", "WARMUP_TEXT"]

# Isınma isteğinin içeriği (OpenAI uyumlu uçta 1 token'lık istek; LM Studio'nun
# isteğe bağlı yüklemesi modeli burada belleğe alır).
WARMUP_TEXT = "Merhaba"

# Hata gövdesi sınıflandırması
_MEMORY_HINT_RE = re.compile(
    r"memory|\boom\b|failed\s+to\s+allocate|insufficient\s+(?:system\s+)?(?:ram|resources)"
    r"|not\s+enough\s+ram",
    re.IGNORECASE,
)
_NOT_FOUND_RE = re.compile(
    r"not\s+found|does\s+not\s+exist|no\s+such\s+model|unknown\s+model|model_not_found",
    re.IGNORECASE,
)
_CONTEXT_RE = re.compile(r"context", re.IGNORECASE)
_CONTEXT_DETAIL_RE = re.compile(r"token|exceed|length|size|overflow|window|limit",
                                re.IGNORECASE)
_CONTEXT_NUMBER_RE = re.compile(
    r"context\s+(?:length|size|window)[^\d]{0,40}(\d{2,7})", re.IGNORECASE)
_TRANSIENT_STATUSES = frozenset({408, 425, 429})
# Sunucu düzeyinde geçici durum (F4-05): yeniden denemeler tükenince bağlantı
# düzeyi başarısızlık sayılır. Bunların dışındaki 5xx (500 ...) parçaya özgü.
_SERVER_LEVEL_STATUSES = frozenset({502, 503, 504}) | _TRANSIENT_STATUSES
_AUTH_STATUSES = frozenset({401, 403})
_CONTEXT_STATUSES = frozenset({400, 413, 422})
_REJECT_STATUSES = frozenset({400, 422})
# extra_body bu alanları ezemez (yanıt biçimi ve istek kimliği bize ait)
_PROTECTED_BODY_KEYS = ("model", "messages", "prompt", "stream")

# Ollama /api/show "thinking.values": false izinli değilse (gpt-oss) sert
# kapatmada gönderilen en düşük belgeli düzey (docs.ollama.com/capabilities/thinking).
_OLLAMA_THINK_LOW = "low"
# llama.cpp server istek alanı: "none" düşünmeyi kapatır (tools/server/README.md).
_REASONING_EFFORT_OFF = "none"
# LM Studio yerel REST sohbet ucu (G2b-1, 2026-10-05; LM Studio 0.4.0+). Düşünme
# YALNIZ burada resmî olarak kapatılır (istek alanı reasoning: "off";
# https://lmstudio.ai/docs/developer/rest/chat). OpenAI uyumlu uçta belgeli
# düşünme alanı yok. Yanıt output[] öğe türleri ve akış olay türleri:
# https://lmstudio.ai/docs/developer/rest/streaming-events
_LMS_CHAT_PATH = "/api/v1/chat"
_LMS_OUTPUT_MESSAGE = "message"
_LMS_OUTPUT_REASONING = "reasoning"
_LMS_EVENT_REASONING = "reasoning.delta"
_LMS_EVENT_MESSAGE = "message.delta"
_LMS_EVENT_ERROR = "error"
_LMS_EVENT_END = "chat.end"
# Yerel REST'in reddettiği (yol yok / ayar desteklenmiyor) durumlar: kapatma
# yolu bu sunucuda yok sayılır, OpenAI uyumlu uca dönülür.
_LMS_REST_UNSUPPORTED_STATUSES = frozenset({404, 405}) | _REJECT_STATUSES
# Akışlı isteğin Accept başlığı (Ollama NDJSON, OpenAI uyumlu SSE)
_STREAM_ACCEPT = "application/x-ndjson, text/event-stream, application/json"
_SSE_DATA = "data:"
_SSE_DONE = "[DONE]"
# Akışsız JSON yanıtın medya türü (sunucu stream:true'yu yok saydıysa)
_JSON_MEDIA_TYPE = "application/json"
# Akış okumasında tek recv'de istenen en çok bayt (read1; süre her okumadan önce denetlenir)
_STREAM_READ_BYTES = 8192

_VERDICT_DROP = "drop"          # isteğe bağlı alan reddedildi: alansız yinele
_VERDICT_ITEM = "item"          # yalnız bu öğe başarısız
_VERDICT_FATAL = "fatal"        # arka uç kullanılamaz: devreyi aç
_VERDICT_RETRY = "retry"        # geçici: yeniden dene


def _error_text(body: str) -> str:
    """HTTP hata gövdesinden okunur ileti ({"error": "…"} / {"error": {"message": …}})."""
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return body or ""
    if isinstance(data, dict):
        err = data.get("error", data.get("message"))
        if isinstance(err, dict):
            err = err.get("message") or json.dumps(err, ensure_ascii=False)
        if isinstance(err, str) and err.strip():
            return err
    return body or ""


def _finish_reason(value: Any) -> Optional[str]:
    if value is None or value == "":
        return None
    return "length" if str(value).lower() == "length" else "stop"


def _find_int(data: Any, keys: Tuple[str, ...]) -> Optional[int]:
    """İç içe JSON'da verilen anahtarlardan ilk pozitif tamsayı."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in keys:
                    number = _b._positive_int(value)
                    if number is not None:
                        return number
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _prompt_chars(body: Dict[str, Any]) -> int:
    messages = body.get("messages")
    if isinstance(messages, list):
        return sum(len(m.get("content") or "") for m in messages if isinstance(m, dict))
    prompt = body.get("prompt")
    return len(prompt) if isinstance(prompt, str) else 0


def _requested_tokens(body: Dict[str, Any]) -> int:
    if "max_tokens" in body:
        return _b._positive_int(body.get("max_tokens")) or 0
    options = body.get("options")
    if isinstance(options, dict):
        return _b._positive_int(options.get("num_predict")) or 0
    return 0


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


_OLLAMA_LATEST = ":latest"
# Birimsiz tamsayı metni ("3600", "-1"): Ollama metni time.ParseDuration'a
# verir ve birimsiz sayıyı reddeder; sayı olarak gönderilir.
_INT_TEXT_RE = re.compile(r"^[+-]?\d+$")


def ollama_keep_alive(value: Any) -> Any:
    """Kayıttaki ``keep_alive``'ı Ollama'nın kabul ettiği biçime çevir (F4-06).

    Ollama ``keep_alive``'ı sayı (saniye; 0 = hemen boşalt, negatif = hep
    tut) ya da süre metni ("10m", "24h") olarak alır. Kaynak (29 Eyl 2026):
    ollama/ollama api/types.go ``Duration.UnmarshalJSON`` — ``float64`` saniye,
    ``string`` ``time.ParseDuration`` (birimsiz sayı metni hata verir).

    - None / "" -> ``OLLAMA_KEEP_ALIVE`` (varsayılan);
    - tamsayı (0 dahil) -> AYNEN sayı;
    - birimsiz tamsayı metni ("3600", "-1") -> sayı;
    - diğer metin ("10m") -> aynen.
    """
    if value is None or isinstance(value, bool):
        return OLLAMA_KEEP_ALIVE
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == value and abs(value) != float("inf"):
        return int(value) if value.is_integer() else value   # Ollama float64 saniye alır
    text = str(value).strip()
    if not text:
        return OLLAMA_KEEP_ALIVE
    if _INT_TEXT_RE.match(text):
        return int(text)
    return text


# ---------------------------------------------------------------------------
# Akışlı istek (yalnız düşünen model; KARARLAR 16, T4/T7)
# ---------------------------------------------------------------------------
def _set_deadline(sock: Any, deadline: float) -> None:
    """Soketin okuma zaman aşımını kalan süreye çek (toplam süre sınırı)."""
    if sock is not None:
        sock.settimeout(max(0.001, deadline - time.monotonic()))


def _hard_close(sock: Any) -> None:
    """Bağlantıyı HEMEN kapat: sunucu yazamaz, üretimi bırakır."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _media_type(resp: Any) -> str:
    """Yanıtın ``Content-Type`` medya türü (parametresiz, küçük harf); yoksa ""."""
    try:
        value = resp.getheader("Content-Type") or ""
    except Exception:   # sahte / eksik yanıt nesnesi
        return ""
    return value.split(";", 1)[0].strip().lower()


def stream_post(url: str, body: dict, timeout: float,
                on_line: Callable[[bytes], bool],
                on_json: Optional[Callable[[Any], None]] = None) -> None:
    """POST + satır satır akış okuma; ``on_line`` True dönerse bağlantı hemen kapatılır.

    ``timeout`` isteğin TOPLAM süresidir (bağlantı + bütün akış): her okumadan
    ÖNCE süre denetlenir ve soketin zaman aşımı kalan süreye çekilir; sunucu
    düşünürken hiç veri göndermese de, satır sonu göndermeden bayt bayt
    damlatsa da sınır işler (2026-09-29, F4-11: okuma ``readline`` yerine
    ``read1`` parçalarıyla). Güvenceler ``http_util`` ile aynı: adres
    ``check_local_url``'den geçer (yalnız bu bilgisayar), proxy kullanılmaz
    (``http.client`` doğrudan bağlanır), yönlendirme İZLENMEZ (3xx
    ``HttpStatusError``), yanıt ``MT_HTTP_MAX_RESPONSE_BYTES`` ile sınırlı.
    Hatalar YALNIZ ``http_util`` istisnalarıdır (HttpUnreachable, HttpTimeout,
    HttpStatusError / HttpInvalidJson): Content-Length'li ya da bağlantı
    kapanışlı yanıt sonlandırıcı satır olmadan bitse de ham ``OSError``
    sızmaz (F4-04; akışın bitişi ``resp.isclosed()`` ile tanınır, okuyucu
    "tamamlanmadan kapandı" der).

    ``on_json``: sunucu ``stream: true``'yu yok sayıp düz JSON (``Content-Type:
    application/json``) döndürürse gövde bütün okunur, JSON olarak ayrıştırılıp
    ``on_json``'a verilir (satır okuyucusu çağrılmaz). Verilmezse gövde
    satır satır okunur.
    """
    target = http_util.check_local_url(url)
    parts = urllib.parse.urlsplit(target)
    deadline = time.monotonic() + max(0.0, float(timeout))
    conn_cls = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Accept": _STREAM_ACCEPT, "User-Agent": http_util._USER_AGENT,
               "Content-Type": "application/json; charset=utf-8"}
    conn = conn_cls(parts.hostname, parts.port, timeout=max(0.001, float(timeout)))
    sock = None
    resp = None
    try:
        try:
            conn.request("POST", path, body=data, headers=headers)
            sock = conn.sock
            _set_deadline(sock, deadline)
            resp = conn.getresponse()
        except (socket.timeout, TimeoutError):
            raise http_util.HttpTimeout(target, timeout) from None
        except (http.client.HTTPException, OSError) as exc:
            raise http_util.HttpUnreachable(target, f"{type(exc).__name__}: {exc}") from None
        status = int(resp.status or 0)
        if not 200 <= status < 300:
            try:
                err_raw = resp.read(MT_HTTP_ERROR_BODY_MAX_CHARS * 4) or b""
            except (http.client.HTTPException, OSError):
                err_raw = b""
            text = err_raw.decode("utf-8", "replace")[:MT_HTTP_ERROR_BODY_MAX_CHARS]
            raise http_util.HttpStatusError(status, text, target)
        whole = on_json is not None and _media_type(resp) == _JSON_MEDIA_TYPE
        total = 0
        pending = b""
        while True:
            # Yanıt bitti (Content-Length'e varıldı ya da bağlantı kapandı):
            # http.client soketi kapatmıştır; soketle bir daha işlem yapılmaz.
            if resp.isclosed():
                break
            if time.monotonic() >= deadline:
                raise http_util.HttpTimeout(target, timeout)
            try:
                _set_deadline(sock, deadline)
                chunk = resp.read1(_STREAM_READ_BYTES)
            except (socket.timeout, TimeoutError):
                raise http_util.HttpTimeout(target, timeout) from None
            except (http.client.HTTPException, OSError, ValueError) as exc:
                if resp.isclosed():
                    break
                raise http_util.HttpUnreachable(target, f"{type(exc).__name__}: {exc}") from None
            if not chunk:
                break
            total += len(chunk)
            if total > MT_HTTP_MAX_RESPONSE_BYTES:
                raise http_util.HttpStatusError(
                    status, f"yanıt çok büyük (> {MT_HTTP_MAX_RESPONSE_BYTES} bayt)", target)
            pending += chunk
            if whole:
                continue
            while True:
                cut = pending.find(b"\n")
                if cut < 0:
                    break
                line, pending = pending[:cut].strip(), pending[cut + 1:]
                if line and on_line(line):
                    return
        if whole:
            try:
                data = json.loads(pending.decode("utf-8")) if pending.strip() else None
            except (UnicodeDecodeError, ValueError):
                snippet = pending[:200].decode("utf-8", "replace")
                raise http_util.HttpInvalidJson(
                    status, f"{http_util.INVALID_JSON_BODY}: {snippet}", target) from None
            on_json(data)
            return
        line = pending.strip()      # satır sonu olmadan biten son satır
        if line:
            on_line(line)
    finally:
        _hard_close(sock)
        if resp is not None:
            try:
                resp.close()
            except (http.client.HTTPException, OSError):
                pass
        conn.close()


class _StreamReader:
    """Akış satırlarını ``ThinkingMeter``'a besleyen ortak okuyucu durumu."""

    def __init__(self, meter: _b.ThinkingMeter):
        self.meter = meter
        self.finish: Optional[str] = None
        self.done = False               # akış olağan biçimde bitti
        self.error: Optional[str] = None    # sunucunun akış içi hata iletisi
        self.bad: Optional[str] = None      # biçim bozuk (JSON değil)
        self.counted_tokens: Optional[int] = None   # sunucunun üretim sayacı (varsa)
        # Sunucu stream:true'yu yok sayıp düz JSON döndürdüyse o yanıt (F4-04);
        # arka uç onu akışsız yanıt gibi ayrıştırır.
        self.whole: Any = None
        self.got_whole = False

    def __call__(self, line: bytes) -> bool:
        raise NotImplementedError

    def take_whole(self, data: Any) -> None:
        """``stream_post``'un ``on_json`` geri çağrısı: akışsız JSON yanıtı sakla."""
        self.whole = data
        self.got_whole = True


class _OllamaStreamReader(_StreamReader):
    """Ollama NDJSON akışı: her satır bir JSON nesnesi; son satırda ``done: true``."""

    def __init__(self, meter: _b.ThinkingMeter, raw: bool):
        super().__init__(meter)
        self.raw = raw

    def __call__(self, line: bytes) -> bool:
        try:
            obj = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.bad = "akış satırı JSON değil"
            return True
        if not isinstance(obj, dict):
            self.bad = "akış satırı JSON nesnesi değil"
            return True
        err = obj.get("error")
        if err:
            self.error = err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)
            return True
        if self.raw:
            thinking, content = obj.get("thinking"), obj.get("response")
        else:
            message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            thinking, content = message.get("thinking"), message.get("content")
        self.meter.add_thinking(_str_or_none(thinking))
        self.meter.add_content(_str_or_none(content))
        if self.meter.exceeded:
            return True
        if obj.get("done"):
            self.done = True
            self.finish = _finish_reason(obj.get("done_reason"))
            self.counted_tokens = _b._positive_int(obj.get("eval_count"))
            return True
        return False


class _SSEReader(_StreamReader):
    """OpenAI uyumlu SSE akışı: ``data: {…}`` olayları, sonda ``data: [DONE]``."""

    def __init__(self, meter: _b.ThinkingMeter, raw: bool):
        super().__init__(meter)
        self.raw = raw

    def __call__(self, line: bytes) -> bool:
        text = line.decode("utf-8", "replace")
        if not text.startswith(_SSE_DATA):
            return False            # yorum (":"), "event:", "id:" satırları
        payload = text[len(_SSE_DATA):].strip()
        if payload == _SSE_DONE:
            self.done = True
            return True
        try:
            obj = json.loads(payload)
        except ValueError:
            self.bad = "akış olayı JSON değil"
            return True
        if not isinstance(obj, dict):
            self.bad = "akış olayı JSON nesnesi değil"
            return True
        if obj.get("error"):
            self.error = _error_text(json.dumps(obj, ensure_ascii=False))
            return True
        choices = obj.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            choice = choices[0]
            if self.raw:
                self.meter.add_content(_str_or_none(choice.get("text")))
            else:
                delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                reasoning = _str_or_none(delta.get("reasoning_content"))
                if reasoning is None:
                    reasoning = _str_or_none(delta.get("reasoning"))
                self.meter.add_thinking(reasoning)
                self.meter.add_content(_str_or_none(delta.get("content")))
            if choice.get("finish_reason"):
                self.finish = _finish_reason(choice.get("finish_reason"))
                self.done = True
        if self.meter.exceeded:
            return True
        return self.done


class _LMStudioChatReader(_StreamReader):
    """LM Studio yerel REST ``POST /api/v1/chat`` SSE akışı (G2b-1, 2026-10-05).

    Olay satırları ``event: <tür>`` + ``data: {…, "type": "<tür>"}``; tür
    JSON'daki ``type``'tan okunur (``event:`` satırı atlanır). Belge:
    https://lmstudio.ai/docs/developer/rest/streaming-events —
    ``reasoning.delta`` (düşünme), ``message.delta`` (yanıt), ``error``,
    ``chat.end`` (``result.stats``). Diğer olaylar (model_load.*,
    prompt_processing.*, *.start/*.end) ölçüme girmez.
    """

    def __call__(self, line: bytes) -> bool:
        text = line.decode("utf-8", "replace")
        if not text.startswith(_SSE_DATA):
            return False            # "event:" ve yorum satırları
        payload = text[len(_SSE_DATA):].strip()
        try:
            obj = json.loads(payload)
        except ValueError:
            self.bad = "akış olayı JSON değil"
            return True
        if not isinstance(obj, dict):
            self.bad = "akış olayı JSON nesnesi değil"
            return True
        kind = obj.get("type")
        if kind == _LMS_EVENT_REASONING:
            self.meter.add_thinking(_str_or_none(obj.get("content")))
        elif kind == _LMS_EVENT_MESSAGE:
            self.meter.add_content(_str_or_none(obj.get("content")))
        elif kind == _LMS_EVENT_ERROR:
            err = obj.get("error")
            message = err.get("message") if isinstance(err, dict) else err
            self.error = (message if isinstance(message, str) and message.strip()
                          else json.dumps(obj, ensure_ascii=False))
            return True
        elif kind == _LMS_EVENT_END:
            self.done = True
            result = obj.get("result") if isinstance(obj.get("result"), dict) else {}
            stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
            self.counted_tokens = _b._positive_int(stats.get("total_output_tokens"))
            return True
        return bool(self.meter.exceeded)


class _HttpBackend(_b.MTBackend):
    """Ollama ve OpenAI uyumlu arka uçların ortak mekaniği."""

    server_label = ""
    optional_keys: Tuple[str, ...] = ()

    def __init__(self, spec, default_url: str):
        super().__init__(spec)
        raw_url = (getattr(spec, "base_url", "") or default_url or "").strip()
        if not raw_url:
            raise BackendUnavailable(MSG_BAD_ID.format(
                engine_id=getattr(spec, "name", ""), why="sunucu adresi yok"))
        # Yerellik (yalnız bu bilgisayar): uzak adres -> MSG_REMOTE_REFUSED
        self.base_url = http_util.check_local_url(raw_url)
        self.model = (getattr(spec, "model", "") or "").strip()
        if not self.model:
            raise BackendUnavailable(MSG_BAD_ID.format(
                engine_id=getattr(spec, "name", ""), why="model adı boş"))
        self.extra_body: Dict[str, Any] = copy.deepcopy(dict(getattr(spec, "extra_body", None) or {}))
        # Ham tamamlama yalnız açık kayıtla (format hymt + raw true); aksi sohbet (B.8)
        self.raw = getattr(spec, "format", "") == FORMAT_HYMT and bool(getattr(spec, "raw", False))
        self._dropped_keys: set = set()
        self._ctx_limit: Optional[int] = None

    # ---------------------------------------------------------------- iletiler
    def server_name(self) -> str:
        """MSG_MODEL_NOT_ON_SERVER'daki sunucu adı."""
        return self.server_label

    def down_message(self) -> str:
        raise NotImplementedError

    def not_found_message(self) -> str:
        return MSG_MODEL_NOT_ON_SERVER.format(model=self.model, server=self.server_name())

    def describe(self) -> str:
        return f"{self.server_label} · {self.model} · {self.base_url}"

    def _memory_message(self, text: str) -> str:
        return MSG_OUT_OF_MEMORY_TR.format(server=self.server_label, detail=_b.short_detail(text))

    def _load_failure(self, exc: http_util.HttpStatusError) -> str:
        text = _error_text(exc.body)
        if exc.status == 404:
            return self.not_found_message()
        if _MEMORY_HINT_RE.search(text):
            return self._memory_message(text)
        if isinstance(exc, http_util.HttpInvalidJson):
            return _b.MSG_BAD_RESPONSE.format(server=self.server_label,
                                              detail=_b.short_detail(text))
        return _b.MSG_LOAD_FAILED.format(
            server=self.server_label, detail=_b.short_detail(f"HTTP {exc.status}: {text}"))

    # ---------------------------------------------------------------- gövde
    def _add_optional(self, body: Dict[str, Any], key: str, value: Any) -> None:
        if key not in self._dropped_keys:
            body[key] = value

    def _rejected_key(self, text: str, body: Dict[str, Any]) -> Optional[str]:
        low = text.lower()
        for key in self.optional_keys:
            if (key in body and key not in self._dropped_keys
                    and key not in self.extra_body and key.lower() in low):
                return key   # Ollama "does not support thinking" -> "think"
        return None

    def _context_message(self, text: str, raw_body: str, body: Dict[str, Any]) -> str:
        data: Any = None
        try:
            data = json.loads(raw_body)
        except (TypeError, ValueError):
            data = None
        ctx = _find_int(data, ("n_ctx", "context_length", "max_context_length"))
        need = _find_int(data, ("n_prompt_tokens", "prompt_tokens"))
        if ctx is None:
            m = _CONTEXT_NUMBER_RE.search(text)
            if m:
                ctx = int(m.group(1))
        if need is None:
            need = _b.estimate_tokens(_prompt_chars(body)) + _requested_tokens(body)
        if ctx is None:
            ctx = self._ctx_limit
        return MSG_CONTEXT_EXCEEDED.format(need=need, ctx=ctx if ctx is not None else "?")

    def _classify(self, exc: http_util.HttpStatusError,
                  body: Dict[str, Any]) -> Tuple[str, str]:
        """HTTP hatasını karara çevir (TASARIM B.6 tablosu)."""
        status = int(exc.status or 0)
        text = _error_text(exc.body)
        if isinstance(exc, http_util.HttpInvalidJson):
            return _VERDICT_ITEM, _b.MSG_BAD_RESPONSE.format(
                server=self.server_label, detail=_b.short_detail(text))
        if status in _REJECT_STATUSES:
            key = self._rejected_key(text, body)
            if key is not None:
                self._dropped_keys.add(key)
                logger.warning("[MT ARKA UÇ] %s '%s' alanını tanımadı; istek alansız "
                               "yineleniyor ve alan bu koşuda bir daha gönderilmeyecek.",
                               self.server_label, key)
                return _VERDICT_DROP, ""
        if status == 404:
            return _VERDICT_FATAL, self.not_found_message()
        if (status in _CONTEXT_STATUSES and _CONTEXT_RE.search(text)
                and _CONTEXT_DETAIL_RE.search(text)):
            return _VERDICT_ITEM, self._context_message(text, exc.body, body)
        if _MEMORY_HINT_RE.search(text) and (status >= 500 or status in _REJECT_STATUSES):
            return _VERDICT_FATAL, self._memory_message(text)
        if status in _AUTH_STATUSES:
            return _VERDICT_FATAL, _b.MSG_SERVER_ERROR.format(
                server=self.server_label, status=status, detail=_b.short_detail(text))
        if status >= 500 or status in _TRANSIENT_STATUSES:
            return _VERDICT_RETRY, text
        if status in _REJECT_STATUSES and _NOT_FOUND_RE.search(text) and "model" in text.lower():
            return _VERDICT_FATAL, self.not_found_message()
        return _VERDICT_ITEM, _b.MSG_SERVER_ERROR.format(
            server=self.server_label, status=status, detail=_b.short_detail(text))

    # ---------------------------------------------------------------- istek
    def _endpoint(self, path: str, root: bool = False) -> str:
        """İstek adresi: taban adres + yol; ``root`` ise sunucu kökü + yol."""
        base = http_util.url_root(self.base_url) if root else self.base_url
        return http_util.url_join(base, path)

    def _retries_exhausted(self, attempts: int, reason: str, status: int, text: str,
                           stream: bool = False) -> str:
        """Yeniden denemeler tükendi: öğe hatası iletisi döner ya da ``BackendUnavailable``.

        2026-09-29 (F4-05) — hata sınıfına göre:

        - Sunucu YANIT VERDİ ve parçaya özgü HTTP hatası (500 ve 502/503/504
          dışındaki 5xx; ör. o girdide çöken model çalıştırıcısı): yalnız bu
          parçanın hatası (``MSG_SERVER_ERROR``); ayrı sayaç art arda
          ``MT_HTTP_SERVER_ERROR_MAX_ITEMS`` parçaya varırsa devre
          ``MSG_SERVER_FAILING`` ile açılır ve ``BackendUnavailable`` fırlar.
        - Bağlantı kurulamadı / sunucu düzeyinde geçici durum (502, 503, 504,
          408, 425, 429): bağlantı sayacı artar. Devre açıldıysa
          ``MSG_SERVER_LOST`` ("kalan segmentler denenmeden geçildi" yalnız
          bu durumda doğrudur); açılmadıysa ``MSG_SERVER_RETRY_FAILED`` (sonraki
          parça sunucuyu yine dener). İkisinde de ``BackendUnavailable``.
        """
        kind = "akış denemesi" if stream else "deneme"
        if status and status not in _SERVER_LEVEL_STATUSES:
            detail = _b.short_detail(text)
            logger.warning("[MT ARKA UÇ] %s: %d %s HTTP %d ile bitti; bu parça çevrilmedi (%s)",
                           self.describe(), attempts, kind, status, detail)
            failing = MSG_SERVER_FAILING.format(
                server=self.server_label, count=max(1, int(_b.MT_HTTP_SERVER_ERROR_MAX_ITEMS)),
                status=status, detail=detail)
            if self.record_item_error(failing):
                raise BackendUnavailable(self.health() or failing)
            return _b.MSG_SERVER_ERROR.format(server=self.server_label, status=status,
                                              detail=detail)
        lost = MSG_SERVER_LOST.format(url=self.base_url)
        opened = self.record_failure(lost)
        logger.error("[MT ARKA UÇ] %s: %d %s başarısız (%s)", self.describe(), attempts,
                     kind, reason)
        if opened:
            raise BackendUnavailable(self.health() or lost)
        raise BackendUnavailable(MSG_SERVER_RETRY_FAILED.format(
            attempts=attempts, url=self.base_url, detail=_b.short_detail(reason)))

    def _request(self, path: str,
                 build_body: Callable[[], Dict[str, Any]], *,
                 root: bool = False) -> Tuple[Any, Optional[str]]:
        """Üretim isteği -> (json, None) ya da (None, öğe hatası).

        Bağlantı / geçici durum hatasında yeniden dener; tükenirse ya da
        arka uç kullanılamaz hâldeyse ``BackendUnavailable`` fırlatır.
        ``root``: yol sunucu kökünden (``/api/v1/chat``; G2b-1), aksi taban adresten.
        """
        self.check_circuit()
        url = self._endpoint(path, root)
        timeout = self.request_timeout()
        retries = max(0, int(_b.DEFAULT_RETRY_COUNT))
        attempt = 0
        body = build_body()
        while True:
            status, text = 0, ""
            try:
                data = http_util.post_json(url, body, timeout)
            except http_util.HttpTimeout:
                return None, MSG_TIMEOUT.format(seconds=_b.format_seconds(timeout))
            except http_util.HttpUnreachable as exc:
                reason = exc.reason or "bağlantı kurulamadı"
            except http_util.HttpStatusError as exc:
                verdict, message = self._classify(exc, body)
                if verdict == _VERDICT_DROP:
                    body = build_body()
                    continue
                if verdict == _VERDICT_ITEM:
                    return None, message
                if verdict == _VERDICT_FATAL:
                    self.open_circuit(message)
                    raise BackendUnavailable(message)
                status, text = int(exc.status or 0), message
                reason = f"HTTP {exc.status}: {_b.short_detail(message)}"
            else:
                self.record_success()
                return data, None

            # Geçici hata: yeniden dene ya da vazgeç
            if attempt >= retries:
                return None, self._retries_exhausted(attempt + 1, reason, status, text)
            delay = float(_b.DEFAULT_RETRY_BASE_DELAY_S) * (2 ** attempt)
            attempt += 1
            logger.warning("[MT ARKA UÇ] %s geçici hata (%s); %s sn sonra yeniden "
                           "deneniyor (%d/%d)", self.server_label, reason,
                           _b.format_seconds(delay), attempt, retries)
            _b._sleep(delay)
            self.check_circuit()   # başka bir iş parçacığı devreyi açmış olabilir

    def _request_stream(self, path: str, build_body: Callable[[], Dict[str, Any]],
                        new_reader: Callable[[], "_StreamReader"],
                        deadline: Optional[float], *, root: bool = False,
                        label_s: Optional[float] = None) -> Optional[str]:
        """Akışlı üretim isteği (``_request`` ile aynı hata tablosu) -> None ya da öğe hatası.

        Her deneme taze bir okuyucu alır (``new_reader``; yarım kalan akışın
        ölçümü tekrar sayılmaz). Süre, öğenin paylaşılan ``deadline``'ından
        kalan kadardır; biterse ``MSG_TIMEOUT`` (*label_s*: iletideki süre —
        düşünerek çeviride parçanın süre sınırı; verilmezse istek zaman aşımı).
        """
        self.check_circuit()
        url = self._endpoint(path, root)
        timeout = self.request_timeout()
        label = float(label_s) if label_s else timeout
        retries = max(0, int(_b.DEFAULT_RETRY_COUNT))
        attempt = 0
        body = build_body()
        while True:
            remaining = self.remaining(deadline, timeout)
            if remaining <= 0:
                return MSG_TIMEOUT.format(seconds=_b.format_seconds(label))
            reader = new_reader()
            status, text = 0, ""
            try:
                stream_post(url, body, remaining, reader, on_json=reader.take_whole)
            except http_util.HttpTimeout:
                return MSG_TIMEOUT.format(seconds=_b.format_seconds(label))
            except http_util.HttpUnreachable as exc:
                reason = exc.reason or "bağlantı kurulamadı"
            except http_util.HttpStatusError as exc:
                verdict, message = self._classify(exc, body)
                if verdict == _VERDICT_DROP:
                    body = build_body()
                    continue
                if verdict == _VERDICT_ITEM:
                    return message
                if verdict == _VERDICT_FATAL:
                    self.open_circuit(message)
                    raise BackendUnavailable(message)
                status, text = int(exc.status or 0), message
                reason = f"HTTP {exc.status}: {_b.short_detail(message)}"
            else:
                self.record_success()
                return None
            if attempt >= retries:
                return self._retries_exhausted(attempt + 1, reason, status, text, stream=True)
            delay = float(_b.DEFAULT_RETRY_BASE_DELAY_S) * (2 ** attempt)
            attempt += 1
            logger.warning("[MT ARKA UÇ] %s geçici hata (%s); %s sn sonra yeniden "
                           "deneniyor (%d/%d)", self.server_label, reason,
                           _b.format_seconds(delay), attempt, retries)
            _b._sleep(delay)
            self.check_circuit()

    def _stream_result(self, reader: Optional["_StreamReader"],
                       err: Optional[str]) -> GenerationResult:
        """Akış okuyucusunun durumunu ``GenerationResult``'a çevir."""
        meter = reader.meter if reader is not None else _b.ThinkingMeter(self.think_budget)
        fields = meter.result_fields()
        if meter.exceeded:
            logger.warning("[MT DÜŞÜNME] %s: düşünme %d token'da kesildi (sınır %d); "
                           "bağlantı kapatıldı", self.server_label, meter.tokens,
                           meter.budget)
            return GenerationResult(error=MSG_THINK_BUDGET.format(
                tokens=meter.tokens, budget=meter.budget), **fields)
        if err:
            return GenerationResult(error=err, **fields)
        if reader is None:
            return GenerationResult(error=_b.MSG_BAD_RESPONSE.format(
                server=self.server_label, detail="akış yok"), **fields)
        if reader.error:
            text = reader.error
            if _MEMORY_HINT_RE.search(text):
                message = self._memory_message(text)
                self.open_circuit(message)
                raise BackendUnavailable(message)
            return GenerationResult(error=_b.MSG_GENERATE_FAILED.format(
                server=self.server_label, detail=_b.short_detail(text)), **fields)
        if reader.bad:
            return GenerationResult(error=_b.MSG_BAD_RESPONSE.format(
                server=self.server_label, detail=reader.bad), **fields)
        if not reader.done:
            return GenerationResult(error=_b.MSG_BAD_RESPONSE.format(
                server=self.server_label, detail="akış tamamlanmadan kapandı"), **fields)
        content = meter.content
        if not content.strip() or not meter.answer_text().strip():
            return GenerationResult(text="", finish_reason=reader.finish,
                                    error=MSG_EMPTY_OUTPUT,
                                    output_tokens=meter.answer_tokens, **fields)
        return GenerationResult(text=content, finish_reason=reader.finish,
                                output_tokens=meter.answer_tokens, **fields)

    def _whole_result(self, content: Any, thinking: Any, finish: Optional[str],
                      source: str, output_tokens: Optional[int] = None,
                      thinking_tokens: Optional[int] = None,
                      budget: Optional[int] = None) -> GenerationResult:
        """Akışsız yanıt: düşünmeyi ölç, içeriği (ham) döndür ya da öğe hatası.

        ``output_tokens`` sunucunun toplam üretim sayacıdır; düşünme de varsa
        yanıt payı yalnız sunucu düşünme sayacı verdiyse (fark) bilinir, aksi
        hâlde None (sınır kararı ``finish_reason``'a kalır — tahminle
        "sınıra dayandı" denmez).
        """
        meter = _b.ThinkingMeter(budget or self.think_budget, source)
        meter.add_whole(_str_or_none(thinking), content if isinstance(content, str) else "",
                        thinking_tokens)
        fields = meter.result_fields()
        if meter.tokens:
            counted = _b._positive_int(thinking_tokens)
            output_tokens = (max(0, output_tokens - counted)
                             if output_tokens is not None and counted is not None else None)
        if not isinstance(content, str) or not content.strip() \
                or not meter.answer_text().strip():
            error = (MSG_THINK_BUDGET.format(tokens=meter.tokens, budget=meter.budget)
                     if meter.exceeded else MSG_EMPTY_OUTPUT)
            return GenerationResult(text="", finish_reason=finish, error=error,
                                    output_tokens=output_tokens, **fields)
        return GenerationResult(text=content, finish_reason=finish,
                                output_tokens=output_tokens, **fields)

    def _warm_up(self, path: str, build_body: Callable[[], Dict[str, Any]]) -> None:
        """Modeli sunucuda belleğe al (``MT_MODEL_LOAD_TIMEOUT_S``); olmazsa BackendUnavailable."""
        url = http_util.url_join(self.base_url, path)
        timeout = float(_b.MT_MODEL_LOAD_TIMEOUT_S)
        while True:
            body = build_body()
            try:
                http_util.post_json(url, body, timeout)
                return
            except http_util.HttpUnreachable:
                raise BackendUnavailable(self.down_message()) from None
            except http_util.HttpTimeout:
                raise BackendUnavailable(
                    MSG_TIMEOUT.format(seconds=_b.format_seconds(timeout))) from None
            except http_util.HttpStatusError as exc:
                if exc.status in _REJECT_STATUSES and not isinstance(exc, http_util.HttpInvalidJson):
                    key = self._rejected_key(_error_text(exc.body), body)
                    if key is not None:
                        self._dropped_keys.add(key)
                        logger.warning("[MT ARKA UÇ] %s '%s' alanını tanımadı; alan bu "
                                       "koşuda gönderilmeyecek.", self.server_label, key)
                        continue
                raise BackendUnavailable(self._load_failure(exc)) from None

    # ---------------------------------------------------------------- yanıt
    def _bad_response(self, detail: str) -> GenerationResult:
        return GenerationResult(error=_b.MSG_BAD_RESPONSE.format(
            server=self.server_label, detail=detail))

    def _context_budget(self, prompt_chars: int, max_tokens: int,
                        ctx: Optional[int]) -> Tuple[int, Optional[GenerationResult]]:
        """(bağlama kısılmış üretim sınırı, istek göndermeden öğe hatası ya da None).

        F4-03: istem sığıyorsa sınır bağlamda kalan yere kısılır; istem tek
        başına sığmıyorsa istek gönderilmez (``backends.context_budget``).
        """
        limit, err = _b.context_budget(prompt_chars, max_tokens, ctx)
        return limit, (GenerationResult(error=err) if err else None)

    def _stream_or_whole(self, reader: Optional["_StreamReader"], err: Optional[str],
                         source: str, budget: Optional[int] = None) -> GenerationResult:
        """Akış sonucu; sunucu stream:true'yu yok sayıp düz JSON döndürdüyse o
        yanıt akışsız yanıt gibi ayrıştırılır (F4-04)."""
        if err is None and reader is not None and reader.got_whole:
            return self._parse(reader.whole, raw=False, source=source, budget=budget)
        return self._stream_result(reader, err)


# ===========================================================================
# Ollama
# ===========================================================================
class OllamaBackend(_HttpBackend):
    """Ollama yerel API'si (``/api/chat``, ``/api/generate``)."""

    kind = BACKEND_OLLAMA
    server_label = BACKEND_LABELS[BACKEND_OLLAMA]
    optional_keys = ("think",)

    def __init__(self, spec):
        super().__init__(spec, OLLAMA_DEFAULT_URL)
        configured = _b._positive_int(self.options.get("num_ctx")) or 0
        self.num_ctx = max(int(MT_CONTEXT_TOKENS), configured)
        if configured and configured < self.num_ctx:
            # Belgelenmiş taban (docs/YENI_MODEL_EKLEME.md, seçenekler tablosu):
            # kısa bağlamda uzun paragraflar Ollama'da sessizce kırpılırdı.
            # Sessiz geçmesin (F4-06): kullanıcının yazdığı değer uygulanmadı.
            logger.warning("[MT ARKA UÇ] Ollama num_ctx=%d kayıtta verildi ama en az %d "
                           "kullanılır (kısa bağlamda uzun paragraflar kırpılırdı)",
                           configured, self.num_ctx)
        self.keep_alive = ollama_keep_alive(self.options.get("keep_alive"))
        self.thinking: Optional[bool] = None       # /api/show capabilities "thinking"
        self.think_values: Optional[List[Any]] = None  # /api/show "thinking.values"
        self.context_length: Optional[int] = None  # /api/show model_info.*.context_length

    def down_message(self) -> str:
        return MSG_OLLAMA_DOWN.format(url=self.base_url)

    # ---------------------------------------------------------------- yükleme
    def load(self) -> None:
        self.check_circuit()
        info = self._show()
        self._refuse_cloud(info)
        if isinstance(info, dict):
            caps = info.get("capabilities")
            if isinstance(caps, list):
                names = {str(c).lower() for c in caps}
                if names and "completion" not in names:
                    raise BackendUnavailable(MSG_EMBEDDING_MODEL)
                self.thinking = "thinking" in names
            think_meta = info.get("thinking")
            if isinstance(think_meta, dict) and isinstance(think_meta.get("values"), list):
                self.think_values = list(think_meta["values"])
            model_info = info.get("model_info")
            if isinstance(model_info, dict):
                for key, value in model_info.items():
                    if str(key).endswith(".context_length"):
                        number = _b._positive_int(value)
                        if number is not None:
                            self.context_length = number
                            break
        if self.context_length:
            self.num_ctx = min(self.num_ctx, self.context_length)
        # G2b-1: düşünme sınıfı /api/show'dan (keşifle aynı kural; HY-MT ham biçimi düşünmez)
        self.thinking_monitor.set_profile(
            ThinkingProfile(THINKING_NONE, basis="HY-MT ham biçimi") if self.raw
            else ollama_profile(info))
        # Isınma: boş prompt modeli belleğe alır; num_ctx burada da verilir ki
        # ilk gerçek istek modeli farklı bağlamla yeniden yükletmesin.
        self._warm_up("/api/generate", lambda: {
            "model": self.model, "prompt": "", "stream": False,
            "keep_alive": self.keep_alive, "options": {"num_ctx": self.num_ctx},
        })
        logger.info("[MT ARKA UÇ] %s hazır (num_ctx=%d, düşünme yeteneği=%s)",
                    self.describe(), self.num_ctx, self.thinking)
        self._log_thinking_profile()

    def _refuse_cloud(self, info: Optional[Dict[str, Any]]) -> None:
        """Bulut modeli (Ollama isteği ollama.com'a aktarır) yüklenmez (F4-01; KARARLAR 2).

        Hüküm keşif ve ön kontrolle aynı (``http_util.ollama_cloud_model``):
        ``/api/show`` yanıtında ``remote_host`` / ``remote_model``, ad
        ``-cloud`` / ``:cloud``. ``/api/show`` alınamadıysa ``/api/tags``
        öğesine de bakılır. Isınma isteği bile gönderilmeden
        ``BackendUnavailable``.
        """
        meta: List[Any] = [info] if isinstance(info, dict) else [self._tags_entry()]
        if http_util.ollama_cloud_model(self.model, *meta):
            host = http_util.ollama_remote_host(*meta) or http_util.OLLAMA_CLOUD_DEFAULT_HOST
            message = MSG_OLLAMA_CLOUD_MODEL.format(model=self.model, host=host)
            self.open_circuit(message)
            raise BackendUnavailable(message)

    def _tags_entry(self) -> Optional[Dict[str, Any]]:
        """``/api/tags``'taki bu modelin öğesi (``qwen3`` ≡ ``qwen3:latest``); yoksa None."""
        try:
            data = http_util.get_json(http_util.url_join(self.base_url, "/api/tags"),
                                      float(_b.ENGINE_CHECK_HTTP_TIMEOUT_S))
        except (http_util.HttpUnreachable, http_util.HttpTimeout, http_util.HttpStatusError):
            return None
        items = data.get("models") if isinstance(data, dict) else None
        wanted = {self.model, self.model + _OLLAMA_LATEST}
        if self.model.endswith(_OLLAMA_LATEST):
            wanted.add(self.model[:-len(_OLLAMA_LATEST)])
        for item in items if isinstance(items, list) else ():
            if isinstance(item, dict) and (item.get("name") in wanted
                                           or item.get("model") in wanted):
                return item
        return None

    def _show(self) -> Optional[Dict[str, Any]]:
        url = http_util.url_join(self.base_url, "/api/show")
        try:
            data = http_util.post_json(url, {"model": self.model},
                                       float(_b.ENGINE_CHECK_HTTP_TIMEOUT_S))
        except http_util.HttpUnreachable:
            raise BackendUnavailable(self.down_message()) from None
        except http_util.HttpTimeout:
            logger.warning("[MT ARKA UÇ] Ollama /api/show zaman aşımı; model bilgisi "
                           "olmadan devam ediliyor")
            return None
        except http_util.HttpStatusError as exc:
            text = _error_text(exc.body)
            if exc.status == 404 or (_NOT_FOUND_RE.search(text) and exc.status < 500):
                raise BackendUnavailable(self.not_found_message()) from None
            logger.warning("[MT ARKA UÇ] Ollama /api/show HTTP %s; model bilgisi "
                           "olmadan devam ediliyor", exc.status)
            return None
        return data if isinstance(data, dict) else None

    # ---------------------------------------------------------------- düşünme
    def thinking_capable(self) -> bool:
        return bool(self.thinking)

    def _think_hard_value(self) -> Optional[str]:
        """Sert kapatmada ``think`` değeri: model ``false``'a izin vermiyor ama
        "low" düzeyini bildiriyorsa (gpt-oss) "low"; yoksa None."""
        values = self.think_values
        if not values or False in values:
            return None
        return _OLLAMA_THINK_LOW if _OLLAMA_THINK_LOW in values else None

    def hard_off_available(self) -> bool:
        return _b.supports_no_think(self.spec) or (
            bool(self.thinking) and self._think_hard_value() is not None)

    def thinking_off_requested(self, hard: bool) -> bool:
        """Bu denemede ``think: false`` (ya da sert denemede "/no_think") gitti mi (G2b-1).

        ``chat_body`` ile aynı kural: ``think`` yalnız düşünme yeteneği bildiren
        modele gönderilir; sert denemede ``false`` izinli değilse en düşük düzey
        ("low") gider — o bir kapatma isteği DEĞİLDİR.
        """
        if self.raw or not _b.wants_thinking_off(self.spec):
            return False
        if hard and _b.supports_no_think(self.spec):
            return True
        if not self.thinking or "think" in self._dropped_keys:
            return False
        return not (hard and self._think_hard_value() is not None)

    # ---------------------------------------------------------------- gövde
    def _options(self, max_tokens: int) -> Dict[str, Any]:
        return {
            "temperature": self.temperature(),
            "seed": self.seed(),
            "repeat_penalty": self.repeat_penalty(),
            "num_predict": int(max_tokens),
            "num_ctx": self.num_ctx,
        }

    def chat_body(self, messages: List[Dict[str, str]], max_tokens: int, *,
                  stream: bool = False, hard: bool = False) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": bool(stream),
            "options": self._options(max_tokens),
            "keep_alive": self.keep_alive,
        }
        think = getattr(self.spec, "think", False)
        # "think" YALNIZ model düşünme yeteneği bildiriyorsa gönderilir
        # (yeteneksiz modele think gönderen istek 400 alır).
        if think is not None and self.thinking:
            value: Any = bool(think)
            if hard and value is False and self._think_hard_value() is not None:
                value = self._think_hard_value()
            self._add_optional(body, "think", value)
        return body

    def generate_body(self, prompt: str, max_tokens: int) -> Dict[str, Any]:
        return {
            "model": self.model,
            "prompt": prompt,
            "raw": True,
            "stream": False,
            "options": self._options(max_tokens),
            "keep_alive": self.keep_alive,
        }

    # ---------------------------------------------------------------- üretim
    def generate_once(self, engine, req: GenerationRequest, *, hard: bool = False,
                      deadline: Optional[float] = None) -> GenerationResult:
        self.check_circuit()
        ctx = self.num_ctx
        budget = self.attempt_think_budget(req)
        if self.raw:
            prompt = engine.build_raw_prompt(req.instruction, req.text)
            limit, early = self._context_budget(len(prompt), req.max_tokens, ctx)
            if early is not None:
                return early
            data, err = self._request("/api/generate",
                                      lambda: self.generate_body(prompt, limit))
            if err:
                return GenerationResult(error=err)
            return _b.context_overflow(self._parse(data, raw=True, source=req.text,
                                                   budget=budget),
                                       limit, req.max_tokens, len(prompt), ctx)
        messages = engine.build_chat_messages(req.instruction, req.text)
        if hard and _b.supports_no_think(self.spec):
            messages = _b.with_no_think(messages)
        chars = sum(len(m.get("content") or "") for m in messages)
        stream = self.use_stream()
        requested = self.stream_max_tokens(req) if stream else req.max_tokens
        limit, early = self._context_budget(chars, requested, ctx)
        if early is not None:
            return early
        if stream:
            readers: List[_StreamReader] = []

            def new_reader() -> _StreamReader:
                readers.append(_OllamaStreamReader(
                    _b.ThinkingMeter(budget, req.text), raw=False))
                return readers[-1]

            err = self._request_stream(
                "/api/chat",
                lambda: self.chat_body(messages, limit, stream=True, hard=hard),
                new_reader, deadline, label_s=self.attempt_time_label(req))
            result = self._stream_or_whole(readers[-1] if readers else None, err, req.text,
                                           budget)
            return _b.context_overflow(result, limit, requested, chars, ctx)
        data, err = self._request("/api/chat", lambda: self.chat_body(
            messages, limit, hard=hard))
        if err:
            return GenerationResult(error=err)
        return _b.context_overflow(self._parse(data, raw=False, source=req.text, budget=budget),
                                   limit, requested, chars, ctx)

    def _parse(self, data: Any, raw: bool, source: str = "",
               budget: Optional[int] = None) -> GenerationResult:
        if not isinstance(data, dict):
            return self._bad_response("JSON nesnesi değil")
        finish = _finish_reason(data.get("done_reason"))
        if raw:
            content, thinking = data.get("response"), data.get("thinking")
        else:
            message = data.get("message")
            if not isinstance(message, dict):
                return self._bad_response("'message' alanı yok")
            # message.thinking çeviriye girmez; yalnız ölçülür (KARARLAR 16)
            content, thinking = message.get("content"), message.get("thinking")
        return self._whole_result(content, thinking, finish, source,
                                  output_tokens=_b._positive_int(data.get("eval_count")),
                                  budget=budget)


# ===========================================================================
# OpenAI uyumlu (LM Studio, llama.cpp server, Jan, vLLM)
# ===========================================================================
class OpenAIBackend(_HttpBackend):
    """OpenAI uyumlu yerel sunucu (``/v1/chat/completions``, ``/v1/completions``)."""

    kind = BACKEND_OPENAI
    optional_keys = ("chat_template_kwargs", "repeat_penalty", "seed", "reasoning_effort")

    def __init__(self, spec):
        self.flavor = getattr(spec, "flavor", "") or ""
        default_url = LMSTUDIO_DEFAULT_URL if self.flavor == FLAVOR_LMSTUDIO else ""
        super().__init__(spec, default_url)
        self.server_label = (SOURCE_STATUS_LABELS[SOURCE_LMSTUDIO]
                             if self.flavor == FLAVOR_LMSTUDIO
                             else BACKEND_LABELS[BACKEND_OPENAI])
        # Bağlam sınırı: options.num_ctx (kullanıcının bildirdiği) ya da LM
        # Studio'nun yüklü örnek bağlamı; bilinmiyorsa ön denetim yok (sunucu 400'ü).
        self._ctx_limit = _b._positive_int(self.options.get("num_ctx"))
        self._ctx_declared = self._ctx_limit is not None
        # Düşünen model: LM Studio /api/v1/models "capabilities.reasoning"
        # (yalnız LM Studio bildirir; diğerlerinde ilk yanıttaki ölçüm karar verir)
        self.reasoning: Optional[bool] = None
        # G2b-1 (2026-10-05): LM Studio yerel REST (POST /api/v1/chat,
        # reasoning "off") ile resmî düşünme kapatma etkin mi; yüklü örneğin
        # bağlamı ve eşzamanlı tahmin sınırı (REST loaded_instances[0].config; MT-15).
        self._native_off = False
        self.loaded_context: Optional[int] = None
        self.loaded_parallel: Optional[int] = None
        # G2b-2 (KARARLAR 22): REST capabilities.reasoning.allowed_options ve
        # düşünerek çeviride yeniden deneme için yerel REST reasoning "low"
        # yolunun durumu (None: henüz yoklanmadı; True: var; False: yok).
        self._reasoning_options: Optional[Tuple[str, ...]] = None
        self._native_low: Optional[bool] = None
        self._native_low_lock = threading.Lock()

    @property
    def is_lmstudio(self) -> bool:
        return self.flavor == FLAVOR_LMSTUDIO

    def server_name(self) -> str:
        if self.is_lmstudio:
            return self.server_label
        return urllib.parse.urlsplit(self.base_url).netloc or self.base_url

    def down_message(self) -> str:
        if self.is_lmstudio:
            return MSG_LMSTUDIO_DOWN.format(url=self.base_url)
        return MSG_OPENAI_DOWN.format(url=self.base_url)

    def not_found_message(self) -> str:
        if self.is_lmstudio:
            return MSG_LMSTUDIO_NOT_LOADED.format(model=self.model)
        return MSG_MODEL_NOT_ON_SERVER.format(model=self.model, server=self.server_name())

    # ---------------------------------------------------------------- yükleme
    def load(self) -> None:
        self.check_circuit()
        self._check_listed()
        if self.is_lmstudio:
            self._enrich_lmstudio()
        else:
            self.thinking_monitor.set_profile(ThinkingProfile(
                THINKING_UNKNOWN, basis="OpenAI uyumlu sunucu düşünme bilgisi vermez"))
        if self.raw:
            self.thinking_monitor.set_profile(ThinkingProfile(THINKING_NONE,
                                                              basis="HY-MT ham biçimi"))
            self._warm_up("/completions", lambda: self.completion_body(WARMUP_TEXT, 1))
        elif not (self._native_off_wanted() and self._warm_up_native()):
            self._warm_up("/chat/completions", lambda: self.chat_body(
                [{"role": "user", "content": WARMUP_TEXT}], 1))
        self._refresh_loaded_config()
        logger.info("[MT ARKA UÇ] %s hazır (bağlam sınırı: %s)", self.describe(),
                    self._ctx_limit if self._ctx_limit else "bilinmiyor")
        self._log_thinking_profile()

    # ------------------------------------------- düşünme kapatma (G2b-1, MT-14)
    def _native_off_wanted(self) -> bool:
        """LM Studio yerel REST ile ``reasoning: "off"`` kullanılacak mı.

        Koşullar: LM Studio, sohbet biçimi, model ``allowed_options``'ta "off"
        bildiriyor (``thinking.lmstudio_profile``), kayıt düşünmeyi kapatmak
        istiyor (``think: false``, varsayılan). Kayıtta ``extra_body`` varsa
        (yalnız OpenAI uyumlu gövde içindir) yerel yol kullanılmaz.
        """
        profile = self.thinking_monitor.profile
        if not (self.is_lmstudio and not self.raw
                and profile.off_path == THINKING_OFF_LMSTUDIO_REST
                and _b.wants_thinking_off(self.spec)):
            return False
        if self.extra_body:
            self.thinking_monitor.mark_off_unavailable(
                THINKING_OFF_LMSTUDIO_REST,
                "kayıtta extra_body var; yalnız OpenAI uyumlu uca uygulanır")
            return False
        return True

    def _warm_up_native(self) -> bool:
        """Yerel REST'i ``reasoning: "off"`` ile ısıt (modeli de yükler).

        Başarılıysa üretim bu uçtan yapılır (True). Uç yoksa (eski LM Studio:
        404) ya da sunucu ayarı reddederse (belge: "Will error if the model ...
        does not support the reasoning setting") kapatma yolu bu koşuda
        kullanılamaz: yapışkan bayrak + OpenAI uyumlu uca dönüş (False).
        Bağlantı yok / zaman aşımı / bellek hatası OpenAI uyumlu ısınmayla
        aynı biçimde ``BackendUnavailable``.
        """
        timeout = float(_b.MT_MODEL_LOAD_TIMEOUT_S)
        try:
            http_util.post_json(self._endpoint(_LMS_CHAT_PATH, root=True),
                                self.native_body("", WARMUP_TEXT, 1), timeout)
        except http_util.HttpUnreachable:
            raise BackendUnavailable(self.down_message()) from None
        except http_util.HttpTimeout:
            raise BackendUnavailable(
                MSG_TIMEOUT.format(seconds=_b.format_seconds(timeout))) from None
        except http_util.HttpStatusError as exc:
            text = _error_text(exc.body)
            if _MEMORY_HINT_RE.search(text) and exc.status not in _LMS_REST_UNSUPPORTED_STATUSES:
                raise BackendUnavailable(self._load_failure(exc)) from None
            self.thinking_monitor.mark_off_unavailable(
                THINKING_OFF_LMSTUDIO_REST, f"HTTP {exc.status}: {_b.short_detail(text)}")
            return False
        self._native_off = True
        logger.info("[MT DÜŞÜNME] %s: düşünme LM Studio yerel REST ile kapatılıyor "
                    "(POST %s, reasoning=%s)", self.describe(), _LMS_CHAT_PATH,
                    LMSTUDIO_REASONING_OFF)
        return True

    def native_body(self, system: str, user: str, max_tokens: int, *,
                    stream: bool = False,
                    reasoning: str = LMSTUDIO_REASONING_OFF) -> Dict[str, Any]:
        """``POST /api/v1/chat`` gövdesi (belgeli alanlar; seed alanı belgede yok).

        *reasoning*: "off" (resmî kapatma, G2b-1) ya da düşünerek çeviride TEK
        yeniden denemenin daha kısa düzeyi "low" (G2b-2).
        """
        body: Dict[str, Any] = {
            "model": self.model,
            "input": user,
            "reasoning": reasoning,
            "max_output_tokens": int(max_tokens),
            "temperature": self.temperature(),
            "stream": bool(stream),
            # Belge: store varsayılanı true (sohbet LM Studio'da saklanır); saklanmaz.
            "store": False,
        }
        if system:
            body["system_prompt"] = system
        self._add_optional(body, "repeat_penalty", self.repeat_penalty())
        return body

    @staticmethod
    def _native_input(messages: List[Dict[str, str]]) -> Optional[Tuple[str, str]]:
        """Sohbet mesajları -> (system_prompt, input); tek kullanıcı mesajı yoksa None."""
        system: List[str] = []
        users: List[str] = []
        for message in messages:
            role, content = message.get("role"), message.get("content") or ""
            if role == "system":
                system.append(content)
            elif role == "user":
                users.append(content)
            else:
                return None
        if len(users) != 1:
            return None
        return "\n".join(system), users[0]

    def thinking_off_requested(self, hard: bool) -> bool:
        """Bu denemede resmî kapatma isteği gitti mi (G2b-1).

        LM Studio yerel REST yolu etkinse her istek ``reasoning: "off"`` taşır;
        sert denemede Qwen3 karma "/no_think" ve (LM Studio dışı sunucuda)
        ``reasoning_effort: "none"`` da kapatma isteğidir.
        """
        if self.raw or not _b.wants_thinking_off(self.spec):
            return False
        if self._native_off:
            return True
        if hard and _b.supports_no_think(self.spec):
            return True
        return bool(hard) and not self.is_lmstudio and \
            "reasoning_effort" not in self._dropped_keys

    def _check_listed(self) -> None:
        url = http_util.url_join(self.base_url, "/models")
        try:
            data = http_util.get_json(url, float(_b.ENGINE_CHECK_HTTP_TIMEOUT_S))
        except http_util.HttpUnreachable:
            raise BackendUnavailable(self.down_message()) from None
        except http_util.HttpTimeout:
            logger.warning("[MT ARKA UÇ] %s model listesi zaman aşımı; ısınma "
                           "isteğiyle devam ediliyor", self.server_label)
            return
        except http_util.HttpStatusError:
            raise BackendUnavailable(MSG_NOT_OPENAI.format(url=self.base_url)) from None
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise BackendUnavailable(MSG_NOT_OPENAI.format(url=self.base_url))
        ids = {str(item.get("id")) for item in items if isinstance(item, dict)}
        if self.model not in ids:
            raise BackendUnavailable(self.not_found_message())

    def _lmstudio_entry(self) -> Optional[Dict[str, Any]]:
        """Bu modelin LM Studio REST öğesi (/api/v1/models, yoksa /api/v0/models); yoksa None."""
        root = http_util.url_root(self.base_url)
        for path, list_key, id_key in (("/api/v1/models", "models", "key"),
                                       ("/api/v0/models", "data", "id")):
            try:
                data = http_util.get_json(http_util.url_join(root, path),
                                          float(_b.ENGINE_CHECK_HTTP_TIMEOUT_S))
            except (http_util.HttpUnreachable, http_util.HttpTimeout,
                    http_util.HttpStatusError):
                continue
            items = data.get(list_key) if isinstance(data, dict) else None
            if not isinstance(items, list):
                continue
            return next((it for it in items
                         if isinstance(it, dict) and str(it.get(id_key)) == self.model), None)
        return None

    def _enrich_lmstudio(self) -> None:
        """LM Studio yerel REST'i: gömme modeli reddi, yüklü bağlam, düşünme sınıfı.

        G2b-1: düşünme sınıfı ``capabilities.reasoning.allowed_options``'tan
        (``thinking.lmstudio_profile``; REST v1 yoksa "bilinmiyor").
        """
        self.thinking_monitor.set_profile(lmstudio_profile(None))
        entry = self._lmstudio_entry()
        if entry is None:
            return
        if str(entry.get("type", "")).lower() in ("embedding", "embeddings"):
            raise BackendUnavailable(MSG_EMBEDDING_MODEL)
        caps = entry.get("capabilities")
        if isinstance(caps, dict):
            self.reasoning = "reasoning" in caps
            self._reasoning_options = lmstudio_reasoning_options(caps.get("reasoning"))
        self.thinking_monitor.set_profile(lmstudio_profile(caps))
        self._apply_loaded_config(entry)

    def _apply_loaded_config(self, entry: Dict[str, Any]) -> None:
        """Yüklü örneğin bağlamını sınıra uygula (MT-15, G2b-1).

        LM Studio modeli istenenden farklı bağlamla yükleyebilir (gemma-4-26b:
        ``--context-length 8192`` istendi, 116 736 yüklendi). Kayıtta
        ``num_ctx`` yoksa sınır yüklü bağlamdır; varsa ikisinin KÜÇÜĞÜ (yüklü
        bağlamı aşan istek sunucuda zaten reddedilir).
        """
        ctx, parallel = http_util.lmstudio_loaded_config(entry)
        if ctx is None:
            return
        self.loaded_context, self.loaded_parallel = ctx, parallel
        if not self._ctx_declared:
            self._ctx_limit = ctx
        elif self._ctx_limit is not None and ctx < self._ctx_limit:
            logger.warning("[MT ARKA UÇ] %s: model %d token bağlamla yüklü, kayıttaki "
                           "num_ctx %d; sınır yüklü bağlam (%d)", self.describe(), ctx,
                           self._ctx_limit, ctx)
            self._ctx_limit = ctx

    def _refresh_loaded_config(self) -> None:
        """Isınmadan sonra (isteğe bağlı yükleme modeli yeni yüklediyse) yüklü bağlamı yeniden oku."""
        if not self.is_lmstudio or self.loaded_context is not None:
            return
        entry = self._lmstudio_entry()
        if entry is not None:
            self._apply_loaded_config(entry)

    # ---------------------------------------------------------------- düşünme
    def thinking_capable(self) -> bool:
        return bool(self.reasoning)

    def hard_off_available(self) -> bool:
        # LM Studio'nun OpenAI uyumlu ucu düşünme alanı belgelemiyor: yalnız
        # Qwen3 yumuşak anahtarı. Diğer sunucular (llama.cpp server):
        # "reasoning_effort": "none" (reddedilirse alan düşürülür).
        return _b.supports_no_think(self.spec) or not self.is_lmstudio

    # ------------------------------- düşünerek çeviri: daha kısa yol (G2b-2)
    def _lms_low_wanted(self) -> bool:
        """LM Studio modeli REST'te "low" düzeyini bildiriyor ve yol kullanılabilir mi.

        Kayıt düşünmeyi kapatmak istiyor olmalı (``think: false``; kullanıcının
        açık düşünme tercihinde istek değiştirilmez), ``extra_body`` yok (yalnız
        OpenAI uyumlu gövdeye aittir), yerel REST daha önce reddedilmemiş.
        """
        return bool(self.is_lmstudio and not self.raw and not self.extra_body
                    and _b.wants_thinking_off(self.spec)
                    and self._reasoning_options
                    and LMSTUDIO_REASONING_LOW in self._reasoning_options
                    and self._native_low is not False)

    def shorter_thinking_available(self) -> bool:
        """Taban yollara ek olarak LM Studio yerel REST ``reasoning: "low"``."""
        return super().shorter_thinking_available() or self._lms_low_wanted()

    def _native_low_ready(self, deadline: Optional[float]) -> bool:
        """Yerel REST "low" yolu bu sunucuda var mı (ilk kullanımda bir kez yoklanır).

        Yoklama ısınmayla aynı biçimde 1 token'lık istektir (``store: false``).
        Uç yoksa / ayar reddedilirse (404/405/400/422) yol bu koşuda kapanır;
        bağlantı / süre sorunu geçicidir (yol kapanmaz, bu deneme aynı istekle
        yapılır). Yanlış sınıflandırma riski yok: 404 burada "model yok"
        sayılmaz (devre açılmaz).
        """
        with self._native_low_lock:
            if self._native_low is not None:
                return self._native_low
            timeout = self.remaining(deadline, self.request_timeout())
            if timeout <= 0:
                return False
            try:
                http_util.post_json(self._endpoint(_LMS_CHAT_PATH, root=True),
                                    self.native_body("", WARMUP_TEXT, 1,
                                                     reasoning=LMSTUDIO_REASONING_LOW),
                                    timeout)
            except (http_util.HttpUnreachable, http_util.HttpTimeout):
                return False
            except http_util.HttpStatusError as exc:
                if exc.status in _LMS_REST_UNSUPPORTED_STATUSES:
                    self._native_low = False
                    logger.warning("[MT DÜŞÜNME] %s: LM Studio yerel REST reasoning=%s "
                                   "kullanılamadı (HTTP %s: %s); yeniden denemeler aynı "
                                   "istekle yapılacak", self.describe(), LMSTUDIO_REASONING_LOW,
                                   exc.status, _b.short_detail(_error_text(exc.body)))
                return False
            self._native_low = True
            logger.info("[MT DÜŞÜNME] %s: düşünerek çeviride cevapsız parçanın yeniden "
                        "denemesi LM Studio yerel REST ile reasoning=%s (POST %s)",
                        self.describe(), LMSTUDIO_REASONING_LOW, _LMS_CHAT_PATH)
            return True

    # ---------------------------------------------------------------- gövde
    def _finish_body(self, body: Dict[str, Any], stream: bool = False) -> Dict[str, Any]:
        self._add_optional(body, "seed", self.seed())
        self._add_optional(body, "repeat_penalty", self.repeat_penalty())
        protected = {key: body[key] for key in _PROTECTED_BODY_KEYS if key in body}
        body.update(copy.deepcopy(self.extra_body))
        body.update(protected)
        body["stream"] = bool(stream)
        return body

    def chat_body(self, messages: List[Dict[str, str]], max_tokens: int, *,
                  stream: bool = False, hard: bool = False) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens),
            "temperature": self.temperature(),
            "stream": bool(stream),
        }
        template_kwargs = dict(getattr(self.spec, "chat_template_kwargs", None) or {})
        if template_kwargs:
            self._add_optional(body, "chat_template_kwargs", template_kwargs)
        if hard and not self.is_lmstudio:
            self._add_optional(body, "reasoning_effort", _REASONING_EFFORT_OFF)
        return self._finish_body(body, stream=stream)

    def completion_body(self, prompt: str, max_tokens: int) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": int(max_tokens),
            "temperature": self.temperature(),
            "stream": False,
        }
        return self._finish_body(body)

    # ---------------------------------------------------------------- üretim
    def generate_once(self, engine, req: GenerationRequest, *, hard: bool = False,
                      deadline: Optional[float] = None) -> GenerationResult:
        self.check_circuit()
        ctx = self._ctx_limit
        budget = self.attempt_think_budget(req)
        if self.raw:
            prompt = engine.build_raw_prompt(req.instruction, req.text)
            limit, early = self._context_budget(len(prompt), req.max_tokens, ctx)
            if early is not None:
                return early
            data, err = self._request("/completions",
                                      lambda: self.completion_body(prompt, limit))
            if err:
                return GenerationResult(error=err)
            return _b.context_overflow(self._parse(data, raw=True, source=req.text,
                                                   budget=budget),
                                       limit, req.max_tokens, len(prompt), ctx)
        messages = engine.build_chat_messages(req.instruction, req.text)
        if hard and _b.supports_no_think(self.spec):
            messages = _b.with_no_think(messages)
        # G2b-2: düşünerek çeviride TEK yeniden deneme (sert kip) — model izin
        # veriyorsa LM Studio yerel REST'in resmî daha kısa düşünme düzeyiyle.
        if hard and self._lms_low_wanted() and self.thinking_mode():
            low = self._native_input(messages)
            if low is not None and self._native_low_ready(deadline):
                return self._generate_native(low, req, deadline,
                                             reasoning=LMSTUDIO_REASONING_LOW)
        native = self._native_input(messages) if self._native_off else None
        if native is not None:
            return self._generate_native(native, req, deadline)
        chars = sum(len(m.get("content") or "") for m in messages)
        stream = self.use_stream()
        requested = self.stream_max_tokens(req) if stream else req.max_tokens
        limit, early = self._context_budget(chars, requested, ctx)
        if early is not None:
            return early
        if stream:
            readers: List[_StreamReader] = []

            def new_reader() -> _StreamReader:
                readers.append(_SSEReader(_b.ThinkingMeter(budget, req.text), raw=False))
                return readers[-1]

            err = self._request_stream(
                "/chat/completions",
                lambda: self.chat_body(messages, limit, stream=True, hard=hard),
                new_reader, deadline, label_s=self.attempt_time_label(req))
            result = self._stream_or_whole(readers[-1] if readers else None, err, req.text,
                                           budget)
            return _b.context_overflow(result, limit, requested, chars, ctx)
        data, err = self._request("/chat/completions",
                                  lambda: self.chat_body(messages, limit, hard=hard))
        if err:
            return GenerationResult(error=err)
        return _b.context_overflow(self._parse(data, raw=False, source=req.text, budget=budget),
                                   limit, requested, chars, ctx)

    def _parse(self, data: Any, raw: bool, source: str = "",
               budget: Optional[int] = None) -> GenerationResult:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return self._bad_response("'choices' alanı yok")
        first = choices[0]
        finish = _finish_reason(first.get("finish_reason"))
        thinking = None
        if raw:
            content = first.get("text")
        else:
            message = first.get("message")
            message = message if isinstance(message, dict) else {}
            content = message.get("content")
            # Düşünme metni çeviriye girmez; yalnız ölçülür (llama.cpp / LM Studio
            # reasoning_content, LM Studio gpt-oss reasoning)
            thinking = _str_or_none(message.get("reasoning_content"))
            if thinking is None:
                thinking = _str_or_none(message.get("reasoning"))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        details = usage.get("completion_tokens_details")
        reasoning_tokens = (_b._positive_int(details.get("reasoning_tokens"))
                            if isinstance(details, dict) else None)
        return self._whole_result(content, thinking, finish, source,
                                  output_tokens=_b._positive_int(usage.get("completion_tokens")),
                                  thinking_tokens=reasoning_tokens, budget=budget)

    # ------------------------------------- LM Studio yerel REST üretimi (G2b-1)
    def _generate_native(self, native: Tuple[str, str], req: GenerationRequest,
                         deadline: Optional[float], *,
                         reasoning: str = LMSTUDIO_REASONING_OFF) -> GenerationResult:
        """``POST /api/v1/chat`` ile tek deneme (*reasoning*: "off" ya da G2b-2 "low").

        OpenAI uyumlu yolla aynı kurallar: bağlam bütçesi (F4-03), düşünen
        modelde akış + düşünme sınırında kesme (kapatma tutmazsa da sınır
        işler; düşünerek çeviride sınır parçanın payı), sunucu ``stream``'i yok
        sayıp düz JSON döndürürse akışsız ayrıştırma.
        """
        system, user = native
        ctx = self._ctx_limit
        chars = len(system) + len(user)
        budget = self.attempt_think_budget(req)
        stream = self.use_stream()
        requested = self.stream_max_tokens(req) if stream else req.max_tokens
        limit, early = self._context_budget(chars, requested, ctx)
        if early is not None:
            return early
        if stream:
            readers: List[_StreamReader] = []

            def new_reader() -> _StreamReader:
                readers.append(_LMStudioChatReader(_b.ThinkingMeter(budget, req.text)))
                return readers[-1]

            err = self._request_stream(
                _LMS_CHAT_PATH, lambda: self.native_body(system, user, limit, stream=True,
                                                         reasoning=reasoning),
                new_reader, deadline, root=True, label_s=self.attempt_time_label(req))
            reader = readers[-1] if readers else None
            if err is None and reader is not None and reader.got_whole:
                result = self._parse_native(reader.whole, req.text, budget)
            else:
                result = self._stream_result(reader, err)
            return _b.context_overflow(result, limit, requested, chars, ctx)
        data, err = self._request(_LMS_CHAT_PATH,
                                  lambda: self.native_body(system, user, limit,
                                                           reasoning=reasoning), root=True)
        if err:
            return GenerationResult(error=err)
        return _b.context_overflow(self._parse_native(data, req.text, budget),
                                   limit, requested, chars, ctx)

    def _parse_native(self, data: Any, source: str = "",
                      budget: Optional[int] = None) -> GenerationResult:
        """Yerel REST yanıtı: ``output[]`` "message" -> yanıt, "reasoning" -> düşünme
        (yalnız ölçülür); ``stats`` toplam ve düşünme token sayaçları. Belgede
        bitiş nedeni yok (finish None; sınır kararı token sayacına kalır)."""
        output = data.get("output") if isinstance(data, dict) else None
        if not isinstance(output, list):
            return self._bad_response("'output' alanı yok")
        content: List[str] = []
        thinking: List[str] = []
        for item in output:
            if not isinstance(item, dict) or not isinstance(item.get("content"), str):
                continue
            if item.get("type") == _LMS_OUTPUT_MESSAGE:
                content.append(item["content"])
            elif item.get("type") == _LMS_OUTPUT_REASONING:
                thinking.append(item["content"])
        stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
        return self._whole_result(
            "".join(content), "".join(thinking) or None, None, source,
            output_tokens=_b._positive_int(stats.get("total_output_tokens")),
            thinking_tokens=_b._positive_int(stats.get("reasoning_output_tokens")),
            budget=budget)
