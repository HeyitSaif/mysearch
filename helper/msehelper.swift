// msehelper — native macOS helper for MySearch.
// PDF text per page (PDFKit), page rendering and image normalisation (CoreGraphics/ImageIO),
// OCR (Vision, on-device). Every command prints one JSON object on stdout.
//
//   msehelper pdf    <file> [--max-pages N]
//   msehelper render <file.pdf> --pages 1,2 --out <dir> [--max-px 1280]
//   msehelper ocr    <file.pdf|image> [--pages 1,2] [--langs de-DE,en-US]   (no --langs: detect the language per image)
//   msehelper image  <file> --out <file.jpg> [--max-px 1536] [--thumb <file.jpg>] [--thumb-px 360]
//   msehelper probe  <image> [--max-px 512]                       (does the picture likely hold text? a cheap pre-check)
//   msehelper keychain get|has|set|delete <account> [--no-ui]      (Ask's cloud provider keys, see below)
//   msehelper watch  <folder>... [--latency 0.3] [--no-defer] [--exclude <folder>]...   (FSEvents, one JSON line per event)
//   msehelper features -                                           (what this build can do: crawl, watch_latency, watch_exclude)
//   msehelper crawl  <root> [--skip-prefix P]... [--skip-name G]... [--skip-dir G]... [--no-descend EXT]...
//                    [--max-depth N] [--nice N]                      (file-name index for the whole-Mac search, below)
//   msehelper describe --probe | --stdin [--max-px N] [--text-px N] [--pdf-px N] [--ocr gate|always|off]
//                    [--qos background|utility|default] [--nice N]   (whole-disk content index: labels and text, see below)

import Foundation
import PDFKit
import Vision
import ImageIO
import CoreGraphics
import Security
import UniformTypeIdentifiers
import AVFoundation
import NaturalLanguage

func emit(_ obj: Any) {
    let data = try! JSONSerialization.data(withJSONObject: obj, options: [])
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write("\n".data(using: .utf8)!)
}

func fail(_ msg: String) -> Never {
    emit(["error": msg])
    exit(1)
}

// MARK: - Keychain
//
// API keys of Ask's cloud providers: generic passwords in the login Keychain, service "com.mysearch.provider",
// account = the provider id. `set` reads the secret from stdin, never from an argument, so it never shows in a
// process list. `get` prints {"found": true, "secret": "…"} to the caller's pipe; `has` never reads the secret.
// --no-ui: fail instead of showing a Keychain dialog (the daemon reads keys in the background). The item's access
// list trusts the binary that wrote it: after rebuilding this helper, store the key again (or allow it once).

let keychainService = "com.mysearch.provider"

func secMessage(_ status: OSStatus) -> String {
    let text = (SecCopyErrorMessageString(status, nil) as String?) ?? "error"
    return "\(text) (OSStatus \(status))"
}

func keychainCommand(_ a: [String]) -> Never {
    let usage = "keychain: use msehelper keychain <get|has|set|delete> <account> [--no-ui]"
    guard a.count >= 2 else { fail(usage) }
    let action = a[0], account = a[1]
    guard account.range(of: "^[A-Za-z0-9._-]{1,64}$", options: .regularExpression) != nil else {
        fail("keychain: invalid account name")
    }
    let noUI = a.contains("--no-ui")
    if noUI { SecKeychainSetUserInteractionAllowed(false) }
    var query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: keychainService,
        kSecAttrAccount as String: account,
    ]
    switch action {
    case "get", "has":
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        if action == "get" { query[kSecReturnData as String] = true } else { query[kSecReturnAttributes as String] = true }
        if noUI { query[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIFail }
        var out: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &out)
        if status == errSecItemNotFound {
            emit(action == "get" ? ["found": false] : ["exists": false])
            exit(0)
        }
        guard status == errSecSuccess else { fail("keychain: \(secMessage(status))") }
        if action == "has" { emit(["exists": true]); exit(0) }
        guard let data = out as? Data, let secret = String(data: data, encoding: .utf8) else {
            fail("keychain: the stored item is not readable text")
        }
        emit(["found": true, "secret": secret])
        exit(0)
    case "set":
        let input = FileHandle.standardInput.readDataToEndOfFile()
        let secret = (String(data: input, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !secret.isEmpty, secret.utf8.count <= 8192, !secret.contains("\n") else {
            fail("keychain: send the secret as one non-empty line on stdin")
        }
        let data = Data(secret.utf8)
        var status = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            add[kSecAttrLabel as String] = "MySearch Ask: \(account)"
            add[kSecAttrDescription as String] = "API key"
            status = SecItemAdd(add as CFDictionary, nil)
        }
        guard status == errSecSuccess else { fail("keychain: \(secMessage(status))") }
        emit(["ok": true])
        exit(0)
    case "delete":
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { fail("keychain: \(secMessage(status))") }
        emit(["ok": true, "deleted": status == errSecSuccess])
        exit(0)
    default:
        fail(usage)
    }
}

if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "keychain" {
    keychainCommand(Array(CommandLine.arguments.dropFirst(2)))
}

// MARK: - Crawl (whole-Mac file-name index)
//
// `crawl <root>` walks a folder breadth-first with getattrlistbulk (one syscall returns the names and attributes of
// many entries) and prints one JSON line per entry:  {"path","name","is_dir","size","mtime","ctime"}  (seconds;
// ctime = creation time). Then {"done":true,"entries":N,"dirs":D,"denied":K,"skipped":S}. {"denied":"<dir>"} lines
// name folders that could not be opened (no permission): they are skipped silently, nothing is asked.
//
//   --skip-prefix P   do not list or descend into the entry whose absolute path is P (case-insensitive)
//   --skip-name G     skip any entry whose name matches G (an exact name or an fnmatch glob, case-insensitive)
//   --skip-dir G      the same, for folders only
//   --no-descend EXT  list folders with this extension (app, framework, photoslibrary ...) but do not enter them
//   --max-depth N     N levels below the root (1 = the root's own entries only); 0 = unlimited
//   --nice N          scheduling priority (default 10); disk I/O is always throttled
//
// This is only a speed-up: the Python side re-checks every line against the privacy rules before storing it.
// Symbolic links are listed, never followed; other volumes (a different st_dev) and iCloud/File Provider placeholder
// folders (SF_DATALESS) are not entered.

let outBufferLimit = 1 << 16
var outBuffer = [UInt8]()
outBuffer.reserveCapacity(outBufferLimit + 4096)

func flushOut() {
    var offset = 0
    outBuffer.withUnsafeBufferPointer { p in
        while offset < p.count {
            let n = write(1, p.baseAddress! + offset, p.count - offset)
            if n < 0 {
                if errno == EINTR { continue }
                exit(0)                         // the reader is gone
            }
            offset += n
        }
    }
    outBuffer.removeAll(keepingCapacity: true)
}

func appendJSONString(_ s: String) {
    outBuffer.append(0x22)
    for b in s.utf8 {
        switch b {
        case 0x22: outBuffer.append(0x5C); outBuffer.append(0x22)
        case 0x5C: outBuffer.append(0x5C); outBuffer.append(0x5C)
        case 0..<0x20:
            let hex = Array("0123456789abcdef".utf8)
            outBuffer.append(contentsOf: [0x5C, 0x75, 0x30, 0x30, hex[Int(b >> 4)], hex[Int(b & 15)]])
        default: outBuffer.append(b)
        }
    }
    outBuffer.append(0x22)
}

func appendRaw(_ s: String) { outBuffer.append(contentsOf: Array(s.utf8)) }

struct CrawlRules {
    var skipPrefixes = Set<String>()
    var skipNames = Set<String>()
    var skipNameGlobs: [String] = []
    var skipDirs = Set<String>()
    var skipDirGlobs: [String] = []
    var noDescend = Set<String>()

    static func isGlob(_ p: String) -> Bool { p.contains("*") || p.contains("?") || p.contains("[") }

    func matches(_ lowerName: String, exact: Set<String>, globs: [String]) -> Bool {
        if exact.contains(lowerName) { return true }
        for g in globs where fnmatch(g, lowerName, 0) == 0 { return true }
        return false
    }
}

func crawlCommand(root: String, args: [String]) -> Never {
    var rules = CrawlRules()
    var maxDepth = 0
    var niceValue: Int32 = 10
    var i = 0
    while i < args.count {
        let a = args[i]
        func value() -> String { i += 1; return i < args.count ? args[i] : "" }
        switch a {
        case "--skip-prefix":
            var v = value().lowercased()
            while v.count > 1 && v.hasSuffix("/") { v.removeLast() }
            rules.skipPrefixes.insert(v)
        case "--skip-name":
            let v = value().lowercased()
            if CrawlRules.isGlob(v) { rules.skipNameGlobs.append(v) } else { rules.skipNames.insert(v) }
        case "--skip-dir":
            let v = value().lowercased()
            if CrawlRules.isGlob(v) { rules.skipDirGlobs.append(v) } else { rules.skipDirs.insert(v) }
        case "--no-descend": rules.noDescend.insert(value().lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")))
        case "--max-depth": maxDepth = Int(value()) ?? 0
        case "--nice": niceValue = Int32(value()) ?? 10
        default: break
        }
        i += 1
    }
    signal(SIGPIPE, SIG_IGN)
    setpriority(PRIO_PROCESS, 0, niceValue)
    _ = setiopolicy_np(IOPOL_TYPE_DISK, IOPOL_SCOPE_PROCESS, IOPOL_THROTTLE)

    var rootPath = root
    while rootPath.count > 1 && rootPath.hasSuffix("/") { rootPath.removeLast() }
    var rootStat = stat()
    guard lstat(rootPath, &rootStat) == 0, (rootStat.st_mode & S_IFMT) == S_IFDIR else {
        fail("crawl: not a folder: \(rootPath)")
    }
    let rootDev = rootStat.st_dev

    // getattrlistbulk: which attributes, in the order the kernel packs them (by bit number).
    let CMN_NAME: UInt32 = 0x00000001, CMN_OBJTYPE: UInt32 = 0x00000008, CMN_CRTIME: UInt32 = 0x00000200
    let CMN_MODTIME: UInt32 = 0x00000400, CMN_FLAGS: UInt32 = 0x00040000, CMN_RETURNED: UInt32 = 0x80000000
    let FILE_DATALENGTH: UInt32 = 0x00000200
    let SF_DATALESS: UInt32 = 0x40000000
    let VREG: UInt32 = 1, VDIR: UInt32 = 2, VLNK: UInt32 = 5

    var attrs = attrlist()
    attrs.bitmapcount = UInt16(5)
    attrs.commonattr = CMN_RETURNED | CMN_NAME | CMN_OBJTYPE | CMN_CRTIME | CMN_MODTIME | CMN_FLAGS
    attrs.fileattr = FILE_DATALENGTH

    let bufSize = 512 * 1024
    let buf = UnsafeMutableRawPointer.allocate(byteCount: bufSize, alignment: 16)
    var entries = 0, dirs = 0, denied = 0, skipped = 0
    var queue: [(String, Int)] = [(rootPath, 0)]
    var head = 0

    while head < queue.count {
        let (dir, depth) = queue[head]
        head += 1
        let fd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        if fd < 0 {
            if errno == EACCES || errno == EPERM {
                denied += 1
                if denied <= 200 { appendRaw("{\"denied\":"); appendJSONString(dir); appendRaw("}\n") }
            }
            continue
        }
        var dst = stat()
        if fstat(fd, &dst) != 0 || dst.st_dev != rootDev { close(fd); skipped += 1; continue }
        let prefix = dir == "/" ? "" : dir
        while true {
            let n = getattrlistbulk(fd, &attrs, buf, bufSize, 0)
            if n <= 0 {
                if n < 0 && (errno == EACCES || errno == EPERM) {
                    denied += 1
                    if denied <= 200 { appendRaw("{\"denied\":"); appendJSONString(dir); appendRaw("}\n") }
                }
                break
            }
            var p = UnsafeRawPointer(buf)
            for _ in 0..<Int(n) {
                let entryStart = p
                let length = Int(p.loadUnaligned(as: UInt32.self))
                var f = p + 4
                let returnedCommon = f.loadUnaligned(as: UInt32.self)
                let returnedFile = (f + 12).loadUnaligned(as: UInt32.self)
                f += 20                                           // attribute_set_t: 5 x UInt32
                p = entryStart + length
                var name = ""
                var objType: UInt32 = 0
                var crtime: Int64 = 0, modtime: Int64 = 0
                var flags: UInt32 = 0
                var size: Int64 = 0
                if returnedCommon & CMN_NAME != 0 {
                    let off = Int(f.loadUnaligned(as: Int32.self))        // relative to this attrreference field
                    let cstr = (f + off).assumingMemoryBound(to: CChar.self)
                    name = String(cString: cstr)
                    f += 8                                                // attrreference_t
                }
                if returnedCommon & CMN_OBJTYPE != 0 { objType = f.loadUnaligned(as: UInt32.self); f += 4 }
                if returnedCommon & CMN_CRTIME != 0 { crtime = Int64(f.loadUnaligned(as: Int64.self)); f += 16 }
                if returnedCommon & CMN_MODTIME != 0 { modtime = Int64(f.loadUnaligned(as: Int64.self)); f += 16 }
                if returnedCommon & CMN_FLAGS != 0 { flags = f.loadUnaligned(as: UInt32.self); f += 4 }
                if returnedFile & FILE_DATALENGTH != 0 { size = f.loadUnaligned(as: Int64.self); f += 8 }
                if name.isEmpty || name == "." || name == ".." { continue }
                guard objType == VREG || objType == VDIR || objType == VLNK else { continue }
                let isDir = objType == VDIR
                let lower = name.lowercased()
                let full = prefix + "/" + name
                if rules.matches(lower, exact: rules.skipNames, globs: rules.skipNameGlobs)
                    || (isDir && rules.matches(lower, exact: rules.skipDirs, globs: rules.skipDirGlobs))
                    || rules.skipPrefixes.contains(full.lowercased()) {
                    skipped += 1
                    continue
                }
                entries += 1
                appendRaw("{\"path\":"); appendJSONString(full)
                appendRaw(",\"name\":"); appendJSONString(name)
                appendRaw(isDir ? ",\"is_dir\":true" : ",\"is_dir\":false")
                appendRaw(",\"size\":\(isDir ? 0 : size),\"mtime\":\(modtime),\"ctime\":\(crtime)}\n")
                if outBuffer.count >= outBufferLimit { flushOut() }
                if isDir {
                    dirs += 1
                    let ext = (name as NSString).pathExtension.lowercased()
                    let enter = (maxDepth == 0 || depth + 1 < maxDepth) && flags & SF_DATALESS == 0
                        && !(ext.isEmpty == false && rules.noDescend.contains(ext))
                    if enter { queue.append((full, depth + 1)) }
                }
            }
        }
        close(fd)
        if head > 4096 && head * 2 > queue.count { queue.removeFirst(head); head = 0 }   // keep the queue small
    }
    appendRaw("{\"done\":true,\"entries\":\(entries),\"dirs\":\(dirs),\"denied\":\(denied),\"skipped\":\(skipped)}\n")
    flushOut()
    exit(0)
}

// `describe` (the whole-disk content index, see the MARK below) takes no file argument, so it is dispatched before the
// argument parsing below, which every other command shares.
if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "describe" {
    describeCommand(Array(CommandLine.arguments.dropFirst(2)))
}

var args = Array(CommandLine.arguments.dropFirst())
guard args.count >= 2 else { fail("usage: msehelper <pdf|render|ocr|image> <file> [options]") }
let command = args.removeFirst()
let path = args.removeFirst()
let url = URL(fileURLWithPath: path)

func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

func pageList(_ spec: String?, count: Int) -> [Int] {
    guard let spec = spec, !spec.isEmpty else { return Array(1...max(count, 1)).filter { $0 <= count } }
    return spec.split(separator: ",").compactMap { Int($0) }.filter { $0 >= 1 && $0 <= count }
}

// MARK: - Rendering

/// Width and height of a page in points, after its rotation.
func pageSize(_ page: CGPDFPage) -> (CGFloat, CGFloat) {
    let box = page.getBoxRect(.cropBox)
    let rotated = page.rotationAngle % 180 != 0
    return rotated ? (box.height, box.width) : (box.width, box.height)
}

/// Renders a page so that its long side is at most `maxPx` pixels (and at most 4 pixels per point).
func renderPage(_ page: CGPDFPage, maxPx: CGFloat) -> CGImage? {
    let (w0, h0) = pageSize(page)
    guard w0 > 0, h0 > 0 else { return nil }
    return renderPage(page, scale: min(maxPx / max(w0, h0), 4.0))
}

/// Renders a page at `scale` pixels per point.
func renderPage(_ page: CGPDFPage, scale: CGFloat) -> CGImage? {
    let (w0, h0) = pageSize(page)
    guard w0 > 0, h0 > 0, scale > 0 else { return nil }
    let w = Int((w0 * scale).rounded()), h = Int((h0 * scale).rounded())
    guard w > 0, h > 0, let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    let target = CGRect(x: 0, y: 0, width: w, height: h)
    ctx.concatenate(page.getDrawingTransform(.cropBox, rect: target, rotate: 0, preserveAspectRatio: true))
    ctx.interpolationQuality = .high
    ctx.drawPDFPage(page)
    return ctx.makeImage()
}

/// Pixels per point for OCR: about 300 dpi, with the long side at most 4,096 px. It is never below the scale the OCR
/// path has always used (a 2,000 px long side, at most 4 pixels per point), so no page is rendered smaller than before.
func ocrScale(_ page: CGPDFPage) -> CGFloat {
    let (w0, h0) = pageSize(page)
    let long = max(w0, h0)
    guard long > 0 else { return 1 }
    let before = min(2000 / long, 4.0)
    let dpi300 = CGFloat(300.0 / 72.0)
    return max(before, min(dpi300, 4096 / long))
}

/// Share of the page (0...1) that text lines cover, from PDFKit's own line selections. A picture with a header or a
/// footer covers little; a page of body text covers much. nil when the page has no selectable text box.
func textCoverage(_ page: PDFPage) -> Double? {
    let box = page.bounds(for: .cropBox)
    let area = Double(box.width * box.height)
    guard area > 0, let whole = page.selection(for: box) else { return nil }
    var covered = 0.0
    for line in whole.selectionsByLine() {
        let r = line.bounds(for: page)
        covered += Double(max(0, r.width) * max(0, r.height))
    }
    let share = min(1.0, covered / area)
    return share.isFinite ? share : nil
}

func writeJPEG(_ image: CGImage, to path: String, quality: Double = 0.85) -> Bool {
    let out = URL(fileURLWithPath: path)
    guard let dest = CGImageDestinationCreateWithURL(out as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else { return false }
    CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
    return CGImageDestinationFinalize(dest)
}

func loadImage(_ url: URL, maxPx: Int) -> CGImage? {
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    let opts: [CFString: Any] = [
        kCGImageSourceCreateThumbnailFromImageAlways: true,
        kCGImageSourceCreateThumbnailWithTransform: true,
        kCGImageSourceThumbnailMaxPixelSize: maxPx,
    ]
    return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
}

// MARK: - OCR

/// Text read from one image. `text` has the lines in reading order, with a blank line where a paragraph ends.
/// `confidence` is the mean over the kept lines (0...1) and `lines` their count (0 when nothing was read).
struct OCRResult {
    let text: String
    let confidence: Double
    let lines: Int

    func json(page: Int) -> [String: Any] {
        var d: [String: Any] = ["page": page, "text": text, "lines": lines]
        if lines > 0 { d["confidence"] = confidence }
        return d
    }
}

/// Reads one image with Vision. Languages: the given ones, or, when none are given, Vision's own detection per image.
/// Observations with a confidence below 0.3 are dropped. Vision's origin is the bottom-left corner, so y grows upwards.
func recognise(_ image: CGImage, langs: [String]?) -> OCRResult {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.minimumTextHeight = 0.006                // dense pages: text down to 0.6% of the image height
    if let newest = VNRecognizeTextRequest.supportedRevisions.max() {
        request.revision = newest                    // the newest revision this OS offers
    }
    if let langs = langs {
        request.recognitionLanguages = langs
        if #available(macOS 13.0, *) { request.automaticallyDetectsLanguage = false }
    } else if #available(macOS 13.0, *) {
        request.automaticallyDetectsLanguage = true
    }
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([request]) } catch { return OCRResult(text: "", confidence: 0, lines: 0) }

    struct Read {
        let text: String
        let conf: Double
        let box: CGRect
    }
    let reads: [Read] = (request.results ?? []).compactMap { obs in
        guard let top = obs.topCandidates(1).first, top.confidence >= 0.3 else { return nil }
        let s = top.string.trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? nil : Read(text: s, conf: Double(top.confidence), box: obs.boundingBox)
    }
    guard !reads.isEmpty else { return OCRResult(text: "", confidence: 0, lines: 0) }

    // Lines, from the top: an observation joins the current line when its vertical centre lies within half a line
    // height of the line's first observation. Within a line, left to right.
    var lines: [[Read]] = []
    for r in reads.sorted(by: { $0.box.midY > $1.box.midY }) {
        if let first = lines.last?.first, abs(first.box.midY - r.box.midY) <= 0.5 * min(first.box.height, r.box.height) {
            lines[lines.count - 1].append(r)
        } else {
            lines.append([r])
        }
    }
    let ordered = lines.map { $0.sorted { $0.box.minX < $1.box.minX } }

    // A gap well above the typical line height starts a new paragraph.
    let heights = reads.map { $0.box.height }.sorted()
    let typical = heights[heights.count / 2]
    var text = ""
    var previousBottom: CGFloat? = nil
    for line in ordered {
        let top = line.map { $0.box.maxY }.max() ?? 0
        let bottom = line.map { $0.box.minY }.min() ?? 0
        if let pb = previousBottom {
            text += (pb - top) > 1.2 * typical ? "\n\n" : "\n"
        }
        text += line.map { $0.text }.joined(separator: " ")
        previousBottom = bottom
    }
    let mean = reads.reduce(0.0) { $0 + $1.conf } / Double(reads.count)
    return OCRResult(text: text, confidence: mean, lines: ordered.count)
}

// MARK: - Commands

switch command {
case "pdf":
    guard let doc = PDFDocument(url: url) else { fail("cannot open pdf") }
    if doc.isLocked { emit(["pages": doc.pageCount, "encrypted": true, "items": []]); exit(0) }
    let maxPages = Int(option("--max-pages") ?? "") ?? 1000
    var items: [[String: Any]] = []
    for i in 0..<min(doc.pageCount, maxPages) {
        autoreleasepool {
            let page = doc.page(at: i)
            let text = page?.string ?? ""
            var item: [String: Any] = ["page": i + 1, "text": text]
            // Only pages with little text report how much of the page their text covers (see textCoverage).
            if let page = page, text.filter({ !$0.isWhitespace }).count < 600, let share = textCoverage(page) {
                item["coverage"] = share
            }
            items.append(item)
        }
    }
    let attrs = doc.documentAttributes ?? [:]
    var meta: [String: Any] = [:]
    for (k, v) in attrs { meta["\(k)"] = "\(v)" }
    emit(["pages": doc.pageCount, "encrypted": false, "items": items, "meta": meta])

case "render":
    guard let doc = CGPDFDocument(url as CFURL) else { fail("cannot open pdf") }
    guard let outDir = option("--out") else { fail("--out required") }
    let maxPx = CGFloat(Double(option("--max-px") ?? "") ?? 1280)
    try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
    var out: [[String: Any]] = []
    for p in pageList(option("--pages"), count: doc.numberOfPages) {
        autoreleasepool {
            guard let page = doc.page(at: p), let img = renderPage(page, maxPx: maxPx) else { return }
            let file = (outDir as NSString).appendingPathComponent("page-\(p).jpg")
            if writeJPEG(img, to: file) { out.append(["page": p, "file": file, "w": img.width, "h": img.height]) }
        }
    }
    emit(["items": out])

case "ocr":
    // --langs: explicit recognition languages. Without it, Vision detects the language of each image itself.
    var langs: [String]? = nil
    if let spec = option("--langs") {
        let list = spec.split(separator: ",").map { String($0) }.filter { !$0.isEmpty }
        if !list.isEmpty { langs = list }
    }
    var out: [[String: Any]] = []
    if url.pathExtension.lowercased() == "pdf" {
        guard let doc = CGPDFDocument(url as CFURL) else { fail("cannot open pdf") }
        for p in pageList(option("--pages"), count: doc.numberOfPages) {
            autoreleasepool {
                guard let page = doc.page(at: p), let img = renderPage(page, scale: ocrScale(page)) else { return }
                out.append(recognise(img, langs: langs).json(page: p))
            }
        }
    } else {
        guard let img = loadImage(url, maxPx: 3000) else { fail("cannot read image") }
        out.append(recognise(img, langs: langs).json(page: 1))
    }
    emit(["items": out])

case "image":
    guard let outFile = option("--out") else { fail("--out required") }
    let maxPx = Int(option("--max-px") ?? "") ?? 1536
    guard let img = loadImage(url, maxPx: maxPx) else { fail("cannot read image") }
    guard writeJPEG(img, to: outFile) else { fail("cannot write jpeg") }
    var result: [String: Any] = ["file": outFile, "w": img.width, "h": img.height]
    if let thumbFile = option("--thumb") {
        let tpx = Int(option("--thumb-px") ?? "") ?? 360
        if let t = loadImage(url, maxPx: tpx), writeJPEG(t, to: thumbFile, quality: 0.8) { result["thumb"] = thumbFile }
    }
    emit(result)

case "probe":
    // Does this picture likely hold text? The cheap pre-check the indexer runs before the full OCR of a picture
    // (media.image_ocr "auto"): Vision at the .fast level, no language correction, on a copy whose long side is at most
    // --max-px (512; the indexer asks for 768, where a 26 px caption in a 1600 px picture is still seen). The minimum
    // text height is the one the full OCR uses, so small text is not dropped before it is looked at. {"text_likely":
    // true} when at least one line with two or more letters or digits was read with a confidence of 0.5 or more (the
    // fast recogniser reports 0.5 for most real text; a lone glyph read out of a shape does not count); "lines" counts
    // those, "confidence" is the best one seen. About 0.06 s against 0.6 s for the full OCR. Pictures only: a PDF
    // page goes through `ocr`.
    let probePx = Int(option("--max-px") ?? "") ?? 512
    guard let img = loadImage(url, maxPx: probePx) else { fail("cannot read image") }
    let probe = VNRecognizeTextRequest()
    probe.recognitionLevel = .fast
    probe.usesLanguageCorrection = false
    probe.minimumTextHeight = 0.006
    let probeHandler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try probeHandler.perform([probe]) } catch { fail("probe failed: \(error.localizedDescription)") }
    var probeLines = 0
    var probeBest = 0.0
    for obs in probe.results ?? [] {
        guard let top = obs.topCandidates(1).first else { continue }
        let conf = Double(top.confidence)
        probeBest = max(probeBest, conf)
        let wordy = top.string.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }.count
        if conf >= 0.5 && wordy >= 2 { probeLines += 1 }
    }
    emit(["text_likely": probeLines >= 1, "lines": probeLines, "confidence": probeBest])

case "watch":
    // File-level FSEvents for one or more folders. One JSON line per event; exits when stdin closes.
    // `watch <folder>... [--latency seconds]`: the first argument must be a folder.
    var roots = [path]
    var latency = 1.5
    var noDefer = false          // --no-defer: the first event after a quiet spell is delivered at once, not after `latency`
    var excluded: [String] = []
    var wi = 0
    while wi < args.count {
        if args[wi] == "--latency", wi + 1 < args.count {
            latency = max(0.05, Double(args[wi + 1]) ?? 1.5)
            wi += 2
        } else if args[wi] == "--no-defer" {
            noDefer = true
            wi += 1
        } else if args[wi] == "--exclude", wi + 1 < args.count {
            excluded.append(args[wi + 1])               // at most 8 folders inside the watched ones (an FSEvents limit)
            wi += 2
        } else {
            if !args[wi].hasPrefix("--") { roots.append(args[wi]) }
            wi += 1
        }
    }
    var ctx = FSEventStreamContext(version: 0, info: nil, retain: nil, release: nil, copyDescription: nil)
    let callback: FSEventStreamCallback = { _, _, count, eventPaths, eventFlags, _ in
        let paths = Unmanaged<CFArray>.fromOpaque(eventPaths).takeUnretainedValue() as! [String]
        for i in 0..<count {
            emit(["path": paths[i], "flags": Int(eventFlags[i])])
        }
    }
    var flags = UInt32(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagUseCFTypes
                       | kFSEventStreamCreateFlagWatchRoot)
    if noDefer { flags |= UInt32(kFSEventStreamCreateFlagNoDefer) }
    guard let stream = FSEventStreamCreate(nil, callback, &ctx, roots as CFArray,
                                           FSEventStreamEventId(kFSEventStreamEventIdSinceNow), latency, flags) else {
        fail("cannot watch")
    }
    if !excluded.isEmpty {
        _ = FSEventStreamSetExclusionPaths(stream, Array(excluded.prefix(8)) as CFArray)
    }
    FSEventStreamSetDispatchQueue(stream, DispatchQueue(label: "mse.fsevents"))
    FSEventStreamStart(stream)
    emit(["watching": roots, "excluded": Array(excluded.prefix(8))])
    DispatchQueue.global().async {
        while readLine() != nil {}
        exit(0)
    }
    dispatchMain()

case "crawl":
    crawlCommand(root: path, args: args)

case "features":
    // What this build can do (an older installed helper answers {"error": "unknown command"}).
    emit(["crawl": true, "watch_latency": true, "watch_exclude": true, "watch_nodefer": true, "ocr_layout": true, "pdf_coverage": true])

default:
    fail("unknown command \(command)")
}

// MARK: - Describe (whole-disk content index, mse/wholedisk.py)
//
//   msehelper describe --probe                 {"describe": true, "version": 1}
//   msehelper describe --stdin [--max-px N] [--text-px N] [--pdf-px N] [--ocr gate|always|off] [--top N] [--min-conf F]
//                              [--qos background|utility|default] [--nice N]
//
// One JSON object per input line: {"id": <any>, "path": "...", "kind": "image" | "pdf" | "video"}. For each one, one JSON
// object on stdout, flushed at once (errors included):
//   {"id", "ok": true, "labels": [{"id", "c"}], "text", "lines", "ocr", "title", "pages", "lang", "ms"}
//   {"id", "ok": false, "error"}
// image: Vision VNClassifyImageRequest on a copy of at most --max-px pixels (labels with confidence >= --min-conf, the top
//        --top, identifiers with "_" written as spaces). Text is read with VNRecognizeTextRequest at the .fast level on a
//        copy of --text-px: --ocr gate reads it only when a text-like label is among the results, always reads it, off never.
// pdf:   the text layer of the first two pages (PDFKit). When page 1 has no text layer the page is rendered at --pdf-px and
//        read with .fast OCR. Encrypted files are reported, not opened.
// video: the labels of the poster frame at 10 % of the duration (AVFoundation). Nothing else is decoded.
// A file that is dataless (an iCloud placeholder: SF_DATALESS) is refused before any read. The process runs at the given
// QoS class (background by default), nice --nice, with its disk I/O throttled, as the crawl does.

struct DescribeOptions {
    var maxPx = 512
    var textPx = 1280
    var pdfPx = 1200
    var ocr = "gate"
    var top = 10
    var minConf: Float = 0.15
}

/// Labels that make an image worth reading for text (a screenshot, a scan of a page, a poster, a sign ...). A static
/// member, not a file-level constant: main.swift initialises its globals in order, and the describe command runs before
/// the end of the file, so a file-level constant would still be unset when it is read.
enum DescribeLabels {
    static let textLike: Set<String> = ["document", "newspaper", "screenshot", "book", "menu", "poster", "receipt", "letter",
                                        "sign", "text", "paper", "label", "chart", "map", "whiteboard", "blackboard"]
}

func describeFastOCR(_ image: CGImage) -> (text: String, lines: Int) {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .fast
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0.012
    if let supported = try? VNRecognizeTextRequest.supportedRecognitionLanguages(for: .fast, revision: request.revision) {
        let wanted = supported.filter { $0 == "en-US" || $0 == "de-DE" }
        if !wanted.isEmpty { request.recognitionLanguages = wanted }
    }
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([request]) } catch { return ("", 0) }
    var reads: [(text: String, conf: Float, box: CGRect)] = []
    for obs in request.results ?? [] {
        guard let top = obs.topCandidates(1).first, top.confidence >= 0.4 else { continue }
        let s = top.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.isEmpty { reads.append((s, top.confidence, obs.boundingBox)) }
    }
    reads.sort { $0.box.midY > $1.box.midY }                    // Vision's origin is bottom-left: top to bottom
    return (reads.map { $0.text }.joined(separator: "\n"), reads.count)
}

func describeClassify(_ image: CGImage, _ o: DescribeOptions) -> (labels: [[String: Any]], ids: [String], textLike: Bool) {
    let request = VNClassifyImageRequest()
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do { try handler.perform([request]) } catch { return ([], [], false) }
    let kept = (request.results ?? [])
        .filter { $0.confidence >= o.minConf }
        .sorted { $0.confidence > $1.confidence }
        .prefix(o.top)
    var labels: [[String: Any]] = [], ids: [String] = []
    var textLike = false
    for obs in kept {
        let raw = obs.identifier
        if DescribeLabels.textLike.contains(raw) { textLike = true }
        let name = raw.replacingOccurrences(of: "_", with: " ")
        labels.append(["id": name, "c": (Double(obs.confidence) * 1000).rounded() / 1000])
        ids.append(name)
    }
    return (labels, ids, textLike)
}

func describeLanguage(_ text: String) -> String {
    guard !text.isEmpty else { return "" }
    let recognizer = NLLanguageRecognizer()
    recognizer.processString(String(text.prefix(2000)))
    switch recognizer.dominantLanguage {
    case .some(.german): return "de"
    case .some(.english): return "en"
    default: return ""
    }
}

func describeImage(_ url: URL, _ o: DescribeOptions) -> [String: Any] {
    guard let image = loadImage(url, maxPx: o.maxPx) else { return ["ok": false, "error": "cannot read image"] }
    if image.width < 48 || image.height < 48 { return ["ok": false, "error": "too small to describe"] }
    let (labels, _, textLike) = describeClassify(image, o)
    var text = "", lines = 0, ocr = false
    if o.ocr == "always" || (o.ocr == "gate" && textLike) {
        if let big = loadImage(url, maxPx: o.textPx) {
            (text, lines) = describeFastOCR(big)
            ocr = true
        }
    }
    return ["ok": true, "labels": labels, "text": text, "lines": lines, "ocr": ocr, "lang": describeLanguage(text)]
}

func describePDF(_ url: URL, _ o: DescribeOptions) -> [String: Any] {
    guard let doc = PDFDocument(url: url) else { return ["ok": false, "error": "cannot open pdf"] }
    if doc.isLocked { return ["ok": false, "error": "encrypted"] }
    let pages = doc.pageCount
    var parts: [String] = []
    var firstChars = 0
    for i in 0..<min(2, pages) {
        let t = doc.page(at: i)?.string ?? ""
        if i == 0 { firstChars = t.filter { !$0.isWhitespace }.count }
        parts.append(t)
    }
    var text = parts.joined(separator: "\n"), lines = 0, ocr = false
    if firstChars < 40 && pages > 0, let cg = CGPDFDocument(url as CFURL), let page = cg.page(at: 1),
       let img = renderPage(page, maxPx: CGFloat(o.pdfPx)) {
        (text, lines) = describeFastOCR(img)                    // a scan: no text layer, read the picture of page 1
        ocr = true
    }
    let title = (doc.documentAttributes?["Title"] as? String) ?? ""
    return ["ok": true, "text": text, "lines": lines, "ocr": ocr, "title": title, "pages": pages,
            "lang": describeLanguage(text)]
}

func describeVideo(_ url: URL, _ o: DescribeOptions) -> [String: Any] {
    let asset = AVURLAsset(url: url)
    var seconds = 0.0
    let loaded = DispatchSemaphore(value: 0)
    Task {
        if let d = try? await asset.load(.duration), d.isNumeric { seconds = d.seconds }
        loaded.signal()
    }
    loaded.wait()
    let generator = AVAssetImageGenerator(asset: asset)
    generator.appliesPreferredTrackTransform = true
    generator.maximumSize = CGSize(width: o.maxPx, height: o.maxPx)
    let at = CMTime(seconds: seconds > 0 ? seconds * 0.1 : 0, preferredTimescale: 600)
    guard let frame = try? generator.copyCGImage(at: at, actualTime: nil) else { return ["ok": false, "error": "no poster frame"] }
    let (labels, _, _) = describeClassify(frame, o)
    return ["ok": true, "labels": labels, "text": "", "lines": 0, "ocr": false, "lang": ""]
}

func describeOne(_ item: [String: Any], _ o: DescribeOptions) -> [String: Any] {
    let started = Date()
    var out: [String: Any] = ["id": item["id"] ?? NSNull()]
    guard let path = item["path"] as? String, !path.isEmpty, path.hasPrefix("/") else {
        out["ok"] = false; out["error"] = "no absolute path"; return out
    }
    var st = stat()
    guard lstat(path, &st) == 0 else { out["ok"] = false; out["error"] = "missing"; return out }
    if st.st_flags & 0x40000000 != 0 {                          // SF_DATALESS: an iCloud placeholder, reading it downloads it
        out["ok"] = false; out["error"] = "dataless"; return out
    }
    let url = URL(fileURLWithPath: path)
    let result: [String: Any]
    switch (item["kind"] as? String) ?? "" {
    case "image": result = describeImage(url, o)
    case "pdf": result = describePDF(url, o)
    case "video": result = describeVideo(url, o)
    default: result = ["ok": false, "error": "unknown kind"]
    }
    out.merge(result) { _, new in new }
    out["ms"] = Int(Date().timeIntervalSince(started) * 1000)
    return out
}

func describeCommand(_ args: [String]) -> Never {
    var o = DescribeOptions()
    var stdinMode = false, probe = false
    var qos = "background"
    var nice: Int32 = 10
    var i = 0
    while i < args.count {
        let a = args[i]
        func value() -> String { i += 1; return i < args.count ? args[i] : "" }
        switch a {
        case "--probe": probe = true
        case "--stdin": stdinMode = true
        case "--max-px": o.maxPx = Int(value()) ?? o.maxPx
        case "--text-px": o.textPx = Int(value()) ?? o.textPx
        case "--pdf-px": o.pdfPx = Int(value()) ?? o.pdfPx
        case "--ocr": let v = value(); if ["gate", "always", "off"].contains(v) { o.ocr = v }
        case "--top": o.top = Int(value()) ?? o.top
        case "--min-conf": o.minConf = Float(value()) ?? o.minConf
        case "--qos": qos = value()
        case "--nice": nice = Int32(value()) ?? nice
        default: break
        }
        i += 1
    }
    if probe { emit(["describe": true, "version": 1]); exit(0) }
    guard stdinMode else { fail("describe: use --stdin (one JSON object per line) or --probe") }
    signal(SIGPIPE, SIG_IGN)
    setpriority(PRIO_PROCESS, 0, nice)
    _ = setiopolicy_np(IOPOL_TYPE_DISK, IOPOL_SCOPE_PROCESS, IOPOL_THROTTLE)
    let qosClass: UInt32 = qos == "background" ? 0x09 : (qos == "utility" ? 0x11 : 0x15)
    _ = pthread_set_qos_class_self_np(qos_class_t(qosClass), 0)
    while let line = readLine(strippingNewline: true) {
        if line.isEmpty { continue }
        autoreleasepool {
            var result: [String: Any]
            if let data = line.data(using: .utf8),
               let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                result = describeOne(obj, o)
            } else {
                result = ["id": NSNull(), "ok": false, "error": "bad input line"]
            }
            emit(result)
        }
    }
    exit(0)
}
