"""
Plugin Hot Reload — automatic plugin reloading during development.

Watches plugin directories for file changes and automatically reloads
plugins when their code is modified. This enables rapid iteration during
plugin development without requiring full Evonic restarts.

Features:
- File system watching for .py, .json, .yaml files
- Debounced reload (waits for file changes to settle)
- Per-plugin enable/disable
- Thread-safe reload coordination
- Graceful error handling

Usage:
    from backend.plugin_hot_reload import hot_reload_manager
    
    # Enable hot reload for a plugin
    hot_reload_manager.enable_for_plugin('my_plugin')
    
    # Disable hot reload
    hot_reload_manager.disable_for_plugin('my_plugin')
    
    # Check status
    status = hot_reload_manager.get_status()
"""

import os
import time
import logging
import threading
from typing import Dict, Set, Optional, Callable
from pathlib import Path
from collections import defaultdict

_logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_DIR = os.path.join(BASE_DIR, 'plugins')

# File extensions to watch
WATCHED_EXTENSIONS = {'.py', '.json', '.yaml', '.yml', '.md'}

# Debounce delay in seconds (wait for changes to settle)
DEBOUNCE_DELAY = 1.0


class PluginHotReloadManager:
    """
    Manages hot reload for plugins during development.
    
    Uses file system polling to detect changes and automatically reloads
    plugins when their code is modified.
    """
    
    def __init__(self, plugin_manager=None):
        """
        Initialize hot reload manager.
        
        Args:
            plugin_manager: PluginManager instance to use for reloading.
                          If None, will be lazy-loaded on first use.
        """
        self._plugin_manager = plugin_manager
        self._enabled_plugins: Set[str] = set()
        self._watchers: Dict[str, threading.Thread] = {}
        self._stop_flags: Dict[str, threading.Event] = {}
        self._file_mtimes: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._pending_reloads: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._global_enabled = False
    
    def _get_plugin_manager(self):
        """Lazy-load plugin manager to avoid circular imports."""
        if self._plugin_manager is None:
            from backend.plugin_manager import plugin_manager
            self._plugin_manager = plugin_manager
        return self._plugin_manager
    
    def is_enabled(self) -> bool:
        """Check if hot reload is globally enabled."""
        return self._global_enabled
    
    def enable_globally(self):
        """Enable hot reload system globally."""
        with self._lock:
            self._global_enabled = True
            _logger.info("Plugin hot reload enabled globally")
    
    def disable_globally(self):
        """Disable hot reload system globally and stop all watchers."""
        with self._lock:
            self._global_enabled = False
            # Stop all watchers
            for plugin_id in list(self._enabled_plugins):
                self._stop_watcher(plugin_id)
            _logger.info("Plugin hot reload disabled globally")
    
    def enable_for_plugin(self, plugin_id: str) -> bool:
        """
        Enable hot reload for a specific plugin.
        
        Args:
            plugin_id: Plugin identifier
            
        Returns:
            bool: True if successfully enabled, False otherwise
        """
        plugin_dir = os.path.join(PLUGINS_DIR, plugin_id)
        
        if not os.path.isdir(plugin_dir):
            _logger.error("Plugin directory not found: %s", plugin_dir)
            return False
        
        with self._lock:
            if plugin_id in self._enabled_plugins:
                _logger.debug("Hot reload already enabled for plugin: %s", plugin_id)
                return True
            
            self._enabled_plugins.add(plugin_id)
            self._start_watcher(plugin_id, plugin_dir)
            _logger.info("Hot reload enabled for plugin: %s", plugin_id)
            return True
    
    def disable_for_plugin(self, plugin_id: str) -> bool:
        """
        Disable hot reload for a specific plugin.
        
        Args:
            plugin_id: Plugin identifier
            
        Returns:
            bool: True if successfully disabled, False otherwise
        """
        with self._lock:
            if plugin_id not in self._enabled_plugins:
                _logger.debug("Hot reload not enabled for plugin: %s", plugin_id)
                return False
            
            self._enabled_plugins.discard(plugin_id)
            self._stop_watcher(plugin_id)
            _logger.info("Hot reload disabled for plugin: %s", plugin_id)
            return True
    
    def _start_watcher(self, plugin_id: str, plugin_dir: str):
        """Start file watcher thread for a plugin."""
        if plugin_id in self._watchers:
            return
        
        stop_flag = threading.Event()
        self._stop_flags[plugin_id] = stop_flag
        
        watcher_thread = threading.Thread(
            target=self._watch_plugin,
            args=(plugin_id, plugin_dir, stop_flag),
            name=f'plugin_watcher_{plugin_id}',
            daemon=True
        )
        watcher_thread.start()
        self._watchers[plugin_id] = watcher_thread
    
    def _stop_watcher(self, plugin_id: str):
        """Stop file watcher thread for a plugin."""
        if plugin_id in self._stop_flags:
            self._stop_flags[plugin_id].set()
        
        if plugin_id in self._watchers:
            watcher = self._watchers.pop(plugin_id)
            # Give thread time to stop gracefully
            watcher.join(timeout=2.0)
        
        self._stop_flags.pop(plugin_id, None)
        self._file_mtimes.pop(plugin_id, None)
        self._pending_reloads.pop(plugin_id, None)
    
    def _watch_plugin(self, plugin_id: str, plugin_dir: str, stop_flag: threading.Event):
        """
        Watch plugin directory for file changes.
        
        Args:
            plugin_id: Plugin identifier
            plugin_dir: Path to plugin directory
            stop_flag: Event to signal thread stop
        """
        _logger.debug("Starting file watcher for plugin: %s", plugin_id)
        
        # Initial scan
        self._scan_directory(plugin_id, plugin_dir)
        
        while not stop_flag.is_set():
            try:
                # Check for file changes
                changed = self._check_for_changes(plugin_id, plugin_dir)
                
                if changed:
                    # Schedule reload with debounce
                    with self._lock:
                        self._pending_reloads[plugin_id] = time.time() + DEBOUNCE_DELAY
                
                # Process pending reloads
                self._process_pending_reloads()
                
                # Sleep briefly
                time.sleep(0.5)
            
            except Exception as e:
                _logger.error("Error in file watcher for %s: %s", plugin_id, e, exc_info=True)
                time.sleep(1.0)
        
        _logger.debug("Stopped file watcher for plugin: %s", plugin_id)
    
    def _scan_directory(self, plugin_id: str, plugin_dir: str):
        """
        Scan plugin directory and record file modification times.
        
        Args:
            plugin_id: Plugin identifier
            plugin_dir: Path to plugin directory
        """
        for root, dirs, files in os.walk(plugin_dir):
            # Skip __pycache__ and hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext in WATCHED_EXTENSIONS:
                    filepath = os.path.join(root, filename)
                    try:
                        mtime = os.path.getmtime(filepath)
                        self._file_mtimes[plugin_id][filepath] = mtime
                    except OSError:
                        pass
    
    def _check_for_changes(self, plugin_id: str, plugin_dir: str) -> bool:
        """
        Check if any watched files have changed.
        
        Args:
            plugin_id: Plugin identifier
            plugin_dir: Path to plugin directory
            
        Returns:
            bool: True if changes detected, False otherwise
        """
        changed = False
        current_files = set()
        
        for root, dirs, files in os.walk(plugin_dir):
            # Skip __pycache__ and hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            
            for filename in files:
                ext = os.path.splitext(filename)[1].lower()
                if ext in WATCHED_EXTENSIONS:
                    filepath = os.path.join(root, filename)
                    current_files.add(filepath)
                    
                    try:
                        mtime = os.path.getmtime(filepath)
                        old_mtime = self._file_mtimes[plugin_id].get(filepath)
                        
                        if old_mtime is None:
                            # New file
                            _logger.debug("New file detected in %s: %s", plugin_id, filename)
                            self._file_mtimes[plugin_id][filepath] = mtime
                            changed = True
                        elif mtime > old_mtime:
                            # Modified file
                            _logger.debug("Modified file detected in %s: %s", plugin_id, filename)
                            self._file_mtimes[plugin_id][filepath] = mtime
                            changed = True
                    
                    except OSError:
                        pass
        
        # Check for deleted files
        old_files = set(self._file_mtimes[plugin_id].keys())
        deleted_files = old_files - current_files
        
        if deleted_files:
            for filepath in deleted_files:
                filename = os.path.basename(filepath)
                _logger.debug("Deleted file detected in %s: %s", plugin_id, filename)
                del self._file_mtimes[plugin_id][filepath]
            changed = True
        
        return changed
    
    def _process_pending_reloads(self):
        """Process pending plugin reloads with debounce."""
        now = time.time()
        to_reload = []
        
        with self._lock:
            for plugin_id, reload_time in list(self._pending_reloads.items()):
                if now >= reload_time:
                    to_reload.append(plugin_id)
                    del self._pending_reloads[plugin_id]
        
        for plugin_id in to_reload:
            self._reload_plugin(plugin_id)
    
    def _reload_plugin(self, plugin_id: str):
        """
        Reload a plugin.
        
        Args:
            plugin_id: Plugin identifier
        """
        try:
            _logger.info("Hot reloading plugin: %s", plugin_id)
            pm = self._get_plugin_manager()
            pm.reload_plugin(plugin_id)
            pm.add_log(plugin_id, 'info', 'Plugin hot reloaded')
            _logger.info("Successfully reloaded plugin: %s", plugin_id)
        
        except Exception as e:
            _logger.error("Failed to reload plugin %s: %s", plugin_id, e, exc_info=True)
            pm = self._get_plugin_manager()
            pm.add_log(plugin_id, 'error', f'Hot reload failed: {e}')
    
    def get_status(self) -> Dict:
        """
        Get hot reload status.
        
        Returns:
            dict with keys:
            - enabled: bool, global enable status
            - watched_plugins: list of plugin IDs being watched
            - pending_reloads: dict of plugin_id -> reload_time
        """
        with self._lock:
            return {
                'enabled': self._global_enabled,
                'watched_plugins': list(self._enabled_plugins),
                'pending_reloads': dict(self._pending_reloads),
                'active_watchers': len(self._watchers)
            }
    
    def shutdown(self):
        """Shutdown hot reload manager and stop all watchers."""
        _logger.info("Shutting down plugin hot reload manager")
        self.disable_globally()


# Global hot reload manager instance
_hot_reload_manager = None


def get_hot_reload_manager() -> PluginHotReloadManager:
    """Get the global hot reload manager instance."""
    global _hot_reload_manager
    if _hot_reload_manager is None:
        _hot_reload_manager = PluginHotReloadManager()
    return _hot_reload_manager


# Convenience alias
hot_reload_manager = get_hot_reload_manager()
