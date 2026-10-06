"""Çok dilli kaynak desteği — 24 Eylül 2026.

Kapsanan değişiklikler:
    A. QC: alfabe tabanlı çevrilmemiş-kaynak kontrolü (her iki yol).
       Kiril/Yunan/Arap/İbrani/CJK alfabeli çevrilmemiş satırlar ve ü/ö/ç
       yüzünden "Türkçe" sanılan Almanca/Fransızca satırlar "untranslated"
       akışına girer; matematik, kaynakça, künye, tek harf/kısaltma muaf.
    B. "Yerel model ile çevir" (Yol B) kaynak dil kapısı: kitap güvenle
       İngilizce değilse MT başlamadan çıkış kodu 4 (EXIT_SOURCE_NOT_ENGLISH).
       28 Eylül 2026 (K5, KARARLAR 14): kaynak dil TEX_SOURCE_LANGS; çıkış 4
       yalnız desteklenmeyen dilde (EXIT_SOURCE_UNSUPPORTED), desteklenen
       dil zincire akar. --export-tex / --import-tex yollarında kapı ÇALIŞMAZ.
    C. CEVIRI_TALIMATI.md ve 1_cikar.py dil-nötr metinler.

Rusça örnekler SENTETİKTİR (25 Eylül 2026, telif: gerçek bir kitaptan cümle
alınmaz; itki/yanma konulu, bu test için yazıldı). Biçimleri tarama bir
ders kitabının Marker çıkarımını taklit eder: paragraf başına tek satır ve
OCR'ın formül hataları — ``\\operatorname{noct}`` = пост, ``p_{\\rm H}`` =
p_н, ``$\\Pi C$`` = ПС, ``I_{y}`` = I_у, ``\\kappa`` = к (Kiril indisin
Latin/Yunan harfe dönmesi).

Testler MT modeli / XeLaTeX GEREKTİRMEZ.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

import translate_book as tb
from core import lang_detect as ld
from latex_pipeline.qc_engine import QCEngine

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ===========================================================================
# Örnek metinler
# ===========================================================================

# Sentetik Rusça metin (Marker çıkarımı biçiminde, LaTeX'e aktarılmış).
# Testlerin dayandığı özellikler: [0] tek satır >= 200 karakter ve
# \textit{тягой}; [1]+[0] birlikte > CITATION_MAX_CHARS; [2] matematik
# dışında "вращательных" ve Kiril indisli satır içi matematik; [3] OCR'lı
# $\Pi C$; her paragraf Rusça işlev kelimeleri taşır (ru kapsaması).
_RU_PARAS = [
    "Любой летательный аппарат, будь то ракета или спутник на орбите, "
    "меняет свою скорость только тогда, когда на него действует внешняя "
    "сила; в ракетной технике такую силу принято называть \\textit{тягой}. "
    "Источником тяги служит двигатель, который выбрасывает поток газа в "
    "сторону, противоположную направлению полета.",
    "Чем выше скорость истечения продуктов сгорания из сопла, тем меньше "
    "топлива нужно израсходовать для набора заданной скорости. Поэтому "
    "конструкторы стремятся поднять температуру в камере и давление перед "
    "соплом.",
    "В первом приближении теплоемкость газовой смеси складывается из "
    "вкладов вращательных $c_{j\\,\\mathrm{вращ}}$ и поступательных "
    "$c_{j\\,\\mathrm{пост}}$ степеней свободы молекул, тогда как "
    "колебательная составляющая при коротком времени пребывания газа в "
    "сопле остается «застывшей» и в расчет обычно не входит.",
    "У химически активной смеси $\\Pi C$ энтальпия зависит не только от "
    "температуры, но и от состава, который меняется по ходу реакций, "
    "поэтому производную энтальпии $\\Pi C$ по температуре вычисляют при "
    "неизменном давлении:",
    "Для идеального газа неизменного состава разность теплоемкостей "
    "$c_p - c_v$ равна универсальной газовой постоянной $R_{\\mu}$ "
    "[Дж/(кмоль град)], отнесенной к молярной массе. В химически "
    "реагирующем потоке это простое соотношение нарушается, и к нему "
    "добавляют член, учитывающий сдвиг равновесия:",
    "Если давление на срезе сопла выше давления окружающей среды, струя "
    "продолжает расширяться уже за двигателем, и часть энергии ПС "
    "теряется. При обратном соотношении давлений в расширяющейся части "
    "сопла возникают скачки уплотнения, а поток может оторваться от "
    "стенки. Лучшие показатели сопло дает тогда, когда оба давления "
    "примерно равны.",
]
# OCR artıkları: \operatorname{noct} (пост), I_{y} (I_у), K_{\Pi} (K_П),
# p_{\kappa} ve F_{\kappa p} (к, кр), p_{\rm H} (p_н).
_RU_EQUATIONS = [
    "\\begin{equation}\n\\bar{c}_{p} = \\sum_{j} x_{j} \\left( "
    "c_{j \\operatorname{noct}} + c_{j \\operatorname{вращ}} \\right) "
    "+ R_{\\mu} \\tag{3.4}\n\\end{equation}",
    "\\[\nI_{y} = K_{\\Pi}\\,\\beta, \\qquad K_{\\Pi} = "
    "\\frac{P}{p_{\\kappa} F_{\\kappa p}} = \\Gamma \\sqrt{\\frac{2k}{k-1}"
    "\\left[1 - \\left(\\frac{p_{a}}{p_{\\kappa}}\\right)^{(k-1)/k}\\right]}"
    " + \\frac{F_{a}}{F_{\\kappa p}} \\left( \\frac{p_{a}}{p_{\\kappa}} - "
    "\\frac{p_{\\rm H}}{p_{\\kappa}} \\right)\n\\]",
]
_RU_SECTION_HEADING = (
    "\\section{ИСТЕЧЕНИЕ ГАЗА ИЗ СОПЛА И ТЕПЛОЕМКОСТЬ ПРОДУКТОВ СГОРАНИЯ}")
_RU_BODY = (
    "\\chapter{ТЯГА И ЭНЕРГЕТИКА РАКЕТНОГО ДВИГАТЕЛЯ}\n\n"
    + _RU_SECTION_HEADING + "\n\n"
    + "\n\n".join(_RU_PARAS[:3]) + "\n\n" + _RU_EQUATIONS[0] + "\n\n"
    + "\n\n".join(_RU_PARAS[3:]) + "\n\n" + _RU_EQUATIONS[1] + "\n"
)
# Sentetik GOST biçimli künye (yazar ve eser uydurmadır): yıl + "М.:" +
# "— N с." yayın işaretleri.
_RU_CITATION = ("Орлов К. П. Термодинамика камер сгорания. — М.: "
                "Машиностроение, 1971. — 312 с.")

# Doğru çevrilmiş Türkçe (Yol A çıktısı): Kiril indisler matematikte,
# kısaltmalar (ЖРД, ПС) düzyazıda tek başına, Yunan harfleri metin içinde,
# çevirmen notu ve Rusça kaynakça — HİÇBİRİ "çevrilmemiş" sayılmamalı.
_TR_GOOD = (
    "\\chapter{Roket Motorunun İtkisi ve Enerjisi}\n\n"
    "\\section{Lüleden Gaz Akışı ve Yanma Ürünlerinin Isı Sığası}\n\n"
    "Roket ya da yörüngedeki bir uydu gibi her uçan araç, hızını ancak "
    "üzerine bir dış kuvvet etkidiğinde değiştirir; füze tekniğinde bu "
    "kuvvete \\textit{itki} denir. İtkinin kaynağı, gaz akışını uçuş "
    "yönünün tersine atan motordur.\n\n"
    "Burada $c_{j\\,\\text{пост}}$ ötelenme, $c_{j\\,\\text{вращ}}$ ise dönme "
    "serbestlik derecelerinin katkısıdır; sıvı yakıtlı füze motoru (ЖРД) "
    "için yanma ürünleri ($\\text{ПС}$) ideal gaz kabul edilir.\n\n"
    "\\begin{equation}\n"
    "\\bar{c}_{p} = \\sum_{j} x_{j} \\left( c_{j\\,\\text{пост}} "
    "+ c_{j\\,\\text{вращ}} \\right) + R_{\\mu} "
    "+ \\text{прочие члены уравнения теплоемкости} \\tag{3.4}\n"
    "\\end{equation}\n\n"
    "\\[\n"
    "I_{\\text{у}} = K_{\\text{П}}\\,\\beta \\quad \\text{при } "
    "p_{\\text{н}} = p_{a}\n"
    "\\]\n\n"
    "Tasarım rejiminde lüle çıkışındaki basınç dış ortam basıncına eşittir: "
    "$p_{a} = p_{\\text{н}}$\\footnote{Çevirmen notu: alt simgedeki н, "
    "наружное (dış) sözcüğünden gelir.}.\n\n"
    "Açı $\\alpha$ ile gösterilir; burada α küçük, β ise ihmal edilebilir "
    "kabul edilir ve Δp çok küçüktür.\n\n"
    + _RU_CITATION + "\n\n"
    "\\section*{Kaynakça}\n"
    "Зимин Р. Т., Гусев Н. Л. Истечение газов из сопел учебное пособие для "
    "студентов энергетических специальностей\n"
    "Орлов К. П. Теплообмен в камерах сгорания справочник для конструкторов "
    "и испытателей двигателей\n"
)

_TR_PAR = (
    "Yanma odasındaki yanma yüksek basınç ve yüksek sıcaklıkta gerçekleşir; "
    "bu nedenle itki, yanma ürünlerinin çıkış hızına bağlıdır."
)

# Çevrilmemiş Avrupa dili cümleleri — ü/ö (Almanca) ve ç (Fransızca) Türkçe
# harflerle ÇAKIŞIR; eski QC bunları "saf Türkçe" sanıp atlıyordu.
_EU_SENTENCES = {
    "de": ("Für die Berechnung der Strömung in der Düse wird angenommen, "
           "dass das Gas ideal ist und sich im chemischen Gleichgewicht "
           "befindet."),
    "fr": ("Pour le calcul de l'écoulement dans la tuyère, on suppose que le "
           "gaz est parfait et qu'il reste en équilibre chimique; c'est une "
           "leçon reçue de la thermodynamique."),
    "es": ("Para el cálculo del flujo en la tobera se supone que el gas es "
           "ideal y que permanece en equilibrio químico, pero también hay "
           "pérdidas de calor."),
    "it": ("Per il calcolo del flusso nell'ugello si assume che il gas sia "
           "ideale e che rimanga in equilibrio chimico, ma questo è solo un "
           "modello."),
}
_EU_NAMES = {"de": "German", "fr": "French", "es": "Spanish", "it": "Italian"}

# Kitap düzeyi tespit için gerçekçi paragraflar (kapı eşiği:
# DETECT_MIN_LETTERS harf, DETECT_MIN_WORDS kelime — paragraf 4 kez
# tekrarlanır).
_EU_BOOK_PARAS = {
    "de": ("Die Verbrennung in der Brennkammer erfolgt bei hohem Druck und "
           "hoher Temperatur. Für die Berechnung der Strömung in der Düse "
           "wird angenommen, dass das Gas ideal ist und sich im chemischen "
           "Gleichgewicht befindet. Über die Wärmeübertragung an die Wand "
           "ist bisher nur wenig bekannt, weil die Messungen sehr schwierig "
           "sind. Der Schub des Triebwerks hängt vor allem von der "
           "Austrittsgeschwindigkeit der Verbrennungsprodukte ab, die "
           "wiederum durch das Druckverhältnis bestimmt wird."),
    "fr": ("La combustion dans la chambre se produit à haute pression et à "
           "haute température. Pour le calcul de l'écoulement dans la "
           "tuyère, on suppose que le gaz est parfait et qu'il reste en "
           "équilibre chimique. Les pertes thermiques sont souvent "
           "négligées, mais leur effet sur la poussée reste mesurable. La "
           "poussée du moteur dépend surtout de la vitesse d'éjection des "
           "produits de combustion, qui est elle-même fixée par le rapport "
           "des pressions."),
    "es": ("La combustión en la cámara se produce a alta presión y a alta "
           "temperatura. Para el cálculo del flujo en la tobera se supone "
           "que el gas es ideal y que permanece en equilibrio químico. Las "
           "pérdidas de calor son pequeñas, pero su efecto sobre el empuje "
           "también es medible. El empuje del motor depende sobre todo de la "
           "velocidad de salida de los productos de combustión, que a su "
           "vez está determinada por la relación de presiones."),
    "it": ("La combustione nella camera avviene ad alta pressione e ad alta "
           "temperatura. Per il calcolo del flusso nell'ugello si assume che "
           "il gas sia ideale e che rimanga in equilibrio chimico. Le "
           "perdite di calore sono piccole, ma il loro effetto sulla spinta "
           "è comunque misurabile. La spinta del motore dipende soprattutto "
           "dalla velocità di uscita dei prodotti della combustione, che a "
           "sua volta è determinata dal rapporto delle pressioni."),
}

# Bol matematikli İngilizce (yanlış pozitif testi): düzyazı az, formül çok.
_EN_MATH_PARA = (
    "For an ideal rocket the thrust is given by the momentum equation and "
    "the pressure term, $F = \\dot{m} v_e + A_e (p_e - p_a)$, where "
    "$\\dot{m}$ denotes the total propellant flow and $A_e$ the exit area.\n"
    "\\begin{equation}\n"
    "v_e = \\sqrt{\\frac{2 k}{k-1} \\frac{R T_0}{M}\\left[1 - "
    "\\left(\\frac{p_e}{p_0}\\right)^{(k-1)/k}\\right]} \\label{eq:ve}\n"
    "\\end{equation}\n"
    "Substituting Eq.~\\eqref{eq:ve} into the thrust relation, with "
    "$k = 1.2$, $T_0 = 3500\\,\\mathrm{K}$ and $p_0/p_e = 70$, we obtain "
    "$c_F = 1.65$ and $I_{sp} = F / (\\dot{m} g_0) \\approx 290$ s.\n"
    "\\[\n\\frac{A_e}{A_t} = \\frac{1}{M_e}\\left[\\frac{2}{k+1}\\left(1 + "
    "\\frac{k-1}{2} M_e^2\\right)\\right]^{\\frac{k+1}{2(k-1)}}\n\\]\n"
    "The characteristic velocity $c^* = p_0 A_t / \\dot{m}$ depends only on "
    "the combustion chamber and not on the nozzle, which is why it is used "
    "to compare propellant combinations.\n"
)
_LATIN_QUOTE = (
    "\\begin{quote}\nGallia est omnis divisa in partes tres, quarum unam "
    "incolunt Belgae, aliam Aquitani, tertiam qui ipsorum lingua Celtae, "
    "nostra Galli appellantur. Hi omnes lingua, institutis, legibus inter "
    "se differunt. Horum omnium fortissimi sunt Belgae, propterea quod a "
    "cultu atque humanitate provinciae longissime absunt.\n\\end{quote}\n"
)


def _issues(report, *names):
    return [i for i in report.issues if i.check_name in names]


def _analyze(body: str, source: str = "", manual: bool = False):
    return QCEngine().analyze(body, source, manual_translation=manual)


# ===========================================================================
# core/lang_detect.py — birim testleri
# ===========================================================================

class TestLangDetectModule:
    def test_profiles_are_consistent(self):
        """en/tr profilleri ayrık; Avrupa profilleri en/tr kelimesi içermez."""
        for lang in ld.EUROPEAN_DETECT_LANGS:
            assert not (ld.PROFILES[lang] & ld.PROFILES["en"])
            assert not (ld.PROFILES[lang] & ld.PROFILES["tr"])
        assert not (ld.PROFILES["en"] & ld.PROFILES["tr"])
        # "et al." İngilizce/Türkçe akademik kalıp — Avrupa kanıtı sayılmaz
        assert "et" not in ld.EUROPEAN_FAMILY_WORDS
        assert "al" not in ld.EUROPEAN_FAMILY_WORDS
        # Türkçede sık geçen kısa kelimeler Avrupa kanıtı sayılmaz
        for w in ("de", "da", "en", "ne", "son", "su", "para", "hasta"):
            assert w not in ld.EUROPEAN_FAMILY_WORDS, w

    def test_clean_keeps_line_count_and_masks_math(self):
        body = _RU_BODY + "\n" + _TR_GOOD
        # Ön koşul: sınanan OCR artığı ve denklem içi Kiril metin gövdede
        # GERÇEKTEN var (yoksa aşağıdaki "yok" iddiaları boşa geçer).
        assert "noct" in body and "прочие" in body
        cleaned = ld.clean_latex_for_detection(body)
        assert cleaned.count("\n") == body.count("\n")
        # Denklem ortamı/\[...\] içi tamamen maskelenir (Kiril metin ve OCR
        # artığı 'noct' dahil); düzyazıdaki kelime kalır.
        assert "прочие" not in cleaned and "noct" not in cleaned
        assert "вращательных" in cleaned
        # Metin argümanları (\textit{...}) korunur, komut adı silinir
        assert "тягой" in cleaned and "\\textit" not in cleaned

    def test_russian_book_detected(self):
        code, conf = ld.detect_language(_RU_BODY)
        assert code == "ru"
        assert conf >= ld.GATE_STOP_CONFIDENCE

    @pytest.mark.parametrize("lang", sorted(_EU_BOOK_PARAS))
    def test_european_books_detected(self, lang):
        body = "\n\n".join([_EU_BOOK_PARAS[lang]] * 4)
        code, conf = ld.detect_language(body)
        assert code == lang
        assert conf >= ld.GATE_STOP_CONFIDENCE

    def test_math_heavy_english_is_english(self):
        """Yanlış pozitif kilidi: formül yoğun İngilizce 'belirsiz'e düşmez."""
        est = ld.analyze_language(_EN_MATH_PARA * 6)
        assert est.code == "en"
        assert not ld.should_stop_for_source_language(est)

    def test_english_with_latin_quote_not_stopped(self):
        """Latince alıntılı bölüm İngilizce kitabı durdurmamalı."""
        body = (_EN_MATH_PARA * 3) + _LATIN_QUOTE + (_EN_MATH_PARA * 2)
        est = ld.analyze_language(body)
        assert est.code == "en"
        assert not ld.should_stop_for_source_language(est)

    def test_english_with_russian_quote_not_stopped(self):
        body = (_EN_MATH_PARA * 5) + _RU_PARAS[1] + "\n"
        est = ld.analyze_language(body)
        assert est.code == "en"
        assert not ld.should_stop_for_source_language(est)

    def test_too_little_text_is_undetermined(self):
        est = ld.analyze_language("\\chapter{Intro}\n$E = mc^2$\nShort.")
        assert est.code == "und" and est.confidence == 0.0
        assert not ld.should_stop_for_source_language(est)

    def test_line_single_greek_letter_and_abbreviation_ignored(self):
        for text in ("Açı α ile gösterilir; burada β ihmal edilir.",
                     "Yanma ürünleri (ПС) ideal gaz kabul edilir; ЖРД için.",
                     "Bu kavram Yunancadaki λόγος kelimesinden gelir.",
                     "Şekilde 如图 gösterildiği gibi akış hızlanır."):
            assert ld.classify_script_line(
                ld.clean_latex_for_detection(text)) is None, text

    def test_line_russian_sentence_is_block(self):
        verdict = ld.classify_script_line(_RU_PARAS[1])
        assert verdict is not None
        assert (verdict.code, verdict.kind) == ("ru", "block")

    def test_gost_citation_is_not_untranslated(self):
        line = _RU_CITATION
        assert ld.looks_like_citation(line)
        assert ld.classify_script_line(line) is None
        # Kontrol: muafiyeti künye kuralı sağlıyor — yılsız aynı satır
        # çevrilmemiş Rusça blok sayılır.
        no_year = line.replace("1971", "")
        assert ld.classify_script_line(no_year).kind == "block"

    def test_long_paragraph_with_year_and_seconds_is_not_citation(self):
        """Uzun paragrafta '1957' + 'С. 5' tesadüfi eşleşmesi künye değil."""
        para = (_RU_PARAS[1] + " В 1957 г. был запущен первый спутник. "
                + _RU_PARAS[0] + " С. 5")
        assert len(para) > ld.CITATION_MAX_CHARS
        assert not ld.looks_like_citation(para)

    @pytest.mark.parametrize("lang", sorted(_EU_SENTENCES))
    def test_latin_line_classifier(self, lang):
        assert ld.classify_latin_line(_EU_SENTENCES[lang]) == lang

    def test_latin_line_classifier_ignores_turkish_and_english(self):
        for text in (_TR_PAR,
                     "Güç ölçümü için bu model de benzer sonuç verir ve üç "
                     "örnek yeterli olur.",  # ş/ğ/ı YOK, ö/ü/ç var
                     "The combustion in the chamber occurs at high pressure.",
                     "Lindqvist MA et al (2018) Relative risk "
                     "estimates in surveys: time to rethink."):
            assert ld.classify_latin_line(text) is None, text


# ===========================================================================
# A. QC — çevrilmemiş kaynak dil (Latin dışı alfabe + Avrupa dilleri)
# ===========================================================================

class TestQCUntranslatedScript:
    def test_correct_turkish_translation_of_russian_book_is_clean(self):
        """Kiril indis (matematik), kısaltma, Yunan harfi, çevirmen notu,
        künye ve Rusça kaynakça: hiçbiri çevrilmemiş sayılmaz."""
        rep = _analyze(_TR_GOOD, _RU_BODY, manual=True)
        bad = _issues(rep, "untranslated", "mixed_language_line")
        assert bad == [], [(i.line_num, i.message) for i in bad]

    def test_forgotten_russian_paragraph_is_critical(self):
        body = _TR_GOOD.replace(
            "Açı $\\alpha$", _RU_PARAS[0] + "\n\nAçı $\\alpha$")
        rep = _analyze(body, _RU_BODY)
        unt = _issues(rep, "untranslated")
        assert len(unt) == 1
        assert unt[0].severity == "critical"   # tek satır, ≥200 karakter
        assert "Russian" in unt[0].message
        assert unt[0].line_num == body.split("\n").index(_RU_PARAS[0]) + 1

    def test_consecutive_russian_lines_block_is_critical(self):
        body = _TR_PAR + "\n" + _RU_PARAS[1] + "\n" + _RU_PARAS[5] + "\n"
        unt = _issues(_analyze(body), "untranslated")
        assert len(unt) == 1
        assert unt[0].severity == "critical"
        assert "Untranslated Russian block: 2 consecutive lines" in (
            unt[0].message)

    def test_short_russian_sentence_is_medium(self):
        body = (_TR_PAR + "\n\nДавление в камере сгорания поддерживается "
                "почти постоянным.\n\n" + _TR_PAR + "\n")
        unt = _issues(_analyze(body), "untranslated")
        assert [i.severity for i in unt] == ["medium"]

    def test_mixed_turkish_russian_line(self):
        body = ("Tepki ilkesi kitapta şu cümleyle özetlenir: чем быстрее газ "
                "покидает сопло, тем большую тягу развивает двигатель при том "
                "же расходе; yani aynı debide gaz lüleden ne kadar hızlı "
                "çıkarsa itki o kadar büyük olur ve bu ilke tüm füze "
                "motorları için geçerlidir.\n")
        rep = _analyze(body)
        mixed = _issues(rep, "mixed_language_line")
        assert len(mixed) == 1 and mixed[0].severity == "medium"
        assert "Russian" in mixed[0].message
        assert _issues(rep, "untranslated") == []

    def test_untranslated_caption_is_caught(self):
        """\\caption{...} metni temizlikte korunur (eski maske siliyordu)."""
        body = (_TR_PAR + "\n\\begin{figure}[H]\n\\centering\n"
                "\\caption{Распределение давления вдоль стенки сопла при "
                "разных режимах работы}\n\\end{figure}\n")
        unt = _issues(_analyze(body), "untranslated")
        assert len(unt) == 1 and "Russian" in unt[0].message

    def test_yol_a_report_only(self):
        """Yol A: tespit edilir, skora yansır ama auto_fixable=False."""
        body = _TR_PAR + "\n\n" + _RU_PARAS[0] + "\n"
        rep = _analyze(body, manual=True)
        unt = _issues(rep, "untranslated")
        assert unt and all(not i.auto_fixable for i in unt)
        assert rep.score < 100

    def test_greek_and_cjk_blocks(self):
        body = (_TR_PAR + "\n\nΠάντα ῥεῖ καὶ οὐδὲν μένει, ἔφη ὁ Ἡράκλειτος "
                "ἐν τῷ βιβλίῳ.\n\n本章介绍液体火箭发动机的基本原理和计算方法。\n")
        msgs = [i.message for i in _issues(_analyze(body), "untranslated")]
        assert any("Greek" in m for m in msgs)
        assert any("CJK" in m for m in msgs)

    def test_hymt_cjk_artifact_not_untranslated(self):
        """2-3 karakterlik HY-MT artefaktı foreign_language_leakage'ın işi."""
        body = "Şekilde 如图 gösterildiği gibi akış hızlanır ve basınç düşer.\n"
        rep = _analyze(body)
        assert _issues(rep, "untranslated") == []
        assert _issues(rep, "foreign_language_leakage")

    def test_untranslated_russian_bibliography_heading_masks_section(self):
        body = (_TR_PAR + "\n\\section*{Список литературы}\n"
                "Гусев Н. Л. Газовая динамика сопел учебное пособие для "
                "инженеров и аспирантов\n")
        rep = _analyze(body)
        assert _issues(rep, "untranslated") == []
        # Kontrol: başlık olmadan aynı satır çevrilmemiş sayılır — muafiyeti
        # kaynakça bölümü sağlıyor.
        bare = body.replace("\\section*{Список литературы}\n", "")
        assert _issues(_analyze(bare), "untranslated")

    def test_cyrillic_heading_reported_as_untranslated_heading(self):
        body = _RU_SECTION_HEADING + "\n\n" + _TR_PAR + "\n"
        rep = _analyze(body)
        assert len(_issues(rep, "untranslated_heading")) == 1
        # Başlık Check 11'in işi — Check 2 aynı satırı çift raporlamaz
        assert _issues(rep, "untranslated") == []


class TestQCUntranslatedEuropean:
    @pytest.mark.parametrize("lang", sorted(_EU_SENTENCES))
    def test_untranslated_block_check_catches_sentence(self, lang):
        """ü/ö/ç'li Almanca/Fransızca ve İspanyolca satır eskiden
        _check_untranslated_blocks'tan kaçıyordu (İtalyanca 'English'
        etiketiyle yakalanıyordu)."""
        body = _TR_PAR + "\n\n" + _EU_SENTENCES[lang] + "\n\n" + _TR_PAR + "\n"
        unt = _issues(_analyze(body), "untranslated")
        assert len(unt) == 1, unt
        assert unt[0].line_num == 3
        assert unt[0].severity == "high"       # 100-199 karakter
        assert _EU_NAMES[lang] in unt[0].message

    @pytest.mark.parametrize("lang", sorted(_EU_SENTENCES))
    def test_foreign_word_check_catches_sentence(self, lang):
        """Mevcut yabancı kelime kontrolü (Check 19) bu dillerde çevrilmemiş
        cümleyi yakalıyor mu? (İspanyolca/İtalyanca eskiden kaçıyordu:
        aksanlı 'también' eşleşmiyor, İtalyanca listede tek eşleşme.)"""
        body = _TR_PAR + "\n\n" + _EU_SENTENCES[lang] + "\n"
        leak = _issues(_analyze(body), "foreign_language_leakage")
        assert leak, f"{lang}: foreign_language_leakage yakalamadı"

    def test_foreign_word_check_ignores_proper_names(self):
        """'Los Angeles', 'Las Vegas' Türkçe metinde meşru özel adlardır."""
        body = ("Araştırma Los Angeles ve Las Vegas şehirlerinde yapıldı; "
                "ardından Los Alamos laboratuvarı ziyaret edildi.\n")
        assert _issues(_analyze(body), "foreign_language_leakage") == []

    def test_german_heading_with_umlauts(self):
        body = "\\section{Über die Düsenströmung und die Verluste}\n\n" + _TR_PAR
        heads = _issues(_analyze(body), "untranslated_heading")
        assert len(heads) == 1 and "German" in heads[0].message

    def test_turkish_heading_with_shared_letters_not_flagged(self):
        body = "\\section{Güç ve Döküm Üretimi}\n\n" + _TR_PAR
        assert _issues(_analyze(body), "untranslated_heading") == []

    def test_bibliography_item_with_french_title_not_flagged(self):
        """Gerçek vaka (bir test kitabının çıktısı): \\item listesine dökülmüş kaynakça.
        Girdi sentetik (KARAR 26): aksansız Fransızca başlık, aynı yapı."""
        body = (_TR_PAR + "\n\\begin{itemize}\n  \\item de Lacoste PM (1791) "
                "Essai sur l'Usage de l'Algebre a la Mesure des Chances "
                "Offertes a la Majorite des voix. Lyon\n"
                "\\end{itemize}\n")
        assert _issues(_analyze(body), "untranslated") == []

    def test_multiline_display_math_not_italian(self):
        """Gerçek vaka (bir test kitabı): \\[ ... T_{ad} ... \\] içindeki 'ad'
        İtalyanca sanılmamalı (matematik tüm metinde maskelenir). Girdi
        sentetik (KARAR 26): aynı yapı, yedi T_{ad}."""
        body = (_TR_PAR + "\n\\[\n\\begin{array}{l|l} \\operatorname{Step 4: "
                "Test} \\left| T_{ad}^{(j)} - T_{ad}^{(j-1)} \\right| < \\epsilon & "
                "T_{ad} = T_{ad}^{(j)} \\\\ T_{ad}^{(j+1)} = T_{ad}^{(j)} - \\beta "
                "\\big( T_{ad}^{(j)} - T_b \\big) & \\operatorname{Return to step 1} "
                "\\end{array}\n\\]\n")
        assert _issues(_analyze(body), "untranslated") == []


class TestQCEnglishUnchanged:
    """Mevcut İngilizce kontrol BOZULMADI: aynı satırlar, aynı mesajlar."""

    _EN = ("The thrust of an ideal rocket depends on the exhaust velocity "
           "and on the pressure difference at the nozzle exit plane, which "
           "is why the expansion ratio must be chosen with great care for "
           "every mission profile.")

    def test_english_paragraph_message_unchanged(self):
        body = _TR_PAR + "\n\n" + self._EN + "\n"
        unt = _issues(_analyze(body), "untranslated")
        assert len(unt) == 1
        assert unt[0].severity == "critical"
        assert unt[0].message.startswith(
            "Untranslated English paragraph (single line,")

    def test_english_block_message_unchanged(self):
        body = _TR_PAR + "\n" + self._EN + "\n" + self._EN + "\n"
        unt = _issues(_analyze(body), "untranslated")
        assert [i.message for i in unt] == [
            "Untranslated English block: 2 consecutive lines starting at L2"]


# ===========================================================================
# B. Yol B kaynak dil kapısı (translate_book.py)
# ===========================================================================

class _FakeStats:
    segments_total = segments_translated = 10
    segments_failed = segments_skipped = 0


class _FakeTranslator:
    """LatexTranslator yerine: model yüklemez, kurulduğunu kaydeder."""

    calls: list = []
    output = "\\chapter{Motorlar}\nMAKINE CEVIRISI\n" + "makine " * 40

    def __init__(self, **kwargs):
        type(self).calls.append(kwargs)
        self.stats = _FakeStats()

    def translate(self, latex_body, checkpoint_dir=None, resume=False,
                  progress_callback=None):
        return type(self).output


@pytest.fixture
def restore_root_logger():
    """run_pipeline kök logger'ı ele geçirir; test sonrası eski hâle döndür."""
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    yield
    for h in list(root.handlers):
        if h not in old_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers[:] = old_handlers
    root.setLevel(old_level)


@pytest.fixture
def mt_env(monkeypatch, restore_root_logger):
    _FakeTranslator.calls = []
    monkeypatch.setattr(tb, "_preflight_check", lambda *a, **k: [])
    monkeypatch.setattr("latex_pipeline.translator.LatexTranslator",
                        _FakeTranslator)
    return _FakeTranslator


def _make_book(tmp_path: Path, body: str, tr_body: str | None = None) -> Path:
    out = tmp_path / "kitap"
    (out / "source").mkdir(parents=True)
    (out / "full_book.md").write_text("# Kitap\n\n" + body, encoding="utf-8")
    (out / "source" / "body.tex").write_text(body, encoding="utf-8")
    if tr_body is not None:
        (out / "source" / "translated_body.tex").write_text(
            tr_body, encoding="utf-8")
    return out


def _run(out: Path, **kwargs) -> int:
    # resume=True + body.tex -> Phase 2 (converter) atlanır, Phase 5'e gelinir
    defaults = dict(skip_translate=False, skip_compile=True, skip_qc=True,
                    book_type="science", pdf_path=None, resume=True)
    defaults.update(kwargs)
    return tb.run_pipeline(str(out), "Test Kitap", **defaults)


def _log(out: Path) -> str:
    return (out / "pipeline.log").read_text(encoding="utf-8")


_EN_BOOK = "\\chapter{Rocket Propulsion}\n\n" + _EN_MATH_PARA * 6


# Sentetik Çince metin (bu test için yazıldı; kitaptan alınmadı): yerel model
# yolunun desteklemediği bir dil (TEX_SOURCE_LANGS dışında, CJK kapsam dışı).
_ZH_PARA = ("火箭发动机的推力取决于喷管出口处气体的速度和压力，"
            "设计工况下出口压力等于环境压力。")
_ZH_BODY = "\\chapter{推力}\n\n" + "\n\n".join([_ZH_PARA] * 40) + "\n"


class TestYolBSourceLanguageGate:
    # K5 (2026-09-28, KARARLAR 14): yerel model yolunun kaynak dilleri
    # TEX_SOURCE_LANGS (en/de/ru/fr/es/it). Eskiden bu sınıf "İngilizce
    # değilse çıkış 4" davranışını kilitliyordu; artık desteklenen dil zincire
    # akar, çıkış 4 yalnız DESTEKLENMEYEN dilde (burada sentetik Çince).
    def test_exit_code_constant_documented(self):
        assert tb.EXIT_SOURCE_UNSUPPORTED == 4
        assert tb.EXIT_SOURCE_NOT_ENGLISH == tb.EXIT_SOURCE_UNSUPPORTED
        assert tb.EXIT_SOURCE_UNSUPPORTED not in (0, 1, 2, 3, 130)
        assert "4    kaynak dil desteklenmiyor" in tb.__doc__

    def test_russian_book_translated_as_russian(self, tmp_path, mt_env):
        out = _make_book(tmp_path, _RU_BODY)
        assert _run(out) == 0
        assert [c["source_lang"] for c in mt_env.calls] == ["ru"]
        log = _log(out)
        assert "Kaynak dil: Rusça" in log
        assert "Phase 5: MT Translation (RU -> TR" in log
        assert "desteklenmiyor" not in log

    @pytest.mark.parametrize("lang,name", [("de", "Almanca"),
                                           ("fr", "Fransızca")])
    def test_european_book_translated_in_its_language(self, tmp_path, mt_env,
                                                      lang, name):
        out = _make_book(tmp_path, "\n\n".join([_EU_BOOK_PARAS[lang]] * 4))
        assert _run(out) == 0
        assert [c["source_lang"] for c in mt_env.calls] == [lang]
        assert f"Kaynak dil kapısı: {name} (kod={lang}" in _log(out)

    def test_unsupported_book_stops_before_mt(self, tmp_path, mt_env):
        out = _make_book(tmp_path, _ZH_BODY)
        code = _run(out)
        assert code == tb.EXIT_SOURCE_UNSUPPORTED
        assert mt_env.calls == [], "MT çevirmeni kurulmamalıydı"
        log = _log(out)
        assert ("Bu kitabın dili desteklenmiyor (tespit: Çince/Japonca/Korece). "
                "Yerel model yalnız şu dillerden Türkçeye çevirir: İngilizce, "
                "Almanca, Rusça, Fransızca, İspanyolca ve İtalyanca.") in log
        assert "Phase 5: MT Translation" not in log
        assert not (out / "source" / "translated_body.tex").exists()

    def test_english_math_heavy_book_passes(self, tmp_path, mt_env):
        out = _make_book(tmp_path, _EN_BOOK)
        assert _run(out) == 0
        assert mt_env.calls, "İngilizce kitapta MT çalışmalıydı"
        assert "İngilizce değil" not in _log(out)

    def test_flag_skips_gate(self, tmp_path, mt_env):
        out = _make_book(tmp_path, _RU_BODY)
        assert _run(out, source_lang_check=False) == 0
        assert mt_env.calls
        assert "--dil-kontrolu-yok ile atlandı" in _log(out)

    def test_undetermined_text_warns_and_continues(self, tmp_path, mt_env):
        out = _make_book(tmp_path, "\\chapter{A}\n$x=1$\n" + "$y$ " * 60)
        assert _run(out) == 0
        assert mt_env.calls
        assert "Kaynak dil belirlenemedi" in _log(out)

    def test_export_tex_path_never_measures(self, tmp_path, mt_env):
        """--export-tex (skip_translate) Rusça kitapta da kapıya takılmaz."""
        out = _make_book(tmp_path, _RU_BODY)
        code = _run(out, skip_translate=True, resume=False)
        assert code == 0
        assert "Kaynak dil kapısı" not in _log(out)

    def test_import_tex_path_never_measures(self, tmp_path, mt_env):
        """--import-tex (manual_translation) Rusça kaynakta kapıya takılmaz."""
        out = _make_book(tmp_path, _RU_BODY, tr_body=_TR_GOOD)
        code = _run(out, skip_translate=True, manual_translation=True)
        assert code == 0
        log = _log(out)
        assert "Kaynak dil kapısı" not in log
        assert "İngilizce değil" not in log

    def test_force_mt_on_yol_a_book_is_gated_marker_kept(self, tmp_path,
                                                         mt_env):
        """Yol A'da çevrilmiş, dili desteklenmeyen kitap + --force-mt: kapı
        yine durdurur ve Yol A kilidi (marker) SİLİNMEZ. (K5: Rusça artık
        destekleniyor; koruma desteklenmeyen dille sınanır.)"""
        out = _make_book(tmp_path, _ZH_BODY)
        marker = out / "source" / "MANUAL_TRANSLATION.marker"
        marker.write_text("import: test\n", encoding="utf-8")
        assert _run(out, force_mt=True) == tb.EXIT_SOURCE_UNSUPPORTED
        assert marker.exists(), "kapı durdurduğu halde Yol A kilidi silindi"
        assert mt_env.calls == []

    def test_yol_a_lock_message_keeps_priority(self, tmp_path, mt_env):
        """Marker'lı kitap, --force-mt YOK: eski Yol A kilidi (exit 2) aynen."""
        out = _make_book(tmp_path, _RU_BODY)
        (out / "source" / "MANUAL_TRANSLATION.marker").write_text(
            "import: test\n", encoding="utf-8")
        assert _run(out) == 2
        assert "Kaynak dil kapısı" not in _log(out)

    def test_error_reaches_event_stream(self, tmp_path, mt_env, monkeypatch):
        """ERROR log -> core/events.py 'error' olayı (arayüz last_error)."""
        events = tmp_path / "events.jsonl"
        monkeypatch.setenv("KITAPCEVIR_EVENT_LOG", str(events))
        out = _make_book(tmp_path, _ZH_BODY)   # K5: desteklenmeyen dil
        assert _run(out) == tb.EXIT_SOURCE_UNSUPPORTED
        errors = [json.loads(line) for line in
                  events.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        errors = [e for e in errors if e.get("kind") == "error"]
        assert errors and "desteklenmiyor" in errors[-1]["message"]

    def test_gate_helper_uncertain_only_warns(self, caplog):
        """Belirsiz tespit (güven eşiğin altında) durdurmaz, uyarır."""
        mixed = (_EN_MATH_PARA * 2) + "\n\n".join(
            [_EU_BOOK_PARAS["de"]] * 2)
        est = ld.analyze_language(mixed)
        # Uyarı dalı gerçekten çalışmalı: tespit İngilizceye dönerse (ya da
        # güven eşiği aşarsa) bu test bir şey ölçmez — ikisi birlikte şart.
        assert est.code != "en" and est.confidence < ld.GATE_STOP_CONFIDENCE
        log = logging.getLogger("test.gate")
        with caplog.at_level(logging.INFO, logger="test.gate"):
            assert tb._source_language_gate(mixed, log) is None
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        # K5: düşük güvenli tespit dili zincire VERMEZ, İngilizce varsayılır
        # (eski davranış); uyarı --kaynak-dil'i önerir.
        assert any("Kaynak dil belirsiz" in r.getMessage()
                   and "--kaynak-dil" in r.getMessage()
                   for r in caplog.records)
        assert tb._source_language_decision(mixed, log) == (None, "en")

    def test_cli_flag_forwarded(self, tmp_path, monkeypatch,
                                restore_root_logger):
        book = tmp_path / "kitap.pdf"
        book.write_bytes(b"%PDF-1.4\n" + b"x" * 2048)
        seen = {}

        def _fake_pipeline(output_dir, book_name, **kwargs):
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(tb, "run_pipeline", _fake_pipeline)
        monkeypatch.setattr(tb, "detect_pages", lambda p: 3)
        monkeypatch.setattr(tb, "run_extraction", lambda *a, **k: True)
        for extra, expected in (([], True), (["--dil-kontrolu-yok"], False)):
            seen.clear()
            monkeypatch.setattr(sys, "argv", [
                "translate_book.py", str(book), "--skip-extract",
                "--output-dir", str(tmp_path / "out"), *extra])
            with pytest.raises(SystemExit):
                tb.main()
            assert seen["source_lang_check"] is expected


# ===========================================================================
# C. Talimat ve 1_cikar.py dil-nötr
# ===========================================================================

class TestDirectiveLanguageNeutral:
    def test_talimat_lists_supported_languages(self):
        text = (PROJECT_ROOT / "CEVIRI_TALIMATI.md").read_text(encoding="utf-8")
        for lang in ("İngilizce", "Almanca", "Rusça", "Fransızca",
                     "İspanyolca", "İtalyanca"):
            assert lang in text, lang
        assert "kapsam dışı" in text          # CJK
        assert "İngilizce LaTeX" not in text
        assert "`İngilizce<TAB>Türkçe`" not in text

    def test_talimat_has_ocr_rule(self):
        text = (PROJECT_ROOT / "CEVIRI_TALIMATI.md").read_text(encoding="utf-8")
        assert "p_{\\text{н}}" in text and "p_{\\rm H}" in text
        assert "noct" in text and "пост" in text
        assert "kaynak PDF" in text.lower() or "Kaynak PDF" in text
        assert "Çevirmen notu" in text

    def test_1_cikar_user_texts_language_neutral(self):
        src = (PROJECT_ROOT / "1_cikar.py").read_text(encoding="utf-8")
        assert "İngilizce LaTeX" not in src
        assert "Rusça" in src
