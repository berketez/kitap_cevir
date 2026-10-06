"""Yöneticinin paylaşılan sabitleri: TEK KAYNAK (TASARIM I.2).

Birden çok modülün (kabuk sözleşmesi, indirici, sunucu, günlük) kullandığı
sayılar yalnız burada durur; başka dosyada aynı sayı yazılmaz, buradan içe
aktarılır (CLAUDE.md §11).

Buradaki değerler tasarım varsayımıdır, ölçüm DEĞİLDİR. Saha dönütüyle yalnız
bu dosya değişir. Ölçülen sayılar (boyutlar, SHA-256'lar) manifesttedir.

Bu modül yalnız stdlib'le çalışır ve hiçbir şey içe aktarmaz: DMG'deki
taşınabilir Python'la (python-build-standalone) ve komut satırından, venv'e
bağımlı olmadan yüklenebilir (TASARIM A.2, B.3).
"""

# ---------------------------------------------------------------------------
# Kabuk ↔ yönetici sözleşmesi (TASARIM B.4, B.6)
# ---------------------------------------------------------------------------
# Kapanışta kabuğun yöneticiyi bekleyeceği süre: Streamlit'in iş iptali
# (ui.runner CANCEL_GRACE_SEC = 10 sn) + pay.
KAPANIS_SURESI_SN = 25
# Yöneticinin ilk JSON satırını basması için kabuğun beklediği en uzun süre.
YONETICI_HAZIR_ZAMAN_ASIMI_SN = 30
# Kilit (durum/kilit) başka örnekteyse yöneticinin çıkış kodu.
KILIT_DOLU_CIKIS_KODU = 10
# Süreç başına üretilen API anahtarının bayt uzunluğu (X-Kitapcevir-Anahtar).
ANAHTAR_BAYT = 32

# ---------------------------------------------------------------------------
# İndirici (TASARIM D.2)
# ---------------------------------------------------------------------------
# İptal bayrağına bakılan en büyük parça; okuma bu boyutu aşmaz.
PARCA_BAYT = 1 << 20
# Eşzamanlı indirme iş parçacıkları: küçük öğeler (tekerlekler) ve büyük
# öğeler (model dosyaları, MacTeX) ayrı havuzlarda iner.
IS_PARCACIGI_KUCUK = 4
IS_PARCACIGI_BUYUK = 2
# Bu boyuttan (bayt) büyük ya da eşit öğe "büyük" havuza gider.
BUYUK_OGE_ESIGI_BAYT = 100_000_000
# Bağlantı hatası / 5xx / 429 sonrası en çok deneme sayısı. Gerçek bayt
# ilerlemesi olan her denemeden sonra sayaç sıfırlanır.
YENIDEN_DENEME = 5
# Üstel bekleme: BEKLEME_TABANI_SN × 2^(deneme-1), en çok BEKLEME_TAVANI_SN.
BEKLEME_TABANI_SN = 2.0
# Hız, son bu kadar saniyede inen gerçek baytlardan ölçülür (kayan pencere).
HIZ_PENCERESI_SN = 5
# Çevrimdışı algılama: manifestteki ilk URL'ye atılan HEAD'in zaman aşımı.
CEVRIMDISI_ZAMAN_ASIMI_SN = 5
# MacTeX kurulumu sürerken /Library/TeX/texbin/xelatex'e bakma aralığı.
MACTEX_YOKLAMA_SN = 5

# ---------------------------------------------------------------------------
# Disk (TASARIM D.2-8)
# ---------------------------------------------------------------------------
# Plan toplamına eklenen güvenlik payı.
DISK_PAYI_BAYT = 1_000_000_000

# ---------------------------------------------------------------------------
# Günlük (TASARIM B.6): RotatingFileHandler
# ---------------------------------------------------------------------------
GUNLUK_BOYUT_BAYT = 5_000_000
GUNLUK_ADET = 3

# ---------------------------------------------------------------------------
# Depo, güncelleme denetimi ve "Kaynak kodu" bağlantısı (AGPL m.13)
# ---------------------------------------------------------------------------
# TEK YER: sürümler başka depoya taşınırsa YALNIZ bu satır değişir (arayüzün
# "Kaynak kodu" bağlantısı ui/branding.py SOURCE_CODE_URL'de aynı adresi taşır;
# eşitliği tests/test_K6.py denetler). Manifest bu URL'yi TAŞIMAZ (iki kaynak
# olmasın); GGUF tekerleğinin sürüm varlığı adresi sürüm betiğinde buradan türer.
# Açık depo (Berke, 5 Ekim 2026): geçmişsiz anlık görüntüyle açılan YENİ depo
# (KARAR 35). Paket kimliği io.github.berketez.kitapcevir bundan bağımsızdır,
# değişmez (Info.plist; WebKit/Caches klasör adları ona bağlı).
DEPO = "berketez/kitap_cevir"
KAYNAK_KODU_URL = "https://github.com/" + DEPO
SURUMLER_SAYFASI_URL = KAYNAK_KODU_URL + "/releases"
GUNCELLEME_API_URL = "https://api.github.com/repos/" + DEPO + "/releases/latest"
# Güncelleme denetimi isteğinin zaman aşımı (yalnız "Denetle" düğmesiyle).
GUNCELLEME_ZAMAN_ASIMI_SN = 10

# ---------------------------------------------------------------------------
# K2 iç sabitleri (tasarım listesinde yok; K2 kulvarının varsayımları)
# ---------------------------------------------------------------------------
# Üstel beklemenin üst sınırı.
BEKLEME_TAVANI_SN = 60.0
# Soket işlemi zaman aşımı: bu süre veri gelmezse bağlantı kopmuş sayılır ve
# kaldığı yerden yeniden denenir.
BAGLANTI_ZAMAN_ASIMI_SN = 30
# İmzalı CDN bağlantısı 403/410 dönünce manifest URL'si yeniden çözümlenir;
# üst üste en çok bu kadar (ilerleme olunca sıfırlanır).
YENIDEN_COZUMLEME_EN_COK = 3
# Bir istekte izlenen en çok yönlendirme.
YONLENDIRME_EN_COK = 10
# SHA uyuşmazsa .part silinip aynı URL'den en çok bu kadar kez baştan inilir
# (TASARIM D.2: "1 kez baştan").
SHA_UYUSMADI_BASTAN_DENEME = 1
# 429'da RateLimit/Retry-After başlığındaki süreye uyulur; bozuk ya da aşırı
# büyük değere karşı üst sınır (HF penceresi 300 sn; iki katı).
HIZ_SINIRI_BEKLEME_TAVANI_SN = 600
# Hız ancak en az bu kadar süre ölçüldükten sonra gösterilir; daha kısa
# sürede "—" (bilinmiyor) döner.
HIZ_OLCUM_EN_AZ_SN = 1.0
# Aynı öğe için iki ilerleme olayı arasındaki en kısa süre (SSE akışı
# boğulmasın). Öğe başı ve sonu her zaman bildirilir.
ILERLEME_OLAY_ARALIGI_SN = 0.25
# Beklemelerde (geri sayım) iptal bayrağına bakma aralığı.
IPTAL_YOKLAMA_SN = 0.2
# İstek kimliği: kişisel veri içermez (TASARIM D.2-11).
KULLANICI_ARACISI_SABLONU = "Kitapcevir-kurulum/{surum}"
# .part dosya adına eklenen SHA-256 önekinin uzunluğu: içeriği farklı iki
# öğe (ör. sürüm değişince aynı kimlik) aynı .part'ı paylaşmasın.
PART_OZET_ONEKI = 16
# .part dosya adında öğe kimliğinden alınan en çok karakter.
PART_AD_EN_COK = 120
# Güncelleme denetimi yanıtından okunacak en çok bayt (GitHub sürüm JSON'u).
GUNCELLEME_YANIT_EN_COK_BAYT = 1_048_576
# Anahtar zincirinden kök sertifikaları dışa aktarma komutunun zaman aşımı
# (TASARIM D.2-10; yalnız TLS hatasında ve kullanıcı onayıyla çalışır).
CA_DISA_AKTARMA_ZAMAN_ASIMI_SN = 30

# ---------------------------------------------------------------------------
# Kök altındaki adlar (TASARIM B.8) ve manifest
# ---------------------------------------------------------------------------
# .part dosyaları + tekerlek deposu; kurulum sonunda silinir. Manifestte
# hedefi bu klasörle başlayan öğe geçicidir (disk ön denetiminde "tepe").
# K3'ün durum.py'si (KokDuzeni) bu adı buradan içe aktarır (tek kaynak).
INDIRILEN_DIZINI = "indirilen"
PART_UZANTISI = ".part"
# Manifest şemasının sürümü (packaging/manifest.schema.json "sema_surumu").
MANIFEST_SEMA_SURUMU = 1
# Manifest boyut sözlüklerinde bütün macOS sürümleri için ortak anahtar.
BOYUT_ORTAK_ANAHTAR = "hepsi"
