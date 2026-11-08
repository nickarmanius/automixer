import re
p = 'ios/App/App/AppDelegate.swift'
s = open(p).read()

# Extra imports
if 'import AVFoundation' not in s:
    s = s.replace('import UIKit', 'import UIKit\nimport AVFoundation', 1)
if 'import MediaPlayer' not in s:
    s = s.replace('import UIKit', 'import UIKit\nimport MediaPlayer\nimport WebKit', 1)
if 'import UniformTypeIdentifiers' not in s:
    s = s.replace('import UIKit', 'import UIKit\nimport UniformTypeIdentifiers', 1)

# Code that runs at app launch
code = '\n        do { try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: []); try AVAudioSession.sharedInstance().setActive(true) } catch { print(error) }\n'
code += '        let t = NSTemporaryDirectory(); for f in (try? FileManager.default.contentsOfDirectory(atPath: t)) ?? [] { try? FileManager.default.removeItem(atPath: t + f) }\n'
code += '        NowPlayingBridge.shared.start()\n'
s, n = re.subn(r'(didFinishLaunchingWithOptions[^{]*\{)', lambda m: m.group(1) + code, s, count=1)
assert n == 1, 'AppDelegate patch failed'

# Native player (Stage 1): the app itself plays the songs with AVAudioPlayer, shows them in
# Now Playing (Control Center, lock screen, watch) and handles the buttons itself.
# The web page is only the screen. It sends commands through document.title:
#   AMC|id|command|urlencoded-json      (commands: hello, pick, play, resume, pause, next, remove)
# and the app answers by calling window.amNative({...}) and window.amDebug('text').
bridge = r'''

final class NowPlayingBridge: NSObject, AVAudioPlayerDelegate, UIDocumentPickerDelegate {
    static let shared = NowPlayingBridge()

    struct Song {
        let url: URL
        let name: String
    }

    private var webView: WKWebView?
    private var observation: NSKeyValueObservation?
    private var songs: [Song] = []
    private var index = -1
    private var player: AVAudioPlayer?
    private var started = false
    private var busy = false
    private var lastError = ""
    private var lastCommand = ""
    private var lastPlaying = false
    private var tickCount = 0

    private var isPlaying: Bool {
        return player?.isPlaying ?? false
    }

    private var songsDir: URL {
        return FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("AMSongs", isDirectory: true)
    }

    func start() {
        try? FileManager.default.removeItem(at: songsDir)
        try? FileManager.default.createDirectory(at: songsDir, withIntermediateDirectories: true, attributes: nil)
        UIApplication.shared.beginReceivingRemoteControlEvents()
        setupCommands()
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.tick()
        }
    }

    // MARK: page connection

    private func attach() {
        guard let root = UIApplication.shared.delegate?.window??.rootViewController,
              let web = (root as? CAPBridgeViewController)?.bridge?.webView else { return }
        webView = web
        observation = web.observe(\.title, options: [.initial, .new]) { [weak self] w, _ in
            self?.parse(w.title ?? "")
        }
        pushState()
    }

    private func parse(_ raw: String) {
        let parts = raw.components(separatedBy: "|")
        guard parts.count >= 4, parts[0] == "AMC" else { return }
        if raw == lastCommand { return }
        lastCommand = raw
        let cmd = parts[2]
        let text = parts[3].removingPercentEncoding ?? ""
        var args: [String: Any] = [:]
        if let data = text.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            args = obj
        }
        DispatchQueue.main.async { [weak self] in
            self?.handle(cmd, args)
        }
    }

    private func handle(_ cmd: String, _ args: [String: Any]) {
        let i = args["i"] as? Int ?? -1
        switch cmd {
        case "hello": pushState()
        case "pick": pickSongs()
        case "play": startFrom(i)
        case "resume": resume()
        case "pause": pausePlayback()
        case "next": skipNext()
        case "remove": removeSong(i)
        default: break
        }
    }

    private func pushState() {
        guard let web = webView else { return }
        let st: [String: Any] = [
            "songs": songs.map { $0.name },
            "idx": index,
            "playing": isPlaying,
            "started": started,
            "busy": busy,
            "err": lastError
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: st),
              let js = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.amNative && window.amNative(\(js))", completionHandler: nil)
    }

    private func sendDebug() {
        let session = AVAudioSession.sharedInstance()
        let cat = session.category.rawValue.replacingOccurrences(of: "AVAudioSessionCategory", with: "")
        let text = "native cat=\(cat) opt=\(session.categoryOptions.rawValue) songs=\(songs.count) idx=\(index) play=\(isPlaying ? 1 : 0)"
        webView?.evaluateJavaScript("window.amDebug && window.amDebug('\(text)')", completionHandler: nil)
    }

    private func tick() {
        if webView == nil { attach() }
        let now = isPlaying
        if now != lastPlaying {
            lastPlaying = now
            updateNowPlaying()
            pushState()
        }
        tickCount += 1
        if tickCount % 10 == 0 && started { updateNowPlaying() }
        if tickCount % 2 == 0 { sendDebug() }
    }

    private func changed() {
        lastPlaying = isPlaying
        updateNowPlaying()
        pushState()
    }

    // MARK: picking songs

    private func pickSongs() {
        guard var top = UIApplication.shared.delegate?.window??.rootViewController else { return }
        while let next = top.presentedViewController { top = next }
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [UTType.audio], asCopy: true)
        picker.allowsMultipleSelection = true
        picker.delegate = self
        top.present(picker, animated: true, completion: nil)
    }

    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        busy = true
        pushState()
        let dir = songsDir
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var added: [Song] = []
            for u in urls {
                let dest = dir.appendingPathComponent(UUID().uuidString + "-" + u.lastPathComponent)
                do {
                    try FileManager.default.copyItem(at: u, to: dest)
                    added.append(Song(url: dest, name: u.deletingPathExtension().lastPathComponent))
                } catch {
                    continue
                }
                try? FileManager.default.removeItem(at: u)
            }
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.songs.append(contentsOf: added)
                self.busy = false
                self.pushState()
            }
        }
    }

    func documentPickerWasCancelled(_ controller: UIDocumentPickerViewController) {
    }

    // MARK: playback

    private func tryPlay(_ j: Int) -> Bool {
        do {
            let p = try AVAudioPlayer(contentsOf: songs[j].url)
            p.delegate = self
            p.prepareToPlay()
            let session = AVAudioSession.sharedInstance()
            try? session.setCategory(.playback, mode: .default, options: [])
            try? session.setActive(true)
            player?.delegate = nil
            player?.stop()
            player = p
            index = j
            started = true
            p.play()
            return true
        } catch {
            lastError = "Cannot play: " + songs[j].name
            return false
        }
    }

    private func startFrom(_ i: Int) {
        guard !songs.isEmpty, i >= 0, i < songs.count else { return }
        lastError = ""
        for offset in 0..<songs.count {
            let j = (i + offset) % songs.count
            if tryPlay(j) {
                changed()
                return
            }
        }
        player?.delegate = nil
        player?.stop()
        player = nil
        started = false
        index = -1
        changed()
    }

    private func resume() {
        if let p = player, started {
            try? AVAudioSession.sharedInstance().setActive(true)
            p.play()
            changed()
        } else {
            startFrom(0)
        }
    }

    private func pausePlayback() {
        player?.pause()
        changed()
    }

    private func skipNext() {
        guard started, !songs.isEmpty else { return }
        startFrom((index + 1) % songs.count)
    }

    private func removeSong(_ i: Int) {
        guard i >= 0, i < songs.count else { return }
        let s = songs.remove(at: i)
        if i == index {
            player?.delegate = nil
            player?.stop()
            player = nil
            started = false
            index = -1
        } else if i < index {
            index -= 1
        }
        try? FileManager.default.removeItem(at: s.url)
        changed()
    }

    func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) {
        guard p === player, !songs.isEmpty else { return }
        startFrom((index + 1) % songs.count)
    }

    // MARK: Now Playing and buttons

    private func updateNowPlaying() {
        let center = MPNowPlayingInfoCenter.default()
        guard started, index >= 0, index < songs.count, let p = player else {
            center.nowPlayingInfo = nil
            center.playbackState = .stopped
            return
        }
        let info: [String: Any] = [
            MPMediaItemPropertyTitle: songs[index].name,
            MPMediaItemPropertyArtist: "Automixer",
            MPMediaItemPropertyPlaybackDuration: p.duration,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: p.currentTime,
            MPNowPlayingInfoPropertyPlaybackRate: p.isPlaying ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyDefaultPlaybackRate: 1.0,
            MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue
        ]
        center.nowPlayingInfo = info
        center.playbackState = p.isPlaying ? .playing : .paused
    }

    private func setupCommands() {
        let c = MPRemoteCommandCenter.shared()
        c.playCommand.isEnabled = true
        c.pauseCommand.isEnabled = true
        c.togglePlayPauseCommand.isEnabled = true
        c.nextTrackCommand.isEnabled = true
        c.previousTrackCommand.isEnabled = false
        c.playCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async {
                self?.resume()
            }
            return .success
        }
        c.pauseCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async {
                self?.pausePlayback()
            }
            return .success
        }
        c.togglePlayPauseCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if self.isPlaying {
                    self.pausePlayback()
                } else {
                    self.resume()
                }
            }
            return .success
        }
        c.nextTrackCommand.addTarget { [weak self] _ in
            DispatchQueue.main.async {
                self?.skipNext()
            }
            return .success
        }
    }
}
'''
s += bridge
open(p, 'w').write(s)
print('AppDelegate patched (native player, stage 1)')
