// Quick Search's keyboard map, as one pure function from a key press to an action. The panel sends every key down here
// before the field editor sees it (QuickPanel.sendEvent), so ↑ ↓ ↵ Esc ⇥ and the ⌘/⌃ shortcuts are the list's, while
// ← → and typing stay the field's. Space is the one conflict: it types a space, except when the list has the keyboard
// (the user has moved into it with ↑ ↓ ⇥ since the last edit) or the field is empty, where there is nothing to type over.
//
//   ↑ ↓            move the selection                     ⇥ / ⇧⇥          next / previous section
//   ↵              open                                   ⌃1 ⌃2 ⌃3 ⌃4    jump to section 1, 2, 3, 4
//   ⌘↵             reveal in Finder                       ⌘1 ⌘2           scope: Whole Mac / Indexed
//   ␣ or ⌘Y        Quick Look (⌘Y always; ␣ as above)     ⌘C              copy path (not when text is selected)
//   ⌘O             open this query in the main window     ⌘R              retry
//   esc            close Quick Look, else close the panel

import Cocoa
import Carbon.HIToolbox

enum QSKeyAction: Equatable {
    case moveUp, moveDown, activate, reveal, escape
    case nextSection, previousSection, section(Int)
    case quickLook, openInApp, copyPath, retry
    case scope(QuickScope)
}

/// What the key map needs to know about the window at the time of the key.
struct QSKeyContext {
    var fieldEmpty: Bool
    var listFocused: Bool
    var fieldHasSelection: Bool
    var quickLookOpen: Bool
}

enum QSKeys {
    /// The text commands of the field: ⌘V ⌘X ⌘A ⌘Z, ⇧⌘Z, and ⌘C with text selected (⌘C without a selection copies the path).
    /// The panel sends them down the responder chain (NSApp.sendAction), so they work from a non-activating panel
    /// whether or not the app's Edit menu gets the key.
    static func editSelector(chars: String, mods: NSEvent.ModifierFlags, fieldHasSelection: Bool) -> Selector? {
        guard mods.contains(.command), !mods.contains(.control), !mods.contains(.option) else { return nil }
        let shift = mods.contains(.shift)
        switch chars.lowercased() {
        case "v" where !shift: return NSSelectorFromString("paste:")
        case "x" where !shift: return NSSelectorFromString("cut:")
        case "a" where !shift: return NSSelectorFromString("selectAll:")
        case "c" where !shift && fieldHasSelection: return NSSelectorFromString("copy:")
        case "z": return NSSelectorFromString(shift ? "redo:" : "undo:")
        default: return nil
        }
    }

    static func action(keyCode: UInt16, chars: String, mods: NSEvent.ModifierFlags, ctx: QSKeyContext) -> QSKeyAction? {
        let cmd = mods.contains(.command)
        let ctrl = mods.contains(.control)
        let shift = mods.contains(.shift)
        let opt = mods.contains(.option)
        let plain = !cmd && !ctrl && !opt && !shift

        switch Int(keyCode) {
        case kVK_UpArrow where !cmd && !ctrl && !opt:
            return .moveUp
        case kVK_DownArrow where !cmd && !ctrl && !opt:
            return .moveDown
        case kVK_Return, kVK_ANSI_KeypadEnter:
            if cmd && !ctrl && !opt && !shift { return .reveal }
            return plain ? .activate : nil
        case kVK_Escape where plain:
            return .escape
        case kVK_Tab:
            if shift && !cmd && !ctrl && !opt { return .previousSection }
            return plain ? .nextSection : nil
        case kVK_Space where plain && (ctx.listFocused || ctx.fieldEmpty || ctx.quickLookOpen):
            return .quickLook
        default:
            break
        }

        if ctrl && !cmd && !opt && !shift, let n = Int(chars), (1...4).contains(n) {
            return .section(n)
        }
        if cmd && !ctrl && !opt && !shift {
            switch chars.lowercased() {
            case "y": return .quickLook
            case "o": return .openInApp
            case "c": return ctx.fieldHasSelection ? nil : .copyPath
            case "1": return .scope(.mac)
            case "2": return .scope(.indexed)
            case "r": return .retry
            default: return nil
            }
        }
        return nil
    }
}
