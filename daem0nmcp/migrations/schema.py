"""
Database migrations for Daem0nMCP.

Handles schema updates for existing databases.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from daem0nmcp.schema_version import CURRENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)

# Migration definitions: (version, description, sql_statements)
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Add vector_embedding column",
        [
            """
        ALTER TABLE memories ADD COLUMN vector_embedding BLOB;
        """
        ],
    ),
    (
        2,
        "Create FTS5 virtual table for full-text search",
        [
            """
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            rationale,
            tags,
            content='memories',
            content_rowid='id'
        );
        """,
            """
        INSERT OR IGNORE INTO memories_fts(rowid, content, rationale, tags)
        SELECT
            id,
            content,
            COALESCE(rationale, ''),
            COALESCE((SELECT group_concat(value, ' ') FROM json_each(tags)), '')
        FROM memories;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content, rationale, tags)
            SELECT new.id, new.content, COALESCE(new.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags)), '');
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, rationale, tags)
            SELECT 'delete', old.id, old.content, COALESCE(old.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(old.tags)), '');
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content, rationale, tags)
            SELECT 'delete', old.id, old.content, COALESCE(old.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(old.tags)), '');
            INSERT INTO memories_fts(rowid, content, rationale, tags)
            SELECT new.id, new.content, COALESCE(new.rationale, ''),
                   COALESCE((SELECT group_concat(value, ' ') FROM json_each(new.tags)), '');
        END;
        """,
        ],
    ),
    (
        3,
        "Add pinned and archived columns to memories",
        [
            "ALTER TABLE memories ADD COLUMN pinned BOOLEAN DEFAULT 0;",
            "ALTER TABLE memories ADD COLUMN archived BOOLEAN DEFAULT 0;",
        ],
    ),
    (
        4,
        "Add file_path_relative column to memories",
        [
            "ALTER TABLE memories ADD COLUMN file_path_relative TEXT;",
            "CREATE INDEX IF NOT EXISTS idx_memories_file_path_relative ON memories(file_path_relative);",
        ],
    ),
    (
        5,
        "Track last_modified for index freshness",
        [
            """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """,
            "INSERT OR IGNORE INTO meta(key, value) VALUES('memories_last_modified', CURRENT_TIMESTAMP);",
            "INSERT OR IGNORE INTO meta(key, value) VALUES('rules_last_modified', CURRENT_TIMESTAMP);",
            """
        CREATE TRIGGER IF NOT EXISTS memories_touch_ins AFTER INSERT ON memories BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'memories_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_touch_upd AFTER UPDATE ON memories BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'memories_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS memories_touch_del AFTER DELETE ON memories BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'memories_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS rules_touch_ins AFTER INSERT ON rules BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'rules_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS rules_touch_upd AFTER UPDATE ON rules BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'rules_last_modified';
        END;
        """,
            """
        CREATE TRIGGER IF NOT EXISTS rules_touch_del AFTER DELETE ON rules BEGIN
            UPDATE meta SET value = CURRENT_TIMESTAMP WHERE key = 'rules_last_modified';
        END;
        """,
        ],
    ),
    (
        6,
        "Add recall_count column for saliency-based pruning",
        ["ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0;"],
    ),
    (
        7,
        "Add memory_relationships table for graph edges",
        [
            """
        CREATE TABLE IF NOT EXISTS memory_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            relationship TEXT NOT NULL,
            description TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (source_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_id) REFERENCES memories(id) ON DELETE CASCADE
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_relationships_source ON memory_relationships(source_id);",
            "CREATE INDEX IF NOT EXISTS idx_relationships_target ON memory_relationships(target_id);",
            "CREATE INDEX IF NOT EXISTS idx_relationships_type ON memory_relationships(relationship);",
        ],
    ),
    (
        8,
        "Add session_state and enforcement_bypass_log tables",
        [
            """
        CREATE TABLE IF NOT EXISTS session_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            project_path TEXT NOT NULL,
            briefed INTEGER DEFAULT 0,
            context_checks TEXT DEFAULT '[]',
            pending_decisions TEXT DEFAULT '[]',
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_session_state_session_id ON session_state(session_id);",
            """
        CREATE TABLE IF NOT EXISTS enforcement_bypass_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            pending_decisions TEXT DEFAULT '[]',
            staged_files_with_warnings TEXT DEFAULT '[]',
            reason TEXT
        );
        """,
        ],
    ),
    (
        9,
        "Add code_entities and memory_code_refs tables for Phase 2",
        [
            """
        CREATE TABLE IF NOT EXISTS code_entities (
            id TEXT PRIMARY KEY,
            project_path TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            line_start INTEGER,
            line_end INTEGER,
            signature TEXT,
            docstring TEXT,
            calls TEXT DEFAULT '[]',
            called_by TEXT DEFAULT '[]',
            imports TEXT DEFAULT '[]',
            inherits TEXT DEFAULT '[]',
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_code_entities_project ON code_entities(project_path);",
            "CREATE INDEX IF NOT EXISTS idx_code_entities_file ON code_entities(file_path);",
            "CREATE INDEX IF NOT EXISTS idx_code_entities_name ON code_entities(name);",
            "CREATE INDEX IF NOT EXISTS idx_code_entities_type ON code_entities(entity_type);",
            """
        CREATE TABLE IF NOT EXISTS memory_code_refs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER,
            code_entity_id TEXT,
            entity_type TEXT,
            entity_name TEXT,
            file_path TEXT,
            line_number INTEGER,
            relationship TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_memory_code_refs_memory ON memory_code_refs(memory_id);",
            "CREATE INDEX IF NOT EXISTS idx_memory_code_refs_entity ON memory_code_refs(code_entity_id);",
        ],
    ),
    (
        10,
        "Add project_links table for cross-repo awareness",
        [
            """
        CREATE TABLE IF NOT EXISTS project_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path TEXT NOT NULL,
            linked_path TEXT NOT NULL,
            relationship TEXT DEFAULT 'related',
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_project_links_source ON project_links(source_path);",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_links_unique ON project_links(source_path, linked_path);",
        ],
    ),
    (
        11,
        "Add file_hashes table for incremental indexing",
        [
            """
        CREATE TABLE IF NOT EXISTS file_hashes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_path, file_path)
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_file_hashes_project ON file_hashes(project_path);",
        ],
    ),
    (
        12,
        "Add surprise_score and importance_score columns to memories",
        [
            "ALTER TABLE memories ADD COLUMN surprise_score REAL;",
            "ALTER TABLE memories ADD COLUMN importance_score REAL;",
        ],
    ),
    (
        13,
        "Add facts table for static knowledge (Engram-inspired)",
        [
            """
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            category TEXT,
            source_memory_id INTEGER,
            verification_count INTEGER DEFAULT 0,
            is_verified BOOLEAN DEFAULT 0,
            tags TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verified_at TIMESTAMP,
            FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE SET NULL
        );
        """,
            "CREATE INDEX IF NOT EXISTS idx_facts_content_hash ON facts(content_hash);",
            "CREATE INDEX IF NOT EXISTS idx_facts_category ON facts(category);",
        ],
    ),
    (
        14,
        "Add bi-temporal columns to memory_versions",
        [
            # Add valid_from column (when fact became true in reality)
            "ALTER TABLE memory_versions ADD COLUMN valid_from TIMESTAMP;",
            # Add valid_to column (when fact was superseded, NULL = still valid)
            "ALTER TABLE memory_versions ADD COLUMN valid_to TIMESTAMP;",
            # Add invalidated_by_version_id column (for contradiction tracking)
            "ALTER TABLE memory_versions ADD COLUMN invalidated_by_version_id INTEGER REFERENCES memory_versions(id);",
            # Backfill valid_from with changed_at for existing records (backwards compatible)
            "UPDATE memory_versions SET valid_from = changed_at WHERE valid_from IS NULL;",
            # Create temporal index for point-in-time queries (valid time dimension)
            "CREATE INDEX IF NOT EXISTS idx_memory_versions_temporal ON memory_versions(memory_id, valid_from);",
            # Create index for transaction time queries
            "CREATE INDEX IF NOT EXISTS idx_memory_versions_transaction ON memory_versions(memory_id, changed_at);",
        ],
    ),
    (
        15,
        "Add source_client and source_model columns to memories for LLM compatibility tracking",
        [
            "ALTER TABLE memories ADD COLUMN source_client TEXT;",
            "ALTER TABLE memories ADD COLUMN source_model TEXT;",
            "CREATE INDEX IF NOT EXISTS idx_memories_source_client ON memories(source_client);",
        ],
    ),
    (
        16,
        "Add append-only v7 memory event and typed projection tables",
        [
            """
            CREATE TABLE IF NOT EXISTS memory_events (
                event_id TEXT PRIMARY KEY
                    CONSTRAINT ck_memory_events_event_id CHECK(
                        length(event_id)=68 AND substr(event_id,1,4)='evt_'
                        AND substr(event_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_memory_events_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                stream_id TEXT NOT NULL,
                stream_kind TEXT NOT NULL
                    CONSTRAINT ck_memory_events_stream_kind CHECK(stream_kind IN ('memory','fact','relationship')),
                stream_version INTEGER NOT NULL
                    CONSTRAINT ck_memory_events_stream_version CHECK(stream_version >= 1),
                event_type TEXT NOT NULL
                    CONSTRAINT ck_memory_events_event_type CHECK(length(event_type) BETWEEN 3 AND 80),
                event_schema_version INTEGER NOT NULL DEFAULT 1
                    CONSTRAINT ck_memory_events_schema_version CHECK(event_schema_version >= 1),
                occurred_at_us INTEGER NOT NULL,
                recorded_at_us INTEGER NOT NULL,
                actor_type TEXT NOT NULL
                    CONSTRAINT ck_memory_events_actor_type CHECK(actor_type IN ('user','client','system','migration','import')),
                actor_id TEXT,
                causation_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                correlation_id TEXT,
                payload_json TEXT NOT NULL
                    CONSTRAINT ck_memory_events_payload_json CHECK(json_valid(payload_json) AND json_type(payload_json)='object'),
                payload_hash TEXT NOT NULL
                    CONSTRAINT ck_memory_events_payload_hash CHECK(length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
                previous_event_hash TEXT
                    CONSTRAINT ck_memory_events_previous_hash CHECK(previous_event_hash IS NULL OR (length(previous_event_hash)=64 AND previous_event_hash NOT GLOB '*[^0-9a-f]*')),
                event_hash TEXT NOT NULL UNIQUE
                    CONSTRAINT ck_memory_events_event_hash CHECK(length(event_hash)=64 AND event_hash NOT GLOB '*[^0-9a-f]*'),
                CONSTRAINT uq_memory_events_stream_version UNIQUE(workspace_id, stream_id, stream_version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_memory_events_stream ON memory_events(workspace_id, stream_id, stream_version)",
            "CREATE INDEX IF NOT EXISTS idx_memory_events_recorded ON memory_events(workspace_id, recorded_at_us, event_id)",
            "CREATE INDEX IF NOT EXISTS idx_memory_events_type ON memory_events(workspace_id, event_type, recorded_at_us)",
            """
            CREATE TRIGGER IF NOT EXISTS memory_events_no_update BEFORE UPDATE ON memory_events
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_EVENT'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_events_no_delete BEFORE DELETE ON memory_events
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_EVENT'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_events_no_replace
            BEFORE INSERT ON memory_events
            WHEN EXISTS (
                SELECT 1 FROM memory_events
                WHERE rowid=NEW.rowid
                   OR event_id=NEW.event_id
                   OR event_hash=NEW.event_hash
                   OR (
                       workspace_id=NEW.workspace_id
                       AND stream_id=NEW.stream_id
                       AND stream_version=NEW.stream_version
                   )
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_EVENT'); END
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_records (
                record_id TEXT PRIMARY KEY
                    CONSTRAINT ck_memory_records_id CHECK(length(record_id)=68 AND substr(record_id,1,4)='mem_' AND substr(record_id,5) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_memory_records_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                record_type TEXT NOT NULL
                    CONSTRAINT ck_memory_records_type CHECK(record_type IN ('decision','pattern','warning','learning','procedure','observation','legacy')),
                legacy_type TEXT,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL
                    CONSTRAINT ck_memory_records_content_hash CHECK(length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
                rationale TEXT,
                context_json TEXT NOT NULL DEFAULT '{}'
                    CONSTRAINT ck_memory_records_context_json CHECK(json_valid(context_json) AND json_type(context_json)='object'),
                tags_json TEXT NOT NULL DEFAULT '[]'
                    CONSTRAINT ck_memory_records_tags_json CHECK(json_valid(tags_json) AND json_type(tags_json)='array'),
                file_path TEXT,
                file_path_relative TEXT,
                keywords TEXT,
                is_permanent INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_memory_records_permanent CHECK(is_permanent IN (0,1)),
                pinned INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_memory_records_pinned CHECK(pinned IN (0,1)),
                archived INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_memory_records_archived CHECK(archived IN (0,1)),
                outcome TEXT,
                worked INTEGER CONSTRAINT ck_memory_records_worked CHECK(worked IS NULL OR worked IN (0,1)),
                recall_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_memory_records_recall_count CHECK(recall_count >= 0),
                surprise_score REAL CONSTRAINT ck_memory_records_surprise CHECK(surprise_score IS NULL OR surprise_score BETWEEN 0.0 AND 1.0),
                importance_score REAL CONSTRAINT ck_memory_records_importance CHECK(importance_score IS NULL OR importance_score BETWEEN 0.0 AND 1.0),
                source_client TEXT,
                source_model TEXT,
                stream_version INTEGER NOT NULL CONSTRAINT ck_memory_records_stream_version CHECK(stream_version >= 1),
                source_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                deleted_at_us INTEGER,
                state_hash TEXT NOT NULL
                    CONSTRAINT ck_memory_records_state_hash CHECK(length(state_hash)=64 AND state_hash NOT GLOB '*[^0-9a-f]*'),
                CONSTRAINT ck_memory_records_legacy_type CHECK((record_type='legacy' AND legacy_type IS NOT NULL) OR (record_type<>'legacy' AND legacy_type IS NULL)),
                CONSTRAINT uq_memory_records_workspace_id UNIQUE(workspace_id, record_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_memory_records_type ON memory_records(workspace_id, record_type, archived, deleted_at_us)",
            "CREATE INDEX IF NOT EXISTS idx_memory_records_content_hash ON memory_records(workspace_id, content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_memory_records_source_event ON memory_records(source_event_id)",
            """
            CREATE TABLE IF NOT EXISTS memory_fact_versions (
                fact_version_id TEXT PRIMARY KEY CONSTRAINT ck_fact_versions_id CHECK(length(fact_version_id)=69 AND substr(fact_version_id,1,5)='fact_' AND substr(fact_version_id,6) NOT GLOB '*[^0-9a-f]*'),
                fact_id TEXT NOT NULL CONSTRAINT ck_fact_versions_fact_id CHECK(length(fact_id)=69 AND substr(fact_id,1,5)='fact_' AND substr(fact_id,6) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL CONSTRAINT ck_fact_versions_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                version INTEGER NOT NULL CONSTRAINT ck_fact_versions_version CHECK(version >= 1),
                subject_record_id TEXT REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                predicate TEXT NOT NULL CONSTRAINT ck_fact_versions_predicate CHECK(length(predicate) BETWEEN 1 AND 120),
                object_kind TEXT NOT NULL CONSTRAINT ck_fact_versions_object_kind CHECK(object_kind IN ('text','number','boolean','json','record_ref','legacy')),
                object_json TEXT NOT NULL CONSTRAINT ck_fact_versions_object_json CHECK(json_valid(object_json)),
                legacy_type TEXT,
                content_hash TEXT NOT NULL CONSTRAINT ck_fact_versions_content_hash CHECK(length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
                confidence REAL NOT NULL DEFAULT 1.0 CONSTRAINT ck_fact_versions_confidence CHECK(confidence BETWEEN 0.0 AND 1.0),
                verification_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_fact_versions_verification_count CHECK(verification_count >= 0),
                is_verified INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_fact_versions_verified CHECK(is_verified IN (0,1)),
                evidence_json TEXT NOT NULL DEFAULT '[]' CONSTRAINT ck_fact_versions_evidence CHECK(json_valid(evidence_json) AND json_type(evidence_json)='array'),
                metadata_json TEXT NOT NULL DEFAULT '{}' CONSTRAINT ck_fact_versions_metadata CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'),
                valid_from_us INTEGER NOT NULL,
                valid_to_us INTEGER,
                transaction_from_us INTEGER NOT NULL,
                transaction_to_us INTEGER,
                asserted_by_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                retracted_by_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                CONSTRAINT uq_fact_versions_fact_version UNIQUE(fact_id, version),
                CONSTRAINT ck_fact_versions_legacy_type CHECK((object_kind='legacy' AND legacy_type IS NOT NULL) OR (object_kind<>'legacy' AND legacy_type IS NULL)),
                CONSTRAINT ck_fact_versions_valid_interval CHECK(valid_to_us IS NULL OR valid_to_us > valid_from_us),
                CONSTRAINT ck_fact_versions_transaction_interval CHECK(transaction_to_us IS NULL OR transaction_to_us > transaction_from_us)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_fact_versions_valid ON memory_fact_versions(workspace_id, predicate, valid_from_us, valid_to_us)",
            "CREATE INDEX IF NOT EXISTS idx_fact_versions_transaction ON memory_fact_versions(workspace_id, transaction_from_us, transaction_to_us)",
            "CREATE INDEX IF NOT EXISTS idx_fact_versions_subject ON memory_fact_versions(subject_record_id, predicate)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_fact_versions_open_transaction ON memory_fact_versions(fact_id) WHERE transaction_to_us IS NULL",
            """
            CREATE TABLE IF NOT EXISTS memory_relationship_versions (
                relationship_version_id TEXT PRIMARY KEY CONSTRAINT ck_relationship_versions_id CHECK(length(relationship_version_id)=68 AND substr(relationship_version_id,1,4)='rel_' AND substr(relationship_version_id,5) NOT GLOB '*[^0-9a-f]*'),
                relationship_id TEXT NOT NULL CONSTRAINT ck_relationship_versions_relationship_id CHECK(length(relationship_id)=68 AND substr(relationship_id,1,4)='rel_' AND substr(relationship_id,5) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL CONSTRAINT ck_relationship_versions_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                version INTEGER NOT NULL CONSTRAINT ck_relationship_versions_version CHECK(version >= 1),
                source_record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                target_record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                relationship_type TEXT NOT NULL CONSTRAINT ck_relationship_versions_type CHECK(relationship_type IN ('led_to','supersedes','depends_on','conflicts_with','related_to','evidence_for','derived_from','invalidates','legacy')),
                legacy_type TEXT,
                description TEXT,
                confidence REAL NOT NULL DEFAULT 1.0 CONSTRAINT ck_relationship_versions_confidence CHECK(confidence BETWEEN 0.0 AND 1.0),
                metadata_json TEXT NOT NULL DEFAULT '{}' CONSTRAINT ck_relationship_versions_metadata CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'),
                content_hash TEXT NOT NULL CONSTRAINT ck_relationship_versions_content_hash CHECK(length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
                valid_from_us INTEGER NOT NULL,
                valid_to_us INTEGER,
                transaction_from_us INTEGER NOT NULL,
                transaction_to_us INTEGER,
                asserted_by_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                retracted_by_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                CONSTRAINT uq_relationship_versions_relationship_version UNIQUE(relationship_id, version),
                CONSTRAINT ck_relationship_versions_legacy_type CHECK((relationship_type='legacy' AND legacy_type IS NOT NULL) OR (relationship_type<>'legacy' AND legacy_type IS NULL)),
                CONSTRAINT ck_relationship_versions_valid_interval CHECK(valid_to_us IS NULL OR valid_to_us > valid_from_us),
                CONSTRAINT ck_relationship_versions_transaction_interval CHECK(transaction_to_us IS NULL OR transaction_to_us > transaction_from_us)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_relationship_versions_source ON memory_relationship_versions(workspace_id, source_record_id, relationship_type, valid_to_us)",
            "CREATE INDEX IF NOT EXISTS idx_relationship_versions_target ON memory_relationship_versions(workspace_id, target_record_id, relationship_type, valid_to_us)",
            "CREATE INDEX IF NOT EXISTS idx_relationship_versions_valid ON memory_relationship_versions(workspace_id, valid_from_us, valid_to_us)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_relationship_versions_open_transaction ON memory_relationship_versions(relationship_id) WHERE transaction_to_us IS NULL",
            """
            CREATE TABLE IF NOT EXISTS projection_manifests (
                manifest_id TEXT PRIMARY KEY CONSTRAINT ck_projection_manifests_id CHECK(length(manifest_id)=68 AND substr(manifest_id,1,4)='prj_' AND substr(manifest_id,5) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL CONSTRAINT ck_projection_manifests_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                projection_name TEXT NOT NULL,
                generation INTEGER NOT NULL CONSTRAINT ck_projection_manifests_generation CHECK(generation >= 1),
                projection_version INTEGER NOT NULL CONSTRAINT ck_projection_manifests_version CHECK(projection_version >= 1),
                status TEXT NOT NULL CONSTRAINT ck_projection_manifests_status CHECK(status IN ('building','ready','active','rebuild_required','failed')),
                source_event_count INTEGER NOT NULL CONSTRAINT ck_projection_manifests_event_count CHECK(source_event_count >= 0),
                source_event_root_hash TEXT NOT NULL CONSTRAINT ck_projection_manifests_root_hash CHECK(length(source_event_root_hash)=64 AND source_event_root_hash NOT GLOB '*[^0-9a-f]*'),
                cursor_recorded_at_us INTEGER,
                cursor_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                row_count INTEGER NOT NULL CONSTRAINT ck_projection_manifests_row_count CHECK(row_count >= 0),
                builder_version TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}' CONSTRAINT ck_projection_manifests_details CHECK(json_valid(details_json) AND json_type(details_json)='object'),
                started_at_us INTEGER NOT NULL,
                completed_at_us INTEGER,
                activated_at_us INTEGER,
                CONSTRAINT uq_projection_manifests_generation UNIQUE(workspace_id, projection_name, generation)
            )
            """,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_projection_active ON projection_manifests(workspace_id, projection_name) WHERE status='active'",
            "CREATE INDEX IF NOT EXISTS idx_projection_status ON projection_manifests(workspace_id, status, projection_name)",
            """
            CREATE TABLE IF NOT EXISTS enrichment_decisions (
                decision_id TEXT PRIMARY KEY CONSTRAINT ck_enrichment_decisions_id CHECK(length(decision_id)=68 AND substr(decision_id,1,4)='enr_' AND substr(decision_id,5) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL CONSTRAINT ck_enrichment_decisions_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                decision_kind TEXT NOT NULL CONSTRAINT ck_enrichment_decisions_kind CHECK(decision_kind IN ('promote','reject','supersede','rollback')),
                status TEXT NOT NULL CONSTRAINT ck_enrichment_decisions_status CHECK(status IN ('proposed','accepted','rejected','superseded','rolled_back')),
                candidate_hash TEXT NOT NULL CONSTRAINT ck_enrichment_decisions_candidate_hash CHECK(length(candidate_hash)=64 AND candidate_hash NOT GLOB '*[^0-9a-f]*'),
                target_record_id TEXT REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                proposed_by_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                inverse_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                policy_version TEXT NOT NULL,
                confidence REAL NOT NULL CONSTRAINT ck_enrichment_decisions_confidence CHECK(confidence BETWEEN 0.0 AND 1.0),
                evidence_json TEXT NOT NULL DEFAULT '[]' CONSTRAINT ck_enrichment_decisions_evidence CHECK(json_valid(evidence_json) AND json_type(evidence_json)='array'),
                has_unresolved_contradiction INTEGER NOT NULL CONSTRAINT ck_enrichment_decisions_contradiction CHECK(has_unresolved_contradiction IN (0,1)),
                is_security_sensitive INTEGER NOT NULL CONSTRAINT ck_enrichment_decisions_security CHECK(is_security_sensitive IN (0,1)),
                has_deterministic_source INTEGER NOT NULL CONSTRAINT ck_enrichment_decisions_source CHECK(has_deterministic_source IN (0,1)),
                independent_source_count INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_enrichment_decisions_source_count CHECK(independent_source_count >= 0),
                reason TEXT,
                created_at_us INTEGER NOT NULL,
                decided_at_us INTEGER,
                CONSTRAINT uq_enrichment_decisions_candidate UNIQUE(workspace_id, decision_kind, candidate_hash, policy_version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_enrichment_status ON enrichment_decisions(workspace_id, status, created_at_us)",
            "CREATE INDEX IF NOT EXISTS idx_enrichment_target ON enrichment_decisions(target_record_id, status)",
            """
            CREATE TABLE IF NOT EXISTS background_jobs (
                job_id TEXT PRIMARY KEY CONSTRAINT ck_background_jobs_id CHECK(length(job_id)=68 AND substr(job_id,1,4)='job_' AND substr(job_id,5) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL CONSTRAINT ck_background_jobs_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                job_type TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_json TEXT NOT NULL CONSTRAINT ck_background_jobs_payload CHECK(json_valid(payload_json)),
                payload_hash TEXT NOT NULL CONSTRAINT ck_background_jobs_payload_hash CHECK(length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
                status TEXT NOT NULL CONSTRAINT ck_background_jobs_status CHECK(status IN ('queued','running','succeeded','failed','cancelled','dead_letter')),
                priority INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_background_jobs_attempts CHECK(attempts >= 0),
                max_attempts INTEGER NOT NULL DEFAULT 3 CONSTRAINT ck_background_jobs_max_attempts CHECK(max_attempts >= 1),
                available_at_us INTEGER NOT NULL,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at_us INTEGER,
                cancel_requested_at_us INTEGER,
                last_error_json TEXT CONSTRAINT ck_background_jobs_last_error CHECK(last_error_json IS NULL OR json_valid(last_error_json)),
                result_json TEXT CONSTRAINT ck_background_jobs_result CHECK(result_json IS NULL OR json_valid(result_json)),
                source_event_id TEXT REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                started_at_us INTEGER,
                finished_at_us INTEGER,
                CONSTRAINT uq_background_jobs_idempotency UNIQUE(workspace_id, job_type, idempotency_key),
                CONSTRAINT ck_background_jobs_running_lease CHECK(
                    (status='running' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at_us IS NOT NULL)
                    OR (status<>'running' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at_us IS NULL)
                )
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_background_jobs_claim ON background_jobs(status, available_at_us, priority DESC, created_at_us)",
            "CREATE INDEX IF NOT EXISTS idx_background_jobs_lease ON background_jobs(status, lease_expires_at_us)",
            """
            CREATE TABLE IF NOT EXISTS v7_migration_runs (
                migration_run_id TEXT PRIMARY KEY CONSTRAINT ck_v7_migration_runs_id CHECK(length(migration_run_id)=68 AND substr(migration_run_id,1,4)='mig_' AND substr(migration_run_id,5) NOT GLOB '*[^0-9a-f]*'),
                workspace_id TEXT NOT NULL CONSTRAINT ck_v7_migration_runs_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                source_db_sha256 TEXT NOT NULL CONSTRAINT ck_v7_migration_runs_source_hash CHECK(length(source_db_sha256)=64 AND source_db_sha256 NOT GLOB '*[^0-9a-f]*'),
                source_schema_version INTEGER NOT NULL,
                source_format_version INTEGER NOT NULL,
                target_format_version INTEGER NOT NULL DEFAULT 7 CONSTRAINT ck_v7_migration_runs_target_format CHECK(target_format_version=7),
                status TEXT NOT NULL CONSTRAINT ck_v7_migration_runs_status CHECK(status IN ('snapshotted','importing','validating','ready','active','failed','rolled_back')),
                snapshot_name TEXT NOT NULL,
                candidate_name TEXT NOT NULL,
                source_inventory_json TEXT NOT NULL CONSTRAINT ck_v7_migration_runs_inventory CHECK(json_valid(source_inventory_json) AND json_type(source_inventory_json)='object'),
                validation_json TEXT CONSTRAINT ck_v7_migration_runs_validation CHECK(validation_json IS NULL OR json_valid(validation_json)),
                last_error_json TEXT CONSTRAINT ck_v7_migration_runs_error CHECK(last_error_json IS NULL OR json_valid(last_error_json)),
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                validated_at_us INTEGER,
                activated_at_us INTEGER,
                rolled_back_at_us INTEGER,
                CONSTRAINT uq_v7_migration_runs_source UNIQUE(workspace_id, source_db_sha256, target_format_version)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_v7_migration_runs_status ON v7_migration_runs(workspace_id, status, updated_at_us)",
            """
            CREATE TABLE IF NOT EXISTS v7_migration_checkpoints (
                migration_run_id TEXT NOT NULL REFERENCES v7_migration_runs(migration_run_id) ON DELETE RESTRICT,
                source_table TEXT NOT NULL,
                last_legacy_pk TEXT,
                rows_imported INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_v7_checkpoints_rows CHECK(rows_imported >= 0),
                rolling_hash TEXT NOT NULL CONSTRAINT ck_v7_checkpoints_hash CHECK(length(rolling_hash)=64 AND rolling_hash NOT GLOB '*[^0-9a-f]*'),
                completed INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_v7_checkpoints_completed CHECK(completed IN (0,1)),
                updated_at_us INTEGER NOT NULL,
                CONSTRAINT pk_v7_migration_checkpoints PRIMARY KEY(migration_run_id, source_table)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS legacy_id_map (
                migration_run_id TEXT NOT NULL REFERENCES v7_migration_runs(migration_run_id) ON DELETE RESTRICT,
                source_table TEXT NOT NULL,
                legacy_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL CONSTRAINT ck_legacy_id_map_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                target_kind TEXT NOT NULL CONSTRAINT ck_legacy_id_map_kind CHECK(target_kind IN ('memory','fact','relationship','placeholder')),
                target_id TEXT NOT NULL,
                source_row_hash TEXT NOT NULL CONSTRAINT ck_legacy_id_map_source_hash CHECK(length(source_row_hash)=64 AND source_row_hash NOT GLOB '*[^0-9a-f]*'),
                imported_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                CONSTRAINT pk_legacy_id_map PRIMARY KEY(migration_run_id, source_table, legacy_id),
                CONSTRAINT uq_legacy_id_map_target UNIQUE(migration_run_id, target_kind, target_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legacy_id_map_source ON legacy_id_map(workspace_id, source_table, legacy_id)",
        ],
    ),
    (
        17,
        "Harden immutable memory events against SQLite rowid replacement",
        [
            "DROP TRIGGER IF EXISTS memory_events_no_replace",
            """
            CREATE TRIGGER memory_events_no_replace
            BEFORE INSERT ON memory_events
            WHEN EXISTS (
                SELECT 1 FROM memory_events
                WHERE rowid=NEW.rowid
                   OR event_id=NEW.event_id
                   OR event_hash=NEW.event_hash
                   OR (
                       workspace_id=NEW.workspace_id
                       AND stream_id=NEW.stream_id
                       AND stream_version=NEW.stream_version
                   )
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_EVENT'); END
            """,
        ],
    ),
    (
        18,
        "Add rebuildable v7 retrieval projection tables",
        [
            """
            CREATE TABLE IF NOT EXISTS retrieval_documents (
                document_rowid INTEGER PRIMARY KEY,
                workspace_id TEXT NOT NULL CONSTRAINT ck_retrieval_documents_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                projection_generation INTEGER NOT NULL CONSTRAINT ck_retrieval_documents_generation CHECK(projection_generation >= 1),
                record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                content TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                tags_text TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                valid_from_us INTEGER,
                valid_to_us INTEGER,
                transaction_from_us INTEGER NOT NULL,
                transaction_to_us INTEGER,
                visibility TEXT NOT NULL DEFAULT 'workspace' CONSTRAINT ck_retrieval_documents_visibility CHECK(visibility IN ('workspace','private','shared')),
                archived INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_retrieval_documents_archived CHECK(archived IN (0,1)),
                content_hash TEXT NOT NULL CONSTRAINT ck_retrieval_documents_content_hash CHECK(length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
                source_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                CONSTRAINT ck_retrieval_documents_valid_interval CHECK(valid_to_us IS NULL OR valid_from_us IS NULL OR valid_to_us > valid_from_us),
                CONSTRAINT ck_retrieval_documents_transaction_interval CHECK(transaction_to_us IS NULL OR transaction_to_us > transaction_from_us),
                CONSTRAINT uq_retrieval_documents_record UNIQUE(workspace_id, projection_generation, record_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_retrieval_documents_generation ON retrieval_documents(workspace_id, projection_generation, archived, category)",
            "CREATE INDEX IF NOT EXISTS idx_retrieval_documents_record ON retrieval_documents(workspace_id, record_id, projection_generation)",
            """
            CREATE TABLE IF NOT EXISTS record_procedures (
                workspace_id TEXT NOT NULL CONSTRAINT ck_record_procedures_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                projection_generation INTEGER NOT NULL CONSTRAINT ck_record_procedures_generation CHECK(projection_generation >= 1),
                record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL CONSTRAINT ck_record_procedures_ordinal CHECK(ordinal >= 0),
                step_text TEXT NOT NULL,
                step_hash TEXT NOT NULL CONSTRAINT ck_record_procedures_step_hash CHECK(length(step_hash)=64 AND step_hash NOT GLOB '*[^0-9a-f]*'),
                source_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                CONSTRAINT pk_record_procedures PRIMARY KEY(workspace_id, projection_generation, record_id, ordinal)
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_record_procedures_record ON record_procedures(workspace_id, record_id, projection_generation)",
            """
            CREATE TABLE IF NOT EXISTS record_outcome_view (
                workspace_id TEXT NOT NULL CONSTRAINT ck_record_outcome_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                projection_generation INTEGER NOT NULL CONSTRAINT ck_record_outcome_generation CHECK(projection_generation >= 1),
                record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                worked INTEGER CONSTRAINT ck_record_outcome_worked CHECK(worked IS NULL OR worked IN (0,1)),
                outcome_text TEXT,
                outcome_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                transaction_at_us INTEGER NOT NULL,
                CONSTRAINT pk_record_outcome PRIMARY KEY(workspace_id, projection_generation, record_id)
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_record_outcome_worked ON record_outcome_view(workspace_id, projection_generation, worked, transaction_at_us)",
            """
            CREATE TABLE IF NOT EXISTS dense_projection_refs (
                workspace_id TEXT NOT NULL CONSTRAINT ck_dense_refs_workspace CHECK(substr(workspace_id,1,3)='ws_'),
                provider_key TEXT NOT NULL,
                projection_generation INTEGER NOT NULL CONSTRAINT ck_dense_refs_generation CHECK(projection_generation >= 1),
                record_id TEXT NOT NULL REFERENCES memory_records(record_id) ON DELETE RESTRICT,
                content_hash TEXT NOT NULL CONSTRAINT ck_dense_refs_content_hash CHECK(length(content_hash)=64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
                model_id TEXT NOT NULL,
                dimension INTEGER NOT NULL CONSTRAINT ck_dense_refs_dimension CHECK(dimension > 0),
                state TEXT NOT NULL CONSTRAINT ck_dense_refs_state CHECK(state IN ('pending','ready','failed','deleted')),
                updated_event_id TEXT NOT NULL REFERENCES memory_events(event_id) ON DELETE RESTRICT,
                failure_code TEXT CONSTRAINT ck_dense_refs_failure CHECK(failure_code IS NULL OR (length(failure_code) BETWEEN 1 AND 80 AND failure_code NOT GLOB '*[^A-Z0-9_]*')),
                updated_at_us INTEGER NOT NULL,
                CONSTRAINT pk_dense_projection_refs PRIMARY KEY(workspace_id, provider_key, projection_generation, record_id)
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_dense_refs_state ON dense_projection_refs(workspace_id, provider_key, projection_generation, state)",
        ],
    ),
    (
        19,
        "Add immutable workspace-scoped public object identifiers",
        [
            """
            CREATE TABLE IF NOT EXISTS public_object_ids (
                workspace_id TEXT NOT NULL CONSTRAINT ck_public_object_ids_workspace CHECK(
                    length(workspace_id)=27
                    AND substr(workspace_id,1,3)='ws_'
                    AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                ),
                object_kind TEXT NOT NULL CONSTRAINT ck_public_object_ids_kind CHECK(
                    object_kind IN ('rule','trigger','entity','active_context','community','code')
                ),
                source_key TEXT NOT NULL CONSTRAINT ck_public_object_ids_source CHECK(
                    (
                        substr(source_key,1,2)='i:'
                        AND length(source_key) BETWEEN 3 AND 21
                        AND substr(source_key,3) NOT GLOB '*[^0-9]*'
                        AND CAST(substr(source_key,3) AS INTEGER) BETWEEN 1 AND 9223372036854775807
                        AND CAST(CAST(substr(source_key,3) AS INTEGER) AS TEXT)=substr(source_key,3)
                    ) OR (
                        substr(source_key,1,2)='s:'
                        AND length(source_key) BETWEEN 3 AND 514
                    )
                ),
                projection_generation INTEGER NOT NULL CONSTRAINT ck_public_object_ids_generation CHECK(
                    (
                        object_kind IN ('rule','trigger','entity','active_context')
                        AND projection_generation=0
                    ) OR (
                        object_kind IN ('community','code')
                        AND projection_generation BETWEEN 1 AND 9223372036854775807
                    )
                ),
                public_id TEXT NOT NULL CONSTRAINT ck_public_object_ids_public_id CHECK(
                    (
                        object_kind='rule'
                        AND length(public_id)=69
                        AND substr(public_id,1,5)='rule_'
                        AND substr(public_id,6) NOT GLOB '*[^0-9a-f]*'
                    ) OR (
                        object_kind='trigger'
                        AND length(public_id)=68
                        AND substr(public_id,1,4)='trg_'
                        AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*'
                    ) OR (
                        object_kind='entity'
                        AND length(public_id)=68
                        AND substr(public_id,1,4)='ent_'
                        AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*'
                    ) OR (
                        object_kind='active_context'
                        AND length(public_id)=68
                        AND substr(public_id,1,4)='act_'
                        AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*'
                    ) OR (
                        object_kind='community'
                        AND length(public_id)=68
                        AND substr(public_id,1,4)='com_'
                        AND substr(public_id,5) NOT GLOB '*[^0-9a-f]*'
                    ) OR (
                        object_kind='code'
                        AND length(public_id)=69
                        AND substr(public_id,1,5)='code_'
                        AND substr(public_id,6) NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                created_at_us INTEGER NOT NULL CONSTRAINT ck_public_object_ids_created CHECK(
                    created_at_us BETWEEN 0 AND 9223372036854775807
                ),
                CONSTRAINT pk_public_object_ids PRIMARY KEY(
                    workspace_id, object_kind, source_key, projection_generation
                ),
                CONSTRAINT uq_public_object_ids_public UNIQUE(public_id)
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_public_object_ids_reverse ON public_object_ids(workspace_id, object_kind, public_id, projection_generation)",
            """
            CREATE TRIGGER IF NOT EXISTS public_object_ids_no_replace
            BEFORE INSERT ON public_object_ids
            WHEN EXISTS (
                SELECT 1 FROM public_object_ids
                WHERE public_id=NEW.public_id
                   OR (
                       workspace_id=NEW.workspace_id
                       AND object_kind=NEW.object_kind
                       AND source_key=NEW.source_key
                       AND projection_generation=NEW.projection_generation
                   )
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_PUBLIC_OBJECT_ID'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS public_object_ids_no_update
            BEFORE UPDATE ON public_object_ids
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_PUBLIC_OBJECT_ID'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS public_object_ids_no_delete
            BEFORE DELETE ON public_object_ids
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_PUBLIC_OBJECT_ID'); END
            """,
        ],
    ),
    (
        20,
        "Add canonical workspace-scoped active context entries",
        [
            """
            CREATE TABLE IF NOT EXISTS active_context_entries (
                active_context_id TEXT PRIMARY KEY
                    CONSTRAINT ck_active_context_entries_id CHECK(
                        length(active_context_id)=68
                        AND substr(active_context_id,1,4)='act_'
                        AND substr(active_context_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_active_context_entries_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                record_id TEXT NOT NULL
                    CONSTRAINT ck_active_context_entries_record CHECK(
                        length(record_id)=68
                        AND substr(record_id,1,4)='mem_'
                        AND substr(record_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                priority INTEGER NOT NULL DEFAULT 0
                    CONSTRAINT ck_active_context_entries_priority CHECK(
                        typeof(priority)='integer' AND priority BETWEEN -100 AND 100
                    ),
                reason TEXT
                    CONSTRAINT ck_active_context_entries_reason CHECK(
                        reason IS NULL OR (
                            typeof(reason)='text' AND length(reason) BETWEEN 1 AND 2000
                        )
                    ),
                added_at_us INTEGER NOT NULL
                    CONSTRAINT ck_active_context_entries_added CHECK(
                        typeof(added_at_us)='integer'
                        AND added_at_us BETWEEN 0 AND 9223372036854775807
                    ),
                expires_at_us INTEGER
                    CONSTRAINT ck_active_context_entries_expires CHECK(
                        expires_at_us IS NULL OR (
                            typeof(expires_at_us)='integer'
                            AND expires_at_us BETWEEN 0 AND 9223372036854775807
                        )
                    ),
                removed_at_us INTEGER
                    CONSTRAINT ck_active_context_entries_removed CHECK(
                        removed_at_us IS NULL OR (
                            typeof(removed_at_us)='integer'
                            AND removed_at_us BETWEEN added_at_us AND 9223372036854775807
                        )
                    ),
                CONSTRAINT uq_active_context_entries_record
                    UNIQUE(workspace_id, record_id),
                CONSTRAINT fk_active_context_entries_record
                    FOREIGN KEY(workspace_id, record_id)
                    REFERENCES memory_records(workspace_id, record_id)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_active_context_entries_current
            ON active_context_entries(
                workspace_id, removed_at_us, priority DESC,
                added_at_us DESC, active_context_id
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_active_context_entries_expiry
            ON active_context_entries(workspace_id, expires_at_us)
            WHERE removed_at_us IS NULL AND expires_at_us IS NOT NULL
            """,
            """
            CREATE TRIGGER IF NOT EXISTS active_context_entries_no_replace
            BEFORE INSERT ON active_context_entries
            WHEN EXISTS (
                SELECT 1 FROM active_context_entries
                WHERE active_context_id=NEW.active_context_id
                   OR (
                       workspace_id=NEW.workspace_id
                       AND record_id=NEW.record_id
                   )
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_ACTIVE_CONTEXT_IDENTITY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS active_context_entries_no_identity_update
            BEFORE UPDATE OF active_context_id, workspace_id, record_id
            ON active_context_entries
            WHEN OLD.active_context_id<>NEW.active_context_id
              OR OLD.workspace_id<>NEW.workspace_id
              OR OLD.record_id<>NEW.record_id
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_ACTIVE_CONTEXT_IDENTITY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS active_context_entries_no_delete
            BEFORE DELETE ON active_context_entries
            BEGIN SELECT RAISE(ABORT, 'SOFT_REMOVE_ACTIVE_CONTEXT'); END
            """,
        ],
    ),
    (
        21,
        "Add append-only governance events and rule/trigger projections",
        [
            """
            CREATE TABLE IF NOT EXISTS governance_events (
                event_id TEXT PRIMARY KEY
                    CONSTRAINT ck_governance_events_id CHECK(
                        length(event_id)=68
                        AND substr(event_id,1,4)='evt_'
                        AND substr(event_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_governance_events_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                stream_id TEXT NOT NULL,
                stream_kind TEXT NOT NULL
                    CONSTRAINT ck_governance_events_stream_kind CHECK(
                        stream_kind IN ('rule','trigger','active_context')
                    ),
                stream_version INTEGER NOT NULL
                    CONSTRAINT ck_governance_events_stream_version CHECK(
                        typeof(stream_version)='integer' AND stream_version >= 1
                    ),
                event_type TEXT NOT NULL
                    CONSTRAINT ck_governance_events_event_type CHECK(
                        length(event_type) BETWEEN 3 AND 80
                    ),
                event_schema_version INTEGER NOT NULL DEFAULT 1
                    CONSTRAINT ck_governance_events_schema_version CHECK(
                        typeof(event_schema_version)='integer'
                        AND event_schema_version >= 1
                    ),
                occurred_at_us INTEGER NOT NULL
                    CONSTRAINT ck_governance_events_occurred CHECK(
                        typeof(occurred_at_us)='integer'
                        AND occurred_at_us BETWEEN 0 AND 9223372036854775807
                    ),
                recorded_at_us INTEGER NOT NULL
                    CONSTRAINT ck_governance_events_recorded CHECK(
                        typeof(recorded_at_us)='integer'
                        AND recorded_at_us BETWEEN 0 AND 9223372036854775807
                    ),
                actor_type TEXT NOT NULL
                    CONSTRAINT ck_governance_events_actor_type CHECK(
                        actor_type IN ('user','client','system','migration','import')
                    ),
                actor_id TEXT,
                causation_event_id TEXT
                    CONSTRAINT ck_governance_events_causation CHECK(
                        causation_event_id IS NULL OR (
                            length(causation_event_id)=68
                            AND substr(causation_event_id,1,4)='evt_'
                            AND substr(causation_event_id,5) NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                correlation_id TEXT
                    CONSTRAINT ck_governance_events_correlation CHECK(
                        correlation_id IS NULL
                        OR length(correlation_id) BETWEEN 1 AND 200
                    ),
                payload_json TEXT NOT NULL
                    CONSTRAINT ck_governance_events_payload_json CHECK(
                        json_valid(payload_json) AND json_type(payload_json)='object'
                    ),
                payload_hash TEXT NOT NULL
                    CONSTRAINT ck_governance_events_payload_hash CHECK(
                        length(payload_hash)=64
                        AND payload_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                previous_event_hash TEXT
                    CONSTRAINT ck_governance_events_previous_hash CHECK(
                        previous_event_hash IS NULL OR (
                            length(previous_event_hash)=64
                            AND previous_event_hash NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                event_hash TEXT NOT NULL UNIQUE
                    CONSTRAINT ck_governance_events_event_hash CHECK(
                        length(event_hash)=64
                        AND event_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT ck_governance_events_stream_id CHECK(
                    (stream_kind='rule' AND length(stream_id)=69
                        AND substr(stream_id,1,5)='rule_'
                        AND substr(stream_id,6) NOT GLOB '*[^0-9a-f]*')
                    OR (stream_kind='trigger' AND length(stream_id)=68
                        AND substr(stream_id,1,4)='trg_'
                        AND substr(stream_id,5) NOT GLOB '*[^0-9a-f]*')
                    OR (stream_kind='active_context' AND length(stream_id)=68
                        AND substr(stream_id,1,4)='act_'
                        AND substr(stream_id,5) NOT GLOB '*[^0-9a-f]*')
                ),
                CONSTRAINT uq_governance_events_stream_version
                    UNIQUE(workspace_id, stream_id, stream_version)
            ) WITHOUT ROWID
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governance_events_stream
            ON governance_events(workspace_id, stream_id, stream_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governance_events_recorded
            ON governance_events(workspace_id, recorded_at_us, event_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governance_events_type
            ON governance_events(workspace_id, event_type, recorded_at_us)
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_events_no_update
            BEFORE UPDATE ON governance_events
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_EVENT'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_events_no_delete
            BEFORE DELETE ON governance_events
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_EVENT'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_events_no_replace
            BEFORE INSERT ON governance_events
            WHEN EXISTS (
                SELECT 1 FROM governance_events
                WHERE event_id=NEW.event_id
                   OR event_hash=NEW.event_hash
                   OR (
                       workspace_id=NEW.workspace_id
                       AND stream_id=NEW.stream_id
                       AND stream_version=NEW.stream_version
                   )
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_EVENT'); END
            """,
            """
            CREATE TABLE IF NOT EXISTS governance_rules (
                rule_id TEXT PRIMARY KEY
                    CONSTRAINT ck_governance_rules_id CHECK(
                        length(rule_id)=69
                        AND substr(rule_id,1,5)='rule_'
                        AND substr(rule_id,6) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL,
                trigger TEXT NOT NULL
                    CONSTRAINT ck_governance_rules_trigger CHECK(
                        length(trigger) BETWEEN 1 AND 2000
                    ),
                must_do_json TEXT NOT NULL
                    CONSTRAINT ck_governance_rules_must_do CHECK(
                        json_valid(must_do_json) AND json_type(must_do_json)='array'
                    ),
                must_not_json TEXT NOT NULL
                    CONSTRAINT ck_governance_rules_must_not CHECK(
                        json_valid(must_not_json) AND json_type(must_not_json)='array'
                    ),
                ask_first_json TEXT NOT NULL
                    CONSTRAINT ck_governance_rules_ask_first CHECK(
                        json_valid(ask_first_json) AND json_type(ask_first_json)='array'
                    ),
                warnings_json TEXT NOT NULL
                    CONSTRAINT ck_governance_rules_warnings CHECK(
                        json_valid(warnings_json) AND json_type(warnings_json)='array'
                    ),
                priority INTEGER NOT NULL
                    CONSTRAINT ck_governance_rules_priority CHECK(
                        typeof(priority)='integer' AND priority BETWEEN -1000 AND 1000
                    ),
                enabled INTEGER NOT NULL
                    CONSTRAINT ck_governance_rules_enabled CHECK(enabled IN (0,1)),
                stream_version INTEGER NOT NULL
                    CONSTRAINT ck_governance_rules_version CHECK(stream_version >= 1),
                source_event_id TEXT NOT NULL
                    REFERENCES governance_events(event_id) ON DELETE RESTRICT,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                state_hash TEXT NOT NULL
                    CONSTRAINT ck_governance_rules_state_hash CHECK(
                        length(state_hash)=64
                        AND state_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT uq_governance_rules_workspace_id
                    UNIQUE(workspace_id, rule_id)
            ) WITHOUT ROWID
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governance_rules_list
            ON governance_rules(
                workspace_id, enabled, priority DESC, created_at_us DESC, rule_id
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_rules_no_replace
            BEFORE INSERT ON governance_rules
            WHEN EXISTS (SELECT 1 FROM governance_rules WHERE rule_id=NEW.rule_id)
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_RULE_IDENTITY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_rules_no_identity_update
            BEFORE UPDATE OF rule_id, workspace_id, created_at_us
            ON governance_rules
            WHEN OLD.rule_id<>NEW.rule_id
              OR OLD.workspace_id<>NEW.workspace_id
              OR OLD.created_at_us<>NEW.created_at_us
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_RULE_IDENTITY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_rules_no_delete
            BEFORE DELETE ON governance_rules
            BEGIN SELECT RAISE(ABORT, 'SOFT_DELETE_GOVERNANCE_RULE'); END
            """,
            """
            CREATE TABLE IF NOT EXISTS governance_context_triggers (
                trigger_id TEXT PRIMARY KEY
                    CONSTRAINT ck_governance_context_triggers_id CHECK(
                        length(trigger_id)=68
                        AND substr(trigger_id,1,4)='trg_'
                        AND substr(trigger_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL,
                trigger_type TEXT NOT NULL
                    CONSTRAINT ck_governance_context_triggers_type CHECK(
                        trigger_type IN ('file','tag','entity')
                    ),
                pattern TEXT NOT NULL
                    CONSTRAINT ck_governance_context_triggers_pattern CHECK(
                        length(pattern) BETWEEN 1 AND 2000
                    ),
                recall_query TEXT NOT NULL
                    CONSTRAINT ck_governance_context_triggers_query CHECK(
                        length(recall_query) BETWEEN 1 AND 2000
                    ),
                categories_json TEXT NOT NULL
                    CONSTRAINT ck_governance_context_triggers_categories CHECK(
                        json_valid(categories_json)
                        AND json_type(categories_json)='array'
                    ),
                enabled INTEGER NOT NULL
                    CONSTRAINT ck_governance_context_triggers_enabled CHECK(
                        enabled IN (0,1)
                    ),
                priority INTEGER NOT NULL
                    CONSTRAINT ck_governance_context_triggers_priority CHECK(
                        typeof(priority)='integer'
                    ),
                stream_version INTEGER NOT NULL
                    CONSTRAINT ck_governance_context_triggers_version CHECK(
                        stream_version >= 1
                    ),
                source_event_id TEXT NOT NULL
                    REFERENCES governance_events(event_id) ON DELETE RESTRICT,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                deleted_at_us INTEGER,
                state_hash TEXT NOT NULL
                    CONSTRAINT ck_governance_context_triggers_state_hash CHECK(
                        length(state_hash)=64
                        AND state_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT ck_governance_context_triggers_deleted CHECK(
                    deleted_at_us IS NULL OR deleted_at_us >= created_at_us
                ),
                CONSTRAINT uq_governance_context_triggers_workspace_id
                    UNIQUE(workspace_id, trigger_id)
            ) WITHOUT ROWID
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_governance_context_triggers_list
            ON governance_context_triggers(
                workspace_id, deleted_at_us, enabled, priority DESC,
                created_at_us, trigger_id
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_context_triggers_no_replace
            BEFORE INSERT ON governance_context_triggers
            WHEN EXISTS (
                SELECT 1 FROM governance_context_triggers
                WHERE trigger_id=NEW.trigger_id
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_TRIGGER_IDENTITY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_context_triggers_no_identity_update
            BEFORE UPDATE OF trigger_id, workspace_id, created_at_us
            ON governance_context_triggers
            WHEN OLD.trigger_id<>NEW.trigger_id
              OR OLD.workspace_id<>NEW.workspace_id
              OR OLD.created_at_us<>NEW.created_at_us
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_GOVERNANCE_TRIGGER_IDENTITY'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_context_triggers_no_delete
            BEFORE DELETE ON governance_context_triggers
            BEGIN SELECT RAISE(ABORT, 'SOFT_DELETE_GOVERNANCE_TRIGGER'); END
            """,
            """
            CREATE TABLE IF NOT EXISTS session_update_sequence (
                update_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_session_update_sequence_workspace CHECK(
                        substr(workspace_id,1,3)='ws_'
                    ),
                event_id TEXT NOT NULL UNIQUE
                    CONSTRAINT ck_session_update_sequence_event CHECK(
                        length(event_id)=68
                        AND substr(event_id,1,4)='evt_'
                        AND substr(event_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                event_source TEXT NOT NULL
                    CONSTRAINT ck_session_update_sequence_source CHECK(
                        event_source IN ('memory','governance')
                    )
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_session_update_sequence_workspace
            ON session_update_sequence(workspace_id, update_sequence)
            """,
            """
            INSERT OR IGNORE INTO session_update_sequence(
                workspace_id,event_id,event_source
            )
            SELECT workspace_id,event_id,'memory'
            FROM memory_events WHERE stream_kind='memory' ORDER BY rowid
            """,
            """
            CREATE TRIGGER IF NOT EXISTS session_update_sequence_no_update
            BEFORE UPDATE ON session_update_sequence
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_UPDATE_SEQUENCE'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS session_update_sequence_no_delete
            BEFORE DELETE ON session_update_sequence
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_UPDATE_SEQUENCE'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS session_update_sequence_no_replace
            BEFORE INSERT ON session_update_sequence
            WHEN EXISTS (
                SELECT 1 FROM session_update_sequence
                WHERE event_id=NEW.event_id
                   OR update_sequence=NEW.update_sequence
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_SESSION_UPDATE_SEQUENCE'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS memory_events_session_update_sequence
            AFTER INSERT ON memory_events
            WHEN NEW.stream_kind='memory'
            BEGIN
                INSERT INTO session_update_sequence(
                    workspace_id,event_id,event_source
                ) VALUES (NEW.workspace_id,NEW.event_id,'memory');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS governance_events_session_update_sequence
            AFTER INSERT ON governance_events
            BEGIN
                INSERT INTO session_update_sequence(
                    workspace_id,event_id,event_source
                ) VALUES (NEW.workspace_id,NEW.event_id,'governance');
            END
            """,
        ],
    ),
    (
        22,
        "Add immutable generation-scoped discovery projections",
        [
            """
            CREATE TABLE IF NOT EXISTS discovery_projection_partitions (
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_discovery_partitions_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                projection_name TEXT NOT NULL
                    CONSTRAINT ck_discovery_partitions_projection CHECK(
                        projection_name IN ('graph','code')
                    ),
                generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_partitions_generation CHECK(
                        typeof(generation)='integer' AND generation >= 1
                    ),
                partition_name TEXT NOT NULL
                    CONSTRAINT ck_discovery_partitions_name CHECK(
                        partition_name IN ('entities','communities','code')
                        AND (
                            (projection_name='graph' AND partition_name IN ('entities','communities'))
                            OR (projection_name='code' AND partition_name='code')
                        )
                    ),
                row_count INTEGER NOT NULL
                    CONSTRAINT ck_discovery_partitions_rows CHECK(
                        typeof(row_count)='integer' AND row_count >= 0
                    ),
                content_hash TEXT NOT NULL
                    CONSTRAINT ck_discovery_partitions_hash CHECK(
                        length(content_hash)=64
                        AND content_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                builder_version TEXT NOT NULL
                    CONSTRAINT ck_discovery_partitions_builder CHECK(
                        length(builder_version) BETWEEN 1 AND 80
                    ),
                built_at_us INTEGER NOT NULL
                    CONSTRAINT ck_discovery_partitions_built_at CHECK(
                        typeof(built_at_us)='integer' AND built_at_us >= 0
                    ),
                CONSTRAINT pk_discovery_projection_partitions PRIMARY KEY(
                    workspace_id,projection_name,generation,partition_name
                ),
                CONSTRAINT fk_discovery_partitions_manifest FOREIGN KEY(
                    workspace_id,projection_name,generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE IF NOT EXISTS discovery_entities (
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL DEFAULT 'graph'
                    CONSTRAINT ck_discovery_entities_projection CHECK(
                        projection_name='graph'
                    ),
                graph_generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_entities_generation CHECK(
                        typeof(graph_generation)='integer' AND graph_generation >= 1
                    ),
                entity_id TEXT NOT NULL
                    CONSTRAINT ck_discovery_entities_id CHECK(
                        length(entity_id)=68
                        AND substr(entity_id,1,4)='ent_'
                        AND substr(entity_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                name TEXT NOT NULL
                    CONSTRAINT ck_discovery_entities_name CHECK(
                        length(name) BETWEEN 1 AND 256
                    ),
                normalized_name TEXT NOT NULL
                    CONSTRAINT ck_discovery_entities_normalized CHECK(
                        length(normalized_name) BETWEEN 1 AND 256
                    ),
                entity_type TEXT NOT NULL
                    CONSTRAINT ck_discovery_entities_type CHECK(
                        length(entity_type) BETWEEN 1 AND 80
                        AND entity_type GLOB '[a-z]*'
                        AND entity_type NOT GLOB '*[^a-z0-9_-]*'
                    ),
                mention_count INTEGER NOT NULL
                    CONSTRAINT ck_discovery_entities_mentions CHECK(
                        typeof(mention_count)='integer' AND mention_count >= 0
                    ),
                identity_hash TEXT NOT NULL
                    CONSTRAINT ck_discovery_entities_identity CHECK(
                        length(identity_hash)=64
                        AND identity_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT pk_discovery_entities PRIMARY KEY(
                    workspace_id,graph_generation,entity_id
                ),
                CONSTRAINT uq_discovery_entities_identity UNIQUE(
                    workspace_id,graph_generation,identity_hash
                ),
                CONSTRAINT uq_discovery_entities_name UNIQUE(
                    workspace_id,graph_generation,entity_type,normalized_name
                ),
                CONSTRAINT fk_discovery_entities_manifest FOREIGN KEY(
                    workspace_id,projection_name,graph_generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_entities_public_id FOREIGN KEY(entity_id)
                    REFERENCES public_object_ids(public_id) ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_discovery_entities_list ON discovery_entities(workspace_id,graph_generation,entity_type,entity_id)",
            """
            CREATE TABLE IF NOT EXISTS discovery_entity_records (
                workspace_id TEXT NOT NULL,
                graph_generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_entity_records_generation CHECK(
                        typeof(graph_generation)='integer' AND graph_generation >= 1
                    ),
                entity_id TEXT NOT NULL,
                record_id TEXT NOT NULL
                    CONSTRAINT ck_discovery_entity_records_record CHECK(
                        length(record_id)=68
                        AND substr(record_id,1,4)='mem_'
                        AND substr(record_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                mention_count INTEGER NOT NULL DEFAULT 1
                    CONSTRAINT ck_discovery_entity_records_mentions CHECK(
                        typeof(mention_count)='integer' AND mention_count >= 1
                    ),
                CONSTRAINT pk_discovery_entity_records PRIMARY KEY(
                    workspace_id,graph_generation,entity_id,record_id
                ),
                CONSTRAINT fk_discovery_entity_records_entity FOREIGN KEY(
                    workspace_id,graph_generation,entity_id
                ) REFERENCES discovery_entities(
                    workspace_id,graph_generation,entity_id
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_entity_records_record FOREIGN KEY(
                    workspace_id,record_id
                ) REFERENCES memory_records(workspace_id,record_id)
                    ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_discovery_entity_records_record ON discovery_entity_records(workspace_id,graph_generation,record_id,entity_id)",
            """
            CREATE TABLE IF NOT EXISTS discovery_communities (
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL DEFAULT 'graph'
                    CONSTRAINT ck_discovery_communities_projection CHECK(
                        projection_name='graph'
                    ),
                graph_generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_communities_generation CHECK(
                        typeof(graph_generation)='integer' AND graph_generation >= 1
                    ),
                community_id TEXT NOT NULL
                    CONSTRAINT ck_discovery_communities_id CHECK(
                        length(community_id)=68
                        AND substr(community_id,1,4)='com_'
                        AND substr(community_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                label TEXT NOT NULL
                    CONSTRAINT ck_discovery_communities_label CHECK(
                        length(label) BETWEEN 1 AND 256
                    ),
                level INTEGER NOT NULL
                    CONSTRAINT ck_discovery_communities_level CHECK(
                        typeof(level)='integer' AND level BETWEEN 0 AND 32
                    ),
                parent_community_id TEXT
                    CONSTRAINT ck_discovery_communities_parent CHECK(
                        parent_community_id IS NULL OR (
                            length(parent_community_id)=68
                            AND substr(parent_community_id,1,4)='com_'
                            AND substr(parent_community_id,5) NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                member_count INTEGER NOT NULL
                    CONSTRAINT ck_discovery_communities_members CHECK(
                        typeof(member_count)='integer'
                        AND member_count BETWEEN 0 AND 1000000
                    ),
                identity_hash TEXT NOT NULL
                    CONSTRAINT ck_discovery_communities_identity CHECK(
                        length(identity_hash)=64
                        AND identity_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT pk_discovery_communities PRIMARY KEY(
                    workspace_id,graph_generation,community_id
                ),
                CONSTRAINT uq_discovery_communities_identity UNIQUE(
                    workspace_id,graph_generation,identity_hash
                ),
                CONSTRAINT fk_discovery_communities_manifest FOREIGN KEY(
                    workspace_id,projection_name,graph_generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_communities_public_id FOREIGN KEY(community_id)
                    REFERENCES public_object_ids(public_id) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_communities_parent FOREIGN KEY(
                    workspace_id,graph_generation,parent_community_id
                ) REFERENCES discovery_communities(
                    workspace_id,graph_generation,community_id
                ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_discovery_communities_list ON discovery_communities(workspace_id,graph_generation,level,parent_community_id,community_id)",
            """
            CREATE TABLE IF NOT EXISTS discovery_community_members (
                workspace_id TEXT NOT NULL,
                graph_generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_community_members_generation CHECK(
                        typeof(graph_generation)='integer' AND graph_generation >= 1
                    ),
                community_id TEXT NOT NULL,
                record_id TEXT NOT NULL
                    CONSTRAINT ck_discovery_community_members_record CHECK(
                        length(record_id)=68
                        AND substr(record_id,1,4)='mem_'
                        AND substr(record_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT pk_discovery_community_members PRIMARY KEY(
                    workspace_id,graph_generation,community_id,record_id
                ),
                CONSTRAINT fk_discovery_community_members_community FOREIGN KEY(
                    workspace_id,graph_generation,community_id
                ) REFERENCES discovery_communities(
                    workspace_id,graph_generation,community_id
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_community_members_record FOREIGN KEY(
                    workspace_id,record_id
                ) REFERENCES memory_records(workspace_id,record_id)
                    ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_discovery_community_members_record ON discovery_community_members(workspace_id,graph_generation,record_id,community_id)",
            """
            CREATE TABLE IF NOT EXISTS discovery_code_entities (
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL DEFAULT 'code'
                    CONSTRAINT ck_discovery_code_projection CHECK(
                        projection_name='code'
                    ),
                code_generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_code_generation CHECK(
                        typeof(code_generation)='integer' AND code_generation >= 1
                    ),
                code_entity_id TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_id CHECK(
                        length(code_entity_id)=69
                        AND substr(code_entity_id,1,5)='code_'
                        AND substr(code_entity_id,6) NOT GLOB '*[^0-9a-f]*'
                    ),
                kind TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_kind CHECK(
                        kind IN ('file','module','class','function','method','variable','symbol')
                    ),
                qualified_name TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_name CHECK(
                        length(qualified_name) BETWEEN 1 AND 256
                    ),
                normalized_name TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_normalized CHECK(
                        length(normalized_name) BETWEEN 1 AND 256
                    ),
                relative_file_path TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_relative_path CHECK(
                        length(relative_file_path) BETWEEN 1 AND 1024
                        AND substr(relative_file_path,1,1)<>'/'
                        AND relative_file_path NOT GLOB '[A-Za-z]:*'
                        AND instr(relative_file_path,'\\')=0
                        AND relative_file_path NOT LIKE '%//%'
                        AND relative_file_path<>'.'
                        AND relative_file_path<>'..'
                        AND relative_file_path NOT LIKE './%'
                        AND relative_file_path NOT LIKE '../%'
                        AND relative_file_path NOT LIKE '%/./%'
                        AND relative_file_path NOT LIKE '%/../%'
                        AND relative_file_path NOT LIKE '%/.'
                        AND relative_file_path NOT LIKE '%/..'
                    ),
                start_line INTEGER NOT NULL
                    CONSTRAINT ck_discovery_code_start CHECK(
                        typeof(start_line)='integer' AND start_line >= 1
                    ),
                end_line INTEGER NOT NULL
                    CONSTRAINT ck_discovery_code_end CHECK(
                        typeof(end_line)='integer' AND end_line >= start_line
                    ),
                identity_hash TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_identity CHECK(
                        length(identity_hash)=64
                        AND identity_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT pk_discovery_code_entities PRIMARY KEY(
                    workspace_id,code_generation,code_entity_id
                ),
                CONSTRAINT uq_discovery_code_identity UNIQUE(
                    workspace_id,code_generation,identity_hash
                ),
                CONSTRAINT fk_discovery_code_manifest FOREIGN KEY(
                    workspace_id,projection_name,code_generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_code_public_id FOREIGN KEY(code_entity_id)
                    REFERENCES public_object_ids(public_id) ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_discovery_code_search ON discovery_code_entities(workspace_id,code_generation,kind,normalized_name,code_entity_id)",
            "CREATE INDEX IF NOT EXISTS idx_discovery_code_path ON discovery_code_entities(workspace_id,code_generation,relative_file_path,start_line,code_entity_id)",
            *[
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROJECTION'); END
                """
                for table in (
                    "discovery_projection_partitions",
                    "discovery_entities",
                    "discovery_entity_records",
                    "discovery_communities",
                    "discovery_community_members",
                    "discovery_code_entities",
                )
            ],
            *[
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROJECTION'); END
                """
                for table in (
                    "discovery_projection_partitions",
                    "discovery_entities",
                    "discovery_entity_records",
                    "discovery_communities",
                    "discovery_community_members",
                    "discovery_code_entities",
                )
            ],
        ],
    ),
    (
        23,
        "Add immutable workspace federation link events",
        [
            """
            CREATE TABLE IF NOT EXISTS workspace_link_events (
                event_id TEXT PRIMARY KEY
                    CONSTRAINT ck_workspace_link_events_id CHECK(
                        length(event_id)=68
                        AND substr(event_id,1,4)='evt_'
                        AND substr(event_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_workspace_link_events_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                linked_workspace_id TEXT NOT NULL
                    CONSTRAINT ck_workspace_link_events_linked CHECK(
                        length(linked_workspace_id)=27
                        AND substr(linked_workspace_id,1,3)='ws_'
                        AND substr(linked_workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                        AND linked_workspace_id<>workspace_id
                    ),
                stream_version INTEGER NOT NULL
                    CONSTRAINT ck_workspace_link_events_version CHECK(
                        typeof(stream_version)='integer' AND stream_version >= 1
                    ),
                event_type TEXT NOT NULL
                    CONSTRAINT ck_workspace_link_events_type CHECK(
                        event_type IN ('workspace.linked','workspace.unlinked')
                    ),
                relationship TEXT NOT NULL DEFAULT 'related'
                    CONSTRAINT ck_workspace_link_events_relationship CHECK(
                        relationship='related'
                    ),
                label TEXT
                    CONSTRAINT ck_workspace_link_events_label CHECK(
                        label IS NULL OR (
                            typeof(label)='text' AND length(label) BETWEEN 1 AND 2000
                        )
                    ),
                occurred_at_us INTEGER NOT NULL
                    CONSTRAINT ck_workspace_link_events_occurred CHECK(
                        typeof(occurred_at_us)='integer'
                        AND occurred_at_us BETWEEN 0 AND 9223372036854775807
                    ),
                recorded_at_us INTEGER NOT NULL
                    CONSTRAINT ck_workspace_link_events_recorded CHECK(
                        typeof(recorded_at_us)='integer'
                        AND recorded_at_us BETWEEN 0 AND 9223372036854775807
                    ),
                previous_event_hash TEXT
                    CONSTRAINT ck_workspace_link_events_previous_hash CHECK(
                        previous_event_hash IS NULL OR (
                            length(previous_event_hash)=64
                            AND previous_event_hash NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                event_hash TEXT NOT NULL UNIQUE
                    CONSTRAINT ck_workspace_link_events_hash CHECK(
                        length(event_hash)=64
                        AND event_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT uq_workspace_link_events_version UNIQUE(
                    workspace_id,linked_workspace_id,stream_version
                )
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_workspace_link_events_list ON workspace_link_events(workspace_id,linked_workspace_id,stream_version DESC)",
            """
            CREATE TRIGGER IF NOT EXISTS workspace_link_events_no_replace
            BEFORE INSERT ON workspace_link_events
            WHEN EXISTS (
                SELECT 1 FROM workspace_link_events
                WHERE event_id=NEW.event_id
                   OR event_hash=NEW.event_hash
                   OR (
                       workspace_id=NEW.workspace_id
                       AND linked_workspace_id=NEW.linked_workspace_id
                       AND stream_version=NEW.stream_version
                   )
            )
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_WORKSPACE_LINK_EVENT'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS workspace_link_events_no_update
            BEFORE UPDATE ON workspace_link_events
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_WORKSPACE_LINK_EVENT'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS workspace_link_events_no_delete
            BEFORE DELETE ON workspace_link_events
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_WORKSPACE_LINK_EVENT'); END
            """,
        ],
    ),
    (
        24,
        "Add durable portable transfer staging",
        [
            """
            CREATE TABLE IF NOT EXISTS portable_transfer_sessions (
                session_id TEXT PRIMARY KEY
                    CONSTRAINT ck_portable_transfer_sessions_id CHECK(
                        length(session_id)=68
                        AND substr(session_id,1,4) IN ('xpt_','ipt_')
                        AND substr(session_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                direction TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_direction CHECK(
                        direction IN ('export','import')
                    ),
                status TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_status CHECK(
                        status IN ('building','ready','staging','finalizing',
                                   'succeeded','failed','expired')
                    ),
                event_root_hash TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_root CHECK(
                        length(event_root_hash)=64
                        AND event_root_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                manifest_json TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_manifest CHECK(
                        json_valid(manifest_json)
                        AND json_type(manifest_json)='object'
                    ),
                manifest_hash TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_manifest_hash CHECK(
                        length(manifest_hash)=64
                        AND manifest_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                page_count INTEGER NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_pages CHECK(
                        typeof(page_count)='integer' AND page_count >= 0
                    ),
                staged_page_count INTEGER NOT NULL DEFAULT 0
                    CONSTRAINT ck_portable_transfer_sessions_staged CHECK(
                        typeof(staged_page_count)='integer'
                        AND staged_page_count BETWEEN 0 AND page_count
                    ),
                total_bytes INTEGER NOT NULL DEFAULT 0
                    CONSTRAINT ck_portable_transfer_sessions_bytes CHECK(
                        typeof(total_bytes)='integer'
                        AND total_bytes BETWEEN 0 AND 4294967296
                    ),
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL
                    CONSTRAINT ck_portable_transfer_sessions_expiry CHECK(
                        expires_at_us > created_at_us
                    ),
                completed_at_us INTEGER
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_portable_transfer_sessions_live ON portable_transfer_sessions(workspace_id,direction,status,expires_at_us)",
            """
            CREATE TABLE IF NOT EXISTS portable_transfer_pages (
                session_id TEXT NOT NULL REFERENCES portable_transfer_sessions(session_id)
                    ON DELETE CASCADE,
                page_index INTEGER NOT NULL
                    CONSTRAINT ck_portable_transfer_pages_index CHECK(
                        typeof(page_index)='integer' AND page_index >= 0
                    ),
                page_kind TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_pages_kind CHECK(
                        page_kind IN ('events','legacy','vectors')
                    ),
                item_count INTEGER NOT NULL
                    CONSTRAINT ck_portable_transfer_pages_items CHECK(
                        typeof(item_count)='integer'
                        AND item_count BETWEEN 0 AND 4096
                    ),
                byte_count INTEGER NOT NULL
                    CONSTRAINT ck_portable_transfer_pages_bytes CHECK(
                        typeof(byte_count)='integer'
                        AND byte_count BETWEEN 1 AND 921600
                    ),
                page_hash TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_pages_hash CHECK(
                        length(page_hash)=64
                        AND page_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                relative_path TEXT NOT NULL
                    CONSTRAINT ck_portable_transfer_pages_path CHECK(
                        length(relative_path) BETWEEN 1 AND 240
                        AND relative_path NOT LIKE '/%'
                        AND relative_path NOT LIKE '\\%'
                        AND relative_path NOT LIKE '%..%'
                        AND relative_path NOT GLOB '*:*'
                    ),
                received_at_us INTEGER NOT NULL,
                CONSTRAINT pk_portable_transfer_pages PRIMARY KEY(
                    session_id,page_index
                )
            ) WITHOUT ROWID
            """,
        ],
    ),
    (
        25,
        "Add native edit approval and reviewed capture staging",
        [
            """
            CREATE TABLE IF NOT EXISTS native_edit_host_sessions (
                host_session_id TEXT PRIMARY KEY
                    CONSTRAINT ck_native_edit_host_session_id CHECK(
                        length(host_session_id)=68
                        AND substr(host_session_id,1,4)='hst_'
                        AND substr(host_session_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_native_edit_host_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                principal_hash TEXT NOT NULL
                    CONSTRAINT ck_native_edit_host_principal CHECK(
                        length(principal_hash)=64
                        AND principal_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                mcp_session_hash TEXT
                    CONSTRAINT ck_native_edit_host_mcp_session CHECK(
                        mcp_session_hash IS NULL OR (
                            length(mcp_session_hash)=64
                            AND mcp_session_hash NOT GLOB '*[^0-9a-f]*'
                        )
                    ),
                transport TEXT NOT NULL
                    CONSTRAINT ck_native_edit_host_transport CHECK(
                        transport IN ('local-ipc','remote-https')
                    ),
                credential_id_hash TEXT NOT NULL
                    CONSTRAINT ck_native_edit_host_credential CHECK(
                        length(credential_id_hash)=64
                        AND credential_id_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                created_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL
                    CONSTRAINT ck_native_edit_host_expiry CHECK(
                        expires_at_us > created_at_us
                    ),
                paired_at_us INTEGER,
                revoked_at_us INTEGER
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_native_edit_host_live ON native_edit_host_sessions(workspace_id,principal_hash,expires_at_us)",
            """
            CREATE TRIGGER IF NOT EXISTS trg_native_edit_host_immutable
            BEFORE UPDATE OF workspace_id,principal_hash,transport,credential_id_hash,
                created_at_us,expires_at_us ON native_edit_host_sessions
            BEGIN
                SELECT RAISE(ABORT,'native edit host identity is immutable');
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS native_edit_pending (
                pending_edit_id TEXT PRIMARY KEY
                    CONSTRAINT ck_native_edit_pending_id CHECK(
                        length(pending_edit_id)=68
                        AND substr(pending_edit_id,1,4)='edt_'
                        AND substr(pending_edit_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL,
                host_session_id TEXT NOT NULL REFERENCES native_edit_host_sessions(host_session_id)
                    ON DELETE RESTRICT,
                tool_name TEXT NOT NULL
                    CONSTRAINT ck_native_edit_pending_tool CHECK(
                        length(tool_name) BETWEEN 1 AND 80
                        AND tool_name GLOB '[A-Za-z]*'
                        AND tool_name NOT GLOB '*[^A-Za-z0-9_.:-]*'
                    ),
                arguments_hash TEXT NOT NULL,
                edit_hash TEXT NOT NULL,
                relative_paths_json TEXT NOT NULL
                    CONSTRAINT ck_native_edit_pending_paths CHECK(
                        json_valid(relative_paths_json)
                        AND json_type(relative_paths_json)='array'
                    ),
                preimages_json TEXT NOT NULL
                    CONSTRAINT ck_native_edit_pending_preimages CHECK(
                        json_valid(preimages_json)
                        AND json_type(preimages_json)='array'
                    ),
                created_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL
                    CONSTRAINT ck_native_edit_pending_expiry CHECK(
                        expires_at_us > created_at_us
                    ),
                preflighted_at_us INTEGER,
                CONSTRAINT ck_native_edit_pending_workspace CHECK(
                    length(workspace_id)=27
                    AND substr(workspace_id,1,3)='ws_'
                    AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_native_edit_pending_hashes CHECK(
                    length(arguments_hash)=64
                    AND arguments_hash NOT GLOB '*[^0-9a-f]*'
                    AND length(edit_hash)=64
                    AND edit_hash NOT GLOB '*[^0-9a-f]*'
                )
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_native_edit_pending_live ON native_edit_pending(workspace_id,host_session_id,expires_at_us)",
            """
            CREATE TRIGGER IF NOT EXISTS trg_native_edit_pending_immutable
            BEFORE UPDATE OF workspace_id,host_session_id,tool_name,arguments_hash,
                edit_hash,relative_paths_json,preimages_json,created_at_us,expires_at_us
                ON native_edit_pending
            BEGIN
                SELECT RAISE(ABORT,'native edit request is immutable');
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS native_edit_receipts (
                receipt_id TEXT PRIMARY KEY
                    CONSTRAINT ck_native_edit_receipt_id CHECK(
                        length(receipt_id)=68
                        AND substr(receipt_id,1,4)='rcp_'
                        AND substr(receipt_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                pending_edit_id TEXT NOT NULL UNIQUE REFERENCES native_edit_pending(pending_edit_id)
                    ON DELETE RESTRICT,
                workspace_id TEXT NOT NULL,
                host_session_id TEXT NOT NULL REFERENCES native_edit_host_sessions(host_session_id)
                    ON DELETE RESTRICT,
                principal_hash TEXT NOT NULL,
                mcp_session_hash TEXT NOT NULL,
                edit_hash TEXT NOT NULL,
                description_hash TEXT NOT NULL,
                receipt_token_hash TEXT NOT NULL UNIQUE,
                issued_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL
                    CONSTRAINT ck_native_edit_receipt_expiry CHECK(
                        expires_at_us > issued_at_us
                    ),
                staged_at_us INTEGER,
                consumed_at_us INTEGER,
                CONSTRAINT ck_native_edit_receipt_workspace CHECK(
                    length(workspace_id)=27
                    AND substr(workspace_id,1,3)='ws_'
                    AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_native_edit_receipt_host CHECK(
                    length(host_session_id)=68
                    AND substr(host_session_id,1,4)='hst_'
                    AND substr(host_session_id,5) NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_native_edit_receipt_hashes CHECK(
                    length(principal_hash)=64
                    AND principal_hash NOT GLOB '*[^0-9a-f]*'
                    AND length(mcp_session_hash)=64
                    AND mcp_session_hash NOT GLOB '*[^0-9a-f]*'
                    AND length(edit_hash)=64
                    AND edit_hash NOT GLOB '*[^0-9a-f]*'
                    AND length(description_hash)=64
                    AND description_hash NOT GLOB '*[^0-9a-f]*'
                    AND length(receipt_token_hash)=64
                    AND receipt_token_hash NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_native_edit_receipt_lifecycle CHECK(
                    consumed_at_us IS NULL OR staged_at_us IS NOT NULL
                )
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_native_edit_receipt_live ON native_edit_receipts(workspace_id,host_session_id,expires_at_us)",
            """
            CREATE TRIGGER IF NOT EXISTS trg_native_edit_receipt_immutable
            BEFORE UPDATE OF pending_edit_id,workspace_id,host_session_id,
                principal_hash,mcp_session_hash,edit_hash,description_hash,
                receipt_token_hash,issued_at_us,expires_at_us ON native_edit_receipts
            BEGIN
                SELECT RAISE(ABORT,'native edit receipt binding is immutable');
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_capture_candidates (
                candidate_id TEXT PRIMARY KEY
                    CONSTRAINT ck_memory_capture_candidate_id CHECK(
                        length(candidate_id)=68
                        AND substr(candidate_id,1,4)='cap_'
                        AND substr(candidate_id,5) NOT GLOB '*[^0-9a-f]*'
                    ),
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_memory_capture_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                idempotency_key TEXT NOT NULL
                    CONSTRAINT ck_memory_capture_idempotency CHECK(
                        length(idempotency_key) BETWEEN 8 AND 128
                        AND idempotency_key NOT GLOB '*[^A-Za-z0-9._~-]*'
                    ),
                candidate_hash TEXT NOT NULL,
                source_kind TEXT NOT NULL
                    CONSTRAINT ck_memory_capture_source CHECK(
                        source_kind IN ('native_edit','tool_result','dreaming','system')
                    ),
                proposed_record_json TEXT NOT NULL
                    CONSTRAINT ck_memory_capture_record CHECK(
                        json_valid(proposed_record_json)
                        AND json_type(proposed_record_json)='object'
                    ),
                provenance_json TEXT NOT NULL
                    CONSTRAINT ck_memory_capture_provenance CHECK(
                        json_valid(provenance_json)
                        AND json_type(provenance_json)='object'
                    ),
                status TEXT NOT NULL DEFAULT 'pending'
                    CONSTRAINT ck_memory_capture_status CHECK(
                        status IN ('pending','promoted')
                    ),
                created_at_us INTEGER NOT NULL,
                promoted_at_us INTEGER,
                promoted_event_id TEXT REFERENCES memory_events(event_id)
                    ON DELETE RESTRICT,
                promotion_idempotency_key TEXT,
                promotion_request_hash TEXT,
                CONSTRAINT uq_memory_capture_hash UNIQUE(workspace_id,candidate_hash),
                CONSTRAINT uq_memory_capture_idempotency UNIQUE(workspace_id,idempotency_key),
                CONSTRAINT ck_memory_capture_hash CHECK(
                    length(candidate_hash)=64
                    AND candidate_hash NOT GLOB '*[^0-9a-f]*'
                ),
                CONSTRAINT ck_memory_capture_promotion CHECK(
                    (status='pending'
                     AND promoted_at_us IS NULL
                     AND promoted_event_id IS NULL
                     AND promotion_idempotency_key IS NULL
                     AND promotion_request_hash IS NULL)
                    OR
                    (status='promoted'
                     AND promoted_at_us IS NOT NULL
                     AND promoted_event_id IS NOT NULL
                     AND promotion_idempotency_key IS NOT NULL
                     AND promotion_request_hash IS NOT NULL
                     AND length(promotion_request_hash)=64
                     AND promotion_request_hash NOT GLOB '*[^0-9a-f]*')
                )
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_memory_capture_list ON memory_capture_candidates(workspace_id,status,created_at_us DESC,candidate_id DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_capture_promotion_key ON memory_capture_candidates(workspace_id,promotion_idempotency_key) WHERE promotion_idempotency_key IS NOT NULL",
            """
            CREATE TRIGGER IF NOT EXISTS trg_memory_capture_candidate_immutable
            BEFORE UPDATE OF workspace_id,idempotency_key,candidate_hash,source_kind,
                proposed_record_json,provenance_json,created_at_us
                ON memory_capture_candidates
            BEGIN
                SELECT RAISE(ABORT,'capture candidate proposal is immutable');
            END
            """,
        ],
    ),
    (
        26,
        "Add immutable generation-scoped code dependency edges",
        [
            """
            CREATE TABLE IF NOT EXISTS discovery_code_edges (
                workspace_id TEXT NOT NULL,
                projection_name TEXT NOT NULL DEFAULT 'code'
                    CONSTRAINT ck_discovery_code_edges_projection CHECK(
                        projection_name='code'
                    ),
                code_generation INTEGER NOT NULL
                    CONSTRAINT ck_discovery_code_edges_generation CHECK(
                        typeof(code_generation)='integer' AND code_generation >= 1
                    ),
                source_code_entity_id TEXT NOT NULL,
                target_code_entity_id TEXT NOT NULL,
                edge_kind TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_edges_kind CHECK(
                        edge_kind IN ('call','import','reference')
                    ),
                source_line INTEGER
                    CONSTRAINT ck_discovery_code_edges_line CHECK(
                        source_line IS NULL OR (
                            typeof(source_line)='integer' AND source_line >= 1
                        )
                    ),
                identity_hash TEXT NOT NULL
                    CONSTRAINT ck_discovery_code_edges_identity CHECK(
                        length(identity_hash)=64
                        AND identity_hash NOT GLOB '*[^0-9a-f]*'
                    ),
                CONSTRAINT ck_discovery_code_edges_distinct CHECK(
                    source_code_entity_id<>target_code_entity_id
                ),
                CONSTRAINT pk_discovery_code_edges PRIMARY KEY(
                    workspace_id,code_generation,source_code_entity_id,
                    target_code_entity_id,edge_kind
                ),
                CONSTRAINT uq_discovery_code_edges_identity UNIQUE(
                    workspace_id,code_generation,identity_hash
                ),
                CONSTRAINT fk_discovery_code_edges_manifest FOREIGN KEY(
                    workspace_id,projection_name,code_generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_code_edges_source FOREIGN KEY(
                    workspace_id,code_generation,source_code_entity_id
                ) REFERENCES discovery_code_entities(
                    workspace_id,code_generation,code_entity_id
                ) ON DELETE RESTRICT,
                CONSTRAINT fk_discovery_code_edges_target FOREIGN KEY(
                    workspace_id,code_generation,target_code_entity_id
                ) REFERENCES discovery_code_entities(
                    workspace_id,code_generation,code_entity_id
                ) ON DELETE RESTRICT
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_discovery_code_edges_target ON discovery_code_edges(workspace_id,code_generation,target_code_entity_id,source_code_entity_id,edge_kind)",
            """
            CREATE TRIGGER IF NOT EXISTS discovery_code_edges_no_update
            BEFORE UPDATE ON discovery_code_edges
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROJECTION'); END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS discovery_code_edges_no_delete
            BEFORE DELETE ON discovery_code_edges
            BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_DISCOVERY_PROJECTION'); END
            """,
        ],
    ),
    (
        27,
        "Add durable workspace consolidation previews and runs",
        [
            """
            CREATE TABLE IF NOT EXISTS consolidation_previews (
                preview_id TEXT PRIMARY KEY,
                target_workspace_id TEXT NOT NULL,
                principal_hash TEXT NOT NULL,
                session_hash TEXT NOT NULL,
                selection_token_hash TEXT NOT NULL,
                sources_json TEXT NOT NULL CHECK(json_valid(sources_json)),
                snapshots_json TEXT NOT NULL CHECK(json_valid(snapshots_json)),
                selection_hash TEXT NOT NULL,
                selected_count INTEGER NOT NULL CHECK(selected_count BETWEEN 1 AND 10000),
                total_bytes INTEGER NOT NULL CHECK(total_bytes BETWEEN 1 AND 67108864),
                created_at_us INTEGER NOT NULL,
                expires_at_us INTEGER NOT NULL CHECK(expires_at_us>created_at_us),
                applied_run_id TEXT,
                CHECK(length(preview_id)=68 AND substr(preview_id,1,4)='cpr_'),
                CHECK(length(principal_hash)=64 AND length(session_hash)=64
                      AND length(selection_token_hash)=64 AND length(selection_hash)=64)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE IF NOT EXISTS consolidation_preview_records (
                preview_id TEXT NOT NULL REFERENCES consolidation_previews(preview_id) ON DELETE RESTRICT,
                ordinal INTEGER NOT NULL CHECK(ordinal>=0),
                source_workspace_id TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                source_state_hash TEXT NOT NULL,
                source_content_hash TEXT NOT NULL,
                target_record_id TEXT NOT NULL,
                PRIMARY KEY(preview_id,ordinal),
                UNIQUE(preview_id,source_workspace_id,source_record_id),
                UNIQUE(preview_id,target_record_id)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE IF NOT EXISTS consolidation_runs (
                run_id TEXT PRIMARY KEY,
                target_workspace_id TEXT NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('consolidate','consolidate_archive')),
                idempotency_key TEXT NOT NULL,
                preview_id TEXT NOT NULL REFERENCES consolidation_previews(preview_id) ON DELETE RESTRICT,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('target_committed','archiving','completed','recovery_required')),
                imported_count INTEGER NOT NULL CHECK(imported_count>=0),
                archived_count INTEGER NOT NULL CHECK(archived_count>=0),
                event_ids_json TEXT NOT NULL CHECK(json_valid(event_ids_json)),
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                UNIQUE(target_workspace_id,operation,idempotency_key)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE IF NOT EXISTS consolidation_mappings (
                target_workspace_id TEXT NOT NULL,
                source_workspace_id TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                target_record_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                source_state_hash TEXT NOT NULL,
                target_event_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES consolidation_runs(run_id) ON DELETE RESTRICT,
                PRIMARY KEY(target_workspace_id,source_workspace_id,source_record_id),
                UNIQUE(target_workspace_id,target_record_id)
            ) WITHOUT ROWID
            """,
            """
            CREATE TABLE IF NOT EXISTS consolidation_archive_progress (
                run_id TEXT NOT NULL REFERENCES consolidation_runs(run_id) ON DELETE RESTRICT,
                source_workspace_id TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                expected_event_id TEXT NOT NULL,
                expected_state_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','archived')),
                archive_event_id TEXT,
                PRIMARY KEY(run_id,source_workspace_id,source_record_id),
                CHECK((status='pending' AND archive_event_id IS NULL)
                   OR (status='archived' AND archive_event_id IS NOT NULL))
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_consolidation_runs_recovery ON consolidation_runs(target_workspace_id,status,updated_at_us)",
            """
            CREATE TRIGGER IF NOT EXISTS consolidation_preview_identity_immutable
            BEFORE UPDATE OF target_workspace_id,principal_hash,session_hash,selection_token_hash,sources_json,
                snapshots_json,selection_hash,selected_count,total_bytes,created_at_us,expires_at_us
                ON consolidation_previews
            BEGIN SELECT RAISE(ABORT,'consolidation preview identity is immutable'); END
            """,
        ],
    ),
    (
        28,
        "Add bounded durable v7 dreaming strategy state",
        [
            """
            CREATE TABLE IF NOT EXISTS dreaming_strategy_state (
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_dreaming_state_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                strategy TEXT NOT NULL
                    CONSTRAINT ck_dreaming_state_strategy CHECK(
                        strategy IN ('failed_decision','pending_outcome',
                                     'connection_discovery','community_refresh')
                    ),
                status TEXT NOT NULL
                    CONSTRAINT ck_dreaming_state_status CHECK(
                        status IN ('idle','running','disabled','degraded')
                    ),
                cursor TEXT
                    CONSTRAINT ck_dreaming_state_cursor CHECK(
                        cursor IS NULL OR length(cursor) BETWEEN 1 AND 128
                    ),
                pending_count INTEGER NOT NULL DEFAULT 0
                    CONSTRAINT ck_dreaming_state_pending CHECK(
                        typeof(pending_count)='integer'
                        AND pending_count BETWEEN 0 AND 10000
                    ),
                cooldown_until_us INTEGER,
                last_started_at_us INTEGER,
                last_success_at_us INTEGER,
                stable_error_code TEXT
                    CONSTRAINT ck_dreaming_state_error CHECK(
                        stable_error_code IS NULL OR (
                            length(stable_error_code) BETWEEN 1 AND 64
                            AND stable_error_code NOT GLOB '*[^A-Z0-9_]*'
                        )
                    ),
                yielded INTEGER NOT NULL DEFAULT 0
                    CONSTRAINT ck_dreaming_state_yielded CHECK(yielded IN (0,1)),
                updated_at_us INTEGER NOT NULL,
                CONSTRAINT pk_dreaming_strategy_state PRIMARY KEY(
                    workspace_id,strategy
                ),
                CONSTRAINT ck_dreaming_state_times CHECK(
                    (cooldown_until_us IS NULL OR cooldown_until_us>=0)
                    AND (last_started_at_us IS NULL OR last_started_at_us>=0)
                    AND (last_success_at_us IS NULL OR last_success_at_us>=0)
                    AND updated_at_us>=0
                )
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_dreaming_state_status ON dreaming_strategy_state(workspace_id,status,updated_at_us DESC,strategy)",
            """
            CREATE TRIGGER IF NOT EXISTS trg_dreaming_state_identity_immutable
            BEFORE UPDATE OF workspace_id,strategy ON dreaming_strategy_state
            BEGIN
                SELECT RAISE(ABORT,'dreaming strategy identity is immutable');
            END
            """,
        ],
    ),
    (
        29,
        "Add owner-fenced portable import finalization leases",
        [
            """
            CREATE TABLE IF NOT EXISTS portable_transfer_finalization_leases (
                session_id TEXT PRIMARY KEY
                    REFERENCES portable_transfer_sessions(session_id)
                    ON DELETE CASCADE,
                owner_token TEXT NOT NULL UNIQUE
                    CONSTRAINT ck_portable_finalization_owner CHECK(
                        length(owner_token)=64
                        AND owner_token NOT GLOB '*[^0-9a-f]*'
                    ),
                attempt_relative_path TEXT NOT NULL
                    CONSTRAINT ck_portable_finalization_attempt CHECK(
                        attempt_relative_path='attempts/' || owner_token
                    ),
                acquired_at_us INTEGER NOT NULL
                    CONSTRAINT ck_portable_finalization_acquired CHECK(
                        typeof(acquired_at_us)='integer' AND acquired_at_us>=0
                    ),
                renewed_at_us INTEGER NOT NULL
                    CONSTRAINT ck_portable_finalization_renewed CHECK(
                        typeof(renewed_at_us)='integer'
                        AND renewed_at_us>=acquired_at_us
                    ),
                expires_at_us INTEGER NOT NULL
                    CONSTRAINT ck_portable_finalization_expiry CHECK(
                        typeof(expires_at_us)='integer'
                        AND expires_at_us>renewed_at_us
                    )
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_portable_finalization_expiry ON portable_transfer_finalization_leases(expires_at_us)",
            """
            CREATE TRIGGER IF NOT EXISTS trg_portable_finalization_owner_immutable
            BEFORE UPDATE OF session_id,owner_token,attempt_relative_path,acquired_at_us
                ON portable_transfer_finalization_leases
            BEGIN
                SELECT RAISE(ABORT,'portable finalization lease identity is immutable');
            END
            """,
            """
            CREATE TABLE IF NOT EXISTS portable_transfer_attempt_artifacts (
                session_id TEXT NOT NULL
                    REFERENCES portable_transfer_sessions(session_id)
                    ON DELETE CASCADE,
                owner_token TEXT NOT NULL
                    CONSTRAINT ck_portable_artifact_owner CHECK(
                        length(owner_token)=64
                        AND owner_token NOT GLOB '*[^0-9a-f]*'
                    ),
                artifact_kind TEXT NOT NULL
                    CONSTRAINT ck_portable_artifact_kind CHECK(
                        artifact_kind='qdrant_collection'
                    ),
                artifact_name TEXT NOT NULL
                    CONSTRAINT ck_portable_artifact_name CHECK(
                        length(artifact_name) BETWEEN 1 AND 255
                    ),
                created_at_us INTEGER NOT NULL
                    CONSTRAINT ck_portable_artifact_created CHECK(
                        typeof(created_at_us)='integer' AND created_at_us>=0
                    ),
                PRIMARY KEY(session_id,owner_token,artifact_kind,artifact_name)
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_portable_artifact_owner ON portable_transfer_attempt_artifacts(session_id,owner_token)",
        ],
    ),
    (
        30,
        "Add bounded briefing record selection indexes",
        [
            "CREATE INDEX IF NOT EXISTS idx_memory_records_briefing_type "
            "ON memory_records(workspace_id,record_type,updated_at_us DESC,record_id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_memory_records_briefing_outcome "
            "ON memory_records(workspace_id,worked,updated_at_us DESC,record_id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_memory_events_root_covering "
            "ON memory_events(workspace_id,event_id,event_hash)",
        ],
    ),
    (
        31,
        "Add dense vector reuse attestations",
        [
            "ALTER TABLE dense_projection_refs ADD COLUMN vector_format TEXT "
            "CONSTRAINT ck_dense_refs_vector_format CHECK(vector_format IS NULL "
            "OR vector_format='qdrant-cosine-f32-le-v1')",
            "ALTER TABLE dense_projection_refs ADD COLUMN vector_sha256 TEXT "
            "CONSTRAINT ck_dense_refs_vector_sha256 CHECK(vector_sha256 IS NULL "
            "OR (length(vector_sha256)=64 AND "
            "vector_sha256 NOT GLOB '*[^0-9a-f]*'))",
        ],
    ),
    (
        32,
        "Add leased dense generation garbage collection",
        [
            """
            CREATE TABLE IF NOT EXISTS dense_generation_read_leases (
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_dense_generation_leases_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                projection_name TEXT NOT NULL DEFAULT 'dense'
                    CONSTRAINT ck_dense_generation_leases_projection CHECK(
                        projection_name='dense'
                    ),
                provider_key TEXT NOT NULL
                    CONSTRAINT ck_dense_generation_leases_provider CHECK(
                        length(provider_key) BETWEEN 1 AND 64
                    ),
                projection_generation INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_leases_generation CHECK(
                        typeof(projection_generation)='integer'
                        AND projection_generation>=1
                    ),
                owner_id TEXT NOT NULL
                    CONSTRAINT ck_dense_generation_leases_owner CHECK(
                        length(owner_id) BETWEEN 1 AND 128
                    ),
                fencing_token INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_leases_fence CHECK(
                        typeof(fencing_token)='integer' AND fencing_token>=1
                    ),
                acquired_at_us INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_leases_acquired CHECK(
                        typeof(acquired_at_us)='integer' AND acquired_at_us>=0
                    ),
                renewed_at_us INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_leases_renewed CHECK(
                        typeof(renewed_at_us)='integer'
                        AND renewed_at_us>=acquired_at_us
                    ),
                expires_at_us INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_leases_expires CHECK(
                        typeof(expires_at_us)='integer'
                        AND expires_at_us>renewed_at_us
                    ),
                CONSTRAINT pk_dense_generation_read_leases PRIMARY KEY(
                    workspace_id,provider_key,projection_generation,owner_id
                ),
                CONSTRAINT fk_dense_generation_read_leases_manifest FOREIGN KEY(
                    workspace_id,projection_name,projection_generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE CASCADE
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_dense_generation_leases_expiry "
            "ON dense_generation_read_leases(workspace_id,provider_key,"
            "projection_generation,expires_at_us)",
            """
            CREATE TABLE IF NOT EXISTS dense_generation_gc_jobs (
                workspace_id TEXT NOT NULL
                    CONSTRAINT ck_dense_generation_gc_workspace CHECK(
                        length(workspace_id)=27
                        AND substr(workspace_id,1,3)='ws_'
                        AND substr(workspace_id,4) NOT GLOB '*[^0-9a-f]*'
                    ),
                projection_name TEXT NOT NULL DEFAULT 'dense'
                    CONSTRAINT ck_dense_generation_gc_projection CHECK(
                        projection_name='dense'
                    ),
                provider_key TEXT NOT NULL
                    CONSTRAINT ck_dense_generation_gc_provider CHECK(
                        length(provider_key) BETWEEN 1 AND 64
                    ),
                projection_generation INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_gc_generation CHECK(
                        typeof(projection_generation)='integer'
                        AND projection_generation>=1
                    ),
                collection_name TEXT
                    CONSTRAINT ck_dense_generation_gc_collection CHECK(
                        collection_name IS NULL
                        OR length(collection_name) BETWEEN 1 AND 255
                    ),
                status TEXT NOT NULL
                    CONSTRAINT ck_dense_generation_gc_status CHECK(
                        status IN ('queued','running','dead_letter')
                    ),
                attempts INTEGER NOT NULL DEFAULT 0
                    CONSTRAINT ck_dense_generation_gc_attempts CHECK(
                        typeof(attempts)='integer' AND attempts>=0
                    ),
                max_attempts INTEGER NOT NULL DEFAULT 3
                    CONSTRAINT ck_dense_generation_gc_max_attempts CHECK(
                        typeof(max_attempts)='integer' AND max_attempts>=1
                    ),
                available_at_us INTEGER NOT NULL
                    CONSTRAINT ck_dense_generation_gc_available CHECK(
                        typeof(available_at_us)='integer' AND available_at_us>=0
                    ),
                claim_owner TEXT,
                claim_token TEXT,
                claim_expires_at_us INTEGER,
                last_error_code TEXT,
                created_at_us INTEGER NOT NULL,
                updated_at_us INTEGER NOT NULL,
                CONSTRAINT pk_dense_generation_gc_jobs PRIMARY KEY(
                    workspace_id,provider_key,projection_generation
                ),
                CONSTRAINT ck_dense_generation_gc_claim CHECK(
                    (status='running'
                     AND claim_owner IS NOT NULL
                     AND claim_token IS NOT NULL
                     AND claim_expires_at_us IS NOT NULL)
                    OR
                    (status<>'running'
                     AND claim_owner IS NULL
                     AND claim_token IS NULL
                     AND claim_expires_at_us IS NULL)
                ),
                CONSTRAINT ck_dense_generation_gc_error CHECK(
                    last_error_code IS NULL
                    OR (length(last_error_code) BETWEEN 2 AND 64
                        AND last_error_code NOT GLOB '*[^A-Z0-9_]*')
                ),
                CONSTRAINT fk_dense_generation_gc_manifest FOREIGN KEY(
                    workspace_id,projection_name,projection_generation
                ) REFERENCES projection_manifests(
                    workspace_id,projection_name,generation
                ) ON DELETE CASCADE
            ) WITHOUT ROWID
            """,
            "CREATE INDEX IF NOT EXISTS idx_dense_generation_gc_ready "
            "ON dense_generation_gc_jobs(status,available_at_us,workspace_id,"
            "projection_generation)",
            """
            CREATE TRIGGER IF NOT EXISTS dense_generation_gc_blocks_activation
            BEFORE UPDATE OF status ON projection_manifests
            WHEN NEW.projection_name='dense' AND NEW.status='active'
              AND EXISTS (
                SELECT 1 FROM dense_generation_gc_jobs gc
                WHERE gc.workspace_id=NEW.workspace_id
                  AND gc.provider_key=json_extract(NEW.details_json,'$.provider_key')
                  AND gc.projection_generation=NEW.generation
              )
            BEGIN SELECT RAISE(ABORT, 'DENSE_GENERATION_GC_ENROLLED'); END
            """,
        ],
    ),
]

if MIGRATIONS[-1][0] != CURRENT_SCHEMA_VERSION:  # pragma: no cover - import guard
    raise RuntimeError("CURRENT_SCHEMA_VERSION does not match the migration ledger")


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get current schema version from database."""
    cursor = conn.cursor()

    # Check if version table exists
    cursor.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='schema_version'
    """)

    if not cursor.fetchone():
        # Create version table
        cursor.execute("""
            CREATE TABLE schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        return 0

    cursor.execute("SELECT MAX(version) FROM schema_version")
    result = cursor.fetchone()
    return result[0] if result[0] else 0


def check_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _retained_public_id(
    workspace_id: str,
    object_kind: str,
    prefix: str,
    source_id: int,
) -> str:
    encoded = json.dumps(
        [
            "daem0nmcp",
            "v7",
            "public-object-id",
            workspace_id,
            object_kind,
            f"i:{source_id}",
            0,
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def backfill_retained_public_object_ids(
    connection: sqlite3.Connection,
    workspace_id: str,
    *,
    created_at_us: int | None = None,
) -> int:
    """Transactionally populate deterministic IDs for retained public rows.

    The caller owns the transaction. Existing exact rows are idempotent;
    collisions or divergent bindings fail closed before any commit.
    """

    if (
        not isinstance(workspace_id, str)
        or len(workspace_id) != 27
        or not workspace_id.startswith("ws_")
        or any(char not in "0123456789abcdef" for char in workspace_id[3:])
    ):
        raise ValueError("workspace_id must be an opaque v7 workspace identifier")
    timestamp = time.time_ns() // 1_000 if created_at_us is None else created_at_us
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, int)
        or timestamp < 0
        or timestamp > 9_223_372_036_854_775_807
    ):
        raise ValueError("created_at_us must be a non-negative signed integer")
    if not _table_exists(connection, "public_object_ids"):
        raise RuntimeError("PUBLIC_ID_SCHEMA_INCOMPLETE")

    sources = (
        ("rule", "rule", "rules"),
        ("trigger", "trg", "context_triggers"),
        ("active_context", "act", "active_context"),
    )
    inserted = 0
    for object_kind, prefix, table in sources:
        if not _table_exists(connection, table):
            continue
        source_rows = connection.execute(
            f'SELECT id FROM "{table}" ORDER BY id'
        ).fetchall()
        for row in source_rows:
            source_id = row[0]
            if (
                isinstance(source_id, bool)
                or not isinstance(source_id, int)
                or source_id < 1
                or source_id > 9_223_372_036_854_775_807
            ):
                raise RuntimeError("PUBLIC_ID_INTEGRITY_ERROR")
            source_key = f"i:{source_id}"
            public_id = _retained_public_id(
                workspace_id,
                object_kind,
                prefix,
                source_id,
            )
            existing = connection.execute(
                "SELECT public_id FROM public_object_ids WHERE workspace_id=? "
                "AND object_kind=? AND source_key=? AND projection_generation=0",
                (workspace_id, object_kind, source_key),
            ).fetchall()
            if existing:
                if len(existing) != 1 or existing[0][0] != public_id:
                    raise RuntimeError("PUBLIC_ID_INTEGRITY_ERROR")
                continue
            collision = connection.execute(
                "SELECT 1 FROM public_object_ids WHERE public_id=? LIMIT 1",
                (public_id,),
            ).fetchone()
            if collision is not None:
                raise RuntimeError("PUBLIC_ID_INTEGRITY_ERROR")
            connection.execute(
                "INSERT INTO public_object_ids "
                "(workspace_id,object_kind,source_key,projection_generation,"
                "public_id,created_at_us) VALUES (?,?,?,?,?,?)",
                (
                    workspace_id,
                    object_kind,
                    source_key,
                    0,
                    public_id,
                    timestamp,
                ),
            )
            inserted += 1
    return inserted


def _retained_timestamp_us(value: object) -> int:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        delta = parsed.astimezone(timezone.utc) - epoch
        timestamp = (
            delta.days * 86_400 + delta.seconds
        ) * 1_000_000 + delta.microseconds
    except (OverflowError, TypeError, ValueError):
        raise RuntimeError("GOVERNANCE_BACKFILL_INVALID") from None
    if not 0 <= timestamp <= 9_223_372_036_854_775_807:
        raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
    return timestamp


def _retained_text_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, RecursionError):
        raise RuntimeError("GOVERNANCE_BACKFILL_INVALID") from None
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and 1 <= len(item) <= 2_000 for item in parsed
    ):
        raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
    return parsed


def _retained_public_mapping(
    connection: sqlite3.Connection,
    workspace_id: str,
    object_kind: str,
    source_id: int,
) -> str:
    rows = connection.execute(
        "SELECT public_id FROM public_object_ids WHERE workspace_id=? "
        "AND object_kind=? AND source_key=? AND projection_generation=0",
        (workspace_id, object_kind, f"i:{source_id}"),
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise RuntimeError("PUBLIC_ID_INTEGRITY_ERROR")
    return rows[0][0]


def _retained_path_workspace_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        root = Path(value).expanduser().resolve()
        root_key = os.path.normcase(str(root))
    except (OSError, RuntimeError, ValueError):
        return None
    return f"ws_{hashlib.sha256(root_key.encode('utf-8')).hexdigest()[:24]}"


def backfill_retained_governance(
    connection: sqlite3.Connection,
    workspace_id: str,
) -> int:
    """Append canonical creation events for retained rules and triggers.

    The caller owns one transaction containing migration 21, the public-ID
    backfill, these events, and their typed projections.
    """

    from daem0nmcp.event_store import (
        GovernanceEventCommand,
        GovernanceEventStore,
    )

    if not _table_exists(connection, "governance_events"):
        raise RuntimeError("GOVERNANCE_SCHEMA_INCOMPLETE")
    store = GovernanceEventStore(connection, assume_transaction=True)
    inserted = 0
    if _table_exists(connection, "rules"):
        rows = connection.execute(
            "SELECT id,trigger,must_do,must_not,ask_first,warnings,priority,"
            "enabled,created_at FROM rules ORDER BY id"
        ).fetchall()
        for row in rows:
            source_id = row[0]
            if (
                isinstance(source_id, bool)
                or not isinstance(source_id, int)
                or source_id < 1
            ):
                raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
            public_id = _retained_public_mapping(
                connection, workspace_id, "rule", source_id
            )
            if (
                connection.execute(
                    "SELECT 1 FROM governance_rules WHERE workspace_id=? AND rule_id=?",
                    (workspace_id, public_id),
                ).fetchone()
                is not None
            ):
                continue
            if (
                connection.execute(
                    "SELECT 1 FROM governance_events WHERE workspace_id=? "
                    "AND stream_id=?",
                    (workspace_id, public_id),
                ).fetchone()
                is not None
            ):
                raise RuntimeError("GOVERNANCE_BACKFILL_INTEGRITY_ERROR")
            created_at_us = _retained_timestamp_us(row[8])
            if not isinstance(row[1], str) or not 1 <= len(row[1]) <= 2_000:
                raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
            if (
                isinstance(row[6], bool)
                or not isinstance(row[6], int)
                or not -1_000 <= row[6] <= 1_000
                or row[7] not in (0, 1)
            ):
                raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
            state = {
                "rule_id": public_id,
                "trigger": row[1],
                "must_do": _retained_text_list(row[2]),
                "must_not": _retained_text_list(row[3]),
                "ask_first": _retained_text_list(row[4]),
                "warnings": _retained_text_list(row[5]),
                "priority": row[6],
                "enabled": bool(row[7]),
                "created_at_us": created_at_us,
                "updated_at_us": created_at_us,
            }
            store.append_and_project(
                GovernanceEventCommand(
                    workspace_id=workspace_id,
                    stream_id=public_id,
                    stream_kind="rule",
                    event_type="rule.created",
                    occurred_at_us=created_at_us,
                    recorded_at_us=created_at_us,
                    actor_type="migration",
                    correlation_id=f"migration21:rule:{source_id}",
                    payload=state,
                    expected_stream_version=1,
                )
            )
            inserted += 1
    if _table_exists(connection, "context_triggers"):
        rows = connection.execute(
            "SELECT id,project_path,trigger_type,pattern,recall_topic,recall_categories,"
            "is_active,priority,created_at FROM context_triggers ORDER BY id"
        ).fetchall()
        public_types = {
            "file_pattern": "file",
            "tag_match": "tag",
            "entity_match": "entity",
        }
        for row in rows:
            source_id = row[0]
            if (
                isinstance(source_id, bool)
                or not isinstance(source_id, int)
                or source_id < 1
            ):
                raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
            if _retained_path_workspace_id(row[1]) != workspace_id:
                continue
            public_id = _retained_public_mapping(
                connection, workspace_id, "trigger", source_id
            )
            if (
                connection.execute(
                    "SELECT 1 FROM governance_context_triggers "
                    "WHERE workspace_id=? AND trigger_id=?",
                    (workspace_id, public_id),
                ).fetchone()
                is not None
            ):
                continue
            if (
                connection.execute(
                    "SELECT 1 FROM governance_events WHERE workspace_id=? "
                    "AND stream_id=?",
                    (workspace_id, public_id),
                ).fetchone()
                is not None
            ):
                raise RuntimeError("GOVERNANCE_BACKFILL_INTEGRITY_ERROR")
            trigger_type = public_types.get(row[2])
            if trigger_type is None:
                raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
            if (
                not isinstance(row[3], str)
                or not 1 <= len(row[3]) <= 2_000
                or not isinstance(row[4], str)
                or not 1 <= len(row[4]) <= 2_000
                or row[6] not in (0, 1)
                or isinstance(row[7], bool)
                or not isinstance(row[7], int)
            ):
                raise RuntimeError("GOVERNANCE_BACKFILL_INVALID")
            created_at_us = _retained_timestamp_us(row[8])
            state = {
                "trigger_id": public_id,
                "trigger_type": trigger_type,
                "pattern": row[3],
                "recall_query": row[4],
                "categories": _retained_text_list(row[5]),
                "enabled": bool(row[6]),
                "priority": row[7],
                "created_at_us": created_at_us,
                "updated_at_us": created_at_us,
                "deleted_at_us": None,
            }
            store.append_and_project(
                GovernanceEventCommand(
                    workspace_id=workspace_id,
                    stream_id=public_id,
                    stream_kind="trigger",
                    event_type="context_trigger.created",
                    occurred_at_us=created_at_us,
                    recorded_at_us=created_at_us,
                    actor_type="migration",
                    correlation_id=f"migration21:trigger:{source_id}",
                    payload=state,
                    expected_stream_version=1,
                )
            )
            inserted += 1
    return inserted


def _has_retained_public_rows(connection: sqlite3.Connection) -> bool:
    for table in ("rules", "context_triggers", "active_context"):
        if (
            _table_exists(connection, table)
            and connection.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()
            is not None
        ):
            return True
    return False


def run_migrations(
    db_path: str,
    *,
    workspace_id: str | None = None,
    maximum_version: int | None = None,
) -> tuple[int, list[str]]:
    """
    Run all pending migrations on the database.

    Args:
        db_path: Path to the SQLite database.
        maximum_version: Optional inclusive migration ceiling for a retained
            format-specific maintenance path.

    Returns:
        Tuple of (migrations_run, list of descriptions)
    """
    if maximum_version is not None and (
        isinstance(maximum_version, bool)
        or not isinstance(maximum_version, int)
        or maximum_version < 1
    ):
        raise ValueError("maximum_version must be a positive integer")
    if not Path(db_path).exists():
        return 0, ["Database does not exist yet - will be created fresh"]

    conn = sqlite3.Connection(db_path)
    applied = []

    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        current_version = get_current_version(conn)

        for version, description, statements in MIGRATIONS:
            if maximum_version is not None and version > maximum_version:
                continue
            if version <= current_version:
                continue

            if (
                version >= 19
                and workspace_id is None
                and _has_retained_public_rows(conn)
            ):
                raise RuntimeError("WORKSPACE_SCOPE_REQUIRED")

            logger.info(f"Applying migration {version}: {description}")

            try:
                conn.execute("BEGIN")
                for sql in statements:
                    sql = sql.strip()
                    if not sql:
                        continue

                    # Handle ALTER TABLE ADD COLUMN - check if column exists first
                    if "ALTER TABLE" in sql and "ADD COLUMN" in sql:
                        # Parse table and column names
                        parts = sql.split()
                        table_idx = parts.index("TABLE") + 1
                        column_idx = parts.index("COLUMN") + 1
                        table = parts[table_idx]
                        column = parts[column_idx]

                        if check_column_exists(conn, table, column):
                            logger.info(
                                f"  Column {column} already exists in {table}, skipping"
                            )
                            continue

                    try:
                        conn.execute(sql)
                    except sqlite3.OperationalError as e:
                        # Ignore "duplicate column" errors
                        if "duplicate column" in str(e).lower():
                            logger.info("  Column already exists, skipping")
                            continue
                        raise

                # Public identity is part of schema publication, not a later
                # best-effort repair.  A failed/colliding backfill rolls back
                # the mapping DDL and its schema-version marker together.
                if version >= 19 and workspace_id is not None:
                    backfill_retained_public_object_ids(conn, workspace_id)
                if version >= 21 and workspace_id is not None:
                    backfill_retained_governance(conn, workspace_id)

                # Record migration
                conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (version,)
                )
                conn.commit()
                applied.append(f"v{version}: {description}")
            except Exception:
                conn.rollback()
                raise

        v7_public_migrations_enabled = maximum_version is None or maximum_version >= 19
        if (
            v7_public_migrations_enabled
            and workspace_id is None
            and get_current_version(conn) >= 19
            and _has_retained_public_rows(conn)
        ):
            raise RuntimeError("WORKSPACE_SCOPE_REQUIRED")
        if (
            v7_public_migrations_enabled
            and workspace_id is not None
            and get_current_version(conn) >= 19
        ):
            try:
                conn.execute("BEGIN")
                backfill_retained_public_object_ids(conn, workspace_id)
                if get_current_version(conn) >= 21:
                    backfill_retained_governance(conn, workspace_id)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()

    return len(applied), applied


_V6_VECTOR_MIGRATION_ERROR = (
    "Deprecated v6-only vector migration cannot run against format 7; use "
    "`python -m daem0nmcp.cli rebuild-projection --projection dense "
    "--workspace-id <workspace-id>` instead."
)
_LAST_V6_SCHEMA_VERSION = 15


def _active_database_selection(db_path: str) -> tuple[Path, int]:
    """Resolve the storage and active format governing ``db_path``."""

    from ..storage_activation import has_canonical_v7_state, resolve_active_database

    target = Path(db_path).resolve()
    for storage in target.parents:
        pointer = storage / "active-db.json"
        if not pointer.exists() and not pointer.is_symlink():
            continue
        active = resolve_active_database(storage)
        if active.path.resolve() != target:
            raise RuntimeError(
                "Legacy vector migration requires the selected active database."
            )
        return storage, active.format_version
    return target.parent, 7 if has_canonical_v7_state(target) else 6


def migrate_and_backfill_vectors(
    db_path: str,
    *,
    workspace_id: str | None = None,
) -> dict:
    """
    Run migrations and backfill legacy format-6 vector embeddings.

    Args:
        db_path: Path to the SQLite database.

    Returns:
        Migration report
    """
    from ..storage_activation import DatabaseFileLock

    storage, _ = _active_database_selection(db_path)
    with DatabaseFileLock(storage, "shared"):
        locked_storage, format_version = _active_database_selection(db_path)
        if locked_storage.resolve() != storage.resolve():
            raise RuntimeError("Active database storage changed during migration.")
        if format_version != 6:
            raise RuntimeError(_V6_VECTOR_MIGRATION_ERROR)
        return _migrate_and_backfill_vectors_v6(db_path, workspace_id=workspace_id)


def _migrate_and_backfill_vectors_v6(
    db_path: str,
    *,
    workspace_id: str | None = None,
) -> dict:
    """Perform the format-6 backfill while the caller holds the storage lock."""

    from .. import vectors

    # First run schema migrations
    count, applied = run_migrations(
        db_path,
        workspace_id=workspace_id,
        maximum_version=_LAST_V6_SCHEMA_VERSION,
    )

    result = {
        "schema_migrations": count,
        "applied": applied,
        "vectors_backfilled": 0,
        "vectors_available": vectors.is_available(),
    }

    if not vectors.is_available():
        result["message"] = (
            f"Schema updated ({count} migrations). "
            "Vector backfill skipped - install sentence-transformers for vectors."
        )
        return result

    # Backfill vectors for memories that don't have them
    conn = sqlite3.Connection(db_path)
    try:
        cursor = conn.cursor()

        # Find memories without vectors
        cursor.execute("""
            SELECT id, content, rationale
            FROM memories
            WHERE vector_embedding IS NULL
        """)

        memories = cursor.fetchall()

        if not memories:
            result["message"] = (
                f"Schema updated ({count} migrations). All memories already have vectors."
            )
            return result

        logger.info(f"Backfilling vectors for {len(memories)} memories...")

        for mem_id, content, rationale in memories:
            text = content
            if rationale:
                text += " " + rationale

            embedding = vectors.encode_document(text)
            if embedding:
                cursor.execute(
                    "UPDATE memories SET vector_embedding = ? WHERE id = ?",
                    (embedding, mem_id),
                )
                result["vectors_backfilled"] += 1

        conn.commit()

        result["message"] = (
            f"Schema updated ({count} migrations). "
            f"Backfilled vectors for {result['vectors_backfilled']} memories."
        )

    finally:
        conn.close()

    return result


# CLI entry point
def main():
    """Run the deprecated format-6 migration from the command line."""
    from ..config import settings
    from ..storage_activation import resolve_active_database

    storage_path = settings.get_storage_path()
    active = resolve_active_database(storage_path)
    if active.format_version != 6:
        print(_V6_VECTOR_MIGRATION_ERROR)
        return 1

    db_path = str(active.path)
    print(f"Migrating database: {db_path}")

    result = migrate_and_backfill_vectors(db_path)

    print("\nMigration complete:")
    print(f"  Schema migrations: {result['schema_migrations']}")
    for m in result.get("applied", []):
        print(f"    - {m}")
    print(f"  Vectors backfilled: {result['vectors_backfilled']}")
    print(f"  Vectors available: {result['vectors_available']}")
    print(f"\n{result['message']}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
