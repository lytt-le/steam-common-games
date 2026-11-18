# Steam Common Games

Pick any number of your Steam friends and see which games you all own.

- Friends list with avatars, online status and search
- **Everyone owns** — games you and every selected friend have
- **Anyone owns** — your games owned by at least one selected friend, with who owns each
- Sort by name, number of owners or your playtime; filter by name
- Friends with private libraries are flagged and skipped instead of emptying the results
- Runs locally; shuts itself down when you close the tab

## Requirements

- Python 3.10+ (standard library only — nothing to install)
- A free [Steam Web API key](https://steamcommunity.com/dev/apikey) (any domain name, e.g. `localhost`)
- Your Steam **Friends List** and **Game details** set to Public in privacy settings

## Run

```sh
python3 app.py
```

This starts a server on `http://127.0.0.1:8765` and opens your browser. On first run it asks for
your API key and Steam profile (URL, custom name or SteamID64). These are stored in
`~/.config/steam-common-games/config.json` with `600` permissions.

Options:

- `--keep-alive` — don't shut down when the last tab closes
- `--no-browser` — don't open a browser tab on start
- `PORT=9000 python3 app.py` — use a different port

### Linux desktop launcher

```sh
cat > ~/.local/share/applications/steam-common-games.desktop <<DESK
[Desktop Entry]
Type=Application
Name=Steam Common Games
Exec=python3 $PWD/app.py
Icon=steam
Terminal=false
Categories=Game;Utility;
DESK
```

## How it works

`app.py` proxies the Steam Web API (`GetFriendList`, `GetPlayerSummaries`, `GetOwnedGames`,
`ResolveVanityURL`) — browsers can't call it directly — and caches responses for 15 minutes.
`index.html` does the matching client-side.
