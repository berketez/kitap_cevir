# Çeviri Talimatı — PDF'den TeX çıkar (Claude Code için otonom playbook)

> **Bu dosya Claude Code (veya başka bir LLM ajanı) içindir.** Kullanıcı
> "bu kitabı çevir" dediğinde, aşağıdaki adımları **sırayla** izle. Amaç:
> bir kitabı (PDF/EPUB) yerel bir çeviri modeline ihtiyaç duymadan, LaTeX
> yapısını **birebir koruyarak** Türkçe PDF'ye çevirmek. Kaynak dil
> İngilizce, Almanca, Rusça, Fransızca, İspanyolca ya da İtalyanca
> olabilir (bkz. "Kaynak dil").
>
> Bu talimat **tek başına yeterlidir** — hiçbir özel/kişisel ajan tanımına
> bağımlı değildir. Yalnızca bu repodaki iki script (`1_cikar.py`,
> `2_derle.py`) ve senin çeviri yeteneğin gerekir.

---

## Kaynak dil

- Bu akış (arayüzde **"PDF'den TeX çıkar"**) **İngilizce, Almanca, Rusça,
  Fransızca, İspanyolca ve İtalyanca** kaynak için kullanılır. Çince /
  Japonca / Korece (CJK) kaynak **kapsam dışıdır**.
- Kaynak dili `EXPORT.tex`'in düzyazısına bakarak belirle ve çeviriyi
  **doğrudan o dilden Türkçeye** yap (İngilizce ara dil kullanma).
- "Yerel model ile çevir" yolu (translate_book.py; HY-MT ya da bu
  bilgisayardaki başka bir yerel model) da aynı altı kaynak dili kabul eder;
  desteklenmeyen dildeki kitabı çeviri başlamadan reddeder (çıkış kodu 4).
  Bu talimattaki akış yerel modelden bağımsızdır ve en iyi kaliteyi verir.
- Aşağıdaki kurallarda geçen "kaynak" = kitabın özgün dili, "kaynak metin" =
  `EXPORT.tex`.

---

## Genel akış (özet)

```
1_cikar.py  →  EXPORT.tex (kaynak dilde LaTeX gövdesi)
   → böl → sözlük çıkar → çevir (koruma kurallarıyla) → birleştir
   → EXPORT_TR.tex
2_derle.py  →  Türkçe PDF
```

Çeviri modeli / GPU **gerekmez**. Ağır olan tek adım PDF çıkarımıdır (Marker
OCR); EPUB girdide o da hafiftir.

---

## Başlamadan önce — kitap zaten çıkarılmış mı?

Kullanıcı kitabı **arayüzden** ("PDF'den TeX çıkar" kartında "1 · TeX'i
çıkar") ya da daha önce `1_cikar.py` ile çıkardıysa **Adım 1'i ATLA**:

- Kitabın klasörü `output/<slug>/`'dir (`<slug>`, arayüzdeki "Kitap adı"nın
  klasör adıdır). Orada çevrilecek `EXPORT.tex` ve bu kitap için **tam
  yollarla doldurulmuş** bir `CEVIRI_TALIMATI.md` bulunur.
- **Kitaba özel `output/<slug>/CEVIRI_TALIMATI.md`'yi oku ve onu izle**:
  başındaki yollar (EXPORT.tex, EXPORT_TR.tex, derleme komutu) o kitabındır.
  Arayüz bu dosyanın tam yolunu ve kopyalanabilir bir istemi gösterir;
  kullanıcı o istemi verdiyse doğrudan o dosyadan başla.
- Arayüze yüklenen kitap dosyası geçici bir klasördedir; ona ihtiyacın yok.
  Kaynak kitabın kopyası `output/<slug>/source_book.pdf` (ya da `.epub`)
  olarak durur (OCR şüphelerinde bakılacak yer).
- Kullanıcı hangi kitabı kastettiğini söylemediyse `output/*/EXPORT.tex`
  içinden yanında `EXPORT_TR.tex` olmayanları bul; tek aday varsa onu al,
  birden çoksa kullanıcıya sor.
- Kitabı yeniden çıkarma: `EXPORT.tex` çeviri sürerken değişirse bölüm
  yapısı kayar. (Gerçekten gerekiyorsa `1_cikar.py --yeniden` önceki
  çıkarımı `full_book.onceki.md` olarak saklayıp baştan çıkarır.)

## Adım 1 — Çevrilecek LaTeX'i çıkar

Kullanıcının kitabını al ve çalıştır:

```bash
python 1_cikar.py "<kitap.pdf veya kitap.epub>"
# Apple Silicon yoksa:  python 1_cikar.py "<kitap>" --device cpu
```

Bu, `output/<slug>/EXPORT.tex` üretir (ayrıca `kitap.json` ve bu kitap için
doldurulmuş bir talimat kopyası). Script'in bastığı `output_dir` ve
`EXPORT.tex` yolunu not al — sonraki adımlarda kullanacaksın.

`EXPORT.tex`'in başında şuna benzer bir blok bulunur — **asla silme, çevirme
veya değiştirme**; `2_derle.py` kitabı bununla bulur:

```
% ==== KITAPCEVIR-META v1 ====
% slug: ...
% ...
% ============================
```

---

## Adım 2 — EXPORT.tex'i bölümlere böl

- `EXPORT.tex`'i **`\chapter{...}` veya `\part{...}` sınırlarından** parçalara
  ayır. **Paragraf ortasından bölme** — çapraz referanslar bozulur.
- Hedef parça boyutu **≤ 80 KB**. Küçük kitap (≤ ~150 sayfa) çoğu zaman 1–2
  parçadır; bölmeye gerek yoksa bölme.
- Parçaları `output/<slug>/parts/part_01.tex`, `part_02.tex` … olarak yaz.
- **`% KITAPCEVIR-META` başlığı yalnızca ilk parçada kalsın** (veya hiçbir
  parçada tutma; birleştirmede en başa sen ekleyeceksin — aşağıya bak).

## Adım 3 — Önce ortak sözlük (glossary) çıkar

- Kitabı tarayıp tekrar eden **teknik terimleri** çıkar (50–100 terim yeter).
- `output/<slug>/glossary_shared.tsv` olarak yaz: her satır
  `Kaynak terim<TAB>Türkçe` (kaynak terim kitabın dilinde: `thrust`,
  `Schub`, `тяга`, `poussée`...).
- **Tüm parçalar bu sözlüğü kullanır** → terminoloji kitap boyunca tutarlı olur.
- Emin olmadığın terimi olduğu gibi bırak (özel ad, formül sembolü, marka).

## Adım 4 — Parçaları çevir

Her parça için (küçük kitapta doğrudan sen çevir; büyük kitapta paralel
`general-purpose` subagent kullanabilirsin):

- **Paralel çalışıyorsan her ajana AYRIK bölüm aralığı ver** — iki ajan aynı
  dosyaya asla yazmasın (veri kaybı riski).
- Her ajana/işine `glossary_shared.tsv`'yi ver.

### KORUMA KURALLARI (çeviride ZORUNLU — ajan promptuna aynen koy)

Yalnızca **insan tarafından okunan düz metni ve başlık metnini** çevir.
Aşağıdakilere **DOKUNMA, aynen bırak**:

- Matematik: `$...$`, `\[...\]`, `\begin{equation}…\end{equation}`,
  `align`, `gather`, `multline`, `matrix` vb. ortamların **içi**.
- Komut argümanları: `\ref{...}`, `\eqref{...}`, `\cite{...}`, `\label{...}`,
  `\includegraphics{...}` içindeki yollar/anahtarlar.
- Ortam adları ve komutların kendisi: `\begin{figure}`, `\section{`, `\item`,
  `\textbf{` … (yalnız `{}` içindeki **metni** çevir, komutu değil).
  Kaynakta çerçeveli/zeminli kutu olan bölüm `\begin{kckutu}` …
  `\end{kckutu}` satırları arasındadır: iki satırı da kendi yerinde aynen
  bırak, aradaki metni çevir.
- Ortam seçenekleri: `\begin{figure}[H]`, `\begin{enumerate}[start=3]` gibi
  `\begin{…}` ardındaki `[…]` aynen kalır (`start=3`: liste kaynakta 3'ten
  sürüyor; silinirse ya da çevrilirse numaralar yeniden 1'den başlar).
- Dosya yolları, URL'ler, kod blokları (`verbatim`/`lstlisting`).
- Kaynakça künyeleri (yazar, eser adı, yayınevi, yıl) kaynak dilde kalır —
  Rusça/Almanca kaynakça başlıkları meşrudur. Yalnız bölüm başlığını çevir
  (`Литература`, `Literatur`, `Bibliographie` → `Kaynakça`).
- Baştaki `% KITAPCEVIR-META` satırları.

### Taranmış (OCR) kitaplar — formüllerdeki harf karışıklığı

Taranmış kitaplarda (özellikle Rusça ders kitapları) OCR, formül içindeki
**Kiril harfleri Latin benzerleriyle karıştırır**. Çeviri sırasında formülü
çevirmezsin ama bu OCR hatalarını düzeltmekten sorumlusun:

| Kaynak PDF'de (doğru)          | EXPORT.tex'te (OCR hatası)          |
|--------------------------------|-------------------------------------|
| `p_{\text{н}}` (p_н)           | `p_{\rm H}`, `p_H`, `p_{n}`          |
| `I_{\text{у}}` (I_у)           | `I_{y}`, `J_{\rm y}`                 |
| `c_{i\,\text{пост}}`           | `c_{i \operatorname{noct}}`          |
| `c_p`                          | `c_n`                               |
| düzyazıda `ПС` (продукты сгорания) | `$\Pi C$` (matematik sanılmış)  |

- Şüphede **kaynak PDF'nin ilgili sayfasına bak** (`output/<slug>/source_book.pdf`)
  ve formülü oradaki gösterime göre düzelt.
- **Formül gösterimi kaynaktaki gibi kalır** — matematik çevrilmez: Kiril
  indis Kiril kalır (`p_{\text{н}}`, Türkçeleştirilip `p_{\text{d}}` gibi
  yeni bir gösterim uydurulmaz). Matematik içinde Kiril harfi `\text{...}`
  içine yaz; çıplak `p_н` matematik fontunda görünmeyebilir.
- Kiril kısaltma indislerini **ilk geçtikleri yerde** bir çevirmen notuyla
  açıklayabilirsin, ör.
  `$v_{\text{ср}}$\footnote{Çevirmen notu: ср = среднее, ortalama değer.}`.
  Sonraki geçişlerde not tekrarlanmaz.
- OCR düzyazıdaki bir kısaltmayı matematiğe çevirmişse (`$\Pi C$`) harfleri
  düzelt ama `$...$` sarmalını koru: `$\text{ПС}$` (satır içi matematik
  sayısı kaynakla aynı kalsın — QC bu sayıyı karşılaştırır). İlk geçişte
  Türkçe açılımını yanına yaz: "yanma ürünleri ($\text{ПС}$)".
- Düzyazı kısaltmalarını (ЭДС) ilk geçişte Türkçe açılımıyla ver:
  "elektromotor kuvvet (ЭДС)"; **sonrasında Türkçe terimi** (ya da onun
  Türkçe kısaltmasını, ör. EMK) kullan. Latin dışı alfabeli kısaltma Türkçe
  düzyazıda yalnız ilk geçişte, parantez içinde görünür; okur Kiril harfini
  sayfa boyunca tekrar görmemeli. (Formül içindeki Kiril indisler bu kuralın
  dışındadır, yukarıya bak.)

### Terim ve biçim tutarlılığı (tüm kaynak diller)

- **Kutu / yan not başlıkları** (Exkurs, Encadré, Recuadro, Riquadro, Врезка,
  Box) Türkçede her kitapta aynı karşılıkla: **"Kutu"** (`Kutu 1.1`). Kitap
  içinde başka karşılık ("Ek Bilgi", "Ara Konu") kullanma.
- **Yüzyıl ve sıra sayıları:** Roma rakamlı ya da üst simgeli sıra sayıları
  Türkçe yazımla Arap rakamı + nokta olur: "XXᵉ siècle", "XX secolo",
  "20. Jahrhundert" → "20. yüzyıl"; "1.ª/1.º" → "1.".
- **Tırnaklar:** kaynak dilin tırnakları („…“, « … », «…», "…") Türkçede
  “…” olur; tırnak içinde tırnak ‘…’.
- **Kaynakta başka dilde verilmiş alıntı** (ör. İtalyanca kitapta Fransızca
  bir alıntı): alıntıyı özgün dilinde AYNEN bırak, Türkçesini
  `\footnote{Çevirmen notu: …}` olarak ekle. Alıntı kaynakta da o dilde
  olduğu için QC bunu çevrilmemiş saymaz.
- **Şekil/grafik içindeki yazılar** (eksen adları, lejant, kutu etiketleri)
  görselin parçasıdır, değiştirilemez. Türkçe karşılıklarını altyazının
  SONUNA parantez içinde bir çevirmen notuyla ver:
  `(Çevirmen notu: grafikteki adlar — başlık: “…”; Messages: Mesajlar; …)`.
  Görseldeki yazı Türkçe okura bir şey söylemiyorsa (tek bir sayı, logo)
  not ekleme. QC bu notu uzunluk karşılaştırmasına saymaz.
- Özel adlar kişinin/yerin yerleşik yazımıyla (Claudio Bernard → Claude
  Bernard); yer adlarının yerleşik Türkçe biçimi varsa o (Sibérie → Sibirya).

### Yapı kuralları

- **Bölüm/ortam sayısı kaynakla (`EXPORT.tex`) birebir aynı** kalmalı. Bir
  `\section`, bir `equation` bile eksilmemeli/artmamalı (QC bunu sayar).
- **Başlıklarda "Bölüm X" YAZMA** — LaTeX şablonu numarayı ve "Bölüm" ön ekini
  kendi ekler. Kitap içi kısım adları için "Kısım" kullan.
  - İstisna: kaynakta başlığı **yalnız bir bölüm etiketinden** oluşan satır
    varsa (`\section{Chapter 3}`, `\section{Глава 2}` — eski çıkarımlarda
    görülür) bu satırı silme, `\section{Bölüm 3}` olarak bırak. Derleme bu
    etiketi kaynakla aynı biçimde kendisi birleştirir; silersen QC
    bölüm sayısını tutmaz.
  - Başlığı yalnız bölüm etiketi olan **`\chapter`** satırı da silinmez
    (kitap bölüme ad vermiyorsa çıkarım böyle üretir: `\chapter{CAPÍTULO
    VII}`, `\chapter{CHAPITRE II}`, `\chapter{Глава первая}`,
    `\chapter{Zweites Kapitel}`). Numarayı **kaynağın biçimiyle** çevir:
    Roma rakamı Roma rakamı kalır (`\chapter{Bölüm VII}`,
    `\chapter{Bölüm II}`); sözcükle yazılmış sıra Türkçe sıra sözcüğü olur
    (`\chapter{Birinci Bölüm}`, `\chapter{İkinci Bölüm}`).
  - **Roma rakamını ya da sıra sözcüğünü Arap rakamına ÇEVİRME**
    (`\chapter{Bölüm 7}` YAZMA): derleme `\chapter{Bölüm 7}` biçimli satırı
    sahte bölüm başlığı sayıp siler; bölüm PDF'de kaybolur, QC "bölüm kaybı"
    (`information_loss`) bildirir. Kaynakta zaten Arap rakamı varsa
    (`\chapter{Capítulo 7}`) `\chapter{Bölüm 7}` yaz: derleme iki tarafı
    aynı kuralla işler.
  - Başlıktaki `§ 1.2.` gibi numara önekini kaynaktaki gibi koru.
- **Türkçe karakterler doğru** (ç, ğ, ı, İ, ö, ş, ü). ASCII'ye düşme. UTF-8.
- Akademik metinde birinci tekil şahıs yerine tarafsız/akademik dil tercih et.

## Adım 5 — Parçaları birleştir

- Parçaları **numara sırasıyla** birleştir → `output/<slug>/EXPORT_TR.tex`.
- **`% KITAPCEVIR-META` başlığı en üstte** olsun (ilk parçadan geldiyse yeterli;
  değilse `EXPORT.tex`'in ilk bloğunu kopyalayıp en başa koy).

## Adım 6 — Kendini denetle (derlemeden önce)

- `\chapter`/`\section`/`\begin{...}` sayıları `EXPORT.tex` ile eşleşiyor mu?
- Koruma kurallarından biri ihlal edildi mi (çevrilmiş `\ref` argümanı,
  bozulmuş `$…$`)? Varsa düzelt.
- Kaynak dilde çevrilmeden kalmış paragraf/başlık var mı? (Kiril, Yunan
  alfabeli ya da Almanca/Fransızca... satırlar.) QC bunları `untranslated` /
  `untranslated_heading` olarak raporlar; kaynakça ve formül içi indisler
  sayılmaz.

## Adım 7 — Derle

```bash
python 2_derle.py output/<slug>/EXPORT_TR.tex
```

Çıktı: `output/<slug>/<slug>_TR.pdf`. Model/GPU gerekmez.

- **QC bölüm/denklem uyumsuzluğu** raporlarsa: hangi bölümde yapısal işaret
  (başlık/ortam) düştüyse o parçayı aç, düzelt, birleştir, tekrar `2_derle`.
- Derleme (XeLaTeX) hatası verirse `output/<slug>/source/book_tr.log`'a bak
  (`compiled/` altına .log kopyalanmaz; `2_derle` hata çıktısı ilk hata
  satırlarını zaten basar); genelde çeviri sırasında bozulan bir
  `{}`/`\begin…\end` dengesizliğidir.

---

## Serbest kullanım — çevirmen SEN olmak zorunda değilsin

`EXPORT.tex` **sıradan bir LaTeX dosyasıdır**; `2_derle.py` çeviriyi kimin
yaptığına bakmaz. Kullanıcı şunların hepsini yapabilir:

- **Elle çeviri/düzenleme:** `EXPORT.tex`'i herhangi bir editörde açıp
  istediği gibi çevirir/düzenler, `EXPORT_TR.tex` olarak kaydeder. Kelime
  tercihi tamamen kullanıcıya aittir — manuel modda pipeline kelime
  seçimlerine karışmaz ("Çevirmen notu: ..." eklemek de serbesttir).
- **Codex ile:**

  ```bash
  timeout 600 codex exec "output/<slug>/EXPORT.tex dosyasını yukarıdaki \
  KORUMA KURALLARI'na uyarak Türkçeye çevir ve EXPORT_TR.tex olarak kaydet" \
  --skip-git-repo-check
  ```

- **Gemini CLI ile:** aynı prompt'u `gemini` komutuna ver; ya da dosyayı
  parça parça herhangi bir sohbet arayüzüne yapıştırıp çıktıyı
  `EXPORT_TR.tex`'te topla.
- **Karışık:** bir kısmını LLM çevirsin, kullanıcı üstünden geçsin —
  `2_derle.py` yine çalışır. Yapı uyarıları (bölüm sayısı) bilgilendirmedir,
  PDF yine üretilir.

Hangi araç kullanılırsa kullanılsın tek sözleşme: **KORUMA KURALLARI**
(yukarıda) + META satırlarının korunması (silindiyse `--output-dir` ile
derlenebilir).

## Kendi paketini/makronu ekle — `preamble_ozel.tex`

Şablonun preamble'ı sabittir; ekleme yapmak istersen
`output/<slug>/preamble_ozel.tex` dosyası oluştur — içeriği derlemede
`\begin{document}`'tan hemen önce AYNEN eklenir:

```latex
% output/<slug>/preamble_ozel.tex
\usepackage{siunitx}
\newtheorem{tanim}{Tanım}
\newcommand{\vektor}[1]{\mathbf{#1}}
```

Gövdede kendi tanımladığın ortam/komutları kullanabilirsin; pipeline
`\newenvironment`/`\newtheorem` ile tanımladığın adları yeniden ADLANDIRMAZ.
(Güvenlik: `\write18` içeren preamble yok sayılır.)

---

## Telif notu

Bu akış **kişisel okuma** içindir (FSEK m. 38 — kişisel kullanım kopyalama
istisnası). Çeviri çıktısı satılmaz/dağıtılmaz; repoya kitap çıktısı eklenmez.
