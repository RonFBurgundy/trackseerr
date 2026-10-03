import sys
from plex_playlist_sync.cli import run

if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
