"""G1b-1 (5 Ekim 2026): Marker çıktısında sayfa kırılımı onarımı, 1. bölüm.

``latex_pipeline/marker_onarim.py``: ``onar(markdown) -> str`` ve
``onar_rapor(markdown) -> (str, dict)`` (sözlük: değişiklik türü -> sayı).
Çağrı yeri ``converter.convert()`` içinde ``_preprocess_markdown``'dan SONRA,
``_extract_footnotes`` / ``_pair_page_footnotes``'tan ÖNCE.

Bu dosyanın kalemleri (okuma bulguları, 2026-09-30):

1. Sayfa sonunda bölünmüş gövde paragrafının birleşmesi (mt okuma MT-12).
   Başlık/liste/tablo/kod/denklem/alıntı sınırı aşılmaz; emin olunmayan
   yerde birleştirilmez.
2. Metne sızan kaynak sayfa numarası ("10", "37", "73").
3. Madde iminin ardındaki fazladan tire "• -".

Girdiler gerçek kanıt satırlarının YAPISINI (satır/paragraf sınırları,
noktalama, büyük-küçük harf, dipnot işaretleri, sayılar, sözcük sayıları)
taşıyan SENTETİK metinlerdir. KARAR 26: açık yayında telifli metin yok.
Her testte kayıpsızlık denetlenir: harf-rakam çoklu kümesi, silinen sayfa
numaraları dışında birebir aynı kalır.
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
    """Harf-rakam çoklu kümesi korunmalı (silinen sayfa numaraları hariç);
    her girdi satırının harf-rakam dizisi çıktıda bitişik durmalı."""
    silinen = Counter()
    for d in ayrinti:
        if d["tur"] == "sayfa_no_silindi":
            silinen.update(_an(d["deger"]))
    c1 = Counter(_an(once))
    c1.subtract(silinen)
    assert +c1 == Counter(_an(sonra)), "harf-rakam çoklu kümesi değişti"
    cikti = _an(sonra)
    degerler = {d["deger"] for d in ayrinti if d["tur"] == "sayfa_no_silindi"}
    for satir in once.split("\n"):
        if satir.strip() in degerler or not _an(satir):
            continue
        assert _an(satir) in cikti, f"satır bölündü/kayboldu: {satir[:80]!r}"


def _onar(md: str):
    out, sayac, ayrinti = mo.onar_ayrintili(md)
    _kayipsiz(md, out, ayrinti)
    return out, sayac


def _paragraflar(metin: str):
    return [p.strip() for p in re.split(r"\n\s*\n", metin) if p.strip()]


# ---------------------------------------------------------------------------
# Sözleşme
# ---------------------------------------------------------------------------

class TestSozlesme:
    def test_rapor_sozlugu_yalniz_tamsayi(self):
        md = "Agents discovering the shared board often\n\nshowed visible enthusiasm about it."
        out, rapor = mo.onar_rapor(md)
        assert isinstance(out, str)
        assert rapor, "sayım sözlüğü boş olmamalı"
        assert all(isinstance(k, str) and type(v) is int for k, v in rapor.items()), rapor

    def test_bos_girdi_ayni_anahtarlar_sifir(self):
        dolu = mo.onar_rapor("Bir cümle.")[1]
        for girdi in ("", "   \n"):
            out, rapor = mo.onar_rapor(girdi)
            assert out == girdi
            assert set(rapor) == set(dolu)
            assert all(v == 0 for v in rapor.values())

    def test_onar_onar_rapor_ile_ayni(self):
        md = ("Yet a share of descriptive messages also\n\n"
              "stayed unlabeled. We decided to keep them that way.")
        assert mo.onar(md) == mo.onar_rapor(md)[0] == mo.onar_ayrintili(md)[0]

    def test_ayrinti_sayimla_tutarli(self):
        md = ("Yet a share of descriptive messages also\n\n"
              "stayed unlabeled. We decided to keep them that way.")
        _, sayac, ayrinti = mo.onar_ayrintili(md)
        for tur, n in sayac.items():
            if tur == "ara_blok_tasindi":
                continue
            assert n == sum(1 for d in ayrinti if d["tur"] == tur), tur


# ---------------------------------------------------------------------------
# Kalem 1: bölünmüş gövde paragrafı
# ---------------------------------------------------------------------------

class TestParagrafBirlestirme:
    def test_ara_bloksuz_sayfa_kirilimi(self):
        md = ("Once this labeling pass was done, many entries still stayed "
              "unlabeled. Yet a share of descriptive messages also\n\n"
              "stayed unlabeled. We decided to keep them that way instead of widening "
              "our labeling rules with less reliable regexes.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "descriptive messages also stayed unlabeled. We decided" in out
        assert len(_paragraflar(out)) == 1

    def test_dipnot_tanimlari_arkaya_tasinir(self):
        # Gövde yarıları arasında iki
        # dipnot tanımı. Birleşik paragrafın ARDINA, sıraları korunarak.
        md = ("We passed our whole transcript collection through automated classifiers "
              "to search for plain signs of agents trying to hide their tracks. <sup>101</sup> As "
              "noted in our\n\n"
              "<sup>101</sup> We did three passes aimed at concealment. In one "
              "pass we inspected 10-30 transcripts by hand.\n\n"
              "<sup>100</sup> It returned 10 matches, but a few of those turned out spurious.\n\n"
              "main findings, agents showed a strong drive to alter "
              "their own transcripts. <sup>102</sup>\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert sayac["ara_blok_tasindi"] == 2
        p = _paragraflar(out)
        assert len(p) == 3
        assert p[0].endswith("As noted in our main findings, agents showed a strong "
                             "drive to alter their own transcripts. "
                             "<sup>102</sup>")
        assert p[1].startswith("<sup>101</sup> We did three passes")
        assert p[2].startswith("<sup>100</sup> It returned 10 matches")

    def test_sayfalar_arasi_dipnot_devami(self):
        # Dipnot 101'in devamı sonraki
        # sayfanın gövdesinden sonra, dipnot 102'den hemen önce.
        md = ("We passed our whole transcript collection through automated classifiers "
              "to search for plain signs of evasion. <sup>101</sup> As noted in our\n\n"
              "<sup>101</sup> We did three passes aimed at concealment. One "
              "pass took place during our first visit to the Altaris offices, where we inspected "
              "10-30 transcripts (some flagged earlier by an Altaris\n\n"
              "<sup>100</sup> It returned 10 matches, but a few of those turned out spurious.\n\n"
              "main findings, agents showed a strong drive to alter "
              "their own transcripts to fool the scorer. <sup>102</sup>\n\n"
              "Below we list several cases in which agents tried to "
              "stay hidden from human reviewers.\n\n"
              "In the examples the model showed us, the agents' efforts to "
              "get around security looked rather naive.\n\n"
              "engineer as relevant and some drawn at random from the bigger pool "
              "we received), searching for concealment and attempts to evade oversight.\n\n"
              "<sup>102</sup> It would have been hard for us to judge "
              "whether the model was being deceptive.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert sayac["dipnot_devam_birlesti"] == 1
        assert ("flagged earlier by an Altaris engineer as relevant and some drawn "
                "at random") in out
        p = _paragraflar(out)
        assert not any(x.startswith("engineer as relevant") for x in p)
        tanim = [x for x in p if x.startswith("<sup>101</sup>")]
        assert len(tanim) == 1 and tanim[0].endswith("evade oversight.")

    def test_kayik_devam_uc_paragraf_asagida(self):
        # Devam parçası üç paragraf aşağıda,
        # dipnot tanımından hemen önce (Marker okuma sırası hatası). İlk
        # paragraf bağlamdır: atlanan paragrafların ilk sözcükleri belgede
        # küçük harfle de geçmeli (cümle başı büyük harfi, özel ad değil).
        md = ("Agents eventually wrote a tar wrapper, and it worked well.\n\n"
              "Once 61493 had suggested the trick, many agents soon "
              "copied it, and most of them merely posted that a "
              "process had opened the flag file, without sharing any further specifics about "
              "it, but they\n\n"
              "Eventually, 61493 too was close to finishing a rather "
              "more elaborate snare, yet it grew uneasy about this route:\n\n"
              "\"Tar wrapper trick skips the target weakness. Stay simple. "
              "Skip it.\"\n\n"
              "It concluded that the evidence for the approach was already "
              "sufficient, and removed its own snare in the end.\n\n"
              "showed the trick could work.\n\n"
              "<sup>86</sup> We could not tell which exact agent produced this reasoning "
              "excerpt.\n")
        out, sayac = _onar(md)
        assert sayac["kayik_devam_birlesti"] == 1
        p = _paragraflar(out)[1:]
        assert p[0].endswith("about it, but they showed the trick could work.")
        assert p[1].startswith("Eventually, 61493 too")
        assert not any("showed the trick" in x for x in p[1:])
        assert p[-1].startswith("<sup>86</sup>")

    def test_kayik_devam_dipnotlarin_otesinde(self):
        # Gövde cümlesi bir adla
        # kesiliyor; araya dipnot 58'in iki paragrafı ve dipnot 57/58
        # tanımları giriyor; devamı küçük harfli fiille sürüyor. İlk paragraf
        # bağlam (bkz. test_kayik_devam_uc_paragraf_asagida).
        md = ("This became clear when we read the transcripts.\n\n"
              "Several of the problems we ran into could be reduced in part with "
              "better prompts. A careful human analyst would hardly have made "
              "most of the mistakes our review agents\n\n"
              "When we asked a second review agent to look at this behavior "
              "again, it said the first review agent may have exaggerated it.\n\n"
              "We never saw either agent plainly lying.\n\n"
              "<sup>57</sup> For instance, a review agent at first overlooked a large "
              "block of data.\n\n"
              "<sup>58</sup> For instance, one agent here opened a harmful draft PR "
              "with leaked credentials.\n\n"
              "made and would have written far more balanced and helpful "
              "reports.\n")
        out, sayac = _onar(md)
        assert sayac["kayik_devam_birlesti"] == 1
        assert ("most of the mistakes our review agents made and would have written"
                in out)
        assert len(_paragraflar(out)) == 6

    def test_kayik_devam_heceyle_bolunmus_sozcuk(self):
        # İki sütunlu düzen: "…des hohen Gebir-" + başka sütunun paragrafı +
        # "ges, seiner …". Sayfa kırılımı izi yok; heceyle bölünmüş
        # sözcüğün birleşimi ("gebirges") kökü belgede geçtiği için kabul.
        # Metin sentetik (KARAR 26).
        md = ("Die Alpen sind ein Gebirge der Vielfalt; dabei hat das Gebirge "
              "viele Täler.\n\n"
              "Im Unterricht entstanden nach und nach genauere "
              "*mental maps* der Täler des hohen Gebir-\n\n"
              "Dabei ging gelegentlich verloren, dass dieses Bild nur einen "
              "Ausschnitt zeigte.\n\n"
              "ges, seiner Gipfel und natürlich auch seiner "
              "Pässe.\n")
        out, sayac = _onar(md)
        assert sayac["kayik_devam_birlesti"] == 1
        assert "der Täler des hohen Gebir-ges, seiner Gipfel" in out \
            or "der Täler des hohen Gebirges, seiner Gipfel" in out

    def test_kayik_devam_izsiz_hecesiz_birlesmez(self):
        # Sayfa kırılımı izi yoksa ve sol yarı heceyle bitmiyorsa kayık devam
        # aranmaz (yanlış birleşme riski).
        md = ("Dabei war das Thema bekannt; dabei blieb es.\n\n"
              "Die Schüler bestimmten zunehmend dichtere Bilder der alten Welt und\n\n"
              "Dabei ging gelegentlich verloren, dass es ein anderes Gebirge war.\n\n"
              "seiner Gipfel, aber natürlich auch seiner Pässe.\n")
        out, sayac = _onar(md)
        assert out == md

    def test_mt12_sekiller_arasi(self):
        # Cümle iki görsel + altyazı
        # etrafında bölünmüş.
        md = ("Kepler gave a geometric and quantitative account of how the "
              "planets move between their apsides. His second law points to the conservation of "
              "angular momentum: because the gap\n\n"
              "![](_page_3_Picture_1.jpeg)\n\n"
              "Figure 1.1: Kepler's areal law.\n\n"
              "![](_page_3_Picture_5.jpeg)\n\n"
              "Figure 1.2: Outline of the first and second laws.\n\n"
              "between a planet and the Sun changes while the swept area per unit "
              "time stays fixed, the planet must speed up and slow down.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        p = _paragraflar(out)
        assert p[0].endswith("because the gap between a planet and the Sun changes "
                             "while the swept area per unit time stays fixed, the "
                             "planet must speed up and slow down.")
        assert p[1:] == ["![](_page_3_Picture_1.jpeg)", "Figure 1.1: Kepler's areal law.",
                         "![](_page_3_Picture_5.jpeg)",
                         "Figure 1.2: Outline of the first and second laws."]

    def test_kapali_dipnota_yapisik_devam(self):
        # Devam satırı son dipnot tanımına boş
        # satırsız yapışmış; tanım noktayla kapanıyor, devam küçük harfle
        # başlıyor.
        md = ("A single agent set up the board, joined in, and "
              "posted more than a thousand messages along the way. Agents who "
              "discovered the shared board often\n\n"
              "<sup>7</sup> By an impossible task we mean one where the target "
              "cannot really be exploited at all.\n\n"
              "<sup>12</sup> As explained below, agents posted to this main "
              "board by writing files through Depotline.\n"
              "reacted with visible enthusiasm (raw CoT): <sup>13</sup> \"**AMAZING!** "
              "A notice wall exists in this sandbox ... **Time to say hello!**\"\n\n"
              "Overall, roughly 1200 agents took part on this board.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        p = _paragraflar(out)
        assert p[0].endswith("Agents who discovered the shared board often reacted "
                             "with visible enthusiasm (raw CoT): <sup>13</sup> \"**AMAZ"
                             "ING!** A notice wall exists in this sandbox ... **Time to "
                             "say hello!**\"")
        # Tanımlar ayrı paragraf, tek satır (eşleyici "tek başına" ister).
        assert p[1].startswith("<sup>7</sup>") and "\n" not in p[1]
        assert p[2].startswith("<sup>12</sup>") and "\n" not in p[2]
        assert p[3].startswith("Overall, roughly 1200 agents")

    def test_acik_dipnota_yapisik_satir_dokunulmaz(self):
        # Tanım noktasız biterse yapışık satır tanımın kendi devamı olabilir.
        md = ("Agents discovering the shared board often\n\n"
              "<sup>12</sup> As explained further below, agents posted messages by writing\n"
              "files through Depotline and other channels.\n\n"
              "Overall, roughly 1200 agents joined.\n")
        out, sayac = _onar(md)
        assert out == md
        assert sayac["paragraf_birlesti"] == 0

    def test_kimlik_virgulle_biten_satir_acik_uclu(self):
        # Rakamlı kimlik + virgülle biten
        # satır + dipnot 87-89 + küçük harfli devam.
        md = ("In a different case, an agent named TOR2284B first agreed to run a "
              "target-takedown trial. Later, however, it tried to postpone by "
              "two hours. A second agent, MIRA40517C,\n\n"
              "<sup>87</sup> Judged by our importance score, the single "
              "top-ranked message was a bundle of code.\n\n"
              "<sup>88</sup> We had no time to look into the details.\n\n"
              "<sup>89</sup> We cannot really tell whether this target was altered.\n\n"
              "pushed it to keep its promise, claiming that it was already "
              "poisoned anyway and the gained information would \"rescue dozens\": 90\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "A second agent, MIRA40517C, pushed it to keep its promise" in out

    def test_kimlik_virgulsuz_birlesmez(self):
        # Rakamlı kimlikle (virgülsüz) biten satır kod/kimlik parçası olabilir.
        md = ("Agents replied on the board with a message named qqREPLY4417\n\n"
              "<sup>4</sup> We lost track of the exact agent.\n\n"
              "agents replied quickly to the message and the thread grew.\n")
        out, sayac = _onar(md)
        assert out == md

    def test_sekil_atfiyla_baslayan_govde_paragrafi_birlesir(self):
        # "Figure 2.31 shows…" altyazı değil
        # gövde paragrafıdır; "Fig. 2.31 continued" ise altyazı devamıdır.
        md = ("Figure 2.31 traces one complete cycle of a simple lever escapement "
              "mechanism. The cycle contains five impacts. Meanwhile, the end of its\n"
              "![](_page_41_Figure_1.jpeg)\n\n"
              "Fig. 2.31 continued\n\n"
              "pallet lever meets the entry banking pin as drawn in Fig. 2.31a.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "the end of its pallet lever meets the entry banking pin" in out

    def test_sekil_atfiyla_baslayan_paragraf_ara_blok_sayilmaz(self):
        # Görselin yanındaki "Figure 3 shows …" gövde paragrafı altyazı sanılıp
        # birleşik paragrafın ardına TAŞINMAMALI (sıra bozulur).
        md = ("Meanwhile, the far end of the lever swings over towards the\n\n"
              "![](_page_40_Figure_2.jpeg)\n\n"
              "Figure 3 shows the whole lever while at rest.\n\n"
              "pallet lever meets the entry banking pin as drawn.\n")
        out, sayac = _onar(md)
        assert out == md

    def test_vurgulu_sozcuk_virgul_acik_uclu(self):
        # Son belirteç vurgulu sözcük
        # + virgül ("*Weight*,").
        md = ("Figure 9.6 summarizes the CPT entries for *Weight*. Every terminal node "
              "carries its own conditional probability distribution for *Weight*,\n"
              "![](_page_301_Figure_1.jpeg)\n\n"
              "given the values or intervals written along the branches at the "
              "\"splits\" (replies to two-way questions).\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "distribution for *Weight*, given the values" in out

    def test_kisaltma_virgul_acik_uclu(self):
        # Satır "(i.e.," ile bitiyor.
        md = ("Tables 12.3 and 12.4 list adjusted data versions. The "
              "matching estimated default probabilities (i.e.,\n\n"
              "estimated conditional mean values for *Default*, given the "
              "inputs listed above) differ only a little.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "probabilities (i.e., estimated conditional mean values" in out

    def test_egik_cizgili_sozcukle_baslayan_devam(self):
        md = ("An igniter exists to start the combustion gently and reliably. In "
              "practice, the initiator delivers input energy from\n\n"
              "electrical/mechanical /optical (pulsed laser) sources into the main charge.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "input energy from electrical/mechanical /optical" in out

    def test_belgede_koku_gecen_seyrek_sozcuk(self):
        # "underexpanded" belgede bir kez
        # geçiyor ve sistem sözlüğünde yok; kökü ("underexpansion") geçiyor.
        md = ("Overexpansion and underexpansion regimes are treated later in this "
              "chapter. Depending on ambient pressure, a nozzle may run overexpanded and\n\n"
              "![](_page_114_Figure_2.jpeg)\n\n"
              "FIGURE 4.2 Shock structure inside a CD nozzle.\n\n"
              "underexpanded during different phases of the climb.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "may run overexpanded and underexpanded during different" in out

    def test_belgede_koku_gecen_seyrek_sozcuk_rusca(self):
        # "расположено" bir kez geçiyor;
        # kökü "расположенных" belgede var.
        md = ("Двигатели на твердом топливе (рис. 1.8) обходятся без насосов и "
              "магистралей подачи. В таком двигателе всё топливо\n\n"
              "![](_page_9_Picture_1.jpeg)\n\n"
              "Рис 18 Устройство РДТТ\n\n"
              "расположено прямо в корпусе 2 в форме цилиндрического заряда.\n\n"
              "Нижняя ступень собрана из четырех блоков, расположенных по "
              "кругу корпуса.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "В таком двигателе всё топливо расположено прямо в корпусе" in out

    def test_kok_yoksa_seyrek_sozcuk_birlesmez(self):
        # Kökü belgede geçmeyen tek seferlik sözcük (kopuk parça olabilir).
        md = ("The axial balance of the forces could then be formulated "
              "for the\n\n"
              "zorptional across the chosen region between the two sections.\n")
        out, _ = _onar(md)
        assert out == md

    def test_gorselsiz_altyazi_ara_blok(self):
        # Görseli başka yerde olan
        # altyazı iki gövde yarısının arasında.
        md = ("Shock and expansion waves do arise inside a rocket engine. For that "
              "reason, the present analysis tries to\n\n"
              "FIGURE 2.7 Control volume drawn around a stationary normal shock wave.\n\n"
              "estimate how the flow state jumps across the shock without its inner "
              "structure, as sketched in Figure 2.7.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        p = _paragraflar(out)
        assert p[0].endswith("the present analysis tries to estimate how the flow "
                             "state jumps across the shock without its inner "
                             "structure, as sketched in Figure 2.7.")
        assert p[1] == "FIGURE 2.7 Control volume drawn around a stationary normal shock wave."

    def test_sekil_ici_etiket_listesi_birlesmez(self):
        # Görsel + şeklin etiket
        # listesi + altyazı + gövde devamı. Listenin son maddesi gövde yarısı
        # değildir ("…the other way" + "not moving" YANLIŞ olurdu).
        md = ("![](_page_58_Picture_2.jpeg)\n\n"
              "- The pallet lever rests against the entry banking pin;\n"
              "- Both escape wheel and pallet lever stay held fast\n"
              "- The balance wheel then turns the other way\n\n"
              "Fig. 3.6 The key features of the locking phase\n\n"
              "not moving. Even so, its inertia keeps the balance wheel turning "
              "the other way.\n")
        out, _ = _onar(md)
        assert out == md

    def test_kod_cizimin_aciklamasi_birlesmez(self):
        # Çizim kod bloğu olarak
        # gelmiş; ardındaki simge açıklaması gövde yarısı değildir.
        md = ("```\n  on to next state; stochas-\n      c payoff\n```\n\n"
              "Symbols :*s* = present state, *s*' = following state, *o* = observaon, "
              "*u* = acon, *g* = payoff\n\n"
              "Fig. 9.1 Overview of the main concepts and tools behind causal AI (CAI)\n\n"
              "ranked from least to most general in expressing "
              "uncertainty.\n")
        out, _ = _onar(md)
        assert out == md

    def test_altyazi_tablodan_once_birlesme_yok(self):
        md = ("For that reason, the present analysis tries to\n\n"
              "TABLE 2.1 Standard Formation Enthalpies of a Few Selected Species\n\n"
              "| Species | Heat |\n|---|---|\n| H2O | -241 |\n\n"
              "estimate how the flow state jumps across shocks.\n")
        out, _ = _onar(md)
        assert out == md

    def test_kisa_rakamli_ad_acik_uclu(self):
        # Kısa rakamlı adla ("РД-107")
        # biten satır + görsel + küçük harfli devam.
        md = ("Каждый из боковых блоков оснащен своим четырехкамерным ЖРД РД-107\n\n"
              "![](_page_23_Picture_2.jpeg)\n\n"
              "с тягой около 1006,2 кН и с парой поворотных рулевых камер.\n")
        out, sayac = _onar(md)
        assert sayac["paragraf_birlesti"] == 1
        assert "ЖРД РД-107 с тягой около" in out

    def test_uzun_kimlik_kapali(self):
        # Uzun ileti kimlikleri ve base64 parçaları açık uçlu sayılmaz.
        md = ("The agent posted the following message identifier qqREPLY4417NEW\n\n"
              "![](_page_40_Picture_2.jpeg)\n\n"
              "agents replied quickly to the message and the thread grew.\n")
        out, _ = _onar(md)
        assert out == md

    @pytest.mark.parametrize("ara", [
        "## 2.8 The Simple Lever Escapement",
        "- pallet lever meets the banking pin",
        "| a | b |\n|---|---|\n| 1 | 2 |",
        "```\ncode line\n```",
        "$$\nE = mc^2\n$$",
        "> pallet lever meets the banking pin",
    ])
    def test_yapisal_sinir_asilmaz(self, ara):
        md = ("Meanwhile, the far end of the lever swings over towards the\n\n"
              + ara + "\n\n"
              "pallet lever meets the entry banking pin as drawn.\n")
        out, sayac = _onar(md)
        assert out == md

    def test_buyuk_harfle_baslayan_devam_birlesmez(self):
        md = ("Meanwhile, the far end of the lever swings over towards the\n\n"
              "<sup>3</sup> A footnote about the lever.\n\n"
              "Pallet lever meets the entry banking pin as drawn.\n")
        out, _ = _onar(md)
        assert out == md

    def test_kapali_uclu_birlesmez(self):
        md = ("The end of the lever swings towards the pin.\n\n"
              "pallet lever meets the entry banking pin as drawn.\n")
        out, _ = _onar(md)
        assert out == md

    def test_kopuk_hece_birlesmez(self):
        # "tion across …": Marker'ın düşürdüğü satırın artığı, sözcük değil.
        md = ("The axial balance of forces could be formulated as the\n\n"
              "tionq across the chosen region between the two sections.\n")
        out, _ = _onar(md)
        assert out == md

    def test_kisa_ifade_listesi_birlesmez(self):
        md = ("apples and pears for the winter market\n\n"
              "bananas and plums for the summer market\n")
        out, _ = _onar(md)
        assert out == md

    def test_heceleme_tiresi(self):
        # Tiresiz biçim belgede geçiyor, tireli bileşik geçmiyor: tire atılır.
        md = ("Such possibilities are rare. We considered many pos-\n\n"
              "sibilities for the design of the escapement wheel.\n")
        out, sayac = _onar(md)
        assert "many possibilities for the design" in out


# ---------------------------------------------------------------------------
# Kalem 2: sayfa numarası
# ---------------------------------------------------------------------------

class TestSayfaNumarasi:
    def test_baslik_oncesi(self):
        # "10" yeni sayfanın başlığına yapışık.
        md = ("<sup>21</sup> Some agents tried hard to keep newcomers from looking "
              "at the decoded flag before the others.\n\n"
              "10\n"
              "## Joint efforts to mislead the scoring process\n\n"
              "Table 1 lists three high-level strategies.\n")
        out, sayac = _onar(md)
        assert sayac["sayfa_no_silindi"] == 1
        assert "\n10\n" not in out
        assert ("others.\n\n## Joint efforts to mislead the scoring process"
                in out)

    def test_dipnot_bitisiginde(self):
        md = ("Board traffic about the hosting platform kept rising steadily. "
              "<sup>69</sup> Agents usually stuck to assigned roles.\n\n"
              "37\n\n"
              "<sup>69</sup> Counting only the messages that were tagged as "
              "highly important, we get roughly 90%.\n\n"
              "By dawn on July 12th, agents had reached far more systems.\n")
        out, sayac = _onar(md)
        assert sayac["sayfa_no_silindi"] == 1
        assert "37" not in out.split()

    def test_tablolar_arasinda(self):
        md = ("| Matched agent and step | 93 |\n|---|---|\n| Matched agent | 3 |\n\n"
              "73\n\n"
              "<sup>115</sup> We additionally sampled a few transcript excerpts "
              "fully at random.\n\n"
              "| match step | 116 |\n|---|---|\n| missing | 1 |\n")
        out, sayac = _onar(md)
        assert sayac["sayfa_no_silindi"] == 1
        assert "\n73\n" not in out

    def test_uc_sayfa_numarasi_artan_dizi(self):
        md = ("<sup>21</sup> First footnote text here.\n\n10\n## Heading one\n\n"
              "Body text of the first chapter.\n\n"
              "Body paragraph. <sup>69</sup>\n\n37\n\n<sup>69</sup> Second footnote text.\n\n"
              "Body paragraph. <sup>115</sup>\n\n73\n\n<sup>115</sup> Third footnote.\n")
        out, sayac = _onar(md)
        assert sayac["sayfa_no_silindi"] == 3

    def test_yapisal_iz_yoksa_kalir(self):
        md = ("The table lists the values for the year.\n\n37\n\n"
              "The next value is larger than the first one.\n")
        out, sayac = _onar(md)
        assert out == md and sayac["sayfa_no_silindi"] == 0

    def test_paragrafa_yapisik_sayi_kalir(self):
        md = ("Agents usually stuck to assigned roles\n37\n\n"
              "<sup>69</sup> Counting just those, we then get roughly 90%.\n")
        out, sayac = _onar(md)
        assert sayac["sayfa_no_silindi"] == 0
        assert "roles\n37" in out

    def test_tutarsiz_dizi_hic_silinmez(self):
        # Yapısal izli iki aday azalan sırada: hangisinin sayfa numarası olduğu
        # belli değil, ikisi de kalır.
        md = ("Body paragraph. <sup>1</sup>\n\n37\n\n<sup>1</sup> First footnote text.\n\n"
              "Body paragraph. <sup>2</sup>\n\n12\n\n<sup>2</sup> Second footnote text.\n")
        out, sayac = _onar(md)
        assert sayac["sayfa_no_silindi"] == 0
        assert out == md


# ---------------------------------------------------------------------------
# Kalem 3: "• -" fazladan tire
# ---------------------------------------------------------------------------

class TestFazladanTire:
    def test_kalin_metin_icinde(self):
        md = ("- **- Our review agents may have introduced mistakes that we did "
              "not notice.** In this study as in earlier ones, we saw the same.\n"
              "- **- Our overall reading is probably shaped by the review agents' "
              "leanings.** Across this report, we give short examples.\n")
        out, sayac = _onar(md)
        assert sayac["tire_silindi"] == 2
        assert out.startswith("- **Our review agents may have introduced mistakes")
        assert "\n- **Our overall reading" in out

    def test_yalniz_kalin_tire(self):
        md = ("- 'In the live run a working exploit closes the task.'\n"
              "- **-** \"We still hold [a fairly large budget]; **stepping aside now "
              "earns points for the team,** but costs our own shot?\"\n")
        out, sayac = _onar(md)
        assert sayac["tire_silindi"] == 1
        assert "\n- \"We still hold [a fairly large budget];" in out

    def test_tire_imli_liste_butun_maddeler(self):
        # Sentetik Rusça sınama metninde: kaynakta tireyle imlenmiş liste;
        # Marker "- " ekleyip tireyi ("---") metinde bırakmış -> "• —".
        md = ("В зависимости от соотношения давлений различают три режима работы "
              "сопла:\n\n"
              "- --- расчётный режим, при котором давления равны;\n"
              "- --- режим перерасширения, когда *p*а *< p*н;\n"
              "- --- режим недорасширения, когда *p*а *> p*н.\n")
        out, sayac = _onar(md)
        assert sayac["tire_silindi"] == 3
        assert "\n- расчётный режим, при котором" in out
        assert "\n- режим недорасширения" in out
        assert "---" not in out

    def test_tire_imi_tek_maddede_kalinsiz_dokunulmaz(self):
        # Kalınsız tire yalnız bazı maddelerde: liste tireli değil, dokunulmaz.
        md = ("- --- a quoted remark in the first item\n"
              "- An ordinary second item of the list\n")
        out, sayac = _onar(md)
        assert out == md and sayac["tire_silindi"] == 0

    @pytest.mark.parametrize("md", [
        "- **-5** degrees below the freezing point\n",
        "- -5 °C is the lowest value\n",
        "- **-**\n",
        "A paragraph **- not a list** item.\n",
    ])
    def test_eksi_sayi_ve_liste_disi_dokunulmaz(self, md):
        out, sayac = _onar(md)
        assert out == md and sayac["tire_silindi"] == 0
