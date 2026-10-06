"""Arayüzdeki adlar ve tanıtım metinleri -- tek değiştirme noktası.

Arayüzün tamamı adları buradan okur, hiçbir ekranda ad sabit yazılmaz.
Kullanıcıya dönük adlar işlevi söyler: "Yol A/B" ya da ATLAS/VECTOR gibi
soyut adlar ekranda kullanılmaz (Berke, 2026-09-24).
"""

from __future__ import annotations

from pathlib import Path

from core.lang_detect import TEX_SOURCE_LANGS, language_list_tr

APP_NAME = "Kitapçevir"
APP_TAGLINE = "Kitap çevirisi ve dizgi"

# Çıktı klasörünü belirleyen alanın adı (kenar çubuğu). Uyarılar kullanıcıyı
# bu alana yönlendirirken adı buradan okur.
BOOK_NAME_LABEL = "Kitap adı"

# Kitaptan TeX çıkarma: kaynak dil core.lang_detect.TEX_SOURCE_LANGS'teki
# dillerden biri olabilir (liste oradan üretilir, burada elle yazılmaz).
# Uygulama .tex'i çıkarır, çeviriyi kullanıcı yapar ya da bir yapay zekâ
# asistanına yaptırır, uygulama dizip PDF üretir. Ek TDK'ye göre okunuşa uyar:
# "pe-de-fe" ünlüyle biter, "PDF'den" (Berke, 2026-09-25). translate_book.py'nin
# dil kapısı iletileri de adı buradan okur.
TEX_TITLE = "PDF'den TeX çıkar"
# Özne-yüklem uyumu (U16): "sen ya da asistanın yapar" değil; iki özne iki
# ayrı yüklemle.
TEX_DESCRIPTION = (
    "Kitaptan (PDF ya da EPUB) çevrilmeye hazır LaTeX'i çıkarır. Çeviriyi sen "
    "yaparsın ya da bir yapay zekâ asistanına yaptırırsın; çeviri hazır olunca "
    f"kitabı dizip PDF üretir. {language_list_tr(TEX_SOURCE_LANGS)}. "
    "Model gerekmez."
)
# TeX kartının iki adım düğmesi. İletiler kullanıcıyı düğmeye adıyla
# yönlendirir (ör. "önce “1 · TeX'i çıkar”ı çalıştır").
TEX_EXTRACT_BUTTON = "1 · TeX'i çıkar"
TEX_COMPILE_BUTTON = "2 · Çevrilmiş TeX'i derle"

# Yerel model ile çeviri: model kitabı baştan sona kendi çevirir. Kaynak dil
# TeX yoluyla aynı listedir (core.lang_detect.TEX_SOURCE_LANGS; KARARLAR 14,
# 2026-09-28), liste oradan üretilir; hedef dil her zaman Türkçe.
MODEL_TITLE = "Yerel model ile çevir"
MODEL_DESCRIPTION = (
    "Bilgisayarındaki çeviri modeli kitabı baştan sona Türkçeye çevirir; "
    "kalite denetimi ve dizgi kendiliğinden ilerler. "
    f"{language_list_tr(TEX_SOURCE_LANGS)}."
)


def quoted(name: str) -> str:
    """Metin içinde anılan ad (kart, düğme): Türkçe çift tırnakla “…”.

    Adların kendisi kesme işareti taşır ("PDF'den TeX çıkar"); düz tek tırnak
    içine alınınca nerede bittiği okunmuyordu (U16).
    """
    return f"“{name}”"


# Paketli kurulum (DMG): bileşenleri kuran, onaran ve kaldıran yönetici
# ekranının adı (TASARIM E.7). Kenar çubuğundaki bağlantı (F4) ve "model
# kurulu değil" iletisi (F5) adı buradan okur.
COMPONENTS_SCREEN = "Bileşenler"
# Paketli kipte varsayılan model kurulu değilken gösterilen yönlendirme
# (F5; ui/runner._builtin_missing_reason). Geliştirici kipinde onun yerine
# kurulum rehberi (docs/KURULUM_VE_KULLANIM.md) anılır.
MODEL_INSTALL_HINT_PACKAGED = f"{quoted(COMPONENTS_SCREEN)} ekranından HY-MT'yi indir."

# Kenar çubuğunun alt bölümü (TASARIM §F, F4; 2026-10-05).
# "Kaynak kodu" bağlantısı: AGPL-3.0 m.13 (PyMuPDF ve EbookLib AGPL; iç
# lisans denetimi notu §4). Açık depo
# berketez/kitap_cevir (5 Ekim 2026); depo değişirse YALNIZ bu satır değişir
# (arayüzde adres başka yerde yazılmaz). Yönetici yalnız stdlib'le çalıştığı ve
# hiçbir şey içe aktarmadığı için aynı adresi packaging/yonetici/ayarlar.py'de
# (DEPO / KAYNAK_KODU_URL) ayrıca taşır; ikisinin eşitliğini tests/test_K6.py
# denetler.
SOURCE_CODE_URL = "https://github.com/berketez/kitap_cevir"
SOURCE_CODE_LABEL = "Kaynak kodu"
ABOUT_LABEL = "Hakkında"
# Tencent HY Community License m.3(e): hizmeti sağlayanın tam adı ve
# Tencent'le bağlantısızlık beyanı son kullanıcıya açıkça gösterilir. Metin
# kurulum sihirbazının HY-MT lisans ekranındaki beyanla birebir aynıdır
# (packaging/yonetici/lisans.py "beyan"; eşitliği tests/test_K6.py denetler).
ABOUT_TEXT = (
    f"{APP_NAME}, Berke Tezgöçen tarafından geliştirilen bağımsız bir projedir. "
    "Tencent ile bağlantılı değildir; Tencent tarafından desteklenmez ya da "
    "onaylanmaz."
)
# GNU GPL 3.0 "uygun yasal bildirimler" (m.0 ve m.5(d)): etkileşimli arayüz telif
# satırını, garanti verilmediğini, yazılımın aynı lisansla dağıtılabildiğini ve
# lisans metninin nerede okunacağını gösterir. macOS "Hakkında" panelindeki
# "© Berke Tezgöçen · GNU GPL 3.0" satırıyla uyumlu.
ABOUT_LICENSE_TEXT = (
    "© Berke Tezgöçen · GNU GPL 3.0 ile dağıtılır; hiçbir garanti verilmez. Bu "
    "yazılımı aynı lisansla değiştirip dağıtabilirsin; lisans metni kaynak kodundaki "
    "LICENSE dosyasındadır."
)


# "Yerel model ile çevir" kartının model seçicisi (2026-09-25). Liste yalnız
# gerçekten bulunan modellerden oluşur (ui/runner.discover_models): veri
# kökünün models/ klasöründeki MLX klasörleri ve GGUF dosyaları (geliştirici
# kipinde projenin models/'ı, paketli kipte <kök>/veri/models; TASARIM F1),
# açık Ollama ve LM Studio sunucularının modelleri. Ürün adları (Ollama, LM
# Studio) kullanıcının tanıdığı adlar olduğu için ekranda geçer.
MODEL_PICKER_LABEL = "Model"
MODEL_SCAN_BUTTON = "Modelleri tara"
# {folder}: taranan klasörün gerçek yolu (model_scan_help).
MODEL_SCAN_HELP = (
    "{folder} klasöründeki MLX klasörlerini ve GGUF dosyalarını, bu "
    "bilgisayarda açık Ollama ve LM Studio sunucularının modellerini tarar."
)


def model_scan_help(models_dir: str | Path) -> str:
    """"Modelleri tara"nın açıklaması: taranan klasörün GERÇEK yolu.

    Klasör keşfin taradığı kökün kendisidir (``registry.MODELS_DIR``; veri
    kökünden türer, K5 2026-10-05): kullanıcı modelini nereye koyacağını
    görür. Ev klasörü "~" diye kısaltılır; yol "/" ile biter.
    """
    path = Path(models_dir)
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        shown = path.as_posix()
    else:
        shown = "~" if relative == Path(".") else f"~/{relative.as_posix()}"
    return MODEL_SCAN_HELP.format(folder=shown.rstrip("/") + "/")


# Önerilen yerel model (KARARLAR 29: HY-MT 1.5 7B). Seçicide registry'nin
# varsayılan motorunun seçeneğine eklenir; bulunduğunda varsayılan seçim odur.
MODEL_RECOMMENDED_LABEL = "önerilen"
# Seçenek etiketindeki kaynak adı (runner.model_option_label). Anahtarlar
# core.mt_engines.contracts.SOURCE_* değerleridir; bu modül contracts'ı içe
# aktarmaz (yalnız core.lang_detect), eşleşmeyi tests/test_ui_model_secici_20260925.py
# denetler.
SOURCE_LABELS = {
    "builtin": "yerleşik",
    "user": "kayıtlı",
    "mlx": "MLX",
    "gguf": "GGUF",
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "openai": "OpenAI uyumlu sunucu",
}
# Modelin durumu: seçenek etiketinin sonunda (kullanılamıyorsa) ve kenar
# çubuğunda ("Çeviri modeli: <ad> — hazır").
MODEL_READY_LABEL = "hazır"
MODEL_UNUSABLE_LABEL = "kullanılamaz"
# Seçili model düşünen bir modelse (keşfin gerçek üst verisi) kartta not.
MODEL_REASONING_NOTE = "düşünen model; yavaş olabilir"
# Düşünme sınıfı (G2b-1, 2026-10-05; Berke'nin 5 Ekim kararı: "Düşünme
# kapatılabiliyorsa kapatılır; kapatılamıyorsa model düşünerek çevirir").
# Model seçicinin kısa etiketleri; gösterim K6'nın işi (app.py). Anahtarlar
# core.mt_engines.contracts.THINKING_* değerleridir (keşif kaydının
# ``thinking`` alanı ya da core.mt_engines.thinking.profile_of); bu modül
# contracts'ı içe aktarmaz, eşleşmeyi tests/test_G2b1_g.py denetler.
THINKING_LABELS = {
    "yok": "düşünme yok",
    "kapatilabilir": "düşünme kapalı çalışır",
    "kapatilamaz": "düşünen model — düşünerek çevirir, daha yavaştır",
    "bilinmiyor": "düşünme durumu bilinmiyor",
}
# Koşu sırasında kapatma isteği tutmadıysa (yapışkan bayrak; model o çeviri
# için "kapatilamaz" sayılır) gösterilecek not.
THINKING_OFF_FAILED_NOTE = ("düşünme kapatılamadı; model bu çeviride düşünerek çevirir, "
                            "daha yavaştır")


def thinking_label(thinking_class: str | None) -> str:
    """Düşünme sınıfının kısa etiketi; sınıf yoksa ya da tanınmıyorsa "bilinmiyor"un."""
    return THINKING_LABELS.get(thinking_class or "", THINKING_LABELS["bilinmiyor"])


# Düşünerek çeviri kipi (G2b-2, 2026-10-05; KARARLAR 22): düşünmesi
# kapatılamayan model her parçada önce düşünür, sonra çevirir (parça başına
# ayrı düşünme payı). Seçici / günlük için kısa not; gösterim K6'nın işi.
THINKING_MODE_NOTE = ("düşünerek çeviri: model her parçada önce düşünür, sonra çevirir; "
                      "daha yavaştır")
# Devre kesici (KARARLAR 22): düşünerek çeviride parçaların çoğunda düşünme
# payı dolup yeniden denemede de cevap gelmezse koşu çıkış 5 ile durur (arka
# uç nedeni; CLI "MODEL KULLANILAMADI", arayüz MODEL_UNAVAILABLE_MESSAGE'ın
# altında gösterir). {count}/{window}: kayan penceredeki GERÇEK sayım;
# {recommended}: önerilen yerel model (KARARLAR 29) — adı registry'deki
# varsayılan motorun etiketidir (core.mt_engines.discovery.engine_label), burada
# yazılmaz.
THINKING_NO_ANSWER_MESSAGE = (
    "Bu model düşünmesini bitirip çeviri üretemiyor: son {window} parçanın {count} "
    "tanesinde düşünme payı doldu, yeniden denemede de cevap gelmedi. Başka bir model "
    "seç — önerilen: {recommended}.")


# "Yerel model ile çevir" beta olarak yayımlanır (Berke, 5 Ekim 2026). Ad
# (MODEL_TITLE) DEĞİŞMEZ; etiket ve kısa açıklama ayrı gösterilir (K6).
MODEL_BETA_LABEL = "Beta"
MODEL_BETA_NOTE = ("Bu yol beta aşamasında: çeviri modelin gücüne bağlıdır ve "
                   "gözden geçirilmemiş bir taslaktır.")
# translate_book.py çıkış kodu 5 (EXIT_MODEL_UNAVAILABLE): seçili model
# koşu başında ya da koşu sırasında kullanılamadı.
MODEL_UNAVAILABLE_MESSAGE = (
    "Seçili model kullanılamadı; nedeni aşağıda. Güncel liste için "
    f"{quoted(MODEL_SCAN_BUTTON)} düğmesini kullan."
)
# Donanım kapısı (2026-09-28, KARARLAR 17): bu Mac referans 4 bit 7B modeli
# çalıştıramıyorsa "Yerel model ile çevir" kartı kapalıdır. Nedenin metni
# (gerçek ölçümle "en az X GB ... bu Mac'te Y GB") core/hardware.py'dedir;
# CLI ile aynı. Kart, model gerektirmeyen TeX yolunu önerir.
MODEL_HW_CLOSED_LABEL = "bu Mac'te kapalı"
MODEL_HW_TEX_HINT = f"Model gerektirmeyen {quoted(TEX_TITLE)} yolu bu Mac'te çalışır."


# Makine çevirisi künyesi (Berke kararı, 30 Eylül 2026): "Yerel model ile
# çevir" çıktısının (PDF ve EPUB) başında kısa, görünür blok. Hangi yerel
# modelle, bu bilgisayarda yapılmış, gözden geçirilmemiş taslak, kaynak dil,
# tarih. "PDF'den TeX çıkar" çıktısına KONMAZ. Künye çeviri gövdesinin parçası
# değildir (translate_book belge şablonuna ekler; kayıpsızlık / QC / EPUB
# hizalaması onu görmez). Model adı keşfin motor etiketidir
# (discovery.engine_label: "HY-MT1.5 7B (MLX)", "openai/gpt-oss-20b (LM Studio)").
MT_NOTICE_TITLE = "Makine çevirisi · gözden geçirilmemiş taslak"
MT_NOTICE_TEXT = (
    "Bu metin, bu bilgisayardaki bir yerel çeviri modelinin "
    f"{APP_NAME}'de ({quoted(MODEL_TITLE)}) ürettiği makine çevirisidir. Bir "
    "çevirmen ya da editör tarafından gözden geçirilmemiş bir taslaktır; yanlış "
    "ya da eksik çeviri içerebilir."
)
MT_NOTICE_FIELDS = "Model: {model} · Kaynak dil: {language} · Çeviri tarihi: {date}"
# Kayıt yoksa (künye kaydı tutulmadan önce çevrilmiş klasör) uydurulmaz.
MT_NOTICE_MODEL_UNKNOWN = "kaydedilmemiş"
MT_NOTICE_LANGUAGE_UNKNOWN = "belirlenemedi"
# EPUB üst verisi (dc:contributor): çeviriyi yapan model.
MT_NOTICE_CONTRIBUTOR = "{model} (yerel model, makine çevirisi)"
TR_MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")


def format_date_tr(day) -> str:
    """``date`` -> "30 Eylül 2026"."""
    return f"{day.day} {TR_MONTHS[day.month - 1]} {day.year}"


def mt_notice_lines(model: str | None, source_lang: str | None, day) -> tuple[str, str, str]:
    """Künyenin üç satırı: (başlık, açıklama, alanlar).

    *model*: motor etiketi (bilinmiyorsa None), *source_lang*: dil kodu
    (bilinmiyorsa None), *day*: çevirinin yapıldığı gün (``datetime.date``).
    """
    from core.lang_detect import language_name_tr

    fields = MT_NOTICE_FIELDS.format(
        model=model or MT_NOTICE_MODEL_UNKNOWN,
        language=language_name_tr(source_lang) if source_lang else MT_NOTICE_LANGUAGE_UNKNOWN,
        date=format_date_tr(day))
    return MT_NOTICE_TITLE, MT_NOTICE_TEXT, fields


def mt_notice_contributor(model: str | None) -> str:
    """EPUB ``dc:contributor`` değeri."""
    return MT_NOTICE_CONTRIBUTOR.format(model=model or MT_NOTICE_MODEL_UNKNOWN)


# Araçlar sütununun sekmeleri. "QA" kısaltması ekranda Türkçe adıyla (U16).
# Sütun 1280 piksel genişlikte ~233 piksel: "Görsel karşılaştırma" sığmıyor,
# sekme yarım görünüyordu (Playwright, 25 Eylül 2026); sekmenin kendi
# açıklaması "piksel düzeyinde karşılaştırır" der.
TERMS_TAB = "Terimler"
MEMORY_TAB = "Bellek"
COMPARE_TAB = "Karşılaştırma"

# Kalite denetimi (latex_pipeline/qc_engine.py) kontrollerinin ekrandaki
# Türkçe adları. Anahtarlar QCIssue.check_name değerleridir; 2_derle.py
# --json "qc_top" alanı bu adlarla gelir. Listede olmayan (yeni) bir kontrol
# ekranda özgün adıyla gösterilir (qc_check_label).
QC_CHECK_LABELS = {
    # 2026-09-30 (G4): okuma ajanlarının bulduğu, QC'nin önceden görmediği hatalar
    "caption_structure": "Altyazı iskeleti",
    "footnote_body_merge": "Dipnota yapışmış gövde metni",
    "footnote_count": "Dipnot sayısı",
    "footnote_def_in_body": "Gövdede kalmış dipnot metni",
    "fragmented_line": "Parçalanmış çeviri satırı",
    "markdown_remnant": "Markdown artığı",
    "number_corruption": "Bozulmuş sayı ya da karşılaştırma",
    "page_number_leak": "Metne sızmış sayfa numarası",
    "plain_footnote_marker": "Düz rakam kalmış dipnot işareti",
    "source_language_sentence": "Kaynak dilde kalmış cümle",
    "unescaped_ampersand": "Kaçışsız & işareti",
    "visible_html_tag": "Görünen HTML etiketi",
    "antonym_guard": "Zıt anlamlı çeviri",
    "ascii_degradation": "Türkçe harflerin ASCII'ye düşmesi",
    "brace_balance": "Süslü ayraç dengesi",
    "broken_command_braces": "Ayraçsız LaTeX komutu",
    "broken_inline_math": "Parçalanmış satır içi matematik",
    "broken_latex_syntax": "Bozuk LaTeX sözdizimi",
    "caption_consistency": "Terim tutarlılığı",
    "equation_count": "Denklem sayısı",
    "figure_reference_integrity": "Şekil ve görsel atıfları",
    "first_person_academic": "Akademik metinde birinci kişi",
    "foreign_language_leakage": "Yabancı dil sızıntısı",
    "formula_integrity": "Denklem bütünlüğü",
    "glossary_compliance": "Sözlüğe uyum",
    "hallucination": "Uydurma ya da kayıp içerik",
    "hallucination_pattern": "Uydurma belirtisi",
    "href_preservation": "Bağlantıların korunması",
    "html_artifact": "HTML kalıntısı",
    "index_preservation": "Dizin kayıtlarının korunması",
    "information_loss": "Bilgi kaybı",
    "inline_math_loss": "Satır içi matematik kaybı",
    "latex_encoded_turkish": "LaTeX kodlu Türkçe harf",
    "markdown_link_remnant": "Markdown bağlantı kalıntısı",
    "missing_images": "Eksik görsel",
    "mixed_language_line": "Karışık dilli satır",
    "name_ref_corruption": "Özel ad ve atıf bozulması",
    "ner_placeholder_residue": "Özel ad yer tutucusu kalıntısı",
    "number_hallucination": "Atıf numarası tutarsızlığı",
    "placeholder_orphans": "Temizlenmemiş yer tutucu",
    "prompt_leakage": "Model isteminin metne sızması",
    "section_number_consistency": "Bölüm numarası tutarlılığı",
    "sentence_length_alignment": "Uzunluk ve paragraf uyumu",
    "stash_placeholder_orphan": "Geri konmamış yer tutucu",
    "table_column_mismatch": "Tablo sütun uyuşmazlığı",
    # G2c (KARAR 25): kitap sözlüğüne göre terim tutarlılığı (uyarı)
    "term_consistency": "Kitap sözlüğüne göre terim tutarlılığı",
    "translated_env_name": "Çevrilmiş LaTeX ortam adı",
    "turkish_naturalness": "Türkçe doğallık",
    "untranslated": "Çevrilmemiş metin",
    "untranslated_heading": "Çevrilmemiş başlık",
    "word_concatenation": "Bitişmiş sözcük",
    "wrong_translation": "Bilinen yanlış çeviri",
}


def qc_check_label(check: str) -> str:
    """Kalite denetimi kontrolünün Türkçe adı; bilinmiyorsa özgün adı."""
    return QC_CHECK_LABELS.get(check, check)


# İlerleme kutusundaki faz satırının Türkçe adları. Pipeline fazını İngilizce
# günlük metniyle bildirir ("Phase 5: MT Translation (IT -> TR, engine=…)");
# ekranda o metin değil, faz numarasının Türkçe adı görünür (F5-08,
# 2026-09-29; biçimleyen: ui/runner.phase_label). Anahtarlar translate_book'un
# "pipeline" günlükçüsünün yazdığı faz numaralarıdır; listede olmayan (yeni)
# bir faz ekranda günlükteki başlığıyla gösterilir. Eşleşmenin eksiksizliğini
# tests/test_fix_U_20260929.py denetler.
PHASE_TITLES = {
    "1.5": "Sayfa düzeni",
    "2": "Markdown'dan LaTeX'e dönüştürme",
    "5": "Makine çevirisi",
    "5.5": "Temizleme, kalite denetimi ve otomatik düzeltme",
    "5.7": "İkinci geçiş (yeniden çeviri)",
    "5.71": "Çevrilmiş LaTeX ortam adlarının düzeltilmesi",
    "5.75": "Sözlük geçişi",
    "5.8": "Sorunlu bölümlerin yeniden çevirisi",
    "6": "Türkçe PDF'nin derlenmesi",
    "7": "Kalite incelemesi",
}
# Makine çevirisi fazı: başlığa kaynak dil eklenir ("… (<dil> → Türkçe)").
PHASE_TRANSLATION = "5"
