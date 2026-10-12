// `MySearch --daemon`: the background service's supervisor. launchd starts THIS executable (inside MySearch.app), and it
// spawns the Python service as its child. macOS attributes a child's file access to the program launchd started, so a single
// Full Disk Access grant on MySearch.app covers the service and the helper's crawl, instead of Homebrew's Python.
//
// Which Python it starts, in this order (resolveDaemonRuntime):
//   1. the project in config.json (`service.project`) when it has mse/ and .venv/bin/python: a clone with a venv, the
//      developer's setup and the one every existing install has;
//   2. the runtime bundled in this app, Contents/Resources/runtime/bin/python3 with the package in Contents/Resources/mse
//      (a build made with scripts/build_app.sh --bundle-runtime): the DMG install, no clone, no venv;
//   3. the clone this app was built in, or ~/Developer/mysearch, when one of them has a venv.
//
// Plain Foundation only: never NSApplication, hot keys, status item, URL handlers or any window. main.swift calls
// runDaemonSupervisor() before anything else is created. It exits with the child's status, so launchd's KeepAlive restarts it.

import Foundation

private func daemonLog(_ message: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    FileHandle.standardError.write(Data("\(stamp) [mysearch --daemon] \(message)\n".utf8))
}

/// The folder with the Python project: config.json key `service.project` whenever it is set (the service writes it, so an
/// install that has run once never reaches the fallback). Otherwise fallbackProjectPath().
func daemonProjectPath() -> String {
    daemonConfiguredProject() ?? fallbackProjectPath()
}

/// config.json key `service.project`, or nil when the file or the key is missing.
func daemonConfiguredProject() -> String? {
    let env = ProcessInfo.processInfo.environment
    let data = env["MSE_HOME"].map { URL(fileURLWithPath: $0) }
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MySearch")
    if let raw = try? Data(contentsOf: data.appendingPathComponent("config.json")),
       let json = try? JSONSerialization.jsonObject(with: raw) as? [String: Any],
       let service = json["service"] as? [String: Any],
       let project = service["project"] as? String, !project.isEmpty {
        return (project as NSString).expandingTildeInPath
    }
    return nil
}

/// The Python the supervisor starts, and where and how.
struct DaemonRuntime: Equatable {
    enum Kind: String { case project, bundled, fallbackProject }
    let kind: Kind
    let python: String
    /// The child's working folder: the project, or the app's Resources folder.
    let workdir: String
    /// Added to the child's environment (the bundled runtime's PYTHONPATH and PYTHONHOME; empty for a venv).
    let extraEnvironment: [String: String]
}

/// The bundled runtime of an app whose Resources folder is `resources`: its Python and the package, or nil without both.
func bundledDaemonRuntime(resources: URL?, fm: FileManager = .default) -> DaemonRuntime? {
    guard let resources else { return nil }
    let root = resources.appendingPathComponent("runtime").path
    let python = root + "/bin/python3"
    guard fm.isExecutableFile(atPath: python), fm.fileExists(atPath: resources.appendingPathComponent("mse/__init__.py").path) else { return nil }
    return DaemonRuntime(kind: .bundled, python: python, workdir: resources.path, extraEnvironment: [
        // Found by the interpreter's own location as well (a relative .pth entry); set anyway, so a stray environment
        // variable of the user's cannot point it elsewhere. No user site, and no byte code written into the signed bundle.
        "PYTHONHOME": root,
        "PYTHONPATH": resources.path,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    ])
}

/// Decides which Python starts the service (see the order at the top of this file). Pure apart from the file checks,
/// so a test can call it with folders it made. nil: nothing usable; the caller reports what was looked for.
func resolveDaemonRuntime(configuredProject: String?, resources: URL?, fallbackProject: String, fm: FileManager = .default) -> DaemonRuntime? {
    func project(_ path: String, _ kind: DaemonRuntime.Kind) -> DaemonRuntime? {
        let python = path + "/.venv/bin/python"
        guard fm.fileExists(atPath: path + "/mse"), fm.isExecutableFile(atPath: python) else { return nil }
        return DaemonRuntime(kind: kind, python: python, workdir: path, extraEnvironment: [:])
    }
    if let configuredProject, let r = project(configuredProject, .project) { return r }
    if let r = bundledDaemonRuntime(resources: resources, fm: fm) { return r }
    return project(fallbackProject, .fallbackProject)
}

/// The project when config.json does not name one. Tried in order, and the first folder with the package (mse/) and the
/// virtual environment's Python wins: the clone this app was built in (the app is <clone>/build/MySearch.app, so the
/// clone is two folders up), then the folder docs/INSTALL.md tells people to clone to.
/// With neither present, the INSTALL.md path is returned, so the error the supervisor logs names that path.
func fallbackProjectPath() -> String {
    let fm = FileManager.default
    let home = fm.homeDirectoryForCurrentUser
    let built = Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().path
    let candidates = [built,
                      home.appendingPathComponent("Developer/mysearch").path]
    func isProject(_ path: String) -> Bool {
        fm.fileExists(atPath: path + "/mse") && fm.isExecutableFile(atPath: path + "/.venv/bin/python")
    }
    return candidates.first(where: isProject) ?? candidates[1]
}

func runDaemonSupervisor() -> Never {
    let fm = FileManager.default
    let configured = daemonConfiguredProject()
    let fallback = fallbackProjectPath()
    let resources = Bundle.main.resourceURL
    guard let runtime = resolveDaemonRuntime(configuredProject: configured, resources: resources, fallbackProject: fallback) else {
        daemonLog("cannot start: no MySearch runtime. Looked for: \((configured ?? "(no service.project in config.json)") + "/.venv/bin/python"), "
                  + "\((resources?.path ?? "(no Resources folder)") + "/runtime/bin/python3 with mse/"), \(fallback + "/.venv/bin/python"). "
                  + "A build with `scripts/build_app.sh --bundle-runtime` carries its own; otherwise set service.project in "
                  + "~/Library/Application Support/MySearch/config.json, then run `uv sync --frozen` there.")
        exit(78)
    }
    let python = runtime.python
    let project = runtime.workdir

    var env = ProcessInfo.processInfo.environment
    for (k, v) in runtime.extraEnvironment { env[k] = v }
    // the same environment the LaunchAgent plist sets; filled in only when launchd did not already pass it
    if env["PATH"] == nil { env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" }
    if env["HF_HUB_OFFLINE"] == nil { env["HF_HUB_OFFLINE"] = "1" }
    if env["PYTHONUNBUFFERED"] == nil { env["PYTHONUNBUFFERED"] = "1" }
    // the helper bundled in this app (Contents/MacOS/msehelper): the crawl/OCR/watch children then belong to the app
    if let exe = Bundle.main.executableURL {
        let helper = exe.deletingLastPathComponent().appendingPathComponent("msehelper").path
        if fm.isExecutableFile(atPath: helper) { env["MSE_HELPER"] = helper }
    }
    // The service stops itself when this supervisor disappears without stopping it (mse/server.py, _watch_supervisor):
    // a SIGKILL of the supervisor cannot run any cleanup here, so the child must watch its own parent.
    env["MSE_SUPERVISED"] = "1"

    // `--daemon` is ours; everything else after it is handed to `mse serve` (e.g. a port)
    var serveArgs = ["-m", "mse", "serve"]
    if let i = CommandLine.arguments.firstIndex(of: "--daemon") { serveArgs += CommandLine.arguments[(i + 1)...] }

    let child = Process()
    child.executableURL = URL(fileURLWithPath: python)
    child.arguments = serveArgs
    child.currentDirectoryURL = URL(fileURLWithPath: project)
    child.environment = env
    child.standardInput = FileHandle.nullDevice      // stdout/stderr stay launchd's log file
    child.terminationHandler = { p in
        let status: Int32 = p.terminationReason == .exit ? p.terminationStatus : 128 + p.terminationStatus
        daemonLog("service ended (\(p.terminationReason == .exit ? "exit" : "signal") \(p.terminationStatus)); exiting \(status)")
        exit(status)
    }
    do { try child.run() } catch {
        daemonLog("cannot start \(python): \(error.localizedDescription)")
        exit(71)
    }
    daemonLog("started \(python) [\(runtime.kind.rawValue)] \(serveArgs.joined(separator: " ")) (pid \(child.processIdentifier)), helper \(env["MSE_HELPER"] ?? "data-dir fallback")")

    // Forward SIGTERM/SIGINT so the service's shutdown handler runs (JOBS.freeze() persists the queue); the child's
    // exit then ends us. If it hangs, kill it after 30 s.
    var sources: [DispatchSourceSignal] = []
    for sig in [SIGTERM, SIGINT] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .global())
        src.setEventHandler {
            daemonLog("signal \(sig) received; forwarding to the service")
            kill(child.processIdentifier, sig)
            DispatchQueue.global().asyncAfter(deadline: .now() + 30) {
                if child.isRunning { daemonLog("service did not stop in 30 s; killing it"); kill(child.processIdentifier, SIGKILL) }
            }
        }
        src.resume()
        sources.append(src)
    }
    withExtendedLifetime(sources) { dispatchMain() }
}
