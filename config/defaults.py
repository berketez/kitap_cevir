"""Merkezi config — tüm proje boyunca paylaşılan sabitler.

Bu modül tek yerden değiştirilince tüm çağrı noktaları
(core/, latex_pipeline/, app.py) güncellenmiş olur.
Yeni sayısal sabit ekleyeceksen: önce buraya ekle, sonra import et.
Hardcoded literal yazma (KURAL §11).
"""

# ---------------------------------------------------------------------------
# Batch boyutları
# ---------------------------------------------------------------------------
DEFAULT_BATCH_SIZE_HYMT_LOCAL = 20   # HYMT yerel model (MPS/MLX) batch
# (KARAR 35, 2026-10-05: dış AI API sabitleri — DEFAULT_BATCH_SIZE_API,
# AI_REVIEW_MAX_TOKENS, MT_API_TIMEOUT_S — AI düzeltme yoluyla birlikte silindi.)
DEFAULT_BATCH_SIZE_UI_SLIDER = 20    # Streamlit slider varsayılanı

# ---------------------------------------------------------------------------
# Segment / token limitleri
# ---------------------------------------------------------------------------
MAX_SEGMENT_LENGTH = 2000            # Karakter cinsinden üst sınır
MT_MAX_INPUT_TOKENS = 2048           # HYMT giriş token üst sınırı

# ---------------------------------------------------------------------------
# Timeout'lar (saniye)
# ---------------------------------------------------------------------------
SEGMENT_TIMEOUT_S = 300              # Tek segment çeviri timeout'u
GENERATE_LOCK_TIMEOUT_S = 300        # MT generate kilidi acquire üst sınırı
                                     # (SEGMENT_TIMEOUT_S ile aynı değer ama
                                     # AYRI kavram — bilinçli olarak bağımsız)
XELATEX_TIMEOUT_S = 600              # Tam kitap xelatex derleme timeout'u
XELATEX_QUICK_TIMEOUT_S = 120        # Hızlı tek bölüm derleme timeout'u
EXTRACT_TIMEOUT_S = 3600             # PDF → LaTeX çıkarma timeout'u

# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------
DEFAULT_RETRY_COUNT = 3              # Hata durumunda yeniden deneme adedi
DEFAULT_RETRY_BASE_DELAY_S = 2.0     # Exponential backoff base delay

# ---------------------------------------------------------------------------
# pdftext worker
# ---------------------------------------------------------------------------
PDFTEXT_WORKERS = 1                  # BrokenProcessPool fix sonrası sabit 1

# ---------------------------------------------------------------------------
# Checkpoint
# ---------------------------------------------------------------------------
CHECKPOINT_INTERVAL_SEGMENTS = 50    # Kaç segmentte bir checkpoint yazılır

# ---------------------------------------------------------------------------
# Çeviri kalite kapısı
# ---------------------------------------------------------------------------
# Bu orandan fazla segment başarısızsa gövde 'çeviri' sayılmaz: FINAL
# checkpoint yazılmaz (latex_pipeline/translator.py) ve translated_body.tex
# resume kaynağı olamaz (translate_book.py). İki kapı AYNI değeri buradan
# import eder (F3, 2026-07-28).
TRANSLATION_MAX_FAIL_RATIO = 0.5

# ---------------------------------------------------------------------------
# Yerel model sunucuları ve keşif (2026-09-25, "Yerel model ile çevir" dört
# kaynak: MLX klasörü, Ollama, LM Studio / OpenAI uyumlu, GGUF)
# ---------------------------------------------------------------------------
# Yalnız bu bilgisayar (loopback) — KURALLAR.md Madde 12. Adresler 127.0.0.1
# ile yazılır: 'localhost' önce ::1 dener, gecikme yaratabilir.
OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434"
LMSTUDIO_DEFAULT_URL = "http://127.0.0.1:1234/v1"
DISCOVERY_HTTP_TIMEOUT_S = 1.5       # keşif: sunucu listesi isteği
ENGINE_CHECK_HTTP_TIMEOUT_S = 3.0    # ön kontrol (CLI + arayüz "Çevir" öncesi)
MT_HTTP_TIMEOUT_S = 240              # tek üretim isteği; SEGMENT_TIMEOUT_S'den
                                     # KÜÇÜK olmalı (zombi thread süresi sınırı)
MT_MODEL_LOAD_TIMEOUT_S = 600        # sunucuda model yükleme / ısınma isteği
MT_HTTP_MAX_PARALLEL = 1             # toplu üretimde eşzamanlı istek (1 = sıralı)
MT_HTTP_MAX_PARALLEL_LIMIT = 8       # options.max_parallel üst sınırı
MT_HTTP_CIRCUIT_MAX_FAILURES = DEFAULT_RETRY_COUNT   # art arda bağlantı hatası
                                                     # sonrası devre açılır
MT_CONTEXT_TOKENS = 8192             # Ollama num_ctx / llama.cpp n_ctx alt sınırı
                                     # (koşu boyunca sabit)
MT_CONTEXT_CHARS_PER_TOKEN = 3.0     # bağlam aşımı ön tahmini (karakter/token);
                                     # _scale_max_tokens ile aynı varsayım
OLLAMA_KEEP_ALIVE = "30m"
# Greedy üretim: MLX generate varsayılanıyla aynı (örnekleme yok, ceza yok)
MT_GREEDY_TEMPERATURE = 0.0
MT_GREEDY_SEED = 0
MT_REPEAT_PENALTY = 1.0
MT_HTTP_MAX_RESPONSE_BYTES = 32 * 1024 * 1024   # tek HTTP yanıtı üst sınırı
MT_HTTP_ERROR_BODY_MAX_CHARS = 2000  # hata gövdesinden saklanan en çok karakter
DISCOVERY_MAX_DEPTH = 3              # models/ altında tarama derinliği
DISCOVERY_MAX_ENTRIES = 5000         # tarama başına en çok dizin girdisi
DISCOVERY_MAX_SHOW_REQUESTS = 20     # Ollama /api/show ile zenginleştirilen model sayısı
GGUF_MAX_KV_COUNT = 10_000           # GGUF üst veri okuma korumaları
GGUF_MAX_STRING_BYTES = 16 * 1024 * 1024

# ---------------------------------------------------------------------------
# Çıktı token sınırı (2026-09-28; iç ölçüm). Bütün arka uçlar aynı kuralı
# kullanır:
#   sınır = kaynak karakter / MT_CONTEXT_CHARS_PER_TOKEN × MT_OUTPUT_TOKENS_FACTOR
#   en az MT_OUTPUT_TOKENS_FLOOR, en çok MT_OUTPUT_TOKENS_CAP.
# Ölçüm: 1423 gerçek HY-MT çevirisinde en uzun çıktı 239 token; Yol A
# çiftlerinde Türkçe çıktı 0,37–0,48 token/kaynak karakter, en kötü paragraf
# 0,614 (sentetik fr). 2,2 / 3 = 0,733 token/karakter (~%20 pay).
# Taban 512 kısa parçada kısır döngü frenidir (model durmazsa sınıra kadar üretir).
# ---------------------------------------------------------------------------
MT_OUTPUT_TOKENS_FLOOR = 512
MT_OUTPUT_TOKENS_FACTOR = 2.2
MT_OUTPUT_TOKENS_CAP = 4096          # (eskiden core/translator._MAX_TOKENS_CAP)
MT_OUTPUT_NEAR_LIMIT_RATIO = 0.95    # üretilen token sınırın bu oranına varırsa
                                     # çıktı "kesilmiş olabilir" diye işaretlenir

# ---------------------------------------------------------------------------
# Düşünme sınırı (2026-09-28, KARARLAR 16; 2026-10-05 KARARLAR 22 ile kısmen
# değişti). Düşünme resmî yolla kapatılabiliyorsa kapatılır. Kapatılmış
# (ya da düşünmesi bilinmeyen) modelde aşağıdaki bütçe yalnız "kapatma tuttu
# mu" yoklamasıdır: yanıt akışla okunur, bütçe aşılınca istek kesilir ve
# kapatma tutmadıysa model koşu boyu "kapatilamaz" sayılıp düşünerek çeviri
# kipine geçer (aşağıdaki bölüm). Motor başına mt_engines.json'da değiştirilebilir.
# ---------------------------------------------------------------------------
MT_THINK_BUDGET_TOKENS = 512         # düşünmesi kapalı sayılan modelde parça başı yoklama bütçesi
MT_THINK_RETRY_COUNT = 1             # bütçeyi aşan / payı dolup cevap vermeyen parça için TEK yeniden deneme

# ---------------------------------------------------------------------------
# Düşünerek çeviri (2026-10-05, KARARLAR 22: "düşünme kapatılamıyorsa
# düşünecek, o şekilde çevirecek"). Düşünmesi kapatılamayan modelde (keşifte
# "kapatilamaz" ya da koşuda kapatma tutmadı) her parçaya AYRI bir düşünme payı:
#   pay = MT_THINK_ALLOWANCE_BASE_TOKENS
#         + kaynak karakter × MT_THINK_ALLOWANCE_PER_CHAR, en çok MT_THINK_ALLOWANCE_CAP_TOKENS
# (mt_engines.json'da motorun options.think_budget'ı tavanı ezer). İstek sınırı
# = pay + çeviri sınırı (MT_OUTPUT_TOKENS_* kuralı); düşünce akıştan ayrılır,
# çeviriye karışmaz.
#
# İddia: taban 1024 + karakter başına 1 token, tavan 4096 düşünen modellerin
#   parçaların çoğunu pay dolmadan bitirmesine yeter.
# Kanıt: iç model testi ölçümü (30 Eylül 2026, LM Studio): gpt-oss-20b parça başına ~35 token (84 parçada 2969 token, 512
#   aşımı 0); gemma-4-26b denemelerin 312/389'unda, qwen3.8-27b 53/113'ünde
#   512'yi aştı. Aşanların GERÇEK uzunluğu ÖLÇÜLMEDİ (513. token'da kesildi);
#   aşmayanların ortalaması gemma'da ~267, qwen3.8'de ~248 token ((toplam −
#   aşan × 513) / aşmayan; pipeline.log son "Model ölçümleri" satırları).
# Güven: düşük — aşan denemelerin dağılımı bilinmiyor. Ayarlanabilir: gerçek
#   koşunun günlüğündeki "[MT DÜŞÜNME] Düşünerek çeviri" satırları (parça başı
#   ölçülen düşünme token'ı ve süresi) ile yeniden seçilir.
# Tavanın üst sınırı bağlamdır: MT_MAX_INPUT_TOKENS + en uzun parçanın
#   (MAX_SEGMENT_LENGTH) çeviri sınırı + tavan <= MT_CONTEXT_TOKENS (2048 + 1466
#   + 4096 = 7610 <= 8192): düşünen istek varsayılan bağlama sığar (testle kilitli).
# ---------------------------------------------------------------------------
MT_THINK_ALLOWANCE_BASE_TOKENS = 1024
MT_THINK_ALLOWANCE_PER_CHAR = 1.0
MT_THINK_ALLOWANCE_CAP_TOKENS = 4096

# Zaman aşımı (düşünerek çeviride; düşünmeyen modelde MT_HTTP_TIMEOUT_S aynen):
#   süre = MT_THINK_TIMEOUT_BASE_S
#          + (çeviri sınırı + deneme sayısı × pay) / hız × MT_THINK_TIMEOUT_SAFETY
#   en çok motorun istek zaman aşımı (options.request_timeout_s ya da
#   MT_HTTP_TIMEOUT_S: latex_pipeline'ın parça süresi SEGMENT_TIMEOUT_S'yi
#   aşan çağrı terk edilir, parça boşa gider). Denemeler süreyi paylaşır; tavan
#   bağlıyorsa ve hız ölçüldüyse pay, deneme sayısı kadar pay + çeviri sınırı
#   bu süreye sığacak kadar kısılır (tek yeniden denemeye de süre kalır).
# hız: koşuda ÖLÇÜLEN düşünme token/sn — en az MT_THINK_RATE_MIN_TOKENS
#   token'lık akışlı denemelerden, istek başından son düşünme token'ına kadar
#   (istem işleme dahil = temkinli); ölçüm yokken MT_THINK_ASSUMED_TOKENS_PER_S.
# İddia/Kanıt: aynı raporun pipeline.log zaman damgaları — qwen3.8-27b'de 513
#   düşünme token'ı ~50–57 sn (~9–10 token/sn), gemma-4-26b-a4b'de ~7–8 sn
#   (~65–70 token/sn). Varsayılan hız ölçülen en yavaştan düşük (8) seçildi;
#   güvenlik çarpanı ölçümün dalgalanmasına (uzun istemde istem işleme) pay
#   bırakır, taban istem işleme / model uyanması içindir. Güven: orta.
MT_THINK_ASSUMED_TOKENS_PER_S = 8.0
MT_THINK_RATE_MIN_TOKENS = 64
MT_THINK_TIMEOUT_BASE_S = 30.0
MT_THINK_TIMEOUT_SAFETY = 2.0

# Devre kesici (KARARLAR 22: düşünme aşımında DEĞİL, yalnız "model cevap
# üretemiyor"da çıkış 5). Düşünerek çeviride pay dolup yeniden denemede de
# cevap gelmeyen parça "cevapsız"dır. Pencere koşu BOYUNCA kayar (RAPOR MT-13:
# eski pencere yalnız koşunun ilk 5 üretim çağrısına bakıyordu; gemma
# koşusunda ilk 4 kısa başlık çevrildi, sonraki 312 aşım hiç değerlendirilmedi):
# son MT_THINK_BREAKER_WINDOW düşünmeli parçada, en az
# MT_THINK_BREAKER_MIN_SAMPLES örnek varken cevapsızların oranı
# MT_THINK_BREAKER_RATIO'ya varırsa devre açılır. Kaynağı
# MT_THINK_BREAKER_SHORT_CHARS karakter ya da daha kısa parçalar (başlık) AYRI
# pencerede sayılır: başlıkların çevrilmesi gövdenin cevapsızlığını örtmez.
# Oran "parçaların çoğunda cevap yok"u (> yarı) söyler; 5 örnekle 3 cevapsız
# eski "ilk 5'in 3'ü" eşiğiyle aynı orandır, ama her yerde geçerlidir.
MT_THINK_BREAKER_WINDOW = 10
MT_THINK_BREAKER_MIN_SAMPLES = 5
MT_THINK_BREAKER_RATIO = 0.6
MT_THINK_BREAKER_SHORT_CHARS = 80

__all__ = [
    "DEFAULT_BATCH_SIZE_HYMT_LOCAL",
    "DEFAULT_BATCH_SIZE_UI_SLIDER",
    "MAX_SEGMENT_LENGTH",
    "MT_MAX_INPUT_TOKENS",
    "SEGMENT_TIMEOUT_S",
    "GENERATE_LOCK_TIMEOUT_S",
    "XELATEX_TIMEOUT_S",
    "XELATEX_QUICK_TIMEOUT_S",
    "EXTRACT_TIMEOUT_S",
    "DEFAULT_RETRY_COUNT",
    "DEFAULT_RETRY_BASE_DELAY_S",
    "PDFTEXT_WORKERS",
    "CHECKPOINT_INTERVAL_SEGMENTS",
    "TRANSLATION_MAX_FAIL_RATIO",
    "OLLAMA_DEFAULT_URL",
    "LMSTUDIO_DEFAULT_URL",
    "DISCOVERY_HTTP_TIMEOUT_S",
    "ENGINE_CHECK_HTTP_TIMEOUT_S",
    "MT_HTTP_TIMEOUT_S",
    "MT_MODEL_LOAD_TIMEOUT_S",
    "MT_HTTP_MAX_PARALLEL",
    "MT_HTTP_MAX_PARALLEL_LIMIT",
    "MT_HTTP_CIRCUIT_MAX_FAILURES",
    "MT_CONTEXT_TOKENS",
    "MT_CONTEXT_CHARS_PER_TOKEN",
    "OLLAMA_KEEP_ALIVE",
    "MT_GREEDY_TEMPERATURE",
    "MT_GREEDY_SEED",
    "MT_REPEAT_PENALTY",
    "MT_HTTP_MAX_RESPONSE_BYTES",
    "MT_HTTP_ERROR_BODY_MAX_CHARS",
    "DISCOVERY_MAX_DEPTH",
    "DISCOVERY_MAX_ENTRIES",
    "DISCOVERY_MAX_SHOW_REQUESTS",
    "GGUF_MAX_KV_COUNT",
    "GGUF_MAX_STRING_BYTES",
    "MT_OUTPUT_TOKENS_FLOOR",
    "MT_OUTPUT_TOKENS_FACTOR",
    "MT_OUTPUT_TOKENS_CAP",
    "MT_OUTPUT_NEAR_LIMIT_RATIO",
    "MT_THINK_BUDGET_TOKENS",
    "MT_THINK_RETRY_COUNT",
    "MT_THINK_ALLOWANCE_BASE_TOKENS",
    "MT_THINK_ALLOWANCE_PER_CHAR",
    "MT_THINK_ALLOWANCE_CAP_TOKENS",
    "MT_THINK_ASSUMED_TOKENS_PER_S",
    "MT_THINK_RATE_MIN_TOKENS",
    "MT_THINK_TIMEOUT_BASE_S",
    "MT_THINK_TIMEOUT_SAFETY",
    "MT_THINK_BREAKER_WINDOW",
    "MT_THINK_BREAKER_MIN_SAMPLES",
    "MT_THINK_BREAKER_RATIO",
    "MT_THINK_BREAKER_SHORT_CHARS",
]

# ---------------------------------------------------------------------------
# Donanım kapısı (2026-09-28, KARARLAR 17 ve 17a). Kaynaklar, güven düzeyleri
# ve "ne değişebilir" notları: docs/DONANIM_VARSAYIMLARI.md. Ölçüm
# core/hardware.py'de GERÇEKTİR (MLX mx.device_info() ->
# max_recommended_working_set_size); buradaki sayılar yalnız tahminin
# varsayımlarıdır, saha dönütüyle YALNIZ bu sayılar değişir:
#   gerekli = ağırlık (diskte ölçülen bayt)
#           + KV önbelleği (katman × KV başlığı × (anahtar + değer boyutu)
#                           × öğe baytı × token)
#           + çalışma payı (ağırlık × HW_WORKSPACE_FRACTION)
#   kullanılabilir = max_recommended_working_set_size
# Token üst sınırları mevcut sabitlerden türer (yeni sayı yok): giriş
# MT_MAX_INPUT_TOKENS, çıktı MT_OUTPUT_TOKENS_* kuralı (toplu yolda en uzun
# parça MAX_SEGMENT_LENGTH), sabit bağlamlı arka uçta MT_CONTEXT_TOKENS; en
# büyük toplu iş DEFAULT_BATCH_SIZE_HYMT_LOCAL.
# ---------------------------------------------------------------------------
# Genel kapının referans modeli: 4 bit 7B, mlx-community/
# Mistral-7B-Instruct-v0.3-4bit (Hugging Face API, safetensors toplamı;
# config.json: 32 katman, 8 KV başlığı, head_dim = 4096 / 32 = 128,
# bfloat16). Bu model toplu iş 1 ile sığmıyorsa "Yerel model ile çevir"
# kapalıdır (KARARLAR 17: "en az 7B'lik modelleri çalıştıramıyorsa").
HW_REFERENCE_WEIGHTS_BYTES = 4_077_480_215
HW_REFERENCE_LAYERS = 32
HW_REFERENCE_KV_HEADS = 8
HW_REFERENCE_HEAD_DIM = 128
# KV önbelleği öğe boyutu (bayt): MLX anahtar/değeri modelin hesap türünde
# tutar (bfloat16 / float16 = 2; mlx_lm cache.py keys.dtype); llama.cpp
# (type_k/type_v) ve Ollama (OLLAMA_KV_CACHE_TYPE) varsayılanı f16 = 2.
# config.json'da float32 yazan modelde 4 kullanılır.
HW_KV_DTYPE_BYTES = 2
# Ollama'nın model başına eşzamanlı istek sayısı (OLLAMA_NUM_PARALLEL
# varsayılanı 1; gereken bellek NUM_PARALLEL × bağlam ile büyür).
HW_OLLAMA_NUM_PARALLEL = 1
# Çalışma payı (TEK varsayım kalemi, ÖLÇÜLMEDİ): etkinleşmeler, ara tamponlar,
# MLX önbellek tamponları. EleutherAI "Transformer Math 101": çıkarımda
# toplam ≈ 1,2 × ağırlık (ileri geçişin ek yükü ≤ %20; kaynak bu payın
# içeriğini ayrıntılandırmıyor, KV'yi ayrıca anmıyor). %20 ile referans 7B
# 4 bit, 8 GB Mac'in çalışma kümesinin (5461 MB) %99,5'ini dolduruyordu; LM
# Studio 8 GB Mac'e "küçük modeller" diyor, Ollama #1231'de (M1; çalışma
# kümesi 5461 MB -> 8 GB çıkarımı; `ollama run llama2`, 3648 MB ağırlık =
# 7B 4 bit) model sığmayıp düştü. Bu yüzden %25. Gerçek pay HY-MT yüklenerek
# ölçülecek (Berke'nin model testi).
HW_WORKSPACE_FRACTION = 0.25
# Metal ölçümü alınamazsa yedek: kullanılabilir = sysctl hw.memsize × oran.
# Gözlenen en düşük oran 2/3 (M1: recommendedMaxWorkingSetSize 5461,34 MB
# = 8192 MB × 2/3, Ollama #1231; bellek miktarı günlükte yazmıyor, 8 GB bu
# eşitlikten çıkarılır); bu Mac 36 GB -> 28,1 GiB (0,78).
HW_WORKING_SET_FALLBACK_RATIO = 2 / 3
HW_SYSCTL_TIMEOUT_S = 2.0            # sysctl alt süreci (çip adı, hw.memsize)
# Kayan pencereli dikkat (SWA; Gemma 3/4'te katmanların çoğu) KV'yi bağlamla
# değil pencereyle tutar. llama.cpp SWA önbelleğini
# n_swa × dizi + n_ubatch token ayırır (src/llama-kv-cache-iswa.cpp,
# size_swa); n_ubatch / Ollama num_batch varsayılanı 512. Pencereye eklenen pay.
HW_SWA_EXTRA_TOKENS = 512

__all__ += [
    "HW_REFERENCE_WEIGHTS_BYTES",
    "HW_REFERENCE_LAYERS",
    "HW_REFERENCE_KV_HEADS",
    "HW_REFERENCE_HEAD_DIM",
    "HW_KV_DTYPE_BYTES",
    "HW_OLLAMA_NUM_PARALLEL",
    "HW_WORKSPACE_FRACTION",
    "HW_WORKING_SET_FALLBACK_RATIO",
    "HW_SYSCTL_TIMEOUT_S",
    "HW_SWA_EXTRA_TOKENS",
]
