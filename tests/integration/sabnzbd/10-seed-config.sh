#!/bin/sh
# linuxserver custom-cont-init.d hook: seed /config/sabnzbd.ini before SABnzbd starts.
cp /seed/sabnzbd.ini /config/sabnzbd.ini
chown abc:abc /config/sabnzbd.ini
