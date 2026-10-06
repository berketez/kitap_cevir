"""NUM-RETRY testleri (2026-07-06 sutton smoke).

Bug: "fax (317) 572-4002" çevirisinde 4002 kayboldu; sayı-guard yalnızca
uyarı basıyordu, final çıktı sayıyı kaybetti. Yeni davranış: kayıpta MT
bir kez daha denenir; retry TÜM sayıları VE <<LATEX_N>> placeholder'larını
içeriyorsa kullanılır.

G2a-1 (2026-10-05, mt okuma MT-7: "Bi < 0,1" → "Bi < ≤ 0 , ≤ 1" basıldı):
retry de tutmazsa bozuk çeviri KORUNMAZ, segment orijinaline döner
(``stats.number_reverts``); çağıran zincir onu kurtarma sırasına sokar
(aynı metin → sade biçim → özgün dilde bırak + işaret). Berke kararı:
çeviri kayıpsız, uydurma sayı/simge basılmaz.
"""


class TestNumberGuardRetry:
    def test_retry_used_when_numbers_recovered(self, make_latex_translator):
        original = "Call us at fax 4002 for details."
        bad = "Bize faks yoluyla ulaşın."  # 4002 kayıp
        good = "Ayrıntı için 4002 numaralı faksı arayın."
        lt = make_latex_translator(single_fn=lambda text: good)

        out = lt._check_numerical_preservation(bad, original)

        assert "4002" in out

    def test_retry_rejected_when_still_missing(self, make_latex_translator):
        original = "Call us at fax 4002 for details."
        bad = "Bize faks yoluyla ulaşın."
        still_bad = "Yine sayı içermeyen bir çeviri."
        lt = make_latex_translator(single_fn=lambda text: still_bad)

        out = lt._check_numerical_preservation(bad, original)

        # G2a-1: bozuk çeviri basılmaz; segment orijinaline döner
        assert out == original
        assert lt.stats.number_reverts == 1

    def test_retry_rejected_when_latex_placeholder_lost(self, make_latex_translator):
        original = "See <<LATEX_3>> and fax 4002."
        bad = "Bakınız <<LATEX_3>>, faks."  # 4002 kayıp
        retry = "4002 numaralı faksı arayın."  # sayı var ama placeholder yok
        lt = make_latex_translator(single_fn=lambda text: retry)

        out = lt._check_numerical_preservation(bad, original)

        # Placeholder bütünlüğü bozulan retry kabul edilmez; bozuk ilk çeviri
        # de basılmaz (G2a-1): segment orijinaline döner
        assert out == original

    def test_no_retry_when_numbers_complete(self, make_latex_translator):
        calls = []

        def spy(text):
            calls.append(text)
            return "asla kullanılmamalı"

        original = "Call fax 4002 now."
        good = "Hemen 4002 numaralı faksı arayın."
        lt = make_latex_translator(single_fn=spy)

        out = lt._check_numerical_preservation(good, original)

        assert out == good
        assert calls == []  # sayı tamken MT'ye gidilmez

    def test_decimal_comma_normalization_not_false_positive(
        self, make_latex_translator
    ):
        calls = []

        def spy(text):
            calls.append(text)
            return "asla kullanılmamalı"

        original = "thrust is 1.3 million pounds at 1.5 rate"
        good = "itki 1,3 milyon pound, oran 1,5"  # Türkçe ondalık virgül
        lt = make_latex_translator(single_fn=spy)

        out = lt._check_numerical_preservation(good, original)

        assert out == good
        assert calls == []
