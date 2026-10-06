#!/bin/bash
# Kitapçevir.app iskeletini üretir (packaging/TASARIM.md B.1, B.7, G.1-2, G.1-8):
#   1. Swift kabuğunu derler (swiftc, arm64, macOS MIN_MACOS+),
#   2. Info.plist ve hazirlik.html'i şablondan doldurur,
#   3. simgeyi çizer (simge.swift → .iconset → iconutil → .icns),
#   4. verilirse çalışma zamanını, uygulama kodunu, manifesti ve lisansları
#      Contents/Resources altına koyar,
#   5. paketi ad-hoc mühürler ve mührü doğrular.
#
# Kullanım:
#   packaging/kabuk/derle.sh --cikti <dizin> --surum X.Y.Z --yapi N \
#       [--calisma-zamani <cpython-…-aarch64-apple-darwin-install_only.tar.gz>] \
#       [--uygulama <uygulama kodu .tar.gz>] [--manifest <manifest.json>] \
#       [--lisanslar <dizin>]
#   packaging/kabuk/derle.sh --sabitler     # okunan sabitleri basar (sınama)
#
# Sayıların tek kaynağı: packaging/yonetici/ayarlar.py (KAPANIS_SURESI_SN …);
# renklerin tek kaynağı: .streamlit/config.toml [theme]. Bu betik değer yazmaz,
# oradan okur. MIN_MACOS kabuk tarafının tek kaynağıdır (swiftc hedefi +
# LSMinimumSystemVersion); manifest verilirse onunla karşılaştırılır.
#
# Ad-hoc mühür (codesign -s -) Apple hesabı istemez; Developer ID imzası DEĞİLDİR.
# Yalnız macOS /bin/bash 3.2 ve sistem araçları (swiftc, codesign, iconutil,
# plutil, xattr) kullanılır; Python gerekmez.
set -euo pipefail

MIN_MACOS="14.0"
PAKET_ADI="Kitapçevir.app"
YURUTULEBILIR="Kitapcevir"

KABUK_DIZINI="$(cd "$(dirname "$0")" && pwd)"
PAKETLEME_DIZINI="$(dirname "$KABUK_DIZINI")"
DEPO_KOKU="$(dirname "$PAKETLEME_DIZINI")"
AYARLAR="$PAKETLEME_DIZINI/yonetici/ayarlar.py"
TEMA="$DEPO_KOKU/.streamlit/config.toml"

hata() { printf 'derle.sh: HATA: %s\n' "$*" >&2; exit 1; }
bilgi() { printf 'derle.sh: %s\n' "$*"; }

# ayarlar.py'den tamsayı sabit: "AD = 5_000_000  # yorum" → 5000000
sabit() {
    local d
    d="$(sed -n -E "s/^$1[[:space:]]*=[[:space:]]*([0-9][0-9_]*)[[:space:]]*(#.*)?$/\1/p" "$AYARLAR" | tr -d '_')"
    [ -n "$d" ] || hata "ayarlar.py'de $1 bulunamadı ya da düz tamsayı değil ($AYARLAR)"
    [ "$(printf '%s\n' "$d" | wc -l | tr -d ' ')" = "1" ] || hata "ayarlar.py'de $1 birden çok kez tanımlı"
    printf '%s' "$d"
}

# config.toml [theme] bölümünden "#rrggbb" renk
tema() {
    local d
    d="$(sed -n -E '/^\[theme\]/,/^\[/ s/^'"$1"'[[:space:]]*=[[:space:]]*"(#[0-9a-fA-F]{6})".*/\1/p' "$TEMA" | head -n 1)"
    [ -n "$d" ] || hata "config.toml [theme] içinde $1 bulunamadı ($TEMA)"
    printf '%s' "$d"
}

sabitleri_oku() {
    [ -f "$AYARLAR" ] || hata "ayarlar.py yok: $AYARLAR"
    [ -f "$TEMA" ] || hata "config.toml yok: $TEMA"
    YONETICI_HAZIR_ZAMAN_ASIMI_SN="$(sabit YONETICI_HAZIR_ZAMAN_ASIMI_SN)"
    KAPANIS_SURESI_SN="$(sabit KAPANIS_SURESI_SN)"
    KILIT_DOLU_CIKIS_KODU="$(sabit KILIT_DOLU_CIKIS_KODU)"
    GUNLUK_BOYUT_BAYT="$(sabit GUNLUK_BOYUT_BAYT)"
    GUNLUK_ADET="$(sabit GUNLUK_ADET)"
    ZEMIN="$(tema backgroundColor)"
    IKINCIL_ZEMIN="$(tema secondaryBackgroundColor)"
    METIN="$(tema textColor)"
    VURGU="$(tema primaryColor)"
}

# Şablondaki @AD@ yer tutucularını doldurur; dolmayan kalırsa durur.
doldur() {
    local kaynak="$1" hedef="$2"
    sed -e "s|@SURUM@|$SURUM|g" \
        -e "s|@YAPI@|$YAPI|g" \
        -e "s|@MIN_MACOS@|$MIN_MACOS|g" \
        -e "s|@YONETICI_HAZIR_ZAMAN_ASIMI_SN@|$YONETICI_HAZIR_ZAMAN_ASIMI_SN|g" \
        -e "s|@KAPANIS_SURESI_SN@|$KAPANIS_SURESI_SN|g" \
        -e "s|@KILIT_DOLU_CIKIS_KODU@|$KILIT_DOLU_CIKIS_KODU|g" \
        -e "s|@GUNLUK_BOYUT_BAYT@|$GUNLUK_BOYUT_BAYT|g" \
        -e "s|@GUNLUK_ADET@|$GUNLUK_ADET|g" \
        -e "s|@ZEMIN@|$ZEMIN|g" \
        -e "s|@IKINCIL_ZEMIN@|$IKINCIL_ZEMIN|g" \
        -e "s|@METIN@|$METIN|g" \
        -e "s|@VURGU@|$VURGU|g" \
        "$kaynak" > "$hedef"
    if grep -n -E '@[A-Z_]+@' "$hedef" >&2; then
        hata "doldurulmamış yer tutucu kaldı: $hedef"
    fi
}

# ---- Argümanlar ------------------------------------------------------------------------------
CIKTI=""; SURUM=""; YAPI=""; CALISMA_ZAMANI=""; UYGULAMA=""; MANIFEST=""; LISANSLAR=""
while [ $# -gt 0 ]; do
    case "$1" in
        --cikti) CIKTI="${2:-}"; shift 2 ;;
        --surum) SURUM="${2:-}"; shift 2 ;;
        --yapi) YAPI="${2:-}"; shift 2 ;;
        --calisma-zamani) CALISMA_ZAMANI="${2:-}"; shift 2 ;;
        --uygulama) UYGULAMA="${2:-}"; shift 2 ;;
        --manifest) MANIFEST="${2:-}"; shift 2 ;;
        --lisanslar) LISANSLAR="${2:-}"; shift 2 ;;
        --sabitler)
            sabitleri_oku
            printf 'MIN_MACOS=%s\n' "$MIN_MACOS"
            printf 'YONETICI_HAZIR_ZAMAN_ASIMI_SN=%s\nKAPANIS_SURESI_SN=%s\nKILIT_DOLU_CIKIS_KODU=%s\n' \
                "$YONETICI_HAZIR_ZAMAN_ASIMI_SN" "$KAPANIS_SURESI_SN" "$KILIT_DOLU_CIKIS_KODU"
            printf 'GUNLUK_BOYUT_BAYT=%s\nGUNLUK_ADET=%s\n' "$GUNLUK_BOYUT_BAYT" "$GUNLUK_ADET"
            printf 'ZEMIN=%s\nIKINCIL_ZEMIN=%s\nMETIN=%s\nVURGU=%s\n' "$ZEMIN" "$IKINCIL_ZEMIN" "$METIN" "$VURGU"
            exit 0 ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) hata "bilinmeyen argüman: $1 (--help)" ;;
    esac
done
[ -n "$CIKTI" ] || hata "--cikti gerekli"
printf '%s' "$SURUM" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' || hata "--surum X.Y.Z biçiminde olmalı (verilen: '$SURUM')"
printf '%s' "$YAPI" | grep -qE '^[0-9]+$' || hata "--yapi tamsayı olmalı (verilen: '$YAPI')"

for arac in swiftc codesign iconutil plutil xattr; do
    command -v "$arac" >/dev/null 2>&1 || hata "$arac bulunamadı (Xcode Komut Satırı Araçları gerekli)"
done
[ "$(uname -m)" = "arm64" ] || bilgi "uyarı: bu makine arm64 değil; ikili yine arm64 için derlenir"

sabitleri_oku

# ---- Girdileri denetle -----------------------------------------------------------------------
if [ -n "$CALISMA_ZAMANI" ]; then
    [ -f "$CALISMA_ZAMANI" ] || hata "çalışma zamanı arşivi yok: $CALISMA_ZAMANI"
    case "$(basename "$CALISMA_ZAMANI")" in
        cpython-*-aarch64-apple-darwin-install_only.tar.gz) ;;
        *) hata "çalışma zamanı arşivinin adı python-build-standalone biçiminde olmalı (cpython-<sürüm>-aarch64-apple-darwin-install_only.tar.gz); kabuk klasör adını buradan türetir" ;;
    esac
fi
[ -z "$UYGULAMA" ] || [ -f "$UYGULAMA" ] || hata "uygulama kodu arşivi yok: $UYGULAMA"
[ -z "$LISANSLAR" ] || [ -d "$LISANSLAR" ] || hata "lisans klasörü yok: $LISANSLAR"
if [ -n "$MANIFEST" ]; then
    [ -f "$MANIFEST" ] || hata "manifest yok: $MANIFEST"
    m_sema="$(plutil -extract sema_surumu raw -o - "$MANIFEST" 2>/dev/null || true)"
    [ "$m_sema" = "$(sabit MANIFEST_SEMA_SURUMU)" ] \
        || hata "manifest okunamadı ya da sema_surumu ($m_sema) ayarlar.py MANIFEST_SEMA_SURUMU ile uyuşmuyor: $MANIFEST"
    m_surum="$(plutil -extract uygulama.surum raw -o - "$MANIFEST" 2>/dev/null || true)"
    [ -z "$m_surum" ] || [ "$m_surum" = "$SURUM" ] || hata "manifest sürümü ($m_surum) --surum ($SURUM) ile uyuşmuyor"
    m_macos="$(plutil -extract uygulama.min_macos raw -o - "$MANIFEST" 2>/dev/null || true)"
    [ -z "$m_macos" ] || [ "$m_macos" = "$MIN_MACOS" ] || hata "manifest min_macos ($m_macos) kabuğun MIN_MACOS ($MIN_MACOS) değeriyle uyuşmuyor"
    if [ -n "$CALISMA_ZAMANI" ]; then
        m_sha="$(plutil -extract uygulama.calisma_zamani.sha256 raw -o - "$MANIFEST" 2>/dev/null || true)"
        if [ -n "$m_sha" ]; then
            g_sha="$(shasum -a 256 "$CALISMA_ZAMANI" | awk '{print $1}')"
            [ "$g_sha" = "$m_sha" ] || hata "çalışma zamanı SHA-256 manifestle uyuşmuyor ($g_sha ≠ $m_sha)"
        fi
    fi
fi

# ---- Yapım -----------------------------------------------------------------------------------
mkdir -p "$CIKTI"
CIKTI="$(cd "$CIKTI" && pwd)"
ARA="$(mktemp -d "$CIKTI/.derle.XXXXXX")"
trap 'rm -rf "$ARA"' EXIT
YAPIM="$ARA/$PAKET_ADI"
CONTENTS="$YAPIM/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

bilgi "kabuk derleniyor (swiftc, arm64-apple-macos$MIN_MACOS)"
swiftc -O -swift-version 5 -target "arm64-apple-macos$MIN_MACOS" \
    "$KABUK_DIZINI/Kitapcevir.swift" -o "$CONTENTS/MacOS/$YURUTULEBILIR"

doldur "$KABUK_DIZINI/Info.plist.sablon" "$CONTENTS/Info.plist"
plutil -lint -s "$CONTENTS/Info.plist" || hata "Info.plist geçersiz"
printf 'APPL????' > "$CONTENTS/PkgInfo"
doldur "$KABUK_DIZINI/hazirlik.html" "$CONTENTS/Resources/hazirlik.html"

bilgi "simge çiziliyor"
swiftc -O -swift-version 5 "$KABUK_DIZINI/simge.swift" -o "$ARA/simge"
"$ARA/simge" "$VURGU" "$ZEMIN" "$METIN" "$ARA/Kitapcevir.iconset"
iconutil -c icns -o "$CONTENTS/Resources/Kitapcevir.icns" "$ARA/Kitapcevir.iconset"

if [ -n "$CALISMA_ZAMANI" ]; then
    mkdir -p "$CONTENTS/Resources/calisma_zamani"
    cp "$CALISMA_ZAMANI" "$CONTENTS/Resources/calisma_zamani/"
else
    bilgi "uyarı: --calisma-zamani verilmedi; paket açılışta 'çalışma zamanı bulunamadı' hatası gösterir"
fi
if [ -n "$UYGULAMA" ]; then
    mkdir -p "$CONTENTS/Resources/uygulama"
    cp "$UYGULAMA" "$CONTENTS/Resources/uygulama/kitapcevir-$SURUM.tar.gz"
else
    bilgi "uyarı: --uygulama verilmedi; paket açılışta 'uygulama kodu bulunamadı' hatası gösterir"
fi
[ -z "$MANIFEST" ] || cp "$MANIFEST" "$CONTENTS/Resources/manifest.json"
[ -z "$LISANSLAR" ] || cp -R "$LISANSLAR" "$CONTENTS/Resources/LICENSES"

HEDEF="$CIKTI/$PAKET_ADI"
rm -rf "$HEDEF"
mv "$YAPIM" "$HEDEF"

# Genişletilmiş öznitelikler (karantina, Finder bilgisi) mühürden önce temizlenir;
# yoksa codesign "resource fork, Finder information, or similar detritus" der.
xattr -cr "$HEDEF"
bilgi "ad-hoc mühür (codesign --force --deep --sign -)"
codesign --force --deep --sign - "$HEDEF"
codesign --verify --deep --strict --verbose=2 "$HEDEF"

IKILI_BAYT="$(stat -f %z "$HEDEF/Contents/MacOS/$YURUTULEBILIR")"
PAKET_KIB="$(du -sk "$HEDEF" | awk '{print $1}')"
bilgi "hazır: $HEDEF"
bilgi "sürüm $SURUM (yapı $YAPI), en düşük macOS $MIN_MACOS, ikili $IKILI_BAYT B, paket $PAKET_KIB KiB"
