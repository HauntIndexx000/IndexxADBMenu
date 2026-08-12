# IndexxADBMenu

A browser-based ADB device console: detects a connected Android/Quest headset
over WebUSB and provides a searchable, categorized reference of ADB commands
with one-click copy.

## Live site

After enabling GitHub Pages (see below), this will be live at:
`https://<your-username>.github.io/IndexxADBMenu/`

## Features

- **Device detection** — uses the WebUSB API (Chrome/Edge desktop only) to
  open your browser's device picker, read the connected device's USB
  descriptors, and confirm whether its ADB interface (class 0xFF, subclass
  0x42, protocol 0x01) is present.
- **Command dashboard** — every command is searchable and filterable by
  category (device, apps, files, perf, display, logs, system, network,
  input). Click any card to copy it to your clipboard.

## Scope / limitations

This detects the device but does **not** execute shell commands from the
browser. Real command execution requires the full ADB authentication
handshake (RSA key generation + signing against the device), which isn't
implemented here. Use the copied commands in a terminal with `adb` installed.

## Running locally

No build step — it's a single static HTML file.

```
git clone https://github.com/<your-username>/IndexxADBMenu.git
cd IndexxADBMenu
# just open index.html in Chrome, or serve it:
python3 -m http.server 8000
```

## License

Personal reference tool — use however you like.
