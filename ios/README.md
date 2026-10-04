# Climate AI for iPhone (WKWebView wrapper)

A minimal SwiftUI app that shows the Climate AI web app from your home server full screen. It
stores one setting (the server URL) and injects nothing into the page: everything you see is
the same web app a browser gets.

**You may not need it.** The web app is a PWA: open it in Safari, tap Share → **Add to Home
Screen**, and you get a full-screen app icon without Xcode or a developer account. Build this
wrapper if you prefer a real app (it keeps its own sign-in, has pull to refresh, and opens
outside links in Safari).

## Build

Requires a Mac with Xcode 15 or later; the app targets iOS 17+.

```bash
brew install xcodegen
cd ios
xcodegen                 # generates ClimateAI.xcodeproj from project.yml
open ClimateAI.xcodeproj
```

In Xcode: select the **ClimateAI** target → **Signing & Capabilities** → choose your team and
change the bundle identifier from `com.example.climateai` to something of your own (for
example `com.yourname.climateai`). Connect your iPhone, pick it as the run destination and press
Run. A free Apple ID works for personal installs (the app then expires after 7 days and needs a
re-run from Xcode); a paid developer account lasts a year and allows TestFlight.

To change the bundle id or team permanently, edit `project.yml` and run `xcodegen` again (the
generated `.xcodeproj` is not meant to be edited by hand or committed).

## Use

- **First run** asks for the server address: the same URL you use in a browser, e.g.
  `https://climate.example.com` (your public HTTPS name through your gateway, see
  `docs/PUBLIC_ACCESS.md`), `https://climate.example.home` (behind your nginx at home) or
  `http://192.168.1.20:8470` (directly on the LAN, if you set `APP_BIND=0.0.0.0` and
  `CLIMATE_COOKIE_SECURE=false`; WebKit drops a `Secure` cookie over plain http, so sign-in
  would not stick). **Test and save** calls `GET /api/health` and saves the address when the
  server answers.
- iOS asks once for **Local Network** access: allow it, or the app cannot reach the server.
- Sign in with the owner password (there is one login; no user accounts). The app itself
  stores nothing but the server URL: the web app keeps its short-lived bearer token in memory
  and, on each launch, gets a new one from the HttpOnly refresh cookie (`Path=/api/auth`) that
  WebKit keeps in the app's persistent website data. You stay signed in until you sign out,
  sign this phone out from More → Security → signed-in devices, change the password, or the
  sign-in lapses (by default 30 days unused, 90 days at most).
- **Pull down** to reload, **swipe from the edge** to go back/forward.
- **Shake the phone** to change the server address. If the server can't be reached, the app
  shows a screen with *Try again* and *Server settings*.
- Links to other sites (Open-Meteo, docs) open in Safari.

## Notes

- Away from home: the public HTTPS name on your gateway (protected by the owner password and
  real bearer tokens), or Tailscale. Plain `http://` is only allowed to local addresses (IP
  addresses, `*.local` and single-label host names) via `NSAllowsLocalNetworking`, so use it
  on your home network only; anything else must be `https://`. Over Tailscale use the HTTPS
  name your nginx (or `tailscale serve`) provides.
- The web view uses the persistent `WKWebsiteDataStore.default()`. Signing out in the web app
  ends this phone's sign-in and clears the refresh cookie; deleting the app clears everything.
- Service workers are not available inside a third-party WKWebView, so the wrapper has no
  offline cache; it always shows live data.
- In Debug builds the web view is inspectable from Safari's Develop menu on a Mac.

## Files

| File | What it does |
|---|---|
| `project.yml` | XcodeGen spec (iOS 17, iPhone + iPad, portrait + landscape) |
| `ClimateAI/ClimateAIApp.swift` | App entry, first-run routing, shake-to-settings |
| `ClimateAI/WebView.swift` | The WKWebView: persistent sign-in cookie, gestures, pull to refresh, external links, JS dialogs, offline screen |
| `ClimateAI/SettingsView.swift` | Server URL (UserDefaults), health check |
| `ClimateAI/Info.plist` | ATS local networking, local-network prompt text, orientations |
| `ClimateAI/Assets.xcassets` | App icon (the web app's thermometer) |
