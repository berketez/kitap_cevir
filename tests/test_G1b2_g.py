"""G1b-2 (5 Ekim 2026): Marker çıktısında sayfa kırılımı onarımı, 2. bölüm.

``latex_pipeline/marker_onarim.py`` (sözleşme: tests/test_G1b_g.py). Bu
dosyanın kalemleri (okuma bulguları, 2026-09-30):

1. Sayfa kırılımıyla bölünmüş tablo ve sayfa sonunda bölünmüş hücre:
   mevcut davranış testle kilitlenir.
2. Sayfa kırılımıyla bölünmüş numaralı/imli liste: ara
   bloksuz (yalnız boş satırla) bölünmüş madde; sayfa kırılımında değişen
   numaralı liste imi ("1." -> "- 2.").
3. İç içe liste düzeyleri: etiket ile içeriği ayrı
   maddeye düşmüş alt madde; sayfa kırılımında düzeyi kaybolmuş harfli alt
   madde dizisi.
4. Denklem çiftinin ikinci yarısı metin satırına düşmüş (mt okuma MT-11).
5. Hizalı denklem bloğunun satırları düz metne karışmış (mt okuma MT-6).

Girdilerin HEPSİ sentetiktir (KARAR 26: açık yayında telifli metin yok);
gerçek kanıt satırlarının yapısı (imler, girinti, boş satır, matematik
sınırlayıcıları, dipnot işaretleri) korunmuş, cümleler yeniden yazılmıştır.
Her testte kayıpsızlık denetlenir: harf-rakam çoklu kümesi birebir aynı
kalır ve her girdi satırının harf-rakam dizisi çıktıda bitişik durur.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest

KOK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KOK))

from latex_pipeline import marker_onarim as mo  # noqa: E402

_AN = re.compile(r"[^\W_]")


def _an(s: str) -> str:
    return "".join(_AN.findall(s))


def _kayipsiz(once: str, sonra: str, ayrinti) -> None:
    # İzinli silmeler: sayfa numarası ve KARAR 32 sayfa üst/alt bilgisi (FX1).
    silinen = Counter()
    for d in ayrinti:
        if d["tur"] in ("sayfa_no_silindi", "ust_bilgi_silindi"):
            silinen.update(_an(d["deger"]))
    c1 = Counter(_an(once))
    c1.subtract(silinen)
    assert +c1 == Counter(_an(sonra)), "harf-rakam çoklu kümesi değişti"
    cikti = _an(sonra)
    degerler = {d["deger"] for d in ayrinti
                if d["tur"] in ("sayfa_no_silindi", "ust_bilgi_silindi")}
    # Yapışık üst bilgi ayrıldıysa satır iki parçaya bölünür; parçalar ayrı
    # ayrı bitişik olmalı.
    ust = [_an(d["ust"]) for d in ayrinti if d["tur"] == "yapisik_ust_bilgi_ayrildi"]
    for satir in once.split("\n"):
        a = _an(satir)
        if satir.strip() in degerler or not a or satir.lstrip().startswith("|"):
            continue
        if a in cikti:
            continue
        assert any(u and u in a and all(x in cikti for x in a.split(u, 1))
                   for u in ust), f"satır bölündü/kayboldu: {satir[:80]!r}"


def _onar(md: str):
    out, sayac, ayrinti = mo.onar_ayrintili(md)
    _kayipsiz(md, out, ayrinti)
    assert mo.onar(out) == out, "eşgüçlü değil"
    return out, sayac


def _degismez(md: str) -> None:
    out, sayac = _onar(md)
    assert out == md, out


# ---------------------------------------------------------------------------
# Sözleşme: yeni sayaç türleri
# ---------------------------------------------------------------------------

class TestSozlesme:
    @pytest.mark.parametrize("tur", [
        "liste_imi_birlesti", "ic_madde_birlesti", "ic_liste_duzeyi_onarildi",
        "denklem_devami_birlesti", "denklem_satiri_ayrildi"])
    def test_yeni_sayac_turleri(self, tur):
        assert tur in mo.SAYAC_TURLERI
        _, sayac = mo.onar_rapor("")
        assert sayac[tur] == 0

    def test_eski_sayac_turleri_duruyor(self):
        for tur in ("sayfa_no_silindi", "paragraf_birlesti", "madde_birlesti",
                    "liste_birlesti", "tablo_birlesti", "tablo_satiri_birlesti",
                    "tablo_bos_sutun_atildi"):
            assert tur in mo.SAYAC_TURLERI


# ---------------------------------------------------------------------------
# Kalem 1: bölünmüş tablo / hücre (mevcut davranış kilitlenir)
# ---------------------------------------------------------------------------

class TestTablo:
    def test_bolunmus_satir_ve_bos_sutun(self):
        # Olgu yapısı: 3 sütunlu tablonun son satırı sayfa sonunda bölünmüş;
        # devamı 4 sütunlu (sonuncusu hep boş) ikinci tablonun "başlığı";
        # arada sayfa numarası ve dipnot tanımı.
        md = ("| Found item and place | Directly located the exact record in the archive | 41 |\n"
              "|---|---|---|\n"
              "| Found item but not the | Located a record that probably matched, but the record was written after | 3 |\n"
              "\n"
              "12\n"
              "\n"
              "<sup>7</sup> We also checked a few records by hand.\n"
              "\n"
              "| exact place | the archive was closed for the season | | |\n"
              "|---|---|---|--|\n"
              "| | Located a record whose page was lost after a reset | 2 | |\n"
              "| Did not find item | Could not locate any record of the item | 1 | |")
        out, sayac = _onar(md)
        assert sayac["tablo_birlesti"] == 1
        assert sayac["tablo_satiri_birlesti"] == 1
        assert sayac["tablo_bos_sutun_atildi"] == 1
        assert sayac["sayfa_no_silindi"] == 1
        tablo = [s for s in out.split("\n") if s.startswith("|")]
        assert len(tablo) == 5
        assert all(len(mo._hucreler(s)) == 3 for s in tablo)
        assert "| Found item but not the exact place | Located a record that probably " \
               "matched, but the record was written after the archive was closed for " \
               "the season | 3 |" in out
        # Dipnot tanımı tablonun ARDINA taşınır, tek başına paragraf kalır.
        assert out.index("<sup>7</sup>") > out.index("| Did not find item")

    def test_sayfa_sonunda_bolunmus_hucre(self):
        # Olgu yapısı: ikinci parçanın "başlığı" asıl veri satırı, ardındaki
        # seyrek satırlar ilk hücrenin devamı.
        md = ("| Group kind | Groups | Entries | Share |\n"
              "|---|---|---|---|\n"
              "| Kind 1 — Fully decoded groups | 3,120 | 512,004 | 48.20% |\n"
              "| Kind 2 — Partly decoded groups | 1,204 | 310,777 | 29.25% |\n"
              "\n"
              "| Lone rows that fit the broad | 9,071 | 9,071 | 0.85% |\n"
              "|---|---|---|---|\n"
              "| patterns yet belonged to no group | | | |\n"
              "| and were never read back at all | | | |")
        out, sayac = _onar(md)
        assert sayac["tablo_birlesti"] == 1
        assert ("| Lone rows that fit the broad patterns yet belonged to no group and "
                "were never read back at all | 9,071 | 9,071 | 0.85% |") in out
        assert len([s for s in out.split("\n") if s.startswith("|")]) == 5

    def test_yinelenen_baslik_birlesmez(self):
        # Sürdürülen tablo başlığını yineliyor: yinelenen başlığı silmek metin
        # düşürür, birleştirilmez (bkz. RAPOR.md, kalem 1).
        md = ("| Model family | What can be answered |\n"
              "|---|---|\n"
              "| 1. Small trees | Most questions, with some effort, for what the |\n"
              "\n"
              "<sup>3</sup> A note on the first row.\n"
              "\n"
              "| Model family | What can be answered |\n"
              "|---|---|\n"
              "| | agents can observe in each round. |")
        _degismez(md)

    def test_farkli_sutun_sayisi_birlesmez(self):
        md = ("| A | B |\n|---|---|\n| one | two |\n\n<sup>4</sup> A short note here.\n\n"
              "| c | d | e |\n|---|---|---|\n| 1 | 2 | 3 |")
        _degismez(md)


# ---------------------------------------------------------------------------
# Kalem 2: bölünmüş liste
# ---------------------------------------------------------------------------

class TestBolunmusMadde:
    def test_ara_bloksuz_liste_devami(self):
        # Sayfa kırılımının tek izi boş satır (sayfa üst bilgisi ve numarası
        # Marker'da düşmüş): madde ikinci liste parçasının küçük harfli ilk
        # maddesi olarak sürüyor.
        md = ("- Fixed menus have been replaced by adaptive skills. What a robot can "
              "choose depends on what it was trained to\n"
              "\n"
              "- carry out in earlier tasks. Later chapters return to this point.\n"
              "- Plans take time and their outcomes are uncertain.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert out == ("- Fixed menus have been replaced by adaptive skills. What a robot "
                       "can choose depends on what it was trained to carry out in earlier "
                       "tasks. Later chapters return to this point.\n"
                       "- Plans take time and their outcomes are uncertain.")

    def test_ara_bloksuz_numarali_madde_devami(self):
        md = ("1. Using links to predict changes. The study fits rates with a regression, "
              "which is not valid for the purpose of judging by how\n"
              "\n"
              "- much lower doses would reduce the rates (Lee 2009).\n"
              "- 2. *Impossible forecasts*. Reading the links as causes leads to a paradox.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        # Birleşen blokta "1." ile "- 2." yan yana: im eşitlenir (eşitlikte
        # ilk maddenin imi), dönüştürücü tek numaralı liste görür.
        assert sayac["liste_imi_birlesti"] == 1
        assert ("judging by how much lower doses would reduce the rates (Lee 2009).\n"
                "2. *Impossible forecasts*.") in out

    def test_ara_bloksuz_paragraf_devami(self):
        md = ("- The first rule keeps the storage small. In practice a dense liquid is "
              "chosen to reduce the tank\n"
              "\n"
              "volume. A salt solution is the usual choice for small corrections.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert out == ("- The first rule keeps the storage small. In practice a dense "
                       "liquid is chosen to reduce the tank volume. A salt solution is the "
                       "usual choice for small corrections.")

    def test_kucuk_harfli_ifade_listesi_birlesmez(self):
        md = ("- reduce the cost of production for small firms in the region\n"
              "\n"
              "- increase the speed of delivery for all orders.")
        _degismez(md)

    def test_cumle_sonu_olmayan_devam_birlesmez(self):
        # "such that" + denklem: maddenin mi cümlenin mi devamı belli değil.
        md = ("- A second net without inner stages and with an output of size p "
              "(same as the first net)\n"
              "\n"
              "such that\n"
              "\n"
              "$$\\max |f(x) - g(x)| < \\epsilon$$")
        _degismez(md)

    def test_noktasiz_ifade_listesinden_sonra_cumle_birlesmez(self):
        # Maddeler noktasız ifade: devam parçası listeye giren cümlenin de
        # devamı olabilir ("which we compare …"); kanıt yok, dokunulmaz.
        md = ("- Supervised training with labelled examples only\n"
              "- Self-supervised pretraining on raw text corpora\n"
              "\n"
              "which we compare in the next section.")
        _degismez(md)

    def test_kaynakca_ad_onekiyle_baslayan_madde_birlesmez(self):
        md = ("- Brown A (2011) Adaptive control. A survey of methods for large networks of\n"
              "\n"
              "- van Dam K (2012) Robust planning. Oxford University Press.")
        _degismez(md)

    def test_daha_girintili_devam_birlesmez(self):
        md = ("- The main sources were the following. We used them in this way and also\n"
              "\n"
              "  - logs from the second period. They were partial.")
        _degismez(md)

    def test_harf_etiketli_alt_madde_devami(self):
        # Roket kitabı yapısı: "a. *Ad*: …" alt maddesi sayfa sonunda kesilmiş,
        # devamı düz paragraf. "a." etiketi küçük harfli başlangıç sayılmaz.
        md = ("- 2. *Twin nozzles*: Two or more nozzles of one kind sit side by side.\n"
              "  - a. *Twin pairs*: Two nozzles of one kind point at each other. The "
              "degree of blending between two nearby pairs is set by\n"
              "\n"
              "the tilt of the first sheets and how much the jets cross. This fixes the "
              "final pattern.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert "is set by the tilt of the first sheets" in out

    def test_uzun_tek_madde_devami(self):
        # Tek maddelik liste, madde 15+ sözcüklük düzyazı ve noktasız kesilmiş.
        md = ("- A clear pattern between longer daily walks and fewer winter colds in "
              "several big surveys of town workers led the council to start a plan to "
              "pay for walking groups for people\n"
              "\n"
              "who sit at a desk all day. The plan ended after one year.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert "groups for people who sit at a desk" in out

    def test_sayfa_no_ve_ust_bilgi_yapisik_madde(self):
        # Derin öğrenme kitabı yapısı: sayfa numarası + bölüm üst bilgisi
        # ("6 1 Introduction") birinci yarının sonuna yapışmış. Üst bilgi ayrı
        # paragraf olur, cümle onsuz birleşir.
        md = ("# 1 Introduction\n"
              "\n"
              "- The first kind is easy to model. A concrete example would be a system "
              "ruled by one balance law plus a tangled material response. For the 6 1 "
              "Introduction\n"
              "\n"
              "former a clean rule is known, whereas the latter must be learned from samples.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert sayac["yapisik_ust_bilgi_ayrildi"] == 1
        assert "For the former a clean rule is known" in out
        # KARAR 32 (FX1): başlıkla eşleşen üst bilgi silinir ve sayılır.
        assert sayac["ust_bilgi_silindi"] == 1 and "6 1 Introduction" not in out

    def test_sayfa_no_ve_ust_bilgi_yapisik_paragraf(self):
        md = ("# 1 Introduction\n"
              "\n"
              "The first kind is easy to model. A concrete example would be a system "
              "ruled by one balance law plus a tangled material response. For the 6 1 "
              "Introduction\n"
              "\n"
              "former a clean rule is known, whereas the latter must be learned from samples.")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert sayac["yapisik_ust_bilgi_ayrildi"] == 1
        assert "For the former a clean rule" in out
        # KARAR 32 (FX1): başlıkla eşleşen üst bilgi silinir ve sayılır.
        assert sayac["ust_bilgi_silindi"] == 1 and "6 1 Introduction" not in out

    def test_bolum_etiketli_baslik_ust_bilgi_olarak_bilinir(self):
        # Başlık "Chapter 1 Introduction", sayfa üst bilgisi "1 Introduction"
        # (bölüm etiketi üst bilgide yok).
        md = ("# <span id=\"page-3-0\"></span>**Chapter 1 Introduction**\n"
              "\n"
              "- The first kind is easy to model. A concrete example would be a system "
              "ruled by one balance law plus a tangled material response. For the 6 1 "
              "Introduction\n"
              "\n"
              "former a clean rule is known, whereas the latter must be learned from samples.")
        out, sayac = _onar(md)
        assert sayac["yapisik_ust_bilgi_ayrildi"] == 1
        assert "For the former a clean rule" in out
        assert sayac["ust_bilgi_silindi"] == 1   # KARAR 32 (FX1)

    def test_tek_basina_sayfa_no_ve_ust_bilgi_ara_bloktur(self):
        # Üst bilgi ("4 1 Introduction") belgede üç kez yinelenmese de sayfa
        # numarası + bilinen başlık metni olduğu için ara bloktur. (Eskiden
        # ilk geçişte ara sayılmıyor, yapışık üst bilgi ayrılınca ikinci
        # geçişte sayılıyordu: onar eşgüçlü değildi.)
        md = ("# **Chapter 1 Introduction**\n"
              "\n"
              "- 3. **Blended tuning**: This group sits between the two others. Such "
              "schemes usually rely on a blend\n"
              "\n"
              "4 1 Introduction\n"
              "\n"
              "of tagged and untagged samples while fitting. For example, few pictures "
              "carry tags.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert "rely on a blend of tagged and untagged samples" in out
        # KARAR 32 (FX1): birleşen yarılar arasındaki (sayfa sınırı kanıtı)
        # başlıkla eşleşen üst bilgi taşınmaz, silinir ve sayılır.
        assert sayac["ust_bilgi_silindi"] == 1 and "4 1 Introduction" not in out

    def test_bilinmeyen_sayi_ve_buyuk_harfli_son_eski_davranis(self):
        # "… in 1936 Vienna": belgede "Vienna" üst bilgisi yok, birleşme eskisi
        # gibi olur (sayı + büyük harfli ad sıradan metindir).
        md = ("The treaty that ended the long war was finally signed after many rounds "
              "of talks in 1936 Vienna\n"
              "\n"
              "and it held for almost ten years.")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert sayac["yapisik_ust_bilgi_ayrildi"] == 0
        assert "1936 Vienna and it held" in out

    def test_dipnotlu_bolunmus_madde_mevcut_davranis(self):
        # Olgu yapısı: madde dipnot tanımlarıyla bölünmüş (mevcut dal).
        md = ("- **Probe the grader:** The group learned about the grader by setting up "
              "tripwires that would fire when the relevant member\n"
              "\n"
              "<sup>25</sup> These tripwires were tested twice.\n"
              "\n"
              "<sup>26</sup> See the appendix for details.\n"
              "\n"
              "- sent its result and reported back to the board afterwards.\n"
              "- **Edit the logs:** Members tried to change their own logs.")
        out, sayac = _onar(md)
        assert sayac["madde_birlesti"] == 1
        assert ("relevant member sent its result and reported back to the board "
                "afterwards.\n- **Edit the logs:**") in out
        assert out.index("<sup>25</sup>") > out.index("Edit the logs")


class TestNumaraliListeImi:
    def test_sayfa_kiriliminda_im_degisir(self):
        # Marker sayfanın ilk maddesini "N." ötekileri "- N." yazmış:
        # dönüştürücü iki ayrı liste çıkarıyordu ("1." + "• 2.").
        md = ("1. The first statement ends with a period.\n"
              "\n"
              "- 2. The second statement ends with a period.\n"
              "- 3. The third statement ends with a period.")
        out, sayac = _onar(md)
        assert sayac["liste_imi_birlesti"] == 1
        assert out == ("- 1. The first statement ends with a period.\n"
                       "\n"
                       "- 2. The second statement ends with a period.\n"
                       "- 3. The third statement ends with a period.")

    def test_tek_maddelik_ara_parca(self):
        # Kaynakça: "- 16." / "17." / "- 18." (her parça ayrı blok).
        md = ("- 15. A. Author, A first title. J. One 1, 1–2 (2001)\n"
              "- 16. B. Author, A second title. J. Two 2, 3–4 (2002)\n"
              "\n"
              "17. C. Author, A third title. J. Three 3, 5–6 (2003)\n"
              "\n"
              "- 18. D. Author, A fourth title. J. Four 4, 7–8 (2004)")
        out, sayac = _onar(md)
        assert sayac["liste_imi_birlesti"] == 1
        assert "\n- 17. C. Author" in out
        assert sum(1 for s in out.split("\n") if s.startswith("- ")) == 4

    def test_dipnotla_bolunmus_ve_im_degismis_liste_birlesir(self):
        md = ("- 1. The first statement ends with a period.\n"
              "- 2. The second statement ends with a period.\n"
              "\n"
              "<sup>7</sup> A footnote that explains the second statement.\n"
              "\n"
              "3. The third statement ends with a period.")
        out, sayac = _onar(md)
        assert sayac["liste_birlesti"] == 1
        assert sayac["liste_imi_birlesti"] == 1
        assert out.startswith("- 1. The first statement ends with a period.\n"
                              "- 2. The second statement ends with a period.\n"
                              "- 3. The third statement ends with a period.\n")
        assert out.rstrip().endswith("<sup>7</sup> A footnote that explains the second statement.")

    def test_ardisik_olmayan_numara_dokunulmaz(self):
        md = ("1. The first statement ends with a period.\n"
              "\n"
              "- 5. The fifth statement ends with a period.")
        _degismez(md)

    def test_tek_imli_liste_dokunulmaz(self):
        md = ("1. The first statement.\n2. The second statement.\n\n"
              "3. The third statement.")
        _degismez(md)

    def test_farkli_girinti_dokunulmaz(self):
        md = ("- 1. The first statement ends with a period.\n"
              "  2. A nested statement that ends with a period.")
        _degismez(md)


# ---------------------------------------------------------------------------
# Kalem 3: iç içe liste düzeyleri
# ---------------------------------------------------------------------------

class TestIcIceListe:
    def test_etiket_ile_icerik_ayri_maddede(self):
        # Olgu yapısı: "**etiket:**" maddesinin içeriği (uzun desen, satır
        # kaymış) tek alt madde olarak verilmiş; kardeşlerde etiket ve içerik
        # aynı satırda.
        md = ("- **Group A:**\n"
              "  - **alpha/one:** ^xxALPHA(?:\\_|[0-9])\n"
              "  - **beta/two:** ^xxBETA(?:\\_|[0-9])\n"
              "  - **gamma/three:**\n"
              "    - ^xx(?:GAMMA|DELTA)(?:\\_|[0-9])\n"
              "  - **delta/four:** ^xxDELTA(?:\\_|[0-9])")
        out, sayac = _onar(md)
        assert sayac["ic_madde_birlesti"] == 1
        assert out == ("- **Group A:**\n"
                       "  - **alpha/one:** ^xxALPHA(?:\\_|[0-9])\n"
                       "  - **beta/two:** ^xxBETA(?:\\_|[0-9])\n"
                       "  - **gamma/three:** ^xx(?:GAMMA|DELTA)(?:\\_|[0-9])\n"
                       "  - **delta/four:** ^xxDELTA(?:\\_|[0-9])")

    def test_etiket_ile_icerik_ayri_ust_duzeyde(self):
        md = ("- **sync:** ^xxSYNC(?:\\_|[0-9])\n"
              "- **act/go:**\n"
              "  - ^xx(?:(?:ACT|GO(?:\\_|[0-9]))|START)\n"
              "- **check/verify:**\n"
              "  - ^xx(?:CHECK|VERIFY|VERIFIED)(?:\\_|[0-9])\n"
              "- **hold:** ^xxHOLD(?:\\_|[0-9])")
        out, sayac = _onar(md)
        assert sayac["ic_madde_birlesti"] == 2
        assert "- **act/go:** ^xx(?:(?:ACT|GO(?:\\_|[0-9]))|START)\n" in out
        assert "- **check/verify:** ^xx(?:CHECK|VERIFY|VERIFIED)(?:\\_|[0-9])\n" in out
        assert out.count("\n  - ") == 0

    def test_gercek_alt_liste_dokunulmaz(self):
        # Olgu yapısı: üst madde iki noktayla bitiyor, iki gerçek alt madde.
        md = ("- The garden plan, as sketched on the first page.\n"
              "- Two kinds of beds:\n"
              "  - Raised beds built from old planks.\n"
              "  - Low beds dug straight into the soil.")
        _degismez(md)

    def test_tek_alt_madde_kardes_kaniti_yoksa_dokunulmaz(self):
        md = ("- **Examples:**\n"
              "  - A single example sentence that stands on its own.\n"
              "- **Notes:** Some remarks follow here.")
        _degismez(md)

    def test_alt_madde_de_etiketliyse_dokunulmaz(self):
        md = ("- **one:** ^xxONE\n"
              "- **two:** ^xxTWO\n"
              "- **group:**\n"
              "  - **inner:** ^xxINNER\n"
              "- **three:** ^xxTHREE")
        _degismez(md)

    def test_harfli_dizi_sayfa_kiriliminda_duzey_kaybi(self):
        # Roket kitabı yapısı: a.–c. ikinci düzeyde, sayfa kırılımından sonra
        # d.–e. birinci düzeye düşmüş.
        md = ("- 3. *Bicycle care*: A few checks keep the bike safe:\n"
              "  - a. Tyres are pumped before every long ride.\n"
              "  - b. The chain is oiled once a month.\n"
              "  - c. Brake pads are replaced when they get thin.\n"
              "\n"
              "- d. Lights are tested before riding at night.\n"
              "- e. Bolts on the saddle are tightened in spring.")
        out, sayac = _onar(md)
        assert sayac["ic_liste_duzeyi_onarildi"] == 1
        assert out == ("- 3. *Bicycle care*: A few checks keep the bike safe:\n"
                       "  - a. Tyres are pumped before every long ride.\n"
                       "  - b. The chain is oiled once a month.\n"
                       "  - c. Brake pads are replaced when they get thin.\n"
                       "  - d. Lights are tested before riding at night.\n"
                       "  - e. Bolts on the saddle are tightened in spring.")

    def test_ayracli_dizi_ve_madde_ici_ikinci_paragraf(self):
        # Bir test kitabındaki yapı: (a) sayfa sonunda biter; sonraki sayfanın ilk satırı
        # (a)'nın ikinci paragrafı, ardından (b), (c) — üçü de birinci düzeye
        # düşmüş.
        md = ("- 4. A good kitchen is organised around a few habits:\n"
              "  - (a) Knives are kept sharp and stored on a rail.\n"
              "\n"
              "- Blunt knives slip more often and are the real danger.\n"
              "- (b) Pans are washed while the oven is still warm.\n"
              "- (c) Spices are labelled and sorted by colour.")
        out, sayac = _onar(md)
        assert sayac["ic_liste_duzeyi_onarildi"] == 1
        assert out == ("- 4. A good kitchen is organised around a few habits:\n"
                       "  - (a) Knives are kept sharp and stored on a rail.\n"
                       "    Blunt knives slip more often and are the real danger.\n"
                       "  - (b) Pans are washed while the oven is still warm.\n"
                       "  - (c) Spices are labelled and sorted by colour.")

    def test_dizi_surmuyorsa_dokunulmaz(self):
        md = ("- 2. The second point has two parts:\n"
              "  - (a) The first part ends here.\n"
              "  - (b) The second part ends here.\n"
              "\n"
              "- (e) A separate remark that is not part of the sequence.")
        _degismez(md)

    def test_gercek_ust_duzey_maddeler_dokunulmaz(self):
        md = ("- 1. The first point has two parts:\n"
              "  - a. The first part ends here.\n"
              "  - b. The second part ends here.\n"
              "\n"
              "- 2. The second point stands alone.")
        _degismez(md)


# ---------------------------------------------------------------------------
# Kalem 4: denklem çiftinin ikinci yarısı (mt okuma MT-11)
# ---------------------------------------------------------------------------

class TestDenklemDevami:
    def test_noktali_virgulle_dusmus_ikinci_yari(self):
        md = ("It is clear from the figure that\n"
              "\n"
              "$$\\hat{u} = \\cos\\phi \\hat{x} + \\sin\\phi \\hat{y}$$\n"
              ";  $\\hat{v} = -\\sin\\phi \\hat{x} + \\cos\\phi \\hat{y}$ \n"
              "\n"
              "and, differentiating,")
        out, sayac = _onar(md)
        assert sayac["denklem_devami_birlesti"] == 1
        assert ("$$\\hat{u} = \\cos\\phi \\hat{x} + \\sin\\phi \\hat{y} \\; ; \\; "
                "\\hat{v} = -\\sin\\phi \\hat{x} + \\cos\\phi \\hat{y}$$\n\nand,") in out

    def test_sondaki_nokta_denklemin_icinde_kalir(self):
        md = "$$b = 0$$\n;  $k = 2m + 17$ .\n\nThe next step follows."
        out, sayac = _onar(md)
        assert sayac["denklem_devami_birlesti"] == 1
        assert out.startswith("$$b = 0 \\; ; \\; k = 2m + 17 .$$\n\nThe next")

    def test_bos_satirla_ayriliysa_dokunulmaz(self):
        _degismez("$$a = 1$$\n\n;  $c = 2$ \n\nText follows here.")

    def test_metin_iceren_satir_dokunulmaz(self):
        _degismez("$$a = 1$$\n;  $c = 2$ where c is the count.")

    def test_iki_satirli_display_blogu(self):
        md = "$$\na = \\frac{p}{q}\n$$\n;  $c = \\frac{r}{s}$ "
        out, sayac = _onar(md)
        assert sayac["denklem_devami_birlesti"] == 1
        assert out == "$$\na = \\frac{p}{q}\n \\; ; \\; c = \\frac{r}{s}$$"


# ---------------------------------------------------------------------------
# Kalem 5: hizalı denklem bloğu düz metne karışmış (mt okuma MT-6)
# ---------------------------------------------------------------------------

class TestDenklemSatirlari:
    def test_hizali_blok_satirlari(self):
        md = ("the following identities hold for the curve:\n"
              "\n"
              "$$q_1 = s/(1+f)$$\n"
              "  \n"
              " $q_2 = s/(1-f)$   \n"
              " $2w = q_1 + q_2 = 2s/(1-f^2)$   \n"
              " $2v = q_2 - q_1 = 2sf/(1-f^2) = 2wf$   \n"
              " $h^2 = w^2 - v^2 = ws$ \n"
              "\n"
              "Hence the total size")
        out, sayac = _onar(md)
        assert sayac["denklem_satiri_ayrildi"] == 1
        assert ("$$q_1 = s/(1+f)$$\n  \n$$q_2 = s/(1-f)$$\n"
                "$$2w = q_1 + q_2 = 2s/(1-f^2)$$\n"
                "$$2v = q_2 - q_1 = 2sf/(1-f^2) = 2wf$$\n"
                "$$h^2 = w^2 - v^2 = ws$$\n\nHence the total size") in out

    def test_tek_sonuc_satiri(self):
        md = ("$$(3 \\times 41.2 + 2 \\times 30.5)(T_{x} - 300) = 1500 \\times 10^{3}$$\n"
              "  \n"
              " $\\rightarrow T_{x} = 2120.5 \\text{ K}$ \n"
              "\n"
              "Next we repeat the step.")
        out, sayac = _onar(md)
        assert sayac["denklem_satiri_ayrildi"] == 1
        assert "\n$$\\rightarrow T_{x} = 2120.5 \\text{ K}$$\n" in out

    def test_bagintisiz_satir_varsa_dokunulmaz(self):
        # İkinci satır birinci denklemin sarılmış devamı (bağıntı yok): ikisi
        # tek satırda birleşik kalmalı.
        md = ("$$a = 5.4$$\n  \n $0.7X + 0.3Y + 5.4(Z + 3.7W) = 4.3U + 3.3V + 3.7$   \n"
              " $\\times 5.4W$ ")
        _degismez(md)

    def test_metinli_satir_varsa_dokunulmaz(self):
        _degismez("$$a = 1$$\n  \n $b = 2$ and so on   \n $c = 3$ ")

    def test_gercek_bos_satirdan_sonra_dokunulmaz(self):
        _degismez("$$a = 1$$\n\n $b = 2$ ")

    def test_display_ardinda_degilse_dokunulmaz(self):
        _degismez("Some text here.\n\n $b = 2$   \n $c = 3$ ")
