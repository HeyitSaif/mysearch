// Markdown to HTML for the Markdown reader. A hand-written renderer, in the spirit of (and adapted from) FinderFlow's
// `MarkdownToHTML`, extended with nested lists, task lists, table alignment, front matter, fenced and indented code,
// GitHub style heading ids, bare-URL links and hard line breaks.
//
// Safety: every character of the source is HTML-escaped (raw HTML in a document is shown as text, never run); link and
// image targets pass `safeURL` (http, https, mailto, file and relative paths only); the page carries a Content
// Security Policy that forbids scripts, and the reader loads it with JavaScript disabled.
//
// Adapted in part from FinderFlow v1.5.2 (FinderFlow/MarkdownReaderView.swift, `MarkdownToHTML`).
// FinderFlow is MIT licensed, Copyright (c) 2026 Gobinda Tarafdar. The full notice is in THIRD_PARTY_NOTICES.md.

import Foundation

final class MarkdownRenderer {
    private var slugCounts: [String: Int] = [:]

    /// A full HTML page. `baseDirectory` is the folder of the Markdown file: relative links and images resolve against it.
    static func page(markdown: String, title: String, baseDirectory: URL?) -> String {
        let r = MarkdownRenderer()
        var md = markdown.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
        var front = ""
        // YAML front matter at the very top is shown as a quiet block, not as a heading and a rule.
        if md.hasPrefix("---\n") {
            let lines = md.components(separatedBy: "\n")
            if let end = lines.indices.dropFirst().first(where: { lines[$0] == "---" || lines[$0] == "..." }) {
                front = "<pre class=\"frontmatter\"><code>\(escapeHTML(lines[1..<end].joined(separator: "\n")))</code></pre>\n"
                md = lines[(end + 1)...].joined(separator: "\n")
            }
        }
        let body = front + r.blocks(md)
        var base = ""
        if let dir = baseDirectory {
            var s = dir.absoluteString
            if !s.hasSuffix("/") { s += "/" }
            base = "<base href=\"\(escapeHTML(s))\">"
        }
        return template(title: title, base: base, body: body)
    }

    // MARK: blocks

    private static let reHeading = try! NSRegularExpression(pattern: #"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$"#)
    private static let reHR = try! NSRegularExpression(pattern: #"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$"#)
    private static let reList = try! NSRegularExpression(pattern: #"^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$"#)
    private static let reSeparator = try! NSRegularExpression(pattern: #"^[ \t]*\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$"#)
    private static let reFence = try! NSRegularExpression(pattern: #"^( {0,3})(`{3,}|~{3,})[ \t]*([^`]*)$"#)

    private func match(_ re: NSRegularExpression, _ s: String) -> NSTextCheckingResult? {
        re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s))
    }

    private func group(_ m: NSTextCheckingResult, _ i: Int, in s: String) -> String? {
        guard i < m.numberOfRanges, m.range(at: i).location != NSNotFound, let r = Range(m.range(at: i), in: s) else { return nil }
        return String(s[r])
    }

    private func isBlank(_ s: String) -> Bool { s.trimmingCharacters(in: .whitespaces).isEmpty }

    private func leadingSpaces(_ s: String) -> Int {
        var n = 0
        for c in s {
            if c == " " { n += 1 } else if c == "\t" { n += 4 - (n % 4) } else { break }
        }
        return n
    }

    private func isHR(_ s: String) -> Bool { match(Self.reHR, s) != nil }
    private func isHeading(_ s: String) -> Bool { match(Self.reHeading, s) != nil }
    private func isFence(_ s: String) -> Bool { match(Self.reFence, s) != nil }
    private func isQuote(_ s: String) -> Bool { s.drop(while: { $0 == " " }).hasPrefix(">") && leadingSpaces(s) < 4 }

    private struct ListMarker { var indent: Int; var ordered: Bool; var number: Int; var contentIndent: Int; var content: String }

    private func listMarker(_ s: String) -> ListMarker? {
        guard !isHR(s), let m = match(Self.reList, s), let ind = group(m, 1, in: s), let mark = group(m, 2, in: s) else { return nil }
        let content = group(m, 3, in: s) ?? ""
        let ordered = mark.first!.isNumber
        // column where the item's content starts: indent + marker + one space
        let contentIndent = ind.count + mark.count + 1
        return ListMarker(indent: ind.count, ordered: ordered, number: Int(mark.dropLast()) ?? 1, contentIndent: contentIndent, content: content)
    }

    private func isTableStart(_ lines: [String], _ i: Int) -> Bool {
        guard i + 1 < lines.count, lines[i].contains("|") else { return false }
        guard match(Self.reSeparator, lines[i + 1]) != nil, lines[i + 1].contains("-") else { return false }
        // a separator row made only of dashes (a setext underline) needs a pipe somewhere to count as a table
        return lines[i + 1].contains("|") || lines[i].contains("|")
    }

    /// True when `line` ends a paragraph (starts another block).
    private func interruptsParagraph(_ lines: [String], _ i: Int) -> Bool {
        let l = lines[i]
        if isBlank(l) || isHeading(l) || isFence(l) || isHR(l) || isQuote(l) { return true }
        if let m = listMarker(l), leadingSpaces(l) < 4, !m.content.isEmpty, (!m.ordered || m.number == 1) { return true }
        return false
    }

    private func blocks(_ md: String) -> String {
        let lines = md.components(separatedBy: "\n")
        var out = ""
        var i = 0
        while i < lines.count {
            let line = lines[i]
            if isBlank(line) { i += 1; continue }

            // fenced code
            if let m = match(Self.reFence, line), let fence = group(m, 2, in: line) {
                let info = (group(m, 3, in: line) ?? "").trimmingCharacters(in: .whitespaces)
                let ch = fence.first!
                var body: [String] = []
                i += 1
                while i < lines.count {
                    let t = lines[i].drop(while: { $0 == " " })
                    if leadingSpaces(lines[i]) < 4, t.hasPrefix(String(repeating: ch, count: fence.count)),
                       t.allSatisfy({ $0 == ch || $0 == " " || $0 == "\t" }) { break }
                    body.append(lines[i]); i += 1
                }
                if i < lines.count { i += 1 }
                var cls = ""
                if let lang = info.split(separator: " ").first {
                    let safe = lang.filter { $0.isLetter || $0.isNumber || "_+-#.".contains($0) }
                    if !safe.isEmpty { cls = " class=\"language-\(safe)\"" }
                }
                out += "<pre><code\(cls)>\(Self.escapeHTML(body.joined(separator: "\n")))</code></pre>\n"
                continue
            }

            // ATX heading
            if let m = match(Self.reHeading, line), let hashes = group(m, 1, in: line) {
                let text = (group(m, 2, in: line) ?? "").trimmingCharacters(in: .whitespaces)
                out += heading(level: hashes.count, text: text)
                i += 1; continue
            }

            // horizontal rule
            if isHR(line) { out += "<hr>\n"; i += 1; continue }

            // block quote
            if isQuote(line) {
                var inner: [String] = []
                while i < lines.count, isQuote(lines[i]) || (!isBlank(lines[i]) && !inner.isEmpty && !interruptsParagraph(lines, i) && !inner.last!.isEmpty) {
                    var l = lines[i]
                    if isQuote(l) {
                        l = String(l.drop(while: { $0 == " " }).dropFirst())
                        if l.hasPrefix(" ") { l.removeFirst() }
                    }
                    inner.append(l); i += 1
                }
                out += "<blockquote>\n\(blocks(inner.joined(separator: "\n")))</blockquote>\n"
                continue
            }

            // table
            if isTableStart(lines, i) {
                let header = splitRow(lines[i])
                let aligns = splitRow(lines[i + 1]).map { cell -> String in
                    let c = cell.trimmingCharacters(in: .whitespaces)
                    if c.hasPrefix(":") && c.hasSuffix(":") { return "center" }
                    if c.hasSuffix(":") { return "right" }
                    if c.hasPrefix(":") { return "left" }
                    return ""
                }
                i += 2
                func cell(_ tag: String, _ text: String, _ col: Int) -> String {
                    let a = col < aligns.count ? aligns[col] : ""
                    let style = a.isEmpty ? "" : " style=\"text-align:\(a)\""
                    return "<\(tag)\(style)>\(inline(text))</\(tag)>"
                }
                out += "<div class=\"tablewrap\"><table>\n<thead><tr>"
                for (c, h) in header.enumerated() { out += cell("th", h, c) }
                out += "</tr></thead>\n<tbody>\n"
                while i < lines.count, !isBlank(lines[i]), lines[i].contains("|") {
                    var cells = splitRow(lines[i])
                    while cells.count < header.count { cells.append("") }
                    out += "<tr>"
                    for (c, t) in cells.prefix(max(header.count, 1)).enumerated() { out += cell("td", t, c) }
                    out += "</tr>\n"; i += 1
                }
                out += "</tbody></table></div>\n"
                continue
            }

            // list
            if let first = listMarker(line), leadingSpaces(line) < 4 {
                out += list(lines, &i, first)
                continue
            }

            // indented code
            if leadingSpaces(line) >= 4 {
                var body: [String] = []
                while i < lines.count, isBlank(lines[i]) || leadingSpaces(lines[i]) >= 4 {
                    body.append(stripIndent(lines[i], 4)); i += 1
                }
                while let last = body.last, last.trimmingCharacters(in: .whitespaces).isEmpty { body.removeLast() }
                out += "<pre><code>\(Self.escapeHTML(body.joined(separator: "\n")))</code></pre>\n"
                continue
            }

            // paragraph (or setext heading)
            var para: [String] = []
            var setext: Int? = nil
            while i < lines.count {
                let l = lines[i]
                if !para.isEmpty {
                    let t = l.trimmingCharacters(in: .whitespaces)
                    if !t.isEmpty && t.allSatisfy({ $0 == "=" }) { setext = 1; i += 1; break }
                    if t.count >= 2 && t.allSatisfy({ $0 == "-" }) && leadingSpaces(l) < 4 { setext = 2; i += 1; break }
                    if interruptsParagraph(lines, i) || isTableStart(lines, i) { break }
                }
                para.append(l)
                i += 1
            }
            if let level = setext {
                out += heading(level: level, text: para.map { $0.trimmingCharacters(in: .whitespaces) }.joined(separator: " "))
            } else if !para.isEmpty {
                let joined = para.enumerated().map { idx, l -> String in
                    idx == para.count - 1 ? l.trimmingCharacters(in: .whitespaces) : l.replacingOccurrences(of: #"^[ \t]+"#, with: "", options: .regularExpression)
                }.joined(separator: "\n")
                out += "<p>\(inline(joined))</p>\n"
            }
        }
        return out
    }

    private func stripIndent(_ s: String, _ n: Int) -> String {
        var removed = 0
        var idx = s.startIndex
        while idx < s.endIndex, removed < n {
            if s[idx] == " " { removed += 1 } else if s[idx] == "\t" { removed += 4 } else { break }
            idx = s.index(after: idx)
        }
        return String(s[idx...])
    }

    private func heading(level: Int, text: String) -> String {
        var plain = text.replacingOccurrences(of: #"!?\[([^\]]*)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
        plain = plain.replacingOccurrences(of: #"[*_`~]"#, with: "", options: .regularExpression)
        var slug = String(plain.lowercased().filter { $0.isLetter || $0.isNumber || $0 == " " || $0 == "-" || $0 == "_" })
            .replacingOccurrences(of: " ", with: "-")
        if slug.isEmpty { slug = "section" }
        let n = slugCounts[slug, default: 0]
        slugCounts[slug] = n + 1
        let id = n == 0 ? slug : "\(slug)-\(n)"
        return "<h\(level) id=\"\(Self.escapeHTML(id))\">\(inline(text))</h\(level)>\n"
    }

    // MARK: lists

    private func list(_ lines: [String], _ i: inout Int, _ first: ListMarker) -> String {
        let ordered = first.ordered
        let indent = first.indent
        var html = ordered ? (first.number != 1 ? "<ol start=\"\(first.number)\">\n" : "<ol>\n") : "<ul>\n"
        func isSibling(_ nm: ListMarker) -> Bool { nm.indent >= indent && nm.indent <= indent + 1 && nm.ordered == ordered }
        while i < lines.count, let m = listMarker(lines[i]), isSibling(m) {
            var item: [String] = [m.content]
            var loose = false
            i += 1
            while i < lines.count {
                let l = lines[i]
                if isBlank(l) {
                    var j = i + 1
                    while j < lines.count && isBlank(lines[j]) { j += 1 }
                    if j < lines.count {
                        let nl = lines[j]
                        if let nm = listMarker(nl), isSibling(nm) {
                            loose = true; i = j; break                 // blank line, then the next item: a loose list
                        }
                        if leadingSpaces(nl) > indent + 1 {
                            for _ in i..<j { item.append("") }
                            loose = true; i = j; continue
                        }
                    }
                    break
                }
                let ls = leadingSpaces(l)
                if let nm = listMarker(l), nm.indent <= indent + 1 { break }          // next sibling, or a parent's item
                if ls > indent, ls >= min(m.contentIndent, indent + 2) {
                    item.append(stripIndent(l, min(ls, m.contentIndent))); i += 1; continue
                }
                // lazy continuation of the item's paragraph
                if listMarker(l) == nil, !interruptsParagraph(lines, i), let last = item.last, !last.isEmpty {
                    item.append(l.trimmingCharacters(in: .whitespaces)); i += 1; continue
                }
                break
            }
            // task list marker
            var task: Bool? = nil
            let head = item[0]
            if head == "[ ]" || head.hasPrefix("[ ] ") {
                task = false; item[0] = String(head.dropFirst(3)).trimmingCharacters(in: .whitespaces)
            } else if head == "[x]" || head == "[X]" || head.hasPrefix("[x] ") || head.hasPrefix("[X] ") {
                task = true; item[0] = String(head.dropFirst(3)).trimmingCharacters(in: .whitespaces)
            }
            var inner = blocks(item.joined(separator: "\n"))
            if !loose, inner.hasPrefix("<p>"), let end = inner.range(of: "</p>\n") {
                inner = String(inner[inner.index(inner.startIndex, offsetBy: 3)..<end.lowerBound]) + String(inner[end.upperBound...])
            }
            if let t = task {
                html += "<li class=\"task\"><input type=\"checkbox\" disabled\(t ? " checked" : "")> \(inner)</li>\n"
            } else {
                html += "<li>\(inner)</li>\n"
            }
        }
        html += ordered ? "</ol>\n" : "</ul>\n"
        return html
    }

    // MARK: tables

    private func splitRow(_ line: String) -> [String] {
        var s = line.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("|") { s.removeFirst() }
        var cells: [String] = []
        var cur = ""
        var inCode = false
        var prevBackslash = false
        for ch in s {
            if ch == "`" { inCode.toggle() }
            if ch == "|" && !inCode && !prevBackslash { cells.append(cur); cur = ""; prevBackslash = false; continue }
            if ch == "|" && prevBackslash && !inCode { cur.removeLast() }       // \| is a literal pipe
            cur.append(ch)
            prevBackslash = (ch == "\\")
        }
        if !cur.trimmingCharacters(in: .whitespaces).isEmpty || cells.isEmpty { cells.append(cur) }
        return cells.map { $0.trimmingCharacters(in: .whitespaces) }
    }

    // MARK: inline

    private static func rx(_ p: String) -> NSRegularExpression { try! NSRegularExpression(pattern: p) }
    private static let reImage = rx(#"!\[([^\]]*)\]\(\s*(<[^>]*>|(?:[^()\s]|\([^()\s]*\))*)(?:\s+"([^"]*)")?\s*\)"#)
    private static let reLink = rx(#"\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*(<[^>]*>|(?:[^()\s]|\([^()\s]*\))*)(?:\s+"([^"]*)")?\s*\)"#)
    private static let reAutolink = rx(#"<((?:https?://|mailto:)[^\s<>]+)>"#)
    private static let reBare = rx("(?<![\\w\"'=/@>])(https?://[^\\s<>\"'\u{FFFE}\u{FFFF}\u{FFFC}]+)")
    private static let emphasisRules: [(NSRegularExpression, String)] = [
        (rx(#"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*"#), "<strong><em>$1</em></strong>"),
        (rx(#"(?<!\w)___(?=\S)(.+?)(?<=\S)___(?!\w)"#), "<strong><em>$1</em></strong>"),
        (rx(#"\*\*(?=\S)(.+?)(?<=\S)\*\*"#), "<strong>$1</strong>"),
        (rx(#"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"#), "<strong>$1</strong>"),
        (rx(#"\*(?=\S)([^*\n]+?)(?<=\S)\*"#), "<em>$1</em>"),
        (rx(#"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)"#), "<em>$1</em>"),
        (rx(#"~~(?=\S)(.+?)(?<=\S)~~"#), "<del>$1</del>"),
    ]

    private func inline(_ input: String) -> String {
        var slots: [String] = []
        func slot(_ html: String) -> String {
            slots.append(html)
            return "\u{FFFE}\(slots.count - 1)\u{FFFF}"
        }
        let clean = input.replacingOccurrences(of: "\u{FFFE}", with: "").replacingOccurrences(of: "\u{FFFF}", with: "")
            .replacingOccurrences(of: "\u{FFFC}", with: "").replacingOccurrences(of: "  \n", with: "\u{FFFC}\n")

        // 1. escapes and code spans
        var out = ""
        let chars = Array(clean)
        var i = 0
        let escapable = Set("\\`*_{}[]()#+-.!|<>~&\"'")
        while i < chars.count {
            let c = chars[i]
            if c == "\\", i + 1 < chars.count {
                if chars[i + 1] == "\n" { out += "\u{FFFC}"; i += 1; continue }
                if escapable.contains(chars[i + 1]) { out += slot(Self.escapeHTML(String(chars[i + 1]))); i += 2; continue }
            }
            if c == "`" {
                var n = 0
                while i + n < chars.count, chars[i + n] == "`" { n += 1 }
                var j = i + n
                var found = -1
                while j < chars.count {
                    if chars[j] == "`" {
                        var m = 0
                        while j + m < chars.count, chars[j + m] == "`" { m += 1 }
                        if m == n { found = j; break }
                        j += m
                    } else { j += 1 }
                }
                if found >= 0 {
                    var code = String(chars[(i + n)..<found]).replacingOccurrences(of: "\n", with: " ")
                    if code.count > 2, code.hasPrefix(" "), code.hasSuffix(" ") { code = String(code.dropFirst().dropLast()) }
                    out += slot("<code>\(Self.escapeHTML(code))</code>")
                    i = found + n; continue
                }
                out += String(repeating: "`", count: n); i += n; continue
            }
            out.append(c); i += 1
        }

        // 2. images, 3. links, 4. autolinks (replaced back to front so the ranges stay valid)
        func replaceAll(_ re: NSRegularExpression, _ build: (NSTextCheckingResult, String) -> String?) {
            let ms = re.matches(in: out, range: NSRange(out.startIndex..., in: out)).reversed()
            for m in ms {
                guard let r = Range(m.range, in: out), let html = build(m, out) else { continue }
                out.replaceSubrange(r, with: slot(html))
            }
        }
        func dest(_ m: NSTextCheckingResult, _ s: String) -> String {
            var d = group(m, 2, in: s) ?? ""
            if d.hasPrefix("<"), d.hasSuffix(">") { d = String(d.dropFirst().dropLast()) }
            return d
        }
        replaceAll(Self.reImage) { m, s in
            let alt = group(m, 1, in: s) ?? ""
            guard let url = Self.safeURL(dest(m, s), allowMail: false) else { return Self.escapeHTML(alt) }
            let title = group(m, 3, in: s).map { " title=\"\(Self.escapeHTML($0))\"" } ?? ""
            return "<img src=\"\(Self.escapeHTML(url))\" alt=\"\(Self.escapeHTML(alt))\"\(title)>"
        }
        replaceAll(Self.reLink) { [self] m, s in
            let text = group(m, 1, in: s) ?? ""
            let inner = emphasize(Self.escapeHTML(text))
            guard let url = Self.safeURL(dest(m, s), allowMail: true) else { return inner }
            let title = group(m, 3, in: s).map { " title=\"\(Self.escapeHTML($0))\"" } ?? ""
            return "<a href=\"\(Self.escapeHTML(url))\"\(title)>\(inner)</a>"
        }
        replaceAll(Self.reAutolink) { m, s in
            let u = group(m, 1, in: s) ?? ""
            guard let url = Self.safeURL(u, allowMail: true) else { return nil }
            return "<a href=\"\(Self.escapeHTML(url))\">\(Self.escapeHTML(u))</a>"
        }
        // 5. bare http(s) URLs
        replaceAll(Self.reBare) { m, s in
            guard var u = group(m, 1, in: s) else { return nil }
            var tail = ""
            while let last = u.last {
                let opens = u.filter { $0 == "(" }.count, closes = u.filter { $0 == ")" }.count
                if ".,;:!?*_~".contains(last) || (last == ")" && closes > opens) { tail = String(last) + tail; u.removeLast() } else { break }
            }
            guard let url = Self.safeURL(u, allowMail: false) else { return nil }
            return "<a href=\"\(Self.escapeHTML(url))\">\(Self.escapeHTML(u))</a>\(Self.escapeHTML(tail))"
        }

        // 6. escape what is left, 7. emphasis, 8. line breaks
        var html = emphasize(Self.escapeHTML(out))
        html = html.replacingOccurrences(of: "\u{FFFC}", with: "<br>")

        // 9. put the slots back, last first (a link may contain a code span slot)
        var k = slots.count - 1
        while k >= 0 {
            html = html.replacingOccurrences(of: "\u{FFFE}\(k)\u{FFFF}", with: slots[k])
            k -= 1
        }
        return html
    }

    private func emphasize(_ s: String) -> String {
        var t = s
        for (re, tpl) in Self.emphasisRules {
            t = re.stringByReplacingMatches(in: t, range: NSRange(t.startIndex..., in: t), withTemplate: tpl)
        }
        return t
    }

    // MARK: URLs and escaping

    /// A link or image target the reader will keep: http, https, mailto (links only), file, an in-page anchor or a
    /// relative path. Everything else (javascript:, data:, vbscript:, custom schemes, protocol-relative) is dropped.
    static func safeURL(_ raw: String, allowMail: Bool) -> String? {
        var u = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        u = String(u.unicodeScalars.filter { $0.value >= 0x20 && $0.value != 0x7F })
        guard !u.isEmpty else { return nil }
        if u.hasPrefix("#") { return u }
        if u.hasPrefix("//") { return nil }
        if let m = try? NSRegularExpression(pattern: #"^([A-Za-z][A-Za-z0-9+.\-]*):"#).firstMatch(in: u, range: NSRange(u.startIndex..., in: u)),
           let r = Range(m.range(at: 1), in: u) {
            let scheme = u[r].lowercased()
            switch scheme {
            case "http", "https", "file": break
            case "mailto": if !allowMail { return nil }
            default: return nil
            }
        }
        return u.replacingOccurrences(of: " ", with: "%20")
    }

    static func escapeHTML(_ s: String) -> String {
        s.replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }

    // MARK: page template

    private static func template(title: String, base: String, body: String) -> String {
        """
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src file: http: https:; style-src 'unsafe-inline'">
        <meta name="color-scheme" content="light dark">
        \(base)
        <title>\(escapeHTML(title))</title>
        <style>
        :root {
          color-scheme: light dark;
          --bg: #ffffff; --text: #1d1d1f; --muted: #6e6e73; --border: #d2d2d7; --surface: #f5f5f7;
          --accent: #0a60ff; --code-bg: #f0f0f3; --code-fg: #b3261e; --quote-bar: #c7c7cc;
        }
        @media (prefers-color-scheme: dark) {
          :root {
            --bg: #1e1e1e; --text: #f2f2f7; --muted: #98989f; --border: #3d3d40; --surface: #28282a;
            --accent: #6cb0ff; --code-bg: #2c2c2e; --code-fg: #ff9f8f; --quote-bar: #55555a;
          }
        }
        html { background: var(--bg); }
        body { margin: 0 auto; max-width: 820px; padding: 28px 32px 72px; color: var(--text); background: var(--bg);
               font: 15px/1.65 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", sans-serif;
               -webkit-font-smoothing: antialiased; overflow-wrap: break-word; }
        h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 1.5em 0 .5em; font-weight: 650; }
        h1 { font-size: 1.9em; padding-bottom: .3em; border-bottom: 1px solid var(--border); }
        h2 { font-size: 1.45em; padding-bottom: .25em; border-bottom: 1px solid var(--border); }
        h3 { font-size: 1.2em; } h4 { font-size: 1.05em; } h5 { font-size: .95em; } h6 { font-size: .9em; color: var(--muted); }
        body > :first-child { margin-top: 0; }
        p { margin: .8em 0; }
        a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
        code { font: .86em ui-monospace, "SF Mono", Menlo, monospace; background: var(--code-bg); color: var(--code-fg);
               padding: .12em .35em; border-radius: 5px; }
        pre { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 14px 16px;
              overflow-x: auto; margin: 1em 0; line-height: 1.5; }
        pre code { background: none; color: var(--text); padding: 0; font-size: .85em; }
        pre.frontmatter { color: var(--muted); font-size: .9em; }
        pre.frontmatter code { color: var(--muted); }
        blockquote { margin: 1em 0; padding: .1em 1em; border-left: 3px solid var(--quote-bar); color: var(--muted); }
        blockquote > :first-child { margin-top: .5em; } blockquote > :last-child { margin-bottom: .5em; }
        ul, ol { padding-left: 1.7em; margin: .7em 0; } li { margin: .25em 0; }
        li > ul, li > ol { margin: .2em 0; }
        li.task { list-style: none; margin-left: -1.3em; }
        li.task input { margin-right: .45em; vertical-align: -1px; }
        hr { border: none; border-top: 1px solid var(--border); margin: 2em 0; }
        .tablewrap { overflow-x: auto; margin: 1em 0; }
        table { border-collapse: collapse; font-size: .95em; }
        th, td { border: 1px solid var(--border); padding: 6px 12px; vertical-align: top; }
        th { background: var(--surface); font-weight: 600; }
        tbody tr:nth-child(even) td { background: var(--surface); }
        img { max-width: 100%; height: auto; border-radius: 6px; }
        del { color: var(--muted); }
        ::selection { background: color-mix(in srgb, var(--accent) 30%, transparent); }
        </style>
        </head>
        <body>
        \(body)
        </body>
        </html>
        """
    }
}
