"""
Daem0nMCP CLI - Command-line interface for memory checks.

Used by pre-commit hooks and direct invocation.

Usage:
    python -m daem0nmcp.cli [--json] [--project-path PATH] <command>

    python -m daem0nmcp.cli check <filepath>
    python -m daem0nmcp.cli briefing
    python -m daem0nmcp.cli scan-todos [--auto-remember] [--path PATH]
    python -m daem0nmcp.cli migrate [--backfill-vectors]
    python -m daem0nmcp.cli pre-commit [--interactive] [--staged-files FILE ...]
    python -m daem0nmcp.cli status
    python -m daem0nmcp.cli record-outcome <memory_id> "<outcome>" --worked|--failed
    python -m daem0nmcp.cli install-hooks [--force]
    python -m daem0nmcp.cli uninstall-hooks
    python -m daem0nmcp.cli install-claude-hooks [--dry-run]
    python -m daem0nmcp.cli uninstall-claude-hooks [--dry-run]
    python -m daem0nmcp.cli install-opencode [--dry-run] [--force]
    python -m daem0nmcp.cli watch [--debounce SECONDS] [--no-system] [--no-log] [--no-poll]
    python -m daem0nmcp.cli index [--path PATH] [--patterns *.py *.ts ...]
    python -m daem0nmcp.cli remember --category CATEGORY --content CONTENT [--rationale TEXT] [--file-path PATH] [--tags TAGS]

Global Options:
    --json              Output as JSON for automation/scripting
    --project-path PATH Specify project root path (sets DAEM0NMCP_PROJECT_ROOT)
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .config import Settings, settings
from .database import DatabaseManager
from .memory import MemoryManager
from .rules import RulesEngine


def safe_print(text: str, file=None) -> None:
    """Print text safely, handling Unicode encoding errors on Windows."""
    output = file or sys.stdout
    try:
        print(text, file=output)
    except UnicodeEncodeError:
        # Replace unencodable characters with ASCII equivalents or ?
        encoding = output.encoding or "utf-8"
        safe_text = text.encode(encoding, errors="replace").decode(
            encoding, errors="replace"
        )
        print(safe_text, file=output)


async def check_file(
    filepath: str, db: DatabaseManager, memory: MemoryManager, rules: RulesEngine
) -> dict:
    """Check a file against memories and rules."""
    await db.init_db()

    results = {
        "file": filepath,
        "warnings": [],
        "blockers": [],
        "must_do": [],
        "must_not": [],
    }

    # Get file-specific memories
    file_memories = await memory.recall_for_file(
        filepath, project_path=settings.project_root
    )

    # Check for warnings in file memories
    for cat in ["warnings", "decisions", "patterns", "learnings"]:
        for mem in file_memories.get(cat, []):
            if mem.get("worked") is False:
                results["warnings"].append(
                    {
                        "type": "FAILED_APPROACH",
                        "content": mem["content"],
                        "outcome": mem.get("outcome"),
                    }
                )
            elif cat == "warnings":
                results["warnings"].append(
                    {"type": "WARNING", "content": mem["content"]}
                )

    # Check rules based on filename
    filename = Path(filepath).name
    rule_check = await rules.check_rules(f"modifying {filename}")

    if rule_check.get("guidance"):
        guidance = rule_check["guidance"]
        results["must_do"] = guidance.get("must_do", [])
        results["must_not"] = guidance.get("must_not", [])

        # Rule warnings become blockers if high priority
        for warning in guidance.get("warnings", []):
            results["warnings"].append({"type": "RULE_WARNING", "content": warning})

    return results


async def get_briefing(db: DatabaseManager, memory: MemoryManager) -> dict:
    """Get session briefing."""
    await db.init_db()
    return await memory.get_statistics()


async def get_enforcement_status(
    db: DatabaseManager, memory: MemoryManager, project_path: str
) -> dict:
    """Get current enforcement status."""
    from datetime import timezone

    from sqlalchemy import func, select

    from .models import Memory

    await db.init_db()

    pending = []
    now = datetime.now(timezone.utc)

    async with db.get_session() as session:
        result = await session.execute(
            select(Memory).where(
                Memory.category == "decision",
                Memory.outcome.is_(None),
                Memory.worked.is_(None),
            )
        )
        for mem in result.scalars().all():
            created = mem.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = now - created

            pending.append(
                {
                    "id": mem.id,
                    "content": mem.content,
                    "age_hours": int(age.total_seconds() / 3600),
                    "created_at": mem.created_at.isoformat()
                    if mem.created_at
                    else None,
                }
            )

        # Get total count
        total_result = await session.execute(select(func.count(Memory.id)))
        total = total_result.scalar() or 0

    return {
        "pending_decisions": pending,
        "total_memories": total,
        "blocking_count": sum(1 for p in pending if p["age_hours"] > 24),
    }


async def record_outcome_cli(
    memory: MemoryManager, memory_id: int, outcome: str, worked: bool
) -> dict:
    """Record outcome via CLI."""
    await memory.db.init_db()

    try:
        result = await memory.record_outcome(
            memory_id=memory_id, outcome=outcome, worked=worked
        )
        return {"success": True, "memory": result}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def run_precommit(
    checker, staged_files: list, project_path: str, interactive: bool, json_output: bool
) -> int:
    """
    Run pre-commit checks and return exit code.

    Args:
        checker: PreCommitChecker instance
        staged_files: List of file paths being committed
        project_path: Project root path
        interactive: If True, prompt user for warnings
        json_output: If True, output JSON instead of human-readable text

    Returns:
        Exit code: 0 (success), 1 (blocked), 2 (user cancelled)
    """
    try:
        await checker.db.init_db()
        result = await checker.check(
            staged_files=staged_files, project_path=project_path
        )
    except Exception as e:
        if json_output:
            print(
                json.dumps(
                    {"error": str(e), "can_commit": False, "blocks": [], "warnings": []}
                )
            )
        else:
            print(f"ERROR: Pre-commit check failed: {e}", file=sys.stderr)
        return 1

    if json_output:
        print(json.dumps(result, default=str))
        return 0 if result["can_commit"] else 1

    # Print blocks
    if result["blocks"]:
        print("BLOCKED: The following issues must be resolved:\n")
        for block in result["blocks"]:
            safe_print(f"  [{block['type']}] {block['message']}")
        print()

    # Print warnings
    if result["warnings"]:
        print("WARNINGS:\n")
        for warn in result["warnings"]:
            safe_print(f"  {warn['message']}")
        print()

    # Clean result
    if not result["blocks"] and not result["warnings"]:
        print("OK: No enforcement issues found.")
        return 0

    # Blocked - must resolve
    if result["blocks"]:
        print("\nCommit blocked. Resolve issues with:")
        print("  python -m daem0nmcp.cli status")
        print(
            '  python -m daem0nmcp.cli record-outcome <id> "<outcome>" --worked|--failed'
        )
        return 1

    # Warnings only - can proceed with confirmation
    if interactive:
        response = (
            input("\nProceed with commit despite warnings? [y/N]: ").strip().lower()
        )
        if response != "y":
            print("Commit cancelled by user.")
            return 2

    return 0


def format_check_result(result: dict) -> str:
    """Format check result for CLI output."""
    lines = []

    if result["warnings"]:
        for w in result["warnings"]:
            lines.append(f"WARNING [{w['type']}]: {w['content']}")
            if w.get("outcome"):
                lines.append(f"  Outcome: {w['outcome']}")

    if result["must_do"]:
        lines.append("MUST DO:")
        for item in result["must_do"]:
            lines.append(f"  - {item}")

    if result["must_not"]:
        lines.append("MUST NOT:")
        for item in result["must_not"]:
            lines.append(f"  - {item}")

    if not lines:
        lines.append("OK: No concerns found")

    return "\n".join(lines)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Daem0nMCP CLI")

    # Global options
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--project-path", help="Project root path")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # check command
    check_parser = subparsers.add_parser("check", help="Check a file against memory")
    check_parser.add_argument("filepath", help="File to check")

    # briefing command
    subparsers.add_parser("briefing", help="Get session briefing")

    # scan-todos command
    scan_parser = subparsers.add_parser(
        "scan-todos", help="Scan for TODO/FIXME comments"
    )
    scan_parser.add_argument(
        "--auto-remember", action="store_true", help="Auto-create warnings"
    )
    scan_parser.add_argument("--path", help="Path to scan", default=".")

    # migrate command
    migrate_parser = subparsers.add_parser("migrate", help="Run database migrations")
    migrate_parser.add_argument(
        "--backfill-vectors",
        action="store_true",
        help="Backfill vector embeddings for existing memories",
    )

    # pre-commit command
    precommit_parser = subparsers.add_parser(
        "pre-commit", help="Run pre-commit enforcement checks"
    )
    precommit_parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Prompt for resolution of warnings",
    )
    precommit_parser.add_argument(
        "--staged-files",
        nargs="*",
        default=None,
        help="Staged files (auto-detected from git if not provided)",
    )

    # status command
    subparsers.add_parser(
        "status", help="Show enforcement status (pending decisions, warnings)"
    )

    # record-outcome command
    record_parser = subparsers.add_parser(
        "record-outcome", help="Record outcome for a decision"
    )
    record_parser.add_argument(
        "memory_id", type=int, help="Memory ID to record outcome for"
    )
    record_parser.add_argument("outcome", help="Description of what happened")
    record_parser.add_argument(
        "--worked", action="store_true", help="The decision worked"
    )
    record_parser.add_argument(
        "--failed", action="store_true", help="The decision failed"
    )

    # install-hooks command
    install_parser = subparsers.add_parser(
        "install-hooks", help="Install git hooks for enforcement"
    )
    install_parser.add_argument(
        "--force", "-f", action="store_true", help="Overwrite existing hooks"
    )

    # uninstall-hooks command
    subparsers.add_parser("uninstall-hooks", help="Remove daem0nmcp git hooks")

    # install-claude-hooks command
    install_claude_parser = subparsers.add_parser(
        "install-claude-hooks", help="Install Claude Code hooks for Daem0n enforcement"
    )
    install_claude_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change"
    )

    # uninstall-claude-hooks command
    uninstall_claude_parser = subparsers.add_parser(
        "uninstall-claude-hooks", help="Remove Daem0n Claude Code hooks"
    )
    uninstall_claude_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change"
    )

    # install-opencode command
    install_oc_parser = subparsers.add_parser(
        "install-opencode",
        help="Install OpenCode integration (creates .opencode/ directory and config)",
    )
    install_oc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be created without making changes",
    )
    install_oc_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Overwrite existing configuration files",
    )

    # watch command
    watch_parser = subparsers.add_parser("watch", help="Start file watcher daemon")
    watch_parser.add_argument(
        "--debounce",
        type=float,
        default=1.0,
        help="Debounce interval in seconds (default: 1.0)",
    )
    watch_parser.add_argument(
        "--no-system", action="store_true", help="Disable system notifications"
    )
    watch_parser.add_argument(
        "--no-log", action="store_true", help="Disable log file channel"
    )
    watch_parser.add_argument(
        "--no-poll", action="store_true", help="Disable editor poll channel"
    )
    watch_parser.add_argument(
        "--extensions",
        nargs="*",
        default=None,
        help="File extensions to watch (e.g., .py .ts)",
    )

    # index command
    index_parser = subparsers.add_parser(
        "index", help="Index code entities for understanding"
    )
    index_parser.add_argument("--path", help="Path to index (default: project root)")
    index_parser.add_argument(
        "--patterns",
        nargs="*",
        default=None,
        help="Glob patterns for files (e.g., **/*.py **/*.ts)",
    )

    # remember command (for hooks to create memories via CLI)
    remember_parser = subparsers.add_parser(
        "remember", help="Create a memory from the command line (for hooks)"
    )
    remember_parser.add_argument(
        "--category",
        required=True,
        choices=["decision", "pattern", "warning", "learning"],
        help="Memory category",
    )
    remember_parser.add_argument("--content", required=True, help="The memory content")
    remember_parser.add_argument(
        "--rationale", default=None, help="Why this is important"
    )
    remember_parser.add_argument(
        "--file-path", default=None, help="Associated file path"
    )
    remember_parser.add_argument("--tags", default=None, help="Comma-separated tags")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Set project path if provided and reload settings
    active_settings = settings
    if args.project_path:
        os.environ["DAEM0NMCP_PROJECT_ROOT"] = args.project_path
        # Create new settings instance to pick up the env var
        active_settings = Settings()

    # --- Commands that do NOT need database/memory initialization ---
    # Handle these before get_storage_path() to avoid creating .daem0nmcp/
    # in the target project directory.

    if args.command == "install-opencode":
        from .opencode_install import install_opencode

        project_path = args.project_path or os.getcwd()
        success, message = install_opencode(
            project_path,
            dry_run=getattr(args, "dry_run", False),
            force=getattr(args, "force", False),
        )
        if args.json:
            print(json.dumps({"success": success, "message": message}))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == "install-hooks":
        from .hooks import install_hooks

        project_path = args.project_path or os.getcwd()
        success, message = install_hooks(project_path, force=args.force)
        if args.json:
            print(json.dumps({"success": success, "message": message}))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == "uninstall-hooks":
        from .hooks import uninstall_hooks

        project_path = args.project_path or os.getcwd()
        success, message = uninstall_hooks(project_path)
        if args.json:
            print(json.dumps({"success": success, "message": message}))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == "install-claude-hooks":
        from .claude_hooks.install import install_claude_hooks

        success, message = install_claude_hooks(dry_run=getattr(args, "dry_run", False))
        if args.json:
            print(json.dumps({"success": success, "message": message}))
        else:
            print(message)
        sys.exit(0 if success else 1)

    elif args.command == "uninstall-claude-hooks":
        from .claude_hooks.install import uninstall_claude_hooks

        success, message = uninstall_claude_hooks(
            dry_run=getattr(args, "dry_run", False)
        )
        if args.json:
            print(json.dumps({"success": success, "message": message}))
        else:
            print(message)
        sys.exit(0 if success else 1)

    # --- Commands that require database/memory initialization ---
    # Initialize components
    storage_path = active_settings.get_storage_path()
    db = DatabaseManager(storage_path)
    memory = MemoryManager(db)
    rules = RulesEngine(db)

    if args.command == "check":
        result = asyncio.run(check_file(args.filepath, db, memory, rules))
        if args.json:
            print(json.dumps(result, default=str))
        else:
            print(format_check_result(result))

    elif args.command == "briefing":
        result = asyncio.run(get_briefing(db, memory))
        if args.json:
            print(json.dumps(result, default=str))
        else:
            print(f"Total memories: {result.get('total_memories', 0)}")
            print(f"By category: {result.get('by_category', {})}")
            if result.get("learning_insights", {}).get("suggestion"):
                safe_print(f"Suggestion: {result['learning_insights']['suggestion']}")

    elif args.command == "scan-todos":
        # Import here to avoid circular imports
        from .server import _scan_for_todos

        todos = _scan_for_todos(args.path)
        if args.json:
            result = {"total": len(todos), "todos": todos}
            print(json.dumps(result, default=str))
        else:
            print(f"Found {len(todos)} TODO/FIXME items:")
            for todo in todos[:20]:  # Limit output
                safe_print(
                    f"  [{todo['type']}] {todo['file']}:{todo['line']} - {todo['content'][:60]}"
                )
            if len(todos) > 20:
                print(f"  ... and {len(todos) - 20} more")

    elif args.command == "migrate":
        from .migrations import migrate_and_backfill_vectors, run_migrations

        db_path = str(Path(storage_path) / "daem0nmcp.db")

        if args.backfill_vectors:
            result = migrate_and_backfill_vectors(db_path)
            if args.json:
                result["database"] = db_path
                print(json.dumps(result, default=str))
            else:
                print(f"Database: {db_path}")
                print("\nMigration complete:")
                print(f"  Schema migrations: {result['schema_migrations']}")
                for m in result.get("applied", []):
                    safe_print(f"    - {m}")
                print(f"  Vectors backfilled: {result['vectors_backfilled']}")
                print(f"  Vectors available: {result['vectors_available']}")
                safe_print(f"\n{result['message']}")
        else:
            count, applied = run_migrations(db_path)
            if args.json:
                result = {
                    "database": db_path,
                    "schema_migrations": count,
                    "applied": applied,
                    "up_to_date": count == 0,
                }
                print(json.dumps(result, default=str))
            else:
                print(f"Database: {db_path}")
                print(f"\nSchema migrations applied: {count}")
                for m in applied:
                    print(f"  - {m}")
                if count == 0:
                    print("Database is up to date.")
                print(
                    "\nTo also backfill vectors, run: python -m daem0nmcp.cli migrate --backfill-vectors"
                )

    elif args.command == "status":
        project_path = args.project_path or os.getcwd()
        try:
            result = asyncio.run(get_enforcement_status(db, memory, project_path))
        except Exception as e:
            if args.json:
                print(
                    json.dumps(
                        {
                            "error": str(e),
                            "pending_decisions": [],
                            "total_memories": 0,
                            "blocking_count": 0,
                        }
                    )
                )
            else:
                print(f"ERROR: Failed to get status: {e}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(json.dumps(result, default=str))
        else:
            print(f"Pending decisions (no outcome): {len(result['pending_decisions'])}")
            for mem in result["pending_decisions"][:10]:
                age = mem.get("age_hours", 0)
                status = "BLOCKING" if age > 24 else "recent"
                safe_print(
                    f"  [{status}] #{mem['id']}: {mem['content'][:60]} ({age}h old)"
                )

            if len(result["pending_decisions"]) > 10:
                print(f"  ... and {len(result['pending_decisions']) - 10} more")

            print(f"\nTotal memories: {result['total_memories']}")
            print(f"Blocking decisions: {result['blocking_count']}")

    elif args.command == "pre-commit":
        import subprocess

        from .enforcement import PreCommitChecker

        # Get staged files from git if not provided
        staged_files = args.staged_files
        if staged_files is None:
            try:
                result = subprocess.run(
                    ["git", "diff", "--cached", "--name-only"],
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=args.project_path or ".",
                )
                staged_files = [
                    f.strip() for f in result.stdout.strip().split("\n") if f.strip()
                ]
            except (subprocess.SubprocessError, FileNotFoundError, OSError):
                staged_files = []

        project_path = args.project_path or os.getcwd()
        checker = PreCommitChecker(db, memory)
        exit_code = asyncio.run(
            run_precommit(
                checker, staged_files, project_path, args.interactive, args.json
            )
        )
        sys.exit(exit_code)

    elif args.command == "record-outcome":
        if not args.worked and not args.failed:
            print("Error: Must specify --worked or --failed", file=sys.stderr)
            sys.exit(1)
        if args.worked and args.failed:
            print("Error: Cannot specify both --worked and --failed", file=sys.stderr)
            sys.exit(1)

        project_path = args.project_path or os.getcwd()
        worked = args.worked
        result = asyncio.run(
            record_outcome_cli(memory, args.memory_id, args.outcome, worked)
        )

        if args.json:
            print(json.dumps(result, default=str))
        else:
            if result.get("success"):
                status = "SUCCESS" if worked else "FAILED"
                print(f"Recorded outcome for memory #{args.memory_id}: {status}")
                safe_print(f"  Outcome: {args.outcome}")
            else:
                safe_print(f"Error: {result.get('error')}", file=sys.stderr)
                sys.exit(1)

    elif args.command == "watch":
        from .channels import EditorPollChannel, LogFileChannel, SystemNotifyChannel
        from .watcher import FileWatcher, LoggingChannel, WatcherConfig

        project_path = Path(args.project_path or os.getcwd()).resolve()

        # Build channel list
        channels = [LoggingChannel()]  # Always log to console

        if not args.no_system:
            channels.append(SystemNotifyChannel())

        if not args.no_log:
            log_path = settings.get_watcher_log_path()
            channels.append(LogFileChannel(log_path))

        if not args.no_poll:
            poll_path = settings.get_watcher_poll_path()
            channels.append(EditorPollChannel(poll_path))

        # Build config
        config = WatcherConfig(
            debounce_seconds=args.debounce, watch_extensions=args.extensions or []
        )

        # Create watcher
        watcher = FileWatcher(
            project_path=project_path,
            memory_manager=memory,
            channels=channels,
            config=config,
        )

        async def run_watcher():
            """Run watcher until interrupted."""
            await db.init_db()

            print(f"Starting file watcher for: {project_path}")
            print(f"Channels: {', '.join(type(c).__name__ for c in channels)}")
            print(f"Debounce: {config.debounce_seconds}s")
            if config.watch_extensions:
                print(f"Extensions: {', '.join(config.watch_extensions)}")
            print("\nPress Ctrl+C to stop...\n")

            await watcher.start()

            try:
                # Run forever until interrupted
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                await watcher.stop()
                stats = watcher.statistics
                print("\nWatcher statistics:")
                print(f"  Files changed: {stats['files_changed']}")
                print(f"  Notifications sent: {stats['notifications_sent']}")
                print(f"  Files debounced: {stats['files_debounced']}")
                print(f"  Files without memories: {stats['files_skipped_no_memories']}")
                print(f"  Errors: {stats['errors']}")

        try:
            asyncio.run(run_watcher())
        except KeyboardInterrupt:
            print("\nStopped.")

    elif args.command == "index":
        from .code_indexer import CodeIndexManager, is_available

        if not is_available():
            if args.json:
                print(
                    json.dumps(
                        {"error": "tree-sitter-languages not installed", "indexed": 0}
                    )
                )
            else:
                print(
                    "ERROR: Code indexing requires tree-sitter-languages",
                    file=sys.stderr,
                )
                print("Install with: pip install tree-sitter-languages")
            sys.exit(1)

        project_path = Path(args.project_path or os.getcwd()).resolve()
        target_path = args.path or str(project_path)

        # Get Qdrant store if available
        qdrant = None
        try:
            from .qdrant_store import QdrantVectorStore

            qdrant = QdrantVectorStore(storage_path=str(storage_path))
        except Exception:
            pass

        indexer = CodeIndexManager(db=db, qdrant=qdrant)

        async def run_indexing():
            await db.init_db()
            return await indexer.index_project(target_path, args.patterns)

        result = asyncio.run(run_indexing())

        if args.json:
            print(json.dumps(result, default=str))
        else:
            print(f"Indexed {result.get('indexed', 0)} code entities")
            print(f"  Files processed: {result.get('files_processed', 0)}")
            print(f"  Files skipped: {result.get('files_skipped', 0)}")
            print(f"  Project: {result.get('project', target_path)}")

            if result.get("error"):
                safe_print(f"\nError: {result['error']}")
                sys.exit(1)

    elif args.command == "remember":
        # Create memory via CLI (for hooks)
        project_path = args.project_path or os.getcwd()
        storage_path_resolved = Path(project_path).resolve() / ".daem0nmcp" / "storage"
        storage_path_resolved.mkdir(parents=True, exist_ok=True)

        # Use dedicated db/memory for this project
        db_remember = DatabaseManager(str(storage_path_resolved))
        memory_remember = MemoryManager(db_remember)

        async def _remember():
            await db_remember.init_db()

            tag_list = [t.strip() for t in args.tags.split(",")] if args.tags else None

            result = await memory_remember.remember(
                category=args.category,
                content=args.content,
                rationale=args.rationale,
                file_path=getattr(args, "file_path", None),
                tags=tag_list,
            )

            return result

        result = asyncio.run(_remember())

        if args.json:
            print(json.dumps(result, default=str))
        else:
            print(f"Memory created: ID {result.get('id')}")


if __name__ == "__main__":
    main()
