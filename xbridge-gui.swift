// XBridge — one-click menu-bar control for the xbridge engine.
//
// Left click the icon  = turn the bridge ON / OFF.
// Right / ⌃-click      = details menu (status, log, launch-at-login, quit).
// Quitting this app never stops the bridge itself (it runs as a launchd agent).

import AppKit
import Foundation
import ServiceManagement
import Darwin

// MARK: - Paths & constants

private let kService   = "com.uaena.xbridge"
private let kPlist     = NSHomeDirectory() + "/Library/LaunchAgents/com.uaena.xbridge.plist"
private let kStateFile = NSHomeDirectory() + "/.xbridge/state.json"
private let kLogFile   = NSHomeDirectory() + "/Library/Logs/xbridge.log"

enum XState { case off, starting, armed, idle }

struct Report {
    var state: XState = .off
    var detail: String = "bridge agent not loaded — click the icon to start"
}

// MARK: - Controller (launchd + state.json)

final class Controller {
    /// Portable build (no launchd agent on this machine): the app owns the engine.
    private var bridgeProc: Process?

    @discardableResult
    private func run(_ launchPath: String, _ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launchPath)
        p.arguments = args
        let outPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = outPipe
        do { try p.run() } catch { return (-1, "") }
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    /// True when the launchd agent is loaded (booted), regardless of crash state.
    private func agentBooted() -> Bool {
        let (code, _) = run("/bin/launchctl", ["print", "gui/\(getuid())/\(kService)"])
        return code == 0
    }

    private func agentRunning() -> Bool {
        let (_, out) = run("/bin/launchctl", ["print", "gui/\(getuid())/\(kService)"])
        // top-level keys are indented by exactly one tab; take the first such line
        for line in out.split(separator: "\n") {
            if line.hasPrefix("\tstate = ") {
                let v = line.dropFirst(9)
                return v == "running" || v == "waiting" // waiting ≈ about to (re)start
            }
        }
        return out.contains("state = running")
    }

    private func pidAlive(_ pid: Int32) -> Bool {
        return pid > 0 && kill(pid, 0) == 0
    }

    /// Live engine pid from state.json, if any (portable mode relies on this).
    private func enginePid() -> Int32? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: kStateFile)),
              let doc = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let pid = doc["pid"] as? Int32 else { return nil }
        return pidAlive(pid) ? pid : nil
    }

    /// Log-tail fallback when state.json is missing/stale (old xbridge builds).
    private func logTailState() -> XState? {
        guard let content = try? String(contentsOfFile: kLogFile, encoding: .utf8) else { return nil }
        let lines = content.split(separator: "\n")
        var lastArmed = -1, lastDown = -1  // indices of the newest transition markers
        for (i, line) in lines.enumerated() {
            if line.contains("bridge ACTIVE") || line.contains("tunnel ") && line.contains("armed:") { lastArmed = i }
            if line.contains("bridge released") || line.contains("bye") { lastDown = i }
        }
        if lastArmed < 0 && lastDown < 0 { return nil }
        return lastArmed > lastDown ? .armed : .idle
    }

    func report() -> Report {
        if !agentBooted() {
            // Fresh Mac (portable build) has no agent: derive status locally instead.
            return FileManager.default.fileExists(atPath: kPlist) ? Report(state: .off)
                                                                  : reportPortable()
        }
        let running = agentRunning()

        // Prefer the machine-readable state file, validated by process liveness.
        if let data = try? Data(contentsOf: URL(fileURLWithPath: kStateFile)),
           let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            let st = doc["state"] as? String ?? ""
            var x: XState?
            if st == "armed" { x = .armed }
            else if st == "idle" { x = .idle }
            else if st == "starting" { x = nil }  // let auto-watch decide; treat as starting

            if let filePid = doc["pid"] as? Int32, pidAlive(filePid) {
                if let x = x {
                    return Report(state: x, detail: fileDetail(doc))
                }
            }
            if !running {
                // booted but process not (re)started yet — crash recovery window
                return Report(state: .starting, detail: "restarting after abnormal exit …")
            }
            if x == nil { return Report(state: .starting, detail: "auto-watch deciding …") }
            if let x = x { return Report(state: x, detail: fileDetail(doc)) }
        }

        // Fallbacks.
        if !running { return Report(state: .starting, detail: "agent loaded; process restarting …") }
        if let s = logTailState() { return Report(state: s) }
        return Report(state: .starting, detail: "bridge process running")
    }

    private func fileDetail(_ doc: [String: Any], managedBy: String = "launchd") -> String {
        var bits: [String] = []
        if let t = doc["time"] as? String { bits.append(t) }
        if let devs = doc["devices"] as? [[String: Any]] {
            let names = devs.compactMap { $0["label"] as? String }
            if !names.isEmpty {
                bits.append(names.joined(separator: ", ")
                            + (names.count > 1 ? "  (+\(names.count - 1) more)" : ""))
            }
        }
        return bits.joined(separator: "  ·  ") + " — managed by \(managedBy)"
    }

    /// Status when no launchd agent exists on this machine (portable build):
    /// same sources as agent mode — state.json + engine process liveness.
    private func reportPortable() -> Report {
        if let data = try? Data(contentsOf: URL(fileURLWithPath: kStateFile)),
           let doc = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           enginePid() != nil {
            let st = doc["state"] as? String ?? ""
            switch st {
            case "armed": return Report(state: .armed, detail: fileDetail(doc, managedBy: "XBridge app"))
            case "idle":  return Report(state: .idle,  detail: fileDetail(doc, managedBy: "XBridge app"))
            default:      return Report(state: .starting, detail: "auto-watch deciding …")
            }
        }
        if bridgeProc?.isRunning == true {
            return Report(state: .starting, detail: "auto-watch deciding …")
        }
        return Report(state: .off)
    }

    // MARK: on / off (persistent across reboots)

    func turnOn() {
        if !FileManager.default.fileExists(atPath: kPlist) {
            turnOnPortable()    // portable build, no launchd agent on this machine
            return
        }
        let dom = "gui/\(getuid())"
        run("/bin/launchctl", ["enable", dom + "/" + kService])      // persist ON after reboot
        if !agentBooted() {
            run("/bin/launchctl", ["bootstrap", dom, kPlist])        // "already loaded" is fine
        } else {
            run("/bin/launchctl", ["kickstart", "-k", dom + "/" + kService]) // restart it
        }
    }

    /// No launchd agent (fresh Mac): run xbridge.py from the app bundle directly.
    /// The engine holds shared state (ports, mDNS), so never start a second one.
    private func turnOnPortable() {
        if bridgeProc?.isRunning == true || enginePid() != nil { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: kPyExe)
        p.arguments = [kPyScript, "bridge", "--all", "--auto"]
        p.environment = kPyEnv
        // Same logging as the launchd agent: append to xbridge.log.
        let logDir = (kLogFile as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: kLogFile) {
            try? Data().write(to: URL(fileURLWithPath: kLogFile))
        }
        if let fh = FileHandle(forWritingAtPath: kLogFile) {
            fh.seekToEndOfFile()
            p.standardOutput = fh
            p.standardError = fh
        } else {
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
        }
        do { try p.run(); bridgeProc = p } catch { bridgeProc = nil }
    }

    func turnOff() {
        if !FileManager.default.fileExists(atPath: kPlist) {
            // portable build: stop the engine (ours, or an orphan from a previous session)
            if let p = bridgeProc, p.isRunning {
                p.terminate()          // clean SIGTERM → xbridge sends the mDNS goodbye
                bridgeProc = nil
            } else if let pid = enginePid() {
                kill(pid, SIGTERM)
            }
            return
        }
        let dom = "gui/\(getuid())"
        run("/bin/launchctl", ["bootout", dom + "/" + kService])     // stop now (clean SIGTERM)
        run("/bin/launchctl", ["disable", dom + "/" + kService])     // persist OFF after reboot
    }

    func loginItemEnabled() -> Bool {
        return SMAppService.mainApp.status == .enabled
    }

    func setLoginItem(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
        } catch { /* reflected by the next status query */ }
    }
}

// MARK: - xbridge.py bridge (device management CLI)
//
// Dev checkout: the venv in ~/tools/xbridge. On a machine without it (portable
// build on a fresh Mac): system Python + xbridge.py shipped in the app's
// Resources, with zeroconf/ifaddr vendored there (xbridge's only deps).

private let kPyExe: String = {
    let venv = NSHomeDirectory() + "/tools/xbridge/.venv-xbridge/bin/python"
    if FileManager.default.fileExists(atPath: venv) { return venv }
    for c in ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
        where FileManager.default.isExecutableFile(atPath: c) { return c }
    return "/usr/bin/python3"
}()

private let kPyScript: String = {
    let dev = NSHomeDirectory() + "/tools/xbridge/xbridge.py"
    if FileManager.default.fileExists(atPath: dev) { return dev }
    let res = Bundle.main.resourcePath ?? (NSHomeDirectory() + "/tools/xbridge")
    return res + "/xbridge.py"
}()

/// Engine environment: in portable mode PYTHONPATH must include Resources so
/// `import zeroconf` finds the vendored copy; in dev mode it is just inherited.
private let kPyEnv: [String: String] = {
    var env = ProcessInfo.processInfo.environment
    let devScript = NSHomeDirectory() + "/tools/xbridge/xbridge.py"
    if !FileManager.default.fileExists(atPath: devScript) {
        let res = Bundle.main.resourcePath ?? ""
        if !res.isEmpty, FileManager.default.fileExists(atPath: res + "/xbridge.py") {
            let pp = env["PYTHONPATH"] ?? ""
            env["PYTHONPATH"] = pp.isEmpty ? res : res + ":" + pp
        }
    }
    return env
}()

struct CLIResult {
    var code: Int32
    var stdout: String
    var stderr: String
}

// MARK: - CLI runner (one-shot xbridge.py subcommands)

/// Run one xbridge.py subcommand to completion; capture output;
/// hard-kill past the timeout so a wedged scan can never hang the UI.
func runCLI(_ args: [String], timeoutSeconds: Double = 90) -> CLIResult {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: kPyExe)
    p.arguments = [kPyScript] + args
    p.environment = kPyEnv
    let outPipe = Pipe()
    let errPipe = Pipe()
    p.standardOutput = outPipe
    p.standardError = errPipe

    do { try p.run() } catch {
        return CLIResult(code: -1, stdout: "",
                         stderr: "cannot start \(kPyExe) — is Python + xbridge.py available? (\(error.localizedDescription))")
    }

    var outData = Data()
    var errData = Data()
    let group = DispatchGroup()
    group.enter()
    DispatchQueue.global(qos: .utility).async {
        outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        group.leave()
    }
    group.enter()
    DispatchQueue.global(qos: .utility).async {
        errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        group.leave()
    }

    DispatchQueue.global().asyncAfter(deadline: .now() + timeoutSeconds) {
        if p.isRunning { p.terminate(); kill(p.processIdentifier, SIGKILL) }
    }
    p.waitUntilExit()
    group.wait()

    return CLIResult(code: p.terminationStatus,
                     stdout: String(data: outData, encoding: .utf8) ?? "",
                     stderr: String(data: errData, encoding: .utf8) ?? "")
}

func jsonObject(_ s: String) -> [String: Any]? {
    return (try? JSONSerialization.jsonObject(with: Data(s.utf8))) as? [String: Any]
}

func jsonString(_ d: [String: Any]) -> String {
    guard let data = try? JSONSerialization.data(withJSONObject: d, options: []),
          let s = String(data: data, encoding: .utf8) else { return "{}" }
    return s
}

// MARK: - Layout helpers (card style)

final class FlippedView: NSView {
    override var isFlipped: Bool { true }
}

func makeCard(_ frame: NSRect, symbolName: String, title: @autoclosure () -> String) ->
    (NSVisualEffectView, NSImageView, NSTextField) {
    let card = NSVisualEffectView(frame: frame)
    card.material = .sidebar
    card.blendingMode = .withinWindow
    card.state = .active
    card.wantsLayer = true
    card.layer?.cornerRadius = 12
    let icon = NSImageView(frame: NSRect(x: 16, y: card.bounds.height - 26, width: 18, height: 18))
    icon.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)
    if let img = icon.image {
        img.isTemplate = true
        icon.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 13, weight: .semibold)
    }
    icon.contentTintColor = NSColor.secondaryLabelColor
    let label = NSTextField(labelWithString: title())
    label.font = .systemFont(ofSize: 13, weight: .semibold)
    label.textColor = .labelColor
    label.frame = NSRect(x: 42, y: card.bounds.height - 27, width: frame.width - 150, height: 20)
    label.lineBreakMode = .byTruncatingTail
    card.addSubview(icon)
    card.addSubview(label)
    return (card, icon, label)
}

// A two-line selectable row (hostname + details).
final class ClickRowView: NSView {
    var onClick: (() -> Void)?
    let text = NSTextField()
    var isSelected = false {
        didSet { if oldValue != isSelected { needsDisplay = true } }
    }

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { false }

    init(height: CGFloat) {
        super.init(frame: NSRect(x: 0, y: 0, width: 100, height: height))
        text.isEditable = false
        text.isBezeled = false
        text.drawsBackground = false
        text.lineBreakMode = .byTruncatingTail
        text.maximumNumberOfLines = 2
        addSubview(text)
    }
    required init?(coder: NSCoder) { fatalError() }

    func setAttributedText(_ s: NSAttributedString) {
        text.attributedStringValue = s
    }

    func layoutText(width: CGFloat) {
        text.frame = NSRect(x: 14, y: (bounds.height - 38) / 2, width: max(10, width - 28), height: 38)
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        // The whole row is clickable, even over the text field.
        if let sup = superview, bounds.contains(sup.convert(point, to: self)) {
            return self
        }
        return super.hitTest(point)
    }

    override func mouseDown(with event: NSEvent) { onClick?() }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds.insetBy(dx: 1, dy: 1),
                                xRadius: 8, yRadius: 8)
        if isSelected {
            NSColor.controlAccentColor.withAlphaComponent(0.22).setFill()
            path.fill()
            NSColor.controlAccentColor.setStroke()
            path.lineWidth = 1
            path.stroke()
        } else {
            NSColor.labelColor.withAlphaComponent(0.045).setFill()
            path.fill()
        }
    }
}

func twoLineText(_ name: String, _ detail: String,
                 nameColor: NSColor = .labelColor) -> NSAttributedString {
    let s = NSMutableAttributedString(
        string: name, attributes: [
            .font: NSFont.systemFont(ofSize: 13, weight: .semibold),
            .foregroundColor: nameColor,
        ])
    s.append(NSAttributedString(string: "\n" + detail, attributes: [
        .font: NSFont.systemFont(ofSize: 11),
        .foregroundColor: NSColor.secondaryLabelColor,
    ]))
    return s
}

// MARK: - Discovered phone (from `discover --json`)

struct DiscoveredPhone {
    let raw: [String: Any]   // round-tripped verbatim into add-device --scan
    var id: String { (raw["id"] as? String) ?? "" }
    var host: String { (raw["bonjour_host"] as? String) ?? "" }
    var shortName: String { (raw["short_name"] as? String) ?? "" }
    var port: Int { (raw["port"] as? Int) ?? 49152 }
    var source: String { (raw["source"] as? String) ?? "phone" }
    var inConfig: Bool { (raw["in_config"] as? Bool) ?? false }
    var configLabel: String { (raw["config_label"] as? String) ?? "" }
    var suggestion: String { (raw["provider_suggestion"] as? String) ?? "tailscale" }
    var tailscaleNode: String { (raw["tailscale_node"] as? String) ?? "" }
    var tailnetIP: String { (raw["tailnet_ip"] as? String) ?? "" }
    var configIP: String { (raw["config_ip"] as? String) ?? "" }
    var online: Bool? { raw["online"] as? Bool }

    var displayName: String {
        if !configLabel.isEmpty { return configLabel }
        if !shortName.isEmpty { return shortName }
        return host
    }
    var detailLine: String {
        var bits = [host.isEmpty ? id : host, ":\(port)"]
        if source == "bridge-copy" { bits.append("· re-published by XBridge — the phone is remote") }
        if !tailscaleNode.isEmpty { bits.append("· tailscale: \(tailscaleNode)") }
        return bits.joined(separator: "  ")
    }
}

func parsePhones(_ result: CLIResult) -> [DiscoveredPhone] {
    guard let doc = jsonObject(result.stdout),
          let found = doc["found"] as? [[String: Any]] else { return [] }
    return found.map { DiscoveredPhone(raw: $0) }
}

// MARK: - Add Device window (three-step form)

final class AddDeviceWindow {
    let window: NSWindow
    var phones: [DiscoveredPhone] = []
    var selectedRow: ClickRowView?
    var rowsContainer: FlippedView!

    private var header1: NSTextField!
    private var refreshButton = NSButton()
    private let spinner = NSProgressIndicator()
    private let connectorSeg = NSSegmentedControl()
    private var nodeInfoLabel = NSTextField()
    private let ipField = NSTextField()
    // tailnet peers from `discover` - lets the user pick this phone's node when
    // the auto-match (bonjour label vs tailnet hostname) comes up empty.
    private var tailnetNodes: [[String: Any]] = []
    private let nodePickerPrompt = NSTextField(labelWithString: "Tailscale node")
    private let nodePicker = NSPopUpButton()
    private let nameField = NSTextField()
    private let saveButton = NSButton()
    private let statusLabel: NSTextField = NSTextField(labelWithString: "")
    private var busy = false
    private var listScroll: NSScrollView!
    private var currentPhone: DiscoveredPhone?
    private var ipPrompt = NSTextField()

    init() {
        let w: CGFloat = 560
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: w, height: 680),
                           styleMask: [.titled, .closable, .miniaturizable],
                           backing: .buffered, defer: false)
        win.title = "Add Device"
        win.isReleasedWhenClosed = false
        window = win
        let content = FlippedView(frame: NSRect(x: 0, y: 0, width: w, height: 680))
        win.contentView = content
        let pad: CGFloat = 24
        let cardW = w - pad * 2

        // subtitle
        let sub = NSTextField(labelWithString:
            "Find an iPhone on your local network and pair it with its network peer.")
        sub.font = .systemFont(ofSize: 12)
        sub.textColor = .secondaryLabelColor
        sub.frame = NSRect(x: pad, y: 14, width: cardW, height: 18)
        content.addSubview(sub)

        // ── Section 1: discovered iPhones + Refresh
        let card1Frame = NSRect(x: pad, y: 48, width: cardW, height: 306)
        let (card1, _, h1) = makeCard(card1Frame, symbolName: "wifi",
                                      title: "Scanning your local network …")
        header1 = h1
        nodeInfoLabel = NSTextField(labelWithString: "")
        nodeInfoLabel.font = .systemFont(ofSize: 12)

        refreshButton = NSButton()
        refreshButton.bezelStyle = .rounded
        refreshButton.controlSize = .small
        refreshButton.title = "Refresh"
        refreshButton.frame = NSRect(x: cardW - 84, y: card1Frame.height - 30, width: 72, height: 24)
        refreshButton.target = self
        refreshButton.action = #selector(refreshTapped)
        card1.addSubview(refreshButton)

        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.isIndeterminate = true
        spinner.frame = NSRect(x: cardW - 84 - 26, y: card1Frame.height - 30, width: 20, height: 20)
        spinner.isHidden = true
        card1.addSubview(spinner)

        // row list (flipped container inside a scroll view; clipped to the card)
        let listWrap = FlippedView(frame: NSRect(x: 14, y: 8,
                                                 width: cardW - 28, height: card1Frame.height - 50))
        card1.addSubview(listWrap)
        listScroll = NSScrollView(frame: NSRect(x: 0, y: 8,
                                                width: listWrap.bounds.width, height: listWrap.bounds.height - 16))
        listScroll.autoresizingMask = [.width, .height]
        listScroll.hasVerticalScroller = true
        listScroll.autohidesScrollers = true
        listScroll.borderType = .noBorder
        let rowsDoc = FlippedView(frame: NSRect(x: 0, y: 0,
                                                width: listWrap.bounds.width - 6, height: 20))
        rowsContainer = rowsDoc
        listScroll.documentView = rowsDoc
        listWrap.addSubview(listScroll)
        content.addSubview(card1)

        // ── Section 2: network connector (Tailscale / Manual IP)
        let card2Frame = NSRect(x: pad, y: 48 + 306 + 16, width: cardW, height: 114)
        let (card2, _, _) = makeCard(card2Frame, symbolName: "globe",
                                     title: "Network connector")
        let c2 = FlippedView(frame: NSRect(x: 16, y: card2Frame.height - 96,
                                           width: cardW - 32, height: 78))
        card2.addSubview(c2)

        connectorSeg.segmentCount = 2
        connectorSeg.setLabel("Tailscale", forSegment: 0)
        connectorSeg.setLabel("Manual IP", forSegment: 1)
        connectorSeg.selectedSegment = 0
        connectorSeg.frame = NSRect(x: 0, y: 0, width: 240, height: 28)
        connectorSeg.target = self
        connectorSeg.action = #selector(connectorChanged)
        c2.addSubview(connectorSeg)

        nodeInfoLabel.font = .systemFont(ofSize: 12)
        nodeInfoLabel.textColor = .secondaryLabelColor
        nodeInfoLabel.frame = NSRect(x: 0, y: 34, width: cardW - 32, height: 18)
        nodeInfoLabel.lineBreakMode = .byTruncatingTail
        c2.addSubview(nodeInfoLabel)

        ipPrompt = NSTextField(labelWithString: "Reachable IP")
        ipPrompt.font = .systemFont(ofSize: 12)
        ipPrompt.textColor = .secondaryLabelColor
        ipPrompt.frame = NSRect(x: 0, y: 56, width: 110, height: 18)
        ipPrompt.isHidden = true
        c2.addSubview(ipPrompt)

        ipField.placeholderString = "e.g. 192.168.1.50"
        ipField.frame = NSRect(x: 114, y: 52, width: cardW - 32 - 120, height: 24)
        ipField.isHidden = true
        c2.addSubview(ipField)

        nodePickerPrompt.font = .systemFont(ofSize: 12)
        nodePickerPrompt.textColor = .secondaryLabelColor
        nodePickerPrompt.frame = NSRect(x: 0, y: 56, width: 110, height: 18)
        nodePickerPrompt.isHidden = true
        c2.addSubview(nodePickerPrompt)

        nodePicker.font = .systemFont(ofSize: 12)
        nodePicker.frame = NSRect(x: 114, y: 50, width: cardW - 32 - 120, height: 25)
        nodePicker.isHidden = true
        nodePicker.target = self
        nodePicker.action = #selector(nodePickerChanged)
        c2.addSubview(nodePicker)
        content.addSubview(card2)

        // ── Section 3: display name
        let card3Frame = NSRect(x: pad, y: card2Frame.maxY + 16, width: cardW, height: 84)
        let (card3, _, _) = makeCard(card3Frame, symbolName: "pencil",
                                     title: "Display name")
        let c3 = FlippedView(frame: NSRect(x: 16, y: card3Frame.height - 76,
                                           width: cardW - 32, height: 40))
        card3.addSubview(c3)
        nameField.placeholderString = "e.g. IU's iPhone"
        nameField.frame = NSRect(x: 0, y: 0, width: cardW - 32, height: 28)
        c3.addSubview(nameField)
        content.addSubview(card3)

        // ── Footer: status + Cancel / Save
        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.frame = NSRect(x: pad, y: card3Frame.maxY + 16, width: cardW - 210, height: 32)
        statusLabel.lineBreakMode = .byTruncatingTail
        content.addSubview(statusLabel)

        saveButton.title = "Save"
        saveButton.target = self
        saveButton.action = #selector(saveTapped)
        saveButton.keyEquivalent = "\u{0D}"
        saveButton.frame = NSRect(x: pad + cardW - 92, y: card3Frame.maxY + 12, width: 88, height: 32)
        content.addSubview(saveButton)

        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancelTapped))
        cancel.bezelStyle = .rounded
        cancel.frame = NSRect(x: pad + cardW - 208, y: card3Frame.maxY + 12, width: 96, height: 32)
        content.addSubview(cancel)

        updateConnectorUI()
    }

    // MARK: show / busy state

    func showAndRefresh() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        refreshTapped()
    }

    private func setBusy(_ on: Bool) {
        guard busy != on else { return }
        busy = on
        spinner.isHidden = !on
        if on { spinner.startAnimation(nil) } else { spinner.stopAnimation(nil) }
        refreshButton.isEnabled = !on
        if !on { saveButton.isEnabled = selectedRow != nil }
    }

    // MARK: step 1 — discover + refresh

    @objc private func refreshTapped() {
        guard !busy else { return }
        setBusy(true)
        header1.stringValue = "Scanning your local network …"
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = "Looking for iPhones on this Wi-Fi (takes a few seconds) …"
        select(phone: nil)

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let res = runCLI(["discover", "--seconds", "10"], timeoutSeconds: 60)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.setBusy(false)
                let found = parsePhones(res)
                self.tailnetNodes = (jsonObject(res.stdout)?["tailnet_nodes"] as? [[String: Any]]) ?? []
                self.rebuildNodePicker(restorePrevious: true)
                if res.stdout.isEmpty && !res.stderr.isEmpty {
                    self.header1.stringValue = "Scan failed"
                    let tail = res.stderr.split(separator: "\n").suffix(2).joined(separator: " ")
                    self.statusLabel.textColor = .systemRed
                    self.statusLabel.stringValue = tail + " — if macOS asked for Local Network access, allow it and press Refresh."
                } else {
                    self.statusLabel.textColor = .secondaryLabelColor
                    self.statusLabel.stringValue = ""
                }
                self.fillList(found)
            }
        }
    }

    private func fillList(_ phones: [DiscoveredPhone]) {
        self.phones = phones
        let suffix = phones.count == 1 ? "" : "s"
        header1.stringValue = phones.isEmpty ? "No iPhones found on the local network" :
            "\(phones.count) iPhone\(suffix) found on the local network"
        for v in rowsContainer.subviews { v.removeFromSuperview() }
        let rowW = max(120, listScroll.bounds.width - 8)
        var y: CGFloat = 6
        for (i, phone) in phones.enumerated() {
            let row = ClickRowView(height: 46)
            row.frame = NSRect(x: 0, y: y, width: rowW, height: 46)
            let mark = phone.inConfig ? "✓ " : ""
            row.setAttributedText(twoLineText(mark + phone.displayName, phone.detailLine))
            row.layoutText(width: rowW)
            row.onClick = { [weak self] in
                guard let self = self, i < self.phones.count else { return }
                self.select(phone: self.phones[i])
            }
            rowsContainer.addSubview(row)
            y += 52
        }
        if phones.isEmpty {
            let note = NSTextField(labelWithString:
                "Nothing found — for the initial scan the phone must be on this Mac's Wi-Fi. "
                + "(Tailscale alone is not enough for discovery; it only carries traffic afterwards.)")
            note.font = .systemFont(ofSize: 12)
            note.textColor = .secondaryLabelColor
            note.maximumNumberOfLines = 3
            note.frame = NSRect(x: 8, y: 10, width: rowW - 24, height: 56)
            note.lineBreakMode = .byWordWrapping
            rowsContainer.addSubview(note)
        }
        rowsContainer.frame = NSRect(x: 0, y: 0, width: rowW, height: max(24, y))
        listScroll.documentView?.needsLayout = true
    }

    private func select(phone: DiscoveredPhone?) {
        let clickRows = rowsContainer.subviews.compactMap { $0 as? ClickRowView }
        let idx = phone.flatMap { p in phones.firstIndex(where: { $0.id == p.id }) }
        for (i, r) in clickRows.enumerated() { r.isSelected = (idx != nil && i == idx!) }
        selectedRow = idx.flatMap { $0 < clickRows.count ? clickRows[$0] : nil }
        currentPhone = phone
        if let p = phone {
            connectorSeg.selectedSegment = (p.suggestion == "manual-ip") ? 1 : 0
            nameField.stringValue = (p.inConfig && !p.configLabel.isEmpty)
                ? p.configLabel : (p.shortName.isEmpty ? "" : p.shortName)
            if !p.configIP.isEmpty { ipField.stringValue = p.configIP }
        } else {
            nameField.stringValue = ""
        }
        saveButton.isEnabled = !busy && phone != nil
        rebuildNodePicker(restorePrevious: false)
        updateConnectorUI()
    }

    @objc private func connectorChanged() { updateConnectorUI() }

    // Rebuild the picker after each scan (or phone selection). A node whose
    // tailnet IP equals the selected phone's known IP is auto-selected: this is how
    // phones whose tailnet node name has no relation to their Bonjour name (e.g. a
    // phone whose tailnet node is "iphone171") are still added via Tailscale. A
    // previously picked node is only restored when re-scanning the same phone.
    private func rebuildNodePicker(restorePrevious: Bool) {
        let previous = pickedNodeName
        nodePicker.removeAllItems()
        nodePicker.addItem(withTitle: currentPhone == nil ? "select a phone first" : "(choose the phone's node…)")
        for n in tailnetNodes {
            let name = (n["name"] as? String) ?? ""
            guard !name.isEmpty else { continue }
            let online = n["online"] as? Bool
            nodePicker.addItem(withTitle: name + (online == true ? "  — online ✓"
                                                     : online == false ? "  — offline ⚠︎" : ""))
        }
        if let p = currentPhone, !p.configIP.isEmpty,
           let i = tailnetNodes.firstIndex(where: { ($0["ip"] as? String ?? "") == p.configIP }),
           !((tailnetNodes[i]["name"] as? String) ?? "").isEmpty {
            nodePicker.selectItem(at: i + 1)
        } else if restorePrevious, let previous,
                  let i = tailnetNodes.firstIndex(where: { ($0["name"] as? String) == previous }) {
            nodePicker.selectItem(at: i + 1)
        } else {
            nodePicker.selectItem(at: 0)
        }
    }

    /// Raw tailnet node name of the current picker choice (nil = none chosen).
    private var pickedNodeName: String? {
        let idx = nodePicker.indexOfSelectedItem
        guard idx > 0, tailnetNodes.indices.contains(idx - 1) else { return nil }
        let name = (tailnetNodes[idx - 1]["name"] as? String) ?? ""
        return name.isEmpty ? nil : name
    }

    @objc private func nodePickerChanged() { updateConnectorUI() }

    private func updateConnectorUI() {
        let manual = connectorSeg.selectedSegment == 1
        ipPrompt.isHidden = !manual
        ipField.isHidden = !manual
        if manual {
            nodeInfoLabel.stringValue = "Direct connection — the phone must be reachable at this IP from the Mac (static LAN or public address)."
            nodeInfoLabel.textColor = .secondaryLabelColor
            nodePickerPrompt.isHidden = true
            nodePicker.isHidden = true
        } else if let p = currentPhone, !p.tailscaleNode.isEmpty {
            var t = "Tailscale node: \(p.tailscaleNode)"
            if !p.tailnetIP.isEmpty { t += "  (\(p.tailnetIP))" }
            if let on = p.online { t += (on ? "  — online ✓" : "  — offline ⚠︎ (Tailscale running on the phone?)") }
            nodeInfoLabel.stringValue = t
            nodeInfoLabel.textColor = (p.online == false ? NSColor.systemOrange : .secondaryLabelColor)
            nodePickerPrompt.isHidden = true
            nodePicker.isHidden = true
        } else if currentPhone == nil {
            nodeInfoLabel.stringValue = "Pick an iPhone above to choose its network connector."
            nodeInfoLabel.textColor = .secondaryLabelColor
            nodePickerPrompt.isHidden = true
            nodePicker.isHidden = true
        } else if tailnetNodes.isEmpty {
            nodeInfoLabel.stringValue = "No matching Tailscale node — and no tailnet nodes visible on this Mac (is tailscaled running?)."
            nodeInfoLabel.textColor = .systemOrange
            nodePickerPrompt.isHidden = true
            nodePicker.isHidden = true
        } else {
            let name = pickedNodeName ?? ""
            if name.isEmpty {
                nodeInfoLabel.stringValue = "No auto-match by the phone's name — is Tailscale on the phone logged into this tailnet? Pick its node below, or use Manual IP."
                nodeInfoLabel.textColor = .systemOrange
            } else {
                var t = "Tailscale node (picked): \(name)"
                if let on = tailnetNodes[nodePicker.indexOfSelectedItem - 1]["online"] as? Bool {
                    t += (on ? "  — online ✓" : "  — offline ⚠︎ (is Tailscale running on the phone?)")
                }
                if let p = currentPhone, !p.configIP.isEmpty,
                   nodePicker.indexOfSelectedItem > 0,
                   (tailnetNodes[nodePicker.indexOfSelectedItem - 1]["ip"] as? String ?? "") == p.configIP {
                    t += "  — matches the phone's known IP ✓"
                }
                nodeInfoLabel.stringValue = t
                nodeInfoLabel.textColor = .secondaryLabelColor
            }
            nodePickerPrompt.isHidden = false
            nodePicker.isHidden = false
        }
    }

    // MARK: step 4 — save into the xbridge config

    @objc private func saveTapped() {
        guard let phone = currentPhone, !busy else { return }
        var args = ["add-device", "--scan", jsonString(phone.raw)]
        let name = nameField.stringValue.trimmingCharacters(in: .whitespaces)
        if !name.isEmpty { args += ["--name", name] }
        if connectorSeg.selectedSegment == 1 {
            args += ["--provider", "manual-ip"]
            let ip = ipField.stringValue.trimmingCharacters(in: .whitespaces)
            if !ip.isEmpty { args += ["--ip", ip] }
        } else {
            args += ["--provider", "tailscale"]
            let node = phone.tailscaleNode.isEmpty ? (pickedNodeName ?? "") : phone.tailscaleNode
            if !node.isEmpty { args += ["--tailscale-node", node] }
        }
        setBusy(true)
        saveButton.title = "Saving…"
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = "Saving device profile …"

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let res = runCLI(args, timeoutSeconds: 60)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.saveButton.title = "Save"
                self.setBusy(false)
                let doc = jsonObject(res.stdout)
                if (doc?["ok"] as? Bool) == true {
                    let label = ((doc?["device"] as? [String: Any])?["label"] as? String) ?? phone.displayName
                    let note = doc?["note"] as? String ?? ""
                    self.statusLabel.textColor = .systemGreen
                    self.statusLabel.stringValue =
                        "✓ Added \"\(label)\" to xbridge — the bridge covers it automatically." +
                        (note.isEmpty ? "" : "  (\(note))")
                    self.currentPhone = nil
                    if let r = self.selectedRow { r.isSelected = false; self.selectedRow = nil }
                } else {
                    let err = (doc?["error"] as? String)
                        ?? res.stderr.split(separator: "\n").last.map(String.init) ?? "unknown error"
                    self.statusLabel.textColor = .systemRed
                    self.statusLabel.stringValue = err
                }
            }
        }
    }

    @objc private func cancelTapped() { window.close() }
}

// MARK: - Manage devices window (list + remove configured phones)

final class DevicesWindow {
    let window: NSWindow
    private var devices: [[String: Any]] = []
    private var selectedRow: ClickRowView?
    private var rowsContainer: FlippedView!
    private var listScroll: NSScrollView!
    private var refreshButton = NSButton()
    private let removeBtn = NSButton()
    private var header1: NSTextField!
    // (removeBtn is configured in place; keep the declaration default above)
    private var statusLabel: NSTextField = NSTextField(labelWithString: "")
    private var selIndex: Int?
    var onAddRequest: (() -> Void)?

    init() {
        let w: CGFloat = 560
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: w, height: 560),
                           styleMask: [.titled, .closable, .miniaturizable],
                           backing: .buffered, defer: false)
        win.title = "Devices"
        win.isReleasedWhenClosed = false
        window = win
        let content = FlippedView(frame: NSRect(x: 0, y: 0, width: w, height: 560))
        win.contentView = content
        let pad: CGFloat = 24
        let cardW = w - pad * 2

        let sub = NSTextField(labelWithString:
            "Phones known to xbridge — the bridge covers all of them automatically.")
        sub.font = .systemFont(ofSize: 12)
        sub.textColor = .secondaryLabelColor
        sub.frame = NSRect(x: pad, y: 14, width: cardW, height: 18)
        content.addSubview(sub)

        let cardFrame = NSRect(x: pad, y: 48, width: cardW, height: 420)
        let (card, _, h1) = makeCard(cardFrame, symbolName: "iphone",
                                     title: "Loading…")
        header1 = h1

        let refresh = NSButton()
        refresh.bezelStyle = .rounded
        refresh.controlSize = .small
        refresh.title = "Refresh"
        refresh.frame = NSRect(x: cardW - 84, y: cardFrame.height - 30, width: 72, height: 24)
        refresh.target = self
        refresh.action = #selector(refreshTapped)
        refreshButton = refresh
        card.addSubview(refresh)

        let listWrap = FlippedView(frame: NSRect(x: 14, y: 8,
                                                 width: cardW - 28, height: cardFrame.height - 50))
        card.addSubview(listWrap)
        listScroll = NSScrollView(frame: NSRect(x: 0, y: 8,
                                                width: listWrap.bounds.width, height: listWrap.bounds.height - 16))
        listScroll.autoresizingMask = [.width, .height]
        listScroll.hasVerticalScroller = true
        listScroll.autohidesScrollers = true
        listScroll.borderType = .noBorder
        let rowsDoc = FlippedView(frame: NSRect(x: 0, y: 0,
                                                width: listWrap.bounds.width - 6, height: 20))
        rowsContainer = rowsDoc
        listScroll.documentView = rowsDoc
        listWrap.addSubview(listScroll)
        content.addSubview(card)

        removeBtn.title = "Remove"
        removeBtn.bezelStyle = .rounded
        removeBtn.isEnabled = false
        removeBtn.frame = NSRect(x: pad, y: cardFrame.maxY + 14, width: 92, height: 32)
        removeBtn.target = self
        removeBtn.action = #selector(removeTapped)
        content.addSubview(removeBtn)

        let add = NSButton(title: "Add Device…", target: self, action: #selector(addTapped))
        add.bezelStyle = .rounded
        add.frame = NSRect(x: pad + cardW - 184, y: cardFrame.maxY + 14, width: 120, height: 32)
        content.addSubview(add)

        let close = NSButton(title: "Close", target: self, action: #selector(closeTapped))
        close.bezelStyle = .rounded
        close.frame = NSRect(x: pad + cardW - 56, y: cardFrame.maxY + 14, width: 56, height: 32)
        content.addSubview(close)

        statusLabel.font = .systemFont(ofSize: 12)
        statusLabel.frame = NSRect(x: pad, y: cardFrame.maxY + 52, width: cardW - 100, height: 36)
        statusLabel.lineBreakMode = .byWordWrapping
        statusLabel.maximumNumberOfLines = 2
        content.addSubview(statusLabel)
    }

    func showAndRefresh() {
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        refreshNow()
    }

    @objc private func addTapped() { onAddRequest?() }
    @objc private func closeTapped() { window.close() }

    @objc private func refreshTapped() { refreshNow() }

    func refreshNow() {
        header1.stringValue = "Loading …"
        statusLabel.stringValue = ""
        let bridgeOn = Controller().report().state != .off
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let res = runCLI(["devices"], timeoutSeconds: 30)
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let doc = jsonObject(res.stdout),
                   let devs = doc["devices"] as? [[String: Any]] {
                    self.devices = devs
                } else if res.stdout.isEmpty {
                    self.statusLabel.textColor = .systemRed
                    self.statusLabel.stringValue = "could not read the device list (\(res.stderr))"
                    self.devices = []
                }
                let n = self.devices.count
                let suffix2 = n == 1 ? "" : "s"
                self.header1.stringValue = n == 0 ? "No devices configured" :
                "\(n) device\(suffix2) in the config"
                for v in self.rowsContainer.subviews { v.removeFromSuperview() }
                let rowW = max(120, self.listScroll.bounds.width - 8)
                var y: CGFloat = 6
                let sel = self.selIndex
                for (i, d) in self.devices.enumerated() {
                    let label = (d["label"] as? String) ?? (d["short_name"] as? String) ?? "device"
                    let st = self.statusText(d, bridgeOn: bridgeOn)
                    let row = ClickRowView(height: 46)
                    row.frame = NSRect(x: 0, y: y, width: rowW, height: 46)
                    row.setAttributedText(twoLineText(label, self.detailText(d),
                                                      nameColor: st.1))
                    row.layoutText(width: rowW)
                    row.isSelected = (sel == i)
                    let idx = i
                    row.onClick = { [weak self] in
                        guard let self = self, idx < self.devices.count else { return }
                        for (j, r) in self.rowsContainer.subviews.compactMap({ $0 as? ClickRowView }).enumerated() {
                            r.isSelected = (j == idx)
                        }
                        self.selIndex = idx
                        self.removeBtn.isEnabled = true
                    }
                    self.rowsContainer.addSubview(row)
                    y += 52
                }
                if self.devices.isEmpty {
                    let note = NSTextField(labelWithString:
                        "No devices yet — use \"Add Device…\" below, or run xbridge capture. "
                        + "(For the initial scan a phone must be on this Mac's Wi-Fi.)")
                    note.font = .systemFont(ofSize: 12)
                    note.textColor = .secondaryLabelColor
                    note.maximumNumberOfLines = 3
                    note.frame = NSRect(x: 8, y: 10, width: rowW - 24, height: 56)
                    note.lineBreakMode = .byWordWrapping
                    self.rowsContainer.addSubview(note)
                }
                self.rowsContainer.frame = NSRect(x: 0, y: 0,
                                                  width: rowW, height: max(24, y))
            }
        }
    }

    private func statusText(_ d: [String: Any], bridgeOn: Bool) -> (String, NSColor) {
        if !bridgeOn { return ("idle — bridge is OFF", .secondaryLabelColor) }
        if (d["bridging"] as? Bool) == true { return ("⇄ bridging now", .systemGreen) }
        if (d["bucket"] as? String) == "local" {
            return ("○ on your Wi-Fi — using Xcode built-in", .secondaryLabelColor)
        }
        if (d["online"] as? Bool) == false {
            return ("⚠︎ unreachable — is Tailscale running on the phone?", .systemOrange)
        }
        return ("○ remote — waiting for the bridge to arm…", .secondaryLabelColor)
    }

    private func detailText(_ d: [String: Any]) -> String {
        let fb = (d["fallback_ip"] as? String) ?? "?"
        let node = (d["tailscale_node"] as? String) ?? "?"
        var conn = ""
        if (d["provider"] as? String) == "manual-ip" {
            conn = "Manual IP · \(fb)"
        } else {
            conn = "Tailscale · \(node)"
        }
        if let r = d["resolved_ip"] as? String, !r.isEmpty {
            conn += "  (\(r))"
        }
        let ts = d["tunnel_start"] as? Int ?? 0
        let te = d["tunnel_end"] as? Int ?? 0
        return conn + "   ·   tunnel \(ts)–\(te)"
    }

    @objc private func removeTapped() {
        guard let idx = selIndex, idx < devices.count else { return }
        let d = devices[idx]
        let label = (d["label"] as? String) ?? "this device"
        guard let id = d["id"] as? String, !id.isEmpty else { return }
        let a = NSAlert()
        a.messageText = "Remove \"\(label)\"?"
        a.informativeText = "This only unregisters the phone from xbridge — the phone itself is untouched. It will stop being bridged when it leaves your Wi-Fi."
        a.addButton(withTitle: "Remove")
        a.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard a.runModal() == .alertFirstButtonReturn else { return }
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.stringValue = "Removing …"
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let res = runCLI(["remove-device", "--uuid", id], timeoutSeconds: 30)
            DispatchQueue.main.async {
                guard let self = self else { return }
                if (jsonObject(res.stdout)?["ok"] as? Bool) == true {
                    self.statusLabel.textColor = .systemGreen
                } else {
                    self.statusLabel.textColor = .systemRed
                    let err = (jsonObject(res.stdout)?["error"] as? String)
                        ?? res.stderr.split(separator: "\n").last.map(String.init) ?? "remove failed"
                    self.statusLabel.stringValue = err
                }
                self.selIndex = nil
                self.removeBtn.isEnabled = false
                self.refreshNow()
            }
        }
    }
}

// MARK: - Status item UI

final class MenuBarApp: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let controller = Controller()
    private var timer: Timer?
    private var lastState: XState?
    private var addDeviceWindow: AddDeviceWindow?
    private var devicesWindow: DevicesWindow?

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.accessory)   // menu-bar only, no Dock icon

        statusItem.button?.target = self
        statusItem.button?.action = #selector(iconClicked)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])

        // NB: .menu is intentionally NOT pre-assigned - doing so makes AppKit
        // swallow left clicks and open the menu. Left click = one-click toggle;
        // right / ⌃-click pops the details menu manually (see iconClicked).

        // Auto-register as a login item so the control stays visible after reboots.
        if !controller.loginItemEnabled() { controller.setLoginItem(true) }

        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.poll()
        }
        poll()
    }

    // Rebuilt every open so it always reflects live state.
    private func buildMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false

        let header = NSMenuItem(title: "XBridge", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)

        let r = controller.report()
        let statusItem1 = NSMenuItem(title: "Status: " + stateTitle(r.state), action: nil, keyEquivalent: "")
        statusItem1.isEnabled = false
        menu.addItem(statusItem1)
        if !r.detail.isEmpty {
            let d = NSMenuItem(title: "  " + r.detail, action: nil, keyEquivalent: "")
            d.isEnabled = false
            menu.addItem(d)
        }

        let toggleTitle = r.state == .off ? "⚡  Turn On the bridge" : "Turn Off the bridge"
        let t = NSMenuItem(title: toggleTitle, action: #selector(menuToggle), keyEquivalent: "")
        t.target = self
        t.isEnabled = true
        menu.addItem(t)

        let hint = NSMenuItem(title: "  (left-click the icon also toggles)", action: nil, keyEquivalent: "")
        hint.isEnabled = false
        menu.addItem(hint)

        menu.addItem(.separator())

        let addDev = NSMenuItem(title: "Add Device…", action: #selector(openAddDevice), keyEquivalent: "")
        addDev.target = self
        menu.addItem(addDev)

        let manage = NSMenuItem(title: "Manage Devices…", action: #selector(openDevices), keyEquivalent: "")
        manage.target = self
        menu.addItem(manage)

        let openLog = NSMenuItem(title: "Open log file …", action: #selector(openLog), keyEquivalent: "")
        openLog.target = self
        menu.addItem(openLog)

        let login = NSMenuItem(title: "Launch at Login", action: #selector(toggleLogin), keyEquivalent: "")
        login.target = self
        login.state = controller.loginItemEnabled() ? .on : .off
        menu.addItem(login)

        menu.addItem(.separator())

        let about = NSMenuItem(title: "About XBridge", action: #selector(showAbout), keyEquivalent: "")
        about.target = self
        menu.addItem(about)

        let quit = NSMenuItem(title: "Quit XBridge", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)

        return menu
    }

    private func stateTitle(_ s: XState) -> String {
        switch s {
        case .off:      return "OFF — click to start"
        case .starting: return "STARTING … (auto-watch deciding where the iPhone is)"
        case .armed:    return "ON · BRIDGING — iPhone off your Wi-Fi, traffic via Tailscale"
        case .idle:     return "ON · IDLE — iPhone on your Wi-Fi, using Xcode built-in (no ports held)"
        }
    }

    private func poll() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self = self else { return }
            let r = self.controller.report()
            DispatchQueue.main.async {
                if self.lastState != r.state || true /* cheap: update title every tick */ {
                    self.apply(r)
                }
            }
        }
    }

    private func apply(_ r: Report) {
        lastState = r.state
        let glyph: String
        switch r.state {
        case .off:      glyph = "⏻"   // power symbol — off
        case .starting: glyph = "◌"   // in between
        case .armed:    glyph = "⇄"   // relaying
        case .idle:     glyph = "○"   // on but waiting at home
        }
        statusItem.button?.title = glyph
        statusItem.button?.toolTip = "XBridge: \(stateTitle(r.state))\nLeft-click to toggle · right-click for details"

        // Refresh the menu while it is open (menu.popUp re-renders from this object).
        statusItem.menu = nil
    }

    private func doToggleThen(_ completion: @escaping () -> Void) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let r = self.controller.report()
            if r.state == .off { self.controller.turnOn() } else { self.controller.turnOff() }
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) {
                completion()
            }
        }
    }

    @objc private func iconClicked() {
        let e = NSApp.currentEvent
        let isMenu = e?.type == .rightMouseUp || (e?.modifierFlags.contains(.control) ?? false)
        if isMenu {
            // Pop the details menu below the status icon (synchronous until dismissed).
            if let button = statusItem.button {
                let menu = buildMenu()
                menu.popUp(positioning: nil,
                           at: NSPoint(x: 0, y: button.bounds.height + 5),
                           in: button)
            }
            return
        }
        doToggleThen({ self.poll() })
    }

    @objc private func menuToggle() { doToggleThen({ self.poll(); self.statusItem.menu = self.buildMenu() }) }

    @objc private func openAddDevice() {
        if addDeviceWindow == nil { addDeviceWindow = AddDeviceWindow() }
        addDeviceWindow?.showAndRefresh()
    }

    @objc private func openDevices() {
        if devicesWindow == nil {
            devicesWindow = DevicesWindow()
            devicesWindow?.onAddRequest = { [weak self] in
                self?.openAddDevice()
                if let dw = self?.devicesWindow { dw.window.orderOut(nil) }
            }
        }
        devicesWindow?.refreshNow()
        NSApp.activate(ignoringOtherApps: true)
        devicesWindow?.window.makeKeyAndOrderFront(nil)
    }

    @objc private func openLog() {
        let fm = FileManager.default
        if fm.fileExists(atPath: kLogFile) {
            NSWorkspace.shared.open(URL(fileURLWithPath: kLogFile))
        } else {
            let a = NSAlert()
            a.messageText = "No log file yet"
            a.informativeText = "xbridge has not written to \(kLogFile) in this session."
            a.runModal()
        }
    }

    @objc private func toggleLogin() {
        controller.setLoginItem(!controller.loginItemEnabled())
        statusItem.menu = buildMenu()
    }

    @objc private func showAbout() {
        NSApp.activate(ignoringOtherApps: true)
        let a = NSAlert()
        a.messageText = "XBridge 1.5"
        a.informativeText = """
The free, open-source wireless debugging bridge for Xcode (v1.5: multi-device).

It controls xbridge (~/tools/xbridge), which keeps Xcode's wireless
debugging working when your iPhone is on a different Wi-Fi, by relaying
over your own Tailscale.

• Left-click the menu-bar icon: turn the bridge ON / OFF
• Right-click or ⌃-click:
    - Add Device…  (find an iPhone on your Wi-Fi, pick Tailscale or a
      manual IP, give it a name, save)
    - Manage Devices…  (list / remove configured phones)
    - status, log, launch-at-login, quit

Quitting XBridge does not stop the bridge. Use “Turn Off the bridge”
in this menu to stop it (it will stay off, even after reboot).

Note: the first “Add Device” scan may trigger a macOS permission prompt
for Local Network access — allow it once.
"""
        a.runModal()
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

// MARK: - main

let app = NSApplication.shared
let delegate = MenuBarApp()
app.delegate = delegate
app.run()
