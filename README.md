# plex-source-injection
On-Demand Search &amp; Ingest Proxy for Plex

A FastAPI reverse proxy that sits between Plex/Plexamp clients and your Plex Media Server.
It injects search results from external sources (YouTube, Spotify, …) into Plex search
responses and downloads + indexes an external track on demand when a client opens it.

```
Plexamp ──► proxy (:32399) ──► Plex Media Server (:32400)
              │
              ├─ /hubs/search, /library/search  → local results + external providers (concurrently)
              ├─ /library/metadata/ext_<p>_<id> → download → Plex partial scan → real track metadata
              └─ everything else                → streamed through unchanged

Administrator ──► admin (:32300) ──► configuration, dependencies & activity logs
```

Administration is served by a separate listener. `/admin` requests on the Plex proxy
port return 404 and are never forwarded to Plex. The admin port does not proxy Plex
traffic. Both listeners share one process and live configuration.

## How it works

1. **Search interception** – `GET /hubs/search` and `GET /library/search` are forwarded to
   Plex while every enabled provider is queried concurrently (`asyncio.gather`). External
   hits are appended to the JSON response as Plex `track` items with synthetic ratingKeys
   such as `ext_yt_<VIDEO_ID>` or `ext_sp_<TRACK_ID>`. A provider that errors or exceeds
   `PROVIDER_TIMEOUT` is skipped; local and remaining provider results are still returned.
   Results are only injected into successful (2xx) JSON responses, so unauthenticated
   clients never see them. XML responses are passed through unchanged.
2. **Ingestion** – when a client requests `/library/metadata/ext_<prefix>_<id>` (or creates
   a play queue whose `uri` references one), the proxy verifies the client's token against
   Plex, dispatches the download to the owning provider (`yt-dlp` / `spotdl`), writes it to
   `DOWNLOAD_DIR` as `Artist - Title [<id>].<ext>`, triggers
   `GET /library/sections/<MUSIC_SECTION_ID>/refresh?path=<PLEX_DOWNLOAD_DIR>`, polls Plex
   (via `python-plexapi`) until the file has a real ratingKey, and then returns Plex's own
   metadata for that ratingKey so the client plays the file natively. Concurrent requests
   for the same item share one download.
3. **Pass-through** – all other requests (including media streams and `Range` requests) are
   streamed to Plex with their `X-Plex-*` headers and query strings untouched.
4. **Cleanup** – every `CLEANUP_INTERVAL_HOURS` (default 24) files in `DOWNLOAD_DIR` older
   than `RETENTION_DAYS` are deleted and the folder is rescanned. `RETENTION_DAYS=0`
   disables cleanup.

## Installation

### EL9 / EL10 RPM packages

Use the release helper from a reviewed, committed `main` checkout:

```bash
./release.sh --dry-run          # preview next patch release; no writes or network calls
./release.sh                   # patch bump, commit, annotated tag, atomic push
./release.sh --minor           # minor bump
./release.sh --major           # major bump
./release.sh --version 1.2.3   # explicit newer version
./release.sh --no-push         # prepare a local release commit/tag only
```

The helper prefers `.venv/bin/python`, otherwise `python3`. It updates `VERSION` and
the RPM spec version/changelog, then pushes `main` and the tag together to trigger the
workflow. Actual releases require a clean worktree, configured Git identity, no tracked
ignored runtime files, working ignore rules and an unused tag. Publishing additionally
checks the remote tag/main state; the normal push must be a fast-forward. Dry runs may
preview a dirty checkout but warn that an actual release would refuse it. Review and
commit application changes separately; the helper never stages the entire worktree.
Initial version is `0.0.0`; choose `--version 0.1.0` for a first `0.1.0` release.
On failure, inspect local changes/commits/tags before retrying: there is no automatic
rollback or forced push. If publication fails after the local commit/tag were created,
resolve the remote issue and push those existing refs rather than rerunning the bump.

GitHub Actions builds x86-64 RPMs in the official Red Hat **UBI 9** and **UBI 10**
images. Release tags must use `vMAJOR.MINOR.PATCH` (for example, `v0.1.0`). Pushing a
version tag builds and tests both platforms, then publishes their RPMs, source RPMs,
application source archives and `SHA256SUMS` to a GitHub Release. The RPM workflow
runs only on version-tag pushes, not on branch commits or pull requests, and has
no manual trigger. Tags must match `VERSION`; invalid tags fail before RPM builds.

Each platform produces two matching packages:

* `plex-source-injection`: application, web assets, launcher and systemd unit.
* `plex-source-injection-pythonlibs`: bundled dependencies, including native wheels,
  upstream distribution metadata and licenses, in a private directory. No target-side
  PyPI installation is required. EL9 uses system Python 3.12; EL10 uses its default
  Python 3.12. Install the packages for your OS release, with matching versions.

Download both binary RPMs from the release and verify `sha256sum -c SHA256SUMS`
for the downloaded assets (use `--ignore-missing` if downloading only your platform).
Then install them together, for example on EL9:

```bash
sudo dnf install ./plex-source-injection-0.1.0-1.el9.x86_64.rpm \
  ./plex-source-injection-pythonlibs-0.1.0-1.el9.x86_64.rpm
sudo plex-inject-passwd
sudo systemctl enable --now plex-source-injection
sudo systemctl status plex-source-injection
sudo journalctl -u plex-source-injection -f
```

Replace version/EL suffixes with your chosen release. The dedicated
`plex-source-injection` system user owns `/var/lib/plex-source-injection`. SQLite,
managed executables and caches live there, outside the installed application.
The RPM creates `/var/lib/plex-source-injection/bin` with service-user ownership
and mode `0750`. Managed spotdl, yt-dlp and FFmpeg are installed and executed from
versioned subdirectories there, independently of all media download locations.
Packages do **not** ship credentials or a database, enable/start the service at install,
or erase state on removal. Set the password explicitly before enabling the service.
The `plex-inject-passwd` command is installed in `/usr/bin` (on the system PATH).
Run `sudo plex-inject-passwd` to set or reset the administrator password; it prompts
twice and runs as the service user so database ownership is preserved. Passwords
must be at least 12 characters; the login username is `admin`. It can also be run
directly as the `plex-source-injection` service user. No service restart is needed.
The helper uses the RPM launcher, including its platform-specific Python interpreter
and private `plex-source-injection-pythonlibs` dependencies; no virtual environment
or system-wide pip installation is needed.
Ports default to Plex `32399` and admin `32300`; restrict the admin port with your
firewall and keep the existing network allowlist/HTTPS guidance.

Choose a writable music download path in the admin interface, and ensure Plex can read
it. A path such as `/var/lib/plex-source-injection/music` keeps it under the service
state directory; arrange group/ACL permissions for your Plex process. Music plugins
still require FFmpeg and (for Spotify) spotdl via managed dependencies or external
executables; these tools are not part of `-pythonlibs`.

The service runs without root, uses a restrictive umask, restarts after failures and
allows ten minutes for graceful shutdown. `ProtectHome=false` permits access to
home directories, but does not grant filesystem permissions. The service user needs
execute (traversal) permission on every parent directory and write/execute permission
on the selected download directory. Prefer targeted ACLs rather than making a home
directory world-writable or running the service as root. For example:

```bash
sudo setfacl -m u:plex-source-injection:--x /home/mj /home/mj/Musik
sudo setfacl -m u:plex-source-injection:rwx /home/mj/Musik/Download
sudo -u plex-source-injection test -w /home/mj/Musik/Download
```

Create the download directory first and adapt these paths to your installation.
Existing media subdirectories also need suitable permissions for writing and cleanup.
`UMask=0077` keeps service state private. Completed media files are explicitly set
to `0664` before scanning, including existing downloads reused on retry, so a separate
Plex account can read them. Parent directories must still allow Plex to traverse them;
configure appropriate directory permissions or ACLs. Other members of the media file's
group can write it; other users can read it.
`ProtectSystem=full` makes `/usr`, `/boot` and `/etc` read-only, not `/home`, so it can
remain enabled. `PrivateTmp` and `NoNewPrivileges` can also remain enabled.
On SELinux-enforcing hosts, check audit denials if ordinary permissions are correct;
use appropriate labels/policy rather than disabling SELinux.
After changing the unit or a drop-in, run `systemctl daemon-reload` and restart the
service. For packaged installations use `systemctl start/stop/restart
plex-source-injection`, not the source-checkout lifecycle script.

To rebuild locally with Docker:

```bash
mkdir -p dist
docker build --build-arg EL_VERSION=9 -f packaging/Containerfile -t plex-rpm-builder:el9 .
docker run --rm -v "$PWD:/source:ro" -v "$PWD/dist:/output" plex-rpm-builder:el9 0.1.0
# Repeat with EL_VERSION=10 and plex-rpm-builder:el10 for EL10.
```

The builder downloads wheels at build time, performs the offline RPM installation,
runs the full test suite against the packaged dependencies and smoke-tests the
installed launcher/service user/listeners. CI also checks installation in a clean UBI
runtime image. Source RPMs include the resolved wheelhouse for rebuilding without
PyPI access; application source archives use an explicit allowlist, never a wholesale
working-directory archive. Wheels are resolved from the dependency requirements per
build, so separate builds can include newer dependencies. RPMs are not GPG-signed.

Before committing, check `git status --short` and
`git ls-files -ci --exclude-standard`: runtime SQLite files/sidecars/backups,
environment secrets, caches, managed dependencies, logs and RPM/build output are
ignored. Ignore rules do not remove files already tracked by Git; the tracked-file
audit must remain empty for these paths.

### Source installation

Requires Python 3.11+. FFmpeg and spotdl can be supplied on `PATH` or installed
explicitly through the admin's managed dependency pages.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# Optional if supplying spotdl yourself; do not install it into the proxy environment:
pipx install spotdl
python main.py --set-admin-password
python main.py         # starts both the Plex proxy and the administration listener
```

Point your Plex clients at `http://<proxy-host>:<PROXY_PORT>` instead of the Plex server.
Open `http://<proxy-host>:32300/admin/` and log in as `admin` with the password you set.
Configure Plex, download paths, enabled providers and credentials there.
The proxy can start without a Plex token, but scans/ingestion need a valid token.
In **General**, save the Plex URL/token, click **Load Plex music libraries**, then
choose the music destination by name and save. Only Plex Music libraries are listed.
The library must contain the music download folder (using its Plex-side path if the
server sees a different filesystem path). A movie/TV library cannot index audio tracks;
an incorrect or missing library ID now produces a configuration error before downloading
rather than an unexpected `Unknown libtype "track"` failure. If no Music libraries are
listed, create one in Plex and load the list again. Existing IDs are preserved until
you explicitly select and save a replacement.

### Start, stop and restart

On Linux, use the lifecycle helper from any working directory:

```bash
./service.sh start
./service.sh status
./service.sh restart
./service.sh stop
```

It runs both listeners in the background using `.venv/bin/python`, reads their ports
from SQLite, waits for both listeners to start and the admin endpoint to respond,
and writes process logs to `.run/proxy.log`. Both ports must be available; a conflict
on either fails startup without leaving the other listener running.
PID tracking includes the process start time to avoid signaling a reused PID.
Repeated start/stop calls are safe, and concurrent lifecycle commands are serialized.
`status` exits with code `0` when running and `3` when stopped.

Use the same OS user, `CONFIG_DB`, and state directory for all commands. Relative
paths are resolved from the repository directory. Optional environment overrides:

* `PYTHON_BIN`: Python executable (defaults to `.venv/bin/python`).
* `SERVICE_STATE_DIR`: PID, lock and log directory (defaults to `.run/`).
* `START_TIMEOUT`: readiness timeout in seconds (default `30`).
* `STOP_TIMEOUT`: graceful shutdown timeout in seconds (default `60`).

Stop sends SIGTERM and allows active streams/downloads to finish. If shutdown exceeds
the timeout, it reports failure and retains the PID file; it does not force-kill.
Increase `STOP_TIMEOUT` if needed. Startup failures are reported with the log path.
The script needs Bash, `flock`, `nohup` and `curl`. It controls only processes started
by this script, not manually started instances. It survives closing the terminal,
but does not provide automatic restart or start on boot; use a service manager for that.

## Configuration

All application and provider settings live in `config.sqlite3`, not in environment
variables. The database is created automatically with owner-only (`0600`) permissions.
Set **`CONFIG_DB` in the process environment** to choose a different database path:

```bash
export CONFIG_DB=/var/lib/plex-source-injection/config.sqlite3
python main.py --set-admin-password
python main.py
```

The database location is a bootstrap setting: it cannot be changed in the web UI.
Use the same path and operating-system user for password setup and running the proxy.
The containing directory must be writable. Back up the database as sensitive data:
provider/Plex credentials are stored in plaintext; the admin password is stored only
as a salted PBKDF2 hash. Do not commit the database or expose it over HTTP.

### Web interface

`/admin/` has a Plex-inspired dark theme with sidebar pages for General, Downloads &
cleanup, Plugins, Advanced settings and Logs. Select plugins using the enable switches
instead of entering a comma-separated list. Each discovered plugin has its own
configuration page, including credentials, download options and saved active status.
The Plugins overview distinguishes active, disabled and enabled-but-unconfigured
providers; switch changes take effect when saved.

#### Category permissions

Each plugin page has a Media categories selection. Only categories the plugin actually
supports are offered. Both built-in providers currently support **music only**; YouTube
extracts audio, not videos, and Spotify provides music. Selecting no categories prevents
that plugin from searching or ingesting music even if the plugin switch remains on.
Music is selected by default to preserve existing installations. Searches explicitly
restricted to Plex movie/series/episode types do not receive external music results.
Unrestricted searches and artist/album/track searches can receive music.
This change does not add a video/series ingestion pipeline.

#### Download locations by category

**Downloads & cleanup** contains separate local paths and optional paths as seen by Plex
for music, series, movies and other videos. Existing music paths are preserved:
`DOWNLOAD_DIR` and `PLEX_DOWNLOAD_DIR` remain the music settings. Blank Plex-side paths
use their category's local path. Local paths must be absolute and must not be `/`.

Series/movie/video paths are preparation for future plugins, not new downloading
capabilities. Current plugins and automatic cleanup still only use the music folder.
Merely configuring another category does not create its folder, download media there,
or delete its files.

#### Activity logs

Open **Logs** to view searches and their result titles/IDs, download destinations,
successful Plex registration, warnings and errors. Filter by category or severity,
click **Refresh logs** for recent events, or **Load older** for more history.
The latest **2,000 entries** are retained in SQLite across restarts, with individual
messages limited to 4,000 characters. No additional log file needs to be configured.

Only application activity is collected, not HTTP access logs or third-party library
output. Configured credentials, URL queries and common token/password fields are
redacted before storage and again when read. Search terms, media titles and file paths
remain visible, so treat the database and this authenticated page as private.
This is a bounded operational history, not a permanent audit trail or live download
progress meter. Raw process diagnostics remain in the lifecycle script's log file.

#### Managed dependencies and updates

Plugin pages link to their dependencies. Each dependency page lets you choose its source:

* **spotdl:** external executable or managed official spotDL GitHub release.
* **yt-dlp:** bundled Python package (default), external executable, or managed official
  yt-dlp GitHub release. Managed/external mode runs the CLI for search, metadata and download.
* **FFmpeg:** external executable or managed Linux x86-64/ARM64 GPL builds published by
  [yt-dlp/FFmpeg-Builds](https://github.com/yt-dlp/FFmpeg-Builds), not by ffmpeg.org.
  The managed install includes both ffmpeg and ffprobe. On other operating systems,
  provide them yourself; keep both in the same directory.

Click **Check for updates**, choose a stable version, then **Install / update** and confirm.
Downloads are restricted to the configured upstream GitHub repositories, size-limited
and verified against GitHub's published SHA-256 digest. Releases without a digest are
rejected. Executables undergo a version check before activation. Unsupported platforms,
verification errors and failed installs produce explicit errors; the prior selection
remains. Managed spotdl binaries require x86-64. No packages are installed into the
proxy's Python environment or the operating system.

Choose **Managed** and save to use the installed binary. Downloading alone does not change
the configured source. Releases are never automatically installed at startup or during
playback. The installed release and digest are shown; previous files are retained so
active requests can finish. Select and install an older verified release to roll back.
FFmpeg's rolling `latest` tag is identified additionally by its asset digest; rollback
requires an upstream release still available on GitHub. GitHub API rate limits apply.

Source checkouts store files by default in `dependencies/` beside the SQLite database,
organized by tool/version/digest. `DEPENDENCY_DIR` can be changed on the Custom settings
page for source checkouts. Standard RPM installations use the fixed directory
`/var/lib/plex-source-injection/bin`. On first startup after upgrading, existing managed
tool directories and manifests are copied to this location and saved executable paths
are updated automatically. Original files are retained for recovery; unrelated media
files are not copied. Migration refuses to overwrite a nonempty destination from an
unrelated installation and reports an error rather than selecting missing executables.
FFmpeg is shared by Spotify and YouTube: changing its mode/path affects both.
For an external binary, specify its executable name or absolute path. External/bundled
dependency updates remain your responsibility; the UI never modifies them.

Upstream standalone binaries may require a newer glibc than your OS provides.
For example, spotdl 4.5.2's Linux binary requires `GLIBC_2.38` and does not run
on EL9; it runs on EL10. Failed executable checks show the subprocess diagnostic
and preserve the previous installation. Choose an older compatible upstream version
or configure an external spotdl installation built for your OS. Do not replace
the system glibc to accommodate a downloaded plugin binary.

On EL9, select **managed-python** on the spotdl dependency page, save, and click
**Install / update**. This installs the selected spotdl version from PyPI into a
private versioned virtual environment beneath `DEPENDENCY_DIR/spotdl` (RPM:
`/var/lib/plex-source-injection/bin/spotdl`). It uses the service's Python interpreter,
not the incompatible upstream standalone executable. The RPM includes pip/venv
support. The spotdl wheel is verified against its PyPI SHA-256; dependencies are
resolved as binary wheels over HTTPS from PyPI and are not a fully locked bundle.
No system Python packages are modified. Standalone and Python installations have
separate manifests; switching sources selects that source's installed version.
Updates are explicit, previous environments are retained for active downloads,
and failed installations do not replace the selected version. Source checkouts
need Python with pip/venv support. FFmpeg must still be configured separately.

#### Tokens and credentials

The General page offers **Sign in with Plex**. Save outstanding changes, start sign-in,
open the Plex authorization link, sign in (including MFA) on Plex's website, and click
**Check authorization**. A completed login saves and applies the token without displaying
it. Use the Plex server owner's account for scanning. Sessions expire after a few minutes;
changing configuration during login requires starting again. The proxy never receives
your Plex username/password. Manual instructions are linked in the UI and at
[Finding X-Plex-Token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

Spotify requires developer application credentials, not an account username/password.
Open the [Spotify developer dashboard](https://developer.spotify.com/dashboard), create
an app, and copy its Client ID and Client Secret to the Spotify plugin page.
[Client Credentials flow](https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow)
access tokens are fetched automatically by Spotipy. A user/password login cannot create
the required developer credentials; Spotify developer-account restrictions still apply.

YouTube needs no token when using yt-dlp search. Optionally create a Google Cloud project,
enable YouTube Data API v3, and create an API key under APIs & Services > Credentials.
Restrict it to the YouTube API and your server's IP where possible. See
[YouTube API setup](https://developers.google.com/youtube/v3/getting-started).
These instructions and console links are also shown on the YouTube page.

Audio formats use a dropdown, booleans and automatic cleanup use switches, and ports,
music libraries use a discovered-library selector; limits and timing values use
numeric controls with units and bounds.
URLs, paths and credentials remain editable inputs. Existing custom audio formats are
preserved rather than silently replaced. Changes across pages are saved together;
navigating between pages retains unsaved edits. Reload asks before discarding edits,
and leaving the interface warns if changes are unsaved.

Custom settings matching a plugin's name prefix (e.g. `SOUNDCLOUD_TOKEN`) appear on
that plugin's page. Other settings are available on the Custom settings page. Each
page offers an additional-setting field. Blank secret fields preserve saved
credentials; an explicit checkbox clears them. Invalid values are rejected without
changing configuration. Saving from a stale browser tab is rejected; reload before
retrying.

Changes are persisted and applied to **new requests immediately**, including upstream
URL, credentials, providers, download paths, timeouts and cleanup scheduling. Active
requests/streams and downloads retain their original configuration until they finish.
Cleanup is rescheduled with the new values and runs immediately, as at startup.
`PROXY_PORT` and `ADMIN_PORT` need a process restart; the UI shows both current and saved
ports. They must be different. Reconnect to the new admin port after changing it.
Keep the admin port restricted to trusted networks with your firewall/reverse proxy;
using a different port alone is not authentication or encryption.
Use `python main.py` or `service.sh` to launch both listeners; invoking the
`main:create_app` Uvicorn factory directly only serves the Plex proxy, without admin.
Use a **single worker/process per database** for consistent live updates. Direct database
edits are not live-reloaded; use the UI/API or restart.

Administration uses a real server-rendered sign-in page at `/admin/login` (username
`admin`), not the browser's Basic-auth popup. The form works without JavaScript.
Set or reset the password with `python main.py --set-admin-password`; it is prompted
rather than exposed in command-line arguments. Without a password, the admin interface
is locked, but the proxy still runs. Failed logins are rate-limited. Authenticated
sessions use HttpOnly, SameSite=Strict cookies and expire after eight hours. They are
stored in server memory, so a restart or password reset invalidates them. Use **Sign out**
to end a session immediately. Cookies use Secure when signing in over HTTPS, including
HTTPS origins behind a TLS reverse proxy. Basic credentials no longer grant API access.

Login forms use single-use CSRF tokens. Session-authenticated API writes require both
same-origin JSON requests and a session CSRF token. The UI handles these automatically.
API automation must submit the login form, retain its cookie, and obtain the CSRF token
from `GET /admin/api/session` to send as `X-CSRF-Token` on writes. Ordinary Plex client
routes retain their existing Plex authentication behavior.

#### Allowed administration networks

**General > Allowed admin networks** controls which direct peer IP addresses may access
any `/admin` route, including the login page, CSS/JavaScript, APIs and unknown routes.
Disallowed connections receive HTTP 403 before authentication. Normal Plex proxy traffic
is not restricted by this setting. The default allows localhost and private LANs:

```text
127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7
```

Enter comma-separated IPv4/IPv6 CIDR networks or individual IP addresses. Use canonical
network addresses (e.g. `192.168.1.0/24`, not `192.168.1.10/24`). Empty/invalid lists are
rejected. IPv4-mapped IPv6 peers are checked against IPv4 networks. Changes are saved to
SQLite and apply immediately. To avoid accidental lockout, the UI/API refuses a list
that excludes the peer making the change. Narrow this default to your actual admin LAN;
it does not make every private network trusted.

If you need to recover or configure access without the web interface:

```bash
python main.py --set-admin-networks '127.0.0.0/8,::1/128,192.168.1.0/24'
./service.sh restart
```

Use the same `CONFIG_DB` as the service. CLI changes require a restart.

**Forwarded headers are not trusted.** The built-in entrypoint disables Uvicorn proxy
headers for both listeners. Behind a reverse proxy, the allowlist
sees the reverse proxy's address, not the original browser: enforce the browser network
restriction at the reverse proxy/firewall too, and allow only the proxy's address here.
An SSH tunnel likewise appears as its server-side peer (usually localhost).

**Use HTTPS through a trusted reverse proxy or access via a local SSH tunnel.** A login
page does not encrypt credentials over HTTP. Do not expose the plain-HTTP administration
endpoint to an untrusted network. If a TLS reverse proxy is used,
preserve the public `Host` header. The origin check accepts HTTPS origins when the
internal hop uses HTTP, but still requires the same host and port. Serve administration
and its API on the same public origin.

### Existing installations

On first initialization only, known settings are imported from the existing `.env`
and process environment (environment takes precedence). Custom provider keys in
`.env` are also imported; unrelated process environment variables are **not** copied.
Custom provider settings that existed only in the process environment should be
added using the UI. After importing, SQLite is authoritative; changing `.env` or
environment settings no longer changes application configuration. Remove or securely
archive `.env` after verifying the migration. The legacy [.env.example](.env.example)
is retained as an optional first-run import template, not an ongoing configuration source.

| Setting | Default | Description |
| --- | --- | --- |
| `PLEX_URL` | `http://127.0.0.1:32400` | Upstream Plex Media Server |
| `PLEX_TOKEN` | – | Token used for scans and polling |
| `MUSIC_SECTION_ID` | `1` | Music library section that contains `DOWNLOAD_DIR` |
| `DOWNLOAD_DIR` | `/music/Downloads` | Music download location; preserved for existing installations |
| `PLEX_DOWNLOAD_DIR` | `DOWNLOAD_DIR` | Same folder as seen by Plex (if paths differ, e.g. containers) |
| `SERIES_DOWNLOAD_DIR` | `/series/Downloads` | Series location reserved for future plugins |
| `MOVIES_DOWNLOAD_DIR` | `/movies/Downloads` | Movie location reserved for future plugins |
| `VIDEOS_DOWNLOAD_DIR` | `/videos/Downloads` | Other video location reserved for future plugins |
| `PLEX_SERIES_DOWNLOAD_DIR` / `PLEX_MOVIES_DOWNLOAD_DIR` / `PLEX_VIDEOS_DOWNLOAD_DIR` | Category's local path | Optional corresponding path as seen by Plex |
| `RETENTION_DAYS` | `30` | Delete downloads older than this (`0` disables) |
| `PROXY_PORT` | `32399` | Listening port; existing saved/imported values are not overwritten |
| `ADMIN_PORT` | `32300` | Separate administration listener; must differ from `PROXY_PORT` |
| `ADMIN_ALLOWED_NETWORKS` | Localhost + private LAN CIDRs above | Networks allowed to access administration; does not restrict Plex traffic |
| `ENABLED_PROVIDERS` | `youtube,spotify` | Comma-separated provider names |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | – | Required for the Spotify provider |
| `YOUTUBE_API_KEY` | – | Optional; uses the YouTube Data API for search instead of `yt-dlp` |
| `AUDIO_FORMAT` | `mp3` | Output format (`mp3`, `flac`, `opus`, …) |
| `SEARCH_LIMIT` | `10` | Max results per provider |
| `PROVIDER_TIMEOUT` | `8` | Seconds before a provider search is abandoned |
| `DOWNLOAD_TIMEOUT` | `300` | Seconds before a download is abandoned |
| `SCAN_TIMEOUT` / `SCAN_POLL_INTERVAL` | `120` / `2` | Polling for the newly scanned track |
| `CLEANUP_INTERVAL_HOURS` | `24` | Cleanup interval |
| `SPOTDL_BINARY` | `spotdl` | spotdl executable name/path |
| `SPOTDL_PASS_CREDENTIALS` | `false` | Pass the Spotify credentials to spotdl as CLI arguments (visible in the process list). By default spotdl uses its own `config.json`. |
| `SPOTIFY_CATEGORIES` / `YOUTUBE_CATEGORIES` | `music` | Selected supported categories; empty disables all |
| `SPOTDL_MODE` / `SPOTDL_BINARY` | `external` / `spotdl` | Managed release or your own executable |
| `YTDLP_MODE` / `YTDLP_BINARY` | `bundled` / `yt-dlp` | Bundled Python package, managed release, or external CLI |
| `FFMPEG_MODE` / `FFMPEG_BINARY` | `external` / `ffmpeg` | Shared FFmpeg source; ffprobe must be alongside an external binary |
| `DEPENDENCY_DIR` | `dependencies/` beside SQLite; RPM: `/var/lib/plex-source-injection/bin` | Absolute path for managed version files; fixed for standard RPM installs |

## Adding a provider

Create a module in `providers/` – no changes to the proxy, search or ingest code are needed:

```python
# providers/soundcloud.py
from pathlib import Path

from .base import BaseProvider, ExternalTrack, ProviderConfigurationError, ProviderSetting
from .registry import register_provider


@register_provider
class SoundCloudProvider(BaseProvider):
    name = "soundcloud"      # used in ENABLED_PROVIDERS
    prefix = "sc"            # ratingKeys become ext_sc_<id> (lowercase alphanumerics)
    display_name = "SoundCloud"
    description = "Search and download music from SoundCloud."
    config_fields = (
        ProviderSetting("SOUNDCLOUD_TOKEN", "Access token", "Required for this provider."),
    )

    def __init__(self, settings):
        super().__init__(settings)
        self.token = settings.get("SOUNDCLOUD_TOKEN")  # raw SQLite setting
        if not self.token:
            raise ProviderConfigurationError("SOUNDCLOUD_TOKEN not set")  # provider is skipped

    async def search(self, query: str, limit: int) -> list[ExternalTrack]: ...
    async def fetch_metadata(self, item_id: str) -> ExternalTrack | None: ...
    async def download(self, item_id: str, output_dir: Path) -> Path:
        ...  # must write "<anything> [<item_id>].<ext>" into output_dir and return the path
```

Restart after installing the module. SoundCloud will appear automatically in the
Plugins selector and sidebar. Open its page to set the token and enable it, then save.
Item IDs must match `[A-Za-z0-9_-]+`.

`config_fields` is optional. Declare `ProviderSetting` entries with a configuration
`key`, human-readable `label`, and optional `description`, `default`, and `kind`
(`text`, `number`, `select`, or `switch`). Numeric fields can supply `minimum`,
`maximum` and `step`; selects supply a tuple of `choices`. Switches write `"true"` or
`"false"`. Defaults are UI suggestions; providers should use the same fallback in
`settings.get()`. Never put credentials in defaults. Keys containing `TOKEN`, `SECRET`,
`PASSWORD` or `KEY` are masked. Declared fields can use keys without the provider
prefix (as Spotify does for `SPOTDL_BINARY`). Provider-specific validation remains the
provider's responsibility. Plugins without declarations still have their own page
and can configure existing or new name-prefixed settings.

Providers can declare `supported_categories` (defaults to `("music",)`) and `dependencies`
(e.g. `("yt-dlp", "ffmpeg")`). Category selections are persisted as
`<PROVIDER_NAME>_CATEGORIES`. Only declare a category when its search/ingestion pipeline
actually exists: the current proxy pipeline only handles music. Tool names must refer
to the fixed dependency catalog; adding an executable requires an explicit trusted
upstream/platform mapping, not a user-supplied download URL.

Category-aware plugins can use `settings.download_location(category)` and
`settings.plex_download_location(category)` for `music`, `series`, `movies` or `videos`.
Unknown categories raise `KeyError` rather than accidentally writing to the music
folder. These helpers prepare path selection only: non-music plugins still need the
corresponding metadata/search/scan/library-resolution pipeline before their categories
can be enabled. Log meaningful activity through the module's standard Python logger;
application provider loggers are included in the admin history.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
# Linux lifecycle integration test (proxy 18766, admin 18767):
bash tests/test_service.sh
```

## Limitations

* Only JSON search responses are augmented (Plexamp requests JSON); XML is passed through.
* WebSocket endpoints (e.g. `/:/websockets/notifications`) are not proxied.
* Search results are only fetched from providers for clients whose token Plex accepts.
