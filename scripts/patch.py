import re
# AUTOMIXER NATIVE - STAGE 1
p = 'ios/App/App/AppDelegate.swift'
s = open(p).read()

# Extra imports
for imp in ['AVFoundation', 'MediaPlayer', 'WebKit', 'UniformTypeIdentifiers']:
    if 'import ' + imp not in s:
        s = s.replace('import UIKit', 'import UIKit\nimport ' + imp, 1)

# Code that runs at app launch
code = '\n        do { try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: []); try AVAudioSession.sharedInstance().setActive(true) } catch { print(error) }\n'
code += '        let t = NSTemporaryDirectory(); for f in (try? FileManager.default.contentsOfDirectory(atPath: t)) ?? [] { try? FileManager.default.removeItem(atPath: t + f) }\n'
code += '        NativePlayer.shared.start()\n'
s, n = re.subn(r'(didFinishLaunchingWithOptions[^{]*\{)', lambda m: m.group(1) + code, s, count=1)
assert n == 1, 'AppDelegate patch failed'

# Native player (stage 1): picks songs, plays them one after another with AVAudioPlayer,
# shows them in Now Playing and handles the remote buttons. The web page is only the screen.
# Page -> native: document.title = 'AMC|number|command|index'
# Native -> page: window.amState({...})
player = r'''

final class NativePlayer: NSObject, UIDocumentPickerDelegate, AVAudioPlayerDelegate {
    static let shared = NativePlayer()
    private weak var webView: WKWebView?
    private var songs: [(name: String, url: URL)] = []
    private var index = -1
    private var player: AVAudioPlayer?
    private var started = false
    private var attached = false
    private var observation: NSKeyValueObservation?
    private var lastSeq = ""
    private var lastError = ""
    private var nextPlayer: AVAudioPlayer?
    private var nextIndex = -1
    private var ended = false
    private var log: [String] = []

    private func note(_ s: String) {
        log.append(s)
        if log.count > 6 { log.removeFirst() }
    }

    // Reads the whole song into memory, so that the file is not needed again while the phone is locked.
    private func makePlayer(_ url: URL) throws -> AVAudioPlayer {
        if let data = try? Data(contentsOf: url), let p = try? AVAudioPlayer(data: data) {
            return p
        }
        return try AVAudioPlayer(contentsOf: url)
    }

    // Opens the next song ahead of time, so that nothing has to be read from disk when the screen is locked.
    private func prepareNext() {
        nextPlayer = nil
        nextIndex = -1
        guard songs.count > 0, index >= 0 else { return }
        let n = (index + 1) % songs.count
        do {
            let np = try makePlayer(songs[n].url)
            np.prepareToPlay()
            nextPlayer = np
            nextIndex = n
        } catch {
            note("prep fail " + error.localizedDescription)
        }
    }

    func start() {
        UIApplication.shared.beginReceivingRemoteControlEvents()
        setupCommands()
        NotificationCenter.default.addObserver(self, selector: #selector(interruption(_:)), name: AVAudioSession.interruptionNotification, object: nil)
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if !self.attached {
                self.attach()
            } else {
                self.pushState()
            }
        }
    }

    private func attach() {
        guard let root = UIApplication.shared.delegate?.window??.rootViewController,
              let web = (root as? CAPBridgeViewController)?.bridge?.webView else { return }
        webView = web
        attached = true
        observation = web.observe(\.title, options: [.initial, .new]) { [weak self] w, _ in
            self?.parse(w.title ?? "")
        }
    }

    // The page sends commands through document.title as: AMC|number|command|songIndex
    private func parse(_ raw: String) {
        let parts = raw.components(separatedBy: "|")
        guard parts.count >= 4, parts[0] == "AMC" else { return }
        if parts[1] == lastSeq { return }
        lastSeq = parts[1]
        let cmd = parts[2]
        let i = Int(parts[3]) ?? -1
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            switch cmd {
            case "pick": self.pickSongs()
            case "toggle": self.toggle()
            case "next": self.next()
            case "play": self.play(at: i)
            case "remove": self.remove(at: i)
            default: self.pushState()
            }
        }
    }

    private func topController() -> UIViewController? {
        var top = UIApplication.shared.delegate?.window??.rootViewController
        while let p = top?.presentedViewController { top = p }
        return top
    }

    private func pickSongs() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [UTType.audio], asCopy: true)
        picker.allowsMultipleSelection = true
        picker.delegate = self
        topController()?.present(picker, animated: true, completion: nil)
    }

    func documentPicker(_ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]) {
        for u in urls {
            // Make the file readable while the phone is locked.
            try? FileManager.default.setAttributes([.protectionKey: FileProtectionType.none], ofItemAtPath: u.path)
            songs.append((name: u.deletingPathExtension().lastPathComponent, url: u))
        }
        if let f = urls.first, let a = try? FileManager.default.attributesOfItem(atPath: f.path),
           let k = a[.protectionKey] as? FileProtectionType {
            note("prot=" + k.rawValue)
        }
        pushState()
    }

    private func play(at i: Int) {
        guard i >= 0 && i < songs.count else { return }
        var chosen: AVAudioPlayer? = nil
        if i == nextIndex, let pre = nextPlayer { chosen = pre }
        nextPlayer = nil
        nextIndex = -1
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .default, options: [])
        try? session.setActive(true)
        do {
            if chosen == nil { chosen = try makePlayer(songs[i].url) }
            if let np = chosen {
                np.delegate = self
                player?.stop()
                player = np
                index = i
                started = true
                ended = false
                lastError = ""
                let ok = np.play()
                note("play \(i) ok=\(ok)")
            }
        } catch {
            lastError = error.localizedDescription
            note("play \(i) fail " + error.localizedDescription)
        }
        prepareNext()
        updateNowPlaying()
        pushState()
    }

    private func pause() {
        guard let p = player, p.isPlaying else { return }
        p.pause()
        updateNowPlaying()
        pushState()
    }

    private func resume() {
        if ended {
            next()
            return
        }
        if let p = player, started {
            if !p.isPlaying {
                try? AVAudioSession.sharedInstance().setActive(true)
                _ = p.play()
                updateNowPlaying()
                pushState()
            }
        } else if !songs.isEmpty {
            play(at: 0)
        }
    }

    private func toggle() {
        if let p = player, p.isPlaying {
            pause()
        } else {
            resume()
        }
    }

    private func previous() {
        guard !songs.isEmpty else { return }
        if let p = player, p.currentTime > 3 {
            p.currentTime = 0
            updateNowPlaying()
            pushState()
            return
        }
        play(at: (index - 1 + songs.count) % songs.count)
    }

    private func next() {
        guard !songs.isEmpty else { return }
        play(at: (index + 1) % songs.count)
    }

    private func remove(at i: Int) {
        guard i >= 0 && i < songs.count else { return }
        if i == index {
            player?.stop()
            player = nil
            started = false
            index = -1
            MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        } else if i < index {
            index -= 1
        }
        try? FileManager.default.removeItem(at: songs[i].url)
        songs.remove(at: i)
        if started {
            prepareNext()
        } else {
            nextPlayer = nil
            nextIndex = -1
        }
        pushState()
    }

    func audioPlayerDidFinishPlaying(_ finished: AVAudioPlayer, successfully flag: Bool) {
        note("finished ok=\(flag)")
        guard finished === player, !songs.isEmpty else { return }
        ended = true
        play(at: (index + 1) % songs.count)
    }

    @objc private func interruption(_ n: Notification) {
        guard let v = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: v) else { return }
        let opts = n.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            if type == .began {
                self.updateNowPlaying()
                self.pushState()
            } else if AVAudioSession.InterruptionOptions(rawValue: opts).contains(.shouldResume) {
                self.resume()
            }
        }
    }

    private func updateNowPlaying() {
        let center = MPNowPlayingInfoCenter.default()
        guard started, index >= 0, index < songs.count, let p = player else {
            center.nowPlayingInfo = nil
            return
        }
        let rc = MPRemoteCommandCenter.shared()
        rc.nextTrackCommand.isEnabled = true
        rc.previousTrackCommand.isEnabled = true
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
        c.previousTrackCommand.isEnabled = true
        c.playCommand.addTarget { [weak self] _ -> MPRemoteCommandHandlerStatus in
            DispatchQueue.main.async { self?.resume() }
            return .success
        }
        c.pauseCommand.addTarget { [weak self] _ -> MPRemoteCommandHandlerStatus in
            DispatchQueue.main.async { self?.pause() }
            return .success
        }
        c.togglePlayPauseCommand.addTarget { [weak self] _ -> MPRemoteCommandHandlerStatus in
            DispatchQueue.main.async { self?.toggle() }
            return .success
        }
        c.previousTrackCommand.addTarget { [weak self] _ -> MPRemoteCommandHandlerStatus in
            DispatchQueue.main.async { self?.previous() }
            return .success
        }
        c.nextTrackCommand.addTarget { [weak self] _ -> MPRemoteCommandHandlerStatus in
            DispatchQueue.main.async { self?.next() }
            return .success
        }
    }

    private func pushState() {
        guard let web = webView else { return }
        let d: [String: Any] = [
            "songs": songs.map { $0.name },
            "idx": index,
            "playing": player?.isPlaying ?? false,
            "started": started,
            "pos": player?.currentTime ?? 0.0,
            "dur": player?.duration ?? 0.0,
            "err": lastError,
            "log": log.joined(separator: " | ")
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: d),
              let json = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.amState && window.amState(\(json))", completionHandler: nil)
    }
}
'''
s += player
open(p, 'w').write(s)
print('AppDelegate patched (native stage 1)')
