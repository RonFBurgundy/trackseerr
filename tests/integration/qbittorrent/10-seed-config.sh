#!/bin/sh
# linuxserver custom-cont-init.d hook: seed /config/qBittorrent/qBittorrent.conf before qBittorrent starts.
mkdir -p /config/qBittorrent
cp /seed/qBittorrent.conf /config/qBittorrent/qBittorrent.conf
chown -R abc:abc /config/qBittorrent
