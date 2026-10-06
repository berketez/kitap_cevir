"""G1a-1 (5 Ekim 2026): dönüştürücü yapı kalemleri.

Olgular okuma bulgularından (2026-09-30; mt okuma MT-23) gelir; buradaki
cümleler SENTETİKTİR (KARAR 26: testlere kitap metni kopyalanmaz).

(1) Başlıksız başlayan belgede ilk "#" başlık kitap adı sanılmaz; PDF puntosu
    öndeki başlıkları bölüm düzeyinde gösteriyorsa ana madde onlardan başlar.
(2) Başlık/madde ayrımı: PDF yer imi (outline) başlığı olan gövde satırı
    başlık olur (sonraki cümleye yapışığı ayrılır); başlığa dönmüş madde
    imli satır liste maddesi, kalın iki noktalı etiket kalın paragraf olur;
    başlığın sonundaki italik soru ayrı satıra iner.
(3) Ekler: kitabın sonundaki "Appendix: X" bölümleri \\appendix ile Ek A/B;
    bölüm içi ya da arada kalan ek başlığı numarasız + İçindekiler'de.
(4) Şekil altyazısı satırına yapışık uzun gövde satırı ayrı paragraf olur.
"""

import re

import pytest

from latex_pipeline.config import PipelineConfig
from latex_pipeline.converter import MarkdownToLatex

fitz = pytest.importorskip("fitz")

# Başlıktan önce düzyazı (150 karakterden uzun): başlık sayfası kurulmaz.
_GIRIS = (
    "An opening paragraph of a synthetic report that runs well beyond one hundred "
    "and fifty characters so that the converter treats the document as starting "
    "with body text rather than a title.\n\n"
)
_HEAD = re.compile(r"^\\(part|chapter|section|subsection|subsubsection|paragraph)(\*?)"
                   r"(?:\[[^\]]*\])?\{(.*)$", re.M)


# Kitap adı + yazar: sınanan başlıklar başlık sayfasına gitmesin.
_KITAP = "# A Synthetic Book Title\n\nJane Q. Writer\n\n"


def _conv(md: str, **cfg) -> str:
    return MarkdownToLatex(PipelineConfig(document_class="book", **cfg)).convert(md)


def _heads(tex: str):
    return [(m.group(1) + m.group(2), m.group(3)) for m in _HEAD.finditer(tex)]


def _after_mainmatter(tex: str):
    return _heads(tex.split("\\mainmatter", 1)[1])


def _before_mainmatter(tex: str):
    return _heads(tex.split("\\mainmatter", 1)[0])


def _pdf(tmp_path, toc, lines=("Synthetic page text.",)):
    """Yer imli (outline) küçük PDF."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for ln in lines:
        page.insert_text((72, y), ln, fontsize=11)
        y += 16
    doc.set_toc(toc)
    path = tmp_path / "kaynak.pdf"
    doc.save(str(path))
    doc.close()
    return str(path)


# ---------------------------------------------------------------------------
# (1) Ön madde -> ana madde geçişi
# ---------------------------------------------------------------------------

class TestGecis:
    MD = (_GIRIS
          + "## Main findings of the review\n\nBody one.\n\n"
          + "## Review method and limits\n\nBody two.\n\n"
          + "# Timeline of the review\n\nBody three.\n\n"
          + "# Sources of evidence\n\nBody four.\n")

    def test_ilk_diyez_baslik_kitap_adi_sanilmaz(self):
        # Düzyazıdan sonra belgenin ilk başlığı "#": ilk bölümdür. Eskiden
        # kitap adı sanılıyor, geçiş ikinci "#"e kayıyordu.
        md = (_GIRIS + "# Timeline of the review\n\nBody three.\n\n"
              + "## A part of the timeline\n\nBody.\n\n"
              + "# Sources of evidence\n\nBody four.\n")
        tex = _conv(md)
        assert _after_mainmatter(tex)[0] == ("chapter", "Timeline of the review}")
        assert ("chapter*", "Timeline of the review}") not in _heads(tex)

    def test_ilk_diyez_pdfte_bolumlerden_buyukse_kitap_adi(self, monkeypatch):
        md = (_GIRIS + "# The Synthetic Book Title\n\nBody.\n\n"
              + "# First real part\n\nBody.\n\n# Second real part\n\nBody.\n")
        sizes = {"thesyntheticbooktitle": 28.0, "firstrealpart": 20.0,
                 "secondrealpart": 20.0}
        monkeypatch.setattr(MarkdownToLatex, "_pdf_heading_sizes", lambda self: sizes)
        tex = _conv(md)
        assert _after_mainmatter(tex)[0] == ("chapter", "First real part}")

    def test_ondeki_alt_basliklar_varken_kanitsiz_eski_kural(self):
        # İlk "#" öncesinde alt başlıklar var ve PDF puntosu yok: eski kural
        # (ilk "#" kitap adı), İçindekiler ön madde sanılan bölümlerin
        # ortasına konmaz (kitap kesiti).
        tex = _conv(self.MD)
        assert _after_mainmatter(tex)[0] == ("chapter", "Sources of evidence}")

    def test_pdf_puntosu_ondeki_bolumleri_gosterirse_gecis_onlardan(self, monkeypatch):
        sizes = {"mainfindingsofthereview": 20.0, "reviewmethodandlimits": 20.0,
                 "timelineofthereview": 16.0, "sourcesofevidence": 16.0}
        monkeypatch.setattr(MarkdownToLatex, "_pdf_heading_sizes", lambda self: sizes)
        tex = _conv(self.MD)
        assert _before_mainmatter(tex) == []
        assert _after_mainmatter(tex)[0][1] == "Main findings of the review}"
        assert tex.count("\\mainmatter") == 1

    def test_ondeki_baslik_kucuk_puntoluysa_geciş_yerinde(self, monkeypatch):
        sizes = {"mainfindingsofthereview": 12.0, "reviewmethodandlimits": 12.0,
                 "timelineofthereview": 16.0, "sourcesofevidence": 16.0}
        monkeypatch.setattr(MarkdownToLatex, "_pdf_heading_sizes", lambda self: sizes)
        tex = _conv(self.MD)
        assert _after_mainmatter(tex)[0][1] == "Timeline of the review}"

    def test_kesit_numarali_alt_bolumle_basliyorsa_gecis_yerinde(self, monkeypatch):
        md = (_GIRIS + "## 7.2 Mass from orbits\n\nBody.\n\n"
              + "# The next chapter title\n\nBody.\n\n# A later chapter\n\nBody.\n")
        pdfsiz = _conv(md)
        sizes = {"72massfromorbits": 20.0, "thenextchaptertitle": 24.0,
                 "alaterchapter": 16.0}
        monkeypatch.setattr(MarkdownToLatex, "_pdf_heading_sizes", lambda self: sizes)
        # Numaralı öndeki başlık geçişi taşımaz; ilk "#" sonrakinden büyük:
        # kanıt yok, eski kural (PDF'siz çıktıyla aynı).
        assert _conv(md) == pdfsiz

    def test_duzyaziyla_baslayan_kitapta_on_madde_izleyen_ilk_baslik_kitap_adi(self):
        # Kapakta uzun tanıtım satırı olan kitap: "# Ad" ardından ön madde
        # geliyorsa eski kural (ad numarasız, geçiş ilk gerçek bölümde).
        md = (_GIRIS + "# The Synthetic Title of a Book\n\n"
              "# Preface\n\nWhy this book.\n\n# The Beginning\n\nText.\n\n"
              "# The Middle\n\nText.\n")
        tex = _conv(md)
        assert _after_mainmatter(tex)[0] == ("chapter", "The Beginning}")
        assert ("chapter", "The Synthetic Title of a Book}") not in _heads(tex)

    def test_baslik_sayfali_kitapta_davranis_aynen(self):
        md = ("# A Synthetic Book Title\n\nJane Q. Writer\n\n"
              "# Preface\n\nWhy.\n\n# First Real Chapter\n\nText.\n")
        tex = _conv(md)
        assert "\\begin{titlepage}" in tex
        assert _after_mainmatter(tex)[0] == ("chapter", "First Real Chapter}")
        assert ("chapter*", "Preface}") in _before_mainmatter(tex)


# ---------------------------------------------------------------------------
# (2) Başlık / madde ayrımı
# ---------------------------------------------------------------------------

class TestYerImiBasliklari:
    TOC = [[1, "Opening chapter of the toy report", 1],
           [2, "Data gathered from the shared cache store", 1],
           [3, "Some field notes were not kept anywhere", 1],
           [2, "Workers often argued about skipping the checks", 1],
           [2, "Short tag", 1]]

    def _md(self):
        return (_GIRIS
                + "Opening chapter of the toy report\n\n"   # ilk başlıktan önce: dokunulmaz
                "# Opening chapter of the toy report\n\nIntro body.\n\n"
                "Data gathered from the shared cache store\n\n"
                "The cache held many synthetic entries for this example.\n\n"
                "Some field notes were not kept anywhere<sup>4</sup>\n\n"
                "<sup>4</sup> A synthetic footnote text.\n\n"
                "Workers often argued about skipping the checks In the summary file the "
                "workers wrote several long notes about it.\n\n"
                "Short tag In the next sentence nothing should split.\n\n"
                "A plain paragraph that is not in the outline\n\n")

    def test_gövde_satiri_yer_imi_basligi_olur(self, tmp_path):
        tex = _conv(self._md(), input_pdf=_pdf(tmp_path, self.TOC))
        heads = _heads(tex)
        titles = [t for _c, t in heads]
        assert any(t.startswith("Data gathered from the shared cache store")
                   for t in titles)
        lvl = dict((t.split("}")[0].split("[")[0], c) for c, t in heads)
        assert lvl["Data gathered from the shared cache store"] == "section"
        # Dipnot işaretli başlık: yer imi metnindeki numara eşleşmeyi bozmaz.
        assert any("Some field notes were not kept anywhere" in t for t in titles)

    def test_yapisik_baslik_ayrilir(self, tmp_path):
        tex = _conv(self._md(), input_pdf=_pdf(tmp_path, self.TOC))
        assert ("section", "Workers often argued about skipping the checks}") in _heads(tex)
        assert re.search(r"^In the summary file the workers", tex, re.M)

    def test_kisa_yer_imi_onek_diye_bolunmez_ve_yabancilar_dokunulmaz(self, tmp_path):
        tex = _conv(self._md(), input_pdf=_pdf(tmp_path, self.TOC))
        assert re.search(r"^Short tag In the next sentence", tex, re.M)
        assert re.search(r"^A plain paragraph that is not in the outline", tex, re.M)
        # İlk başlıktan önceki ad satırı başlığa dönmez.
        assert re.search(r"^Opening chapter of the toy report$", tex, re.M)

    def test_kurtarilan_basligin_kucuk_puntolu_alt_basligi_bir_duzey_iner(
            self, tmp_path, monkeypatch):
        sizes = {"somefieldnoteswerenotkeptanywhere": 12.0,
                 "howoftenthenoteswerekept": 11.0, "anotherkeptnote": 12.0}
        monkeypatch.setattr(MarkdownToLatex, "_pdf_heading_sizes", lambda self: sizes)
        md = (_GIRIS + "# Opening chapter of the toy report\n\nIntro body.\n\n"
              "Some field notes were not kept anywhere\n\nBody.\n\n"
              "## **How often the notes were kept**\n\nBody.\n\n"
              "## Another kept note\n\nBody.\n")
        toc = [[1, "Opening chapter of the toy report", 1],
               [2, "Some field notes were not kept anywhere", 1]]
        tex = _conv(md, input_pdf=_pdf(tmp_path, toc))
        hs = _heads(tex)
        assert ("section", "Some field notes were not kept anywhere}") in hs
        assert ("subsection", "How often the notes were kept}") in hs
        # Eşit puntolu kardeş inmez.
        assert ("section", "Another kept note}") in hs

    def test_pdfsiz_davranis_aynen(self):
        tex = _conv(self._md())
        assert not any(t.startswith("Data gathered") for _c, t in _heads(tex))


class TestMaddeBasliklari:
    MD = (_KITAP + "# Chapter of tags\n\nSome intro text here.\n\n"
          "#### - **Ask:**\n\nqqASK_TOY_MESSAGE_ONE\n\n"
          "- **Ask:** qqASK_TOY_MESSAGE_TWO\n\n"
          "### **- Give:**\n\nqqGIVE_TOY_MESSAGE\n\n"
          "#### **- Group of notes**\n\n"
          "- **first kind:** ^qqONE\n- **second kind:** ^qqTWO\n\n"
          "#### - *slanted leaf:*\n\n*^(?P<x>toy)*\n\n"
          "#### **Answer:**\n\nqqANSWER_TOY\n\n"
          "## Next part of chapter\n\nText.\n")

    def test_madde_basliklari_liste_olur(self):
        tex = _conv(self.MD)
        titles = [t for _c, t in _heads(tex)]
        assert not any(re.search(r"Ask|Give|Group of notes|slanted|Answer", t)
                       for t in titles), titles
        assert "\\item \\textbf{Ask:} qqASK\\_TOY\\_MESSAGE\\_ONE" in tex
        assert "\\item \\textbf{Give:} qqGIVE\\_TOY\\_MESSAGE" in tex
        assert "\\item \\textbf{Group of notes}" in tex
        assert "\\item \\textit{slanted leaf:} \\textit{" in tex

    def test_kalin_iki_noktali_etiket_paragraf_olur(self):
        tex = _conv(self.MD)
        assert re.search(r"^\\textbf\{Answer:\}$", tex, re.M)
        assert "qqANSWER\\_TOY" in tex
        # Gerçek alt başlık numarası kaymaz.
        assert ("section", "Next part of chapter}") in _heads(tex)

    def test_madde_iminden_sonra_bosluksuz_tire_baslik_kalir(self):
        tex = _conv(_KITAP + "# Book part\n\nText.\n\n## -Minus sign rules\n\nBody.\n")
        assert any("Minus sign rules" in t for _c, t in _heads(tex))


class TestBaslikSoru:
    def test_sondaki_italik_soru_ayri_satira_iner(self):
        md = (_KITAP + "# First toy chapter\n\nText.\n\n"
              "## Order of the main events in this toy case *What was the order of the "
              "main events in this toy case?*\n\nBody.\n")
        tex = _conv(md)
        heads = _heads(tex)
        assert heads[-1] == ("section", "Order of the main events in this toy case}")
        assert re.search(r"^\\textit\{What was the order of the main events in this toy "
                         r"case\?\}$", tex, re.M)
        assert tex.count("What was the order") == 1

    def test_soru_olmayan_italik_kuyruk_kalir(self):
        tex = _conv(_KITAP + "# Toy chapter\n\nText.\n\n## Methods *(revised)*\n\nBody.\n")
        assert "(revised)" in _heads(tex)[-1][1]

    def test_tamamen_italik_soru_basligi_degismez(self):
        tex = _conv(_KITAP + "# Toy chapter\n\nText.\n\n## *Why does the toy work?*\n\nBody.\n")
        assert any("Why does the toy work?" in t for _c, t in _heads(tex))


# ---------------------------------------------------------------------------
# (3) Ekler
# ---------------------------------------------------------------------------

class TestEkler:
    def test_kitap_sonu_ekleri_appendix(self):
        md = ("# Book Name Here\n\n# First Chapter\n\nText.\n\n## A section\n\nText.\n\n"
              "# Appendix: Field notes of the review\n\nText.\n\n## Day one\n\nText.\n\n"
              "# Appendix: methods used here\n\nText.\n")
        tex = _conv(md)
        assert tex.count("\\appendix") == 1
        after = _heads(tex.split("\\appendix", 1)[1])
        assert after[0] == ("chapter", "Field notes of the review}")
        assert ("chapter", "methods used here}") in after
        assert "Appendix:" not in tex

    def test_harfli_ekler_sirali_ise_etiket_atilir(self):
        md = ("# Book Name Here\n\n# First Chapter\n\nText.\n\n"
              "# Appendix A - Spherical toy rules\n\nText.\n\n"
              "# Appendix B: Another toy proof\n\nText.\n\n# Index\n\nx, 1\n")
        tex = _conv(md)
        assert tex.count("\\appendix") == 1
        hs = _heads(tex)
        assert ("chapter", "Spherical toy rules}") in hs
        assert ("chapter", "Another toy proof}") in hs

    def test_arada_kalan_ek_numarasiz_ve_icindekilerde(self):
        md = ("# Book Name Here\n\n# First Chapter\n\nText.\n\n"
              "# Appendix - Sphere of toy influence\n\nText.\n\n"
              "# Second Chapter\n\nText.\n")
        tex = _conv(md)
        assert "\\appendix" not in tex
        assert re.search(r"^\\chapter\*\{Appendix - Sphere of toy influence\}"
                         r"\\addcontentsline\{toc\}\{chapter\}", tex, re.M)
        assert ("chapter", "Second Chapter}") in _heads(tex)

    def test_bolum_ici_ek_numarasiz(self):
        md = ("## Chapter 1\n\n# Toy Mechanics\n\nText.\n\n## 1.1 First part\n\nText.\n\n"
              "### **Appendix: Vector toy operations**\n\nText.\n\n"
              "## Chapter 2\n\n# Toy Motion\n\nText.\n\n## 2.1 Frames\n\nText.\n\n"
              "## Appendix: Orbit toy types\n\nText.\n")
        tex = _conv(md)
        assert "\\appendix" not in tex
        assert re.search(r"^\\subsection\*\{Appendix: Vector toy operations\}"
                         r"\\addcontentsline\{toc\}\{subsection\}", tex, re.M)
        assert re.search(r"^\\section\*\{Appendix: Orbit toy types\}"
                         r"\\addcontentsline\{toc\}\{section\}", tex, re.M)

    def test_bolum_ici_ek_pdf_puntosu_kardeslerle_aynıysa_section(self, monkeypatch):
        md = ("## Chapter 1\n\n# Toy Mechanics\n\nText.\n\n## 1.1 First part\n\nText.\n\n"
              "### **Appendix: Vector toy operations**\n\nText.\n\n"
              "## Chapter 2\n\n# Toy Motion\n\nText.\n")
        sizes = {"11firstpart": 14.3, "appendixvectortoyoperations": 14.3,
                 "toymechanics": 24.8, "toymotion": 24.8}
        monkeypatch.setattr(MarkdownToLatex, "_pdf_heading_sizes", lambda self: sizes)
        tex = _conv(md)
        assert re.search(r"^\\section\*\{Appendix: Vector toy operations\}"
                         r"\\addcontentsline\{toc\}\{section\}", tex, re.M)

    def test_ayrac_ve_yalin_etiket_appendix_almaz(self):
        md = ("# Book Name Here\n\n# First Chapter\n\nText.\n\n# Appendices\n\n"
              "## APPENDIX A\n\nTOY CONSTANTS\n\n## APPENDIX D: TOY TABLES\n\nText.\n")
        tex = _conv(md)
        assert "\\appendix" not in tex
        assert re.search(r"^\\chapter\*\{Appendices\}\\addcontentsline", tex, re.M)
        assert re.search(r"^\\section\*\{APPENDIX D: TOY TABLES\}\\addcontentsline", tex, re.M)

    @pytest.mark.parametrize("word", ["Anhang", "Annexe", "Apéndice", "Anexo",
                                      "Appendice", "Приложение"])
    def test_alti_dilde_ek_sozcugu(self, word):
        md = ("# Book Name Here\n\n# First Chapter\n\nText.\n\n"
              f"# {word}: Toy tables\n\nText.\n")
        tex = _conv(md)
        assert tex.count("\\appendix") == 1
        assert ("chapter", "Toy tables}") in _heads(tex)

    def test_rakamli_ekler_harfe_donmez(self):
        md = ("# Book Name Here\n\n# First Chapter\n\nText.\n\n"
              "# Anhang 1: Toy tables\n\nText.\n\n# Anhang 2: Toy plots\n\nText.\n")
        tex = _conv(md)
        assert "\\appendix" not in tex
        assert re.search(r"^\\chapter\*\{Anhang 1: Toy tables\}\\addcontentsline", tex, re.M)


# ---------------------------------------------------------------------------
# (4) Altyazıya yapışmış gövde paragrafı
# ---------------------------------------------------------------------------

class TestAltyazi:
    def test_altyaziya_yapisik_govde_ayri_paragraf(self):
        md = (_KITAP + "# Toy chapter\n\nIntro.\n\n![](fig_toy.png)\n\n"
              "*Figure 4: Each toy line shows the active span of one synthetic worker in "
              "this made-up example.*\n"
              "Of the 120 synthetic workers active in this toy example,<sup>5</sup> most "
              "joined quickly and the rest followed in the later afternoon session.\n\n"
              "<sup>5</sup> A synthetic footnote about the toy workers.\n\n"
              "## Next toy section\n\nText.\n")
        tex = _conv(md)
        cap = re.search(r"\\caption\*\{(.*)\}", tex)
        assert cap and "Figure 4:" in cap.group(1) and "120 synthetic" not in cap.group(1)
        i = tex.index("Of the 120 synthetic workers")
        assert tex[:i].endswith("\n\n")
        # KARAR 31 (FX1): eşlenen dipnot kaynak numarasıyla.
        assert "\\footnote[5]{A synthetic footnote about the toy workers.}" in tex

    def test_kisa_kuyruk_altyazida_kalir(self):
        md = (_KITAP + "# Toy chapter\n\nIntro.\n\n![](fig_toy.png)\n\nFig. 2.1 A toy diagram\n"
              "Source: toy lab\n\nBody text.\n")
        tex = _conv(md)
        cap = re.search(r"\\caption\*\{(.*)\}", tex)
        assert cap and "Source: toy lab" in cap.group(1)

    def test_kucuk_harfli_devam_altyazi_tamamlanmamissa_kalir(self):
        md = (_KITAP + "# Toy chapter\n\nIntro.\n\n![](fig_toy.png)\n\nFig. 2.2 The toy distribution of the "
              "values across all of the synthetic samples drawn for this example and\n"
              "more words that continue the same caption sentence until it finally ends "
              "here in a natural way.\n\nBody text.\n")
        tex = _conv(md)
        cap = re.search(r"\\caption\*\{(.*)\}", tex)
        assert cap and "finally ends" in cap.group(1)
