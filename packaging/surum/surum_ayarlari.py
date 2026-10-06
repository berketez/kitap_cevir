"""Sürüm betiğinin sabitleri: TEK KAYNAK (packaging/TASARIM.md G.1, C.1).

Bu dosya yalnız sürüm çıkaran makinede kullanılır; uygulama arşivine girmez
(``DISLANANLAR``). Sürümler burada SABİTLENİR: sürüm betiği çalışma anında
"en yeni sürüm" sorgusu yapmaz (lisans raporu K2). Bir bileşeni yükseltmek =
yalnız bu dosyada bir satır değişir; boyutları ve SHA-256'ları sürüm betiği
yeniden ÖLÇER, buraya sayı yazılmaz.

Başka yerde tek kaynağı olan değerler burada YAZILMAZ, oradan okunur:

- depo / kaynak kodu / güncelleme adresi: ``packaging/yonetici/ayarlar.py``
  (``DEPO``, ``KAYNAK_KODU_URL``); GGUF tekerleğinin sürüm varlığı adresi de
  oradan türetilir;
- en düşük macOS: ``packaging/kabuk/derle.sh`` (``MIN_MACOS``; ``--sabitler``);
- paket sürümleri: ``packaging/requirements/*.txt`` kilitleri;
- Surya/Marker model klasörleri ve yazıtipi adresi: kilitteki ``surya-ocr`` ve
  ``marker-pdf`` tekerleklerinin ``settings.py`` dosyaları;
- ekrandaki adlar: ``ui/branding.py``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Gömülü Python: python-build-standalone (DMG'ye girer; TASARIM A.2, B.1)
# ---------------------------------------------------------------------------
PBS_YAYIN_ETIKETI = "20260924"
PBS_PYTHON = "3.12.14"
PBS_ARSIV_ADI = f"cpython-{PBS_PYTHON}+{PBS_YAYIN_ETIKETI}-aarch64-apple-darwin-install_only.tar.gz"
PBS_SURUM_KOKU = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_YAYIN_ETIKETI}"
# GitHub adresinde "+" %2B diye kodlanır (yönlendirme ve CDN ikisini de kabul ediyor).
PBS_URL = f"{PBS_SURUM_KOKU}/{PBS_ARSIV_ADI.replace('+', '%2B')}"
PBS_OZET_URL = f"{PBS_SURUM_KOKU}/SHA256SUMS"

# ---------------------------------------------------------------------------
# Çekirdek: uv (yalnız çevrimdışı kurar; TASARIM D.1)
# ---------------------------------------------------------------------------
UV_SURUMU = "0.12.20"
UV_ARSIV_ADI = "uv-aarch64-apple-darwin.tar.gz"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_SURUMU}/{UV_ARSIV_ADI}"
UV_OZET_URL = UV_URL + ".sha256"
UV_UYESI = "uv-aarch64-apple-darwin/uv"          # arşivdeki ikili (adimlar/arsiv.py UV_UYESI)

# ---------------------------------------------------------------------------
# Hafif TeX: tectonic (MIT)
# ---------------------------------------------------------------------------
TECTONIC_SURUMU = "0.17.0"
TECTONIC_ARSIV_ADI = f"tectonic-{TECTONIC_SURUMU}-aarch64-apple-darwin.tar.gz"
TECTONIC_ETIKETI = f"tectonic%40{TECTONIC_SURUMU}"
TECTONIC_URL = ("https://github.com/tectonic-typesetting/tectonic/releases/download/"
                f"{TECTONIC_ETIKETI}/{TECTONIC_ARSIV_ADI}")
# GitHub sürüm API'si varlık başına "digest" (sha256:…) veriyor; kendi ölçümümüzle karşılaştırılır.
TECTONIC_API_URL = ("https://api.github.com/repos/tectonic-typesetting/tectonic/releases/tags/"
                    f"{TECTONIC_ETIKETI}")
TECTONIC_KAYNAK_URL = ("https://github.com/tectonic-typesetting/tectonic/releases/tag/"
                       f"{TECTONIC_ETIKETI}")

# ---------------------------------------------------------------------------
# Tam TeX: MacTeX (TUG; yıllık değişir, özet aynı yansıdaki .sha512'den okunur)
# ---------------------------------------------------------------------------
MACTEX_URL = "https://mirror.ctan.org/systems/mac/mactex/MacTeX.pkg"
MACTEX_SHA512_URL = MACTEX_URL + ".sha512"
# TUG'un resmî indirme sayfası paketin SHA-512'sini yazar. 5 Ekim 2026 ölçümü: TUG 24 Mart
# 2026'da MacTeX-2026 paketini değiştirdi; CTAN yansılarındaki .sha512/.md5 dosyaları ESKİ
# paketin (2 Mart) özetini taşıyor, yeni paketle uyuşmuyor. Bu yüzden sürüm betiği özeti
# önce bu sayfayla doğrular, manifest de MacTeX'i ölçülen SHA-256'ya sabitler (sha512_url
# yazılmaz: kurulum yansının bayat .sha512'sine bakıp her kullanıcıda düşerdi).
MACTEX_TUG_SAYFASI = "https://tug.org/mactex/mactex-download.html"
MACTEX_XELATEX = "/Library/TeX/texbin/xelatex"   # adimlar/tex.py TEXBIN_XELATEX ile aynı yol

# ---------------------------------------------------------------------------
# HY-MT 1.5 7B (isteğe bağlı, ÖNERİLEN yerel model; KARAR 29). Ağırlıklar ÖLÇÜM
# İÇİN İNDİRİLMEZ: boyut ve LFS SHA-256'ları HF ağacından gelir (TASARIM G.1-7).
# ---------------------------------------------------------------------------
HYMT_DEPO = "mlx-community/HY-MT1.5-7B-bf16"
HYMT_COMMIT = "5c83644a4a3d4dc0e2b2498e42556ffeafdccc1a"
HYMT_KLASORU = "veri/models/hymt-7b-mlx"       # lisans K6: klasör adı korunur
TENCENT_LISANS_DEPO = "tencent/HY-MT1.5-7B"
TENCENT_LISANS_COMMIT = "397085d46158ad0b437ffd74228499e8c2a35c76"
TENCENT_LISANS_DOSYASI = "License.txt"
HF_KOKU = "https://huggingface.co"
# bf16 ağırlık: KV önbelleğinde öğe başına bayt (core.hardware formülünün girdisi).
HYMT_KV_OGE_BAYT = 2
# yeniden_kullan: aynı SHA'lı dosyanın aranacağı BİLİNEN yollar; tarama yapılmaz
# (TASARIM C.3, KARARLAR 1). İlki Hugging Face önbelleğinin standart yeri; sonraki
# ikisi geliştirici kurulumunun models/ klasörü (proje ~/kitapcevir'de).
HYMT_YENIDEN_KULLAN = (
    f"~/.cache/huggingface/hub/models--{HYMT_DEPO.replace('/', '--')}/snapshots/{HYMT_COMMIT}",
    "~/kitapcevir/models/hymt-7b-mlx",
    "~/kitapcevir/models/hymt-7b-mlx.nosync",
)

# ---------------------------------------------------------------------------
# Surya/Marker modelleri (PDF desteği). Klasör listesi ve adres kilitteki
# surya-ocr tekerleğinin settings.py'sinden okunur; burada yalnız okuma kuralı.
# ---------------------------------------------------------------------------
SURYA_AYAR_DOSYASI = "surya/settings.py"
MARKER_AYAR_DOSYASI = "marker/settings.py"
# Surya'nın varsayılan önbelleği (platformdirs user_cache_dir("datalab")/models):
# geliştirici makinesinde aynı SHA'lı dosya varsa APFS klonuyla yeniden kullanılır.
DATALAB_YENIDEN_KULLAN_KOKU = "~/Library/Caches/datalab/models"
MARKER_LISANS_DOSYASI = "MODEL_LICENSE"
MARKER_LISANS_HAM = "https://raw.githubusercontent.com/datalab-to/marker/v{surum}/MODEL_LICENSE"
MARKER_LISANS_GORUNEN = "https://github.com/datalab-to/marker/blob/v{surum}/MODEL_LICENSE"
MARKER_KAYNAK_URL = "https://github.com/datalab-to/marker/tree/v{surum}"
SURYA_KAYNAK_URL = "https://github.com/datalab-to/surya/tree/v{surum}"

# ---------------------------------------------------------------------------
# DMG'deki standart lisans metinleri (lisans raporu §8.3): SPDX lisans listesi,
# sabit etiket. Kitapçevir'in kendi GPL-3.0 metni depodaki LICENSE'tır.
# ---------------------------------------------------------------------------
SPDX_ETIKETI = "v3.29.0"
SPDX_METIN_URL = ("https://raw.githubusercontent.com/spdx/license-list-data/"
                  f"{SPDX_ETIKETI}/text/{{kimlik}}.txt")
SPDX_METINLERI = ("AGPL-3.0-only", "Apache-2.0", "MIT", "BSD-3-Clause", "MPL-2.0", "HPND")
# AGPL/GPL bileşenlerin kaynak bağlantıları (KAYNAK_KODU.txt): kilitteki sürüme göre.
AGPL_GPL_KAYNAKLARI = {
    "pymupdf": "https://github.com/pymupdf/PyMuPDF/tree/{surum}",
    "ebooklib": "https://github.com/aerkalov/ebooklib/tree/v{surum}",
    "marker-pdf": MARKER_KAYNAK_URL,
    "surya-ocr": SURYA_KAYNAK_URL,
}

# ---------------------------------------------------------------------------
# Gömülü üçüncü taraf ikililerin lisans DOSYALARI (K9 E2; KARAR 34: dağıtım
# bildirimi, özet değil). LICENSES/<grup>/ altına konur; 00_OKU.txt aynı listeden
# üretilir (iddia ile dosyalar ayrışmasın).
# ---------------------------------------------------------------------------
# PBS install_only arşivinde lisans klasörü yok (yalnız CPython'un kısa LICENSE'ı).
# Aynı yayının "full" arşivi python/licenses/ klasörünü taşır (5 Ekim 2026 ölçümü:
# 19 dosya; OpenSSL 3, mpdecimal, bzip2, expat, libffi, liblzma, SQLite, Tcl, zlib …
# ve CPython'un "Incorporated Software" bölümünü de içeren LICENSE.cpython.txt).
# Arşiv zstd'li ve macOS tar'ı zstd'yi kendisi açamıyor: sürüm makinesinde `zstd`
# gerekir (Homebrew: brew install zstd). SHA-256 aynı SHA256SUMS'tan doğrulanır.
PBS_FULL_ARSIV_ADI = (f"cpython-{PBS_PYTHON}+{PBS_YAYIN_ETIKETI}-aarch64-apple-darwin-"
                      "pgo+lto-full.tar.zst")
PBS_FULL_URL = f"{PBS_SURUM_KOKU}/{PBS_FULL_ARSIV_ADI.replace('+', '%2B')}"
PBS_LISANS_KLASORU = "python/licenses/"
# PBS'in lisans klasöründe OLMAYAN ama DMG'deki Python'da bulunan bileşenler (ölçüldü):
# Tk, itcl ve thread dylib'leri ile libpython'a gömülü HACL* (CPython Modules/_hacl;
# 3.12'nin Doc/license.rst'inde yok). Sürümler install_only arşivinden okunur
# (python/lib/tk*/pkgIndex.tcl, python/lib/itcl<sürüm>/, python/lib/thread<sürüm>/);
# {tire}: "9.0.4" → "9-0-4" (depoların sürüm etiketi biçimi).
TCL_EK_LISANS_URLLERI = {
    "tk": "https://raw.githubusercontent.com/tcltk/tk/core-{tire}/license.terms",
    "itcl": "https://raw.githubusercontent.com/tcltk/itcl/itcl-{tire}/license.terms",
    "thread": "https://raw.githubusercontent.com/tcltk/thread/thread-{tire}/license.terms",
}
HACL_KAYNAK_URL = ("https://raw.githubusercontent.com/python/cpython/v{python}/"
                   "Modules/_hacl/Hacl_Hash_SHA2.c")
# uv ve tectonic arşivlerinde lisans dosyası yok (ölçüldü): kaynak depolarından, sürüm etiketiyle.
UV_LISANS_URLLERI = {ad: f"https://raw.githubusercontent.com/astral-sh/uv/{UV_SURUMU}/{ad}"
                     for ad in ("LICENSE-MIT", "LICENSE-APACHE")}
TECTONIC_LISANS_URL = ("https://raw.githubusercontent.com/tectonic-typesetting/tectonic/"
                       f"{TECTONIC_ETIKETI}/LICENSE")
# GGUF tekerleği bizim sürüm sayfamızdan iner (K9 D6): içine derlenen llama.cpp/ggml ve
# gömülü kitaplıkların lisansları, sürüm makinesinin derlediği kaynak paketten (kilit
# SHA'sıyla doğrulanmış) alınır. Kaynak paketteki yol (kök klasör hariç) → LICENSES adı.
GGUF_SDIST_LISANSLARI = {
    "LICENSE.md": "LICENSE.md",
    "vendor/llama.cpp/LICENSE": "LICENSE.llama.cpp.txt",
    "vendor/llama.cpp/licenses/LICENSE-jsonhpp": "LICENSE.nlohmann-json.txt",
    "vendor/llama.cpp/vendor/cpp-httplib/LICENSE": "LICENSE.cpp-httplib.txt",
}

# ---------------------------------------------------------------------------
# Tekerlekler (TASARIM C.1-4, D.1)
# ---------------------------------------------------------------------------
# Kilit grupları ve ölçüm sırası: ortak bağımlılık ilk kuran gruba yazılır.
KILIT_GRUPLARI = ("cekirdek", "mlx", "pdf", "gguf")
# Bileşenlerin ölçüm sırası (C.1-3). hymt ölçülmez (15 GB; boyut HF ağacından).
OLCUM_SIRASI = ("cekirdek", "mlx", "pdf", "tex-hafif", "gguf")
# Desteklenen macOS ana sürümleri: en düşük 14 (MLX tekerlekleri); 16–25 yok.
# Son sürüm koşulda "26+" (26 ve sonrası) yazılır.
MACOS_ANA_SURUMLERI = (14, 15, 26)
# PyPI'da yalnız kaynak paketi olan ve sürüm çıkarken derlenen paketler.
# Ortam: TASARIM G.1-5 (Metal). MACOSX_DEPLOYMENT_TARGET = MIN_MACOS (derle.sh).
KAYNAKTAN_DERLENENLER = {"llama-cpp-python": {"CMAKE_ARGS": "-DGGML_METAL=on"}}
PYPI_JSON_URL = "https://pypi.org/pypi/{ad}/{surum}/json"

# ---------------------------------------------------------------------------
# Bileşen tanımları (ad ve açıklama ekrana gider; E.3 maketinin metinleri).
# HY-MT'nin açıklaması ui/branding.py adlarıyla sürüm betiğinde kurulur.
# ---------------------------------------------------------------------------
BILESEN_METINLERI = {
    "cekirdek": ("Çekirdek", "Python, arayüz, EPUB yolu, derleme"),
    "mlx": ("MLX", "Apple Silicon hesaplama kitaplığı"),
    "pdf": ("PDF desteği", "Marker + torch + Marker modelleri"),
    "tex-hafif": ("Hafif TeX (tectonic)",
                  "Paketleri gerektikçe indirir; kurulumda bir kez ısıtılır"),
    "tex-tam": ("Tam TeX (MacTeX)", "TeX Users Group; yönetici parolası ister"),
    "tex-kurulu": ("Kurulu olanı kullan", ""),
    "hymt": ("HY-MT 1.5 7B çeviri modeli (Tencent)", None),
    "gguf": ("GGUF desteği (llama-cpp-python, Metal)", "GGUF model dosyalarını doğrudan açar"),
}
TEX_GRUBU = ("tex", "TeX dağıtımı", "tex-hafif")   # (kimlik, ad, varsayılan)
# Kurulum sonrası içe aktarma sınaması (model YÜKLENMEZ).
DOGRULAMA_MODULLERI = {
    "cekirdek": ["streamlit", "fitz", "ebooklib", "bs4", "lxml", "rapidfuzz", "PIL", "numpy",
                 "jinja2", "docx", "huggingface_hub"],
    "mlx": ["mlx.core", "mlx_lm"],
    "pdf": ["torch", "marker", "surya"],
    "gguf": ["llama_cpp"],
}
LISANSLAR = {
    "cekirdek": {"spdx": "GPL-3.0-only", "onay_gerekli": False, "dagitim": "dmg"},
    "mlx": {"spdx": "MIT", "onay_gerekli": False, "dagitim": "indirilir"},
    "tex-hafif": {"spdx": "MIT", "onay_gerekli": False, "dagitim": "indirilir"},
    "tex-tam": {"spdx": "LicenseRef-TeX-Live", "onay_gerekli": False,
                "dagitim": "harici_kurulum"},
    "gguf": {"spdx": "MIT", "onay_gerekli": False, "dagitim": "indirilir"},
}
PDF_LISANS_SPDX = "LicenseRef-Datalab-OpenRAIL-M"
HYMT_LISANS_SPDX = "LicenseRef-Tencent-HY-Community"
# Resmî lisans metinlerinin gözden geçirilmiş sürümü (KARAR 34). Onay ekranında özet
# yoktur, resmî metin gösterilir; yine de metin değişirse sürüm DURUR: bir insan yeni
# metni okur, yukarıdaki lisans adlarını ve lisans.py'deki giriş cümlesini denetler,
# sonra bu satırı günceller. Bu SHA onay kaydına GİRMEZ (kayıt manifestteki metnin
# SHA'sını ve adresini taşır). Datalab: marker v1.10.2 MODEL_LICENSE; Tencent:
# tencent/HY-MT1.5-7B @ 397085d License.txt (lisans raporu kaynaklar/).
DATALAB_LISANS_SHA256 = "ae68ef89bb074ab9ac48da76e9682dd633e690ebd5c213f0cf4a20d4b24729af"
TENCENT_LISANS_SHA256 = "d7d9db858500ac9073f4b5decef8e208454357226f535f65079ce4376047569f"
# Uygulama arşivindeki lisans kopyaları (onay ekranı tam metni doğrulayarak gösterir:
# yonetici.lisans_metni önce köke, sonra uygulama koduna göre arar).
LISANS_KLASORU = "LICENSES"
DATALAB_LISANS_GORELI = f"{LISANS_KLASORU}/datalab/{MARKER_LISANS_DOSYASI}"
TENCENT_LISANS_GORELI = f"{LISANS_KLASORU}/tencent-hy/{TENCENT_LISANS_DOSYASI}"
TENCENT_LISANS_KLASORU = "tencent-hy"          # lisans_yaz: <kök>/LICENSES/<bu>/

# ---------------------------------------------------------------------------
# Uygulama arşivi: açık yayına ve DMG'ye GİRMEYENLER (depo köküne göre glob).
# Görev metni (5 Ekim) + TASARIM G.1-3; sürüm betiği arşivden sonra ayrıca bekçiyle
# denetler (bunlardan biri arşivde görülürse durur).
# ---------------------------------------------------------------------------
DISLANANLAR = (
    # Açık yayına girmeyecekler (Berke, 5 Ekim 2026)
    "data/test_data/*.epub",
    "docs/archive/**",
    "kutuphane/*.md",
    "kutuphane/**",
    # Geliştirme sürecinin iç tasarım notları (.gitattributes export-ignore ile aynı; K9 O3)
    "docs/C5_LAYOUT_DETECTION_PLAN.md",
    "docs/LATEX_PIPELINE_ARCHITECTURE.md",
    "docs/PDF_TO_LATEX_TOOLS_COMPARISON.md",
    "reports/**",
    "GELECEK_PLANI.md",
    "TODO.md",
    "KURALLAR.md",
    "output/**",
    "models/**",
    # Geliştirme ve sınama (TASARIM G.1-3)
    "tests/**",
    ".claude/**",
    "dev_tools/**",
    "packaging/testler/**",
    "conftest.py",
    "pytest.ini",
    "scripts/**",
    # Yalnız sürüm çıkaran makinede gerekenler (kabuk derlenip pakete girer)
    "packaging/surum/**",
    "packaging/kabuk/**",
    "packaging/kur_prototip.sh",
    "packaging/marker_modelleri.py",
    "packaging/TASARIM.md",
    # Yapay zekâ asistanı ve düzenleyici artıkları
    "**/CLAUDE.md",
    # Kullanılmayan yazı tipleri (hiçbir kod okumuyor; .gitattributes ile aynı)
    "fonts/**",
    ".claude-project",
    ".gitignore",
    "**/*.orig",
    "**/*.rej",
    "**/*.bak",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/.DS_Store",
)
# Bekçi: arşivde HİÇ bulunmaması gereken yol önekleri (DISLANANLAR'ın Berke listesi).
BEKCI_ONEKLERI = ("data/test_data/", "docs/archive/", "reports/", "output/", "models/",
                  "tests/", ".claude/")
BEKCI_DOSYALARI = ("GELECEK_PLANI.md", "TODO.md", "KURALLAR.md")
# Bytecode da bekçide (K9 E1): .pyc'nin co_filename'i derlendiği makinenin yolunu taşır.
BEKCI_DESENLERI = ("kutuphane/*.md", "data/test_data/*.epub",
                   "__pycache__/*", "*/__pycache__/*", "*.pyc")

# ---------------------------------------------------------------------------
# Paket ve DMG
# ---------------------------------------------------------------------------
UYGULAMA_ARSIV_SABLONU = "kitapcevir-{surum}.tar.gz"   # kabuk bu adı bekler (K1 raporu)
DMG_ADI_SABLONU = "Kitapcevir-{surum}.dmg"
DMG_CILT_ADI = "Kitapçevir"
DMG_BICIMI = "ULFO"                                    # lzfse (macOS 10.11+; TASARIM G.1-9)
# DMG'deki ilk açılış yönergesi (K9 O4; imzasız dağıtım). UTF-8 BOM'lu düz metin:
# TextEdit ve Hızlı Bakış kodlamayı doğru tanır.
DMG_ILK_ACILIS_ADI = "İlk açılış.txt"
PAKET_ADI = "Kitapçevir.app"
KULLANICI_ARACISI = "Kitapcevir-surum/{surum}"
OZET_DOSYASI = "SHA256SUMS"
# İndirme eşzamanlılığı (sürüm makinesi; yöneticinin ayarlarından bağımsız).
INDIRME_IS_PARCACIGI = 6
AG_ZAMAN_ASIMI_SN = 60
