import SwiftUI
import AppKit
import Security
import UniformTypeIdentifiers

enum APICredentials {
    static func query(_ provider: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "io.github.virtuecho.img-link-migrator",
         kSecAttrAccount as String: provider]
    }
    static func load(_ provider: String) throws -> String {
        var attributes = query(provider)
        attributes[kSecReturnData as String] = true
        attributes[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(attributes as CFDictionary, &result)
        if status == errSecItemNotFound { return "" }
        try check(status)
        return (result as? Data).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }
    static func save(_ key: String, provider: String) throws {
        let attributes = query(provider)
        if key.isEmpty {
            let status = SecItemDelete(attributes as CFDictionary)
            if status != errSecItemNotFound { try check(status) }
            return
        }
        let value = [kSecValueData as String: Data(key.utf8)]
        let status = SecItemUpdate(attributes as CFDictionary, value as CFDictionary)
        if status == errSecItemNotFound {
            try check(SecItemAdd(attributes.merging(value) { _, new in new } as CFDictionary, nil))
        } else {
            try check(status)
        }
    }
    static func check(_ status: OSStatus) throws {
        guard status != errSecSuccess else { return }
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(status),
                      userInfo: [NSLocalizedDescriptionKey: SecCopyErrorMessageString(status, nil) as String? ?? "Keychain error \(status)"])
    }
}

struct ImageRow: Identifiable {
    var id: String { url }
    let url: String
    let file: String
    var host: String { URL(string: url)?.host ?? "Unknown" }
    var status = "Pending"
    var detail = ""
    var newURL = ""
}

@MainActor final class MigratorModel: ObservableObject {
    @Published var targets: [String] = []
    @Published var provider = "imgbb" {
        didSet {
            UserDefaults.standard.set(provider, forKey: "uploadPlatform")
            loadAPIKey()
        }
    }
    @Published var apiKey = "" {
        didSet {
            guard !loadingKey else { return }
            do { try APICredentials.save(apiKey.trimmingCharacters(in: .whitespacesAndNewlines), provider: provider) }
            catch { status = "Cannot save API key to Keychain: \(error.localizedDescription)" }
        }
    }
    @Published var mode = "size_limit"
    @Published var includeHosts = "xhscdn"
    @Published var excludeHosts = ""
    @Published var retries = 3
    @Published var backup = true
    @Published var stateDir = ""
    @Published var rows: [ImageRow] = []
    @Published var disabledHosts: Set<String> = []
    @Published var busy = false
    @Published var stopping = false
    @Published var showURLs = true
    @Published var status = "Add documents or folders, then scan."
    @Published var log = ""
    @Published var completed = 0
    @Published var total = 0
    private var scannedSignature = ""
    private var loadingKey = false
    private var process: Process?
    private var input: FileHandle?
    private var outputBuffer = Data()
    var pendingQuit = false

    init() {
        if let saved = UserDefaults.standard.string(forKey: "uploadPlatform"), ["imgbb", "picgo"].contains(saved) {
            provider = saved
        }
        loadAPIKey()
    }
    func loadAPIKey() {
        loadingKey = true
        defer { loadingKey = false }
        do { apiKey = try APICredentials.load(provider) }
        catch { apiKey = ""; status = "Cannot read API key from Keychain: \(error.localizedDescription)" }
    }

    var settings: [String: Any] {
        ["targets": targets, "provider": provider == "imgbb" ? "imgbb" : "chevereto",
         "apiKey": apiKey, "mode": mode,
         "includeHosts": includeHosts, "excludeHosts": excludeHosts,
         "retries": retries, "backup": backup, "stateDir": stateDir]
    }
    var signature: String {
        [targets.joined(separator: "\n"), provider, includeHosts, excludeHosts].joined(separator: "\u{0}")
    }
    var canMigrate: Bool { !busy && !apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !rows.isEmpty && scannedSignature == signature && !selectedURLs.isEmpty }
    var selectedURLs: [String] { rows.filter { !disabledHosts.contains($0.host) }.map(\.url) }
    var domains: [String] { Array(Set(rows.map(\.host))).sorted() }
    var failedURLs: [String] { rows.filter { $0.status == "Failed" && !disabledHosts.contains($0.host) }.map(\.url) }

    func add(_ paths: [String]) {
        guard !busy else { return }
        targets = Array(Set(targets + paths)).sorted()
    }
    func chooseTargets() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = true
        panel.allowedContentTypes = [.plainText, UTType(filenameExtension: "md") ?? .text, UTType(filenameExtension: "markdown") ?? .text]
        if panel.runModal() == .OK { add(panel.urls.map(\.path)) }
    }
    func chooseDirectory(forState: Bool) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        if panel.runModal() == .OK, let path = panel.url?.path {
            if forState { stateDir = path } else { add([path]) }
        }
    }
    func clearLocalData() {
        guard !busy else { return }
        let root = stateDir.isEmpty
            ? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("IMG Link Migrator")
            : URL(fileURLWithPath: (stateDir as NSString).expandingTildeInPath)
        let paths = ["state.json", "backups"].map { root.appendingPathComponent($0) }
            .filter { FileManager.default.fileExists(atPath: $0.path) }
        guard !paths.isEmpty else { status = "No local cache or backups to clear."; return }
        guard confirm("Clear cache and backups?", "Move these paths to Trash:\n\n" + paths.map(\.path).joined(separator: "\n"), accept: "Move to Trash") else { return }
        do {
            for path in paths { try FileManager.default.trashItem(at: path, resultingItemURL: nil) }
            status = "Local cache and backups moved to Trash."
        } catch { status = "Cannot clear local data: \(error.localizedDescription)" }
    }
    func confirm(_ title: String, _ message: String, accept: String) -> Bool {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.addButton(withTitle: accept).keyEquivalent = "\r"
        alert.addButton(withTitle: "Cancel").keyEquivalent = "\u{1b}"
        return alert.runModal() == .alertFirstButtonReturn
    }
    func scan() {
        rows = []
        disabledHosts = []
        scannedSignature = ""
        log = ""
        send("scan")
    }
    func migrate(retry: Bool = false) {
        let urls = retry ? failedURLs : selectedURLs
        guard !urls.isEmpty else { return }
        if confirm(retry ? "Retry failed images?" : "Start migration?",
                   "Process \(urls.count) images and replace their links after successful upload. \(backup ? "Backups are enabled." : "Backups are disabled.")",
                   accept: retry ? "Retry" : "Start") {
            send(retry ? "retry" : "migrate", urls: urls)
        }
    }
    func stop() {
        guard busy && !stopping else { return }
        if confirm("Stop this task?", "The current operation will finish at a safe point. Completed changes are kept.", accept: "Stop") {
            requestStop()
        }
    }
    func requestStop() {
        guard busy else { return }
        stopping = true
        status = "Stopping at a safe point…"
        write(["action": "stop"])
    }
    func startBackend() throws {
        if process?.isRunning == true { return }
        guard let bundleURL = Bundle.main.resourceURL?.deletingLastPathComponent() else {
            throw NSError(domain: "Migrator", code: 1, userInfo: [NSLocalizedDescriptionKey: "App resources are missing."])
        }
        let task = Process()
        task.executableURL = bundleURL.appendingPathComponent("Helpers/migrator-engine/migrator-engine")
        let incoming = Pipe(), outgoing = Pipe(), errors = Pipe()
        task.standardInput = incoming
        task.standardOutput = outgoing
        task.standardError = errors
        outgoing.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            Task { @MainActor [weak self] in self?.receive(data) }
        }
        errors.fileHandleForReading.readabilityHandler = { handle in
            // Drain codec diagnostics; structured backend events provide user-facing errors.
            if handle.availableData.isEmpty { handle.readabilityHandler = nil }
        }
        task.terminationHandler = { [weak self] task in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if self.busy { self.status = "Processing engine stopped unexpectedly (\(task.terminationStatus))." }
                self.busy = false
                self.stopping = false
                self.process = nil
                if self.pendingQuit { NSApp.reply(toApplicationShouldTerminate: true) }
            }
        }
        try task.run()
        process = task
        input = incoming.fileHandleForWriting
    }
    func send(_ action: String, urls: [String] = []) {
        do {
            try startBackend()
            busy = true
            stopping = false
            completed = 0
            total = urls.count
            status = action == "scan" ? "Scanning documents…" : "Processing images…"
            write(["action": action, "settings": settings, "selectedURLs": urls])
        } catch { busy = false; status = "Cannot start engine: \(error.localizedDescription)" }
    }
    func write(_ request: [String: Any]) {
        do {
            var data = try JSONSerialization.data(withJSONObject: request)
            data.append(10)
            try input?.write(contentsOf: data)
        } catch { status = "Cannot send request: \(error.localizedDescription)"; busy = false }
    }
    func receive(_ data: Data) {
        outputBuffer.append(data)
        while let newline = outputBuffer.firstIndex(of: 10) {
            let line = Data(outputBuffer[..<newline])
            outputBuffer.removeSubrange(...newline)
            guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
            handle(event)
        }
    }
    func handle(_ event: [String: Any]) {
        let kind = event["kind"] as? String ?? ""
        let url = event["url"] as? String ?? ""
        let message = event["message"] as? String ?? ""
        switch kind {
        case "scan":
            status = "Scanning: \(event["path"] as? String ?? "")"
        case "scan_done":
            let items = event["items"] as? [[String: Any]] ?? []
            rows = (event["urls"] as? [String] ?? []).map { url in
                ImageRow(url: url, file: items.first { $0["url"] as? String == url }?["path"] as? String ?? "")
            }
            scannedSignature = signature
            status = "Scanned \(event["files"] as? Int ?? 0) files: \(event["references"] as? Int ?? 0) references, \(rows.count) unique images."
        case "url_start":
            if let i = rows.firstIndex(where: { $0.url == url }) { rows[i].status = "Processing"; rows[i].detail = "" }
            total = event["total"] as? Int ?? total
            status = "Processing image \(event["current"] as? Int ?? 0) of \(total)…"
        case "image_prepared":
            if let i = rows.firstIndex(where: { $0.url == url }) {
                let size = ByteCountFormatter.string(fromByteCount: Int64(event["bytes"] as? Int ?? 0), countStyle: .file)
                let quality = event["quality"] as? Int
                rows[i].detail = "\(event["format"] as? String ?? "") · \(size) · \(event["width"] as? Int ?? 0) × \(event["height"] as? Int ?? 0)" + (quality.map { " · Q\($0)" } ?? " · \(event["method"] as? String ?? "")")
            }
        case "url_done":
            if let i = rows.firstIndex(where: { $0.url == url }) {
                rows[i].status = (event["status"] as? String ?? "failed").capitalized
                rows[i].newURL = event["new_url"] as? String ?? ""
                rows[i].detail += (rows[i].detail.isEmpty ? "" : " · ") + message
            }
            completed = event["current"] as? Int ?? completed
        case "done":
            let r = event["summary"] as? [String: Any] ?? [:]
            status = "\((r["cancelled"] as? Bool ?? false) ? "Stopped" : "Finished"): \(r["uploaded_urls"] as? Int ?? 0) uploaded, \(r["cached_urls"] as? Int ?? 0) cached, \(r["failed_urls"] as? Int ?? 0) failed; \(r["updated_files"] as? Int ?? 0) files updated."
            if let backupPath = r["backup_dir"] as? String { appendLog("Backups: " + backupPath) }
        case "error": status = message; appendLog(message)
        case "idle":
            busy = false
            stopping = false
            if pendingQuit { shutdown(); NSApp.reply(toApplicationShouldTerminate: true) }
        default:
            if !message.isEmpty { appendLog("\(kind): \(message)") }
            if kind == "write_failed" { appendLog("Write failed: \(event["path"] as? String ?? "")") }
        }
    }
    func appendLog(_ text: String) {
        log += text + "\n"
        if log.count > 30_000 { log = String(log.suffix(20_000)) }
    }
    func shutdown() { try? input?.close(); input = nil }
}

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    static let model = MigratorModel()
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let model = Self.model
        if !model.busy { model.shutdown(); return .terminateNow }
        if model.confirm("Stop and quit?", "The app will quit after the current operation reaches a safe point.", accept: "Stop and Quit") {
            if !model.busy { model.shutdown(); return .terminateNow }
            model.pendingQuit = true
            model.requestStop()
            return .terminateLater
        }
        return .terminateCancel
    }
}

struct MigratorView: View {
    @ObservedObject var model: MigratorModel
    @State private var advanced = false
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Image(systemName: "photo.on.rectangle.angled").font(.largeTitle).foregroundStyle(.tint)
                VStack(alignment: .leading) {
                    Text("IMG Link Migrator").font(.title2.bold())
                    Text("Optimize images and migrate links in Markdown and text files.").foregroundStyle(.secondary)
                }
                Spacer()
            }
            GroupBox("Documents and folders") {
                VStack(alignment: .leading) {
                    HStack {
                        Button("Add Files or Folders…") { model.chooseTargets() }
                        Button("Clear") { model.targets = [] }.disabled(model.targets.isEmpty)
                        Spacer()
                        Text("Drop files or folders here").foregroundStyle(.secondary)
                    }
                    ScrollView {
                        VStack(alignment: .leading, spacing: 3) {
                            ForEach(model.targets, id: \.self) { path in
                                HStack {
                                    Text(path).lineLimit(1).truncationMode(.middle).help(path)
                                    Spacer()
                                    Button { model.targets.removeAll { $0 == path } } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                                }
                            }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(height: 60)
                }.padding(4)
            }.disabled(model.busy)
            .onDrop(of: [.fileURL], isTargeted: nil) { providers in
                guard !model.busy else { return false }
                for provider in providers {
                    _ = provider.loadObject(ofClass: URL.self) { url, _ in
                        if let url { Task { @MainActor in model.add([url.path]) } }
                    }
                }
                return true
            }
            HStack(alignment: .top, spacing: 20) {
                VStack(alignment: .leading) {
                    Picker("Upload to", selection: $model.provider) {
                        Text("ImgBB").tag("imgbb")
                        Text("PicGo.net").tag("picgo")
                    }
                    Text("API key")
                    SecureField("Enter your \(model.provider == "imgbb" ? "ImgBB" : "PicGo.net") API key", text: $model.apiKey)
                    Text("Saved in macOS Keychain for future launches.").font(.caption).foregroundStyle(.secondary)
                }
                VStack(alignment: .leading) {
                    Picker("Image mode", selection: $model.mode) {
                        Text("Size Limit — below 1 MB").tag("size_limit")
                        Text("Original Upload").tag("original")
                    }
                    Text(model.mode == "size_limit" ? "Lossless first; AVIF Q80 → Q70; shrink 15% only when necessary." : "Keep supported originals up to \(model.provider == "imgbb" ? 32 : 25) MB; process larger or unsupported images.")
                        .font(.caption).foregroundStyle(.secondary)
                    Toggle("Back up documents before writing", isOn: $model.backup)
                }
            }.disabled(model.busy)
            DisclosureGroup("Advanced Options", isExpanded: $advanced) {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 8) {
                    GridRow {
                        Text("Include domains")
                        TextField("xhscdn; clear to include all domains", text: $model.includeHosts)
                        Text("Exclude domains")
                        TextField("Optional comma-separated domains", text: $model.excludeHosts)
                    }
                    GridRow {
                        Text("Automatic retries")
                        Stepper("\(model.retries)", value: $model.retries, in: 0...10)
                        Text("State directory")
                        HStack { TextField("Default: Application Support", text: $model.stateDir); Button("Choose…") { model.chooseDirectory(forState: true) } }
                    }
                }.padding(.top, 6)
            }.disabled(model.busy)
            HStack {
                Button("Scan") { model.scan() }.disabled(model.busy || model.targets.isEmpty)
                Button("Start Migration") { model.migrate() }.disabled(!model.canMigrate)
                Button("Retry Failed") { model.migrate(retry: true) }.disabled(model.busy || model.failedURLs.isEmpty || !model.canMigrate)
                Button("Stop") { model.stop() }.disabled(!model.busy || model.stopping)
                Spacer()
                Button("Clear Cache and Backups…") { model.clearLocalData() }.disabled(model.busy)
            }
            if !model.domains.isEmpty {
                GroupBox("Select source domains") {
                    ScrollView(.horizontal) {
                        HStack {
                            ForEach(model.domains, id: \.self) { host in
                                Toggle(host, isOn: Binding(get: { !model.disabledHosts.contains(host) }, set: { enabled in
                                    if enabled { model.disabledHosts.remove(host) } else { model.disabledHosts.insert(host) }
                                }))
                            }
                        }.padding(3)
                    }
                }.disabled(model.busy)
            }
            HStack {
                Text("\(model.selectedURLs.count) selected images").font(.headline)
                Spacer()
                Toggle("Show URLs", isOn: $model.showURLs).toggleStyle(.checkbox)
            }
            Table(model.rows) {
                TableColumn("Image / Document") { row in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(model.showURLs ? row.url : row.host).lineLimit(1).help(row.url)
                        Text(row.file).font(.caption).foregroundStyle(.secondary).lineLimit(1).help(row.file)
                    }.textSelection(.enabled)
                }.width(min: 200, ideal: 420)
                TableColumn("Status") { row in Text(row.status).foregroundStyle(row.status == "Failed" ? .red : .primary) }.width(90)
                TableColumn("Details") { row in Text(row.detail).font(.caption).lineLimit(2).help(row.detail + (row.newURL.isEmpty ? "" : "\n" + row.newURL)) }.width(min: 180, ideal: 320)
            }.frame(minHeight: 130)
            if model.busy {
                if model.total > 0 { ProgressView(value: Double(model.completed), total: Double(model.total)) }
                else { ProgressView().controlSize(.small) }
            }
            Text(model.status).font(.callout).textSelection(.enabled).lineLimit(3)
            if !model.log.isEmpty {
                DisclosureGroup("Activity Log") {
                    ScrollView { Text(model.log).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(height: 85)
                }
            }
        }.padding(20).frame(minWidth: 880, minHeight: 690)
    }
}

@main struct MigratorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        WindowGroup("IMG Link Migrator") { MigratorView(model: AppDelegate.model) }
            .defaultSize(width: 1040, height: 800)
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}
