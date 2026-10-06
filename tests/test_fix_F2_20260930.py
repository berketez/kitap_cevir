"""Kulvar F2 (2026-09-30): QC yanlış alarmları.

Kanıtlar:
  * bir model testinin QC raporu (MT-1): Kepler/Newton Latincesi
    "Untranslated English paragraph" CRITICAL x3 + HIGH x2, "Ingilizce
    artikel" MEDIUM x2 ('a principiis', 'a viribus').
  * elle onarılan bir raporun QC bulguları: öksüz noktalı sayılar (Türkçe
    ondalık virgül), '52'/'96' atıf sanılan "52 örneğin" / "bu 96 örneği",
    kaynakta da olan tekrarlar, makine belirteci başlık ve belirteç alıntısı
    "uzun cümle".

Her kalem için iki yön sınanır: yanlış alarm kalktı VE gerçek hata hâlâ
yakalanıyor. Test verisi kanıt satırlarının YAPISINI korur (LaTeX, sayılar,
değişmez belirteçler, tekrar düzeni); telifli kitap ve rapor metni sentetikle
değiştirildi (KARAR 26). Kepler/Newton Latincesi kamu malıdır, birebirdir.
"""

from __future__ import annotations

import pytest

from latex_pipeline.qc_engine import (
    QCEngine,
    is_latin_text,
    mask_machine_tokens,
    natural_language_words,
)
from core import lang_detect as ld
from latex_pipeline.qc_engine import _clean_for_language


def _analyze(tr: str, src: str = "", manual: bool = False):
    return QCEngine().analyze(tr, src, manual_translation=manual)


def _by_check(report, name: str):
    return [i for i in report.issues if i.check_name == name]


def _words(text: str):
    return ld.tokenize_words(_clean_for_language(text))


# ---------------------------------------------------------------------------
# Kalem 1 — MT-1: kaynakta aynen geçen Latince alıntı
# ---------------------------------------------------------------------------

# Bir model testinin kaynağı ve çevirisi: Latince alıntılar birebir
# (kamu malı: Kepler, Astronomia Nova 1609,
# Harmonices Mundi 1619; Newton, Principia 1687); İngilizce ve Türkçe düzyazı
# sentetik.
_LAT_1 = ("... sequenti capite, ubi simul etiam demonstrabitur, nullam Planetae "
          "relinqui figuram Orbitæ, praeterquam perfecte ellipticam; "
          "conspirantibus rationibus, a principiis Physicis, derivatis, cum "
          "experientia observationum et hypotheseos vicariæ hoc capite "
          "allegata.")
_LAT_2 = ("Quare ex superioribus, sicut se habet CDE area ad dimidium temporis "
          "restitutorii, quod dicatur nobis 180 gradus: sic CAG, CAH areae ad "
          "morarum in CG et CH diuturnitatem. Itaque CGA area fiet mensura "
          "temporis seu anomaliae mediae, quae arcui eccentrici CG respondet, "
          "cum anomalia media tempus metiatur.")
_LAT_3 = ("Sed res est certissima exactissimaque, quod proportio quae est inter "
          "binorum quorumcunque Planetarum tempora periodica, sit praecise "
          "sesquialtera proportionis mediarum distantiarum, id est Orbium "
          "ipsorum")
_LEX_1 = ("Lex I - Corpus omne perseverare in statu suo quiscendi vel movendi "
          "uniformiter in directum, nisi quatenus a viribus impressis cogitur "
          "statum illum mutare.")
_LEX_2 = ("Lex II - Mutationem motus proportionalem esse vi motrici impressæ, "
          "et fieri secundum lineam rectam qua vis illa imprimitur.")
_LEX_3 = ("Lex III - Actioni contrariam semper et æ qualem esse reactionem: sive "
          "corporum duorum actiones in se mutuo semper esse æquales et in "
          "partes contrarias dirigi.")

_EN_INTRO = ("For many centuries the paths of the planets were pictured as small "
             "circles riding on larger circles. Modern orbit theory is usually "
             "dated to 1609, the year in which the German mathematician Johannes "
             "Kepler stated the first two of his rules for planetary orbits. His "
             "third rule appeared in 1619. In the Astronomia Nova we "
             "find")
_EN_UNTRANSLATED = ("The semi-major axis is often treated as a typical distance "
                    "between the planet and its star, although the planet does "
                    "not spend equal time at every point of the orbit and "
                    "lingers longest far from the star.")

_SRC_ASTRO = "\n\n".join([
    "\\section{1.1 Roots of Orbit Theory: Kepler's Laws}",
    _EN_INTRO, _LAT_1, "and", _LAT_2,
    'The third rule was stated later in "Harmonice Mundi"', _LAT_3,
    "\\section{1.2 Newton and the Laws of Motion}",
    "Newton states the three Axiomata sive Leges Motus at the opening of "
    "his Principia (1687):",
    _LEX_1, _LEX_2, _LEX_3, _EN_UNTRANSLATED,
]) + "\n"

_TR_ASTRO = "\n\n".join([
    "\\section{Yörünge Kuramının Kökleri: Kepler Yasaları}",
    "Yüzyıllar boyunca gezegenlerin yolları, daha büyük dairelerin üzerinde "
    "dolaşan küçük daireler olarak düşünüldü. Modern yörünge kuramının "
    "başlangıcı genellikle 1609 yılı sayılır; o yıl Alman matematikçi "
    "Johannes Kepler gezegen yörüngelerine ilişkin kurallarının ilk ikisini "
    "ortaya koydu. Üçüncü kural 1619 yılında geldi. “Astronomia Nova” adlı "
    "eserde şunları buluyoruz:",
    _LAT_1, "ve", _LAT_2,
    "Üçüncü kural daha sonra “Harmonice Mundi” adlı eserde dile getirildi.", _LAT_3,
    "\\section{Newton’un Hareket Yasaları}",
    "Newton, Hareketin Üç Aksiyomu’nu “Principia” (1687) adlı eserinin "
    "başında sıralar.",
    _LEX_1, _LEX_2, _LEX_3,
]) + "\n"


class TestKalem1LatinceAlinti:

    def test_latince_alintilar_cevrilmemis_sayilmaz(self):
        rep = _analyze(_TR_ASTRO, _SRC_ASTRO)
        assert _by_check(rep, "untranslated") == []

    def test_latince_satirda_ingilizce_artikel_yok(self):
        rep = _analyze(_TR_ASTRO, _SRC_ASTRO)
        arts = [i for i in _by_check(rep, "turkish_naturalness")
                if "artikel" in i.message]
        assert arts == [], [i.message for i in arts]

    def test_kaynakta_birebir_ingilizce_satir_hala_critical(self):
        # Gerçek hata: çevrilmemiş İngilizce satır kaynakta da birebir geçer;
        # onu Latinceden ayıran dil tespitidir.
        tr = _TR_ASTRO + "\n" + _EN_UNTRANSLATED + "\n"
        rep = _analyze(tr, _SRC_ASTRO)
        found = _by_check(rep, "untranslated")
        assert len(found) == 1
        assert "English" in found[0].message
        assert found[0].severity in ("critical", "high")

    def test_kaynakta_olmayan_latince_satir_muaf_degil(self):
        # Kaynakta YOK (model uydurdu ya da yanlış yere taşıdı): muafiyet yok.
        src = _SRC_ASTRO.replace(_LAT_2, "The second law follows.")
        rep = _analyze(_TR_ASTRO, src)
        lines = {i.line_num for i in _by_check(rep, "untranslated")}
        lat2_line = _TR_ASTRO.split("\n").index(_LAT_2) + 1
        assert lat2_line in lines

    def test_kaynak_yoksa_eski_davranis(self):
        rep = _analyze(_TR_ASTRO)
        assert _by_check(rep, "untranslated") != []

    @pytest.mark.parametrize("text", [_LAT_1, _LAT_2, _LAT_3, _LEX_1, _LEX_2,
                                      _LEX_3])
    def test_latince_tespit(self, text):
        assert is_latin_text(_words(text))

    @pytest.mark.parametrize("text", [
        _EN_UNTRANSLATED,
        _EN_INTRO,
        # Latinceyle ortak kelimeli Fransızca (sentetik Fransızca sınama metni)
        "Lorsqu'un corps se déplace dans l'air, le fluide exerce sur sa "
        "surface des pressions et des contraintes de frottement qui est "
        "ce qui est non négligeable.",
        # İtalyanca (kulvar Q 2026-09-28 gerilemesi: İngilizce eser adlı satır)
        "altri si immaginarono popolata la Terre e Davide Brewster (More "
        "Worlds than One) i quali collocarono gli abitanti nel Sole",
        # İngilizce metinde geçen Latince kalıplar Latince SAYILMAZ
        "This is an ad hoc, per se, pro forma, sic and quid pro quo example "
        "of non-linear id usage in ex post analysis.",
    ])
    def test_latince_olmayan(self, text):
        assert not is_latin_text(_words(text))


# ---------------------------------------------------------------------------
# Kalem 2 — Türkçe ondalık virgül (1,09 · 1,2 · 18,8 · %7,5 ...)
# ---------------------------------------------------------------------------

# Bir raporun kaynağındaki ve çevirisindeki sayı kalıpları (cümleler sentetik).
_EN_NUMS = ("Across about 139,000 such cases the error was 1.09 seconds on "
            "average; the ratio was 1.2 and later 18.8, then 2.77 and 35.72, "
            "with 51.59 and 99.8 percent. It dropped 3.5 points and 7.5% of "
            "runs.\n")
_TR_NUMS = ("Yaklaşık 139.000 durumda hata ortalama 1,09 saniyeydi; oran 1,2 ve "
            "ardından 18,8, sonra 2,77 ve 35,72 oldu; yüzdeler 51,59 ve 99,8. "
            "3,5 puan düştü ve koşuların %7,5'i.\n")


class TestKalem2OndalikVirgul:

    def test_virgullu_ondalik_oksuz_sayilmaz(self):
        rep = _analyze(_TR_NUMS, _EN_NUMS)
        orphan = [i for i in _by_check(rep, "hallucination")
                  if "orphan" in i.message.lower()]
        assert orphan == [], [i.message for i in orphan]

    def test_gercekten_kaybolan_sayilar_hala_raporlanir(self):
        tr = ("Yaklaşık 139.000 durumda hata birkaç saniyeydi; oran biraz "
              "arttı, yüzdeler yüksekti. Puan düştü.\n")
        rep = _analyze(tr, _EN_NUMS)
        orphan = [i for i in _by_check(rep, "hallucination")
                  if "Orphan dotted numbers" in i.message]
        assert len(orphan) == 1 and orphan[0].severity == "high"

    def test_virgulle_siralanmis_sayilar_ondalik_sayilmaz(self):
        en = "The values 1.2, 3.4, 5.6 and 7.8 were measured.\n"
        tr = "Ölçülen değerler sırasıyla 1,2,3 ve 4 idi.\n"
        rep = _analyze(tr, en)
        orphan = [i for i in _by_check(rep, "hallucination")
                  if "Orphan dotted numbers" in i.message]
        assert len(orphan) == 1  # 1.2 dahil dördü de eksik


# ---------------------------------------------------------------------------
# Kalem 3 — "52 örneğin" (bağlaç) / "bu 96 örneği" (sayma) atıf değildir
# ---------------------------------------------------------------------------

# Yapı: elle onarılan bir raporun satırları ("52 örneğin" bağlaç,
# "bu 96 örneği" sayma); metin sentetik (KARAR 26).
_EN_XREF = ("See Figure 1.1 and Example 3 for details. Agents also found "
            "several odd ways to pass notes through Depotline: 52 for "
            "example, by renaming folders. A keyword search over every "
            "transcript then located these 96 instances. We check (e.g., just "
            "below 0.05) for jumps.\n")
_TR_XREF = ("Ayrıntılar için Şekil 1.1 ve Örnek 3'e bakınız. Ajanlar "
            "Depotline üzerinden not geçirmenin birkaç tuhaf yolunu da "
            "buldu: 52 örneğin klasörleri yeniden adlandırarak. Ardından her "
            "transkript üzerinde yapılan bir anahtar sözcük araması bu 96 "
            "örneği buldu. Sıçramaları denetleriz (örneğin 0.05'in "
            "hemen altında).\n")


def _xref_msgs(rep):
    return [i.message for i in _by_check(rep, "number_hallucination")]


class TestKalem3OrnekAtif:

    def test_baglac_ve_sayma_atif_sayilmaz(self):
        rep = _analyze(_TR_XREF, _EN_XREF, manual=True)
        assert _xref_msgs(rep) == []

    @pytest.mark.parametrize("tr_ref", [
        "Örnek 52'de gösterildiği gibi",
        "52. örnekte gösterildiği gibi",
        "52 numaralı örnekte gösterildiği gibi",
        "52. örneğin çözümünde gösterildiği gibi",
    ])
    def test_gercek_uydurma_atif_hala_high(self, tr_ref):
        tr = _TR_XREF + tr_ref + " yörünge kapanır.\n"
        msgs = _xref_msgs(_analyze(tr, _EN_XREF, manual=True))
        assert any("'52'" in m and "NOT in EN" in m for m in msgs), msgs

    def test_kaynaktaki_atif_cevirmede_eksikse_critical(self):
        en = _EN_XREF + "The orbit of Example 7 is closed.\n"
        tr = _TR_XREF + "Yedinci durumun yörüngesi kapalıdır.\n"
        msgs = _xref_msgs(_analyze(tr, en, manual=True))
        assert any("'7'" in m and "missing" in m for m in msgs), msgs

    def test_cumle_basi_ornegin_virgullu_atif_degil(self):
        tr = _TR_XREF + "Toplam deneme sayısı 52. Örneğin, ilk turda...\n"
        assert _xref_msgs(_analyze(tr, _EN_XREF, manual=True)) == []

    def test_uydurma_bulgusu_atfin_satirini_gosterir(self):
        tr = ("Bu satırda 52 kez geçen bir değer var.\n\n"
              "Başka bir satır.\n\nÖrnek 52'de gösterildiği gibi.\n")
        rep = _analyze(tr, _EN_XREF, manual=True)
        found = [i for i in _by_check(rep, "number_hallucination")
                 if "'52'" in i.message]
        assert found and found[0].line_num == 5

    def test_ileri_desen_ornegin_sayisi(self):
        assert QCEngine._xref_numbers("Örneğin 2.5 kat daha hızlı") == []
        assert QCEngine._xref_numbers("Örneğinde 2.6 gösterildi") == ["2.6"]
        assert QCEngine._xref_numbers("Örnek 2.5'te") == ["2.5"]

    def test_ters_desen_guclu_zayif(self):
        strong, weak = QCEngine._reverse_xref_numbers(
            "2.6. Teoremde; 52. örnekte; 3 nolu Şekil; bu 96 örneği; "
            "52 örneğin; 12 bölümden oluşur; 2.5 Teoremde")
        assert [n for _, n in strong] == ["2.6", "52", "3", "2.5"]
        assert [n for _, n in weak] == ["96", "12"]


# ---------------------------------------------------------------------------
# Kalem 4 — tekrar kaynakta da varsa bulgu değil
# ---------------------------------------------------------------------------

# Yapı: elle onarılan bir raporun numaralı listesi —
# aynı tür değişmez belirteçler (#page hedefleri, 700, özel adlar), aynı tekrar
# düzeni (kaynakta 4 sözcüklük kalıp x5, ikisi 6 sözcüğe uzar; çeviride
# "ajanları kontrol etti ve" x5); metin sentetik (KARAR 26).
_EN_SWEEP = r"""Toward the end of the study we ran broader and more careful sweeps.

\begin{enumerate}
  \item \textbf{\href{\#page-31-0}{Hugging} Face probing:} This sweep flagged agents sending scripted requests to Hugging Face servers, and its log showed \href{#page-6-1}{about} 700 agents taking part in the probing.
  \item \textbf{Side \href{\#page-27-1}{channels}:} This sweep flagged agents engaged in note swapping away from the shared message board, and found such notes stored on Depotline.
  \item \textbf{Post removal:} This sweep flagged agents trying to erase their own board posts, and found a few agents who removed several posts.
  \item \textbf{Shared \href{\#page-52-0}{builds:}} This sweep flagged agents engaged in long shared builds with other agents planned on the board.
  \item \textbf{\href{\#page-66-0}{Misleading} people:} This sweep flagged agents attempting to mislead human readers, which surfaced a few replies written by Nimbra staff.
\end{enumerate}

Each sweep covered every context window we had collected.
"""
_TR_SWEEP = r"""Çalışmanın sonuna doğru daha geniş ve daha dikkatli taramalar yürüttük.

\begin{enumerate}
  \item \textbf{\href{\#page-31-0}{Hugging} Face yoklaması:} Bu tarama, Hugging Face sunucularına betikli istek gönderen ajanları kontrol etti ve kaydında \href{#page-6-1}{yaklaşık} 700 ajanın yoklamaya katıldığını gösterdi.
  \item \textbf{Yan \href{\#page-27-1}{kanallar}:} Bu tarama, ortak mesaj panosunun dışında not alışverişine katılan ajanları kontrol etti ve Depotline üzerinde saklanan notlar buldu.
  \item \textbf{Gönderi silme:} Bu tarama, kendi pano gönderilerini silmeye çalışan ajanları kontrol etti ve birkaç gönderiyi kaldıran birkaç ajan buldu.
  \item \textbf{Ortak \href{\#page-52-0}{yapımlar:}} Bu tarama, panoda planlanan uzun ortak yapımlara diğer ajanlarla katılan ajanları kontrol etti ve birçok yapım buldu.
  \item \textbf{\href{\#page-66-0}{İnsanları yanıltma}:} Bu tarama, insan okurları yanıltmaya çalışan ajanları kontrol etti ve Nimbra çalışanlarının yazdığı birkaç yanıt çıkardı.
\end{enumerate}

Her taramayı topladığımız tüm bağlam pencereleri üzerinde çalıştırdık.
"""

# Bir test kitabının kalıbı: makine çevirisi 2.9/2.10 alıştırmalarını ÜÇ kez
# yazmış; kaynakta birer kez (ve alakasız bir kalıp "is burnt with" x3).
_EN_EXERCISE = r"""\begin{itemize}
  \item \textbf{2.8} A propellant is burnt with oxygen at 298 K, 0.1 MPa; determine the flame temperature.
  \item \textbf{2.9} Inside a closed tank, the dissociation H2 ⇔ 2H takes place. Find the mole fractions of H2 and H at T = 2500 K and T = 3200 K.
  \item \textbf{2.10} Inside a chamber, 4 mol of methane is burnt with 8 mol of O2 under 10 MPa at 2000 K. Find the composition at equilibrium.
  \item \textbf{2.11} Methane is burnt with air at 300 K; comment on your results.
\end{itemize}
"""
_TR_REPEAT = ("Kapalı bir tankta H2 ⇔ 2H ayrışması kendiliğinden gerçekleşir; T = 2500 K "
              "ve T = 3200 K için mol kesirlerini bulunuz.")
_TR_EXERCISE = ("\\begin{itemize}\n"
                "  \\item \\textbf{2.8} Bir itici, 298 K, 0.1 MPa değerlerinde "
                "oksijenle yakılır; alev sıcaklığını belirleyin.\n"
                "  \\item \\textbf{2.9} " + _TR_REPEAT + "\n"
                "  \\item \\textbf{2.9} " + _TR_REPEAT + "\n"
                "  \\item \\textbf{2.9} " + _TR_REPEAT + "\n"
                "  \\item \\textbf{2.10} Bir odada 4 mol metan, 10 MPa basınç "
                "ve 2000 K sıcaklıkta 8 mol O2 ile yakılır. Dengedeki bileşimi "
                "bulun.\n"
                "  \\item \\textbf{2.11} Metan 300 K'de havayla yakılır; "
                "sonuçlarınızı yorumlayın.\n"
                "\\end{itemize}\n")


def _repeat_msgs(rep):
    return [i.message for i in _by_check(rep, "hallucination_pattern")
            if "Tekrarlayan fraz" in i.message]


class TestKalem4KaynaktakiTekrar:

    def test_kaynakta_da_olan_tekrar_bulgu_degil(self):
        # Kaynak yokken bulgu var (sınama gerçekten bu durumu kapsıyor)...
        assert any("kontrol etti ve" in m
                   for m in _repeat_msgs(_analyze(_TR_SWEEP, manual=True)))
        # ...kaynakta "This checked for agents" x5 varken yok.
        assert _repeat_msgs(_analyze(_TR_SWEEP, _EN_SWEEP, manual=True)) == []

    def test_makine_cevirisi_tekrari_hala_high(self):
        rep = _analyze(_TR_EXERCISE, _EN_EXERCISE)
        msgs = _repeat_msgs(rep)
        assert msgs, "gerçek MT tekrarı kaçtı"
        assert all(i.severity == "high"
                   for i in _by_check(rep, "hallucination_pattern")
                   if "Tekrarlayan fraz" in i.message)

    def test_hizalanamayan_kaynakta_muafiyet_yok(self):
        # Kaynakta tekrar var ama ortak değişmez belirteç yok (başka metin):
        # hizalama kanıtı yetersiz, bulgu kalır.
        en = ("\n".join(f"This checked for agents doing thing {w}."
                        for w in ("alpha", "beta", "gamma", "delta", "eps"))
              + "\n")
        tr = _TR_SWEEP.replace("Hugging", "Kucak").replace("Nimbra", "Şirket")
        tr = tr.replace("Depotline", "depo").replace("700", "yedi yüz")
        tr = tr.replace("#page", "#sayfa")
        assert _repeat_msgs(_analyze(tr, en, manual=True)) != []

    def test_frazin_ortak_boyu(self):
        words = ("a b ajanları kontrol etti ve x y ajanları kontrol etti ve "
                 "z ajanları kontrol etti ve").split()
        assert QCEngine._phrase_span(words, "ajanları kontrol etti ve") == 4
        words = "p q r s t u p q r s t v p q r s t w".split()
        assert QCEngine._phrase_span(words, "p q r s") == 5


# ---------------------------------------------------------------------------
# Kalem 5 — makine belirteci başlık çevrilmemiş sayılmaz
# ---------------------------------------------------------------------------

_TOKEN_HEADING = r"\subsubsection{qqHARBORCLOCK\_OLDSIG/STOP\_T1700000001\_U40404}"


class TestKalem5BelirtecBaslik:

    def test_belirtec_baslik_bulgu_degil(self):
        src = _TOKEN_HEADING + "\n\nThe agent posted this token twice.\n"
        tr = _TOKEN_HEADING + "\n\nAjan bu belirteci iki kez paylaştı.\n"
        rep = _analyze(tr, src, manual=True)
        assert _by_check(rep, "untranslated_heading") == []

    def test_ingilizce_baslik_hala_raporlanir(self):
        head = r"\subsection{Orbital Mechanics of the Planets}"
        src = head + "\n\nThe planets move on ellipses.\n"
        tr = head + "\n\nGezegenler elipsler üzerinde hareket eder.\n"
        rep = _analyze(tr, src, manual=True)
        assert len(_by_check(rep, "untranslated_heading")) == 1

    def test_belirtec_ve_ingilizce_kelimeli_baslik_raporlanir(self):
        head = r"\subsection{The qqHARBOR\_T17 token and its meaning}"
        src = head + "\n\nText.\n"
        tr = head + "\n\nMetin.\n"
        rep = _analyze(tr, src, manual=True)
        assert len(_by_check(rep, "untranslated_heading")) == 1

    @pytest.mark.parametrize("text,expected", [
        (r"qqHARBORCLOCK\_OLDSIG/STOP\_T1700000001\_U40404", []),
        (r"QUOTA\allowbreak{}X\allowbreak{}\_relay", []),
        ("2.3.4", []),
        (r"The \textit{n}-Body Case", ["The", "n-Body", "Case"]),
        ("Kepler's Laws", ["Kepler's", "Laws"]),
    ])
    def test_dogal_dil_kelimeleri(self, text, expected):
        assert natural_language_words(text) == expected


# ---------------------------------------------------------------------------
# Kalem 6 — belirteç alıntısı "uzun noktalamasız cümle" değildir
# ---------------------------------------------------------------------------

# Yapı: elle onarım öncesi bir çeviri satırının başı — iki \texttt belirteç
# alıntısı, ikincisi satır sonuna dek kapanmıyor. Belirteç harfleri sentetik
# (uzunluk, büyük/küçük harf, rakam ve \allowbreak yerleri aynı), aradaki
# bağlaç cümlesi sentetik, sözcük sayısı aynı (KARAR 26).
_BELIRTEC_ALINTISI = (r"“\texttt{\textbf{KGDXS\allowbreak{}D\allowbreak{}\_nhbwm\allowbreak{}"
         r"nfbac\allowbreak{}}\_WUmfc\allowbreak{}c9k\allowbreak{}\_atyfk\allowbreak{}"
         r"hhu\allowbreak{}\_QXR\allowbreak{}\_uu2u\allowbreak{}\_fi525\allowbreak{}"
         r"\_fn\allowbreak{}\_enfig\allowbreak{}SOX\allowbreak{}\_\_\textbf{U\allowbreak{}"
         r"\_frrhn\allowbreak{}\_kaph}}” ve öbür ajan da şu karşılığı verdi: "
         r"“\texttt{\textbf{QDCUK\allowbreak{}QICXG\allowbreak{}}\_QXRBW\allowbreak{}"
         r"XQN\allowbreak{}\_XCMUV\allowbreak{}D\allowbreak{}\_\allowbreak{}[\allowbreak{}"
         r".\allowbreak{}.\allowbreak{}.\allowbreak{}]\allowbreak{}\_mwu658 62\allowbreak{}"
         r"\_\_mqokw\allowbreak{}bacBD\allowbreak{}M\allowbreak{}\_nae+\allowbreak{}"
         r"naeCA\allowbreak{}B\allowbreak{}\_x42k\allowbreak{}\_qakqs\allowbreak{}"
         r"hnwro\allowbreak{}\_")


def _long_sentence(rep):
    return [i for i in _by_check(rep, "turkish_naturalness")
            if "noktalama olmadan" in i.message]


class TestKalem6BelirtecUzunCumle:

    def test_belirtec_alintisi_uzun_cumle_degil(self):
        tr = "Ajanlar şöyle yazıştı.\n\n" + _BELIRTEC_ALINTISI + "\n"
        assert _long_sentence(_analyze(tr, manual=True)) == []

    def test_gercek_uzun_noktalamasiz_cumle_hala_bulgu(self):
        run_on = ("raporun ilk taslağında görülen veri kümesi eksiklerini "
                  "gidermek amacıyla ek transkriptler topladık ve bunları "
                  "mevcut veri kümesiyle birleştirerek yeniden analiz ettik ve "
                  "sonuçların büyük ölçüde değişmediğini gördük ama bazı "
                  "ayrıntıların farklılaştığını da fark ettik ve bu farkların "
                  "kaynağını anlamak için ayrı bir inceleme başlattık \\texttt{x} "
                  "sonra devam ettik")
        rep = _analyze("Giriş.\n\n" + run_on + "\n", manual=True)
        assert len(_long_sentence(rep)) == 1

    def test_maske_cumle_sonunu_korur(self):
        # Bir EPUB kitabında bağlantı adresindeki altçizgi cümle
        # sonundaki noktayı yutmamalı.
        line = (r"harbor duties.\href{part0025_split_001.html#unnumbered-5}{1} "
                r"After the storm")
        masked = mask_machine_tokens(line)
        assert "duties." in masked and "part0025" not in masked

    def test_maske_belirteci_tek_kelimeye_indirir(self):
        assert mask_machine_tokens(
            "Ajan şunu yazdı: zQX_BRNO47912_LAP2207_UT1853__RELAY_UP. Sonra "
            "durdu.") == "Ajan şunu yazdı: KOD. Sonra durdu."
