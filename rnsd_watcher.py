#!/usr/bin/env python3
# Drop-in replacement for `rnsd --service`: does everything rnsd normally
# does, plus watches for destinations/interfaces heard DIRECTLY (1 hop) on
# a specific interface (e.g. an RNode), logging verbosely to a dedicated
# file and sending an LXMF notification when a new one is seen (or when
# a previously-seen one is seen again after a configurable interval).
#
# Runs as the owning Reticulum instance (same as rnsd), so it has access
# to real interface objects in Transport.path_table - this is required
# to accurately identify which physical interface received an announce.
# A separate script attached to rnsd as a shared-instance client only
# sees a LocalInterface proxy and cannot tell which physical interface
# actually received a packet - which is why this replaces rnsd entirely
# rather than running alongside it.
#
# See README.md for setup instructions and config.example for the
# configuration format. Uses the same config file style (and the same
# bundled configobj parser) as Reticulum's own config files.

import RNS
import RNS.Discovery
from RNS.vendor.configobj import ConfigObj
import LXMF
import time
import os
import sys
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")

def load_config():
    if not os.path.isfile(CONFIG_PATH):
        print(f"Config file not found at {CONFIG_PATH}.", file=sys.stderr)
        print("Copy config.example to config and fill in your own values.", file=sys.stderr)
        sys.exit(1)
    return ConfigObj(CONFIG_PATH)

def as_list(value):
    """configobj returns a plain string for a single value and a list only
    when a key has multiple comma-separated values - normalise to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    value = value.strip()
    return [value] if value else []

CONFIG = load_config()

WATCH_LOG_PATH   = os.path.expanduser(CONFIG["paths"]["watch_log_path"])
SEEN_PATH        = os.path.expanduser(CONFIG["paths"]["seen_path"])
WATCHER_STORAGE  = os.path.expanduser(CONFIG["paths"]["watcher_storage"])

TARGET_IFACE_SUBSTRING = CONFIG["watch"]["target_interface_substring"]

# Whether a destination can ever trigger more than one notification.
# false = notify only once per destination, ever, no matter how often it
# reappears. true = allow repeats, gated by renotify_interval_minutes below.
RENOTIFY_ENABLED = CONFIG["watch"].as_bool("renotify")

# Minimum minutes before the SAME destination can trigger another
# notification, once RENOTIFY_ENABLED is true. 0 means no wait - eligible
# to renotify the moment it's seen again.
RENOTIFY_INTERVAL_SECONDS = CONFIG["watch"].as_int("renotify_interval_minutes") * 60

# Global floor on how often ANY notification can be sent, regardless of how
# many distinct new destinations show up. Per-destination renotify only
# throttles repeats of the SAME destination - this protects against a burst
# of many different new destinations firing a wave of messages at once
# (e.g. right after first deploying, or several devices announcing in the
# same short window). 0 disables this safeguard entirely.
MIN_MINUTES_BETWEEN_NOTIFICATIONS = CONFIG["watch"].as_int("min_minutes_between_notifications")
MIN_SECONDS_BETWEEN_NOTIFICATIONS = MIN_MINUTES_BETWEEN_NOTIFICATIONS * 60

EXCLUDED_IDENTITIES = set(as_list(CONFIG["watch"].get("excluded_identities")))

OUTBOUND_PROPAGATION_NODE = CONFIG["lxmf"].get("outbound_propagation_node") or None
WATCHER_DISPLAY_NAME = CONFIG["lxmf"].get("watcher_display_name", "RNode Watcher")

NOTIFY_RECIPIENTS = [
    {"label": label, "pubkey_hex": section["pubkey_hex"]}
    for label, section in CONFIG.get("recipients", {}).items()
]

_discovery_results = []
_iface_parser = RNS.Discovery.InterfaceAnnounceHandler(callback=_discovery_results.append)

def log_line(line):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    full = f"[{ts}] {line}"
    print(full, flush=True)
    with open(WATCH_LOG_PATH, "a") as f:
        f.write(full + "\n")

def describe_app_data(app_data):
    """Best-effort human-readable description of what an announce is advertising."""
    if not app_data:
        return "(no app data - plain presence announce, no name/capability attached)"

    # Plain UTF-8 display name (e.g. NomadNet node_name, simple LXMF display name)
    try:
        text = app_data.decode("utf-8")
        if text.isprintable():
            return f"display name: {text!r}"
    except Exception:
        pass

    # LXMF's own display-name helper (handles msgpacked display-name+stamp structs)
    try:
        dn = LXMF.display_name_from_app_data(app_data)
        if dn:
            return f"display name: {dn!r}"
    except Exception:
        pass

    # Raw msgpack structures - recognize known shapes, else show generically
    try:
        import RNS.vendor.umsgpack as msgpack
        unpacked = msgpack.unpackb(app_data)
        if isinstance(unpacked, list) and len(unpacked) == 7 and isinstance(unpacked[5], list):
            # LXMF propagation-node announce: LXMRouter.get_propagation_node_app_data()
            _, ts, node_state, transfer_limit, sync_limit, stamp, metadata = unpacked
            stamp_cost, stamp_flex, peering_cost = stamp
            name = None
            if isinstance(metadata, dict):
                for v in metadata.values():
                    if isinstance(v, (bytes, bytearray)):
                        try:
                            name = v.decode("utf-8"); break
                        except Exception:
                            pass
            name_part = f" name={name!r}" if name else ""
            return (f"propagation node capability{name_part} (active={node_state}, "
                    f"transfer_limit={transfer_limit}KB, sync_limit={sync_limit}KB, "
                    f"stamp_cost={stamp_cost}, flex={stamp_flex}, peering_cost={peering_cost})")
        return f"raw structured data: {unpacked!r}"
    except Exception:
        pass

    return f"undecodable binary data ({len(app_data)} bytes)"

def load_seen():
    # {dest_hex: last_notified_unix_timestamp}
    if os.path.isfile(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                # migrate from an older set-only format (no timestamps available)
                return {h: 0 for h in data}
            return data
        except Exception:
            return {}
    return {}

def save_seen(seen):
    tmp_path = SEEN_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(seen, f)
    os.replace(tmp_path, SEEN_PATH)

class Notifier:
    def __init__(self):
        os.makedirs(WATCHER_STORAGE, exist_ok=True)
        identity_path = os.path.join(WATCHER_STORAGE, "identity")
        if os.path.isfile(identity_path):
            self.identity = RNS.Identity.from_file(identity_path)
        else:
            self.identity = RNS.Identity()
            self.identity.to_file(identity_path)

        self.router = LXMF.LXMRouter(identity=self.identity, storagepath=WATCHER_STORAGE, autopeer=True)
        self.local_destination = self.router.register_delivery_identity(
            self.identity, display_name=WATCHER_DISPLAY_NAME
        )

        if OUTBOUND_PROPAGATION_NODE:
            self.router.set_outbound_propagation_node(bytes.fromhex(OUTBOUND_PROPAGATION_NODE))

        # Announce so recipients can resolve our display name instead of
        # falling back to "Anonymous Peer" - without this, clients have no
        # cached name for a sender they've never seen announce before.
        self.router.announce(self.local_destination.hash)

        self.recipients = []
        for r in NOTIFY_RECIPIENTS:
            recipient_identity = RNS.Identity(create_keys=False)
            recipient_identity.load_public_key(bytes.fromhex(r["pubkey_hex"]))
            dest = RNS.Destination(recipient_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
            self.recipients.append({"label": r["label"], "destination": dest})

    def notify(self, subject, body):
        for r in self.recipients:
            try:
                lxm = LXMF.LXMessage(
                    r["destination"], self.local_destination, body, title=subject,
                    desired_method=LXMF.LXMessage.PROPAGATED
                )
                self.router.handle_outbound(lxm)
                log_line(f"NOTIFY sent to {r['label']} ({RNS.hexrep(r['destination'].hash, delimit=False)})")
            except Exception as e:
                log_line(f"NOTIFY FAILED to {r['label']}: {e}")

class WatchHandler:
    aspect_filter = None  # fire on every announce, of any kind

    def __init__(self, notifier, seen):
        self.notifier = notifier
        self.seen = seen

    def received_announce(self, destination_hash, announced_identity, app_data):
        entry = RNS.Transport.path_table.get(destination_hash)
        hops  = entry[2] if entry else None
        iface = entry[5] if entry else None
        iface_str = str(iface) if iface else "unknown"

        if not (iface is not None and TARGET_IFACE_SUBSTRING in iface_str and hops == 1):
            return

        dest_hex = RNS.hexrep(destination_hash, delimit=False)
        identity_hex = RNS.hexrep(announced_identity.hash, delimit=False) if announced_identity else None

        if identity_hex in EXCLUDED_IDENTITIES:
            return

        _discovery_results.clear()
        try:
            _iface_parser.received_announce(destination_hash, announced_identity, app_data)
        except Exception:
            pass

        if _discovery_results:
            info = _discovery_results[0]
            extra = ""
            if info["type"] == "RNodeInterface":
                extra = f", freq={info.get('frequency')} bw={info.get('bandwidth')} sf={info.get('sf')} cr={info.get('cr')}"
            what_line = (f"advertising a discoverable {info['type']} named {info['name']!r} "
                         f"(transport_id={info['transport_id']}, transport_capable={info['transport']}, "
                         f"lat={info['latitude']}, lon={info['longitude']}, height={info['height']}{extra})")
        else:
            what_line = describe_app_data(app_data)

        log_line(f"WHO=identity:{identity_hex} WHAT={what_line} "
                 f"[dest={dest_hex} hops={hops} iface={iface_str}]")

        now = time.time()
        last_notified = self.seen.get(dest_hex)
        is_new = last_notified is None
        due_for_renotify = (
            not is_new
            and RENOTIFY_ENABLED
            and (now - last_notified >= RENOTIFY_INTERVAL_SECONDS)
        )

        if is_new or due_for_renotify:
            global_last_notify = self.seen.get("__global_last_notify__", 0)
            if MIN_SECONDS_BETWEEN_NOTIFICATIONS > 0 and (now - global_last_notify) < MIN_SECONDS_BETWEEN_NOTIFICATIONS:
                wait_left = MIN_SECONDS_BETWEEN_NOTIFICATIONS - (now - global_last_notify)
                log_line(f"NOTIFY SUPPRESSED (rate limit) for dest={dest_hex} - "
                         f"{wait_left:.0f}s left before another notification is allowed. "
                         f"Not marked as notified, will be reconsidered on its next announce.")
                return

            self.seen[dest_hex] = now
            self.seen["__global_last_notify__"] = now
            save_seen(self.seen)
            subject = "New direct contact" if is_new else "Direct contact seen again"
            body = (f"{'A new' if is_new else 'A previously seen'} destination was heard directly "
                    f"(1 hop) on the watched interface.\n\n"
                    f"Identity: {identity_hex}\n"
                    f"Destination: {dest_hex}\n"
                    f"What: {what_line}\n"
                    f"Interface: {iface_str}\n"
                    f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            self.notifier.notify(subject, body)

def main():
    reticulum = RNS.Reticulum(configdir=None, verbosity=None, logdest=RNS.LOG_FILE)
    if reticulum.is_connected_to_shared_instance:
        RNS.log("rnsd_watcher connected to another shared instance - this should not happen, watcher needs to be the owning instance!", RNS.LOG_CRITICAL)
    else:
        RNS.log("Started rnsd_watcher (rnsd + direct-contact watcher)", RNS.LOG_NOTICE)

    seen = load_seen()
    notifier = Notifier()
    RNS.Transport.register_announce_handler(WatchHandler(notifier, seen))
    seen_count = len([k for k in seen if not k.startswith("__")])
    log_line(f"Watcher active - logging direct (1-hop) contacts on interfaces matching "
             f"{TARGET_IFACE_SUBSTRING!r}, {seen_count} already-seen destinations loaded, "
             f"notifications armed")

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
