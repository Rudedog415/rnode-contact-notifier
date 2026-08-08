# rnode-contact-notifier

A [Reticulum](https://reticulum.network/) daemon that watches a specific
local interface (e.g. an RNode LoRa radio) for destinations/services heard
**directly** (1 hop away — no relay in between), logs them verbosely, and
sends an [LXMF](https://github.com/markqvist/LXMF) notification message the
first time each one is seen (and again after a configurable interval, if
it reappears).

## Why this exists

Reticulum's `rnstatus -d` shows discovered interfaces/destinations network-wide,
but there's no built-in way to be notified in real time when something new
shows up specifically on one physical interface. This fills that gap.

It also solves a non-obvious problem: if you write a *separate* script that
attaches to an already-running `rnsd` as a shared-instance client, you can
register an announce handler, but you **cannot** reliably tell which physical
interface received a given announce — from a client's perspective, everything
arrives via a generic `LocalInterface` proxy, not the real interface object.
The only way to get accurate per-interface attribution is to run inside the
*owning* Reticulum instance itself, where `Transport.path_table` holds real
interface references. So this script is a **drop-in replacement for `rnsd`**
rather than a companion process — it does everything `rnsd --service` does,
plus the watching/notifying.

## What it detects

Because it registers a global announce handler (`aspect_filter = None`), it
sees *every* announce Transport processes, regardless of source app:

- LXMF delivery destinations (personal messaging identities)
- LXMF propagation nodes
- NomadNet node/page-server announces
- RNS's on-network interface-discovery protocol (`rnstatus -d`'s data source)
- Any other RNS-based app that calls `.announce()`

The only filters applied are: heard **directly** (`hops == 1`) on an interface
whose name contains `target_interface_substring`, and not on the identity
exclude-list.

## Setup

1. You need `RNS` and `LXMF` installed in the same Python environment (if
   installed via `pipx install rns`, inject LXMF into it: `pipx inject rns lxmf`).
   Config parsing uses RNS's own bundled `configobj` (`RNS.vendor.configobj`),
   the same format and parser Reticulum, NomadNet, and lxmd use for their
   own config files - no extra dependency needed.
2. Copy `config.example` to `config` (same directory as `rnsd_watcher.py`,
   no file extension - same convention as Reticulum's own config files) and
   fill in your values (see inline comments in that file for what each
   setting means).
3. Replace your `rnsd` systemd service (or however you launch `rnsd`) to run
   this script instead. Example unit file, assuming a pipx-installed `rns`:

   ```ini
   [Unit]
   Description=Reticulum Network Stack Daemon
   After=network.target
   Wants=network.target

   [Service]
   Type=simple
   User=youruser
   Group=youruser
   Restart=always
   RestartSec=5
   ExecStart=/home/youruser/.local/share/pipx/venvs/rns/bin/python3 /path/to/rnsd_watcher.py

   [Install]
   WantedBy=multi-user.target
   ```

4. Back up your existing `rnsd.service` unit file first, so you can revert
   instantly if anything goes wrong:
   ```bash
   sudo cp /etc/systemd/system/rnsd.service /etc/systemd/system/rnsd.service.bak
   ```
5. Reload and restart:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl restart rnsd.service
   ```
6. Confirm your interfaces came back up normally with `rnstatus`, and check
   the watch log (path from your config, default `~/.reticulum/rnode_watch.log`).

## Getting a recipient's public key

Each `[[Name]]` subsection under `[recipients]` needs that recipient's raw
public key (128 hex chars), not their destination hash. Most LXMF clients
can export a shareable `lxma://<destination_hash>:<public_key_hex>` contact
URI — the part after the `:` is what you want for `pubkey_hex`.

If you only have someone's destination hash and no contact URI, you can
alternatively rely on `RNS.Identity.recall()` if this Pi has ever seen them
announce, but that's less reliable for a background service (the cache can
expire, or may never have populated at all if they've never been in range).

## Notes

- Every matching announce is always logged, regardless of any notification
  throttle below - only the LXMF message is throttled, never the log.
- `renotify = no` disables renotify entirely - each destination only ever
  notifies once, the first time it's seen, regardless of
  `renotify_interval_minutes`. With `renotify = yes`,
  `renotify_interval_minutes: 0` means no wait - eligible to renotify the
  moment the destination is seen again.
- Notifications are sent via `desired_method=PROPAGATED`, so they're
  delivered via store-and-forward through a propagation node rather than
  requiring the recipient to be online at that exact moment. Leave
  `outbound_propagation_node` blank if you'd rather attempt direct
  delivery only.
- The watcher creates its own dedicated LXMF identity on first run (stored
  under `watcher_storage`), separate from any other identity on the system.
