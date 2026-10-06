// Kitapçevir pencere kabuğu (packaging/TASARIM.md B.2–B.7).
//
// Kabuğun işi yalnız şunlardır (B.3):
//   - Python çalışma zamanını ve uygulama kodunu paketten köke açmak,
//   - yöneticiyi (stdlib Python, packaging/yonetici/yonetici.py) başlatmak,
//   - pencereyi, menüleri ve kapanışı yönetmek.
// Kurulum ve süreç mantığının tamamı yöneticidedir.
//
// Sayılar: yöneticiyle paylaşılan sabitler packaging/yonetici/ayarlar.py'de
// TEK yerde durur. derle.sh onları Info.plist'e ("KC…" anahtarları) yazar,
// kabuk oradan okur. Bu dosyada yalnız kabuğa özgü sabitler bulunur.
//
// Derleme: packaging/kabuk/derle.sh (swiftc, arm64, macOS 14+).

import Cocoa
import WebKit
import CryptoKit
import Darwin

// MARK: - Kabuğa özgü sabitler (yalnız bu dosyada kullanılır)

/// Arşiv okuma ve SHA-256 hesabında parça boyu (bayt).
private let OKUMA_PARCASI_BAYT = 1 << 20
/// Kabuğun yönetici API'sine attığı isteklerin zaman aşımı (sn).
private let API_ZAMAN_ASIMI_SN: TimeInterval = 5
/// Kapanışta süreçlere bakma aralığı (sn).
private let YOKLAMA_ARALIGI_SN: TimeInterval = 0.2
/// Hata uyarısında gösterilen günlük satırı sayısı (TASARIM B.4-6).
private let HATA_GUNLUK_SATIRI = 20
/// Son satırlar okunurken günlük dosyasının sonundan okunan en çok bayt.
private let GUNLUK_KUYRUK_OKUMA_BAYT: UInt64 = 64 * 1024
/// app/ altında tutulan sürüm klasörü sayısı, güncel dahil (TASARIM B.4-5).
private let SAKLANAN_SURUM = 2
/// Pencere boyutları (nokta).
private let PENCERE_BOYUTU = NSSize(width: 1240, height: 860)
private let PENCERE_EN_KUCUK = NSSize(width: 900, height: 620)
/// Geçersiz ilk satırdan sonra yöneticinin kendiliğinden çıkması (ör. kilit dolu,
/// çıkış kodu KCKilitDoluCikisKodu) için tanınan süre (sn); çıkmazsa hata gösterilir.
private let GECERSIZ_SATIR_BEKLEME_SN: TimeInterval = 1.5
/// Kapanışta art arda kapatılacak en çok bağlı sayfa (uyarı/panel) sayısı.
private let SAYFA_KAPATMA_EN_COK = 4
/// Sayfa yakınlaştırma adımı ve sınırları.
private let YAKINLASTIRMA_ADIMI: CGFloat = 0.1
private let YAKINLASTIRMA_EN_AZ: CGFloat = 0.5
private let YAKINLASTIRMA_EN_COK: CGFloat = 3.0

/// Info.plist anahtarları: derle.sh bu değerleri ayarlar.py'den ve
/// .streamlit/config.toml'dan yazar.
private enum BilgiAnahtari {
    static let hazirZamanAsimi = "KCYoneticiHazirZamanAsimiSn"
    static let kapanisSuresi = "KCKapanisSuresiSn"
    static let kilitDoluCikisKodu = "KCKilitDoluCikisKodu"
    static let gunlukBoyut = "KCGunlukBoyutBayt"
    static let gunlukAdet = "KCGunlukAdet"
    static let klasorAdi = "KCKlasorAdi"
    static let zeminRengi = "KCZeminRengi"
}

/// Adım kimlikleri: hazirlik.html bu kimliklerle eşleşir.
private enum AdimKimligi {
    static let calismaZamani = "calisma_zamani"
    static let uygulamaKodu = "uygulama_kodu"
    static let yonetici = "yonetici"
    static let hepsi = [calismaZamani, uygulamaKodu, yonetici]
}

// MARK: - Ayarlar (Info.plist)

private struct AyarHatasi: Error { let ileti: String }

private struct Ayarlar {
    let hazirZamanAsimiSn: TimeInterval
    let kapanisSuresiSn: TimeInterval
    let kilitDoluCikisKodu: Int32
    let gunlukBoyutBayt: Int
    let gunlukAdet: Int
    let klasorAdi: String
    let zeminRengi: NSColor?
    let surum: String
    let kimlik: String

    static func yukle() -> Result<Ayarlar, AyarHatasi> {
        let b = Bundle.main
        func tamsayi(_ k: String) -> Int? {
            if let n = b.object(forInfoDictionaryKey: k) as? NSNumber { return n.intValue }
            return nil
        }
        var eksik: [String] = []
        let hazir = tamsayi(BilgiAnahtari.hazirZamanAsimi)
        let kapanis = tamsayi(BilgiAnahtari.kapanisSuresi)
        let kilit = tamsayi(BilgiAnahtari.kilitDoluCikisKodu)
        let boyut = tamsayi(BilgiAnahtari.gunlukBoyut)
        let adet = tamsayi(BilgiAnahtari.gunlukAdet)
        let klasor = b.object(forInfoDictionaryKey: BilgiAnahtari.klasorAdi) as? String
        let surum = b.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        if hazir == nil { eksik.append(BilgiAnahtari.hazirZamanAsimi) }
        if kapanis == nil { eksik.append(BilgiAnahtari.kapanisSuresi) }
        if kilit == nil { eksik.append(BilgiAnahtari.kilitDoluCikisKodu) }
        if boyut == nil { eksik.append(BilgiAnahtari.gunlukBoyut) }
        if adet == nil { eksik.append(BilgiAnahtari.gunlukAdet) }
        if klasor?.isEmpty ?? true { eksik.append(BilgiAnahtari.klasorAdi) }
        if surum?.isEmpty ?? true { eksik.append("CFBundleShortVersionString") }
        if b.bundleIdentifier == nil { eksik.append("CFBundleIdentifier") }
        guard eksik.isEmpty else {
            return .failure(AyarHatasi(ileti: "Info.plist'te eksik anahtar: " + eksik.joined(separator: ", ")))
        }
        let renk = (b.object(forInfoDictionaryKey: BilgiAnahtari.zeminRengi) as? String)
            .flatMap(Ayarlar.renk(onaltilik:))
        return .success(Ayarlar(
            hazirZamanAsimiSn: TimeInterval(hazir!), kapanisSuresiSn: TimeInterval(kapanis!),
            kilitDoluCikisKodu: Int32(kilit!), gunlukBoyutBayt: boyut!, gunlukAdet: adet!,
            klasorAdi: klasor!.precomposedStringWithCanonicalMapping, zeminRengi: renk,
            surum: surum!, kimlik: b.bundleIdentifier!))
    }

    /// "#rrggbb" → NSColor (sRGB).
    static func renk(onaltilik s: String) -> NSColor? {
        guard s.count == 7, s.hasPrefix("#"), let v = UInt32(s.dropFirst(), radix: 16) else { return nil }
        return NSColor(srgbRed: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                       blue: CGFloat(v & 0xff) / 255, alpha: 1)
    }
}

// MARK: - Günlük (~/Library/Logs/<klasör>/kabuk.log, döndürmeli)

/// RotatingFileHandler karşılığı: dosya `boyut` baytı aşacaksa
/// kabuk.log → kabuk.log.1 → … → kabuk.log.<adet> kaydırılır.
private final class Gunluk {
    let dizin: URL
    let dosya: URL
    private let boyut: Int
    private let adet: Int
    private let kuyruk = DispatchQueue(label: "kitapcevir.gunluk")
    private var tutamak: FileHandle?
    private let bicim: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd HH:mm:ss.SSS"
        return f
    }()

    init(dizin: URL, boyut: Int, adet: Int) {
        self.dizin = dizin
        self.dosya = dizin.appendingPathComponent("kabuk.log")
        self.boyut = boyut
        self.adet = adet
        try? FileManager.default.createDirectory(at: dizin, withIntermediateDirectories: true)
    }

    func yaz(_ ileti: String) {
        let satir = "\(bicim.string(from: Date())) [kabuk] \(ileti)\n"
        kuyruk.async { self.ekle(satir) }
    }

    /// Kuyruktaki bütün yazmalar bitene kadar bekler (kapanışta).
    func bosalt() { kuyruk.sync {} }

    private func ekle(_ satir: String) {
        guard let veri = satir.data(using: .utf8) else { return }
        let fm = FileManager.default
        if let n = (try? fm.attributesOfItem(atPath: dosya.path))?[.size] as? Int,
           boyut > 0, n + veri.count > boyut {
            try? tutamak?.close()
            tutamak = nil
            dondur()
        }
        if tutamak == nil {
            if !fm.fileExists(atPath: dosya.path) { fm.createFile(atPath: dosya.path, contents: nil) }
            tutamak = try? FileHandle(forWritingTo: dosya)
            _ = try? tutamak?.seekToEnd()
        }
        try? tutamak?.write(contentsOf: veri)
    }

    private func dondur() {
        guard adet > 0 else {
            try? FileManager.default.removeItem(at: dosya)
            return
        }
        let yol = dosya.path
        for i in stride(from: adet - 1, through: 1, by: -1) {
            _ = rename("\(yol).\(i)", "\(yol).\(i + 1)")
        }
        _ = rename(yol, "\(yol).1")
    }

    /// Dosyanın son `n` satırı (hata uyarısı için).
    func sonSatirlar(_ n: Int) -> [String] {
        bosalt()
        guard let h = try? FileHandle(forReadingFrom: dosya) else { return [] }
        defer { try? h.close() }
        let son = (try? h.seekToEnd()) ?? 0
        let bas = son > GUNLUK_KUYRUK_OKUMA_BAYT ? son - GUNLUK_KUYRUK_OKUMA_BAYT : 0
        try? h.seek(toOffset: bas)
        let veri = (try? h.readToEnd()) ?? Data()
        let satirlar = String(decoding: veri, as: UTF8.self).split(separator: "\n", omittingEmptySubsequences: true)
        return satirlar.suffix(n).map(String.init)
    }
}

/// Yöneticinin süreç başına anahtarı günlüğe yazılmaz (TASARIM B.6: sırlar günlüğe girmez).
private func anahtariGizle(_ s: String) -> String {
    s.replacingOccurrences(of: #"anahtar=[^&\s"']+"#, with: "anahtar=***", options: .regularExpression)
}

/// Bir borudan gelen baytları satırlara böler; her satır ana iş parçacığında işlenir.
/// Her süreç kendi okuyucusunu alır (yeniden denemede eski sürecin çıktısı karışmaz).
private final class SatirOkuyucu {
    private var tampon = Data()
    private let satirGeldi: (String) -> Void

    init(_ fh: FileHandle, satirGeldi: @escaping (String) -> Void) {
        self.satirGeldi = satirGeldi
        // Döngü bilerek: tutamak EOF'ta işleyiciyi bırakınca okuyucu da serbest kalır.
        fh.readabilityHandler = { h in
            let d = h.availableData
            if d.isEmpty { h.readabilityHandler = nil }
            DispatchQueue.main.async { self.besle(d) }
        }
    }

    private func besle(_ d: Data) {
        tampon.append(d)
        while let i = tampon.firstIndex(of: 0x0A) {
            let satir = String(decoding: tampon[tampon.startIndex..<i], as: UTF8.self)
            tampon.removeSubrange(tampon.startIndex...i)
            satirGeldi(satir)
        }
        if d.isEmpty, !tampon.isEmpty {
            satirGeldi(String(decoding: tampon, as: UTF8.self))
            tampon = Data()
        }
    }
}

extension URL {
    /// Yolun NFC biçimi. Foundation dosya yollarını NFD döndürür; yöneticiye (Python),
    /// günlüğe ve ekrana giden yollar NFC tutulur ("Kitapçevir" iki biçimde de aynı klasördür).
    var nfcYol: String { path.precomposedStringWithCanonicalMapping }
}

// MARK: - Yönetici süreci (posix_spawn)

/// Yöneticiyi başlatan alt süreç. Foundation `Process` argüman ve ortam değerlerini
/// NFD'ye çeviriyor (ölçüldü: "ç" → "c" + U+0327); bu sınıf baytları olduğu gibi
/// (NFC) geçirir. Ayrıca: süreci kendi süreç grubunda başlatır (kapanışta killpg),
/// kabuğun yok saydığı sinyalleri (SIGTERM, SIGPIPE…) varsayılana döndürür ve
/// yalnız 0/1/2'yi devreder (POSIX_SPAWN_CLOEXEC_DEFAULT). Girdi borusunun yazma
/// ucu kabukta kalır: kabuk ölürse yönetici stdin'de EOF görür.
private final class Cocuk {
    let processIdentifier: pid_t
    let girdi: FileHandle
    let cikti: FileHandle
    let hata: FileHandle
    private(set) var isRunning = true
    private(set) var terminationStatus: Int32 = 0
    private(set) var sinyalleBitti = false
    private var kaynak: DispatchSourceProcess?
    private var bitince: ((Cocuk) -> Void)?

    init(_ yurutulebilir: String, _ argumanlar: [String], ortam: [String: String], dizin: String,
         bitince: @escaping (Cocuk) -> Void) throws {
        var g: [Int32] = [-1, -1], c: [Int32] = [-1, -1], h: [Int32] = [-1, -1]
        guard pipe(&g) == 0, pipe(&c) == 0, pipe(&h) == 0 else {
            for fd in g + c + h where fd >= 0 { close(fd) }
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        for fd in [g[1], c[0], h[0]] { _ = fcntl(fd, F_SETFD, FD_CLOEXEC) }

        var fa: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&fa)
        defer { posix_spawn_file_actions_destroy(&fa) }
        posix_spawn_file_actions_adddup2(&fa, g[0], 0)
        posix_spawn_file_actions_adddup2(&fa, c[1], 1)
        posix_spawn_file_actions_adddup2(&fa, h[1], 2)
        posix_spawn_file_actions_addchdir_np(&fa, dizin)
        var at: posix_spawnattr_t?
        posix_spawnattr_init(&at)
        defer { posix_spawnattr_destroy(&at) }
        var maske: sigset_t = 0
        var varsayilan: sigset_t = ~0
        posix_spawnattr_setsigmask(&at, &maske)
        posix_spawnattr_setsigdefault(&at, &varsayilan)
        posix_spawnattr_setpgroup(&at, 0)
        posix_spawnattr_setflags(&at, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_SETSIGMASK
                                            | POSIX_SPAWN_SETSIGDEF | POSIX_SPAWN_CLOEXEC_DEFAULT))
        let argv: [UnsafeMutablePointer<CChar>?] = ([yurutulebilir] + argumanlar).map { strdup($0) } + [nil]
        let envp: [UnsafeMutablePointer<CChar>?] = ortam.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer { argv.forEach { free($0) }; envp.forEach { free($0) } }
        var pid: pid_t = 0
        let r = posix_spawn(&pid, yurutulebilir, &fa, &at, argv, envp)
        close(g[0]); close(c[1]); close(h[1])
        guard r == 0 else {
            close(g[1]); close(c[0]); close(h[0])
            throw POSIXError(POSIXErrorCode(rawValue: r) ?? .EIO)
        }
        processIdentifier = pid
        girdi = FileHandle(fileDescriptor: g[1], closeOnDealloc: true)
        cikti = FileHandle(fileDescriptor: c[0], closeOnDealloc: true)
        hata = FileHandle(fileDescriptor: h[0], closeOnDealloc: true)
        self.bitince = bitince
        // Kaynak süreç bitene dek nesneyi tutar (yeniden denemede başvuru bırakılsa da süreç toplanır).
        let k = DispatchSource.makeProcessSource(identifier: pid, eventMask: .exit, queue: .main)
        k.setEventHandler { self.yokla() }
        kaynak = k
        k.resume()
        DispatchQueue.main.async { self.yokla() }   // kaynak kurulmadan önce biten süreç için
    }

    /// Süreç bittiyse toplar (waitpid) ve bitince'yi bir kez çağırır. Ana iş parçacığında.
    func yokla() {
        guard isRunning else { return }
        var durum: Int32 = 0
        guard waitpid(processIdentifier, &durum, WNOHANG) == processIdentifier else { return }
        isRunning = false
        if durum & 0x7f == 0 {
            terminationStatus = (durum >> 8) & 0xff
        } else {
            sinyalleBitti = true
            terminationStatus = durum & 0x7f
        }
        kaynak?.cancel()
        kaynak = nil
        let b = bitince
        bitince = nil
        b?(self)
    }
}

// MARK: - Hata

private struct KabukHatasi: Error {
    enum Tur { case genel, kilit }
    let adim: String
    let ileti: String
    var tur: Tur = .genel
}

// MARK: - Dosya işleri (hazırlık)

private enum Dosya {
    static let fm = FileManager.default

    static func sha256(_ url: URL) throws -> String {
        let h = try FileHandle(forReadingFrom: url)
        defer { try? h.close() }
        var ozet = SHA256()
        while let d = try h.read(upToCount: OKUMA_PARCASI_BAYT), !d.isEmpty { ozet.update(data: d) }
        return ozet.finalize().map { String(format: "%02x", $0) }.joined()
    }

    static func tamamIsareti(_ dizin: URL) -> String? {
        guard let s = try? String(contentsOf: dizin.appendingPathComponent(".tamam"), encoding: .utf8) else { return nil }
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func tamamYaz(_ dizin: URL, _ sha: String) throws {
        try (sha + "\n").write(to: dizin.appendingPathComponent(".tamam"), atomically: true, encoding: .utf8)
    }

    static func varMi(_ url: URL) -> Bool { fm.fileExists(atPath: url.path) }

    static func sil(_ url: URL) {
        // Yalnız kabuğun kendi geçici klasörleri (.yeni / .eski) ve eski sürümler.
        var dizinMi: ObjCBool = false
        if fm.fileExists(atPath: url.path, isDirectory: &dizinMi) || (try? fm.destinationOfSymbolicLink(atPath: url.path)) != nil {
            try? fm.removeItem(at: url)
        }
    }

    /// `kaynak`'ı `hedef`'in yerine koyar. Hedef varsa önce yana alınır, sonra silinir.
    static func yerineKoy(_ kaynak: URL, _ hedef: URL) throws {
        let eski = hedef.deletingLastPathComponent().appendingPathComponent(hedef.lastPathComponent + ".eski")
        sil(eski)
        if varMi(hedef) {
            guard rename(hedef.path, eski.path) == 0 else {
                throw KabukHatasi(adim: "", ileti: "Eski klasör yana alınamadı: \(hedef.nfcYol) (\(String(cString: strerror(errno))))")
            }
        }
        guard rename(kaynak.path, hedef.path) == 0 else {
            throw KabukHatasi(adim: "", ileti: "Klasör yerine konamadı: \(hedef.nfcYol) (\(String(cString: strerror(errno))))")
        }
        sil(eski)
    }

    /// com.apple.quarantine özniteliğini ağaçtaki her öğeden siler (TASARIM B.7).
    /// Dönüş: özniteliği silinen öğe sayısı.
    @discardableResult
    static func karantinaSil(_ kok: URL) -> Int {
        var silinen = 0
        func sil(_ yol: String) {
            if removexattr(yol, "com.apple.quarantine", XATTR_NOFOLLOW) == 0 { silinen += 1 }
        }
        sil(kok.path)
        if let e = fm.enumerator(atPath: kok.path) {
            for case let goreli as String in e { sil(kok.path + "/" + goreli) }
        }
        return silinen
    }

    /// Dizindeki karantinalı öğe sayısı (yalnız doğrulama/günlük için).
    static func karantinaliSay(_ kok: URL) -> Int {
        var n = 0
        func bak(_ yol: String) {
            if getxattr(yol, "com.apple.quarantine", nil, 0, 0, XATTR_NOFOLLOW) >= 0 { n += 1 }
        }
        bak(kok.path)
        if let e = fm.enumerator(atPath: kok.path) {
            for case let goreli as String in e { bak(kok.path + "/" + goreli) }
        }
        return n
    }
}

// MARK: - Süreç tablosu (libproc) — kapanışta torun süreçleri bulmak için

private struct SurecBilgisi {
    let pid: pid_t
    let ppid: pid_t
    let pgid: pid_t
    let baslangic: UInt64   // µs; PID yeniden kullanımına karşı kimlik
}

private enum SurecTablosu {
    static func hepsi() -> [pid_t: SurecBilgisi] {
        let n = proc_listallpids(nil, 0)
        guard n > 0 else { return [:] }
        var pidler = [pid_t](repeating: 0, count: Int(n) + 128)
        let m = pidler.withUnsafeMutableBytes { proc_listallpids($0.baseAddress, Int32($0.count)) }
        var sonuc: [pid_t: SurecBilgisi] = [:]
        for pid in pidler.prefix(Int(max(m, 0))) where pid > 0 {
            if let b = bilgi(pid) { sonuc[pid] = b }
        }
        return sonuc
    }

    static func bilgi(_ pid: pid_t) -> SurecBilgisi? {
        var i = proc_bsdinfo()
        let boyut = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &i, boyut) == boyut else { return nil }
        return SurecBilgisi(pid: pid, ppid: pid_t(i.pbi_ppid), pgid: pid_t(i.pbi_pgid),
                            baslangic: i.pbi_start_tvsec &* 1_000_000 &+ i.pbi_start_tvusec)
    }

    /// `kok`'ün bütün torunları (ppid zinciriyle).
    static func torunlar(_ kok: pid_t, _ tablo: [pid_t: SurecBilgisi]) -> [SurecBilgisi] {
        var cocuklar: [pid_t: [pid_t]] = [:]
        for b in tablo.values { cocuklar[b.ppid, default: []].append(b.pid) }
        var sonuc: [SurecBilgisi] = []
        var kuyruk = [kok]
        var gorulen: Set<pid_t> = [kok]
        while let p = kuyruk.popLast() {
            for c in cocuklar[p] ?? [] where gorulen.insert(c).inserted {
                if let b = tablo[c] { sonuc.append(b); kuyruk.append(c) }
            }
        }
        return sonuc
    }

    /// Sürecin yürütülebilir yolu + argv baytları (KERN_PROCARGS2).
    static func komutSatiri(_ pid: pid_t) -> Data? {
        var enCok: Int32 = 0
        var boy = MemoryLayout<Int32>.size
        var mib: [Int32] = [CTL_KERN, KERN_ARGMAX]
        guard sysctl(&mib, 2, &enCok, &boy, nil, 0) == 0, enCok > 0 else { return nil }
        var tampon = [UInt8](repeating: 0, count: Int(enCok))
        var uzunluk = Int(enCok)
        mib = [CTL_KERN, KERN_PROCARGS2, pid]
        guard sysctl(&mib, 3, &tampon, &uzunluk, nil, 0) == 0 else { return nil }
        return Data(tampon.prefix(uzunluk))
    }

    /// Sürecin dinlediği TCP kapıları (LISTEN). İlk satırdaki adresin gerçekten bu
    /// yöneticiye ait olduğunu doğrulamak için: kilit doluyken öteki örneğin adresi
    /// basılırsa pencere ona yönelmez ve kapanış isteği öteki örneğe gitmez.
    static func dinlenenKapilar(_ pid: pid_t) -> Set<Int> {
        let boy = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, nil, 0)
        guard boy > 0 else { return [] }
        let adim = MemoryLayout<proc_fdinfo>.stride
        var fdler = [proc_fdinfo](repeating: proc_fdinfo(), count: Int(boy) / adim + 16)
        let gercek = fdler.withUnsafeMutableBytes { proc_pidinfo(pid, PROC_PIDLISTFDS, 0, $0.baseAddress, Int32($0.count)) }
        guard gercek > 0 else { return [] }
        var kapilar: Set<Int> = []
        for fd in fdler.prefix(Int(gercek) / adim) where fd.proc_fdtype == UInt32(PROX_FDTYPE_SOCKET) {
            var si = socket_fdinfo()
            let n = proc_pidfdinfo(pid, fd.proc_fd, PROC_PIDFDSOCKETINFO, &si, Int32(MemoryLayout<socket_fdinfo>.size))
            guard n == Int32(MemoryLayout<socket_fdinfo>.size), si.psi.soi_kind == Int32(SOCKINFO_TCP) else { continue }
            let tcp = si.psi.soi_proto.pri_tcp
            guard tcp.tcpsi_state == Int32(TSI_S_LISTEN) else { continue }
            kapilar.insert(Int(UInt16(bigEndian: UInt16(truncatingIfNeeded: tcp.tcpsi_ini.insi_lport))))
        }
        return kapilar
    }

    /// Komut satırı verilen yollardan birini içeriyor mu (NFC ve NFD biçimleri denenir).
    static func yolIceriyor(_ pid: pid_t, _ yollar: [Data]) -> Bool {
        guard let k = komutSatiri(pid) else { return false }
        return yollar.contains { !$0.isEmpty && k.range(of: $0) != nil }
    }
}

// MARK: - Uygulama

private enum Evre { case baslangic, hazirlaniyor, yoneticiBekleniyor, hazir, hata, kapaniyor }

private struct YoneticiDurumu {
    let calisanIs: Int
    let kurulumSuruyor: Bool
}

final class Uygulama: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate,
                      WKUIDelegate, WKDownloadDelegate, NSMenuItemValidation {
    private var ayarlar: Ayarlar!
    private var gunluk: Gunluk!
    private var kok: URL!
    private var pencere: NSWindow!
    private var web: WKWebView!

    private var evre: Evre = .baslangic
    private var adimlar: [String: [String: Any]] = [:]
    private var hataBilgisi: [String: Any]?
    private var tasinmamis = false

    private let isKuyrugu = DispatchQueue(label: "kitapcevir.hazirlik")
    private let altSurecKilidi = NSLock()
    private var aktifAltSurec: Process?
    private var iptalIstendi = false

    private var yonetici: Cocuk?
    private var yoneticiAdresi: URL?
    private var anahtar: String?
    private var kapanisSuresi: TimeInterval = 0
    private var zamanAsimi: DispatchWorkItem?
    private var yoneticiBaslangici = Date()

    private var kapanisTamam = false
    /// Uygulamadan çıkış başladı (yeniden denemedeki durdurmadan ayrı).
    private var cikisSuruyor = false
    private var onayUyarisi: NSAlert?
    private var sinyalKaynaklari: [DispatchSourceSignal] = []
    private lazy var oturum: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.connectionProxyDictionary = [:]
        c.timeoutIntervalForRequest = API_ZAMAN_ASIMI_SN
        c.timeoutIntervalForResource = API_ZAMAN_ASIMI_SN
        return URLSession(configuration: c)
    }()

    private var kaynaklar: URL { Bundle.main.resourceURL! }
    private var hazirlikSayfasi: URL { kaynaklar.appendingPathComponent("hazirlik.html") }

    // MARK: Açılış

    func applicationWillFinishLaunching(_ n: Notification) {
        NSApp.mainMenu = menuKur()
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        signal(SIGPIPE, SIG_IGN)
        switch Ayarlar.yukle() {
        case .success(let a): ayarlar = a
        case .failure(let h):
            let u = NSAlert()
            u.alertStyle = .critical
            u.messageText = "Kitapçevir açılamadı"
            u.informativeText = "Uygulama paketi bozuk: \(h.ileti). Uygulamayı yeniden indirip kur."
            u.runModal()
            exit(1)
        }
        kapanisSuresi = ayarlar.kapanisSuresiSn

        // 1. Tek örnek (B.4-1): aynı kimlikli başka örnek varsa o öne gelir, bu kapanır.
        if let diger = digerOrnek() {
            diger.activate(options: [.activateAllWindows])
            exit(0)
        }

        // 2. Kök ve günlük (B.4-2, B.6).
        let fm = FileManager.default
        let kutuphane = fm.urls(for: .libraryDirectory, in: .userDomainMask).first!
        gunluk = Gunluk(dizin: kutuphane.appendingPathComponent("Logs").appendingPathComponent(ayarlar.klasorAdi),
                        boyut: ayarlar.gunlukBoyutBayt, adet: ayarlar.gunlukAdet)
        if let sinama = ProcessInfo.processInfo.environment["KITAPCEVIR_KOK"], !sinama.isEmpty {
            kok = URL(fileURLWithPath: sinama.precomposedStringWithCanonicalMapping, isDirectory: true)
        } else {
            kok = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
                .appendingPathComponent(ayarlar.klasorAdi, isDirectory: true)
        }
        let paket = Bundle.main.bundleURL.nfcYol
        tasinmamis = paket.hasPrefix("/Volumes/") || paket.contains("/AppTranslocation/")
        gunluk.yaz("açılış: sürüm \(ayarlar.surum), paket \(paket), kök \(kok.nfcYol)"
                   + (ProcessInfo.processInfo.environment["KITAPCEVIR_KOK"] != nil ? " (KITAPCEVIR_KOK sınama kökü)" : "")
                   + (tasinmamis ? ", uygulama taşınmamış" : ""))

        sinyalleriKur()

        // 3. Pencere + hazırlık sayfası (B.4-3).
        // NSApp.activate() çağrılmaz: Finder/`open` açılışı uygulamayı zaten öne getirir;
        // komut satırından (sınama) açılışta kullanıcının odağı çalınmaz.
        pencereKur()
        hazirlikBaslat()
    }

    private func digerOrnek() -> NSRunningApplication? {
        guard let kimlik = Bundle.main.bundleIdentifier else { return nil }
        let ben = ProcessInfo.processInfo.processIdentifier
        return NSRunningApplication.runningApplications(withBundleIdentifier: kimlik)
            .first { $0.processIdentifier != ben && !$0.isTerminated }
    }

    private func sinyalleriKur() {
        for s in [SIGTERM, SIGINT, SIGHUP] {
            signal(s, SIG_IGN)
            let k = DispatchSource.makeSignalSource(signal: s, queue: .main)
            k.setEventHandler { [weak self] in self?.sinyalGeldi(s) }
            k.resume()
            sinyalKaynaklari.append(k)
        }
    }

    private func pencereKur() {
        let c = WKWebViewConfiguration()
        // Kalıcı web verisi yok: ~/Library/WebKit, Caches, HTTPStorages'a yazılmaz.
        c.websiteDataStore = .nonPersistent()
        c.applicationNameForUserAgent = "Kitapcevir/\(ayarlar.surum)"
        web = WKWebView(frame: NSRect(origin: .zero, size: PENCERE_BOYUTU), configuration: c)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsMagnification = true
        web.allowsBackForwardNavigationGestures = false

        pencere = NSWindow(contentRect: NSRect(origin: .zero, size: PENCERE_BOYUTU),
                           styleMask: [.titled, .closable, .miniaturizable, .resizable],
                           backing: .buffered, defer: false)
        pencere.title = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Kitapçevir"
        pencere.minSize = PENCERE_EN_KUCUK
        pencere.appearance = NSAppearance(named: .darkAqua)
        if let r = ayarlar.zeminRengi { pencere.backgroundColor = r }
        pencere.isRestorable = false
        pencere.isReleasedWhenClosed = false
        pencere.delegate = self
        pencere.contentView = web
        pencere.center()
        pencere.makeKeyAndOrderFront(nil)
    }

    // MARK: Hazırlık (B.4-3…5)

    private func hazirlikBaslat() {
        evre = .hazirlaniyor
        hataBilgisi = nil
        iptalIstendi = false
        yoneticiAdresi = nil
        anahtar = nil
        for k in AdimKimligi.hepsi { adimlar[k] = ["kimlik": k, "durum": "bekliyor"] }
        hazirlikSayfasiniYukle()
        isKuyrugu.async { [weak self] in
            guard let self else { return }
            do {
                let python = try self.calismaZamaniHazirla()
                let betik = try self.uygulamaKoduHazirla()
                DispatchQueue.main.async { self.yoneticiyiBaslat(python: python, betik: betik) }
            } catch let h as KabukHatasi {
                DispatchQueue.main.async { self.hataGoster(h) }
            } catch {
                DispatchQueue.main.async {
                    self.hataGoster(KabukHatasi(adim: AdimKimligi.calismaZamani, ileti: error.localizedDescription))
                }
            }
        }
    }

    private func hazirlikSayfasiniYukle() {
        if web.url?.standardizedFileURL != hazirlikSayfasi.standardizedFileURL {
            web.loadFileURL(hazirlikSayfasi, allowingReadAccessTo: kaynaklar)
        } else {
            sayfaDurumunuGonder()
        }
    }

    /// Arka plandan adım durumu (ana iş parçacığına aktarılır).
    private func adim(_ k: String, _ durum: String, not: String? = nil, basladi: Bool = false, sure: TimeInterval? = nil) {
        let simdi = Date().timeIntervalSince1970 * 1000
        DispatchQueue.main.async {
            var a = self.adimlar[k] ?? ["kimlik": k]
            a["durum"] = durum
            a["not"] = not ?? NSNull()
            if basladi { a["baslangic_ms"] = simdi }
            if let sure { a["sure_sn"] = sure } else if durum != "tamam" { a["sure_sn"] = NSNull() }
            self.adimlar[k] = a
            self.sayfaDurumunuGonder()
        }
    }

    private func sayfaDurumunuGonder() {
        guard web.url?.isFileURL == true else { return }
        let durum: [String: Any] = [
            "surum": ayarlar.surum,
            "tasinmamis": tasinmamis,
            "gunluk": gunluk.dosya.nfcYol,
            "adimlar": AdimKimligi.hepsi.compactMap { adimlar[$0] },
            "hata": hataBilgisi ?? NSNull(),
            "kapaniyor": evre == .kapaniyor,
        ]
        guard let veri = try? JSONSerialization.data(withJSONObject: durum),
              let json = String(data: veri, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.kabukDurumu && window.kabukDurumu(\(json));", completionHandler: nil)
    }

    private func calismaZamaniHazirla() throws -> URL {
        let k = AdimKimligi.calismaZamani
        adim(k, "suruyor", not: "denetleniyor", basladi: true)
        let dizin = kaynaklar.appendingPathComponent("calisma_zamani")
        let arsivler = ((try? FileManager.default.contentsOfDirectory(at: dizin, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent.hasSuffix(".tar.gz") }
        guard arsivler.count == 1, let arsiv = arsivler.first else {
            throw KabukHatasi(adim: k, ileti: "Uygulama paketi eksik: Python çalışma zamanı arşivi bulunamadı (\(dizin.nfcYol)).")
        }
        guard let ad = Uygulama.calismaZamaniAdi(arsiv.lastPathComponent) else {
            throw KabukHatasi(adim: k, ileti: "Beklenmeyen çalışma zamanı arşivi adı: \(arsiv.lastPathComponent)")
        }
        let sha = try Dosya.sha256(arsiv)
        if let beklenen = manifesttekiCalismaZamaniSHA(), beklenen != sha {
            throw KabukHatasi(adim: k, ileti: "Uygulama paketi bozuk: çalışma zamanı arşivinin SHA-256 özeti manifestle uyuşmuyor. Uygulamayı yeniden indir.")
        }
        let ust = kok.appendingPathComponent("calisma_zamani", isDirectory: true)
        let hedef = ust.appendingPathComponent(ad, isDirectory: true)
        let python = hedef.appendingPathComponent("bin/python3")
        if Dosya.tamamIsareti(hedef) == sha, FileManager.default.isExecutableFile(atPath: python.path) {
            gunluk.yaz("çalışma zamanı hazır: \(hedef.nfcYol)")
            adim(k, "tamam", not: "zaten_hazir")
            return python
        }
        adim(k, "suruyor", not: "aciliyor", basladi: true)
        let t0 = Date()
        let yeni = ust.appendingPathComponent(ad + ".yeni", isDirectory: true)
        Dosya.sil(yeni)
        try klasorKur(yeni, adim: k)
        let acilanSha = try arsivAc(arsiv, hedef: yeni, adim: k)
        guard acilanSha == sha else {
            throw KabukHatasi(adim: k, ileti: "Çalışma zamanı arşivi açılırken değişti (SHA-256 uyuşmadı).")
        }
        let ic = yeni.appendingPathComponent("python", isDirectory: true)
        guard FileManager.default.isExecutableFile(atPath: ic.appendingPathComponent("bin/python3").path) else {
            throw KabukHatasi(adim: k, ileti: "Beklenmeyen çalışma zamanı arşivi düzeni: python/bin/python3 yok.")
        }
        let silinen = Dosya.karantinaSil(ic)
        try pythonSina(ic.appendingPathComponent("bin/python3"), adim: k)
        try Dosya.tamamYaz(ic, sha)
        do { try Dosya.yerineKoy(ic, hedef) } catch let h as KabukHatasi {
            throw KabukHatasi(adim: k, ileti: h.ileti)
        }
        Dosya.sil(yeni)
        let sure = Date().timeIntervalSince(t0)
        gunluk.yaz(String(format: "çalışma zamanı açıldı: %@ (%.2f sn, karantinası silinen öğe: %d)", hedef.nfcYol, sure, silinen))
        adim(k, "tamam", not: "acildi", sure: sure)
        return python
    }

    /// "cpython-3.12.14+20260924-aarch64-apple-darwin-install_only.tar.gz" → "python-3.12.14+20260924"
    static func calismaZamaniAdi(_ dosya: String) -> String? {
        let on = "cpython-", son = "-aarch64-apple-darwin-install_only.tar.gz"
        guard dosya.hasPrefix(on), dosya.hasSuffix(son) else { return nil }
        let surum = dosya.dropFirst(on.count).dropLast(son.count)
        guard !surum.isEmpty, !surum.contains("/"), !surum.contains("..") else { return nil }
        return "python-" + surum
    }

    /// Resources/manifest.json › uygulama.calisma_zamani.sha256 (varsa).
    private func manifesttekiCalismaZamaniSHA() -> String? {
        let url = kaynaklar.appendingPathComponent("manifest.json")
        guard let veri = try? Data(contentsOf: url),
              let kok = try? JSONSerialization.jsonObject(with: veri) as? [String: Any],
              let uyg = kok["uygulama"] as? [String: Any],
              let cz = uyg["calisma_zamani"] as? [String: Any],
              let sha = cz["sha256"] as? String else { return nil }
        return sha.lowercased()
    }

    private func uygulamaKoduHazirla() throws -> URL {
        let k = AdimKimligi.uygulamaKodu
        adim(k, "suruyor", not: "denetleniyor", basladi: true)
        let surum = ayarlar.surum
        let arsiv = kaynaklar.appendingPathComponent("uygulama/kitapcevir-\(surum).tar.gz")
        guard Dosya.varMi(arsiv) else {
            throw KabukHatasi(adim: k, ileti: "Uygulama paketi eksik: uygulama kodu arşivi bulunamadı (\(arsiv.nfcYol)).")
        }
        let sha = try Dosya.sha256(arsiv)
        let appDizini = kok.appendingPathComponent("app", isDirectory: true)
        let hedef = appDizini.appendingPathComponent(surum, isDirectory: true)
        var not = "zaten_hazir"
        var sure: TimeInterval?
        if Dosya.tamamIsareti(hedef) != sha {
            adim(k, "suruyor", not: "aciliyor", basladi: true)
            let t0 = Date()
            let yeni = appDizini.appendingPathComponent(surum + ".yeni", isDirectory: true)
            Dosya.sil(yeni)
            try klasorKur(yeni, adim: k)
            let acilanSha = try arsivAc(arsiv, hedef: yeni, adim: k)
            guard acilanSha == sha else {
                throw KabukHatasi(adim: k, ileti: "Uygulama kodu arşivi açılırken değişti (SHA-256 uyuşmadı).")
            }
            let ic = try icerikKoku(yeni, adim: k)
            let silinen = Dosya.karantinaSil(ic)
            try Dosya.tamamYaz(ic, sha)
            do { try Dosya.yerineKoy(ic, hedef) } catch let h as KabukHatasi {
                throw KabukHatasi(adim: k, ileti: h.ileti)
            }
            if ic != yeni { Dosya.sil(yeni) }
            sure = Date().timeIntervalSince(t0)
            not = "acildi"
            gunluk.yaz(String(format: "uygulama kodu açıldı: %@ (%.2f sn, karantinası silinen öğe: %d)", hedef.nfcYol, sure!, silinen))
        }
        try guncelBaginiKur(appDizini, surum)
        adim(k, "tamam", not: not, sure: sure)

        let betik = appDizini.appendingPathComponent("guncel/packaging/yonetici/yonetici.py")
        guard Dosya.varMi(betik) else {
            throw KabukHatasi(adim: AdimKimligi.yonetici,
                              ileti: "Kurulum yöneticisi bu sürümde bulunamadı (packaging/yonetici/yonetici.py). Uygulama paketi eksik ya da bu yapı henüz tamamlanmamış.")
        }
        return betik
    }

    /// Arşiv kökte ya da tek bir üst klasörde olabilir; içinde packaging/ bulunan kök döner.
    private func icerikKoku(_ yeni: URL, adim k: String) throws -> URL {
        if Dosya.varMi(yeni.appendingPathComponent("packaging")) { return yeni }
        let ogeler = ((try? FileManager.default.contentsOfDirectory(at: yeni, includingPropertiesForKeys: nil)) ?? [])
            .filter { $0.lastPathComponent != ".DS_Store" }
        if ogeler.count == 1, Dosya.varMi(ogeler[0].appendingPathComponent("packaging")) { return ogeler[0] }
        throw KabukHatasi(adim: k, ileti: "Beklenmeyen uygulama kodu arşivi düzeni: packaging/ klasörü yok.")
    }

    /// app/guncel → <sürüm> (göreli sembolik bağ; symlink + rename ile atomik).
    private func guncelBaginiKur(_ appDizini: URL, _ surum: String) throws {
        let fm = FileManager.default
        let bag = appDizini.appendingPathComponent("guncel")
        if (try? fm.destinationOfSymbolicLink(atPath: bag.path)) == surum { return }
        var dizinMi: ObjCBool = false
        if (try? fm.destinationOfSymbolicLink(atPath: bag.path)) == nil,
           fm.fileExists(atPath: bag.path, isDirectory: &dizinMi), dizinMi.boolValue {
            // Beklenmeyen gerçek klasör: silinmez, yana alınır.
            let yan = appDizini.appendingPathComponent("guncel.eski-\(Int(Date().timeIntervalSince1970))")
            try fm.moveItem(at: bag, to: yan)
            gunluk.yaz("uyarı: app/guncel sembolik bağ değildi, yana alındı: \(yan.nfcYol)")
        }
        let gecici = appDizini.appendingPathComponent("guncel.yeni")
        Dosya.sil(gecici)
        try fm.createSymbolicLink(atPath: gecici.path, withDestinationPath: surum)
        guard rename(gecici.path, bag.path) == 0 else {
            throw KabukHatasi(adim: AdimKimligi.uygulamaKodu,
                              ileti: "app/guncel bağı kurulamadı (\(String(cString: strerror(errno)))).")
        }
        gunluk.yaz("app/guncel → \(surum)")
    }

    private func klasorKur(_ url: URL, adim k: String) throws {
        do {
            try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        } catch {
            throw KabukHatasi(adim: k, ileti: "Klasör oluşturulamadı: \(url.nfcYol) (\(error.localizedDescription))")
        }
    }

    /// Arşivi `/usr/bin/tar`'a BORUDAN verir: tar dosya yolunu görmediği için
    /// arşivin karantina özniteliği açılan dosyalara taşınmaz (ölçüldü; B.7).
    /// Aynı geçişte SHA-256 hesaplanır. Dönüş: arşivin özeti.
    private func arsivAc(_ arsiv: URL, hedef: URL, adim k: String) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/tar")
        p.arguments = ["-xzf", "-", "-C", hedef.path]
        let giris = Pipe(), hata = Pipe()
        p.standardInput = giris
        p.standardOutput = FileHandle.nullDevice
        p.standardError = hata
        var hataMetni = Data()
        let hataKilidi = NSLock()
        hata.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            if d.isEmpty { h.readabilityHandler = nil } else { hataKilidi.lock(); hataMetni.append(d); hataKilidi.unlock() }
        }
        try altSurecBaslat(p, adim: k)
        defer { altSurecBitti() }
        var ozet = SHA256()
        let okuyucu = try FileHandle(forReadingFrom: arsiv)
        defer { try? okuyucu.close() }
        var yazmaHatasi: Error?
        while let d = try okuyucu.read(upToCount: OKUMA_PARCASI_BAYT), !d.isEmpty {
            ozet.update(data: d)
            do { try giris.fileHandleForWriting.write(contentsOf: d) } catch { yazmaHatasi = error; break }
        }
        try? giris.fileHandleForWriting.close()
        p.waitUntilExit()
        hata.fileHandleForReading.readabilityHandler = nil
        hataKilidi.lock()
        let hataYazisi = String(decoding: hataMetni, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        hataKilidi.unlock()
        if iptalIstendi { throw KabukHatasi(adim: k, ileti: "İptal edildi.") }
        guard p.terminationStatus == 0, yazmaHatasi == nil else {
            gunluk.yaz("tar hatası (\(arsiv.lastPathComponent)): çıkış \(p.terminationStatus); \(hataYazisi)")
            throw KabukHatasi(adim: k, ileti: "Arşiv açılamadı: \(arsiv.lastPathComponent) (tar çıkış kodu \(p.terminationStatus)). Diskte yer olduğundan emin ol.")
        }
        return ozet.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private func pythonSina(_ python: URL, adim k: String) throws {
        let p = Process()
        p.executableURL = python
        p.arguments = ["-I", "-c", "import ssl, sqlite3"]
        p.standardOutput = FileHandle.nullDevice
        let hata = Pipe()
        p.standardError = hata
        try altSurecBaslat(p, adim: k)
        let cikti = hata.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        altSurecBitti()
        guard p.terminationStatus == 0 else {
            gunluk.yaz("python sınaması başarısız: \(String(decoding: cikti, as: UTF8.self))")
            throw KabukHatasi(adim: k, ileti: "Python çalışma zamanı sınamadan geçmedi (import ssl, sqlite3; çıkış kodu \(p.terminationStatus)).")
        }
    }

    private func altSurecBaslat(_ p: Process, adim k: String) throws {
        altSurecKilidi.lock()
        defer { altSurecKilidi.unlock() }
        if iptalIstendi { throw KabukHatasi(adim: k, ileti: "İptal edildi.") }
        do { try p.run() } catch {
            throw KabukHatasi(adim: k, ileti: "\(p.executableURL?.path ?? "?") başlatılamadı: \(error.localizedDescription)")
        }
        aktifAltSurec = p
    }

    private func altSurecBitti() {
        altSurecKilidi.lock()
        aktifAltSurec = nil
        altSurecKilidi.unlock()
    }

    /// Kapanışta hazırlık alt sürecini (tar, python sınaması) durdurur.
    private func hazirligiIptalEt() {
        altSurecKilidi.lock()
        iptalIstendi = true
        if let p = aktifAltSurec, p.isRunning { p.terminate() }
        altSurecKilidi.unlock()
    }

    // MARK: Yönetici (B.4-6, B.5)

    private func yoneticiyiBaslat(python: URL, betik: URL) {
        guard evre == .hazirlaniyor else { return }
        let k = AdimKimligi.yonetici
        evre = .yoneticiBekleniyor
        yoneticiBaslangici = Date()
        adim(k, "suruyor", not: "baslatiliyor", basladi: true)

        var ortam = ProcessInfo.processInfo.environment
        ortam["KITAPCEVIR_GUNLUK_DIZINI"] = gunluk.dizin.nfcYol
        ortam["KITAPCEVIR_PAKET_YOLU"] = Bundle.main.bundleURL.nfcYol
        let p: Cocuk
        do {
            // --ebeveyn-pid: kabuk ölürse (kill -9) yönetici kendini kapatır (K4 yonetici.py).
            let argumanlar = ["-I", "-X", "utf8", betik.nfcYol, "--kok", kok.nfcYol, "--port", "0",
                              "--ebeveyn-pid", String(ProcessInfo.processInfo.processIdentifier)]
            p = try Cocuk(python.nfcYol, argumanlar,
                          ortam: ortam, dizin: kok.appendingPathComponent("app/guncel", isDirectory: true).nfcYol) { [weak self] s in
                self?.yoneticiBitti(s)
            }
        } catch {
            hataGoster(KabukHatasi(adim: k, ileti: "Kurulum yöneticisi başlatılamadı: \(error.localizedDescription)"))
            return
        }
        var ilk = true
        _ = SatirOkuyucu(p.cikti) { [weak self] satir in
            guard let self else { return }
            if ilk, p === self.yonetici {
                ilk = false
                self.ilkSatir(satir, surec: p)
            } else {
                self.gunluk.yaz("yönetici: " + anahtariGizle(satir))
            }
        }
        _ = SatirOkuyucu(p.hata) { [weak self] satir in
            self?.gunluk.yaz("yönetici stderr: " + anahtariGizle(satir))
        }
        yonetici = p
        gunluk.yaz("yönetici başlatıldı: pid \(p.processIdentifier), \(betik.nfcYol)")
        let is_ = DispatchWorkItem { [weak self] in self?.yoneticiZamanAsimi(p) }
        zamanAsimi = is_
        DispatchQueue.main.asyncAfter(deadline: .now() + ayarlar.hazirZamanAsimiSn, execute: is_)
    }

    /// İlk satır: {"url": "http://127.0.0.1:<P>/?anahtar=<tok>", "kapanis_suresi_sn": <ayarlar.KAPANIS_SURESI_SN>}
    private func ilkSatir(_ satir: String, surec: Cocuk) {
        gunluk.yaz("yönetici ilk satır: " + anahtariGizle(satir))
        guard evre == .yoneticiBekleniyor else { return }
        guard surec.isRunning else { return }   // çıkış kodu terminationHandler'da işlenir (ör. kilit dolu)
        if let veri = satir.data(using: .utf8),
           let nesne = try? JSONSerialization.jsonObject(with: veri) as? [String: Any],
           nesne["zaten_calisiyor"] as? Bool == true {
            // Kilit başka örnekte (B.6): adres yüklenmez; çıkış kodu yoneticiBitti'de işlenir.
            ilkSatirReddedildi("kilit başka bir örnekte (zaten_calisiyor)", surec: surec)
            return
        }
        guard let veri = satir.data(using: .utf8),
              let nesne = try? JSONSerialization.jsonObject(with: veri) as? [String: Any],
              let metin = nesne["url"] as? String, let url = URL(string: metin),
              url.scheme == "http", let host = url.host, ["127.0.0.1", "localhost"].contains(host),
              let kapi = url.port
        else {
            ilkSatirReddedildi("yerel adres içeren JSON bekleniyordu", surec: surec)
            return
        }
        guard SurecTablosu.dinlenenKapilar(surec.processIdentifier).contains(kapi) else {
            ilkSatirReddedildi("adresteki kapı (\(kapi)) bu yöneticinin dinlediği bir kapı değil", surec: surec)
            return
        }
        zamanAsimi?.cancel()
        if let s = nesne["kapanis_suresi_sn"] as? NSNumber, s.doubleValue > 0 { kapanisSuresi = s.doubleValue }
        anahtar = URLComponents(url: url, resolvingAgainstBaseURL: false)?
            .queryItems?.first { $0.name == "anahtar" }?.value
        yoneticiAdresi = url
        evre = .hazir
        let sure = Date().timeIntervalSince(yoneticiBaslangici)
        adim(AdimKimligi.yonetici, "tamam", sure: sure)
        gunluk.yaz(String(format: "yönetici hazır (%.2f sn): %@", sure, anahtariGizle(url.absoluteString)))
        web.load(URLRequest(url: url))
        eskiSurumleriTemizle()
    }

    /// Geçersiz ilk satır: yönetici kısa sürede çıkarsa (kilit dolu vb.) çıkış kodu
    /// yoneticiBitti'de işlenir; çıkmazsa durdurulur ve hata gösterilir.
    private func ilkSatirReddedildi(_ neden: String, surec: Cocuk) {
        gunluk.yaz("ilk satır kabul edilmedi: \(neden)")
        DispatchQueue.main.asyncAfter(deadline: .now() + GECERSIZ_SATIR_BEKLEME_SN) { [weak self] in
            guard let self, surec === self.yonetici, surec.isRunning, self.evre == .yoneticiBekleniyor else { return }
            self.yoneticiyiDurdur(apiIle: false) {}
            self.hataGoster(KabukHatasi(adim: AdimKimligi.yonetici,
                                        ileti: "Kurulum yöneticisinin ilk satırı beklenen biçimde değil (\(neden))."))
        }
    }

    private func yoneticiZamanAsimi(_ p: Cocuk) {
        guard evre == .yoneticiBekleniyor, p === yonetici else { return }
        let n = Int(ayarlar.hazirZamanAsimiSn)
        gunluk.yaz("yönetici \(n) sn içinde hazır olmadı; durduruluyor")
        yoneticiyiDurdur(apiIle: false) {}
        hataGoster(KabukHatasi(adim: AdimKimligi.yonetici, ileti: "Kurulum yöneticisi \(n) saniye içinde hazır olmadı."))
    }

    private func yoneticiBitti(_ p: Cocuk) {
        let kod = p.terminationStatus
        let neden = p.sinyalleBitti ? "sinyal \(kod)" : "çıkış kodu \(kod)"
        gunluk.yaz("yönetici bitti: pid \(p.processIdentifier), \(neden)")
        guard p === yonetici else { return }
        switch evre {
        case .kapaniyor, .hata, .baslangic, .hazirlaniyor:
            return
        case .yoneticiBekleniyor, .hazir:
            zamanAsimi?.cancel()
            let hazirdi = evre == .hazir
            if !p.sinyalleBitti, kod == ayarlar.kilitDoluCikisKodu {
                kilitDolu()
            } else if hazirdi {
                hataGoster(KabukHatasi(adim: AdimKimligi.yonetici,
                                       ileti: "Kurulum yöneticisi beklenmedik biçimde kapandı (\(neden))."))
            } else {
                hataGoster(KabukHatasi(adim: AdimKimligi.yonetici,
                                       ileti: "Kurulum yöneticisi başlarken kapandı (\(neden))."))
            }
        }
    }

    /// Yönetici kilidi alamadı (B.6): başka örnek varsa öne getir, yoksa bildir.
    private func kilitDolu() {
        if let diger = digerOrnek() {
            gunluk.yaz("kilit dolu; öteki örnek öne getiriliyor (pid \(diger.processIdentifier))")
            diger.activate(options: [.activateAllWindows])
            kapanisTamam = true
            NSApp.terminate(nil)
            return
        }
        hataGoster(KabukHatasi(adim: AdimKimligi.yonetici,
                               ileti: "Kitapçevir'in başka bir kopyası bu kurulum klasörünü kullanıyor (ör. komut satırından koşan bir kurulum). O kapandıktan sonra yeniden dene.",
                               tur: .kilit))
    }

    /// Yönetici hazır olduktan sonra: en son iki sürüm dışındaki app/<sürüm>
    /// klasörleri ve yarım kalmış .yeni/.eski klasörleri silinir (B.4-5).
    /// Çalışma zamanı klasörlerine dokunulmaz: venv onlara bağlıdır, karar yöneticinin.
    private func eskiSurumleriTemizle() {
        let appDizini = kok.appendingPathComponent("app", isDirectory: true)
        let czDizini = kok.appendingPathComponent("calisma_zamani", isDirectory: true)
        let guncel = ayarlar.surum
        isKuyrugu.async { [gunluk] in
            let fm = FileManager.default
            for ust in [appDizini, czDizini] {
                for ad in (try? fm.contentsOfDirectory(atPath: ust.path)) ?? []
                where ad.hasSuffix(".yeni") || ad.hasSuffix(".eski") {
                    Dosya.sil(ust.appendingPathComponent(ad))
                    gunluk?.yaz("yarım kalan klasör silindi: \(ust.appendingPathComponent(ad).nfcYol)")
                }
            }
            let surumler = ((try? fm.contentsOfDirectory(atPath: appDizini.path)) ?? [])
                .filter { $0 != guncel && Uygulama.surumParcalari($0) != nil }
                .sorted { Uygulama.surumParcalari($0)!.lexicographicallyPrecedes(Uygulama.surumParcalari($1)!) }
                .reversed()
            for ad in surumler.dropFirst(SAKLANAN_SURUM - 1) {
                Dosya.sil(appDizini.appendingPathComponent(ad))
                gunluk?.yaz("eski sürüm klasörü silindi: app/\(ad)")
            }
        }
    }

    static func surumParcalari(_ s: String) -> [Int]? {
        let p = s.split(separator: ".", omittingEmptySubsequences: false)
        guard p.count == 3 else { return nil }
        let n = p.compactMap { Int($0) }
        return n.count == 3 ? n : nil
    }

    // MARK: Hata (B.4-6)

    private func hataGoster(_ h: KabukHatasi) {
        if cikisSuruyor { return }
        evre = .hata
        zamanAsimi?.cancel()
        gunluk.yaz("HATA [\(h.adim)]: \(h.ileti)")
        var a = adimlar[h.adim] ?? ["kimlik": h.adim]
        a["durum"] = "hata"
        adimlar[h.adim] = a
        hataBilgisi = ["adim": h.adim, "ileti": h.ileti, "tur": h.tur == .kilit ? "kilit" : "genel"]
        hazirlikSayfasiniYukle()
        hataUyarisi(h)
    }

    private func hataUyarisi(_ h: KabukHatasi) {
        guard evre == .hata else { return }
        let u = NSAlert()
        u.alertStyle = .warning
        u.messageText = h.tur == .kilit ? "Kitapçevir zaten çalışıyor" : "Kitapçevir hazırlanamadı"
        u.informativeText = h.ileti + "\n\nGünlük: " + gunluk.dosya.nfcYol
        u.addButton(withTitle: "Yeniden dene")
        u.addButton(withTitle: "Günlüğü göster")
        u.addButton(withTitle: "Çık")
        let satirlar = gunluk.sonSatirlar(HATA_GUNLUK_SATIRI)
        if !satirlar.isEmpty {
            let kaydirma = NSScrollView(frame: NSRect(x: 0, y: 0, width: 560, height: 220))
            kaydirma.hasVerticalScroller = true
            kaydirma.borderType = .bezelBorder
            let metin = NSTextView(frame: kaydirma.bounds)
            metin.isEditable = false
            metin.font = NSFont.monospacedSystemFont(ofSize: NSFont.smallSystemFontSize, weight: .regular)
            metin.string = satirlar.joined(separator: "\n")
            metin.autoresizingMask = [.width]
            kaydirma.documentView = metin
            u.accessoryView = kaydirma
        }
        u.beginSheetModal(for: pencere) { [weak self] yanit in
            guard let self else { return }
            switch yanit {
            case .alertFirstButtonReturn: self.yenidenDene()
            case .alertSecondButtonReturn:
                self.gunlukleriGoster(nil)
                DispatchQueue.main.async { self.hataUyarisi(h) }
            case .alertThirdButtonReturn: NSApp.terminate(nil)
            default: break   // kabuk sayfayı kendisi kapattı (kapanış sürüyor)
            }
        }
    }

    private func yenidenDene() {
        gunluk.yaz("yeniden deneniyor")
        if let p = yonetici, p.isRunning {
            // Evre .hata kalır: eski yöneticinin bitişi hata sayılmaz.
            yoneticiyiDurdur(apiIle: false) { [weak self] in
                guard let self, !self.cikisSuruyor else { return }
                self.hazirlikBaslat()
            }
        } else {
            hazirlikBaslat()
        }
    }

    // MARK: Kapanış (B.6)

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Pencereyi kapatmak uygulamayı kapatır; onay reddedilirse pencere kalır.
        NSApp.terminate(nil)
        return false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { pencere?.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if kapanisTamam { return .terminateNow }
        if cikisSuruyor || onayUyarisi != nil { return .terminateCancel }
        guard let p = yonetici, p.isRunning else {
            hazirligiIptalEt()
            gunluk?.yaz("kapanış: çalışan yönetici yok")
            gunluk?.bosalt()
            return .terminateNow
        }
        if evre == .hazir, yoneticiAdresi != nil {
            isDurumunuSor { [weak self] durum in
                guard let self else { return }
                if let d = durum, d.calisanIs > 0 || d.kurulumSuruyor {
                    self.kapanisOnayi(d)
                } else {
                    self.kapanisiBaslat()
                }
            }
        } else {
            kapanisiBaslat()
        }
        return .terminateLater
    }

    private func kapanisOnayi(_ d: YoneticiDurumu) {
        let u = NSAlert()
        u.alertStyle = .warning
        u.messageText = "Kitapçevir kapatılsın mı?"
        var satirlar: [String] = []
        if d.calisanIs > 0 {
            satirlar.append("Süren bir iş var (\(d.calisanIs)). Kapatırsan iptal edilecek; yarım kalan çeviri kaldığı yerden sürdürülebilir.")
        }
        if d.kurulumSuruyor {
            satirlar.append("Kurulum sürüyor; kapatırsan indirilen kısım saklanır, sonra kaldığı yerden sürer.")
        }
        u.informativeText = satirlar.joined(separator: "\n\n")
        let kapat = u.addButton(withTitle: "Kapat")
        kapat.hasDestructiveAction = true
        u.addButton(withTitle: "Vazgeç")
        onayUyarisi = u
        gunluk.yaz("kapanış onayı soruldu: çalışan iş \(d.calisanIs), kurulum \(d.kurulumSuruyor)")
        u.beginSheetModal(for: pencere) { [weak self] yanit in
            guard let self else { return }
            self.onayUyarisi = nil
            if yanit == .alertFirstButtonReturn {
                self.kapanisiBaslat()
            } else {
                self.gunluk.yaz("kapanıştan vazgeçildi")
                NSApp.reply(toApplicationShouldTerminate: false)
            }
        }
    }

    private func kapanisiBaslat() {
        cikisSuruyor = true
        evre = .kapaniyor
        pencere.title = (pencere.title.components(separatedBy: " — ").first ?? pencere.title) + " — kapatılıyor…"
        hazirligiIptalEt()
        yoneticiyiDurdur(apiIle: true) { [weak self] in
            self?.kapanisTamam = true
            self?.gunluk.bosalt()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
    }

    /// SIGTERM / SIGINT / SIGHUP: onay sorulmadan düzenli kapanış.
    private func sinyalGeldi(_ s: Int32) {
        gunluk?.yaz("sinyal \(s) alındı: onaysız kapanış")
        if let u = onayUyarisi {
            // Onay bekleniyorsa "Kapat" seçilmiş sayılır (uyarının tamamlayıcısı kapanışı başlatır).
            pencere.endSheet(u.window, returnCode: .alertFirstButtonReturn)
            return
        }
        guard !cikisSuruyor, !kapanisTamam else { return }
        cikisSuruyor = true
        evre = .kapaniyor
        hazirligiIptalEt()
        yoneticiyiDurdur(apiIle: true) { [weak self] in
            guard let self else { exit(0) }
            self.kapanisTamam = true
            self.gunluk?.bosalt()
            // Pencereye bağlı bir sayfa (hata uyarısı, kaydetme paneli) açıkken
            // NSApp.terminate yok sayılıyor (ölçüldü); önce sayfa kapatılır.
            for _ in 0..<SAYFA_KAPATMA_EN_COK {
                guard let sayfa = self.pencere?.attachedSheet else { break }
                self.pencere.endSheet(sayfa, returnCode: .abort)
            }
            NSApp.terminate(nil)
        }
    }

    /// Yöneticiyi ve torunlarını durdurur (B.6-2/3):
    /// 1) süreç ağacının anlık görüntüsü alınır; 2) POST /api/kapat (olmazsa SIGTERM);
    /// 3) yönetici ve torunları kendiliğinden kapanana dek en çok `kapanisSuresi` beklenir;
    /// 4) süre dolunca yaşayanlara ve süreç gruplarına SIGKILL.
    private func yoneticiyiDurdur(apiIle: Bool, bitince: @escaping () -> Void) {
        guard let p = yonetici else { bitince(); return }
        let pid = p.processIdentifier
        var kayit: [pid_t: UInt64] = [:]
        var gruplar: Set<pid_t> = []
        let benimGrubum = getpgrp()
        let kokYollari = kokYolBaytlari()

        func goruntuEkle(_ tablo: [pid_t: SurecBilgisi]) {
            var kokler: [pid_t] = []
            if p.isRunning { kokler.append(pid) }
            kokler += kayit.keys.filter { tablo[$0]?.baslangic == kayit[$0] }
            for k in kokler {
                if k == pid, let b = tablo[pid] { kayit[pid] = b.baslangic; gruplar.insert(b.pgid) }
                for t in SurecTablosu.torunlar(k, tablo) { kayit[t.pid] = t.baslangic; gruplar.insert(t.pgid) }
            }
        }
        var tablo = SurecTablosu.hepsi()
        goruntuEkle(tablo)
        calisanDosyasindanEkle(tablo, kokYollari, &kayit, &gruplar)
        gruplar.remove(benimGrubum)
        gruplar.remove(0)
        gruplar.remove(1)
        gunluk.yaz("durdurma: yönetici pid \(pid), izlenen süreç \(kayit.count), süreç grubu \(gruplar.sorted())")

        let t0 = Date()
        let sonTarih = t0.addingTimeInterval(kapanisSuresi)
        if p.isRunning {
            if apiIle, yoneticiAdresi != nil {
                apiIstegi("/api/kapat", yontem: "POST", govde: Data("{}".utf8)) { [weak self] kod, _ in
                    self?.gunluk.yaz("durdurma: POST /api/kapat → \(kod.map(String.init) ?? "yanıt yok")")
                    if kod == nil || !(200..<300).contains(kod!), p.isRunning { kill(pid, SIGTERM) }
                }
            } else {
                kill(pid, SIGTERM)
            }
        }
        let zamanlayici = Timer(timeInterval: YOKLAMA_ARALIGI_SN, repeats: true) { [weak self] z in
            guard let self else { z.invalidate(); return }
            p.yokla()
            tablo = SurecTablosu.hepsi()
            goruntuEkle(tablo)
            gruplar.remove(benimGrubum); gruplar.remove(0); gruplar.remove(1)
            let yasayan = kayit.filter { tablo[$0.key]?.baslangic == $0.value }
            if !p.isRunning, yasayan.isEmpty {
                z.invalidate()
                self.gunluk.yaz(String(format: "durdurma: düzenli bitti (%.2f sn)", Date().timeIntervalSince(t0)))
                bitince()
                return
            }
            guard Date() >= sonTarih else { return }
            z.invalidate()
            var oldurulen: Set<pid_t> = []
            for g in gruplar {
                let uyeler = tablo.values.filter { $0.pgid == g }
                let bizim = uyeler.contains { kayit[$0.pid] == $0.baslangic }
                    || uyeler.contains { SurecTablosu.yolIceriyor($0.pid, kokYollari) }
                if bizim, killpg(g, SIGKILL) == 0 { oldurulen.formUnion(uyeler.map(\.pid)) }
            }
            for (k, b) in kayit where !oldurulen.contains(k) && SurecTablosu.bilgi(k)?.baslangic == b {
                if kill(k, SIGKILL) == 0 { oldurulen.insert(k) }
            }
            self.gunluk.yaz(String(format: "durdurma: %.0f sn doldu; %d süreç SIGKILL ile durduruldu", self.kapanisSuresi, oldurulen.count))
            bitince()
        }
        RunLoop.main.add(zamanlayici, forMode: .common)
    }

    /// Kök yolunun NFC ve NFD bayt biçimleri (komut satırı eşleştirmesi için).
    private func kokYolBaytlari() -> [Data] {
        let y = kok.path
        return Array(Set([Data(y.precomposedStringWithCanonicalMapping.utf8),
                          Data(y.decomposedStringWithCanonicalMapping.utf8)]))
    }

    /// <kök>/durum/calisan.json: yöneticinin kaydettiği PID ve süreç grupları.
    /// Adı "pid" ile biten tamsayı alanlar PID, adı "grup"/"pgid" içeren tamsayı
    /// ya da tamsayı dizisi alanlar süreç grubu sayılır. Her biri ancak komut
    /// satırı kök yolunu içeriyorsa kabul edilir (bayat dosya / PID yeniden kullanımı).
    private func calisanDosyasindanEkle(_ tablo: [pid_t: SurecBilgisi], _ yollar: [Data],
                                        _ kayit: inout [pid_t: UInt64], _ gruplar: inout Set<pid_t>) {
        let url = kok.appendingPathComponent("durum/calisan.json")
        guard let veri = try? Data(contentsOf: url),
              let nesne = try? JSONSerialization.jsonObject(with: veri) else { return }
        var pidler: [Int] = [], gruplarAday: [Int] = []
        func gez(_ o: Any, _ ad: String) {
            if let s = o as? [String: Any] {
                for (k, v) in s { gez(v, k.lowercased()) }
            } else if let a = o as? [Any] {
                for v in a { gez(v, ad) }
            } else if let n = o as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() {
                if ad.contains("grup") || ad.contains("pgid") { gruplarAday.append(n.intValue) }
                else if ad.hasSuffix("pid") { pidler.append(n.intValue) }
            }
        }
        gez(nesne, "")
        for n in pidler {
            let p = pid_t(truncatingIfNeeded: n)
            if let b = tablo[p], SurecTablosu.yolIceriyor(p, yollar) { kayit[p] = b.baslangic }
        }
        for n in gruplarAday {
            let g = pid_t(truncatingIfNeeded: n)
            if tablo.values.contains(where: { $0.pgid == g && SurecTablosu.yolIceriyor($0.pid, yollar) }) {
                gruplar.insert(g)
            }
        }
    }

    // MARK: Yönetici API'si (B.5)

    private func apiIstegi(_ yol: String, yontem: String, govde: Data? = nil,
                           tamam: @escaping (Int?, Data?) -> Void) {
        guard let adres = yoneticiAdresi,
              var c = URLComponents(url: adres, resolvingAgainstBaseURL: false) else { tamam(nil, nil); return }
        c.path = yol
        c.query = nil
        guard let url = c.url else { tamam(nil, nil); return }
        var istek = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: API_ZAMAN_ASIMI_SN)
        istek.httpMethod = yontem
        if let anahtar { istek.setValue(anahtar, forHTTPHeaderField: "X-Kitapcevir-Anahtar") }
        if let govde {
            istek.httpBody = govde
            istek.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        oturum.dataTask(with: istek) { veri, yanit, _ in
            let kod = (yanit as? HTTPURLResponse)?.statusCode
            DispatchQueue.main.async { tamam(kod, veri) }
        }.resume()
    }

    /// GET /api/is_durumu → {calisan_is: n, kurulum_suruyor?: bool}
    private func isDurumunuSor(_ tamam: @escaping (YoneticiDurumu?) -> Void) {
        apiIstegi("/api/is_durumu", yontem: "GET") { [weak self] kod, veri in
            guard let kod, (200..<300).contains(kod), let veri,
                  let n = try? JSONSerialization.jsonObject(with: veri) as? [String: Any] else {
                self?.gunluk.yaz("is_durumu alınamadı (\(kod.map(String.init) ?? "yanıt yok")); onaysız kapanılıyor")
                tamam(nil)
                return
            }
            tamam(YoneticiDurumu(calisanIs: (n["calisan_is"] as? NSNumber)?.intValue ?? 0,
                                 kurulumSuruyor: (n["kurulum_suruyor"] as? Bool) ?? false))
        }
    }

    // MARK: Menüler

    private func menuKur() -> NSMenu {
        let ad = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Kitapçevir"
        let ana = NSMenu()
        func ust(_ baslik: String, _ ogeler: [NSMenuItem]) -> NSMenu {
            let m = NSMenu(title: baslik)
            ogeler.forEach(m.addItem)
            let o = NSMenuItem(title: baslik, action: nil, keyEquivalent: "")
            o.submenu = m
            ana.addItem(o)
            return m
        }
        func oge(_ b: String, _ s: Selector?, _ k: String = "", _ m: NSEvent.ModifierFlags = .command) -> NSMenuItem {
            let o = NSMenuItem(title: b, action: s, keyEquivalent: k)
            o.keyEquivalentModifierMask = m
            return o
        }
        let digerleriniGizle = oge("Diğerlerini Gizle", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        _ = ust(ad, [
            oge("\(ad) Hakkında", #selector(NSApplication.orderFrontStandardAboutPanel(_:))),
            .separator(),
            oge("Bileşenler…", #selector(bilesenleriAc(_:)), ","),
            .separator(),
            oge("\(ad)'i Gizle", #selector(NSApplication.hide(_:)), "h"),
            digerleriniGizle,
            oge("Tümünü Göster", #selector(NSApplication.unhideAllApplications(_:))),
            .separator(),
            oge("\(ad)'den Çık", #selector(NSApplication.terminate(_:)), "q"),
        ])
        _ = ust("Düzen", [
            oge("Geri Al", Selector(("undo:")), "z"),
            oge("Yinele", Selector(("redo:")), "z", [.command, .shift]),
            .separator(),
            oge("Kes", #selector(NSText.cut(_:)), "x"),
            oge("Kopyala", #selector(NSText.copy(_:)), "c"),
            oge("Yapıştır", #selector(NSText.paste(_:)), "v"),
            oge("Tümünü Seç", #selector(NSText.selectAll(_:)), "a"),
        ])
        _ = ust("Görünüm", [
            oge("Yeniden Yükle", #selector(yenidenYukle(_:)), "r"),
            oge("Tarayıcıda Aç", #selector(tarayicidaAc(_:)), "o", [.command, .option]),
            .separator(),
            oge("Gerçek Boyut", #selector(gercekBoyut(_:)), "0"),
            oge("Yakınlaştır", #selector(yakinlastir(_:)), "+"),
            oge("Uzaklaştır", #selector(uzaklastir(_:)), "-"),
        ])
        let pencereMenusu = ust("Pencere", [
            oge("Küçült", #selector(NSWindow.performMiniaturize(_:)), "m"),
            oge("Büyüt", #selector(NSWindow.performZoom(_:))),
        ])
        NSApp.windowsMenu = pencereMenusu
        _ = ust("Yardım", [
            oge("Günlükleri Göster", #selector(gunlukleriGoster(_:))),
        ])
        return ana
    }

    func validateMenuItem(_ o: NSMenuItem) -> Bool {
        switch o.action {
        case #selector(bilesenleriAc(_:)): return evre == .hazir && yoneticiAdresi != nil
        case #selector(tarayicidaAc(_:)): return ["http", "https"].contains(web?.url?.scheme ?? "")
        default: return true
        }
    }

    @objc private func bilesenleriAc(_ s: Any?) {
        guard evre == .hazir, let adres = yoneticiAdresi,
              var c = URLComponents(url: adres, resolvingAgainstBaseURL: false) else { return }
        var q = (c.queryItems ?? []).filter { $0.name != "kip" }
        q.append(URLQueryItem(name: "kip", value: "bilesenler"))
        c.queryItems = q
        if let url = c.url { web.load(URLRequest(url: url)) }
    }

    @objc private func yenidenYukle(_ s: Any?) { web.reload() }

    @objc private func tarayicidaAc(_ s: Any?) {
        if let u = web.url, ["http", "https"].contains(u.scheme ?? "") { NSWorkspace.shared.open(u) }
    }

    @objc private func gercekBoyut(_ s: Any?) { web.pageZoom = 1 }
    @objc private func yakinlastir(_ s: Any?) { web.pageZoom = min(YAKINLASTIRMA_EN_COK, web.pageZoom + YAKINLASTIRMA_ADIMI) }
    @objc private func uzaklastir(_ s: Any?) { web.pageZoom = max(YAKINLASTIRMA_EN_AZ, web.pageZoom - YAKINLASTIRMA_ADIMI) }

    @objc private func gunlukleriGoster(_ s: Any?) {
        gunluk.bosalt()
        if Dosya.varMi(gunluk.dosya) {
            NSWorkspace.shared.activateFileViewerSelecting([gunluk.dosya])
        } else {
            NSWorkspace.shared.open(gunluk.dizin)
        }
    }

    // MARK: WKNavigationDelegate — gezinme kuralları

    private static func yerelMi(_ url: URL) -> Bool {
        guard ["http", "https"].contains(url.scheme ?? "") else { return false }
        return ["127.0.0.1", "localhost", "::1", "[::1]"].contains(url.host ?? "")
    }

    func webView(_ webView: WKWebView, decidePolicyFor eylem: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if eylem.shouldPerformDownload { decisionHandler(.download); return }
        guard let url = eylem.request.url else { decisionHandler(.cancel); return }
        let anaCerceve = eylem.targetFrame?.isMainFrame ?? true
        switch url.scheme ?? "" {
        case "http", "https":
            if Uygulama.yerelMi(url) { decisionHandler(.allow); return }
            if anaCerceve {
                // Dış bağlantılar (kaynak kodu, lisans metni…) varsayılan tarayıcıda açılır.
                NSWorkspace.shared.open(url)
            } else {
                gunluk.yaz("dış çerçeve engellendi: \(url.host ?? "?")")
            }
            decisionHandler(.cancel)
        case "file":
            decisionHandler(url.standardizedFileURL.path.hasPrefix(kaynaklar.standardizedFileURL.path) ? .allow : .cancel)
        case "about", "blob", "data":
            decisionHandler(.allow)
        default:
            if eylem.navigationType == .linkActivated { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor yanit: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if let h = yanit.response as? HTTPURLResponse,
           let cd = h.value(forHTTPHeaderField: "Content-Disposition"), cd.lowercased().hasPrefix("attachment") {
            decisionHandler(.download)
        } else if !yanit.canShowMIMEType {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if webView.url?.isFileURL == true { sayfaDurumunuGonder() }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        gezinmeHatasi(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        gezinmeHatasi(error)
    }

    private func gezinmeHatasi(_ error: Error) {
        let e = error as NSError
        if e.domain == NSURLErrorDomain, e.code == NSURLErrorCancelled { return }
        if e.domain == "WebKitErrorDomain", e.code == 102 { return }   // gezinme indirmeye dönüştü
        gunluk.yaz("sayfa yüklenemedi: \(anahtariGizle(e.localizedDescription)) (\(e.domain) \(e.code))")
        if evre == .hazir, let p = yonetici, !p.isRunning { return }   // yoneticiBitti ele alır
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        gunluk.yaz("WebKit içerik süreci kapandı; sayfa yeniden yükleniyor")
        if webView.url?.isFileURL == true { hazirlikSayfasiniYukle() } else { webView.reload() }
    }

    // MARK: İndirme (WKDownloadDelegate)

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: pencere) { [weak self] sonuc in
            guard sonuc == .OK, let hedef = panel.url else { completionHandler(nil); return }
            // Kullanıcı panelde "Değiştir"i onayladıysa eski dosya kaldırılır (WKDownload var olan dosyaya yazmaz).
            if Dosya.varMi(hedef) { try? FileManager.default.removeItem(at: hedef) }
            self?.gunluk.yaz("indirme: \(hedef.lastPathComponent)")
            completionHandler(hedef)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        gunluk.yaz("indirme bitti")
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        gunluk.yaz("indirme başarısız: \(error.localizedDescription)")
        let u = NSAlert()
        u.messageText = "Dosya kaydedilemedi"
        u.informativeText = error.localizedDescription
        u.beginSheetModal(for: pencere, completionHandler: nil)
    }

    // MARK: WKUIDelegate — dosya seçme, JavaScript iletileri, yeni pencere

    func webView(_ webView: WKWebView, runOpenPanelWith parametreler: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parametreler.allowsMultipleSelection
        panel.canChooseDirectories = parametreler.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: pencere) { sonuc in
            completionHandler(sonuc == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage ileti: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let u = NSAlert()
        u.messageText = ileti
        u.addButton(withTitle: "Tamam")
        u.beginSheetModal(for: pencere) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage ileti: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let u = NSAlert()
        u.messageText = ileti
        u.addButton(withTitle: "Tamam")
        u.addButton(withTitle: "Vazgeç")
        u.beginSheetModal(for: pencere) { y in completionHandler(y == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt ileti: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        let u = NSAlert()
        u.messageText = ileti
        let alan = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        alan.stringValue = defaultText ?? ""
        u.accessoryView = alan
        u.addButton(withTitle: "Tamam")
        u.addButton(withTitle: "Vazgeç")
        u.beginSheetModal(for: pencere) { y in completionHandler(y == .alertFirstButtonReturn ? alan.stringValue : nil) }
    }

    func webView(_ webView: WKWebView, createWebViewWith c: WKWebViewConfiguration,
                 for eylem: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        // Yeni pencere istekleri (target=_blank, window.open) varsayılan tarayıcıda açılır.
        if let url = eylem.request.url, ["http", "https"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        } else {
            gunluk.yaz("yeni pencere isteği yok sayıldı: \(eylem.request.url?.scheme ?? "?")")
        }
        return nil
    }
}

// MARK: - Giriş

let uygulama = NSApplication.shared
let temsilci = Uygulama()
uygulama.delegate = temsilci
uygulama.setActivationPolicy(.regular)
uygulama.run()
