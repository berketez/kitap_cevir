# Donanım kapısı: ölçüm, formül ve varsayımlar

“Yerel model ile çevir” yolu bu Mac'in belleğine bakar (28 Eylül 2026'dan
beri). Bu belge kapının neyi ölçtüğünü, neyi varsaydığını ve her
varsayımın kaynağını anlatır. Sayıların tek kaynağı `config/defaults.py`'nin
“Donanım kapısı” bölümüdür (`HW_*` sabitleri); kod `core/hardware.py`'dedir.

**Saha dönütüyle yalnız sayı değişir:** varsayımlardan biri yanlış çıkarsa
(ör. HY-MT yüklenerek ölçülen gerçek çalışma payı farklıysa) formüle ve koda
dokunulmaz, `config/defaults.py`'deki ilgili sayı güncellenir ve bu belgedeki
kaydı yenilenir.

## Ne ölçülür (varsayım değil)

- **Kullanılabilir bellek:** MLX `mx.device_info()` (eski sürümlerde
  `mx.metal.device_info()`) sözlüğündeki `max_recommended_working_set_size`.
  Bu, Metal'in `recommendedMaxWorkingSetSize` değeridir; mlx-lm modeli
  yüklerken bellek sınırını da buna çeker. Bu Mac'te (Apple M4 Max, 36 GB):
  30 150 672 384 bayt = 28,1 GiB = arayüzde “30,1 GB” (ondalık GB).
- **Toplam bellek:** aynı sözlükteki `memory_size` (yoksa `sysctl hw.memsize`).
- **Çip adı:** `sysctl -n machdep.cpu.brand_string` (yoksa MLX'in `device_name`'i).
- **Model ağırlığı:** keşfin ölçtüğü gerçek bayt: MLX klasöründeki
  `*.safetensors` toplamı, GGUF dosyası (parçalıysa parçaların toplamı),
  Ollama `/api/tags` `size`, LM Studio `size_bytes`. HY-MT1.5 7B (bf16) diskte
  15 009 177 951 bayt.
- **KV şekli:** modelin kendi üst verisinden: MLX `config.json`
  (`num_hidden_layers`, `num_key_value_heads`, `head_dim`), GGUF başlığı ve
  Ollama `/api/show` `model_info` (`<mimari>.block_count`,
  `<mimari>.attention.head_count_kv`, `key_length` / `value_length`).

Ölçüm alınamazsa sahte değer gösterilmez: MLX yoksa yedek `sysctl hw.memsize` ×
`HW_WORKING_SET_FALLBACK_RATIO` kullanılır ve arayüzde bu yazılır; o da yoksa
“bellek ölçülemedi” uyarısıyla kapı açık kalır.

## Formül

    gerekli = ağırlık
            + KV önbelleği = katman × KV başlığı × (anahtar + değer boyutu) × öğe baytı × token
            + çalışma payı = ağırlık × HW_WORKSPACE_FRACTION
    kullanılabilir = max_recommended_working_set_size

Token sayısı mevcut sabitlerden türer (yeni sayı yoktur):

- **Süreç içi MLX (toplu kip):** KV parça başına büyür ve toplu üretimde parça
  sayısıyla çarpılır. Bir parça en çok `MT_MAX_INPUT_TOKENS` (giriş) + çıktı
  kuralının `MAX_SEGMENT_LENGTH` karakterlik parçaya verdiği sınır (bugün
  2048 + 1466 = 3514 token) tutar; tek parça yolunda (uzun parça) giriş +
  `MT_OUTPUT_TOKENS_CAP` (bugün 6144 token). `b` parçalık toplu işte KV token
  sayısı `max(b × 3514, 6144)`'tür. Sığan en büyük `b`
  (≤ `DEFAULT_BATCH_SIZE_HYMT_LOCAL`) çeviriye verilir.
- **llama.cpp, Ollama, LM Studio (bağlam kipi):** KV, bağlam uzunluğu kadar
  baştan ayrılır: llama.cpp'de `n_ctx` (yoksa `MT_CONTEXT_TOKENS`), Ollama'da
  `max(MT_CONTEXT_TOKENS, num_ctx)` (modelin bağlam sınırıyla kırpılır) ×
  `HW_OLLAMA_NUM_PARALLEL`, LM Studio'da `num_ctx` ya da `MT_CONTEXT_TOKENS`.
- **KV şekli okunamazsa** (LM Studio, eksik üst veri, sıkıştırılmış KV'li
  mimari): token başına KV ağırlıkla orantılı alınır; oran referans modelin
  oranıdır (128 KiB/token ÷ 4 077 480 215 bayt). Yüksek hassasiyetli modelde
  üst sınır yönünde yanılır.

Tasarımdaki ilk hesap (HY-MT için 4248 token × 20 parça ≈ 10,4 GiB) kaba bir
üst sınır örneğiydi; formül token sayısını yukarıdaki sabitlerden türetir
(3514 × 20 × 128 KiB ≈ 8,6 GiB). Gerçekte `LatexTranslator` en çok
`PipelineConfig.mlx_batch_size` (8) parçayı aynı anda üretir; 20 ile hesap
temkinli yöndedir.

**Genel kapı:** referans 4 bit 7B model (aşağıda) toplu iş 1 ile sığmıyorsa
“Yerel model ile çevir” bu Mac'te kapalıdır; “PDF'den TeX çıkar” çalışır.
**Model başına kapı:** toplu iş 1 ile bile sığmayan model “bu Mac'e sığmaz”
nedeniyle seçilemez (MLX, GGUF, Ollama); boyutu bilinmeyen model (genel
OpenAI uyumlu sunucu) için hüküm verilmez. LM Studio ve boyutu bilinen OpenAI
uyumlu sunucu modelinde (KV şekli bildirilmez, tahmin oran yedeğidir) “sığmaz”
engel değil gerekçeli uyarıdır (“bu Mac'e sığmayabilir (kaba tahmin …)”);
sunucunun şu an yüklü tuttuğu modele sığma hükmü verilmez (varsayım 10).

## Formülün öngördüğü sonuçlar

Aşağıdaki çalışma kümesi değerleri 36 GB dışında bu Mac'te ölçülmedi; kamuya
açık günlüklerden alındı. Sonuçlar formülün hesabıdır, gerçek yükleme ölçümü
değildir.

| Mac belleği | Kullanılabilir (çalışma kümesi) | Genel kapı | HY-MT 7B bf16 | 4 bit 7B toplu iş |
|---|---|---|---|---|
| 8 GB | 5,7 GB (5461 MB, günlük) | kapalı (en az 6,0 GB gerekir) | sığmaz | — |
| 16 GB | 11,4 GB (10923 MB, günlük) | açık | sığmaz (~19,6 GB gerekir) | 13 |
| 36 GB (bu Mac) | 30,1 GB (ölçüldü) | açık | sığar, toplu iş 20 | 20 |

## Varsayımlar ve kaynakları

### 1. Kullanılabilir bellek = Metal'in önerdiği çalışma kümesi

- Claim: Yerel modelin kullanabileceği bellek Metal'in `recommendedMaxWorkingSetSize` değeridir; MLX bunu `mx.device_info()["max_recommended_working_set_size"]` olarak verir, mlx-lm de model yüklerken bellek sınırını bu değere çeker ve ağırlık bu değerin %90'ını aşarsa yavaşlık uyarısı verir.
- Evidence: https://developer.apple.com/documentation/metal/mtldevice/recommendedmaxworkingsetsize ; https://ml-explore.github.io/mlx/build/html/python/_autosummary/mlx.core.device_info.html ; https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/generate.py (`wired_limit`: `max_rec_size = mx.device_info()["max_recommended_working_set_size"]`, `if model_bytes > 0.9 * max_rec_size`; kurulu mlx-lm 0.30.7'de aynı); bu Mac'te `mx.device_info()` = 30 150 672 384 bayt (MLX 0.31.0).
- Confidence: high
- Date checked: 2026-09-28
- What may change: macOS sürümü değeri değiştirebilir; `sudo sysctl iogpu.wired_limit_mb` ile sınırı elle yükselten kullanıcıda Metal değeri yine öneri değerini verir (kapı temkinli kalır).

### 2. Yedek oran: HW_WORKING_SET_FALLBACK_RATIO = 2/3

- Claim: MLX ölçümü alınamazsa kullanılabilir bellek toplam belleğin 2/3'ü sayılır; gözlenen en düşük oran budur (büyük bellekli Mac'lerde oran daha yüksektir, yani yedek temkinli yönde yanılır).
- Evidence: https://github.com/ollama/ollama/issues/1231 (M1: `recommendedMaxWorkingSetSize = 5461.34 MB` = 8192 MB × 2/3; bellek miktarı günlükte yazmıyor, 8 GB bu eşitlikten çıkarılır); https://github.com/ggml-org/llama.cpp/issues/2522 (M1 Pro: `10922.67 MB` = 16384 MB × 2/3; bellek miktarı günlükte yazmıyor); bu Mac 36 GB → 28,1 GiB (oran 0,78); https://stencel.io/posts/apple-silicon-limitations-with-usage-on-local-llm%20.html (büyük Mac'lerde ~%75; blog, düşük güven).
- Confidence: medium
- Date checked: 2026-09-28
- What may change: Apple oranı sürümle değiştirebilir; yedek yalnız MLX çalışmadığında devreye girer ve arayüzde “yedek ölçüm” diye yazılır.

### 3. Genel kapının referans modeli: HW_REFERENCE_WEIGHTS_BYTES, HW_REFERENCE_LAYERS, HW_REFERENCE_KV_HEADS, HW_REFERENCE_HEAD_DIM

- Claim: “En az 7B'lik modeli çalıştırabilmek” ölçütünün referansı 4 bit nicemlenmiş Mistral-7B-Instruct-v0.3'tür (MLX): ağırlık 4 077 480 215 bayt; 32 katman, 8 KV başlığı, head_dim = 4096 / 32 = 128, bfloat16 → 128 KiB/token (HY-MT 7B ile aynı dikkat şekli).
- Evidence: https://huggingface.co/mlx-community/Mistral-7B-Instruct-v0.3-4bit (Hugging Face API `?blobs=true` safetensors toplamı ve `config.json`: `num_hidden_layers` 32, `num_key_value_heads` 8, `hidden_size` 4096, `num_attention_heads` 32, `quantization.bits` 4).
- Confidence: high
- Date checked: 2026-09-28
- What may change: referans başka bir 7B modele çevrilebilir (ör. HY-MT'nin 4 ya da 8 bit sürümü kalite ölçümünden sonra); yalnız bu dört sayı değişir.

### 4. KV önbelleği formülü ve öğe boyutu: HW_KV_DTYPE_BYTES = 2

- Claim: Token başına KV = katman × KV başlığı × (anahtar boyutu + değer boyutu) × öğe baytı. MLX anahtar ve değeri modelin hesap türünde tutar (bfloat16 / float16 = 2 bayt; float32 modelde 4); llama.cpp ve Ollama KV'yi varsayılan olarak f16 (2 bayt) tutar. HY-MT: 2 × 32 × 8 × 128 × 2 = 128 KiB/token.
- Evidence: https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/models/cache.py (`KVCache.update_and_fetch`: `mx.zeros(k_shape, keys.dtype)`); https://github.com/ggml-org/ggml/blob/master/docs/gguf.md (`[llm].block_count`, `[llm].attention.head_count_kv`, `key_length` / `value_length`, yoksa `n_embd / n_head`); https://github.com/abetlen/llama-cpp-python/blob/main/llama_cpp/llama.py (`type_k` / `type_v`: “default: f16”; kurulu 0.3.35'te aynı); https://github.com/ollama/ollama/blob/main/docs/faq.mdx (`OLLAMA_KV_CACHE_TYPE` varsayılanı `f16`); https://github.com/ollama/ollama/blob/main/docs/api.md (`/api/show` `model_info` anahtarları GGUF anahtarlarıyla aynı).
- Confidence: high
- Date checked: 2026-09-28
- What may change: kayan pencereli ya da karma dikkatli mimarilerde formül üst sınır verir (temkinli); sıkıştırılmış KV'li (MLA) mimarilerde şekil okunmaz, ağırlıkla orantılı yedek kullanılır; KV nicemleme açılırsa (Ollama `q8_0`) gerçek KV yarıya iner.

### 5. Ollama eşzamanlı istek: HW_OLLAMA_NUM_PARALLEL = 1

- Claim: Ollama bir modeli varsayılan olarak tek eşzamanlı istekle çalıştırır; gereken bellek `OLLAMA_NUM_PARALLEL × bağlam` ile büyür. Uygulama da istekleri sıralı gönderir (`MT_HTTP_MAX_PARALLEL = 1`).
- Evidence: https://github.com/ollama/ollama/blob/main/docs/faq.mdx (“`OLLAMA_NUM_PARALLEL` - The maximum number of parallel requests each model will process at the same time, default 1. Required RAM will scale by `OLLAMA_NUM_PARALLEL` * `OLLAMA_CONTEXT_LENGTH`.”)
- Confidence: high
- Date checked: 2026-09-28
- What may change: kullanıcı Ollama sunucusunu farklı `OLLAMA_NUM_PARALLEL` ile başlatırsa gerçek bellek artar; uygulama sunucunun ortam değişkenini göremez.

### 6. Çalışma payı: HW_WORKSPACE_FRACTION = 0,25 (ÖLÇÜLMEDİ)

- Claim: Ağırlık ve KV dışındaki bellek (etkinleşmeler, ara tamponlar, MLX önbellek tamponları) ağırlığın %25'i sayılır. Bu tek varsayım kalemidir.
- Evidence: https://blog.eleuther.ai/transformer-math/ (çıkarım için “Total Memory_Inference ≈ (1.2) × Model Memory”: ileri geçişin ek yükü ≤ %20; yazı bu payın içeriğini ayrıntılandırmıyor, KV önbelleğini ayrıca anmıyor); https://lmstudio.ai/docs/app/system-requirements (“16GB+ RAM recommended. You may still be able to use LM Studio on 8GB Macs, but stick to smaller models and modest context sizes.”); https://github.com/ollama/ollama/blob/v0.12.0/README.md (“You should have at least 8 GB of RAM available to run the 7B models”; güncel README'de bu cümle yok); https://github.com/ollama/ollama/issues/1231 (M1'de `ollama run llama2`: 3648 MB ağırlık (7B 4 bit boyutu) + 2048 MB KV + 288 MB hesap tamponu, 5461 MB çalışma kümesini aştı ve çalışmadı; günlükte bellek miktarı ve niceleme yazmıyor, 8 GB ve 4 bit bu sayılardan çıkarılır). %20 ile referans 4 bit 7B, 8 GB Mac'in çalışma kümesinin %99,5'ini dolduruyordu (sığar görünüyordu); LM Studio'nun ve bu günlüğün gösterdiği gibi 8 GB rahat değildir. %25, EleutherAI'nin %20'sinin üstündeki ve 8 GB'ı kapatan en küçük yuvarlak (%5 adımlı) değerdir.
- Confidence: low
- Date checked: 2026-09-28
- What may change: gerçek pay HY-MT yüklenerek ölçülecek; ölçüm farklıysa yalnız bu sayı değişir.

### 7. Token üst sınırları (türetilir, ayrı sayı yok)

- Claim: KV'nin token sayısı çeviri kurallarının kendi sınırlarından gelir: giriş `MT_MAX_INPUT_TOKENS`, çıktı token kuralı (`MT_OUTPUT_TOKENS_FLOOR` / `FACTOR` / `CAP`, `MT_CONTEXT_CHARS_PER_TOKEN`); toplu yola yalnız `MAX_SEGMENT_LENGTH` karakteri aşmayan parçalar girer, daha uzunları tek tek çevrilir.
- Evidence: `config/defaults.py`; `latex_pipeline/translator.py` (`max_segment_chars` üstündeki parça toplu yola girmez); https://github.com/ml-explore/mlx-lm/blob/main/mlx_lm/models/cache.py (KV parça başına ve üretildikçe büyür).
- Confidence: medium
- Date checked: 2026-09-28
- What may change: çıktı token kuralı ya da parça sınırı değişirse hesap kendiliğinden izler (aynı sabitler).

### 8. Komut satırı süresi: HW_SYSCTL_TIMEOUT_S = 2 sn

- Claim: `sysctl` alt süreci (çip adı, `hw.memsize`) en çok 2 saniye beklenir; tarama süresi (≤ 3 sn) korunur. Ölçüm süreç başına bir kez yapılır.
- Evidence: bu Mac'te `sysctl -n hw.memsize` ve `machdep.cpu.brand_string` milisaniyeler içinde döner; `python -m core.mt_engines.discovery --json` MLX ölçümüyle birlikte 0,24 sn sürdü (28 Eylül 2026).
- Confidence: high
- Date checked: 2026-09-28
- What may change: yalnız çok yavaş sistemde sınır büyütülür.

### 9. Kayan pencereli dikkat: HW_SWA_EXTRA_TOKENS = 512

- Claim: Katmanları farklı modellerde (Gemma 4) KV katman katman toplanır: tam dikkatli katman bağlam kadar, kayan pencereli (SWA) katman en çok `pencere × dizi + 512` token tutar. gemma4:31b (60 katman; 50 kayan pencereli, 16 KV başı × 256; 10 tam, 4 KV başı × 512; pencere 1024) 8192 bağlamda KV ≈ 1,9 GB; eski tekdüze MHA varsayımı 32 GB veriyordu (model "sığmaz" diye yanlışlıkla reddediliyordu).
- Evidence: https://github.com/ggml-org/llama.cpp/blob/master/src/llama-kv-cache-iswa.cpp (`size_swa = GGML_PAD(std::min(size_base, hparams.n_swa*(unified ? n_seq_max : 1) + n_ubatch), 256)`; `n_ubatch` varsayılanı 512); Ollama `/api/show` bu Mac'te gemma4:31b için (30 Eylül 2026): kısa yanıtta `gemma4.attention.head_count_kv` ve `sliding_window_pattern` null, `"verbose": true` ile 60 öğelik diziler (`[16,16,16,16,16,4,…]`, `[true×5,false,…]`); `sliding_window` 1024, `key_length_swa` 256. Model bu Mac'te (çalışma kümesi 30,1 GB) Ollama ile çalıştı (29 Eylül); yeni tahmin 26,8 GB.
- Confidence: medium (llama.cpp kaynaktan doğrulandı; Ollama'nın kendi motorundaki SWA önbellek boyu kaynaktan doğrulanmadı, aynı biçimde varsayıldı)
- Date checked: 2026-09-30
- What may change: sunucu `swa_full` (tam SWA önbelleği) açarsa kayan katmanlar da bağlam kadar tutar; Ollama ayrıntılı yanıtın biçimini değiştirirse şekil okunamaz ve ağırlıkla orantılı yedeğe düşülür (yanlış MHA'ya değil). MLX `config.json` yolu (`layer_types`) henüz katman katman okunmuyor: orada tahmin üst sınır verir.

### 10. Sunucu modelinde hüküm: LM Studio / OpenAI uyumlu sunucu

- Claim: LM Studio ve OpenAI uyumlu sunucu modelin KV önbelleği yapısını bildirmez; tahmin ağırlıkla orantılı yedektir (`KV_SOURCE_RATIO`) ve MoE / karma dikkatli (doğrusal dikkat katmanlı) modellerde gerçeğin çok üstünde çıkar. Bu yüzden bu modellerde “sığmaz” ENGEL değil gerekçeli UYARIDIR (model seçilebilir; `memory.verdict` “uyarı”, ön kontrol `ok` + `warning`); sunucunun şu an yüklü tuttuğu modele (LM Studio REST `loaded_instances` / `state: loaded`) sığma hükmü verilmez (`memory.verdict` “yüklü”). MLX / GGUF dosyası okunan modelde ve Ollama'da (şekil `/api/show`'dan okunur) engel aynen kalır; genel kapı (referans 7B) değişmez. Yeni sayı yoktur; kural `core/hardware.py` `server_verdict`'te. `~/.lmstudio` taranmaz (keşif kapsamı: yalnız `models/` + sunucular).
- Evidence: bu Mac'te (çalışma kümesi 30,1 GB) `lmstudio:qwen/qwen3.5-35b-a3b` için oran yedeği “~33,5 GB, sığmaz” verdi (29 Eylül 2026 model denemesi); aynı denemede LM Studio `lms load` ile gpt-oss-20b (11,28 GiB) ve qwen3.8-27b (16,52 GiB) yüklendi. LM Studio yüklemeden önce kendi bellek tahminini yapar ve “resource guardrails” ayarına göre yüklemeyi reddeder: https://lmstudio.ai/docs/cli/local-models/load (“This model may be loaded based on your resource guardrails settings”), https://lmstudio.ai/blog/lmstudio-v0.3.9 (“insufficient system resources error”).
- Confidence: medium (LM Studio'nun koruma davranışı kendi belgesinden; genel OpenAI uyumlu sunucuların yükleme denetimi sunucuya göre değişir)
- Date checked: 2026-09-30
- What may change: sunucu KV şeklini bildirmeye başlarsa (ör. LM Studio REST'te mimari ayrıntısı) hüküm yeniden bağlayıcı yapılabilir; LM Studio korumaları “Off” iken sığmayan model yine de yüklenip sistemi yavaşlatabilir (uyarı iletisi bu yüzden kaldırılmadı).
