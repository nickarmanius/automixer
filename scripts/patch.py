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
        let center = MPNowPlayingInfoCenter.default()
        if state == "x" {
            center.nowPlayingInfo = nil
            center.playbackState = .stopped
            return
        }
        let playing = state == "1"
        let info: [String: Any] = [
            MPMediaItemPropertyTitle: title,
            MPMediaItemPropertyArtist: artist,
            MPNowPlayingInfoPropertyPlaybackRate: playing ? 1.0 : 0.0
        ]
        center.nowPlayingInfo = info
        center.playbackState = playing ? .playing : .paused
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
