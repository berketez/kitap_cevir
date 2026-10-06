"""Motor katmanının kulvarlar arası sözleşmesi: veri yapıları, hatalar, iletiler.

Bu dosyayı yalnız ana oturum (K0) değiştirir; kulvar ajanları okur.
Hafif modüldür: yalnız stdlib + ``config.defaults`` içe aktarır (mlx, torch,
requests, httpx, openai, llama_cpp YOK). ``from core.mt_engines.contracts
import *`` her yerden güvenle yapılabilir.

Kulvarlar için kurallar
-----------------------
- Kullanıcıya gösterilen her ileti buradaki ``MSG_*`` kalıplarından
  ``str.format`` ile üretilir; komut satırı ve arayüz AYNI metni gösterir.
- Motor / arka uç hata iletileri ``OOM_TRIGGER_WORDS`` alt dizgilerini
  ('out of memory', 'oom', 'cuda', 'mps') içermez: core/translator.py'nin
  transformers OOM-bölme dalı bu alt dizgilere bakar. Dış kaynaklı metin
  (sunucu hata gövdesi, model adı) bir istisna iletisine gömülecekse önce
  ``oom_safe()`` ile etkisizleştirilir.
- Hedef dil YALNIZ Türkçe; "Yerel model ile çevir" yolunun kaynak dili
  ``core.lang_detect.TEX_SOURCE_LANGS``'teki altı dilden biri (en, de, ru,
  fr, es, it; KARARLAR 14 — dil kapısı translate_book'ta). Hiçbir yere hedef
  dil seçeneği eklenmez.
- Yalnız bu bilgisayar: her sunucu adresi ``http_util.check_local_url``'den
  geçer (127.0.0.0/8, ::1, localhost). API anahtarı / Authorization yoktur;
  ``mt_sources.json`` bunu açamaz ve model klasörü EKLEYEMEZ (taranan tek
  dosya kökü projenin ``models/`` klasörüdür; başka yerdeki model oraya
  sembolik bağla konur).
- Yeni modüller (backends, http_backends, llamacpp_backend, discovery)
  paket ``__init__``'ten dışa aktarılmaz; doğrudan alt modülden içe aktarılır
  (döngüsel içe aktarma ve ağır bağımlılık riski olmasın).

Test yalıtım sözleşmesi (tests/conftest.py ``_mt_isolation``, autouse)
------------------------------------------------------------------------
Fixture aşağıdaki noktaları yamalar; ürün kodu bu adları çağrı anında MODÜL
ÖZNİTELİĞİ olarak okumalıdır (``from x import AD`` ile bağlanan kopya
yamalanmaz):

- ``core.mt_engines.registry.MODELS_DIR`` → boş bir tmp klasör (model kökü;
  keşif ve ``mlx:``/``gguf:`` kimlik çözümü bunu ``registry.MODELS_DIR``
  olarak okur).
- ``core.mt_engines.registry.USER_SPECS_PATH`` → var olmayan tmp dosya.
- ``core.mt_engines.discovery.load_sources`` (modül varsa) → ``path=None``
  çağrısı kapalı bir yerel porta işaret eden ``SourcesConfig`` döner; açık
  ``path`` verilirse gerçek fonksiyon koşar. Çağıranlar
  ``discovery.load_sources(...)`` biçiminde çağırır.
- ``ui.runner.engine_choice_path`` → tmp ``mt_selection.json`` yolu
  döndüren fonksiyon (``load_engine_choice`` / ``save_engine_choice`` bu
  fonksiyonu çağrı anında çağırır).
- ``sys.modules['llama_cpp'] = None`` (kurulu değilmiş gibi); ``fake_llama``
  fixture'ı sahte modülü koyar. Ürün kodu llama_cpp'yi yalnız
  ``importlib.util.find_spec`` + fonksiyon içi ``import`` ile arar.
- ``socket.create_connection``: loopback üzerindeki 11434 ve 1234
  portlarına bağlantı ``ConnectionRefusedError`` alır (testler gerçek
  Ollama / LM Studio sunucusuna ASLA gitmez).
``@pytest.mark.yerel_model`` / ``integration`` işaretli testlerde yalıtım
uygulanmaz (gerçek model testleri, kullanıcı onayıyla).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from config.defaults import LMSTUDIO_DEFAULT_URL, OLLAMA_DEFAULT_URL

# ---------------------------------------------------------------------------
# Arka uç × biçim (TASARIM B.1)
# ---------------------------------------------------------------------------
BACKEND_MLX = "mlx"            # süreç içi MLX (bugünkü HY-MT yolu, değişmez)
BACKEND_OLLAMA = "ollama"      # Ollama yerel API (/api/chat, /api/generate)
BACKEND_OPENAI = "openai"      # OpenAI uyumlu HTTP: LM Studio, llama.cpp server, Jan, vLLM
BACKEND_LLAMACPP = "llamacpp"  # süreç içi llama-cpp-python, doğrudan .gguf
BACKENDS = (BACKEND_MLX, BACKEND_OLLAMA, BACKEND_OPENAI, BACKEND_LLAMACPP)

# Arka ucun ekranda / günlükte görünen adı (describe(), iletiler)
BACKEND_LABELS: Dict[str, str] = {
    BACKEND_MLX: "MLX",
    BACKEND_OLLAMA: "Ollama",
    BACKEND_OPENAI: "OpenAI uyumlu sunucu",
    BACKEND_LLAMACPP: "llama.cpp",
}

FORMAT_HYMT = "hymt"           # HY-MT özel-token ham prompt'u
FORMAT_CHAT = "chat"           # sohbet mesajları; şablonu tokenizer / sunucu uygular
FORMATS = (FORMAT_HYMT, FORMAT_CHAT)

# EngineSpec.flavor (yalnız openai arka ucu); "" = genel OpenAI uyumlu sunucu
FLAVOR_LMSTUDIO = "lmstudio"
FLAVOR_GENERIC = ""

# EngineSpec.source
SPEC_SOURCE_BUILTIN = "builtin"   # kodla gelen (hymt-7b)
SPEC_SOURCE_USER = "user"         # config/mt_engines.json kaydı
SPEC_SOURCE_ADHOC = "adhoc"       # önekli kimlikten sentezlenen (mlx:, ollama:, ...)
SPEC_SOURCES = (SPEC_SOURCE_BUILTIN, SPEC_SOURCE_USER, SPEC_SOURCE_ADHOC)

# EngineSpec.options için izinli anahtarlar (registry doğrular, arka uç okur)
ENGINE_OPTION_KEYS = frozenset({
    "num_ctx",            # ollama: bağlam uzunluğu (koşu boyunca sabit)
    "keep_alive",         # ollama: modelin bellekte kalma süresi ("30m")
    "max_parallel",       # http: toplu üretimde eşzamanlı istek
    "n_ctx",              # llamacpp: bağlam uzunluğu
    "n_gpu_layers",       # llamacpp: GPU'ya alınan katman (-1 = hepsi)
    "temperature",
    "repeat_penalty",
    "seed",
    "request_timeout_s",  # tek üretim isteği zaman aşımı (MT_HTTP_TIMEOUT_S yerine)
    "think_budget",       # parça başına düşünme bütçesi (token; MT_THINK_BUDGET_TOKENS yerine)
})

# Düşünen modellerde düşünme varsayılan KAPALI (KARARLAR 4). MLX
# apply_chat_template'e ve openai istek gövdesine "chat_template_kwargs"
# olarak geçer; şablon bu değişkeni kullanmıyorsa yok sayılır.
THINKING_OFF_TEMPLATE_KWARGS: Dict[str, Any] = {"enable_thinking": False}


def default_chat_template_kwargs() -> Dict[str, Any]:
    """EngineSpec.chat_template_kwargs varsayılanı (her çağrıda yeni sözlük)."""
    return dict(THINKING_OFF_TEMPLATE_KWARGS)


# ---------------------------------------------------------------------------
# Düşünme sınıfı (G2b-1, 2026-10-05). Berke'nin 5 Ekim kararı (KARARLAR 16'yı
# kısmen değiştirir): "Düşünme kapatılabiliyorsa kapatılır; kapatılamıyorsa
# model düşünerek çevirecek." Sınıflandırma kuralları ve resmî kaynaklar
# core/mt_engines/thinking.py'dedir. Değerler kimliktir (ekranda gösterilmez;
# kullanıcıya dönük kısa metinler ui/branding.py THINKING_LABELS).
# ---------------------------------------------------------------------------
THINKING_NONE = "yok"                    # düşünmeyen model
THINKING_CAN_DISABLE = "kapatilabilir"   # düşünen model, resmî kapatma yolu var
THINKING_ALWAYS = "kapatilamaz"          # düşünen model, resmî kapatma yolu yok: düşünerek çevirir
THINKING_UNKNOWN = "bilinmiyor"          # kaynak düşünme bilgisi vermiyor
THINKING_CLASSES = (THINKING_NONE, THINKING_CAN_DISABLE, THINKING_ALWAYS, THINKING_UNKNOWN)
# Resmî kapatma yolu (DiscoveredModel.thinking_off; yalnız "kapatilabilir"de dolu)
THINKING_OFF_LMSTUDIO_REST = "lmstudio_rest_reasoning_off"     # LM Studio POST /api/v1/chat reasoning "off"
THINKING_OFF_OLLAMA_THINK = "ollama_think_false"              # Ollama /api/chat think: false
THINKING_OFF_TEMPLATE_SWITCH = "chat_template_enable_thinking"  # MLX apply_chat_template enable_thinking=False
THINKING_OFF_QWEN3_SOFT = "qwen3_no_think"                    # Qwen3 karma "/no_think" yumuşak anahtarı
THINKING_OFF_PATHS = (THINKING_OFF_LMSTUDIO_REST, THINKING_OFF_OLLAMA_THINK,
                      THINKING_OFF_TEMPLATE_SWITCH, THINKING_OFF_QWEN3_SOFT)


# ---------------------------------------------------------------------------
# Keşif kaynakları (DiscoveredModel.source / SourceStatus.source)
# ---------------------------------------------------------------------------
SOURCE_BUILTIN, SOURCE_USER = "builtin", "user"          # yerleşik / config/mt_engines.json kaydı
SOURCE_MLX, SOURCE_GGUF = "mlx", "gguf"                  # dosya sistemi (yalnız models/)
SOURCE_OLLAMA, SOURCE_LMSTUDIO, SOURCE_OPENAI = "ollama", "lmstudio", "openai"
# DiscoveryReport.models sırası
DISCOVERY_SOURCE_ORDER = (SOURCE_BUILTIN, SOURCE_USER, SOURCE_MLX, SOURCE_GGUF,
                          SOURCE_OLLAMA, SOURCE_LMSTUDIO, SOURCE_OPENAI)
# SourceStatus.label (genel OpenAI sunucularında etiket "<ad> (<host:port>)")
SOURCE_STATUS_LABELS: Dict[str, str] = {
    SOURCE_MLX: "MLX klasörleri",
    SOURCE_GGUF: "GGUF dosyaları",
    SOURCE_OLLAMA: "Ollama",
    SOURCE_LMSTUDIO: "LM Studio",
}

# ---------------------------------------------------------------------------
# Motor kimliği (TASARIM C.1)
# ---------------------------------------------------------------------------
PREFIX_MLX = "mlx"            # mlx:<klasör>            -> backend mlx
PREFIX_GGUF = "gguf"          # gguf:<dosya.gguf>       -> backend llamacpp
PREFIX_OLLAMA = "ollama"      # ollama:<model>          -> backend ollama
PREFIX_LMSTUDIO = "lmstudio"  # lmstudio:<model>        -> backend openai, flavor lmstudio
PREFIX_OPENAI = "openai"      # openai:<adres>|<model>  -> backend openai
ID_PREFIXES = (PREFIX_MLX, PREFIX_GGUF, PREFIX_OLLAMA, PREFIX_LMSTUDIO, PREFIX_OPENAI)
PREFIX_BACKEND: Dict[str, str] = {
    PREFIX_MLX: BACKEND_MLX,
    PREFIX_GGUF: BACKEND_LLAMACPP,
    PREFIX_OLLAMA: BACKEND_OLLAMA,
    PREFIX_LMSTUDIO: BACKEND_OPENAI,
    PREFIX_OPENAI: BACKEND_OPENAI,
}
ID_PREFIX_SEP = ":"           # önek ayracı (ilk ':'; sonrası AYNEN değerdir)
OPENAI_ID_SEP = "|"           # openai:<adres>|<model> (adres ':' içerdiği için)
# Adlı motor (builtin / mt_engines.json kaydı): ':' İÇEREMEZ
ENGINE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MODEL_NAME_MAX_LEN = 200      # önekli kimlikte model adı üst sınırı
GGUF_SUFFIX = ".gguf"
HYMT_NAME_RE = re.compile(r"(?i)hy[-_]?mt")              # ad/dosya adından biçim tahmini


def guess_format(name: str) -> str:
    """Klasör/dosya/model adından biçim tahmini: HY-MT ise 'hymt', değilse 'chat'."""
    return FORMAT_HYMT if HYMT_NAME_RE.search(name or "") else FORMAT_CHAT


def build_engine_id(prefix: str, value: str, url: str = "") -> str:
    """Önekli kimlik kur (ayrıştırma ve doğrulama registry.resolve_spec'tedir).

    ``build_engine_id("ollama", "qwen3:8b")`` -> ``"ollama:qwen3:8b"``;
    ``build_engine_id("openai", "qwen", "http://127.0.0.1:8080/v1")`` ->
    ``"openai:http://127.0.0.1:8080/v1|qwen"``.
    """
    if prefix not in ID_PREFIXES:
        raise ValueError(MSG_BAD_ID.format(
            engine_id=f"{prefix}{ID_PREFIX_SEP}{value}",
            why=f"bilinmeyen önek {prefix!r}"))
    if prefix == PREFIX_OPENAI:
        if not url:
            raise ValueError(MSG_BAD_ID.format(
                engine_id=f"{prefix}{ID_PREFIX_SEP}{value}",
                why="openai kimliği sunucu adresi ister"))
        return f"{prefix}{ID_PREFIX_SEP}{url}{OPENAI_ID_SEP}{value}"
    return f"{prefix}{ID_PREFIX_SEP}{value}"


# ---------------------------------------------------------------------------
# Yapılandırma dosyaları (config/ altında; isteğe bağlı, git DIŞI; docs/YENI_MODEL_EKLEME.md)
# ---------------------------------------------------------------------------
MT_SELECTION_FILENAME = "mt_selection.json"   # arayüzün son seçimi (ui.runner)
MT_SOURCES_FILENAME = "mt_sources.json"       # yerel sunucu adresleri (discovery)
MT_ENGINES_FILENAME = "mt_engines.json"       # kullanıcı motor kayıtları (registry)


# ---------------------------------------------------------------------------
# Bellek hatası alt dizgisi koruması
# ---------------------------------------------------------------------------
# core/translator.py transformers dalı RuntimeError iletisinde bunlardan biri
# geçerse batch'i ikiye böler; arka uç iletileri bunları İÇERMEZ.
OOM_TRIGGER_WORDS = ("out of memory", "oom", "cuda", "mps")
_OOM_TRIGGER_RE = re.compile("|".join(re.escape(w) for w in OOM_TRIGGER_WORDS),
                             re.IGNORECASE)
_OUT_OF_MEMORY_RE = re.compile(r"out\s+of\s+memory", re.IGNORECASE)
_ZERO_WIDTH_SPACE = "​"


def has_oom_trigger(text: str) -> bool:
    """Metin OOM-bölme dalını tetikleyecek bir alt dizgi içeriyor mu."""
    return bool(_OOM_TRIGGER_RE.search(text or ""))


def oom_safe(text: str) -> str:
    """Dış kaynaklı metni istisna iletisine gömmeye hazırla.

    'out of memory' -> 'bellek yetersiz'; kalan tetikleyicilerin ('oom',
    'cuda', 'mps' — ör. "bloom", "CUDA") ilk harfinden sonra sıfır genişlikli
    boşluk eklenir: görünen metin aynı kalır, alt dizgi eşleşmez.
    """
    if not text:
        return text or ""
    result = _OUT_OF_MEMORY_RE.sub("bellek yetersiz", text)
    for _ in range(8):   # ekleme yeni eşleşme doğurmaz; sınır yalnız güvence
        if not _OOM_TRIGGER_RE.search(result):
            break
        result = _OOM_TRIGGER_RE.sub(
            lambda m: m.group(0)[0] + _ZERO_WIDTH_SPACE + m.group(0)[1:], result)
    return result


# ---------------------------------------------------------------------------
# Hatalar
# ---------------------------------------------------------------------------
class BackendUnavailable(RuntimeError):
    """Sunucu kapalı, model yok, modül kurulu değil, yol izinsiz... (Türkçe ileti).

    RuntimeError alt sınıfı: mevcut ``except RuntimeError`` yakalamaları
    çalışmaya devam eder. İleti bilerek 'oom/cuda/mps/out of memory' içermez
    (bellek hatasında da ``MSG_OUT_OF_MEMORY_TR`` kullanılır ve ``detail``
    ``oom_safe``'ten geçer). ``hint``: başlatma / kurulum ipucu ya da "".
    """

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint
        # Toplu üretim (``MTBackend.generate_batch``) arka uç yarıda
        # kullanılamaz olunca doldurur (2026-09-29, F4-07): girdi sırasıyla
        # istek başına sonuç; kopmadan ÖNCE tamamlanan öğenin
        # ``GenerationResult``'ı, denenmemiş öğe için None. Boş demet = toplu
        # üretim dışı (tekil çağrı, ön kontrol). Çağıran başarılı sonuçları
        # atmadan kullanabilir (kayıpsızlık); istisna yine yukarı çıkar.
        self.partial_results: Tuple[Optional["GenerationResult"], ...] = ()


class ThinkingBudgetExceeded(BackendUnavailable):
    """Düşünme devre kesicisi: model düşünmesini bitirip cevap üretemiyor.

    G2b-2 (2026-10-05, KARARLAR 22; eski anlam KARARLAR 16 "düşünmeyi
    kapatmıyor" idi): düşünerek çeviride son ``MT_THINK_BREAKER_WINDOW``
    parçanın (en az ``MT_THINK_BREAKER_MIN_SAMPLES`` örnek) en az
    ``MT_THINK_BREAKER_RATIO`` oranında düşünme payı dolup yeniden denemede de
    cevap gelmezse arka uç devresi bu türle açılır (pencere koşu boyunca kayar;
    kısa parçalar ayrı pencerede). Düşünme payını aşmak tek başına devreyi
    açmaz. ``BackendUnavailable`` alt sınıfıdır: çevirmen yutmaz, translate_book
    tanır ve çıkış 5 (EXIT_MODEL_UNAVAILABLE) verir; ileti
    ``ui.branding.THINKING_NO_ANSWER_MESSAGE`` (``backends.thinking_no_answer_reason``).
    Ad geriye uyum için korunur.
    """


class GenerationError(RuntimeError):
    """Tek bir üretim başarısız (zaman aşımı, bağlam aşımı, boş çıktı). Devreyi açmaz."""


# ---------------------------------------------------------------------------
# Veri yapıları
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GenerationRequest:
    instruction: str          # _build_translation_instruction çıktısı (yer tutucu cümlesi dahil)
    text: str                 # çevrilecek metin (sanitize motorda: build_* içinde)
    max_tokens: int           # çeviri (yanıt) üst sınırı (_scale_max_tokens)
    # --- Deneme başına (G2b-2, 2026-10-05): MTBackend.generate her denemede
    # doldurur; çevirmenin kurduğu istekte None kalır (arka ucun varsayılanı). ---
    # Bu denemenin düşünme sınırı (token): düşünerek çeviride parçanın düşünme
    # payı, değilse yoklama bütçesi (MT_THINK_BUDGET_TOKENS). İstek token sınırı
    # = max_tokens + think_budget (akışlı istekte).
    think_budget: Optional[int] = None
    # Düşünerek çeviride parçanın (bütün denemeleri) öğe başından itibaren süre
    # sınırı (sn); zaman aşımı iletisinde gösterilir. None: istek zaman aşımı.
    time_limit_s: Optional[float] = None


@dataclass(frozen=True)
class GenerationResult:
    text: str = ""
    finish_reason: Optional[str] = None     # "stop" | "length" | None (bilinmiyor)
    error: Optional[str] = None             # Türkçe; None = başarılı
    # --- Ölçüm alanları (2026-09-28, KARARLAR 15-16; yalnız gerçek ölçüm) ---
    # Yanıtın GenerationRequest.max_tokens sınırına sayılan token sayısı:
    # akışta yanıt (düşünme dışı) parçası sayısı, akışsız yanıtta sunucunun
    # üretim sayacı (Ollama eval_count, OpenAI usage.completion_tokens; düşünme
    # de varsa yalnız sunucu düşünme sayacı verdiyse fark). Bilinmiyorsa None
    # (MLX HY-MT yolunda çevirmen kendisi sayar).
    output_tokens: Optional[int] = None
    # Düşünme token'ı (tüm denemeler toplamı): akış parçası ya da sunucunun
    # düşünme sayacı; ikisi de yoksa düşünme metninin karakterinden tahmin
    # (thinking_estimated True). 0 = düşünme görülmedi.
    thinking_tokens: int = 0
    thinking_estimated: bool = False
    thinking_exceeded: bool = False          # son deneme düşünme bütçesini aştı
    think_attempts_exceeded: int = 0         # bütçeyi aşan deneme sayısı (yeniden deneme dahil)
    think_retries: int = 0                   # sert kapatmalı yeniden deneme sayısı
    # Düşünme metninde etiket dışı gerçek içerik var mı (G2b-1, 2026-10-05): boş
    # "<think>\n\n</think>" bloğu (ör. Qwen3 "/no_think" yanıtı) düşünce sayılmaz;
    # kapatma doğrulaması (thinking.ThinkingOffMonitor) buna bakar.
    thinking_substantive: bool = False
    # G2b-2 (2026-10-05, KARARLAR 22): son denemenin düşünme sınırı (token;
    # düşünerek çeviride parçanın payı) ve ÖLÇÜLEN düşünme süresi (sn: istek
    # başından son düşünme parçasına kadar; yalnız akışta ölçülür, akışsız
    # yanıtta None — tahmin yazılmaz).
    think_budget: int = 0
    thinking_seconds: Optional[float] = None


@dataclass(frozen=True)
class DiscoveredModel:
    engine_id: str            # --mt-engine'e AYNEN verilecek kimlik
    label: str                # ekrandaki sade ad (ör. "qwen3:8b", "Qwen3.8-27B-Uncensored-MLX")
    source: str               # SOURCE_*
    backend: str              # BACKENDS
    format_guess: str         # FORMATS
    usable: bool
    reason: str = ""          # usable False ise Türkçe neden; True ise ""
    size_bytes: Optional[int] = None   # dosya boyutu / sunucunun bildirdiği; bilinmiyorsa None
    detail: str = ""          # gerçek üst veri: "27B · Q4_K_M", "mimari: qwen3_5"; yoksa ""
    location: str = ""        # dosya yolu ya da sunucu adresi
    reasoning: Optional[bool] = None   # düşünen model belirtisi; bilinmiyorsa None
    loaded: Optional[bool] = None      # sunucuda şu an bellekte mi; bilinmiyorsa None
    # --- G2b-1 (2026-10-05). None = değerlendirilmedi (model bulunamadı, sunucu
    # kapalı); keşif JSON'unda (discovery.report_to_json) None olan bu üç alan
    # yazılmaz, TASARIM D.4 biçimi değişmez. ---
    thinking: Optional[str] = None      # THINKING_CLASSES (core/mt_engines/thinking.py)
    thinking_off: Optional[str] = None  # THINKING_OFF_PATHS; yalnız "kapatilabilir"de dolu
    # LM Studio'nun modeli YÜKLEDİĞİ bağlam (REST loaded_instances[0].config.context_length;
    # MT-15: istenen değer olmayabilir); yüklü değilse ya da bilinmiyorsa None
    loaded_context: Optional[int] = None


@dataclass(frozen=True)
class SourceStatus:
    source: str               # SOURCE_OLLAMA | SOURCE_LMSTUDIO | SOURCE_OPENAI | SOURCE_MLX | SOURCE_GGUF
    label: str                # "Ollama", "LM Studio", "llama.cpp (127.0.0.1:8080)", "MLX klasörleri", "GGUF dosyaları"
    reachable: Optional[bool] # HTTP kaynaklar için; dosya kaynaklarında None
    message: str              # Türkçe tek satır: "3 model", "sunucu kapalı (…)", "llama-cpp-python kurulu değil …"
    location: str = ""


@dataclass(frozen=True)
class DiscoveryReport:
    models: Tuple[DiscoveredModel, ...]   # sıra: DISCOVERY_SOURCE_ORDER
    sources: Tuple[SourceStatus, ...]
    scanned_at: float                     # time.time()

    def model_by_id(self, engine_id: str) -> Optional[DiscoveredModel]:
        """Kimliği birebir eşleşen ilk model; yoksa None."""
        for model in self.models:
            if model.engine_id == engine_id:
                return model
        return None


@dataclass(frozen=True)
class EngineCheck:
    engine_id: str
    ok: bool
    label: str
    reason: str = ""          # Türkçe; ok ise ""
    hint: str = ""            # başlatma/kurulum ipucu; yoksa ""


@dataclass(frozen=True)
class OpenAIServer:
    """config/mt_sources.json 'openai_servers' girdisi (llama.cpp server, Jan, vLLM)."""
    name: str                 # ekrandaki ad ("llama.cpp")
    url: str                  # check_local_url'den geçmiş taban adres (".../v1")


@dataclass(frozen=True)
class SourcesConfig:
    """discovery.load_sources() sonucu (config/mt_sources.json; yoksa varsayılanlar).

    KARARLAR 1-2: yalnız yerel sunucu adreslerini taşır; model klasörü
    EKLENEMEZ (tarama kökü her zaman registry.MODELS_DIR), uzak adres
    kabul edilmez. Bozuk dosya fail-open: uyarı + varsayılanlar.
    """
    ollama_url: str = OLLAMA_DEFAULT_URL
    ollama_enabled: bool = True
    lmstudio_url: str = LMSTUDIO_DEFAULT_URL
    lmstudio_enabled: bool = True
    openai_servers: Tuple[OpenAIServer, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# İleti kataloğu (CLI + arayüz aynı metni kullanır; str.format ile doldurulur)
# ---------------------------------------------------------------------------
LLAMA_CPP_INSTALL_CMD = 'CMAKE_ARGS="-DGGML_METAL=on" ./venv/bin/pip install llama-cpp-python'

MSG_UNKNOWN_ENGINE = ("Desteklenmeyen MT motoru: {engine_id!r}. Kullanılabilir adlar: {names}. "
                      "Kimlik biçimleri: mlx:<klasör>, gguf:<dosya>, ollama:<model>, "
                      "lmstudio:<model>, openai:<adres>|<model>. Ayrıntı: docs/YENI_MODEL_EKLEME.md")
MSG_BAD_ID = "Geçersiz model kimliği {engine_id!r}: {why}."
MSG_BAD_URL = "Geçersiz sunucu adresi {url!r}: {why}."
MSG_REMOTE_REFUSED = ("Yalnız bu bilgisayardaki sunuculara bağlanılır; {url} yerel bir adres değil.")
MSG_PATH_NOT_ALLOWED = "Model yolu izinli klasörlerin dışında: {path}. İzinli klasörler: {roots}."
MSG_BUILTIN_MISSING = "Çeviri modeli kurulu değil: {path}."
MSG_MLX_MISSING = "MLX model klasörü bulunamadı: {path}."
MSG_MLX_INCOMPLETE = "MLX klasöründe model dosyaları eksik ({missing}): {path}."
MSG_MLX_ARCH_UNSUPPORTED = ("Kurulu mlx-lm bu mimariyi tanımıyor ({arch}); mlx-lm güncellenmeli "
                            "ya da model LM Studio üzerinden kullanılmalı.")
MSG_GGUF_MISSING = "GGUF dosyası bulunamadı: {path}."
MSG_GGUF_INVALID = "Geçerli bir GGUF model dosyası değil ({why}): {path}."
MSG_GGUF_LOAD_FAILED = ("GGUF modeli llama-cpp-python ile yüklenemedi ({why}): {path}. Kurulu "
                        "llama.cpp bu mimariyi tanımıyor olabilir; model Ollama ya da LM Studio "
                        "üzerinden denenebilir.")
MSG_LLAMA_MISSING = ("GGUF dosyasını doğrudan açmak için llama-cpp-python gerekli ama kurulu değil. "
                     "Kurulum: " + LLAMA_CPP_INSTALL_CMD)
MSG_OLLAMA_DOWN = ("Ollama sunucusu kapalı ya da erişilemiyor ({url}). Ollama uygulamasını açın "
                   "ya da terminalde `ollama serve` çalıştırın.")
MSG_OLLAMA_INSTALLED_DOWN = ("Ollama kurulu ama sunucusu kapalı ({url}). Ollama uygulamasını açın "
                             "ya da terminalde `ollama serve` çalıştırın.")
MSG_LMSTUDIO_DOWN = ("LM Studio sunucusu kapalı ({url}). LM Studio'da Developer bölümünden sunucuyu "
                     "başlatın ya da `lms server start` çalıştırın.")
MSG_OPENAI_DOWN = "Sunucu yanıt vermiyor ({url})."
MSG_NOT_OPENAI = "Bu adres OpenAI uyumlu bir model listesi döndürmedi ({url})."
MSG_MODEL_NOT_ON_SERVER = ("“{model}” modeli {server} sunucusunda yok. Güncel liste için "
                           "“Modelleri tara”yı kullanın.")
MSG_LMSTUDIO_NOT_LOADED = ("“{model}” LM Studio'da listelenmiyor: model indirilmemiş ya da yüklü "
                           "değil. LM Studio'da modeli yükleyin ya da isteğe bağlı yüklemeyi "
                           "(Just-in-Time) açın.")
MSG_EMBEDDING_MODEL = "Bu bir gömme (embedding) modeli; metin üretemez."
# Ollama bulut modeli (2026-09-29, F4-01; KARARLAR 2): yerel sunucu isteği
# ollama.com'a aktarır; /api/tags ya da /api/show'da remote_host /
# remote_model dolu ya da ad "-cloud" / ":cloud" ile biter.
MSG_OLLAMA_CLOUD_MODEL = ("“{model}” bir Ollama bulut modeli, bu bilgisayarda çalışmaz: çevrilecek "
                          "metin {host} adresine gönderilirdi. Yalnız bu bilgisayarda çalışan "
                          "modeller kullanılır.")
# Sunucu açık ama model listesini vermedi (2026-09-29, F5-10): zaman aşımı ya
# da HTTP hatası; "kapalı … ollama serve" yalnız bağlantı reddinde söylenir.
MSG_SERVER_NO_LIST = ("{server} sunucusu açık ama model listesini vermedi ({url}): {why}. Sunucu "
                      "meşgul olabilir (ör. model yüklüyor); biraz bekleyip yeniden deneyin.")
# Devre AÇILDIĞINDA (art arda bağlantı düzeyi başarısızlık): kalan segmentler
# gerçekten denenmez.
MSG_SERVER_LOST = "Model sunucusuyla bağlantı koptu ({url}); kalan segmentler denenmeden geçildi."
# Yeniden denemeler tükendi ama devre henüz açılmadı (2026-09-29, F4-05):
# sonraki parça sunucuyu yine dener.
MSG_SERVER_RETRY_FAILED = ("Model sunucusu {attempts} denemede yanıt vermedi ({url}): {detail}. Bu "
                           "parça çevrilmedi; sunucu sonraki parçada yeniden denenecek.")
# Sunucu ayakta ama art arda parçalarda HTTP hatası (ör. 500 "llama runner
# process has terminated") döndürdü; devre açılır (2026-09-29, F4-05).
MSG_SERVER_FAILING = ("{server} art arda {count} parçada hata döndürdü (HTTP {status}): {detail}. "
                      "Sunucu ya da model sorunlu görünüyor; kalan segmentler denenmeden geçildi.")
MSG_OUT_OF_MEMORY_TR = "Model belleğe sığmadı ({server}): {detail}"
MSG_CONTEXT_EXCEEDED = "Segment modelin bağlam sınırını aşıyor ({need} > {ctx} token)."
MSG_EMPTY_OUTPUT = "Model boş çıktı döndürdü."
MSG_TIMEOUT = "Model {seconds} sn içinde yanıt vermedi."
# Arka uç hata iletileri (2026-09-28: backends.py'den taşındı, metin aynı;
# dış kaynaklı {detail} oom_safe/short_detail'den geçer)
MSG_SERVER_ERROR = "{server} isteği başarısız oldu (HTTP {status}): {detail}"
MSG_BAD_RESPONSE = "{server} yanıtı beklenen biçimde değil: {detail}"
MSG_LOAD_FAILED = "Model yüklenemedi ({server}): {detail}"
MSG_GENERATE_FAILED = "{server} üretimi başarısız oldu: {detail}"
# Düşünme sınırı (2026-09-28, KARARLAR 16). MSG_THINK_BUDGET düşünerek
# çeviride de öğe hatasıdır: {budget} o parçanın düşünme payıdır (G2b-2).
MSG_THINK_BUDGET = "Model düşünme sınırını aştı ({tokens} > {budget} token); parça çevrilmedi."
# ESKİ (KARARLAR 16) kesici iletisi; G2b-2'den (KARARLAR 22) beri kesici
# "model cevap üretemiyor"da açılır ve iletisi ui.branding.THINKING_NO_ANSWER_MESSAGE'tır
# (backends.thinking_no_answer_reason). Bu kalıp üretimde KULLANILMIYOR; yalnız
# eski içe aktarımlar kırılmasın diye durur (tests/test_token_dusunme_20260928.py;
# G2b-2 öneri yaması bu kullanımları kaldırır, sonra silinebilir).
MSG_THINK_BREAKER = ("Seçilen model düşünmeyi kapatmıyor ve düşünme sınırını aşıyor (ilk {window} "
                     "parçadan {count} tanesinde); düşünmeyen bir model seçin.")
# SourceStatus.message sayım kalıpları (yalnız gerçek sayımdan)
MSG_SOURCE_COUNT_MODELS = "{n} model"
MSG_SOURCE_COUNT_DIRS = "{n} klasör"
# Sayıma yerleşik / kayıtlı motorun klasörü de girer; o klasör motorun kendi
# kaydıyla listelenir, ayrıca "mlx:" kaydı olmaz (2026-09-29, F4-08).
MSG_SOURCE_COUNT_DIRS_ENGINES = "{n} klasör ({engines} dahil)"
MSG_SOURCE_COUNT_FILES = "{n} dosya"
MSG_SOURCE_COUNT_FILES_ENGINES = "{n} dosya ({engines} dahil)"
# Tarama DISCOVERY_MAX_ENTRIES girdide kesildi (2026-09-29, F4-10); sayım
# satırının sonuna eklenir.
MSG_SOURCE_COUNT_TRUNCATED = "tarama {limit} girdide kesildi; bazı modeller listelenmemiş olabilir"

# Katalogdaki tüm kalıplar (test: hepsi Türkçe, OOM alt dizgisi yok, alanları belli)
MESSAGE_CATALOG: Dict[str, str] = {
    name: value for name, value in dict(globals()).items()
    if name.startswith("MSG_") and isinstance(value, str)
}

__all__ = [
    # arka uç / biçim / kaynak / önek sabitleri
    "BACKEND_MLX", "BACKEND_OLLAMA", "BACKEND_OPENAI", "BACKEND_LLAMACPP", "BACKENDS",
    "BACKEND_LABELS",
    "FORMAT_HYMT", "FORMAT_CHAT", "FORMATS",
    "FLAVOR_LMSTUDIO", "FLAVOR_GENERIC",
    "SPEC_SOURCE_BUILTIN", "SPEC_SOURCE_USER", "SPEC_SOURCE_ADHOC", "SPEC_SOURCES",
    "ENGINE_OPTION_KEYS", "THINKING_OFF_TEMPLATE_KWARGS", "default_chat_template_kwargs",
    # düşünme sınıfı (G2b-1)
    "THINKING_NONE", "THINKING_CAN_DISABLE", "THINKING_ALWAYS", "THINKING_UNKNOWN",
    "THINKING_CLASSES", "THINKING_OFF_LMSTUDIO_REST", "THINKING_OFF_OLLAMA_THINK",
    "THINKING_OFF_TEMPLATE_SWITCH", "THINKING_OFF_QWEN3_SOFT", "THINKING_OFF_PATHS",
    "SOURCE_BUILTIN", "SOURCE_USER", "SOURCE_MLX", "SOURCE_GGUF",
    "SOURCE_OLLAMA", "SOURCE_LMSTUDIO", "SOURCE_OPENAI",
    "DISCOVERY_SOURCE_ORDER", "SOURCE_STATUS_LABELS",
    "PREFIX_MLX", "PREFIX_GGUF", "PREFIX_OLLAMA", "PREFIX_LMSTUDIO", "PREFIX_OPENAI",
    "ID_PREFIXES", "PREFIX_BACKEND", "ID_PREFIX_SEP", "OPENAI_ID_SEP",
    "ENGINE_NAME_RE", "MODEL_NAME_MAX_LEN", "GGUF_SUFFIX", "HYMT_NAME_RE",
    "guess_format", "build_engine_id",
    "MT_SELECTION_FILENAME", "MT_SOURCES_FILENAME", "MT_ENGINES_FILENAME",
    "OOM_TRIGGER_WORDS", "has_oom_trigger", "oom_safe",
    # hatalar ve veri yapıları
    "BackendUnavailable", "ThinkingBudgetExceeded", "GenerationError",
    "GenerationRequest", "GenerationResult",
    "DiscoveredModel", "SourceStatus", "DiscoveryReport", "EngineCheck",
    "OpenAIServer", "SourcesConfig",
    # iletiler
    "LLAMA_CPP_INSTALL_CMD", "MESSAGE_CATALOG",
] + sorted(MESSAGE_CATALOG)
