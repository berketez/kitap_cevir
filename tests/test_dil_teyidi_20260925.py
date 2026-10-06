"""Yol A dil teyidi (25 Eylül 2026): çevrilmemiş Almanca paragrafın QC tespiti.

Negatif kontrolde (bir paragraf kaynak dilde bırakıldı) Fransızca, İspanyolca,
İtalyanca ve Rusça paragraf CRITICAL "Untranslated … paragraph" alırken
Almanca paragraf yalnız MEDIUM "Mixed Turkish/English" alıyordu: ü/ö
has_turkish'i, umlautsuz bir cümle de _is_likely_english'i tetikliyor ve
satır "karışık" dalına düşüyordu. classify_latin_line satırı doğru biçimde
'de' diye tanıdığı hâlde bu karar kullanılmıyordu.
"""

from latex_pipeline.qc_engine import QCEngine

# Sentetik Almanca (kitaptan alıntı değil): ü/ö içeren ve içermeyen cümleler.
ALMANCA = (
    "Die Übertragung von Energie durch eine feste Wand hängt von ihrer Dicke ab. "
    "In der Praxis unterscheidet man drei Fälle, die wir nacheinander betrachten. "
    "Dieses Kapitel behandelt nur den einfachsten Fall, bei dem sich das Material "
    "nicht bewegt und die Temperaturen an beiden Seiten konstant bleiben."
)
TURKCE = (
    "Katı bir duvardan enerji aktarımı duvarın kalınlığına bağlıdır. Uygulamada "
    "üç durum ayırt edilir ve bunlar sırayla incelenir. Bu bölüm yalnız en basit "
    "durumu, yani malzemenin hareket etmediği ve iki yüzdeki sıcaklıkların sabit "
    "kaldığı durumu ele alır."
)
KARISIK_INGILIZCE = (
    "Bu bölümde görüldüğü gibi, the transfer of energy through a solid wall depends "
    "on its thickness and on the material that the wall is made of in practice."
)


def _rapor(ceviri, kaynak):
    return QCEngine(book_type="science").analyze(ceviri, kaynak, manual_translation=True)


def _untranslated(rapor):
    return [i for i in rapor.issues if "ntranslated" in i.message
            and "heading" not in i.message]


def test_cevrilmemis_almanca_paragraf_critical():
    kaynak = f"\\chapter{{Wärmeleitung}}\n\n{ALMANCA}\n\n{ALMANCA}\n"
    ceviri = f"\\chapter{{Isı İletimi}}\n\n{TURKCE}\n\n{ALMANCA}\n"
    bulgular = _untranslated(_rapor(ceviri, kaynak))
    assert any(i.severity == "critical" and "German" in i.message for i in bulgular)
    assert not [i for i in _rapor(ceviri, kaynak).issues
                if i.check_name == "mixed_language_line"]


def test_tam_cevrilmis_metinde_almanca_uyarisi_yok():
    kaynak = f"\\chapter{{Wärmeleitung}}\n\n{ALMANCA}\n"
    ceviri = f"\\chapter{{Isı İletimi}}\n\n{TURKCE}\n"
    assert _untranslated(_rapor(ceviri, kaynak)) == []


def test_turkce_ingilizce_karisik_satir_eskisi_gibi_mixed():
    kaynak = f"\\chapter{{Heat}}\n\n{KARISIK_INGILIZCE}\n"
    ceviri = f"\\chapter{{Isı}}\n\n{KARISIK_INGILIZCE}\n"
    rapor = _rapor(ceviri, kaynak)
    assert not any("German" in i.message for i in rapor.issues)
