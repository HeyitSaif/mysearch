// Ask, native: the views (SwiftUI, hosted by AskViewController), in the style of Finder: system colours and materials
// (light and dark follow the system), a sidebar-style list of recent chats, SF fonts for the question and the answer.
// The transcript: the question, status dots with ⎿ result lines (small monospace) that fold into "3 steps · 2.1 s", a markdown answer with [n] chips,
// the sources, then Stop / Copy / Retry and a footer with the model. Under it the composer: a multi-line field
// (↵ asks, ⇧↵ new line), the folder chip and model menu (both drawn inside this window, so they can be snapshotted),
// the Cloud / Local switch. On the left, the recent chats.

import Cocoa
import PDFKit
import SwiftUI

// MARK: - small pieces

/// Lays its children out left to right and wraps them onto new lines (the footer's badges, the chips).
struct AskWrap: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let w = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineH: CGFloat = 0, maxX: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0, x + sz.width > w { x = 0; y += lineH + lineSpacing; lineH = 0 }
            x += sz.width + spacing; lineH = max(lineH, sz.height); maxX = max(maxX, x - spacing)
        }
        return CGSize(width: proposal.width ?? maxX, height: y + lineH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineH: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > bounds.minX, x + sz.width > bounds.maxX { x = bounds.minX; y += lineH + lineSpacing; lineH = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(sz))
            x += sz.width + spacing; lineH = max(lineH, sz.height)
        }
    }
}

extension Font {
    /// Monospace is kept only for the step lines and file paths.
    static func askMono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font { .system(size: size, weight: weight, design: .monospaced) }
}

/// A small status dot (the step lines, errors, hints).
struct AskDot: View {
    let color: Color
    var body: some View { Circle().fill(color).frame(width: 7, height: 7).alignmentGuide(.firstTextBaseline) { d in d[.bottom] + 1 } }
}

/// The sidebar material behind the recent chats, like Finder's sidebar.
struct AskSidebarBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material = .sidebar
        v.blendingMode = .behindWindow
        v.state = .followsWindowActiveState
        return v
    }
    func updateNSView(_ v: NSVisualEffectView, context: Context) {}
}

/// The small "cloud" / "local" badge.
struct AskWhereBadge: View {
    let local: Bool
    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: local ? "desktopcomputer" : "cloud").font(.system(size: 9.5, weight: .medium))
            Text(local ? "local" : "cloud").font(.system(size: 10.5, weight: .medium))
        }
        .padding(.horizontal, 6).padding(.vertical, 1.5)
        .background(RoundedRectangle(cornerRadius: 5).fill(local ? AskColors.okSoft : AskColors.glow))
        .foregroundStyle(local ? AskColors.ok : AskColors.prompt)
        .help(local ? "Answered on this Mac" : "Answered by a cloud model")
    }
}

struct AskTrainBadge: View {
    var body: some View {
        Text("⚠ \(AskText.trainText)")
            .font(.system(size: 10.5, weight: .medium))
            .padding(.horizontal, 6).padding(.vertical, 1.5)
            .background(RoundedRectangle(cornerRadius: 5).fill(AskColors.warnSoft))
            .foregroundStyle(AskColors.warn)
            .help(AskText.trainWhy)
    }
}

struct AskChipStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 9).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 6).fill(configuration.isPressed ? AskColors.hover : AskColors.well))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(AskColors.rule, lineWidth: 0.5))
            .contentShape(RoundedRectangle(cornerRadius: 6))
    }
}

struct AskGhostStyle: ButtonStyle {
    var primary = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: primary ? .semibold : .regular))
            .padding(.horizontal, 10).padding(.vertical, 4)
            .foregroundStyle(primary ? Color.white : AskColors.ink)
            .background(RoundedRectangle(cornerRadius: 6).fill(primary ? AskColors.prompt : (configuration.isPressed ? AskColors.hover : Color(nsColor: .controlColor))))
            .overlay(RoundedRectangle(cornerRadius: 6).stroke(primary ? Color.clear : AskColors.rule, lineWidth: 0.5))
            .contentShape(RoundedRectangle(cornerRadius: 6))
            .opacity(configuration.isPressed ? 0.8 : 1)
    }
}

// MARK: - markdown (port of the web page's answerBlocks)

enum AskBlock: Hashable {
    case paragraph(String)
    case heading(String)
    case list(ordered: Bool, items: [String])
    case code(String)

    static func parse(_ text: String) -> [AskBlock] {
        var out: [AskBlock] = []
        var para: [String] = []
        var list: (ordered: Bool, items: [String])?
        var code: [String]?
        func flushPara() { if !para.isEmpty { out.append(.paragraph(para.joined(separator: " "))); para = [] } }
        func flushList() { if let l = list { out.append(.list(ordered: l.ordered, items: l.items)); list = nil } }
        func match(_ pattern: String, _ line: String) -> String? {
            guard let r = try? NSRegularExpression(pattern: pattern),
                  let m = r.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)),
                  m.numberOfRanges > 1, let g = Range(m.range(at: m.numberOfRanges - 1), in: line) else { return nil }
            return String(line[g])
        }
        for raw in text.replacingOccurrences(of: "\r", with: "").components(separatedBy: "\n") {
            let line = raw
            let isFence = line.trimmingCharacters(in: .whitespaces).hasPrefix("```")
            if code != nil {
                if isFence { out.append(.code(code!.joined(separator: "\n"))); code = nil } else { code!.append(line) }
                continue
            }
            if isFence { flushPara(); flushList(); code = []; continue }
            if let h = match("^\\s*(#{1,4})\\s+(.*)$", line) { flushPara(); flushList(); out.append(.heading(h)); continue }
            let ul = match("^\\s*[-*•]\\s+(.*)$", line)
            let ol = ul == nil ? match("^\\s*\\d{1,2}[.)]\\s+(.*)$", line) : nil
            if let item = ul ?? ol {
                flushPara()
                let ordered = ul == nil
                if list == nil || list!.ordered != ordered { flushList(); list = (ordered, []) }
                list!.items.append(item)
            } else if line.trimmingCharacters(in: .whitespaces).isEmpty {
                flushPara(); flushList()
            } else {
                flushList(); para.append(line.trimmingCharacters(in: .whitespaces))
            }
        }
        if let c = code { out.append(.code(c.joined(separator: "\n"))) }
        flushPara(); flushList()
        return out
    }
}

enum AskMarkdown {
    /// Inline markdown (bold, italic, code, links) with [n] swapped for tappable chips. A half-streamed line that
    /// does not parse (an unclosed **) is shown as plain text until the rest arrives.
    static func inline(_ s: String, turn: String, sources: [AskSource], cursor: Bool = false) -> AttributedString {
        let text = cursor ? s + " ▍" : s
        var attr = (try? AttributedString(markdown: text, options: .init(allowsExtendedAttributes: false,
                    interpretedSyntax: .inlineOnlyPreservingWhitespace, failurePolicy: .returnPartiallyParsedIfPossible))) ?? AttributedString(text)
        let plain = String(attr.characters)
        guard let re = try? NSRegularExpression(pattern: "\\[(\\d{1,2})\\]") else { return attr }
        let known = Set(sources.map { $0.n })
        for m in re.matches(in: plain, range: NSRange(plain.startIndex..., in: plain)) {
            guard let whole = Range(m.range, in: plain), let g = Range(m.range(at: 1), in: plain),
                  let n = Int(plain[g]), known.contains(n) else { continue }
            let lo = attr.index(attr.startIndex, offsetByCharacters: plain.distance(from: plain.startIndex, to: whole.lowerBound))
            let hi = attr.index(attr.startIndex, offsetByCharacters: plain.distance(from: plain.startIndex, to: whole.upperBound))
            attr[lo..<hi].link = URL(string: "mysearch-cite://\(turn)/\(n)")
            attr[lo..<hi].font = .system(size: 12, weight: .semibold)
            attr[lo..<hi].backgroundColor = AskColors.glow
        }
        return attr
    }

    static func stripMarks(_ s: String) -> String {
        s.replacingOccurrences(of: "</?mark>", with: "", options: .regularExpression)
            .replacingOccurrences(of: "&amp;", with: "&").replacingOccurrences(of: "&lt;", with: "<")
            .replacingOccurrences(of: "&gt;", with: ">").replacingOccurrences(of: "&quot;", with: "\"")
    }
}

// MARK: - root

struct AskRootView: View {
    @ObservedObject var m: AskModel

    var body: some View {
        HStack(spacing: 0) {
            AskChatRail(m: m).frame(width: 230)
            Rectangle().fill(AskColors.rule).frame(width: 1)
            AskMain(m: m)
            if let s = m.openSource {
                Rectangle().fill(AskColors.rule).frame(width: 1)
                AskSourcePreview(m: m, source: s.source).frame(width: 380)
            }
        }
        .background(AskColors.bg)
    }
}

// MARK: - chats rail

struct AskChatRail: View {
    @ObservedObject var m: AskModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button { m.newChat() } label: {
                HStack(spacing: 8) {
                    Image(systemName: "square.and.pencil").font(.system(size: 13)).foregroundStyle(AskColors.prompt).frame(width: 18)
                    Text("New Chat").font(.system(size: 13))
                    Spacer()
                    Text("⌘N").font(.system(size: 11)).foregroundStyle(AskColors.dim)
                }
                .foregroundStyle(AskColors.ink)
                .padding(.horizontal, 8).padding(.vertical, 5)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.top, 12)
            Text("Recent").font(.system(size: 11, weight: .semibold)).foregroundStyle(AskColors.dim).padding(.top, 8).padding(.leading, 8).padding(.bottom, 2)
            if m.chats.isEmpty {
                Text("Your chats appear here. They stay on this Mac.").font(.system(size: 12)).foregroundStyle(AskColors.dim).padding(.horizontal, 8)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 1) {
                        ForEach(m.chats) { c in AskChatRow(m: m, chat: c) }
                    }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 10)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(AskSidebarBackground())
    }
}

struct AskChatRow: View {
    @ObservedObject var m: AskModel
    let chat: AskChat
    @State private var hover = false

    var body: some View {
        let on = chat.id == m.currentID
        HStack(alignment: .center, spacing: 2) {
            Button { m.open(chat.id) } label: {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "bubble.left").font(.system(size: 12)).foregroundStyle(on ? AskColors.prompt : AskColors.ink2).frame(width: 18).padding(.top, 1)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(chat.title.isEmpty ? "Untitled" : chat.title)
                            .font(.system(size: 13, weight: on ? .medium : .regular)).foregroundStyle(AskColors.ink)
                            .lineLimit(2).multilineTextAlignment(.leading)
                        Text("\(chat.turns.count) question\(chat.turns.count == 1 ? "" : "s") · \(AskText.ago(chat.updated))\(chat.route == "local" ? " · local" : "")")
                            .font(.system(size: 11)).foregroundStyle(AskColors.dim).lineLimit(1)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if hover || on {
                Button { m.remove(chat.id) } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .bold)).foregroundStyle(AskColors.dim).frame(width: 18, height: 18)
                }
                .buttonStyle(.plain).help("Remove this chat").opacity(hover ? 1 : 0.0)
            }
        }
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(RoundedRectangle(cornerRadius: 6).fill(on ? AskColors.selected : (hover ? AskColors.hover : Color.clear)))
        .onHover { hover = $0 }
        .contextMenu { Button("Remove Chat") { m.remove(chat.id) } }
    }
}

// MARK: - the main column

struct AskHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

struct AskMain: View {
    @ObservedObject var m: AskModel
    @State private var composerH: CGFloat = 120

    var body: some View {
        ZStack(alignment: .bottom) {
            VStack(spacing: 0) {
                AskHeader(m: m)
                AskThread(m: m)
                AskComposer(m: m)
                    .background(GeometryReader { g in Color.clear.preference(key: AskHeightKey.self, value: g.size.height) })
            }
            .onPreferenceChange(AskHeightKey.self) { composerH = $0 }
            if m.overlay != .none {
                Color.black.opacity(0.001).contentShape(Rectangle()).onTapGesture { m.overlay = .none }
                Group {
                    if m.overlay == .folder { AskFolderPicker(m: m) } else { AskModelMenu(m: m) }
                }
                .frame(maxWidth: .infinity, alignment: m.overlay == .model ? .trailing : .leading)
                .padding(.horizontal, 22)
                .padding(.bottom, composerH + 2)
            }
            if m.mentionOpen && !m.mentionItems.isEmpty {
                AskMentionList(m: m)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 22)
                    .padding(.bottom, composerH + 2)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct AskHeader: View {
    @ObservedObject var m: AskModel

    var body: some View {
        HStack(spacing: 10) {
            Text(m.currentChat?.title ?? "New chat")
                .font(.system(size: 13, weight: .semibold)).foregroundStyle(AskColors.ink).lineLimit(1)
            Spacer()
            if m.currentChat != nil {
                Button("+ New chat") { m.newChat() }.buttonStyle(AskGhostStyle()).help("Start a new chat (the context of this one is not carried over)")
            }
        }
        .padding(.horizontal, 22).padding(.vertical, 9)
        .background(AskColors.bg)
        .overlay(alignment: .bottom) { Rectangle().fill(AskColors.rule).frame(height: 1) }
    }
}

// MARK: - the transcript

struct AskThread: View {
    @ObservedObject var m: AskModel

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let c = m.currentChat, !c.turns.isEmpty {
                        ForEach(Array(c.turns.enumerated()), id: \.element.id) { i, t in
                            AskTurnView(m: m, turn: t, index: i, live: m.streaming && m.liveIndex == i)
                        }
                    } else {
                        AskEmptyState(m: m)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .frame(maxWidth: 860, alignment: .leading)
                .padding(.horizontal, 26).padding(.top, 14).padding(.bottom, 10)
                .frame(maxWidth: .infinity)
            }
            .environment(\.openURL, OpenURLAction { url in
                guard url.scheme == "mysearch-cite", let turn = url.host, let n = Int(url.lastPathComponent) else { return .systemAction }
                m.showSource(turn: turn, n: n)
                return .handled
            })
            .tint(AskColors.prompt)
            .background(AskColors.well)
            .onChange(of: m.tokenCount) { _, _ in if m.streaming { proxy.scrollTo("bottom", anchor: .bottom) } }
            .onChange(of: m.currentChat?.turns.count) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
            .onChange(of: m.currentChat?.turns.last?.steps.count) { _, _ in if m.streaming { proxy.scrollTo("bottom", anchor: .bottom) } }
            .onChange(of: m.currentID) { _, _ in proxy.scrollTo("bottom", anchor: .bottom) }
            .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
        }
    }
}

struct AskEmptyState: View {
    @ObservedObject var m: AskModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Image(systemName: "text.bubble").font(.system(size: 22, weight: .light)).foregroundStyle(AskColors.prompt)
                Text("Ask your documents").font(.system(size: 20, weight: .semibold)).foregroundStyle(AskColors.ink)
            }
            Text(message).font(.system(size: 13.5)).foregroundStyle(AskColors.ink2).fixedSize(horizontal: false, vertical: true)
            if m.statusFailed == false, m.status == nil, m.isBusy {
                Text("Connecting…").font(.system(size: 12)).foregroundStyle(AskColors.dim)
            }
            if let s = m.status, s.available, !s.enabled {
                Button("Turn on Ask") { m.enableAsk() }.buttonStyle(AskGhostStyle(primary: true))
            } else if m.needsModel {
                HStack(spacing: 8) {
                    AskDot(color: AskColors.warn)
                    Text("Choose a model").font(.system(size: 13, weight: .semibold)).foregroundStyle(AskColors.warn)
                    Text("— open the model menu under the message box.").font(.system(size: 13)).foregroundStyle(AskColors.dim)
                    Button("Choose…") { m.overlay = .model }.buttonStyle(AskGhostStyle())
                }
            } else if m.ready {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Try").font(.system(size: 11, weight: .semibold)).foregroundStyle(AskColors.dim)
                    ForEach(["What were the payment terms in the last contract?", "When does the licence expire?", "Summarise the meeting notes from last week"], id: \.self) { q in
                        Button { m.draft = q } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "arrow.turn.down.right").font(.system(size: 11)).foregroundStyle(AskColors.prompt)
                                Text(q).font(.system(size: 13)).foregroundStyle(AskColors.ink2)
                            }.contentShape(Rectangle())
                        }.buttonStyle(.plain)
                    }
                }.padding(.top, 4)
            }
        }
        .frame(maxWidth: 620, alignment: .leading)
        .padding(.top, 60)
    }

    var message: String {
        if m.statusFailed && m.status == nil { return "The search service is not running, so there is nothing to ask. Start it with mse service install." }
        guard let s = m.status else { return m.isBusy ? "" : "This search service has no Ask yet. Update MySearch, then come back here." }
        if !s.available { return "The answer model is not installed on this Mac, so Ask cannot be turned on. Search works as before." }
        if !s.enabled { return "Ask writes a short answer from your top search results, with numbered references to the documents it used. It is off: nothing is sent." }
        return "Type a question and get a short answer written from your top results, with numbered references to the documents it used. Pick where it reads with the folder chip, or type @."
    }
}

struct AskTurnView: View {
    @ObservedObject var m: AskModel
    let turn: AskTurn
    let index: Int
    let live: Bool

    var body: some View {
        let cited = Set(turn.citations)
        VStack(alignment: .leading, spacing: 0) {
            // the question
            Text(turn.q).font(.system(size: 15, weight: .semibold)).foregroundStyle(AskColors.ink).textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            if !turn.steps.isEmpty { AskSteps(m: m, turn: turn, live: live).padding(.top, 10) }
            if let e = turn.error {
                VStack(alignment: .leading, spacing: 1) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        AskDot(color: AskColors.err)
                        Text("Error").font(.system(size: 13, weight: .medium)).foregroundStyle(AskColors.err)
                    }
                    Text(e).font(.system(size: 13)).foregroundStyle(AskColors.err).textSelection(.enabled).padding(.leading, 15)
                }.padding(.top, 6)
            } else if turn.a.isEmpty {
                if !live { Text(turn.stopped ? "Stopped before an answer." : "No answer.").font(.system(size: 13)).foregroundStyle(AskColors.dim).padding(.top, 8) }
            } else {
                AskAnswer(turn: turn, streaming: live).padding(.top, 12)
            }
            if !turn.a.isEmpty, !turn.sources.isEmpty {
                VStack(alignment: .leading, spacing: 1) {
                    Text("Sources").font(.system(size: 11, weight: .semibold)).foregroundStyle(AskColors.dim).padding(.bottom, 3)
                    ForEach(turn.sources) { s in
                        AskSourceRow(m: m, turn: turn, source: s, dimmed: !cited.isEmpty && !cited.contains(s.n))
                    }
                }.padding(.top, 12)
            }
            HStack(alignment: .center, spacing: 6) {
                if live {
                    Button("Stop") { m.stop() }.buttonStyle(AskGhostStyle()).help("Stop (esc)")
                } else {
                    if !turn.a.isEmpty {
                        Button("Copy") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(m.copyText(of: turn), forType: .string)
                        }.buttonStyle(AskGhostStyle()).help("Copy the answer with its sources")
                    }
                    if !m.streaming {
                        Button("Retry") { m.retry(index) }.buttonStyle(AskGhostStyle()).help("Ask this question again")
                    }
                }
                Spacer(minLength: 8)
                if !live { AskMeta(m: m, turn: turn) }
            }.padding(.top, 10)
        }
        .padding(.top, index == 0 ? 6 : 14).padding(.bottom, 20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .top) { if index > 0 { Rectangle().fill(AskColors.rule).frame(height: 0.5) } }
    }
}

struct AskMeta: View {
    @ObservedObject var m: AskModel
    let turn: AskTurn

    var body: some View {
        AskWrap(spacing: 6, lineSpacing: 3) {
            if !turn.a.isEmpty, let model = turn.model {
                Text(model).font(.system(size: 11, weight: .medium)).foregroundStyle(AskColors.ink2)
                    .help(whereTitle)
            }
            if !turn.a.isEmpty, let w = turn.answeredWhere { AskWhereBadge(local: w != "cloud") }
            if !turn.a.isEmpty, turn.training { AskTrainBadge() }
            if !turn.a.isEmpty, let f = turn.fallback {
                Text(AskText.fallbackText(f)).font(.system(size: 11)).foregroundStyle(AskColors.warn)
            }
            if let ms = turn.ms, ms > 0 {
                Text(String(format: "%.1f s", Double(ms) / 1000)).font(.system(size: 11)).foregroundStyle(AskColors.dim)
            }
            if !turn.a.isEmpty { Text("check the sources").font(.system(size: 11)).foregroundStyle(AskColors.dim) }
        }
        .frame(maxWidth: .infinity, alignment: .trailing)
    }

    var whereTitle: String {
        let who = [m.providerLabel(turn.provider), turn.model ?? ""].filter { !$0.isEmpty }.joined(separator: " · ")
        return turn.answeredWhere == "cloud" ? "Answered in the cloud by \(who.isEmpty ? "a cloud model" : who)." : "Answered on this Mac."
    }
}

struct AskSteps: View {
    @ObservedObject var m: AskModel
    let turn: AskTurn
    let live: Bool

    var body: some View {
        let n = turn.steps.count
        let secs = (turn.ms ?? 0) > 0 ? String(format: " · %.1f s", Double(turn.ms ?? 0) / 1000) : ""
        let label = "\(n) step\(n == 1 ? "" : "s")\(secs)"
        let open = live || m.expanded.contains(turn.id)
        VStack(alignment: .leading, spacing: 0) {
            if !live {
                Button {
                    if open { m.expanded.remove(turn.id) } else { m.expanded.insert(turn.id) }
                } label: {
                    HStack(spacing: 6) {
                        if !open { AskDot(color: turn.steps.contains { $0.state == .err } ? AskColors.err : AskColors.ok) }
                        else { Image(systemName: "chevron.down").font(.system(size: 9, weight: .semibold)).foregroundStyle(AskColors.dim) }
                        Text(label).font(.askMono(11)).foregroundStyle(AskColors.dim)
                        if !open { Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(AskColors.dim) }
                    }.contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(open ? "Hide the steps" : "Show the steps")
            }
            if open {
                ForEach(turn.steps) { s in AskStepLine(step: s) }.padding(.top, live ? 0 : 2)
            }
        }
    }
}

struct AskStepLine: View {
    let step: AskStep

    var body: some View {
        let color: Color = step.state == .err ? AskColors.err : step.state == .run ? AskColors.run : AskColors.ok
        let res = [step.res, step.ms.map { AskText.fmtMs($0) }].compactMap { $0 }.joined(separator: " · ")
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                AskDot(color: color)
                Text(step.title).font(.askMono(11)).foregroundStyle(step.state == .err ? AskColors.err : AskColors.ink2)
                    .lineLimit(2).textSelection(.enabled)
            }
            if !res.isEmpty {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("⎿").font(.askMono(11)).foregroundStyle(AskColors.dim)
                    Text(res).font(.askMono(11)).foregroundStyle(AskColors.dim)
                }.padding(.leading, 15)
            }
        }
        .padding(.vertical, 1.5)
    }
}

struct AskAnswer: View {
    let turn: AskTurn
    let streaming: Bool

    var body: some View {
        let blocks = AskBlock.parse(turn.a)
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { i, b in
                let last = streaming && i == blocks.count - 1
                switch b {
                case .paragraph(let s):
                    Text(AskMarkdown.inline(s, turn: turn.id, sources: turn.sources, cursor: last)).textSelection(.enabled)
                case .heading(let s):
                    Text(AskMarkdown.inline(s, turn: turn.id, sources: turn.sources, cursor: last)).font(.system(size: 14.5, weight: .semibold)).textSelection(.enabled)
                case .list(let ordered, let items):
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(items.enumerated()), id: \.offset) { j, it in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(ordered ? "\(j + 1)." : "•").foregroundStyle(AskColors.dim).frame(minWidth: 16, alignment: .trailing)
                                Text(AskMarkdown.inline(it, turn: turn.id, sources: turn.sources, cursor: last && j == items.count - 1)).textSelection(.enabled)
                            }
                        }
                    }
                case .code(let s):
                    Text(s).font(.askMono(12)).foregroundStyle(AskColors.ink).textSelection(.enabled)
                        .padding(10).frame(maxWidth: .infinity, alignment: .leading)
                        .background(RoundedRectangle(cornerRadius: 8).fill(AskColors.panel))
                        .overlay(RoundedRectangle(cornerRadius: 8).stroke(AskColors.rule, lineWidth: 0.5))
                }
            }
            if streaming && blocks.isEmpty { Text("▍").foregroundStyle(AskColors.prompt) }
        }
        .font(.system(size: 14.5))
        .lineSpacing(4)
        .foregroundStyle(AskColors.ink)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct AskSourceRow: View {
    @ObservedObject var m: AskModel
    let turn: AskTurn
    let source: AskSource
    let dimmed: Bool
    @State private var hover = false

    var body: some View {
        Button { m.openSource = (turn.id, source) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("[\(source.n)]").font(.system(size: 12, weight: .semibold)).foregroundStyle(AskColors.prompt)
                (Text("\(source.collection) / ").foregroundStyle(AskColors.dim) + Text(source.path).foregroundStyle(hover ? AskColors.ink : AskColors.ink2))
                    .font(.askMono(11)).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 8)
                if let p = source.page { Text("p. \(p)").font(.system(size: 11)).foregroundStyle(AskColors.dim) }
                else if let ts = source.ts { Text(ts).font(.system(size: 11)).foregroundStyle(AskColors.dim) }
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .opacity(dimmed ? 0.6 : 1)
        .onHover { hover = $0 }
        .help("\(source.collection)/\(source.path)\(source.page.map { ", page \($0)" } ?? "")")
    }
}

// MARK: - the composer

struct AskComposer: View {
    @ObservedObject var m: AskModel
    @State private var fieldH: CGFloat = 22

    var body: some View {
        let pred = m.predict()
        VStack(alignment: .leading, spacing: 6) {
            VStack(alignment: .leading, spacing: 8) {
                ZStack(alignment: .topLeading) {
                    AskComposerField(
                        text: $m.draft, height: $fieldH, enabled: m.ready, focusOnAppear: !m.snapshot,
                        onSend: { m.send(m.draft); if m.ready { m.draft = "" } },
                        onKey: { handle($0) },
                        onCaret: { t, c in m.updateMention(text: t, caret: c) })
                    if m.draft.isEmpty {
                        Text(m.ready ? (m.currentChat?.turns.isEmpty == false ? "Follow up…" : "Ask about your documents") : "Ask is off")
                            .font(.system(size: 14)).foregroundStyle(AskColors.dim).padding(.top, 4).allowsHitTesting(false)
                    }
                }
                .frame(height: min(max(fieldH, 24), 168))
                HStack(spacing: 8) {
                    Button { m.overlay == .folder ? (m.overlay = .none) : m.openPicker() } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "folder").font(.system(size: 11))
                            Text(m.whereLabel).font(.system(size: 12)).lineLimit(1).truncationMode(.middle).frame(maxWidth: 260)
                            Image(systemName: "chevron.up.chevron.down").font(.system(size: 8, weight: .semibold)).foregroundStyle(AskColors.dim)
                        }.foregroundStyle(AskColors.ink2).fixedSize()
                    }
                    .buttonStyle(AskChipStyle()).disabled(!m.ready)
                    .help("This chat reads \(m.whereLabel). Click to choose another folder, or type @ to mention one.")
                    if pred != nil && m.ready { AskRouteSwitch(m: m) }
                    Spacer(minLength: 6)
                    if m.cloudAPI && m.routeKnown && m.ready { AskModelChip(m: m) }
                    AskSendButton(m: m, onSend: { m.send(m.draft); if m.ready { m.draft = "" } })
                }
            }
            .padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 8)
            .background(RoundedRectangle(cornerRadius: 10).fill(AskColors.well))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(AskColors.rule, lineWidth: 0.5))
            AskNote(m: m, pred: pred)
        }
        .padding(.horizontal, 22).padding(.top, 10).padding(.bottom, 12)
        .overlay(alignment: .top) { Rectangle().fill(AskColors.rule).frame(height: 0.5) }
        .background(AskColors.bg)
    }

    /// Keys the composer hands up: the @ list, Esc.
    func handle(_ e: NSEvent) -> Bool {
        let code = e.keyCode
        if m.mentionOpen, !m.mentionItems.isEmpty {
            switch code {
            case 125: m.mentionSel = min(m.mentionSel + 1, m.mentionItems.count - 1); return true
            case 126: m.mentionSel = max(m.mentionSel - 1, 0); return true
            case 36, 76, 48: m.acceptMention(m.mentionSel); return true
            default: break
            }
        }
        if code == 53 {
            if m.mentionOpen { m.closeMention(); return true }
            if m.overlay != .none { m.overlay = .none; return true }
            if m.streaming { m.stop(); return true }
        }
        return false
    }
}

struct AskSendButton: View {
    @ObservedObject var m: AskModel
    let onSend: () -> Void

    var body: some View {
        let canSend = m.ready && !m.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        Button {
            if m.streaming { m.stop() } else { onSend() }
        } label: {
            Image(systemName: m.streaming ? "stop.fill" : "arrow.up")
                .font(.system(size: 12, weight: .bold)).foregroundStyle(Color.white)
                .frame(width: 26, height: 26)
                .background(Circle().fill(m.streaming || canSend ? AskColors.prompt : AskColors.run))
        }
        .buttonStyle(.plain)
        .disabled(!m.streaming && !canSend)
        .help(m.streaming ? "Stop (esc)" : "Ask (↵)")
    }
}

struct AskRouteSwitch: View {
    @ObservedObject var m: AskModel

    var body: some View {
        let route = m.chatRoute
        let settingsLocal = m.cfg.route == "local"
        HStack(spacing: 0) {
            seg("Cloud", on: route == "cloud" && !settingsLocal, help: settingsLocal ? "Switch back to the cloud model chosen in the menu" : "Answered by the cloud model chosen in the menu") { m.setChatRoute("cloud") }
            seg("Local", on: route == "local" || settingsLocal, help: "Answered on this Mac by \(m.localModels.current ?? m.status?.localModel ?? "the local model"); nothing is sent") { m.setChatRoute("local") }
        }
        .padding(2)
        .background(RoundedRectangle(cornerRadius: 7).fill(AskColors.hover))
        .overlay(RoundedRectangle(cornerRadius: 7).stroke(AskColors.rule, lineWidth: 0.5))
        .disabled(m.streaming)
    }

    func seg(_ title: String, on: Bool, help: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(.system(size: 11.5, weight: on ? .semibold : .regular)).lineLimit(1).fixedSize()
                .foregroundStyle(on ? AskColors.ink : AskColors.dim)
                .padding(.horizontal, 10).padding(.vertical, 2.5)
                .background(RoundedRectangle(cornerRadius: 5).fill(on ? AskColors.well : Color.clear))
                .shadow(color: on ? Color.black.opacity(0.12) : .clear, radius: 1, y: 0.5)
        }
        .buttonStyle(.plain).help(help)
    }
}

struct AskModelChip: View {
    @ObservedObject var m: AskModel

    var body: some View {
        let sel = m.selection
        Button { m.toggle(.model) } label: {
            HStack(spacing: 6) {
                if let s = sel {
                    Text(s.model).font(.system(size: 12, weight: .medium)).lineLimit(1).truncationMode(.middle)
                    Text("· \(s.local ? "On this Mac" : m.providerLabel(s.provider))").font(.system(size: 11)).foregroundStyle(AskColors.dim).lineLimit(1)
                    AskWhereBadge(local: s.local)
                    if m.trains(s.provider, s.model) { Text("⚠").font(.system(size: 11)).foregroundStyle(AskColors.warn) }
                } else {
                    Text("Choose a model").font(.system(size: 11.5, weight: .medium)).foregroundStyle(AskColors.warn)
                }
                Image(systemName: "chevron.up.chevron.down").font(.system(size: 8, weight: .semibold)).foregroundStyle(AskColors.dim)
            }
            .foregroundStyle(AskColors.ink2)
        }
        .buttonStyle(AskChipStyle())
        .layoutPriority(-1)
        .help(sel.map { "\($0.model) · \($0.local ? "on this Mac" : m.providerLabel($0.provider))" } ?? "Choose the model that writes the answers")
    }
}

struct AskNote: View {
    @ObservedObject var m: AskModel
    let pred: AskPrediction?

    var body: some View {
        AskWrap(spacing: 5, lineSpacing: 2) {
            if !m.ready {
                Text("Turn Ask on to ask questions.").foregroundStyle(AskColors.dim)
            } else {
                let sc = Text("Reads \(m.whereLabel) · ↵ ask · ⇧↵ new line · @ mention a folder")
                sc.foregroundStyle(AskColors.dim)
                if let p = pred {
                    Text("·").foregroundStyle(AskColors.dim)
                    if !p.local, let prob = p.problem {
                        (Text("next answer: ").foregroundStyle(AskColors.dim) + Text(AskText.problem[prob] ?? "no cloud model is ready").foregroundStyle(AskColors.warn).bold()
                         + Text(" · pick a model, or switch to Local").foregroundStyle(AskColors.dim))
                    } else {
                        Text("next answer").foregroundStyle(AskColors.dim)
                        AskWhereBadge(local: p.local)
                        Text(p.model).bold().foregroundStyle(AskColors.ink2)
                        if !p.local, m.trains(m.cfg.provider, p.model) { AskTrainBadge() }
                    }
                }
            }
        }
        .font(.system(size: 11))
        .padding(.horizontal, 4)
    }
}

// MARK: - the text field (an NSTextView: ↵ sends, ⇧↵ adds a line, the @ list and Esc come through)

final class AskNSTextView: NSTextView {
    var keyHook: ((NSEvent) -> Bool)?
    var wantsFocus = false

    override func keyDown(with event: NSEvent) {
        if keyHook?(event) == true { return }
        super.keyDown(with: event)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if wantsFocus, let w = window { DispatchQueue.main.async { w.makeFirstResponder(self) } }
    }
}

struct AskComposerField: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    var enabled: Bool
    var focusOnAppear: Bool
    var onSend: () -> Void
    var onKey: (NSEvent) -> Bool
    var onCaret: (String, Int) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = false
        scroll.hasHorizontalScroller = false
        scroll.borderType = .noBorder
        let tv = AskNSTextView()
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        tv.drawsBackground = false
        tv.font = NSFont.systemFont(ofSize: 14)
        tv.textColor = .labelColor
        tv.insertionPointColor = .labelColor
        tv.textContainerInset = NSSize(width: 0, height: 4)
        tv.textContainer?.lineFragmentPadding = 0
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false
        tv.isAutomaticSpellingCorrectionEnabled = false
        tv.wantsFocus = focusOnAppear
        tv.keyHook = { [weak coord = context.coordinator] e in coord?.handleKey(e) ?? false }
        tv.string = text
        scroll.documentView = tv
        context.coordinator.textView = tv
        context.coordinator.measure()
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let tv = context.coordinator.textView else { return }
        tv.isEditable = enabled
        if tv.string != text {
            tv.string = text
            tv.setSelectedRange(NSRange(location: (text as NSString).length, length: 0))
            context.coordinator.measure()
        }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: AskComposerField
        weak var textView: AskNSTextView?
        init(_ p: AskComposerField) { parent = p }

        func textDidChange(_ n: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
            measure()
            parent.onCaret(tv.string, tv.selectedRange().location)
        }

        func textViewDidChangeSelection(_ n: Notification) {
            guard let tv = textView, tv.string == parent.text else { return }
            parent.onCaret(tv.string, tv.selectedRange().location)
        }

        func measure() {
            guard let tv = textView, let lm = tv.layoutManager, let tc = tv.textContainer else { return }
            lm.ensureLayout(for: tc)
            let h = ceil(lm.usedRect(for: tc).height) + tv.textContainerInset.height * 2
            if abs(h - parent.height) > 0.5 { DispatchQueue.main.async { self.parent.height = h } }
        }

        /// ↵ asks, ⇧↵ (or ⌥↵) adds a line. Everything else goes up to the view (the @ list, Esc).
        func handleKey(_ e: NSEvent) -> Bool {
            guard let tv = textView else { return false }
            if parent.onKey(e) { return true }
            if (e.keyCode == 36 || e.keyCode == 76), !tv.hasMarkedText() {
                if e.modifierFlags.intersection([.shift, .option]).isEmpty {
                    parent.onSend()
                    return true
                }
                tv.insertNewlineIgnoringFieldEditor(nil)
                return true
            }
            return false
        }
    }
}

// MARK: - the folder picker (an overlay, not a popover: it stays inside the window)

struct AskFolderPicker: View {
    @ObservedObject var m: AskModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // breadcrumbs
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 4) {
                    ForEach(Array(m.pickerCrumbs.enumerated()), id: \.offset) { i, c in
                        if i > 0 { Text("›").foregroundStyle(AskColors.dim) }
                        let last = i == m.pickerCrumbs.count - 1
                        Button(c.label) { m.pickerGo(c.scope, c.path) }
                            .buttonStyle(.plain).font(.system(size: 12, weight: last ? .semibold : .regular))
                            .foregroundStyle(last ? AskColors.ink : AskColors.prompt)
                    }
                }.padding(.horizontal, 12).padding(.vertical, 9)
            }
            Rectangle().fill(AskColors.rule).frame(height: 1)
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.system(size: 11)).foregroundStyle(AskColors.dim)
                TextField("Type to filter", text: $m.pickerFilter).textFieldStyle(.plain).font(.system(size: 12.5))
                    .focused($focused).onSubmit { openFirst() }
            }.padding(.horizontal, 12).padding(.vertical, 7)
            Rectangle().fill(AskColors.rule).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) { rows }.padding(.vertical, 4)
            }.frame(maxHeight: 260)
            Rectangle().fill(AskColors.rule).frame(height: 1)
            HStack {
                Text(useLabel).font(.system(size: 11)).foregroundStyle(AskColors.dim).lineLimit(1).truncationMode(.middle)
                Spacer()
                Button("Use this folder") { use(m.pickerScope ?? "all", m.pickerPath) }.buttonStyle(AskGhostStyle(primary: true))
            }.padding(.horizontal, 12).padding(.vertical, 8)
        }
        .frame(width: 430)
        .background(RoundedRectangle(cornerRadius: 10).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(AskColors.rule, lineWidth: 0.5))
        .shadow(color: Color.black.opacity(0.22), radius: 14, y: 5)
        .onAppear { if !m.snapshot { focused = true } }
    }

    var useLabel: String { "Reads: \(m.label(scope: m.pickerScope ?? "all", path: m.pickerPath))" }

    func use(_ scope: String, _ path: String) { m.pickerUse(scope: scope, path: path) }

    var filterTerms: [String] { m.pickerFilter.lowercased().split(separator: " ").map(String.init) }
    func matches(_ s: String) -> Bool { filterTerms.allSatisfy { s.lowercased().contains($0) } }

    func openFirst() {
        if let s = m.pickerScope {
            if let f = m.pickerRows.first(where: { matches($0.name) }) { f.children ? m.pickerGo(s, f.path) : use(s, f.path) }
        } else if let c = (m.library?.collections ?? []).first(where: { $0.enabled && $0.exists && matches($0.name) }) {
            m.pickerGo(c.name)
        }
    }

    @ViewBuilder var rows: some View {
        if let s = m.pickerScope {
            if m.pickerLoading { Text("Loading…").font(.system(size: 12)).foregroundStyle(AskColors.dim).padding(12) }
            let list = m.pickerRows.filter { matches($0.name) }
            if !m.pickerLoading && list.isEmpty {
                Text(m.pickerRows.isEmpty ? "No subfolders." : "No folder matches.").font(.system(size: 12)).foregroundStyle(AskColors.dim).padding(12)
            }
            ForEach(list) { f in
                pickRow(icon: "folder", title: f.name, sub: nil, chevron: f.children,
                        open: { f.children ? m.pickerGo(s, f.path) : use(s, f.path) }, use: { use(s, f.path) })
            }
        } else {
            if matches("all folders") { pickRow(icon: "tray.full", title: "All folders", sub: nil, chevron: false, open: { use("all", "") }, use: nil) }
            let groups = (m.library?.groups ?? []).filter { matches($0.name) }
            if !groups.isEmpty { header("Groups") }
            ForEach(groups, id: \.name) { g in
                pickRow(icon: "square.stack.3d.up", title: g.name, sub: "\(g.members.count) folders", chevron: false, open: { use(g.name, "") }, use: nil)
            }
            let cols = (m.library?.collections ?? []).filter { $0.enabled && $0.exists && matches($0.name) }
            if !cols.isEmpty { header("Folders") }
            ForEach(cols, id: \.name) { c in
                pickRow(icon: "folder", title: c.name, sub: c.files > 0 ? "\(c.files) files" : nil, chevron: true,
                        open: { m.pickerGo(c.name) }, use: { use(c.name, "") })
            }
        }
    }

    func header(_ t: String) -> some View {
        Text(t).font(.system(size: 11, weight: .semibold)).foregroundStyle(AskColors.dim).padding(.horizontal, 12).padding(.top, 8).padding(.bottom, 2)
    }

    func pickRow(icon: String, title: String, sub: String?, chevron: Bool, open: @escaping () -> Void, use: (() -> Void)?) -> some View {
        AskPickRow(icon: icon, title: title, sub: sub, chevron: chevron, open: open, use: use)
    }
}

struct AskPickRow: View {
    let icon: String
    let title: String
    let sub: String?
    let chevron: Bool
    let open: () -> Void
    let use: (() -> Void)?
    @State private var hover = false

    var body: some View {
        HStack(spacing: 8) {
            Button(action: open) {
                HStack(spacing: 8) {
                    Image(systemName: icon).font(.system(size: 12)).foregroundStyle(AskColors.prompt).frame(width: 16)
                    Text(title).font(.system(size: 12.5)).foregroundStyle(AskColors.ink).lineLimit(1)
                    if let sub { Text(sub).font(.system(size: 11)).foregroundStyle(AskColors.dim) }
                    Spacer(minLength: 4)
                    if chevron { Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold)).foregroundStyle(AskColors.dim) }
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            if let use, chevron {
                Button("Use", action: use).buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(AskColors.prompt).opacity(hover ? 1 : 0.55)
                    .help("Read this folder, without opening it")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 5)
        .background(hover ? AskColors.hover : Color.clear)
        .onHover { hover = $0 }
    }
}

// MARK: - the model menu

struct AskModelMenu: View {
    @ObservedObject var m: AskModel

    var body: some View {
        let sections = m.modelSections()
        let terms = m.modelFilter.lowercased().split(separator: " ").map(String.init)
        let total = sections.flatMap { $0.subs.flatMap { $0.items } }.count
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").font(.system(size: 11)).foregroundStyle(AskColors.dim)
                TextField("Search \(total) model\(total == 1 ? "" : "s")", text: $m.modelFilter).textFieldStyle(.plain).font(.system(size: 12.5))
            }.padding(.horizontal, 12).padding(.vertical, 8)
            Rectangle().fill(AskColors.rule).frame(height: 1)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if sections.isEmpty {
                        Text("No provider is connected yet: add a key in Settings › Ask (Classic MySearch).").font(.system(size: 12)).foregroundStyle(AskColors.dim).padding(12)
                    }
                    ForEach(sections) { sec in
                        let visible = sec.subs.map { sub in (sub.sub, sub.items.filter { it in
                            terms.allSatisfy { "\(it.model) \(it.name) \(sec.head) \(sub.sub ?? "")".lowercased().contains($0) } }) }.filter { !$0.1.isEmpty }
                        if !visible.isEmpty {
                            HStack(spacing: 6) {
                                Text(sec.head).font(.system(size: 11.5, weight: .semibold)).foregroundStyle(AskColors.ink)
                                if let n = sec.note { Text(n).font(.system(size: 10.5)).foregroundStyle(AskColors.dim) }
                            }.padding(.horizontal, 12).padding(.top, 9).padding(.bottom, 2)
                            ForEach(Array(visible.enumerated()), id: \.offset) { _, v in
                                if let s = v.0 { Text(s).font(.system(size: 10.5, weight: .medium)).foregroundStyle(AskColors.dim).padding(.horizontal, 12).padding(.top, 4) }
                                ForEach(v.1) { it in AskModelRow(m: m, entry: it) }
                            }
                        }
                    }
                }.padding(.bottom, 6)
            }.frame(maxHeight: 340)
            if sections.flatMap({ $0.subs.flatMap { $0.items } }).contains(where: { m.trains($0.provider, $0.model) }) {
                Rectangle().fill(AskColors.rule).frame(height: 1)
                Text("⚠ \(AskText.trainText). You confirm each marked model once.")
                    .font(.system(size: 10.5)).foregroundStyle(AskColors.warn).padding(.horizontal, 12).padding(.vertical, 6)
            }
        }
        .frame(width: 440)
        .background(RoundedRectangle(cornerRadius: 10).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(AskColors.rule, lineWidth: 0.5))
        .shadow(color: Color.black.opacity(0.22), radius: 14, y: 5)
    }
}

struct AskModelRow: View {
    @ObservedObject var m: AskModel
    let entry: AskModelEntry
    @State private var hover = false

    var body: some View {
        let cur = m.isCurrent(entry)
        Button { m.choose(entry) } label: {
            HStack(spacing: 8) {
                Text(entry.model).font(.system(size: 12.5, weight: cur ? .semibold : .regular)).foregroundStyle(AskColors.ink).lineLimit(1).truncationMode(.middle)
                if !entry.name.isEmpty { Text(entry.name).font(.system(size: 11)).foregroundStyle(AskColors.dim) }
                Spacer(minLength: 4)
                if m.trains(entry.provider, entry.model) { AskTrainBadge() }
                if cur { Image(systemName: "checkmark").font(.system(size: 10, weight: .bold)).foregroundStyle(AskColors.prompt) }
            }
            .padding(.horizontal, 12).padding(.vertical, 4)
            .background(hover ? AskColors.hover : Color.clear)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hover = $0 }
    }
}

// MARK: - @ mentions

struct AskMentionList: View {
    @ObservedObject var m: AskModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Folders and groups").font(.system(size: 10.5, weight: .semibold)).foregroundStyle(AskColors.dim)
                Spacer()
                Text("↵ read · esc").font(.system(size: 10.5)).foregroundStyle(AskColors.dim)
            }.padding(.horizontal, 12).padding(.vertical, 6)
            Rectangle().fill(AskColors.rule).frame(height: 1)
            ForEach(Array(m.mentionItems.enumerated()), id: \.element.id) { i, it in
                Button { m.acceptMention(i) } label: {
                    HStack {
                        Text(it.label).font(.system(size: 12.5)).foregroundStyle(AskColors.ink).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        Text(it.sub).font(.system(size: 10.5)).foregroundStyle(AskColors.dim)
                    }
                    .padding(.horizontal, 12).padding(.vertical, 4)
                    .background(i == m.mentionSel ? AskColors.prompt.opacity(0.25) : Color.clear)
                    .contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
        }
        .frame(width: 430)
        .background(RoundedRectangle(cornerRadius: 10).fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(AskColors.rule, lineWidth: 0.5))
        .shadow(color: Color.black.opacity(0.2), radius: 12, y: 4)
    }
}

// MARK: - the source preview (a PDF opens at its page, other files show the passage)

struct AskPDFPage: NSViewRepresentable {
    let url: URL
    let page: Int

    func makeNSView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        v.displayMode = .singlePageContinuous
        v.displaysPageBreaks = false
        v.backgroundColor = .clear
        load(v)
        return v
    }

    func updateNSView(_ v: PDFView, context: Context) {
        if v.document?.documentURL != url { load(v) } else { go(v) }
    }

    private func load(_ v: PDFView) {
        v.document = PDFDocument(url: url)
        go(v)
    }

    private func go(_ v: PDFView) {
        guard let doc = v.document, doc.pageCount > 0 else { return }
        if let p = doc.page(at: min(max(page - 1, 0), doc.pageCount - 1)) { v.go(to: p) }
    }
}

struct AskSourcePreview: View {
    @ObservedObject var m: AskModel
    let source: AskSource

    var body: some View {
        let url = m.absoluteURL(for: source)
        let exists = FileManager.default.fileExists(atPath: url.path)
        let isPDF = url.pathExtension.lowercased() == "pdf"
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("[\(source.n)] \(source.title ?? url.lastPathComponent)").font(.system(size: 13, weight: .semibold)).foregroundStyle(AskColors.ink).lineLimit(2)
                    Text("\(source.collection) / \(source.path)\(source.page.map { " · page \($0)" } ?? "")\(source.ts.map { " · \($0)" } ?? "")")
                        .font(.askMono(11)).foregroundStyle(AskColors.dim).lineLimit(3).textSelection(.enabled)
                }
                Spacer(minLength: 4)
                Button { m.openSource = nil } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .bold)).foregroundStyle(AskColors.dim).frame(width: 22, height: 22)
                }.buttonStyle(.plain).help("Close the preview")
            }.padding(12)
            Rectangle().fill(AskColors.rule).frame(height: 1)
            if let sn = source.snippet, !sn.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Passage").font(.system(size: 11, weight: .semibold)).foregroundStyle(AskColors.dim)
                    ScrollView { Text(AskMarkdown.stripMarks(sn)).font(.system(size: 13)).foregroundStyle(AskColors.ink).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading) }
                        .frame(maxHeight: isPDF && exists ? 110 : .infinity)
                }.padding(12)
                Rectangle().fill(AskColors.rule).frame(height: 1)
            }
            if isPDF && exists {
                AskPDFPage(url: url, page: source.page ?? 1).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                if !exists {
                    Text("This file is not at \(url.path) on this Mac, so it cannot be opened from here.").font(.system(size: 12)).foregroundStyle(AskColors.warn).padding(12)
                }
                Spacer(minLength: 0)
            }
            Rectangle().fill(AskColors.rule).frame(height: 1)
            HStack(spacing: 8) {
                Button("Open file") { NSWorkspace.shared.open(url) }.buttonStyle(AskGhostStyle(primary: true)).disabled(!exists)
                Button("Show in Finder") { NSWorkspace.shared.activateFileViewerSelecting([url]) }.buttonStyle(AskGhostStyle()).disabled(!exists)
                Button("Copy path") { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(url.path, forType: .string) }.buttonStyle(AskGhostStyle())
            }.padding(12)
        }
        .background(AskColors.well)
    }
}
