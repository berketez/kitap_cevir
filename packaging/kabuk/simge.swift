// Kitapçevir uygulama simgesinin kaynağı (derle.sh çalıştırır; uygulamaya girmez).
//
// Kullanım: simge <vurgu #rrggbb> <zemin #rrggbb> <metin #rrggbb> <çıktı.iconset>
// Renkler .streamlit/config.toml [theme]'den gelir (primaryColor, backgroundColor,
// textColor); burada renk yazılmaz. Çizim 1024×1024 birimlik ızgarada tanımlıdır ve
// her boyut ayrı çizilir (küçük boyutlar ölçeklenmiş görüntü değildir).
// Yalnız geometrik şekiller: yazıtipi ya da SF Symbols kullanılmaz (lisans).

import AppKit

func renk(_ s: String) -> NSColor {
    guard s.count == 7, s.hasPrefix("#"), let v = UInt32(s.dropFirst(), radix: 16) else {
        FileHandle.standardError.write(Data("geçersiz renk: \(s)\n".utf8))
        exit(2)
    }
    return NSColor(srgbRed: CGFloat((v >> 16) & 0xff) / 255, green: CGFloat((v >> 8) & 0xff) / 255,
                   blue: CGFloat(v & 0xff) / 255, alpha: 1)
}

let arg = CommandLine.arguments
guard arg.count == 5 else {
    FileHandle.standardError.write(Data("kullanım: simge <vurgu> <zemin> <metin> <çıktı.iconset>\n".utf8))
    exit(2)
}
let vurgu = renk(arg[1]), zemin = renk(arg[2]), metin = renk(arg[3])
let cikti = URL(fileURLWithPath: arg[4], isDirectory: true)

// macOS simge ızgarası: 1024'lük tuvalde 824'lük yuvarlatılmış kare (kenar payı 100).
let IZGARA: CGFloat = 1024
let KARE = NSRect(x: 100, y: 100, width: 824, height: 824)
let KOSE: CGFloat = 185

func ciz() {
    let kare = NSBezierPath(roundedRect: KARE, xRadius: KOSE, yRadius: KOSE)
    let alt = vurgu.blended(withFraction: 0.45, of: zemin) ?? vurgu
    NSGradient(starting: vurgu, ending: alt)?.draw(in: kare, angle: -90)

    // Açık kitap: sırtta birleşen iki sayfa.
    func sayfa(_ yon: CGFloat) -> NSBezierPath {
        let x = { (d: CGFloat) in 512 + yon * d }
        let p = NSBezierPath()
        p.move(to: NSPoint(x: x(8), y: 318))
        p.curve(to: NSPoint(x: x(276), y: 352), controlPoint1: NSPoint(x: x(110), y: 372), controlPoint2: NSPoint(x: x(200), y: 352))
        p.line(to: NSPoint(x: x(276), y: 706))
        p.curve(to: NSPoint(x: x(8), y: 676), controlPoint1: NSPoint(x: x(200), y: 706), controlPoint2: NSPoint(x: x(110), y: 728))
        p.close()
        return p
    }
    metin.setFill()
    sayfa(-1).fill()
    sayfa(1).fill()

    // Sayfalardaki satırlar (yalnız büyük boyutlarda seçilir; küçükte de zarar vermez).
    vurgu.withAlphaComponent(0.55).setStroke()
    for yon: CGFloat in [-1, 1] {
        for i in 0..<4 {
            let y = 610 - CGFloat(i) * 62
            let p = NSBezierPath()
            p.lineWidth = 16
            p.lineCapStyle = .round
            p.move(to: NSPoint(x: 512 + yon * 70, y: y - 6))
            p.curve(to: NSPoint(x: 512 + yon * 220, y: y + 6),
                    controlPoint1: NSPoint(x: 512 + yon * 120, y: y + 10),
                    controlPoint2: NSPoint(x: 512 + yon * 180, y: y + 4))
            p.stroke()
        }
    }
}

func png(_ piksel: Int) -> Data {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: piksel, pixelsHigh: piksel,
                               bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .calibratedRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    let bag = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = bag
    bag.imageInterpolation = .high
    let olcek = CGFloat(piksel) / IZGARA
    let donusum = NSAffineTransform()
    donusum.scale(by: olcek)
    donusum.concat()
    ciz()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

try? FileManager.default.createDirectory(at: cikti, withIntermediateDirectories: true)
for taban in [16, 32, 128, 256, 512] {
    for kat in [1, 2] {
        let ad = kat == 1 ? "icon_\(taban)x\(taban).png" : "icon_\(taban)x\(taban)@2x.png"
        do {
            try png(taban * kat).write(to: cikti.appendingPathComponent(ad))
        } catch {
            FileHandle.standardError.write(Data("yazılamadı: \(ad): \(error)\n".utf8))
            exit(1)
        }
    }
}
