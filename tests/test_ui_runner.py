"""ui/runner.py -- arayüzün pipeline koşucusu.

Koşu testleri gerçek alt süreçlerle çalışır: sahte bir CLI betiği gerçek
kapılar gibi olay dosyasına yazar, stdout'a sayaç ve son satırda JSON özeti
basar. Yükleme testleri eski app.py'nin APP-B05 güvencesini taşır.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from ui import runner
from ui.runner import PROJECT_ROOT, Run, discard_upload, save_upload, upload_problem

MB = 1024 * 1024


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Yükleme dizinleri sistemin geçici dizini yerine tmp_path altına açılır."""
    root = tmp_path / "gecici"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "sahte_cli.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


def _finish(run: Run, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while run.running and time.time() < deadline:
        run.poll()
        time.sleep(0.05)
    assert not run.running, "süreç zaman aşımında bitmedi"


class TestPlatform:
    def test_apple_silicon_mac_kabul(self, monkeypatch):
        monkeypatch.setattr(runner.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(runner.platform, "machine", lambda: "arm64")
        assert runner.platform_problem() is None

    def test_mac_disi_reddedilir(self, monkeypatch):
        monkeypatch.setattr(runner.platform, "system", lambda: "Linux")
        assert "macOS" in runner.platform_problem()

    def test_intel_ve_rosetta_reddedilir(self, monkeypatch):
        monkeypatch.setattr(runner.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
        assert "Apple Silicon" in runner.platform_problem()


class TestCommands:
    """Arayüz iş mantığı kopyalamaz: her iş CLI kapısından koşar."""

    def test_cikar_kapisi_json_ile(self):
        book = Path("/kitaplar/a.pdf")
        assert runner.extract_command(book) == [
            sys.executable, str(PROJECT_ROOT / "1_cikar.py"), str(book), "--json"]
        assert runner.extract_command(book, "Ad")[-2:] == ["--name", "Ad"]

    def test_derle_kapisi_json_ile(self):
        tex = Path("/cikti/EXPORT_TR.tex")
        assert runner.compile_command(tex) == [
            sys.executable, str(PROJECT_ROOT / "2_derle.py"), str(tex), "--json"]

    def test_yerel_model_kapisi(self):
        book = Path("/kitaplar/a.epub")
        assert runner.translate_command(book) == [
            sys.executable, str(PROJECT_ROOT / "translate_book.py"), str(book)]
        assert runner.translate_command(book, "Ad")[-2:] == ["--name", "Ad"]

    def test_yollar_cli_adlandirmasini_izler(self):
        from translate_book import guess_book_name, slugify

        book = Path("/kitaplar/Kitap Adi -- Yayinevi 9781234567890.pdf")
        slug = slugify(guess_book_name(str(book)))
        files = runner.tex_files(book)
        assert files.output_dir == PROJECT_ROOT / "output" / slug
        assert files.export_tex == files.output_dir / "EXPORT.tex"
        assert files.export_tr_tex == files.output_dir / "EXPORT_TR.tex"
        assert runner.model_pdf(book) == (
            PROJECT_ROOT / "output" / slug / "compiled" / f"{slug}_TR.pdf")

    def test_ad_verilirse_yollar_ona_gore(self):
        from translate_book import slugify

        book = Path("/kitaplar/a.pdf")
        assert runner.tex_files(book, "Özel Ad").output_dir == (
            PROJECT_ROOT / "output" / slugify("Özel Ad"))


class TestSaveUpload:
    """APP-B05: yükleme adı diske aynen yazıldığı için yol aşımı reddedilir."""

    @pytest.mark.parametrize("name", [
        "../../etc/passwd.pdf",
        "..\\..\\Windows\\system32\\cmd.exe.pdf",
        "/etc/shadow.pdf",
        "book.pdf/../../secret.txt",
        "",
        "..",
    ])
    def test_yol_asimi_reddedilir(self, name):
        assert upload_problem(name, 1024, 500 * MB) is not None
        with pytest.raises(ValueError):
            save_upload(name, b"%PDF", 500 * MB)

    def test_boyut_siniri(self):
        assert upload_problem("kitap.pdf", 600 * MB, 500 * MB) is not None

    def test_kitap_disi_uzanti_reddedilir(self):
        assert upload_problem("notlar.txt", 1024, 500 * MB) is not None

    def test_normal_yukleme_ozgun_adla_yazilir(self, temp_root):
        assert upload_problem("kitap.pdf", 10 * MB, 500 * MB) is None
        path = save_upload("Kitap Adı.PDF", b"%PDF-1.4", 500 * MB)
        try:
            assert path.name == "Kitap Adı.PDF"
            assert path.read_bytes() == b"%PDF-1.4"
            assert path.parent.name.startswith(runner.UPLOAD_DIR_PREFIX)
            assert path.parent.parent == temp_root
        finally:
            assert discard_upload(path)


class TestUploadCleanup:
    """Denetim 2026-09-24 Mercek 3 #16: yüklenen kitabın geçici kopyası silinir.

    Yeni kitap gelince önceki kitabın dizini, sunucu kapanınca kalanların
    hepsi silinir; save_upload'ın açmadığı hiçbir yola dokunulmaz.
    """

    def test_yeni_yukleme_oncekinin_dizinini_siler(self, temp_root):
        old = save_upload("Eski.pdf", b"%PDF eski", 500 * MB)
        new = save_upload("Yeni.pdf", b"%PDF yeni", 500 * MB, replaces=old)
        try:
            assert not old.parent.exists()
            assert new.read_bytes() == b"%PDF yeni"
            assert list(temp_root.iterdir()) == [new.parent]
        finally:
            discard_upload(new)

    def test_reddedilen_yukleme_oncekini_silmez(self, temp_root):
        old = save_upload("Eski.pdf", b"%PDF eski", 500 * MB)
        try:
            with pytest.raises(ValueError):
                save_upload("notlar.txt", b"metin", 500 * MB, replaces=old)
            assert old.read_bytes() == b"%PDF eski"
            assert list(temp_root.iterdir()) == [old.parent]
        finally:
            discard_upload(old)

    def test_ilk_yuklemede_onceki_yok(self, temp_root):
        path = save_upload("Kitap.pdf", b"%PDF", 500 * MB, replaces=None)
        assert path.exists()
        assert discard_upload(path)
        assert not path.parent.exists()
        assert not discard_upload(path)        # ikinci kez: artık kayıtlı değil

    def test_yabanci_yollar_silinmez(self, tmp_path, temp_root):
        own = save_upload("Kitap.pdf", b"%PDF", 500 * MB)
        user_book = tmp_path / "kitaplar" / "Kitap.pdf"
        look_alike = temp_root / f"{runner.UPLOAD_DIR_PREFIX}elle" / "Kitap.pdf"
        for path in (user_book, look_alike):
            path.parent.mkdir()
            path.write_bytes(b"%PDF")
        try:
            assert not discard_upload(None)
            assert not discard_upload(user_book)
            assert not discard_upload(look_alike)
            # Kayıtlı dizinden türetilmiş ama ondan çıkan ya da alt dizine inen yol.
            assert not discard_upload(own.parent / ".." / look_alike.parent.name / "Kitap.pdf")
            assert not discard_upload(own.parent / "alt" / "Kitap.pdf")
            assert not discard_upload(own.parent)
            assert user_book.exists() and look_alike.exists() and own.exists()
        finally:
            discard_upload(own)

    def test_baglantiya_donmus_dizin_silinmez(self, tmp_path, temp_root):
        own = save_upload("Kitap.pdf", b"%PDF", 500 * MB)
        victim = tmp_path / "degerli"
        victim.mkdir()
        (victim / "Kitap.pdf").write_bytes(b"korunmali")
        shutil.rmtree(own.parent)
        own.parent.symlink_to(victim, target_is_directory=True)
        try:
            assert not discard_upload(own)
            assert (victim / "Kitap.pdf").read_bytes() == b"korunmali"
        finally:
            own.parent.unlink()

    def test_surec_kapanirken_kalan_dizinler_silinir(self, tmp_path):
        """atexit kaydı gerçek bir süreçte: süreç bitince dizin kalmamalı."""
        code = textwrap.dedent("""
            from ui.runner import save_upload
            first = save_upload("Bir.pdf", b"%PDF", 1 << 20)
            second = save_upload("Iki.epub", b"PK", 1 << 20)
            print(first.parent)
            print(second.parent)
        """)
        env = dict(os.environ, TMPDIR=str(tmp_path))
        done = subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        folders = [Path(line) for line in done.stdout.split()]
        assert len(folders) == 2
        for folder in folders:
            assert folder.name.startswith(runner.UPLOAD_DIR_PREFIX)
            assert folder.resolve().parent == tmp_path.resolve()
            assert not folder.exists()


class TestExitCodes:
    """Arayüz CLI çıkış kodlarını kendisi tanımlamaz, translate_book'tan okur."""

    def test_dil_kapisi_kodu_cli_ile_ayni(self):
        import translate_book

        assert runner.EXIT_SOURCE_NOT_ENGLISH == translate_book.EXIT_SOURCE_NOT_ENGLISH


class TestRun:
    def test_olay_sayac_ve_json_ozeti(self, tmp_path):
        run = Run("translate", _script(tmp_path, """
            import json, os
            with open(os.environ["KITAPCEVIR_EVENT_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "phase", "phase": "5.5",
                                    "title": "Çeviri", "epoch": 1.0}) + "\\n")
            print("Segment 3/10")
            print("Recognizing Text:  42%|####| 64/152 [01:03<01:27,  1.00it/s]")
            print("bir günlük satırı")
            print(json.dumps({"ok": True, "pdf": "/x/y.pdf"}))
        """))
        run.start()
        _finish(run)
        assert run.returncode == 0
        assert (run.phase_number, run.phase_title) == ("5.5", "Çeviri")
        # Faz olayı dosyadan sayaçtan SONRA okunsa da, kendisinden sonra
        # gelen sayacı sıfırlamaz.
        assert run.percent() == pytest.approx(0.3)
        assert run.progress_unit == "segment"
        assert run.result == {"ok": True, "pdf": "/x/y.pdf"}
        assert run.tail() == ["Segment 3/10", "bir günlük satırı"]

    def test_yeni_faz_eski_sayaci_sifirlar(self, tmp_path):
        run = Run("translate", _script(tmp_path, """
            import json, os, time
            print("Segment 9/10", flush=True)
            time.sleep(0.2)
            with open(os.environ["KITAPCEVIR_EVENT_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "phase", "phase": "6",
                                    "title": "Derleme", "epoch": time.time()}) + "\\n")
        """))
        run.start()
        _finish(run)
        assert run.phase_number == "6"
        assert run.percent() is None       # sayaç yoksa gösterge çizilmez

    def test_sayfa_araliginda_oran_yok(self, tmp_path):
        run = Run("extract", _script(tmp_path, """
            print("[14:28:26] Batch: pages 20-39 (20 pages)...")
        """))
        run.start()
        _finish(run)
        assert run.progress_unit == "page"
        assert (run.progress_from, run.progress_current) == (20, 39)
        assert run.percent() is None       # toplam bilinmiyor: çubuk yok

    def test_hata_olayi_kaydedilir(self, tmp_path):
        run = Run("compile", _script(tmp_path, """
            import json, os
            with open(os.environ["KITAPCEVIR_EVENT_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "error", "message": "Derleme başarısız"}) + "\\n")
            raise SystemExit(2)
        """))
        run.start()
        _finish(run)
        assert run.returncode == 2
        assert run.last_error == "Derleme başarısız"

    def test_basarisiz_kosu_kuyrugu_tutar(self, tmp_path):
        run = Run("extract", _script(tmp_path, """
            import sys
            print("adım 1")
            print("HATA: bir şey kırıldı")
            sys.exit(1)
        """))
        run.start()
        _finish(run)
        assert run.returncode == 1
        assert run.tail()[-1] == "HATA: bir şey kırıldı"
        assert run.result is None

    def test_olay_dosyasi_yoksa_poll_guvenli(self, tmp_path):
        run = Run("extract", _script(tmp_path, "print('olaysız')"))
        run.start()
        _finish(run)
        assert run.returncode == 0 and run.phase_title == ""

    def test_iptal_tum_surec_grubunu_durdurur(self, tmp_path):
        pid_file = tmp_path / "cocuk.pid"
        run = Run("translate", _script(tmp_path, f"""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pid_file)!r}, "w").write(str(child.pid))
            time.sleep(60)
        """))
        run.start()
        deadline = time.time() + 10
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        child_pid = int(pid_file.read_text())

        run.cancel()

        assert run.cancelled and run.returncode is not None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("iptalden sonra alt sürecin çocuğu hâlâ yaşıyor")

    def test_oran_sinirli(self):
        run = Run("translate", ["true"])
        run.progress_current, run.progress_total = 12, 10
        assert run.percent() == 1.0
        run.progress_total = 0
        assert run.percent() is None


class TestRunEventDir:
    """Denetçi B (24 Eylül 2026): koşunun olay dizini sistemin geçici
    dizininde birikiyordu (her Run.__init__ bir kitapcevir-olay-* açıyor,
    hiçbiri silinmiyordu). Artık dizin start()'ta açılır, koşu bitince
    olaylar okunduktan sonra silinir; başlamayan koşu dizin açmaz."""

    @staticmethod
    def _event_dirs(root: Path) -> list[Path]:
        return sorted(root.glob(f"{runner.EVENT_DIR_PREFIX}*"))

    def test_baslamayan_kosu_dizin_acmaz(self, temp_root):
        run = Run("translate", ["true"])
        run.poll()                              # başlamadan poll güvenli
        assert run.event_path is None and run.returncode is None
        assert self._event_dirs(temp_root) == []

    def test_biten_kosunun_dizini_silinir_olaylar_okunur(self, tmp_path, temp_root):
        run = Run("compile", _script(tmp_path, """
            import json, os
            with open(os.environ["KITAPCEVIR_EVENT_LOG"], "a", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "error", "message": "Derleme başarısız"}) + "\\n")
            raise SystemExit(2)
        """))
        run.start()
        assert run.event_path.parent.parent == temp_root
        _finish(run)
        assert run.returncode == 2
        assert run.last_error == "Derleme başarısız"   # silmeden önce okundu
        assert self._event_dirs(temp_root) == []
        run.poll()                              # bitmiş koşuda tekrar poll güvenli
        assert run.last_error == "Derleme başarısız"

    def test_iptal_edilen_kosunun_dizini_silinir(self, tmp_path, temp_root):
        run = Run("translate", _script(tmp_path, "import time; time.sleep(60)"))
        run.start()
        assert len(self._event_dirs(temp_root)) == 1
        run.cancel()
        assert run.cancelled and run.returncode is not None
        assert self._event_dirs(temp_root) == []
