#!/bin/sh
# linuxserver custom-cont-init.d hook: seed /config/config.xml (test-only API key) before Lidarr starts.
cp /seed/config.xml /config/config.xml
chown abc:abc /config/config.xml
