"""UserMixin — user directory, contacts, tags, groups, and access control.

Provides CRUD for the 7 user-related tables:
  - users
  - user_contacts
  - user_agents
  - user_tags
  - groups
  - group_members
  - user_audit_log

All methods require self._connect() from the host Database class.
Access control (can_communicate) is enforced at the data layer.
"""
import json
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backend.events import emit


class UserMixin:
    """User directory, contacts, tags, groups, and access control.

    All 7 user tables are created via _init_tables() override.
    """

    # ──────────────────────────────────────────────────────────
    # Schema
    # ──────────────────────────────────────────────────────────

    def _init_tables(self):
        """Override _init_tables to create user-related tables alongside existing schema."""
        # Call parent _init_tables first (SchemaMixin or other mixins)
        try:
            super()._init_tables()
        except AttributeError:
            pass
        # Now create user tables
        with self._connect() as conn:
            cursor = conn.cursor()
            self._init_user_tables(cursor)

    def _init_user_tables(self, cursor):
        """Create all 8 user-related tables."""
        # 1. users
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                notes           TEXT DEFAULT '',
                metadata        TEXT DEFAULT '{}',
                avatar_url      TEXT DEFAULT '',
                is_approved     INTEGER DEFAULT 0,
                blocked_at      TEXT,
                blocked_reason  TEXT DEFAULT '',
                erp_sync_enabled INTEGER DEFAULT 0,
                merged_into_id  TEXT,
                deleted_at      TEXT,
                last_synced_at  TEXT,
                sync_status     TEXT DEFAULT 'pending',
                sync_error      TEXT DEFAULT '',
                first_seen_at   TEXT,
                last_active_at  TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_name ON users(name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_merged ON users(merged_into_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_sync_status ON users(sync_status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_deleted_at ON users(deleted_at)")

        # 2. user_contacts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_contacts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                channel_type    TEXT NOT NULL,
                channel_id      TEXT,
                external_user_id TEXT,
                value           TEXT NOT NULL,
                label           TEXT DEFAULT '',
                is_primary      INTEGER DEFAULT 0,
                is_verified     INTEGER DEFAULT 0,
                is_active       INTEGER DEFAULT 1,
                replaced_by     INTEGER,
                sync_source     TEXT DEFAULT 'evonic',
                sync_id         TEXT DEFAULT '',
                deleted_at      TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(channel_type, external_user_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_contacts_user ON user_contacts(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_contacts_external ON user_contacts(channel_type, external_user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_contacts_sync ON user_contacts(sync_source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_contacts_deleted_at ON user_contacts(deleted_at)")

        # 3. user_agents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_agents (
                user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                contact_id      INTEGER REFERENCES user_contacts(id),
                channel_id      TEXT,
                nickname        TEXT DEFAULT '',
                notes           TEXT DEFAULT '',
                is_favorite     INTEGER DEFAULT 0,
                is_auto_created INTEGER DEFAULT 0,
                removed_at      TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, agent_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_agents_agent ON user_agents(agent_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_agents_removed ON user_agents(removed_at)")

        # 4. user_tags
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_tags (
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                tag         TEXT NOT NULL,
                created_by  TEXT,
                source      TEXT DEFAULT 'evonic',
                removed_at  TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, tag)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tags_tag ON user_tags(tag)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tags_source ON user_tags(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_tags_active ON user_tags(user_id) WHERE removed_at IS NULL")

        # 5. groups
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id              TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                description     TEXT DEFAULT '',
                created_by      TEXT,
                deleted_at      TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                updated_at      TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_normalized ON groups(normalized_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_deleted_at ON groups(deleted_at)")

        # 6. group_members
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                group_id    TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                member_type TEXT NOT NULL CHECK(member_type IN ('user', 'agent')),
                member_id   TEXT NOT NULL,
                joined_by   TEXT,
                removed_at  TEXT,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (group_id, member_type, member_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_members_member ON group_members(member_type, member_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_members_active ON group_members(group_id) WHERE removed_at IS NULL")

        # 7. user_audit_log
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT REFERENCES users(id),
                action      TEXT NOT NULL,
                actor_type  TEXT,
                actor_id    TEXT,
                details     TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_audit_user ON user_audit_log(user_id, created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_audit_action ON user_audit_log(action)")
        # 8. tag_rules
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tag_rules (
                id          TEXT PRIMARY KEY,
                tag_pattern TEXT NOT NULL,
                effect      TEXT NOT NULL CHECK(effect IN ('allow_all', 'deny', 'require_role', 'match_agent', 'role_tag')),
                priority    INTEGER NOT NULL DEFAULT 5,
                config      TEXT DEFAULT '{}',
                description TEXT DEFAULT '',
                enabled     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now')),
                updated_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_rules_priority ON tag_rules(priority DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tag_rules_enabled ON tag_rules(enabled)")

        # Seed default tag rules
        cursor.execute("SELECT COUNT(*) FROM tag_rules")
        if cursor.fetchone()[0] == 0:
            seeds = [
                ('public',          'allow_all',    0, '{}',       'Any agent can communicate with users having the public tag'),
                ('agent:*',         'match_agent',  0, '{}',       'Agent-specific tag allows specific agent communication'),
                ('restricted',      'deny',         20, '{}',      'Only agents with admin role can communicate'),
                ('role:*',          'role_tag',      5, '{}',      'Role-based classification tag'),
            ]
            for tag_pattern, effect, priority, config, desc in seeds:
                rule_id = f"builtin_{tag_pattern.replace(':', '_').replace('*', 'all')}"
                cursor.execute("INSERT OR IGNORE INTO tag_rules (id, tag_pattern, effect, priority, config, description) VALUES (?, ?, ?, ?, ?, ?)",
                               (rule_id, tag_pattern, effect, priority, config, desc))

    # ──────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────

    def _normalize_group_name(self, name: str) -> str:
        """Lowercased, spaces/hyphens replaced with dashes."""
        return name.lower().replace(' ', '-').replace('_', '-')

    def _log_audit(self, cursor, user_id: str, action: str, actor_type: str = None,
                   actor_id: str = None, details: dict = None):
        cursor.execute("""
            INSERT INTO user_audit_log (user_id, action, actor_type, actor_id, details)
            VALUES (?, ?, ?, ?, ?)
        """, (user_id, action, actor_type, actor_id, json.dumps(details) if details else '{}'))

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        """Convert a sqlite3.Row to dict, or return None."""
        if row is None:
            return None
        return dict(row)

    def _rows_to_list(self, rows) -> List[Dict[str, Any]]:
        return [dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────
    # Users — CRUD
    # ──────────────────────────────────────────────────────────

    def create_user(self, user_id: str, name: str, notes: str = '',
                    metadata: dict = None, actor_type: str = 'system',
                    actor_id: str = None) -> Dict[str, Any]:
        """Create a new user record."""
        with self._connect() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                INSERT INTO users (id, name, notes, metadata, first_seen_at, last_active_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, name, notes, json.dumps(metadata or {}), now, now))
            self._log_audit(cursor, user_id, 'created', actor_type, actor_id)
            conn.commit()
        emit('user.created', {'user_id': user_id, 'name': name})
        return self.get_user(user_id)

    def get_user(self, user_id: str, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Get a single user by ID."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_deleted:
                cursor.execute("SELECT id, name, notes, metadata, avatar_url, is_approved, blocked_at, blocked_reason, erp_sync_enabled, merged_into_id, deleted_at, last_synced_at, sync_status, sync_error, first_seen_at, last_active_at, created_at, updated_at FROM users WHERE id = ?", (user_id,))
            else:
                cursor.execute("SELECT id, name, notes, metadata, avatar_url, is_approved, blocked_at, blocked_reason, erp_sync_enabled, merged_into_id, deleted_at, last_synced_at, sync_status, sync_error, first_seen_at, last_active_at, created_at, updated_at FROM users WHERE id = ? AND deleted_at IS NULL", (user_id,))
            return self._row_to_dict(cursor.fetchone())

    def update_user(self, user_id: str, updates: dict, actor_type: str = None,
                    actor_id: str = None) -> bool:
        """Update user fields. Only certain columns are allowed."""
        allowed = {'name', 'notes', 'metadata', 'avatar_url', 'is_approved',
                   'blocked_at', 'blocked_reason', 'erp_sync_enabled',
                   'last_active_at', 'last_synced_at', 'sync_status', 'sync_error'}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False
        if 'metadata' in filtered and isinstance(filtered['metadata'], dict):
            filtered['metadata'] = json.dumps(filtered['metadata'])
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [user_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE users SET {set_clause}, updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
                values
            )
            rc = cursor.rowcount
            self._log_audit(cursor, user_id, 'updated', actor_type, actor_id,
                            {'changes': filtered})
            conn.commit()
            if rc > 0:
                emit('user.updated', {'user_id': user_id, 'changes': filtered})
                return True
        return False

    def block_user(self, user_id: str, reason: str = '',
                   actor_type: str = None, actor_id: str = None) -> bool:
        """Block a user by setting is_approved=2."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET is_approved = 2, blocked_at = datetime('now'), "
                "blocked_reason = ?, updated_at = datetime('now') "
                "WHERE id = ? AND deleted_at IS NULL",
                (reason, user_id))
            rc = cursor.rowcount
            self._log_audit(cursor, user_id, 'blocked', actor_type, actor_id,
                            {'reason': reason})
            conn.commit()
            if rc > 0:
                emit('user.blocked', {'user_id': user_id, 'reason': reason})
                return True
        return False

    def unblock_user(self, user_id: str,
                     actor_type: str = None, actor_id: str = None) -> bool:
        """Unblock a user by setting is_approved=1."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET is_approved = 1, blocked_at = NULL, "
                "blocked_reason = '', updated_at = datetime('now') "
                "WHERE id = ? AND deleted_at IS NULL AND is_approved = 2",
                (user_id,))
            rc = cursor.rowcount
            self._log_audit(cursor, user_id, 'unblocked', actor_type, actor_id)
            conn.commit()
            if rc > 0:
                emit('user.unblocked', {'user_id': user_id})
                return True
        return False

    def soft_delete_user(self, user_id: str, actor_type: str = None,
                         actor_id: str = None) -> bool:
        """Soft-delete a user (set deleted_at)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("UPDATE users SET deleted_at = ?, updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
                           (now, user_id))
            rc = cursor.rowcount
            self._log_audit(cursor, user_id, 'deleted', actor_type, actor_id)
            conn.commit()
            if rc > 0:
                emit('user.deleted', {'user_id': user_id})
                return True
        return False

    def hard_delete_user(self, user_id: str, actor_type: str = None,
                         actor_id: str = None) -> bool:
        """GDPR-compliant hard delete: pseudonymize name/hash external_user_id,
        NULL user_id in sessions, then delete the users record."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()

            # 1. Pseudonymize user name (regardless of existing deleted_at)
            cursor.execute("UPDATE users SET name = '[Deleted]', deleted_at = ?, updated_at = datetime('now') WHERE id = ?",
                           (now, user_id))

            # 2. Hash external_user_id in user_contacts
            cursor.execute("SELECT id, external_user_id FROM user_contacts WHERE user_id = ? AND deleted_at IS NULL", (user_id,))
            for row in cursor.fetchall():
                if row['external_user_id']:
                    hashed = hashlib.sha256(row['external_user_id'].encode()).hexdigest()
                    cursor.execute("UPDATE user_contacts SET external_user_id = ?, value = '[deleted]', deleted_at = ? WHERE id = ?",
                                   (hashed, now, row['id']))

            self._log_audit(cursor, user_id, 'hard_deleted', actor_type, actor_id,
                            {'method': 'pseudonymization'})
            conn.commit()
            emit('user.deleted', {'user_id': user_id})
        return True

    def merge_users(self, source_id: str, target_id: str, actor_type: str = None,
                    actor_id: str = None) -> bool:
        """Merge source user into target. Source is soft-deleted with merged_into_id set."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()

            # Reassign contacts to target
            cursor.execute("UPDATE user_contacts SET user_id = ? WHERE user_id = ? AND deleted_at IS NULL",
                           (target_id, source_id))

            # Reassign agents to target
            for row in cursor.execute("SELECT agent_id FROM user_agents WHERE user_id = ? AND removed_at IS NULL", (source_id,)):
                # Remove any existing link from target to same agent to avoid PK conflict
                cursor.execute("UPDATE user_agents SET removed_at = datetime('now') WHERE user_id = ? AND agent_id = ? AND removed_at IS NULL",
                               (target_id, row['agent_id']))
            cursor.execute("UPDATE user_agents SET user_id = ? WHERE user_id = ?", (target_id, source_id))

            # Reassign tags to target
            for row in cursor.execute("SELECT tag FROM user_tags WHERE user_id = ? AND removed_at IS NULL LIMIT 1000", (source_id,)):
                cursor.execute("UPDATE user_tags SET removed_at = datetime('now') WHERE user_id = ? AND tag = ? AND removed_at IS NULL",
                               (target_id, row['tag']))
            cursor.execute("UPDATE user_tags SET user_id = ? WHERE user_id = ?", (target_id, source_id))

            # Reassign group memberships to target
            for row in cursor.execute("SELECT group_id FROM group_members WHERE member_type = 'user' AND member_id = ? AND removed_at IS NULL", (source_id,)):
                cursor.execute("UPDATE group_members SET removed_at = datetime('now') WHERE group_id = ? AND member_type = 'user' AND member_id = ? AND removed_at IS NULL",
                               (row['group_id'], target_id))
            cursor.execute("UPDATE group_members SET member_id = ? WHERE member_type = 'user' AND member_id = ?",
                           (target_id, source_id))

            # Mark source as merged
            now = datetime.utcnow().isoformat()
            cursor.execute("UPDATE users SET merged_into_id = ?, deleted_at = ?, updated_at = datetime('now') WHERE id = ?",
                           (target_id, now, source_id))

            self._log_audit(cursor, target_id, 'merged', actor_type, actor_id,
                            {'source_id': source_id})
            conn.commit()
            emit('user.merged', {'source_id': source_id, 'target_id': target_id})
        return True

    def search_users(self, query: str = '', tags: List[str] = None,
                     group_id: str = None, limit: int = 20, offset: int = 0,
                     include_deleted: bool = False) -> List[Dict[str, Any]]:
        """Search users by name, tags, and/or group membership."""
        conditions = []
        params = []

        if not include_deleted:
            conditions.append("u.deleted_at IS NULL")

        if query:
            conditions.append("(u.name LIKE ? OR u.notes LIKE ?)")
            params.extend([f"%{query}%", f"%{query}%"])

        where = " AND ".join(conditions) if conditions else "1=1"

        base = "FROM users u"
        joins = ""

        if tags:
            placeholders = ",".join("?" for _ in tags)
            # Build parameterized IN (...); table/column names are compile-time constants.
            joins += (" JOIN user_tags ut ON ut.user_id = u.id AND ut.tag IN ("
                      + placeholders + ") AND ut.removed_at IS NULL")
            params.extend(tags)

        if group_id:
            joins += (" JOIN group_members gm ON gm.member_id = u.id"
                      " AND gm.member_type = 'user' AND gm.group_id = ? AND gm.removed_at IS NULL")
            params.append(group_id)

        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute(
                # All table/column names in base, joins, where are compile-time constants.
                "SELECT DISTINCT u.* " + base + joins + " WHERE " + where
                + " ORDER BY u.last_active_at DESC NULLS LAST LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            return self._rows_to_list(cursor.fetchall())

    def list_users(self, limit: int = 20, offset: int = 0,
                   include_deleted: bool = False) -> List[Dict[str, Any]]:
        """List all non-deleted users with pagination."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_deleted:
                cursor.execute("SELECT id, name, notes, metadata, avatar_url, is_approved, blocked_at, blocked_reason, erp_sync_enabled, merged_into_id, deleted_at, last_synced_at, sync_status, sync_error, first_seen_at, last_active_at, created_at, updated_at FROM users ORDER BY last_active_at DESC NULLS LAST LIMIT ? OFFSET ?",
                               (limit, offset))
            else:
                cursor.execute("SELECT id, name, notes, metadata, avatar_url, is_approved, blocked_at, blocked_reason, erp_sync_enabled, merged_into_id, deleted_at, last_synced_at, sync_status, sync_error, first_seen_at, last_active_at, created_at, updated_at FROM users WHERE deleted_at IS NULL ORDER BY last_active_at DESC NULLS LAST LIMIT ? OFFSET ?",
                               (limit, offset))
            return self._rows_to_list(cursor.fetchall())

    # ──────────────────────────────────────────────────────────
    # Contacts
    # ──────────────────────────────────────────────────────────

    def add_contact(self, user_id: str, channel_type: str, external_user_id: str,
                    value: str, channel_id: str = None, label: str = '',
                    is_primary: bool = False, sync_source: str = 'evonic',
                    actor_type: str = None, actor_id: str = None) -> Optional[Dict[str, Any]]:
        """Add a contact for a user."""
        with self._connect() as conn:
            cursor = conn.cursor()
            # Auto-set primary if this is the first contact
            cursor.execute("SELECT COUNT(*) FROM user_contacts WHERE user_id = ? AND deleted_at IS NULL", (user_id,))
            existing_count = cursor.fetchone()[0]
            is_primary = existing_count == 0
            cursor.execute("""
                INSERT INTO user_contacts (user_id, channel_type, channel_id, external_user_id,
                    value, label, is_primary, sync_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, channel_type, channel_id, external_user_id, value, label,
                  1 if is_primary else 0, sync_source))
            contact_id = cursor.lastrowid
            self._log_audit(cursor, user_id, 'contact_added', actor_type, actor_id,
                            {'channel_type': channel_type, 'external_user_id': external_user_id})
            conn.commit()
            emit('contact.added', {'user_id': user_id, 'contact_id': contact_id})
            return self.get_contact(contact_id)

    def get_contact(self, contact_id: int, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Get a single contact by ID."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_deleted:
                cursor.execute("SELECT id, user_id, channel_type, channel_id, external_user_id, value, label, is_primary, is_verified, is_active, replaced_by, sync_source, sync_id, deleted_at, created_at, updated_at FROM user_contacts WHERE id = ?", (contact_id,))
            else:
                cursor.execute("SELECT id, user_id, channel_type, channel_id, external_user_id, value, label, is_primary, is_verified, is_active, replaced_by, sync_source, sync_id, deleted_at, created_at, updated_at FROM user_contacts WHERE id = ? AND deleted_at IS NULL", (contact_id,))
            return self._row_to_dict(cursor.fetchone())

    def get_contacts(self, user_id: str, include_deleted: bool = False) -> List[Dict[str, Any]]:
        """Get all contacts for a user."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_deleted:
                cursor.execute("SELECT id, user_id, channel_type, channel_id, external_user_id, value, label, is_primary, is_verified, is_active, replaced_by, sync_source, sync_id, deleted_at, created_at, updated_at FROM user_contacts WHERE user_id = ? ORDER BY is_primary DESC, created_at LIMIT 1000", (user_id,))
            else:
                cursor.execute("SELECT id, user_id, channel_type, channel_id, external_user_id, value, label, is_primary, is_verified, is_active, replaced_by, sync_source, sync_id, deleted_at, created_at, updated_at FROM user_contacts WHERE user_id = ? AND deleted_at IS NULL ORDER BY is_primary DESC, created_at LIMIT 1000", (user_id,))
            return self._rows_to_list(cursor.fetchall())

    def find_user_by_contact(self, channel_type: str, external_user_id: str) -> Optional[Dict[str, Any]]:
        """Find a user by their contact (channel_type + external_user_id)."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.* FROM users u
                JOIN user_contacts uc ON uc.user_id = u.id
                WHERE uc.channel_type = ? AND uc.external_user_id = ?
                  AND uc.deleted_at IS NULL AND u.deleted_at IS NULL
                  AND u.is_approved != 2 AND u.blocked_at IS NULL
                LIMIT 1
            """, (channel_type, external_user_id))
            row = cursor.fetchone()
            return self._row_to_dict(row)

    def is_user_blocked_by_external_id(self, external_user_id: str) -> bool:
        """Check if a user with the given external_user_id is blocked.
        
        Returns True if the user is blocked (is_approved == 2 or blocked_at IS NOT NULL),
        False if the user is not found or is not blocked.
        """
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.is_approved, u.blocked_at FROM users u
                JOIN user_contacts uc ON uc.user_id = u.id
                WHERE uc.external_user_id = ?
                  AND uc.deleted_at IS NULL AND u.deleted_at IS NULL
                LIMIT 1
            """, (external_user_id,))
            row = cursor.fetchone()
            if row is None:
                return False
            is_approved = row['is_approved'] if isinstance(row, dict) else row[0]
            blocked_at = row['blocked_at'] if isinstance(row, dict) else row[1]
            return is_approved == 2 or blocked_at is not None

    def update_contact(self, contact_id: int, updates: dict,
                       actor_type: str = None, actor_id: str = None) -> bool:
        """Update a contact record."""
        allowed = {'external_user_id', 'value', 'label', 'is_primary', 'is_verified',
                   'is_active', 'sync_source', 'sync_id'}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [contact_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE user_contacts SET {set_clause}, updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
                values
            )
            conn.commit()
            if cursor.rowcount > 0:
                emit('contact.updated', {'contact_id': contact_id, 'changes': filtered})
                return True
        return False

    def set_primary_contact(self, user_id: str, contact_id: int,
                            actor_type: str = None, actor_id: str = None) -> bool:
        """Set one contact as primary (unset others)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE user_contacts SET is_primary = 0 WHERE user_id = ? AND deleted_at IS NULL", (user_id,))
            cursor.execute("UPDATE user_contacts SET is_primary = 1, updated_at = datetime('now') WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
                           (contact_id, user_id))
            rc = cursor.rowcount
            self._log_audit(cursor, user_id, 'contact_primary_changed', actor_type, actor_id,
                            {'contact_id': contact_id})
            conn.commit()
            return rc > 0

    def soft_delete_contact(self, contact_id: int, actor_type: str = None,
                            actor_id: str = None) -> bool:
        """Soft-delete a contact. Clears external_user_id to free UNIQUE constraint."""
        with self._connect() as conn:
            cursor = conn.cursor()
            now = datetime.utcnow().isoformat()
            cursor.execute("""
                UPDATE user_contacts SET external_user_id = NULL, value = '[deleted]',
                    is_active = 0, deleted_at = ?, updated_at = datetime('now')
                WHERE id = ? AND deleted_at IS NULL
            """, (now, contact_id))
            rc = cursor.rowcount
            self._log_audit(cursor, None, 'contact_removed', actor_type, actor_id,
                            {'contact_id': contact_id})
            conn.commit()
            if rc > 0:
                emit('contact.removed', {'contact_id': contact_id})
                return True
        return False

    # ──────────────────────────────────────────────────────────
    # User-Agent Links
    # ──────────────────────────────────────────────────────────

    def link_user_to_agent(self, user_id: str, agent_id: str,
                           contact_id: int = None, channel_id: str = None,
                           nickname: str = '', is_auto_created: bool = False,
                           actor_type: str = None, actor_id: str = None) -> bool:
        """Link a user to an agent."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO user_agents (user_id, agent_id, contact_id, channel_id,
                    nickname, is_auto_created)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, agent_id, contact_id, channel_id, nickname,
                  1 if is_auto_created else 0))
            conn.commit()
            return cursor.rowcount > 0

    def unlink_user_from_agent(self, user_id: str, agent_id: str,
                                actor_type: str = None, actor_id: str = None) -> bool:
        """Soft-remove a user-agent link."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_agents SET removed_at = datetime('now'), updated_at = datetime('now')
                WHERE user_id = ? AND agent_id = ? AND removed_at IS NULL
            """, (user_id, agent_id))
            conn.commit()
            return cursor.rowcount > 0

    def get_agent_users(self, agent_id: str, include_removed: bool = False) -> List[Dict[str, Any]]:
        """Get all users linked to an agent."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_removed:
                cursor.execute("""
                    SELECT u.* FROM users u
                    JOIN user_agents ua ON ua.user_id = u.id
                    WHERE ua.agent_id = ?
                """, (agent_id,))
            else:
                cursor.execute("""
                    SELECT u.* FROM users u
                    JOIN user_agents ua ON ua.user_id = u.id
                    WHERE ua.agent_id = ? AND ua.removed_at IS NULL AND u.deleted_at IS NULL
                """, (agent_id,))
            return self._rows_to_list(cursor.fetchall())

    def get_user_agents(self, user_id: str, include_removed: bool = False) -> List[Dict[str, Any]]:
        """Get all agents linked to a user. Returns ua.* + a.id, a.name if available."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_removed:
                cursor.execute("""
                    SELECT ua.*, a.name AS agent_name FROM user_agents ua
                    LEFT JOIN agents a ON a.id = ua.agent_id
                    WHERE ua.user_id = ?
                """, (user_id,))
            else:
                cursor.execute("""
                    SELECT ua.*, a.name AS agent_name FROM user_agents ua
                    LEFT JOIN agents a ON a.id = ua.agent_id
                    WHERE ua.user_id = ? AND ua.removed_at IS NULL
                """, (user_id,))
            return self._rows_to_list(cursor.fetchall())

    # ──────────────────────────────────────────────────────────
    # Tags
    # ──────────────────────────────────────────────────────────

    def add_tag(self, user_id: str, tag: str, created_by: str = None,
                source: str = 'evonic', actor_type: str = None,
                actor_id: str = None) -> bool:
        """Add a tag to a user. If tag already exists (removed_at IS NOT NULL), restore it."""
        with self._connect() as conn:
            cursor = conn.cursor()
            # Check if tag exists but was removed — restore it
            cursor.execute("SELECT removed_at FROM user_tags WHERE user_id = ? AND tag = ?", (user_id, tag))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("""
                    UPDATE user_tags SET removed_at = NULL, created_by = ?, source = ?, created_at = datetime('now')
                    WHERE user_id = ? AND tag = ?
                """, (created_by, source, user_id, tag))
            else:
                cursor.execute("""
                    INSERT INTO user_tags (user_id, tag, created_by, source)
                    VALUES (?, ?, ?, ?)
                """, (user_id, tag, created_by, source))
            self._log_audit(cursor, user_id, 'tag_added', actor_type, actor_id, {'tag': tag})
            conn.commit()
            emit('tag.added', {'user_id': user_id, 'tag': tag})
            return True

    def remove_tag(self, user_id: str, tag: str, actor_type: str = None,
                   actor_id: str = None) -> bool:
        """Soft-remove a tag from a user."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE user_tags SET removed_at = datetime('now')
                WHERE user_id = ? AND tag = ? AND removed_at IS NULL
            """, (user_id, tag))
            rc = cursor.rowcount
            self._log_audit(cursor, user_id, 'tag_removed', actor_type, actor_id, {'tag': tag})
            conn.commit()
            if rc > 0:
                emit('tag.removed', {'user_id': user_id, 'tag': tag})
                return True
        return False

    def get_tags(self, user_id: str, include_removed: bool = False) -> List[str]:
        """Get all active (or all) tags for a user."""
        with self._connect() as conn:
            cursor = conn.cursor()
            if include_removed:
                cursor.execute("SELECT tag FROM user_tags WHERE user_id = ? LIMIT 1000", (user_id,))
            else:
                cursor.execute("SELECT tag FROM user_tags WHERE user_id = ? AND removed_at IS NULL LIMIT 1000", (user_id,))
            return [row[0] for row in cursor.fetchall()]

    def search_by_tag(self, tag: str, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """Find users with a specific tag."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("""
                SELECT u.* FROM users u
                JOIN user_tags ut ON ut.user_id = u.id
                WHERE ut.tag = ? AND ut.removed_at IS NULL AND u.deleted_at IS NULL
                ORDER BY u.last_active_at DESC NULLS LAST LIMIT ? OFFSET ?
            """, (tag, limit, offset))
            return self._rows_to_list(cursor.fetchall())

    def search_by_tags_multiple(self, tags: List[str], match_all: bool = True,
                                 limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """Find users matching one or all tags."""
        if not tags:
            return []
        placeholders = ",".join("?" for _ in tags)
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if match_all:
                cursor.execute(f"""
                    SELECT u.* FROM users u
                    JOIN user_tags ut ON ut.user_id = u.id
                    WHERE ut.tag IN ({placeholders}) AND ut.removed_at IS NULL AND u.deleted_at IS NULL
                    GROUP BY u.id HAVING COUNT(DISTINCT ut.tag) = ?
                    ORDER BY u.last_active_at DESC NULLS LAST LIMIT ? OFFSET ?
                """, tags + [len(tags), limit, offset])
            else:
                cursor.execute(f"""
                    SELECT DISTINCT u.* FROM users u
                    JOIN user_tags ut ON ut.user_id = u.id
                    WHERE ut.tag IN ({placeholders}) AND ut.removed_at IS NULL AND u.deleted_at IS NULL
                    ORDER BY u.last_active_at DESC NULLS LAST LIMIT ? OFFSET ?
                """, tags + [limit, offset])
            return self._rows_to_list(cursor.fetchall())

    # ──────────────────────────────────────────────────────────
    # Groups
    # ──────────────────────────────────────────────────────────

    def create_group(self, name: str, description: str = '',
                     group_id: str = None, created_by: str = None) -> Optional[Dict[str, Any]]:
        """Create a new group. group_id is auto-generated if not provided."""
        normalized = self._normalize_group_name(name)
        if group_id is None:
            group_id = normalized
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO groups (id, name, normalized_name, description, created_by)
                VALUES (?, ?, ?, ?, ?)
            """, (group_id, name, normalized, description, created_by))
            conn.commit()
            return self.get_group(group_id)

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        """Get a single group by ID."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, normalized_name, description, created_by, deleted_at, created_at, updated_at FROM groups WHERE id = ? AND deleted_at IS NULL", (group_id,))
            return self._row_to_dict(cursor.fetchone())

    def delete_group(self, group_id: str, actor_type: str = None,
                     actor_id: str = None) -> bool:
        """Soft-delete a group."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE groups SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ? AND deleted_at IS NULL",
                           (group_id,))
            conn.commit()
            return cursor.rowcount > 0

    def list_groups(self, include_deleted: bool = False) -> List[Dict[str, Any]]:
        """List all groups."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if include_deleted:
                cursor.execute("SELECT id, name, normalized_name, description, created_by, deleted_at, created_at, updated_at FROM groups ORDER BY name LIMIT 10000")
            else:
                cursor.execute("SELECT id, name, normalized_name, description, created_by, deleted_at, created_at, updated_at FROM groups WHERE deleted_at IS NULL ORDER BY name LIMIT 10000")
            return self._rows_to_list(cursor.fetchall())

    def search_groups(self, query: str) -> List[Dict[str, Any]]:
        """Search groups by name."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("SELECT id, name, normalized_name, description, created_by, deleted_at, created_at, updated_at FROM groups WHERE deleted_at IS NULL AND (name LIKE ? OR normalized_name LIKE ?) ORDER BY name LIMIT 100",
                           (f"%{query}%", f"%{query}%"))
            return self._rows_to_list(cursor.fetchall())

    def add_group_member(self, group_id: str, member_type: str, member_id: str,
                         joined_by: str = None, actor_type: str = None,
                         actor_id: str = None) -> bool:
        """Add a member (user or agent) to a group."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO group_members (group_id, member_type, member_id, joined_by)
                VALUES (?, ?, ?, ?)
            """, (group_id, member_type, member_id, joined_by))
            conn.commit()
            if cursor.rowcount > 0:
                emit('group.changed', {'group_id': group_id, 'action': 'member_added',
                                       'member_type': member_type, 'member_id': member_id})
                return True
        return False

    def remove_group_member(self, group_id: str, member_type: str, member_id: str,
                            actor_type: str = None, actor_id: str = None) -> bool:
        """Remove a member from a group (soft)."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE group_members SET removed_at = datetime('now'), updated_at = datetime('now')
                WHERE group_id = ? AND member_type = ? AND member_id = ? AND removed_at IS NULL
            """, (group_id, member_type, member_id))
            conn.commit()
            if cursor.rowcount > 0:
                emit('group.changed', {'group_id': group_id, 'action': 'member_removed',
                                       'member_type': member_type, 'member_id': member_id})
                return True
        return False

    def get_group_members(self, group_id: str) -> Tuple[List[Dict], List[Dict]]:
        """Get all active members of a group. Returns (users, agents)."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("""
                SELECT group_id, member_type, member_id, joined_by, removed_at, created_at, updated_at FROM group_members
                WHERE group_id = ? AND removed_at IS NULL AND member_type = 'user'
                ORDER BY created_at
            """, (group_id,))
            users = self._rows_to_list(cursor.fetchall())
            cursor.execute("""
                SELECT group_id, member_type, member_id, joined_by, removed_at, created_at, updated_at FROM group_members
                WHERE group_id = ? AND removed_at IS NULL AND member_type = 'agent'
                ORDER BY created_at
            """, (group_id,))
            agents = self._rows_to_list(cursor.fetchall())
            return users, agents

    def get_user_groups(self, user_id: str) -> List[Dict[str, Any]]:
        """Get all groups a user belongs to."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("""
                SELECT g.* FROM groups g
                JOIN group_members gm ON gm.group_id = g.id
                WHERE gm.member_type = 'user' AND gm.member_id = ? AND gm.removed_at IS NULL AND g.deleted_at IS NULL
            """, (user_id,))
            return self._rows_to_list(cursor.fetchall())

    def get_agent_groups(self, agent_id: str) -> List[Dict[str, Any]]:
        """Get all groups an agent belongs to."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("""
                SELECT g.* FROM groups g
                JOIN group_members gm ON gm.group_id = g.id
                WHERE gm.member_type = 'agent' AND gm.member_id = ? AND gm.removed_at IS NULL AND g.deleted_at IS NULL
            """, (agent_id,))
            return self._rows_to_list(cursor.fetchall())

    # ──────────────────────────────────────────────────────────
    # Access Control
    # ──────────────────────────────────────────────────────────

    def can_communicate(self, agent_id: str, user_id: str) -> bool:
        """Check if an agent can communicate with a user using DB-driven tag rules.

        Access is granted if:
          1. They share at least one group (both active members), OR
          2. Tag rules allow it (allow_all / match_agent rules evaluated first)
          3. NOT if a deny/require_role rule applies (priority-ordered)
        """
        with self._connect() as conn:
            cursor = conn.cursor()

            # Check group overlap first (fastest path)
            cursor.execute("""
                SELECT 1 FROM group_members gm1
                JOIN group_members gm2 ON gm1.group_id = gm2.group_id
                WHERE gm1.member_type = 'agent' AND gm1.member_id = ? AND gm1.removed_at IS NULL
                  AND gm2.member_type = 'user'  AND gm2.member_id = ? AND gm2.removed_at IS NULL
                LIMIT 1
            """, (agent_id, user_id))
            if cursor.fetchone():
                return True

        # Evaluate tag rules
        tags = self.get_tags(user_id)
        if not tags:
            return False

        tag_rules = self.get_tag_rules(enabled_only=True)
        allowed, reasons = self._evaluate_tag_rules(agent_id, tags, tag_rules)
        return allowed

    def can_manage_user(self, agent_id: str, user_id: str = None) -> bool:
        """Check if an agent can modify user profile, tags, groups.
        Requires 'admin' or 'user-manager' role."""
        return self._agent_has_role(agent_id, 'admin') or self._agent_has_role(agent_id, 'user-manager')

    def can_block_user(self, agent_id: str, user_id: str = None) -> bool:
        """Check if an agent can block/unblock a user. Requires 'admin' role."""
        return self._agent_has_role(agent_id, 'admin')

    def can_merge_users(self, agent_id: str) -> bool:
        """Check if an agent can merge user records. Requires 'admin' role."""
        return self._agent_has_role(agent_id, 'admin')

    def get_access_control_info(self, agent_id: str, user_id: str) -> Dict[str, Any]:
        """Return transparency info: why access is granted or denied, with rule evaluation trace."""
        result = {'agent_id': agent_id, 'user_id': user_id, 'can_communicate': False, 'reasons': []}

        # Check group overlap first
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT g.name FROM groups g
                JOIN group_members gm1 ON gm1.group_id = g.id
                JOIN group_members gm2 ON gm2.group_id = g.id
                WHERE gm1.member_type = 'agent' AND gm1.member_id = ? AND gm1.removed_at IS NULL
                  AND gm2.member_type = 'user'  AND gm2.member_id = ? AND gm2.removed_at IS NULL
                  AND g.deleted_at IS NULL
            """, (agent_id, user_id))
            shared_groups = [row[0] for row in cursor.fetchall()]
            if shared_groups:
                result['can_communicate'] = True
                result['reasons'].append(f"Shared groups: {', '.join(shared_groups)}")

        # Tag rules evaluation (only matters if group check didn't already allow)
        if not result['can_communicate']:
            tags = self.get_tags(user_id)
            tag_rules = self.get_tag_rules(enabled_only=True)
            allowed, rule_reasons = self._evaluate_tag_rules(agent_id, tags, tag_rules)

            if rule_reasons:
                result['reasons'].extend(rule_reasons)

            if allowed:
                result['can_communicate'] = True

            if not result['can_communicate'] and not result['reasons']:
                result['reasons'].append("No shared groups and no allow-rules matched")

        # Permission checks
        result['can_manage_user'] = self.can_manage_user(agent_id, user_id)
        result['can_block_user'] = self.can_block_user(agent_id, user_id)
        result['can_merge_users'] = self.can_merge_users(agent_id)
        result['user_tags'] = self.get_tags(user_id)
        result['active_rules'] = [{'id': r['id'], 'tag_pattern': r['tag_pattern'],
                                   'effect': r['effect'], 'priority': r['priority']}
                                  for r in self.get_tag_rules(enabled_only=True)]
        result['agent_groups'] = [g['name'] for g in self.get_agent_groups(agent_id)]

        return result

    # ──────────────────────────────────────────────────────────
    # Notification Resolution
    # ──────────────────────────────────────────────────────────

    def resolve_notification_target(self, user_id: str, agent_id: str = None) -> Optional[Dict[str, Any]]:
        """Resolve the best contact for sending a notification to a user.

        Returns a dict with channel info, or None if no reachable contact exists.
        Honors can_communicate checks.
        """
        if agent_id and not self.can_communicate(agent_id, user_id):
            return None

        contacts = self.get_contacts(user_id)
        if not contacts:
            return None

        # Primary contact first, then first active contact
        for c in contacts:
            if c.get('is_primary') and c.get('is_active'):
                return {
                    'user_id': user_id,
                    'contact_id': c['id'],
                    'channel_type': c['channel_type'],
                    'external_user_id': c['external_user_id'],
                    'value': c['value'],
                }
        # Fallback to first active contact
        for c in contacts:
            if c.get('is_active'):
                return {
                    'user_id': user_id,
                    'contact_id': c['id'],
                    'channel_type': c['channel_type'],
                    'external_user_id': c['external_user_id'],
                    'value': c['value'],
                }
        return None

    # ──────────────────────────────────────────────────────────

    # -----------------------------------------------------------------------
    # Tag Rules - CRUD
    # -----------------------------------------------------------------------

    def get_tag_rules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """List all tag rules, ordered by priority descending."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            if enabled_only:
                cursor.execute("SELECT id, tag_pattern, effect, priority, config, description, enabled, created_at, updated_at FROM tag_rules WHERE enabled = 1 ORDER BY priority DESC")
            else:
                cursor.execute("SELECT id, tag_pattern, effect, priority, config, description, enabled, created_at, updated_at FROM tag_rules ORDER BY priority DESC")
            return self._rows_to_list(cursor.fetchall())

    def get_tag_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """Get a single tag rule by ID."""
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            cursor.execute("SELECT id, tag_pattern, effect, priority, config, description, enabled, created_at, updated_at FROM tag_rules WHERE id = ?", (rule_id,))
            return self._row_to_dict(cursor.fetchone())

    def create_tag_rule(self, rule_id: str, tag_pattern: str, effect: str,
                        priority: int = 5, config: dict = None,
                        description: str = '') -> Optional[Dict[str, Any]]:
        """Create a new tag rule."""
        valid_effects = {'allow_all', 'deny', 'require_role', 'match_agent', 'role_tag'}
        if effect not in valid_effects:
            raise ValueError(f"Invalid effect: {effect}. Must be one of {valid_effects}")
        config_json = json.dumps(config or {})
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tag_rules (id, tag_pattern, effect, priority, config, description)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (rule_id, tag_pattern, effect, priority, config_json, description))
            conn.commit()
        return self.get_tag_rule(rule_id)

    def update_tag_rule(self, rule_id: str, updates: dict) -> Optional[Dict[str, Any]]:
        """Update a tag rule. Allowed fields: tag_pattern, effect, priority, config, description, enabled."""
        allowed = {'tag_pattern', 'effect', 'priority', 'config', 'description', 'enabled'}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return self.get_tag_rule(rule_id)
        if 'config' in filtered and isinstance(filtered['config'], dict):
            filtered['config'] = json.dumps(filtered['config'])
        if 'effect' in filtered:
            valid = {'allow_all', 'deny', 'require_role', 'match_agent', 'role_tag'}
            if filtered['effect'] not in valid:
                raise ValueError(f"Invalid effect: {filtered['effect']}")
        set_clause = ', '.join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [rule_id]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE tag_rules SET {set_clause}, updated_at = datetime('now') WHERE id = ?", values)
            conn.commit()
        return self.get_tag_rule(rule_id)

    def delete_tag_rule(self, rule_id: str) -> bool:
        """Delete a tag rule."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tag_rules WHERE id = ?", (rule_id,))
            conn.commit()
            return cursor.rowcount > 0

    def toggle_tag_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        """Toggle the enabled status of a tag rule."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE tag_rules SET enabled = CASE WHEN enabled THEN 0 ELSE 1 END, updated_at = datetime('now') WHERE id = ?", (rule_id,))
            conn.commit()
        return self.get_tag_rule(rule_id)

    # -----------------------------------------------------------------------
    # Tag Rules - Engine
    # -----------------------------------------------------------------------

    @staticmethod
    def _tag_matches_pattern(tags: List[str], pattern: str) -> bool:
        """Check if any tag matches a pattern (exact or wildcard)."""
        if '*' in pattern:
            prefix = pattern.replace('*', '')
            return any(t.startswith(prefix) for t in tags)
        return pattern in tags

    def _evaluate_tag_rules(self, agent_id: str, tags: List[str],
                            tag_rules: List[Dict[str, Any]] = None) -> tuple:
        """Evaluate tag rules and return (allowed: bool, reasons: List[str]).

        First pass: unconditional allow rules (allow_all, match_agent) return immediately.
        Second pass: priority-ordered evaluation -- deny / require_role rules.
        """
        if tag_rules is None:
            tag_rules = self.get_tag_rules(enabled_only=True)

        sorted_rules = sorted(tag_rules, key=lambda r: r['priority'], reverse=True)
        reasons = []

        # FIRST PASS: unconditional allow rules (allow_all, match_agent)
        for rule in sorted_rules:
            if not rule['enabled']:
                continue
            if not self._tag_matches_pattern(tags, rule['tag_pattern']):
                continue

            if rule['effect'] == 'allow_all':
                reasons.append(f"Allow-all rule matched: {rule['tag_pattern']} (rule: {rule['id']})")
                return (True, reasons)

            elif rule['effect'] == 'match_agent':
                if rule['tag_pattern'] == 'agent:*':
                    agent_tag = f'agent:{agent_id}'
                    if agent_tag in tags:
                        reasons.append(f"Agent match: {rule['tag_pattern']} -> {agent_id} (rule: {rule['id']})")
                        return (True, reasons)
                # For exact match agent:X patterns
                specific_pattern = rule['tag_pattern'].replace('*', agent_id)
                if specific_pattern in tags:
                    reasons.append(f"Agent match: {rule['tag_pattern']} -> {agent_id} (rule: {rule['id']})")
                    return (True, reasons)

        # SECOND PASS: restrictive rules (deny, require_role) - priority descending
        for rule in sorted_rules:
            if not rule['enabled']:
                continue
            if not self._tag_matches_pattern(tags, rule['tag_pattern']):
                continue

            if rule['effect'] == 'deny':
                reasons.append(f"Deny rule: {rule['tag_pattern']} (priority {rule['priority']}, rule: {rule['id']})")

            elif rule['effect'] == 'require_role':
                cfg = json.loads(rule.get('config', '{}')) if isinstance(rule.get('config'), str) else rule.get('config', {})
                required_roles = cfg.get('roles', [])
                if required_roles:
                    has_any = any(self._agent_has_role(agent_id, r) for r in required_roles)
                    if not has_any:
                        reasons.append(f"Role required: {required_roles} for tag {rule['tag_pattern']} (rule: {rule['id']})")

        if reasons:
            return (False, reasons)

        reasons.append("Default deny: no allow-rule matched")
        return (False, reasons)

    # Internal helpers — tag rules & agent roles
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def _tag_priority(tag: str) -> int:
        priorities = {
            'restricted': 20,
            'erp-managed': 40,
            'vip': 10,
        }
        if tag.startswith('role:'):
            return 5
        return priorities.get(tag, 0)

    def _agent_has_role(self, agent_id: str, role: str) -> bool:
        """Check if an agent has a specific role via group membership.
        Accepts either a single role string or a list of roles (returns True if any match)."""
        if isinstance(role, (list, tuple)):
            if not role:
                return False
            placeholders = ",".join("?" for _ in role)
            roles = role
        else:
            placeholders = "?"
            roles = [role]
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT 1 FROM group_members gm
                JOIN groups g ON gm.group_id = g.id
                WHERE gm.member_type = 'agent'
                  AND gm.member_id = ?
                  AND g.normalized_name IN ({placeholders})
                  AND gm.removed_at IS NULL
                  AND g.deleted_at IS NULL
                LIMIT 1
            """, (agent_id, *roles))
            return cursor.fetchone() is not None

    # ──────────────────────────────────────────────────────────
    # Audit Log Query
    # ──────────────────────────────────────────────────────────

    def get_audit_log(self, user_id: str = None, action: str = None,
                      limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Query the audit log, optionally filtered by user and/or action."""
        conditions = []
        params = []
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        where = " AND ".join(conditions) if conditions else "1=1"
        with self._connect() as conn:
            conn.row_factory = self._row_factory
            cursor = conn.cursor()
            # All column names are compile-time constants; values use ? placeholders.
            cursor.execute(
                "SELECT id, user_id, action, actor_type, actor_id, details, created_at FROM user_audit_log WHERE " + where
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            return self._rows_to_list(cursor.fetchall())

    # ──────────────────────────────────────────────────────────
    # Row factory helper
    # ──────────────────────────────────────────────────────────

    @property
    def _row_factory(self):
        import sqlite3
        return sqlite3.Row
