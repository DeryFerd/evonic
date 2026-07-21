"""Channel Manager — lifecycle management for active channels."""

import logging
import socket
from typing import Dict, Type, Optional, Set
from backend.channels.base import BaseChannel
from backend.channels.telegram import TelegramChannel
from backend.channels.whatsapp import WhatsAppChannel
from backend.channels.whatsapp_shared import SharedWhatsAppChannel
from backend.channels.discord import DiscordChannel
from models.db import db

_logger = logging.getLogger(__name__)

# Map of channel type -> class
CHANNEL_TYPES: Dict[str, Type[BaseChannel]] = {
    'telegram': TelegramChannel,
    'whatsapp': WhatsAppChannel,
    'whatsapp_shared': SharedWhatsAppChannel,
    'discord': DiscordChannel,
}

# Channel types backed by a Baileys sidecar that binds a local bridge port.
_BRIDGE_PORT_TYPES = ('whatsapp', 'whatsapp_shared')
_BRIDGE_PORT_BASE = 3001


def _port_is_free(port: int) -> bool:
    """True if `port` can be bound on localhost right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


class ChannelManager:
    def __init__(self):
        self._active: Dict[str, BaseChannel] = {}  # channel_id -> instance

    def start_channel(self, channel_id: str) -> bool:
        """Start a channel by its DB ID. Skips disabled channels."""
        if channel_id in self._active and self._active[channel_id].is_running:
            return True  # already running

        channel_data = db.get_channel(channel_id)
        if not channel_data:
            _logger.warning("Channel %s not found in database — cannot start", channel_id)
            return False

        if not channel_data.get('enabled'):
            _logger.info("Skipping disabled channel %s", channel_id)
            return False

        # Don't start channels for disabled agents (shared channels have no agent)
        if channel_data.get('agent_id'):
            agent = db.get_agent(channel_data['agent_id'])
            if agent and not agent.get('enabled', True):
                _logger.info("Skipping channel %s — agent %s is disabled", channel_id, channel_data['agent_id'])
                return False

        chan_type = channel_data.get('type')
        cls = CHANNEL_TYPES.get(chan_type)
        if not cls:
            _logger.error("Unknown channel type '%s' for channel %s", chan_type, channel_id)
            raise ValueError(f"Unknown channel type: {chan_type}")

        config = channel_data.get('config', {})
        if isinstance(config, str):
            import json
            config = json.loads(config)

        # Assign a unique, currently-free bridge port to WhatsApp-type channels so
        # multiple numbers (shared or per-agent) can run concurrently instead of all
        # colliding on the default 3001. Persisted to config for stability across restarts.
        if chan_type in _BRIDGE_PORT_TYPES:
            port = self._resolve_bridge_port(channel_id, config)
            if config.get('bridge_port') != port:
                config['bridge_port'] = port
                db.update_channel(channel_id, {'config': config})

        _logger.info("Starting channel %s (type: %s, agent: %s)", channel_id, chan_type, channel_data['agent_id'])
        instance = cls(channel_id, channel_data['agent_id'], config)
        instance.start()
        self._active[channel_id] = instance
        _logger.info("Channel %s (%s) started successfully for agent %s", channel_id, chan_type, channel_data['agent_id'])
        return True

    def _active_bridge_ports(self, exclude_channel_id: Optional[str] = None) -> Set[int]:
        """Ports currently held by running WhatsApp-type channel instances."""
        ports: Set[int] = set()
        for cid, inst in self._active.items():
            if cid == exclude_channel_id:
                continue
            port = getattr(inst, 'bridge_port', None)
            if port:
                ports.add(int(port))
        return ports

    def _resolve_bridge_port(self, channel_id: str, config: dict) -> int:
        """Pick a bridge port for a WhatsApp-type channel.

        Reuse the channel's persisted port when it's not held by another running
        channel and is free on the OS; otherwise allocate the lowest free port,
        skipping ports already claimed by active instances.
        """
        taken = self._active_bridge_ports(exclude_channel_id=channel_id)
        existing = config.get('bridge_port')
        if existing is not None:
            existing = int(existing)
            if existing not in taken and _port_is_free(existing):
                return existing
        port = _BRIDGE_PORT_BASE
        while port in taken or not _port_is_free(port):
            port += 1
        return port

    def stop_channel(self, channel_id: str) -> bool:
        """Stop a running channel."""
        instance = self._active.get(channel_id)
        if not instance:
            _logger.warning("Channel %s not found in active list — nothing to stop", channel_id)
            return False
        _logger.info("Stopping channel %s", channel_id)
        instance.stop()
        del self._active[channel_id]
        _logger.info("Channel %s stopped", channel_id)
        return True

    def get_channel_instance(self, channel_id: str) -> Optional[BaseChannel]:
        """Return the active channel instance for the given channel_id, or None."""
        return self._active.get(channel_id)

    def is_running(self, channel_id: str) -> bool:
        instance = self._active.get(channel_id)
        return instance.is_running if instance else False

    def start_all_enabled(self):
        """Start all enabled channels from DB (called at app startup).
        Skips channels belonging to disabled agents."""
        _logger.info("Starting all enabled channels...")
        agents = db.get_agents()
        for agent in agents:
            if not agent.get('enabled', True):
                continue  # skip disabled agents entirely
            channels = db.get_channels(agent['id'])
            for ch in channels:
                if ch.get('enabled'):
                    try:
                        self.start_channel(ch['id'])
                    except Exception as e:
                        _logger.error("Failed to start channel %s (type: %s): %s",
                                      ch['id'], ch.get('type', 'unknown'), e)
        # Shared channels are not bound to any agent (agent_id IS NULL)
        for ch in db.get_shared_channels():
            if ch.get('enabled'):
                try:
                    self.start_channel(ch['id'])
                except Exception as e:
                    _logger.error("Failed to start shared channel %s (type: %s): %s",
                                  ch['id'], ch.get('type', 'unknown'), e)
        _logger.info("Finished starting all enabled channels")

    def stop_all(self):
        """Stop all running channels."""
        _logger.info("Stopping all channels (%d active)...", len(self._active))
        for channel_id in list(self._active.keys()):
            self.stop_channel(channel_id)
        _logger.info("All channels stopped")


# Global instance
channel_manager = ChannelManager()
