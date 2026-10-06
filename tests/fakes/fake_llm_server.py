"""Sahte yerel LLM sunucusu: Ollama, OpenAI uyumlu (llama.cpp server benzeri), LM Studio.

Gerçek soket üzerinde koşar (stdlib ``ThreadingHTTPServer``, ``127.0.0.1:0``):
``core.mt_engines.http_util`` ve arka uçlar gerçek HTTP yığınıyla sınanır.
Hiçbir model YÜKLENMEZ; yanıt ``reply`` fonksiyonundan gelir. Yalnız test
altyapısıdır (kullanıcıya gösterilen veri değildir).

Kullanım::

    with FakeLLMServer("ollama", models=["qwen3:8b"]) as srv:
        post_json(srv.url + "/api/chat", {...}, timeout=2)
        method, path, body = srv.requests[-1]

Rotalar (gerçek sunucuların belgelenmiş biçimleri, 25 Eyl 2026):

- ``flavor="ollama"``: ``GET /`` (metin), ``GET /api/version``, ``GET /api/tags``,
  ``GET /api/ps``, ``POST /api/show``, ``POST /api/chat``, ``POST /api/generate``.
  ``stream`` açıkça ``false`` değilse yanıt NDJSON akışıdır (gerçek Ollama gibi;
  JSON bekleyen istemci ayrıştıramaz): önce düşünme parçaları
  (``message.thinking``), sonra içerik parçaları, sonda ``done: true`` +
  ``done_reason`` + ``eval_count``. ``think`` açık (true / düzey adı) gönderilir
  ve modelin düşünme yeteneği yoksa 400 ("does not support thinking").
  ``FakeModel.think_values`` verilirse ``/api/show`` üst düzey ``thinking``
  nesnesini (``values`` / ``default``) döner. Boş ``prompt`` / boş ``messages``
  = model yükleme yanıtı (``done_reason: "load"``).
- ``flavor="openai"``: ``GET /health``, ``GET /v1/models``,
  ``POST /v1/chat/completions``, ``POST /v1/completions``. ``stream: true`` ->
  SSE (JSON değil): ``delta`` parçaları (``reasoning_field`` adlı düşünme alanı,
  ``content``), son parçada ``finish_reason``, sonda ``data: [DONE]``.

Akış (2026-09-28, düşünme bütçesi testleri): parçalar TEK TEK yazılır ve
``stream_chunk_delay_s`` kadar beklenir; istemci bağlantıyı kapatınca yazma
durur. ``FakeRequest.stream_chunks`` gönderilebilen parça sayısını,
``stream_closed_early`` istemcinin akışı erken kapattığını kaydeder
(sunucu tarafında "istek hemen kesildi" kanıtı). Parçalar içeriği birebir
korur (boşluklar dahil; birleşim = tam içerik).
- ``flavor="lmstudio"``: openai rotaları + ``GET /api/v1/models`` (LM Studio
  0.4 REST) + ``GET /api/v0/models`` (eski REST). ``jit=False`` iken
  ``/v1/models`` yalnız yüklü modelleri listeler ve yüklü olmayan modele
  üretim isteği 404 alır. ``native_chat=True`` (varsayılan KAPALI; G2b-1
  önerisi, G2b-2'de uygulandı) yerel REST ``POST /api/v1/chat``'i açar
  (https://lmstudio.ai/docs/developer/rest/chat): istek ``reasoning`` değerini
  sunucu UYGULAMAZ — yanıttaki düşünce ``reply(text, body)``'nin döndürdüğü
  ``FakeReply.thinking``'dir (test ``body["reasoning"]``'e göre yanıt seçer);
  ``reject_reasoning=True`` ise ``reasoning`` alanı 400 alır.
  ``FakeModel.reasoning_options`` REST'teki ``allowed_options``'tır.

Varsayılan yanıt: sohbette son user içeriğinin ilk ``"\\n\\n"``'den sonrası
(system mesajı varsa user içeriğinin tamamı), ham prompt'ta ``<|extra_4|>``
ile ``<|extra_0|>`` arası metin -> ``"ÇEVRİLDİ: " + metin``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "stream_pieces",
    "FLAVORS",
    "FakeLLMServer",
    "FakeModel",
    "FakeReply",
    "FakeRequest",
    "REPLY_PREFIX",
    "THINK_BLOCK",
    "default_reply",
    "extract_chat_text",
    "extract_prompt_text",
]

FLAVORS = ("ollama", "openai", "lmstudio")
REPLY_PREFIX = "ÇEVRİLDİ: "
THINK_BLOCK = "<think>\nBu metni Türkçeye çevirmeliyim.\n</think>\n\n"
THINKING_TEXT = "Bu metni Türkçeye çevirmeliyim."
FAKE_OLLAMA_VERSION = "0.12.3"
_HYMT_OPEN = "<|extra_4|>"
_HYMT_CLOSE = "<|extra_0|>"
_CREATED_AT = "2026-09-25T12:00:00.000000Z"
_CREATED_TS = 1790337600
_DEFAULT_MODELS = {
    "ollama": ("qwen3:8b",),
    "openai": ("qwen",),
    "lmstudio": ("qwen/qwen3-8b",),
}


# ---------------------------------------------------------------------------
# Veri yapıları
# ---------------------------------------------------------------------------
@dataclass
class FakeModel:
    """Sahte sunucudaki bir model (üst veri alanları gerçek API adlarına eşlenir)."""
    name: str
    kind: str = "llm"                  # "llm" | "embedding"
    parameter_size: str = "8.2B"       # ollama details.parameter_size / lmstudio params_string
    quantization: str = "Q4_K_M"       # ollama details.quantization_level / lmstudio quantization.name
    format: str = "gguf"               # "gguf" | "mlx"
    family: str = "qwen3"              # mimari / aile
    size_bytes: int = 5_234_000_000
    context_length: int = 40960        # /api/show model_info, lmstudio max_context_length
    thinking: Optional[bool] = None    # None -> sunucunun thinking_capability değeri
    loaded: bool = False               # /api/ps, lmstudio loaded_instances
    vision: bool = False
    publisher: str = ""                # lmstudio; boşsa adın '/' öncesi
    think_values: Optional[list] = None  # ollama /api/show "thinking.values" (ör. gpt-oss)
    think_default: Any = None            # ollama /api/show "thinking.default"
    # lmstudio /api/v1/models "capabilities.reasoning.allowed_options" (G2b-2).
    # None -> düşünen modelde ["off", "on"]; "off" içermeyen liste (ör. gpt-oss
    # ["low", "medium", "high"]) düşünmesi kapatılamayan modeli taklit eder.
    reasoning_options: Optional[list] = None


@dataclass
class FakeReply:
    """``reply`` fonksiyonunun öğe başına ayrıntılı yanıtı (düz str yerine döndürülebilir)."""
    content: Optional[str] = None      # None + status 200 -> içerik None/boş (content_none gibi)
    status: int = 200                  # != 200 -> hata yanıtı
    error: str = ""                    # hata gövdesi iletisi (sunucu biçiminde sarılır)
    finish_reason: Optional[str] = None  # None -> sunucunun finish_reason'ı
    delay_s: float = 0.0               # yanıttan önce bekle (öğe başına zaman aşımı testi)
    thinking: Optional[str] = None     # ayrı düşünme alanı (ollama message.thinking /
                                       # openai reasoning_content)


@dataclass
class FakeRequest:
    method: str
    path: str
    json: Any
    headers: Dict[str, str]
    t_start: float
    t_end: float = 0.0
    status: int = 0
    stream_chunks: int = 0             # akışta yazılabilen parça sayısı
    stream_total: int = 0              # akışta yazılacak toplam parça
    stream_closed_early: bool = False  # istemci akışı bitmeden kapattı


ReplyFn = Callable[..., Union[str, FakeReply]]


def default_reply(text: str) -> str:
    return REPLY_PREFIX + text


def extract_chat_text(messages: Sequence[dict]) -> str:
    """Sohbet mesajlarından çevrilecek metni çıkar (varsayılan yanıtın girdisi)."""
    users = [m for m in messages or () if isinstance(m, dict) and m.get("role") == "user"]
    if not users:
        return ""
    content = users[-1].get("content") or ""
    if isinstance(content, list):   # OpenAI çok parçalı içerik
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
        return content
    if "\n\n" in content:
        return content.split("\n\n", 1)[1]
    return content


def extract_prompt_text(prompt: str) -> str:
    """Ham prompt'tan metin: HY-MT işaretleri arası; yoksa ilk boş satırdan sonrası."""
    prompt = prompt or ""
    if _HYMT_OPEN in prompt:
        rest = prompt.split(_HYMT_OPEN, 1)[1]
        return rest.split(_HYMT_CLOSE, 1)[0]
    if "\n\n" in prompt:
        return prompt.split("\n\n", 1)[1]
    return prompt


def _normalize_model(m: Union[str, dict, FakeModel]) -> FakeModel:
    if isinstance(m, FakeModel):
        return m
    if isinstance(m, str):
        return FakeModel(name=m)
    if isinstance(m, dict):
        return FakeModel(**m)
    raise TypeError(f"model str/dict/FakeModel olmalı: {m!r}")


def _digest(name: str) -> str:
    return "sha256:" + hashlib.sha256(name.encode("utf-8")).hexdigest()


def _count_tokens(text: str) -> int:
    return max(1, len((text or "").split()))


_PIECE_RE = re.compile(r"\s*\S+\s*|\s+")


def stream_pieces(text: Optional[str]) -> List[str]:
    """Metni akış parçalarına böl (sözcük + ardındaki boşluk); birleşim = metin."""
    return _PIECE_RE.findall(text or "")


# ---------------------------------------------------------------------------
# Sunucu
# ---------------------------------------------------------------------------
class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def handle_error(self, request, client_address):   # istemci kopunca sessiz kal
        pass


class FakeLLMServer:
    """Sahte Ollama / OpenAI uyumlu / LM Studio sunucusu.

    Parametreler çalışırken de değiştirilebilir (öznitelik olarak):
    ``models`` (``set_models``), ``reply``, ``delay_s``, ``think_block``,
    ``prefix``, ``finish_reason``, ``thinking_capability``,
    ``reasoning_content``, ``reject_body_keys``, ``content_none``, ``jit``,
    ``strict_model``, ``stream_chunk_delay_s``, ``reasoning_field``,
    ``reject_reasoning``, ``native_chat`` (rota tablosu her istekte yeniden
    kurulur).

    ``reply(text)`` ya da ``reply(text, request_json)`` -> ``str`` | ``FakeReply``;
    istisna fırlatırsa 500 döner.
    """

    def __init__(self, flavor: str = "ollama",
                 models: Optional[Iterable[Union[str, dict, FakeModel]]] = None,
                 reply: Optional[ReplyFn] = None, delay_s: float = 0.0, *,
                 think_block: bool = False, prefix: str = "",
                 finish_reason: str = "stop", thinking_capability: bool = False,
                 reasoning_content: Optional[str] = None,
                 reject_body_keys: Iterable[str] = (), content_none: bool = False,
                 jit: bool = True, strict_model: bool = True,
                 stream_chunk_delay_s: float = 0.0, reasoning_field: str = "reasoning_content",
                 native_chat: bool = False, reject_reasoning: bool = False,
                 host: str = "127.0.0.1", start: bool = True):
        if flavor not in FLAVORS:
            raise ValueError(f"flavor {FLAVORS} içinden olmalı: {flavor!r}")
        self.flavor = flavor
        self._lock = threading.RLock()
        self.models: List[FakeModel] = []
        self.set_models(_DEFAULT_MODELS[flavor] if models is None else models)
        self.reply: ReplyFn = reply or default_reply
        self.delay_s = delay_s
        self.think_block = think_block
        self.prefix = prefix
        self.finish_reason = finish_reason
        self.thinking_capability = thinking_capability
        self.reasoning_content = reasoning_content
        self.reject_body_keys = tuple(reject_body_keys)
        self.content_none = content_none
        self.jit = jit
        self.strict_model = strict_model
        self.stream_chunk_delay_s = stream_chunk_delay_s
        self.reasoning_field = reasoning_field
        # G2b-1 önerisi: LM Studio yerel REST POST /api/v1/chat (varsayılan KAPALI;
        # açılırsa reasoning "off" isteyen düşünen modeller bu uca geçer).
        self.native_chat = native_chat
        self.reject_reasoning = reject_reasoning   # "reasoning" alanına 400 (ayar desteklenmiyor)
        self.host = host
        self.request_details: List[FakeRequest] = []
        self.max_concurrent = 0
        self._active = 0
        self._injections: Dict[str, List[list]] = {}   # path -> [[status, body, kalan], ...]
        self._delays: Dict[str, List[list]] = {}       # path -> [[saniye, kalan], ...]
        self._counter = 0
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        if start:
            self.start()

    # ----------------------------------------------------------------- yaşam
    def start(self) -> "FakeLLMServer":
        if self._httpd is not None:
            return self
        handler = _make_handler(self)
        self._httpd = _Server((self.host, 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        name=f"FakeLLMServer-{self.flavor}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Dinlemeyi bırak; sonraki bağlantılar anında reddedilir."""
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def __enter__(self) -> "FakeLLMServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ----------------------------------------------------------------- adres
    @property
    def port(self) -> int:
        if self._httpd is None:
            raise RuntimeError("sahte sunucu çalışmıyor")
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        """Sunucu kökü: ``http://127.0.0.1:<port>`` (sonda '/' yok)."""
        return f"http://{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        """Arka ucun taban adresi: ollama -> kök; openai/lmstudio -> kök + ``/v1``."""
        return self.url if self.flavor == "ollama" else self.url + "/v1"

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}"

    # ----------------------------------------------------------------- ayar
    def set_models(self, models: Iterable[Union[str, dict, FakeModel]]) -> None:
        with self._lock:
            self.models = [_normalize_model(m) for m in models]

    def model(self, name: str) -> Optional[FakeModel]:
        with self._lock:
            for m in self.models:
                if m.name == name:
                    return m
            if self.flavor == "ollama" and ":" not in name:   # qwen3 == qwen3:latest
                for m in self.models:
                    if m.name == name + ":latest":
                        return m
        return None

    def set_status(self, path: str, status: int, body: Any = None,
                   times: Optional[int] = None) -> None:
        """``path``'e gelen sonraki ``times`` isteğe (None = hep) ``status`` döndür.

        ``body``: dict/list -> JSON; str -> düz metin; None -> sunucu biçiminde
        ``"sahte hata <status>"`` iletisi. Birden çok çağrı sıraya girer.
        """
        with self._lock:
            self._injections.setdefault(path, []).append([status, body, times])

    def clear_status(self, path: Optional[str] = None) -> None:
        with self._lock:
            if path is None:
                self._injections.clear()
            else:
                self._injections.pop(path, None)

    def set_delay(self, path: str, seconds: float, times: Optional[int] = None) -> None:
        """``path``'e gelen sonraki ``times`` isteği (None = hep) ``seconds`` beklet."""
        with self._lock:
            self._delays.setdefault(path, []).append([seconds, times])

    # ----------------------------------------------------------------- kayıt
    @property
    def requests(self) -> List[Tuple[str, str, Any]]:
        """``[(method, path, json), ...]`` — gelen istekler, geliş sırasıyla."""
        with self._lock:
            return [(r.method, r.path, r.json) for r in self.request_details]

    def requests_to(self, path: str) -> List[Any]:
        """Belirli bir yola gelen isteklerin JSON gövdeleri."""
        with self._lock:
            return [r.json for r in self.request_details if r.path == path]

    def reset_requests(self) -> None:
        with self._lock:
            self.request_details.clear()
            self.max_concurrent = 0

    # ----------------------------------------------------------------- iç
    def _take_injection(self, path: str) -> Optional[Tuple[int, Any]]:
        with self._lock:
            queue = self._injections.get(path)
            if not queue:
                return None
            entry = queue[0]
            status, body, times = entry
            if times is not None:
                entry[2] = times - 1
                if entry[2] <= 0:
                    queue.pop(0)
            return status, body

    def _take_delay(self, path: str) -> float:
        with self._lock:
            queue = self._delays.get(path)
            if not queue:
                return 0.0
            entry = queue[0]
            seconds, times = entry
            if times is not None:
                entry[1] = times - 1
                if entry[1] <= 0:
                    queue.pop(0)
            return seconds

    def _next_id(self) -> int:
        with self._lock:
            self._counter += 1
            return self._counter

    def _call_reply(self, text: str, body: Any) -> FakeReply:
        fn = self.reply
        try:
            nparams = len(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            nparams = 1
        out = fn(text, body) if nparams >= 2 else fn(text)
        if isinstance(out, FakeReply):
            return out
        return FakeReply(content=out)

    def _has_thinking(self, m: FakeModel) -> bool:
        return self.thinking_capability if m.thinking is None else bool(m.thinking)

    def _decorate(self, content: Optional[str]) -> Optional[str]:
        if content is None or self.content_none:
            return None
        out = self.prefix + content
        if self.think_block:
            out = THINK_BLOCK + out
        return out


# ---------------------------------------------------------------------------
# İstek işleyici
# ---------------------------------------------------------------------------
def _make_handler(server: FakeLLMServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        srv = server

        def log_message(self, fmt, *args):   # stderr'e yazma
            pass

        # -------------------------------------------------------- giriş
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def _dispatch(self, method: str) -> None:
            srv = self.srv
            path = self.path.split("?", 1)[0]
            raw = b""
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {"_ham_govde": raw.decode("utf-8", "replace")}
            rec = FakeRequest(method=method, path=path, json=body,
                              headers={k: v for k, v in self.headers.items()},
                              t_start=time.monotonic())
            self._rec = rec
            self._finished = False
            with srv._lock:
                srv.request_details.append(rec)
                srv._active += 1
                srv.max_concurrent = max(srv.max_concurrent, srv._active)
            try:
                wait = srv.delay_s + srv._take_delay(path)
                if wait > 0:
                    time.sleep(wait)
                injected = srv._take_injection(path)
                if injected is not None:
                    status, ibody = injected
                    if ibody is None:
                        ibody = self._error_body(status, f"sahte hata {status}")
                    self._send(status, ibody)
                else:
                    self._route(method, path, body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                self._finish()

        def _finish(self) -> None:
            """İstek kaydını kapat (durum, bitiş anı, etkin sayaç) — bir kez.

            Yanıt gövdesi yazılmadan ÖNCE çağrılır: istemci yanıtı aldığında kayıt
            eksiksizdir (2026-09-28: eskiden ``finally``'de yazılıyordu; istemci
            ``t_end``'i 0 görebiliyordu — test_eszamanli_istek_sayaci yarışı).
            """
            if getattr(self, "_finished", True):
                return
            self._finished = True
            rec = self._rec
            rec.status = getattr(self, "_sent_status", 0)
            rec.t_end = time.monotonic()
            with self.srv._lock:
                self.srv._active -= 1

        # -------------------------------------------------------- çıkış
        def _send(self, status: int, payload: Any, content_type: Optional[str] = None) -> None:
            if isinstance(payload, (dict, list)):
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                ctype = content_type or "application/json; charset=utf-8"
            elif isinstance(payload, bytes):
                data = payload
                ctype = content_type or "application/octet-stream"
            else:
                data = str(payload).encode("utf-8")
                ctype = content_type or "text/plain; charset=utf-8"
            self._sent_status = status
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._finish()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _send_stream(self, lines: List[bytes], content_type: str) -> None:
            """Akışı parça parça yaz (Content-Length yok; HTTP/1.0 bağlantı sonu).

            İstemci kapatınca yazma hatası alınır ve durulur; kayıt parçayı sayar.
            """
            rec = getattr(self, "_rec", None)
            if rec is not None:
                rec.stream_total = len(lines)
            self._sent_status = 200
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for line in lines:
                    self.wfile.write(line)
                    self.wfile.flush()
                    if rec is not None:
                        rec.stream_chunks += 1
                    if self.srv.stream_chunk_delay_s:
                        time.sleep(self.srv.stream_chunk_delay_s)
            except (BrokenPipeError, ConnectionResetError, OSError):
                if rec is not None:
                    rec.stream_closed_early = True
                self._finish()
                return
            if rec is not None and rec.stream_chunks < rec.stream_total:
                rec.stream_closed_early = True
            self._finish()

        def _error_body(self, status: int, message: str) -> Any:
            if self.srv.flavor == "ollama":
                return {"error": message}
            code = "model_not_found" if status == 404 else None
            return {"error": {"message": message, "type": "invalid_request_error",
                              "param": None, "code": code}}

        def _fail(self, status: int, message: str) -> None:
            self._send(status, self._error_body(status, message))

        # -------------------------------------------------------- yönlendirme
        def _route(self, method: str, path: str, body: Any) -> None:
            srv = self.srv
            routes: Dict[Tuple[str, str], Callable[[Any], None]] = {}
            if srv.flavor == "ollama":
                routes = {
                    ("GET", "/"): self._ollama_root,
                    ("HEAD", "/"): self._ollama_root,
                    ("GET", "/api/version"): self._ollama_version,
                    ("GET", "/api/tags"): self._ollama_tags,
                    ("GET", "/api/ps"): self._ollama_ps,
                    ("POST", "/api/show"): self._ollama_show,
                    ("POST", "/api/chat"): self._ollama_chat,
                    ("POST", "/api/generate"): self._ollama_generate,
                }
            else:
                routes = {
                    ("GET", "/health"): lambda b: self._send(200, {"status": "ok"}),
                    ("GET", "/v1/models"): self._openai_models,
                    ("POST", "/v1/chat/completions"): self._openai_chat,
                    ("POST", "/v1/completions"): self._openai_completions,
                }
                if srv.flavor == "lmstudio":
                    routes[("GET", "/api/v1/models")] = self._lmstudio_v1_models
                    routes[("GET", "/api/v0/models")] = self._lmstudio_v0_models
                    if srv.native_chat:
                        routes[("POST", "/api/v1/chat")] = self._lmstudio_native_chat
            fn = routes.get((method, path))
            if fn is None:
                if srv.flavor == "ollama":
                    self._send(404, "404 page not found")
                elif srv.flavor == "lmstudio":
                    self._send(404, {"error": f"Unexpected endpoint or method. ({method} {path})"})
                else:
                    self._send(404, {"error": {"message": "File Not Found",
                                               "type": "not_found_error", "code": 404}})
                return
            fn(body if isinstance(body, dict) else {})

        # -------------------------------------------------------- ortak üretim
        def _produce(self, text: str, body: Any) -> Optional[FakeReply]:
            """reply'ı çağır; hata ya da gecikmeyi uygula. None -> yanıt gönderildi."""
            try:
                rep = self.srv._call_reply(text, body)
            except Exception as exc:   # sahte sunucu içi hata -> 500
                self._fail(500, f"{type(exc).__name__}: {exc}")
                return None
            if rep.delay_s > 0:
                time.sleep(rep.delay_s)
            if rep.status != 200:
                self._fail(rep.status, rep.error or f"sahte hata {rep.status}")
                return None
            return rep

        def _content_of(self, rep: FakeReply, text: str, explicit_none_ok: bool) -> Optional[str]:
            if rep.content is None:
                return None if explicit_none_ok else ""
            return self.srv._decorate(rep.content)

        # -------------------------------------------------------- Ollama
        def _ollama_root(self, body):
            self._send(200, "Ollama is running")

        def _ollama_version(self, body):
            self._send(200, {"version": FAKE_OLLAMA_VERSION})

        def _ollama_details(self, m: FakeModel) -> dict:
            return {"parent_model": "", "format": m.format, "family": m.family,
                    "families": [m.family], "parameter_size": m.parameter_size,
                    "quantization_level": m.quantization}

        def _ollama_tags(self, body):
            with self.srv._lock:
                models = list(self.srv.models)
            self._send(200, {"models": [
                {"name": m.name, "model": m.name, "modified_at": _CREATED_AT,
                 "size": m.size_bytes, "digest": _digest(m.name),
                 "details": self._ollama_details(m)}
                for m in models]})

        def _ollama_ps(self, body):
            with self.srv._lock:
                models = [m for m in self.srv.models if m.loaded]
            self._send(200, {"models": [
                {"name": m.name, "model": m.name, "size": m.size_bytes,
                 "digest": _digest(m.name),
                 "details": self._ollama_details(m), "expires_at": "2026-09-25T12:30:00Z",
                 "size_vram": m.size_bytes, "context_length": m.context_length}
                for m in models]})

        def _ollama_capabilities(self, m: FakeModel) -> List[str]:
            if m.kind == "embedding":
                return ["embedding"]
            caps = ["completion"]
            if m.vision:
                caps.append("vision")
            if self.srv._has_thinking(m):
                caps.append("thinking")
            return caps

        def _ollama_model_or_404(self, body) -> Optional[FakeModel]:
            name = body.get("model") or body.get("name") or ""
            m = self.srv.model(name)
            if m is None:
                self._fail(404, f"model \"{name}\" not found, try pulling it first")
            return m

        def _ollama_show(self, body):
            m = self._ollama_model_or_404(body)
            if m is None:
                return
            template = "{{ .Prompt }}"
            if self.srv._has_thinking(m):
                template = "{{ if .Think }}<think>{{ end }}{{ .Prompt }}"
            payload = {
                "modelfile": f"FROM {m.name}\n",
                "parameters": "temperature 0.6",
                "template": template,
                "details": self._ollama_details(m),
                "model_info": {"general.architecture": m.family,
                               "general.parameter_count": 8_190_735_360,
                               f"{m.family}.context_length": m.context_length},
                "capabilities": self._ollama_capabilities(m),
                "modified_at": _CREATED_AT,
            }
            if m.think_values is not None:
                payload["thinking"] = {"values": list(m.think_values),
                                       "default": m.think_default}
            self._send(200, payload)

        def _ollama_stream_guard(self, body, m: FakeModel, content: str,
                                 thinking: Optional[str] = None, finish: str = "stop",
                                 raw: bool = False) -> bool:
            """stream açıkça false değilse NDJSON akışı gönder (gerçek varsayılan)."""
            if body.get("stream") is False:
                return False

            def line(obj: dict) -> bytes:
                return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

            def part(text: str, key: str) -> dict:
                base = {"model": m.name, "created_at": _CREATED_AT, "done": False}
                if raw:
                    base["response"] = text if key == "content" else ""
                    if key == "thinking":
                        base["thinking"] = text
                else:
                    msg = {"role": "assistant", "content": text if key == "content" else ""}
                    if key == "thinking":
                        msg["thinking"] = text
                    base["message"] = msg
                return base

            thinks = stream_pieces(thinking)
            contents = stream_pieces(content)
            lines = [line(part(t, "thinking")) for t in thinks]
            lines += [line(part(c, "content")) for c in contents]
            final = {"model": m.name, "created_at": _CREATED_AT, "done": True,
                     "done_reason": finish, "eval_count": len(thinks) + len(contents)}
            if raw:
                final["response"] = ""
            else:
                final["message"] = {"role": "assistant", "content": ""}
            lines.append(line(final))
            self._send_stream(lines, "application/x-ndjson")
            return True

        def _ollama_check_think(self, body, m: FakeModel) -> bool:
            # Gerçek Ollama: think açıkken (true/"high"...) yeteneksiz model 400 döner.
            if body.get("think") not in (None, False) and not self.srv._has_thinking(m):
                self._fail(400, f"\"{m.name}\" does not support thinking")
                return False
            return True

        def _mark_loaded(self, m: FakeModel) -> None:
            with self.srv._lock:
                m.loaded = True

        def _ollama_chat(self, body):
            m = self._ollama_model_or_404(body)
            if m is None:
                return
            if m.kind == "embedding":
                self._fail(400, f"\"{m.name}\" does not support chat")
                return
            if not self._ollama_check_think(body, m):
                return
            messages = body.get("messages") or []
            if not messages:
                self._mark_loaded(m)
                self._send(200, {"model": m.name, "created_at": _CREATED_AT,
                                 "message": {"role": "assistant", "content": ""},
                                 "done_reason": "load", "done": True})
                return
            text = extract_chat_text(messages)
            rep = self._produce(text, body)
            if rep is None:
                return
            self._mark_loaded(m)
            content = self._content_of(rep, text, explicit_none_ok=False) or ""
            thinking = rep.thinking
            if thinking is None and body.get("think") is True:
                thinking = THINKING_TEXT
            finish = rep.finish_reason or self.srv.finish_reason
            if self._ollama_stream_guard(body, m, content, thinking, finish):
                return
            message = {"role": "assistant", "content": content}
            if thinking is not None:
                message["thinking"] = thinking
            self._send(200, {"model": m.name, "created_at": _CREATED_AT, "message": message,
                             "done_reason": finish, "done": True,
                             "total_duration": 1_000_000, "load_duration": 1_000,
                             "prompt_eval_count": _count_tokens(text),
                             "eval_count": _count_tokens(content)})

        def _ollama_generate(self, body):
            m = self._ollama_model_or_404(body)
            if m is None:
                return
            if m.kind == "embedding":
                self._fail(400, f"\"{m.name}\" does not support generate")
                return
            if not self._ollama_check_think(body, m):
                return
            prompt = body.get("prompt") or ""
            if not prompt:
                self._mark_loaded(m)
                self._send(200, {"model": m.name, "created_at": _CREATED_AT, "response": "",
                                 "done": True, "done_reason": "load"})
                return
            text = extract_prompt_text(prompt)
            rep = self._produce(text, body)
            if rep is None:
                return
            self._mark_loaded(m)
            content = self._content_of(rep, text, explicit_none_ok=False) or ""
            finish = rep.finish_reason or self.srv.finish_reason
            if self._ollama_stream_guard(body, m, content, rep.thinking, finish, raw=True):
                return
            self._send(200, {"model": m.name, "created_at": _CREATED_AT, "response": content,
                             "done": True, "done_reason": finish,
                             "prompt_eval_count": _count_tokens(text),
                             "eval_count": _count_tokens(content)})

        # -------------------------------------------------------- OpenAI / LM Studio
        def _visible_openai_models(self) -> List[FakeModel]:
            with self.srv._lock:
                models = list(self.srv.models)
            if self.srv.flavor == "lmstudio" and not self.srv.jit:
                models = [m for m in models if m.loaded]
            return models

        def _openai_models(self, body):
            self._send(200, {"object": "list", "data": [
                {"id": m.name, "object": "model", "created": _CREATED_TS,
                 "owned_by": "organization_owner"}
                for m in self._visible_openai_models()]})

        def _openai_model_or_error(self, body) -> Optional[FakeModel]:
            srv = self.srv
            name = body.get("model") or ""
            m = srv.model(name)
            if m is None and not srv.strict_model and srv.models:
                m = srv.models[0]   # llama.cpp server: model alanını yok sayar
            if m is None:
                self._fail(404, f"The model `{name}` does not exist.")
                return None
            if srv.flavor == "lmstudio" and not srv.jit and not m.loaded:
                self._fail(404, f"Model \"{name}\" is not loaded. Please load it first.")
                return None
            if m.kind == "embedding":
                self._fail(400, f"Model \"{name}\" is not a text generation model (embedding).")
                return None
            for key in srv.reject_body_keys:
                if key in body:
                    self._fail(400, f"Unrecognized request argument supplied: {key}")
                    return None
            return m

        def _openai_sse(self, payload: dict, content: Optional[str] = None,
                        reasoning: Optional[str] = None, raw: bool = False) -> None:
            """OpenAI akış biçimi: ``chat.completion.chunk`` delta olayları + [DONE]."""
            choice = payload["choices"][0]
            finish = choice.get("finish_reason")
            obj = "text_completion" if raw else "chat.completion.chunk"

            def event(delta_or_text: Any, fin: Optional[str] = None) -> bytes:
                ch: Dict[str, Any] = {"index": 0, "logprobs": None, "finish_reason": fin}
                if raw:
                    ch["text"] = delta_or_text
                else:
                    ch["delta"] = delta_or_text
                chunk = {"id": payload["id"], "object": obj, "created": payload["created"],
                         "model": payload["model"], "choices": [ch]}
                return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")

            lines: List[bytes] = []
            if not raw:
                lines.append(event({"role": "assistant", "content": ""}))
                for piece in stream_pieces(reasoning):
                    lines.append(event({self.srv.reasoning_field: piece}))
            for piece in stream_pieces(content):
                lines.append(event(piece if raw else {"content": piece}))
            lines.append(event("" if raw else {}, finish))
            lines.append(b"data: [DONE]\n\n")
            self._send_stream(lines, "text/event-stream")

        def _openai_chat(self, body):
            m = self._openai_model_or_error(body)
            if m is None:
                return
            text = extract_chat_text(body.get("messages") or [])
            rep = self._produce(text, body)
            if rep is None:
                return
            self._mark_loaded(m)
            content = self._content_of(rep, text, explicit_none_ok=True)
            finish = rep.finish_reason or self.srv.finish_reason
            max_tokens = body.get("max_tokens")
            if isinstance(max_tokens, int) and max_tokens <= 1 and content:
                content = content.split()[0] if content.split() else content[:1]
                finish = "length"
            message: Dict[str, Any] = {"role": "assistant", "content": content}
            reasoning = rep.thinking if rep.thinking is not None else self.srv.reasoning_content
            if reasoning is not None:
                message[self.srv.reasoning_field] = reasoning
            payload = {"id": f"chatcmpl-sahte-{self.srv._next_id()}", "object": "chat.completion",
                       "created": _CREATED_TS, "model": m.name,
                       "choices": [{"index": 0, "message": message, "logprobs": None,
                                    "finish_reason": finish}],
                       "usage": {"prompt_tokens": _count_tokens(text),
                                 "completion_tokens": _count_tokens(content or ""),
                                 "total_tokens": _count_tokens(text) + _count_tokens(content or "")}}
            if body.get("stream") is True:
                self._openai_sse(payload, content, reasoning)
                return
            self._send(200, payload)

        def _openai_completions(self, body):
            m = self._openai_model_or_error(body)
            if m is None:
                return
            text = extract_prompt_text(body.get("prompt") or "")
            rep = self._produce(text, body)
            if rep is None:
                return
            self._mark_loaded(m)
            content = self._content_of(rep, text, explicit_none_ok=False) or ""
            finish = rep.finish_reason or self.srv.finish_reason
            payload = {"id": f"cmpl-sahte-{self.srv._next_id()}", "object": "text_completion",
                       "created": _CREATED_TS, "model": m.name,
                       "choices": [{"index": 0, "text": content, "logprobs": None,
                                    "finish_reason": finish}],
                       "usage": {"prompt_tokens": _count_tokens(text),
                                 "completion_tokens": _count_tokens(content),
                                 "total_tokens": _count_tokens(text) + _count_tokens(content)}}
            if body.get("stream") is True:
                self._openai_sse(payload, content, raw=True)
                return
            self._send(200, payload)

        def _publisher(self, m: FakeModel) -> str:
            if m.publisher:
                return m.publisher
            return m.name.split("/", 1)[0] if "/" in m.name else "yerel"

        def _lmstudio_v1_models(self, body):
            with self.srv._lock:
                models = list(self.srv.models)
            out = []
            for m in models:
                item: Dict[str, Any] = {
                    "type": "embedding" if m.kind == "embedding" else "llm",
                    "publisher": self._publisher(m),
                    "key": m.name,
                    "display_name": m.name.split("/")[-1],
                    "quantization": {"name": m.quantization, "bits_per_weight": 4},
                    "size_bytes": m.size_bytes,
                    "params_string": m.parameter_size,
                    "loaded_instances": ([{"id": m.name, "config": {
                        "context_length": 4096, "eval_batch_size": 512, "parallel": 4,
                        "flash_attention": True}}] if m.loaded else []),
                    "max_context_length": m.context_length,
                    "format": m.format,
                    "description": None,
                }
                if m.kind != "embedding":
                    item["architecture"] = m.family
                    caps: Dict[str, Any] = {"vision": m.vision, "trained_for_tool_use": True}
                    if self.srv._has_thinking(m):
                        options = (list(m.reasoning_options) if m.reasoning_options is not None
                                   else ["off", "on"])
                        default = "on" if "on" in options else (options[-1] if options else None)
                        caps["reasoning"] = {"allowed_options": options, "default": default}
                    item["capabilities"] = caps
                out.append(item)
            self._send(200, {"models": out})

        def _lmstudio_native_chat(self, body):
            """LM Studio yerel REST ``POST /api/v1/chat`` (https://lmstudio.ai/docs/developer/rest/chat).

            Akışta ``event: <tür>`` + ``data: {"type": <tür>, ...}``; düz yanıtta
            ``output[]`` + ``stats``. ``reasoning: "off"`` UYGULANMAZ: yanıttaki
            düşünce ``FakeReply.thinking``'den gelir (kapatma tutmadı senaryosu).
            """
            model = self.srv.model(str(body.get("model") or ""))
            if model is None:
                return self._send(404, {"error": "model yok"})
            if self.srv.reject_reasoning and "reasoning" in body:
                return self._send(400, {"error": "reasoning setting not supported"})
            rep = self._produce(str(body.get("input") or ""), body)
            if rep is None:
                return None
            content = self._content_of(rep, "", False) or ""
            thinking = rep.thinking
            pieces = stream_pieces(content)
            think_pieces = stream_pieces(thinking) if thinking else []
            stats = {"input_tokens": _count_tokens(str(body.get("input") or "")),
                     "total_output_tokens": len(pieces) + len(think_pieces),
                     "reasoning_output_tokens": len(think_pieces),
                     "tokens_per_second": 40.0, "time_to_first_token_seconds": 0.1}
            if body.get("stream"):
                def event(kind, **data):
                    obj = {"type": kind, **data}
                    return (f"event: {kind}\n"
                            f"data: {json.dumps(obj, ensure_ascii=False)}\n\n").encode("utf-8")
                lines = [event("chat.start", model_instance_id=model.name)]
                lines += [event("reasoning.delta", content=p) for p in think_pieces]
                lines.append(event("message.start"))
                lines += [event("message.delta", content=p) for p in pieces]
                lines.append(event("message.end"))
                lines.append(event("chat.end", result={
                    "model_instance_id": model.name, "output": [], "stats": stats}))
                return self._send_stream(lines, "text/event-stream")
            output = [{"type": "reasoning", "content": thinking}] if thinking else []
            output.append({"type": "message", "content": content})
            return self._send(200, {"model_instance_id": model.name, "output": output,
                                    "stats": stats})

        def _lmstudio_v0_models(self, body):
            with self.srv._lock:
                models = list(self.srv.models)
            self._send(200, {"object": "list", "data": [
                {"id": m.name, "object": "model",
                 "type": "embeddings" if m.kind == "embedding" else ("vlm" if m.vision else "llm"),
                 "publisher": self._publisher(m), "arch": m.family,
                 "compatibility_type": m.format, "quantization": m.quantization,
                 "state": "loaded" if m.loaded else "not-loaded",
                 "max_context_length": m.context_length}
                for m in models]})

    return Handler
