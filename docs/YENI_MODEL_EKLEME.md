# Yerel Model ile Çevir: Model Kaynakları ve Yeni Model Ekleme

"Yerel model ile çevir" kitabı bu bilgisayarda çalışan bir dil modeliyle
baştan sona çevirir. Bu yol **beta** aşamasındadır: çeviri modelin gücüne
bağlıdır ve çıktı gözden geçirilmemiş bir taslaktır.

Önerilen ve varsayılan model **HY-MT 1.5 7B**'dir (Tencent; MLX, kimliği
`hymt-7b`). En çok sınanan model budur ve kalite denetiminin eşikleri onun
çıktısına göre ayarlıdır; yerel model kullanacaksanız onu seçmenizi öneririz.
DMG kurulumunda Bileşenler ekranından, kaynak koddan kurulumda kurulum
rehberindeki komutla kurulur (`docs/KURULUM_VE_KULLANIM.md`, Bölüm 2.3 ve
2.6). Onun yerine dört kaynaktan bir model seçilebilir; bu kaynaklar
desteklenir ama **beta düzeyindedir ve daha az sınanmıştır**:

1. **MLX klasörü:** `models/` klasöründeki MLX biçimli model; mlx-lm ile
   uygulamanın kendi sürecinde çalışır.
2. **Ollama:** bu bilgisayarda açık olan Ollama sunucusundaki model.
3. **LM Studio:** LM Studio'nun yerel sunucusundaki model. Aynı yolla
   llama.cpp sunucusu, Jan, vLLM gibi OpenAI uyumlu yerel sunucular da
   kullanılabilir.
4. **GGUF dosyası:** `models/` klasöründeki `.gguf` dosyası; ya llama-cpp-python
   ile doğrudan ya da Ollama / LM Studio üzerinden.

Model seçmek için kod yazmak ya da ayar dosyası hazırlamak gerekmez:
arayüzdeki **Model** listesi ve **Modelleri tara** düğmesi yeterlidir. Komut
satırında aynı seçim `--mt-engine <kimlik>` ile yapılır (Bölüm 4). Ayar
dosyaları yalnızca nadir durumlar içindir (Bölüm 5).

> **Klasörlerin yeri:** Bu belgede `models/` ve `config/` dendiğinde kaynak
> koddan kurulumda proje klasöründeki klasörler anlaşılır. DMG kurulumunda
> bunlar kullanıcı verisinin kökündedir:
> `~/Library/Application Support/Kitapçevir/veri/models/` ve
> `~/Library/Application Support/Kitapçevir/veri/config/` (kurulum rehberi,
> Bölüm 2.5).

## Değişmeyen kurallar

| Kural | Açıklama |
|---|---|
| Yalnız bu bilgisayar | Sunucu adresi yalnız `127.0.0.1`, `localhost` ya da `::1` olabilir. Yerel ağdaki ya da internetteki bir sunucuya bağlanılmaz, API anahtarı desteği yoktur ve sistemin proxy ayarı bu isteklerde kullanılmaz. |
| Kaynak dil | "Yerel model ile çevir" yolunun kaynak dilleri: İngilizce, Almanca, Rusça, Fransızca, İspanyolca, İtalyanca. Bu liste bütün modellerde (HY-MT 1.5 7B dahil) aynıdır. İngilizceden başka kaynak dillerde çeviri kalitesi seçilen modele bağlıdır; HY-MT 1.5 7B ile bu dillerden yalnız kısa Almanca ve Rusça deneme belgeleri sınandı. |
| Dil kapıları | Kitabın dili her modelde aynı iki kapıda ölçülür ve ölçülen dil çeviriye aktarılır (modele verilen talimat, çevrilmeden kalan satırın denetimi). Dili desteklenmeyen kitap çıkış kodu 4 ile durur. Tespit yanlışsa dil kartın **Kaynak dil** listesinden, komut satırında `--kaynak-dil` ile verilir. |
| Hedef dil yalnız Türkçe | Hedef dil seçeneği yoktur; her model Türkçeye çevirir. |
| Taranan tek klasör `models/` | MLX klasörleri ve GGUF dosyaları yalnız `models/` klasöründe aranır. LM Studio'nun ya da Hugging Face'in indirme klasörleri taranmaz; başka yerdeki bir model `models/` altına sembolik bağla konur (Bölüm 3.5). |
| Çeviri belleği model başına ayrı | Her model yalnız kendi önceki çevirilerini bellekten kullanır; bir modelin kayıtları başka modelde görünmez. HY-MT 1.5 7B'nin mevcut kayıtları olduğu gibi korunur. |
| Düşünen modeller | Düşünme resmî yolla kapatılabiliyorsa kapatılır; kapatılamıyorsa model düşünerek çevirir (daha yavaştır); model cevap üretemezse çeviri açık bir iletiyle durur (Bölüm 6). |
| Platform | Uygulama yine yalnızca macOS + Apple Silicon (M serisi) bir Mac'te çalışır. |

## 1. Kaynaklar bir bakışta

| Kaynak | Hazırlık | Kimlik örneği | Çeviri nerede çalışır |
|---|---|---|---|
| Yerleşik HY-MT 1.5 7B (önerilen) | DMG: Bileşenler ekranı; kaynak koddan: `models/hymt-7b-mlx` (kurulum rehberi, Bölüm 2.6) | `hymt-7b` | Uygulamanın süreci, mlx-lm |
| MLX klasörü | `models/` altında MLX klasörü | `mlx:qwen3-8b-mlx` | Uygulamanın süreci, mlx-lm |
| Ollama | Ollama kurulu, model indirilmiş, sunucu açık | `ollama:qwen3:8b` | Ollama sunucusu (bu bilgisayarda) |
| LM Studio | Model LM Studio'da indirilmiş, yerel sunucu açık | `lmstudio:qwen/qwen3-8b` | LM Studio sunucusu (bu bilgisayarda) |
| GGUF dosyası | `models/` altında `.gguf` + llama-cpp-python | `gguf:Qwen3-8B-Q4_K_M.gguf` | Uygulamanın süreci, llama-cpp-python |
| Başka bir OpenAI uyumlu sunucu | `config/mt_sources.json` kaydı (Bölüm 5.1) | `openai:http://127.0.0.1:8080/v1\|qwen` | O sunucu (llama.cpp sunucusu, Jan, vLLM) |

Bir GGUF dosyası Ollama ya da LM Studio'ya da yüklenebilir; o zaman kimlik
`ollama:` ya da `lmstudio:` ile başlar (Bölüm 3.4).

Kitapçevir Ollama'yı ve LM Studio'yu bu programların kendi varsayılan
adreslerinde arar. Bu adresler tek yerde, `config/defaults.py` dosyasında
tanımlıdır (`OLLAMA_DEFAULT_URL`, `LMSTUDIO_DEFAULT_URL`); programların
portunu değiştirmediyseniz hiçbir ayar gerekmez. Taramanın baktığı adresler
`python -m core.mt_engines.discovery --json` çıktısında her kaynağın
`location` alanında görünür.

## 2. Arayüzde model seçmek

Her kaynakta akış aynıdır: **hazırla → Modelleri tara → seç → Çevir**.

1. **Hazırla:** Modeli Bölüm 3'e göre hazırlayın: klasörü ya da dosyayı
   `models/` altına koyun ya da Ollama / LM Studio sunucusunu açın.
2. **Modelleri tara:** **Yerel model ile çevir** kartındaki düğmeye basın.
   Arayüz `models/` klasörünü ve açık Ollama / LM Studio sunucularını tarar.
   İlk açılışta tarama bir kez kendiliğinden yapılır; sonra yalnızca bu
   düğmeyle yenilenir. Kartta son taramanın saati yazar.
3. **Seç:** **Model** listesinden modeli seçin. Her seçenekte modelin adı ve
   kaynağı, düşünme durumu, biliniyorsa ayrıntısı (parametre sayısı, niceleme,
   mimari) ve boyutu yazar; HY-MT 1.5 7B "önerilen" diye işaretlidir. Seçim
   hatırlanır.
4. **Çevir:** Düğmeye basınca model önce birkaç saniyelik son bir denetimden
   geçer. Sunucu kapandıysa ya da model artık yoksa iş başlamaz ve nedeni
   yazılır.

Bilmeniz gerekenler:

- Listede yalnızca taramada gerçekten bulunan modeller görünür. Sunucusu
  kapalı bir kaynaktan model listelenmez; onun yerine kartta o kaynağın
  durumu yazar (ör. "Ollama sunucusu kapalı ya da erişilemiyor (…)").
  Yerleşik HY-MT 1.5 7B listede her zaman bulunur.
- Kullanılamayan bir model (mimarisi tanınmayan MLX klasörü, llama-cpp-python
  kurulu değilken GGUF dosyası, gömme modeli vb.) listede "kullanılamaz" diye
  görünür. Böyle bir model seçilirse **Çevir** soluk kalır ve nedeni altında
  yazar.
- Kart seçili modelin düşünme durumunu yazar (Bölüm 6).
- Son seçilen model bir sonraki taramada bulunamazsa varsayılan model seçilir
  ve bu durum kartta yazar.
- Kenar çubuğundaki "Sistem" bölümü seçili modeli ve son taramaya göre hazır
  olup olmadığını gösterir.
- İş sürerken model listesi ve **Modelleri tara** kapalıdır; ilerleme
  panelinde çeviriyi yapan model yazar.
- Model çeviri sırasında kullanılamaz hâle gelirse (ör. sunucu kapandı) iş
  **çıkış kodu 5** ile biter ve arayüz "Seçili model kullanılamadı; nedeni
  aşağıda." iletisini gösterir. Sunucuyu yeniden açın, **Modelleri tara**'ya
  basın ve çeviriyi yeniden başlatın.

## 3. Kaynakları hazırlamak

### 3.1. MLX klasörü

MLX klasörü, Apple Silicon için dönüştürülmüş bir modeldir. Klasörde
`config.json`, en az bir `*.safetensors` dosyası ve tokenizer dosyaları
(`tokenizer.json`, `tokenizer.model` ya da `tokenizer_config.json`)
bulunmalıdır. Hugging Face'teki bir modeli dönüştürmek için kaynak koddan
kurulumda proje klasöründe:

```bash
./venv/bin/mlx_lm.convert --hf-path Qwen/Qwen3-8B --mlx-path models/qwen3-8b-mlx
```

DMG kurulumunda aynı araç uygulamanın sanal ortamındadır
(`~/Library/Application Support/Kitapçevir/venv/bin/mlx_lm.convert`); hedef
klasör `~/Library/Application Support/Kitapçevir/veri/models/<ad>` olur.
Hazır dönüştürülmüş MLX modelleri (ör. Hugging Face'teki `mlx-community`
depoları) doğrudan `models/` altına indirilebilir.

- Kimlik: `mlx:<klasör adı>`, örneğin `mlx:qwen3-8b-mlx`.
- `models/<klasör>` yoksa `models/<klasör>.nosync` da denenir (iCloud
  eşitlemesinden kaçınma deseni).
- Klasör adında "HY-MT" geçiyorsa HY-MT'nin özel istem biçimi, geçmiyorsa
  modelin kendi sohbet şablonu kullanılır. Adında "HY-MT" geçmeyen, HY-MT'den
  türetilmiş bir model için biçim bir kayıtla belirtilir (Bölüm 5.2).
- Kurulu mlx-lm sürümünün tanımadığı mimariler listede "kullanılamaz"
  görünür (ör. mlx-lm 0.30.7 `gemma4`'ü tanımıyor). Böyle bir model LM Studio
  üzerinden kullanılabilir.
- İçinde `config.json` olmayan klasörler çeviri modeli değildir,
  listelenmez.

### 3.2. Ollama

1. Ollama'yı kurun (https://ollama.com) ve bir model indirin:

   ```bash
   ollama pull qwen3:8b
   ```

2. Ollama uygulamasını açın ya da terminalde `ollama serve` çalıştırın.
3. **Modelleri tara** → model listede "Ollama" kaynağıyla görünür.

- Kimlik: `ollama:<model>`. İlk `:` işaretinden sonrası olduğu gibi model
  adıdır: `ollama:qwen3:8b`, `ollama:hf.co/<kullanıcı>/<depo>:Q4_K_M`. Etiketsiz
  ad `:latest` ile aynıdır (`ollama:qwen3` ile `ollama:qwen3:latest` aynı
  modeldir).
- Kitapçevir Ollama'nın kendi API'sini kullanır, OpenAI uyumlu `/v1` ucunu
  değil: bağlam uzunluğu (`num_ctx`) yalnızca bu API ile ayarlanabilir; kısa
  bağlamda uzun paragraflar sessizce kırpılırdı. Bağlam en az 8192 token'a
  (model daha kısa bağlamlıysa modelin sınırına) ayarlanır ve koşu boyunca
  değişmez (değişirse Ollama modeli yeniden yükler). Bağlama sığmayacak bir
  paragraf modele hiç gönderilmez: özgün hâliyle kalır ve kalite raporunda
  görünür.
- Model çeviri boyunca bellekte tutulur (`keep_alive`: `30m`).
- Gömme (embedding) modelleri listede "kullanılamaz" görünür.
- Ollama'yı başka bir portta açtıysanız (ör. `OLLAMA_HOST` ortam
  değişkeniyle) adresi `config/mt_sources.json`'a yazın (Bölüm 5.1).

### 3.3. LM Studio ve diğer OpenAI uyumlu yerel sunucular

1. LM Studio'da bir model indirin (GGUF ya da MLX biçiminde).
2. LM Studio'da Developer bölümünden yerel sunucuyu başlatın ya da terminalde
   `lms server start` çalıştırın.
3. Modeli LM Studio'da yükleyin ya da sunucu ayarlarında isteğe bağlı
   yüklemeyi (Just-in-Time) açın. İsteğe bağlı yükleme kapalıyken LM Studio
   yalnızca yüklü modelleri listeler.
4. **Modelleri tara** → model listede "LM Studio" kaynağıyla görünür.

- Kimlik: `lmstudio:<model>`, LM Studio'nun gösterdiği model kimliğiyle;
  örneğin `lmstudio:qwen/qwen3-8b`.
- İlk istekte LM Studio modeli belleğe yükler. Büyük bir modelde bu birkaç
  dakika sürebilir; en çok 600 sn beklenir.
- Model LM Studio'da yüklüyse, yüklendiği bağlam uzunluğu (Context Length)
  okunur ve bellek tahmininde ve bağlam sınırında o kullanılır.
- Gömme modelleri listede "kullanılamaz" görünür.
- LM Studio sunucusunun portunu değiştirdiyseniz adresi
  `config/mt_sources.json`'a yazın (Bölüm 5.1).

**Diğer OpenAI uyumlu sunucular** (llama.cpp sunucusu `llama-server`, Jan,
vLLM) kendiliğinden yoklanmaz; ilgisiz bir geliştirme sunucusuna istek
gitmesin diye adresleri `config/mt_sources.json`'daki `openai_servers`
listesine yazılır (Bölüm 5.1). Örnek, 8080 portunda llama.cpp sunucusu:

```bash
llama-server -m models/Qwen3-8B-Q4_K_M.gguf --port 8080 --alias qwen
```

Bu sunucudaki modelin kimliği:

```
openai:http://127.0.0.1:8080/v1|qwen
```

Adres `:` içerdiği için adres ile model adı `|` ile ayrılır.

### 3.4. GGUF dosyası

GGUF, llama.cpp ailesinin tek dosyalık model biçimidir. Üç yolla
kullanılabilir.

**a) Doğrudan, llama-cpp-python ile.** Dosyayı `models/` altına koyun (ya da
sembolik bağla bağlayın, Bölüm 3.5) ve **Modelleri tara**'ya basın. Kimlik:
`gguf:<dosya adı>`, örneğin `gguf:Qwen3-8B-Q4_K_M.gguf`.

llama-cpp-python **isteğe bağlı** bir bağımlılıktır. DMG kurulumunda
Bileşenler ekranındaki **GGUF desteği** onu kurar. Kaynak koddan kurulumda
`requirements.txt` onu kurmaz. Kurulu değilse uygulama yine açılır, GGUF
dosyaları listede "kullanılamaz" görünür ve kurulum komutu yazılır. Kaynak
koddan kurulumda Apple Silicon'da Metal (GPU) desteğiyle kurmak için proje
klasöründe:

```bash
CMAKE_ARGS="-DGGML_METAL=on" ./venv/bin/pip install llama-cpp-python
```

Derleme birkaç dakika sürebilir.

- Modelin bütün katmanları GPU'ya alınır (`n_gpu_layers`: `-1`); bağlam
  varsayılan olarak 8192 token'dır (`n_ctx`).
- `mmproj-*.gguf` gibi görsel projektör dosyaları model değildir,
  listelenmez.
- Parçalı dosyalarda (`…-00001-of-00003.gguf`) yalnızca ilk parça listelenir;
  boyut parçaların toplamıdır. Bütün parçalar aynı klasörde durmalıdır.
- Kurulu llama-cpp-python'ın tanımadığı yeni bir mimari ancak yükleme
  sırasında anlaşılır ("GGUF modeli llama-cpp-python ile yüklenemedi …").
  Böyle bir model Ollama ya da LM Studio üzerinden denenebilir.

**b) Ollama üzerinden.** Ollama GGUF dosyasını içe aktarabilir: bir
`Modelfile` dosyasına `FROM ./Qwen3-8B-Q4_K_M.gguf` yazıp
`ollama create qwen3-yerel -f Modelfile` çalıştırın. Hugging Face'teki bir GGUF
deposu doğrudan da indirilebilir: `ollama pull hf.co/<kullanıcı>/<depo>:Q4_K_M`.
Kimlik `ollama:qwen3-yerel` (ya da indirilen adla `ollama:hf.co/…`) olur.

**c) LM Studio üzerinden.** LM Studio'nun indirdiği GGUF modelleri onun
sunucusunda çalışır; kimlik `lmstudio:<model>` olur (Bölüm 3.3).

### 3.5. Başka bir klasördeki modeli kullanmak: sembolik bağ

Kitapçevir yalnızca `models/` klasörünü tarar. LM Studio'nun indirdiği bir
MLX klasörünü ya da başka bir diskteki GGUF dosyasını kopyalamadan kullanmak
için `models/` altına sembolik bağ koyun (kaynak koddan kurulumda proje
klasöründe):

```bash
# MLX klasörü
ln -s ~/.lmstudio/models/<yayıncı>/<Model-MLX> models/<Model-MLX>
# GGUF dosyası
ln -s ~/.lmstudio/models/<yayıncı>/<Model-GGUF>/<dosya>.gguf models/<dosya>.gguf
```

DMG kurulumunda bağın hedef klasörü kullanıcı verisinin kökündedir:

```bash
ln -s ~/.lmstudio/models/<yayıncı>/<Model-MLX> "$HOME/Library/Application Support/Kitapçevir/veri/models/<Model-MLX>"
```

Bağ `models/` içinde durduğu sürece model taramada görünür ve bağın adıyla
seçilir (`mlx:<Model-MLX>`, `gguf:<dosya>.gguf`). Asıl dosya taşınır ya da
silinirse bağ kırılır; model listeden düşer. Tarama klasörü ayar dosyasıyla
eklenemez.

## 4. Komut satırı

Komut satırı kaynak koddan kurulumda kullanılır (sanal ortam etkinken):

```bash
python translate_book.py kitap.pdf --mt-engine ollama:qwen3:8b
python translate_book.py kitap.pdf --mt-engine lmstudio:qwen/qwen3-8b
python translate_book.py kitap.pdf --mt-engine mlx:qwen3-8b-mlx
python translate_book.py kitap.pdf --mt-engine gguf:Qwen3-8B-Q4_K_M.gguf
python translate_book.py kitap.pdf --mt-engine 'openai:http://127.0.0.1:8080/v1|qwen'
```

`|` kabukta boru işaretidir; `openai:` kimliğini tek tırnak içinde yazın.
`--mt-engine` verilmezse varsayılan `hymt-7b` kullanılır; komut satırı
arayüzün son seçimini okumaz.

Taramayı terminalde görmek için:

```bash
python -m core.mt_engines.discovery          # Türkçe tablo
python -m core.mt_engines.discovery --json   # makinece okunur çıktı
```

Tarama hiçbir modeli yüklemez; bir sunucunun yanıtı en çok 1,5 sn beklenir.
JSON çıktısında, değerlendirilebildiyse modelin düşünme durumu da `thinking`
alanında yazar (Bölüm 6).

### 4.1. Kimlik biçimleri

| Kimlik | Anlamı |
|---|---|
| `hymt-7b` | Yerleşik HY-MT 1.5 7B (varsayılan, önerilen) |
| `<ad>` | `config/mt_engines.json`'daki kayıt (ad `:` içeremez) |
| `mlx:<klasör>` | `models/<klasör>` (ya da `models/<klasör>.nosync`), mlx-lm ile |
| `gguf:<dosya>.gguf` | `models/<dosya>.gguf`, llama-cpp-python ile |
| `ollama:<model>` | Ollama modeli; ilk `:` işaretinden sonrası model adıdır |
| `lmstudio:<model>` | LM Studio modeli; adres `config/mt_sources.json`'dan, yoksa varsayılan |
| `openai:<adres>\|<model>` | OpenAI uyumlu yerel sunucu; adres yalnızca bu bilgisayarı gösterebilir |

Klasör ve dosya adında üst klasöre çıkan yollar (`..`) kabul edilmez; mutlak
yol yalnızca `models/` klasörünün içini gösteriyorsa kabul edilir. Model adı
boşluk ya da denetim karakteri içeremez ve en çok 200 karakterdir.

### 4.2. Çıkış kodu 5

`translate_book.py` seçili model kullanılamıyorsa **çıkış kodu 5** ile durur:
kimlik geçersiz ya da model bilinmiyor, klasör ya da dosya yok, sunucu kapalı,
model sunucuda yok, llama-cpp-python kurulu değil, çeviri sırasında sunucu
bağlantısı koptu ya da düşünen model parçaların çoğunda cevap üretemedi
(Bölüm 6). Denetim çıkarımdan **önce** yapılır: sunucu kapalıysa iş uzun PDF
çıkarımını beklemeden birkaç saniyede durur. Terminale `HATA:` satırıyla
nedeni, varsa `İpucu:` satırıyla ne yapılacağı yazılır. Kaynak dil kapısı
(çıkış kodu 4) önce gelir: dili desteklenmeyen bir kitapta model değiştirmek
çözüm değildir.

## 5. Ayar dosyaları (isteğe bağlı)

Çoğu kullanımda hiçbir ayar dosyası gerekmez: tarama `models/` klasörünü ve
varsayılan adreslerdeki sunucuları kendiliğinden bulur. Aşağıdaki üç dosya
`config/` altındadır (DMG kurulumunda `veri/config/` altında), isteğe
bağlıdır ve git dışıdır (makineye özgü adres ve adlar içerir). Örnek dosya
yoktur: gereken durumda dosyayı aşağıdaki kısa örneklere bakarak kendiniz
oluşturun (UTF-8, JSON). `_` ile başlayan anahtarlar yorum sayılır ve atlanır.

| Dosya | Ne tutar | Ne zaman gerekir |
|---|---|---|
| `config/mt_selection.json` | Arayüzde son seçilen model | Hiçbir zaman elle yazılmaz; arayüz kendiliğinden yazar |
| `config/mt_sources.json` | Yerel sunucu adresleri | Sunucu varsayılandan farklı bir portta çalışıyorsa ya da llama.cpp sunucusu gibi ek bir sunucu kullanılacaksa |
| `config/mt_engines.json` | Adlı model kayıtları ve ince ayarlar | Modele kısa bir ad vermek, istem biçimini ya da bağlam uzunluğu gibi bir ayarı sabitlemek gerekiyorsa |

Bozuk ya da eksik bir ayar dosyası çeviriyi durdurmaz: günlüğe uyarı yazılır
ve o alan için varsayılan kullanılır (bozuk kayıt atlanır).

### 5.1. `config/mt_sources.json`: farklı port ya da ek sunucu

Dosya yoksa Ollama ve LM Studio varsayılan adreslerinde aranır
(`config/defaults.py`: `OLLAMA_DEFAULT_URL`, `LMSTUDIO_DEFAULT_URL`). Dosyaya
yalnızca değiştirdiğiniz alanları yazın; yazmadıklarınız varsayılan kalır.
Örnek: Ollama 11500 portunda, LM Studio kullanılmıyor, 8080 portunda bir
llama.cpp sunucusu var:

```json
{
  "ollama": {"url": "http://127.0.0.1:11500"},
  "lmstudio": {"enabled": false},
  "openai_servers": [{"name": "llama.cpp", "url": "http://127.0.0.1:8080/v1"}]
}
```

| Alan | Açıklama |
|---|---|
| `ollama.url`, `lmstudio.url` | Sunucu adresi (LM Studio'da `/v1` ile biter) |
| `ollama.enabled`, `lmstudio.enabled` | `false` ise o sunucu hiç yoklanmaz |
| `openai_servers` | Ek OpenAI uyumlu sunucular: `name` listede görünen ad, `url` `/v1` ile biten taban adres |

- Adresler yalnızca bu bilgisayarı gösterebilir (`127.0.0.1`, `localhost`,
  `::1`). Başka bir adres günlüğe uyarı yazılarak reddedilir: Ollama ve
  LM Studio için varsayılan adres kullanılır, ek sunucu atlanır.
- Bu dosyaya model klasörü eklenemez; taranan tek klasör `models/`'dır
  (Bölüm 3.5).

### 5.2. `config/mt_engines.json`: adlı model kayıtları

Kimlik (Bölüm 4.1) çoğu durumda yeter. Şu durumlarda kayıt kullanılır:
modele kısa bir ad vermek; istem biçimini, bağlam uzunluğu gibi bir ayarı ya
da düşünme ayarını sabitlemek. Kayıtlı modeller listede "kayıtlı" kaynağıyla
görünür ve komut satırında `--mt-engine <ad>` ile seçilir. Örnek: adında
"HY-MT" geçmeyen, HY-MT'den türetilmiş bir MLX modeli ve bağlamı büyütülmüş
bir Ollama modeli:

```json
{
  "benim-mt": {"local_dir": "benim-mt-7b-mlx", "format": "hymt",
               "description": "HY-MT'den türetilmiş model (MLX)"},
  "qwen3-uzun": {"backend": "ollama", "model": "qwen3:8b",
                 "options": {"num_ctx": 16384}, "description": "Qwen3 8B, uzun bağlam"}
}
```

Alanlar:

| Alan | Arka uç | Açıklama |
|---|---|---|
| `backend` | hepsi | `mlx` (varsayılan), `ollama`, `openai` (LM Studio ve diğer OpenAI uyumlu sunucular), `llamacpp` (doğrudan GGUF) |
| `format` | hepsi | `chat` (varsayılan): modelin kendi sohbet şablonu; `hymt`: HY-MT'nin özel istem biçimi |
| `description` | hepsi | Listede görünen açıklama |
| `local_dir` | `mlx` | `models/` altındaki klasör adı (yalnızca ad, yol değil) |
| `path` | `mlx`, `llamacpp` | `models/` altındaki klasörün ya da `.gguf` dosyasının adı |
| `model` | `ollama`, `openai` | Sunucudaki model adı (zorunlu) |
| `base_url` | `ollama`, `openai` | Sunucu adresi; yalnızca bu bilgisayar. Ollama'da verilmezse `config/mt_sources.json`'daki ya da varsayılan adres kullanılır |
| `flavor` | `openai` | `lmstudio`: LM Studio sunucusu; `base_url` verilmezse LM Studio'nun adresi kullanılır |
| `options` | `ollama`, `openai`, `llamacpp` | Üretim ayarları (aşağıdaki tablo) |
| `extra_body` | `openai` | İstek gövdesine olduğu gibi eklenen alanlar |
| `think` | `ollama` | `false` (varsayılan): düşünmeyi kapatmayı dener; `null`: dokunmaz |
| `chat_template_kwargs` | `mlx`, `openai` | Sohbet şablonuna geçen değişkenler; varsayılan `{"enable_thinking": false}` |
| `system_prompt` | `chat` biçimi | `true`: çeviri talimatı ayrı bir sistem mesajında gider (varsayılan: tek kullanıcı mesajı) |
| `raw` | `ollama`, `openai` | `format: hymt` ile birlikte `true`: ham tamamlama ucu kullanılır (deneysel) |
| `model_id` | `mlx` | Hugging Face kimliği (dönüştürme komutu ipucu için) |
| `strip_tokens` | hepsi | Çıktıdan silinecek ek özel dizgiler (ör. `<\|im_end\|>`) |
| `languages` | hepsi | Çeviri talimatındaki dil adları (dil kodu → ad, ör. `{"en": "English"}`); verilmezse yerleşik tablo kullanılır, çoğu zaman gerekmez |

`options` anahtarları (MLX modelleri bugünkü ayarlarla çalışır, bu anahtarları
okumaz):

| Anahtar | Arka uç | Varsayılan | Açıklama |
|---|---|---|---|
| `num_ctx` | `ollama` | 8192 | Bağlam uzunluğu (token); 8192'den küçük değer 8192'ye yükseltilir, modelin kendi sınırı daha kısaysa ona indirilir; koşu boyunca sabit kalır |
| `keep_alive` | `ollama` | `30m` | Modelin istekler arasında bellekte kalma süresi |
| `n_ctx` | `llamacpp` | 8192 | Bağlam uzunluğu (token) |
| `n_gpu_layers` | `llamacpp` | -1 | GPU'ya alınan katman sayısı (-1: hepsi) |
| `max_parallel` | `ollama`, `openai` | 1 | Toplu çeviride aynı anda gönderilen istek sayısı (1: sırayla), en çok 8. Sunucuda paralel yuva yoksa istekler orada sıraya girer |
| `temperature` | `ollama`, `openai`, `llamacpp` | 0 | Örnekleme sıcaklığı (0: aynı girdiye hep aynı çıktı) |
| `repeat_penalty` | `ollama`, `openai`, `llamacpp` | 1.0 | Tekrar cezası (1.0: ceza yok) |
| `seed` | `ollama`, `openai`, `llamacpp` | 0 | Rastgelelik tohumu |
| `request_timeout_s` | `ollama`, `openai`, `llamacpp` | 240 | Tek bir paragrafın en uzun üretim süresi (sn). Pipeline bir paragrafı en çok 300 sn beklediğinden bundan büyük değer işe yaramaz |
| `think_budget` | hepsi | 512 / 4096 | Düşünen modelde paragraf başına düşünme sınırı (token). Düşünmesi kapalı sayılan modelde kapatmanın tutup tutmadığını yoklayan pay (varsayılan 512), düşünerek çeviride paragraf payının tavanı (varsayılan 4096) olarak kullanılır (Bölüm 6). MLX modellerinin okuduğu tek anahtardır |

Kayıt kuralları:

- Ad yalnızca harf, rakam, `.`, `_` ve `-` içerebilir (en çok 64 karakter);
  `:` içeremez, böylece önekli kimliklerle karışmaz.
- Yerleşik `hymt-7b` adı kullanıcı kaydıyla ezilemez.
- Bozuk ya da eksik bir kayıt çeviriyi durdurmaz: günlüğe uyarı yazılır ve
  kayıt atlanır. Model yükleme hatası ise her zaman açık bir hatadır.

## 6. Düşünen modeller

Qwen3, gpt-oss, DeepSeek-R1 gibi modeller yanıttan önce "düşünür". Kural
şudur: **düşünme resmî yolla kapatılabiliyorsa kapatılır; kapatılamıyorsa
model düşünerek çevirir.** HY-MT 1.5 7B düşünmeyen bir modeldir; bu bölüm onun
çevirisini değiştirmez.

**Düşünme durumu.** Tarama, kaynağın bildirdiğinden her modelin düşünme
durumunu çıkarır: düşünmüyor (`yok`), düşünmesi kapatılabiliyor
(`kapatilabilir`), düşünmesi kapatılamıyor (`kapatilamaz`) ya da kaynak bilgi
vermiyor (`bilinmiyor`; genel OpenAI uyumlu sunucularda her zaman böyledir).
Model listesi ve kart bu durumu yazar: "düşünme yok", "düşünme kapalı
çalışır", "düşünen model — düşünerek çevirir, daha yavaştır" ya da "düşünme
durumu bilinmiyor". Düşünmesi kapatılamayan modelde kartta "Düşünerek çeviri:
model her parçada önce düşünür, sonra çevirir; daha yavaştır" notu da çıkar.
Durumu bilinmeyen ama düşündüğü bilinen model için kartta "düşünen model;
yavaş olabilir" notu görünür.

**Kapatma.** Yalnızca resmî belgede yazan yollar kullanılır:

- LM Studio'da model düşünme ayarında "off" seçeneği bildiriyorsa istek
  LM Studio'nun yerel REST ucuna düşünme kapalı olarak gider.
- Ollama'da model düşünme yeteneği bildiriyorsa isteğe `"think": false`
  eklenir.
- MLX modellerinde ve OpenAI uyumlu sunucularda sohbet şablonuna
  `{"enable_thinking": false}` geçer (şablon bu değişkeni tanımıyorsa yok
  sayılır).
- Kapatma yolu `/no_think` anahtarı olan Qwen3 modellerinde (doğrudan GGUF ya
  da şablonunda kapatma değişkeni olmayan MLX) anahtar ilk istekten eklenir.

**Düşünme sınırı.** Kapatma her modelde tutmaz; bu yüzden her yanıtta düşünme
ölçülür (ayrı düşünme alanı ya da yanıttaki `<think>…</think>` bloğu).
Sohbet şablonu düşünmeyi istemin içinde açan modellerde (ör. DeepSeek-R1
damıtmaları, Qwen3 "Thinking" sürümleri; istem `<think>` ile biter) yanıtın
başı `</think>`'e kadar düşünme sayılır; kapanmayan düşünme hiçbir zaman
çeviri sayılmaz. Düşünmesi kapalı sayılan modelde paragraf başına 512 token'lık
bir yoklama payı vardır: yanıt parça parça okunur ve düşünme bu payı aşınca
istek hemen kesilir. Kapatma isteğine rağmen düşünce geldiyse model o çeviri
boyunca "düşünmesi kapatılamıyor" sayılır (günlüğe bir uyarı satırı yazılır)
ve düşünerek çeviriye geçilir; kesilen paragraf bu kipte bir kez daha
denenir. Yeniden denemede kaynağın daha sert kapatma yolu varsa o kullanılır
(Qwen3 modellerinde isteğe `/no_think` anahtarı eklenir; LM Studio dışındaki
OpenAI uyumlu sunucularda düşünme istek düzeyinde kapatılır); bu yol düşünmeyi
gerçekten kapattıysa çevirinin kalanında da kullanılır.

**Düşünerek çeviri.** Düşünmesi kapatılamayan modelde her paragrafa ayrı bir
düşünme payı ayrılır: varsayılan olarak 1024 token ile paragrafın karakter
sayısı kadar token'ın toplamı, en çok 4096 token. İsteğin token sınırı bu pay
ile paragrafın çeviri sınırının toplamıdır; düşünce ayıklanır, çeviriye
karışmaz. Süre sınırı paya ve modelin bu koşuda ölçülen hızına göre
hesaplanır (en çok paragrafın üretim süresi kadar). Bellek tahmininde de
düşünme payı hesaba katılır; bellek darsa aynı anda çevrilen paragraf sayısı
küçülür. Her paragraf için `pipeline.log` dosyasına düşünmenin gerçek token
sayısı ve süresi yazılır. İş daha yavaş sürer:

1. Pay dolup cevap gelmezse paragraf bir kez yeniden denenir: kaynağın resmî
   daha kısa düşünme yolu varsa onunla (Ollama'da ve LM Studio'da düşük
   düşünme düzeyi, llama.cpp sunucusunda düşünmesiz istek, Qwen3'te
   `/no_think`), yoksa aynı istekle.
2. Yine cevap yoksa paragraf özgün dilinde kalır ve "çevrilmedi" olarak
   işaretlenir; kalite denetimi bu satırları raporlar. Çeviri sürer, boş çıktı
   hiçbir zaman çeviri sayılmaz.
3. Model paragrafların çoğunda cevap üretemiyorsa (son 10 düşünmeli
   paragraftan, en az 5 örnekle, %60'ı ya da daha fazlası cevapsızsa; başlık
   gibi kısa paragraflar ayrı sayılır) çeviri durur, iş çıkış kodu 5 ile biter
   ve "Bu model düşünmesini bitirip çeviri üretemiyor: …" iletisi önerilen
   modelin adıyla gösterilir. Başka bir model seçin.

Düşünme payının tavanını bir model için değiştirmek (ör. düşünmeyi
kapatamayan bir modelin paragraf başına düşünmesini sınırlamak) `config/mt_engines.json` kaydıyla yapılır; `think_budget` hem yoklama payını hem de düşünerek
çevirideki tavanı belirler:

```json
{
  "qwen3-8b-think": {"backend": "ollama", "model": "qwen3:8b",
                     "options": {"think_budget": 1024},
                     "description": "Qwen3 8B, düşünme payı 1024 token"}
}
```

Düşünmeyi açık bırakmak için kayıtta `"think": null` ve
`"chat_template_kwargs": {}` kullanılır: model kapatma denenmeden düşünerek
çevirir. Çeviri için önerilmez.

**Üretim sınırı.** Bir paragraf için üretilecek en çok token, paragrafın
uzunluğuna göre belirlenir (kısa paragrafta 512, uzun paragrafta orantılı
olarak daha çok, en çok 4096); bütün model kaynakları aynı kuralı kullanır.
Çeviri bu sınıra dayanırsa kesilmiş olabilir: çeviri kabul edilir ama sessiz
geçmez; `pipeline.log` dosyasına o paragraf için "Çıktı üretim sınırına
dayandı" uyarısı, çeviri sonunda da toplam sayı yazılır.

## 7. Kalite, bellek ve hız

- **Genel sohbet modelleri çevirmen değildir:** metindeki bir soruyu
  yanıtlayabilir, açıklama ekleyebilir, özetleyebilir. Kitapçevir yaygın
  kalıpları temizler ("Here is the translation:" ya da "İşte çeviri:" gibi
  önsözler, çıktıyı tırnağa ya da kod çitine sarma, sona eklenen not,
  `<think>` blokları); kalanını kalite denetimi yakalar.
- **Kalite denetiminin eşikleri HY-MT 1.5 7B çıktısına göre ayarlıdır.** Yeni
  bir modelle çevrilen ilk kitapta `qc_report.txt` dosyasını gözden geçirin.
- **Korunan öğeler:** formül, atıf gibi öğeler modele `<<LATEX_3>>` gibi yer
  tutucularla gider. Model bir yer tutucuyu kaybederse o paragraf özgün
  hâline döner.
- **Bellek:** Ollama / LM Studio'da yüklü modeller, uygulamanın kendi
  sürecindeki model (MLX ya da GGUF) ve PDF çıkarımı aynı birleşik belleği
  paylaşır. Örneğin 16 GB'lık bir model LM Studio'da yüklüyken aynı büyüklükte
  bir MLX modeli seçmek belleği iki kez doldurur. Kullanmadığınız modelleri
  sunucuda boşaltın.
- **Hız:** İstekler varsayılan olarak sırayla gider. Tek bir paragraf en çok
  240 sn beklenir; süre aşılırsa o paragraf özgün kalır ve kalite raporunda
  görünür. Büyük bir modelin sunucuda ilk yüklenmesi dakikalar sürebilir
  (en çok 600 sn beklenir).
- **Sunucu koparsa:** Birkaç yeniden denemeden sonra kalan paragraflar
  denenmeden geçilir ve iş çıkış kodu 5 ile biter.
- **Aynı kitabı iki modelle çevirmek:** Çeviri belleği model başına ayrı
  olduğundan modeller birbirinin çevirisini kullanmaz. Ama çıktı klasörü kitap
  adına göredir; önceki sonucu korumak için ikinci çeviride farklı bir kitap
  adı verin.
- **Makine çevirisi künyesi:** Bu yolun PDF ve EPUB çıktısının başında
  metnin hangi yerel modelle çevrildiğini, gözden geçirilmemiş bir taslak
  olduğunu, kaynak dili ve çeviri tarihini yazan kısa bir künye bulunur.

## 8. Sorun giderme

Arayüz ve komut satırı aynı iletileri gösterir; iletideki `(…)` yerinde
gerçek adres, dosya yolu ya da ayrıntı yazar. Paragraf düzeyi hatalar (zaman
aşımı, boş çıktı, düşünme sınırı, üretim hatası gibi) işi durdurmaz: o paragraf
özgün kalır, `pipeline.log` dosyasına yazılır ve kalite denetiminde görünür.

| İleti | Ne yapmalı |
|---|---|
| Ollama sunucusu kapalı ya da erişilemiyor (…). Ollama uygulamasını açın ya da terminalde `ollama serve` çalıştırın. | Ollama'yı açın, sonra **Modelleri tara**. Ollama başka bir portta çalışıyorsa adresi `config/mt_sources.json`'a yazın (Bölüm 5.1). |
| Ollama kurulu ama sunucusu kapalı (…). Ollama uygulamasını açın ya da terminalde `ollama serve` çalıştırın. | Aynı çözüm: sunucuyu başlatıp yeniden tarayın. |
| LM Studio sunucusu kapalı (…). LM Studio'da Developer bölümünden sunucuyu başlatın ya da `lms server start` çalıştırın. | Sunucuyu başlatın, sonra **Modelleri tara**. LM Studio başka bir portta çalışıyorsa adresi `config/mt_sources.json`'a yazın. |
| Sunucu yanıt vermiyor (…). | `config/mt_sources.json`'daki `openai_servers` sunucusu kapalı; başlatın ya da adresi düzeltin. |
| Bu adres OpenAI uyumlu bir model listesi döndürmedi (…). | Adres yanlış olabilir: taban adres çoğunlukla `/v1` ile biter. Portta başka bir program çalışıyor da olabilir. |
| “qwen3:8b” modeli Ollama sunucusunda yok. Güncel liste için “Modelleri tara”yı kullanın. | Model adını `ollama list` ile denetleyin; yoksa `ollama pull qwen3:8b` ile indirin. |
| “qwen/qwen3-8b” LM Studio'da listelenmiyor: model indirilmemiş ya da yüklü değil. … | Modeli LM Studio'da yükleyin ya da isteğe bağlı yüklemeyi (Just-in-Time) açın. |
| Bu bir gömme (embedding) modeli; metin üretemez. | Metin üreten bir sohbet (instruct) modeli seçin. |
| GGUF dosyasını doğrudan açmak için llama-cpp-python gerekli ama kurulu değil. Kurulum: CMAKE_ARGS="-DGGML_METAL=on" ./venv/bin/pip install llama-cpp-python | DMG kurulumunda Bileşenler ekranından **GGUF desteği**ni indirin; kaynak koddan kurulumda komutu proje klasöründe çalıştırın; ya da dosyayı Ollama / LM Studio üzerinden kullanın (Bölüm 3.4). |
| GGUF modeli llama-cpp-python ile yüklenemedi (…) | Kurulu llama.cpp bu mimariyi tanımıyor olabilir; modeli Ollama ya da LM Studio üzerinden deneyin. |
| Geçerli bir GGUF model dosyası değil (…) | Dosya yarım inmiş ya da bozuk olabilir, ya da bir görsel projektör (`mmproj`) dosyasıdır; modeli yeniden indirin. |
| GGUF dosyası bulunamadı: … | Dosya adını ve `models/` altında durduğunu denetleyin; sembolik bağ kırılmış olabilir (`ls -l models/`). |
| Çeviri modeli kurulu değil: … | HY-MT 1.5 7B kurulu değil. DMG kurulumunda Bileşenler ekranından indirin; kaynak koddan kurulumda kurulum rehberindeki `mlx_lm.convert` komutunu çalıştırın (`docs/KURULUM_VE_KULLANIM.md`, Bölüm 2.6). |
| MLX model klasörü bulunamadı: … | Klasör adını ve `models/` altında durduğunu denetleyin; sembolik bağ kırılmış olabilir. |
| MLX klasöründe model dosyaları eksik (…): … | İndirme ya da dönüştürme yarım kalmış: `config.json`, `*.safetensors` ve tokenizer dosyaları bulunmalı. |
| Kurulu mlx-lm bu mimariyi tanımıyor (gemma4); mlx-lm güncellenmeli ya da model LM Studio üzerinden kullanılmalı. | Modeli LM Studio'da açın ya da mlx-lm'i güncelleyin (güncellemeden sonra varsayılan modelle kısa bir deneme yapın). |
| Model yolu izinli klasörlerin dışında: … | Model `models/` dışında; `models/` altına sembolik bağ koyun (Bölüm 3.5). |
| Yalnız bu bilgisayardaki sunuculara bağlanılır; http://192.168.1.20:11434 yerel bir adres değil. | Başka bir bilgisayardaki sunucu desteklenmez. Adres `127.0.0.1`, `localhost` ya da `::1` olmalı. |
| Geçersiz sunucu adresi … | Adresi `http://127.0.0.1:<port>` biçiminde yazın (OpenAI uyumlu sunucularda sonuna `/v1`). |
| Geçersiz model kimliği … | Kimliği Bölüm 4.1'deki biçimlerden birine göre yazın. |
| Desteklenmeyen MT motoru: … | Ad yanlış yazılmış ya da `config/mt_engines.json` kaydı geçersiz (neden günlükteki uyarıda). Listeden seçmek için **Modelleri tara**. |
| Model sunucusuyla bağlantı koptu (…); kalan segmentler denenmeden geçildi. | Sunucu çeviri sırasında kapandı ya da çöktü (çoğunlukla bellek yetmedi). İş çıkış kodu 5 ile biter; sunucuyu açıp çeviriyi yeniden başlatın. |
| Model belleğe sığmadı (…): … | Sunucuda yüklü diğer modelleri boşaltın; daha küçük bir model ya da daha düşük niceleme (ör. `Q4_K_M`) seçin; GGUF'ta `n_ctx` değerini düşürün (Ollama'da `num_ctx` en az 8192'dir: daha küçük değer günlükte uyarıyla 8192'ye yükseltilir). |
| Model yüklenemedi (…): … | Sunucu modeli belleğe alamadı; ayrıntı iletide. Sunucunun kendi günlüğüne bakın; gerekirse modeli yeniden indirin ya da başka bir model seçin. |
| … isteği başarısız oldu (HTTP …): … | Sunucu isteği reddetti; nedeni iletideki ayrıntıda. Sunucu sürümü ya da model ayarı uyumsuz olabilir. |
| … yanıtı beklenen biçimde değil: … | Sunucu tanınmayan bir yanıt verdi (çoğunlukla sürüm uyumsuzluğu); sunucuyu güncelleyin ya da modeli başka bir kaynaktan deneyin. |
| Bu model düşünmesini bitirip çeviri üretemiyor: son … parçanın … tanesinde düşünme payı doldu, yeniden denemede de cevap gelmedi. Başka bir model seç — önerilen: … | Düşünmesi kapatılamayan model paragrafların çoğunda cevap üretemedi; çeviri çıkış kodu 5 ile durdu (Bölüm 6). Önerilen modeli (HY-MT 1.5 7B) ya da düşünmeyen başka bir model seçin. |
| Seçilen model düşünmeyi kapatmıyor ve düşünme sınırını aşıyor (ilk … parçadan … tanesinde); düşünmeyen bir model seçin. | Bu ileti önceki sürümlerden kalmadır; eski bir kitap klasörünün `pipeline.log` dosyasında görülebilir. Bu sürümde düşünmesi kapatılamayan model durdurulmaz, düşünerek çevirir (Bölüm 6). |
| Segment modelin bağlam sınırını aşıyor (… > … token). | Kayıtta `num_ctx` (Ollama) ya da `n_ctx` (GGUF) değerini artırın (Bölüm 5.2); LM Studio'da modeli daha uzun bağlam uzunluğuyla (Context Length) yükleyin. Paragrafın çevirisi bağlamda kalan yere sığmadığında da bu ileti görünür: kesik çeviri kabul edilmez, paragraf özgün kalır. |
| Model boş çıktı döndürdü. | Düşünen bir model payını düşünmeye harcamış olabilir (Bölüm 6); başka bir model deneyin. |
| Model 240 sn içinde yanıt vermedi. | Model bu bilgisayar için çok yavaş; daha küçük bir model seçin ya da kayıtta `request_timeout_s` değerini artırın. Düşünerek çeviride iletideki süre paragrafın o denemedeki süre sınırıdır. |
| Model düşünme sınırını aştı (… > … token); parça çevrilmedi. | Model, düşünme payı içinde cevap vermedi (Bölüm 6). Paragraf özgün kalır ve işaretlenir; çok sık görülüyorsa düşünmeyen bir model seçin ya da kayıtta `think_budget` değerini değiştirin. |
| … üretimi başarısız oldu: … | Model üretim sırasında hata verdi; o paragraf özgün kalır. Sık tekrarlanırsa modeli yeniden yükleyin ya da başka bir model deneyin. |
| “…” bir Ollama bulut modeli, bu bilgisayarda çalışmaz: çevrilecek metin … adresine gönderilirdi. Yalnız bu bilgisayarda çalışan modeller kullanılır. | Bulut modelleri (adı `-cloud` / `:cloud` ile biten ya da ollama.com'a bağlı modeller) yerel Ollama'dan listelense de çeviriyi ollama.com'da yapar; kitap metni bu bilgisayardan çıkardı. Yerel bir model indirin (ör. `ollama pull qwen3:8b`) ve onu seçin. |
| Ollama / LM Studio sunucusu açık ama model listesini vermedi (…): …. Sunucu meşgul olabilir (ör. model yüklüyor); biraz bekleyip yeniden deneyin. | Sunucu çalışıyor ama zamanında ya da beklenen biçimde yanıt vermedi (ör. büyük bir modeli yüklüyor). Biraz bekleyip **Modelleri tara**; sürerse sunucunun kendi günlüğüne bakın. Sunucu zaten açık: `ollama serve` gerekmez. |
| Model sunucusu … denemede yanıt vermedi (…): …. Bu parça çevrilmedi; sunucu sonraki parçada yeniden denenecek. | Sunucuya ulaşılamadı ya da sunucu meşguldü (ör. HTTP 503); o paragraf özgün kalır, sonraki paragraf sunucuyu yine dener. Art arda tekrarlanırsa "bağlantı koptu" iletisiyle iş çıkış kodu 5 ile biter. |
| … art arda … parçada hata döndürdü (HTTP …): …. Sunucu ya da model sorunlu görünüyor; kalan segmentler denenmeden geçildi. | Sunucu açık ama birbirini izleyen paragraflarda hata verdi (ör. `llama runner process has terminated`). İş çıkış kodu 5 ile biter; sunucunun günlüğüne bakın, modeli yeniden yükleyin ya da başka bir model deneyin. Tek tük paragrafta görülen böyle bir hata işi durdurmaz: o paragraf özgün kalır. |

## 9. Geliştiriciler için

- **Katmanlar:** Model kimliği ve kayıtlar `core/mt_engines/registry.py`,
  tarama `core/mt_engines/discovery.py`, sunucu arka uçları
  `core/mt_engines/http_backends.py`, doğrudan GGUF
  `core/mt_engines/llamacpp_backend.py`, ortak arka uç arayüzü ve düşünme
  sınırları `core/mt_engines/backends.py`, düşünme durumu ve kapatma
  doğrulaması `core/mt_engines/thinking.py`. İletiler ve veri yapıları
  `core/mt_engines/contracts.py`'de, sayısal ayarlar ve varsayılan sunucu
  adresleri `config/defaults.py`'de, kullanıcı verisinin kökü
  `config/paths.py`'de tek yerde tanımlıdır.
- **Model-bağımsız mekanik:** GPU kilidi, zaman aşımı, yeniden deneme, istem
  sızıntısı temizliği ve yer tutucu koruması bütün modellerde aynen geçerlidir;
  yeni bir model bunları kendiliğinden alır.
- **Yeni istem biçimi (nadir):** Bir model ne `chat` ne `hymt` biçimine
  uyuyorsa `core/mt_engines/` altında `MTEngine`'den türeyen küçük bir sınıf
  yazılır ve `registry.ENGINE_FORMATS`'a eklenir. Örnek olarak
  `core/mt_engines/hymt.py` incelenebilir.
- **Testler gerçek model yüklemez:** Sunucular sahte bir HTTP sunucusuyla
  (`tests/fakes/fake_llm_server.py`), llama-cpp-python sahte bir modülle
  taklit edilir; testler Ollama'nın ve LM Studio'nun gerçek portlarına
  gitmez. Gerçek modelle çalışan testler `@pytest.mark.yerel_model` ile
  işaretlenir, varsayılan koşuda seçilmez ve yalnızca açıkça istenince
  çalıştırılır: `python -m pytest -m yerel_model`.
