// The native Settings window: Advanced. Its one switch shows or hides the Classic interface (the old web UI). It is off by
// default; the setting is an app setting in UserDefaults (ClassicUI.swift), not a service setting.

import Cocoa
import SwiftUI

struct SWAdvancedView: View {
    @EnvironmentObject var m: SWModel

    var body: some View {
        SWFormPage {
            Section {
                SWRow(id: "classic.toggle", title: "Show Classic interface (legacy web UI)",
                      detail: "The old web version of MySearch, in its own window. Its menu item, ⌘⇧0, mysearch://classic and Classic Settings come back when this is on. Off by default.") {
                    Toggle("", isOn: Binding(get: { m.classicOn }, set: { on in
                        ClassicUI.setEnabled(on)
                        m.flashSaved()
                    }))
                    .labelsHidden().toggleStyle(.switch)
                }
                if m.classicOn {
                    Text("On. The Classic items are back in the App and View menus, the menu-bar item and the Command Palette.")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                } else {
                    SWNote(tone: .neutral, text: "Off. A link to Classic (mysearch://classic) shows a note that points back to this switch.")
                }
            } header: { Text("Interface") }
        }
    }
}
