# Discord Mega-link Indexer Bot

A two-server Discord bot that **reads** Mega.nz download links posted in a
*source* server and **relays** them to your *user-facing* server in response
to `/get <game>` or `/search <query>`.

It does **not** download or store any files. The original Mega URLs are
handed out as ephemeral embeds, so they don't end up persisting in your
public channels.

> Use this responsibly. The Mega links it relays are user-posted elsewhere;
> you are responsible for respecting copyright, the source server's rules, and
> Discord's Terms of Service.

---

## Architecture in one paragraph

* The bot joins your user-facing server plus **one or more source servers**
  where Mega links get posted. Add as many `SOURCE_GUILD_IDS` and
  `SOURCE_CHANNELS` entries as you need; the indexer fans out across all of
  them.
* On startup it scans every source channel's message history (resumable from
  the last seen message id), runs a multi-format parser (regex over content +
  embed fields), and stores every distinct Mega link + extracted metadata in
  a local SQLite database.
* Members of the user-facing server type `/get cyberpunk 2077` and the bot
  does `rapidfuzz` against Steam's full app list (~150k titles cached
  locally), narrows down to a Steam app id, looks up matching Mega links,
  and posts them as an ephemeral Discord embed with "Open" buttons.

---

## Setup

### 1. Discord Application + Bot

1. Visit https://discord.com/developers/applications and click **New Application**.
2. Give it a name, save.
3. Sidebar → **Bot** → click **Reset Token**, copy it (one-time). That's
   `DISCORD_TOKEN`.
4. **Important — Privileged Intents**: scroll down and enable
   **Message Content Intent** (the bot reads message bodies to find Mega
   links). Save.
5. Sidebar → **OAuth2 → URL Generator**:
   * Scopes: `bot`, `applications.commands`
   * Bot permissions: `View Channels`, `Send Messages`, `Read Message
     History`, `Embed Links` (and `Use External Emojis` if you want
     custom reaction buttons).
6. Use the generated URL to invite the bot to **both** your source server
   and your user-facing server.
7. In each server, enable Developer Mode (Settings → Advanced), then
   right-click the server icon → **Copy Server ID** → for source servers
   this becomes one entry in `SOURCE_GUILD_IDS` (comma-separated). For your
   user-facing server it's `TARGET_GUILD_ID`. Same trick on the source
   channel → `SOURCE_CHANNELS` (also comma-separated).

### 2. Local clone & install

```bash
git clone <your-repo> mega-indexer
cd mega-indexer
python3.12 -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
$EDITOR .env                 # fill in DISCORD_TOKEN, IDs, etc.
```

### 3. Smoke run

```bash
python main.py
```

On first launch the bot will:

1. Create `data/bot.sqlite` from `schema.sql`.
2. Fetch the Steam app list (~150k entries) and cache it locally (~10 s).
3. Sync slash commands to your `TARGET_GUILD_ID`.
4. Start backfilling every `SOURCE_CHANNELS` id.

Logs go to stdout. Watch for `Indexed <channel_id>` lines.

---

## Deploy

Two production-grade options.

### Option A — Render (free sleeping tier)

You picked the sleeping tier. Be aware that while the free instance sleeps,
**new source-channel messages are missed** until either the bot wakes up
(resume triggers a reconnect-backfill) or you send a restart manually.

1. Push the repo to GitHub.
2. Render dashboard → **New → Web Service** (NOT Background Worker — Render
   free sleeps web services more aggressively; choose **Background Worker**
   if available in your plan, or **Private Service**).
3. Build: `Dockerfile`.
4. Add the env vars from `.env.example`.
5. Mount a **persistent disk** at `/app/data` so the SQLite file survives
   restarts.
6. Deploy. Tail logs to confirm bootstrap.

### Option B — Oracle Cloud Always Free ARM (recommended)

24/7 free, no sleep. See [Oracle's signup docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm).

1. Provision an Ampere A1 (4 OCPU, 24 GB RAM) instance. Ubuntu 22.04+.
2. SSH in, `git clone` the repo, set up `venv`, `pip install -r
   requirements.txt`, copy `.env`.
3. Run under `systemd`:

```ini
# /etc/systemd/system/mega-indexer.service
[Unit]
Description=Discord Mega-link indexer bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/mega-indexer
ExecStart=/home/ubuntu/mega-indexer/.venv/bin/python main.py
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/mega-indexer/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mega-indexer
sudo journalctl -u mega-indexer -f
```

### Option C — Hetzner CX22 (€4.35/mo)

Same systemd unit. Best paid-vps reliability-to-cost ratio.

---

## Discord commands

### Public (target guild)

| Command | Description |
|---------|-------------|
| `/get <query>` | Look up Mega.nz link by game name, partial name, or Steam app ID. Autocomplete suggests matches. |
| `/search <query>` | List multiple matches with "Open" buttons. |

Both reply **ephemerally** (only the caller sees the link — no public
spam). Per-user cooldown (`COOLDOWN_SECONDS`, default 5 s). Optional role
gate via `ALLOWED_ROLE_NAME`.

### Admin (target guild, only users in `ADMIN_USER_IDS`)

| Command | Description |
|---------|-------------|
| `/admin stats` | Indexer counts + per-channel status. |
| `/admin reseed <channel>` | Re-scan whole channel history from scratch. |
| `/admin delete_entry <id>` | Remove a noisy entry. |
| `/admin refresh_steam` | Force-refresh Steam app list cache. |
| `/admin parser_test <text>` | Dry-run the parser against arbitrary text. Useful when tuning against real messages. |
| `/admin link_channel <channel>` | Add a source channel at runtime (persists in DB). |
| `/admin unlink_channel <channel>` | Remove a source channel at runtime. |

---

## How the parser works

`parser.parse_message(text, ...)` runs over both `message.content` and the
message's embeds (title, description, url, each field). It pulls:

* Every `https://mega.nz/(file|folder)/KEY#HASH` URL — supports multiple
  per message and de-duplicates.
* Steam app ID from explicit phrases ("App ID:", "Steam ID:", "Application
  ID:", "App #") or from `store.steampowered.com/app/<id>` URLs.
* Game title from labeled lines (`Title:`, `Game:`, `Name:`) or — as a
  fallback — from the closest filename, with archive extensions stripped.
* File size from `<number> GB` / `MB` / `TB` patterns.
* Archive filename via a same-line lookback from each Mega URL.

When the parser is told `app_id`, the Steam cache lookup enriches the row
with `canonical_name` so `/get` resolutions are clean.

To tune the parser for your real source-channel format, paste an actual
sample message into `/admin parser_test`; iterate on the regexes in
`parser.py` until the embed shows the right name + appid + filename.

---

## Operational notes

* **Cold backfill** on first launch can take hours on a large source
  channel (Discord rate limits ~5 history req/2 s). The bot persists
  `last_seen_message_id` in the `sources` table so subsequent restarts
  only scan the next page.
* **Gateway reconnect** (`on_resumed`) automatically re-scans everything
  since `last_seen_message_id`, so it's safe to leave running through
  Deploy restarts.
* **Mega link staleness**: Mega frequently takes down files for quota or
  takedown. By default we don't probe their heads (Mega blocks bot HEAD
  requests), so `is_stale` stays `false`. If you enable
  `MEGA_STALENESS_CHECK=true` you get periodic verification but expect
  false positives from Mega's anti-bot layer.
* **SQLite WAL** mode is enabled in `schema.sql` so reads don't block
  writes. Concurrent `/get` and the live indexer will not contend.
* **Privacy**: ephemeral defaults mean a colleague reading your public
  channels cannot see who downloaded what. If you'd like the bot to be
  even more restricted, set `ALLOWED_ROLE_NAME` and grant that role
  manually.

---

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| Slash commands don't show up in Discord | Either `TARGET_GUILD_ID` is wrong, or the bot was kicked from the server. Re-invite with the OAuth2 URL. |
| Bot logs `Forbidden during backfill` | Bot was added to the source server without `Read Message History` permission. Re-authorize. |
| `/get <anything>` returns "no matches" | The bot has not yet indexed those entries. Run `/admin stats` to see counts. If 0, run `/admin reseed <source_channel>`. |
| `/get` autocomplete is empty | Steam cache is stale. Try `/admin refresh_steam`. Requires `Message Content Intent` enabled. |
| Bot keeps disconnecting on Render free | You're on the sleeping tier. Either upgrade to Render paid, or move to Oracle / Hetzner. |

---

## Security checklist

- [ ] `DISCORD_TOKEN` and `ADMIN_USER_IDS` are set in `.env` only —
      never commit `.env`.
- [ ] Bot is added with **minimum** permissions (`View Channels`,
      `Send Messages`, `Read Message History`, `Embed Links`).
- [ ] `ALLOWED_ROLE_NAME` is set to your trusted-role name if you want to
      gate public use.
- [ ] Source server admins **know** you're indexing their channel. Don't
      bypass their community rules.
- [ ] You're complying with copyright law and the source server's terms.

---

## License

This template is MIT-licensed; do what you want with it. The Mega.nz links
this bot relays are not licensed by you — that's between the linker and
the source. Ship responsibly.
