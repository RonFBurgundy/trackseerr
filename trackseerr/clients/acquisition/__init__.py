"""Acquisition driver registry and factory for TrackSeerr Phase 3."""

import json
from typing import Any, Union

from trackseerr.clients.acquisition.base import AcquisitionDriver
from trackseerr.clients.acquisition.deluge import DelugeDriver
from trackseerr.clients.acquisition.lidarr_adapter import LidarrAdapter
from trackseerr.clients.acquisition.nzbget import NzbgetDriver
from trackseerr.clients.acquisition.qbittorrent import QbittorrentDriver
from trackseerr.clients.acquisition.sabnzbd import SabnzbdDriver
from trackseerr.clients.acquisition.slskd import SlskdDriver
from trackseerr.clients.acquisition.torznab import TorznabDriver
from trackseerr.clients.acquisition.transmission import TransmissionDriver
from trackseerr.models import DownloadClientConfig, DownloadDriverType, IndexerConfig


DRIVER_CLASSES: dict[str, type[AcquisitionDriver]] = {
    DownloadDriverType.SLSKD.value: SlskdDriver,
    DownloadDriverType.SABNZBD.value: SabnzbdDriver,
    DownloadDriverType.QBITTORRENT.value: QbittorrentDriver,
    DownloadDriverType.LIDARR.value: LidarrAdapter,
    DownloadDriverType.TRANSMISSION.value: TransmissionDriver,
    DownloadDriverType.DELUGE.value: DelugeDriver,
    DownloadDriverType.NZBGET.value: NzbgetDriver,
}


def is_torrent_driver_type(driver_type: str | DownloadDriverType | None) -> bool:
    """True when the download-client driver type is a torrent client (derived from the driver's ``is_torrent``)."""
    val = getattr(driver_type, "value", driver_type)
    cls = DRIVER_CLASSES.get(str(val or "").strip().lower())
    return bool(cls is not None and cls.is_torrent)


def get_acquisition_driver(
    config: Union[dict[str, Any], DownloadClientConfig],
) -> AcquisitionDriver:
    """Instantiates the appropriate AcquisitionDriver from a download client configuration."""
    cfg = config.to_dict() if isinstance(config, DownloadClientConfig) else dict(config)
    driver_type = str(cfg.get("driver_type", "")).lower()
    host_url = str(cfg.get("host_url", "")).rstrip("/")
    api_key = cfg.get("api_key")
    username = cfg.get("username")
    password = cfg.get("password")

    extra: dict[str, Any] = {}
    extra_json = cfg.get("extra_settings_json")
    if extra_json and isinstance(extra_json, str):
        try:
            extra = json.loads(extra_json)
        except Exception:
            extra = {}
    elif isinstance(cfg.get("extra_settings"), dict):
        extra = cfg["extra_settings"]

    if driver_type in (DownloadDriverType.SLSKD.value, "slskd"):
        return SlskdDriver(
            host_url=host_url,
            api_key=api_key,
            username=username,
            password=password,
            download_dir=extra.get("download_dir"),
        )
    elif driver_type in (DownloadDriverType.SABNZBD.value, "sabnzbd"):
        return SabnzbdDriver(
            host_url=host_url,
            api_key=api_key,
            username=username,
            password=password,
            category=extra.get("category", "music"),
        )
    elif driver_type in (DownloadDriverType.QBITTORRENT.value, "qbittorrent"):
        return QbittorrentDriver(
            host_url=host_url,
            username=username,
            password=password,
            category=extra.get("category", "trackseerr"),
        )
    elif driver_type in (DownloadDriverType.TRANSMISSION.value, "transmission"):
        return TransmissionDriver(
            host_url=host_url,
            rpc_path=str(extra.get("rpc_path") or "/transmission/rpc"),
            username=username,
            password=password,
            category=extra.get("category", "trackseerr"),
            download_dir=extra.get("download_dir"),
        )
    elif driver_type in (DownloadDriverType.DELUGE.value, "deluge"):
        return DelugeDriver(
            host_url=host_url,
            password=password,
            label=extra.get("label") or extra.get("category", "trackseerr"),
            download_location=extra.get("download_location") or extra.get("download_dir"),
        )
    elif driver_type in (DownloadDriverType.NZBGET.value, "nzbget"):
        return NzbgetDriver(
            host_url=host_url,
            username=username,
            password=password,
            category=extra.get("category", "music"),
        )
    elif driver_type in (DownloadDriverType.LIDARR.value, "lidarr"):
        return LidarrAdapter(
            host_url=host_url,
            api_key=api_key or "",
            auto_search=bool(extra.get("auto_search", True)),
            root_folder=extra.get("root_folder"),
            prefer_singles=bool(extra.get("prefer_singles", True)),
        )
    else:
        raise ValueError(f"Unsupported download driver type: '{driver_type}'")


def get_indexer_driver(
    config: Union[dict[str, Any], IndexerConfig],
) -> TorznabDriver:
    """Instantiates a Torznab/Newznab indexer driver from configuration."""
    cfg = config.to_dict() if isinstance(config, IndexerConfig) else dict(config)
    host_url = str(cfg.get("host_url", "")).rstrip("/")
    api_key = cfg.get("api_key")
    categories = str(cfg.get("categories") or "3000,3010,3020,3030,3040")
    indexer_type = str(cfg.get("indexer_type") or "torznab")

    return TorznabDriver(
        host_url=host_url,
        api_key=api_key,
        categories=categories,
        indexer_type=indexer_type,
    )


__all__ = [
    "DRIVER_CLASSES",
    "is_torrent_driver_type",
    "AcquisitionDriver",
    "SlskdDriver",
    "SabnzbdDriver",
    "QbittorrentDriver",
    "TransmissionDriver",
    "DelugeDriver",
    "NzbgetDriver",
    "TorznabDriver",
    "LidarrAdapter",
    "get_acquisition_driver",
    "get_indexer_driver",
]
