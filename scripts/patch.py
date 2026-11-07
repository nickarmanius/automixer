import re
p = 'ios/App/App/AppDelegate.swift'
s = open(p).read()

# Extra imports
if 'import AVFoundation' not in s:
    s = s.replace('import UIKit', 'import UIKit\nimport AVFoundation', 1)
if 'import MediaPlayer' not in s:
    s = s.replace('import UIKit', 'import UIKit\nimport MediaPlayer\nimport WebKit', 1)

# Code that runs at app launch
code = '\n        do { try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: []); try AVAudioSession.sharedInstance().setActive(true) } catch { print(error) }\n'
code += '        let t = NSTemporaryDirectory(); for f in (try? FileManager.default.contentsOfDirectory(atPath: t)) ?? [] { try? FileManager.default.removeItem(atPath: t + f) }\n'
code += '        NowPlayingBridge.shared.start()\n'
s, n = re.subn(r'(didFinishLaunchingWithOptions[^{]*\{)', lambda m: m.group(1) + code, s, count=1)
assert n == 1, 'AppDelegate patch failed'

# Native bridge: shows what Automixer is playing in the iPhone's Now Playing
# (Control Center, lock screen, Bluetooth devices, watch) and passes the buttons back to the page.
# The page publishes its state through document.title as: AM|state|title|artist
# state: 1 = playing, 0 = paused, x = nothing.
bridge = r'''

final class NowPlayingBridge: NSObject {
    static let shared = NowPlayingBridge()
    private var webView: WKWebView?
    private var observation: NSKeyValueObservation?
    private var title = ""
    private var artist = ""
    private var state = "x"
    private var fixes = 0
    private var pubCount = 0
    private var elapsed = 0.0
    private var lastPub = Date()
    private var keeper: AVAudioPlayer?
    private var keeperWanted = true

    func start() {
        UIApplication.shared.beginReceivingRemoteControlEvents()
        startKeeper()
        NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] n in
            if let v = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
               AVAudioSession.InterruptionType(rawValue: v) == .ended {
                try? AVAudioSession.sharedInstance().setActive(true)
                if self?.keeperWanted == true { self?.keeper?.play() }
            }
        }
        Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if let k = self.keeper { if self.keeperWanted && !k.isPlaying { k.play() } else if !self.keeperWanted && k.isPlaying { k.stop() } }
            if self.webView == nil {
                self.attach()
            } else {
                self.publish()
            }
        }
    }

    private func startKeeper() {
        guard keeper == nil else { return }
        let n = 16000
        var d = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append("RIFF".data(using: .ascii)!); u32(UInt32(36 + n * 2))
        d.append("WAVEfmt ".data(using: .ascii)!); u32(16); u16(1); u16(1); u32(8000); u32(16000); u16(2); u16(16)
        d.append("data".data(using: .ascii)!); u32(UInt32(n * 2))
        d.append(Data(count: n * 2))
        guard let p = try? AVAudioPlayer(data: d) else { return }
        p.numberOfLoops = -1
        p.volume = 1.0
        p.prepareToPlay()
        p.play()
        keeper = p
    }

    private func attach() {
        guard let root = UIApplication.shared.delegate?.window??.rootViewController,
              let web = (root as? CAPBridgeViewController)?.bridge?.webView else { return }
        webView = web
        observation = web.observe(\.title, options: [.initial, .new]) { [weak self] w, _ in
            self?.parse(w.title ?? "")
        }
        setupCommands()
    }

    private func parse(_ raw: String) {
        let parts = raw.components(separatedBy: "|")
        guard parts.count >= 4, parts[0] == "AM" else { return }
        state = parts[1]
        keeperWanted = (state != "1")
        if state == "1" { keeper?.stop() } else if let k = keeper, !k.isPlaying { k.play() }
        let newTitle = parts[2].removingPercentEncoding ?? ""
        if newTitle != title { elapsed = 0 }
        title = newTitle
        artist = parts[3].removingPercentEncoding ?? ""
        DispatchQueue.main.async { [weak self] in
            self?.publish()
        }
    }

    private func publish() {
        let session = AVAudioSession.sharedInstance()
        // WebKit may switch the audio session to a mixing mode, which keeps the app out of Now Playing.
        // While something is playing or paused, put it back to plain playback.
        if state != "x" && (session.category != .playback || session.categoryOptions.contains(.mixWithOthers)) {
            fixes += 1
            try? session.setCategory(.playback, mode: .default, options: [])
            try? session.setActive(true)
        }
        pubCount += 1
        let nowDate = Date()
        if state == "1" { elapsed += nowDate.timeIntervalSince(lastPub) }
        lastPub = nowDate
        let center = MPNowPlayingInfoCenter.default()
        if state == "x" {
            center.nowPlayingInfo = nil
            center.playbackState = .stopped
        } else {
            let playing = state == "1"
            let info: [String: Any] = [
                MPMediaItemPropertyTitle: title,
                MPMediaItemPropertyArtist: artist,
                MPNowPlayingInfoPropertyPlaybackRate: playing ? 1.0 : 0.0,
                MPNowPlayingInfoPropertyDefaultPlaybackRate: 1.0,
                MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue,
                MPNowPlayingInfoPropertyIsLiveStream: true,
                MPNowPlayingInfoPropertyElapsedPlaybackTime: elapsed
            ]
            center.nowPlayingInfo = info
            center.playbackState = playing ? .playing : .paused
        }
        sendDebug(session)
    }

    private func sendDebug(_ session: AVAudioSession) {
        let cat = session.category.rawValue.replacingOccurrences(of: "AVAudioSessionCategory", with: "")
        let text = "cat=\(cat) opt=\(session.categoryOptions.rawValue) fix=\(fixes) pub=\(pubCount) st=\(state) kp=\(keeper?.isPlaying == true ? 1 : 0)"
        webView?.evaluateJavaScript("window.amDebug && window.amDebug('\(text)')", completionHandler: nil)
    }

    private func setupCommands() {
        let c = MPRemoteCommandCenter.shared()
        c.playCommand.isEnabled = true
        c.pauseCommand.isEnabled = true
        c.togglePlayPauseCommand.isEnabled = true
        c.nextTrackCommand.isEnabled = true
        c.previousTrackCommand.isEnabled = false
        c.playCommand.addTarget { [weak self] _ in
            self?.send("play")
            return .success
        }
        c.pauseCommand.addTarget { [weak self] _ in
            self?.send("pause")
            return .success
        }
        c.togglePlayPauseCommand.addTarget { [weak self] _ in
            self?.send(self?.state == "1" ? "pause" : "play")
            return .success
        }
        c.nextTrackCommand.addTarget { [weak self] _ in
            self?.send("next")
            return .success
        }
    }

    private func send(_ command: String) {
        DispatchQueue.main.async { [weak self] in
            self?.webView?.evaluateJavaScript("window.amRemote && window.amRemote('\(command)')", completionHandler: nil)
        }
    }
}
'''
s += bridge
open(p, 'w').write(s)
print('AppDelegate patched')
