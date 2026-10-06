"""TOC tabanlı başlık seviyesi düzeltici testleri.

Marker'ın başlık over-promotion bug'ı (13 bölümlük kitapta 61 \\chapter)
için PDF TOC outline'ını yer-gerçeği alan relevel() fonksiyonunu test eder.
"""
import re

from latex_pipeline.toc_releveler import relevel, build_toc_index, _normalize


def _count(body, cmd):
    return len(re.findall(r"\\" + cmd + r"\*?\{", body))


# Springer-tarzı outline: 1=Part/önmadde, 2=Chapter, 3=section, 4=subsection
TOC = [
    (1, "Preface", 1),
    (1, "Part I: Foundations", 2),
    (2, "Chapter 1: Getting Started", 3),
    (3, "Introduction", 3),
    (3, "Background and Motivation", 5),
    (2, "Chapter 2: Core Methods", 10),
    (3, "The Main Algorithm", 11),
    (4, "A Worked Example", 12),
    (1, "Part II: Applications", 20),
    (2, "Chapter 3: Real World Use", 21),
]


def test_over_promotion_demoted():
    """Marker hepsini \\chapter yapmış -> doğru seviyelere inmeli."""
    body = "\n".join([
        r"\chapter{Preface}",
        r"\chapter{Part I Foundations}",
        r"\chapter{Getting Started}",
        r"\chapter{Introduction}",
        r"\chapter{Background and Motivation}",
        r"\chapter{Core Methods}",
        r"\chapter{The Main Algorithm}",
        r"\chapter{A Worked Example}",
        r"\chapter{Part II Applications}",
        r"\chapter{Real World Use}",
    ])
    new, rep = relevel(body, TOC)
    assert rep["applied"] is True
    # 3 gerçek bölüm (Getting Started, Core Methods, Real World Use)
    assert len(re.findall(r"\\chapter\{", new)) == 3
    # 2 part
    assert len(re.findall(r"\\part\{", new)) == 2
    # Preface numarasız önmadde
    assert r"\chapter*{Preface}" in new
    # section/subsection üretildi
    assert _count(new, "section") >= 2
    assert _count(new, "subsection") >= 1


def test_no_heading_loss():
    """Başlık sayısı korunur (hiçbir başlık silinmez/eklenmez)."""
    body = "\n".join([
        r"\chapter{Getting Started}",
        r"\chapter{Introduction}",
        r"\chapter{Core Methods}",
        r"\section{The Main Algorithm}",
    ])
    before = sum(_count(body, c) for c in ["part", "chapter", "section", "subsection", "subsubsection"])
    new, _ = relevel(body, TOC)
    after = sum(_count(new, c) for c in ["part", "chapter", "section", "subsection", "subsubsection"])
    assert before == after


def test_part_detection():
    body = r"\chapter{Part I Foundations}" + "\n" + r"\section{Part II Applications}"
    new, _ = relevel(body, TOC)
    assert len(re.findall(r"\\part\{", new)) == 2


def test_frontmatter_unnumbered():
    body = r"\chapter{Preface}"
    new, _ = relevel(body, TOC)
    assert r"\chapter*{Preface}" in new


def test_stray_toc_ref_not_promoted_to_part():
    """Contents'ten sızan subsection 'Part I' part'a 3 seviye terfi etmemeli."""
    body = "\n".join([
        r"\subsection{Part I Foundations}",   # stray TOC referansı
        r"\chapter{Part I Foundations}",      # gerçek part divider
        r"\chapter{Getting Started}",
    ])
    new, _ = relevel(body, TOC)
    # Sadece 1 \part olmalı (gerçek olan), stray subsection kalmalı
    assert len(re.findall(r"\\part\{", new)) == 1
    assert r"\subsection{Part I Foundations}" in new


def test_ocr_fuzzy_match():
    """OCR hatalı başlık ('Ouestions') yine de eşleşmeli."""
    toc = [(2, "Chapter 1: Harbor Questions", 1)]
    body = r"\chapter{Harbor Ouestions}"  # O yerine Q OCR hatası
    new, rep = relevel(body, toc, threshold=0.82)
    # chapter seviyesinde kalır (eşleşti, demote olmadı)
    assert r"\chapter{Harbor Ouestions}" in new


def test_empty_toc_safe_fallback():
    """TOC yoksa body AYNEN döner (güvenli fallback)."""
    body = r"\chapter{A}" + "\n" + r"\chapter{B}"
    new, rep = relevel(body, [])
    assert new == body
    assert rep["applied"] is False


def test_correct_marker_self_disables():
    """Marker doğru seviyelemiş (chapter sayısı ~ TOC) -> demotion yapılmaz."""
    body = "\n".join([
        r"\chapter{Getting Started}",
        r"\chapter{Core Methods}",
        r"\chapter{Real World Use}",
    ])
    new, rep = relevel(body, TOC)
    # 3 chapter, TOC'ta 3 chapter -> over-promotion yok, demotion 0
    assert rep["demoted"] == 0
    assert len(re.findall(r"\\chapter\{", new)) == 3


def test_nested_braces_in_title():
    """Başlıkta iç içe parantez (\\textit{...}) dengeli ayrıştırılır."""
    toc = [(2, "Chapter 1: Limits of Homo economicus", 1)]
    body = r"\chapter{Limits of \textit{Homo economicus}}"
    new, rep = relevel(body, toc)
    # Başlık içeriği (parantezleriyle) korunur
    assert r"\textit{Homo economicus}" in new


def test_normalize_strips_prefix_and_number():
    assert _normalize("Chapter 1: Getting Started") == "getting started"
    assert _normalize("2.2.3 Leaky ReLU") == "leaky relu"
    assert _normalize(r"Limits of \textit{Homo economicus}") == "limits of homo economicus"
