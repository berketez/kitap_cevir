"""Donanım kapısı: bu Mac'in yerel model için kullanılabilir belleği ve bir
modelin tahmini bellek gereksinimi (KARARLAR 17 ve 17a, 28 Eylül 2026).

Ölçüm GERÇEKTİR, uydurma sayı yoktur:

1. MLX: ``mx.device_info()`` (eski sürümlerde ``mx.metal.device_info()``) ->
   ``max_recommended_working_set_size`` (Metal'in
   ``recommendedMaxWorkingSetSize`` değeri) ve ``memory_size``. mlx-lm modeli
   yüklerken bellek sınırını da bu değere çeker (``mlx_lm.generate.wired_limit``).
2. MLX yoksa (ya da değer vermediyse) AYNI Metal değeri stdlib ``ctypes`` ile
   (``MTLCreateSystemDefaultDevice()`` -> ``recommendedMaxWorkingSetSize``;
   packaging/TASARIM.md A.3 ve §F F3, 5 Ekim 2026). Ölçüldü: bu Mac'te iki
   yol da 30.150.672.384 bayt verir. Kaynak yine ``SOURCE_METAL``'dir; MLX
   kurulmadan (kurulum sihirbazı, MLX'siz kurulum) yedeğe düşülmez.
3. Yedek: ``sysctl -n hw.memsize`` × ``HW_WORKING_SET_FALLBACK_RATIO``
   (kaynaklı oran; yedek kullanıldığı ``HardwareInfo.problem``'da yazar).
4. Hiçbiri alınamazsa "ölçülemedi": kapı AÇIK kalır, uyarı yazılır.

Çip adı ``sysctl -n machdep.cpu.brand_string``'ten (yoksa MLX'in
``device_name``'i ya da Metal aygıtının adı). Ölçüm süreç başına bir kez
yapılır (önbellek).

Tahmin (KARARLAR 17a; sayıların tek kaynağı config/defaults.py, kaynaklar
docs/DONANIM_VARSAYIMLARI.md)::

    gerekli = ağırlık (diskte / sunucuda ölçülen gerçek bayt)
            + KV önbelleği = katman × KV başlığı × (anahtar + değer boyutu)
                             × öğe baytı × token
            + çalışma payı = ağırlık × HW_WORKSPACE_FRACTION  (TEK varsayım)
    kullanılabilir = max_recommended_working_set_size

KV şekli modelin kendi üst verisinden okunur: MLX'te ``config.json``, GGUF'ta
başlık, Ollama'da ``/api/show`` ``model_info`` (GGUF anahtarlarıyla aynı).
Katmanları farklı modellerde (Gemma 4: katman başına KV başı, kayan pencereli
katmanlar) KV katman katman toplanır; kayan pencereli katmanın KV'si
pencereyle sınırlıdır (``HW_SWA_EXTRA_TOKENS``).
Okunamazsa ağırlıkla orantılı yedek kullanılır (referans modelin KV /
ağırlık oranı; ``KV_SOURCE_RATIO``).

İki kip vardır:

- ``MODE_BATCHED`` (süreç içi MLX): KV parça başına büyür ve toplu üretimde
  (``mlx_lm.batch_generate``) parça sayısıyla çarpılır. Token = giriş üst
  sınırı (``MT_MAX_INPUT_TOKENS``) + çıktı üst sınırı (çıktı token kuralı,
  toplu yolda en uzun parça ``MAX_SEGMENT_LENGTH``); tek parça yolunda çıktı
  tavanı ``MT_OUTPUT_TOKENS_CAP``. Sığan en büyük toplu iş boyutu
  (≤ ``DEFAULT_BATCH_SIZE_HYMT_LOCAL``) hesaplanır.
- ``MODE_CONTEXT`` (llama.cpp, Ollama, LM Studio): KV bağlam uzunluğu kadar
  baştan ayrılır; toplu iş yoktur.

Düşünerek çeviri (G2b-2, 2026-10-05; KARARLAR 22): düşünmesi kapatılamayan
modelde istek başına düşünme payı (``assess(think_tokens=...)``; keşif
"kapatilamaz" MLX modelinde pay tavanını, ``MT_THINK_ALLOWANCE_CAP_TOKENS``
ya da motorun ``options.think_budget``'ını verir) toplu kipte HER dizinin KV
token'ına eklenir: sığan toplu iş küçülür, toplu iş 1 bile sığmazsa mevcut
kural (kapı) geçerlidir. Bağlam kipinde KV bağlam kadar baştan ayrıldığı için
değişmez; pay bağlamın içinde kalır (tavan, giriş üst sınırı + en uzun
parçanın çeviri sınırı + tavan <= ``MT_CONTEXT_TOKENS`` olacak biçimde
seçildi — config/defaults.py).

Genel kapı: referans 4 bit 7B model toplu iş 1 ile sığmıyorsa "Yerel model
ile çevir" bu Mac'te kapalıdır.

Hükmün bağlayıcılığı (``MemoryFit.verdict``; 2026-09-30, MT-12): MLX / GGUF
dosyası ve Ollama modelinde "sığmaz" ENGELDİR (``VERDICT_BINDING``). LM Studio
ve OpenAI uyumlu sunucu KV şeklini bildirmez; oradaki tahmin ağırlıkla
orantılı yedektir (``KV_SOURCE_RATIO``) ve MoE / karma dikkatli modellerde
gerçeğin çok üstünde çıkar (lmstudio:qwen/qwen3.5-35b-a3b "~33,5 GB"). Bu
modellerde "sığmaz" yalnız gerekçeli UYARIDIR (``VERDICT_ADVISORY``): yükleme
ve bellek denetimi sunucunundur. Sunucunun şu an yüklü tuttuğu modele "sığmaz"
hükmü verilmez (``VERDICT_LOADED``; zaten çalışıyor). Bkz. ``server_verdict``.

Hafif modüldür: yalnız stdlib + ``config.defaults``; ``mlx`` ve ``ctypes`` YALNIZ
ölçüm fonksiyonlarının, çıktı token kuralı (``core.mt_engines.backends``) YALNIZ
``output_token_limit``'in içinde içe aktarılır. İletiler ``oom``/``mps``/``cuda``/``out
of memory`` alt dizgilerini içermez (core/translator.py'nin bölme dalı).
"""

from __future__ import annotations

import logging
import math
import shutil
import subprocess
import threading
from dataclasses import dataclass, replace
from typing import Any, Mapping, Optional

from config.defaults import (
    DEFAULT_BATCH_SIZE_HYMT_LOCAL,
    HW_KV_DTYPE_BYTES,
    HW_OLLAMA_NUM_PARALLEL,
    HW_REFERENCE_HEAD_DIM,
    HW_REFERENCE_KV_HEADS,
    HW_REFERENCE_LAYERS,
    HW_REFERENCE_WEIGHTS_BYTES,
    HW_SWA_EXTRA_TOKENS,
    HW_SYSCTL_TIMEOUT_S,
    HW_WORKING_SET_FALLBACK_RATIO,
    HW_WORKSPACE_FRACTION,
    MAX_SEGMENT_LENGTH,
    MT_CONTEXT_TOKENS,
    MT_MAX_INPUT_TOKENS,
    MT_OUTPUT_TOKENS_CAP,
)

logger = logging.getLogger(__name__)

__all__ = [
    "HardwareInfo",
    "KvShape",
    "MemoryFit",
    "GateVerdict",
    "hardware_info",
    "kv_shape_from_hf_config",
    "kv_shape_from_gguf_keys",
    "gguf_needs_per_layer",
    "reference_kv",
    "fallback_kv_bytes_per_token",
    "ollama_context_tokens",
    "output_token_limit",
    "batch_sequence_tokens",
    "single_sequence_tokens",
    "batched_kv_tokens",
    "assess",
    "reference_fit",
    "general_gate",
    "too_big_label",
    "too_big_message",
    "server_verdict",
    "blocks",
    "fit_warning",
    "hardware_caption",
    "batch_note",
    "format_gb",
    "format_fit_gb",
]

# Ölçüm kaynağı (HardwareInfo.source)
SOURCE_METAL = "metal"        # Metal recommendedMaxWorkingSetSize (MLX ya da ctypes)
SOURCE_SYSCTL = "sysctl"      # yedek: hw.memsize × HW_WORKING_SET_FALLBACK_RATIO

# MLX'siz Metal ölçümü (F3): Objective-C çalışma zamanı ve Metal çerçevesi
# ctypes ile açılır, varsayılan aygıta üç ileti gönderilir. Seçici adları
# Apple'ın MTLDevice / NSString arayüzündeki adlardır.
_OBJC_LIBRARY = "objc"
_METAL_FRAMEWORK = "/System/Library/Frameworks/Metal.framework/Metal"
_SEL_RESPONDS = b"respondsToSelector:"
_SEL_WORKING_SET = b"recommendedMaxWorkingSetSize"
_SEL_NAME = b"name"
_SEL_UTF8 = b"UTF8String"

# KV şeklinin kaynağı (KvShape.source / MemoryFit.kv_source)
KV_SOURCE_CONFIG = "config.json"
KV_SOURCE_GGUF = "gguf"
KV_SOURCE_OLLAMA = "ollama"
KV_SOURCE_REFERENCE = "referans"
KV_SOURCE_RATIO = "oran"      # şekil bilinmiyor: ağırlıkla orantılı yedek

# Tahmin kipi (MemoryFit.mode)
MODE_BATCHED = "toplu"        # süreç içi MLX: KV × toplu iş
MODE_CONTEXT = "bağlam"       # llama.cpp / sunucu: KV = bağlam uzunluğu

# Hükmün bağlayıcılığı (MemoryFit.verdict; 2026-09-30, MT-12)
VERDICT_BINDING = "engel"     # sığmıyorsa model kullanılamaz (MLX, GGUF, Ollama)
VERDICT_ADVISORY = "uyarı"    # KV şekli bilinmeyen sunucu modeli: "sığmaz" yalnız uyarı
VERDICT_LOADED = "yüklü"      # sunucu modeli şu an yüklü tutuyor: "sığmaz" hükmü verilmez

# Ekrandaki bellek birimi: ondalık GB (arayüzün model boyutlarıyla aynı birim).
BYTES_PER_GB = 10 ** 9
_GB_TENTHS = 10               # bir ondalık basamak

# Ölçüm ve kapı iletileri (CLI ve arayüz AYNI metni gösterir).
MSG_HW_TOO_SMALL = ("Bu Mac yerel model için yetersiz: en az {need} GB kullanılabilir "
                    "bellek gerekir, bu Mac'te {have} GB.")
MSG_HW_TOO_BIG_LABEL = "bu Mac'e sığmaz (gerekli ~{need} GB, kullanılabilir {have} GB)"
MSG_HW_TOO_BIG = "Bu model " + MSG_HW_TOO_BIG_LABEL + "."
MSG_HW_UNMEASURED = ("Bu Mac'in kullanılabilir belleği ölçülemedi ({why}); bellek "
                     "denetimi yapılmadan devam ediliyor.")
MSG_HW_FALLBACK = ("Metal ölçümü alınamadı ({why}); kullanılabilir bellek toplam "
                   "belleğin {ratio} katı sayıldı.")
MSG_HW_CAPTION = "{chip} · yerel model için kullanılabilir bellek {have} GB"
MSG_HW_CAPTION_UNMEASURED = "{chip} · bellek ölçülemedi"
MSG_HW_CAPTION_FALLBACK_NOTE = " (yedek ölçüm)"
MSG_HW_BATCH = ("Bellek: aynı anda en çok {batch} parça çevrilir (tahmini gerekli "
                "~{need} GB, kullanılabilir {have} GB).")
# MT-12 (2026-09-30): KV şekli bilinmeyen sunucu modelinde (LM Studio, OpenAI
# uyumlu sunucu) "sığmaz" engel değil, gerekçeli uyarı. Seçicideki kısa not ve
# CLI / ön kontrol uyarısı.
MSG_HW_ROUGH_LABEL = "bu Mac'e sığmayabilir (kaba tahmin ~{need} GB, kullanılabilir {have} GB)"
MSG_HW_ROUGH = ("Bellek uyarısı: bu model bu Mac'e sığmayabilir (kaba tahmin ~{need} GB, "
                "kullanılabilir {have} GB). Sunucu modelin KV önbelleği yapısını "
                "bildirmediği için tahmin ağırlıkla orantılı bir üst sınırdır; MoE ve "
                "karma dikkatli modellerde gerçeğin çok üstünde çıkabilir. Model "
                "kullanılabilir kalır: modeli yükleyen ve belleği denetleyen sunucunun "
                "kendisidir.")
# Sunucunun şu an yüklü tuttuğu model: sığma hükmü verilmez (zaten çalışıyor).
MSG_HW_LOADED_NOTE = "model şu an sunucu tarafından yüklü tutuluyor; sığma hükmü verilmez"
_UNKNOWN_CHIP = "Apple Silicon"


# ---------------------------------------------------------------------------
# Ölçüm
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HardwareInfo:
    """Bu Mac'in ölçülen belleği (uydurma değer yok; bilinmiyorsa None)."""
    available_bytes: Optional[int]   # yerel model için kullanılabilir (çalışma kümesi)
    memory_bytes: Optional[int]      # toplam birleşik bellek
    chip: str = ""                   # "Apple M4 Max"
    source: str = ""                 # SOURCE_METAL | SOURCE_SYSCTL | "" (ölçülemedi)
    problem: str = ""                # Türkçe: yedek kullanıldıysa / ölçülemediyse neden

    @property
    def measured(self) -> bool:
        return isinstance(self.available_bytes, int) and self.available_bytes > 0


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _run_sysctl(name: str) -> str:
    """``sysctl -n <ad>`` çıktısı; alınamazsa ""."""
    exe = shutil.which("sysctl") or "/usr/sbin/sysctl"
    try:
        done = subprocess.run([exe, "-n", name], capture_output=True, text=True,
                              timeout=HW_SYSCTL_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _memsize() -> Optional[int]:
    raw = _run_sysctl("hw.memsize")
    return _positive_int(int(raw)) if raw.isdigit() else None


def _metal_device_info() -> tuple:
    """(MLX aygıt bilgisi sözlüğü ya da None, Türkçe neden)."""
    try:
        import mlx.core as mx   # yalnız burada: modül içe aktarımı hafif kalsın
    except Exception as exc:    # ImportError, Metal başlatma hatası ...
        return None, f"MLX yüklenemedi: {type(exc).__name__}"
    getters = (getattr(mx, "device_info", None),
               getattr(getattr(mx, "metal", None), "device_info", None))
    for getter in getters:
        if not callable(getter):
            continue
        try:
            info = getter()
        except Exception as exc:
            return None, f"MLX aygıt bilgisi alınamadı: {type(exc).__name__}"
        if isinstance(info, dict):
            return info, ""
    return None, "MLX aygıt bilgisi sunmuyor"


def _metal_ctypes_info() -> tuple:
    """(kullanılabilir bayt ya da None, Metal aygıtının adı, Türkçe neden) — MLX'siz.

    ``MTLCreateSystemDefaultDevice()`` -> ``recommendedMaxWorkingSetSize``:
    MLX'in ``max_recommended_working_set_size``'ı bu değerin kendisidir
    (TASARIM A.3; yöntem packaging/yonetici/donanim.py ``metal_olc`` ile aynı).
    ``objc_msgSend`` her ileti için kendi dönüş türüyle çağrılır; seçiciyi
    tanımayan aygıta ileti gönderilmez (Objective-C istisnası süreci
    düşürürdü). Her hata ölçümsüzlük sayılır, neden döner.
    """
    try:
        import ctypes   # yalnız burada: modül içe aktarımı hafif kalsın
        import ctypes.util

        objc_path = ctypes.util.find_library(_OBJC_LIBRARY)
        if not objc_path:
            return None, "", "Metal ölçülemedi: objc kitaplığı bulunamadı"
        objc = ctypes.cdll.LoadLibrary(objc_path)
        metal = ctypes.cdll.LoadLibrary(_METAL_FRAMEWORK)
        create_device = metal.MTLCreateSystemDefaultDevice
        create_device.restype = ctypes.c_void_p
        create_device.argtypes = []
        selector = objc.sel_registerName
        selector.restype = ctypes.c_void_p
        selector.argtypes = [ctypes.c_char_p]
        device = create_device()
        if not device:
            return None, "", "Metal aygıtı yok"
        send = ("objc_msgSend", objc)
        send_bool = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p)(send)
        send_u64 = ctypes.CFUNCTYPE(ctypes.c_uint64, ctypes.c_void_p, ctypes.c_void_p)(send)
        send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        send_text = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        working_set = selector(_SEL_WORKING_SET)
        if not send_bool(device, selector(_SEL_RESPONDS), working_set):
            return None, "", "Metal aygıtı çalışma kümesi değerini sunmuyor"
        available = int(send_u64(device, working_set))
        name = ""
        ns_name = send_id(device, selector(_SEL_NAME))
        if ns_name:
            raw = send_text(ns_name, selector(_SEL_UTF8))
            if raw:
                name = raw.decode("utf-8", "replace").strip()
    except Exception as exc:    # OSError (çerçeve yok), AttributeError (sembol yok) ...
        return None, "", f"Metal ölçülemedi: {type(exc).__name__}"
    if available <= 0:
        return None, name, "Metal kullanılabilir bellek değeri vermedi"
    return available, name, ""


def _measure() -> HardwareInfo:
    """Gerçek ölçüm (önbelleksiz). Testler bunu sabit bir değerle değiştirir."""
    chip = _run_sysctl("machdep.cpu.brand_string")
    info, problem = _metal_device_info()
    mlx_total: Optional[int] = None
    if info is not None:
        available = _positive_int(info.get("max_recommended_working_set_size"))
        mlx_total = _positive_int(info.get("memory_size"))
        if not chip:
            chip = str(info.get("device_name") or "").strip()
        if available:
            return HardwareInfo(available_bytes=available,
                                memory_bytes=mlx_total or _memsize(), chip=chip,
                                source=SOURCE_METAL)
        problem = "MLX kullanılabilir bellek değeri vermedi"
    # F3: MLX yoksa aynı Metal değeri ctypes ile. Yedek değildir (kaynak
    # SOURCE_METAL, uyarı yok); yalnız o da alınamazsa sysctl yedeğine düşülür.
    available, device, metal_problem = _metal_ctypes_info()
    if available:
        return HardwareInfo(available_bytes=available,
                            memory_bytes=mlx_total or _memsize(), chip=chip or device,
                            source=SOURCE_METAL)
    problem = "; ".join(part for part in (problem, metal_problem) if part)
    total = _memsize()
    if total:
        return HardwareInfo(available_bytes=int(total * HW_WORKING_SET_FALLBACK_RATIO),
                            memory_bytes=total, chip=chip, source=SOURCE_SYSCTL,
                            problem=problem)
    return HardwareInfo(available_bytes=None, memory_bytes=None, chip=chip, source="",
                        problem=problem or "sysctl hw.memsize okunamadı")


_CACHE: Optional[HardwareInfo] = None
_CACHE_LOCK = threading.Lock()


def hardware_info(refresh: bool = False) -> HardwareInfo:
    """Bu Mac'in ölçümü (süreç başına bir kez; ``refresh`` yeniden ölçer)."""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None or refresh:
            _CACHE = _measure()
            if not _CACHE.measured:
                logger.warning("Donanım ölçülemedi: %s", _CACHE.problem)
        return _CACHE


# ---------------------------------------------------------------------------
# KV önbelleği şekli
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KvShape:
    """Token başına KV önbelleğini belirleyen mimari değerleri."""
    layers: int
    kv_heads: int
    key_dim: int
    value_dim: int
    dtype_bytes: int
    source: str
    # 2026-09-30 (MT-11): katmanlar farklıysa tam dikkatli katmanların toplam
    # token başına KV'si (None: tekdüze formül); kayan pencereli katmanlar ayrı.
    full_bytes_per_token: Optional[int] = None
    swa_bytes_per_token: int = 0
    swa_window: int = 0

    @property
    def bytes_per_token(self) -> int:
        """Bağlamla doğrusal büyüyen (tam dikkatli katmanların) token başına KV."""
        if self.full_bytes_per_token is not None:
            return self.full_bytes_per_token
        return self.layers * self.kv_heads * (self.key_dim + self.value_dim) * self.dtype_bytes

    def kv_bytes(self, tokens: int, sequences: int = 1) -> int:
        """``tokens`` token (``sequences`` dizi) için KV baytı; kayan pencereli
        katmanlar dizi başına pencere + ``HW_SWA_EXTRA_TOKENS`` ile sınırlı."""
        tokens = max(0, int(tokens))
        total = self.bytes_per_token * tokens
        if self.swa_bytes_per_token:
            cap = self.swa_window * max(1, int(sequences)) + HW_SWA_EXTRA_TOKENS
            total += self.swa_bytes_per_token * min(tokens, cap)
        return total


def reference_kv() -> KvShape:
    """Genel kapının referans 4 bit 7B modelinin KV şekli (config/defaults.py)."""
    return KvShape(HW_REFERENCE_LAYERS, HW_REFERENCE_KV_HEADS, HW_REFERENCE_HEAD_DIM,
                   HW_REFERENCE_HEAD_DIM, HW_KV_DTYPE_BYTES, KV_SOURCE_REFERENCE)


# 32 bitlik kayan nokta = 4 bayt (birim tanımı; varsayım değil)
_FLOAT32_NAMES = ("float32", "fp32")
_FLOAT32_BYTES = 4


def _dtype_bytes(name: Any) -> int:
    """config.json'daki hesap türü -> KV öğe baytı (yalnız float32 farklıdır)."""
    if isinstance(name, str) and name.strip().lower() in _FLOAT32_NAMES:
        return _FLOAT32_BYTES
    return HW_KV_DTYPE_BYTES


def kv_shape_from_hf_config(config: Any) -> Optional[KvShape]:
    """MLX / Hugging Face ``config.json``'dan KV şekli; okunamazsa None.

    Görsel-dil modellerinde değerler ``text_config`` altındadır. Sıkıştırılmış
    KV'li (MLA: ``kv_lora_rank``) modellerde formül farklıdır -> None (yedek).
    Kayan pencereli / karma dikkatli modellerde formül üst sınır verir.
    """
    if not isinstance(config, Mapping):
        return None
    text = config.get("text_config")
    src: Mapping = config
    if isinstance(text, Mapping) and _positive_int(text.get("num_hidden_layers")):
        src = text
    if src.get("kv_lora_rank") is not None or config.get("kv_lora_rank") is not None:
        return None
    layers = _positive_int(src.get("num_hidden_layers"))
    heads = _positive_int(src.get("num_attention_heads"))
    kv_heads = _positive_int(src.get("num_key_value_heads")) or heads
    hidden = _positive_int(src.get("hidden_size"))
    head_dim = _positive_int(src.get("head_dim"))
    if head_dim is None and hidden and heads and hidden % heads == 0:
        head_dim = hidden // heads
    if not (layers and kv_heads and head_dim):
        return None
    dtype = src.get("torch_dtype") or src.get("dtype") or config.get("torch_dtype") \
        or config.get("dtype")
    return KvShape(layers, kv_heads, head_dim, head_dim, _dtype_bytes(dtype),
                   KV_SOURCE_CONFIG)


def kv_shape_from_gguf_keys(values: Any, source: str = KV_SOURCE_GGUF) -> Optional[KvShape]:
    """GGUF üst verisinden (ya da Ollama ``model_info``'dan) KV şekli; yoksa None.

    Anahtarlar mimari önekli: ``<mimari>.block_count``,
    ``<mimari>.attention.head_count(_kv)``, ``<mimari>.attention.key_length`` /
    ``value_length`` (yoksa ``embedding_length / head_count``). llama.cpp ve
    Ollama KV'yi varsayılan olarak f16 tutar (``HW_KV_DTYPE_BYTES``).
    """
    if not isinstance(values, Mapping):
        return None
    arch = values.get("general.architecture")
    if not isinstance(arch, str) or not arch.strip():
        return None
    arch = arch.strip()

    def num(key: str) -> Optional[int]:
        return _positive_int(values.get(f"{arch}.{key}"))

    if values.get(f"{arch}.attention.kv_lora_rank") is not None:
        return None
    # 2026-09-30 (MT-11): anahtar VAR ama değeri null = Ollama'nın kısa
    # /api/show'u katman başına diziyi gizlemiş (gemma4). Eskiden MHA sanılıp
    # KV 8 kat büyük tahmin ediliyordu (gemma4:31b "~57 GB, sığmaz"); artık
    # şekil bilinmiyor -> None (oran yedeği). Anahtar hiç yoksa GGUF kuralı: MHA.
    if gguf_needs_per_layer(values):
        return None
    layers = num("block_count")
    heads = num("attention.head_count")
    embedding = num("embedding_length")
    per_head = embedding // heads if embedding and heads and embedding % heads == 0 else None
    key_dim = num("attention.key_length") or per_head
    value_dim = num("attention.value_length") or per_head
    raw_kv = values.get(f"{arch}.attention.head_count_kv")
    pattern = values.get(f"{arch}.attention.sliding_window_pattern")
    if isinstance(raw_kv, list) or isinstance(pattern, list):
        return _per_layer_shape(values, arch, layers, heads, key_dim, value_dim, source)
    kv_heads = num("attention.head_count_kv") or heads
    if not (layers and kv_heads and key_dim and value_dim):
        return None
    return KvShape(layers, kv_heads, key_dim, value_dim, HW_KV_DTYPE_BYTES, source)


def gguf_needs_per_layer(values: Any) -> bool:
    """Katman başına dizisi null gelen üst veri mi (Ollama kısa ``/api/show``)?

    Ollama ``verbose`` olmadan dizi değerlerini null döndürür; gemma4'te
    ``head_count_kv`` ve ``sliding_window_pattern`` dizidir. True ise KV şekli
    ``verbose=true`` yanıtından okunmalıdır.
    """
    if not isinstance(values, Mapping):
        return False
    arch = values.get("general.architecture")
    if not isinstance(arch, str) or not arch.strip():
        return False
    arch = arch.strip()
    return any(f"{arch}.attention.{k}" in values and values[f"{arch}.attention.{k}"] is None
               for k in ("head_count_kv", "sliding_window_pattern"))


def _per_layer_shape(values: Mapping, arch: str, layers: Optional[int], heads: Optional[int],
                     key_dim: Optional[int], value_dim: Optional[int],
                     source: str) -> Optional[KvShape]:
    """Katmanları farklı model (Gemma 4): KV katman katman toplanır.

    ``head_count_kv`` katman başına dizi (ya da tek sayı), ``sliding_window_pattern``
    katman başına (true = kayan pencereli). Kayan pencereli katmanda anahtar /
    değer boyutu ``key_length_swa`` / ``value_length_swa`` (yoksa tam katmanınki),
    KV pencereyle sınırlı (``KvShape.kv_bytes``). Diziler katman sayısıyla
    uyuşmazsa şekil bilinmiyor sayılır (None).
    """
    if not (layers and key_dim and value_dim):
        return None
    raw_kv = values.get(f"{arch}.attention.head_count_kv")
    if isinstance(raw_kv, list):
        kv_list = [_positive_int(v) for v in raw_kv]
    else:
        kv_list = [_positive_int(raw_kv) or heads] * layers
    pattern = values.get(f"{arch}.attention.sliding_window_pattern")
    window = _positive_int(values.get(f"{arch}.attention.sliding_window"))
    swa_list = [bool(v) for v in pattern] if isinstance(pattern, list) else [False] * layers
    if len(kv_list) != layers or len(swa_list) != layers or not all(kv_list):
        return None
    if any(swa_list) and not window:
        swa_list = [False] * layers          # pencere bilinmiyor: tam katman say (üst sınır)
    key_swa = _positive_int(values.get(f"{arch}.attention.key_length_swa")) or key_dim
    value_swa = _positive_int(values.get(f"{arch}.attention.value_length_swa")) or value_dim
    full = sum(kv * (key_dim + value_dim) for kv, swa in zip(kv_list, swa_list) if not swa)
    sliding = sum(kv * (key_swa + value_swa) for kv, swa in zip(kv_list, swa_list) if swa)
    return KvShape(layers, max(kv_list), key_dim, value_dim, HW_KV_DTYPE_BYTES, source,
                   full_bytes_per_token=full * HW_KV_DTYPE_BYTES,
                   swa_bytes_per_token=sliding * HW_KV_DTYPE_BYTES,
                   swa_window=window or 0)


def ollama_context_tokens(configured: Any = None, model_context: Any = None) -> int:
    """Ollama arka ucunun istediği bağlam (http_backends ile aynı kural).

    ``max(MT_CONTEXT_TOKENS, options.num_ctx)``, modelin bağlam sınırı
    biliniyorsa onunla kırpılır; sonra ``HW_OLLAMA_NUM_PARALLEL`` ile çarpılır.
    """
    tokens = max(int(MT_CONTEXT_TOKENS), _positive_int(configured) or 0)
    limit = _positive_int(model_context)
    if limit:
        tokens = min(tokens, limit)
    return tokens * HW_OLLAMA_NUM_PARALLEL


# ---------------------------------------------------------------------------
# Token üst sınırları (config/defaults.py'deki kurallardan türer)
# ---------------------------------------------------------------------------
def output_token_limit(chars: int) -> int:
    """Çıktı token kuralı (KARARLAR 15): karakter / MT_CONTEXT_CHARS_PER_TOKEN ×
    MT_OUTPUT_TOKENS_FACTOR; en az MT_OUTPUT_TOKENS_FLOOR, en çok
    MT_OUTPUT_TOKENS_CAP.

    2026-09-28 (VTD): kuralın ikinci kopyası tutulmaz; çevirinin gerçek üretim
    sınırını veren TEK hesap (``core.mt_engines.backends.output_token_limit``)
    çağrılır. İçe aktarma fonksiyon içindedir: bu modülün içe aktarımı hafif
    kalır (mlx / sunucu katmanı yüklenmez).
    """
    from core.mt_engines.backends import output_token_limit as translation_rule

    return translation_rule(chars)


def batch_sequence_tokens(think_tokens: int = 0) -> int:
    """Toplu yolda bir parçanın KV'de tuttuğu en çok token (giriş + çıktı
    + düşünerek çeviride istek başı düşünme payı)."""
    return MT_MAX_INPUT_TOKENS + output_token_limit(MAX_SEGMENT_LENGTH) + max(0, int(think_tokens))


def single_sequence_tokens(think_tokens: int = 0) -> int:
    """Tek parça yolunda (uzun parça) en çok token: giriş + çıktı tavanı
    (+ düşünme payı)."""
    return MT_MAX_INPUT_TOKENS + MT_OUTPUT_TOKENS_CAP + max(0, int(think_tokens))


def batched_kv_tokens(batch: int, think_tokens: int = 0) -> int:
    """``batch`` parçalık toplu üretimde KV'deki en çok token (*think_tokens*:
    istek başı düşünme payı, her diziye eklenir)."""
    return max(max(1, int(batch)) * batch_sequence_tokens(think_tokens),
               single_sequence_tokens(think_tokens))


# ---------------------------------------------------------------------------
# Tahmin
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MemoryFit:
    """Bir modelin bu Mac'teki bellek hükmü (keşif JSON'unda ``memory``)."""
    weights_bytes: int            # ölçülen ağırlık (disk / sunucu)
    kv_bytes_per_token: int
    kv_source: str                # KV_SOURCE_*
    mode: str                     # MODE_BATCHED | MODE_CONTEXT
    kv_tokens: int                # required_bytes'taki KV token sayısı
    workspace_bytes: int          # çalışma payı (varsayım)
    required_bytes: int           # batch_size'da (sığmıyorsa toplu iş 1'de) gereken
    minimum_bytes: int            # toplu iş 1'de gereken
    available_bytes: Optional[int]
    # None: donanım ölçülemedi ya da hüküm verilmedi (verdict VERDICT_LOADED)
    fits: Optional[bool]
    batch_size: Optional[int]     # sığan en büyük toplu iş; sığmıyor/ölçülemedi: None
    hardware_source: str = ""     # SOURCE_METAL | SOURCE_SYSCTL | ""
    # MT-12 (2026-09-30): "sığmaz" hükmünün bağlayıcılığı (VERDICT_*; bkz. server_verdict)
    verdict: str = VERDICT_BINDING


def fallback_kv_bytes_per_token(weights_bytes: int) -> int:
    """Şekil bilinmiyorsa token başına KV: ağırlıkla orantılı, referans modelin
    KV / ağırlık oranıyla (üst sınır yönünde; kaynak docs/DONANIM_VARSAYIMLARI.md)."""
    return max(1, math.ceil(weights_bytes * reference_kv().bytes_per_token
                            / HW_REFERENCE_WEIGHTS_BYTES))


def assess(weights_bytes: int, kv: Optional[KvShape], *, mode: str = MODE_BATCHED,
           context_tokens: Optional[int] = None, hw: Optional[HardwareInfo] = None,
           max_batch: int = DEFAULT_BATCH_SIZE_HYMT_LOCAL,
           think_tokens: int = 0) -> MemoryFit:
    """Modelin gereken belleğini hesapla ve bu Mac'e sığıp sığmadığına bak.

    *think_tokens* (G2b-2): düşünerek çeviride istek başı düşünme payı. Toplu
    kipte her dizinin KV token'ına eklenir (sığan toplu iş küçülür; toplu iş 1
    bile sığmazsa ``fits`` False — mevcut kapı). Bağlam kipinde KV bağlam
    uzunluğudur, değişmez.
    """
    hw = hw if hw is not None else hardware_info()
    weights = max(0, int(weights_bytes))
    fallback = fallback_kv_bytes_per_token(weights)
    # Gösterilen token başı KV: pencere sınırı uygulanmadan önceki en yüksek oran
    per_token = kv.bytes_per_token + kv.swa_bytes_per_token if kv is not None else fallback
    kv_source = kv.source if kv is not None else KV_SOURCE_RATIO

    def kv_need(tokens: int, sequences: int) -> int:
        return kv.kv_bytes(tokens, sequences) if kv is not None else fallback * tokens
    workspace = math.ceil(weights * HW_WORKSPACE_FRACTION)
    base = weights + workspace
    available = hw.available_bytes if hw.measured else None

    if mode == MODE_BATCHED:
        def need(batch: int) -> int:
            return base + kv_need(batched_kv_tokens(batch, think_tokens), batch)

        minimum, tokens = need(1), batched_kv_tokens(1, think_tokens)
        required, batch_size, fits = minimum, None, None
        if available is not None:
            fits = minimum <= available
            if fits:
                batch_size = 1
                for candidate in range(max(1, int(max_batch)), 0, -1):
                    if need(candidate) <= available:
                        batch_size = candidate
                        break
                required, tokens = need(batch_size), batched_kv_tokens(batch_size,
                                                                       think_tokens)
    else:
        tokens = _positive_int(context_tokens) or int(MT_CONTEXT_TOKENS)
        minimum = required = base + kv_need(tokens, HW_OLLAMA_NUM_PARALLEL)
        fits = None if available is None else minimum <= available
        batch_size = 1 if fits else None

    return MemoryFit(weights_bytes=weights, kv_bytes_per_token=per_token,
                     kv_source=kv_source, mode=mode, kv_tokens=tokens,
                     workspace_bytes=workspace, required_bytes=required,
                     minimum_bytes=minimum, available_bytes=available, fits=fits,
                     batch_size=batch_size, hardware_source=hw.source)


def reference_fit(hw: Optional[HardwareInfo] = None) -> MemoryFit:
    """Referans 4 bit 7B model (toplu kip) bu Mac'te."""
    return assess(HW_REFERENCE_WEIGHTS_BYTES, reference_kv(), mode=MODE_BATCHED, hw=hw)


@dataclass(frozen=True)
class GateVerdict:
    """Genel kapının hükmü: ``allowed`` False ise "Yerel model ile çevir" kapalı."""
    allowed: bool
    reason: str                   # kapalıysa Türkçe neden
    warning: str                  # ölçülemediyse / yedek ölçümse Türkçe not
    hardware: HardwareInfo
    reference: MemoryFit


def _ratio_text(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _hardware_warning(hw: HardwareInfo) -> str:
    if not hw.measured:
        return MSG_HW_UNMEASURED.format(why=hw.problem or "neden bilinmiyor")
    if hw.source == SOURCE_SYSCTL:
        return MSG_HW_FALLBACK.format(why=hw.problem or "neden bilinmiyor",
                                      ratio=_ratio_text(HW_WORKING_SET_FALLBACK_RATIO))
    return ""


def general_gate(hw: Optional[HardwareInfo] = None) -> GateVerdict:
    """Bu Mac referans 4 bit 7B modeli toplu iş 1 ile çalıştırabiliyor mu?

    Ölçülemezse kapı AÇIK kalır (``warning`` doludur): ölçemediğimiz makineyi
    kapatmak uydurma hüküm olurdu.
    """
    hw = hw if hw is not None else hardware_info()
    fit = reference_fit(hw)
    warning = _hardware_warning(hw)
    if fit.fits is False:
        reason = MSG_HW_TOO_SMALL.format(need=format_gb(fit.minimum_bytes, round_up=True),
                                         have=format_gb(hw.available_bytes, round_up=False))
        return GateVerdict(False, reason, warning, hw, fit)
    return GateVerdict(True, "", warning, hw, fit)


# ---------------------------------------------------------------------------
# Biçim ve iletiler
# ---------------------------------------------------------------------------
def _gb_tenths(num_bytes: Optional[int], *, round_up: bool) -> int:
    tenths_exact = (num_bytes or 0) * _GB_TENTHS / BYTES_PER_GB
    return math.ceil(tenths_exact) if round_up else math.floor(tenths_exact)


def _tenths_text(tenths: int) -> str:
    return f"{tenths // _GB_TENTHS},{tenths % _GB_TENTHS}"


def format_gb(num_bytes: Optional[int], *, round_up: bool) -> str:
    """Ondalık GB, bir basamak, Türkçe virgülle. Gereken yukarı, eldeki aşağı
    yuvarlanır: gösterilen gereksinim hiçbir zaman eksik, eldeki fazla görünmez."""
    return _tenths_text(_gb_tenths(num_bytes, round_up=round_up))


def format_fit_gb(required_bytes: Optional[int], available_bytes: Optional[int]) -> str:
    """SIĞAN modelin gereken belleği (``required <= available``) ekranda.

    2026-09-29 (F4-13): gereken yukarı, eldeki aşağı yuvarlandığı için dar
    bir bantta "gerekli ~30,2 GB · kullanılabilir 30,1 GB · sığar" çelişkisi
    çıkıyordu. Sığan modelde gereken, eldekinin gösteriminden büyük
    gösterilmez: ``min(yukarı(gereken), aşağı(eldeki))`` ("~" yaklaşık
    işaretiyle; fark < 0,1 GB). Eldeki bilinmiyorsa ya da gereken ondan
    büyükse (hüküm sığmaz) ``format_gb(..., round_up=True)`` ile aynı.
    """
    need = _gb_tenths(required_bytes, round_up=True)
    if (isinstance(available_bytes, int) and available_bytes > 0
            and isinstance(required_bytes, int) and required_bytes <= available_bytes):
        need = min(need, _gb_tenths(available_bytes, round_up=False))
    return _tenths_text(need)


def server_verdict(fit: MemoryFit, *, loaded: Optional[bool] = None) -> MemoryFit:
    """LM Studio / OpenAI uyumlu sunucu modelinin hükmü (MT-12, 2026-09-30).

    Bu sunucular KV şeklini bildirmez; tahmin ağırlıkla orantılı yedektir
    (``KV_SOURCE_RATIO``) ve MoE / karma dikkatli modellerde aşırıdır
    (lmstudio:qwen/qwen3.5-35b-a3b "~33,5 GB, sığmaz"). Yükleme ve bellek
    denetimi sunucunundur:

    - sunucu modeli ŞU AN yüklü tutuyorsa (*loaded* True) "sığmaz" hükmü
      verilmez: ``fits`` None, ``verdict`` ``VERDICT_LOADED`` (zaten çalışıyor);
    - KV şekli bilinmiyorsa (oran yedeği) "sığmaz" ENGEL değil uyarıdır:
      ``fits`` False kalır, ``verdict`` ``VERDICT_ADVISORY``;
    - şekil biliniyorsa ya da tahmin sığıyorsa hüküm aynen kalır.

    MLX / GGUF dosyası ve Ollama modeli bu fonksiyondan GEÇMEZ (engel aynen).
    """
    if fit.fits is not False:
        return fit
    if loaded is True:
        return replace(fit, fits=None, batch_size=None, verdict=VERDICT_LOADED)
    if fit.kv_source == KV_SOURCE_RATIO:
        return replace(fit, verdict=VERDICT_ADVISORY)
    return fit


def blocks(fit: Optional[MemoryFit]) -> bool:
    """Hüküm modeli kullanılamaz yapar mı ("sığmaz" + bağlayıcı)?"""
    return fit is not None and fit.fits is False and fit.verdict == VERDICT_BINDING


def fit_warning(fit: Optional[MemoryFit]) -> str:
    """Engel olmayan "sığmayabilir" hükmünün uyarısı (CLI ve ön kontrol); yoksa ""."""
    if fit is None or fit.fits is not False or fit.verdict != VERDICT_ADVISORY:
        return ""
    return MSG_HW_ROUGH.format(need=format_gb(fit.minimum_bytes, round_up=True),
                               have=format_gb(fit.available_bytes, round_up=False))


def too_big_label(fit: MemoryFit) -> str:
    """Seçicideki kısa not: "bu Mac'e sığmaz (gerekli ~X GB, kullanılabilir Y GB)".

    MT-12: hüküm yalnız uyarıysa (``VERDICT_ADVISORY``) "bu Mac'e sığmayabilir
    (kaba tahmin ~X GB, …)" — seçici modeli kullanılamaz göstermez.
    """
    template = (MSG_HW_ROUGH_LABEL if fit.verdict == VERDICT_ADVISORY
                else MSG_HW_TOO_BIG_LABEL)
    return template.format(need=format_gb(fit.minimum_bytes, round_up=True),
                           have=format_gb(fit.available_bytes, round_up=False))


def too_big_message(fit: MemoryFit) -> str:
    """Model başına kapının nedeni (CLI ve arayüz aynı metin)."""
    return MSG_HW_TOO_BIG.format(need=format_gb(fit.minimum_bytes, round_up=True),
                                 have=format_gb(fit.available_bytes, round_up=False))


def batch_note(fit: Optional[MemoryFit]) -> str:
    """Toplu iş belleğe göre küçüldüyse not; küçülmediyse ""."""
    if (fit is None or fit.mode != MODE_BATCHED or not fit.batch_size
            or fit.batch_size >= DEFAULT_BATCH_SIZE_HYMT_LOCAL):
        return ""
    return MSG_HW_BATCH.format(batch=fit.batch_size,
                               need=format_fit_gb(fit.required_bytes, fit.available_bytes),
                               have=format_gb(fit.available_bytes, round_up=False))


def hardware_caption(hw: Optional[HardwareInfo] = None) -> str:
    """Kenar çubuğu satırı: "Apple M4 Max · yerel model için kullanılabilir bellek 30,1 GB"."""
    hw = hw if hw is not None else hardware_info()
    chip = hw.chip or _UNKNOWN_CHIP
    if not hw.measured:
        return MSG_HW_CAPTION_UNMEASURED.format(chip=chip)
    caption = MSG_HW_CAPTION.format(chip=chip,
                                    have=format_gb(hw.available_bytes, round_up=False))
    # Yedek (sysctl × oran) değer gerçek Metal ölçümü gibi gösterilmez.
    return caption + (MSG_HW_CAPTION_FALLBACK_NOTE if hw.source == SOURCE_SYSCTL else "")
