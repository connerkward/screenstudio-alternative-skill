// events-log.swift — capture-side input event logger for screen recordings.
//
// PURPOSE: This is a demo-recording companion tool, NOT a keylogger. It runs
// ONLY for the duration of a screen recording (started/stopped by the
// recording orchestrator, terminated via SIGINT/SIGTERM when the recording
// ends). Its output feeds post-production: auto-zoom (cursor/click density),
// keystroke overlays, and cursor smoothing. While macOS secure event input
// is active (password fields, sudo prompts, etc.) ALL key events are dropped
// and a single {"type":"secure-input"} marker is written per episode.
//
// USAGE: events-log <output.jsonl>
//   Writes a header line, then JSONL events until SIGINT/SIGTERM, then
//   flushes and exits 0.
//
// OUTPUT (one JSON object per line):
//   {"type":"header","epoch":<unix s>,"display":{"w":<px>,"h":<px>,
//    "pointsW":<pt>,"pointsH":<pt>},"scale":<factor>}
//   {"t":<s since epoch>,"type":"move","x":..,"y":..}          60 Hz cursor samples
//   {"t":..,"type":"down"|"up","x":..,"y":..,"button":"left"|"right"}
//   {"t":..,"type":"key","key":"⌘⇧S"}                          human-readable keys
//   {"t":..,"type":"secure-input"}                              once per secure episode
//   {"t":..,"type":"warning","detail":"no-input-tap"}           degraded mode marker
//
// Coordinates are GLOBAL display points, origin TOP-LEFT (CGEvent space).
// Multiply by "scale" to map to main-display video pixels.
//
// Build: swiftc -O events-log.swift -o events-log
//
// Permissions: clicks + keys need the event tap, which requires Accessibility
// (System Settings → Privacy & Security → Accessibility) and/or Input
// Monitoring (… → Input Monitoring) for the host process (e.g. Terminal).
// Without it the tool degrades to cursor-move sampling only.

import Foundation
import CoreGraphics
import AppKit
import Carbon.HIToolbox

// MARK: - JSON helpers

func jsonEscape(_ s: String) -> String {
    var out = ""
    for scalar in s.unicodeScalars {
        switch scalar {
        case "\"": out += "\\\""
        case "\\": out += "\\\\"
        case "\n": out += "\\n"
        case "\r": out += "\\r"
        case "\t": out += "\\t"
        default:
            if scalar.value < 0x20 {
                out += String(format: "\\u%04x", scalar.value)
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
    }
    return out
}

func num(_ v: CGFloat) -> String { String(format: "%.2f", v) }

// MARK: - Buffered JSONL writer

final class LineWriter {
    private let handle: FileHandle
    private var buffer = Data()
    let epoch: Double

    init?(path: String) {
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let h = FileHandle(forWritingAtPath: path) else { return nil }
        handle = h
        epoch = Date().timeIntervalSince1970
    }

    /// Seconds since header epoch, 3 decimals.
    var t: String { String(format: "%.3f", Date().timeIntervalSince1970 - epoch) }

    func line(_ json: String) {
        buffer.append(Data((json + "\n").utf8))
    }

    func flush() {
        guard !buffer.isEmpty else { return }
        handle.write(buffer)
        buffer.removeAll(keepingCapacity: true)
    }

    func close() {
        flush()
        try? handle.close()
    }
}

// MARK: - Key event → human string

let specialKeys: [Int64: String] = [
    Int64(kVK_Return): "⏎",
    Int64(kVK_ANSI_KeypadEnter): "⏎",
    Int64(kVK_Tab): "⇥",
    Int64(kVK_Space): "␣",
    Int64(kVK_Delete): "⌫",
    Int64(kVK_ForwardDelete): "⌦",
    Int64(kVK_Escape): "⎋",
    Int64(kVK_LeftArrow): "←",
    Int64(kVK_RightArrow): "→",
    Int64(kVK_UpArrow): "↑",
    Int64(kVK_DownArrow): "↓",
    Int64(kVK_Home): "↖",
    Int64(kVK_End): "↘",
    Int64(kVK_PageUp): "⇞",
    Int64(kVK_PageDown): "⇟",
]

func keyString(for event: CGEvent) -> String {
    let keyCode = event.getIntegerValueField(.keyboardEventKeycode)
    let flags = event.flags

    var mods = ""
    if flags.contains(.maskCommand)   { mods += "⌘" }
    if flags.contains(.maskShift)     { mods += "⇧" }
    if flags.contains(.maskAlternate) { mods += "⌥" }
    if flags.contains(.maskControl)   { mods += "⌃" }

    var base: String
    if let special = specialKeys[keyCode] {
        base = special
    } else {
        var length = 0
        var chars = [UniChar](repeating: 0, count: 8)
        event.keyboardGetUnicodeString(maxStringLength: 8,
                                       actualStringLength: &length,
                                       unicodeString: &chars)
        base = length > 0 ? String(utf16CodeUnits: chars, count: length) : ""
        // Reject control chars / NSEvent function-key private range (F-keys etc.)
        let unusable = base.isEmpty || base.unicodeScalars.contains {
            $0.value < 0x20 || (0xF700...0xF8FF).contains($0.value)
        }
        if unusable { base = "vk\(keyCode)" }
    }
    return mods + base
}

// MARK: - Argument parsing

let args = CommandLine.arguments
guard args.count == 2 else {
    FileHandle.standardError.write(Data("usage: events-log <output.jsonl>\n".utf8))
    exit(1)
}

guard let writer = LineWriter(path: args[1]) else {
    FileHandle.standardError.write(Data("events-log: cannot open \(args[1]) for writing\n".utf8))
    exit(1)
}
let log = writer

// MARK: - Header (main display geometry)

let displayID = CGMainDisplayID()
let boundsPt = CGDisplayBounds(displayID)               // points, top-left origin
let mode = CGDisplayCopyDisplayMode(displayID)
let pxW = mode?.pixelWidth ?? CGDisplayPixelsWide(displayID)
let pxH = mode?.pixelHeight ?? CGDisplayPixelsHigh(displayID)
let scale: Double = boundsPt.width > 0
    ? Double(pxW) / Double(boundsPt.width)
    : Double(NSScreen.main?.backingScaleFactor ?? 1.0)

log.line("{\"type\":\"header\",\"epoch\":\(String(format: "%.3f", log.epoch)),"
    + "\"display\":{\"w\":\(pxW),\"h\":\(pxH),"
    + "\"pointsW\":\(num(boundsPt.width)),\"pointsH\":\(num(boundsPt.height))},"
    + "\"scale\":\(String(format: "%.2f", scale))}")
log.flush()

// MARK: - Shared state (main run loop only — no locking needed)

var lastCursor: CGPoint? = nil
var inSecureEpisode = false
var eventTap: CFMachPort? = nil

// MARK: - Event tap callback (clicks + keys)

let tapCallback: CGEventTapCallBack = { _, type, event, _ in
    switch type {
    case .tapDisabledByTimeout, .tapDisabledByUserInput:
        if let tap = eventTap { CGEvent.tapEnable(tap: tap, enable: true) }
    case .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp:
        let loc = event.location  // global points, top-left origin
        let kind = (type == .leftMouseDown || type == .rightMouseDown) ? "down" : "up"
        let button = (type == .leftMouseDown || type == .leftMouseUp) ? "left" : "right"
        log.line("{\"t\":\(log.t),\"type\":\"\(kind)\",\"x\":\(num(loc.x)),"
            + "\"y\":\(num(loc.y)),\"button\":\"\(button)\"}")
    case .keyDown:
        // Privacy guard: never record keys while secure input (password
        // fields, sudo, etc.) is active. One marker per episode.
        if IsSecureEventInputEnabled() {
            if !inSecureEpisode {
                inSecureEpisode = true
                log.line("{\"t\":\(log.t),\"type\":\"secure-input\"}")
            }
        } else {
            inSecureEpisode = false
            log.line("{\"t\":\(log.t),\"type\":\"key\",\"key\":\"\(jsonEscape(keyString(for: event)))\"}")
        }
    default:
        break
    }
    return Unmanaged.passUnretained(event)
}

// MARK: - Create the listen-only session tap

let tapMask: CGEventMask =
    (1 << CGEventType.leftMouseDown.rawValue) |
    (1 << CGEventType.leftMouseUp.rawValue) |
    (1 << CGEventType.rightMouseDown.rawValue) |
    (1 << CGEventType.rightMouseUp.rawValue) |
    (1 << CGEventType.keyDown.rawValue)

eventTap = CGEvent.tapCreate(tap: .cgSessionEventTap,
                             place: .tailAppendEventTap,
                             options: .listenOnly,
                             eventsOfInterest: tapMask,
                             callback: tapCallback,
                             userInfo: nil)

if let tap = eventTap {
    let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
    CGEvent.tapEnable(tap: tap, enable: true)
} else {
    FileHandle.standardError.write(Data("""
    events-log: could not create the input event tap (clicks/keys unavailable).
    Grant this process (e.g. Terminal) permission in:
      System Settings → Privacy & Security → Accessibility
      System Settings → Privacy & Security → Input Monitoring
    Continuing in degraded mode: cursor-move sampling only.\n
    """.utf8))
    log.line("{\"t\":\(log.t),\"type\":\"warning\",\"detail\":\"no-input-tap\"}")
    log.flush()
    // If even cursor polling is unavailable there is nothing left to log.
    if CGEvent(source: nil)?.location == nil {
        FileHandle.standardError.write(Data("events-log: cursor polling also unavailable; nothing to capture. Exiting.\n".utf8))
        log.close()
        exit(2)
    }
}

// MARK: - 60 Hz cursor sampling (works without any tap)

let moveTimer = Timer(timeInterval: 1.0 / 60.0, repeats: true) { _ in
    guard let loc = CGEvent(source: nil)?.location else { return }
    if let last = lastCursor, abs(last.x - loc.x) < 0.01, abs(last.y - loc.y) < 0.01 {
        return  // unchanged — skip duplicate sample
    }
    lastCursor = loc
    log.line("{\"t\":\(log.t),\"type\":\"move\",\"x\":\(num(loc.x)),\"y\":\(num(loc.y))}")
}
RunLoop.main.add(moveTimer, forMode: .common)

// MARK: - Periodic flush (1 s)

let flushTimer = Timer(timeInterval: 1.0, repeats: true) { _ in
    log.flush()
}
RunLoop.main.add(flushTimer, forMode: .common)

// MARK: - Clean shutdown on SIGINT / SIGTERM

func installSignalHandler(_ sig: Int32) -> DispatchSourceSignal {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler {
        log.close()
        exit(0)
    }
    source.resume()
    return source
}

let sigintSource = installSignalHandler(SIGINT)
let sigtermSource = installSignalHandler(SIGTERM)

CFRunLoopRun()
