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
    private var keepAlive: AVAudioPlayer?

    func start() {
        UIApplication.shared.beginReceivingRemoteControlEvents()
        Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            if self.webView == nil {
                self.attach()
            } else {
                self.publish()
            }
        }
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
        title = parts[2].removingPercentEncoding ?? ""
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
        setKeepAlive(state == "1")
        let center = MPNowPlayingInfoCenter.default()
        if state == "x" {
            center.nowPlayingInfo = nil
            center.playbackState = .stopped
        } else {
            let playing = state == "1"
            let info: [String: Any] = [
                MPMediaItemPropertyTitle: title,
                MPMediaItemPropertyArtist: artist,
                MPNowPlayingInfoPropertyPlaybackRate: playing ? 1.0 : 0.0
            ]
            center.nowPlayingInfo = info
            center.playbackState = playing ? .playing : .paused
        }
        sendDebug(session)
    }

    // Plays one second of pure silence in a loop inside the app itself while the music plays.
    // The real music is played by the web view, which iOS does not count as the app playing.
    // With this, iOS treats Automixer as the playing app right from the start.
    private func setKeepAlive(_ on: Bool) {
        if on {
            if keepAlive == nil {
                let path = NSTemporaryDirectory() + "am_silence.wav"
                if !FileManager.default.fileExists(atPath: path) {
                    func le32(_ v: UInt32) -> [UInt8] { return [UInt8(v & 255), UInt8((v >> 8) & 255), UInt8((v >> 16) & 255), UInt8((v >> 24) & 255)] }
                    func le16(_ v: UInt16) -> [UInt8] { return [UInt8(v & 255), UInt8((v >> 8) & 255)] }
                    let dataSize: UInt32 = 16000
                    var bytes = [UInt8]()
                    bytes += Array("RIFF".utf8); bytes += le32(36 + dataSize); bytes += Array("WAVEfmt ".utf8)
                    bytes += le32(16); bytes += le16(1); bytes += le16(1); bytes += le32(8000); bytes += le32(16000)
                    bytes += le16(2); bytes += le16(16); bytes += Array("data".utf8); bytes += le32(dataSize)
                    bytes += [UInt8](repeating: 0, count: Int(dataSize))
                    FileManager.default.createFile(atPath: path, contents: Data(bytes))
                }
                keepAlive = try? AVAudioPlayer(contentsOf: URL(fileURLWithPath: path))
                keepAlive?.numberOfLoops = -1
                keepAlive?.prepareToPlay()
            }
            if keepAlive?.isPlaying != true { keepAlive?.play() }
        } else if keepAlive?.isPlaying == true {
            keepAlive?.pause()
        }
    }

    private func sendDebug(_ session: AVAudioSession) {
        let cat = session.category.rawValue.replacingOccurrences(of: "AVAudioSessionCategory", with: "")
        let text = "cat=\(cat) opt=\(session.categoryOptions.rawValue) fix=\(fixes) pub=\(pubCount) st=\(state)"
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
