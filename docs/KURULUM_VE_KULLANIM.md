# Kurulum ve Kullanım Rehberi

Bu belge Kitapçevir'in kurulumunu ve iki çeviri yolunun kullanımını adım adım
anlatır. İki kurulum yolu vardır:

- **Uygulama (DMG) ile** (Bölüm 2.1–2.5): GitHub sürüm sayfasından indirilen
  uygulama; kurulum sihirbazı gereken bileşenleri kendisi indirir. Terminal
  gerekmez.
- **Kaynak koddan** (Bölüm 2.6): depoyu indirip Python sanal ortamıyla
  çalıştırmak; geliştiriciler ve komut satırı kullanımı için.

Aynı içeriğin PDF sürümü: `docs/Kullanma_Kilavuzu.pdf`.

> **Durum:** Bakım modu — yalnız hata düzeltmeleri.

## İki çeviri yolu

| | **PDF'den TeX çıkar** (önerilen) | **Yerel model ile çevir** (Beta) |
|---|---|---|
| **Çeviriyi kim yapar** | Siz ya da yapay zekâ asistanınız (Claude Code vb.) | Bu Mac'teki yerel çeviri modeli; önerilen model HY-MT 1.5 7B (MLX). MLX klasörü, Ollama, LM Studio ya da GGUF dosyasındaki bir model de seçilebilir (beta, daha az sınandı) |
| **Kaynak dil** | İngilizce, Almanca, Rusça, Fransızca, İspanyolca, İtalyanca | İngilizce, Almanca, Rusça, Fransızca, İspanyolca, İtalyanca (İngilizceden başka kaynak dillerde çeviri kalitesi seçilen modele bağlıdır; HY-MT 1.5 7B ile bu dillerden yalnız kısa Almanca ve Rusça deneme belgeleri sınandı) |
| **Çeviri modeli** | Gerekmez | Gerekir (HY-MT 1.5 7B diskte yaklaşık 15 GB) |
| **Donanım** | Her Apple Silicon Mac | Güçlü donanım ister (Bölüm 1.1) |
| **Kalite** | Çeviriyi yapana bağlı; en iyi sonuç bu yoldadır | Ön okuma / taslak kalitesi |
| **Gözetim** | Çeviri adımı sizde | Baştan sona kendiliğinden ilerler |

Hedef dil her zaman Türkçedir. Her iki yol da aynı iskeleti paylaşır:
çıkarım (PDF için Marker, EPUB için EPUB ayrıştırıcı), Markdown→LaTeX
dönüşümü, sanitizer, kalite denetimi (QC), AutoFix ve XeLaTeX dizgisi
ortaktır. Fark, çeviri adımını kimin yaptığıdır.

> **Önerilen: PDF'den TeX çıkar.** Çeviri modeli gerektirmez ve en iyi sonucu
> verir. Çeviriyi bir yapay zekâ asistanına yaptırmak için arayüz, TeX
> çıkarıldıktan sonra Claude Code'a verilecek hazır istemi gösterir
> (Bölüm 5.2).

> **Yerel model hakkında dürüst not:** "Yerel model ile çevir" beta
> aşamasındadır: çeviri modelin gücüne bağlıdır ve çıktı gözden geçirilmemiş
> bir taslaktır. Eylül 2026'daki karşılaştırmalı denemede bu yolun çevirisi,
> TeX yolunda yapay zekâ asistanıyla yapılan çeviriden belirgin biçimde düşük
> kalitede çıktı. Bu yol gözetimsiz ve çevrim dışı çalışır; ön okuma ve taslak
> için uygundur. Yerel model kullanacaksanız en çok sınanan model olan HY-MT
> 1.5 7B'yi seçmenizi öneririz.

---

## 1. Sistem gereksinimleri

- **Bilgisayar:** **Apple Silicon (M serisi)** işlemcili, macOS 14 ya da
  sonrası çalışan bir Mac. Arayüz başka bir sistemde (Intel Mac, Linux,
  Windows) açılmaz; açılışta platformu denetler ve uygun değilse nedenini
  yazıp durur. Komut satırı betiklerinde başka donanımlar için seçenekler
  (ör. `--device cpu`) bulunsa da bunlar test edilmemiştir ve desteklenmez.
- **İnternet:** Kurulum sırasında bileşenler indirilir. Uygulamanın TeX
  çıkarma, yerel modelle çeviri ve dizgi adımları çevrim dışı çalışır (Hafif
  TeX yeni bir LaTeX paketine ilk kez gerek duyduğunda onu indirir).
- **Disk:** PDF desteği (Marker, torch ve Marker modelleri) birkaç GB yer
  tutar; Marker modelleri tek başına yaklaşık 3,5 GB'tır. "Yerel model ile
  çevir" kullanılacaksa çeviri modeli için ayrıca yer gerekir (HY-MT 1.5 7B
  yaklaşık 15 GB; başka bir model seçilirse o modelin boyutu). Kurulum
  sihirbazı seçilen bileşenlerin indirme ve disk boyutunu gösterir, boş diski
  ölçer ve yer yetmiyorsa kurulumu başlatmaz.
- **Bellek ("Yerel model ile çevir" için):** Model çeviri sırasında bütünüyle
  belleğe yüklenir; uygulama bu Mac'in belleğini ölçüp modelin sığıp
  sığmadığına kendisi bakar (Bölüm 1.1). Ollama ya da LM Studio'da yüklü
  modeller de aynı birleşik belleği kullanır. TeX yolu çeviri modeli yüklemez.
- **Kaynak koddan kurulumda ayrıca:** arm64 Python 3.12 (Rosetta
  altında çalışan x86_64 Python kabul edilmez) ve XeLaTeX içeren güncel bir
  MacTeX / TeX Live. Şablon `fontspec` ile Latin Modern OTF yazı tiplerini
  kullanır (TeX Live ile gelir).

### 1.1. Donanım gereksinimi

"PDF'den TeX çıkar" her Apple Silicon Mac'te çalışır. "Yerel model ile çevir"
güçlü donanım ister: önerilen HY-MT 1.5 7B ile 20 sayfalık bir kitabın
çevirisinde ölçülen tepe bellek kullanımı yaklaşık 26 GB'tı (36 GB belleği
olan bir Mac'te, 29 Eylül 2026).

Uygulama bu Mac'in belleğini **ölçer**: MLX'in bildirdiği, Metal'in önerdiği
çalışma kümesi (`max_recommended_working_set_size`; belleğin kabaca üçte
ikisi ile dörtte üçü) "kullanılabilir bellek"tir. MLX bu değeri veremezse
toplam bellekten kaynaklı bir oranla hesaplanır ve bu durum kartta yazar;
hiç ölçülemezse kart açık kalır ve "bellek ölçülemedi" uyarısı çıkar.
Arayüzün sol kenarındaki **Sistem** satırı çipi ve kullanılabilir belleği
gösterir. Bir modelin tahmini gereksinimi şöyle hesaplanır:

- **ağırlık:** modelin diskte (ya da sunucuda) ölçülen gerçek boyutu;
- **KV önbelleği:** modelin kendi üst verisinden (katman, KV başlığı, başlık
  boyutu) kesin formülle, çevirinin token sınırlarına göre; düşünerek
  çeviren modelde parça başına düşünme payı da eklenir;
- **çalışma payı:** ağırlığın %25'i (ölçülmemiş, kaynaklı tek varsayım).

Sonuçlar:

- **Genel kapı:** Bu Mac 4 bit nicemlenmiş bir 7B modeli (referans model)
  toplu iş 1 ile çalıştıramıyorsa "Yerel model ile çevir" kartı kapalıdır ve
  neden sayılarıyla yazar: "Bu Mac yerel model için yetersiz: en az … GB
  kullanılabilir bellek gerekir, bu Mac'te … GB." Formüle göre bu 8 GB'lık
  Mac'lerde olur. "PDF'den TeX çıkar" kartı normal çalışır. Kurulum
  sihirbazında da HY-MT ve GGUF seçenekleri bu durumda kapalıdır.
- **Model başına kapı:** Toplu iş 1 ile bile sığmayan model listede "bu Mac'e
  sığmaz (gerekli ~… GB, kullanılabilir … GB)" diye görünür; seçilirse
  "Çevir" soluk kalır. Formüle göre HY-MT 1.5 7B 16 GB'lık Mac'e sığmaz.
  LM Studio ve OpenAI uyumlu sunucular modelin KV önbelleği yapısını
  bildirmediği için oradaki tahmin kabadır: bu modeller engellenmez, listede
  "bu Mac'e sığmayabilir (kaba tahmin ~… GB, kullanılabilir … GB)" uyarısıyla
  seçilebilir kalır (yüklemeyi ve bellek denetimini sunucunun kendisi yapar);
  sunucunun şu an yüklü tuttuğu modele "sığmaz" denmez.
- **Uyarlanır toplu iş:** Bellek darsa aynı anda çevrilen parça sayısı
  sığacak kadar küçültülür ve kartta "Bellek: aynı anda en çok … parça
  çevrilir" notu çıkar. Bellek yetiyorsa hiçbir ayar değişmez.

Komut satırı aynı hükmü verir: kart kapalıysa ya da model sığmıyorsa
`translate_book.py` çıkarımdan önce aynı iletiyle çıkış kodu 5 verir (Bölüm
9.2). Ölçüm, formül ve her varsayımın kaynağı:
[`docs/DONANIM_VARSAYIMLARI.md`](DONANIM_VARSAYIMLARI.md).

## 2. Kurulum

### 2.1. Uygulama (DMG) ile kurulum

1. Deponun sürüm sayfasından
   (https://github.com/berketez/kitap_cevir/releases/latest)
   `Kitapcevir-<sürüm>.dmg` dosyasını indirin.
2. DMG'yi açın ve **Kitapçevir** simgesini **Uygulamalar** (Applications)
   kısayolunun üzerine sürükleyin.
3. Uygulamayı **Uygulamalar** klasöründen açın. DMG'nin içinden ya da
   taşımadan açarsanız açılış ekranı "Kitapçevir'i Uygulamalar klasörüne
   taşıyıp oradan açman önerilir." der.
4. İlk açılışta macOS uygulamayı engeller; Bölüm 2.2'deki adımlarla bir kez
   onaylayın (DMG'deki **İlk açılış.txt** aynı adımları özetler).
5. Kurulum sihirbazı açılır (Bölüm 2.3). Kurulum bitince **Kitapçevir'i aç**
   düğmesiyle arayüz aynı pencerede açılır.

Uygulama kendi verisini ve bileşenlerini
`~/Library/Application Support/Kitapçevir/` altında tutar (Bölüm 2.5);
günlükler `~/Library/Logs/Kitapçevir/` altındadır. Uygulama paketinin içine
hiçbir şey yazılmaz.

### 2.2. İlk açılış: imzasız uygulamayı açmak

Kitapçevir imzasız dağıtılır: Apple geliştirici kimliğiyle imzalanmamış ve
Apple'a onaylatılmamıştır. Bu yüzden macOS ilk açılışta uygulamayı engeller.
Onay bir kez verilir; sonraki açılışlar normaldir.

- **Finder'da sağ tık → Aç:** Uygulamalar klasöründe Kitapçevir'e sağ
  tıklayın (ya da Control tuşuyla tıklayın), **Aç**'ı seçin ve açılan
  pencerede yeniden **Aç**'a basın. Bu yol macOS 14'te çalışır.
- **Gerekirse Sistem Ayarları → Gizlilik ve Güvenlik → Yine de Aç:** macOS 15
  ve sonrasında sağ tık yolu kaldırıldı. Uygulamayı açmayı bir kez deneyin;
  engellendikten sonra **Sistem Ayarları → Gizlilik ve Güvenlik** bölümünde,
  Güvenlik başlığı altında beliren **Yine de Aç** düğmesine basın ve giriş
  parolanızı girin. Düğme açma denemesinden sonra yaklaşık bir saat görünür.

### 2.3. Kurulum sihirbazı

Sihirbaz uygulamanın içindedir; sırayla şu ekranları gösterir. Ekrandaki her
sayı (boyut, hız, kalan süre) gerçek ölçümden gelir; bilinmeyen değer "—"
olarak gösterilir.

1. **Kurulum (hoş geldin):** Bu Mac'in çipi, macOS sürümü, kullanılabilir
   belleği ve boş diski ile internet bağlantısı. Bileşenler yayıncılarının
   kendi sunucularından (ör. GitHub, PyPI, Hugging Face, datalab.to) doğrudan
   iner; Kitapçevir bu dosyaları barındırmaz.
2. **Lisanslar:** Kitapçevir'in kendi lisansı (GNU GPL 3.0) ve PDF desteğinin
   yapay zekâ modelleri için Datalab model lisansının resmî metni ve
   bağlantısı; "Tüm lisansları göster". Datalab lisansı kabul edilmeden kurulum
   sürdürülemez: PDF desteği zorunlu bileşendir ve bu modeller olmadan
   çalışmaz (bileşenlerin lisansları: Bölüm 13).
3. **Bileşenler:** Ne kurulacağını seçersiniz:

   | Bölüm | Bileşen | Not |
   |---|---|---|
   | Zorunlu (değiştirilemez) | Çekirdek | Python, arayüz, EPUB yolu, derleme |
   | | MLX | Apple Silicon hesaplama kitaplığı |
   | | PDF desteği | Marker + torch + Marker modelleri (Datalab lisansı) |
   | TeX dağıtımı (birini seç) | Hafif TeX (tectonic) | Varsayılan. Paketleri gerektikçe indirir; kurulumda şablon bir kez derlenerek ısıtılır |
   | | Tam TeX (MacTeX) | Büyük indirme; macOS Yükleyici açılır ve yönetici parolası ister |
   | | Kurulu olanı kullan | Yalnızca bu Mac'te XeLaTeX içeren bir TeX bulunursa görünür; seçilmeden önce şablon onunla derlenerek sınanır |
   | İsteğe bağlı | HY-MT 1.5 7B | Önerilen yerel çeviri modeli (yaklaşık 15 GB). Tencent lisansının kabulü gerekir; bu Mac'e sığmıyorsa seçilemez ve nedeni yazar |
   | | GGUF desteği | llama-cpp-python (Metal); GGUF dosyalarını doğrudan açmak için |

   Ollama ya da LM Studio kuruluysa Kitapçevir onları kendisi bulur; burada
   kurulmaz.
4. **Özet:** Kurulacaklar, toplam indirme, diskte kaplayacağı yer, boş disk
   ve kurulum yeri. Boş disk yetmiyorsa ne kadar daha gerektiği yazar. Bağlantı
   yoksa "Bağlantı yok. Kurulu bileşenler kullanılabilir; eksikler bağlantı
   gelince kurulur." der.
5. **Kurulum:** Genel ilerleme yalnızca indirmede, gerçek baytlarla
   gösterilir (hız ve tahmini kalan süreyle); kurma adımlarında çubuk yoktur,
   geçen süre yazar. **Duraklat**, **İptal** ve **Sürdür** düğmeleri vardır:
   yarım inen dosyalar saklanır, kurulum kaldığı yerden sürer. Bir sunucunun
   istek sınırına takılınca "… istek sınırı: … sn sonra devam edilecek"
   satırı çıkar. Kurulum günlüğünün yolu ekranda yazar.
6. **Kurulum tamamlandı:** Kurulan bileşenlerin listesi; **Kitapçevir'i aç**
   ve **Bileşenler** düğmeleri.

Kurulum sürerken uygulamayı kapatırsanız indirilen kısım saklanır ve bir
sonraki açılışta kurulum kaldığı yerden sürer.

### 2.4. Bileşenler ekranı, güncelleme ve kaldırma

**Kitapçevir → Bileşenler…** menüsü (⌘,) ya da arayüzün sol kenarındaki
**Bileşenler** bağlantısı sihirbazın ikinci yüzünü, Bileşenler ekranını
açar. Tabloda her bileşenin durumu, sürümü ve diskte kapladığı yer
(gerçek ölçüm) yazar. İşlemler:

- **Onar:** Bileşenin dosyalarını SHA-256 ile doğrular, bozuk ya da eksik
  olanları yeniden indirir ve yeniden kurar.
- **İndir / Kaldır (isteğe bağlı bileşenler):** HY-MT 1.5 7B ve GGUF desteği
  sonradan indirilebilir ya da kaldırılabilir. Kaldırırken kitaplarınız ve
  çeviri belleğiniz silinmez.
- **Türünü değiştir… (TeX):** Hafif TeX, Tam TeX ve kurulu TeX arasında geçiş.
- **Eksikleri kur:** Zorunlu bir bileşen eksikse kurulumu tamamlar.
- **Güncelleme: Denetle:** Yeni sürüm yalnızca bu düğmeyle denetlenir,
  kendiliğinden denetlenmez ve kurulmaz. Yeni sürüm varsa **Sürüm sayfasını
  aç** sürüm sayfasını açar; yeni DMG'deki uygulamayı eskisinin üzerine
  sürüklersiniz. Kitaplarınız ve ayarlarınız uygulamadan ayrı tutulduğu için
  güncellemeden etkilenmez.
- **Kaynak kodu · Lisanslar · Günlükleri göster** bağlantıları.
- **Kitapçevir'i kaldır…:** Silinecek klasörler boyutlarıyla listelenir.
  Varsayılan olarak `veri/` klasörü **korunur**: kitaplarınız, çeviri
  belleğiniz ve modeller (sihirbazın kurduğu HY-MT 1.5 7B dahil; panel adını
  ve diskte kapladığı gerçek yeri ayrıca gösterir). Yalnız HY-MT'yi silmek
  için "Şunu da sil: HY-MT …" kutusunu, verinin tamamını silmek için
  "Kitaplarımı, belleği ve modeller klasörünün tamamını da sil" kutusunu
  işaretleyin. Kaldırma bitince "Şimdi
  uygulamayı kapat (⌘Q) ve Uygulamalar klasöründen Çöp Sepeti'ne sürükle."
  yazar. MacTeX sistem geneline kurulduğu için kaldırılmaz; kaldırmak için
  TeX Users Group'un kaldırma yönergesine bakın.

Bir iş sürerken bileşen değiştirilemez: önce işin bitmesini bekleyin ya da
işi iptal edin.

**Elle kaldırma** (uygulama açılamıyorsa): şu klasörleri silin, sonra
uygulamayı Çöp Sepeti'ne sürükleyin.

- `~/Library/Application Support/Kitapçevir/` (kitaplarınız ve çeviri
  belleğiniz de buradadır: `veri/` alt klasörü; saklamak istiyorsanız önce
  onu taşıyın)
- `~/Library/Logs/Kitapçevir/`
- `~/Library/WebKit/io.github.berketez.kitapcevir`,
  `~/Library/Caches/io.github.berketez.kitapcevir`,
  `~/Library/HTTPStorages/io.github.berketez.kitapcevir`,
  `~/Library/Preferences/io.github.berketez.kitapcevir.plist`,
  `~/Library/Saved Application State/io.github.berketez.kitapcevir.savedState`
  (varsa)

### 2.5. Veri klasörü: kitaplar, modeller, çeviri belleği

DMG kurulumunda kullanıcı verisi uygulamanın kodundan ayrı, tek bir kökte
durur: `~/Library/Application Support/Kitapçevir/veri/`. Kaynak koddan
kurulumda aynı klasörler proje klasörünün içindedir.

| İçerik | DMG kurulumu (`~/Library/Application Support/Kitapçevir/` altında) | Kaynak koddan kurulum (proje klasöründe) |
|---|---|---|
| Kitap klasörleri | `veri/output/<kitap_adı>/` | `output/<kitap_adı>/` |
| Kendi eklediğiniz modeller (MLX klasörleri, GGUF dosyaları); HY-MT de buraya iner | `veri/models/` | `models/` |
| Çeviri belleği | `veri/data/translation_memory.db` | `data/translation_memory.db` |
| İsteğe bağlı ayar dosyaları | `veri/config/` | `config/` |
| Marker modelleri | `modeller/datalab/` | Marker'ın kendi önbelleği (`~/Library/Caches/datalab/`) |

Arayüzdeki **Klasörü aç** düğmesi kitabın klasörünü doğrudan açar; yolu
ezberlemeniz gerekmez. Bu belgede `models/` ve `config/` dendiğinde DMG
kurulumunda `veri/models/` ve `veri/config/` anlaşılmalıdır.

### 2.6. Kaynak koddan kurulum (geliştiriciler için)

#### Depoyu indir ve bağımlılıkları kur

```bash
git clone https://github.com/berketez/kitap_cevir.git kitapcevir
cd kitapcevir
python3.12 -m venv venv
source venv/bin/activate
pip install --no-deps -r requirements.txt   # --no-deps gerekli: requirements.txt başındaki nota bak
```

Sanal ortamın adı `venv` olmalıdır: arayüzü başlatma komutu
(`./venv/bin/streamlit`) bu adı kullanır.

#### XeLaTeX'i kur

XeLaTeX yoksa MacTeX'i kurun ve yeni bir terminal açın:

```bash
brew install --cask mactex
xelatex --version        # kurulumu doğrular
```

#### Çeviri modelini hazırla (yalnızca "Yerel model ile çevir" için)

TeX yolunu kullanacaksanız **bu adımı atlayın.** Önerilen çeviri modeli
HY-MT 1.5 7B'dir. Model, boyutu nedeniyle depoda yoktur (`models/` klasörü
git dışıdır); tek komutla indirilip MLX biçimine dönüştürülür:

```bash
./venv/bin/mlx_lm.convert --hf-path tencent/HY-MT1.5-7B --mlx-path models/hymt-7b-mlx
```

Model `models/hymt-7b-mlx/` altına yerleşir (yaklaşık 15 GB). Arayüz modeli
bu klasörde arar; kenar çubuğundaki "Sistem" bölümünde çeviri modelinin hazır
olduğu yazar.

> **Düşük bellekli makineler:** `mlx_lm.convert` komutuna `-q` eklenirse model
> nicemlenir ve daha az yer kaplar. Ancak bu projede çeviri kalitesi
> yalnızca nicemlenmemiş modelle denenmiştir; nicemlenmiş modelde kalite
> garanti edilmez.

**Başka bir model kullanmak:** HY-MT 1.5 7B yerine dört kaynaktan biri de
seçilebilir (beta düzeyinde, daha az sınandı); o zaman HY-MT'yi indirmek
gerekmez:

| Kaynak | Hazırlık |
|---|---|
| MLX klasörü | MLX biçimli model `models/` altına konur |
| Ollama | Ollama kurulur, model indirilir (`ollama pull qwen3:8b`), sunucu açık tutulur |
| LM Studio | Model LM Studio'da indirilip yüklenir, yerel sunucu başlatılır |
| GGUF dosyası | `.gguf` dosyası `models/` altına konur; doğrudan açmak için llama-cpp-python gerekir |

Kitapçevir yalnızca `models/` klasörünü tarar; başka yerdeki bir model
`models/` altına sembolik bağla konur. Sunuculara yalnızca bu bilgisayarda
(`127.0.0.1`) bağlanılır. Ollama ve LM Studio kendi varsayılan adreslerinde
aranır (tek kaynak: `config/defaults.py`), bu yüzden çoğu zaman hiçbir ayar
dosyası gerekmez. Ayrıntılar, kimlik biçimleri ve nadir elle ayar durumları:
[`docs/YENI_MODEL_EKLEME.md`](YENI_MODEL_EKLEME.md).

llama-cpp-python **isteğe bağlıdır** ve `requirements.txt` ile kurulmaz.
Yalnızca GGUF dosyasını Ollama ya da LM Studio olmadan doğrudan açmak için
gerekir; Apple Silicon'da Metal desteğiyle kurulumu (proje klasöründe):

```bash
CMAKE_ARGS="-DGGML_METAL=on" ./venv/bin/pip install llama-cpp-python
```

#### Kendiliğinden inen bileşenler (adım gerekmez)

- **Marker modelleri:** ilk PDF çıkarımında kendiliğinden iner (yaklaşık
  3,5 GB). Bu yüzden ilk PDF her zamankinden uzun sürer. Ağırlıklar
  Datalab'ın model lisansıyla sunulur; metin:
  https://github.com/datalab-to/marker (`MODEL_LICENSE`).
- **`data/dictionary/`** (genel amaçlı İngilizce-Türkçe sözlük verisi):
  isteğe bağlıdır, depoda ve DMG kurulumunda yoktur. Bulunamazsa günlüğe uyarı
  yazılır ve çalışma sözlüksüz sürer.

## 3. Uygulamayı başlatma

**DMG kurulumunda:** Uygulamalar klasöründen **Kitapçevir**'i açın. Arayüz
uygulamanın kendi penceresinde açılır; **Görünüm → Tarayıcıda Aç** (⌥⌘O) aynı
sayfayı varsayılan tarayıcıda açar. Uygulamayı **⌘Q** ile kapatın. Bir iş
sürerken kapatırsanız onay istenir ve iş iptal edilir.

**Kaynak koddan:** Proje klasöründe:

```bash
./venv/bin/streamlit run app.py
```

Arayüz tarayıcıda **http://127.0.0.1:8501** adresinde açılır (tarayıcı
kendiliğinden açılmazsa terminalde yazan adresi kullanın). Arayüzü kapatmak
için terminalde `Ctrl+C`'ye basın.

İki kurulumda da ayarlar `.streamlit/config.toml` dosyasındadır:

- Arayüz yalnızca bu bilgisayardan erişilebilir (yerel adres `127.0.0.1`).
- En büyük yükleme boyutu 500 MB'tır.

## 4. Arayüzün bölümleri

| Bölge | İçerik |
|---|---|
| **Sol kenar** | **Kitap adı** (isteğe bağlı): çıktı klasörünün adı. Boş bırakılırsa dosya adından çıkarılır; aynı adda başka bir kitap varsa ayırmak için kullanılır. **Sistem:** "macOS · Apple Silicon", çip ve kullanılabilir bellek, seçili çeviri modelinin hazır olup olmadığı. DMG kurulumunda **Bileşenler** bağlantısı (Bölüm 2.4). **Hakkında:** proje ve Tencent'le bağlantısızlık beyanı, **Kaynak kodu** bağlantısı. |
| **Orta, "Çeviri"** | Kitap yükleme alanı (PDF ya da EPUB). Kitap yüklenince dosya kartı (biçim, boyut, PDF'de sayfa sayısı) ve iki çeviri kartı görünür: **PDF'den TeX çıkar** ve **Yerel model ile çevir** (Beta). İş sürerken ilerleme paneli, iş bitince sonuç burada görünür. |
| **Sağ, "Araçlar"** | **Terimler**, **Bellek** ve **Karşılaştırma** sekmeleri (bkz. Bölüm 8). |

Aynı anda tek iş çalışır; bir iş sürerken çeviri düğmeleri, kitap adı ve
kitap yükleme kapalıdır.
Sayfayı yenileseniz de çalışan iş sürer ve ilerlemesi görünmeye devam eder.

## 5. PDF'den TeX çıkar (önerilen yol)

Bu yol üç adımdır: **çıkar → çevir → derle.** Çeviri modeli gerekmez.

### 5.1. Arayüzle, adım adım

1. Orta alana kitabı (PDF ya da EPUB) yükleyin. İsterseniz önce sol kenarda
   kitap adını yazın.
2. **PDF'den TeX çıkar** kartında **"1 · TeX'i çıkar"** düğmesine basın.
   İş bitince "TeX çıkarıldı: … bölüm" iletisi ve **"EXPORT.tex'i indir"**
   düğmesi çıkar. Dosya kitap klasöründe `EXPORT.tex` olarak kaydedilmiştir
   (Bölüm 2.5).
3. Kartta beliren **"Klasörü aç"** düğmesi kitap klasörünü Finder'da açar.
   Kart ayrıca çevrilecek dosyanın ve bu kitaba özel çeviri talimatının tam
   yolunu gösterir.
4. `EXPORT.tex`'i Türkçeye çevirin (kendiniz ya da bir yapay zekâ asistanıyla;
   kurallar için bkz. 5.3) ve **aynı klasöre** `EXPORT_TR.tex` adıyla kaydedin.
5. **"2 · Çevrilmiş TeX'i derle"** düğmesine basın. Bu düğme, klasörde
   `EXPORT_TR.tex` görülene kadar soluk kalır. Dosya kaydedildikten sonra
   düğme birkaç saniye içinde kendiliğinden açılır; açılmazsa sol kenardaki
   **Kitap adı**nın TeX'i çıkarırken kullandığınızla aynı olduğuna bakın.
6. İş bitince **"PDF'yi aç"** (PDF'yi varsayılan uygulamayla açar) ve
   **"İndir"** düğmeleri çıkar. Türkçe PDF kitap klasörüne
   `<kitap_adı>_TR.pdf` olarak da kaydedilmiştir.

### 5.2. Yapay zekâ asistanıyla (Claude Code)

TeX çıkarıldıktan sonra kart, çevrilecek dosyanın ve bu kitaba özel çeviri
talimatının (`CEVIRI_TALIMATI.md`, kitap klasöründe) tam yollarını ve
**"Claude Code'a şunu yaz:"** başlığı altında hazır bir istem gösterir. İstemi
Claude Code'a verin; asistan talimatı okur, metni bölümlere böler, ortak bir
terim sözlüğü çıkarır, çevirir ve `EXPORT_TR.tex`'i kaydeder. Ardından arayüzde
**"2 · Çevrilmiş TeX'i derle"** düğmesine basın.

Kaynak koddan kurulumda proje klasöründe Claude Code'u açıp **"bu kitabı
çevir"** demeniz de yeterlidir: Claude, kökteki
[`CEVIRI_TALIMATI.md`](../CEVIRI_TALIMATI.md) talimatını okur ve akışı
(çıkar, çevir, derle) kendisi yürütür.

### 5.3. Çeviri kuralları

Çeviriyi kim yaparsa yapsın şu kurallara uyulmalıdır:

1. **Yalnızca doğal dildeki metin ve başlıklar çevrilir.** LaTeX komutları,
   matematik (`$...$`, `\begin{equation}` …), `\ref`/`\cite`/`\label`
   argümanları, dosya yolları ve ortam adları olduğu gibi bırakılır.
2. **`EXPORT.tex`'in başındaki `% KITAPCEVIR-META` satırları silinmez ve
   değiştirilmez.** Derleme adımı kitabı bu satırlarla tanır; dosyayı başka
   bir klasöre taşısanız bile kimlik bu sayede kaybolmaz.
3. **Belge yapısı korunur:** `\chapter`/`\section` hiyerarşisi ve ortam
   sayısı kaynakla birebir aynı kalır. Derleme adımı bölüm sayısını kaynakla
   karşılaştırır ve uyuşmazlıkta uyarır.
4. **Hiçbir metin atlanmaz:** her cümle, dipnot, tablo hücresi ve şekil
   altyazısı çevrilir.
5. **Büyük kitaplar bölüm sınırlarından parçalanır** (parça başına en çok
   yaklaşık 80 KB önerilir); parçalar ayrı çevrilip sırayla birleştirilir.
   Tüm parçalar aynı terim listesini kullanmalıdır.
6. **Başlıklara "Bölüm X" yazılmaz;** şablon bölüm numarasını ve "Bölüm" ön
   ekini kendisi ekler.
7. Türkçe karakterler (ç, ğ, ı, İ, ö, ş, ü) doğru yazılır; dosya **UTF-8**
   olarak kaydedilir. UTF-8 olmayan dosyada derleme adımı açık bir hata verir.

Derleme *manuel çeviri kipinde* çalışır: yapısal denetimler (ortam dengesi,
yer tutucu bütünlüğü, derleme) aynen yapılır, ama çevirmenin sözcük
tercihlerinin üzerine yazılmaz.

## 6. Yerel model ile çevir (Beta)

1. Orta alana kitabı (PDF ya da EPUB) yükleyin. Kitap bu yolun desteklediği
   kaynak dilde olmalıdır (baştaki "İki çeviri yolu" tablosu; dil denetimi:
   Bölüm 6.1).
2. **Yerel model ile çevir** kartında **Kaynak dil** listesine bakın: kitabın
   dili kendiliğinden ölçülür ve güveniyle birlikte yazar. Tespit belirsizse
   "Çevir"den önce dili seçmeniz istenir; tespit yanlışsa dili buradan
   değiştirin.
3. **Model** listesinden çeviri modelini seçin (önerilen ve varsayılan:
   HY-MT 1.5 7B; listede "önerilen" diye işaretlidir). Liste, arayüz ilk açıldığında bir kez kendiliğinden
   taranır; bir modeli yeni hazırladıysanız ya da bir sunucuyu yeni
   açtıysanız **"Modelleri tara"** düğmesine basın (Bölüm 6.2). Kullanılamayan
   bir model seçiliyse **"Çevir"** düğmesi soluk kalır ve nedeni altında
   yazar.
4. **"Çevir"** düğmesine basın. Model önce birkaç saniyelik son bir
   denetimden geçer; sunucu kapandıysa ya da model artık yoksa iş başlamaz ve
   nedeni yazılır. Denetim geçerse çıkarım, çeviri, kalite denetimi ve dizgi
   kendiliğinden ilerler; ilerleme panelinde çeviriyi yapan model yazar.
5. İş bitince **"PDF'yi aç"** ve **"İndir"** düğmeleri çıkar. PDF kitap
   klasöründe `compiled/<kitap_adı>_TR.pdf` olarak kaydedilir.

Bu yolun PDF ve EPUB çıktısının başında (kitabın özgün kapağı basıldıysa onun
arkasında) kısa bir makine çevirisi künyesi bulunur: metnin bu bilgisayardaki
hangi yerel modelle çevrildiğini, gözden geçirilmemiş bir taslak olduğunu,
kaynak dili ve çeviri tarihini yazar ("PDF'den TeX çıkar" çıktısında künye
yoktur). Makine çevirisini kamuya açık bir yerde paylaşacaksanız künyeyi
silmeyin ve kullandığınız modelin lisansındaki koşullara bakın (lisans
metinlerinin bağlantıları: Bölüm 13).

Kalite beklentisi için baştaki dürüst nota bakın: bu yolun çıktısı ön okuma
ve taslak içindir. Kalite denetiminin eşikleri HY-MT 1.5 7B'ye göre ayarlıdır;
başka bir modelle çevrilen ilk kitapta `qc_report.txt` dosyasını gözden
geçirin.

### 6.1. Dil denetimi

Yerel model kitabın dilini iki kapıda ölçer:

1. **Ön kapı (çıkarımdan önce):** PDF'nin metin katmanına ya da EPUB metnine
   bakar. Metin katmanı olan ve dili bu yolda desteklenmeyen bir kitapta
   iş, uzun süren çıkarım başlamadan saniyeler içinde durur. Metin katmanı
   yoksa (taranmış kitap) ya da sonuç belirsizse ön kapı geçer ve kararı tam
   kapıya bırakır.
2. **Tam kapı (çıkarımdan sonra):** Çıkarılan metni (taranmış kitapta OCR
   sonucunu) ölçer ve çeviri başlamadan karar verir. Ölçülen dil
   desteklenen dillerden biriyse çeviriye aktarılır: modele verilen talimat
   o dilden Türkçeye çevirmesini söyler, çevrilmeden kalan satırlar o dilde
   aranır. Tespit belirsizse (ölçülebilir düzyazı az ya da güven düşük) iş
   durmaz, kaynak İngilizce varsayılır ve `pipeline.log` dosyasına uyarı
   yazılır.

Kartta dili elle seçtiyseniz (tespitten farklı bir dil ya da tespit
belirsizken yapılan seçim) iki kapı da atlanır ve çeviri seçtiğiniz dille
yapılır.

Kitabın desteklenmeyen bir dilde (ör. Çince ya da Portekizce) olduğu güvenle
ölçülürse iki kapı da işi **çıkış kodu 4** ile durdurur; çeviri hiç başlamaz.
Arayüz bu durumda "Yerel model bu kitabı çevirmedi: kitabın dili
desteklenmiyor (kaynak dil kapısı, çıkış kodu 4)." iletisini, altında tespit
edilen dili ve desteklenen dillerin listesini gösterir.

Kitabın dili aslında destekleniyorsa ve tespit yanlışsa kartın **Kaynak dil**
listesinden doğru dili seçip yeniden çevirin. Komut satırında (Bölüm 9.2)
`--kaynak-dil` bayrağı dili elle verir (ör. `--kaynak-dil de`),
`--dil-kontrolu-yok` ise kaynağı İngilizce varsayar. İki bayrak da iki kapıyı
birden atlar; `--dil-kontrolu-yok` arayüzde yoktur ve yalnızca tespit yanlış
olduğunda kullanılır.

### 6.2. Model seçimi

- **Önerilen model HY-MT 1.5 7B:** en çok sınanan model budur; kalite
  denetiminin eşikleri de onun çıktısına göre ayarlıdır. DMG kurulumunda
  Bileşenler ekranından, kaynak koddan kurulumda Bölüm 2.6'daki komutla
  kurulur.
- **Diğer kaynaklar (beta, daha az sınandı):** MLX klasörü (`models/`
  altında), Ollama, LM Studio (ve llama.cpp sunucusu gibi OpenAI uyumlu yerel
  sunucular) ve GGUF dosyası (`models/` altında). Hazırlık:
  [`docs/YENI_MODEL_EKLEME.md`](YENI_MODEL_EKLEME.md).
- **Taranan tek klasör `models/`** (DMG kurulumunda `veri/models/`, Bölüm
  2.5): LM Studio'nun ya da Hugging Face'in indirme klasörleri taranmaz;
  başka yerdeki bir model `models/` altına sembolik bağla konur (Bölüm 11).
- **Listede yalnızca gerçekten bulunan modeller görünür.** Sunucusu kapalı bir
  kaynaktan model listelenmez; kartta o kaynağın durumu yazar (ör. "Ollama
  sunucusu kapalı ya da erişilemiyor (…)"). Her seçenekte modelin adı ve
  kaynağı, biliniyorsa ayrıntısı ve boyutu yazar; kullanılamayan model
  "kullanılamaz", bu Mac'in belleğine sığmayan model "bu Mac'e sığmaz
  (gerekli ~… GB, kullanılabilir … GB)" diye işaretlidir (Bölüm 1.1).
- **Seçim hatırlanır** (`config/mt_selection.json`, git dışı). Son seçilen
  model bir sonraki taramada bulunamazsa varsayılan model seçilir ve bu durum
  kartta yazar. İş sürerken model listesi ve tarama düğmesi kapalıdır.
- **Düşünen modeller** (Qwen3, gpt-oss vb.): düşünme resmî yolla
  kapatılabiliyorsa kapatılır ve model düşünmeden çevirir. Kapatılamıyorsa
  model düşünerek çevirir: her parçaya ayrı bir düşünme payı ayrılır, iş daha
  yavaş sürer. Pay dolup cevap gelmeyen parça bir kez yeniden denenir; yine
  cevap yoksa o parça özgün dilinde kalır ve işaretlenir (kalite raporunda
  görünür), çeviri sürer. Model parçaların çoğunda cevap üretemiyorsa çeviri
  "Bu model düşünmesini bitirip çeviri üretemiyor: …" iletisiyle, **çıkış
  kodu 5** ile durur ve önerilen model adıyla başka bir model seçmeniz
  istenir. Listede her modelin düşünme durumu yazar ("düşünme yok",
  "düşünme kapalı çalışır", "düşünen model — düşünerek çevirir, daha
  yavaştır" ya da "düşünme durumu bilinmiyor"); kart seçili modelinkini de
  gösterir. Düşünme durumu bilinmeyen ama düşündüğü bilinen model için kartta
  "düşünen model; yavaş olabilir" notu çıkar. Ayrıntı: [`docs/YENI_MODEL_EKLEME.md`](YENI_MODEL_EKLEME.md),
  Bölüm 6.
- **Çeviri belleği model başına ayrıdır:** her model yalnızca kendi önceki
  çevirilerini kullanır; HY-MT'nin mevcut kayıtları olduğu gibi korunur.
- **Yalnızca bu bilgisayar:** Ollama, LM Studio ve diğer sunuculara yalnızca
  yerel adresle (`127.0.0.1`, `localhost`, `::1`) bağlanılır.
- **Model çeviri sırasında kullanılamaz hâle gelirse** (ör. sunucu kapandı)
  iş çıkış kodu 5 ile biter ve arayüz "Seçili model kullanılamadı; nedeni
  aşağıda." iletisini gösterir.

## 7. İlerleme, iptal ve sonuç

- **İlerleme paneli** işin adını ("TeX çıkarılıyor", "Çeviri sürüyor",
  "Kitap diziliyor"), o anki fazı ve geçen süreyi gösterir. İlerleme çubuğu
  yalnızca pipeline gerçek bir sayaç verdiğinde (ör. "12/40 segment" ya da
  sayfa sayısı) çizilir; sayacı olmayan aşamada çubuk yoktur.
- **İptal:** Panelde "İptal" düğmesi vardır; iş ve açtığı alt süreçler durdurulur.
- **Başarılı sonuç:** "PDF hazır" iletisi, "PDF'yi aç" ve "İndir" düğmeleri ve
  PDF'nin dosya yolu.
- **Kalite eşiği altında:** "PDF üretildi ama kalite denetimi eşiğin altında
  kaldı" iletisi çıkar. PDF yine açılabilir; ayrıntı kitap klasöründeki
  `qc_report.txt` dosyasındadır. TeX yolunda sık neden, çeviride bir başlığın
  ya da ortamın düşmesi ya da metnin bir kısmının çevrilmeden kalmasıdır.
- **Hata:** Hata iletisi ve "Son çıktı" bölümünde işin son satırları
  gösterilir. Tam kayıt kitap klasöründeki `pipeline.log` dosyasındadır.
  Seçili çeviri modeli kullanılamadıysa (çıkış kodu 5) ileti bunu söyler ve
  **"Modelleri tara"** ile güncel listeye bakmayı önerir.

## 8. Araçlar

- **Terimler:** Seçili kitabın kendi sözlüğünü (kitap klasöründeki
  `glossary_shared.tsv`; bkz. Bölüm 8.1) gösterir. Kaynak dildeki terimde ya
  da Türkçe karşılıkta arar; tam eşleşme yoksa en yakın terimi benzerlik
  oranıyla önerir. Sözlükteki terim sayısı da yazılır. Kitap seçilmemişse ya
  da kitabın sözlüğü henüz yoksa sekme bunu söyler.
- **Bellek:** Çeviri belleğindeki kayıt ve onaylı kayıt sayısını gösterir;
  yazdığınız metne benzeyen kayıtları (en çok beş) kaynak ve çevirisiyle
  listeler. Bu sekme belleğe yalnızca bakar, bir şey değiştirmez.
- **Karşılaştırma:** Orijinal ve çevrilmiş PDF'yi yükleyip "Karşılaştır"a
  basınca iki PDF'yi sayfa sayfa piksel düzeyinde karşılaştırır:
  karşılaştırılan sayfa sayısı, ortalama fark ve sorunlu sayfaları gösterir,
  HTML raporu indirmenizi sağlar. "Karşılaştırma ayarları"ndan sayfa sınırı
  (varsayılan 50 ya da tüm sayfalar), DPI (72–300, varsayılan 150) ve
  görsellerin rapora eklenip eklenmeyeceği seçilir.

### 8.1. Kitap sözlüğü

Terim tutarlılığı her kitabın kendi sözlüğüyle sağlanır; bütün kitaplara
uygulanan sabit bir genel alan sözlüğü yoktur. Sözlük kitap klasöründeki
`glossary_shared.tsv` dosyasıdır: UTF-8, her satır `kaynak terim<TAB>Türkçe
karşılık` (isteğe bağlı üçüncü sütun not); kaynak terim kitabın kendi
dilindedir. İki yol aynı biçimi kullanır.

- **PDF'den TeX çıkar:** Sözlüğü çeviriyi yapan çıkarır. Yapay zekâ asistanı,
  kitaba özel `CEVIRI_TALIMATI.md` gereği çeviriden önce kitabın tekrar eden
  teknik terimlerini (50–100 terim) bu dosyaya yazar ve bütün parçalarda
  kullanır. Kendiniz çeviriyorsanız aynı dosyayı siz de tutabilirsiniz.
- **Yerel model ile çevir:** Kitap klasöründe sözlük yoksa uygulama, çeviri
  bittikten sonra kitabın sık geçen terimlerini kaynak dilde (altı dilin
  hepsinde) çıkarır, her terimi bir kez, geçtiği cümleyle birlikte seçili
  modele çevirtir ve dosyaya yazar. Kaynak dili İngilizce olan kitapta, elle
  doğrulanmış başlangıç terimlerinden (`glossary/curated.json`) kitapta
  geçenler önce gelir. Ardından çeviri bu sözlükle denetlenir ("Sözlük
  geçişi" fazı): çıktıda özgün dilinde kalmış terim sözlükteki karşılığıyla
  değiştirilir; aynı terimin farklı çevrildiği yerler kalite raporunda düşük
  önemli uyarı olarak görünür. Model terimleri çeviremezse sözlük yazılmaz,
  bir sonraki çeviride yeniden denenir.
- **Var olan sözlük korunur:** Dosya zaten varsa (sizin, TeX yolunun ya da
  önceki bir çevirinin yazdığı) üzerine yazılmaz, olduğu gibi kullanılır.
  Sözlüğü elle düzeltirseniz sonraki çeviride sözlük geçişi ve kalite
  denetimi düzelttiğiniz karşılıkları kullanır.

## 9. Komut satırı (kaynak koddan kurulumda)

Arayüz, işleri aşağıdaki komut satırı betiklerini ayrı bir süreçte çalıştırarak
yapar; kaynak koddan kurulumda aynı işler doğrudan terminalden de yapılabilir.
Sanal ortam etkin olmalıdır (`source venv/bin/activate`).

### 9.1. PDF'den TeX çıkar

```bash
python 1_cikar.py kitap.pdf                          # → output/<ad>/EXPORT.tex
python 1_cikar.py kitap.epub --name "Kitap Adı"      # çıktı klasörüne ad vermek için
# EXPORT.tex'i çevirin → aynı klasöre EXPORT_TR.tex olarak kaydedin
python 2_derle.py output/<ad>/EXPORT_TR.tex          # → output/<ad>/<ad>_TR.pdf
```

`1_cikar.py`, `EXPORT.tex`'in yanına bir `kitap.json` kimlik dosyası ve o
kitabın yollarıyla doldurulmuş bir `CEVIRI_TALIMATI.md` kopyası yazar.
Aynı adlı klasörde farklı bir kitap varsa durur ve `--name` ile farklı bir ad
vermenizi ister.

`2_derle.py` seçenekleri:

| Seçenek | Açıklama |
|---|---|
| `--output-dir output/<ad>` | Kimlik satırları silinmiş ya da dosya taşınmışsa kitabın klasörünü elle gösterir |
| `--strict` | Bölüm sayısı kaynakla uyuşmazsa uyarı yerine hata verir |
| `--no-epub-output` | EPUB kaynaklı kitapta Türkçe EPUB üretmez (varsayılan: üretir) |

Aynı akışın `translate_book.py` karşılığı: `--export-tex` (çıkar) ve
`--import-tex EXPORT_TR.tex` (çeviriyi al ve derle). `1_cikar.py` ile
`2_derle.py` bunları sarar.

### 9.2. Yerel model ile çevir

```bash
python translate_book.py kitap.pdf        # PDF → Türkçe PDF
python translate_book.py kitap.epub       # EPUB → Türkçe PDF (+ Türkçe EPUB)
```

Sık kullanılan seçenekler:

| Seçenek | Açıklama |
|---|---|
| `--name "Kitap Adı"` | Çıktı klasörünün ve PDF'nin adı (varsayılan: dosya adından) |
| `--output-dir DİZİN` | Çıktı klasörü (varsayılan: `output/<ad>/`) |
| `--book-type {science,philosophy,novel}` | Kitap türü (`science`: bilim ve teknik, `philosophy`: felsefe ve kişisel gelişim, `novel`: roman); türe göre kuralları (ör. kalite denetimi) etkiler (varsayılan: kendiliğinden tespit) |
| `--resume` | Çökme sonrasında kaldığı yerden devam eder |
| `--skip-qc` | Kalite denetimi ve AutoFix fazını atlar (önerilmez; kitap sözlüğü de çıkarılmaz) |
| `--extract-only` | Yalnızca çıkarım yapar |
| `--skip-compile` | Yalnızca çevrilmiş `.tex` üretir, PDF derlemez |
| `--no-epub-output` | EPUB kaynaklı kitapta Türkçe EPUB üretmez |
| `--batch dosya1 dosya2 ...` | Birden çok kitabı sırayla işler |
| `--mt-engine KİMLİK` | Çeviri modelini seçer (varsayılan `hymt-7b`). Biçimler: `mlx:<klasör>`, `gguf:<dosya>`, `ollama:<model>`, `lmstudio:<model>`, `openai:<adres>\|<model>`; ayrıntı `docs/YENI_MODEL_EKLEME.md`. Arayüzün son seçimini okumaz |
| `--dil-kontrolu-yok` | Kaynak dilin iki kapısını birden (çıkarımdan önceki ön kapı ve çıkarımdan sonraki tam kapı) atlar ve kaynağı İngilizce varsayar. Yalnızca dil tespiti yanlışsa, yani kitabın dili aslında destekleniyorken kullanılır (başka bir dil için `--kaynak-dil`); arayüzde yoktur (bkz. Bölüm 6.1) |
| `--kaynak-dil KOD` | Kitabın kaynak dilini elle verir: `en`, `de`, `ru`, `fr`, `es` ya da `it`. İki dil kapısını da atlar; arayüzdeki karşılığı kartın **Kaynak dil** listesidir (bkz. Bölüm 6.1) |

Model seçerek çeviri ve model taraması:

```bash
python translate_book.py kitap.pdf --mt-engine ollama:qwen3:8b
python translate_book.py kitap.pdf --mt-engine 'openai:http://127.0.0.1:8080/v1|qwen'   # | kabukta boru işareti: tırnak şart
python -m core.mt_engines.discovery          # bulunan modeller ve sunucuların durumu (--json: makinece okunur)
```

Seçili model kullanılamıyorsa (kimlik geçersiz, sunucu kapalı, model yok…)
ya da bu Mac'in belleği yetmiyorsa (Bölüm 1.1) `translate_book.py` bunu
çıkarımdan **önce** denetler ve çıkış kodu 5 ile durur; uzun PDF çıkarımı
boşuna beklenmez. Bellek kapısında ileti arayüzdekiyle aynıdır ve altında
model gerektirmeyen TeX yolu önerilir.

`translate_book.py` çıkış kodları:

| Kod | Anlamı |
|---|---|
| 0 | Başarı |
| 1 | Hata (çıkarım, çeviri ya da ön kontrol) |
| 2 | Derleme başarısız; ya da kitap TeX yoluyla çevrildiği için yerel model çeviriyi reddetti |
| 3 | Kalite denetimi eşiğin altında (PDF yine üretilir) |
| 4 | Kaynak dil bu yolda desteklenmiyor: dil kapısı işi çeviri başlamadan durdurdu (Bölüm 6.1) |
| 5 | Seçili çeviri modeli kullanılamıyor: kimlik geçersiz, klasör ya da dosya yok, sunucu kapalı, model sunucuda yok, llama-cpp-python kurulu değil, bu Mac'in belleği yerel model için yetersiz ya da model bu Mac'e sığmıyor (Bölüm 1.1), çeviri sırasında sunucu bağlantısı koptu (Bölüm 6.2) ya da düşünen model parçaların çoğunda cevap üretemedi (Bölüm 6.2) |
| 130 | Kullanıcı kesti (`Ctrl+C`) |

## 10. Kitap klasörü

Her kitap kendi klasöründe toplanır: kaynak koddan kurulumda
`output/<kitap_adı>/` (`output/` git dışıdır), DMG kurulumunda
`~/Library/Application Support/Kitapçevir/veri/output/<kitap_adı>/`
(Bölüm 2.5).

```
<kitap_adı>/
├── source_book.pdf | .epub   kaynak kitabın kopyası
├── full_book.md              çıkarım sonucu (Markdown)
├── EXPORT.tex                çevrilecek LaTeX (TeX yolu)
├── EXPORT_TR.tex             sizin kaydettiğiniz çeviri (TeX yolu)
├── kitap.json                kitap kimliği (TeX yolu)
├── CEVIRI_TALIMATI.md        bu kitaba özel çeviri talimatı (TeX yolu)
├── glossary_shared.tsv       kitap sözlüğü (Bölüm 8.1)
├── source/                   ara LaTeX dosyaları ve görseller
├── compiled/                 derlenmiş PDF
├── <kitap_adı>_TR.pdf        Türkçe PDF (TeX yolu)
├── qc_report.txt             kalite denetimi raporu
└── pipeline.log              ayrıntılı iş kaydı
```

## 11. Sorun giderme

| Belirti | Çözüm |
|---|---|
| macOS uygulamayı açmıyor (tanınmayan geliştirici uyarısı) | Uygulama imzasızdır; Bölüm 2.2'deki adımları izleyin: Finder'da sağ tık → Aç; macOS 15 ve sonrasında Sistem Ayarları → Gizlilik ve Güvenlik → Yine de Aç. |
| Açılış ekranında "Kitapçevir'i Uygulamalar klasörüne taşıyıp oradan açman önerilir." | Uygulama DMG'nin içinden ya da taşınmadan açıldı. Uygulamayı Uygulamalar klasörüne sürükleyip oradan açın. |
| Sihirbaz: "Datalab model lisansı kabul edilmeden kurulum sürdürülemez" | PDF desteği zorunlu bileşendir ve modelleri olmadan çalışmaz. Kurulumu sürdürmek için lisansı okuyup kabul edin. |
| Sihirbaz: "Boş disk: … Yetmiyor: … daha gerekiyor." | Yer açın ya da isteğe bağlı bileşenleri (ör. HY-MT) seçmeden kurun; onları sonra Bileşenler ekranından ekleyebilirsiniz. |
| Sihirbaz: "Bağlantı yok." | İnternet bağlantısını açıp "Yeniden dene"ye basın. Yarım inen dosyalar saklanır. |
| Sihirbazda HY-MT seçilemiyor ("Bu Mac yerel model için yetersiz" ya da "bu Mac'e sığmaz") | Bu Mac'in belleği HY-MT'ye yetmiyor (Bölüm 1.1). "PDF'den TeX çıkar" yolunu kullanın. |
| "Kurulum başlatılamadı" ekranı | Ekrandaki iletiyi ve günlüğü okuyun ("Göster" günlük dosyasını açar). Günlükler `~/Library/Logs/Kitapçevir/` altındadır. |
| Arayüz "yalnızca macOS'ta çalışır" ya da "Apple Silicon (M serisi) bir Mac ister" yazıp duruyor | Kitapçevir yalnızca Apple Silicon Mac'te çalışır. Kaynak koddan kurulumda Apple Silicon Mac'te bile bu ileti çıkıyorsa Python Rosetta altında (x86_64) çalışıyordur; sanal ortamı arm64 Python ile yeniden kurun. |
| "Çevir" düğmesi soluk, seçili model "kullanılamaz" | Nedeni kartta yazar. HY-MT kurulu değilse DMG kurulumunda Bileşenler ekranından indirin; kaynak koddan kurulumda Bölüm 2.6'daki `mlx_lm.convert` komutunu çalıştırın. Diğer modeller için `docs/YENI_MODEL_EKLEME.md`, Bölüm 8. |
| Ollama ya da LM Studio modeli listede yok | Sunucu kapalıdır; kartta durumu yazar. Ollama uygulamasını açın (ya da `ollama serve`) veya LM Studio'da yerel sunucuyu başlatın (`lms server start`), sonra "Modelleri tara". LM Studio'da model yüklü değilse ve isteğe bağlı yükleme (Just-in-Time) kapalıysa model listelenmez. Sunucu varsayılandan farklı bir portta çalışıyorsa adresi `config/mt_sources.json`'a yazın (`docs/YENI_MODEL_EKLEME.md`, Bölüm 5.1). |
| Başka klasördeki bir model (ör. LM Studio'nun indirdiği) listede yok | Yalnızca `models/` taranır; `models/` altına sembolik bağ koyun: `ln -s <asıl klasör> models/<ad>` (DMG kurulumunda hedef klasör `~/Library/Application Support/Kitapçevir/veri/models/`). |
| GGUF dosyası "kullanılamaz", kurulum komutu yazıyor | llama-cpp-python kurulu değil (isteğe bağlıdır). DMG kurulumunda Bileşenler ekranından GGUF desteğini indirin; kaynak koddan kurulumda Bölüm 2.6'daki komutla kurun ya da dosyayı Ollama / LM Studio üzerinden kullanın. |
| Çeviri çıkış kodu 5 ile bitti, "Seçili model kullanılamadı; nedeni aşağıda." | Model çeviriden önce ya da çeviri sırasında kullanılamaz hâle geldi (sunucu kapalı, model yok, bağlantı koptu). Nedeni iletinin altında ve `pipeline.log`'da yazar; sorunu giderip "Modelleri tara" ile yeniden deneyin. |
| "Bu model düşünmesini bitirip çeviri üretemiyor: …" (çıkış kodu 5) | Düşünmesi kapatılamayan model parçaların çoğunda düşünme payını doldurup cevap vermedi (Bölüm 6.2). İletide önerilen modeli (HY-MT 1.5 7B) ya da düşünmeyen başka bir model seçin. |
| "Yalnız bu bilgisayardaki sunuculara bağlanılır; …" | Başka bir bilgisayardaki sunucu desteklenmez; adres `127.0.0.1`, `localhost` ya da `::1` olmalı. |
| "2 · Çevrilmiş TeX'i derle" soluk | Çeviri, `EXPORT.tex` ile aynı klasörde tam olarak `EXPORT_TR.tex` adıyla durmalıdır. Dosya kaydedildikten sonra düğme birkaç saniye içinde kendiliğinden açılır; açılmazsa sol kenardaki **Kitap adı**nın TeX'i çıkarırken kullandığınızla aynı olduğuna bakın. |
| Kitap yüklenmiyor, boyut sınırı iletisi çıkıyor | Yükleme sınırı 500 MB'tır. Daha büyük kitaplar için kaynak koddan kurulumda komut satırını kullanın. |
| `xelatex: command not found` (kaynak koddan kurulum) | MacTeX'i kurun (Bölüm 2.6) ve yeni bir terminal açın. |
| Derleme: "hangi kitaba ait olduğu çözülemedi" | Çevrilmiş dosyada `% KITAPCEVIR-META` satırları silinmiş olabilir. `2_derle.py`'ye `--output-dir output/<ad>` verin. |
| Derleme: "full_book.md bulunamadı" | Önce TeX'i çıkarın (arayüzde "1 · TeX'i çıkar" ya da `1_cikar.py`, aynı kitap adıyla). |
| Derleme: "UTF-8 değil" | Dosyayı UTF-8 olarak kaydedin (VS Code: sağ alttaki kodlama → "Save with Encoding" → UTF-8). |
| "FARKLI bir kitap var" | Aynı adlı çıktı klasöründe başka bir kitap var. Sol kenarda (ya da `--name` ile) farklı bir kitap adı verin. |
| "Yerel model bu kitabı çevirmedi: kitabın dili desteklenmiyor" (çıkış kodu 4) | Kitabın dili desteklenen dillerden biri değilse bu beklenen davranıştır. Dil aslında destekleniyorsa tespit yanlıştır; kartın **Kaynak dil** listesinden doğru dili seçip yeniden çevirin (komut satırında `python translate_book.py kitap.pdf --kaynak-dil de`, kitabın dilinin koduyla; bkz. Bölüm 6.1). |
| Yerel model, TeX yoluyla derlenmiş bir kitabı çevirmeyi reddediyor | Bu bilinçli bir korumadır: yerel model, önceki çevirinizin üzerine yazmaz. Kitabı model ile de çevirmek istiyorsanız sol kenarda farklı bir kitap adı verin. |
| İlk PDF çok yavaş (kaynak koddan kurulum) | Marker modelleri ilk seferde iniyor (yaklaşık 3,5 GB); sonraki kitaplarda bu indirme yapılmaz. DMG kurulumunda modeller kurulum sırasında iner. |
| `[Dictionary] Sözlük bulunamadı` uyarısı | Zararsızdır; `data/dictionary/` isteğe bağlıdır. |
| Çeviri sırasında bellek baskısı | Diğer uygulamaları kapatın, Ollama / LM Studio'da yüklü kullanılmayan modelleri boşaltın; bellek gereksinimi için Bölüm 1.1'e bakın. |
| "Bu Mac yerel model için yetersiz: en az … GB kullanılabilir bellek gerekir, bu Mac'te … GB." ("Yerel model ile çevir" kartı kapalı) | Bu Mac 4 bit bir 7B modeli bile rahat çalıştıramaz (Bölüm 1.1). Model gerektirmeyen "PDF'den TeX çıkar" yolunu kullanın. |
| Model listede "bu Mac'e sığmaz (gerekli ~… GB, kullanılabilir … GB)" | Modelin tahmini gereksinimi bu Mac'in kullanılabilir belleğini aşıyor. Daha küçük ya da daha çok nicemlenmiş (ör. 4 bit) bir model seçin (`docs/YENI_MODEL_EKLEME.md`). |

## 12. Testler (geliştiriciler için)

```bash
pip install pytest                  # test aracı requirements.txt'te yok
python -m pytest tests/ -q          # tam takım; çeviri modeli gerektirmez
```

Gerçek çeviri modeliyle çalışan entegrasyon testleri varsayılan koşuda
atlanır; `python -m pytest -m integration` ile ayrıca çalıştırılır (model
gerekir). Ollama, LM Studio ve GGUF katmanının testleri sahte bir sunucu ve
sahte bir llama-cpp-python modülüyle çalışır, gerçek sunucuya gitmez. Gerçek
bir modeli yükleyen testler `yerel_model` işaretlidir ve varsayılan koşuda
seçilmez (`python -m pytest -m yerel_model`). Kurulum sihirbazının testleri
`packaging/testler/` altındadır.

## 13. Lisanslar

Kitapçevir GNU GPL v3.0 ile dağıtılır (`LICENSE`). Kurulumda indirilen ya da
kullanılan başlıca üçüncü taraf bileşenler kendi lisanslarıyla gelir.
Koşullar burada özetlenmez; bağlayıcı olan, aşağıda bağlantısı verilen resmî
lisans metinleridir:

- **Marker ve Surya** (marker-pdf 1.10.2, surya-ocr 0.17.1): kod
  GPL-3.0-or-later; model ağırlıkları Datalab'ın "AI Pubs Open RAIL-M
  (değiştirilmiş)" lisansıyla sunulur. Metinler:
  https://github.com/datalab-to/marker (`LICENSE`, `MODEL_LICENSE`) ve
  https://github.com/datalab-to/surya.
- **PyMuPDF** (GNU AGPL-3.0 ya da Artifex ticari lisansı) ve **EbookLib**
  (GNU AGPL-3.0-or-later). Kaynak kodları ve lisans metinleri:
  https://github.com/pymupdf/PyMuPDF ve https://github.com/aerkalov/ebooklib.
- **HY-MT 1.5 7B** (isteğe bağlı): Tencent HY Community License; metin:
  https://huggingface.co/tencent/HY-MT1.5-7B (`License.txt`). Kitapçevir
  bağımsız bir projedir; Tencent ile bağlantılı değildir, Tencent tarafından
  desteklenmez ya da onaylanmaz.
- **TeX:** tectonic (MIT; https://github.com/tectonic-typesetting/tectonic);
  tectonic'in indirdiği TeX paketleri ve MacTeX, TeX Live bileşenlerinin
  kendi özgür lisanslarıyla gelir.

Bileşenler ekranındaki **Lisanslar** bağlantısı uygulamanın lisans klasörünü
açar: Kitapçevir'in GPL metni, kaynak kodu bağlantıları, Datalab ve Tencent
lisanslarının resmî metinleri, DMG'deki Python'un (python-build-standalone;
içine derlenen OpenSSL, SQLite, Tcl/Tk gibi kitaplıklar dahil) ve kurulumda
inen uv, tectonic ve GGUF bileşenlerinin kendi lisans dosyaları; `00_OKU.txt`
hepsini sıralar. Kurulan Python paketlerinin lisansları kendi `*.dist-info`
klasörlerindedir.

---

> **Telif notu:** Bu depo yalnızca pipeline kodunu içerir. Kitap dosyaları ve
> çeviri çıktıları depoya eklenmez; üretilen çeviriler yalnızca kişisel kullanım
> içindir (FSEK m. 38).
