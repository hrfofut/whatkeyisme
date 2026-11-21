# whatkeyisme

> Key & BPM data from [GetSongBPM](https://getsongbpm.com).

Live page: https://hrfofut.github.io/whatkeyisme/ (demo only — the real thing runs locally).

A Spotify-style page that shows the track you're playing, a working play bar (play/pause, skip, seek, shuffle, repeat, volume), and the song's **key signature**: key, sharps/flats, relative key, Camelot code, BPM, time signature, the scale on a piano, and harmonically compatible keys for mixing.

## Why there's a small server

Spotify stopped giving key/tempo data (`/audio-features`) to new apps in Nov 2024. This page tries Spotify first anyway (older apps may still have access), then falls back to **GetSongBPM** (free API, key + BPM). `server.py` runs that lookup so your GetSongBPM key stays private and browser CORS isn't a problem. It's plain Python with no installs.

## Setup (≈3 minutes)

1. **Spotify Developer Dashboard → your app → Edit → Redirect URIs → add:**
   `http://127.0.0.1:5000/callback`
   Spotify no longer accepts `localhost` redirect URIs (since Nov 2025).
2. **User Management tab:** make sure your Spotify account's email is listed. Development-mode apps only work for listed users, and the app owner needs Premium.
3. **GetSongBPM key (for automatic key lookup):** request one at https://getsongbpm.com/api. It's free. They require a visible link back to them, which this page already includes in the corner.
4. Run:
   ```
   python server.py        # Windows
   python3 server.py       # macOS / Linux
   ```
   Then open **http://127.0.0.1:5000**. Use the IP; `localhost` breaks the login.
5. Paste your Spotify Client ID and the GetSongBPM key in the setup screen (⚙ in the top-right), click **Connect Spotify**, and press play anywhere.

To preview without any keys, open `http://127.0.0.1:5000/?demo`.

## Notes

- **Key wrong or missing?** Use the "Wrong key? Set it…" menu. Your choice is saved per track in the browser.
- Lookups are cached in `key_cache.json`, so each song hits GetSongBPM only once.
- **Port 5000 busy?** On macOS, AirPlay Receiver uses port 5000. Run `PORT=8888 python3 server.py` and register `http://127.0.0.1:8888/callback` as the redirect URI instead.
- Playback controls need Spotify Premium and an active device. Space toggles play/pause, and the ←/→ keys seek when the progress bar is focused.
